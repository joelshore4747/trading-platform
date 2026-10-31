"""The store: migrations that refuse edited history, and bars that never merge sources.

These tests need a real Postgres. They are skipped without one, and a skip is
not a pass — `scripts/dev_postgres.sh start` then
`eval "$(scripts/dev_postgres.sh env)"` makes them run, and CI sets the same
variable. The guard that stops a skipped suite passing for a green one is in
`tests/test_store_guard.py`, deliberately in another file: it must not be
skipped by the mark below, and when it lived here it was.

Each test runs in its own schema, so the migrations execute for real against a
genuinely empty database rather than against tables a previous test left behind.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trading_platform.core.events import Bar
from trading_platform.data.intervals import UnknownIntervalError
from trading_platform.store.bars import (
    BarConflict,
    ensure_source,
    heartbeat,
    ingest_run,
    read_bars,
    source_coverage,
    write_bars,
)
from trading_platform.store.migrate import (
    MIGRATIONS_DIR,
    MigrationError,
    applied_migrations,
    discover,
    migrate,
    pending_migrations,
)

TEST_DSN_VAR = "TRADING_PLATFORM_TEST_DSN"
T0 = datetime(2026, 8, 1, tzinfo=UTC)

psycopg = pytest.importorskip("psycopg", reason="the store needs psycopg")


def _dsn() -> str | None:
    return os.environ.get(TEST_DSN_VAR) or None


def _reachable(dsn: str) -> bool:
    try:
        psycopg.connect(dsn).close()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    _dsn() is None or not _reachable(_dsn() or ""),
    reason=f'set {TEST_DSN_VAR} — scripts/dev_postgres.sh start && eval "$(scripts/dev_postgres.sh env)"',
)


@pytest.fixture
def conn(request: pytest.FixtureRequest) -> Iterator[Any]:
    """A connection whose search_path is a schema of its own, dropped after."""
    dsn = _dsn()
    assert dsn is not None
    schema = "t_" + str(abs(hash(request.node.nodeid)) % 10**12)
    connection = psycopg.connect(dsn)
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{schema}"')
        cursor.execute(f'SET search_path TO "{schema}"')
    connection.commit()
    try:
        yield connection
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA "{schema}" CASCADE')
        connection.commit()
        connection.close()


def bar(minute: int, close: str = "100.50", symbol: str = "BTCUSDT") -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=T0 + timedelta(minutes=minute),
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal(close),
        volume=Decimal("12.25000000"),
    )


def setup_source(conn: Any, name: str = "binance-um") -> tuple[Any, int]:
    migrate(conn)
    source = ensure_source(conn, name, kind="venue", venue="binance")
    with ingest_run(conn, source, kind="test") as run_id:
        return source, run_id


# -- migrations --------------------------------------------------------------------------


def test_migrations_apply_from_empty_and_are_idempotent(conn: Any) -> None:
    """Mutation: have `pending_migrations` ignore what is already applied — the
    second call would re-run 0001 and CREATE TABLE would fail."""
    first = migrate(conn)
    assert [str(m) for m in first] == [str(m) for m in discover()]
    assert migrate(conn) == [], "a second run must apply nothing"

    applied = applied_migrations(conn)
    assert set(applied) == {m.version for m in discover()}

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() ORDER BY table_name"
        )
        tables = {row[0] for row in cursor.fetchall()}
    assert {"sources", "ingest_runs", "bars", "schema_migrations"} <= tables


def test_editing_an_applied_migration_is_refused(conn: Any, tmp_path: Path) -> None:
    """The defect this exists for: a migration edited after it ran leaves the
    database and the repository describing different schemas, and nothing else
    notices. Mutation: drop the checksum comparison in `pending_migrations`."""
    original = tmp_path / "0001_spine.sql"
    original.write_text("CREATE TABLE only_this (id INT);")
    migrate(conn, tmp_path)

    original.write_text("CREATE TABLE only_this (id INT, added TEXT);")
    with pytest.raises(MigrationError, match="has changed since"):
        pending_migrations(conn, tmp_path)


def test_renaming_an_applied_migration_is_refused(conn: Any, tmp_path: Path) -> None:
    body = "CREATE TABLE only_this (id INT);"
    (tmp_path / "0001_spine.sql").write_text(body)
    migrate(conn, tmp_path)
    (tmp_path / "0001_spine.sql").unlink()
    (tmp_path / "0001_renamed.sql").write_text(body)
    with pytest.raises(MigrationError, match="ran as"):
        pending_migrations(conn, tmp_path)


def test_a_gap_in_the_numbering_is_refused(tmp_path: Path) -> None:
    """A missing 2 is the trace of a migration someone dropped on a merge.
    Mutation: delete the `versions != list(range(...))` check."""
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="no gaps"):
        discover(tmp_path)


def test_a_database_ahead_of_the_checkout_is_refused(conn: Any, tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE a (id INT);")
    (tmp_path / "0002_b.sql").write_text("CREATE TABLE b (id INT);")
    migrate(conn, tmp_path)
    (tmp_path / "0002_b.sql").unlink()
    with pytest.raises(MigrationError, match="ahead of this checkout"):
        pending_migrations(conn, tmp_path)


def test_a_bad_filename_is_refused(tmp_path: Path) -> None:
    (tmp_path / "add-stuff.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="not a migration filename"):
        discover(tmp_path)


def test_the_shipped_migrations_are_wellformed() -> None:
    found = discover()
    assert found, f"no migrations in {MIGRATIONS_DIR}"
    assert [m.version for m in found] == list(range(1, len(found) + 1))


# -- sources -----------------------------------------------------------------------------


def test_a_source_is_created_once_and_returned_after(conn: Any) -> None:
    migrate(conn)
    first = ensure_source(conn, "binance-um", kind="venue", venue="binance")
    again = ensure_source(conn, "binance-um", kind="venue", venue="binance")
    assert first == again
    assert first.id == again.id


def test_changing_a_sources_terms_is_refused(conn: Any) -> None:
    """A source's kind and venue are part of what its rows mean; changing them
    restates every bar already attributed to it. Mutation: return the existing
    row without comparing kind and venue."""
    migrate(conn)
    ensure_source(conn, "feed", kind="venue", venue="binance")
    with pytest.raises(ValueError, match="already recorded"):
        ensure_source(conn, "feed", kind="vendor", venue="binance")
    with pytest.raises(ValueError, match="already recorded"):
        ensure_source(conn, "feed", kind="venue", venue="coinbase")


def test_an_unknown_source_kind_is_refused(conn: Any) -> None:
    migrate(conn)
    with pytest.raises(ValueError, match="kind must be"):
        ensure_source(conn, "feed", kind="guesswork")


# -- ingest runs -------------------------------------------------------------------------


def test_a_run_records_its_rows_and_closes_ok(conn: Any) -> None:
    migrate(conn)
    source = ensure_source(conn, "binance-um", kind="venue", venue="binance")
    with ingest_run(conn, source, kind="backfill", params={"symbols": ["BTCUSDT"]}) as run_id:
        write_bars(conn, source, run_id, market="um", interval="1m", bars=[bar(1), bar(2)])

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT status, rows_written, error, finished_at IS NOT NULL, params "
            "FROM ingest_runs WHERE id = %s",
            (run_id,),
        )
        status, rows, error, finished, params = cursor.fetchone()
    assert (status, rows, error, finished) == ("ok", 2, "", True)
    assert params == {"symbols": ["BTCUSDT"]}


def test_a_run_that_raises_is_marked_failed_with_the_reason(conn: Any) -> None:
    """Mutation: drop the `except` clause in `ingest_run` — the run would stay
    `running` forever and look like a crash rather than a failure."""
    migrate(conn)
    source = ensure_source(conn, "binance-um", kind="venue", venue="binance")
    with (
        pytest.raises(RuntimeError, match="feed died"),
        ingest_run(conn, source, kind="live") as run_id,
    ):
        raise RuntimeError("feed died")

    with conn.cursor() as cursor:
        cursor.execute("SELECT status, error FROM ingest_runs WHERE id = %s", (run_id,))
        status, error = cursor.fetchone()
    assert status == "failed"
    assert "feed died" in error


# -- bars: the rule that sources are never merged ----------------------------------------


def test_two_sources_reporting_the_same_bar_are_two_rows(conn: Any) -> None:
    """The whole reason for the schema. Mutation: drop `source_id` from the
    primary key of `bars` — the second write would be swallowed by ON CONFLICT
    and the disagreement between the feeds would be gone."""
    migrate(conn)
    binance = ensure_source(conn, "binance-um", kind="venue", venue="binance")
    polygon = ensure_source(conn, "polygon", kind="vendor", venue="binance")

    with ingest_run(conn, binance, kind="live") as run:
        assert (
            write_bars(conn, binance, run, market="um", interval="1m", bars=[bar(1, "100.50")]) == 1
        )
    with ingest_run(conn, polygon, kind="live") as run:
        assert (
            write_bars(conn, polygon, run, market="um", interval="1m", bars=[bar(1, "100.51")]) == 1
        )

    assert read_bars(conn, binance, market="um", symbol="BTCUSDT", interval="1m")[
        0
    ].close == Decimal("100.50")
    assert read_bars(conn, polygon, market="um", symbol="BTCUSDT", interval="1m")[
        0
    ].close == Decimal("100.51")

    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM bars")
        assert cursor.fetchone()[0] == 2


def test_a_source_contradicting_itself_raises_and_writes_nothing(conn: Any) -> None:
    """Mutation: delete the clash query and keep only ON CONFLICT DO NOTHING —
    the contradiction would be silently dropped and the first value kept."""
    migrate(conn)
    source, _ = setup_source(conn)

    with ingest_run(conn, source, kind="live") as run:
        write_bars(conn, source, run, market="um", interval="1m", bars=[bar(1, "100.50")])

    with (
        ingest_run(conn, source, kind="live") as run,
        pytest.raises(BarConflict, match="has already reported"),
    ):
        # A different close, still a valid bar in its own right — the
        # contradiction has to be one the engine's own validation would let
        # through, or this would be testing `Bar.__post_init__` instead.
        write_bars(
            conn,
            source,
            run,
            market="um",
            interval="1m",
            bars=[bar(1, "100.75"), bar(2, "100.00")],
        )

    stored = read_bars(conn, source, market="um", symbol="BTCUSDT", interval="1m")
    assert [b.close for b in stored] == [Decimal("100.50")], "the whole batch must be refused"


def test_an_identical_replay_is_skipped_not_an_error(conn: Any) -> None:
    """A reconnect replaying the last minute is normal. Mutation: drop the
    ON CONFLICT clause — this would raise a UniqueViolation."""
    migrate(conn)
    source, _ = setup_source(conn)
    with ingest_run(conn, source, kind="live") as run:
        assert write_bars(conn, source, run, market="um", interval="1m", bars=[bar(1), bar(2)]) == 2
    with ingest_run(conn, source, kind="live") as run:
        assert (
            write_bars(conn, source, run, market="um", interval="1m", bars=[bar(1), bar(2), bar(3)])
            == 1
        )


def test_decimal_precision_survives_the_round_trip(conn: Any) -> None:
    """NUMERIC, not double precision. Mutation: change the price columns to
    `double precision` in 0001 — 0.1 + 0.2 stops being 0.3 and the trailing
    zeros in 12.25000000 are lost."""
    migrate(conn)
    source, _ = setup_source(conn)
    awkward = Bar(
        symbol="BTCUSDT",
        timestamp=T0,
        open=Decimal("0.10"),
        high=Decimal("0.30"),
        low=Decimal("0.10"),
        close=Decimal("0.30"),
        volume=Decimal("12.25000000"),
    )
    with ingest_run(conn, source, kind="test") as run:
        write_bars(conn, source, run, market="um", interval="1m", bars=[awkward])

    (stored,) = read_bars(conn, source, market="um", symbol="BTCUSDT", interval="1m")
    assert stored.open == Decimal("0.10")
    assert stored.close + stored.open == Decimal("0.40")
    assert str(stored.volume) == "12.25000000", "trailing zeros carry the venue's precision"
    assert stored.timestamp == T0 and stored.timestamp.tzinfo is not None


def test_bars_are_read_back_in_time_order_and_windowed(conn: Any) -> None:
    migrate(conn)
    source, _ = setup_source(conn)
    with ingest_run(conn, source, kind="test") as run:
        write_bars(conn, source, run, market="um", interval="1m", bars=[bar(3), bar(1), bar(2)])

    everything = read_bars(conn, source, market="um", symbol="BTCUSDT", interval="1m")
    assert [b.timestamp for b in everything] == [T0 + timedelta(minutes=i) for i in (1, 2, 3)]

    windowed = read_bars(
        conn,
        source,
        market="um",
        symbol="BTCUSDT",
        interval="1m",
        start=T0 + timedelta(minutes=2),
        end=T0 + timedelta(minutes=2),
    )
    assert [b.timestamp for b in windowed] == [T0 + timedelta(minutes=2)], "bounds are inclusive"


def test_a_naive_timestamp_is_refused(conn: Any) -> None:
    migrate(conn)
    source, run = setup_source(conn)
    naive = Bar(
        symbol="BTCUSDT",
        timestamp=datetime(2026, 8, 1, 0, 1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        write_bars(conn, source, run, market="um", interval="1m", bars=[naive])


def test_an_unknown_market_or_interval_is_refused(conn: Any) -> None:
    migrate(conn)
    source, run = setup_source(conn)
    with pytest.raises(ValueError, match="unknown market"):
        write_bars(conn, source, run, market="commodities", interval="1m", bars=[bar(1)])
    with pytest.raises(UnknownIntervalError):
        write_bars(conn, source, run, market="um", interval="7m", bars=[bar(1)])


def test_coverage_reports_what_each_source_delivered(conn: Any) -> None:
    migrate(conn)
    source, _ = setup_source(conn)
    with ingest_run(conn, source, kind="test") as run:
        write_bars(conn, source, run, market="um", interval="1m", bars=[bar(1), bar(2), bar(3)])

    (row,) = source_coverage(conn)
    assert row["source"] == "binance-um"
    assert row["bars"] == 3
    assert row["first_bar"] == T0 + timedelta(minutes=1)
    assert row["last_bar"] == T0 + timedelta(minutes=3)


# -- concurrency: the workload the store exists for --------------------------------------
#
# Every test above this line uses one connection on one thread. ADR 0010 justifies
# the store by the arrival of MANY CONCURRENT WRITERS, and an adversarial review
# found that the no-merge rule held only for the single-writer case: of 300 races
# where two connections reported the same instant differently, 299 ended with one
# writer silently discarded and no exception. These tests are that case.


class _HookedCursor:
    """A cursor that runs a callback before a chosen statement. Test scaffolding."""

    def __init__(self, inner: Any, hook: Any) -> None:
        self._inner = inner
        self._hook = hook

    def execute(self, query: Any, params: Any = None) -> Any:
        self._hook(str(query))
        return (
            self._inner.execute(query, params) if params is not None else self._inner.execute(query)
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __enter__(self) -> _HookedCursor:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.__exit__(*exc)


class _HookedConnection:
    def __init__(self, inner: Any, hook: Any) -> None:
        self._inner = inner
        self._hook = hook

    def cursor(self) -> _HookedCursor:
        return _HookedCursor(self._inner.cursor(), self._hook)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _second_connection(conn: Any) -> Any:
    """Another backend pointed at the same private schema as the fixture's."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        schema = cursor.fetchone()[0]
    dsn = _dsn()
    assert dsn is not None
    other = psycopg.connect(dsn)
    with other.cursor() as cursor:
        cursor.execute(f'SET search_path TO "{schema}"')
    other.commit()
    return other


def test_a_second_writer_committing_mid_write_raises_rather_than_losing_a_value(
    conn: Any,
) -> None:
    """The race, made deterministic: writer B is paused just before its INSERT,
    writer A commits a different close for the same instant, then B resumes.
    B's insert finds the row present and does nothing, so without the check that
    runs AFTER the insert, B returns 0 — indistinguishable from a replay — and
    B's value is gone with no record that two sources ever disagreed.

    Mutation: delete the `_refuse_clash(cursor, source, "after")` call in
    `write_bars`. This test then sees B return 0 and go green on the assertion
    that no exception was raised, which is why it asserts the stored value and
    the exception, not just the count."""
    source, run_id = setup_source(conn)
    other = _second_connection(conn)
    try:
        fired = []

        def hook(query: str) -> None:
            if "INSERT INTO bars" in query and not fired:
                fired.append(query)
                other_source = ensure_source(other, source.name, kind="venue", venue="binance")
                with ingest_run(other, other_source, kind="race") as other_run:
                    assert (
                        write_bars(
                            other,
                            other_source,
                            other_run,
                            market="um",
                            interval="1m",
                            bars=[bar(1, "100.50")],
                        )
                        == 1
                    )

        hooked = _HookedConnection(conn, hook)
        with pytest.raises(BarConflict) as caught:
            write_bars(hooked, source, run_id, market="um", interval="1m", bars=[bar(1, "100.75")])
        assert fired, "the hook never fired; this test did not exercise the race"
        assert "100.50" in str(caught.value) and "100.75" in str(caught.value)
    finally:
        other.close()

    # The winner's value stands, alone and intact. The loser's is not in the
    # store and, crucially, its writer was told.
    stored = read_bars(conn, source, market="um", symbol="BTCUSDT", interval="1m")
    assert [str(b.close) for b in stored] == ["100.50"]


def test_a_batch_that_contradicts_itself_raises_whichever_order_it_arrives_in(
    conn: Any,
) -> None:
    """Two rows, one instant, different closes, in a single call. `ON CONFLICT
    DO NOTHING` keeps whichever COPY delivered first, so the surviving price
    depended on row order and nobody was told. A feed emitting a correction in
    the same poll window does exactly this.

    Mutation: delete `_refuse_internal_contradiction`. Both orders then write
    one row and return 1, and the two assertions below disagree with each other."""
    source, run_id = setup_source(conn)
    for batch in ([bar(1, "100.50"), bar(1, "100.75")], [bar(1, "100.75"), bar(1, "100.50")]):
        with pytest.raises(BarConflict, match="2 different ways"):
            write_bars(conn, source, run_id, market="um", interval="1m", bars=batch)
        assert read_bars(conn, source, market="um", symbol="BTCUSDT", interval="1m") == []


def test_an_identical_bar_twice_in_one_batch_is_not_a_contradiction(conn: Any) -> None:
    """The other half: a repeat that agrees is a poll window overlapping itself,
    which is normal. Mutation: make the internal check fire on any duplicate
    key rather than on differing values — this goes red."""
    source, run_id = setup_source(conn)
    assert (
        write_bars(conn, source, run_id, market="um", interval="1m", bars=[bar(1), bar(1), bar(2)])
        == 2
    )


# -- failure paths leave the connection and the record usable ----------------------------


def test_a_database_error_rolls_back_and_names_itself(conn: Any) -> None:
    """A check constraint failing used to leave the transaction aborted, so
    `ingest_run`'s "mark this failed" UPDATE raised `InFailedSqlTransaction`
    over the top of the real cause and the run stayed at `running` — reported
    by SCHEMA.md's query as a crash. Mutation: remove `_rollback_on_error` from
    `write_bars`; the run below is left `running` and the type is wrong."""
    source, _ = setup_source(conn)
    # `pytest.raises` outermost, so the exception reaches `ingest_run.__exit__`
    # rather than being swallowed inside the run's body.
    with pytest.raises(Exception) as caught, ingest_run(conn, source, kind="test") as run_id:
        write_bars(
            conn,
            source,
            run_id,
            market="um",
            interval="1m",
            bars=[bar(1, symbol=" BTCUSDT")],  # leading space: bars_symbol_shape refuses it
        )
    assert "InFailedSqlTransaction" not in type(caught.value).__name__

    with conn.cursor() as cursor:  # the connection is still usable
        cursor.execute("SELECT status, finished_at FROM ingest_runs ORDER BY id DESC LIMIT 1")
        status, finished = cursor.fetchone()
    assert status == "failed" and finished is not None


def test_a_failed_run_reports_the_rows_it_had_already_written(conn: Any) -> None:
    """A run is not a transaction: batches committed before the failure stay.
    A failed run claiming `rows_written = 0` over two bars that reference it is
    a lie about data sitting right there. Mutation: drop the `rows_written`
    subquery from the failure-path UPDATE — this goes red."""
    source, _ = setup_source(conn)
    with pytest.raises(RuntimeError), ingest_run(conn, source, kind="test") as run_id:
        write_bars(conn, source, run_id, market="um", interval="1m", bars=[bar(1), bar(2)])
        raise RuntimeError("the fetch died after two bars landed")

    with conn.cursor() as cursor:
        cursor.execute("SELECT status, rows_written FROM ingest_runs ORDER BY id DESC LIMIT 1")
        status, rows_written = cursor.fetchone()
        cursor.execute("SELECT count(*) FROM bars")
        actually_there = cursor.fetchone()[0]
    assert status == "failed"
    assert rows_written == actually_there == 2


def test_write_bars_refuses_an_autocommit_connection(conn: Any) -> None:
    """Autocommit drops the `ON COMMIT DROP` staging table between statements,
    which surfaced as `UndefinedTable: relation "incoming" does not exist` —
    a message naming neither the cause nor the fix. Mutation: delete
    `_refuse_autocommit` and this raises the wrong error type."""
    source, run_id = setup_source(conn)

    class _Autocommit:
        autocommit = True

        def __getattr__(self, name: str) -> Any:
            return getattr(conn, name)

    with pytest.raises(ValueError, match="autocommit"):
        write_bars(_Autocommit(), source, run_id, market="um", interval="1m", bars=[bar(1)])


# -- a typo must not look like an empty market -------------------------------------------


def test_read_bars_refuses_what_it_cannot_find(conn: Any) -> None:
    """Every one of these used to return `[]`, which is indistinguishable from
    "that source has no bars there" — the exact shape of silent wrongness this
    repository exists to refuse. Mutation: drop the validation block at the top
    of `read_bars` and all four raises become empty lists."""
    source, run_id = setup_source(conn)
    write_bars(conn, source, run_id, market="um", interval="1m", bars=[bar(1)])

    with pytest.raises(ValueError, match="no source named"):
        read_bars(conn, "binance-spot", market="um", symbol="BTCUSDT", interval="1m")
    with pytest.raises(ValueError, match="unknown market"):
        read_bars(conn, source, market="commodities", symbol="BTCUSDT", interval="1m")
    with pytest.raises(UnknownIntervalError):
        read_bars(conn, source, market="um", symbol="BTCUSDT", interval="7m")
    with pytest.raises(ValueError, match="timezone-aware"):
        read_bars(
            conn,
            source,
            market="um",
            symbol="BTCUSDT",
            interval="1m",
            start=datetime(2026, 8, 1),  # naive on purpose
        )


def test_write_bars_refuses_a_price_numeric_cannot_hold(conn: Any) -> None:
    """`Decimal("1E+2")` is a legal price and NUMERIC stores it as `100` — equal
    as a number, different as a quote, and the round trip silently stops being
    one. Venue feeds never produce it; `Decimal` arithmetic in a derived source
    does. Mutation: delete `_refuse_positive_exponent` — this writes, and the
    read-back below returns `100` where `1E+2` went in."""
    source, run_id = setup_source(conn)
    odd = Bar(
        symbol="BTCUSDT",
        timestamp=T0,
        open=Decimal("1E+2"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=Decimal("1"),
    )
    with pytest.raises(ValueError, match="positive exponent"):
        write_bars(conn, source, run_id, market="um", interval="1m", bars=[odd])
    assert read_bars(conn, source, market="um", symbol="BTCUSDT", interval="1m") == []


# -- the heartbeat (T-f280e8f0) ------------------------------------------------------------


def test_a_running_capture_reports_its_counters_where_a_monitor_can_read_them(
    conn: Any,
) -> None:
    """The whole point: `collector_health` answers "is it working" while it is
    still running. `feed_health` cannot — it reads bars, and a process that died
    a minute ago still has yesterday's bars.

    Mutation: make `heartbeat` skip the `connection.commit()` — the row is
    invisible to the separate read below and this goes red, which matters
    because the operations page reads on its own connection."""
    migrate(conn)
    source = ensure_source(conn, "alpaca-sip", kind="vendor", venue="alpaca")
    # Read on a SECOND backend, because that is who reads it: the operations
    # page and `check_health.py` each open their own connection. Reading back on
    # the writer's connection sees uncommitted rows and would pass with the
    # commit removed, proving only that the UPDATE was spelled correctly.
    other = _second_connection(conn)
    try:
        with ingest_run(conn, source, kind="capture") as run_id:
            heartbeat(conn, run_id, {"polls": 12, "consecutive_errors": 3, "sink_failures": 1})
            with other.cursor() as cursor:
                cursor.execute(
                    "SELECT source, consecutive_errors, sink_failures, since_heartbeat "
                    "FROM collector_health WHERE run_id = %s",
                    (run_id,),
                )
                row = cursor.fetchone()
    finally:
        other.close()

    assert row is not None, "a running capture must appear in collector_health"
    assert row[0] == "alpaca-sip"
    assert (row[1], row[2]) == (3, 1)
    assert row[3] < timedelta(minutes=1)


def test_a_finished_run_leaves_collector_health(conn: Any) -> None:
    """The view is "what is running now". A run that closed is history and
    belongs in `ingest_runs`, not in the thing somebody alarms on.

    Mutation: drop the `WHERE r.status = 'running'` from the view — every run
    this repository has ever made appears as live, most of them ancient, and the
    alarm fires permanently. Red."""
    migrate(conn)
    source = ensure_source(conn, "polygon", kind="vendor", venue="polygon")
    with ingest_run(conn, source, kind="capture") as run_id:
        heartbeat(conn, run_id, {"polls": 1})
    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM collector_health WHERE run_id = %s", (run_id,))
        assert cursor.fetchone()[0] == 0


def test_a_run_that_never_reported_has_a_null_heartbeat_not_a_zero(conn: Any) -> None:
    """ "Never reported" and "reported nothing" are different states and only one
    of them is an alarm. Defaulting the column to `now()` would make every run
    look alive the instant it opened.

    Mutation: give `heartbeat_at` a `DEFAULT now()` in the migration — red."""
    migrate(conn)
    source = ensure_source(conn, "binance", kind="vendor", venue="binance")
    with ingest_run(conn, source, kind="capture") as run_id, conn.cursor() as cursor:
        cursor.execute(
            "SELECT heartbeat_at, since_heartbeat, consecutive_errors "
            "FROM collector_health WHERE run_id = %s",
            (run_id,),
        )
        beat, since, errors = cursor.fetchone()
    assert beat is None and since is None
    assert errors == 0, "an absent stats blob reads as zero counters, not as NULL"
