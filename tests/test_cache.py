"""Parquet cache tests.

The load-bearing property is exact ``Decimal`` round-tripping. A cache that
quietly routes prices through float would corrupt every backtest downstream of
it, permanently and invisibly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.core.events import Bar
from trading_platform.data.cache import (
    CacheMissError,
    cache_path,
    describe,
    merge_bars,
    read_bars,
    write_bars,
)

TS = datetime(2025, 1, 1, tzinfo=UTC)
SYMBOL = "BTCUSDT"


def bar(i: int, close: str = "42000.12345678") -> Bar:
    price = Decimal(close)
    return Bar(
        symbol=SYMBOL,
        timestamp=TS + timedelta(days=i),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1234.87654321"),
    )


def test_round_trip_preserves_decimals_exactly(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The whole reason this module exists."""
    original = [
        Bar(
            symbol=SYMBOL,
            timestamp=TS,
            open=Decimal("0.00000001"),  # smallest satoshi-scale tick
            high=Decimal("99999999.99999999"),  # 16 significant digits
            low=Decimal("0.00000001"),
            close=Decimal("42000.12345678"),
            volume=Decimal("0.000000010000"),  # trailing zeros are significant
        )
    ]
    path = tmp_path / "s.parquet"
    write_bars(path, original)
    restored = read_bars(path, SYMBOL)

    assert len(restored) == 1
    got, want = restored[0], original[0]
    assert got.open == want.open
    assert got.high == want.high
    assert got.close == want.close
    # String equality is the strict check — it would fail if anything passed
    # through float, even where numeric equality survived.
    assert str(got.volume) == str(want.volume) == "1.0000E-8"
    assert got.timestamp == want.timestamp


def test_float_would_have_lost_this() -> None:
    """Demonstrates the precision the string encoding protects.

    float64 carries ~15-17 significant digits. ERC-20 tokens use 18 decimals,
    and a large price with fine ticks exceeds the budget outright.
    """
    # 17 significant digits: the fractional part vanishes completely.
    assert float(Decimal("99999999.999999999")) == 100000000.0

    # 19 significant digits, ERC-20 scale: silently truncated.
    token_qty = Decimal("1.234567890123456789")
    assert Decimal(str(float(token_qty))) != token_qty

    # And float does not accumulate cleanly across many fills — which is the
    # failure mode that makes a run impossible to reconcile.
    #
    # Accumulated the way a ledger accumulates, one fill at a time. NOT with
    # builtin sum(): Python 3.12 gave it Neumaier compensated summation, so
    # sum([0.1] * 10) is exactly 1.0 there and this demonstrated the opposite
    # of the invariant on the interpreter the venv actually ran. A ledger adds
    # in a loop and gets no compensation. T-29b63f70.
    running = 0.0
    for _ in range(10):
        running += 0.1
    assert running != 1.0

    exact = Decimal("0")
    for _ in range(10):
        exact += Decimal("0.1")
    assert exact == Decimal("1.0")


def test_round_trip_preserves_timezone(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "s.parquet"
    write_bars(path, [bar(0)])
    restored = read_bars(path, SYMBOL)
    assert restored[0].timestamp.tzinfo is not None
    assert restored[0].timestamp == TS


def test_naive_timestamps_are_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    naive = Bar(
        symbol=SYMBOL,
        timestamp=datetime(2025, 1, 1),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
    )
    with pytest.raises(ValueError, match="naive timestamp"):
        write_bars(tmp_path / "s.parquet", [naive])


def test_windowed_read_is_inclusive(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "s.parquet"
    write_bars(path, [bar(i) for i in range(10)])

    windowed = read_bars(path, SYMBOL, start=TS + timedelta(days=2), end=TS + timedelta(days=5))
    assert len(windowed) == 4, "both endpoints included"
    assert windowed[0].timestamp == TS + timedelta(days=2)
    assert windowed[-1].timestamp == TS + timedelta(days=5)


def test_missing_cache_names_the_fix(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(CacheMissError, match="fetch_binance"):
        read_bars(tmp_path / "absent.parquet", SYMBOL)


# -- Refusals that protect the cache from poisoning -------------------------


def test_empty_series_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="empty series"):
        write_bars(tmp_path / "s.parquet", [])


def test_mixed_symbols_are_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    other = Bar(
        symbol="ETHUSDT",
        timestamp=TS + timedelta(days=1),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
    )
    with pytest.raises(ValueError, match="single-symbol"):
        write_bars(tmp_path / "s.parquet", [bar(0), other])


def test_duplicate_bars_are_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="Duplicate bar"):
        write_bars(tmp_path / "s.parquet", [bar(0), bar(0)])


def test_out_of_order_bars_are_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="out of order"):
        write_bars(tmp_path / "s.parquet", [bar(5), bar(0)])


def test_write_is_atomic_leaving_no_temp_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """An interrupted write must not leave a truncated file that reads as a hit."""
    path = tmp_path / "s.parquet"
    write_bars(path, [bar(i) for i in range(3)])
    assert path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_rewrite_replaces_cleanly(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "s.parquet"
    write_bars(path, [bar(i) for i in range(5)])
    write_bars(path, [bar(i) for i in range(2)])
    assert len(read_bars(path, SYMBOL)) == 2, "overwrite, not append"


# -- Merge ------------------------------------------------------------------


def test_merge_deduplicates_overlapping_pages() -> None:
    a = [bar(i) for i in range(5)]
    b = [bar(i) for i in range(3, 8)]  # overlaps on 3, 4
    merged = merge_bars(a, b)
    assert len(merged) == 8
    assert [m.timestamp for m in merged] == sorted(m.timestamp for m in merged)


def test_merge_lets_incoming_win_because_venues_revise_history() -> None:
    old = bar(0, close="100")
    revised = bar(0, close="101")
    merged = merge_bars([old], [revised])
    assert len(merged) == 1
    assert merged[0].close == Decimal("101")


def test_merge_handles_empty_sides() -> None:
    assert merge_bars([], []) == []
    assert len(merge_bars([], [bar(0)])) == 1
    assert len(merge_bars([bar(0)], [])) == 1


# -- Paths and description --------------------------------------------------


def test_cache_path_keys_on_market_because_symbols_overlap() -> None:
    spot = cache_path("/c", "spot", SYMBOL, "1d")
    futures = cache_path("/c", "um", SYMBOL, "1d")
    assert spot != futures, "same symbol trades on both at different prices"
    assert spot.as_posix().endswith("spot/BTCUSDT/1d.parquet")


def test_describe_summarises_coverage(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "s.parquet"
    write_bars(path, [bar(i) for i in range(10)])
    text = describe(path, SYMBOL)
    assert "10 bars" in text
    assert "2025-01-01" in text
    assert "2025-01-10" in text


def test_cache_survives_a_full_binance_parse_round_trip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Parser -> cache -> engine-ready bars, the real path."""
    import json
    from pathlib import Path as P

    from trading_platform.data.binance import klines_to_bars

    rows = json.loads(
        (P(__file__).parent / "fixtures" / "binance" / "klines_1d_us_boundary.json").read_text()
    )
    bars = klines_to_bars(rows, SYMBOL, "1d", now_ms=10**15)
    path = tmp_path / "s.parquet"
    write_bars(path, bars)
    restored = read_bars(path, SYMBOL)

    assert [b.timestamp for b in restored] == [b.timestamp for b in bars]
    assert [b.close for b in restored] == [b.close for b in bars]
