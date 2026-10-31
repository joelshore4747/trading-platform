"""Guards for durable capture.

The recorder exists to survive things: a kill mid-write, a venue replaying its
own bars on reconnect, a day that straddles midnight. Each of those is tested by
doing it, not by asserting that the happy path works.

The rule underneath all of them is the vendor loader's: **a capture is a record
of what arrived, holes included.** Nothing here interpolates, and a repeat that
disagrees with itself is refused rather than resolved by guesswork.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.core.events import Bar
from trading_platform.data.cache import read_bars
from trading_platform.live.recorder import (
    JournalError,
    JournalRecorder,
    compact,
    journal_path,
    read_journal,
)

START = datetime(2026, 3, 4, 22, 0, tzinfo=UTC)
MARKET, SYMBOL, INTERVAL = "um", "BTCUSDT", "1m"


@pytest.fixture
def root() -> Path:
    path = Path(tempfile.mkdtemp(prefix="recorder-test-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def bar(minute: int, close: str = "100.5") -> Bar:
    return Bar(
        symbol=SYMBOL,
        timestamp=START + timedelta(minutes=minute),
        open=Decimal("100.25"),
        high=Decimal("101"),
        low=Decimal("99.75"),
        close=Decimal(close),
        volume=Decimal("12.5"),
    )


def test_a_recorded_bar_round_trips_exactly(root: Path) -> None:
    """Prices are Decimal on both sides. A float here would round every price
    the platform ever captured, at the boundary the house rules protect."""
    with JournalRecorder(root, MARKET) as rec:
        rec.record(bar(0))

    [restored] = read_journal(journal_path(root, MARKET, SYMBOL, START), SYMBOL)
    assert restored == bar(0)
    assert isinstance(restored.close, Decimal)


def test_capture_splits_on_the_utc_day_boundary(root: Path) -> None:
    """A session running through midnight writes two files, not one long one."""
    with JournalRecorder(root, MARKET) as rec:
        rec.record(bar(0))  # 22:00
        rec.record(bar(150))  # 00:30 the next day

    assert journal_path(root, MARKET, SYMBOL, START).is_file()
    assert journal_path(root, MARKET, SYMBOL, START + timedelta(minutes=150)).is_file()


def test_a_torn_final_line_is_discarded_not_parsed(root: Path) -> None:
    """The crash case, produced by actually truncating the file.

    A process killed mid-write leaves a partial last line. Parsing it is the
    dangerous outcome: a truncated object can decode into a bar with plausible
    fields, and a fabricated bar in the archive outlives the crash that made it.
    """
    with JournalRecorder(root, MARKET) as rec:
        for minute in range(5):
            rec.record(bar(minute))

    path = journal_path(root, MARKET, SYMBOL, START)
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: -len(text.splitlines()[-1]) // 2], encoding="utf-8")  # tear the tail

    survivors = read_journal(path, SYMBOL)
    assert len(survivors) == 4, "the four complete lines stand"
    assert survivors == [bar(m) for m in range(4)]


def test_damage_anywhere_but_the_tail_raises(root: Path) -> None:
    """Only a clean kill tears the LAST line. Anything else is corruption, and
    quietly skipping it would silently shorten the archive."""
    with JournalRecorder(root, MARKET) as rec:
        for minute in range(4):
            rec.record(bar(minute))

    path = journal_path(root, MARKET, SYMBOL, START)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = '{"t": broken'
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(JournalError, match="line 2 of"):
        read_journal(path, SYMBOL)


def test_compaction_writes_the_parquet_cache_the_backtester_reads(root: Path) -> None:
    """The whole point of the split: today's capture is tomorrow's backtest."""
    with JournalRecorder(root, MARKET) as rec:
        for minute in range(10):
            rec.record(bar(minute))

    result = compact(root, MARKET, SYMBOL, INTERVAL)
    assert result.bars_written == 10
    assert result.path is not None

    from_cache = read_bars(result.path, SYMBOL)
    assert from_cache == [bar(m) for m in range(10)]


def test_a_venue_replaying_identical_bars_is_tolerated(root: Path) -> None:
    """Reconnection commonly resends the last few bars. That is not corruption."""
    with JournalRecorder(root, MARKET) as rec:
        for minute in range(5):
            rec.record(bar(minute))
        for minute in (3, 4):  # the replay after a reconnect
            rec.record(bar(minute))

    result = compact(root, MARKET, SYMBOL, INTERVAL)
    assert result.bars_read == 7
    assert result.bars_written == 5
    assert result.duplicates_dropped == 2


def test_a_replay_that_disagrees_with_itself_is_refused(root: Path) -> None:
    """Two different bars at one timestamp is a correction or a defect.

    Picking one silently would put an unexplained number into the archive that
    every later backtest reads as fact. The refusal names both possibilities
    rather than deciding between them.
    """
    with JournalRecorder(root, MARKET) as rec:
        # Both valid bars — `Bar.__post_init__` would reject an impossible one
        # long before compaction, so the disagreement has to be a plausible
        # correction, which is exactly the hard case.
        rec.record(bar(1, close="100.00"))
        rec.record(bar(1, close="100.75"))

    with pytest.raises(JournalError, match="two different bars recorded"):
        compact(root, MARKET, SYMBOL, INTERVAL)


def test_compaction_orders_across_files(root: Path) -> None:
    """Days are folded in timestamp order regardless of filesystem order."""
    with JournalRecorder(root, MARKET) as rec:
        rec.record(bar(150))  # next day, written first
        rec.record(bar(0))

    result = compact(root, MARKET, SYMBOL, INTERVAL)
    assert result.days == 2
    assert result.path is not None
    stamps = [b.timestamp for b in read_bars(result.path, SYMBOL)]
    assert stamps == sorted(stamps)


def test_compacting_nothing_writes_nothing(root: Path) -> None:
    """`write_bars` refuses an empty series, and an empty capture is not an
    error — a venue that said nothing is a fact about the venue."""
    result = compact(root, MARKET, SYMBOL, INTERVAL)
    assert result.bars_written == 0
    assert result.path is None


def test_the_journal_does_not_live_where_the_cache_reader_looks(root: Path) -> None:
    """A half-written day must not be mistaken for a finished series.

    `CachedBarFeed` reads the Parquet layout. If a journal in progress sat
    there, a backtest would pick up a partial day as though it were complete —
    the quiet contamination `data/cache.py` refuses everywhere else.
    """
    from trading_platform.data.cache import cache_path

    with JournalRecorder(root, MARKET) as rec:
        rec.record(bar(0))

    journal = journal_path(root, MARKET, SYMBOL, START)
    assert journal.is_file()
    assert not cache_path(root, MARKET, SYMBOL, INTERVAL).exists()
    assert "journal" in journal.parts


def test_a_naive_day_is_refused(root: Path) -> None:
    """Mixing naive and aware datetimes raises at the worst possible moment."""
    with pytest.raises(ValueError, match="timezone-aware"):
        journal_path(root, MARKET, SYMBOL, datetime(2026, 3, 4))
