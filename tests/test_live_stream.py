"""Guards for the push→pull bridge.

The feed is tested against a venue that **misbehaves**, because a venue that
behaves is not the case that breaks a live system. Reordering, lateness,
overflow, reconnection and gaps each get a test, and each is driven by a
controllable clock so the assertions are exact rather than timing-dependent.

The property that outranks the rest: the engine must never receive a backwards
timestamp step. Every other guarantee in the backtester's lookahead defence is
built on that one.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.core.events import Bar
from trading_platform.data.feed import DataFeed
from trading_platform.live.stream import LiveFeed

START = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    """A clock the test drives, so 'two seconds later' costs no wall time."""

    def __init__(self, at: datetime = START) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


def bar(symbol: str, minute: int, close: str = "100") -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=START + timedelta(minutes=minute),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal("1"),
    )


def drain(feed: LiveFeed) -> list[Bar]:
    """Everything the feed will emit, once stopped."""
    feed.stop()
    return list(feed)


def test_a_live_feed_satisfies_the_data_feed_protocol() -> None:
    """A strategy must not be able to tell which kind of feed it is running on."""
    assert isinstance(LiveFeed(["BTCUSDT"], now=FakeClock()), DataFeed)


def test_bars_are_released_in_timestamp_order_not_arrival_order() -> None:
    """The whole reason the reorder buffer exists."""
    clock = FakeClock()
    feed = LiveFeed(["A", "B"], reorder_window=timedelta(seconds=2), now=clock)

    feed.publish(bar("A", 3))
    feed.publish(bar("B", 1))
    feed.publish(bar("A", 2))
    clock.advance(600)  # every slot is now due

    assert [b.timestamp for b in drain(feed)] == [
        START + timedelta(minutes=1),
        START + timedelta(minutes=2),
        START + timedelta(minutes=3),
    ]


def test_a_bar_is_not_released_before_its_reorder_window_elapses() -> None:
    """Releasing early is what lets a late sibling arrive too late."""
    clock = FakeClock(START + timedelta(minutes=5))
    feed = LiveFeed(["A"], reorder_window=timedelta(seconds=30), now=clock)
    feed.publish(bar("A", 5))

    assert feed.pending() == 1
    clock.advance(29)
    assert feed.pending() == 1, "released a bar whose window had not elapsed"

    clock.advance(2)
    assert len(drain(feed)) == 1


def test_a_bar_arriving_after_its_slot_is_refused_and_counted() -> None:
    """The engine's ordering guarantee wins; the late bar is reported.

    Emitting it would hand the backtester a backwards step, which is precisely
    what the lookahead defence forbids. Silently dropping it would leave a hole
    nobody could see. So: refused, counted, and visible in `is_complete`.
    """
    clock = FakeClock()
    feed = LiveFeed(["A"], reorder_window=timedelta(seconds=1), now=clock)

    feed.publish(bar("A", 10))
    # Past the bar's timestamp AND its reorder window. `advance(600)` alone
    # lands the clock exactly on the timestamp, where the window has not
    # elapsed and the feed correctly blocks — which is what it is for.
    clock.advance(600 + 5)
    emitted = next(iter(feed))
    assert emitted.timestamp == START + timedelta(minutes=10)

    accepted = feed.publish(bar("A", 9))
    assert accepted is False
    assert feed.stats.refused_late == 1
    assert not feed.stats.is_complete
    assert "INCOMPLETE" in feed.stats.report()


def test_overflow_drops_the_oldest_and_says_so() -> None:
    """A full queue must not silently look like a quiet market."""
    clock = FakeClock()
    feed = LiveFeed(["A"], reorder_window=timedelta(0), max_buffered=3, now=clock)

    for minute in range(6):
        feed.publish(bar("A", minute))
    clock.advance(600)

    assert feed.stats.dropped_overflow == 3
    assert [b.timestamp.minute for b in drain(feed)] == [3, 4, 5], "kept the newest three"
    assert not feed.stats.is_complete


def test_the_recorder_sees_bars_the_consumer_never_will() -> None:
    """The archive must not be hostage to the strategy loop's speed.

    This is the split that makes overflow survivable: the record is written on
    the producer's thread before the queue is touched, so a slow consumer costs
    decisions, never data.
    """
    recorded: list[Bar] = []

    class Sink:
        def record(self, b: Bar) -> None:
            recorded.append(b)

    clock = FakeClock()
    feed = LiveFeed(["A"], reorder_window=timedelta(0), max_buffered=2, recorder=Sink(), now=clock)
    for minute in range(5):
        feed.publish(bar("A", minute))
    clock.advance(600)

    assert len(recorded) == 5, "every published bar reached the recorder"
    assert len(drain(feed)) == 2, "the consumer saw only what fitted"
    assert feed.stats.dropped_overflow == 3


def test_gaps_are_counted_and_never_filled() -> None:
    clock = FakeClock()
    feed = LiveFeed(
        ["A"], reorder_window=timedelta(0), expected_interval=timedelta(minutes=1), now=clock
    )
    feed.publish(bar("A", 0))
    feed.publish(bar("A", 1))
    feed.publish(bar("A", 9))
    clock.advance(600)

    assert feed.stats.gaps == 1
    # Minutes 2 through 8 never printed: seven bars of MISSING data, not the
    # eight-minute step between the two that did. The gap measures absence, so
    # a stream with no gaps reports zero rather than one interval.
    assert feed.stats.widest_gap == timedelta(minutes=7)
    assert len(drain(feed)) == 3, "the hole is recorded, not interpolated into bars"


def test_a_regular_stream_reports_no_gaps() -> None:
    """The negative control. A counter that only ever increments says nothing."""
    clock = FakeClock()
    feed = LiveFeed(
        ["A"], reorder_window=timedelta(0), expected_interval=timedelta(minutes=1), now=clock
    )
    for minute in range(10):
        feed.publish(bar("A", minute))
    clock.advance(600)

    assert feed.stats.gaps == 0
    assert feed.stats.widest_gap is None
    assert feed.stats.is_complete


def test_reconnections_are_counted() -> None:
    feed = LiveFeed(["A"], now=FakeClock())
    feed.note_reconnect()
    feed.note_reconnect()
    assert feed.stats.reconnects == 2


def test_a_symbol_the_feed_never_declared_is_refused() -> None:
    """`Backtester.run` validates a feed's symbols against its instruments
    before consuming a bar, so an undeclared symbol must fail at the door."""
    feed = LiveFeed(["A"], now=FakeClock())
    with pytest.raises(ValueError, match="not one of this feed's symbols"):
        feed.publish(bar("B", 1))


def test_stopping_drains_rather_than_discards() -> None:
    """The last bars of a session are as real as the first."""
    clock = FakeClock()
    feed = LiveFeed(["A"], reorder_window=timedelta(hours=1), now=clock)
    for minute in range(4):
        feed.publish(bar("A", minute))

    assert feed.pending() == 4, "nothing is due yet"
    assert len(drain(feed)) == 4, "stopping released them anyway"


def test_a_producer_thread_and_a_consumer_thread_agree() -> None:
    """The bridge's actual job, exercised across a real thread boundary."""
    clock = FakeClock(START + timedelta(hours=1))
    feed = LiveFeed(["A"], reorder_window=timedelta(0), max_buffered=1000, now=clock)
    received: list[Bar] = []

    def consume() -> None:
        received.extend(feed)

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    for minute in range(50):
        feed.publish(bar("A", minute))
    feed.stop()
    consumer.join(timeout=10)

    assert not consumer.is_alive(), "the consumer did not finish after stop()"
    assert len(received) == 50
    assert received == sorted(received, key=lambda b: b.timestamp)
    assert feed.stats.is_complete


def test_staleness_is_none_before_the_first_bar() -> None:
    """None is not zero. A feed that has said nothing yet has no staleness."""
    clock = FakeClock()
    feed = LiveFeed(["A"], now=clock)
    assert feed.seconds_since_publish() is None

    feed.publish(bar("A", 0))
    assert feed.seconds_since_publish() == 0.0
    clock.advance(45)
    assert feed.seconds_since_publish() == 45.0


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"symbols": []}, "no symbols"),
        ({"symbols": ["A"], "reorder_window": timedelta(seconds=-1)}, "must not be negative"),
        ({"symbols": ["A"], "max_buffered": 0}, "at least 1"),
    ],
)
def test_impossible_configurations_are_refused_at_construction(
    kwargs: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        LiveFeed(now=FakeClock(), **kwargs)  # type: ignore[arg-type]
