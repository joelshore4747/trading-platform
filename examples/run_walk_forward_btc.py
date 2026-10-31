"""Walk-forward evaluation of SMACrossover on the REAL BTCUSDT archive.

Run with:  .venv/bin/python examples/run_walk_forward_btc.py

Roadmap step 35. Its companion `run_walk_forward.py` runs the same strategy on
the synthetic generator; this one runs it on 1-minute Binance perpetual bars
that actually printed, and the difference between the two is the point.

**The expected outcome is failure.** SMA crossover is the "hello world" of
systematic trading and this repository's prime directive is that the default
outcome of a strategy is "this does not work". A clean verdict here should be
read as evidence of a broken harness before it is read as an edge.

Four properties of this data bound what any number below can mean, and all four
are `docs/ROADMAP.md` step 25's findings rather than guesses:

- **Coverage is 46%**, in 18 segments, longest contiguous run ~2,900 bars.
  A fold boundary can therefore straddle a multi-day hole.
- **The missing time is not random.** Hours 07-10 UTC average 56% coverage
  against 25% for 18-23 UTC, so anything time-of-day sensitive is biased here.
- **These are perpetuals and no funding series ships with the archive.** Carry
  is unmodelled, so a result is a *price* result, not a tradeable one.
- **32 days.** One month of one instrument is one regime with a small sample,
  whatever the bar count says.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from trading_platform import (
    Backtester,
    RiskLimits,
    RiskManager,
    crypto_perpetual,
    walk_forward,
)
from trading_platform.analytics.walkforward import Params
from trading_platform.data.cache import cache_path, read_bars
from trading_platform.strategy.base import Strategy
from trading_platform.strategy.examples.sma_crossover import SMACrossover

SYMBOL = "BTCUSDT"
MARKET = "um"
INTERVAL = "1m"
CACHE = Path("data/cache")

#: Minutes in a year. Annualising 1-minute bars with 365 would understate
#: Sharpe by sqrt(1440); getting this constant wrong scales every risk-adjusted
#: figure by a constant and is easy to miss.
PERIODS_PER_YEAR = 365 * 24 * 60

#: Coarse on purpose, and stated before the run rather than tuned after it.
#: Sixteen cells. A wider grid on this many training bars will find something —
#: that is a property of the grid, not of the market.
GRID: dict[str, list[object]] = {"fast": [10, 30, 60, 120], "slow": [240, 480, 720, 1440]}

#: Sized in minutes. 3,000 training bars is about two days of *observed* data,
#: not two days of wall clock — the gaps mean those are different things.
TRAIN_SIZE = 3000
TEST_SIZE = 1000
#: At least the longest lookback in the grid, or a fold measures a strategy
#: waiting for its slow window to fill.
WARMUP = 1440


def make_strategy(params: Params) -> Strategy:
    """A NEW strategy per run. They accumulate state across a backtest."""
    return SMACrossover(SYMBOL, **params)  # type: ignore[arg-type]


def make_backtester() -> Backtester:
    """A NEW Backtester and a NEW RiskManager per run.

    `RiskManager` carries peak-equity state, so one shared instance would leak
    a drawdown halt out of an early fold into every later one. Default
    `CostModel` — `ZERO_COST` isolates engine bugs and is never evidence.

    The instrument is a **perpetual**, not spot: the archive is
    `binance_futures`, and filing it as spot would quietly apply the wrong fee
    tier and imply funding does not exist.
    """
    return Backtester(
        # Fully collateralised (1x) on purpose. The archive gives no funding
        # series, so carry is unmodelled; taking leverage on top of an
        # unmodelled cost would compound one unknown with another. State the
        # assumption rather than inheriting it.
        instruments={SYMBOL: crypto_perpetual(SYMBOL, initial_margin_rate=None)},
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=RiskLimits()),
    )


def main() -> None:
    path = cache_path(CACHE, MARKET, SYMBOL, INTERVAL)
    if not path.exists():
        raise SystemExit(
            f"No cached series at {path}.\n"
            "Run: .venv/bin/python scripts/import_vendor.py historical.zip"
        )

    bars = read_bars(path, SYMBOL)
    print(f"{len(bars):,} bars of real {SYMBOL} {INTERVAL}")
    print(f"  {bars[0].timestamp:%Y-%m-%d %H:%M} -> {bars[-1].timestamp:%Y-%m-%d %H:%M} UTC")
    span_minutes = int((bars[-1].timestamp - bars[0].timestamp).total_seconds() // 60) + 1
    print(f"  span {span_minutes:,} minutes, so coverage is {len(bars) / span_minutes:.0%}")
    print(
        f"  grid {len(GRID['fast']) * len(GRID['slow'])} cells, train {TRAIN_SIZE}, "
        f"test {TEST_SIZE}, warmup {WARMUP}"
    )

    report = walk_forward(
        bars,
        GRID,
        strategy_factory=make_strategy,
        backtester_factory=make_backtester,
        train_size=TRAIN_SIZE,
        test_size=TEST_SIZE,
        warmup=WARMUP,
        anchored=False,
        benchmark_symbol=SYMBOL,
        periods_per_year=PERIODS_PER_YEAR,
    )

    print(f"\n{'=' * 78}\nREAL BTCUSDT 1m — rolling walk-forward\n{'=' * 78}")
    print(report.report())

    print(f"\n{'=' * 78}")
    print(
        "What this run cannot tell you, regardless of the numbers above:\n"
        "  - Whether it survives funding. These are perpetuals and the archive\n"
        "    ships no funding series, so carry is unmodelled entirely.\n"
        "  - Whether it generalises. 32 days of one instrument is one regime.\n"
        "  - Whether the gaps matter. Coverage is 46% in 18 segments, and a fold\n"
        "    boundary can straddle a multi-day hole.\n"
        "A run with no verdicts has not demonstrated an edge. It has failed to\n"
        "falsify one, on this data, at this cost model."
    )


if __name__ == "__main__":
    main()
