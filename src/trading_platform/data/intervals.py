"""One vocabulary for bar intervals.

The repository grew five representations of the same idea: a Binance-style
string (``"1m"``), a ``timedelta``, an ``int`` of minutes on one CLI, a suffix
stripped from a filename and then discarded, and one the engine *infers* at
runtime from the smallest gap it observes. Nothing converted between them, so
each boundary was a chance to mean a different thing.

The string is canonical, because it is what goes in a cache path and what every
venue's API speaks. ``timedelta`` is the working form inside ``data/vendor.py``.
This module is the only place the two are allowed to meet.

A calendar month is deliberately not representable. It has no fixed length, and
a store keyed by interval must not contain a key whose meaning drifts by up to
three days.
"""

from __future__ import annotations

from datetime import timedelta

__all__ = [
    "INTERVAL_MS",
    "UnknownIntervalError",
    "interval_ms",
    "interval_to_timedelta",
    "timedelta_to_interval",
]


class UnknownIntervalError(ValueError):
    """An interval string or duration outside the supported vocabulary."""


#: Fixed-length intervals, in milliseconds. Binance's ``1M`` (one month) is
#: deliberately absent — a month is not a fixed number of milliseconds, and
#: pretending otherwise drifts by up to three days. Monthly bars need calendar
#: arithmetic; the converters below reject them explicitly rather than guessing.
INTERVAL_MS: dict[str, int] = {
    "1s": 1_000,
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}

_BY_MS: dict[int, str] = {ms: name for name, ms in INTERVAL_MS.items()}


def interval_ms(interval: str) -> int:
    """Length of ``interval`` in milliseconds.

    Raises for ``1M`` (calendar month) and anything unrecognised. Interval
    strings are case-sensitive: ``1m`` is a minute, ``1M`` is a month, and the
    file archives spell the latter ``1mo``.
    """
    try:
        return INTERVAL_MS[interval]
    except KeyError:
        if interval in ("1M", "1mo"):
            raise UnknownIntervalError(
                f"Interval {interval!r} is a calendar month, which has no fixed "
                "millisecond length. Monthly bars need calendar arithmetic and are "
                "not supported yet."
            ) from None
        raise UnknownIntervalError(
            f"Unknown interval {interval!r}. Valid: {sorted(INTERVAL_MS)} "
            "(case-sensitive — '1m' is a minute, '1M' is a month)."
        ) from None


def interval_to_timedelta(interval: str) -> timedelta:
    """``"15m"`` -> ``timedelta(minutes=15)``."""
    return timedelta(milliseconds=interval_ms(interval))


def timedelta_to_interval(duration: timedelta) -> str:
    """``timedelta(minutes=15)`` -> ``"15m"``.

    Raises rather than inventing a name for an unsupported duration. A store
    keyed by interval cannot hold a key it cannot spell, and a silently coined
    key like ``"7m"`` would read back as a cache miss forever.
    """
    total_ms = round(duration.total_seconds() * 1000)
    try:
        return _BY_MS[total_ms]
    except KeyError:
        raise UnknownIntervalError(
            f"No interval name for {duration!r} ({total_ms} ms). Valid: {sorted(INTERVAL_MS)}."
        ) from None
