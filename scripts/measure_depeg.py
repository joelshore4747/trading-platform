"""Measure what a stablecoin depeg does to a backtest's reported figures.

Produces the tables in ``docs/adr/0002-quote-currency.md``. Run it before
believing them:

    .venv/bin/python scripts/measure_depeg.py

The point of the exercise is the first row of the first table: a depeg that
recovers inside the sample leaves **total return exactly unchanged** while
inflating measured volatility 2.7x. The number quoted first is the number
blind to the problem.

Uses the real BTCUSDT minute bars in ``historical.zip`` and a simulated rate
path, because no USDT/USD series can be fetched in this environment.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal

from trading_platform import (
    Backtester,
    CostModel,
    QuoteRates,
    RateObservation,
    RiskLimits,
    RiskManager,
    summarise,
    translate_curve,
)
from trading_platform.core.events import Bar
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.data.vendor import instrument_for, load_bars
from trading_platform.strategy.examples.sma_crossover import SMACrossover

SYMBOL = "BTCUSDT"
ARCHIVE = "historical.zip"
MEMBER = "historical/btcusdt_1m.csv"
PERIODS_PER_YEAR = 365 * 1440

#: Depeg magnitudes to simulate. The largest matches the scale of the March
#: 2023 USDC episode; it is a scenario parameter, not a venue print. There was
#: no single price that day — reported lows ran from ~$0.87 to ~$0.74 across
#: venues. See docs/adr/0002-quote-currency.md.
DEPEG_LOWS = {
    "1.00%": Decimal("0.99"),
    "5.00%": Decimal("0.95"),
    "12.11%": Decimal("0.8789"),
}


def real_bars() -> list[Bar]:
    with zipfile.ZipFile(ARCHIVE) as archive, archive.open(MEMBER) as handle:
        text = io.TextIOWrapper(handle, encoding="utf-8")
        return list(load_bars(text, symbol=SYMBOL, stamp="open"))


def depeg_rates(
    bars: list[Bar],
    *,
    low: Decimal,
    onset_hours: int = 6,
    recovery_hours: int = 72,
    recovers: bool = True,
) -> QuoteRates:
    """A USDT/USD path: 1.0, a slide to ``low``, then a recovery or not.

    Onset is placed a third of the way through the sample so there is history
    before it and room to recover after it.
    """
    first, last = bars[0].timestamp, bars[-1].timestamp
    onset = first + (last - first) / 3
    trough = onset + timedelta(hours=onset_hours)
    restored = trough + timedelta(hours=recovery_hours)

    observations = []
    for bar in bars:
        at = bar.timestamp
        observations.append(
            RateObservation(at, _rate_at(at, onset, trough, restored, low, recovers))
        )

    rates = QuoteRates(reporting_currency="USD")
    rates.add("USDT", observations)
    return rates


def _rate_at(
    at: datetime,
    onset: datetime,
    trough: datetime,
    restored: datetime,
    low: Decimal,
    recovers: bool,
) -> Decimal:
    one = Decimal("1")
    if at < onset:
        return one
    if at < trough:
        progress = Decimal((at - onset).total_seconds()) / Decimal((trough - onset).total_seconds())
        return one - (one - low) * progress
    if not recovers:
        return low
    if at < restored:
        progress = Decimal((at - trough).total_seconds()) / Decimal(
            (restored - trough).total_seconds()
        )
        return low + (one - low) * progress
    return one


def run(bars: list[Bar]):  # type: ignore[no-untyped-def]
    backtester = Backtester(
        instruments={SYMBOL: instrument_for(SYMBOL, "binance-perp")},
        starting_cash=Decimal("100000"),
        cost_model=CostModel(),
        risk_manager=RiskManager(limits=RiskLimits()),
    )
    return backtester.run(SMACrossover(SYMBOL, fast=20, slow=60), InMemoryBarFeed(bars))


def main() -> None:
    bars = real_bars()
    print(f"{SYMBOL}: {len(bars)} bars, {bars[0].timestamp} -> {bars[-1].timestamp}\n")

    result = run(bars)
    quoted = summarise(result.equity_curve, periods_per_year=PERIODS_PER_YEAR, currency="USDT")
    print("Same trades throughout. Only the denomination of the report changes.\n")
    print(f"{'depeg':>7} {'recovers':>9} {'return':>11} {'maxDD':>10} {'vol':>10} {'sharpe':>9}")
    print(
        f"{'none':>7} {'-':>9} {quoted.total_return:>11.4%} {quoted.max_drawdown:>10.4%}"
        f" {quoted.annualised_volatility:>10.4%} {quoted.sharpe_ratio:>9.3f}   <- USDT"
    )

    for label, low in DEPEG_LOWS.items():
        for recovers in (True, False):
            rates = depeg_rates(bars, low=low, recovers=recovers)
            usd = translate_curve(
                result.equity_curve, from_currency="USDT", to_currency="USD", rates=rates
            )
            summary = summarise(usd, periods_per_year=PERIODS_PER_YEAR, currency="USD")
            print(
                f"{label:>7} {recovers!s:>9} {summary.total_return:>11.4%}"
                f" {summary.max_drawdown:>10.4%} {summary.annualised_volatility:>10.4%}"
                f" {summary.sharpe_ratio:>9.3f}"
            )

    print(
        "\nRead the 'recovers=True' rows against the USDT row: total return is\n"
        "identical to the digit, and every path statistic is not. That is why\n"
        "quoting total return alone hides this entirely."
    )


if __name__ == "__main__":
    main()
