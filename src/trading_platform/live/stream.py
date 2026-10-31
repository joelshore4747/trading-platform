"""The bridge between a venue's push and the engine's pull.

A backtest feed is pulled: ``for bar in feed`` drives the loop. A live venue
pushes, on its own thread, whenever it feels like it. Every live trading system
has to bridge those two, and the bridge is where the interesting failures live.

Three properties are non-negotiable here, because the engine's correctness rests
on them.

**Ordering.** ``data/feed.py`` guarantees non-decreasing timestamps and the
event loop's whole lookahead defence assumes it. Network reordering is normal,
so bars are held in a short reorder buffer and released in timestamp order. A
bar that arrives after its slot has already been emitted **cannot** be released
without breaking that guarantee, so it is refused and counted — never quietly
emitted out of order, and never used to rewrite history.

**A gap is a gap.** Nothing is interpolated, ever. The same rule the vendor
loader follows for the same reason: a filled hole is a fiction that a strategy
will trade against.

**The record is not the strategy's view.** ``publish`` writes to the recorder
*before* it enqueues, so a slow strategy loop can never cost you data on disk.
If the queue overflows, the strategy misses bars and the counter says so; the
archive stays complete.

The clock is injected. A module that reads the wall clock cannot be tested
deterministically, and ``data/synthetic.py`` already holds that line with a test
asserting it never does.
"""

from __future__ import annotations

import heapq
import itertools
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from trading_platform.core.events import Bar


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BarSink(Protocol):
    """Anything that durably records a bar as it arrives."""

    def record(self, bar: Bar) -> None: ...


@dataclass(slots=True)
class StreamStats:
    """What happened to the stream, in numbers a reader can act on.

    Every counter here exists because the corresponding event is invisible
    otherwise. A live feed that silently drops 3% of its bars looks exactly
    like a quiet market.
    """

    published: int = 0
    emitted: int = 0
    #: Arrived after its slot was already emitted. Releasing it would break the
    #: engine's ordering guarantee, so it is refused.
    refused_late: int = 0
    #: Lost because the consumer could not keep up. The recorder still has them.
    dropped_overflow: int = 0
    #: Observed steps larger than the expected interval, per symbol.
    gaps: int = 0
    #: Largest single gap observed, for reporting alongside the count.
    widest_gap: timedelta | None = None
    reconnects: int = 0

    @property
    def is_complete(self) -> bool:
        """True when nothing was lost or refused. The honest headline."""
        return self.refused_late == 0 and self.dropped_overflow == 0

    def report(self) -> str:
        lines = [
            f"published {self.published}  emitted {self.emitted}",
            f"refused late {self.refused_late}  dropped (overflow) {self.dropped_overflow}",
            f"gaps {self.gaps}" + (f"  widest {self.widest_gap}" if self.widest_gap else ""),
            f"reconnects {self.reconnects}",
        ]
        if not self.is_complete:
            lines.append(
                "INCOMPLETE — the strategy did not see every bar this stream carried. "
                "The recorder's archive is unaffected; the run's decisions are not."
            )
        return "\n".join(lines)


class LiveFeed:
    """A ``DataFeed`` fed by a venue thread rather than a file.

    Satisfies the same protocol the backtester consumes, so a strategy runs
    unchanged in both. That is the point: the moment live needs its own code
    path, paper and backtest stop being comparable and step 50's divergence
    tracking measures two different programs.
    """

    def __init__(
        self,
        symbols: Sequence[str],
        *,
        reorder_window: timedelta = timedelta(seconds=2),
        max_buffered: int = 10_000,
        expected_interval: timedelta | None = None,
        recorder: BarSink | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not symbols:
            raise ValueError("A feed with no symbols cannot be validated against instruments")
        if reorder_window < timedelta(0):
            raise ValueError(f"reorder_window must not be negative, got {reorder_window}")
        if max_buffered < 1:
            raise ValueError(f"max_buffered must be at least 1, got {max_buffered}")

        self._symbols = tuple(dict.fromkeys(symbols))
        self._reorder_window = reorder_window
        self._max_buffered = max_buffered
        self._expected_interval = expected_interval
        self._recorder = recorder
        self._now = now

        self.stats = StreamStats()
        self._buffer: list[tuple[datetime, str, int, Bar]] = []
        self._tiebreak = itertools.count()
        self._last_emitted: datetime | None = None
        self._last_per_symbol: dict[str, datetime] = {}
        self._last_publish_at: datetime | None = None
        self._stopped = False
        self._condition = threading.Condition()

    # -- producer side --------------------------------------------------------

    def publish(self, bar: Bar) -> bool:
        """Accept one bar from the venue. Returns whether the engine will see it.

        Safe to call from the venue's own thread. Recording happens first and
        unconditionally: whatever the strategy loop is doing, the archive gets
        the bar.
        """
        if bar.symbol not in self._symbols:
            raise ValueError(
                f"{bar.symbol} is not one of this feed's symbols {self._symbols}. "
                "A feed must declare its symbols before iteration so the engine can "
                "validate them against its instruments."
            )

        if self._recorder is not None:
            self._recorder.record(bar)

        with self._condition:
            self.stats.published += 1
            self._last_publish_at = self._now()
            self._note_gap(bar)

            if self._last_emitted is not None and bar.timestamp < self._last_emitted:
                # Its slot has gone. Emitting now would hand the engine a
                # backwards step, which is the one thing the feed contract
                # forbids — and the engine's lookahead defence rests on it.
                self.stats.refused_late += 1
                return False

            if len(self._buffer) >= self._max_buffered:
                heapq.heappop(self._buffer)
                self.stats.dropped_overflow += 1

            heapq.heappush(self._buffer, (bar.timestamp, bar.symbol, next(self._tiebreak), bar))
            self._condition.notify()
        return True

    def note_reconnect(self) -> None:
        """Record that the transport dropped and came back.

        Counted rather than hidden: a session with twelve reconnections and no
        gaps is a different object of study from one with neither.
        """
        with self._condition:
            self.stats.reconnects += 1

    def stop(self) -> None:
        """Stop iteration once the buffer drains."""
        with self._condition:
            self._stopped = True
            self._condition.notify_all()

    def _note_gap(self, bar: Bar) -> None:
        """Record, never repair. Called with the lock held."""
        if self._expected_interval is None:
            return
        previous = self._last_per_symbol.get(bar.symbol)
        self._last_per_symbol[bar.symbol] = bar.timestamp
        if previous is None:
            return
        step = bar.timestamp - previous
        if step > self._expected_interval:
            self.stats.gaps += 1
            missing = step - self._expected_interval
            if self.stats.widest_gap is None or missing > self.stats.widest_gap:
                self.stats.widest_gap = missing

    # -- consumer side --------------------------------------------------------

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def seconds_since_publish(self) -> float | None:
        """Staleness. ``None`` before the first bar — not the same as zero."""
        with self._condition:
            if self._last_publish_at is None:
                return None
            return (self._now() - self._last_publish_at).total_seconds()

    def pending(self) -> int:
        with self._condition:
            return len(self._buffer)

    def __iter__(self) -> Iterator[Bar]:
        """Yield bars in timestamp order, blocking until each is due.

        A bar is due once ``reorder_window`` has passed since its timestamp,
        which is the window inside which a late sibling can still overtake it.
        On ``stop()`` the buffer drains in order rather than being discarded —
        the last bars of a session are as real as the first.
        """
        while True:
            with self._condition:
                while True:
                    ready = self._next_due()
                    if ready is not None:
                        break
                    if self._stopped:
                        if not self._buffer:
                            return
                        # Draining: everything left is due by definition.
                        ready = heapq.heappop(self._buffer)[3]
                        break
                    self._condition.wait(timeout=self._wait_hint())

                if self._last_emitted is not None and ready.timestamp < self._last_emitted:
                    # Unreachable while `publish` refuses late bars, and asserted
                    # rather than assumed: this is the invariant the engine's
                    # lookahead guard is built on, and a silent violation here
                    # would look like a working system.
                    raise AssertionError(
                        f"LiveFeed emitted {ready.timestamp} after {self._last_emitted}"
                    )
                self._last_emitted = ready.timestamp
                self.stats.emitted += 1
            yield ready

    def _next_due(self) -> Bar | None:
        """Pop the earliest bar if its reorder window has elapsed. Lock held."""
        if not self._buffer:
            return None
        timestamp = self._buffer[0][0]
        if self._now() - timestamp < self._reorder_window:
            return None
        return heapq.heappop(self._buffer)[3]

    def _wait_hint(self) -> float:
        """How long to sleep before re-checking. Lock held.

        Bounded so a stop() that races with an empty buffer cannot hang, and so
        a feed with nothing to say still wakes often enough to notice.
        """
        if not self._buffer:
            return 0.5
        due_in = (self._buffer[0][0] + self._reorder_window - self._now()).total_seconds()
        return max(0.0, min(due_in, 0.5))
