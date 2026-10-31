"""Reading the cache back — which nothing in `src/` could do before this.

`data/cache.py` had one writer and no readers anywhere in the package. Every
executable path in the repository was fed by the synthetic generator, so no
backtest had ever run on real stored data. These tests pin the reader, the
single path layout, and the ordering the engine's lookahead guard depends on.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform import Backtester, RiskLimits, RiskManager, crypto_perpetual
from trading_platform.core.events import Bar
from trading_platform.data.cache import (
    LAYOUT_VERSION,
    CachedBarFeed,
    CacheLayoutError,
    CacheMissError,
    cache_path,
    ensure_layout,
    funding_path,
    read_bars,
    write_bars,
)
from trading_platform.data.intervals import UnknownIntervalError
from trading_platform.data.universe import Listing, UniverseRegistry, UnknownListing
from trading_platform.strategy.examples.sma_crossover import SMACrossover

START = datetime(2026, 1, 1, tzinfo=UTC)


def series(symbol: str, count: int, base: int = 100) -> list[Bar]:
    out = []
    for i in range(count):
        price = Decimal(base + (i % 7))
        out.append(
            Bar(
                symbol=symbol,
                timestamp=START + timedelta(minutes=i),
                open=price,
                high=price * Decimal("1.01"),
                low=price * Decimal("0.99"),
                close=price,
                volume=Decimal("10"),
            )
        )
    return out


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    ensure_layout(tmp_path)
    for symbol, base in (("AAA", 100), ("MMM", 200), ("ZZZ", 300)):
        write_bars(cache_path(tmp_path, "um", symbol, "1m"), series(symbol, 60, base))
    return tmp_path


# -- One layout ---------------------------------------------------------------


def test_cache_path_rejects_a_market_outside_the_vocabulary(tmp_path: Path) -> None:
    """A typo would file a series where nothing looks for it, and read back as
    a cache miss that re-downloads a year of history."""
    with pytest.raises(CacheLayoutError, match="Unknown market"):
        cache_path(tmp_path, "spto", "BTCUSDT", "1d")


def test_cache_path_rejects_an_unspellable_interval(tmp_path: Path) -> None:
    with pytest.raises(UnknownIntervalError):
        cache_path(tmp_path, "spot", "BTCUSDT", "7m")


def test_funding_lives_beside_the_bars_rather_than_in_a_hand_built_sibling(
    tmp_path: Path,
) -> None:
    assert (
        funding_path(tmp_path, "BTCUSDT").parent
        == cache_path(tmp_path, "um", "BTCUSDT", "1d").parent
    )


def test_the_layout_marker_refuses_a_cache_of_another_version(tmp_path: Path) -> None:
    ensure_layout(tmp_path)
    marker = tmp_path / "_layout.json"
    assert json.loads(marker.read_text())["layout_version"] == LAYOUT_VERSION
    marker.write_text(json.dumps({"layout_version": LAYOUT_VERSION + 1}))
    with pytest.raises(CacheLayoutError, match="written by cache layout"):
        ensure_layout(tmp_path)


def test_nothing_outside_the_cache_module_builds_a_cache_path() -> None:
    """The fork this closed: three components each assembled their own layout,
    so what one wrote another could not find. Guarded rather than remembered."""
    root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in list((root / "src").rglob("*.py")) + list((root / "scripts").rglob("*.py")):
        if path.name in ("cache.py",):
            continue
        text = path.read_text()
        for marker in ('/ f"{interval}.parquet"', '/ "funding.json"', '.parquet"'):
            if marker in text and "cache_path(" not in text and "funding_path(" not in text:
                offenders.append(f"{path.relative_to(root)}: {marker}")
    assert not offenders, (
        "these build a cache path by hand instead of calling cache_path() or "
        f"funding_path(): {offenders}"
    )


# -- The reader ---------------------------------------------------------------


def test_symbols_are_known_before_a_single_bar_is_read(cache: Path) -> None:
    """`Backtester.run` validates the feed's symbols against its instruments
    before consuming anything, so a feed that only learns them by streaming
    cannot be used."""
    feed = CachedBarFeed(cache, "um", ["ZZZ", "AAA"], "1m")
    assert feed.symbols == ("AAA", "ZZZ")


def test_a_missing_series_fails_up_front_and_names_every_one(cache: Path) -> None:
    with pytest.raises(CacheMissError) as excinfo:
        CachedBarFeed(cache, "um", ["AAA", "NOPE", "ALSONOPE"], "1m")
    message = str(excinfo.value)
    assert "NOPE" in message and "ALSONOPE" in message


def test_bars_arrive_in_timestamp_then_symbol_order(cache: Path) -> None:
    """The order the engine's cross-symbol lookahead guard depends on."""
    feed = CachedBarFeed(cache, "um", ["ZZZ", "AAA", "MMM"], "1m")
    keys = [(bar.timestamp, bar.symbol) for bar in feed]
    assert keys == sorted(keys)
    assert len(keys) == 180
    assert [k[1] for k in keys[:3]] == ["AAA", "MMM", "ZZZ"]


def test_the_feed_is_re_iterable(cache: Path) -> None:
    feed = CachedBarFeed(cache, "um", ["AAA"], "1m")
    assert [b.close for b in feed] == [b.close for b in feed]


def test_a_window_narrows_what_the_feed_emits(cache: Path) -> None:
    feed = CachedBarFeed(
        cache,
        "um",
        ["AAA"],
        "1m",
        start=START + timedelta(minutes=10),
        end=START + timedelta(minutes=19),
    )
    stamps = [b.timestamp for b in feed]
    assert len(stamps) == 10
    assert stamps[0] == START + timedelta(minutes=10)
    assert stamps[-1] == START + timedelta(minutes=19)


def test_a_windowed_read_matches_filtering_the_full_read(cache: Path) -> None:
    """The bisection that made windowed reads cheap must not change what they
    return — an off-by-one here silently drops a bar from every backtest."""
    path = cache_path(cache, "um", "AAA", "1m")
    everything = read_bars(path, "AAA")
    for lo, hi in ((0, 0), (0, 59), (5, 5), (7, 23), (58, 200), (200, 300)):
        start, end = START + timedelta(minutes=lo), START + timedelta(minutes=hi)
        assert read_bars(path, "AAA", start=start, end=end) == [
            b for b in everything if start <= b.timestamp <= end
        ]


# -- End to end ---------------------------------------------------------------


def test_a_backtest_runs_on_cached_data(cache: Path) -> None:
    """The thing that had never happened: real stored data through `src/` code
    into the engine, with no synthetic generator anywhere in the path."""
    symbols = ["AAA", "MMM", "ZZZ"]
    result = Backtester(
        instruments={s: crypto_perpetual(s) for s in symbols},
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=RiskLimits()),
    ).run(SMACrossover("AAA", fast=3, slow=8), CachedBarFeed(cache, "um", symbols, "1m"))

    assert result.bars_processed == 180
    assert result.quote_currencies == ("USDT",)
    assert result.start == START
    assert result.end == START + timedelta(minutes=59)


# -- Point-in-time membership -------------------------------------------------


def universe(root: Path, **asserted: date | None) -> UniverseRegistry:
    return UniverseRegistry(
        listings=tuple(
            Listing(
                market="um",
                symbol=symbol,
                interval="1m",
                first_seen=START,
                last_seen=START + timedelta(minutes=59),
                bars=60,
                listed=asserted.get(symbol),
            )
            for symbol in ("AAA", "MMM", "ZZZ")
        )
    )


def test_from_registry_refuses_a_universe_bounded_by_the_archive(cache: Path) -> None:
    """Every lifetime here starts and ends at the archive's own edges, so the
    registry cannot say who was listed and does not pretend to."""
    with pytest.raises(UnknownListing, match="bounded by the archive"):
        CachedBarFeed.from_registry(universe(cache), cache, "um", "1m")


def test_from_registry_drops_bars_from_before_a_symbol_listed(cache: Path) -> None:
    """The property that distinguishes this from resolving a symbol list.

    The cache holds ZZZ's full history. The registry says ZZZ listed later, so
    the feed must not emit the earlier bars — otherwise a backtest trades a
    symbol on days it did not exist, and `from_registry` is `list(members)`
    with extra steps.
    """
    listed_late = universe(
        cache,
        AAA=date(2025, 12, 31),
        MMM=date(2025, 12, 31),
        ZZZ=date(2026, 1, 1),  # first tradeable at the close of 2026-01-01
    )
    plain = list(CachedBarFeed(cache, "um", ["AAA", "MMM", "ZZZ"], "1m"))
    filtered = list(CachedBarFeed.from_registry(listed_late, cache, "um", "1m"))

    assert len(plain) == 180
    assert [b for b in filtered if b.symbol == "ZZZ"] == []
    assert len([b for b in filtered if b.symbol == "AAA"]) == 60
    assert len(filtered) == 120, "ZZZ's 60 bars all precede its listing instant"


def test_from_registry_leaves_a_fully_listed_universe_untouched(cache: Path) -> None:
    everyone = universe(
        cache,
        AAA=date(2025, 12, 31),
        MMM=date(2025, 12, 31),
        ZZZ=date(2025, 12, 31),
    )
    filtered = list(CachedBarFeed.from_registry(everyone, cache, "um", "1m"))
    assert len(filtered) == 180
    keys = [(b.timestamp, b.symbol) for b in filtered]
    assert keys == sorted(keys), "membership filtering must not disturb feed order"


# -- Instrument identity ------------------------------------------------------


def test_an_instrument_filed_under_the_wrong_key_raises_before_any_bar(
    cache: Path,
) -> None:
    """A silent order-of-magnitude error, not a naming inconvenience.

    `Portfolio.position()` binds by `instrument.symbol` while everything else
    keys by the dict key, and `_refresh` branches on the bound instrument's
    `is_derivative`. A Spot registered under a perpetual's key therefore marks
    full notional where unrealised PnL belongs: a 10-unit position opened at
    100 and marked at 110 contributes 1,100 instead of 100.
    """
    with pytest.raises(KeyError, match="not their symbol"):
        Backtester(
            instruments={"AAA": crypto_perpetual("MMM")},
            starting_cash=Decimal("100000"),
            risk_manager=RiskManager(limits=RiskLimits()),
        ).run(SMACrossover("AAA", fast=3, slow=8), CachedBarFeed(cache, "um", ["AAA"], "1m"))
