"""Render a tearsheet for SMACrossover on the real BTCUSDT 1m perpetual archive.

Same constants as examples/run_walk_forward_btc.py, plus the tearsheet step from
examples/run_tearsheet.py. Default CostModel, default RiskLimits.
Usage: python make_tearsheet.py <cache_dir> <out_html>
"""

from __future__ import annotations

import sys
import time
from decimal import Decimal
from pathlib import Path

from trading_platform import (
    Backtester,
    InMemoryBarFeed,
    RiskLimits,
    RiskManager,
    crypto_perpetual,
    walk_forward,
)
from trading_platform.analytics.tearsheet import write_tearsheet
from trading_platform.analytics.trades import summarise_trades
from trading_platform.analytics.walkforward import Params, buy_and_hold_curve
from trading_platform.analytics.windows import TestWindow
from trading_platform.data.cache import cache_path, read_bars
from trading_platform.strategy.base import Strategy
from trading_platform.strategy.examples.sma_crossover import SMACrossover

SYMBOL = "BTCUSDT"
PERIODS_PER_YEAR = 365 * 24 * 60
GRID: dict[str, list[object]] = {"fast": [10, 30, 60, 120], "slow": [240, 480, 720, 1440]}
INSTRUMENTS = {SYMBOL: crypto_perpetual(SYMBOL, initial_margin_rate=None)}


def make_strategy(params: Params) -> Strategy:
    return SMACrossover(SYMBOL, **params)  # type: ignore[arg-type]


def make_backtester() -> Backtester:
    return Backtester(
        instruments=INSTRUMENTS,
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=RiskLimits()),
    )


def main() -> None:
    cache, out = Path(sys.argv[1]), Path(sys.argv[2])
    bars = read_bars(cache_path(cache, "um", SYMBOL, "1m"), SYMBOL)
    t0 = time.perf_counter()
    report = walk_forward(
        bars,
        GRID,
        strategy_factory=make_strategy,
        backtester_factory=make_backtester,
        train_size=3000,
        test_size=1000,
        warmup=1440,
        anchored=False,
        benchmark_symbol=SYMBOL,
        periods_per_year=PERIODS_PER_YEAR,
    )
    print(f"walk-forward took {time.perf_counter() - t0:.1f}s")
    print(report.report())
    chosen = report.folds[-1].params
    backtester = make_backtester()
    result = backtester.run(make_strategy(chosen), InMemoryBarFeed(bars))
    benchmark = buy_and_hold_curve(
        TestWindow(bars=tuple(bars)), SYMBOL, starting_cash=result.starting_cash
    )
    write_tearsheet(
        result,
        out,
        cost_model=backtester.cost_model,
        instruments=INSTRUMENTS,
        benchmark=benchmark,
        walk_forward=report,
        periods_per_year=PERIODS_PER_YEAR,
        title=f"SMACrossover {chosen} — BTCUSDT 1m perpetual, real archive",
    )
    print(result.summary())
    print(summarise_trades(result.fills, INSTRUMENTS).report())
    print(f"verdicts: {len(report.verdicts)}")
    for v in report.verdicts:
        print("  -", v)
    print("wrote", out)


if __name__ == "__main__":
    main()
