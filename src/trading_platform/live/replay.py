"""Replay a captured session through the backtester, and compare.

This is the bridge roadmap step 50 rests on. The exit criterion for paper
trading is thirty days with no unexplained divergence between the live run and
the backtest of the same period — and **any divergence is a bug in one of
them**, which is the entire point of looking.

Two halves.

`replay_feed` turns a capture back into a `DataFeed`, so the same `Strategy`
object runs over the same bars through the ordinary `Backtester`. Nothing about
the strategy or the engine changes; only where the bars come from.

`compare` then puts the live fills beside the replayed ones and reports the
first place they disagree. Reporting the *first* divergence rather than a count
is deliberate: fills are causally chained through the portfolio, so one early
difference makes every later one meaningless. A report of "sixty-one fills
differ" invites you to look for sixty-one causes when there is one.

What a clean comparison establishes is narrow and worth stating: the two paths
agreed on this capture. It does not establish that either is right — they could
share a defect, and a defect in the engine is exactly what they would share.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading_platform.core.events import Bar, Fill
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.live.recorder import iter_journal_days, read_journal


def replay_feed(
    root: Path | str,
    market: str,
    symbols: Sequence[str],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> InMemoryBarFeed:
    """Rebuild a feed from captured journals.

    Reads the journal rather than the compacted Parquet on purpose: the journal
    is what actually arrived, in the order it arrived, including any duplicate a
    reconnect replayed. Comparing against the compacted file would compare the
    live run to a *cleaned* version of its own input and quietly credit the
    cleaning with the agreement.

    `InMemoryBarFeed` sorts and validates ordering, which is what the engine
    requires. Exact duplicates are dropped — the live feed would not have
    emitted them twice either, since `LiveFeed` refuses a bar whose slot has
    passed.
    """
    seen: dict[tuple[str, datetime], Bar] = {}
    for symbol in symbols:
        for path in iter_journal_days(root, market, symbol):
            for bar in read_journal(path, symbol):
                if start is not None and bar.timestamp < start:
                    continue
                if end is not None and bar.timestamp > end:
                    continue
                seen.setdefault((symbol, bar.timestamp), bar)
    return InMemoryBarFeed(list(seen.values()))


@dataclass(frozen=True, slots=True)
class FillDifference:
    """The first place two fill sequences stop agreeing."""

    index: int
    field: str
    live: str
    replayed: str

    def __str__(self) -> str:
        return f"fill {self.index}: {self.field} was {self.live} live and {self.replayed} on replay"


@dataclass(frozen=True, slots=True)
class DivergenceReport:
    """Whether a replay reproduced a live session, and where it first did not."""

    live_fills: int
    replayed_fills: int
    first_difference: FillDifference | None

    @property
    def agrees(self) -> bool:
        return self.first_difference is None and self.live_fills == self.replayed_fills

    def report(self) -> str:
        if self.agrees:
            return (
                f"AGREES — {self.live_fills} fill(s) reproduced exactly.\n"
                "This says the two paths agree on this capture. It does not say either is "
                "right: a defect in the engine is one they would share."
            )
        lines = [
            f"DIVERGES — live {self.live_fills} fill(s), replay {self.replayed_fills}.",
            "Any divergence is a bug in one of them; finding out which is the point.",
        ]
        if self.first_difference is not None:
            lines.append(f"  first: {self.first_difference}")
            lines.append(
                "  Only the FIRST is shown. Fills chain through the portfolio, so every "
                "later difference is downstream of this one."
            )
        return "\n".join(lines)


def _fields(fill: Fill) -> dict[str, str]:
    return {
        "symbol": fill.symbol,
        "timestamp": fill.timestamp.isoformat(),
        "side": fill.side.name,
        "quantity": str(fill.quantity),
        "price": str(fill.price),
    }


def compare(
    live: Sequence[Fill],
    replayed: Sequence[Fill],
    *,
    price_tolerance: Decimal = Decimal("0"),
) -> DivergenceReport:
    """Compare two fill sequences, reporting the first disagreement.

    `price_tolerance` defaults to **zero**. A replay of the same bars through
    the same engine is deterministic, so any price difference at all is a real
    finding rather than noise to be absorbed. The parameter exists for the case
    where a venue's own paper fills are being compared against the simulator's,
    where a tolerance is a stated modelling assumption — and it should be
    stated, not defaulted into.
    """
    for index, (a, b) in enumerate(zip(live, replayed, strict=False)):
        left, right = _fields(a), _fields(b)
        for field, value in left.items():
            other = right[field]
            if (
                field == "price"
                and price_tolerance > 0
                and abs(Decimal(value) - Decimal(other)) <= price_tolerance
            ):
                continue
            if value != other:
                return DivergenceReport(
                    len(live), len(replayed), FillDifference(index, field, value, other)
                )

    if len(live) != len(replayed):
        shorter = min(len(live), len(replayed))
        longer = live if len(live) > len(replayed) else replayed
        extra = longer[shorter]
        return DivergenceReport(
            len(live),
            len(replayed),
            FillDifference(
                shorter,
                "presence",
                "a fill" if len(live) > len(replayed) else "nothing",
                "nothing" if len(live) > len(replayed) else f"a fill at {extra.timestamp}",
            ),
        )

    return DivergenceReport(len(live), len(replayed), None)
