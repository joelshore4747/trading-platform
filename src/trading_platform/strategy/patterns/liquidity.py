"""Liquidity sweeps: a wick through a level that closes back inside it.

The folklore's claim is that clusters of stop orders sit just beyond obvious
highs and lows, that price reaches through to fill them, and that the reversal
follows. The first half is plausible market microstructure and is not in
dispute; the second half is the part this platform exists to test.

A sweep is knowable at the bar that closes, with no confirmation lag — but the
LEVEL it swept must have been confirmed beforehand, so `swing_points` and
`confirmed_by` do the work and the lookahead risk lives there.

Deliberately distinct from a break of structure: a **close** beyond a level is a
break, a **wick** beyond it that closes back inside is a sweep. The same bar
cannot be both, and the folklore reads them as opposite signals, so keeping them
mutually exclusive is what makes either testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_platform.core.events import Bar
from trading_platform.strategy.patterns.structure import (
    Direction,
    Swing,
    confirmed_by,
    swing_points,
)

__all__ = ["LiquiditySweep", "liquidity_sweeps"]


@dataclass(frozen=True, slots=True)
class LiquiditySweep:
    """A bar that reached beyond a prior swing and closed back inside."""

    index: int
    timestamp: datetime
    #: UP means a high was swept — which the folklore reads as BEARISH, because
    #: the buy stops above it were filled and the move failed. The direction
    #: names the level taken, not the trade it suggests, and the two are
    #: opposite; the strategy is where that inversion happens, visibly.
    direction: Direction
    level: Decimal
    swing_index: int
    #: How far beyond the level the wick reached, as a fraction of the level.
    penetration: Decimal

    @property
    def suggests(self) -> Direction:
        """The direction the folklore reads this as. Stated, not assumed."""
        return Direction.DOWN if self.direction is Direction.UP else Direction.UP


def liquidity_sweeps(
    bars: Sequence[Bar], strength: int = 2, *, min_penetration: Decimal = Decimal(0)
) -> list[LiquiditySweep]:
    """Every sweep of a confirmed swing.

    `min_penetration` is a floor on how far past the level the wick must reach,
    as a fraction. Zero by default: a threshold is a parameter, and a parameter
    chosen after seeing the results is how a backtest gets fitted. Raise it
    deliberately and say why.
    """
    if min_penetration < 0:
        raise ValueError("min_penetration is a magnitude and must not be negative")
    swings = swing_points(bars, strength)
    out: list[LiquiditySweep] = []
    for i, bar in enumerate(bars):
        available: list[Swing] = [s for s in confirmed_by(swings, i) if s.index < i]
        highs = [s for s in available if s.direction is Direction.UP]
        lows = [s for s in available if s.direction is Direction.DOWN]
        if highs:
            level = highs[-1]
            # Wick through, close back inside. A close beyond is a break, not a
            # sweep, and the two must stay mutually exclusive.
            if bar.high > level.price and bar.close <= level.price:
                depth = (bar.high - level.price) / level.price
                if depth >= min_penetration:
                    out.append(
                        LiquiditySweep(
                            i, bar.timestamp, Direction.UP, level.price, level.index, depth
                        )
                    )
                    continue
        if lows:
            level = lows[-1]
            if bar.low < level.price and bar.close >= level.price:
                depth = (level.price - bar.low) / level.price
                if depth >= min_penetration:
                    out.append(
                        LiquiditySweep(
                            i, bar.timestamp, Direction.DOWN, level.price, level.index, depth
                        )
                    )
    return out
