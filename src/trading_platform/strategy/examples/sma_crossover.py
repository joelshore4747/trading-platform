"""Reference strategy: SMA crossover.

This exists to exercise the engine end to end and to serve as the shape every
other strategy should copy. It is NOT a strategy worth trading — a two-moving-
average crossover on a single instrument is the "hello world" of systematic
trading and loses money net of costs on most real series.

What it demonstrates:
  - reading only backward-looking history from the context
  - refusing to act until enough history exists (no partial-window signals)
  - equity-relative sizing via ``target_notional``
  - keeping all parameters in ``__init__`` so they can be swept
"""

from __future__ import annotations

from decimal import Decimal

from trading_platform.core.events import Bar, Fill
from trading_platform.strategy.base import Strategy, StrategyContext


class SMACrossover(Strategy):
    """Long when the fast SMA is above the slow SMA; flat otherwise.

    Args:
        symbol: instrument to trade.
        fast: fast SMA window, in bars.
        slow: slow SMA window, in bars.
        allocation: fraction of equity to deploy when long.
    """

    def __init__(
        self,
        symbol: str,
        fast: int = 10,
        slow: int = 30,
        allocation: Decimal = Decimal("0.95"),
    ) -> None:
        if fast >= slow:
            raise ValueError(f"fast window ({fast}) must be shorter than slow ({slow})")
        if fast < 1:
            raise ValueError(f"fast window must be >= 1, got {fast}")
        if not (Decimal("0") < allocation <= Decimal("1")):
            raise ValueError(f"allocation must be in (0, 1], got {allocation}")

        self.symbol = symbol
        self.fast = fast
        self.slow = slow
        self.allocation = allocation
        self.name = f"SMACrossover({symbol}, {fast}/{slow})"
        self.fills_seen = 0

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
        if bar.symbol != self.symbol:
            return

        closes = ctx.closes(self.symbol)
        # Wait for a full slow window. Acting on a partial window means the
        # first N signals are computed from a different distribution than the
        # rest — a subtle but real source of backtest distortion.
        if len(closes) < self.slow:
            return

        fast_ma = _sma(closes, self.fast)
        slow_ma = _sma(closes, self.slow)

        currently_long = ctx.position(self.symbol) > 0

        if fast_ma > slow_ma and not currently_long:
            ctx.target_notional(
                self.symbol,
                ctx.equity * self.allocation,
                tag="sma_entry",
            )
        elif fast_ma <= slow_ma and currently_long:
            ctx.close_position(self.symbol, tag="sma_exit")

    def on_fill(self, fill: Fill, ctx: StrategyContext) -> None:
        self.fills_seen += 1


def _sma(values: list[Decimal], window: int) -> Decimal:
    """Simple moving average of the last ``window`` values."""
    window_values = values[-window:]
    return sum(window_values, Decimal("0")) / Decimal(len(window_values))
