"""Coinbase Exchange candle parsing. **Network-free**; the transport lives in ``live/coinbase.py``.

Coinbase's ``/products/{id}/candles`` endpoint returns a JSON array of arrays::

    [[1788825600, 77589.0, 79477.07, 79091.97, 78579.99, 3205.38629433], ...]

**Read that array carefully, because it is not OHLC.** The documented order is::

    [ time, LOW, HIGH, OPEN, CLOSE, volume ]

Low and high come *first*, and open comes *after* high. A parser that assumes
the conventional open-high-low-close order therefore puts the true OPEN into
``low`` and the true LOW into ``open``.

For most candles that is caught: ``low <= open`` holds on real data, so the
swap makes ``low > open`` and ``Bar.__post_init__`` refuses it. The net is real
and this module does not need to be the only thing standing. But it fails for a
candle that opens exactly on its low — ordinary in an illiquid minute — where
the misread stays inside ``[low, high]`` and is accepted in silence. That case,
and the quieter high/close confusion, are why the order is written down once as
named constants rather than as bare indices at the point of use.

Three more things differ from every other vendor in this repository, and each
is silent rather than loud when it is wrong:

**``time`` is in SECONDS.** Binance and Polygon both send milliseconds. A
millisecond reading of a Coinbase timestamp lands in January 1970, which is at
least obvious; the reverse would not be.

**``time`` is the START of the bucket.** The engine stamps bars at their close,
so the timestamp is ``time + interval``, computed. Passing the start through
gives every signal one bar of hindsight — defect D01 in ``evals/``, found here
once already, and the reason ``data/alpaca.py`` and ``data/polygon.py`` carry
the same note.

**Rows arrive newest-first.** Binance and Polygon are both ascending. Bars are
sorted here rather than trusted, because ``cache.write_bars`` refuses a series
that is not strictly increasing and a reversed series would fail far from its
cause.

Prices are JSON numbers, so the best available precision is ``Decimal(str(x))``
— exact for what the JSON decoder produced. Binance sends strings and keeps
full precision; the difference is a property of the vendors' encodings and is
recorded rather than smoothed over.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_platform.core.events import Bar
from trading_platform.data.intervals import UnknownIntervalError, interval_ms

__all__ = [
    "SUPPORTED_GRANULARITY_S",
    "CoinbaseParseError",
    "bars_from_payload",
    "coinbase_granularity",
    "row_to_bar",
]

#: The candle array's layout, stated once. Every read below goes through these
#: names; no bare index appears anywhere else in this module.
_TIME, _LOW, _HIGH, _OPEN, _CLOSE, _VOLUME = range(6)
_ROW_WIDTH = 6

#: Granularities Coinbase accepts, in seconds. Anything else answers
#: ``400 Unsupported granularity``. Measured against the live API 2026-09-08,
#: and every one of them already has a name in ``INTERVAL_MS``.
SUPPORTED_GRANULARITY_S: tuple[int, ...] = (60, 300, 900, 3600, 21600, 86400)


class CoinbaseParseError(ValueError):
    """A Coinbase response is not the shape this module knows how to read."""


def coinbase_granularity(interval: str) -> int:
    """Seconds for a repository interval string, or raise.

    Derived from ``INTERVAL_MS`` rather than from a table of its own: a second
    mapping between the same two vocabularies is a second place for them to
    disagree, which is the defect ``data/intervals.py`` exists to have fixed.
    """
    try:
        seconds = interval_ms(interval) // 1000
    except UnknownIntervalError:
        raise
    if seconds not in SUPPORTED_GRANULARITY_S:
        names = ", ".join(_interval_name(s) for s in SUPPORTED_GRANULARITY_S)
        raise CoinbaseParseError(f"Coinbase does not serve {interval!r}. It accepts only: {names}.")
    return seconds


def _interval_name(seconds: int) -> str:
    from trading_platform.data.intervals import timedelta_to_interval

    return timedelta_to_interval(timedelta(seconds=seconds))


def _decimal(row: Sequence[Any], index: int, field: str) -> Decimal:
    try:
        return Decimal(str(row[index]))
    except (InvalidOperation, TypeError, ValueError):
        raise CoinbaseParseError(f"{field} is not a number: {row[index]!r}") from None


def row_to_bar(row: Sequence[Any], symbol: str, interval: str) -> Bar:
    """One candle to a close-stamped ``Bar``.

    ``row`` is ``[time, low, high, open, close, volume]`` and ``time`` is the
    bucket's start, in seconds.
    """
    if not isinstance(row, Sequence) or isinstance(row, str | bytes):
        raise CoinbaseParseError(f"candle is not an array: {row!r}")
    if len(row) != _ROW_WIDTH:
        raise CoinbaseParseError(
            f"candle has {len(row)} field(s), expected {_ROW_WIDTH} "
            f"[time, low, high, open, close, volume]: {row!r}"
        )
    try:
        started_at = datetime.fromtimestamp(int(row[_TIME]), UTC)
    except (OSError, OverflowError, TypeError, ValueError):
        raise CoinbaseParseError(f"candle time is not epoch seconds: {row[_TIME]!r}") from None

    return Bar(
        symbol=symbol,
        timestamp=started_at + timedelta(seconds=coinbase_granularity(interval)),
        open=_decimal(row, _OPEN, "open"),
        high=_decimal(row, _HIGH, "high"),
        low=_decimal(row, _LOW, "low"),
        close=_decimal(row, _CLOSE, "close"),
        volume=_decimal(row, _VOLUME, "volume"),
    )


def bars_from_payload(
    payload: Any,
    symbol: str,
    interval: str,
    *,
    now: datetime,
    drop_forming: bool = True,
) -> list[Bar]:
    """Every CLOSED candle in one response, close-stamped and sorted ascending.

    ``now`` is passed in rather than read from the clock, for the reason
    ``data/binance.py`` gives: the engine never reads wall time, and an
    injected value is what makes forming-bar rejection testable at all.

    Coinbase returns the in-progress bucket like any other candle, with nothing
    marking it unclosed. Written to the cache it becomes a fraction of a day
    recorded as a whole one, at the most recent bar — the one most likely to
    matter to whatever prompted the fetch. T-aae4a1eb.

    A repeated timestamp carrying different values is raised rather than
    resolved. Overlapping windows legitimately repeat their edge candle, so an
    identical repeat is dropped — but a venue contradicting itself is a finding,
    not a tie to break. The same rule ``data/polygon.py`` applies, and for the
    same reason it had to be added there.
    """
    if isinstance(payload, Mapping):
        message = payload.get("message")
        raise CoinbaseParseError(f"Coinbase refused the request: {message or payload!r}")
    if not isinstance(payload, list):
        raise CoinbaseParseError(f"response is not a list of candles: {type(payload).__name__}")

    seen: dict[datetime, Bar] = {}
    for row in payload:
        bar = row_to_bar(row, symbol, interval)
        if drop_forming and bar.timestamp > now:
            # Its close is in the future, so its OHLC and volume will change.
            continue
        existing = seen.get(bar.timestamp)
        if existing is None:
            seen[bar.timestamp] = bar
            continue
        if existing != bar:
            raise CoinbaseParseError(
                f"{symbol} at {bar.timestamp.isoformat()} appears twice with different "
                f"values: {existing} then {bar}"
            )

    # The wire is newest-first. Sorted here rather than trusted: write_bars
    # refuses a series that is not strictly increasing, and a reversed series
    # would fail a long way from the thing that reversed it.
    return sorted(seen.values(), key=lambda b: b.timestamp)
