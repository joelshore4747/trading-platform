"""Guards for the decision journal and the replay comparison.

The journal's job is to make tomorrow's review possible, so the tests are about
what a review needs: refusals kept as records rather than absences, the account
state that produced each decision, and a restart leaving a visible seam.

The replay test is the important one. It captures a session, replays it through
the ordinary `Backtester`, and requires the fills to match exactly — which is
roadmap step 50's whole mechanism in miniature.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.core.events import Bar, Fill, Order
from trading_platform.core.types import OrderType, Side
from trading_platform.live.journal import (
    AccountState,
    DecisionJournal,
    EventKind,
    JournalEvent,
    decisions_path,
    read_decisions,
    summarise_day,
)
from trading_platform.live.recorder import JournalRecorder
from trading_platform.live.replay import compare, replay_feed

START = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
MARKET, SYMBOL = "um", "BTCUSDT"


@pytest.fixture
def root() -> Path:
    path = Path(tempfile.mkdtemp(prefix="journal-test-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def account(equity: str = "100000") -> AccountState:
    return AccountState(
        cash=Decimal(equity),
        equity=Decimal(equity),
        margin_reserved=Decimal("0"),
        free_cash=Decimal(equity),
    )


def an_order(symbol: str = SYMBOL) -> Order:
    return Order(
        order_id="o1",
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal("0.4"),
        order_type=OrderType.MARKET,
    )


# -- the journal --------------------------------------------------------------


def test_an_event_round_trips_through_its_line(root: Path) -> None:
    event = JournalEvent(START, EventKind.FILL, "s1", SYMBOL, {"price": "100.25"}, account())
    assert JournalEvent.from_line(event.as_line()) == event


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        JournalEvent(datetime(2026, 3, 4), EventKind.NOTE, "s1")


def test_a_journal_without_a_session_is_refused(root: Path) -> None:
    """An unattributed record cannot be read across a restart."""
    with pytest.raises(ValueError, match="session id"):
        DecisionJournal(root, "")


def test_refusals_are_recorded_not_merely_absent(root: Path) -> None:
    """The distinction the whole module exists for.

    Three fills and four hundred refusals is a completely different day from
    three fills. Keeping only fills makes them indistinguishable in the archive.
    """
    with DecisionJournal(root, "s1") as journal:
        journal.order(an_order(), START, account())
        journal.rejection(START, SYMBOL, "margin", "0.98 of equity committed", account())
        journal.rejection(START, SYMBOL, "margin", "0.98 of equity committed", account())
        journal.rejection(START, SYMBOL, "order_rate", "10 orders examined", account())

    summary = summarise_day(read_decisions(decisions_path(root, START)))
    assert summary.counts[str(EventKind.REJECTION)] == 3
    assert summary.rejections_by_reason == {"margin": 2, "order_rate": 1}
    assert summary.intent_refused == 3.0
    assert "describe the risk layer, not the strategy" in summary.report()


def test_every_event_carries_the_state_that_produced_it(root: Path) -> None:
    """ "Nearest snapshot" is the approximation that makes a post-mortem argue
    with itself, so state travels on the event rather than being sampled."""
    with DecisionJournal(root, "s1") as journal:
        journal.order(an_order(), START, account("100000"))

    [event] = read_decisions(decisions_path(root, START))
    assert event.account is not None
    assert event.account.equity == Decimal("100000")
    assert isinstance(event.account.equity, Decimal)


def test_a_restart_inside_a_day_leaves_a_visible_seam(root: Path) -> None:
    """A record that silently heals a restart claims a continuity it never had."""
    with DecisionJournal(root, "morning") as journal:
        journal.mark(START, account("100000"))
    with DecisionJournal(root, "afternoon") as journal:
        journal.mark(START + timedelta(hours=2), account("100500"))

    summary = summarise_day(read_decisions(decisions_path(root, START)))
    assert summary.sessions == ("morning", "afternoon")
    assert "RESTARTED" in summary.report()
    assert summary.equity_change == Decimal("500")


def test_a_single_session_day_reports_no_restart(root: Path) -> None:
    """Negative control: the seam warning must not fire on every day."""
    with DecisionJournal(root, "only") as journal:
        journal.mark(START, account())
    summary = summarise_day(read_decisions(decisions_path(root, START)))
    assert "RESTARTED" not in summary.report()


def test_a_halt_is_surfaced_in_the_summary(root: Path) -> None:
    with DecisionJournal(root, "s1") as journal:
        journal.halt(START, "max drawdown breached", account())
    summary = summarise_day(read_decisions(decisions_path(root, START)))
    assert summary.halts == 1
    assert "HALTED" in summary.report()


def test_the_journal_splits_on_the_utc_day(root: Path) -> None:
    with DecisionJournal(root, "s1") as journal:
        journal.mark(datetime(2026, 3, 4, 23, 59, tzinfo=UTC), account())
        journal.mark(datetime(2026, 3, 5, 0, 1, tzinfo=UTC), account())

    assert decisions_path(root, datetime(2026, 3, 4, tzinfo=UTC)).is_file()
    assert decisions_path(root, datetime(2026, 3, 5, tzinfo=UTC)).is_file()


def test_an_empty_day_summarises_without_raising() -> None:
    summary = summarise_day([])
    assert summary.opening_equity is None
    assert summary.intent_refused is None


def test_a_fill_records_its_costs(root: Path) -> None:
    """Commission and slippage are the difference between a result and a
    result that survived costs."""
    fill = Fill(
        order_id="o1",
        symbol=SYMBOL,
        timestamp=START,
        side=Side.BUY,
        quantity=Decimal("0.4"),
        price=Decimal("100.25"),
        commission=Decimal("0.04"),
        slippage=Decimal("0.01"),
    )
    with DecisionJournal(root, "s1") as journal:
        journal.fill(fill, account())

    [event] = read_decisions(decisions_path(root, START))
    assert event.detail["commission"] == "0.04"
    assert event.detail["slippage"] == "0.01"


# -- replay and divergence ----------------------------------------------------


def make_bar(minute: int, close: str) -> Bar:
    return Bar(
        symbol=SYMBOL,
        timestamp=START + timedelta(minutes=minute),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal("10"),
    )


def test_a_capture_replays_into_a_feed_the_engine_accepts(root: Path) -> None:
    with JournalRecorder(root, MARKET) as rec:
        for minute in range(5):
            rec.record(make_bar(minute, "100"))

    feed = replay_feed(root, MARKET, [SYMBOL])
    bars = list(feed)
    assert feed.symbols == (SYMBOL,)
    assert [b.timestamp for b in bars] == sorted(b.timestamp for b in bars)
    assert len(bars) == 5


def test_replay_reads_what_arrived_not_the_cleaned_version(root: Path) -> None:
    """A reconnect's duplicate is dropped once, and only exact duplicates.

    Replaying the compacted Parquet instead would compare the live run against a
    *cleaned* copy of its own input and credit the cleaning with the agreement.
    """
    with JournalRecorder(root, MARKET) as rec:
        for minute in range(3):
            rec.record(make_bar(minute, "100"))
        rec.record(make_bar(2, "100"))  # the reconnect replay

    assert len(list(replay_feed(root, MARKET, [SYMBOL]))) == 3


def test_replay_honours_a_window(root: Path) -> None:
    with JournalRecorder(root, MARKET) as rec:
        for minute in range(10):
            rec.record(make_bar(minute, "100"))

    windowed = list(
        replay_feed(
            root,
            MARKET,
            [SYMBOL],
            start=START + timedelta(minutes=3),
            end=START + timedelta(minutes=6),
        )
    )
    assert [b.timestamp.minute for b in windowed] == [3, 4, 5, 6]


def a_fill(minute: int, price: str, quantity: str = "1") -> Fill:
    return Fill(
        order_id=f"o{minute}",
        symbol=SYMBOL,
        timestamp=START + timedelta(minutes=minute),
        side=Side.BUY,
        quantity=Decimal(quantity),
        price=Decimal(price),
    )


def test_identical_fill_sequences_agree() -> None:
    fills = [a_fill(1, "100"), a_fill(2, "101")]
    report = compare(fills, list(fills))
    assert report.agrees
    assert "AGREES" in report.report()
    assert "does not say either is" in report.report(), "a clean run must not overclaim"


def test_the_first_difference_is_reported_and_the_rest_are_not() -> None:
    """Fills chain through the portfolio, so counting differences invites a
    search for many causes where there is one."""
    live = [a_fill(1, "100"), a_fill(2, "101"), a_fill(3, "102")]
    replayed = [a_fill(1, "100"), a_fill(2, "999"), a_fill(3, "888")]

    report = compare(live, replayed)
    assert not report.agrees
    assert report.first_difference is not None
    assert report.first_difference.index == 1
    assert report.first_difference.field == "price"
    assert "999" in report.report()
    assert "888" not in report.report(), "only the first difference is shown"


def test_a_missing_fill_diverges_even_when_the_shared_prefix_matches() -> None:
    live = [a_fill(1, "100"), a_fill(2, "101")]
    report = compare(live, live[:1])
    assert not report.agrees
    assert report.first_difference is not None
    assert report.first_difference.field == "presence"


def test_price_tolerance_defaults_to_exact() -> None:
    """A replay through the same engine is deterministic, so any difference is
    a finding. A tolerance is a modelling assumption and must be stated."""
    live = [a_fill(1, "100.00")]
    replayed = [a_fill(1, "100.01")]
    assert not compare(live, replayed).agrees
    assert compare(live, replayed, price_tolerance=Decimal("0.05")).agrees
