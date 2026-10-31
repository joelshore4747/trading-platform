"""Durable capture of a live stream, and compaction into the Parquet cache.

``data/cache.py::write_bars`` overwrites a whole file and demands a non-empty,
single-symbol, strictly increasing series. That is the right shape for a
finished archive and the wrong one for a process that has been running for nine
days: it cannot append, so a 24/7 recorder using it would rewrite the entire
day's file on every bar.

So capture is split in two.

**Append** is a line-per-bar JSONL journal, one file per symbol per UTC day. It
is crash-safe by construction — a killed process loses at most the line it was
writing, and a partial final line is discarded on read rather than parsed into a
plausible bar. Prices are strings, so the ``Decimal`` round-trip is exact and a
float never touches the path between the venue and the disk.

**Compact** folds a finished day into the canonical Parquet cache through the
existing ``write_bars``, which then applies its own ordering and duplicate
checks. Those checks are the point: compaction is where a day's capture is
audited, and it refuses rather than repairing.

Nothing here interpolates, deduplicates by guesswork, or reorders. A capture is
a record of what arrived, including its holes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading_platform.core.events import Bar
from trading_platform.data.cache import cache_path, write_bars

#: Journal layout: ``<root>/<market>/<symbol>/<YYYY-MM-DD>.jsonl``.
#: Deliberately not the Parquet cache's layout. A half-written day sitting where
#: `CachedBarFeed` reads would be picked up by a backtest as though it were a
#: finished series, which is exactly the kind of quiet contamination
#: `data/cache.py` refuses elsewhere.
JOURNAL_DIRNAME = "journal"


class JournalError(RuntimeError):
    """A journal file is unreadable or inconsistent with its own name."""


def journal_path(root: Path | str, market: str, symbol: str, day: datetime) -> Path:
    """Location of one symbol's journal for one UTC day."""
    if day.tzinfo is None:
        raise ValueError(f"day must be timezone-aware, got {day!r}")
    return Path(root) / JOURNAL_DIRNAME / market / symbol / f"{day.astimezone(UTC):%Y-%m-%d}.jsonl"


def _encode(bar: Bar) -> str:
    return json.dumps(
        {
            "t": int(bar.timestamp.timestamp() * 1000),
            "o": str(bar.open),
            "h": str(bar.high),
            "l": str(bar.low),
            "c": str(bar.close),
            "v": str(bar.volume),
        },
        separators=(",", ":"),
    )


def _decode(line: str, symbol: str) -> Bar:
    raw = json.loads(line)
    return Bar(
        symbol=symbol,
        timestamp=datetime.fromtimestamp(raw["t"] / 1000, tz=UTC),
        open=Decimal(raw["o"]),
        high=Decimal(raw["h"]),
        low=Decimal(raw["l"]),
        close=Decimal(raw["c"]),
        volume=Decimal(raw["v"]),
    )


@dataclass(slots=True)
class RecorderStats:
    written: int = 0
    files_touched: int = 0
    #: Handles released when the day rolled over. Without eviction the recorder
    #: runs out of file descriptors and stops writing silently.
    files_closed: int = 0


class JournalRecorder:
    """Append-only capture. Satisfies ``live.stream.BarSink``.

    One open handle per symbol *for the current day*, flushed on every bar.
    Yesterday's handles are released when the day rolls over; a late bar for a
    past day reopens it in append mode, so nothing is lost but the descriptor.
    Flushing per bar costs
    a syscall and buys the property that matters: the archive is never more than
    one bar behind reality, so a crash at 03:00 costs a bar rather than an hour.
    """

    def __init__(self, root: Path | str, market: str, *, flush: bool = True) -> None:
        self._root = Path(root)
        self._market = market
        self._flush = flush
        self._handles: dict[tuple[str, str], object] = {}
        self.stats = RecorderStats()

    def record(self, bar: Bar) -> None:
        day = bar.timestamp.astimezone(UTC).strftime("%Y-%m-%d")
        key = (bar.symbol, day)
        handle = self._handles.get(key)
        if handle is None:
            # Close yesterday before opening today. Without this the recorder
            # holds one file descriptor per symbol-day forever and eventually
            # hits RLIMIT_NOFILE — measured at 61 of 80 symbol-days under a
            # limit of 64, with every subsequent bar lost and NOTHING reported,
            # because the journal is the destination whose failures the rest of
            # the system trusts (adversarial review, 2026-09-04). At systemd's
            # usual 1024 that is about five days on 200 symbols.
            self._close_other_days(day)
            path = journal_path(self._root, self._market, bar.symbol, bar.timestamp)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8")
            self._handles[key] = handle
            self.stats.files_touched += 1
        handle.write(_encode(bar) + "\n")  # type: ignore[attr-defined]
        if self._flush:
            handle.flush()  # type: ignore[attr-defined]
        self.stats.written += 1

    def _close_other_days(self, day: str) -> None:
        """Release handles for every day but this one.

        A bar arriving late for yesterday reopens yesterday's file in append
        mode, so nothing is lost — only the descriptor is.
        """
        stale = [key for key in self._handles if key[1] != day]
        for key in stale:
            self._handles.pop(key).close()  # type: ignore[attr-defined]
            self.stats.files_closed += 1

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()  # type: ignore[attr-defined]
        self._handles.clear()

    def __enter__(self) -> JournalRecorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_journal(path: Path | str, symbol: str) -> list[Bar]:
    """Read one journal file, discarding a torn final line.

    A process killed mid-write leaves a partial last line. Discarding it is
    correct and parsing it is not: a truncated JSON object either raises or, far
    worse, decodes into a bar with plausible-looking fields. Only the **last**
    line may be torn — a bad line anywhere else means the file was corrupted by
    something other than a clean kill, and that raises.
    """
    source = Path(path)
    if not source.is_file():
        raise JournalError(f"no journal at {source}")

    lines = source.read_text(encoding="utf-8").splitlines()
    bars: list[Bar] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            bars.append(_decode(line, symbol))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            if index == len(lines) - 1:
                break  # torn tail from a kill; the rest of the file stands
            raise JournalError(
                f"{source}: line {index + 1} of {len(lines)} is unreadable ({exc}). "
                "Only a torn FINAL line is expected — damage in the middle means "
                "something other than a clean kill wrote this file."
            ) from exc
    return bars


def iter_journal_days(root: Path | str, market: str, symbol: str) -> Iterator[Path]:
    """Every journal file for a symbol, oldest first."""
    directory = Path(root) / JOURNAL_DIRNAME / market / symbol
    if not directory.is_dir():
        return iter(())
    return iter(sorted(directory.glob("*.jsonl")))


@dataclass(frozen=True, slots=True)
class CompactionResult:
    symbol: str
    days: int
    bars_read: int
    bars_written: int
    path: Path | None

    @property
    def duplicates_dropped(self) -> int:
        """Bars read but not written — a venue resending on reconnect.

        Exact-duplicate timestamps are dropped, and only where the whole bar
        matches. A venue that resends a bar with *different* values is not
        repeating itself; that is a correction or a bug, and `compact` raises
        rather than silently picking one.
        """
        return self.bars_read - self.bars_written


def compact(
    root: Path | str,
    market: str,
    symbol: str,
    interval: str,
    *,
    days: Sequence[Path] | None = None,
) -> CompactionResult:
    """Fold journal days into the Parquet cache, auditing as it goes.

    Exact duplicates are dropped — a reconnecting venue commonly replays its
    last few bars. A repeat that *disagrees* with what was already recorded
    raises: that is a correction or a defect, and choosing one silently would
    put an unexplained number into the archive every strategy then reads.

    Ordering and remaining duplicates are then checked again by ``write_bars``,
    which is the audit this whole split exists to reach.
    """
    files = list(days) if days is not None else list(iter_journal_days(root, market, symbol))
    seen: dict[datetime, Bar] = {}
    bars_read = 0

    for path in files:
        for bar in read_journal(path, symbol):
            bars_read += 1
            previous = seen.get(bar.timestamp)
            if previous is None:
                seen[bar.timestamp] = bar
            elif previous != bar:
                raise JournalError(
                    f"{symbol}: two different bars recorded at {bar.timestamp.isoformat()}. "
                    "A venue replaying its own bars on reconnect is expected; a venue "
                    "replaying them with different values is a correction or a defect, "
                    "and this refuses to pick one for you."
                )

    if not seen:
        return CompactionResult(symbol, len(files), bars_read, 0, None)

    ordered = [seen[key] for key in sorted(seen)]
    destination = cache_path(root, market, symbol, interval)
    write_bars(destination, ordered)
    return CompactionResult(symbol, len(files), bars_read, len(ordered), destination)
