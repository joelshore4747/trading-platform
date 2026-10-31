"""Data feeds.

A feed yields ``Bar`` events in non-decreasing timestamp order. That ordering
guarantee is what the engine relies on to prevent lookahead — feeds validate it
rather than trusting the source, because silently out-of-order data produces a
backtest that looks excellent and is entirely fictional.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol, runtime_checkable

from trading_platform.core.events import Bar


@runtime_checkable
class DataFeed(Protocol):
    """Anything that can produce a time-ordered stream of bars."""

    def __iter__(self) -> Iterator[Bar]: ...

    @property
    def symbols(self) -> tuple[str, ...]: ...


class OutOfOrderDataError(ValueError):
    """Raised when a feed emits a bar earlier than one it already emitted."""


def _validate_ordering(bars: Iterable[Bar]) -> Iterator[Bar]:
    """Pass bars through, failing loudly on any backwards timestamp step."""
    last: datetime | None = None
    for bar in bars:
        if last is not None and bar.timestamp < last:
            raise OutOfOrderDataError(
                f"{bar.symbol}: bar at {bar.timestamp} follows {last}. "
                "Sort the source data by timestamp before feeding it to the engine."
            )
        last = bar.timestamp
        yield bar


class CSVBarFeed:
    """Reads bars from a CSV with columns:
    ``timestamp,symbol,open,high,low,close,volume``.

    Timestamps must be ISO-8601. Naive timestamps are assumed UTC and made
    explicit, because mixing naive and aware datetimes in comparisons raises at
    the worst possible moment — mid-backtest, several minutes in.
    """

    REQUIRED_COLUMNS = frozenset({"timestamp", "symbol", "open", "high", "low", "close", "volume"})

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"No such data file: {self.path}")
        self._symbols = self._scan_symbols()

    def _scan_symbols(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        with self.path.open(newline="") as fh:
            reader = csv.DictReader(fh)
            missing = self.REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"{self.path} is missing required columns: {sorted(missing)}")
            for row in reader:
                seen.setdefault(row["symbol"], None)
        return tuple(seen)

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def __iter__(self) -> Iterator[Bar]:
        def rows() -> Iterator[Bar]:
            with self.path.open(newline="") as fh:
                for row in csv.DictReader(fh):
                    yield Bar(
                        symbol=row["symbol"],
                        timestamp=_parse_timestamp(row["timestamp"]),
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        volume=Decimal(row["volume"]),
                    )

        return _validate_ordering(rows())


class InMemoryBarFeed:
    """Wraps an in-memory bar sequence. Primary use is tests and fixtures."""

    def __init__(self, bars: Iterable[Bar]) -> None:
        self._bars = sorted(bars, key=lambda b: (b.timestamp, b.symbol))
        self._symbols = tuple(dict.fromkeys(b.symbol for b in self._bars))

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def __iter__(self) -> Iterator[Bar]:
        return _validate_ordering(self._bars)


def _parse_timestamp(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def synthetic_walk(
    symbol: str,
    start: datetime,
    periods: int,
    start_price: Decimal = Decimal("100"),
    step: timedelta = timedelta(days=1),
    drift: Decimal = Decimal("0.001"),
    amplitude: Decimal = Decimal("0.02"),
) -> InMemoryBarFeed:
    """Deterministic synthetic price series for tests and smoke runs.

    Uses a fixed sinusoid plus drift rather than randomness so that a test
    asserting on backtest output stays stable across runs and machines.
    """
    import math

    bars: list[Bar] = []
    price = start_price
    for i in range(periods):
        wave = Decimal(str(math.sin(i / 6.0))) * amplitude
        price = price * (Decimal("1") + drift + wave)
        price = price.quantize(Decimal("0.01"))
        high = (price * Decimal("1.005")).quantize(Decimal("0.01"))
        low = (price * Decimal("0.995")).quantize(Decimal("0.01"))
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=start + step * i,
                open=price,
                high=high,
                low=low,
                close=price,
                volume=Decimal("1000"),
            )
        )
    return InMemoryBarFeed(bars)
