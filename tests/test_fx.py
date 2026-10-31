"""Quote-currency rates, and the refusal to guess one.

The property under test throughout: a rate is known or it is an error. There
is no fallback to 1.0, because the fallback is wrong exactly when it matters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform import (
    Backtester,
    Bar,
    RiskLimits,
    RiskManager,
    Strategy,
    StrategyContext,
    crypto_spot,
    equity,
    summarise,
)
from trading_platform.core.fx import (
    MixedCurrencyError,
    QuoteRates,
    RateObservation,
    StaleQuoteRate,
    UnknownQuoteRate,
    assume_pegged,
)
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.engine.portfolio import EquityPoint, translate_curve

START = datetime(2026, 1, 1, tzinfo=UTC)


def obs(day: int, rate: str) -> RateObservation:
    return RateObservation(START + timedelta(days=day), Decimal(rate))


# -- A rate is known, or it is an error --------------------------------------


def test_an_unknown_asset_raises_rather_than_assuming_parity() -> None:
    rates = QuoteRates()
    with pytest.raises(UnknownQuoteRate, match="no rate series"):
        rates.rate("USDT", START)


def test_the_reporting_currency_is_one_and_cannot_be_overridden() -> None:
    rates = QuoteRates(reporting_currency="USD")
    assert rates.rate("USD", START) == Decimal("1")
    with pytest.raises(ValueError, match="reporting currency"):
        rates.add("USD", [obs(0, "1.02")])


def test_a_lookup_before_the_series_starts_raises() -> None:
    """Extending a series backwards would assume a rate nobody observed."""
    rates = QuoteRates()
    rates.add("USDT", [obs(10, "1.0")])
    with pytest.raises(UnknownQuoteRate, match="at or before"):
        rates.rate("USDT", START)


def test_a_rate_too_old_to_stand_in_raises() -> None:
    rates = QuoteRates(max_staleness=timedelta(days=2))
    rates.add("USDT", [obs(0, "1.0")])
    assert rates.rate("USDT", START + timedelta(days=2)) == Decimal("1.0")
    with pytest.raises(StaleQuoteRate, match="stale"):
        rates.rate("USDT", START + timedelta(days=2, seconds=1))


def test_a_rate_observation_rejects_naive_stamps_and_impossible_values() -> None:
    with pytest.raises(ValueError, match="no timezone"):
        RateObservation(datetime(2026, 1, 1), Decimal("1"))
    with pytest.raises(ValueError, match="positive"):
        RateObservation(START, Decimal("0"))


# -- Lookup steps back, never forward ----------------------------------------


def test_lookup_never_reaches_a_later_observation() -> None:
    """The same discipline as the one-bar fill delay.

    A rate observed on day 5 must not inform a mark on day 3, however much
    closer it is in time. Interpolating toward the next observation is the
    lookahead bug wearing an FX hat.
    """
    rates = QuoteRates()
    rates.add("USDT", [obs(0, "1.00"), obs(5, "0.90")])

    assert rates.rate("USDT", START + timedelta(days=3)) == Decimal("1.00")
    assert rates.rate("USDT", START + timedelta(days=4, hours=23)) == Decimal("1.00")
    # Stamped at the close of its interval, so it is known at its own instant.
    assert rates.rate("USDT", START + timedelta(days=5)) == Decimal("0.90")


def test_series_are_sorted_and_deduped_on_the_way_in() -> None:
    rates = QuoteRates()
    rates.add("USDT", [obs(5, "0.90"), obs(0, "1.00")])
    rates.add("USDT", [obs(5, "0.95")])  # later write wins
    assert rates.rate("USDT", START + timedelta(days=5)) == Decimal("0.95")
    assert rates.rate("USDT", START) == Decimal("1.00")
    assert rates.assets == ("USDT",)


# -- Asserting the peg is a decision someone types ---------------------------


def test_assume_pegged_covers_only_what_it_names() -> None:
    rates = assume_pegged("USDT")
    assert rates.rate("USDT", datetime(1999, 1, 1, tzinfo=UTC)) == Decimal("1")
    assert rates.covers("USDT")
    assert not rates.covers("BUSD")
    with pytest.raises(UnknownQuoteRate):
        rates.rate("BUSD", START)


def test_a_peg_and_a_measured_series_coexist() -> None:
    rates = assume_pegged("USDC")
    rates.add("USDT", [obs(0, "0.9485")])
    assert rates.rate("USDC", START) == Decimal("1")
    assert rates.rate("USDT", START) == Decimal("0.9485")


# -- Translation carries the path, not the endpoints -------------------------


def flat_curve(days: int, value: str = "100000") -> list[EquityPoint]:
    """A strategy that did precisely nothing: no return, no drawdown, no vol."""
    return [
        EquityPoint(
            timestamp=START + timedelta(days=d),
            equity=Decimal(value),
            cash=Decimal(value),
            positions_value=Decimal("0"),
        )
        for d in range(days)
    ]


def test_same_currency_needs_no_rates_at_all() -> None:
    curve = flat_curve(3)
    same = translate_curve(curve, from_currency="USD", to_currency="USD", rates=None)
    assert [p.equity for p in same] == [p.equity for p in curve]


def test_translating_without_rates_raises() -> None:
    with pytest.raises(UnknownQuoteRate, match="without a rate series"):
        translate_curve(flat_curve(3), from_currency="USDT", to_currency="USD", rates=None)


def test_translating_into_a_currency_the_rates_do_not_report_raises() -> None:
    rates = assume_pegged("USDT", reporting_currency="USD")
    with pytest.raises(ValueError, match="report into"):
        translate_curve(flat_curve(3), from_currency="USDT", to_currency="EUR", rates=rates)


def test_a_recovering_depeg_leaves_total_return_untouched_and_wrecks_the_rest() -> None:
    """The finding that makes this whole module worth having.

    A depeg that recovers inside the sample moves the endpoints not at all, so
    **total return — the number quoted first — is exactly unchanged.** Every
    path statistic moves a lot. On the real 32-day BTCUSDT sample the same
    trades measured 25.12% max drawdown and 20.07% volatility in USDT against
    27.64% and 54.61% in USD, a 2.7x understatement of volatility.

    Here the strategy is perfectly flat, so the USD curve's entire drawdown is
    the quote asset's, and the arithmetic is exact rather than incidental.
    """
    curve = flat_curve(9)
    rates = QuoteRates()
    # 1.00 -> 0.8789 -> back to 1.00. A 12.11% fall, the scale of the March
    # 2023 USDC episode; a scenario parameter, not a venue print.
    rates.add(
        "USDT",
        [
            obs(0, "1.0"),
            obs(1, "1.0"),
            obs(2, "1.0"),
            obs(3, "0.9400"),
            obs(4, "0.8789"),
            obs(5, "0.9400"),
            obs(6, "1.0"),
            obs(7, "1.0"),
            obs(8, "1.0"),
        ],
    )
    usd = translate_curve(curve, from_currency="USDT", to_currency="USD", rates=rates)

    quoted = summarise(curve, periods_per_year=365, currency="USDT")
    reported = summarise(usd, periods_per_year=365, currency="USD")

    # Identical endpoints. This is the blind spot.
    assert quoted.total_return == reported.total_return == 0.0
    assert usd[0].equity == curve[0].equity
    assert usd[-1].equity == curve[-1].equity

    # And a completely different risk profile.
    assert quoted.max_drawdown == 0.0
    assert reported.max_drawdown == pytest.approx(0.1211, abs=1e-4)
    assert quoted.annualised_volatility == 0.0
    # 93.4% annualised, on a strategy that did nothing whatsoever.
    assert reported.annualised_volatility == pytest.approx(0.934, abs=1e-3)

    assert quoted.currency == "USDT"
    assert reported.currency == "USD"


def test_a_depeg_that_does_not_recover_moves_terminal_wealth_by_its_full_size() -> None:
    curve = flat_curve(4)
    rates = QuoteRates()
    rates.add("USDT", [obs(0, "1.0"), obs(1, "1.0"), obs(2, "0.8789"), obs(3, "0.8789")])
    usd = translate_curve(curve, from_currency="USDT", to_currency="USD", rates=rates)

    quoted_return = (curve[-1].equity - curve[0].equity) / curve[0].equity
    usd_return = (usd[-1].equity - usd[0].equity) / usd[0].equity
    assert quoted_return == Decimal("0")
    assert usd_return == pytest.approx(Decimal("-0.1211"), abs=1e-4)


# -- A run cannot report a currency it does not know -------------------------


class BuyOnce(Strategy):
    name = "BuyOnce"

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
        if ctx.is_flat(bar.symbol):
            ctx.buy(bar.symbol, Decimal("1"))


def bars_for(*symbols: str, days: int = 4) -> list[Bar]:
    return sorted(
        [
            Bar(
                symbol=sym,
                timestamp=START + timedelta(days=d),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("1"),
            )
            for d in range(days)
            for sym in symbols
        ],
        key=lambda b: (b.timestamp, b.symbol),
    )


def run_over(instruments: dict[str, object]) -> object:
    return Backtester(
        instruments=instruments,  # type: ignore[arg-type]
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=RiskLimits()),
    ).run(BuyOnce(), InMemoryBarFeed(bars_for(*instruments)))


def test_a_run_records_the_currency_its_figures_are_in() -> None:
    result = run_over({"BTCUSDT": crypto_spot("BTCUSDT")})
    assert result.quote_currencies == ("USDT",)  # type: ignore[attr-defined]
    assert result.quote_currency == "USDT"  # type: ignore[attr-defined]
    assert "USDT" in result.summary()  # type: ignore[attr-defined]


def test_a_usdt_run_refuses_to_report_usd_without_a_rate_series() -> None:
    """The exit criterion. Before this, every such run reported USD silently."""
    result = run_over({"BTCUSDT": crypto_spot("BTCUSDT")})
    with pytest.raises(UnknownQuoteRate, match="without a rate series"):
        result.curve_in("USD")  # type: ignore[attr-defined]

    # With an explicit assumption on the record, it proceeds.
    pegged = result.curve_in("USD", assume_pegged("USDT"))  # type: ignore[attr-defined]
    assert [p.equity for p in pegged] == [
        p.equity
        for p in result.equity_curve  # type: ignore[attr-defined]
    ]


def test_a_usd_quoted_run_needs_no_rates() -> None:
    result = run_over({"AAPL": equity("AAPL")})
    assert result.quote_currency == "USD"  # type: ignore[attr-defined]
    assert result.curve_in("USD") == result.equity_curve  # type: ignore[attr-defined]


def test_a_run_mixing_quote_currencies_refuses_to_name_one() -> None:
    """One cash balance cannot hold two currencies, so it has been summing
    unlike units. The figures are not merely mislabelled — they are nothing."""
    result = run_over({"BTCUSDT": crypto_spot("BTCUSDT"), "AAPL": equity("AAPL")})
    assert result.quote_currencies == ("USD", "USDT")  # type: ignore[attr-defined]
    with pytest.raises(MixedCurrencyError, match="unlike units"):
        _ = result.quote_currency  # type: ignore[attr-defined]
    assert "REFUSED" in result.summary()  # type: ignore[attr-defined]
