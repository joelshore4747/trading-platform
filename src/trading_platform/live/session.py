"""Trading sessions, so a market being shut is not mistaken for a market moving.

Crypto never closes. Equities close nightly, at weekends, on holidays, and early
on a handful of days a year — and every one of those is a hole in the bar series
that looks exactly like quiet trading. A strategy that reads Friday's close and
Monday's open as consecutive bars has just measured a two-day gap as a one-bar
move, which is the same class of error as the vendor archive's open-stamping:
silent, plausible, and fatal to anything time-sensitive.

**What is derivable is derived; what is not is refused.**

Most US market holidays follow rules — fixed dates with weekend observance
shifted, nth-weekday-of-month, and Good Friday, which is computable from Easter.
Those are calculated, so the calendar cannot go stale on them.

Ad-hoc closures cannot be derived: a national day of mourning, a hurricane, the
week of September 2001. There is no algorithm for those, so they must be
supplied, and a calendar asked about a date outside the range it was told it
knows **raises** rather than guessing. That is the same refusal `core/fx.py`
makes for an unknown rate and `data/universe.py` makes for a censored listing:
the failure mode of guessing here is a backtest that trades on a day the market
was shut, and reports a profit for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo


class CalendarUnknown(LookupError):
    """The calendar was asked about a date it was never told about.

    Raised rather than answered, because a wrong answer here is invisible: a
    strategy simply trades on a day the venue was closed, and the equity curve
    looks fine.
    """


def easter_sunday(year: int) -> date:
    """Gregorian Easter, by the anonymous algorithm. Exact, not approximate.

    Needed because Good Friday is the one moveable US market holiday, and a
    calendar that silently omitted it would leave one trading day a year that
    never existed.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    month, day = divmod(h + lam - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """`nth`-th `weekday` of a month; negative `nth` counts back from the end."""
    if nth > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (nth - 1))
    following = date(year + (month == 12), (month % 12) + 1, 1)
    last = following - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset + 7 * (-nth - 1))


def _observed(day: date) -> date:
    """Weekend holidays are observed on the adjacent weekday."""
    if day.weekday() == 5:  # Saturday -> Friday
        return day - timedelta(days=1)
    if day.weekday() == 6:  # Sunday -> Monday
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=64)
def us_market_holidays(year: int) -> frozenset[date]:
    """The rule-derivable NYSE/Nasdaq holidays for one year.

    Derived rather than listed, so it cannot go stale. **Not complete**: ad-hoc
    closures have no rule and must be supplied to the calendar separately.
    """
    return frozenset(
        {
            _observed(date(year, 1, 1)),  # New Year's Day
            _nth_weekday(year, 1, 0, 3),  # MLK Day
            _nth_weekday(year, 2, 0, 3),  # Presidents' Day
            easter_sunday(year) - timedelta(days=2),  # Good Friday
            _nth_weekday(year, 5, 0, -1),  # Memorial Day
            _observed(date(year, 6, 19)),  # Juneteenth
            _observed(date(year, 7, 4)),  # Independence Day
            _nth_weekday(year, 9, 0, 1),  # Labor Day
            _nth_weekday(year, 11, 3, 4),  # Thanksgiving
            _observed(date(year, 12, 25)),  # Christmas
        }
    )


@dataclass(frozen=True, slots=True)
class TradingCalendar:
    """When a venue is open, and an honest answer when that is not known."""

    name: str
    #: Empty means the venue never closes, and every query is answerable.
    timezone: str = ""
    open_time: time | None = None
    close_time: time | None = None
    #: Monday is 0. Empty means every day.
    weekdays: frozenset[int] = frozenset()
    #: Closures no rule can derive. Supplied, never guessed.
    ad_hoc_closures: frozenset[date] = frozenset()
    #: Early closes, e.g. 13:00 the day after Thanksgiving.
    early_closes: dict[date, time] = field(default_factory=dict)
    #: Whether the rule-derived US holiday set applies.
    uses_us_holidays: bool = False
    #: The span over which ad-hoc closures are asserted complete. Outside it a
    #: query raises: the derived holidays would still be right, and the ad-hoc
    #: ones silently absent, which is the worse kind of wrong.
    known_from: date | None = None
    known_until: date | None = None

    @property
    def always_open(self) -> bool:
        return self.open_time is None and self.close_time is None

    def _check_known(self, day: date) -> None:
        if self.always_open:
            return
        if self.known_from is not None and day < self.known_from:
            raise CalendarUnknown(
                f"{self.name}: {day} is before {self.known_from}, the first date this "
                "calendar's ad-hoc closures are asserted to cover. Extend them or state "
                "explicitly that none applied."
            )
        if self.known_until is not None and day > self.known_until:
            raise CalendarUnknown(
                f"{self.name}: {day} is after {self.known_until}, the last date this "
                "calendar's ad-hoc closures are asserted to cover. The rule-derived "
                "holidays would still be right and any ad-hoc closure would be silently "
                "missing, which is the worse kind of wrong."
            )

    def is_holiday(self, day: date) -> bool:
        self._check_known(day)
        if day in self.ad_hoc_closures:
            return True
        return self.uses_us_holidays and day in us_market_holidays(day.year)

    def is_trading_day(self, day: date) -> bool:
        if self.always_open:
            return True
        if self.weekdays and day.weekday() not in self.weekdays:
            return False
        return not self.is_holiday(day)

    def close_on(self, day: date) -> time | None:
        """Closing time for a day, honouring early closes."""
        if self.always_open:
            return None
        return self.early_closes.get(day, self.close_time)

    def is_open(self, at: datetime) -> bool:
        """Whether the venue is trading at an instant.

        `at` must be timezone-aware. A naive datetime here would be interpreted
        as whatever the host's locale happens to be, which turns a correctness
        question into a deployment one.
        """
        if at.tzinfo is None:
            raise ValueError(f"is_open needs a timezone-aware datetime, got {at!r}")
        if self.always_open:
            return True

        local = at.astimezone(ZoneInfo(self.timezone))
        day = local.date()
        if not self.is_trading_day(day):
            return False

        close = self.close_on(day)
        assert self.open_time is not None and close is not None
        return self.open_time <= local.time() < close

    def spans_a_close(self, earlier: datetime, later: datetime) -> bool:
        """Whether the venue closed between two instants.

        The question a strategy needs before treating two consecutive bars as
        consecutive prices. Across a close, the second bar's open is a fresh
        auction that absorbed everything that happened while the book was shut —
        it is not one interval of drift from the first.
        """
        if earlier > later:
            raise ValueError(f"{earlier} is not before {later}")
        if self.always_open:
            return False
        # Any closed minute between them is a close. Checked at the boundaries
        # and each intervening local midnight, which is enough: a session is
        # contiguous within a day, so a gap can only appear at an edge.
        if not self.is_open(earlier) or not self.is_open(later):
            return True
        return (
            earlier.astimezone(ZoneInfo(self.timezone)).date()
            != later.astimezone(ZoneInfo(self.timezone)).date()
        )


#: Crypto. Never closes, so every query is answerable and none can raise.
ALWAYS_OPEN = TradingCalendar(name="always-open")


def us_equities(
    *,
    known_from: date,
    known_until: date,
    ad_hoc_closures: frozenset[date] = frozenset(),
    early_closes: dict[date, time] | None = None,
) -> TradingCalendar:
    """NYSE/Nasdaq regular hours: 09:30 to 16:00 America/New_York, weekdays.

    `known_from` and `known_until` are **required**, with no default. A default
    would be a claim about which dates the ad-hoc closures cover, made by this
    function rather than by the caller who actually knows — and the caller is
    the one who can be wrong about it usefully.

    Regular-hours only: pre- and post-market are a different session with
    different liquidity, and treating them as one is how a backtest fills at a
    price nobody could have traded in size.
    """
    return TradingCalendar(
        name="us-equities",
        timezone="America/New_York",
        open_time=time(9, 30),
        close_time=time(16, 0),
        weekdays=frozenset({0, 1, 2, 3, 4}),
        ad_hoc_closures=ad_hoc_closures,
        early_closes=early_closes or {},
        uses_us_holidays=True,
        known_from=known_from,
        known_until=known_until,
    )
