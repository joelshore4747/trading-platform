"""Importing real vendor data.

The fixture is a 240-row slice of the actual `historical.zip` BTCUSDT file,
cut so it straddles a genuine collector outage (2026-07-20 17:40 -> 23:39, a
5h59m hole). Synthetic fixtures would not have caught the two things that
matter here — the open-stamping convention and the shape of the missing data.

The load-bearing test is `test_an_open_stamped_file_lands_close_stamped`.
Getting that backwards is a silent one-bar lookahead, and at 1-minute frequency
it is the difference between a result and a fiction.
"""

from __future__ import annotations

import io
import itertools
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.core.events import Bar
from trading_platform.core.instruments import Perpetual, Spot
from trading_platform.data.feed import OutOfOrderDataError
from trading_platform.data.vendor import (
    Segment,
    VendorFormatError,
    align,
    assess,
    common_grid,
    coverage,
    coverage_by_hour,
    instrument_for,
    load_bars,
    load_segments,
    load_zip,
    merge,
    screen,
    split_filename,
    symbol_from_filename,
)

FIXTURE = Path(__file__).parent / "fixtures" / "vendor" / "btcusdt_1m.csv"
ARCHIVE = Path(__file__).parent.parent / "historical.zip"
MINUTE = timedelta(minutes=1)

#: The first row of the fixture, as it appears in the file (open-stamped).
FIRST_RAW = datetime(2026, 7, 20, 15, 41, tzinfo=UTC)


def csv_text(rows: str) -> io.StringIO:
    return io.StringIO("timestamp,open,high,low,close,volume,source\n" + rows)


# -- The one that matters ----------------------------------------------------


def test_an_open_stamped_file_lands_close_stamped() -> None:
    """Binance returns openTime; this repository stamps bars at their close.

    Loading an open-stamped file as though it were close-stamped shifts every
    bar one interval early, which lets a strategy act on a bar before it
    finished. That is a one-bar lookahead, not a cosmetic offset.
    """
    bars = load_bars(FIXTURE, interval=MINUTE, stamp="open")
    assert bars[0].timestamp == FIRST_RAW + MINUTE, (
        f"file row says {FIRST_RAW:%H:%M}, so the bar closed at "
        f"{FIRST_RAW + MINUTE:%H:%M} and must be stamped there"
    )


def test_declaring_the_file_close_stamped_does_not_shift_it() -> None:
    """The flag has to actually do something, or it is decoration."""
    as_open = load_bars(FIXTURE, interval=MINUTE, stamp="open")
    as_close = load_bars(FIXTURE, interval=MINUTE, stamp="close")
    assert as_close[0].timestamp == FIRST_RAW
    assert as_open[0].timestamp - as_close[0].timestamp == MINUTE


def test_the_interval_drives_the_shift() -> None:
    """The interval is the shift, so it must match the file."""
    minutely = load_bars(FIXTURE, interval=MINUTE, stamp="open")
    assert minutely[0].timestamp == FIRST_RAW + MINUTE


def test_an_interval_the_file_contradicts_is_refused() -> None:
    """This test used to assert the opposite — that a wrong interval quietly
    mis-stamped every bar — because that was the behaviour.

    A lookahead audit pointed out where the guard was: `load_zip` refused when
    the *filename* named a conflicting interval, which is the case where the
    caller already knows. The rows settle it — the modal gap between
    consecutive stamps IS the interval — and `load_bars` read every one of
    those stamps and threw them away. The check lived where the information
    was redundant and was missing where it mattered.
    """
    with pytest.raises(VendorFormatError, match="most often"):
        load_bars(FIXTURE, interval=timedelta(hours=1), stamp="open")


def test_under_declaring_names_itself_as_a_lookahead() -> None:
    """The dangerous direction, and the message has to say which it is.

    Declaring 1m for a 5m file stamps every bar four minutes before its true
    close. Over-declaring merely stamps late — wrong, but not a leak — and
    saying "lookahead" for that would be crying wolf.
    """
    rows = ["timestamp,open,high,low,close,volume"]
    for i in range(20):
        rows.append(f"2026-01-01T{i // 12:02d}:{(i * 5) % 60:02d}:00Z,100,101,99,100,5")
    five_minute = io.StringIO("\n".join(rows) + "\n")

    with pytest.raises(VendorFormatError, match="BEFORE its true close"):
        load_bars(five_minute, symbol="X", interval=MINUTE, stamp="open")


def test_a_real_archive_full_of_gaps_still_passes_the_check() -> None:
    """Only the modal step is used. Every gap is a larger step, and this
    fixture is 46% covered with a 5h59m hole in the middle — a check that
    fired on it would fire on everything and so say nothing."""
    bars = load_bars(FIXTURE, interval=MINUTE, stamp="open")
    assert len(bars) == 240


@pytest.mark.parametrize("stamp", ["OPEN", "middle", ""])
def test_an_unknown_stamping_convention_is_refused(stamp: str) -> None:
    with pytest.raises(VendorFormatError, match="stamp must be"):
        load_bars(FIXTURE, stamp=stamp)  # type: ignore[arg-type]


# -- Gaps are structure, not noise -------------------------------------------


def test_a_real_collector_outage_becomes_a_segment_boundary() -> None:
    """The fixture straddles a genuine 5h59m hole. Concatenating across it
    would tell a 60-bar moving average that six hours is one minute."""
    bars = load_bars(FIXTURE, interval=MINUTE)
    segments = load_segments(bars, interval=MINUTE)

    assert len(segments) == 2, "one hole, two contiguous runs"
    assert segments[0].preceded_by_gap is None, "nothing precedes the first"
    assert segments[1].preceded_by_gap == timedelta(hours=5, minutes=59)
    assert segments[0].end + segments[1].preceded_by_gap == segments[1].start


def test_every_segment_is_internally_contiguous() -> None:
    """The property the whole abstraction exists to provide."""
    segments = load_segments(load_bars(FIXTURE, interval=MINUTE), interval=MINUTE)
    for segment in segments:
        for previous, bar in itertools.pairwise(segment.bars):
            assert bar.timestamp - previous.timestamp == MINUTE


def test_segments_partition_the_series_without_loss() -> None:
    bars = load_bars(FIXTURE, interval=MINUTE)
    segments = load_segments(bars, interval=MINUTE)
    assert sum(len(s) for s in segments) == len(bars)
    rejoined = [b for s in segments for b in s.bars]
    assert [b.timestamp for b in rejoined] == [b.timestamp for b in bars]


def test_a_tolerance_can_absorb_jitter_without_hiding_an_outage() -> None:
    bars = load_bars(FIXTURE, interval=MINUTE)
    generous = load_segments(bars, interval=MINUTE, tolerance=timedelta(minutes=5))
    assert len(generous) == 2, "six hours is not jitter"


def test_a_continuous_series_is_one_segment() -> None:
    bars = load_bars(FIXTURE, interval=MINUTE)
    first = load_segments(bars, interval=MINUTE)[0]
    assert len(load_segments(first.bars, interval=MINUTE)) == 1


def test_an_empty_series_yields_no_segments() -> None:
    assert load_segments([], interval=MINUTE) == []


def test_a_segment_cannot_be_empty() -> None:
    with pytest.raises(ValueError, match="at least one bar"):
        Segment(symbol="BTCUSDT", bars=())


# -- Coverage is reported, never repaired ------------------------------------


def test_coverage_reports_the_hole_rather_than_filling_it() -> None:
    bars = load_bars(FIXTURE, interval=MINUTE)
    assert 0.35 < coverage(bars, interval=MINUTE) < 0.50


def test_coverage_by_hour_exposes_a_bias_a_single_figure_hides() -> None:
    """The real archive is not thinned at random: hours 18-23 UTC run about
    half the coverage of the rest of the day, so anything time-of-day sensitive
    measured on it is biased."""
    if not ARCHIVE.exists():
        pytest.skip("historical.zip not present")
    bars = load_zip(ARCHIVE, symbols=["BTCUSDT"])["BTCUSDT"]
    by_hour = coverage_by_hour(bars, interval=MINUTE)

    assert set(by_hour) == set(range(24)), "every hour reported, including empty ones"
    morning = sum(by_hour[h] for h in range(7, 11)) / 4
    evening = sum(by_hour[h] for h in range(18, 24)) / 6
    assert evening < morning * 0.7, (
        f"expected the documented evening shortfall: mornings {morning:.0%} "
        f"against evenings {evening:.0%}"
    )


# -- Symbols and instruments -------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("historical/btcusdt_1m.csv", "BTCUSDT"),
        ("ethusdt_1m.csv", "ETHUSDT"),
        ("1000shibusdt_1m.csv", "1000SHIBUSDT"),
        ("/a/b/solusdt_1h.csv", "SOLUSDT"),
        ("plain.csv", "PLAIN"),
    ],
)
def test_symbol_is_derived_from_the_filename(filename: str, expected: str) -> None:
    assert symbol_from_filename(filename) == expected


def test_a_filename_that_contradicts_the_file_is_refused() -> None:
    """Two sources of truth that can disagree is how a BTC file gets loaded as
    ETH. Refusing beats picking one."""
    handle = csv_text("2026-07-20T15:41:00Z,1,1,1,1,1,binance_futures:ETHUSDT\n")
    with pytest.raises(VendorFormatError, match="says ETHUSDT"):
        load_bars(handle, symbol="BTCUSDT")


def test_the_check_can_be_waived_deliberately() -> None:
    handle = csv_text("2026-07-20T15:41:00Z,1,1,1,1,1,binance_futures:ETHUSDT\n")
    bars = load_bars(handle, symbol="BTCUSDT", strict_symbol=False)
    assert bars[0].symbol == "BTCUSDT"


def test_a_futures_source_produces_a_perpetual_not_spot() -> None:
    """Perpetuals accrue funding. Treating one as spot silently omits a carry
    cost that runs to double digits annualised."""
    assert isinstance(instrument_for("BTCUSDT", "binance_futures:BTCUSDT"), Perpetual)
    assert isinstance(instrument_for("BTCUSDT", "binance_spot:BTCUSDT"), Spot)
    assert isinstance(instrument_for("BTCUSDT", None), Spot)


# -- Parsing guards ----------------------------------------------------------


def test_prices_are_exact_decimals_parsed_from_the_string() -> None:
    """Never via float. Decimal(0.1) is not 0.1, and a price that round-trips
    through float cannot be reconciled against an exchange statement."""
    bars = load_bars(FIXTURE, interval=MINUTE)
    assert isinstance(bars[0].close, Decimal)
    assert bars[0].open == Decimal("64933.9")
    assert bars[0].volume == Decimal("240.095")


def test_a_missing_column_is_named() -> None:
    handle = io.StringIO("timestamp,open,high,low\n2026-07-20T15:41:00Z,1,1,1\n")
    with pytest.raises(VendorFormatError, match="missing required column"):
        load_bars(handle, symbol="X")


def test_a_naive_timestamp_is_refused() -> None:
    """A naive stamp in market data is an invitation to an off-by-one-session
    bug that surfaces months later."""
    handle = csv_text("2026-07-20 15:41:00,1,1,1,1,1,x:X\n")
    with pytest.raises(VendorFormatError, match="no timezone"):
        load_bars(handle, symbol="X", strict_symbol=False)


@pytest.mark.parametrize("bad", ["not-a-date", "", "2026-13-99T00:00:00Z"])
def test_an_unparseable_timestamp_names_the_line(bad: str) -> None:
    handle = csv_text(f"{bad},1,1,1,1,1,x:X\n")
    with pytest.raises(VendorFormatError, match="line 2"):
        load_bars(handle, symbol="X", strict_symbol=False)


def test_a_bad_price_names_the_field_and_line() -> None:
    handle = csv_text("2026-07-20T15:41:00Z,1,1,oops,1,1,x:X\n")
    with pytest.raises(VendorFormatError, match="line 2: bad low"):
        load_bars(handle, symbol="X", strict_symbol=False)


def test_backwards_timestamps_raise_rather_than_being_sorted() -> None:
    """Sorting here would hide a collector bug instead of surfacing it."""
    handle = csv_text("2026-07-20T15:41:00Z,1,1,1,1,1,x:X\n2026-07-20T15:39:00Z,1,1,1,1,1,x:X\n")
    with pytest.raises(OutOfOrderDataError):
        load_bars(handle, symbol="X", strict_symbol=False)


def test_an_impossible_bar_is_rejected_by_the_constructor() -> None:
    """Everything goes through the real Bar, so __post_init__ still applies."""
    handle = csv_text("2026-07-20T15:41:00Z,10,5,20,10,1,x:X\n")
    with pytest.raises(ValueError):
        load_bars(handle, symbol="X", strict_symbol=False)


# -- The cross-section -------------------------------------------------------


def test_merging_symbols_produces_a_valid_feed_order() -> None:
    bars_a = load_bars(FIXTURE, symbol="AAAUSDT", strict_symbol=False)
    bars_b = load_bars(FIXTURE, symbol="ZZZUSDT", strict_symbol=False)
    merged = merge([bars_a, bars_b])

    assert len(merged) == len(bars_a) + len(bars_b)
    keys = [(b.timestamp, b.symbol) for b in merged]
    assert keys == sorted(keys), "a feed must emit (timestamp, symbol) ordered"


@pytest.mark.slow
def test_the_whole_archive_loads_as_one_cross_section() -> None:
    """40 symbols merged. Safe only because the cross-symbol fill leak is
    closed — before that, a run over this data would have been fictional."""
    if not ARCHIVE.exists():
        pytest.skip("historical.zip not present")
    loaded = load_zip(ARCHIVE)
    assert len(loaded) == 40

    merged = merge(loaded.values())
    assert len(merged) > 800_000
    for previous, bar in itertools.pairwise(merged):
        assert previous.timestamp <= bar.timestamp


def test_loading_a_subset_of_the_archive_works() -> None:
    if not ARCHIVE.exists():
        pytest.skip("historical.zip not present")
    loaded = load_zip(ARCHIVE, symbols=["BTCUSDT", "ETHUSDT"])
    assert set(loaded) == {"BTCUSDT", "ETHUSDT"}
    assert all(b.symbol == "BTCUSDT" for b in loaded["BTCUSDT"])


# -- Quality screening -------------------------------------------------------
#
# Added after `data-validator` found what a hand-check had missed. I had
# inspected BTCUSDT, seen no zero-volume bars, and reported that as a property
# of the archive. TONUSDT has 21,158 of them and one distinct price across the
# whole month; QNTUSDT has 453. Generalising from one symbol is exactly the
# method this screen replaces.


def synthetic_bars(symbol: str, closes: list[str], volumes: list[str] | None = None) -> list[Bar]:
    vols = volumes or ["10"] * len(closes)
    return [
        Bar(
            symbol=symbol,
            timestamp=datetime(2026, 7, 20, 0, 0, tzinfo=UTC) + i * MINUTE,
            open=Decimal(c),
            high=Decimal(c),
            low=Decimal(c),
            close=Decimal(c),
            volume=Decimal(v),
        )
        for i, (c, v) in enumerate(zip(closes, vols, strict=True))
    ]


def test_a_frozen_placeholder_is_identified_as_dead() -> None:
    """TONUSDT in the real archive: one price, no volume, a whole month."""
    quality = assess(synthetic_bars("TONUSDT", ["1.5901"] * 50, ["0"] * 50))
    assert quality.is_dead
    assert quality.distinct_closes == 1
    assert quality.zero_volume_fraction == 1.0
    assert "placeholder" in quality.concerns()[0]


def test_a_dead_symbol_reports_only_that_it_is_dead() -> None:
    """No point listing a stalled run inside an instrument that never traded."""
    quality = assess(synthetic_bars("TONUSDT", ["1"] * 50, ["0"] * 50))
    assert len(quality.concerns()) == 1


def test_a_healthy_symbol_raises_no_concern() -> None:
    closes = [str(100 + (i % 11)) for i in range(200)]
    quality = assess(synthetic_bars("BTCUSDT", closes))
    assert not quality.is_dead
    assert quality.concerns() == ()


def test_a_flat_price_with_volume_is_a_quiet_market_not_a_fault() -> None:
    """The calibration that matters. At 1-minute resolution BTCUSDT itself has
    an 8-bar flat run, so flagging flat runs alone flags 36 of 39 symbols —
    and a check that fires on everything says nothing."""
    quality = assess(synthetic_bars("BTCUSDT", ["100"] * 10 + ["101"] * 10))
    assert quality.longest_flat_run == 10
    assert quality.longest_silent_run == 0
    assert quality.concerns() == ()


def test_a_flat_price_with_no_volume_is_a_stalled_feed() -> None:
    closes = ["100"] * 6 + [str(100 + i) for i in range(20)]
    volumes = ["0"] * 6 + ["10"] * 20
    quality = assess(synthetic_bars("XUSDT", closes, volumes))
    assert quality.longest_silent_run == 6
    assert any("stalled feed" in c for c in quality.concerns())


def test_screening_keeps_the_quality_of_what_it_drops() -> None:
    """Excluded symbols stay inspectable rather than silently vanishing."""
    universe = {
        "GOOD": synthetic_bars("GOOD", [str(100 + i % 7) for i in range(50)]),
        "DEAD": synthetic_bars("DEAD", ["1"] * 50, ["0"] * 50),
    }
    kept, quality = screen(universe)
    assert set(kept) == {"GOOD"}
    assert set(quality) == {"GOOD", "DEAD"}, "quality covers everything, kept or not"
    assert quality["DEAD"].is_dead


def test_assessing_an_empty_series_raises() -> None:
    with pytest.raises(ValueError, match="empty series"):
        assess([])


@pytest.mark.slow
def test_the_real_archive_contains_exactly_one_dead_symbol() -> None:
    if not ARCHIVE.exists():
        pytest.skip("historical.zip not present")
    _, quality = screen(load_zip(ARCHIVE))
    dead = sorted(s for s, q in quality.items() if q.is_dead)
    assert dead == ["TONUSDT"]


# -- Cross-sectional alignment -----------------------------------------------


def test_symbols_are_aligned_by_intersection_never_by_forward_fill() -> None:
    """Forward-filling would fix the shape by inventing prices that never
    printed, and a strategy trading an invented price is trading a fiction.
    Dropping unshared minutes costs data and states the cost."""
    a = synthetic_bars("AUSDT", ["1", "2", "3", "4"])
    b = synthetic_bars("BUSDT", ["1", "2", "3", "4"])
    del b[2]  # BUSDT is missing the third minute

    aligned, retained = align({"AUSDT": a, "BUSDT": b})

    assert len(aligned["AUSDT"]) == len(aligned["BUSDT"]) == 3
    assert [x.timestamp for x in aligned["AUSDT"]] == [x.timestamp for x in aligned["BUSDT"]]
    assert retained == pytest.approx(6 / 7)


def test_the_common_grid_is_the_shared_timestamps() -> None:
    a = synthetic_bars("AUSDT", ["1", "2", "3"])
    b = synthetic_bars("BUSDT", ["1", "2", "3"])
    del b[0]
    grid = common_grid({"AUSDT": a, "BUSDT": b})
    assert grid == [a[1].timestamp, a[2].timestamp]


def test_aligning_an_empty_universe_is_not_an_error() -> None:
    aligned, retained = align({})
    assert aligned == {}
    assert retained == 1.0


@pytest.mark.slow
def test_the_real_universe_aligns_at_low_cost() -> None:
    """Bar counts run 20,869 to 21,160, so the grids genuinely differ — but
    intersecting costs only about 1% of the data, far cheaper than inventing
    prices to paper over it."""
    if not ARCHIVE.exists():
        pytest.skip("historical.zip not present")
    kept, _ = screen(load_zip(ARCHIVE))
    aligned, retained = align(kept)

    assert retained > 0.95, f"alignment discarded {1 - retained:.1%} of the data"
    sizes = {len(bars) for bars in aligned.values()}
    assert len(sizes) == 1, "every symbol must end on the same grid"


def test_a_mixed_resolution_archive_raises_rather_than_mis_stamping(tmp_path: Path) -> None:
    """The silent-corruption path this closed.

    `symbol_from_filename` stripped the interval suffix and threw it away, so
    `load_zip` applied one caller-supplied interval to every member. An archive
    mixing `btcusdt_1m.csv` and `ethusdt_1h.csv` parsed as all-1m — and since
    the interval is what shifts an open-stamp to a close-stamp, every ETH bar
    landed 59 minutes early. Nothing downstream could detect it.
    """
    rows = (
        "timestamp,open,high,low,close,volume\n"
        "2026-01-01T00:00:00Z,100,101,99,100,5\n"
        "2026-01-01T00:01:00Z,100,101,99,100,5\n"
    )
    archive = tmp_path / "mixed.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("btcusdt_1m.csv", rows)
        handle.writestr("ethusdt_1h.csv", rows)

    with pytest.raises(VendorFormatError, match="filename says 1h"):
        load_zip(archive, interval=timedelta(minutes=1))

    # Loading one resolution at a time is still fine.
    assert list(load_zip(archive, interval=timedelta(minutes=1), symbols=["BTCUSDT"])) == [
        "BTCUSDT"
    ]


def test_split_filename_returns_the_interval_instead_of_discarding_it() -> None:
    assert split_filename("historical/btcusdt_1m.csv") == ("BTCUSDT", "1m")
    assert split_filename("ethusdt_1h.csv") == ("ETHUSDT", "1h")
    assert split_filename("spy.csv") == ("SPY", None)
