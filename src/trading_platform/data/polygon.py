"""Polygon aggregate-bar parsing. **Network-free**; the transport lives in ``live/polygon.py``.

Polygon's aggregates endpoint returns, per ticker::

    {"ticker": "AAPL", "results": [
        {"t": 1756728000000, "o": 100.1, "h": 101.0, "l": 99.8, "c": 100.5,
         "v": 12345, "vw": 100.4, "n": 321}], "next_url": "..."}

Two things decide everything here, and they are the same two that decided the
Alpaca parser — which is the point: every vendor gets these wrong differently
and a reader should be able to check both against each other.

**``t`` is the START of the aggregate window**, in epoch milliseconds. The
engine stamps bars at their close, so the timestamp is ``t + timespan``,
computed. Passing the start through gives every signal one bar of hindsight.
That is defect D01 in ``evals/defects/``, found once here already, and the
first test in ``tests/test_polygon.py`` pins it.

**Prices are JSON numbers.** Polygon sends floats, so the best available
precision is ``Decimal(str(x))`` — exact for what the JSON decoder produced,
which is the value to the precision Polygon chose to send, and no more. Binance
sends strings and keeps full precision; the difference between the two vendors
is a property of their encodings and is recorded rather than smoothed over.

Everything about the wire format here is **UNVERIFIED**: this sandbox answers
403 to api.polygon.io, so it is written from the published API reference and
pinned by tests. The first real response is the verification, and until one
agrees with these tests no figure from this path should be believed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_platform.core.events import Bar
from trading_platform.data.intervals import UnknownIntervalError, interval_ms

__all__ = [
    "PolygonParseError",
    "bars_from_payload",
    "polygon_timespan",
    "result_to_bar",
]

#: Interval to Polygon's (multiplier, timespan) pair.
_TIMESPANS: dict[str, tuple[int, str]] = {
    "1m": (1, "minute"),
    "5m": (5, "minute"),
    "15m": (15, "minute"),
    "30m": (30, "minute"),
    "1h": (1, "hour"),
    "4h": (4, "hour"),
    "1d": (1, "day"),
    "1w": (1, "week"),
}


class PolygonParseError(ValueError):
    """A response does not match the documented shape."""


def polygon_timespan(interval: str) -> tuple[int, str]:
    """(multiplier, timespan) for a repository interval, e.g. 5m -> (5, "minute")."""
    try:
        return _TIMESPANS[interval]
    except KeyError:
        raise PolygonParseError(
            f"no Polygon timespan for interval {interval!r}; known: {sorted(_TIMESPANS)}"
        ) from None


def _interval(interval: str) -> timedelta:
    try:
        return timedelta(milliseconds=interval_ms(interval))
    except UnknownIntervalError as exc:
        raise PolygonParseError(str(exc)) from None


def _decimal(row: Mapping[str, Any], key: str, field: str) -> Decimal:
    if key not in row:
        raise PolygonParseError(f"aggregate missing {field} ({key!r}): {row!r}")
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise PolygonParseError(f"aggregate {field} ({key!r}) is not a number: {value!r}")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise PolygonParseError(f"aggregate {field} ({key!r}) is not a number: {value!r}") from None


def result_to_bar(row: Mapping[str, Any], symbol: str, interval: str) -> Bar:
    """One aggregate to a close-stamped ``Bar``. ``t`` is the window's START."""
    raw = row.get("t")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise PolygonParseError(f"aggregate missing integer 't': {row!r}")
    if raw < 0:
        raise PolygonParseError(f"aggregate 't' is negative: {raw!r}")
    opened = datetime.fromtimestamp(raw / 1000, tz=UTC)
    return Bar(
        symbol=symbol,
        timestamp=opened + _interval(interval),
        open=_decimal(row, "o", "open"),
        high=_decimal(row, "h", "high"),
        low=_decimal(row, "l", "low"),
        close=_decimal(row, "c", "close"),
        volume=_decimal(row, "v", "volume"),
    )


def bars_from_payload(
    payload: Mapping[str, Any],
    interval: str,
    *,
    symbol: str | None = None,
) -> tuple[list[Bar], str | None]:
    """Every bar in one response page, sorted, plus the next page URL.

    Polygon reports its own status. `OK` and `DELAYED` both carry results;
    `DELAYED` means the plan is on delayed data, which is a fact about the
    figures and is left to the caller rather than silently accepted here.
    Anything else with no results raises rather than returning an empty list,
    because "no data" and "your request was wrong" must not look alike.
    """
    ticker = symbol or payload.get("ticker")
    if not isinstance(ticker, str) or not ticker:
        raise PolygonParseError("response carries no ticker and none was supplied")

    status = payload.get("status")
    results = payload.get("results")
    if results is None:
        if status in ("OK", "DELAYED") and payload.get("resultsCount") == 0:
            return [], None
        raise PolygonParseError(
            f"response for {ticker} has no results and status {status!r}: "
            f"{ {k: v for k, v in payload.items() if k != 'results'} }"
        )
    if not isinstance(results, list):
        raise PolygonParseError(f"results for {ticker} is not a list: {type(results).__name__}")

    bars = [result_to_bar(row, ticker, interval) for row in results]
    bars.sort(key=lambda b: b.timestamp)

    token = payload.get("next_url")
    if token is not None and not isinstance(token, str):
        raise PolygonParseError(f"next_url is not a string: {token!r}")
    return bars, token or None


def iter_pages(payloads: Iterable[Mapping[str, Any]], interval: str, symbol: str) -> list[Bar]:
    """Flatten several pages into one sorted series. Duplicates across page
    boundaries are dropped; a duplicate that DISAGREES raises, because a vendor
    contradicting itself between pages is a finding rather than a tie to break."""
    seen: dict[datetime, Bar] = {}
    for payload in payloads:
        page, _ = bars_from_payload(payload, interval, symbol=symbol)
        for bar in page:
            existing = seen.get(bar.timestamp)
            if existing is None:
                seen[bar.timestamp] = bar
            elif existing != bar:
                raise PolygonParseError(
                    f"{symbol} at {bar.timestamp.isoformat()} appears twice across pages with "
                    f"different values: {existing} then {bar}"
                )
    return [seen[t] for t in sorted(seen)]
