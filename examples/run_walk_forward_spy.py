"""Walk-forward evaluation of SMACrossover on REAL SPY daily bars.

Run with:  .venv/bin/python examples/run_walk_forward_spy.py

The first equity result this repository has ever produced. Every prior number
here is crypto, so this exists to find out whether the harness says anything
different when the asset class changes — not because SMA crossover on an index
fund is a promising idea. It is the "hello world" of systematic trading applied
to the single most efficiently priced instrument in the world, and the prime
directive stands: the default outcome of a strategy is that it does not work.
A clean verdict here should be read as evidence of a broken harness first.

Four properties of this data bound what any number below can mean. They are
measured, not assumed, and three of them are properties of the *account* rather
than of the market:

- **499 bars, about two years.** Polygon's free plan caps history at ~2 years
  and truncated a six-year request silently (T-594716a0). Two years of daily
  data is a small sample whatever the fold count says.
- **The sample is one regime.** 2024-09 to 2026-09 is a single stretch of
  market history. Folds cut it finer; they do not make it more than one regime.
- **Dividends are absent.** These are `adjusted=false` price bars, deliberately
  — an adjusted series shows a backtest prices nobody could have traded. SPY
  yields roughly 1.2%/yr, so total return is understated by about that much,
  and buy-and-hold is penalised more than a strategy that sits in cash.
- **A daily bar is stamped midnight exchange-local**, which trails the real
  16:00 ET close by ~8 hours and shifts with DST. It never *precedes* the
  information, so there is no lookahead — but a fill "at the next bar's open"
  is at the next session's open, roughly 17 hours later, not seconds.

Sizing is deliberately modest and stated before the run. 499 bars will not
support a wide grid or many folds, and pretending otherwise by shrinking the
windows until the fold count looks respectable is how an overfit is
manufactured. If the harness calls this underpowered, that is the finding.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from trading_platform import (
    Backtester,
    RiskLimits,
    RiskManager,
    equity,
    walk_forward,
)
from trading_platform.analytics.walkforward import BacktesterFactory, Params, StrategyFactory
from trading_platform.data.cache import cache_path, read_bars
from trading_platform.strategy.base import Strategy
from trading_platform.strategy.examples.sma_crossover import SMACrossover

SYMBOL = "SPY"
MARKET = "etf"
INTERVAL = "1d"
CACHE = Path("data/cache")

#: US trading days in a year, not calendar days. Annualising daily equity bars
#: with 365 would overstate Sharpe by sqrt(365/252) — about 20% — and it is a
#: constant that is easy to get wrong and impossible to spot in the output.
PERIODS_PER_YEAR = 252

#: Nine cells, coarse on purpose. A wider grid over 150 training bars will find
#: something; that is a property of the grid, not of the market.
GRID: dict[str, list[object]] = {"fast": [5, 10, 20], "slow": [50, 80, 100]}

#: Sized against 499 bars. Warmup covers the longest lookback in the grid, or a
#: fold measures a strategy waiting for its slow window to fill.
TRAIN_SIZE = 150
TEST_SIZE = 40
WARMUP = 100


def make_strategy_for(symbol: str) -> StrategyFactory:
    """A factory bound to one symbol. A NEW strategy per run: they accumulate
    state across a backtest, so reusing one leaks a position into the next fold."""

    def make(params: Params) -> Strategy:
        return SMACrossover(symbol, **params)  # type: ignore[arg-type]

    return make


def make_backtester_for(symbol: str) -> BacktesterFactory:
    """A NEW Backtester and a NEW RiskManager per run, bound to one symbol."""

    def make() -> Backtester:
        return Backtester(
            instruments={symbol: equity(symbol)},
            starting_cash=Decimal("100000"),
            risk_manager=RiskManager(limits=RiskLimits()),
        )

    return make


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default=SYMBOL, help=f"default {SYMBOL}")
    parser.add_argument("--market", default=MARKET, help=f"default {MARKET}")
    args = parser.parse_args(argv)
    symbol, market = args.symbol, args.market

    path = cache_path(CACHE, market, symbol, INTERVAL)
    if not path.exists():
        raise SystemExit(
            f"No cached series at {path}.\n"
            f"Run: .venv/bin/python scripts/fetch_polygon.py {symbol} --market {market} "
            "--interval 1d --start 2024-09-10"
        )

    bars = read_bars(path, symbol)
    print(f"{len(bars):,} bars of real {symbol} {INTERVAL}")
    print(f"  {bars[0].timestamp:%Y-%m-%d} -> {bars[-1].timestamp:%Y-%m-%d} UTC")
    span_days = (bars[-1].timestamp - bars[0].timestamp).days
    print(f"  span {span_days:,} calendar days = {span_days / 365.25:.2f} years")
    print(f"  {len(bars) / (span_days / 7 * 5):.0%} of expected trading days present")
    print(
        f"  grid {len(GRID['fast']) * len(GRID['slow'])} cells, train {TRAIN_SIZE}, "
        f"test {TEST_SIZE}, warmup {WARMUP}"
    )

    report = walk_forward(
        bars,
        GRID,
        strategy_factory=make_strategy_for(symbol),
        backtester_factory=make_backtester_for(symbol),
        train_size=TRAIN_SIZE,
        test_size=TEST_SIZE,
        warmup=WARMUP,
        anchored=False,
        benchmark_symbol=symbol,
        periods_per_year=PERIODS_PER_YEAR,
    )

    print(f"\n{'=' * 78}\nREAL {symbol} 1d — rolling walk-forward\n{'=' * 78}")
    print(report.report())


if __name__ == "__main__":
    main()
