"""Local Parquet cache for bar series.

Market data is fetched once and read many times. Re-downloading years of
history on every backtest is slow, rude to the venue, and — because exchanges
occasionally revise history — not even reproducible.

**Prices are stored as strings, not floats.** Parquet has a native decimal type,
but it requires a fixed precision and scale declared up front, and crypto
venues use wildly different scales (BTC prices to 2dp, SHIB quantities to 12).
Strings round-trip any scale exactly and cost only storage. Writing floats here
would defeat the ``Decimal`` house rule at the single point where it is easiest
to lose and hardest to notice.
"""

from __future__ import annotations

import bisect
import heapq
import itertools
import json
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from trading_platform.core.events import Bar
from trading_platform.data.feed import _validate_ordering
from trading_platform.data.intervals import interval_ms
from trading_platform.data.universe import UniverseRegistry

#: Column order in the stored file. Timestamps are int64 epoch milliseconds —
#: unambiguous across timezones and readable by any Parquet consumer.
_SCHEMA = pa.schema(
    [
        pa.field("timestamp_ms", pa.int64(), nullable=False),
        pa.field("open", pa.string(), nullable=False),
        pa.field("high", pa.string(), nullable=False),
        pa.field("low", pa.string(), nullable=False),
        pa.field("close", pa.string(), nullable=False),
        pa.field("volume", pa.string(), nullable=False),
    ]
)


class CacheMissError(FileNotFoundError):
    """Raised when a requested series is not cached."""


class CacheLayoutError(ValueError):
    """A path component outside the vocabulary, or a cache of another layout."""


#: Version of the on-disk layout, written to ``<root>/_layout.json``. A future
#: reorganisation bumps this so a stale cache is a loud error rather than a
#: silent cache miss that re-downloads a year of history.
LAYOUT_VERSION = 1

#: The first path component. Not free-form: the same symbol trades on several
#: markets at different prices, and a typo would read back as a cache miss
#: forever. Crypto uses Binance's own vocabulary so a hand-placed file from
#: their archive is findable; the rest are ours.
MARKETS: frozenset[str] = frozenset(
    {
        "spot",  # crypto spot
        "um",  # USD-margined perpetuals and futures
        "cm",  # coin-margined
        "equity",
        "etf",
        "option",
    }
)


def cache_path(root: Path | str, market: str, symbol: str, interval: str) -> Path:
    """Location of one series: ``<root>/<market>/<symbol>/<interval>.parquet``.

    **The only sanctioned way to build a cache path.** Two other places used to
    construct their own — one dropping market and interval entirely and
    lowercasing the symbol, one writing funding JSON to a hand-built sibling —
    so the cache had three layouts, and what one component wrote another could
    not find. A test now asserts nothing outside this module builds one.

    ``interval`` is validated against the shared vocabulary, so a coined name
    like ``"7m"`` fails here rather than becoming a permanent cache miss.
    """
    if market not in MARKETS:
        raise CacheLayoutError(f"Unknown market {market!r}. Valid: {sorted(MARKETS)}.")
    if not symbol or symbol != symbol.strip():
        raise CacheLayoutError(f"Symbol {symbol!r} is empty or padded")
    interval_ms(interval)  # raises UnknownIntervalError on an unspellable name
    return Path(root) / market / symbol / f"{interval}.parquet"


def funding_path(root: Path | str, symbol: str) -> Path:
    """Location of one symbol's funding series.

    Beside the bar series rather than in a hand-built sibling. It used to be
    assembled inline as ``<root>/um/<symbol>/funding.json``, which happened to
    match the layout but would not have followed it through any change.

    Funding is JSON, not Parquet: a few hundred settlements a year, and the
    raw venue rows are worth keeping verbatim.
    """
    if not symbol or symbol != symbol.strip():
        raise CacheLayoutError(f"Symbol {symbol!r} is empty or padded")
    return Path(root) / "um" / symbol / "funding.json"


def universe_path(root: Path | str) -> Path:
    """Location of the universe registry for a cache root.

    Beside `_layout.json` rather than under a market, because a registry spans
    markets: the same run may hold `um/BTCUSDT` and `equity/AAPL`, and their
    memberships are one question, not two.
    """
    return Path(root) / "_universe.json"


def ensure_layout(root: Path | str) -> Path:
    """Create the cache root and stamp it with the layout version.

    Returns the marker path. Refuses a root written by a different layout
    version rather than reading it as though the shape were unchanged.
    """
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    marker = base / "_layout.json"
    if marker.exists():
        found = json.loads(marker.read_text()).get("layout_version")
        if found != LAYOUT_VERSION:
            raise CacheLayoutError(
                f"{base} was written by cache layout {found}, this build expects "
                f"{LAYOUT_VERSION}. Re-import rather than mixing layouts."
            )
        return marker
    marker.write_text(json.dumps({"layout_version": LAYOUT_VERSION}, indent=2) + "\n")
    return marker


def write_bars(path: Path | str, bars: Sequence[Bar]) -> Path:
    """Write a bar series, overwriting any existing file.

    Bars must be non-empty, single-symbol, and strictly increasing in time.
    Those are checked here rather than trusted: a cache silently holding
    duplicated or out-of-order bars poisons every backtest that reads it, and
    the corruption long outlives the session that caused it.
    """
    if not bars:
        raise ValueError("Refusing to write an empty series")

    symbols = {b.symbol for b in bars}
    if len(symbols) > 1:
        raise ValueError(f"Series must be single-symbol, got {sorted(symbols)}")

    timestamps = [_to_ms(b.timestamp) for b in bars]
    for earlier, later in itertools.pairwise(timestamps):
        if later == earlier:
            raise ValueError(f"Duplicate bar at {_from_ms(earlier).isoformat()}")
        if later < earlier:
            raise ValueError(
                f"Bars out of order: {_from_ms(later).isoformat()} follows "
                f"{_from_ms(earlier).isoformat()}"
            )

    table = pa.table(
        {
            "timestamp_ms": timestamps,
            "open": [str(b.open) for b in bars],
            "high": [str(b.high) for b in bars],
            "low": [str(b.low) for b in bars],
            "close": [str(b.close) for b in bars],
            "volume": [str(b.volume) for b in bars],
        },
        schema=_SCHEMA,
    )

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Write to a sibling temp file and rename. A run interrupted mid-write
    # would otherwise leave a truncated Parquet file that reads as a cache hit.
    staging = target.with_suffix(".parquet.tmp")
    pq.write_table(table, staging, compression="zstd")
    staging.replace(target)
    return target


def read_bars(
    path: Path | str,
    symbol: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Bar]:
    """Read a cached series, optionally windowed to ``[start, end]`` inclusive.

    ``symbol`` is supplied by the caller because it is part of the path rather
    than the file — storing it per row would repeat one string a million times.
    """
    source = Path(path)
    if not source.exists():
        raise CacheMissError(
            f"No cached series at {source}. Fetch it first with scripts/fetch_binance.py."
        )

    table = pq.read_table(source, schema=_SCHEMA)
    stamps = table["timestamp_ms"].to_pylist()

    # Find the window's bounds ONCE by bisection, rather than comparing every
    # row's timestamp inside the construction loop. That per-row comparison was
    # part of the ~51% of this function's cost that went to its own loop rather
    # than to Parquet (4% of it) or to `Bar` (its construction and validation
    # together are ~26%). A one-day window of a 32-day file used to cost 18% of
    # a full read to return 5% of the data; now it decodes only the slice.
    lo = 0 if start is None else bisect.bisect_left(stamps, _to_ms(start))
    hi = len(stamps) if end is None else bisect.bisect_right(stamps, _to_ms(end))
    if lo >= hi:
        return []

    window = table.slice(lo, hi - lo)

    # Bound to locals. `_from_ms` was a Python call per row (0.52 us/bar of the
    # 5.28 this function cost), and global lookups for `Decimal` and `Bar`
    # repeat a dict miss a million times over a year of minute data. The
    # remaining cost is `Bar` itself — 3.29 us/bar to build and validate one —
    # which is the house rule working as intended and is not being traded away
    # for the 6% its validation accounts for.
    from_timestamp = datetime.fromtimestamp
    decimal = Decimal
    bar = Bar
    utc = UTC

    return [
        bar(
            symbol=symbol,
            timestamp=from_timestamp(ts_ms / 1000, utc),
            open=decimal(o),
            high=decimal(h),
            low=decimal(low),
            close=decimal(c),
            volume=decimal(v),
        )
        for ts_ms, o, h, low, c, v in zip(
            window["timestamp_ms"].to_pylist(),
            window["open"].to_pylist(),
            window["high"].to_pylist(),
            window["low"].to_pylist(),
            window["close"].to_pylist(),
            window["volume"].to_pylist(),
            strict=True,
        )
    ]


def merge_bars(existing: Iterable[Bar], incoming: Iterable[Bar]) -> list[Bar]:
    """Combine two series, newer data winning on conflict.

    Paging windows overlap at their boundaries, so the same bar routinely
    arrives twice. Binance also revises history occasionally, which is why
    ``incoming`` wins rather than being discarded as a duplicate.
    """
    merged: dict[datetime, Bar] = {b.timestamp: b for b in existing}
    for bar in incoming:
        merged[bar.timestamp] = bar
    return [merged[ts] for ts in sorted(merged)]


def describe(path: Path | str, symbol: str) -> str:
    """One-line summary of a cached series, for `ls`-style inspection."""
    bars = read_bars(path, symbol)
    if not bars:
        return f"{symbol}: empty"
    return f"{symbol}: {len(bars)} bars, {bars[0].timestamp.date()} -> {bars[-1].timestamp.date()}"


def _to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError(f"Refusing to store a naive timestamp: {dt!r}")
    return int(dt.timestamp() * 1000)


def _from_ms(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)


class CachedBarFeed:
    """A ``DataFeed`` over cached Parquet series. Multi-symbol, feed-ordered.

    The first thing in this repository that lets a backtest run on real stored
    data. Until now ``data/cache.py`` had no reader anywhere in ``src/``: one
    script wrote it, nothing read it back, and every executable path — the
    examples, the UI, the walk-forward harness — was fed by the synthetic
    generator. A cache with no readers is not a data plane.

    Ordering is ``(timestamp, symbol)``, which is what the engine's
    cross-symbol lookahead guard depends on: ``engine/execution.py`` compares
    each order's creation instant against the bar it would fill on, and
    ``engine/backtester.py`` closes out an instant only when the timestamp
    advances. Merged with ``heapq.merge`` rather than by concatenating and
    sorting, so a symbol arriving out of order surfaces through
    ``_validate_ordering`` instead of being quietly repaired.

    ``symbols`` is known before iteration begins, which ``Backtester.run``
    requires — it validates the feed's symbols against its instruments before
    consuming a single bar.
    """

    __slots__ = (
        "_end",
        "_interval",
        "_market",
        "_registry",
        "_root",
        "_start",
        "_symbols",
    )

    def __init__(
        self,
        root: Path | str,
        market: str,
        symbols: Sequence[str],
        interval: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        if not symbols:
            raise ValueError("CachedBarFeed needs at least one symbol")
        self._root = Path(root)
        self._market = market
        self._symbols = tuple(sorted(set(symbols)))
        self._interval = interval
        self._start = start
        self._end = end
        #: Set only by `from_registry`. When present, bars outside a symbol's
        #: membership are dropped rather than emitted.
        self._registry: UniverseRegistry | None = None
        # Fail now, not part-way through a backtest, and name every missing
        # series rather than the first one.
        missing = [
            symbol
            for symbol in self._symbols
            if not cache_path(self._root, market, symbol, interval).exists()
        ]
        if missing:
            raise CacheMissError(
                f"No cached {interval} series under {self._root / market} for: "
                f"{missing}. Import them first with scripts/import_vendor.py."
            )

    @classmethod
    def from_registry(
        cls,
        registry: UniverseRegistry,
        root: Path | str,
        market: str,
        interval: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> CachedBarFeed:
        """A feed over whichever symbols were members during the window.

        Two things distinguish this from resolving a symbol list and passing
        it to ``__init__``, and the second is the one that matters:

        1. It refuses when the registry cannot answer — a censored lifetime
           gives ``UnknownListing`` rather than a plausible universe.
        2. **It emits only bars falling inside each symbol's membership.** A
           symbol that listed mid-window contributes nothing before its
           listing instant. Without that this would be ``list(members)`` with
           extra steps, and a backtest would trade a symbol on days it did
           not exist.
        """
        window_start = start or registry.archive_start
        window_end = end or registry.archive_end
        if window_start is None or window_end is None:
            raise ValueError("An empty registry has no window to resolve")

        symbols = registry.members_between(window_start, window_end)
        if not symbols:
            raise ValueError(
                f"No symbol was a member between {window_start.isoformat()} and "
                f"{window_end.isoformat()}"
            )
        feed = cls(root, market, symbols, interval, start=start, end=end)
        feed._registry = registry
        return feed

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    @property
    def interval(self) -> str:
        return self._interval

    def __iter__(self) -> Iterator[Bar]:
        streams = [
            read_bars(
                cache_path(self._root, self._market, symbol, self._interval),
                symbol,
                start=self._start,
                end=self._end,
            )
            for symbol in self._symbols
        ]
        merged = heapq.merge(*streams, key=lambda bar: (bar.timestamp, bar.symbol))
        if self._registry is not None:
            registry = self._registry
            merged = (
                bar for bar in merged if registry.is_member(bar.symbol, bar.timestamp) is not None
            )
        return _validate_ordering(merged)

    def __repr__(self) -> str:
        return f"CachedBarFeed({self._market}/{self._interval}, {len(self._symbols)} symbols)"
