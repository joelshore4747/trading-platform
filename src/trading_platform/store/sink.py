"""Writing captured bars to the store, without letting the store cost you a bar.

The rule this module exists for: **a database outage must never lose data.**
The durable record of what arrived is the append-only JSONL journal in
`live/recorder.py`, which needs nothing but a filesystem. The store is a second
destination, and a second destination that can take the first one down with it
is worse than no second destination at all.

So `StoreSink` swallows its own failures, counts them, and keeps accepting
bars. That is the opposite of this repository's usual refuse-rather-than-assume
stance, and the exception is deliberate: refusing here would mean dropping a
bar that the journal could have kept. What it must not do is fail *quietly* —
`StoreSink.stats` carries the failure count and the last error, the operations
page shows both, and a sink that has failed says so on `close()`.

`FanOutSink` is the other half: it writes to several sinks and one sink's
failure never stops another's.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from trading_platform.core.events import Bar
from trading_platform.live.stream import BarSink
from trading_platform.store.bars import BarConflict, Source, write_bars

__all__ = ["FanOutSink", "SinkStats", "StoreSink"]


@dataclass(slots=True)
class SinkStats:
    """What a sink has managed and what it has not."""

    accepted: int = 0
    written: int = 0
    #: Bars accepted, found already present with identical values, and skipped.
    #: A reconnect replaying is normal; a large number is a poller re-fetching
    #: more than it needs to.
    duplicate: int = 0
    #: Bars buffered but never written, because every flush of them failed.
    dropped: int = 0
    flushes: int = 0
    failures: int = 0
    conflicts: int = 0
    #: Times a dead connection was replaced. A database restart is survivable;
    #: before this existed, one cost every subsequent row until the collector
    #: was restarted by hand, and `Restart=always` never fired because the
    #: collector does not exit on a sink failure.
    reconnects: int = 0
    last_error: str = ""

    @property
    def healthy(self) -> bool:
        return self.failures == 0 and self.conflicts == 0 and self.dropped == 0

    def report(self) -> str:
        state = "ok" if self.healthy else "DEGRADED"
        line = (
            f"{state}: accepted {self.accepted:,}, written {self.written:,}, "
            f"duplicate {self.duplicate:,}, dropped {self.dropped:,}, "
            f"flushes {self.flushes}, failures {self.failures}, "
            f"conflicts {self.conflicts}, reconnects {self.reconnects}"
        )
        return line if not self.last_error else f"{line}\n  last error: {self.last_error}"


@dataclass(slots=True)
class StoreSink:
    """A `BarSink` that batches into Postgres and survives it being down.

    Batched because one INSERT per bar across hundreds of symbols is the wrong
    shape for both the database and the poller; flushed on a count or an age so
    a quiet symbol is not held indefinitely.
    """

    connection: Any
    source: Source
    run_id: int
    market: str
    interval: str
    batch_size: int = 500
    max_age: timedelta = timedelta(seconds=5)
    #: How to get a fresh connection when this one dies. `None` means a dead
    #: connection stays dead, which loses every subsequent row: a five-minute
    #: database restart cost the store everything until someone noticed
    #: (adversarial review, 2026-09-04).
    reconnect: Callable[[], Any] | None = None
    now: Any = None
    stats: SinkStats = field(default_factory=SinkStats)
    _buffer: list[Bar] = field(default_factory=list)
    _oldest: datetime | None = None

    def _clock(self) -> datetime:
        return self.now() if self.now is not None else datetime.now(UTC)

    def record(self, bar: Bar) -> None:
        """Accept a bar. Never raises: the journal must get it either way."""
        self.stats.accepted += 1
        self._buffer.append(bar)
        if self._oldest is None:
            self._oldest = self._clock()
        if len(self._buffer) >= self.batch_size or self._clock() - self._oldest >= self.max_age:
            self.flush()

    def flush(self) -> int:
        """Write what is buffered. Returns rows written; 0 on failure.

        On failure the buffer is *discarded*, not retried forever. Holding it
        would grow without bound through a long outage and eventually take the
        collector down, which is the failure this whole module exists to avoid.
        The bars are in the journal; `stats.dropped` says how many the store
        will be missing, and `scripts/journal_to_store.py` puts them back once
        the database is up.
        """
        if not self._buffer:
            return 0
        batch, self._buffer, self._oldest = self._buffer, [], None
        self.stats.flushes += 1
        try:
            written = self._write(batch)
        except BarConflict as exc:
            # The source contradicted itself. Not an outage: a finding. The
            # contradiction is never coerced or retried — but `write_bars`
            # refuses the WHOLE batch, and with a 500-bar batch that meant one
            # bad print cost up to 499 innocent bars which the capture had
            # already counted as recorded and would never re-fetch. So the
            # batch is re-sent one bar at a time: every bar that does not
            # contradict anything lands, and only the ones that do are counted
            # as conflicts and dropped.
            self.stats.last_error = str(exc)
            return self._write_individually(batch)
        except Exception as exc:
            self._failed(exc, len(batch))
            return 0
        self.stats.written += written
        self.stats.duplicate += len(batch) - written
        return written

    def _write(self, batch: Sequence[Bar]) -> int:
        return write_bars(
            self.connection,
            self.source,
            self.run_id,
            market=self.market,
            interval=self.interval,
            bars=batch,
        )

    def _write_individually(self, batch: Sequence[Bar]) -> int:
        """Salvage a batch one contradiction spoiled. Returns rows written."""
        written = 0
        for bar in batch:
            try:
                new_rows = self._write([bar])
            except BarConflict as exc:
                self.stats.conflicts += 1
                self.stats.dropped += 1
                self.stats.last_error = str(exc)
                continue
            except Exception as exc:
                self._failed(exc, 1)
                continue
            written += new_rows
            self.stats.written += new_rows
            self.stats.duplicate += 1 - new_rows
        return written

    def _failed(self, exc: Exception, lost: int) -> None:
        self.stats.failures += 1
        self.stats.dropped += lost
        self.stats.last_error = f"{type(exc).__name__}: {exc}"
        with suppress(Exception):  # the connection may already be gone
            self.connection.rollback()
        self._reconnect_if_dead()

    def _reconnect_if_dead(self) -> None:
        """Replace a connection the server has closed, if we know how.

        Only when it is actually dead: reconnecting on an ordinary constraint
        violation would throw away a perfectly good connection on every bad
        row. `psycopg` reports both `broken` and `closed`; anything without
        those attributes is not a connection this can judge, so it is left
        alone.
        """
        if self.reconnect is None:
            return
        broken = bool(getattr(self.connection, "broken", False))
        closed = bool(getattr(self.connection, "closed", False))
        if not (broken or closed):
            return
        try:
            self.connection = self.reconnect()
        except Exception as exc:
            self.stats.last_error = f"reconnect failed: {type(exc).__name__}: {exc}"
            return
        self.stats.reconnects += 1

    def close(self) -> SinkStats:
        """Write the tail batch and report. Called on every clean shutdown.

        The flush matters: at a 500-bar batch size a SIGTERM between flushes
        would otherwise discard everything buffered since the last one, and a
        graceful stop would lose more data than a crash.
        """
        self.flush()
        return self.stats


@dataclass(slots=True)
class FanOutSink:
    """Write every bar to several sinks; one sink's failure stops no other.

    The journal is expected to be first in the list. Order is preserved so the
    durable destination is written before the optional one, which matters if
    the process dies mid-record.
    """

    sinks: Sequence[BarSink]
    #: name -> error, for every sink that raised on the most recent bar.
    failures: dict[str, str] = field(default_factory=dict)

    def record(self, bar: Bar) -> None:
        for sink in self.sinks:
            name = type(sink).__name__
            try:
                sink.record(bar)
            except Exception as exc:
                # A sink that raises is a broken sink, not a reason to lose the
                # bar for every other destination.
                self.failures[name] = f"{type(exc).__name__}: {exc}"

    def drain_failures(self) -> dict[str, str]:
        """Take and clear the failures since the last drain.

        Read by `collect/capture.py` on every poll. Without it, a `FanOutSink`
        swallowing a journal failure meant nothing anywhere reported it: the
        capture counted the bar as recorded and stayed `healthy` while the
        durable record silently stopped being written.
        """
        failures, self.failures = dict(self.failures), {}
        return failures
