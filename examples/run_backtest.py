"""End-to-end smoke run: SMA crossover on deterministic synthetic data.

Run with:  .venv/bin/python examples/run_backtest.py

This exists so that "does the platform work" is answerable in five seconds
without reading any code. The strategy is not expected to make money — see the
note at the bottom of the output.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading_platform import (
    ZERO_COST,
    Backtester,
    CostModel,
    RiskLimits,
    RiskManager,
    crypto_spot,
    summarise,
    synthetic_walk,
)
from trading_platform.strategy.examples.sma_crossover import SMACrossover

SYMBOL = "BTCUSDT"
CRYPTO_PERIODS_PER_YEAR = 365  # crypto trades continuously — not 252


def main() -> None:
    instrument = crypto_spot(SYMBOL)
    start = datetime(2025, 1, 1, tzinfo=UTC)

    def run(cost_model: CostModel, label: str) -> None:
        feed = synthetic_walk(SYMBOL, start, periods=500, start_price=Decimal("40000"))
        backtester = Backtester(
            instruments={SYMBOL: instrument},
            starting_cash=Decimal("100000"),
            cost_model=cost_model,
            # A fresh RiskManager per run: it carries peak-equity state.
            risk_manager=RiskManager(limits=RiskLimits()),
        )
        result = backtester.run(SMACrossover(SYMBOL, fast=10, slow=30), feed)
        summary = summarise(result.equity_curve, periods_per_year=CRYPTO_PERIODS_PER_YEAR)

        print(f"\n{'=' * 62}\n{label}\n{'=' * 62}")
        print(result.summary())
        print("-" * 62)
        print(summary.report())
        if result.rejections:
            print("-" * 62)
            print(f"Risk rejections     : {result.rejection_summary_text()}")

    run(ZERO_COST, "FRICTIONLESS (ZERO_COST) — engine check only, NOT a result")
    run(CostModel(), "REALISTIC COSTS — this is the number that counts")

    print(f"\n{'=' * 62}")
    print(
        "Reminder: SMA crossover is the 'hello world' of systematic trading.\n"
        "If the realistic-cost run looks profitable here, that is an artefact\n"
        "of synthetic data with positive drift — not evidence of an edge.\n"
        "Real evaluation starts at docs/PLAN.md Phase 2."
    )


if __name__ == "__main__":
    main()
