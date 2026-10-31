"""One ICT strategy, composed from the primitives, stated so it can be refuted.

## The hypothesis, in full

Stop orders cluster just beyond obvious swing highs and lows. When price reaches
through such a level and closes back inside it, those stops have been filled by
participants who wanted the liquidity, and the move that follows goes the other
way. The effect is claimed to be strongest during the London and New York
"killzones" and absent otherwise.

The first half — that stops cluster beyond visible extremes — is ordinary market
microstructure and is not in dispute. **The second half is the claim under test:
that the reversal is systematically tradeable after costs.**

## What would refute it

- No edge over a random-entry null of the same trade count and holding period.
- An edge that vanishes at realistic costs (`/cost-check`).
- An edge in-sample that does not survive walk-forward (`/walk-forward`).
- An edge that disappears when the killzone filter is removed *or* when it is
  moved to arbitrary hours — the first would mean the filter is the whole
  result, the second that any three hours would have done.

**The expected outcome is rejection.** A strategy built from folklore, on one
month of one venue's data, being profitable is evidence of a bug before it is
evidence of anything else.

## Parameters, counted honestly

Four: `strength`, `hold`, `allocation`, `use_killzones`. Every one is a
dimension to overfit in, and the deflated Sharpe for the real trial count
(`analytics/multiplicity.py`) is the only number worth quoting. They are kept in
`__init__` so a sweep can move them, and kept few so a sweep means something.

## What it does NOT do

No stop loss and no target. The engine has no stop orders, and simulating one
from bar data means deciding whether the high or the low came first inside the
bar — which the data does not say. Inventing that answer is how a backtest of a
stop-based strategy produces a number that cannot be traded. The position is
held for a fixed number of bars or until an opposing sweep, both of which are
decidable from a close.
"""

from __future__ import annotations

from decimal import Decimal

from trading_platform.core.events import Bar
from trading_platform.strategy.base import Strategy, StrategyContext
from trading_platform.strategy.patterns import (
    Direction,
    in_killzone,
    liquidity_sweeps,
)

__all__ = ["ICTSweepReversal"]


class ICTSweepReversal(Strategy):
    """Fade a liquidity sweep, inside a killzone, for a fixed number of bars.

    Args:
        symbol: instrument to trade.
        strength: fractal width for the swing levels being swept. Wider means
            fewer, more obvious levels — which is what the idea is about.
        hold: bars to hold before flattening. A time stop, not a price stop:
            see the module docstring for why there is no price stop.
        allocation: fraction of equity deployed per entry, either side.
        use_killzones: apply the session filter. Exposed so the null — the same
            strategy with the filter off — is one argument away, because "the
            filter is the whole result" is the first thing to rule out.
        lookback: bars of history the primitives see. Bounded because the
            context caps history anyway and an unbounded scan re-derives every
            swing on every bar.
    """

    name = "ict-sweep-reversal"

    def __init__(
        self,
        symbol: str,
        strength: int = 2,
        hold: int = 20,
        allocation: Decimal = Decimal("0.50"),
        *,
        use_killzones: bool = True,
        lookback: int = 200,
    ) -> None:
        if strength < 1:
            raise ValueError(f"strength must be at least 1, got {strength}")
        if hold < 1:
            raise ValueError(f"hold must be at least 1 bar, got {hold}")
        if not 0 < allocation <= 1:
            raise ValueError(f"allocation must be in (0, 1], got {allocation}")
        if lookback < 4 * strength + 4:
            raise ValueError(
                f"lookback {lookback} is too short to confirm a swing of strength "
                f"{strength} and then sweep it"
            )
        self.symbol = symbol
        self.strength = strength
        self.hold = hold
        self.allocation = allocation
        self.use_killzones = use_killzones
        self.lookback = lookback
        #: Bars held so far. None when flat.
        self._held: int | None = None
        #: Counters, so a run that traded nothing says why rather than looking
        #: like a run that traded nothing profitably.
        self.sweeps_seen = 0
        self.sweeps_outside_killzone = 0
        self.entries = 0
        self.time_stops = 0

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
        if bar.symbol != self.symbol:
            return

        if self._held is not None:
            self._held += 1
            if self._held >= self.hold:
                ctx.close_position(self.symbol, tag="time-stop")
                self._held = None
                self.time_stops += 1
                return

        history = ctx.history(self.symbol, self.lookback)
        if len(history) < 2 * self.strength + 2:
            return

        sweeps = liquidity_sweeps(history, self.strength)
        if not sweeps:
            return
        latest = sweeps[-1]
        # Only a sweep on THIS bar is a signal. An older one has already been
        # acted on or missed, and re-entering on it every bar would turn one
        # signal into a hundred.
        if latest.index != len(history) - 1:
            return
        self.sweeps_seen += 1

        if self.use_killzones and not in_killzone(bar.timestamp):
            self.sweeps_outside_killzone += 1
            return

        # `suggests` is the inversion: sweeping a HIGH is a bearish signal,
        # because the buy stops above it were filled and the move failed.
        wanted = latest.suggests
        signed = ctx.equity * self.allocation
        target = signed if wanted is Direction.UP else -signed
        if ctx.target_notional(self.symbol, target, tag=f"sweep-{latest.direction}"):
            self.entries += 1
            self._held = 0

    def report(self) -> str:
        """What the run did, in the numbers that explain a flat result."""
        return (
            f"{self.name}: {self.sweeps_seen} sweep(s) seen, "
            f"{self.sweeps_outside_killzone} outside a killzone, "
            f"{self.entries} entries, {self.time_stops} time stops"
        )
