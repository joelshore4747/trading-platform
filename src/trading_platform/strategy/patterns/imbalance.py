"""Fair value gaps and order blocks: the two "where price returns to" ideas.

Both are stated here in the narrowest form the folklore supports, because a
loose definition is untestable and a tight one that fails is a result.

**Fair value gap** (three-bar imbalance): bars A, B, C consecutive, and either
`C.low > A.high` (bullish gap, the span between them was never traded through in
this window) or `C.high < A.low` (bearish). Knowable the moment C closes — no
confirmation lag, unlike a swing.

**Order block**: the last opposing-close bar before a displacement. "Displacement"
is the vague part, and it is pinned here as *a move that leaves a fair value gap
within `window` bars*. That choice is arguable and is stated rather than hidden:
the alternative definitions (a multiple of ATR, a percentile of recent range)
are parameters, and every parameter is another dimension to overfit in.

Nothing here predicts anything. These are descriptions of what a chart did.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_platform.core.events import Bar
from trading_platform.strategy.patterns.structure import Direction

__all__ = ["FairValueGap", "OrderBlock", "fair_value_gaps", "order_blocks", "unfilled"]


@dataclass(frozen=True, slots=True)
class FairValueGap:
    """An untraded span between bar i-2's extreme and bar i's.

    `index` is the third bar — the one whose close makes the gap knowable. There
    is no confirmation lag here, which is exactly why it is worth having beside
    swings: it is the one primitive in this vocabulary a strategy can act on the
    moment it appears.
    """

    index: int
    timestamp: datetime
    direction: Direction
    lower: Decimal
    upper: Decimal

    @property
    def size(self) -> Decimal:
        return self.upper - self.lower

    @property
    def midpoint(self) -> Decimal:
        return (self.upper + self.lower) / Decimal("2")

    def contains(self, price: Decimal) -> bool:
        return self.lower <= price <= self.upper


def fair_value_gaps(bars: Sequence[Bar]) -> list[FairValueGap]:
    """Every three-bar imbalance, in order. No lookahead: each is known at its
    own third bar."""
    out: list[FairValueGap] = []
    for i in range(2, len(bars)):
        first, third = bars[i - 2], bars[i]
        if third.low > first.high:
            out.append(FairValueGap(i, third.timestamp, Direction.UP, first.high, third.low))
        elif third.high < first.low:
            out.append(FairValueGap(i, third.timestamp, Direction.DOWN, third.high, first.low))
    return out


def unfilled(gaps: Sequence[FairValueGap], bars: Sequence[Bar], at: int) -> list[FairValueGap]:
    """Gaps that exist at bar `at` and have not been traded back through.

    "Filled" means a later bar's range reached the gap's far edge — the price
    the gap says was skipped. A touch of the near edge is not a fill, and
    treating it as one would discard most of them.
    """
    live: list[FairValueGap] = []
    for gap in gaps:
        if gap.index > at:
            continue
        after = bars[gap.index + 1 : at + 1]
        if gap.direction is Direction.UP:
            if any(b.low <= gap.lower for b in after):
                continue
        elif any(b.high >= gap.upper for b in after):
            continue
        live.append(gap)
    return live


@dataclass(frozen=True, slots=True)
class OrderBlock:
    """The last opposing-close bar before a displacement.

    `confirmed_at` is the bar whose gap made it a displacement — always after
    `index`. Same trap as a swing, same discipline: act on `confirmed_at`.
    """

    index: int
    confirmed_at: int
    timestamp: datetime
    direction: Direction
    low: Decimal
    high: Decimal

    def contains(self, price: Decimal) -> bool:
        return self.low <= price <= self.high


def order_blocks(bars: Sequence[Bar], window: int = 3) -> list[OrderBlock]:
    """Last opposing candle before a move that leaves a fair value gap.

    `window` is how far after the block the gap may appear. Small on purpose:
    the idea claims the block *caused* the displacement, and a gap five bars
    later is a different event that happens to follow it.
    """
    if window < 1:
        raise ValueError(f"window must be at least 1, got {window}")
    out: list[OrderBlock] = []
    for gap in fair_value_gaps(bars):
        wanted_down = gap.direction is Direction.UP
        # Walk back from the bar that opened the displacement, not from the gap's
        # third bar: the block is what preceded the move.
        start = gap.index - 2
        for j in range(start, max(start - window, -1), -1):
            bar = bars[j]
            is_down = bar.close < bar.open
            if is_down == wanted_down:
                out.append(
                    OrderBlock(
                        index=j,
                        confirmed_at=gap.index,
                        timestamp=bar.timestamp,
                        direction=gap.direction,
                        low=bar.low,
                        high=bar.high,
                    )
                )
                break
    return out
