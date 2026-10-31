"""One interval vocabulary.

The repository had five representations of a bar interval and no conversion
between them, so every boundary was a chance to mean a different thing. These
tests pin the vocabulary and, more importantly, pin the refusals.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from trading_platform.data.intervals import (
    INTERVAL_MS,
    UnknownIntervalError,
    interval_ms,
    interval_to_timedelta,
    timedelta_to_interval,
)


@pytest.mark.parametrize("name", sorted(INTERVAL_MS))
def test_every_interval_round_trips(name: str) -> None:
    assert timedelta_to_interval(interval_to_timedelta(name)) == name


def test_the_conversions_agree_with_the_table() -> None:
    assert interval_to_timedelta("15m") == timedelta(minutes=15)
    assert interval_to_timedelta("4h") == timedelta(hours=4)
    assert timedelta_to_interval(timedelta(days=1)) == "1d"


@pytest.mark.parametrize("name", ["1M", "1mo"])
def test_a_calendar_month_is_refused_not_approximated(name: str) -> None:
    """A month is not a fixed number of milliseconds. Treating it as 30 days
    drifts by up to three, and a store keyed by interval must not hold a key
    whose meaning moves."""
    with pytest.raises(UnknownIntervalError, match="calendar month"):
        interval_ms(name)


def test_an_unspellable_duration_raises_rather_than_being_coined() -> None:
    """A silently coined key like '7m' would read back as a cache miss forever."""
    with pytest.raises(UnknownIntervalError, match="No interval name"):
        timedelta_to_interval(timedelta(minutes=7))


def test_interval_names_are_case_sensitive() -> None:
    assert interval_ms("1m") == 60_000
    with pytest.raises(UnknownIntervalError):
        interval_ms("1H")


def test_binance_still_raises_its_own_error_type() -> None:
    """The Binance parser delegates here but must keep surfacing parse errors
    as parse errors, because that is what its callers catch."""
    from trading_platform.data.binance import BinanceParseError
    from trading_platform.data.binance import interval_ms as binance_interval_ms

    with pytest.raises(BinanceParseError):
        binance_interval_ms("1M")
