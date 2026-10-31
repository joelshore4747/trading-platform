"""Coinbase Exchange market data over HTTP. Parsing lives in ``data/coinbase.py``.

The second crypto venue. The collector's whole premise is that disagreement
between independent sources is the signal — two feeds that agree tell you
nothing you did not already know from one — and until this existed, crypto had
exactly one verified source and nothing to disagree with.

**No credentials.** ``/products/{id}/candles`` is public. This client declares
an empty ``CredentialSet`` deliberately rather than reading the
``COINBASE_API_KEY`` that may be in the environment: a key that is not needed
should not be required, and requiring one would make the adapter fail for
anyone without a Coinbase account, for no gain.

**The User-Agent is not optional.** Coinbase answers ``403`` to urllib's
default. Measured 2026-09-08 — and mistaken for geoblocking the first time,
which is exactly how a header like this gets deleted as noise. It is a header
this client must send.

**Windows are walked backwards.** A response is capped at 350 candles and
arrives newest-first, so paging forwards would need the caller to know the cap;
paging backwards from ``end`` follows the grain of the API.

**A product id is not a symbol from another venue.** ``BTC-USD`` is quoted in
dollars; Binance's ``BTCUSDT`` is quoted in Tether. They are different
instruments and the gap between them is the peg, which this repository has
already misread once (``harness/KNOWN_FAILURES.md``, 2026-08-24). The product
id is stored verbatim so the two never silently meet.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from trading_platform.core.credentials import CredentialSet
from trading_platform.core.events import Bar
from trading_platform.data.coinbase import bars_from_payload, coinbase_granularity
from trading_platform.live.alpaca import Transport, TransportError

__all__ = ["COINBASE_HOST", "MAX_CANDLES_PER_REQUEST", "USER_AGENT", "CoinbaseMarketData"]

COINBASE_HOST = "https://api.exchange.coinbase.com"

#: Candles per response. Measured, not documented: an unbounded request
#: returned exactly this at every granularity on 2026-09-08.
MAX_CANDLES_PER_REQUEST = 350

#: Sent on every request. Without it Coinbase returns 403 — see the module
#: docstring before removing this as decoration.
USER_AGENT = "trading_platform/0.0.1 (systematic research; contact via repository)"


@dataclass(slots=True)
class CoinbaseMarketData:
    """Candles for one product at a time, paged backwards, close-stamped."""

    transport: Transport
    #: Public endpoint, so there is nothing to require. Declared rather than
    #: omitted so the absence is visible and deliberate.
    credentials: CredentialSet = field(default_factory=lambda: CredentialSet(venue="coinbase"))
    host: str = COINBASE_HOST
    max_pages: int = 40
    #: Injected so forming-bar rejection is testable, matching CaptureConfig
    #: and data/binance.py. Nothing here reads wall time directly.
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    @property
    def venue(self) -> str:
        """The tape, which for an exchange is the exchange."""
        return "coinbase"

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": USER_AGENT, "Accept": "application/json"}

    def candles_url(self, product: str, interval: str, start: datetime, end: datetime) -> str:
        """``/products/{id}/candles`` with an RFC3339 window and a granularity."""
        if end < start:
            raise ValueError(f"end {end.isoformat()} is before start {start.isoformat()}")
        params = {
            "granularity": str(coinbase_granularity(interval)),
            "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        path = f"/products/{urllib.parse.quote(product, safe='')}/candles"
        return f"{self.host}{path}?{urllib.parse.urlencode(params)}"

    def _get(self, url: str) -> Any:
        raw = self.transport(url, self._headers())
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TransportError(f"response from {url} is not JSON: {exc}") from None

    def fetch_symbol(
        self, product: str, interval: str, start: datetime, end: datetime
    ) -> list[Bar]:
        """Every candle for one product in ``[start, end]``, close-stamped, sorted.

        Walks backwards from ``end`` in windows the API can answer in one
        response. Bounded by ``max_pages`` so a clock skew or an off-by-one
        cannot spin: a bounded gap is a finding, an unbounded loop is an
        outage.
        """
        granularity = coinbase_granularity(interval)
        window = timedelta(seconds=granularity * MAX_CANDLES_PER_REQUEST)

        collected: dict[datetime, Bar] = {}
        cursor = end
        for _ in range(self.max_pages):
            if cursor <= start:
                break
            page_start = max(start, cursor - window)
            page = bars_from_payload(
                self._get(self.candles_url(product, interval, page_start, cursor)),
                product,
                interval,
                now=self.clock(),
            )
            if not page:
                break
            for bar in page:
                collected.setdefault(bar.timestamp, bar)
            oldest = page[0].timestamp
            # The oldest bar's own window start, minus one interval, or a page
            # whose first bar sits exactly on `page_start` would ask again for
            # the same window forever.
            next_cursor = oldest - timedelta(seconds=granularity)
            if next_cursor >= cursor:
                break
            cursor = next_cursor

        return sorted(
            (
                b
                for b in collected.values()
                if start <= b.timestamp <= end + timedelta(seconds=granularity)
            ),
            key=lambda b: b.timestamp,
        )

    def fetch_bars(
        self,
        symbols: Sequence[str],
        interval: str,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Bar]:
        """The `BarFetcher` protocol, so a `CaptureConfig` can use this directly.

        One request per product: Coinbase's candles endpoint takes a single
        product id, unlike Alpaca's multi-symbol one.
        """
        finish = end or datetime.now(UTC)
        out: list[Bar] = []
        for product in symbols:
            out.extend(self.fetch_symbol(product, interval, start, finish))
        out.sort(key=lambda b: (b.timestamp, b.symbol))
        return out
