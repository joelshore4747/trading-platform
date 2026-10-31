"""The columnar cross-section.

Two properties matter here and the rest is detail: the panel must agree
exactly with the object path it derives from, and it must never fill a hole.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.core.events import Bar
from trading_platform.data.cache import cache_path, ensure_layout, read_bars, write_bars
from trading_platform.data.panel import Panel, PanelError

START = datetime(2026, 1, 1, tzinfo=UTC)


def bar(symbol: str, day: int, close: str, volume: str = "10") -> Bar:
    price = Decimal(close)
    return Bar(
        symbol=symbol,
        timestamp=START + timedelta(days=day),
        open=price,
        high=price * Decimal("1.01"),
        low=price * Decimal("0.99"),
        close=price,
        volume=Decimal(volume),
    )


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    """Three symbols, one of which is missing a day the others have."""
    ensure_layout(tmp_path)
    series = {
        "AAA": [bar("AAA", d, str(100 + d)) for d in range(5)],
        "BBB": [bar("BBB", d, str(50 + d * 2)) for d in range(5)],
        "CCC": [bar("CCC", d, str(10 + d)) for d in range(5) if d != 2],
    }
    for symbol, bars in series.items():
        write_bars(cache_path(tmp_path, "equity", symbol, "1d"), bars)
    return tmp_path


@pytest.mark.parametrize(
    ("start_day", "end_day"),
    [(None, None), (0, 4), (1, 4), (2, 3), (3, None), (None, 2)],
)
def test_a_panel_agrees_exactly_with_the_object_path(
    cache: Path, start_day: int | None, end_day: int | None
) -> None:
    """Two representations of one file can disagree, and a disagreement here
    would be invisible: the panel drives ranking, the objects drive fills.

    **Parametrised over windows deliberately.** This test originally loaded the
    whole file and passed, while a separate test checked that a window narrowed
    the *grid* — and neither checked a windowed panel's *values*. A lookahead
    audit found that every windowed panel was reading prices from the start of
    the file: the row offset was re-derived from an already-sliced array, so it
    was always zero, and `take` applied window-relative indices to the full
    column. Nothing raised, because the indices stayed in range. With
    `start=day 5` on a ten-day file the panel returned days 0-4's prices under
    days 5-9's timestamps.
    """
    panel = Panel.load(
        cache,
        "equity",
        ["AAA", "BBB"],
        "1d",
        start=None if start_day is None else START + timedelta(days=start_day),
        end=None if end_day is None else START + timedelta(days=end_day),
    )
    for symbol in ("AAA", "BBB"):
        bars = read_bars(cache_path(cache, "equity", symbol, "1d"), symbol)
        by_timestamp = {b.timestamp: b.close for b in bars}
        from_panel = panel.series("close", symbol).to_pylist()
        for stamp, value in zip(panel.datetimes(), from_panel, strict=True):
            assert value == float(by_timestamp[stamp]), (
                f"{symbol} at {stamp}: panel says {value}, file says {by_timestamp[stamp]}"
            )


def test_windowed_panels_agree_when_files_start_at_different_dates() -> None:
    """The case the offset bug corrupted worst: ragged file starts give each
    symbol a different row offset, so a cross-section ranked one symbol's
    price against another's from a different day entirely."""
    import tempfile

    root = Path(tempfile.mkdtemp())
    ensure_layout(root)
    starts = {"AAA": 0, "BBB": 3, "CCC": 6}
    for symbol, first in starts.items():
        write_bars(
            cache_path(root, "equity", symbol, "1d"),
            [bar(symbol, d, str(1000 + d)) for d in range(first, first + 12)],
        )

    panel = Panel.load(root, "equity", list(starts), "1d", start=START + timedelta(days=8))
    # Close encodes the day, so every cell in a row must be the same number.
    for index, stamp in enumerate(panel.datetimes()):
        expected = float(1000 + (stamp - START).days)
        assert panel.row("close", index).to_pylist() == [expected] * 3


def test_the_grid_is_an_intersection_and_the_hole_is_not_filled(cache: Path) -> None:
    """CCC is missing day 2. The panel loses that timestamp for everyone
    rather than carrying CCC's day-1 price forward into it — a forward-filled
    price is one nobody could have traded."""
    panel = Panel.load(cache, "equity", ["AAA", "BBB", "CCC"], "1d")

    days = [t.day for t in panel.datetimes()]
    assert days == [1, 2, 4, 5], "day 3 (index 2) must be dropped from every symbol"
    assert panel.shape == (4, 3)
    assert panel.retained == pytest.approx(12 / 14)

    # And the surviving CCC values are its own, not interpolated.
    assert panel.series("close", "CCC").to_pylist() == [10.0, 11.0, 13.0, 14.0]


def test_only_the_requested_fields_are_carried(cache: Path) -> None:
    panel = Panel.load(cache, "equity", ["AAA"], "1d", fields=("close", "volume"))
    assert panel.fields == ("close", "volume")
    with pytest.raises(PanelError, match="does not carry"):
        panel.field("open")


def test_a_window_narrows_the_grid(cache: Path) -> None:
    panel = Panel.load(
        cache,
        "equity",
        ["AAA", "BBB"],
        "1d",
        start=START + timedelta(days=1),
        end=START + timedelta(days=3),
    )
    assert [t.day for t in panel.datetimes()] == [2, 3, 4]


def test_symbols_sharing_no_timestamp_raise(tmp_path: Path) -> None:
    ensure_layout(tmp_path)
    write_bars(cache_path(tmp_path, "equity", "AAA", "1d"), [bar("AAA", 0, "100")])
    write_bars(cache_path(tmp_path, "equity", "ZZZ", "1d"), [bar("ZZZ", 9, "100")])
    with pytest.raises(PanelError, match="share no timestamp"):
        Panel.load(tmp_path, "equity", ["AAA", "ZZZ"], "1d")


def test_unknown_field_raises_before_any_file_is_opened(cache: Path) -> None:
    with pytest.raises(PanelError, match="Unknown field"):
        Panel.load(cache, "equity", ["AAA"], "1d", fields=("vwap",))


# -- Cross-sectional operations ----------------------------------------------


def test_rank_and_zscore_operate_across_symbols_at_one_instant(cache: Path) -> None:
    panel = Panel.load(cache, "equity", ["AAA", "BBB", "CCC"], "1d")
    # Row 0: AAA=100, BBB=50, CCC=10.
    assert panel.row("close", 0).to_pylist() == [100.0, 50.0, 10.0]
    assert panel.rank_row("close", 0).to_pylist() == [1, 2, 3]
    assert panel.rank_row("close", 0, descending=False).to_pylist() == [3, 2, 1]

    scores = panel.zscore_row("close", 0).to_pylist()
    assert scores[0] > 0 > scores[2]
    assert sum(scores) == pytest.approx(0.0, abs=1e-12)


def test_a_flat_cross_section_scores_zero_rather_than_infinity(tmp_path: Path) -> None:
    """Zero dispersion divides by zero. An infinity here would read downstream
    as an enormously strong signal rather than as no signal at all."""
    ensure_layout(tmp_path)
    for symbol in ("AAA", "BBB", "CCC"):
        write_bars(
            cache_path(tmp_path, "equity", symbol, "1d"),
            [bar(symbol, d, "100") for d in range(3)],
        )
    panel = Panel.load(tmp_path, "equity", ["AAA", "BBB", "CCC"], "1d")
    assert panel.zscore_row("close", 0).to_pylist() == [0.0, 0.0, 0.0]


def test_a_row_outside_the_grid_raises(cache: Path) -> None:
    panel = Panel.load(cache, "equity", ["AAA"], "1d")
    with pytest.raises(PanelError, match="outside"):
        panel.row("close", 99)


def test_an_unknown_symbol_raises_rather_than_returning_nulls(cache: Path) -> None:
    panel = Panel.load(cache, "equity", ["AAA"], "1d")
    with pytest.raises(PanelError, match="not in this panel"):
        panel.series("close", "BBB")


# -- What the rectangle hides ------------------------------------------------


def test_a_gap_in_the_grid_is_reported_rather_than_smoothed(tmp_path: Path) -> None:
    """An intersection creates holes present in neither input, and adjacent
    rows either side of one are weeks apart. A cross-sectional return computed
    as row(i) - row(i-1) across it is a multi-week move labelled as one bar.

    On the real archive this fires: 18 gaps, the largest 8 days 13 hours.
    """
    ensure_layout(tmp_path)
    write_bars(
        cache_path(tmp_path, "equity", "AAA", "1d"),
        [bar("AAA", d, str(100 + d)) for d in range(40)],
    )
    write_bars(
        cache_path(tmp_path, "equity", "BBB", "1d"),
        [bar("BBB", d, str(200 + d)) for d in list(range(10)) + list(range(30, 40))],
    )
    panel = Panel.load(tmp_path, "equity", ["AAA", "BBB"], "1d")

    assert not panel.is_contiguous
    assert panel.gaps == ((10, timedelta(days=21)),)
    assert panel.largest_gap == timedelta(days=21)
    assert "gaps=1" in repr(panel)


def test_a_contiguous_panel_reports_no_gaps(cache: Path) -> None:
    panel = Panel.load(cache, "equity", ["AAA", "BBB"], "1d")
    assert panel.is_contiguous
    assert panel.gaps == ()
    assert panel.largest_gap == timedelta(days=1)
    assert "gaps=" not in repr(panel)


def test_a_symbol_that_dies_mid_sample_is_named_as_survivorship(tmp_path: Path) -> None:
    """The intersection guarantees every symbol prints a price at every row.
    That guarantee is itself a future observation: a symbol dying at the
    midpoint deletes rows from *before* it died, for everyone. Not repaired —
    an intersection is still more honest than a forward fill — but said.
    """
    ensure_layout(tmp_path)
    for symbol in ("AAA", "BBB"):
        write_bars(
            cache_path(tmp_path, "equity", symbol, "1d"),
            [bar(symbol, d, str(100 + d)) for d in range(10)],
        )
    write_bars(
        cache_path(tmp_path, "equity", "DEAD", "1d"),
        [bar("DEAD", d, str(100 + d)) for d in range(5)],
    )
    panel = Panel.load(tmp_path, "equity", ["AAA", "BBB", "DEAD"], "1d")

    assert panel.shape[0] == 5, "the survivors lose the rows DEAD never had"
    assert panel.rows_removed_by[0] == ("DEAD", 5)
    warning = panel.survivorship_warning()
    assert warning is not None
    assert "DEAD" in warning and "delisting" in warning
    assert "SURVIVORSHIP-CONDITIONED" in repr(panel)


def test_a_universe_with_no_exits_raises_no_survivorship_warning(cache: Path) -> None:
    panel = Panel.load(cache, "equity", ["AAA", "BBB"], "1d")
    assert panel.rows_removed_by == ()
    assert panel.survivorship_warning() is None


# -- The union grid ----------------------------------------------------------


def test_the_intersection_destroys_history_a_mid_window_listing_should_keep(
    tmp_path: Path,
) -> None:
    """Why the union grid exists.

    A and B live throughout; C lists at the midpoint. Under an intersection, C
    deletes A's and B's first five days — so handing a *correct* point-in-time
    universe to an intersection panel silently undoes the point-in-time
    selection, and `survivorship_warning()` fires on data that is right.
    """
    ensure_layout(tmp_path)
    for symbol, days in (("A", range(10)), ("B", range(10)), ("C", range(5, 10))):
        write_bars(
            cache_path(tmp_path, "equity", symbol, "1d"),
            [bar(symbol, d, str(100 + d)) for d in days],
        )

    intersection = Panel.load(tmp_path, "equity", ["A", "B", "C"], "1d")
    union = Panel.load(tmp_path, "equity", ["A", "B", "C"], "1d", grid="union")

    assert intersection.shape == (5, 3)
    assert union.shape == (10, 3)
    assert intersection.series("close", "A").to_pylist() == [105.0, 106, 107, 108, 109]
    assert union.series("close", "A").to_pylist() == [float(100 + d) for d in range(10)]


def test_a_union_cell_the_symbol_never_printed_is_null_not_a_number(
    tmp_path: Path,
) -> None:
    ensure_layout(tmp_path)
    for symbol, days in (("A", range(10)), ("C", range(5, 10))):
        write_bars(
            cache_path(tmp_path, "equity", symbol, "1d"),
            [bar(symbol, d, str(100 + d)) for d in days],
        )
    union = Panel.load(tmp_path, "equity", ["A", "C"], "1d", grid="union")

    assert union.row("close", 0).to_pylist() == [100.0, None]
    assert union.row("close", 9).to_pylist() == [109.0, 109.0]
    assert union.valid is not None
    assert union.valid["close"].column("C").to_pylist() == [False] * 5 + [True] * 5


def test_ranking_a_union_grid_refuses_rather_than_choosing_for_you(
    tmp_path: Path,
) -> None:
    """Whether a missing symbol ranks worst, median, or is excluded is a
    modelling decision. The shape exists so a point-in-time universe can be
    represented; the operations that would quietly decide refuse."""
    ensure_layout(tmp_path)
    for symbol, days in (("A", range(10)), ("C", range(5, 10))):
        write_bars(
            cache_path(tmp_path, "equity", symbol, "1d"),
            [bar(symbol, d, str(100 + d)) for d in days],
        )
    union = Panel.load(tmp_path, "equity", ["A", "C"], "1d", grid="union")

    for operation in ("rank_row", "zscore_row"):
        with pytest.raises(PanelError, match="union grid"):
            getattr(union, operation)("close", 9)


def test_retained_is_none_on_a_union_because_the_ratio_would_read_one(
    tmp_path: Path,
) -> None:
    """`retained` counts kept-over-total. On a union nothing is dropped, so it
    would report "no data lost" precisely when the data is most missing."""
    ensure_layout(tmp_path)
    for symbol, days in (("A", range(10)), ("C", range(5, 10))):
        write_bars(
            cache_path(tmp_path, "equity", symbol, "1d"),
            [bar(symbol, d, str(100 + d)) for d in days],
        )
    union = Panel.load(tmp_path, "equity", ["A", "C"], "1d", grid="union")
    assert union.retained is None
    assert union.grid == "union"
    assert "grid=union" in repr(union)


# -- The two constructors must be equally honest ------------------------------


def test_from_bars_reports_what_load_reports(tmp_path: Path) -> None:
    """The parity test that makes the fix real.

    `from_bars` used to leave `rows_removed_by` and `gaps` empty, so a panel
    built from a symbol that died mid-sample plus a twenty-day hole reported
    `is_contiguous=True` and `survivorship_warning()=None`. Two constructors
    for one type, and the quieter one was the untested one.
    """
    series = {
        "AAA": [bar("AAA", d, str(100 + d)) for d in range(40)],
        "BBB": [bar("BBB", d, str(200 + d)) for d in list(range(10)) + list(range(30, 40))],
        "DEAD": [bar("DEAD", d, str(300 + d)) for d in range(5)],
    }
    ensure_layout(tmp_path)
    for symbol, bars_ in series.items():
        write_bars(cache_path(tmp_path, "equity", symbol, "1d"), bars_)

    loaded = Panel.load(tmp_path, "equity", list(series), "1d")
    in_memory = Panel.from_bars(series, interval=timedelta(days=1))

    assert in_memory.shape == loaded.shape
    assert in_memory.rows_removed_by == loaded.rows_removed_by
    assert in_memory.gaps == loaded.gaps
    assert in_memory.is_contiguous == loaded.is_contiguous
    assert (in_memory.survivorship_warning() is None) == (loaded.survivorship_warning() is None)


def test_from_bars_refuses_to_drop_an_empty_symbol_silently() -> None:
    """It used to filter them out, so `panel.symbols` differed from what was
    asked for with nothing recording it."""
    with pytest.raises(PanelError, match="No bars for"):
        Panel.from_bars({"AAA": [bar("AAA", 0, "100")], "GONE": []}, interval=timedelta(days=1))


def test_a_union_grid_reports_a_symbol_s_own_outage(tmp_path: Path) -> None:
    """`gaps` is grid-level, and a union axis is contiguous by construction
    whenever *any* symbol printed — so a symbol's own three-day hole vanishes
    from `is_contiguous` and would reappear nowhere. Differencing across it
    gives a multi-day move labelled as one bar, which is exactly what `gaps`
    exists to surface on an intersection.
    """
    ensure_layout(tmp_path)
    write_bars(
        cache_path(tmp_path, "equity", "AAA", "1d"),
        [bar("AAA", d, str(100 + d)) for d in range(20)],
    )
    write_bars(
        cache_path(tmp_path, "equity", "BBB", "1d"),
        [bar("BBB", d, str(500 + d)) for d in range(20) if d not in (8, 9, 10)],
    )
    union = Panel.load(tmp_path, "equity", ["AAA", "BBB"], "1d", grid="union")

    assert union.shape[0] == 20
    assert union.is_contiguous, "the axis itself is regular"
    assert not union.is_complete, "but BBB is missing from three rows of it"
    assert union.missing_by == (("BBB", 3),)
    assert "missing=1 symbols (worst BBB: 3)" in repr(union)


def test_a_complete_union_reports_nothing_missing(tmp_path: Path) -> None:
    ensure_layout(tmp_path)
    for symbol in ("AAA", "BBB"):
        write_bars(
            cache_path(tmp_path, "equity", symbol, "1d"),
            [bar(symbol, d, str(100 + d)) for d in range(10)],
        )
    union = Panel.load(tmp_path, "equity", ["AAA", "BBB"], "1d", grid="union")
    assert union.missing_by == ()
    assert union.is_complete
