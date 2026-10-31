"""Binance response parsers.

**This module performs no network I/O and must never acquire any.** Fetching
lives in ``scripts/fetch_binance.py``. The split is deliberate: parsing is where
the bugs are, and a parser that needs a socket cannot be tested. A test asserts
this module imports nothing that opens a connection.

Three properties of Binance's wire format will silently corrupt a backtest if
handled naively. Each is defended here and pinned by a test.

1. ``closeTime`` is ``openTime + interval - 1ms`` — an inclusive last tick, not
   a boundary. Binance's own doc example: a ``1w`` bar spans
   ``1499040000000 -> 1499644799999``, seven days minus one millisecond. We
   stamp bars at their true close, so we compute ``openTime + interval`` and
   never read the ``closeTime`` field. Using ``closeTime`` is 1ms early; using
   ``openTime`` is a full interval of lookahead.

2. Spot *archive* timestamps switch from milliseconds to microseconds on
   2025-01-01. Concatenating a 2024 and a 2025 file without normalising places
   every 2025 bar roughly fifty thousand years in the future — which still
   sorts monotonically, so ordering validation does not catch it.

3. The final bar of a response with no ``endTime`` is **still forming**: its
   close time is in the future and its high/low/close will still change.
   Ingesting it puts a partial bar in history.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading_platform.core.events import Bar
from trading_platform.core.types import funding_payment
from trading_platform.data.intervals import INTERVAL_MS, UnknownIntervalError
from trading_platform.data.intervals import interval_ms as _interval_ms

#: Above this, an epoch value is microseconds rather than milliseconds.
#: Milliseconds are ~1.8e12 today; microseconds ~1.8e15. 1e14 separates them
#: with several orders of magnitude of headroom in both directions.
_MICROSECOND_THRESHOLD = 100_000_000_000_000

# Kline array positions. Named so no magic index appears below.
_OPEN_TIME = 0
_OPEN = 1
_HIGH = 2
_LOW = 3
_CLOSE = 4
_VOLUME = 5
_MIN_KLINE_FIELDS = 6


#: Re-exported from ``data/intervals.py``, which owns the vocabulary. Kept here
#: because callers reasonably look for Binance's interval table in the Binance
#: module, and because tests import it from this path.
__all__ = [
    "INTERVAL_MS",
    "BinanceParseError",
    "interval_ms",
    "kline_to_bar",
    "klines_to_bars",
    "normalise_epoch_ms",
    "observed_funding_interval_hours",
]


class BinanceParseError(ValueError):
    """Raised when a response does not match the documented shape."""


def interval_ms(interval: str) -> int:
    """Length of ``interval`` in milliseconds, as ``BinanceParseError``.

    Thin wrapper over the shared vocabulary in ``data/intervals.py`` so that a
    bad interval arriving through a Binance response still surfaces as a parse
    error, which is what every caller here catches.
    """
    try:
        return _interval_ms(interval)
    except UnknownIntervalError as exc:
        raise BinanceParseError(str(exc)) from None


def normalise_epoch_ms(raw: int) -> int:
    """Return ``raw`` in milliseconds, collapsing microsecond timestamps.

    Binance switched spot *archive* timestamps to microseconds on 2025-01-01
    while the REST API stayed in milliseconds. A loader that concatenates
    archive files across that boundary without this produces a series that is
    still monotonic — and therefore passes ordering checks — but places 2025
    bars tens of thousands of years after 2024.
    """
    if raw < 0:
        raise BinanceParseError(f"Negative epoch timestamp: {raw}")
    return raw // 1000 if raw > _MICROSECOND_THRESHOLD else raw


def _to_datetime(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)


def kline_open_time_ms(row: Sequence[Any]) -> int:
    """Normalised open time of a kline row. This is Binance's unique key."""
    if len(row) < _MIN_KLINE_FIELDS:
        raise BinanceParseError(
            f"Kline row has {len(row)} fields, expected at least {_MIN_KLINE_FIELDS}: {row!r}"
        )
    return normalise_epoch_ms(int(row[_OPEN_TIME]))


def kline_to_bar(row: Sequence[Any], symbol: str, interval: str) -> Bar:
    """Convert one kline array to a close-stamped ``Bar``.

    The timestamp is ``openTime + interval`` — computed, never read from the
    response's ``closeTime`` field, which is one millisecond early.

    All OHLCV values arrive as JSON *strings*. They are passed to ``Decimal``
    verbatim; routing them through ``float`` first would discard precision at
    the one boundary where it is cheapest to keep.
    """
    open_ms = kline_open_time_ms(row)
    close_ms = open_ms + interval_ms(interval)
    return Bar(
        symbol=symbol,
        timestamp=_to_datetime(close_ms),
        open=Decimal(str(row[_OPEN])),
        high=Decimal(str(row[_HIGH])),
        low=Decimal(str(row[_LOW])),
        close=Decimal(str(row[_CLOSE])),
        volume=Decimal(str(row[_VOLUME])),
    )


def klines_to_bars(
    rows: Iterable[Sequence[Any]],
    symbol: str,
    interval: str,
    *,
    now_ms: int,
    drop_forming: bool = True,
) -> list[Bar]:
    """Parse a kline response into bars, dropping any still-forming final bar.

    ``now_ms`` is passed in rather than read from the clock. The engine never
    reads wall time (see ``CLAUDE.md``), and an injected value is what makes
    forming-bar rejection testable at all.

    Gaps are permitted — exchanges have outages, and silently interpolating over
    a hole hides the problem it creates. Bars that are out of order or duplicated
    are rejected, because those indicate a broken source rather than a real one.
    """
    step = interval_ms(interval)
    bars: list[Bar] = []
    previous_open: int | None = None

    for row in rows:
        open_ms = kline_open_time_ms(row)

        if previous_open is not None:
            if open_ms == previous_open:
                raise BinanceParseError(
                    f"{symbol} {interval}: duplicate bar at open time {open_ms}"
                )
            if open_ms < previous_open:
                raise BinanceParseError(
                    f"{symbol} {interval}: open time {open_ms} precedes {previous_open}. "
                    "Source is not in chronological order."
                )
            if (open_ms - previous_open) % step != 0:
                raise BinanceParseError(
                    f"{symbol} {interval}: open times {previous_open} -> {open_ms} are not a "
                    f"whole number of {step}ms intervals apart. Mixed intervals, or a "
                    "millisecond/microsecond boundary that was not normalised."
                )

        if drop_forming and open_ms + step > now_ms:
            # Still forming: its close is in the future and its OHLC will change.
            previous_open = open_ms
            continue

        bars.append(kline_to_bar(row, symbol, interval))
        previous_open = open_ms

    return bars


@dataclass(frozen=True, slots=True)
class FundingRate:
    """One funding settlement on a perpetual contract.

    ``rate`` is a decimal *fraction*, not a percentage: Binance's ``0.00010000``
    means 0.01%. It can be negative, in which case shorts pay longs.

    ``timestamp`` is the settlement instant. A position pays or receives only if
    it is open at exactly that moment.
    """

    symbol: str
    timestamp: datetime
    rate: Decimal
    mark_price: Decimal | None = None

    def payment_on(self, signed_notional: Decimal) -> Decimal:
        """Cash impact for a position of ``signed_notional`` at this settlement.

        Longs pay and shorts receive when the rate is positive; both invert when
        it is negative. Delegates to ``core.types.funding_payment`` so the sign
        convention has exactly one definition.
        """
        return funding_payment(signed_notional, self.rate)


def parse_funding_rate(row: dict[str, Any]) -> FundingRate:
    """Parse one entry from ``GET /fapi/v1/fundingRate``.

    ``markPrice`` was added to this endpoint later and may be absent or empty on
    older records, so it is optional.
    """
    try:
        symbol = str(row["symbol"])
        timestamp = _to_datetime(normalise_epoch_ms(int(row["fundingTime"])))
        rate = Decimal(str(row["fundingRate"]))
    except KeyError as exc:
        raise BinanceParseError(f"Funding row missing required field {exc}: {row!r}") from None

    raw_mark = row.get("markPrice")
    mark = Decimal(str(raw_mark)) if raw_mark not in (None, "") else None
    return FundingRate(symbol=symbol, timestamp=timestamp, rate=rate, mark_price=mark)


def parse_funding_rates(rows: Iterable[dict[str, Any]]) -> list[FundingRate]:
    """Parse a funding history response, deduplicating on settlement instant.

    Deduplication is on ``fundingTime`` because paging windows overlap at the
    boundaries and the same settlement is routinely returned twice.
    """
    seen: dict[datetime, FundingRate] = {}
    for row in rows:
        entry = parse_funding_rate(row)
        seen[entry.timestamp] = entry
    return [seen[ts] for ts in sorted(seen)]


def observed_funding_interval_hours(rates: Sequence[FundingRate]) -> Decimal | None:
    """Most common gap between consecutive settlements, in hours.

    Derived from the data rather than assumed. Binance moved selected perpetuals
    from 8-hourly to 4-hourly funding on 2023-10-12, so a multi-year series
    contains both regimes and any hardcoded interval is wrong for part of it.
    Returns ``None`` when there are too few settlements to tell.
    """
    if len(rates) < 2:
        return None
    gaps: dict[int, int] = {}
    for earlier, later in itertools.pairwise(rates):
        seconds = int((later.timestamp - earlier.timestamp).total_seconds())
        if seconds > 0:
            gaps[seconds] = gaps.get(seconds, 0) + 1
    if not gaps:
        return None
    most_common = max(gaps, key=lambda s: gaps[s])
    return Decimal(most_common) / Decimal(3600)
