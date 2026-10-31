"""Killzones: time-of-day windows, in a named timezone, refusing to guess.

The folklore holds that the hours around the London and New York opens carry the
day's real moves and the rest is noise. It is the most testable claim in the
whole vocabulary — a time filter is one parameter and its null is trivially
constructible — and also the one most exposed to overfitting, because a window
chosen after seeing the returns will always look good.

So: the windows here are the **published** ones, written down before any result,
and they are New York local time as the source states them. Converting to a
fixed UTC offset would be wrong twice a year, and a strategy that traded an hour
late every winter would look like a strategy that does not work.

A naive timestamp raises. There is no sensible default timezone for a question
that is entirely about what time it is somewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

__all__ = ["KILLZONES", "Killzone", "active_killzone", "in_killzone"]

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class Killzone:
    """A window in a named timezone. Half-open: start inclusive, end exclusive."""

    name: str
    start: time
    end: time
    tz: ZoneInfo = NEW_YORK

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError(
                f"{self.name}: {self.start} is not before {self.end}. A window that "
                "wraps midnight is two windows; say so explicitly."
            )

    def contains(self, moment: datetime) -> bool:
        if moment.tzinfo is None:
            raise ValueError(
                f"{self.name}: a naive timestamp has no time of day in {self.tz.key}. "
                "There is no sensible default for a question that is entirely about "
                "what time it is somewhere."
            )
        local = moment.astimezone(self.tz).time()
        return self.start <= local < self.end


#: The published windows, in New York local time, written down before any result.
KILLZONES: tuple[Killzone, ...] = (
    Killzone("london", time(2, 0), time(5, 0)),
    Killzone("new-york-am", time(7, 0), time(10, 0)),
    Killzone("new-york-pm", time(13, 30), time(16, 0)),
)


def in_killzone(moment: datetime, zones: tuple[Killzone, ...] = KILLZONES) -> bool:
    return any(zone.contains(moment) for zone in zones)


def active_killzone(moment: datetime, zones: tuple[Killzone, ...] = KILLZONES) -> Killzone | None:
    for zone in zones:
        if zone.contains(moment):
            return zone
    return None
