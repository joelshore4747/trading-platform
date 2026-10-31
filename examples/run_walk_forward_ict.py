"""Walk-forward the ICT sweep-reversal on the REAL BTCUSDT archive.

    .venv/bin/python examples/run_walk_forward_ict.py

**The expected outcome is rejection.** These concepts are widely taught and
thinly evidenced; the point of mechanising them is to find out, and this
repository's prime directive is that the default outcome of a strategy is "this
does not work". A clean verdict should be read as evidence of a broken harness
before it is read as an edge.

Everything that bounds this run, stated before it (all from `docs/ROADMAP.md`
step 25's measurements, not guesses):

- **Coverage is 46%**, in 18 segments. A fold boundary can straddle a multi-day
  hole, and a "swing high" either side of one is not a swing anybody saw.
- **The missing time is not random.** Hours 07-10 UTC average 56% coverage
  against 25% for 18-23 UTC. This strategy has a **time-of-day filter**, so it
  is exposed to that bias more directly than anything run here before: the
  New York morning killzone sits in the better-covered hours. A result could be
  a property of the gaps.
- **These are perpetuals with no funding series.** Carry is unmodelled, so any
  number is a price result and not a tradeable one.
- **32 days of one instrument is one regime.**

And one more, specific to this strategy: **the killzones are New York local
hours applied to a 24-hour crypto market.** The idea comes from index futures,
where the session boundaries are real. Whether they mean anything for a
perpetual that never closes is itself part of what is being tested, and the
`use_killzones=False` arm is in the grid so the filter has to earn its place.
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
from trading_platform.strategy.examples.ict_sweep_reversal import ICTSweepReversal

SYMBOL = "BTCUSDT"
MARKET = "um"
INTERVAL = "1m"
CACHE = Path("data/cache")
PERIODS_PER_YEAR = 365 * 24 * 60

#: Twelve cells, coarse, written down before the run. `use_killzones` is IN the
#: grid rather than fixed, so the filter competes rather than being assumed —
#: and if the winning cell always has it off, that is the finding.
GRID: dict[str, list[object]] = {
    "strength": [2, 5, 10],
    "hold": [15, 60],
    "use_killzones": [True, False],
}

TRAIN_SIZE = 3000
TEST_SIZE = 1000
#: Enough for the widest fractal to confirm and be swept several times over.
WARMUP = 300


def make_strategy(params: Params) -> Strategy:
    return ICTSweepReversal(SYMBOL, **params)  # type: ignore[arg-type]


def make_backtester() -> Backtester:
    """A NEW engine and risk manager per run: `RiskManager` carries peak-equity
    state, so one shared instance leaks a drawdown halt between folds.

    Default `CostModel`, never `ZERO_COST`. Fully collateralised at 1x, because
    the archive has no funding series and taking leverage on top of an
    unmodelled cost compounds one unknown with another.
    """
    return Backtester(
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
    cells = len(GRID["strength"]) * len(GRID["hold"]) * len(GRID["use_killzones"])
    print(f"  grid {cells} cells, train {TRAIN_SIZE}, test {TEST_SIZE}, warmup {WARMUP}")

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
    print(f"\n{'=' * 78}\nREAL BTCUSDT 1m — ICT sweep reversal, rolling walk-forward\n{'=' * 78}")
    print(report.report())
    print(f"\n{'=' * 78}")
    print(
        "What this run cannot tell you, whatever the numbers say:\n"
        "  - Whether it survives funding. Carry is unmodelled entirely.\n"
        "  - Whether the time filter is measuring the market or the gaps.\n"
        "    Coverage is 56% in hours 07-10 UTC and 25% in 18-23.\n"
        "  - Whether it generalises. One instrument, 32 days, one regime.\n"
        "  - Whether the swing levels are real. A 'swing high' either side of a\n"
        "    multi-day hole is not a level anybody was looking at.\n"
        "A run with no verdicts has not demonstrated an edge. It has failed to\n"
        "falsify one, on this data, at this cost model."
    )


if __name__ == "__main__":
    main()
