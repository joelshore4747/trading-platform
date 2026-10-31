"""Polygon's wire format, pinned offline.

`api.polygon.io` answers 403 in this sandbox, so nothing here has met a real
response. That is exactly why these tests are written the way they are: they
encode what the published reference says, so that the first real response
either agrees with them or produces a failure that names the disagreement.
Until that happens, `data/polygon.py` and `live/polygon.py` are UNVERIFIED and
no figure from them is evidence.

The first test is the one that matters. Defect D01 in `evals/defects/` is a
vendor timestamp passed through as a close when it was an open, found in this
repository once already, in the Alpaca parser.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trading_platform.core.credentials import CredentialSet, MissingCredentialError
from trading_platform.data.polygon import (
    PolygonParseError,
    bars_from_payload,
    iter_pages,
    polygon_timespan,
    result_to_bar,
)
from trading_platform.live.alpaca import TransportError
from trading_platform.live.polygon import PolygonMarketData

# 2026-08-01 12:00:00Z, the START of the aggregate window.
OPEN_MS = int(datetime(2026, 8, 1, 12, 0, tzinfo=UTC).timestamp() * 1000)


def agg(t: int = OPEN_MS, **over: Any) -> dict[str, Any]:
    row = {"t": t, "o": 100.1, "h": 101.0, "l": 99.8, "c": 100.5, "v": 12345, "n": 321}
    row.update(over)
    return row


def payload(*rows: dict[str, Any], **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"ticker": "AAPL", "status": "OK", "results": list(rows)}
    body.update(over)
    return body


# -- the stamping ------------------------------------------------------------------------


def test_polygon_t_is_the_window_open_and_the_bar_is_stamped_at_its_close() -> None:
    """Polygon's `t` is the aggregate window's START. A `Bar` is stamped at its
    CLOSE. Passing `t` through gives every strategy one bar of hindsight — the
    single most common way a backtest lies, and defect D01 here already.

    Mutation: `timestamp=opened` instead of `opened + _interval(interval)`.
    This goes red for both intervals below, and the two differ, so a constant
    offset does not satisfy it either."""
    minute = result_to_bar(agg(), "AAPL", "1m")
    assert minute.timestamp == datetime(2026, 8, 1, 12, 1, tzinfo=UTC)

    hour = result_to_bar(agg(), "AAPL", "1h")
    assert hour.timestamp == datetime(2026, 8, 1, 13, 0, tzinfo=UTC)


def test_the_interval_shifts_the_stamp_by_exactly_one_interval() -> None:
    """Mutation: shift by a fixed minute regardless of interval. Red on 15m."""
    for interval, delta in (("5m", 5), ("15m", 15), ("30m", 30), ("4h", 240)):
        bar = result_to_bar(agg(), "AAPL", interval)
        assert bar.timestamp - datetime(2026, 8, 1, 12, 0, tzinfo=UTC) == timedelta(
            minutes=delta
        ), interval


# -- prices ------------------------------------------------------------------------------


def test_prices_are_decimals_taken_from_the_json_number_as_written() -> None:
    """Polygon sends JSON floats. `Decimal(str(x))` is exact for what the decoder
    produced — no more precision than the vendor sent, and no float error added.

    Mutation: `Decimal(value)` on a float instead of `Decimal(str(value))`.
    `Decimal(100.1)` is 100.099999999999994315658113919198513031005859375, and
    the equality below goes red."""
    bar = result_to_bar(agg(), "AAPL", "1m")
    assert bar.open == Decimal("100.1")
    assert str(bar.open) == "100.1"
    assert isinstance(bar.volume, Decimal)


def test_a_string_price_is_accepted_and_a_boolean_is_not() -> None:
    """`True` is an `int` in Python and would silently become a price of 1.
    Mutation: drop the `isinstance(value, bool)` guard — red."""
    assert result_to_bar(agg(o=100.1, c="100.5"), "AAPL", "1m").close == Decimal("100.5")
    with pytest.raises(PolygonParseError, match="not a number"):
        result_to_bar(agg(c=True), "AAPL", "1m")


def test_a_missing_field_names_itself() -> None:
    for key, field in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume")):
        row = agg()
        del row[key]
        with pytest.raises(PolygonParseError, match=field):
            result_to_bar(row, "AAPL", "1m")


def test_a_missing_or_negative_timestamp_raises() -> None:
    with pytest.raises(PolygonParseError, match="integer 't'"):
        result_to_bar({k: v for k, v in agg().items() if k != "t"}, "AAPL", "1m")
    with pytest.raises(PolygonParseError, match="negative"):
        result_to_bar(agg(t=-1), "AAPL", "1m")
    with pytest.raises(PolygonParseError, match="integer 't'"):
        result_to_bar(agg(t=True), "AAPL", "1m")


# -- payloads ----------------------------------------------------------------------------


def test_no_results_and_no_status_raises_rather_than_returning_nothing() -> None:
    """ "No data" and "your request was wrong" must not look alike. An empty list
    for a rejected request is how a gap becomes invisible.

    Mutation: `return [], None` when `results` is absent — red."""
    with pytest.raises(PolygonParseError, match="no results"):
        bars_from_payload({"ticker": "AAPL", "status": "ERROR"}, "1m")


def test_a_genuinely_empty_window_is_empty_and_says_so_explicitly() -> None:
    """The other half: Polygon reporting OK with `resultsCount: 0` is a real
    empty window, not an error. Mutation: make the `resultsCount == 0` branch
    raise too — red, and gaps would become exceptions."""
    bars, token = bars_from_payload({"ticker": "AAPL", "status": "OK", "resultsCount": 0}, "1m")
    assert bars == [] and token is None


def test_delayed_status_still_carries_results_and_is_not_silently_treated_as_live() -> None:
    """A DELAYED plan returns real bars that are 15 minutes old. The parser
    accepts them — it is not the parser's job to decide — and the status stays
    on the payload for a caller that must not trade on them."""
    body = payload(agg(), status="DELAYED")
    bars, _ = bars_from_payload(body, "1m")
    assert len(bars) == 1
    assert body["status"] == "DELAYED"


def test_bars_come_back_sorted_whatever_order_the_page_had() -> None:
    minute = 60_000
    bars, _ = bars_from_payload(
        payload(agg(t=OPEN_MS + 2 * minute), agg(t=OPEN_MS), agg(t=OPEN_MS + minute)), "1m"
    )
    assert [b.timestamp for b in bars] == sorted(b.timestamp for b in bars)


def test_a_response_with_no_ticker_and_no_symbol_refuses_to_guess() -> None:
    with pytest.raises(PolygonParseError, match="no ticker"):
        bars_from_payload({"status": "OK", "results": [agg()]}, "1m")


def test_pages_that_repeat_a_bar_identically_are_deduplicated() -> None:
    minute = 60_000
    bars = iter_pages(
        [payload(agg(t=OPEN_MS), agg(t=OPEN_MS + minute)), payload(agg(t=OPEN_MS + minute))],
        "1m",
        "AAPL",
    )
    assert len(bars) == 2


def test_pages_that_repeat_a_bar_differently_raise_rather_than_pick_one() -> None:
    """A vendor contradicting itself between pages is a finding. Picking a
    winner would discard the evidence that there was ever a disagreement.

    Mutation: `seen.setdefault(bar.timestamp, bar)` — first wins, silently, red."""
    with pytest.raises(PolygonParseError, match="different values"):
        # The differing bar must itself be valid, or this would be testing
        # `Bar.__post_init__` instead of the page-boundary check.
        iter_pages([payload(agg()), payload(agg(c=100.6))], "1m", "AAPL")


def test_an_unknown_interval_names_the_ones_that_exist() -> None:
    with pytest.raises(PolygonParseError, match="7m"):
        polygon_timespan("7m")
    assert polygon_timespan("15m") == (15, "minute")


# -- the transport -----------------------------------------------------------------------


def keyed(**over: Any) -> PolygonMarketData:
    """A client with a fake key, so URL construction is testable without one."""
    import os

    os.environ.setdefault("POLYGON_API_KEY", "test-key-not-real")
    return PolygonMarketData(transport=lambda url, headers: b"{}", **over)


def test_the_api_key_goes_in_a_header_and_never_in_the_url() -> None:
    """A key in a query string ends up in proxy logs, exception messages and
    shell history. Mutation: append `&apiKey=...` to `bars_url` — red."""
    client = keyed()
    url = client.bars_url(
        "AAPL", "1m", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 2, tzinfo=UTC)
    )
    assert "apiKey" not in url and "test-key-not-real" not in url
    assert client._headers()["Authorization"].startswith("Bearer ")


def test_the_url_is_never_adjusted() -> None:
    """A split adjustment is computed from an announcement made after the bars
    it changes. An adjusted series shows a backtest prices nobody could trade.

    Mutation: `adjusted=true` — red."""
    url = keyed().bars_url(
        "AAPL", "5m", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 2, tzinfo=UTC)
    )
    assert "adjusted=false" in url
    assert "/range/5/minute/" in url


def test_the_url_bounds_are_epoch_milliseconds_in_order() -> None:
    start = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    end = datetime(2026, 8, 1, 13, 0, tzinfo=UTC)
    url = keyed().bars_url("AAPL", "1m", start, end)
    assert f"/{int(start.timestamp() * 1000)}/{int(end.timestamp() * 1000)}?" in url
    with pytest.raises(ValueError, match="before start"):
        keyed().bars_url("AAPL", "1m", end, start)


def test_a_next_url_pointing_off_host_is_refused_rather_than_followed() -> None:
    """Following a redirect with the Authorization header attached is how a key
    leaks. Mutation: drop the host check — the fake transport below would then
    be called with an attacker's URL carrying the Bearer token."""
    import json

    pages = [
        json.dumps(payload(agg(), next_url="https://evil.example/steal")).encode(),
    ]

    def transport(url: str, headers: dict[str, str]) -> bytes:
        return pages.pop(0)

    client = PolygonMarketData(transport=transport)
    with pytest.raises(TransportError, match="another origin"):
        client.fetch_symbol(
            "AAPL", "1m", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 2, tzinfo=UTC)
        )


def test_fetch_symbol_follows_pages_on_the_same_host() -> None:
    import json

    minute = 60_000
    first = payload(agg(t=OPEN_MS), next_url="https://api.polygon.io/v2/aggs/next")
    second = payload(agg(t=OPEN_MS + minute))
    pages = [json.dumps(first).encode(), json.dumps(second).encode()]
    seen: list[str] = []

    def transport(url: str, headers: dict[str, str]) -> bytes:
        seen.append(url)
        return pages.pop(0)

    bars = PolygonMarketData(transport=transport).fetch_symbol(
        "AAPL", "1m", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 2, tzinfo=UTC)
    )
    assert len(bars) == 2
    assert seen[1] == "https://api.polygon.io/v2/aggs/next"


def test_a_missing_key_names_the_variable_rather_than_401ing() -> None:
    client = PolygonMarketData(
        transport=lambda url, headers: b"{}",
        credentials=CredentialSet.from_env("polygon", "POLYGON_KEY_THAT_IS_UNSET"),
    )
    with pytest.raises(MissingCredentialError, match="POLYGON_KEY_THAT_IS_UNSET"):
        client._headers()


def test_pages_that_repeat_a_bar_differently_raise_on_the_production_path_too() -> None:
    """`data/polygon.iter_pages` raises on a disagreeing repeat and is tested —
    but it is not what `fetch_symbol` calls, so on the path that actually runs,
    a vendor contradicting itself across a page boundary was resolved
    first-wins and silently (adversarial review, 2026-09-04).

    Mutation: go back to deduplicating on the timestamp alone in
    `fetch_symbol` — the call below returns 100.5 instead of raising, red."""
    import json

    first = payload(agg(), next_url="https://api.polygon.io/v2/aggs/next")
    second = payload(agg(c=100.6))
    pages = [json.dumps(first).encode(), json.dumps(second).encode()]

    client = PolygonMarketData(transport=lambda url, headers: pages.pop(0))
    with pytest.raises(PolygonParseError, match="different values"):
        client.fetch_symbol(
            "AAPL", "1m", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 2, tzinfo=UTC)
        )


def test_an_identical_repeat_across_a_page_boundary_is_still_fine() -> None:
    """Polygon repeating its edge bar unchanged is normal paging, not a finding."""
    import json

    first = payload(agg(), next_url="https://api.polygon.io/v2/aggs/next")
    second = payload(agg())
    pages = [json.dumps(first).encode(), json.dumps(second).encode()]

    client = PolygonMarketData(transport=lambda url, headers: pages.pop(0))
    bars = client.fetch_symbol(
        "AAPL", "1m", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 2, tzinfo=UTC)
    )
    assert len(bars) == 1
