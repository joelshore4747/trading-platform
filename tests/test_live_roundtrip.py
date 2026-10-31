"""The round trip: capture a session, replay it, require identical fills.

Roadmap step 50 in miniature. Its exit criterion is thirty days of paper trading
with no unexplained divergence from the backtest of the same period, and **any
divergence is a bug in one of them**. That comparison is only possible if a
captured session can be fed back through the ordinary `Backtester` — so this
test exercises the whole Phase 2 chain end to end:

    venue -> LiveFeed -> JournalRecorder -> disk
                      -> Backtester -> live fills
    disk  -> replay_feed -> Backtester -> replayed fills
    compare(live, replayed) must AGREE

It is deliberately built on the *real* engine and a real strategy rather than
stubs. A round trip that agrees because both sides did nothing would pass while
proving nothing, so the test asserts the strategy actually traded first.
"""

from __future__ import annotations

import math
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.core.events import Bar
from trading_platform.core.instruments import crypto_spot
from trading_platform.engine.backtester import Backtester
from trading_platform.live.recorder import JournalRecorder
from trading_platform.live.replay import compare, replay_feed
from trading_platform.live.stream import LiveFeed
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.examples.sma_crossover import SMACrossover

START = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
MARKET, SYMBOL = "um", "BTCUSDT"


@pytest.fixture
def root() -> Path:
    path = Path(tempfile.mkdtemp(prefix="roundtrip-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


class FrozenClock:
    """Far past every bar, so the reorder window never withholds one."""

    def __call__(self) -> datetime:
        return START + timedelta(days=365)


def session_bars(count: int = 120) -> list[Bar]:
    """A deterministic wave, so the crossover actually crosses.

    No randomness: a round-trip test whose input differs between runs cannot
    distinguish a replay bug from a generator one.
    """
    bars = []
    for i in range(count):
        price = Decimal(str(round(100 + 8 * math.sin(i / 7.0), 2)))
        bars.append(
            Bar(
                symbol=SYMBOL,
                timestamp=START + timedelta(minutes=i),
                open=price,
                high=(price + Decimal("0.5")),
                low=(price - Decimal("0.5")),
                close=price,
                volume=Decimal("100"),
            )
        )
    return bars


def a_backtester() -> Backtester:
    """A fresh engine AND a fresh RiskManager, as the harness requires.

    `RiskManager` carries peak-equity state, so a shared instance would leak a
    drawdown halt from the live leg into the replay and manufacture exactly the
    divergence this test exists to detect.
    """
    return Backtester(
        instruments={SYMBOL: crypto_spot(SYMBOL)},
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=RiskLimits()),
    )


def test_a_captured_session_replays_to_identical_fills(root: Path) -> None:
    bars = session_bars()

    # --- live leg: the venue pushes, the recorder writes, the engine consumes
    with JournalRecorder(root, MARKET) as recorder:
        feed = LiveFeed(
            [SYMBOL],
            reorder_window=timedelta(0),
            max_buffered=10_000,
            recorder=recorder,
            now=FrozenClock(),
        )
        for bar in bars:
            assert feed.publish(bar), "the venue's bars must all be accepted"
        feed.stop()
        live = a_backtester().run(SMACrossover(SYMBOL, fast=3, slow=12), feed)

    assert feed.stats.is_complete, f"the stream lost bars: {feed.stats.report()}"
    assert feed.stats.emitted == len(bars)
    # A round trip that agrees because nothing happened proves nothing.
    assert len(live.fills) >= 5, f"the strategy barely traded ({len(live.fills)} fills)"

    # --- replay leg: same strategy, same engine, bars read back off disk
    replayed = a_backtester().run(
        SMACrossover(SYMBOL, fast=3, slow=12), replay_feed(root, MARKET, [SYMBOL])
    )

    report = compare(live.fills, replayed.fills)
    assert report.agrees, report.report()
    assert live.ending_equity == replayed.ending_equity


def test_the_round_trip_would_notice_a_difference(root: Path) -> None:
    """The control for the test above.

    An agreement is only evidence if disagreement was reachable. Replaying with
    different parameters must diverge — otherwise the comparison above could be
    passing because `compare` cannot fail.
    """
    bars = session_bars()
    with JournalRecorder(root, MARKET) as recorder:
        feed = LiveFeed([SYMBOL], reorder_window=timedelta(0), recorder=recorder, now=FrozenClock())
        for bar in bars:
            feed.publish(bar)
        feed.stop()
        live = a_backtester().run(SMACrossover(SYMBOL, fast=3, slow=12), feed)

    different = a_backtester().run(
        SMACrossover(SYMBOL, fast=5, slow=30), replay_feed(root, MARKET, [SYMBOL])
    )
    assert not compare(live.fills, different.fills).agrees


def test_a_gap_in_the_capture_survives_the_round_trip(root: Path) -> None:
    """A hole must replay as a hole. Filling it is the fiction the vendor
    loader refuses for the same reason."""
    bars = [b for b in session_bars(60) if not 20 <= b.timestamp.minute % 60 < 30]

    with JournalRecorder(root, MARKET) as recorder:
        feed = LiveFeed(
            [SYMBOL],
            reorder_window=timedelta(0),
            expected_interval=timedelta(minutes=1),
            recorder=recorder,
            now=FrozenClock(),
        )
        for bar in bars:
            feed.publish(bar)
        feed.stop()
        live = a_backtester().run(SMACrossover(SYMBOL, fast=3, slow=12), feed)

    assert feed.stats.gaps >= 1, "the capture should have noticed the hole"

    replayed_bars = list(replay_feed(root, MARKET, [SYMBOL]))
    assert len(replayed_bars) == len(bars), "replay invented or dropped bars"

    replayed = a_backtester().run(
        SMACrossover(SYMBOL, fast=3, slow=12), replay_feed(root, MARKET, [SYMBOL])
    )
    assert compare(live.fills, replayed.fills).agrees
