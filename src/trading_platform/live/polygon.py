"""Polygon aggregates over HTTP. Parsing lives in ``data/polygon.py``.

The second independent view of the same US equity tape, which is the entire
point of paying for it: Polygon and Alpaca both claim to report the
consolidated market, so where they differ, one of them is wrong and
``collect/reconcile.py`` will say by how much.

**UNVERIFIED in this sandbox.** ``api.polygon.io`` answers 403 here like every
other market-data host, so this ships written from the published reference and
pinned by offline tests. First contact is on the VPS; until a real response
agrees with ``tests/test_polygon.py``, no figure from this path is evidence.

Two decisions worth stating:

**The key goes in a header, never the query string.** Polygon accepts
``?apiKey=``; URLs end up in proxy logs, exception messages and shell history,
and a key in one of those is a key to rotate. ``Authorization: Bearer`` is
supported and costs nothing.

**One ticker per request.** Polygon's aggregates endpoint is per-ticker, unlike
Alpaca's multi-symbol one, so N symbols is N requests and the rate limit is
per-minute on the free tier. ``fetch_bars`` therefore reports the request count
it used; a caller polling 200 symbols every 15 seconds on a 5-per-minute plan
will find out from the 429, and should find out from this docstring first.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from trading_platform.core.credentials import CredentialSet
from trading_platform.core.events import Bar
from trading_platform.data.polygon import (
    PolygonParseError,
    bars_from_payload,
    polygon_timespan,
)
from trading_platform.live.alpaca import Transport, TransportError

__all__ = ["POLYGON_HOST", "PolygonMarketData"]

POLYGON_HOST = "https://api.polygon.io"


def _epoch_ms(at: datetime) -> int:
    if at.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(at.astimezone(UTC).timestamp() * 1000)


@dataclass(slots=True)
class PolygonMarketData:
    """Aggregate bars for one ticker at a time, paged, close-stamped.

    ``adjusted=false`` always. A split adjustment is computed from a corporate
    action announced after the bars it changes, so an adjusted series shows a
    backtest prices nobody could have traded. The same rule as Alpaca's
    ``adjustment=raw`` and for the same reason.
    """

    transport: Transport
    credentials: CredentialSet = field(
        default_factory=lambda: CredentialSet.from_env("polygon", "POLYGON_API_KEY")
    )
    host: str = POLYGON_HOST
    page_limit: int = 50_000
    market: str = "stocks"

    def __post_init__(self) -> None:
        if not 1 <= self.page_limit <= 50_000:
            raise ValueError("page_limit must be in 1..50000")

    @property
    def venue(self) -> str:
        return "polygon"

    def _headers(self) -> dict[str, str]:
        values = self.credentials.require_all()  # names the missing variable, loudly
        return {
            "Authorization": f"Bearer {values['POLYGON_API_KEY']}",
            "Accept": "application/json",
        }

    def bars_url(self, symbol: str, interval: str, start: datetime, end: datetime) -> str:
        """``/v2/aggs/ticker/{t}/range/{mult}/{span}/{from}/{to}`` with epoch-ms bounds.

        Bounds are milliseconds rather than dates because a poller asks for the
        last few minutes, and a date-bounded request would re-fetch the day.
        """
        if end < start:
            raise ValueError(f"end {end.isoformat()} is before start {start.isoformat()}")
        multiplier, timespan = polygon_timespan(interval)
        path = (
            f"/v2/aggs/ticker/{urllib.parse.quote(symbol, safe='')}"
            f"/range/{multiplier}/{timespan}/{_epoch_ms(start)}/{_epoch_ms(end)}"
        )
        params = {
            "adjusted": "false",  # never adjusted: adjustment is computed from the future
            "sort": "asc",
            "limit": str(self.page_limit),
        }
        return f"{self.host}{path}?{urllib.parse.urlencode(params)}"

    def _get(self, url: str, headers: dict[str, str]) -> Any:
        raw = self.transport(url, headers)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PolygonParseError(f"response from {url} is not JSON: {exc}") from None

    def fetch_symbol(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        *,
        max_pages: int = 20,
    ) -> list[Bar]:
        """Every bar for one ticker in ``[start, end]``, close-stamped, sorted.

        ``next_url`` is followed as Polygon returns it, with the same headers.
        A ``next_url`` pointing somewhere other than ``host`` is refused rather
        than followed: a redirect carrying the Authorization header to another
        origin is how a key leaks.
        """
        headers = self._headers()
        url: str | None = self.bars_url(symbol, interval, start, end)
        out: list[Bar] = []
        seen: dict[datetime, Bar] = {}
        for _ in range(max_pages):
            if url is None:
                break
            page, next_url = bars_from_payload(self._get(url, headers), interval, symbol=symbol)
            for bar in page:
                existing = seen.get(bar.timestamp)
                if existing == bar:
                    continue  # a page boundary repeating its edge bar, unchanged
                if existing is not None:
                    # The same rule `data/polygon.iter_pages` applies, and it has
                    # to be here too: this is the production path, and it used to
                    # keep whichever page arrived first and say nothing. A vendor
                    # contradicting itself between pages is a finding, not a tie
                    # to break (adversarial review, 2026-09-04).
                    raise PolygonParseError(
                        f"{symbol} at {bar.timestamp.isoformat()} appears twice across "
                        f"pages with different values: {existing} then {bar}"
                    )
                seen[bar.timestamp] = bar
                out.append(bar)
            if next_url is not None and not next_url.startswith(f"{self.host}/"):
                raise TransportError(
                    f"next_url for {symbol} leaves {self.host}: {next_url.split('?')[0]}. "
                    "Refusing to send the API key to another origin."
                )
            url = next_url
        else:
            raise TransportError(
                f"more than {max_pages} pages for {symbol} {interval} from {start.isoformat()}"
            )
        out.sort(key=lambda b: b.timestamp)
        return out

    def fetch_bars(
        self,
        symbols: Sequence[str],
        interval: str,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Bar]:
        """The ``BarFetcher`` shape: several symbols, one request each.

        ``end`` defaults to now because Polygon's range endpoint requires an
        upper bound, unlike Alpaca's. Defaulting it inside the client keeps the
        two clients interchangeable to a caller.
        """
        upper = end if end is not None else datetime.now(UTC)
        out: list[Bar] = []
        for symbol in symbols:
            out.extend(self.fetch_symbol(symbol, interval, start, upper))
        out.sort(key=lambda b: (b.timestamp, b.symbol))
        return out
