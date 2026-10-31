"""Reference cross-sectional strategy: rank by trailing return, hold the top k.

The shape every cross-sectional strategy should copy, and — like
`SMACrossover` — **not a strategy worth trading**. Cross-sectional momentum in
crypto is among the most heavily mined effects there is, which is exactly why
it is the right first ranker: if this pipeline cannot reject a crowded,
well-known effect on data that has none of it, nothing it says later means
anything.

The mechanism claimed for it, which belongs in the trial registry before it
runs: slow diffusion of information across a correlated asset class means a
move in the leaders is not immediately priced into the laggards, so recent
relative strength persists over the following days. That is a real hypothesis
with a real reason it might once have been true and a real reason it should be
arbitraged away by now.

What it demonstrates:
  - acting from `on_cross_section`, never comparing symbols inside `on_bar`
  - taking the tradeable set from `ctx.cross_section()` rather than from a
    hardcoded list, so a symbol that did not print is absent rather than ranked
  - equal-weight sizing through `target_notional`
  - keeping every parameter in `__init__` so the grid can sweep them
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trading_platform.core.events import Bar
from trading_platform.strategy.base import Strategy, StrategyContext


class CrossSectionalMomentum(Strategy):
    """Hold the `top_k` symbols by trailing return, rebalanced periodically.

    Args:
        lookback: bars of trailing return used to rank. Ranking over a window
            the strategy has not fully seen would compare symbols on unequal
            evidence, so a symbol without a full window is not ranked.
        top_k: how many symbols to hold.
        rebalance_every: instants between rebalances. Every instant is a
            defensible choice and a very expensive one; the cost model is what
            decides, so this is a swept parameter rather than a default.
        allocation: fraction of equity deployed when fully invested.
    """

    def __init__(
        self,
        lookback: int = 20,
        top_k: int = 5,
        rebalance_every: int = 20,
        allocation: Decimal = Decimal("0.90"),
    ) -> None:
        if lookback < 2:
            raise ValueError(f"lookback must be >= 2, got {lookback}")
        if top_k < 1:
            raise ValueError(f"top_k must be >= 1, got {top_k}")
        if rebalance_every < 1:
            raise ValueError(f"rebalance_every must be >= 1, got {rebalance_every}")
        if not (Decimal("0") < allocation <= Decimal("1")):
            raise ValueError(f"allocation must be in (0, 1], got {allocation}")

        self.lookback = lookback
        self.top_k = top_k
        self.rebalance_every = rebalance_every
        self.allocation = allocation
        self.name = f"CrossSectionalMomentum({lookback}/{top_k}/{rebalance_every})"
        self._instants = 0
        self.rebalances = 0
        #: Instants skipped because too few symbols had a full window. Counted
        #: rather than ignored: a ranker that quietly did nothing looks
        #: identical in the equity curve to one that found nothing to do.
        self.skipped_thin = 0

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
        """Deliberately empty.

        Every decision this strategy makes compares symbols to one another, and
        `on_bar` is the one place where that comparison is guaranteed wrong —
        the symbols later in the instant have not been revealed yet.
        """

    def _trailing_return(self, symbol: str, ctx: StrategyContext) -> float | None:
        closes = ctx.closes(symbol, self.lookback)
        if len(closes) < self.lookback or closes[0] <= 0:
            return None
        return float(closes[-1] / closes[0])

    def on_cross_section(self, timestamp: datetime, ctx: StrategyContext) -> None:
        self._instants += 1
        if self._instants % self.rebalance_every:
            return

        tradeable = ctx.cross_section()
        scores: dict[str, float] = {}
        for symbol in tradeable:
            score = self._trailing_return(symbol, ctx)
            if score is not None:
                scores[symbol] = score

        # Ranking two names and calling it a cross-section is how a portfolio
        # statistic gets computed on a sample that cannot support it.
        if len(scores) < self.top_k:
            self.skipped_thin += 1
            return

        # Ties broken by symbol so a run is reproducible. Arbitrary, and
        # arbitrary-but-fixed beats arbitrary-and-varying.
        ranked = sorted(scores, key=lambda s: (-scores[s], s))
        winners = set(ranked[: self.top_k])
        self.rebalances += 1

        target = ctx.equity * self.allocation / Decimal(self.top_k)
        # Exits before entries: selling first frees the cash the buys need, and
        # submitting in the other order leaves the buys to be rejected for
        # insufficient cash on exactly the rebalances that turn the book over
        # most.
        for symbol in ctx.holdings():
            if symbol not in winners:
                ctx.close_position(symbol, tag="rank_exit")
        for symbol in ranked:
            if symbol in winners:
                ctx.target_notional(symbol, target, tag="rank_entry")
