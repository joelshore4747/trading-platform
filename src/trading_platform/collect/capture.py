"""Run many independent captures of the same markets at once.

The reason for several connections is **not** redundancy. Two feeds that agree
tell you nothing you did not already know from one. Two that disagree have
found something: a bad print, a stale connection, a venue outage, a vendor
quietly serving delayed data. `collect/reconcile.py` is where that value is
extracted; this module's only job is to get every source's own view onto disk
without contaminating it with any other source's.

So the rules here are narrow and strict:

**No merging, ever.** Each capture writes under its own source id. Two sources
covering the same symbol produce two rows per instant, by primary key. A
function that picked one would be making a decision about who to believe, and
that decision belongs in a reconciler a human reads, not in a collector at 3am.

**One capture's failure is its own.** A source that starts erroring keeps
erroring in its own thread and its own counters; the others do not notice. A
collector that dies because one vendor 500s is a collector that loses every
vendor's data.

**Nothing publishes an unclosed bar.** `live/polling.py` holds that rule; this
module calls it and does not re-implement it.

**The journal is the durable record.** Each capture writes to a `FanOutSink`
whose first sink is the JSONL journal — a filesystem append — and whose second
is Postgres. A database outage costs rows in the store and no data at all; the
count of what the store is missing is on `StoreSink.stats.dropped`, and
`scripts/journal_to_store.py` puts them back.

A fan-out swallows its sinks' failures so that one cannot take down another,
which means the capture has to DRAIN them or a broken journal is invisible —
measured at 61 of 80 symbol-days silently lost with the capture still reporting
`ok`. `CaptureStats.sink_failures` is that drain.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from trading_platform.core.events import Bar
from trading_platform.data.intervals import interval_ms
from trading_platform.live.polling import due_bars
from trading_platform.live.stream import BarSink

__all__ = ["BarFetcher", "CaptureStats", "Collector", "SourceCapture"]


@runtime_checkable
class BarFetcher(Protocol):
    """A client that can answer "bars for these symbols between these times".

    Deliberately smaller than `MarketDataConnector`: a capture needs nothing
    but this, and a protocol a fake can satisfy in four lines is a protocol
    the offline tests can actually exercise.
    """

    @property
    def venue(self) -> str: ...

    def fetch_bars(
        self,
        symbols: Sequence[str],
        interval: str,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Bar]: ...


@dataclass(slots=True)
class CaptureStats:
    """One source's own numbers. Never combined with another source's."""

    polls: int = 0
    fetched: int = 0
    recorded: int = 0
    #: Bars whose close had not passed at poll time. Not lost — the next poll
    #: after their close carries them again.
    withheld_open: int = 0
    #: Bars an overlapping poll window re-delivered unchanged. Expected.
    already_seen: int = 0
    #: Instants the vendor re-sent with DIFFERENT values. Passed through to the
    #: journal, which appends both, and to the store, which refuses to merge
    #: them and records a conflict. A rising count is a vendor whose prints are
    #: not final, which is a fact about that feed worth knowing.
    revised: int = 0
    #: Bars for a symbol this capture never asked for. Recorded nowhere: a
    #: vendor answering a request for AAPL with TSLA is wrong about something,
    #: and quietly filing it under this source would attribute it to a request
    #: that was never made.
    unrequested: int = 0
    #: Bars a sink in the fan-out failed on. The journal is the durable record,
    #: so a journal failure that nobody surfaces is the worst kind: measured at
    #: 61 of 80 symbol-days lost with the capture still reporting `ok`
    #: (adversarial review, 2026-09-04).
    sink_failures: int = 0
    errors: int = 0
    #: Consecutive failed polls. One is a hiccup; a rising number is an outage,
    #: and it is the number the operations page leads with.
    consecutive_errors: int = 0
    last_error: str = ""
    last_poll_at: datetime | None = None
    #: Close time of the newest bar recorded. `last_poll_at - last_bar_at` is
    #: how far behind the market this source is.
    last_bar_at: datetime | None = None

    @property
    def healthy(self) -> bool:
        return self.consecutive_errors == 0 and self.sink_failures == 0

    def counters(self) -> dict[str, int]:
        """The numbers alone, for the heartbeat row a monitor reads.

        `report()` is for a person at the end of a run. This is for the database
        DURING one, which is the only time the numbers can still change anything
        (T-f280e8f0).
        """
        return {
            "polls": self.polls,
            "fetched": self.fetched,
            "recorded": self.recorded,
            "withheld_open": self.withheld_open,
            "already_seen": self.already_seen,
            "revised": self.revised,
            "unrequested": self.unrequested,
            "sink_failures": self.sink_failures,
            "errors": self.errors,
            "consecutive_errors": self.consecutive_errors,
        }

    def report(self) -> str:
        state = "ok" if self.healthy else f"FAILING ({self.consecutive_errors} in a row)"
        lag = ""
        if self.last_poll_at is not None and self.last_bar_at is not None:
            lag = f", behind by {self.last_poll_at - self.last_bar_at}"
        line = (
            f"{state}: polls {self.polls}, fetched {self.fetched:,}, "
            f"recorded {self.recorded:,}, open withheld {self.withheld_open:,}, "
            f"repeats {self.already_seen:,}, revised {self.revised:,}, "
            f"unrequested {self.unrequested:,}, errors {self.errors}, "
            f"sink failures {self.sink_failures}{lag}"
        )
        return line if not self.last_error else f"{line}\n    last error: {self.last_error}"


@dataclass(slots=True)
class SourceCapture:
    """One source, polling on its own schedule into its own sink.

    Distinct from `live/alpaca.py`'s `AlpacaPoller`, which publishes into a
    `LiveFeed` for a strategy to consume: that path must refuse a late bar to
    keep the engine's ordering guarantee. This path is the archive, where a
    late bar is still a bar and is written. Same rule about unclosed bars,
    opposite rule about late ones, and the difference is deliberate.
    """

    name: str
    fetcher: BarFetcher
    sink: BarSink
    symbols: Sequence[str]
    interval: str
    poll_every: timedelta = timedelta(seconds=15)
    #: How far back the first poll reaches. Later polls resume from the newest
    #: close already recorded, so a restart inside this window loses nothing.
    lookback: timedelta = timedelta(hours=2)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    stats: CaptureStats = field(default_factory=CaptureStats)
    #: Called after every poll with `stats.counters()`. `None` means nobody is
    #: watching, which is how this shipped: the counters were printed at exit by
    #: a process that runs for a month (T-f280e8f0).
    heartbeat: Callable[[Mapping[str, int]], None] | None = None
    #: The last bar recorded per symbol, whole. Keeping only the timestamp made
    #: a vendor's revision indistinguishable from an ordinary repeat, so
    #: revisions were silently discarded.
    _last_close: dict[str, Bar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a capture needs a name; it is what its rows are attributed to")
        if not self.symbols:
            raise ValueError(f"capture {self.name!r} has no symbols")
        interval_ms(self.interval)  # raises on a made-up interval, at construction
        if self.poll_every <= timedelta(0):
            raise ValueError(f"capture {self.name!r}: poll_every must be positive")
        if self.lookback <= timedelta(0):
            raise ValueError(f"capture {self.name!r}: lookback must be positive")

    @property
    def venue(self) -> str:
        return self.fetcher.venue

    def poll_once(self) -> int:
        """One fetch and record. Returns bars recorded. Never raises.

        A capture that raised would take down its thread and stop capturing,
        which is the failure this whole module exists to avoid. Every failure
        is counted and named instead, and `consecutive_errors` is what says
        whether it is a hiccup or an outage.
        """
        now = self.clock()
        self.stats.polls += 1
        self.stats.last_poll_at = now
        since = min(
            (bar.timestamp for bar in self._last_close.values()),
            default=now - self.lookback,
        )
        try:
            bars = self.fetcher.fetch_bars(self.symbols, self.interval, since, now)
        except Exception as exc:
            self._failed(exc, "fetch")
            return 0

        # Everything below is inside a try as well. A fetcher returning
        # something that is not a list of bars, or a bar with a naive
        # timestamp, used to raise out of `poll_once` and kill the capture's
        # thread — and `Collector.run` never noticed, so the dead source went
        # on reporting `ok` and `healthy` forever.
        try:
            self.stats.fetched += len(bars)
            wanted = set(self.symbols)
            asked_for = [b for b in bars if b.symbol in wanted]
            self.stats.unrequested += len(bars) - len(asked_for)
            decision = due_bars(asked_for, now, self._last_close)
        except Exception as exc:
            self._failed(exc, "response")
            return 0
        self.stats.withheld_open += decision.withheld_open
        self.stats.already_seen += decision.already_seen
        self.stats.revised += decision.revised

        recorded = 0
        for bar in decision.due:
            try:
                self.sink.record(bar)
            except Exception as exc:
                # A sink that raises has broken its contract (`FanOutSink` and
                # `StoreSink` both swallow). Counted, and the poll continues:
                # one unwritable bar is not a reason to lose the rest.
                self._failed(exc, f"sink {type(self.sink).__name__}")
                continue
            recorded += 1
            self._last_close[bar.symbol] = bar
            if self.stats.last_bar_at is None or bar.timestamp > self.stats.last_bar_at:
                self.stats.last_bar_at = bar.timestamp

        # A sink inside a fan-out swallows its own failures, so nothing above
        # sees them. Drained here, or a broken journal is invisible.
        drain = getattr(self.sink, "drain_failures", None)
        if callable(drain):
            for name, error in drain().items():
                self.stats.sink_failures += 1
                self.stats.last_error = f"sink {name}: {error}"

        self.stats.recorded += recorded
        if recorded or not decision.due:
            self.stats.consecutive_errors = 0
        return recorded

    def _failed(self, exc: Exception, where: str) -> None:
        self.stats.errors += 1
        self.stats.consecutive_errors += 1
        self.stats.last_error = f"{where}: {type(exc).__name__}: {exc}"

    def run(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set. Sleeps in short slices so stop is prompt.

        `poll_once` does not raise, but this catches anyway: a capture thread
        that dies leaves a source that looks alive in every counter, and a
        silent dead feed is the failure this module is most concerned with.
        """
        while not stop.is_set():
            try:
                self.poll_once()
            except BaseException as exc:  # pragma: no cover - poll_once swallows
                self._failed(
                    exc if isinstance(exc, Exception) else RuntimeError(str(exc)), "poll loop"
                )
                if not isinstance(exc, Exception):
                    raise
            self._beat()
            self._wait(stop)

    def _beat(self) -> None:
        """Report the counters, and never let reporting them stop the capture.

        Swallowed on purpose. A heartbeat that raises kills the thread it was
        added to observe, and the observer killing the observed is the R06
        defect — the account tracker crashing the session it was watching. It is
        counted as a sink failure so a heartbeat that never lands is itself
        visible in the next one that does.
        """
        if self.heartbeat is None:
            return
        try:
            self.heartbeat(self.stats.counters())
        except Exception as exc:
            self.stats.sink_failures += 1
            self.stats.last_error = f"heartbeat: {type(exc).__name__}: {exc}"

    def _wait(self, stop: threading.Event) -> None:
        remaining = self.poll_every.total_seconds()
        while remaining > 0 and not stop.is_set():
            slice_s = min(0.5, remaining)
            self.sleep(slice_s)
            remaining -= slice_s


@dataclass(slots=True)
class Collector:
    """N captures, N threads, one stop signal, and no shared state between them.

    Several captures may cover the same symbols on purpose — that is the
    "hundreds of connections" case — and may even be two connections to the
    same vendor. Two captures writing under the *same* store source name are
    deduplicated by the `bars` primary key and cost nothing; two under
    *different* names are two opinions, which is what the reconciler wants.
    """

    captures: Sequence[SourceCapture]

    def __post_init__(self) -> None:
        if not self.captures:
            raise ValueError("a collector with no captures would run silently forever")
        names = [c.name for c in self.captures]
        if len(set(names)) != len(names):
            duplicated = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(
                f"capture names must be distinct so their counters can be told apart; "
                f"repeated: {duplicated}"
            )

    def run(self, stop: threading.Event, *, join_timeout: float = 30.0) -> dict[str, CaptureStats]:
        """Run every capture until ``stop``, then return each one's own stats.

        A thread that will not join within ``join_timeout`` is reported rather
        than waited on forever; the process exiting is better than a collector
        that cannot be stopped.
        """
        threads = [
            threading.Thread(target=c.run, args=(stop,), name=f"capture-{c.name}", daemon=True)
            for c in self.captures
        ]
        for thread in threads:
            thread.start()
        try:
            while not stop.wait(timeout=1.0):
                self._notice_dead_threads(threads)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=join_timeout)
        return {c.name: c.stats for c in self.captures}

    def _notice_dead_threads(self, threads: Sequence[threading.Thread]) -> None:
        """A capture whose thread has ended before the stop signal is not healthy.

        Without this a dead thread is invisible: its counters freeze at whatever
        they last were, `healthy` stays True, and the collector reports a source
        that has not fetched anything for hours as fine.
        """
        for capture, thread in zip(self.captures, threads, strict=True):
            if not thread.is_alive() and not capture.stats.last_error.startswith("thread died"):
                capture.stats.errors += 1
                capture.stats.consecutive_errors += 1
                capture.stats.last_error = (
                    "thread died before the collector was stopped; this source has "
                    "captured nothing since"
                )

    def report(self) -> str:
        lines = []
        for capture in self.captures:
            lines.append(f"  {capture.name} ({capture.venue}, {capture.interval})")
            lines.append(f"    {capture.stats.report()}")
        unhealthy = [c.name for c in self.captures if not c.stats.healthy]
        if unhealthy:
            lines.append(f"  DEGRADED — failing sources: {', '.join(sorted(unhealthy))}")
        return "\n".join(lines)
