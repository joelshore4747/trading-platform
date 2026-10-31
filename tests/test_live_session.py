"""Guards for trading sessions.

A closed market and a quiet market look identical in a bar series, so the tests
are about the cases where confusing them changes a result: overnight gaps,
weekends, holidays that move, and the dates the calendar has no business
answering about.

Every derived holiday is checked against a date that can be verified
independently rather than against the algorithm's own output.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from trading_platform.live.session import (
    ALWAYS_OPEN,
    CalendarUnknown,
    TradingCalendar,
    easter_sunday,
    us_equities,
    us_market_holidays,
)

KNOWN_FROM, KNOWN_UNTIL = date(2026, 1, 1), date(2026, 12, 31)


def nyse(**kwargs: object) -> TradingCalendar:
    return us_equities(known_from=KNOWN_FROM, known_until=KNOWN_UNTIL, **kwargs)  # type: ignore[arg-type]


def et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """An instant expressed in New York time, as a venue would state it."""
    from zoneinfo import ZoneInfo

    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York"))


# -- the derived holidays -----------------------------------------------------


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2024, date(2024, 3, 31)),
        (2025, date(2025, 4, 20)),
        (2026, date(2026, 4, 5)),
        (2027, date(2027, 3, 28)),
    ],
)
def test_easter_matches_independently_known_dates(year: int, expected: date) -> None:
    """Good Friday is the one moveable US market holiday, so this has to be exact."""
    assert easter_sunday(year) == expected


def test_good_friday_is_a_holiday() -> None:
    assert date(2026, 4, 3) in us_market_holidays(2026)


def test_a_saturday_holiday_is_observed_on_the_friday() -> None:
    """4 July 2026 falls on a Saturday, so the market shuts on Friday the 3rd."""
    holidays = us_market_holidays(2026)
    assert date(2026, 7, 3) in holidays
    assert date(2026, 7, 4) not in holidays


def test_a_sunday_holiday_is_observed_on_the_monday() -> None:
    """1 January 2028 is a Saturday; 2022's New Year fell on a Saturday too.
    2023-01-01 was a Sunday, observed Monday the 2nd."""
    assert date(2023, 1, 2) in us_market_holidays(2023)


def test_an_ordinary_weekday_is_not_a_holiday() -> None:
    """The negative control. A set that contained everything would pass every
    positive test above and mean nothing."""
    assert date(2026, 3, 17) not in us_market_holidays(2026)


# -- crypto -------------------------------------------------------------------


def test_an_always_open_venue_never_closes_and_never_refuses() -> None:
    """Crypto has no sessions, so every query is answerable."""
    for at in (
        datetime(2026, 1, 1, 3, 0, tzinfo=UTC),  # a holiday, overnight
        datetime(2026, 8, 16, 12, 0, tzinfo=UTC),  # a Sunday
        datetime(2199, 1, 1, tzinfo=UTC),  # far outside any known range
    ):
        assert ALWAYS_OPEN.is_open(at)
    assert not ALWAYS_OPEN.spans_a_close(
        datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
    )


# -- equities -----------------------------------------------------------------


def test_regular_hours_open_and_close() -> None:
    calendar = nyse()
    assert not calendar.is_open(et(2026, 3, 17, 9, 29))
    assert calendar.is_open(et(2026, 3, 17, 9, 30))
    assert calendar.is_open(et(2026, 3, 17, 15, 59))
    assert not calendar.is_open(et(2026, 3, 17, 16, 0)), "the close is exclusive"


def test_the_venue_is_shut_at_weekends_and_on_holidays() -> None:
    calendar = nyse()
    assert not calendar.is_open(et(2026, 3, 21, 12, 0)), "Saturday"
    assert not calendar.is_open(et(2026, 4, 3, 12, 0)), "Good Friday"
    assert not calendar.is_open(et(2026, 7, 3, 12, 0)), "observed Independence Day"


def test_daylight_saving_is_handled_by_the_zone_not_by_an_offset() -> None:
    """14:30 UTC is inside the session in summer and before it in winter.

    Hardcoding -5 or -4 would be right for half the year, which is the sort of
    bug that shows up as a strategy that mysteriously stops trading in March.
    """
    calendar = nyse()
    assert calendar.is_open(datetime(2026, 7, 15, 14, 30, tzinfo=UTC)), "EDT: 10:30 local"
    assert not calendar.is_open(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)), "EST: 09:00 local"


def test_an_early_close_is_honoured() -> None:
    calendar = nyse(early_closes={date(2026, 11, 27): time(13, 0)})
    assert calendar.is_open(et(2026, 11, 27, 12, 59))
    assert not calendar.is_open(et(2026, 11, 27, 13, 30))
    assert calendar.is_open(et(2026, 11, 30, 13, 30)), "the Monday is a full day"


def test_an_ad_hoc_closure_is_respected() -> None:
    """No rule derives a day of mourning, so it has to be supplied."""
    calendar = nyse(ad_hoc_closures=frozenset({date(2026, 3, 17)}))
    assert not calendar.is_open(et(2026, 3, 17, 12, 0))
    assert nyse().is_open(et(2026, 3, 17, 12, 0)), "and it is open without the closure"


# -- the refusal --------------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "match"),
    [(et(2025, 6, 10, 12, 0), "before 2026-01-01"), (et(2027, 6, 10, 12, 0), "after 2026-12-31")],
)
def test_a_date_outside_the_known_range_raises(at: datetime, match: str) -> None:
    """The derived holidays would still be right and any ad-hoc closure silently
    missing — a plausible answer that is wrong on exactly the days that matter."""
    with pytest.raises(CalendarUnknown, match=match):
        nyse().is_open(at)


def test_a_naive_datetime_is_refused() -> None:
    """Interpreting it as the host's locale turns correctness into deployment."""
    with pytest.raises(ValueError, match="timezone-aware"):
        nyse().is_open(datetime(2026, 3, 17, 12, 0))


# -- the property a strategy actually needs -----------------------------------


def test_two_bars_inside_one_session_do_not_span_a_close() -> None:
    calendar = nyse()
    assert not calendar.spans_a_close(et(2026, 3, 17, 10, 0), et(2026, 3, 17, 15, 0))


def test_an_overnight_pair_spans_a_close() -> None:
    """The case that matters: yesterday's last bar and today's first are not
    one interval apart, and a strategy differencing them measures a gap."""
    calendar = nyse()
    assert calendar.spans_a_close(et(2026, 3, 17, 15, 59), et(2026, 3, 18, 9, 30))


def test_a_weekend_pair_spans_a_close() -> None:
    calendar = nyse()
    assert calendar.spans_a_close(et(2026, 3, 20, 15, 59), et(2026, 3, 23, 9, 30))


def test_spanning_is_refused_when_the_order_is_backwards() -> None:
    calendar = nyse()
    with pytest.raises(ValueError, match="not before"):
        calendar.spans_a_close(et(2026, 3, 18, 10, 0), et(2026, 3, 17, 10, 0))
