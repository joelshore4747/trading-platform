"""The collector: many sources at once, and what must survive each of them failing.

Everything here runs offline. No venue is reachable from this sandbox, so the
transports are fakes — which is the right shape for these tests anyway: what is
being tested is the collector's behaviour when a feed lies, stalls or dies, and
a real feed will not do those things on demand.

What is NOT tested here, and is therefore UNVERIFIED: that any real vendor's
response parses, and that two real feeds captured concurrently agree. Both need
network. `docs/RUNBOOK.md` says how to run that on a machine that has it.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trading_platform.collect.capture import CaptureStats, Collector, SourceCapture
from trading_platform.core.events import Bar
from trading_platform.data.intervals import UnknownIntervalError
from trading_platform.live.polling import due_bars
from trading_platform.store.bars import BarConflict, Source
from trading_platform.store.sink import FanOutSink, SinkStats, StoreSink

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def bar(minute: int, symbol: str = "BTCUSDT", close: str = "100.50") -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=T0 + timedelta(minutes=minute),
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal(close),
        volume=Decimal("1.5"),
    )


class ListSink:
    def __init__(self) -> None:
        self.bars: list[Bar] = []

    def record(self, b: Bar) -> None:
        self.bars.append(b)


class AngrySink:
    def __init__(self) -> None:
        self.attempts = 0

    def record(self, b: Bar) -> None:
        self.attempts += 1
        raise RuntimeError("the disk is full")


class FakeFetcher:
    """Returns whatever it was handed, or raises whatever it was handed."""

    def __init__(self, *responses: Any, venue: str = "fake") -> None:
        self._responses = list(responses)
        self._venue = venue
        self.calls: list[tuple[Any, ...]] = []

    @property
    def venue(self) -> str:
        return self._venue

    def fetch_bars(
        self,
        symbols: Any,
        interval: str,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Bar]:
        self.calls.append((tuple(symbols), interval, start, end))
        response = self._responses.pop(0) if self._responses else []
        if isinstance(response, Exception):
            raise response
        return list(response)


# -- the rule every poller shares --------------------------------------------------------


def test_a_bar_whose_close_has_not_passed_is_withheld() -> None:
    """The bar in progress. Its `close` is the last trade so far, not the close.
    Publishing it hands a strategy a price that will still change.

    Mutation: `if bar.timestamp >= now` or dropping the check entirely — the
    second assertion goes red, and so does the boundary test below."""
    decision = due_bars([bar(1), bar(2)], now=T0 + timedelta(minutes=1), last_seen={})
    assert [b.timestamp for b in decision.due] == [T0 + timedelta(minutes=1)]
    assert decision.withheld_open == 1


def test_a_bar_stamped_exactly_now_has_closed_and_is_due() -> None:
    """The boundary, in the direction that matters: a close-stamped bar at
    exactly `now` HAS closed. Withholding it would delay every bar by a poll.

    Mutation: `bar.timestamp >= now` — red."""
    decision = due_bars([bar(1)], now=T0 + timedelta(minutes=1), last_seen={})
    assert len(decision.due) == 1 and decision.withheld_open == 0


def test_an_overlapping_poll_window_repeats_bars_and_that_is_not_an_error() -> None:
    """Poll windows overlap on purpose so a restart loses nothing. The repeat is
    counted, not published twice. Mutation: drop the `last_close` check — the
    `already_seen` assertion goes red and the store would see a duplicate."""
    last = {"BTCUSDT": bar(1)}
    decision = due_bars([bar(1), bar(2)], now=T0 + timedelta(minutes=5), last_seen=last)
    assert [b.timestamp for b in decision.due] == [T0 + timedelta(minutes=2)]
    assert decision.already_seen == 1


def test_the_last_close_is_tracked_per_symbol_not_globally() -> None:
    """One symbol running ahead must not suppress another's bars.

    Mutation: use a single `last_close` for all symbols — ETHUSDT's bar 1
    disappears and this goes red."""
    last = {"BTCUSDT": bar(5)}
    decision = due_bars(
        [bar(1, "ETHUSDT"), bar(6, "BTCUSDT")], now=T0 + timedelta(minutes=10), last_seen=last
    )
    assert {b.symbol for b in decision.due} == {"ETHUSDT", "BTCUSDT"}


def test_a_naive_clock_or_bar_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        due_bars([], now=datetime(2026, 8, 1, 12, 0), last_seen={})  # naive on purpose


# -- one source ---------------------------------------------------------------------------


def capture(fetcher: Any, sink: Any, **over: Any) -> SourceCapture:
    defaults: dict[str, Any] = {
        "name": "fake-1",
        "fetcher": fetcher,
        "sink": sink,
        "symbols": ["BTCUSDT"],
        "interval": "1m",
        "clock": lambda: T0 + timedelta(minutes=10),
        "sleep": lambda _s: None,
    }
    defaults.update(over)
    return SourceCapture(**defaults)


def test_a_capture_records_closed_bars_and_advances_its_watermark() -> None:
    sink = ListSink()
    cap = capture(FakeFetcher([bar(1), bar(2)]), sink)
    assert cap.poll_once() == 2
    assert [b.timestamp for b in sink.bars] == [
        T0 + timedelta(minutes=1),
        T0 + timedelta(minutes=2),
    ]
    assert cap.stats.recorded == 2 and cap.stats.last_bar_at == T0 + timedelta(minutes=2)


def test_the_second_poll_resumes_from_the_last_close_not_from_the_lookback() -> None:
    """Otherwise every poll re-fetches the whole lookback window, which on a
    5-per-minute rate limit is the difference between working and not.

    Mutation: always use `now - lookback` — the second call's `start` would
    equal the first's and this goes red."""
    fetcher = FakeFetcher([bar(1)], [bar(2)])
    cap = capture(fetcher, ListSink(), lookback=timedelta(hours=2))
    cap.poll_once()
    cap.poll_once()
    first_start, second_start = fetcher.calls[0][2], fetcher.calls[1][2]
    assert first_start == T0 + timedelta(minutes=10) - timedelta(hours=2)
    assert second_start == T0 + timedelta(minutes=1)


def test_a_fetch_that_raises_is_counted_and_the_capture_keeps_going() -> None:
    """A capture that raised would take its own thread down, and a collector
    that loses a source to one bad response is a collector that loses data.

    Mutation: let `fetch_bars` errors propagate — `poll_once` raises and every
    assertion after the first goes unreachable."""
    fetcher = FakeFetcher(RuntimeError("HTTP 503"), [bar(1)])
    cap = capture(fetcher, ListSink())
    assert cap.poll_once() == 0
    assert cap.stats.errors == 1 and cap.stats.consecutive_errors == 1
    assert "RuntimeError: HTTP 503" in cap.stats.last_error
    assert not cap.stats.healthy
    assert cap.poll_once() == 1
    assert cap.stats.consecutive_errors == 0 and cap.stats.healthy


def test_a_sink_that_raises_does_not_cost_the_rest_of_the_batch() -> None:
    """`FanOutSink` and `StoreSink` both swallow, so a raising sink has broken
    its contract. It is still not a reason to drop the bars behind it.

    Mutation: `break` instead of `continue` — attempts drops to 1, red."""
    sink = AngrySink()
    cap = capture(FakeFetcher([bar(1), bar(2), bar(3)]), sink)
    assert cap.poll_once() == 0
    assert sink.attempts == 3
    assert cap.stats.errors == 3


def test_a_capture_refuses_an_impossible_configuration_at_construction() -> None:
    fetcher, sink = FakeFetcher(), ListSink()
    with pytest.raises(UnknownIntervalError):
        capture(fetcher, sink, interval="7m")
    with pytest.raises(ValueError, match="no symbols"):
        capture(fetcher, sink, symbols=[])
    with pytest.raises(ValueError, match="poll_every"):
        capture(fetcher, sink, poll_every=timedelta(0))
    with pytest.raises(ValueError, match="needs a name"):
        capture(fetcher, sink, name="")


# -- many sources -------------------------------------------------------------------------


def test_two_captures_of_the_same_symbol_keep_separate_counters() -> None:
    """The point of running several: their numbers must be comparable, which
    means never combined. Mutation: share one `CaptureStats` between captures —
    the two assertions below become equal and this goes red."""
    a = capture(FakeFetcher([bar(1)]), ListSink(), name="alpaca")
    b = capture(FakeFetcher([bar(1), bar(2)]), ListSink(), name="polygon")
    Collector([a, b])
    a.poll_once()
    b.poll_once()
    assert a.stats.recorded == 1
    assert b.stats.recorded == 2


def test_a_collector_refuses_two_captures_with_the_same_name() -> None:
    """Two sets of counters under one name cannot be told apart, and neither can
    their rows in the store."""
    a = capture(FakeFetcher(), ListSink(), name="alpaca")
    b = capture(FakeFetcher(), ListSink(), name="alpaca")
    with pytest.raises(ValueError, match="distinct"):
        Collector([a, b])
    with pytest.raises(ValueError, match="no captures"):
        Collector([])


def test_one_source_failing_does_not_stop_another_from_capturing() -> None:
    """Run for real, on threads, with one source erroring on every poll.

    Mutation: run the captures in one loop on one thread with no try/except —
    the healthy source records nothing and this goes red."""
    healthy_sink = ListSink()
    healthy = capture(
        FakeFetcher([bar(1)], [bar(2)], [bar(3)]),
        healthy_sink,
        name="healthy",
        poll_every=timedelta(seconds=0.001),
    )

    class AlwaysDown:
        """Raises on every poll, however many polls there turn out to be. A
        fixed list of errors would run out and the source would look healthy
        again, which is not the case under test."""

        venue = "fake"

        def fetch_bars(self, symbols: Any, interval: str, start: datetime, end: Any = None) -> Any:
            raise RuntimeError("down")

    broken = capture(
        AlwaysDown(),
        ListSink(),
        name="broken",
        poll_every=timedelta(seconds=0.001),
    )
    stop = threading.Event()
    collector = Collector([healthy, broken])
    runner = threading.Thread(target=lambda: collector.run(stop), daemon=True)
    runner.start()
    # Wait for BOTH: the healthy source having captured everything, and the
    # broken one having actually failed. Stopping as soon as the healthy one
    # finished would let the test pass without the broken source ever polling.
    tick = threading.Event()
    for _ in range(1000):
        if len(healthy_sink.bars) >= 3 and broken.stats.errors > 0:
            break
        tick.wait(0.01)
    stop.set()
    runner.join(timeout=10)

    assert len(healthy_sink.bars) == 3, "the healthy source did not capture"
    assert broken.stats.errors > 0
    assert "broken" in collector.report() and "DEGRADED" in collector.report()


def test_capture_stats_report_says_degraded_rather_than_printing_a_number() -> None:
    stats = CaptureStats(polls=3, errors=2, consecutive_errors=2, last_error="RuntimeError: down")
    assert "FAILING" in stats.report() and "RuntimeError: down" in stats.report()
    assert "ok" in CaptureStats(polls=1).report()


# -- the store sink: a database outage must never cost a bar ------------------------------


class FakeConnection:
    def __init__(self) -> None:
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1


SOURCE = Source(id=1, name="fake", kind="venue", venue="fake", description="")


def store_sink(
    monkeypatch: pytest.MonkeyPatch, behaviour: Any, **over: Any
) -> tuple[StoreSink, list[Any]]:
    calls: list[Any] = []

    def fake_write(connection: Any, source: Any, run_id: int, **kwargs: Any) -> int:
        calls.append(kwargs["bars"])
        result = behaviour(kwargs["bars"]) if callable(behaviour) else behaviour
        if isinstance(result, Exception):
            raise result
        return int(result)

    monkeypatch.setattr("trading_platform.store.sink.write_bars", fake_write)
    defaults: dict[str, Any] = {
        "connection": FakeConnection(),
        "source": SOURCE,
        "run_id": 1,
        "market": "um",
        "interval": "1m",
        "batch_size": 2,
    }
    defaults.update(over)
    return StoreSink(**defaults), calls


def test_the_store_being_down_never_raises_at_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The journal is the durable record; the store is the second destination.
    A second destination that can take the first one down is worse than none.

    Mutation: let the exception out of `flush` — `record` raises, the capture
    counts it as a sink error, and the bar never reaches the journal."""
    sink, _ = store_sink(monkeypatch, OSError("connection refused"))
    sink.record(bar(1))
    sink.record(bar(2))  # batch_size reached: flush attempted and fails
    assert sink.stats.failures == 1
    assert sink.stats.dropped == 2
    assert "OSError: connection refused" in sink.stats.last_error
    assert not sink.stats.healthy


def test_a_failed_flush_discards_its_batch_rather_than_growing_without_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retaining the batch through a long outage grows until the collector dies,
    which loses everything rather than the store's copy.

    Mutation: keep the batch on failure — the second flush would carry four
    bars and `dropped` would be 6, so both assertions go red."""
    sink, calls = store_sink(monkeypatch, OSError("down"))
    for minute in range(1, 5):
        sink.record(bar(minute))
    assert sink.stats.dropped == 4
    assert [len(batch) for batch in calls] == [2, 2]


def test_a_source_contradicting_itself_is_a_finding_not_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`BarConflict` means the feed disagreed with itself. Retrying or coercing
    it would resolve a disagreement silently, which is the one thing the store
    refuses to do. Mutation: catch `BarConflict` in the generic handler — the
    conflict counter stays 0, red."""
    sink, _ = store_sink(monkeypatch, BarConflict("BTCUSDT at 12:01 twice"))
    sink.record(bar(1))
    sink.record(bar(2))
    # Both bars are re-tried one at a time and both contradict, so both are
    # counted and dropped. The salvage path is what makes that two rather than
    # a whole batch lost to one bad print.
    assert sink.stats.conflicts == 2 and sink.stats.failures == 0
    assert sink.stats.dropped == 2


def test_a_quiet_symbol_is_flushed_on_age_not_held_until_the_batch_fills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bar an hour would otherwise sit in memory for 500 hours.

    Mutation: drop the age condition from `record` — nothing flushes and
    `written` stays 0."""
    clock = [T0]
    sink, _ = store_sink(
        monkeypatch,
        1,
        batch_size=500,
        max_age=timedelta(seconds=5),
        now=lambda: clock[0],
    )
    sink.record(bar(1))
    assert sink.stats.written == 0
    clock[0] = T0 + timedelta(seconds=6)
    sink.record(bar(2))
    assert sink.stats.written > 0


def test_bars_already_present_are_counted_as_duplicates_not_as_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`write_bars` returns rows that were NEW. A reconnect replaying the last
    minute is normal; a large duplicate count is a poller re-fetching too much,
    and that is only visible if the two are counted apart."""
    sink, _ = store_sink(monkeypatch, 1, batch_size=2)
    sink.record(bar(1))
    sink.record(bar(2))
    assert sink.stats.written == 1 and sink.stats.duplicate == 1


def test_sink_stats_report_names_the_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    assert SinkStats(accepted=5, written=5).healthy
    degraded = SinkStats(accepted=5, written=3, dropped=2, failures=1, last_error="OSError: x")
    assert "DEGRADED" in degraded.report() and "OSError: x" in degraded.report()


# -- fan-out: one destination's failure is not another's ----------------------------------


def test_one_sink_failing_does_not_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    """The journal is first in the list on purpose: the durable destination is
    written before the optional one.

    Mutation: let the exception out of `FanOutSink.record` — the journal-like
    sink below keeps its bar but the third sink never sees one, red."""
    journal, angry, store = ListSink(), AngrySink(), ListSink()
    fan = FanOutSink([journal, angry, store])
    fan.record(bar(1))
    assert len(journal.bars) == 1
    assert len(store.bars) == 1
    assert "AngrySink" in fan.failures and "the disk is full" in fan.failures["AngrySink"]


# -- what the adversarial review of 2026-09-04 found ---------------------------------------


def test_a_vendor_revising_a_bar_is_not_mistaken_for_an_ordinary_repeat() -> None:
    """Poll windows overlap, so a bar comes back every poll. Comparing only the
    TIMESTAMP made a revision — the same instant with different prices —
    indistinguishable from that, and it was silently discarded. A vendor whose
    prints are not final is a fact about the feed worth knowing.

    Mutation: revert `last_seen` to timestamps only (`bar.timestamp <= seen`) —
    `revised` is 0, `due` is empty, and both assertions go red."""
    original = bar(1, close="100.50")
    revision = bar(1, close="100.75")
    decision = due_bars([revision], now=T0 + timedelta(minutes=5), last_seen={"BTCUSDT": original})
    assert decision.revised == 1
    assert decision.already_seen == 0
    assert decision.due == [revision], "a revision must go through, not be swallowed"


def test_an_unchanged_repeat_is_still_just_a_repeat() -> None:
    """The other half: the overlap re-delivering an identical bar is normal and
    must not start showing up as a revision. Mutation: compare timestamps only
    when deciding `revised` — every repeat becomes a revision, red."""
    decision = due_bars([bar(1)], now=T0 + timedelta(minutes=5), last_seen={"BTCUSDT": bar(1)})
    assert decision.revised == 0 and decision.already_seen == 1 and decision.due == []


def test_a_polls_bars_come_out_sorted_whatever_order_they_arrived_in() -> None:
    """A recorder writes what it is handed, in the order it is handed it, and
    the reorder buffer that would fix that is on the strategy path, not here.

    Mutation: delete the `decision.due.sort(...)` line — red."""
    decision = due_bars(
        [bar(3), bar(1, "ETHUSDT"), bar(2)], now=T0 + timedelta(minutes=10), last_seen={}
    )
    assert [(b.timestamp, b.symbol) for b in decision.due] == sorted(
        (b.timestamp, b.symbol) for b in decision.due
    )


def test_a_fetcher_returning_nonsense_does_not_kill_the_capture() -> None:
    """A dead capture thread is invisible: its counters freeze, `healthy` stays
    True, and the collector reports a source that has fetched nothing for hours
    as fine. Everything after the fetch is inside a try for that reason.

    Mutation: move `len(bars)` and `due_bars(...)` back outside the try — this
    raises out of `poll_once` and the assertions below never run."""

    class Nonsense:
        venue = "fake"

        def fetch_bars(self, symbols: Any, interval: str, start: Any, end: Any = None) -> Any:
            return None  # not a list; `len(None)` raises

    cap = capture(Nonsense(), ListSink())
    assert cap.poll_once() == 0
    assert cap.stats.errors == 1
    assert not cap.stats.healthy
    assert "response" in cap.stats.last_error


def test_a_capture_whose_thread_dies_stops_reporting_healthy() -> None:
    """`poll_once` does not raise, so this needs the thread killed another way.
    The property is the collector noticing at all.

    Mutation: delete `_notice_dead_threads` from `Collector.run` — the dead
    source keeps reporting `ok` and this goes red."""
    cap = capture(FakeFetcher([bar(1)]), ListSink(), poll_every=timedelta(seconds=0.001))
    collector = Collector([cap])
    stop = threading.Event()

    # A thread that ends immediately, standing in for one that died.
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    collector._notice_dead_threads([dead])

    assert not cap.stats.healthy
    assert "thread died" in cap.stats.last_error
    stop.set()


def test_a_journal_failure_inside_a_fan_out_reaches_the_captures_counters() -> None:
    """`FanOutSink` swallows so one destination cannot take down another — which
    means nothing above it sees a journal failure unless the capture drains it.
    The journal is the DURABLE record; a silent failure there is the worst kind.

    Mutation: delete the `drain_failures` block from `poll_once` — the capture
    counts the bar as recorded, stays `healthy`, and this goes red."""
    journal, store = AngrySink(), ListSink()
    cap = capture(FakeFetcher([bar(1)]), FanOutSink([journal, store]))
    assert cap.poll_once() == 1  # the store sink still got it
    assert cap.stats.sink_failures == 1
    assert not cap.stats.healthy
    assert "AngrySink" in cap.stats.last_error


def test_draining_the_fan_out_twice_does_not_double_count() -> None:
    fan = FanOutSink([AngrySink()])
    fan.record(bar(1))
    assert list(fan.drain_failures()) == ["AngrySink"]
    assert fan.drain_failures() == {}


def test_one_bad_print_does_not_cost_the_whole_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`write_bars` refuses a whole batch that contradicts the store, by design.
    At a 500-bar batch size that meant one bad print cost up to 499 innocent
    bars — which the capture had already counted as recorded and would never
    re-fetch. The batch is re-sent one bar at a time instead.

    Mutation: return 0 from `flush` on `BarConflict` instead of calling
    `_write_individually` — `written` is 0 and `dropped` is 3, both red."""

    def behaviour(batch: Any) -> Any:
        if len(batch) > 1:
            return BarConflict("minute 2 contradicts the store")
        return BarConflict("contradiction") if batch[0].timestamp.minute == 2 else 1

    sink, _ = store_sink(monkeypatch, behaviour, batch_size=3)
    for minute in (1, 2, 3):
        sink.record(bar(minute))
    assert sink.stats.written == 2, "the innocent bars must land"
    assert sink.stats.conflicts == 1
    assert sink.stats.dropped == 1


def test_a_dead_connection_is_replaced_rather_than_kept_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database restart used to cost the store every subsequent row until
    somebody restarted the collector by hand — and `Restart=always` never fires,
    because the collector does not exit on a sink failure.

    Mutation: delete `_reconnect_if_dead` — `reconnects` stays 0, red."""
    fresh = FakeConnection()

    class Dead(FakeConnection):
        broken = True

    state = {"fail": True}

    def behaviour(batch: Any) -> Any:
        if state["fail"]:
            state["fail"] = False
            return OSError("server closed the connection unexpectedly")
        return len(batch)

    sink, _ = store_sink(
        monkeypatch, behaviour, batch_size=1, connection=Dead(), reconnect=lambda: fresh
    )
    sink.record(bar(1))
    assert sink.stats.reconnects == 1
    assert sink.connection is fresh
    sink.record(bar(2))
    assert sink.stats.written == 1


def test_a_healthy_connection_is_not_thrown_away_on_an_ordinary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnecting on every failure would discard a good connection on every
    bad row. Mutation: drop the `broken or closed` test — red."""
    sink, _ = store_sink(
        monkeypatch,
        OSError("some transient thing"),
        batch_size=1,
        reconnect=lambda: FakeConnection(),
    )
    sink.record(bar(1))
    assert sink.stats.reconnects == 0


def test_a_generic_failure_rolls_back_so_the_connection_stays_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leaving an aborted transaction open makes every later flush fail with
    `InFailedSqlTransaction` over the real cause.

    Mutation: remove the `connection.rollback()` from `_failed` — red."""
    connection = FakeConnection()
    sink, _ = store_sink(monkeypatch, OSError("boom"), batch_size=1, connection=connection)
    sink.record(bar(1))
    assert connection.rollbacks == 1


def test_close_flushes_the_tail_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """At a 500-bar batch size a SIGTERM between flushes would otherwise discard
    everything buffered since the last one — a graceful stop losing more data
    than a crash.

    Mutation: make `close()` return `self.stats` without flushing — `written`
    stays 0, red."""
    sink, _ = store_sink(monkeypatch, 1, batch_size=500)
    sink.record(bar(1))
    assert sink.stats.written == 0
    stats = sink.close()
    assert stats.written == 1


def test_repeated_sink_failures_keep_the_capture_unhealthy() -> None:
    """`consecutive_errors` must not reset while every bar is failing to land.

    Mutation: reset it unconditionally at the end of `poll_once` — the capture
    reports `ok` while recording nothing, red."""
    cap = capture(FakeFetcher([bar(1)], [bar(2)], [bar(3)]), AngrySink())
    for _ in range(3):
        cap.poll_once()
    assert cap.stats.consecutive_errors == 3
    assert not cap.stats.healthy


# -- the heartbeat: the counters, while they can still change something --------------------


def test_the_capture_reports_its_counters_after_every_poll() -> None:
    """`consecutive_errors` and `sink_failures` were printed at exit, by a
    process built to run for a month (T-f280e8f0). They are now written onto the
    open ingest run each poll, which is the only time reading them helps.

    Mutation: drop the `self._beat()` call from `run` — nothing is reported and
    this goes red, while every other test in this file stays green, because the
    counters themselves are still perfectly correct."""
    beats: list[dict[str, int]] = []
    stop = threading.Event()
    capture = SourceCapture(
        name="s",
        fetcher=FakeFetcher([bar(0)], [bar(1)]),
        sink=ListSink(),
        symbols=["BTCUSDT"],
        interval="1m",
        poll_every=timedelta(seconds=1),
        # Stopped from the SLEEP, never from the heartbeat. A loop stopped by
        # the thing under test does not terminate once the thing under test is
        # removed, and a test that hangs is not a test that fails.
        sleep=lambda _s: stop.set(),
        heartbeat=lambda counters: beats.append(dict(counters)),
    )
    # Driven through `run`, not by calling `_beat` directly: calling the private
    # method proves the method works and says nothing about whether the loop
    # uses it, which is the part that had no test.
    capture.run(stop)

    assert beats and beats[-1]["polls"] == 1
    assert set(beats[-1]) >= {"consecutive_errors", "sink_failures", "recorded", "errors"}


def test_a_heartbeat_that_raises_does_not_kill_the_capture_it_watches() -> None:
    """R06 again: the account tracker crashed the session it was observing. An
    observer that can stop the observed is worse than no observer, because the
    thing it was watching now fails in a way nobody predicted.

    Mutation: let `_beat` propagate — the capture thread dies on the first
    heartbeat and `polls` stops rising, red."""

    stop = threading.Event()
    naps = 0

    def explode(counters: Mapping[str, int]) -> None:
        raise RuntimeError("the store went away")

    def nap(_seconds: float) -> None:
        # Two polls, then stop — from the sleep, so that letting `_beat` raise
        # ends the run rather than hanging it.
        nonlocal naps
        naps += 1
        if naps >= 2:
            stop.set()

    capture = SourceCapture(
        name="s",
        fetcher=FakeFetcher([bar(0)], [bar(1)]),
        sink=ListSink(),
        symbols=["BTCUSDT"],
        interval="1m",
        # Half a second, so `_wait` sleeps exactly once per poll: `_wait` slices
        # its wait into half-seconds, so a one-second interval would count two
        # naps within a single poll and stop after one.
        poll_every=timedelta(seconds=0.5),
        sleep=nap,
        heartbeat=explode,
    )
    capture.run(stop)

    assert capture.stats.polls == 2, "the capture kept going"
    assert capture.stats.sink_failures == 2, "and the failed heartbeats are themselves counted"
    assert "the store went away" in capture.stats.last_error
