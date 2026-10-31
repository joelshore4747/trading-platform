"""Compare what two sources said about the same instant, and keep the difference.

This is the payoff for running several feeds. The comparison is pairwise and
symmetric-by-construction: for each (market, symbol, interval, instant) covered
by more than one source, every unordered pair of sources is compared once, with
``source_a_id < source_b_id`` so a pair is never recorded twice in mirror image.

**The distinction that makes the table readable** — and the reason this is not
a diff:

*Same venue.* Two sources claiming the same venue should be identical. Any gap
is a data error in one of them, and its size is the size of the error.

*Different venues.* Two venues quoting the same asset should differ. That gap is
the basis: a market fact, not a fault. Recording it as an error would bury the
real errors in noise.

``same_venue`` is stamped on every row from ``sources.venue`` at comparison
time, and the two kinds must never be summed together. A source with no
recorded venue cannot be classified, so it is **refused** rather than guessed —
a wrong ``same_venue`` turns a basis into a false alarm or an error into a
shrug, and both are worse than a raised exception.

**Instants only one source covered are not evidence of agreement.** They are
counted separately (`instants` counts instants at least two sources reached)
and the agreement rate is over comparisons actually possible. An agreement rate
without its denominator is meaningless: 100% over three comparisons is not
agreement, and `report()` never prints the one without the other.

Volume is stored on every disagreement row and never gates one. Two venues
genuinely trade different amounts, and even one venue reported by two vendors
can differ on volume through trade-condition filtering. A volume difference is
context for a price difference, not evidence on its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading_platform.core.events import Bar

__all__ = [
    "Disagreement",
    "ReconcileError",
    "ReconcileResult",
    "compare_bars",
    "reconcile",
]

#: The fields a price disagreement is measured on. Volume is deliberately absent.
PRICE_FIELDS = ("open", "high", "low", "close")


class ReconcileError(RuntimeError):
    """The comparison could not be made honestly."""


@dataclass(frozen=True, slots=True)
class Disagreement:
    """One instant where two sources gave different prices."""

    market: str
    symbol: str
    interval: str
    ts: datetime
    source_a_id: int
    source_b_id: int
    same_venue: bool
    close_a: Decimal
    close_b: Decimal
    volume_a: Decimal
    volume_b: Decimal
    worst_gap: Decimal
    worst_field: str

    @property
    def basis_points(self) -> Decimal:
        """``worst_gap`` in basis points, the unit a trader reads it in."""
        return self.worst_gap * Decimal("10000")


def compare_bars(a: Bar, b: Bar) -> tuple[Decimal, str] | None:
    """The largest relative gap across open/high/low/close, or None if identical.

    Relative to ``a``, which is well defined because ``Bar`` refuses a
    non-positive price at construction — there is no division by zero to guard.
    **Deliberately asymmetric**: 100 against 200 is a gap of 1.0, and 200
    against 100 is 0.5. Which source is ``a`` is fixed by
    ``source_a_id < source_b_id``, so a given pair always reports the same
    number and rows stay comparable; the figure is "how far b is from a" and
    should be read that way rather than as a symmetric distance.

    Decimal exponents are ignored on purpose here: ``100.5`` and ``100.50``
    are the same price quoted to different precision, and calling that a
    disagreement would fill the table with vendors' formatting choices. The
    exponent is preserved in ``bars`` for anyone who needs it.
    """
    worst = Decimal(0)
    field = ""
    for name in PRICE_FIELDS:
        left: Decimal = getattr(a, name)
        right: Decimal = getattr(b, name)
        if left == right:
            continue
        gap = abs(left - right) / left
        # `field == ""` as well as `gap > worst`: a field that differs is
        # recorded even if its relative gap rounds to zero, so that a mutation
        # loosening the equality test above (comparing `str(left) != str(right)`,
        # say, which would make 100.5 and 100.50 "different") changes what this
        # returns instead of being masked by a strict `>`. Unmutated, values that
        # differ always have a positive gap, so nothing changes in real use.
        if field == "" or gap > worst:
            worst, field = gap, name
    return (worst, field) if field else None


@dataclass(slots=True)
class ReconcileResult:
    """What one pass compared and what it found."""

    run_id: int | None
    market: str
    interval: str
    window_start: datetime
    window_end: datetime
    #: Instants at least two sources both covered. Anything else is uncomparable.
    instants: int = 0
    #: (instant, source pair) comparisons actually made. The all-in denominator.
    compared: int = 0
    #: Comparisons between two sources claiming the SAME venue. These should
    #: agree exactly, so this is the denominator of the only rate that means
    #: "how often was something wrong".
    same_venue_compared: int = 0
    disagreements: list[Disagreement] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.disagreements is None:
            self.disagreements = []

    @property
    def agreement_rate(self) -> Decimal | None:
        """Fraction of ALL comparisons that matched, or None when none were possible.

        None rather than 1.0: no comparisons is not perfect agreement, and a
        caller that prints 100% for an empty window has told a lie. Every
        caller must handle the None.

        **This is not the error rate**, and on its own it is close to
        meaningless: cross-venue pairs are *expected* to differ, so a
        configuration with two venues drives this number toward zero while
        nothing at all is wrong. Measured: three sources across two venues
        reported "agreement 33.3333%" on clean data. Read
        `same_venue_agreement_rate` instead, which is what `report()` leads with.
        """
        if self.compared == 0:
            return None
        return Decimal(self.compared - len(self.disagreements)) / Decimal(self.compared)

    @property
    def same_venue_agreement_rate(self) -> Decimal | None:
        """The error rate's complement: how often two sources on one venue agreed.

        The number that means something. None when no two sources shared a
        venue, which is not agreement — it is a configuration in which nothing
        could have been checked.
        """
        if self.same_venue_compared == 0:
            return None
        errors = len(self.same_venue_disagreements)
        return Decimal(self.same_venue_compared - errors) / Decimal(self.same_venue_compared)

    @property
    def cross_venue_compared(self) -> int:
        return self.compared - self.same_venue_compared

    @property
    def same_venue_disagreements(self) -> list[Disagreement]:
        """The ones that are errors. The others are basis and are not summed in."""
        return [d for d in self.disagreements if d.same_venue]

    def report(self) -> str:
        header = (
            f"{self.market} {self.interval} "
            f"{self.window_start.isoformat()}..{self.window_end.isoformat()}"
        )
        if self.compared == 0:
            return (
                f"{header}: NOTHING COMPARED — {self.instants} instants had two sources. "
                "No agreement rate can be stated."
            )
        errors = self.same_venue_disagreements
        basis = len(self.disagreements) - len(errors)
        lines = [
            header,
            f"  {self.compared:,} comparisons over {self.instants:,} shared instants",
        ]
        # The same-venue rate first, and alone on its line, because it is the
        # only one that means "something was wrong". The all-in rate below it is
        # labelled as including the basis so nobody reads it as an error rate.
        same_rate = self.same_venue_agreement_rate
        if same_rate is None:
            lines.append(
                f"  same venue: NOTHING COMPARED — no two of these sources claim the same "
                f"venue, so no error rate can be stated. All {self.compared:,} comparisons "
                "are cross-venue and are expected to differ."
            )
        else:
            lines.append(
                f"  same venue: {same_rate * 100:.4f}% agreement over "
                f"{self.same_venue_compared:,} comparisons — {len(errors):,} error(s), "
                "each one a bad print in one of the two sources"
            )
        lines.append(
            f"  cross venue: {basis:,} of {self.cross_venue_compared:,} comparisons differ. "
            "That is the basis, a market fact, and is NOT an error"
        )
        all_rate = self.agreement_rate
        if all_rate is not None:
            lines.append(
                f"  (all-in agreement {all_rate * 100:.4f}%, basis included — not an error rate)"
            )
        if errors:
            worst = max(errors, key=lambda d: d.worst_gap)
            lines.append(
                f"  worst same-venue gap: {worst.symbol} at {worst.ts.isoformat()} "
                f"{worst.worst_field} {worst.basis_points:.2f} bp "
                f"(sources {worst.source_a_id} vs {worst.source_b_id})"
            )
        return "\n".join(lines)


def _source_venues(connection: Any) -> dict[int, str | None]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, venue FROM sources")
        return {int(sid): venue for sid, venue in cursor.fetchall()}


def _open_run(connection: Any, market: str, interval: str, start: datetime, end: datetime) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO reconcile_runs (window_start, window_end, market, interval) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (start, end, market, interval),
        )
        run_id = int(cursor.fetchone()[0])
    connection.commit()
    return run_id


def reconcile(
    connection: Any,
    *,
    market: str,
    interval: str,
    window_start: datetime,
    window_end: datetime,
    symbols: Sequence[str] | None = None,
    min_gap: Decimal = Decimal(0),
    record: bool = True,
) -> ReconcileResult:
    """Compare every pair of sources over a window and record what differed.

    ``min_gap`` is a floor on what is worth a row, expressed as a fraction
    (``Decimal("0.0001")`` is one basis point). It defaults to zero: with
    ``NUMERIC`` storage and ``Decimal`` comparison there is no floating-point
    noise to filter, so any nonzero gap is a real difference in what two
    sources said. Raise it only with a reason, and say what the reason was —
    a threshold is how a systematic error becomes invisible.

    ``record=False`` compares and returns without writing, for a dry run.
    """
    # Awareness first: comparing a naive bound to an aware one raises a
    # TypeError that names neither bound nor says what to do about it.
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ValueError("reconcile window bounds must be timezone-aware")
    if window_end < window_start:
        raise ValueError(
            f"window_end {window_end.isoformat()} is before {window_start.isoformat()}"
        )
    if min_gap < 0:
        raise ValueError(f"min_gap must not be negative, got {min_gap}")

    venues = _source_venues(connection)

    clauses = [
        "a.market = %s",
        "a.interval = %s",
        "a.ts >= %s",
        "a.ts <= %s",
    ]
    params: list[Any] = [market, interval, window_start, window_end]
    if symbols is not None:
        if not symbols:
            raise ValueError("symbols was given as empty; omit it to compare every symbol")
        clauses.append("a.symbol = ANY(%s)")
        params.append(list(symbols))
    where = " AND ".join(clauses)

    run_id = _open_run(connection, market, interval, window_start, window_end) if record else None
    result = ReconcileResult(
        run_id=run_id,
        market=market,
        interval=interval,
        window_start=window_start,
        window_end=window_end,
    )

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM (SELECT a.symbol, a.ts FROM bars a WHERE "
            + where
            + " GROUP BY a.symbol, a.ts HAVING count(DISTINCT a.source_id) > 1) shared",
            params,
        )
        result.instants = int(cursor.fetchone()[0])

        cursor.execute(
            "SELECT a.symbol, a.ts, a.source_id, b.source_id, "
            "a.open, a.high, a.low, a.close, a.volume, "
            "b.open, b.high, b.low, b.close, b.volume "
            "FROM bars a JOIN bars b ON a.market = b.market AND a.symbol = b.symbol "
            "AND a.interval = b.interval AND a.ts = b.ts AND a.source_id < b.source_id "
            "WHERE " + where + " ORDER BY a.symbol, a.ts, a.source_id, b.source_id",
            params,
        )
        rows = cursor.fetchall()

    for symbol, ts, a_id, b_id, ao, ah, al, ac, av, bo, bh, bl, bc, bv in rows:
        a_id, b_id = int(a_id), int(b_id)
        for sid in (a_id, b_id):
            if venues.get(sid) is None:
                raise ReconcileError(
                    f"source {sid} has no recorded venue, so a comparison against it cannot "
                    "be classified as same-venue (an error) or cross-venue (basis). "
                    "Set the venue on the source rather than guessing which it is."
                )
        stamped = ts.astimezone(UTC)
        left = Bar(symbol, stamped, Decimal(ao), Decimal(ah), Decimal(al), Decimal(ac), Decimal(av))
        right = Bar(
            symbol, stamped, Decimal(bo), Decimal(bh), Decimal(bl), Decimal(bc), Decimal(bv)
        )
        same_venue = venues[a_id] == venues[b_id]
        result.compared += 1
        if same_venue:
            result.same_venue_compared += 1
        found = compare_bars(left, right)
        if found is None:
            continue
        worst_gap, worst_field = found
        if worst_gap <= min_gap:
            continue
        result.disagreements.append(
            Disagreement(
                market=market,
                symbol=symbol,
                interval=interval,
                ts=stamped,
                source_a_id=a_id,
                source_b_id=b_id,
                same_venue=same_venue,
                close_a=left.close,
                close_b=right.close,
                volume_a=left.volume,
                volume_b=right.volume,
                worst_gap=worst_gap,
                worst_field=worst_field,
            )
        )

    if record and run_id is not None:
        _write_result(connection, run_id, result)
    return result


def _write_result(connection: Any, run_id: int, result: ReconcileResult) -> None:
    with connection.cursor() as cursor:
        if result.disagreements:
            cursor.executemany(
                "INSERT INTO source_disagreements (reconcile_run_id, market, symbol, interval, "
                "ts, source_a_id, source_b_id, same_venue, close_a, close_b, volume_a, volume_b, "
                "worst_gap, worst_field) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT ON CONSTRAINT source_disagreements_once DO NOTHING",
                [
                    (
                        run_id,
                        d.market,
                        d.symbol,
                        d.interval,
                        d.ts,
                        d.source_a_id,
                        d.source_b_id,
                        d.same_venue,
                        d.close_a,
                        d.close_b,
                        d.volume_a,
                        d.volume_b,
                        d.worst_gap,
                        d.worst_field,
                    )
                    for d in result.disagreements
                ],
            )
        cursor.execute(
            "UPDATE reconcile_runs SET finished_at = now(), instants = %s, compared = %s, "
            "disagreements = %s, same_venue_compared = %s, same_venue_disagreements = %s "
            "WHERE id = %s",
            (
                result.instants,
                result.compared,
                len(result.disagreements),
                result.same_venue_compared,
                len(result.same_venue_disagreements),
                run_id,
            ),
        )
    connection.commit()
