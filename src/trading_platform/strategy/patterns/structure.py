"""Market structure: swing points, and breaks of them.

**These concepts are widely taught and thinly evidenced.** "ICT" names a body of
retail trading folklore whose claims are almost never stated precisely enough to
test. Mechanising them is exactly what this platform is for: the point of the
code below is not that it works, it is that it turns vague advice into something
that can be falsified. The expected result is rejection.

## The one thing that must not be got wrong

A swing high at bar `i` is **not knowable at bar `i`**. It is knowable once
`strength` further bars have printed without exceeding it. Every function here
therefore reports `confirmed_at` alongside `index`, and `confirmed_by` is the
only sanctioned way to ask "what did I know at bar n". A strategy that reads
`index` and acts on it has given itself `strength` bars of hindsight, which is
the most common way a backtest of these ideas lies.

`tests/test_patterns.py` pins that with a test that walks the series bar by bar
and asserts the answer at each step matches the answer computed from a truncated
series. That is the check that matters.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading_platform.core.events import Bar

__all__ = [
    "Break",
    "BreakKind",
    "Direction",
    "Swing",
    "confirmed_by",
    "market_structure",
    "swing_points",
]


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"


class BreakKind(StrEnum):
    #: A close beyond the last swing in the SAME direction as the prevailing
    #: structure. Continuation.
    BOS = "bos"
    #: The first close beyond a swing AGAINST the prevailing structure. The
    #: "change of character" — the only part of this vocabulary that claims to
    #: be a signal rather than a description.
    CHOCH = "choch"


@dataclass(frozen=True, slots=True)
class Swing:
    """A confirmed pivot.

    `index` is where the extreme printed. `confirmed_at` is the first bar by
    which anyone could have known it. They are never the same bar, and using the
    first where the second belongs is lookahead.
    """

    index: int
    confirmed_at: int
    timestamp: datetime
    price: Decimal
    direction: Direction

    @property
    def lag(self) -> int:
        return self.confirmed_at - self.index


def swing_points(bars: Sequence[Bar], strength: int = 2) -> list[Swing]:
    """Fractal pivots: a high with `strength` lower highs on each side, and the
    mirror for lows.

    Ties are **not** swings. `>` rather than `>=` on both sides, so a flat top
    across three bars produces nothing. A tie is genuinely ambiguous — the
    "pivot" could be any of the equal bars — and picking one would be inventing
    structure the market did not offer.
    """
    if strength < 1:
        raise ValueError(f"strength must be at least 1, got {strength}")
    out: list[Swing] = []
    for i in range(strength, len(bars) - strength):
        window = range(i - strength, i + strength + 1)
        high = bars[i].high
        low = bars[i].low
        if all(bars[j].high < high for j in window if j != i):
            out.append(Swing(i, i + strength, bars[i].timestamp, high, Direction.UP))
        if all(bars[j].low > low for j in window if j != i):
            out.append(Swing(i, i + strength, bars[i].timestamp, low, Direction.DOWN))
    return out


def confirmed_by(swings: Sequence[Swing], index: int) -> list[Swing]:
    """The swings a strategy standing at bar `index` could actually know about.

    The whole reason `Swing` carries two indices. Filtering on `index <= n`
    instead of `confirmed_at <= n` is the lookahead this module exists to make
    hard, and it looks identical in a diff.
    """
    return [s for s in swings if s.confirmed_at <= index]


@dataclass(frozen=True, slots=True)
class Break:
    """A close beyond a prior swing. Knowable at the bar that closed."""

    index: int
    timestamp: datetime
    kind: BreakKind
    direction: Direction
    #: The swing that was broken.
    level: Decimal
    swing_index: int


def market_structure(bars: Sequence[Bar], strength: int = 2) -> list[Break]:
    """Every break of structure and change of character, in order.

    A break requires a **close** beyond the level, not a wick through it. That
    is a choice, and it is the conservative one: a wick beyond a level that
    closes back inside is a liquidity sweep (see `liquidity.py`), which the same
    folklore treats as the opposite signal. Counting it as a break as well would
    make the vocabulary self-contradictory.

    The first break in a series has no prevailing direction to contradict, so it
    is a BOS by definition. Only the second and later breaks can be a CHoCH.
    """
    swings = swing_points(bars, strength)
    breaks: list[Break] = []
    prevailing: Direction | None = None
    #: The most recent swing of each kind that has been confirmed and not broken.
    for i, bar in enumerate(bars):
        available = confirmed_by(swings, i)
        highs = [s for s in available if s.direction is Direction.UP and s.index < i]
        lows = [s for s in available if s.direction is Direction.DOWN and s.index < i]
        broke: Break | None = None
        if highs:
            last_high = highs[-1]
            if bar.close > last_high.price:
                kind = BreakKind.CHOCH if prevailing is Direction.DOWN else BreakKind.BOS
                broke = Break(
                    i, bar.timestamp, kind, Direction.UP, last_high.price, last_high.index
                )
        if lows and broke is None:
            last_low = lows[-1]
            if bar.close < last_low.price:
                kind = BreakKind.CHOCH if prevailing is Direction.UP else BreakKind.BOS
                broke = Break(
                    i, bar.timestamp, kind, Direction.DOWN, last_low.price, last_low.index
                )
        if broke is not None and (
            not breaks
            or breaks[-1].direction is not broke.direction
            or breaks[-1].level != broke.level
        ):
            breaks.append(broke)
            prevailing = broke.direction
    return breaks
