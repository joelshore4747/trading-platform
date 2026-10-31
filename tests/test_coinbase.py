"""The Coinbase adapter.

Every test here is named after the defect it prevents, because each of the four
things Coinbase does differently from the other two vendors is SILENT when it
is wrong. None of them raises; all of them produce bars that look fine.

The load-bearing one is `test_the_array_is_low_high_open_close_not_ohlc`. The
wire order is `[time, low, high, open, close, volume]`, so a parser assuming
OHLC swaps open with low and high with close — and the resulting bar still
satisfies `high >= max(open, close)` and `low <= min(open, close)`, because the
set of four numbers is unchanged. Nothing downstream can detect it. The fixture
below therefore uses four values that are distinguishable in every position; a
fixture shaped like real OHLC data would pass under the wrong mapping, which is
the trap this file exists to avoid falling into.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.core.events import Bar
from trading_platform.data.coinbase import (
    SUPPORTED_GRANULARITY_S,
    CoinbaseParseError,
    bars_from_payload,
    coinbase_granularity,
    row_to_bar,
)
from trading_platform.data.intervals import UnknownIntervalError
from trading_platform.live.coinbase import (
    MAX_CANDLES_PER_REQUEST,
    USER_AGENT,
    CoinbaseMarketData,
)

#: Four values that are distinct in every field, so a mis-ordered read lands
#: somewhere provably wrong. Deliberately NOT shaped like a plausible candle.
T0 = 1_788_825_600  # 2026-09-08T00:00:00Z
LOW, HIGH, OPEN, CLOSE, VOLUME = 10.0, 40.0, 20.0, 30.0, 7.5
CANDLE = [T0, LOW, HIGH, OPEN, CLOSE, VOLUME]

#: Far enough ahead that nothing in these fixtures is still forming, so a
#: test about ordering is about ordering and not accidentally about clocks.
FAR_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


# -- The array order ----------------------------------------------------------


def test_the_array_is_low_high_open_close_not_ohlc() -> None:
    bar = row_to_bar(CANDLE, "BTC-USD", "1d")
    assert bar.low == Decimal("10.0"), "row[1] is the LOW"
    assert bar.high == Decimal("40.0"), "row[2] is the HIGH"
    assert bar.open == Decimal("20.0"), "row[3] is the OPEN, after the high"
    assert bar.close == Decimal("30.0"), "row[4] is the CLOSE"
    assert bar.volume == Decimal("7.5")


def test_an_ohlc_misread_is_caught_by_bar_validation_for_any_real_candle() -> None:
    """The safety net, asserted rather than assumed — and it is a real one.

    Reading the array as [time, o, h, l, c, v] puts the true OPEN into `low`
    and the true LOW into `open`. Since low <= open always holds on a real
    candle, the misread produces low > open, and `Bar.__post_init__` refuses it
    with "open outside [low, high]".

    So this failure is LOUD, not silent. The field-by-field test above still
    earns its place for the one case this does not cover — a candle where open
    == low exactly, which is common in an illiquid minute — and because a
    high/close confusion produces a different, quieter wrongness.
    """
    real = [T0, 77589.0, 79477.07, 79091.97, 78579.99, 3205.38]
    _t, wrong_open, wrong_high, wrong_low, wrong_close, _v = real
    with pytest.raises(ValueError, match="outside"):
        Bar(
            symbol="BTC-USD",
            timestamp=datetime.fromtimestamp(T0, UTC),
            open=Decimal(str(wrong_open)),
            high=Decimal(str(wrong_high)),
            low=Decimal(str(wrong_low)),
            close=Decimal(str(wrong_close)),
            volume=Decimal("1"),
        )

    # And the correct reading constructs cleanly from the same row.
    assert row_to_bar(real, "BTC-USD", "1d").open == Decimal("79091.97")


def test_the_degenerate_candle_the_safety_net_misses() -> None:
    """open == low, so the misread stays inside [low, high] and Bar accepts it.

    This is the gap the field-by-field test covers and validation cannot: an
    illiquid minute that opens on its low is ordinary, and there the wrong
    mapping is silent.
    """
    degenerate = [T0, 100.0, 110.0, 100.0, 105.0, 1.0]  # low == open == 100
    _t, wrong_open, wrong_high, wrong_low, wrong_close, _v = degenerate
    accepted = Bar(
        symbol="BTC-USD",
        timestamp=datetime.fromtimestamp(T0, UTC),
        open=Decimal(str(wrong_open)),
        high=Decimal(str(wrong_high)),
        low=Decimal(str(wrong_low)),
        close=Decimal(str(wrong_close)),
        volume=Decimal("1"),
    )
    assert accepted.open == Decimal("100.0"), (
        "the misread was accepted; only the parser guards this"
    )


# -- The time unit and the stamping -------------------------------------------


def test_time_is_seconds_not_milliseconds() -> None:
    bar = row_to_bar(CANDLE, "BTC-USD", "1d")
    # 1788825600 as seconds is 2026-09-08; as milliseconds it is 1970.
    assert bar.timestamp.year == 2026, "a millisecond reading lands in 1970"


def test_the_bar_is_close_stamped_at_bucket_start_plus_interval() -> None:
    """`time` is the bucket START. Passing it through grants a bar of hindsight
    — defect D01, found in this repository once already."""
    for interval, seconds in (("1m", 60), ("1h", 3600), ("1d", 86400)):
        bar = row_to_bar(CANDLE, "BTC-USD", interval)
        expected = datetime.fromtimestamp(T0, UTC) + timedelta(seconds=seconds)
        assert bar.timestamp == expected, f"{interval} must stamp at start + {seconds}s"


# -- The sort order -----------------------------------------------------------


def test_a_newest_first_response_comes_back_ascending() -> None:
    """The wire is descending; `write_bars` refuses a series that is not
    strictly increasing, and would fail far from the thing that reversed it."""
    descending = [[T0 + 2 * 86400, *CANDLE[1:]], [T0 + 86400, *CANDLE[1:]], CANDLE]
    bars = bars_from_payload(descending, "BTC-USD", "1d", now=FAR_FUTURE)
    stamps = [b.timestamp for b in bars]
    assert stamps == sorted(stamps), "bars must be ascending regardless of wire order"
    assert len(stamps) == 3


def test_an_identical_repeat_across_overlapping_windows_is_dropped() -> None:
    bars = bars_from_payload([CANDLE, list(CANDLE)], "BTC-USD", "1d", now=FAR_FUTURE)
    assert len(bars) == 1


def test_a_venue_contradicting_itself_is_raised_not_resolved() -> None:
    """A repeated instant with different values is a finding, not a tie."""
    contradiction = [CANDLE, [T0, LOW, HIGH, OPEN, CLOSE + 1, VOLUME]]
    with pytest.raises(CoinbaseParseError, match="twice with different"):
        bars_from_payload(contradiction, "BTC-USD", "1d", now=FAR_FUTURE)


# -- Granularity --------------------------------------------------------------


def test_only_the_granularities_coinbase_serves_are_accepted() -> None:
    assert coinbase_granularity("1d") == 86400
    assert coinbase_granularity("6h") == 21600
    # 3m is in the repository's vocabulary but Coinbase answers 400 for it.
    with pytest.raises(CoinbaseParseError, match="does not serve"):
        coinbase_granularity("3m")


def test_an_interval_outside_the_repository_vocabulary_still_raises() -> None:
    with pytest.raises(UnknownIntervalError):
        coinbase_granularity("1M")


def test_the_supported_set_matches_what_was_measured() -> None:
    assert SUPPORTED_GRANULARITY_S == (60, 300, 900, 3600, 21600, 86400)


# -- Malformed responses ------------------------------------------------------


def test_a_short_row_is_refused_rather_than_index_errored() -> None:
    with pytest.raises(CoinbaseParseError, match="expected 6"):
        row_to_bar([T0, 1.0, 2.0], "BTC-USD", "1d")


def test_an_error_object_is_reported_as_the_refusal_it_is() -> None:
    with pytest.raises(CoinbaseParseError, match="Unsupported granularity"):
        bars_from_payload({"message": "Unsupported granularity"}, "BTC-USD", "1d", now=FAR_FUTURE)


# -- The transport ------------------------------------------------------------


def test_every_request_carries_a_user_agent() -> None:
    """Without one Coinbase returns 403. Mistaken for geoblocking once already,
    which is how a header like this gets deleted as noise."""
    seen: list[Mapping[str, str]] = []

    def transport(url: str, headers: Mapping[str, str]) -> bytes:
        seen.append(headers)
        return json.dumps([CANDLE]).encode()

    client = CoinbaseMarketData(transport=transport)
    client.fetch_symbol(
        "BTC-USD", "1d", datetime(2026, 9, 7, tzinfo=UTC), datetime(2026, 9, 8, tzinfo=UTC)
    )
    assert seen, "no request was made"
    assert seen[0].get("User-Agent") == USER_AGENT


def test_the_client_needs_no_credentials() -> None:
    """The endpoint is public. Requiring a key would fail for anyone without a
    Coinbase account, for no gain."""
    client = CoinbaseMarketData(transport=lambda url, headers: b"[]")
    assert client.credentials.credentials == ()
    assert client.venue == "coinbase"


def test_the_url_carries_the_granularity_and_an_rfc3339_window() -> None:
    client = CoinbaseMarketData(transport=lambda url, headers: b"[]")
    url = client.candles_url(
        "BTC-USD", "1h", datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)
    )
    assert "granularity=3600" in url
    assert "2026-09-01T00%3A00%3A00Z" in url
    assert "/products/BTC-USD/candles" in url


def test_an_end_before_start_is_refused() -> None:
    client = CoinbaseMarketData(transport=lambda url, headers: b"[]")
    with pytest.raises(ValueError, match="before"):
        client.candles_url(
            "BTC-USD", "1d", datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)
        )


def test_paging_is_bounded_so_a_bad_cursor_cannot_spin() -> None:
    """A page that never advances must end the walk, not loop forever."""
    calls = {"n": 0}

    def stuck(url: str, headers: Mapping[str, str]) -> bytes:
        calls["n"] += 1
        return json.dumps([CANDLE]).encode()  # always the same instant

    client = CoinbaseMarketData(transport=stuck, max_pages=5)
    client.fetch_symbol(
        "BTC-USD", "1d", datetime(2020, 1, 1, tzinfo=UTC), datetime(2026, 9, 8, tzinfo=UTC)
    )
    assert calls["n"] <= 5, f"walked {calls['n']} pages against a max_pages of 5"


def test_the_page_size_matches_what_the_api_was_measured_to_return() -> None:
    assert MAX_CANDLES_PER_REQUEST == 350


# -- The candle that has not closed yet ---------------------------------------


def test_the_still_forming_candle_is_withheld() -> None:
    """Coinbase returns the in-progress bucket with nothing marking it unclosed.

    Written to the cache it becomes a fraction of a day recorded as a whole
    one, at the most recent bar — the one most likely to matter to whatever
    prompted the fetch. Found on first contact: 38 of 39 bars matched the wire
    and the newest disagreed on close and volume, because it was still moving.
    T-aae4a1eb.
    """
    closed = [T0 - 86400, LOW, HIGH, OPEN, CLOSE, VOLUME]
    forming = CANDLE  # bucket starts at T0, so it closes at T0 + 1d
    # "Now" sits inside the forming bucket: after its start, before its close.
    now = datetime.fromtimestamp(T0 + 3600, UTC)

    bars = bars_from_payload([forming, closed], "BTC-USD", "1d", now=now)
    stamps = [b.timestamp for b in bars]
    assert datetime.fromtimestamp(T0, UTC) in stamps, "the closed candle must survive"
    assert datetime.fromtimestamp(T0 + 86400, UTC) not in stamps, (
        "the candle whose close is in the future was written as though final"
    )
    assert len(bars) == 1


def test_drop_forming_can_be_turned_off_deliberately() -> None:
    now = datetime.fromtimestamp(T0 + 3600, UTC)
    bars = bars_from_payload([CANDLE], "BTC-USD", "1d", now=now, drop_forming=False)
    assert len(bars) == 1


def test_the_transport_uses_its_injected_clock_not_wall_time() -> None:
    """Nothing here may read the clock directly: the engine never reads wall
    time, and an injected value is what makes this testable at all."""
    frozen = datetime.fromtimestamp(T0 + 3600, UTC)
    client = CoinbaseMarketData(
        transport=lambda url, headers: json.dumps([CANDLE]).encode(),
        clock=lambda: frozen,
        max_pages=1,
    )
    bars = client.fetch_symbol("BTC-USD", "1d", datetime.fromtimestamp(T0 - 86400, UTC), frozen)
    assert bars == [], "the only candle offered was still forming at the injected now"
