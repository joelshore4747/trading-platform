"""Funding settlement schedules for perpetual contracts.

A perpetual has no expiry; instead longs and shorts exchange a payment at
discrete instants to keep the contract tethered to spot. Over a multi-day hold
that payment is frequently *larger than the price move*, so a perp backtest
that omits funding is not approximately right — it is measuring the wrong thing.

This module holds the settlement series and answers one question for the event
loop: which settlements fall in a given time interval. Parsing lives in
``data/binance.py``; nothing here touches the network.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_platform.data.binance import (
    FundingRate,
    observed_funding_interval_hours,
    parse_funding_rates,
)

__all__ = ["FundingRate", "FundingSchedule"]


class FundingSchedule:
    """Funding settlements, indexed by symbol and ordered in time.

    Built once per run and queried per bar, so lookups are binary searches
    rather than scans — a multi-year 4-hourly series is several thousand
    settlements per symbol.
    """

    def __init__(self, rates: Iterable[FundingRate]) -> None:
        by_symbol: dict[str, list[FundingRate]] = {}
        for rate in rates:
            by_symbol.setdefault(rate.symbol, []).append(rate)

        self._by_symbol: dict[str, list[FundingRate]] = {}
        self._timestamps: dict[str, list[datetime]] = {}
        for symbol, entries in by_symbol.items():
            ordered = sorted(entries, key=lambda r: r.timestamp)
            seen: dict[datetime, FundingRate] = {r.timestamp: r for r in ordered}
            deduped = [seen[ts] for ts in sorted(seen)]
            self._by_symbol[symbol] = deduped
            self._timestamps[symbol] = [r.timestamp for r in deduped]

    @classmethod
    def from_binance_rows(cls, rows: Iterable[dict[str, Any]]) -> FundingSchedule:
        """Build from a raw ``/fapi/v1/fundingRate`` response."""
        return cls(parse_funding_rates(rows))

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_symbol))

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_symbol.values())

    def __bool__(self) -> bool:
        return len(self) > 0

    def for_symbol(self, symbol: str) -> Sequence[FundingRate]:
        return self._by_symbol.get(symbol, ())

    def settlements_in(self, after: datetime | None, upto: datetime) -> list[FundingRate]:
        """Settlements in the half-open interval ``(after, upto]``.

        Half-open is what makes a settlement landing exactly on a bar boundary
        belong to exactly one bar — never zero, never two. ``after=None`` means
        "everything up to and including ``upto``", used only on the first bar,
        where no position can exist yet.

        Results are ordered by timestamp across all symbols, so the caller
        applies them in the order they actually occurred.
        """
        found: list[FundingRate] = []
        for symbol, entries in self._by_symbol.items():
            stamps = self._timestamps[symbol]
            # bisect_right(after) skips anything at or before `after`;
            # bisect_right(upto) includes anything at exactly `upto`.
            lo = 0 if after is None else bisect.bisect_right(stamps, after)
            hi = bisect.bisect_right(stamps, upto)
            found.extend(entries[lo:hi])
        found.sort(key=lambda r: (r.timestamp, r.symbol))
        return found

    def interval_hours(self, symbol: str) -> Decimal | None:
        """Observed settlement interval, derived from the data.

        Never assumed: Binance moved selected perpetuals from 8-hourly to
        4-hourly in October 2023, so a multi-year series contains both regimes
        and any hardcoded value is wrong for part of it.
        """
        return observed_funding_interval_hours(list(self._by_symbol.get(symbol, ())))

    def describe(self) -> str:
        if not self:
            return "FundingSchedule(empty)"
        parts = []
        for symbol in self.symbols:
            entries = self._by_symbol[symbol]
            parts.append(
                f"{symbol}: {len(entries)} settlements "
                f"{entries[0].timestamp.date()} -> {entries[-1].timestamp.date()} "
                f"@ {self.interval_hours(symbol)}h"
            )
        return "FundingSchedule(" + "; ".join(parts) + ")"
