"""Binance parser tests.

The first three sections pin the traps documented in ``data/binance.py``. Each
one, if unhandled, produces a backtest that runs cleanly and reports fiction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.data.binance import (
    INTERVAL_MS,
    BinanceParseError,
    FundingRate,
    interval_ms,
    kline_to_bar,
    klines_to_bars,
    normalise_epoch_ms,
    observed_funding_interval_hours,
    parse_funding_rates,
)

FIXTURES = Path(__file__).parent / "fixtures" / "binance"


def load(name: str):  # type: ignore[no-untyped-def]
    return json.loads((FIXTURES / name).read_text())


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


FAR_FUTURE_MS = ms(datetime(2030, 1, 1, tzinfo=UTC))


# -- Trap 1: close time is open + interval - 1ms ----------------------------


def test_bar_is_stamped_at_open_plus_interval_not_at_close_time_field() -> None:
    """Binance's own 1w doc example. closeTime is 1499644799999 — one ms early.

    The bar must be stamped 1499644800000. Reading the closeTime field directly
    would be 1ms early; using openTime would be a full week of lookahead.
    """
    rows = load("klines_1w_doc_example.json")
    bar = kline_to_bar(rows[0], "ETHBTC", "1w")

    assert ms(bar.timestamp) == 1499644800000
    assert ms(bar.timestamp) == rows[0][6] + 1, "must be closeTime + 1ms"
    assert ms(bar.timestamp) == rows[0][0] + INTERVAL_MS["1w"], "must be openTime + interval"
    assert ms(bar.timestamp) != rows[0][0], "stamping at openTime is a full interval of lookahead"


def test_ohlcv_parses_from_strings_without_precision_loss() -> None:
    rows = load("klines_1w_doc_example.json")
    bar = kline_to_bar(rows[0], "ETHBTC", "1w")

    assert bar.open == Decimal("0.01634790")
    assert bar.high == Decimal("0.80000000")
    assert bar.low == Decimal("0.01575800")
    assert bar.close == Decimal("0.01577100")
    assert bar.volume == Decimal("148976.11427815")
    # Exact string round-trip: proof nothing went through float.
    assert str(bar.volume) == "148976.11427815"


# -- Trap 2: the 2025 microsecond switch ------------------------------------


def test_microsecond_timestamps_are_normalised_to_milliseconds() -> None:
    assert normalise_epoch_ms(1735689600000) == 1735689600000  # ms passes through
    assert normalise_epoch_ms(1735689600000000) == 1735689600000  # µs collapses


def test_ms_and_us_rows_land_exactly_one_interval_apart() -> None:
    """The 2024/2025 archive boundary. Without normalisation the second bar
    lands ~50,000 years later — and still sorts monotonically, so an ordering
    check would not catch it."""
    rows = load("klines_1d_us_boundary.json")
    bars = klines_to_bars(rows, "BTCUSDT", "1d", now_ms=FAR_FUTURE_MS)

    assert len(bars) == 2
    delta = bars[1].timestamp - bars[0].timestamp
    assert delta == timedelta(days=1), f"expected exactly one day apart, got {delta}"
    assert bars[0].timestamp == datetime(2025, 1, 1, tzinfo=UTC)
    assert bars[1].timestamp == datetime(2025, 1, 2, tzinfo=UTC)


def test_unnormalised_microseconds_would_have_been_catastrophic() -> None:
    """Documents the size of the bug being prevented.

    Treating a 2025 microsecond timestamp as milliseconds lands in year ~56971,
    which datetime cannot even represent. In a loader that catches and skips
    bad rows, this becomes a silent hole in history rather than a loud failure.
    """
    raw_us = 1735689600000000
    with pytest.raises(ValueError, match=r"year .* is out of range"):
        datetime.fromtimestamp(raw_us / 1000, tz=UTC)

    # Normalised, it is an ordinary date.
    assert datetime.fromtimestamp(normalise_epoch_ms(raw_us) / 1000, tz=UTC) == datetime(
        2025, 1, 1, tzinfo=UTC
    )


# -- Trap 3: the still-forming final bar ------------------------------------


def test_still_forming_final_bar_is_dropped() -> None:
    rows = load("klines_1d_us_boundary.json")
    # "Now" is midway through the second bar's day, so that bar is incomplete.
    now = ms(datetime(2025, 1, 1, 12, tzinfo=UTC))
    bars = klines_to_bars(rows, "BTCUSDT", "1d", now_ms=now)

    assert len(bars) == 1, "the in-progress bar must not enter history"
    assert bars[0].timestamp == datetime(2025, 1, 1, tzinfo=UTC)


def test_bar_closing_exactly_now_is_kept() -> None:
    """A bar whose close is exactly `now` is complete, not forming."""
    rows = load("klines_1d_us_boundary.json")
    now = ms(datetime(2025, 1, 2, tzinfo=UTC))  # exactly bar 2's close
    bars = klines_to_bars(rows, "BTCUSDT", "1d", now_ms=now)
    assert len(bars) == 2


def test_drop_forming_can_be_disabled_for_archive_files() -> None:
    rows = load("klines_1d_us_boundary.json")
    bars = klines_to_bars(rows, "BTCUSDT", "1d", now_ms=0, drop_forming=False)
    assert len(bars) == 2


# -- Interval handling ------------------------------------------------------


def test_all_fifteen_fixed_intervals_present() -> None:
    assert set(INTERVAL_MS) == {
        "1s",
        "1m",
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
        "6h",
        "8h",
        "12h",
        "1d",
        "3d",
        "1w",
    }


def test_monthly_interval_is_rejected_rather_than_approximated() -> None:
    for spelling in ("1M", "1mo"):
        with pytest.raises(BinanceParseError, match="calendar month"):
            interval_ms(spelling)


def test_interval_lookup_is_case_sensitive() -> None:
    assert interval_ms("1m") == 60_000  # minute
    with pytest.raises(BinanceParseError, match="calendar month"):
        interval_ms("1M")  # month — a different thing entirely


def test_unknown_interval_names_the_valid_set() -> None:
    with pytest.raises(BinanceParseError, match="Unknown interval"):
        interval_ms("7h")


# -- Structural validation --------------------------------------------------


def test_duplicate_open_times_are_rejected() -> None:
    row = load("klines_1w_doc_example.json")[0]
    with pytest.raises(BinanceParseError, match="duplicate bar"):
        klines_to_bars([row, row], "ETHBTC", "1w", now_ms=FAR_FUTURE_MS)


def test_out_of_order_rows_are_rejected() -> None:
    a = [1735603200000, "1", "1", "1", "1", "1", 0, "0", 0, "0", "0", "0"]
    b = [1735689600000, "1", "1", "1", "1", "1", 0, "0", 0, "0", "0", "0"]
    with pytest.raises(BinanceParseError, match="precedes"):
        klines_to_bars([b, a], "BTCUSDT", "1d", now_ms=FAR_FUTURE_MS)


def test_misaligned_interval_spacing_is_rejected() -> None:
    """Catches a mixed-interval merge, or an unnormalised unit boundary."""
    a = [1735603200000, "1", "1", "1", "1", "1", 0, "0", 0, "0", "0", "0"]
    b = [1735603200000 + 3_600_000, "1", "1", "1", "1", "1", 0, "0", 0, "0", "0", "0"]
    with pytest.raises(BinanceParseError, match="whole number of"):
        klines_to_bars([a, b], "BTCUSDT", "1d", now_ms=FAR_FUTURE_MS)


def test_gaps_are_permitted() -> None:
    """Exchange outages are real. Interpolating over them hides the problem."""
    a = [1735603200000, "1", "1", "1", "1", "1", 0, "0", 0, "0", "0", "0"]
    c = [1735603200000 + 3 * 86_400_000, "1", "1", "1", "1", "1", 0, "0", 0, "0", "0", "0"]
    bars = klines_to_bars([a, c], "BTCUSDT", "1d", now_ms=FAR_FUTURE_MS)
    assert len(bars) == 2
    assert bars[1].timestamp - bars[0].timestamp == timedelta(days=3)


def test_short_row_is_rejected() -> None:
    with pytest.raises(BinanceParseError, match="expected at least"):
        kline_to_bar([1735603200000, "1", "1"], "BTCUSDT", "1d")


def test_negative_epoch_is_rejected() -> None:
    with pytest.raises(BinanceParseError, match="Negative epoch"):
        normalise_epoch_ms(-1)


# -- Funding ----------------------------------------------------------------


def test_funding_rates_parse_as_fractions_not_percentages() -> None:
    rates = parse_funding_rates(load("funding_rate_history.json"))
    assert len(rates) == 3
    assert rates[0].rate == Decimal("0.00010000")  # 0.01%, not 0.0001%
    assert rates[1].rate == Decimal("-0.00025000"), "rates can be negative"
    assert rates[0].timestamp == datetime(2023, 10, 31, 16, tzinfo=UTC)


def test_missing_mark_price_is_tolerated() -> None:
    rates = parse_funding_rates(load("funding_rate_history.json"))
    assert rates[0].mark_price == Decimal("34287.54619963")
    assert rates[2].mark_price is None, "older records omit markPrice"


def test_funding_history_deduplicates_overlapping_pages() -> None:
    rows = load("funding_rate_history.json")
    assert len(parse_funding_rates(rows + rows)) == 3


def test_funding_payment_signs_are_correct_in_all_four_cases() -> None:
    ts = datetime(2023, 10, 31, 16, tzinfo=UTC)
    positive = FundingRate("BTCUSDT", ts, Decimal("0.0001"))
    negative = FundingRate("BTCUSDT", ts, Decimal("-0.0001"))
    long_notional = Decimal("10000")
    short_notional = Decimal("-10000")

    assert positive.payment_on(long_notional) == Decimal("-1.0"), "long pays on positive rate"
    assert positive.payment_on(short_notional) == Decimal("1.0"), "short receives"
    assert negative.payment_on(long_notional) == Decimal("1.0"), "long receives on negative rate"
    assert negative.payment_on(short_notional) == Decimal("-1.0"), "short pays"


def test_funding_interval_is_derived_from_data_not_assumed() -> None:
    """Binance moved some perps from 8h to 4h on 2023-10-12, so a multi-year
    series contains both regimes and any hardcoded value is wrong for part."""
    rates = parse_funding_rates(load("funding_rate_history.json"))
    assert observed_funding_interval_hours(rates) == Decimal(8)

    base = datetime(2024, 1, 1, tzinfo=UTC)
    four_hourly = [
        FundingRate("X", base + timedelta(hours=4 * i), Decimal("0.0001")) for i in range(6)
    ]
    assert observed_funding_interval_hours(four_hourly) == Decimal(4)

    assert observed_funding_interval_hours([]) is None
    assert observed_funding_interval_hours(four_hourly[:1]) is None


# -- The offline guarantee --------------------------------------------------


def test_parser_module_opens_no_sockets() -> None:
    """This module must stay testable without a network. Fetching belongs in
    scripts/fetch_binance.py."""
    import trading_platform.data.binance as mod

    source = Path(mod.__file__).read_text()
    for forbidden in ("import requests", "import httpx", "urllib.request", "import socket"):
        assert forbidden not in source, f"{forbidden} must not appear in the parser"
