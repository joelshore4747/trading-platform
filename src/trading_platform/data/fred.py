"""FRED and ALFRED observation parsing. **Network-free**; the transport is in
`scripts/fetch_macro.py`.

FRED serves the current value of a series. **ALFRED serves every value the
series has ever had**, which is the one that matters here: a backtest that reads
today's GDP figure for a quarter three years ago has read a number nobody had at
the time. The two endpoints differ only in parameters, and this parser handles
both by treating the single-vintage case as the degenerate one.

The response shape::

    {"observations": [
        {"realtime_start": "2026-02-01", "realtime_end": "2026-03-01",
         "date": "2026-01-01", "value": "3.5"}]}

- `date` is the **period the number describes**.
- `realtime_start` is the **vintage**: the day this value became the published
  figure. It is the field the whole vintage design turns on.
- `realtime_end` is when it stopped being current. `9999-12-31` means it still
  is. Not stored: it is derivable from the next vintage, and storing it would
  create a second place for the truth to live.
- `value` is a **string**, and `"."` means the provider published no figure.
  That is a real answer and is stored as NULL, because "no value" and "we never
  fetched it" must not look alike.

Everything here is **UNVERIFIED**: `api.stlouisfed.org` answers 403 from this
sandbox, so this is written from the published API reference and pinned by
tests. The first real response is the verification.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "FredParseError",
    "Observation",
    "latest_by_period",
    "observations_from_payload",
    "parse_observation",
]

#: FRED's "still current" sentinel. Not a date anyone should compute with.
OPEN_ENDED = date(9999, 12, 31)

#: What FRED sends for a period it has no figure for.
MISSING = "."


class FredParseError(ValueError):
    """A response does not match the documented shape."""


@dataclass(frozen=True, slots=True)
class Observation:
    """One value, as published on one day, for one period."""

    #: The period the number describes.
    obs_date: date
    #: The day it became the published figure.
    vintage: date
    #: None when the provider published no figure. A real answer, not a gap.
    value: Decimal | None

    def __post_init__(self) -> None:
        if self.vintage < self.obs_date:
            # Not impossible in principle — a forecast — but FRED does not serve
            # those, so it means the fields have been read the wrong way round,
            # which silently grants the backtest hindsight.
            raise FredParseError(
                f"vintage {self.vintage} precedes the period {self.obs_date} it describes. "
                "FRED does not publish forecasts, so this usually means `date` and "
                "`realtime_start` have been swapped — which hands a backtest the future."
            )


def _date(raw: Any, field: str) -> date:
    if not isinstance(raw, str):
        raise FredParseError(f"{field} is not a string: {raw!r}")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise FredParseError(f"{field} is not an ISO date: {raw!r}") from None


def parse_observation(row: Mapping[str, Any]) -> Observation:
    """One row to an `Observation`. `realtime_start` is the vintage."""
    for field in ("date", "realtime_start", "value"):
        if field not in row:
            raise FredParseError(f"observation is missing {field!r}: {row!r}")

    raw_value = row["value"]
    if not isinstance(raw_value, str):
        raise FredParseError(f"value is not a string: {raw_value!r}")
    if raw_value.strip() == MISSING:
        value = None
    else:
        try:
            value = Decimal(raw_value.strip())
        except InvalidOperation:
            raise FredParseError(
                f"value is not a number and not {MISSING!r}: {raw_value!r}"
            ) from None

    return Observation(
        obs_date=_date(row["date"], "date"),
        vintage=_date(row["realtime_start"], "realtime_start"),
        value=value,
    )


def observations_from_payload(payload: Mapping[str, Any]) -> list[Observation]:
    """Every observation in a response, sorted by (period, vintage).

    An empty `observations` list with no error is a real empty answer and comes
    back empty. A response with no `observations` key at all is a rejected
    request wearing the shape of a successful one, and raises — "no data" and
    "your request was wrong" must not look alike.
    """
    rows = payload.get("observations")
    if rows is None:
        detail = {k: v for k, v in payload.items() if k != "observations"}
        raise FredParseError(f"response carries no `observations`: {detail}")
    if not isinstance(rows, list):
        raise FredParseError(f"`observations` is not a list: {type(rows).__name__}")

    out = [parse_observation(row) for row in rows]
    out.sort(key=lambda o: (o.obs_date, o.vintage))
    return out


def latest_by_period(observations: Iterable[Observation]) -> dict[date, Observation]:
    """The most recent vintage of each period — i.e. **today's** view.

    Provided, and named to be hard to misuse: this is the thing a backtest must
    NOT read. It is what a chart shows and what a person means by "the GDP
    figure", and reading it as of any date in the past is the revision trap.
    `store/macro.read_as_of` is what a backtest uses.
    """
    best: dict[date, Observation] = {}
    for obs in observations:
        current = best.get(obs.obs_date)
        if current is None or obs.vintage > current.vintage:
            best[obs.obs_date] = obs
    return best
