"""Alpaca parsing and polling, network-free.

Mutations these catch: reading ``t`` as the close (first test, the D01
trap); publishing the in-progress bar; re-publishing on an overlapping poll;
dropping the feed parameter or the raw adjustment from the stocks URL;
credentials read from anywhere but the environment.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from trading_platform.core.credentials import MissingCredentialError
from trading_platform.data.alpaca import (
    AlpacaParseError,
    alpaca_timeframe,
    bars_from_payload,
    row_to_bar,
)
from trading_platform.live.alpaca import AlpacaMarketData, AlpacaPoller, TransportError
from trading_platform.live.stream import LiveFeed

T0 = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)


def row(minute: int, close: float = 100.5) -> dict[str, object]:
    return {
        "t": (T0 + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "o": 100.0,
        "h": 101.0,
        "l": 99.5,
        "c": close,
        "v": 1234,
        "n": 10,
        "vw": 100.2,
    }


# -- parsing --------------------------------------------------------------------------


def test_the_bar_is_stamped_at_its_close_not_at_alpacas_open_time() -> None:
    bar = row_to_bar(row(0), "AAPL", "1m")
    assert bar.timestamp == T0 + timedelta(minutes=1)
    assert bar.timestamp.tzinfo is not None
    assert bar.close == Decimal("100.5")
    assert bar.volume == Decimal("1234")


def test_a_naive_time_is_refused_rather_than_assumed_utc() -> None:
    bad = row(0)
    bad["t"] = "2026-09-01T13:30:00"
    with pytest.raises(AlpacaParseError, match="no timezone"):
        row_to_bar(bad, "AAPL", "1m")


def test_a_malformed_row_is_refused() -> None:
    bad = row(0)
    del bad["h"]
    with pytest.raises(AlpacaParseError, match="missing 'h'"):
        row_to_bar(bad, "AAPL", "1m")
    bad = row(0)
    bad["c"] = "not-a-number"
    with pytest.raises(AlpacaParseError, match="not a number"):
        row_to_bar(bad, "AAPL", "1m")


def test_a_payload_is_sorted_and_carries_its_page_token() -> None:
    payload = {
        "bars": {"MSFT": [row(1)], "AAPL": [row(1), row(0)]},
        "next_page_token": "abc",
    }
    bars, token = bars_from_payload(payload, "1m", symbols=["AAPL", "MSFT"])
    assert token == "abc"
    assert [(b.symbol, b.timestamp.minute) for b in bars] == [
        ("AAPL", 31),
        ("AAPL", 32),
        ("MSFT", 32),
    ]


def test_an_unrequested_symbol_in_the_response_is_an_error() -> None:
    with pytest.raises(AlpacaParseError, match="not requested"):
        bars_from_payload({"bars": {"TSLA": [row(0)]}}, "1m", symbols=["AAPL"])


def test_timeframes_map_and_unknown_intervals_refuse() -> None:
    assert alpaca_timeframe("1m") == "1Min"
    assert alpaca_timeframe("1d") == "1Day"
    with pytest.raises(AlpacaParseError):
        alpaca_timeframe("7m")


# -- the client -------------------------------------------------------------------------


class FakeTransport:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def __call__(self, url: str, headers: Mapping[str, str]) -> bytes:
        self.calls.append((url, headers))
        return json.dumps(self.pages[len(self.calls) - 1]).encode()


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APCA_API_KEY_ID", "key-id-for-tests")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "secret-for-tests-only")


def test_stocks_url_names_the_feed_and_never_adjusts(creds: None) -> None:
    transport = FakeTransport([{"bars": {"AAPL": [row(0)]}}])
    client = AlpacaMarketData("stocks", transport, stocks_feed="iex")
    client.fetch_bars(["AAPL"], "1m", T0, T0 + timedelta(hours=1))
    url, headers = transport.calls[0]
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    assert parsed.path == "/v2/stocks/bars"
    assert q["feed"] == ["iex"] and q["adjustment"] == ["raw"]
    assert q["timeframe"] == ["1Min"] and q["sort"] == ["asc"]
    assert q["start"] == ["2026-09-01T13:30:00Z"]
    assert headers["APCA-API-KEY-ID"] == "key-id-for-tests"
    assert headers["APCA-API-SECRET-KEY"] == "secret-for-tests-only"


def test_crypto_url_uses_the_crypto_path_without_a_feed(creds: None) -> None:
    transport = FakeTransport([{"bars": {"BTC/USD": [row(0)]}}])
    client = AlpacaMarketData("crypto", transport)
    bars = client.fetch_bars(["BTC/USD"], "1m", T0)
    url, _ = transport.calls[0]
    parsed = urlparse(url)
    assert parsed.path == "/v1beta3/crypto/us/bars"
    assert "feed" not in parse_qs(parsed.query)
    assert bars[0].symbol == "BTC/USD"


def test_pages_are_followed_until_the_token_runs_out(creds: None) -> None:
    transport = FakeTransport(
        [
            {"bars": {"AAPL": [row(0)]}, "next_page_token": "p2"},
            {"bars": {"AAPL": [row(1)]}, "next_page_token": None},
        ]
    )
    client = AlpacaMarketData("stocks", transport)
    bars = client.fetch_bars(["AAPL"], "1m", T0)
    assert [b.timestamp.minute for b in bars] == [31, 32]
    assert "page_token=p2" in transport.calls[1][0]
    assert "page_token" not in transport.calls[0][0]


def test_runaway_pagination_raises(creds: None) -> None:
    transport = FakeTransport([{"bars": {"AAPL": []}, "next_page_token": "again"}] * 5)
    client = AlpacaMarketData("stocks", transport)
    with pytest.raises(TransportError, match="more than 3 pages"):
        client.fetch_bars(["AAPL"], "1m", T0, max_pages=3)


def test_missing_credentials_fail_loudly_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    transport = FakeTransport([{"bars": {}}])
    with pytest.raises(MissingCredentialError, match="APCA_API_KEY_ID"):
        AlpacaMarketData("stocks", transport).fetch_bars(["AAPL"], "1m", T0)
    assert transport.calls == []


def test_the_client_repr_never_shows_the_secret(creds: None) -> None:
    client = AlpacaMarketData("stocks", FakeTransport([]))
    assert "secret-for-tests-only" not in repr(client)
    assert "secret-for-tests-only" not in repr(client.credentials)


# -- the poller -------------------------------------------------------------------------


def make_feed() -> LiveFeed:
    return LiveFeed(["AAPL"], reorder_window=timedelta(0), now=lambda: T0 + timedelta(days=1))


def test_the_bar_in_progress_is_never_published(creds: None) -> None:
    """At 13:32:30 the 13:32 bar (closing 13:33) is still forming."""
    transport = FakeTransport([{"bars": {"AAPL": [row(0), row(1), row(2)]}}])
    feed = make_feed()
    poller = AlpacaPoller(
        AlpacaMarketData("stocks", transport),
        feed,
        ["AAPL"],
        "1m",
        clock=lambda: T0 + timedelta(minutes=2, seconds=30),
    )
    assert poller.poll_once() == 2
    assert poller.stats.withheld_open == 1
    feed.stop()
    assert [b.timestamp.minute for b in feed] == [31, 32]


def test_overlapping_polls_publish_each_bar_once(creds: None) -> None:
    transport = FakeTransport(
        [
            {"bars": {"AAPL": [row(0), row(1)]}},
            {"bars": {"AAPL": [row(0), row(1), row(2)]}},
        ]
    )
    feed = make_feed()
    clock = [T0 + timedelta(minutes=2)]
    poller = AlpacaPoller(
        AlpacaMarketData("stocks", transport), feed, ["AAPL"], "1m", clock=lambda: clock[0]
    )
    assert poller.poll_once() == 2
    clock[0] = T0 + timedelta(minutes=3)
    assert poller.poll_once() == 1
    assert poller.stats.published == 3 and poller.stats.refused == 0
    # The second poll resumed from the last published close, not from the lookback.
    assert "start=2026-09-01T13%3A32%3A00Z" in transport.calls[1][0]


def test_a_transport_error_is_counted_and_polling_continues(creds: None) -> None:
    class Flaky:
        def __init__(self) -> None:
            self.n = 0

        def __call__(self, url: str, headers: Mapping[str, str]) -> bytes:
            self.n += 1
            if self.n == 1:
                raise TransportError("HTTP 429", status=429)
            return json.dumps({"bars": {"AAPL": [row(0)]}}).encode()

    feed = make_feed()
    poller = AlpacaPoller(
        AlpacaMarketData("stocks", Flaky()),
        feed,
        ["AAPL"],
        "1m",
        clock=lambda: T0 + timedelta(hours=1),
    )
    assert poller.poll_once() == 0
    assert poller.stats.errors == 1 and "429" in poller.stats.last_error
    assert poller.poll_once() == 1


def test_run_stops_the_feed_when_told_to_stop(creds: None) -> None:
    transport = FakeTransport([{"bars": {"AAPL": [row(0)]}}] * 3)
    feed = make_feed()
    stop = threading.Event()
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            stop.set()

    poller = AlpacaPoller(
        AlpacaMarketData("stocks", transport),
        feed,
        ["AAPL"],
        "1m",
        poll_every=timedelta(seconds=5),
        clock=lambda: T0 + timedelta(hours=1),
        sleep=sleep,
    )
    poller.run(stop)
    assert slept == [5.0, 5.0]
    assert [b.timestamp.minute for b in feed] == [31], "drained, then ended"


# -- the transport must not hand the key to a second origin --------------------------------


def test_a_cross_origin_redirect_never_carries_the_api_key() -> None:
    """`urllib`'s default redirect handler forwards every header except
    `content-*` across a 30x — `Authorization` included. A vendor that is
    compromised, or merely misconfigured, can answer `302 https://elsewhere/`
    and be handed the API key. Demonstrated against this transport before the
    fix: the second server received `Authorization: Bearer <key>`.

    `live/polygon.py` refuses a cross-origin `next_url`, but that guard is above
    the transport and a redirect goes around it. This is the layer the key
    passes through.

    Mutation: build the opener without `SameOriginRedirects` — `stolen` below is
    non-empty and this goes red."""
    import http.server
    import socketserver
    import threading as _threading

    from trading_platform.live.alpaca import TransportError, urllib_transport

    stolen: list[str | None] = []

    class Evil(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            stolen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            pass

    class Vendor(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{evil_port}/steal")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    evil = socketserver.TCPServer(("127.0.0.1", 0), Evil)
    evil_port = evil.server_address[1]
    vendor = socketserver.TCPServer(("127.0.0.1", 0), Vendor)
    vendor_port = vendor.server_address[1]
    _threading.Thread(target=evil.serve_forever, daemon=True).start()
    _threading.Thread(target=vendor.serve_forever, daemon=True).start()
    try:
        get = urllib_transport(timeout_s=5)
        with pytest.raises(TransportError, match="refusing a 302 redirect"):
            get(f"http://127.0.0.1:{vendor_port}/bars", {"Authorization": "Bearer SECRET-0123"})
    finally:
        evil.shutdown()
        vendor.shutdown()

    assert stolen == [], "the API key reached the second origin"


def test_a_same_origin_redirect_is_still_followed() -> None:
    """The guard must not break ordinary redirects — a path change or a trailing
    slash within the vendor's own origin is normal and must keep working, or
    somebody removes the guard."""
    import http.server
    import socketserver
    import threading as _threading

    from trading_platform.live.alpaca import urllib_transport

    class Redirecting(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/bars":
                self.send_response(302)
                self.send_header("Location", "/bars/v2")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *args: object) -> None:
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Redirecting)
    port = server.server_address[1]
    _threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        body = urllib_transport(timeout_s=5)(f"http://127.0.0.1:{port}/bars", {"X-Test": "1"})
    finally:
        server.shutdown()
    assert body == b'{"ok": true}'
