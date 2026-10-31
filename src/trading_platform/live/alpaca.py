"""Alpaca market data over HTTP, and a poller that feeds a ``LiveFeed``.

The only network code in ``live/``. It is **UNVERIFIED in this sandbox**
(every market-data host answers 403) and ships saying so; first contact is on
the VPS, following ``docs/RUNBOOK.md``. Everything that can be checked
offline is: URL construction, headers, pagination, the open→close stamping,
and the one rule that matters most for a poller — **a bar is published only
once its close time has passed.** Alpaca returns the bar in progress when
``end`` is now; publishing it would hand the strategy a close that has not
happened. That is lookahead, and it is the reason `poll_once` takes a clock.

Credentials: ``APCA_API_KEY_ID`` and ``APCA_API_SECRET_KEY`` from the
environment only (``core.credentials``). Market-data scopes are read-only.
Nothing here can place an order; ``brokers/`` still refuses.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from trading_platform.core.credentials import CredentialSet
from trading_platform.core.events import Bar
from trading_platform.data.alpaca import (
    STOCKS_FEEDS,
    AlpacaParseError,
    alpaca_timeframe,
    bars_from_payload,
)
from trading_platform.data.intervals import interval_ms
from trading_platform.live.polling import due_bars
from trading_platform.live.stream import LiveFeed

__all__ = [
    "AlpacaMarketData",
    "AlpacaPoller",
    "PollStats",
    "SameOriginRedirects",
    "TransportError",
    "urllib_transport",
]

DATA_HOST = "https://data.alpaca.markets"
Transport = Callable[[str, Mapping[str, str]], bytes]


class TransportError(RuntimeError):
    """The HTTP layer failed. Carries the status so a 401 (bad key) and a 429
    (rate limit) are told apart by the caller, not guessed."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that leaves the origin the request was addressed to.

    `urllib`'s default handler forwards every header except `content-*` across a
    30x, **including `Authorization`**. A vendor that is compromised, or merely
    misconfigured, can therefore answer `302 https://elsewhere/` and be handed
    the API key. That is not hypothetical: it was demonstrated against this
    transport with two local servers, and the second one received
    `Authorization: Bearer <key>` (adversarial review, 2026-09-04).

    `live/polygon.py` already refuses a cross-origin `next_url`, but that guard
    is above the transport and a redirect goes around it. This is the layer the
    key actually passes through, so the check belongs here as well.

    Same origin means identical scheme, host and port. A redirect within the
    origin — a path change, a trailing slash — is followed as normal.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        origin = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if (origin.scheme, origin.hostname, origin.port) != (
            target.scheme,
            target.hostname,
            target.port,
        ):
            raise TransportError(
                f"refusing a {code} redirect from {origin.scheme}://{origin.netloc} to "
                f"{target.scheme}://{target.netloc}: urllib would forward the "
                "Authorization header to the new origin, which is how an API key leaks.",
                status=code,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urllib_transport(timeout_s: float = 30.0) -> Transport:
    """A GET over the standard library. Honours ``HTTPS_PROXY`` like any urllib client.

    Built on an opener that refuses cross-origin redirects, because the default
    one forwards the API key across them — see `SameOriginRedirects`.
    """
    opener = urllib.request.build_opener(SameOriginRedirects)

    def get(url: str, headers: Mapping[str, str]) -> bytes:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with opener.open(request, timeout=timeout_s) as response:
                return bytes(response.read())
        except urllib.error.HTTPError as exc:
            raise TransportError(f"HTTP {exc.code} from {url}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            # A TransportError raised inside the redirect handler surfaces here
            # wrapped in URLError; unwrap so the caller sees the real reason.
            if isinstance(exc.reason, TransportError):
                raise exc.reason from None
            raise TransportError(f"{exc.reason} reaching {url}") from exc

    return get


def _rfc3339(at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class AlpacaMarketData:
    """Historical bars for stocks or crypto, paged, close-stamped.

    ``market`` is ``"stocks"`` or ``"crypto"``; the two live on different
    paths and the crypto one keys symbols like ``BTC/USD``. ``stocks_feed``
    is recorded on every dataset this produces because IEX and SIP are not
    the same tape.
    """

    market: str
    transport: Transport
    credentials: CredentialSet = field(
        default_factory=lambda: CredentialSet.from_env(
            "alpaca", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"
        )
    )
    stocks_feed: str = "iex"
    host: str = DATA_HOST
    page_limit: int = 10_000

    def __post_init__(self) -> None:
        if self.market not in ("stocks", "crypto"):
            raise ValueError(f"market must be 'stocks' or 'crypto', got {self.market!r}")
        if self.stocks_feed not in STOCKS_FEEDS:
            raise ValueError(f"stocks_feed must be one of {STOCKS_FEEDS}, got {self.stocks_feed!r}")
        if not 1 <= self.page_limit <= 10_000:
            raise ValueError("page_limit must be in 1..10000")

    @property
    def venue(self) -> str:
        return f"alpaca-{self.market}"

    def _headers(self) -> dict[str, str]:
        values = self.credentials.require_all()  # names the missing variable, loudly
        return {
            "APCA-API-KEY-ID": values["APCA_API_KEY_ID"],
            "APCA-API-SECRET-KEY": values["APCA_API_SECRET_KEY"],
            "Accept": "application/json",
        }

    def bars_url(
        self,
        symbols: Sequence[str],
        interval: str,
        start: datetime,
        end: datetime | None,
        page_token: str | None = None,
    ) -> str:
        params: dict[str, str] = {
            "symbols": ",".join(symbols),
            "timeframe": alpaca_timeframe(interval),
            "start": _rfc3339(start),
            "limit": str(self.page_limit),
            "sort": "asc",
        }
        if end is not None:
            params["end"] = _rfc3339(end)
        if self.market == "stocks":
            params["feed"] = self.stocks_feed
            params["adjustment"] = "raw"  # never adjusted: adjustment is computed from the future
            path = "/v2/stocks/bars"
        else:
            path = "/v1beta3/crypto/us/bars"
        if page_token:
            params["page_token"] = page_token
        return f"{self.host}{path}?{urllib.parse.urlencode(params)}"

    def fetch_bars(
        self,
        symbols: Sequence[str],
        interval: str,
        start: datetime,
        end: datetime | None = None,
        *,
        max_pages: int = 100,
    ) -> list[Bar]:
        """Every bar for ``symbols`` in ``[start, end]``, close-stamped, sorted.

        Follows ``next_page_token`` up to ``max_pages``; more than that is a
        request that should have been split, and raising beats an unbounded
        loop against a rate-limited host.
        """
        headers = self._headers()
        token: str | None = None
        out: list[Bar] = []
        for _ in range(max_pages):
            url = self.bars_url(symbols, interval, start, end, token)
            raw = self.transport(url, headers)
            try:
                payload: Any = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AlpacaParseError(f"response from {url} is not JSON: {exc}") from None
            page, token = bars_from_payload(payload, interval, symbols=symbols)
            out.extend(page)
            if token is None:
                out.sort(key=lambda b: (b.timestamp, b.symbol))
                return out
        raise TransportError(f"more than {max_pages} pages for {symbols} {interval} from {start}")


@dataclass(slots=True)
class PollStats:
    polls: int = 0
    fetched: int = 0
    published: int = 0
    #: Bars the feed refused (late for their slot) — counted, never dropped silently.
    refused: int = 0
    #: Bars withheld because their close had not yet passed at poll time.
    withheld_open: int = 0
    #: Bars re-delivered by an overlapping poll window and already published.
    already_seen: int = 0
    #: Bars the vendor re-sent for an instant it had already published, with
    #: different values. Passed through, and counted.
    revised: int = 0
    errors: int = 0
    last_error: str = ""


@dataclass(slots=True)
class AlpacaPoller:
    """Pull bars from Alpaca on an interval and push them into a ``LiveFeed``.

    Polling rather than the websocket, on purpose: it needs no dependency, it
    restarts cleanly (the next poll re-asks from the last close it published),
    and a missed poll is a gap the feed already counts. Latency is one poll
    period, which for a 1-minute strategy on paper is acceptable and stated.
    """

    client: AlpacaMarketData
    feed: LiveFeed
    symbols: Sequence[str]
    interval: str
    poll_every: timedelta = timedelta(seconds=15)
    #: How far back the first poll looks. Later polls resume from the last
    #: published close, so a restart within this window loses nothing.
    lookback: timedelta = timedelta(hours=2)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    stats: PollStats = field(default_factory=PollStats)
    #: The last bar published per symbol, not just its close time: a vendor
    #: re-sending an instant with different prices has revised it, and that is
    #: only visible if the values are kept.
    _last_close: dict[str, Bar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("a poller needs symbols")
        interval_ms(self.interval)  # validate early; raises on an unknown interval
        if self.poll_every <= timedelta(0):
            raise ValueError("poll_every must be positive")

    def poll_once(self) -> int:
        """One fetch and publish. Returns how many bars were published."""
        now = self.clock()
        since = min(
            (bar.timestamp for bar in self._last_close.values()),
            default=now - self.lookback,
        )
        self.stats.polls += 1
        try:
            bars = self.client.fetch_bars(self.symbols, self.interval, since, now)
        except (TransportError, AlpacaParseError) as exc:
            self.stats.errors += 1
            self.stats.last_error = str(exc)
            return 0
        self.stats.fetched += len(bars)
        published = 0
        # The bar in progress is withheld here, not below: its close has not
        # happened, and `live/polling.py` holds that rule for every poller.
        decision = due_bars(bars, now, self._last_close)
        self.stats.withheld_open += decision.withheld_open
        self.stats.already_seen += decision.already_seen
        self.stats.revised += decision.revised
        for bar in decision.due:
            if self.feed.publish(bar):
                published += 1
                self._last_close[bar.symbol] = bar
            else:
                self.stats.refused += 1
        self.stats.published += published
        return published

    def run(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set, then stop the feed so it drains."""
        try:
            while not stop.is_set():
                self.poll_once()
                self.sleep(self.poll_every.total_seconds())
        finally:
            self.feed.stop()
