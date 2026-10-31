"""Reading and writing bars, without ever merging two sources into one.

The one rule this module exists to keep: a bar arriving twice from the same
source is either the same bar, in which case it is a reconnect replaying and is
dropped, or it is a *different* bar, in which case the source has contradicted
itself and that is a finding, not a row to overwrite.

`live/recorder.py` already applies exactly this rule when folding a day's
journal into Parquet. The rule lives in two places because the two paths are
independent, and a source that contradicts itself should be caught on whichever
one it reaches first.

Writing goes through COPY into a temporary table, then one insert. On the
archive that is 824,304 rows: row-by-row inserts take minutes, this takes
seconds, and more importantly the whole batch lands in one transaction, so a
crash mid-write leaves no half-written day.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading_platform.core.events import Bar
from trading_platform.data.cache import MARKETS
from trading_platform.data.intervals import interval_ms

__all__ = [
    "BarConflict",
    "Source",
    "ensure_source",
    "ingest_run",
    "read_bars",
    "source_coverage",
    "write_bars",
]

SOURCE_KINDS = ("venue", "vendor", "government", "derived")


class BarConflict(ValueError):
    """A source contradicted itself: the same instant, different prices.

    Never resolved automatically. Whichever value is written, the other is lost
    along with the evidence that there was ever a disagreement — and one of the
    two is a bad print worth knowing about.
    """


_CLASH_SQL = """
    SELECT i.symbol, i.ts, b.open, b.high, b.low, b.close, b.volume,
           i.open, i.high, i.low, i.close, i.volume
    FROM incoming i
    JOIN bars b ON b.source_id = i.source_id AND b.market = i.market
               AND b.symbol = i.symbol AND b.interval = i.interval AND b.ts = i.ts
    WHERE (b.open, b.high, b.low, b.close, b.volume)
       IS DISTINCT FROM (i.open, i.high, i.low, i.close, i.volume)
    LIMIT 5
"""

_INTERNAL_CLASH_SQL = """
    SELECT symbol, ts, count(DISTINCT (open, high, low, close, volume)) AS variants
    FROM incoming
    GROUP BY source_id, market, symbol, interval, ts
    HAVING count(DISTINCT (open, high, low, close, volume)) > 1
    LIMIT 5
"""


@contextmanager
def _rollback_on_error(connection: Any) -> Iterator[None]:
    """Leave the connection usable whatever went wrong inside.

    Without this, a check constraint or a dropped connection propagates with
    the transaction open and aborted: the next statement on that connection —
    including `ingest_run`'s "mark this run failed" — raises
    `InFailedSqlTransaction` and buries the real cause in `__context__`. The
    collector then has a dead connection and a run stuck at `running`, which
    reads as a crash when it was a validation failure.
    """
    try:
        yield
    except BaseException:
        with suppress(Exception):  # the connection may already be gone
            connection.rollback()
        raise


def _refuse_positive_exponent(bar: Bar) -> None:
    """NUMERIC has no positive exponent, so `Decimal("1E+2")` comes back as `100`.

    Equal as numbers, different as quotes: the exponent is how many decimal
    places the venue priced to, and this repository treats that as information.
    Venue feeds never send a positive exponent; `Decimal` arithmetic in a
    `derived` source does. Refused rather than normalised, because the round
    trip would then not be a round trip and `scripts/store_load_cache.py` would
    report "precision lost" against a store that stored what it was given.
    """
    for name in ("open", "high", "low", "close", "volume"):
        value: Decimal = getattr(bar, name)
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and exponent > 0:
            raise ValueError(
                f"{bar.symbol} at {bar.timestamp.isoformat()}: {name} {value} has a positive "
                f"exponent, which NUMERIC cannot store — it would come back as "
                f"{value.quantize(Decimal(1))}. Normalise it deliberately "
                f"(Decimal(str(value)) or .quantize()) so the precision you meant is the "
                "precision that is stored."
            )


def _refuse_autocommit(connection: Any, what: str) -> None:
    """An autocommit connection cannot hold the temp table this write needs.

    Said plainly here rather than surfacing later as `UndefinedTable: relation
    "incoming" does not exist`, which names neither the cause nor the fix.
    """
    if getattr(connection, "autocommit", False):
        raise ValueError(
            f"{what} needs a transaction (it stages rows in a temporary table and "
            "rolls the whole batch back on a contradiction); this connection is in "
            "autocommit mode. Set connection.autocommit = False."
        )


def _refuse_internal_contradiction(cursor: Any, source: Source) -> None:
    """Two rows in one batch, same instant, different prices."""
    cursor.execute(_INTERNAL_CLASH_SQL)
    rows = cursor.fetchall()
    if not rows:
        return
    symbol, ts, variants = rows[0]
    raise BarConflict(
        f"this batch reports {symbol} at {ts.isoformat()} {variants} different ways, from "
        f"{source.name}. Writing it would keep whichever row arrived first and drop the "
        f"rest with no record; {len(rows)} instant(s) in this batch are like this. "
        "Nothing was written."
    )


def _refuse_clash(cursor: Any, source: Source, when: str) -> None:
    """This batch against what is stored. Run before the insert and after it."""
    cursor.execute(_CLASH_SQL)
    clashes = cursor.fetchall()
    if not clashes:
        return
    symbol, ts, *values = clashes[0]
    stored, incoming = values[:5], values[5:]
    raced = (
        " Another writer committed it between this batch's check and its insert, so the "
        "two disagree about a bar neither of them wrote alone."
        if when == "after"
        else ""
    )
    raise BarConflict(
        f"{source.name} has already reported {symbol} at {ts.isoformat()} as "
        f"OHLCV {stored}, and now reports {incoming}. One of them is wrong; "
        f"{len(clashes)} clash(es) in this batch. Nothing was written.{raced}"
    )


@dataclass(frozen=True, slots=True)
class Source:
    id: int
    name: str
    kind: str
    venue: str | None
    description: str


def ensure_source(
    connection: Any,
    name: str,
    *,
    kind: str,
    venue: str | None = None,
    description: str = "",
) -> Source:
    """Get the source by name, creating it if new. Refuses to change an existing one.

    A source's kind and venue are part of what its rows mean. Changing them
    would silently restate every bar already attributed to it, so this returns
    the stored row and raises if the caller asked for different terms.
    """
    if kind not in SOURCE_KINDS:
        raise ValueError(f"kind must be one of {SOURCE_KINDS}, got {kind!r}")

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, name, kind, venue, description FROM sources WHERE name = %s", (name,)
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "INSERT INTO sources (name, kind, venue, description) VALUES (%s, %s, %s, %s) "
                "RETURNING id, name, kind, venue, description",
                (name, kind, venue, description),
            )
            row = cursor.fetchone()
            connection.commit()
            return Source(*row)

    existing = Source(*row)
    if existing.kind != kind or existing.venue != venue:
        raise ValueError(
            f"source {name!r} is already recorded as kind={existing.kind!r} "
            f"venue={existing.venue!r}; asked for kind={kind!r} venue={venue!r}. "
            "Changing these restates every bar already attributed to it — use a new name."
        )
    return existing


@contextmanager
def ingest_run(
    connection: Any,
    source: Source,
    *,
    kind: str,
    params: dict[str, Any] | None = None,
) -> Iterator[int]:
    """Open a run, close it on the way out, and mark it failed if the body raised.

    Committed on entry so the run is visible while it is still running. A run
    that never closes is a crash, and looks like one: `status = 'running'` with
    no `finished_at`.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO ingest_runs (source_id, kind, params) VALUES (%s, %s, %s) RETURNING id",
            (source.id, kind, json.dumps(params or {})),
        )
        run_id = int(cursor.fetchone()[0])
    connection.commit()

    try:
        yield run_id
    except BaseException as exc:
        # Roll back first. If the body failed at the database, the transaction
        # is aborted and this UPDATE would raise `InFailedSqlTransaction`,
        # replacing the real cause and leaving the run stuck at `running` —
        # which the "a run left running is a crash" query then misreports.
        with suppress(Exception):
            connection.rollback()
        with connection.cursor() as cursor:
            # `rows_written` is counted on this path too. A run is not a
            # transaction: batches that committed before the failure are still
            # in `bars`, and a failed run claiming it wrote nothing is a lie
            # about data that is right there referencing it.
            cursor.execute(
                "UPDATE ingest_runs SET status = 'failed', finished_at = now(), error = %s, "
                "rows_written = (SELECT count(*) FROM bars WHERE ingest_run_id = %s) "
                "WHERE id = %s",
                (f"{type(exc).__name__}: {exc}"[:2000], run_id, run_id),
            )
        connection.commit()
        raise
    else:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE ingest_runs SET status = 'ok', finished_at = now(), "
                "rows_written = (SELECT count(*) FROM bars WHERE ingest_run_id = %s) "
                "WHERE id = %s",
                (run_id, run_id),
            )
        connection.commit()


def heartbeat(connection: Any, run_id: int, stats: Mapping[str, int]) -> None:
    """Say the process is still alive, and what its counters read.

    Called on a timer by a long-running capture. Without it a run sits at
    `status = 'running'` and looks identical whether it is capturing a bar a
    minute or failing every single poll — the collector prints its counters at
    exit, and a collector that runs for a month never exits (T-f280e8f0).

    Deliberately best-effort at the CALL SITE, not here: a heartbeat that raises
    would kill the capture it was added to observe, which is the R06 defect
    again (the tracker crashed the session it was watching). It commits on its
    own so the number is visible to the operations page immediately.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE ingest_runs SET heartbeat_at = now(), stats = %s WHERE id = %s",
            (json.dumps(dict(stats)), run_id),
        )
    connection.commit()


def write_bars(
    connection: Any,
    source: Source,
    run_id: int,
    *,
    market: str,
    interval: str,
    bars: Sequence[Bar],
) -> int:
    """Write bars for one market and interval. Returns how many rows were new.

    Bars already present with identical values are skipped and not counted: a
    reconnect replaying the last minute is normal and is not an error. A bar
    present with *different* values raises `BarConflict`.
    """
    if market not in MARKETS:
        raise ValueError(f"unknown market {market!r}; valid: {sorted(MARKETS)}")
    interval_ms(interval)  # raises UnknownIntervalError on a made-up interval
    if not bars:
        return 0
    for bar in bars:
        if bar.timestamp.tzinfo is None:
            raise ValueError(f"bar timestamps must be timezone-aware, got {bar.timestamp!r}")
        _refuse_positive_exponent(bar)
    _refuse_autocommit(connection, "write_bars")

    with _rollback_on_error(connection), connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMPORARY TABLE incoming (LIKE bars INCLUDING DEFAULTS) ON COMMIT DROP"
        )
        with cursor.copy(
            "COPY incoming (source_id, market, symbol, interval, ts, open, high, low, close, "
            "volume, ingest_run_id) FROM STDIN"
        ) as copy:
            for bar in bars:
                copy.write_row(
                    (
                        source.id,
                        market,
                        bar.symbol,
                        interval,
                        bar.timestamp,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        run_id,
                    )
                )

        # A batch that contradicts ITSELF, before anything is compared to what
        # is stored. `ON CONFLICT DO NOTHING` would otherwise keep whichever
        # copy COPY happened to deliver first and drop the other silently, so
        # the result would depend on row order and no one would be told.
        _refuse_internal_contradiction(cursor, source)

        # Then contradictions with what is stored: if this batch disagrees with
        # the store, the write does not happen at all. Reported with the symbol
        # and instant so the disagreement can be looked at rather than guessed at.
        _refuse_clash(cursor, source, "before")

        cursor.execute(
            """
            INSERT INTO bars (source_id, market, symbol, interval, ts, open, high, low, close,
                              volume, ingest_run_id)
            SELECT source_id, market, symbol, interval, ts, open, high, low, close,
                   volume, ingest_run_id
            FROM incoming
            ON CONFLICT (source_id, market, symbol, interval, ts) DO NOTHING
            """
        )
        written = cursor.rowcount

        # And again, AFTER the insert. This is the check that matters under the
        # workload this store was built for. Another writer on another
        # connection can commit a contradicting row in the window between the
        # check above and this insert; our insert then blocks on its primary
        # key, wakes to find the row present, and DOES NOTHING — silently
        # discarding our value and returning a count that looks like an
        # ordinary replay. Under READ COMMITTED this second SELECT takes a
        # fresh snapshot and sees what they committed, so the merge becomes a
        # `BarConflict` instead of a quiet loss. Measured before the fix: 299
        # of 300 concurrent contradictions were lost without an exception.
        _refuse_clash(cursor, source, "after")
    connection.commit()
    return int(written)


def read_bars(
    connection: Any,
    source: Source | str,
    *,
    market: str,
    symbol: str,
    interval: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Bar]:
    """One source's bars for one symbol, oldest first, inclusive of both bounds.

    One source, deliberately: a function that read several would have to decide
    what to do when they disagree, and that decision does not belong hidden
    inside a reader.
    """
    if market not in MARKETS:
        raise ValueError(f"unknown market {market!r}; valid: {sorted(MARKETS)}")
    interval_ms(interval)  # raises UnknownIntervalError on a made-up interval
    for bound, label in ((start, "start"), (end, "end")):
        if bound is not None and bound.tzinfo is None:
            raise ValueError(f"{label} must be timezone-aware, got {bound!r}")

    name = source if isinstance(source, str) else source.name
    if isinstance(source, str):
        # A misspelled source name would otherwise return `[]`, which is
        # indistinguishable from "this source has no bars for that symbol" —
        # the exact shape of silent wrongness this repository refuses.
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM sources WHERE name = %s", (name,))
            if cursor.fetchone() is None:
                cursor.execute("SELECT name FROM sources ORDER BY name")
                known = [row[0] for row in cursor.fetchall()]
                raise ValueError(f"no source named {name!r}; known sources: {known}")

    clauses = [
        "s.name = %s",
        "b.market = %s",
        "b.symbol = %s",
        "b.interval = %s",
    ]
    params: list[Any] = [name, market, symbol, interval]
    if start is not None:
        clauses.append("b.ts >= %s")
        params.append(start)
    if end is not None:
        clauses.append("b.ts <= %s")
        params.append(end)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT b.ts, b.open, b.high, b.low, b.close, b.volume FROM bars b "
            "JOIN sources s ON s.id = b.source_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY b.ts",
            params,
        )
        rows = cursor.fetchall()

    return [
        Bar(
            symbol=symbol,
            timestamp=ts.astimezone(UTC),
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(low),
            close=Decimal(c),
            volume=Decimal(v),
        )
        for ts, o, h, low, c, v in rows
    ]


def source_coverage(connection: Any) -> list[dict[str, Any]]:
    """What every source has delivered: rows, first bar, last bar, last written."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT source, market, symbol, interval, bars, first_bar, last_bar, last_written "
            "FROM source_coverage ORDER BY source, market, symbol, interval"
        )
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
