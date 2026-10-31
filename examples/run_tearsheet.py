"""Render a tearsheet for SMACrossover on synthetic data.

Run with:  .venv/bin/python examples/run_tearsheet.py

Writes to ``runs/`` (gitignored — a tearsheet is regenerable, and a committed
one invites reading a stale chart as current).

The page is deliberately hard to be flattered by. It renders at the default
``CostModel``, draws cost-free buy-and-hold on the same axis, shades the
out-of-sample windows from a real walk-forward run, and prints every objection
the harness raised **above** the equity curve rather than beneath it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading_platform import (
    Backtester,
    InMemoryBarFeed,
    RiskLimits,
    RiskManager,
    crypto_spot,
    walk_forward,
)
from trading_platform.analytics.tearsheet import write_tearsheet
from trading_platform.analytics.trades import summarise_trades
from trading_platform.analytics.walkforward import Params, buy_and_hold_curve
from trading_platform.analytics.windows import TestWindow
from trading_platform.data.synthetic import Regime, RegimeSpec, generate_bars
from trading_platform.strategy.base import Strategy
from trading_platform.strategy.examples.sma_crossover import SMACrossover

SYMBOL = "BTCUSDT"
START = datetime(2021, 1, 1, tzinfo=UTC)
SEED = 7
GRID: dict[str, list[object]] = {"fast": [5, 10, 15, 20], "slow": [30, 50, 80, 120]}
REGIMES = [
    RegimeSpec(Regime.TREND, 200),
    RegimeSpec(Regime.CHOP, 200),
    RegimeSpec(Regime.CRASH, 60),
    RegimeSpec(Regime.RECOVERY, 120),
    RegimeSpec(Regime.CHOP, 200),
    RegimeSpec(Regime.TREND, 120),
]
INSTRUMENTS = {SYMBOL: crypto_spot(SYMBOL)}


def make_strategy(params: Params) -> Strategy:
    return SMACrossover(SYMBOL, **params)  # type: ignore[arg-type]


def make_backtester() -> Backtester:
    """Fresh every time — RiskManager carries peak-equity state."""
    return Backtester(
        instruments=INSTRUMENTS,
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=RiskLimits()),
    )


def main() -> None:
    bars = generate_bars(SYMBOL, START, REGIMES, seed=SEED, start_price=Decimal("30000"))

    # The walk-forward run supplies the out-of-sample shading and the verdicts.
    report = walk_forward(
        bars,
        GRID,
        strategy_factory=make_strategy,
        backtester_factory=make_backtester,
        train_size=300,
        test_size=100,
        warmup=120,
        benchmark_symbol=SYMBOL,
    )

    # The single run the page charts, using the parameters the LAST fold chose.
    # Its equity curve is in-sample over most of its length, which is exactly
    # what the shading and the banner exist to say out loud.
    chosen = report.folds[-1].params
    backtester = make_backtester()
    result = backtester.run(make_strategy(chosen), InMemoryBarFeed(bars))

    window = TestWindow(bars=tuple(bars))
    benchmark = buy_and_hold_curve(window, SYMBOL, starting_cash=result.starting_cash)

    path = write_tearsheet(
        result,
        Path("runs") / "sma_crossover_tearsheet.html",
        cost_model=backtester.cost_model,
        instruments=INSTRUMENTS,
        benchmark=benchmark,
        walk_forward=report,
        periods_per_year=365,
        title=f"SMACrossover {chosen} — {SYMBOL} synthetic",
    )

    trades = summarise_trades(result.fills, INSTRUMENTS)
    print(result.summary())
    print("-" * 62)
    print(trades.report())
    print("-" * 62)
    print(f"Walk-forward objections: {len(report.verdicts)}")
    for i, verdict in enumerate(report.verdicts, start=1):
        print(f"  {i}. {verdict}")
    print("-" * 62)
    print(f"Tearsheet written to {path}")


if __name__ == "__main__":
    main()
