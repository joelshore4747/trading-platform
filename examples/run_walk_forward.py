"""Walk-forward evaluation of SMACrossover on synthetic data.

Run with:  .venv/bin/python examples/run_walk_forward.py

Companion to ``run_backtest.py``, which reports a single in-sample number. This
one reports the number that matters: how much of an in-sample result survives
on data the parameters were not chosen on.

**The expected outcome is that it does not survive.** SMA crossover is the
"hello world" of systematic trading. If this script ever prints a clean verdict
on real data, the first assumption should be that the harness is broken.

Two scenarios run, and the contrast is the point:

- **Mixed regimes** — trend, chop, crash, recovery. An earlier generator's
  TREND regime carried an embedded annualised Sharpe near 6, and almost
  anything that held a long position looked good; that result was retracted
  (experiment E-001) and the generator is now calibrated, with
  `MAX_REGIME_SHARPE` refusing any regime that pays more than a market does.
  The buy-and-hold benchmark is still passed, because the same trap exists in
  smaller form on any trending sample: without it, "profitable out-of-sample"
  would be an artefact of the data rather than a property of the strategy. On
  the calibrated generator the recorded result is efficiency 0.27, parameter
  stability 0%, chop control 0.18 (E-003) — the expected failure.
- **Pure chop** — a series with no exploitable structure at all. Any edge
  reported here is definitionally a coincidence, so this is the harness's own
  control: the verdicts must fire.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading_platform import (
    Backtester,
    RiskLimits,
    RiskManager,
    crypto_spot,
    walk_forward,
)
from trading_platform.analytics.walkforward import Params
from trading_platform.data.synthetic import Regime, RegimeSpec, generate_bars
from trading_platform.strategy.base import Strategy
from trading_platform.strategy.examples.sma_crossover import SMACrossover

SYMBOL = "BTCUSDT"
START = datetime(2021, 1, 1, tzinfo=UTC)
SEED = 7

#: Coarse on purpose. A 200-cell grid on 300 training bars will find something;
#: that is a property of the grid, not of the market.
GRID: dict[str, list[object]] = {"fast": [5, 10, 15, 20], "slow": [30, 50, 80, 120]}

MIXED = [
    RegimeSpec(Regime.TREND, 200),
    RegimeSpec(Regime.CHOP, 200),
    RegimeSpec(Regime.CRASH, 60),
    RegimeSpec(Regime.RECOVERY, 120),
    RegimeSpec(Regime.CHOP, 200),
    RegimeSpec(Regime.TREND, 120),
]
CHOP_ONLY = [RegimeSpec(Regime.CHOP, 900)]


def make_strategy(params: Params) -> Strategy:
    """A NEW strategy per run. They accumulate state across a backtest."""
    return SMACrossover(SYMBOL, **params)  # type: ignore[arg-type]


def make_backtester() -> Backtester:
    """A NEW Backtester per run, with a NEW RiskManager.

    Not an optimisation detail: ``RiskManager`` carries peak-equity state, so
    one shared instance would leak a drawdown halt out of an early fold and
    silently report zeros for every fold after it. Default ``CostModel`` —
    ``ZERO_COST`` isolates engine bugs and is never evidence.
    """
    return Backtester(
        instruments={SYMBOL: crypto_spot(SYMBOL)},
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=RiskLimits()),
    )


def run(label: str, regimes: list[RegimeSpec], *, anchored: bool) -> None:
    bars = generate_bars(SYMBOL, START, regimes, seed=SEED, start_price=Decimal("30000"))
    report = walk_forward(
        bars,
        GRID,
        strategy_factory=make_strategy,
        backtester_factory=make_backtester,
        train_size=300,
        test_size=100,
        # At least the longest lookback in the grid, or short folds measure
        # mostly a strategy waiting for its slow window to fill.
        warmup=120,
        anchored=anchored,
        benchmark_symbol=SYMBOL,
    )

    mode = "anchored" if anchored else "rolling"
    print(f"\n{'=' * 78}\n{label} — {mode}\n{'=' * 78}")
    print(report.report())


def main() -> None:
    for anchored in (False, True):
        run("MIXED REGIMES (trend/chop/crash/recovery)", MIXED, anchored=anchored)
    run("PURE CHOP — no edge exists; the verdicts must fire", CHOP_ONLY, anchored=False)

    print(f"\n{'=' * 78}")
    print(
        "Reading this output:\n"
        "  - EFFICIENCY is out-of-sample over in-sample. Below ~0.5 the parameters\n"
        "    were fitted. It is necessary, not sufficient: on pure noise the\n"
        "    out-of-sample scores scatter around zero and their mean can beat a\n"
        "    barely-positive in-sample mean, producing an efficiency above 1.0 for a\n"
        "    strategy with no edge whatsoever. That is why the grid, sample-size, and\n"
        "    benchmark checks exist alongside it.\n"
        "  - The 'hold' column is cost-free buy-and-hold over the same window. Losing\n"
        "    to it means the strategy spent commission and took risk to underperform\n"
        "    doing nothing.\n"
        "  - A run with no verdicts has not demonstrated an edge. It has failed to\n"
        "    falsify one, on this data, at this cost model."
    )


if __name__ == "__main__":
    main()
