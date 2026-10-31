"""Alpaca market-data parsing. **Network-free**; the transport lives in ``live/alpaca.py``.

Alpaca's bar endpoints (stocks ``/v2/stocks/bars``, crypto
``/v1beta3/crypto/us/bars``) return, per symbol, rows shaped::

    {"t": "2026-09-01T13:30:00Z", "o": 100.1, "h": 101.0, "l": 99.8,
     "c": 100.5, "v": 12345, "n": 321, "vw": 100.4}

Two things about that shape decide everything here.

**``t`` is the bar's OPEN time.** The engine stamps bars at their close, so
the timestamp is ``t + timeframe``, computed. Passing the open time through
gives every signal one bar of hindsight — the D01 defect in ``evals/``, which
this module exists not to repeat.

**Prices are JSON numbers, not strings.** Binance sends strings and this
repository parses them into ``Decimal`` verbatim. Alpaca sends floats, so the
best available precision is ``Decimal(str(x))`` — exact for what the JSON
decoder produced, which is the documented value to the precision Alpaca chose
to send. Recorded rather than hidden: a sub-cent disagreement with another
source is a property of the vendor's encoding, not of the market.

Everything about the wire format that this module assumes is **UNVERIFIED in
this sandbox** (every market-data host answers 403). It is written from the
published API reference and must be checked against a real response on the
VPS before any figure from it is believed; ``tests/test_alpaca.py`` pins the
documented shape so a change on the wire fails loudly instead of quietly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_platform.core.events import Bar
from trading_platform.data.intervals import UnknownIntervalError, interval_ms

__all__ = [
    "STOCKS_FEEDS",
    "AlpacaParseError",
    "alpaca_timeframe",
    "bars_from_payload",
    "row_to_bar",
]

#: Alpaca's timeframe vocabulary for the intervals this repository uses.
_TIMEFRAMES = {
    "1m": "1Min",
    "5m": "5Min",
    "15m": "15Min",
    "30m": "30Min",
    "1h": "1Hour",
    "4h": "4Hour",
    "1d": "1Day",
}
#: Stock data feeds: ``iex`` is free and partial (one exchange), ``sip`` is
#: the consolidated tape on a paid plan. Which one produced a dataset is part
#: of its provenance and must be recorded with it.
STOCKS_FEEDS = ("iex", "sip")


class AlpacaParseError(ValueError):
    """A response does not match the documented shape."""


def alpaca_timeframe(interval: str) -> str:
    try:
        return _TIMEFRAMES[interval]
    except KeyError:
        raise AlpacaParseError(
            f"no Alpaca timeframe for interval {interval!r}; known: {sorted(_TIMEFRAMES)}"
        ) from None


def _interval(interval: str) -> timedelta:
    try:
        return timedelta(milliseconds=interval_ms(interval))
    except UnknownIntervalError as exc:
        raise AlpacaParseError(str(exc)) from None


def _decimal(row: Mapping[str, Any], key: str) -> Decimal:
    if key not in row:
        raise AlpacaParseError(f"bar row missing {key!r}: {row!r}")
    try:
        return Decimal(str(row[key]))
    except InvalidOperation:
        raise AlpacaParseError(f"bar row field {key!r} is not a number: {row[key]!r}") from None


def _open_time(row: Mapping[str, Any]) -> datetime:
    raw = row.get("t")
    if not isinstance(raw, str):
        raise AlpacaParseError(f"bar row missing string 't': {row!r}")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        raise AlpacaParseError(f"unparseable bar time {raw!r}") from None
    if stamp.tzinfo is None:
        raise AlpacaParseError(f"bar time {raw!r} carries no timezone; refusing to assume UTC")
    return stamp.astimezone(UTC)


def row_to_bar(row: Mapping[str, Any], symbol: str, interval: str) -> Bar:
    """One Alpaca bar row to a close-stamped ``Bar``. ``t`` is the open time."""
    close_at = _open_time(row) + _interval(interval)
    return Bar(
        symbol=symbol,
        timestamp=close_at,
        open=_decimal(row, "o"),
        high=_decimal(row, "h"),
        low=_decimal(row, "l"),
        close=_decimal(row, "c"),
        volume=_decimal(row, "v"),
    )


def bars_from_payload(
    payload: Mapping[str, Any], interval: str, *, symbols: Iterable[str] | None = None
) -> tuple[list[Bar], str | None]:
    """Every bar in one response page, sorted, plus the next page token.

    The ``bars`` object maps symbol to rows. A symbol asked for but absent
    from the response is not an error here — Alpaca omits symbols with no
    data in the window — but a symbol present that was not asked for is,
    because it means the request and the response disagree about what was
    fetched.
    """
    raw = payload.get("bars")
    if not isinstance(raw, Mapping):
        raise AlpacaParseError(f"response has no 'bars' object: {list(payload)!r}")
    wanted = None if symbols is None else set(symbols)
    out: list[Bar] = []
    for symbol, rows in raw.items():
        if wanted is not None and symbol not in wanted:
            raise AlpacaParseError(f"response carries {symbol!r}, which was not requested")
        if not isinstance(rows, list):
            raise AlpacaParseError(f"bars for {symbol!r} are not a list: {type(rows).__name__}")
        for row in rows:
            out.append(row_to_bar(row, symbol, interval))
    out.sort(key=lambda b: (b.timestamp, b.symbol))
    token = payload.get("next_page_token")
    if token is not None and not isinstance(token, str):
        raise AlpacaParseError(f"next_page_token is not a string: {token!r}")
    return out, token or None
