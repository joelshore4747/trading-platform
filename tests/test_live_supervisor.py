"""The paper supervisor: journalled, reconciled on boot, stoppable, cycled.

Mutations these catch: skipping `restore` (restart test sees a flat book);
comparing equity instead of cash in `reconcile` (tamper test passes on a
stale mark); the kill switch checked after the strategy runs (an order slips
through); the daily loss compared against starting cash rather than the
day's open; `mark` not returning the failed cycle (zero test keeps trading).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.core.events import Bar, Fill
from trading_platform.core.instruments import crypto_perpetual, crypto_spot
from trading_platform.core.types import Side
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.engine.backtester import Backtester
from trading_platform.engine.execution import ZERO_COST, CostModel
from trading_platform.live.accounts import AccountCycles, CycleOutcome
from trading_platform.live.journal import EventKind, iter_decision_days, read_decisions
from trading_platform.live.recorder import JournalRecorder
from trading_platform.live.replay import compare, replay_feed
from trading_platform.live.stream import LiveFeed
from trading_platform.live.supervisor import (
    PaperSupervisor,
    ReconciliationError,
    SupervisorConfig,
)
from trading_platform.risk.leverage import LeverageAcknowledgement, LeverageGate
from trading_platform.risk.limits import RiskLimits
from trading_platform.strategy.base import Strategy, StrategyContext

T0 = datetime(2026, 9, 1, tzinfo=UTC)
SYMBOL = "BTCUSDT"
BTC = crypto_spot(SYMBOL)
PERP_SYMBOL = "BTCUSDT-PERP"
PERP = crypto_perpetual(PERP_SYMBOL, initial_margin_rate=Decimal("0.1"))


def bars(prices: list[str], *, start: datetime = T0, symbol: str = SYMBOL) -> list[Bar]:
    out = []
    for i, p in enumerate(prices):
        out.append(
            Bar(
                symbol=symbol,
                timestamp=start + timedelta(days=i),
                open=Decimal(p),
                high=Decimal(p),
                low=Decimal(p),
                close=Decimal(p),
                volume=Decimal("1"),
            )
        )
    return out


class BuyOnce(Strategy):
    name = "buy-once"

    def __init__(self, qty: str = "5", symbol: str = SYMBOL) -> None:
        self.qty = Decimal(qty)
        self.symbol = symbol
        self.seen_positions: list[Decimal] = []
        self.bought = False

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
        self.seen_positions.append(ctx.position(self.symbol))
        if not self.bought:
            ctx.buy(self.symbol, self.qty)
            self.bought = True


class BuyEveryBar(Strategy):
    name = "buy-every-bar"

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
        ctx.buy(SYMBOL, Decimal("1"))


#: These sessions hold a 10x perpetual, so they need an acknowledgement — that
#: is the control working, not a nuisance. Written out in full rather than
#: hidden in a helper, because the numbers are the thing being acknowledged.
PERP_ACK = LeverageAcknowledgement(
    account="a1",
    symbol=PERP_SYMBOL,
    max_leverage=Decimal("10"),
    reference_price=Decimal("100.00"),
    reference_equity=Decimal("10000.00"),
    stated_liquidation_price=Decimal("90.00"),
    stated_loss_at_20pct=Decimal("20000.00"),  # twice the account, and the excess is owed
    acknowledged_by="the test suite",
    acknowledged_at=T0,
    notes="Test fixture. A real account's acknowledgement is a row written by a person.",
)


def config(tmp_path: Path, **over: object) -> SupervisorConfig:
    kwargs: dict[str, object] = dict(
        root=tmp_path,
        account="a1",
        starting_cash=Decimal("10000"),
        instruments={SYMBOL: BTC, PERP_SYMBOL: PERP},
        cost_model=ZERO_COST,
        limits=RiskLimits(),
        kill_switch=tmp_path / "KILL",
        leverage_gate=LeverageGate.of("a1", [PERP_ACK]),
    )
    kwargs.update(over)
    return SupervisorConfig(**kwargs)  # type: ignore[arg-type]


def events(cfg: SupervisorConfig) -> list:
    out = []
    for path in iter_decision_days(cfg.account_dir):
        out.extend(read_decisions(path))
    return out


# -- journalling -------------------------------------------------------------------


def test_a_session_journals_intent_fills_and_marks_with_the_account(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    sup = PaperSupervisor(cfg, BuyOnce(), now=T0 - timedelta(hours=1))
    out = sup.run(InMemoryBarFeed(bars(["100", "100", "110"])))

    assert out.stopped_by == "feed ended"
    assert len(out.result.fills) == 1
    kinds = [e.kind for e in events(cfg)]
    assert kinds.count(EventKind.ORDER) == 1
    assert kinds.count(EventKind.FILL) == 1
    assert kinds.count(EventKind.MARK) == 4  # one per instant, plus the closing mark
    fill_event = next(e for e in events(cfg) if e.kind is EventKind.FILL)
    assert fill_event.account is not None
    assert fill_event.account.cash == Decimal("9500.00")
    last_mark = [e for e in events(cfg) if e.kind is EventKind.MARK][-1]
    assert last_mark.account is not None
    assert last_mark.account.equity == Decimal("10050.00")  # 9500 + 5 * 110


# -- reconcile on boot -----------------------------------------------------------------


def test_a_restart_rebuilds_the_book_from_the_journal(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    PaperSupervisor(cfg, BuyOnce(), now=T0 - timedelta(hours=1)).run(
        InMemoryBarFeed(bars(["100", "100", "110"]))
    )

    probe = BuyOnce()
    probe.bought = True  # never trades; only observes
    second = PaperSupervisor(cfg, probe, now=T0 + timedelta(days=2, hours=12))
    report = second.reconcile()
    assert report.agrees
    assert report.fills_replayed == 1 and report.orders_seen == 1
    assert report.cash == Decimal("9500.00")
    assert report.positions == {SYMBOL: Decimal("5")}
    assert report.cycle == 1

    out = second.run(InMemoryBarFeed(bars(["120", "120"], start=T0 + timedelta(days=3))))
    assert probe.seen_positions == [Decimal("5"), Decimal("5")]
    assert out.fills == (), "nothing traded this session"
    assert len(out.result.fills) == 1, "the cycle's ledger still carries the restored fill"
    assert out.result.equity_curve[-1].equity == Decimal("10100.00")  # 9500 + 5 * 120


def test_an_edited_fill_in_the_journal_is_refused_on_boot(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    PaperSupervisor(cfg, BuyOnce(), now=T0 - timedelta(hours=1)).run(
        InMemoryBarFeed(bars(["100", "100", "110"]))
    )
    edited = 0
    for path in iter_decision_days(cfg.account_dir):
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            raw = json.loads(line)
            if raw["k"] == "fill":
                raw["d"]["price"] = "101"
                lines[i] = json.dumps(raw, separators=(",", ":"), sort_keys=True)
                edited += 1
        path.write_text("\n".join(lines) + "\n")
    assert edited == 1

    with pytest.raises(ReconciliationError, match="DISAGREES"):
        PaperSupervisor(cfg, BuyOnce(), now=T0 + timedelta(days=3)).reconcile()


def test_a_drawdown_halt_survives_a_restart(tmp_path: Path) -> None:
    cfg = config(tmp_path, limits=RiskLimits(max_drawdown_pct=Decimal("0.02")))
    first = PaperSupervisor(cfg, BuyOnce("50"), now=T0 - timedelta(hours=1))
    out = first.run(InMemoryBarFeed(bars(["100", "100", "90", "90"])))
    assert out.result.halted is True
    assert any(e.kind is EventKind.HALT for e in events(cfg))

    second = PaperSupervisor(cfg, BuyEveryBar(), now=T0 + timedelta(days=4))
    report = second.reconcile()
    assert report.halted is True
    out2 = second.run(InMemoryBarFeed(bars(["90", "90"], start=T0 + timedelta(days=5))))
    assert out2.fills == ()
    assert {r.reason for r in out2.result.rejections} == {"halted"}


# -- stops ------------------------------------------------------------------------------


def test_the_kill_switch_stops_intent_before_the_strategy_runs(tmp_path: Path) -> None:
    cfg = config(tmp_path)

    class KillOnThird(Strategy):
        name = "killer"

        def __init__(self) -> None:
            self.bars = 0

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            self.bars += 1
            ctx.buy(SYMBOL, Decimal("1"))
            if self.bars == 2:
                assert cfg.kill_switch is not None
                cfg.kill_switch.touch()

    strat = KillOnThird()
    out = PaperSupervisor(cfg, strat, now=T0 - timedelta(hours=1)).run(
        InMemoryBarFeed(bars(["100"] * 6))
    )
    assert strat.bars == 2, "the strategy must not be called once the switch is present"
    # Bar 0's order fills at bar 1, bar 1's at bar 2's open — before the gate
    # sees the switch. Then the run ends: no third bar, no result object.
    assert len(out.fills) == 2
    assert out.result is None
    assert out.stopped_by == "kill switch"
    notes = [e for e in events(cfg) if e.kind is EventKind.NOTE]
    assert any(e.detail.get("reason") == "kill switch" for e in notes)


def test_the_daily_loss_limit_bites_on_a_leveraged_drop(tmp_path: Path) -> None:
    cfg = config(
        tmp_path,
        max_daily_loss_pct=Decimal("0.05"),
        limits=RiskLimits(max_drawdown_pct=None),
    )
    strat = BuyOnce("60")  # 6,000 notional on 10,000
    intraday = [
        Bar(
            symbol=SYMBOL,
            timestamp=T0 + timedelta(hours=h),
            open=Decimal(p),
            high=Decimal(p),
            low=Decimal(p),
            close=Decimal(p),
            volume=Decimal("1"),
        )
        for h, p in enumerate(["100", "100", "100", "90", "90", "90"])
    ]
    out = PaperSupervisor(cfg, strat, now=T0 - timedelta(hours=1)).run(InMemoryBarFeed(intraday))
    # Fill at hour 1 (open 100). Hour 3 close 90: equity 9,400 = -6% vs the
    # day's open of 10,000 -> halt.
    assert out.stopped_by == "daily loss limit"
    assert len(strat.seen_positions) == 3, "strategy not called after the halt"
    halts = [e for e in events(cfg) if e.kind is EventKind.HALT]
    assert len(halts) == 1 and halts[0].detail["reason"].startswith("daily loss")


# -- cycles ------------------------------------------------------------------------------


def test_equity_reaching_zero_fails_the_cycle_and_ends_the_session(tmp_path: Path) -> None:
    cfg = config(tmp_path, limits=RiskLimits(max_drawdown_pct=None, max_gross_leverage=None))

    class ShortOnce(Strategy):
        name = "short-once"

        def __init__(self) -> None:
            self.done = False
            self.calls = 0

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            self.calls += 1
            if not self.done:
                ctx.sell(PERP_SYMBOL, Decimal("100"))
                self.done = True

    strat = ShortOnce()
    out = PaperSupervisor(cfg, strat, now=T0 - timedelta(hours=1)).run(
        InMemoryBarFeed(bars(["100", "100", "250", "250", "250"], symbol=PERP_SYMBOL))
    )
    # Short 100 at 100 (10x margin: 1,000 reserved). At 250 the loss is 15,000 on 10,000.
    assert out.cycle_failed is not None
    assert out.cycle_failed.outcome is CycleOutcome.FAILED
    assert out.cycle_failed.ending_equity <= 0
    assert out.stopped_by == "cycle failed"
    assert out.cycle == 2
    # Called on days 0 and 1; the day-2 bar reveals 250, the check runs before
    # the strategy, and a dead account gets no third call.
    assert strat.calls == 2, "no strategy call once the cycle has failed"

    again = AccountCycles(root=tmp_path, account="a1", starting_cash=Decimal("10000"), now=T0)
    assert again.number == 2
    assert [r.outcome for r in again.history] == [CycleOutcome.FAILED]
    # And the next session starts on the fresh cycle with fresh cash.
    report = PaperSupervisor(cfg, BuyOnce(), now=T0 + timedelta(days=6)).reconcile()
    assert report.cycle == 2 and report.cash == Decimal("10000") and report.positions == {}


# -- one program, two settings -------------------------------------------------------------


def test_a_live_session_replays_through_a_plain_backtester_exactly(tmp_path: Path) -> None:
    """Step 50 in miniature: the same strategy over the recorded bars through
    the ordinary Backtester must reproduce the supervised session's fills."""
    cfg = config(tmp_path, cost_model=CostModel())
    recorder = JournalRecorder(tmp_path / "bars", "test")
    feed = LiveFeed([SYMBOL], now=lambda: T0 + timedelta(days=365), recorder=recorder)
    series = bars(["100", "101", "99", "103", "104"])
    for bar in series:
        assert feed.publish(bar)
    feed.stop()
    recorder.close()

    class Swing(Strategy):
        name = "swing"

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            prev = ctx.history(SYMBOL)
            if len(prev) >= 2 and prev[-1].close > prev[-2].close and ctx.is_flat(SYMBOL):
                ctx.buy(SYMBOL, Decimal("3"))
            elif len(prev) >= 2 and prev[-1].close < prev[-2].close and not ctx.is_flat(SYMBOL):
                ctx.close_position(SYMBOL)

    live = PaperSupervisor(cfg, Swing(), now=T0 - timedelta(hours=1)).run(feed)
    assert live.result.fills, "the scenario must trade or the comparison is vacuous"

    replayed = Backtester(
        instruments={SYMBOL: BTC},
        starting_cash=Decimal("10000"),
        cost_model=CostModel(),
        risk_manager=__import__(
            "trading_platform.risk.limits", fromlist=["RiskManager"]
        ).RiskManager(limits=RiskLimits()),
    ).run(Swing(), replay_feed(tmp_path / "bars", "test", [SYMBOL]))
    report = compare(live.result.fills, replayed.fills)
    assert report.agrees, report.report()


# -- what the adversarial review found, 2026-09-02 ----------------------------------------


class ShortPerpOnce(Strategy):
    name = "short-perp-once"

    def __init__(self, qty: str) -> None:
        self.qty = Decimal(qty)
        self.done = False

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
        if not self.done and bar.symbol == PERP_SYMBOL:
            ctx.sell(PERP_SYMBOL, self.qty)
            self.done = True


class BuySpotOnce(Strategy):
    name = "buy-spot-once"

    def __init__(self, qty: str) -> None:
        self.qty = Decimal(qty)
        self.done = False

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
        if not self.done and bar.symbol == SYMBOL:
            ctx.buy(SYMBOL, self.qty)
            self.done = True


def two_symbol_bars(spot: list[str], perp: list[str], start: datetime = T0) -> list[Bar]:
    out = bars(spot, start=start) + bars(perp, start=start, symbol=PERP_SYMBOL)
    return sorted(out, key=lambda b: (b.timestamp, b.symbol))


def test_a_restored_loss_is_visible_to_the_risk_layer_before_its_symbol_prints(
    tmp_path: Path,
) -> None:
    """F1. Short 60 perps at 100 (10x margin); the price goes to 250 and the
    journal's last mark says equity 1,000. On restart, before the perp prints
    again, a 9,500 spot buy must be refused. It used to pass with zero
    rejections: the engine's price dict had never seen the perp, so equity
    read 10,000 and gross exposure 0."""
    cfg = config(tmp_path, limits=RiskLimits(max_drawdown_pct=None))
    first = PaperSupervisor(cfg, ShortPerpOnce("60"), now=T0 - timedelta(hours=1))
    out = first.run(InMemoryBarFeed(two_symbol_bars(["100", "100", "100"], ["100", "100", "250"])))
    assert out.result is not None
    assert out.result.equity_curve[-1].equity == Decimal("1000.00")

    second = PaperSupervisor(cfg, BuySpotOnce("95"), now=T0 + timedelta(days=2, hours=12))
    report = second.reconcile()
    assert report.marked_equity == Decimal("1000.00"), "re-marked at the last journalled price"
    # The spot bar arrives first at each instant; the perp has not printed yet.
    out2 = second.run(
        InMemoryBarFeed(
            two_symbol_bars(["100", "100"], ["250", "250"], start=T0 + timedelta(days=3))
        )
    )
    assert out2.fills == ()
    assert out2.result is not None
    assert out2.result.rejections, "the buy must be refused against the real equity"


def test_a_rewritten_derivative_fill_is_refused_on_boot(tmp_path: Path) -> None:
    """F2. A perp fill moves cash by commission only, so a journal whose fill
    quantity was rewritten still agreed on cash. Positions are compared too."""
    cfg = config(tmp_path, limits=RiskLimits(max_drawdown_pct=None))
    PaperSupervisor(cfg, ShortPerpOnce("10"), now=T0 - timedelta(hours=1)).run(
        InMemoryBarFeed(bars(["100", "100", "100"], symbol=PERP_SYMBOL))
    )
    edited = 0
    for path in iter_decision_days(cfg.account_dir):
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            raw = json.loads(line)
            if raw["k"] == "fill":
                raw["d"]["quantity"] = "1000"
                lines[i] = json.dumps(raw, separators=(",", ":"), sort_keys=True)
                edited += 1
        path.write_text("\n".join(lines) + "\n")
    assert edited == 1
    with pytest.raises(ReconciliationError, match="DISAGREES"):
        PaperSupervisor(cfg, BuyEveryBar(), now=T0 + timedelta(days=3)).reconcile()


def test_the_kill_switch_also_stops_the_cross_section_hook(tmp_path: Path) -> None:
    """F3. The switch used to be read in `on_bar` only; a ranker's whole
    rebalance went through on the next instant."""
    cfg = config(tmp_path)

    class Ranker(Strategy):
        name = "ranker"

        def __init__(self) -> None:
            self.bars = 0
            self.cross_sections: list[datetime] = []

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            self.bars += 1
            if self.bars == 2:
                assert cfg.kill_switch is not None
                cfg.kill_switch.touch()

        def on_cross_section(self, timestamp: datetime, ctx: StrategyContext) -> None:
            self.cross_sections.append(timestamp)
            ctx.buy(SYMBOL, Decimal("1"))

    strat = Ranker()
    out = PaperSupervisor(cfg, strat, now=T0 - timedelta(hours=1)).run(
        InMemoryBarFeed(bars(["100"] * 5))
    )
    # Instant 0 closes when bar 1 arrives (before the switch); the switch is
    # touched during bar 1's on_bar; instant 1's cross-section must not run.
    assert strat.cross_sections == [T0]
    assert len(out.fills) == 1
    assert out.stopped_by == "kill switch"


def test_a_mid_day_restart_keeps_the_days_opening_equity(tmp_path: Path) -> None:
    """F4. Limit 5%. Session 1 loses 3% intraday; a restart re-based the
    limit on the restart bar, so a further 3% in session 2 never halted."""
    cfg = config(
        tmp_path, max_daily_loss_pct=Decimal("0.05"), limits=RiskLimits(max_drawdown_pct=None)
    )

    def hours(prices: list[str], start_hour: int) -> list[Bar]:
        return [
            Bar(
                symbol=SYMBOL,
                timestamp=T0 + timedelta(hours=start_hour + h),
                open=Decimal(p),
                high=Decimal(p),
                low=Decimal(p),
                close=Decimal(p),
                volume=Decimal("1"),
            )
            for h, p in enumerate(prices)
        ]

    # Long 60 at 100 (fill hour 1). Hour 2: 95 -> equity 9,700, -3%.
    first = PaperSupervisor(cfg, BuyOnce("60"), now=T0 - timedelta(hours=1))
    out1 = first.run(InMemoryBarFeed(hours(["100", "100", "95"], 0)))
    assert out1.stopped_by == "feed ended"

    probe = BuyOnce()
    probe.bought = True
    second = PaperSupervisor(cfg, probe, now=T0 + timedelta(hours=3))
    report = second.reconcile()
    assert report.day_open_equity == Decimal("10000.00")
    # Hour 4: 91 -> equity 9,460, -5.4% from the day's open; -2.5% from the restart bar.
    out2 = second.run(InMemoryBarFeed(hours(["95", "91", "91"], 4)))
    assert out2.stopped_by == "daily loss limit"


def test_the_drawdown_peak_is_journalled_and_restored(tmp_path: Path) -> None:
    """F5. The peak used to be re-derived from MARK equities, which are
    sampled once per instant and missed the intra-instant high.

    Two symbols, so the peak lands after the SECOND symbol's reveal at T2,
    where no journalled account state carries it: the T2 MARK is written at
    the spot bar (10,000) and the T3 MARK reads 9,900. Only the `peak` detail
    on the MARK can restore it. The first version of this test had one symbol,
    so every MARK equalled the running peak and deleting the restore branch
    left it green (guard-auditor, 2026-09-03)."""
    cfg = config(tmp_path, limits=RiskLimits(max_drawdown_pct=Decimal("0.5")))

    class LongSpotAndPerp(Strategy):
        name = "long-spot-and-perp"

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            if bar.timestamp == T0:
                if bar.symbol == SYMBOL:
                    ctx.buy(SYMBOL, Decimal("50"))
                else:
                    ctx.buy(PERP_SYMBOL, Decimal("10"))

    first = PaperSupervisor(cfg, LongSpotAndPerp(), now=T0 - timedelta(hours=1))
    first.run(
        InMemoryBarFeed(two_symbol_bars(["100", "100", "100", "90"], ["100", "100", "140", "120"]))
    )
    assert first._risk is not None
    peak = first._risk.peak_equity
    assert peak == Decimal("10400.00")  # after the perp's T2 reveal: 10,000 + 10 * 40
    journalled = [e.account.equity for e in events(cfg) if e.account is not None]
    assert max(journalled) < peak, "precondition: no journalled state carries the peak"

    second = PaperSupervisor(cfg, BuyEveryBar(), now=T0 + timedelta(days=4))
    assert second.reconcile().peak_equity == peak


def test_a_daily_loss_halt_does_not_become_a_cycle_long_halt(tmp_path: Path) -> None:
    """F6. Every HALT kind used to restore as the risk layer's terminal halt."""
    cfg = config(
        tmp_path, max_daily_loss_pct=Decimal("0.05"), limits=RiskLimits(max_drawdown_pct=None)
    )
    intraday = [
        Bar(
            symbol=SYMBOL,
            timestamp=T0 + timedelta(hours=h),
            open=Decimal(p),
            high=Decimal(p),
            low=Decimal(p),
            close=Decimal(p),
            volume=Decimal("1"),
        )
        for h, p in enumerate(["100", "100", "100", "90"])
    ]
    out = PaperSupervisor(cfg, BuyOnce("60"), now=T0 - timedelta(hours=1)).run(
        InMemoryBarFeed(intraday)
    )
    assert out.stopped_by == "daily loss limit"

    second = PaperSupervisor(cfg, BuyEveryBar(), now=T0 + timedelta(days=1))
    assert second.reconcile().halted is False
    out2 = second.run(InMemoryBarFeed(bars(["90", "90", "90"], start=T0 + timedelta(days=1))))
    assert len(out2.fills) == 2, "a new day trades again"


def test_a_failed_cycle_stops_the_engine_before_any_other_fill_lands(tmp_path: Path) -> None:
    """F7. Two symbols. The perp wipes the account at T2 while a spot order is
    queued for T2's open. The session must end with no fill into the dead
    book, no strategy call, and no fill journalled against the new cycle."""
    cfg = config(tmp_path, limits=RiskLimits(max_drawdown_pct=None, max_gross_leverage=None))

    class Both(Strategy):
        name = "both"

        def __init__(self) -> None:
            self.calls = 0
            self.fills: list[Fill] = []

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            self.calls += 1
            if bar.symbol == PERP_SYMBOL and bar.timestamp == T0:
                ctx.sell(PERP_SYMBOL, Decimal("100"))
            if bar.symbol == SYMBOL and bar.timestamp == T0 + timedelta(days=1):
                ctx.buy(SYMBOL, Decimal("2"))  # queued for T2's open

        def on_fill(self, fill: Fill, ctx: StrategyContext) -> None:
            self.fills.append(fill)

    strat = Both()
    out = PaperSupervisor(cfg, strat, now=T0 - timedelta(hours=1)).run(
        InMemoryBarFeed(two_symbol_bars(["100", "100", "100", "100"], ["100", "100", "250", "250"]))
    )
    assert out.cycle_failed is not None and out.result is None
    # T0: 2 calls; T1: 2 calls; T2: the spot bar sorts first, the perp is still
    # marked at 100, so the account is alive: the spot order fills at T2's open
    # and the strategy is called once more. Then the perp bar reveals 250, the
    # cycle fails on the spot, and nothing follows — no call, no fill.
    assert strat.calls == 5
    assert [f.symbol for f in strat.fills] == [PERP_SYMBOL, SYMBOL]
    assert out.cycle_failed.fills == 2, "the failed record accounts for every fill it took"
    fills = [e for e in events(cfg) if e.kind is EventKind.FILL]
    assert len(fills) == 2
    halts = [e for e in events(cfg) if e.kind is EventKind.HALT]
    assert halts[-1].detail["kind"] == "cycle_failed"
    report = PaperSupervisor(cfg, BuyOnce(), now=T0 + timedelta(days=6)).reconcile()
    assert report.cycle == 2 and report.positions == {} and report.cash == Decimal("10000")


def test_a_feed_starting_before_the_cycle_opened_is_refused(tmp_path: Path) -> None:
    """F8. With the default clock and historical bars, a wipe-out used to
    escape as `ValueError: a cycle cannot close before it opened`."""
    cfg = config(tmp_path)
    sup = PaperSupervisor(cfg, BuyOnce())  # now=None: wall clock, far after T0
    with pytest.raises(ValueError, match="not after the cycle opened"):
        sup.run(InMemoryBarFeed(bars(["100", "100"])))


def test_fills_the_cycle_clock_would_exclude_are_refused_on_boot(tmp_path: Path) -> None:
    """F8b. Reconcile used to agree vacuously when every fill fell before
    `opened_at`, and the strategy saw a flat book."""
    cfg = config(tmp_path)
    PaperSupervisor(cfg, BuyOnce(), now=T0 - timedelta(hours=1)).run(
        InMemoryBarFeed(bars(["100", "100", "100"]))
    )
    # Delete the cycle store so the next boot opens "cycle 1" fresh at a later clock.
    (cfg.account_dir / "cycles.jsonl").unlink()
    with pytest.raises(ReconciliationError, match="clock is wrong"):
        PaperSupervisor(cfg, BuyOnce(), now=T0 + timedelta(days=5)).reconcile()


def test_one_writer_per_account(tmp_path: Path) -> None:
    """F9. Two supervisors on one account split the books."""
    from trading_platform.live.supervisor import AccountLocked

    cfg = config(tmp_path)
    holder = PaperSupervisor(cfg, BuyOnce(), now=T0 - timedelta(hours=1))
    holder.reconcile()
    lock = holder._acquire_lock()
    try:
        with pytest.raises(AccountLocked):
            PaperSupervisor(cfg, BuyOnce(), now=T0 - timedelta(hours=1)).run(
                InMemoryBarFeed(bars(["100"]))
            )
    finally:
        holder._release_lock(lock)
    # Released: a session runs, and releases again on exit.
    PaperSupervisor(cfg, BuyOnce(), now=T0 - timedelta(hours=1)).run(InMemoryBarFeed(bars(["100"])))
    assert not (cfg.account_dir / "LOCK").exists()


def test_a_cycle_store_with_a_number_closed_twice_is_refused(tmp_path: Path) -> None:
    """F9b. Two processes each failing cycle 1 used to reload as cycle 3 with
    cycle 2 skipped and no error."""
    acct = AccountCycles(root=tmp_path, account="a1", starting_cash=Decimal("1000"), now=T0)
    path = tmp_path / "accounts" / "a1" / "cycles.jsonl"
    text = path.read_text()
    first_open = text.splitlines()[0]
    closed = json.loads(first_open)
    closed.update(outcome="FAILED", closed_at=closed["opened_at"], ending_equity="0")
    line = json.dumps(closed, separators=(",", ":"), sort_keys=True)
    path.write_text(text + line + "\n" + line + "\n")
    del acct
    from trading_platform.live.accounts import CycleError

    with pytest.raises(CycleError, match="closes twice"):
        AccountCycles(root=tmp_path, account="a1", starting_cash=Decimal("1000"), now=T0)


def test_a_refusal_for_want_of_a_price_reaches_the_journal(tmp_path: Path) -> None:
    """F10. `no_reference_price` refusals skipped `on_reject`, so the journal
    held an ORDER with no outcome."""
    cfg = config(tmp_path)

    class BuyTheOtherSymbolFirst(Strategy):
        name = "other-first"

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            if bar.symbol == SYMBOL and bar.timestamp == T0:
                ctx.buy(PERP_SYMBOL, Decimal("1"))  # the perp has not printed yet

    out = PaperSupervisor(cfg, BuyTheOtherSymbolFirst(), now=T0 - timedelta(hours=1)).run(
        InMemoryBarFeed(two_symbol_bars(["100", "100"], ["100", "100"]))
    )
    assert out.result is not None
    assert [r.reason for r in out.result.rejections] == ["no_reference_price"]
    rejections = [e for e in events(cfg) if e.kind is EventKind.REJECTION]
    assert len(rejections) == 1 and rejections[0].detail["reason"] == "no_reference_price"


def test_funding_is_refused_until_the_journal_can_carry_it(tmp_path: Path) -> None:
    """Gauntlet, 2026-09-03: a funded account's cash moved with no journal
    event, so reconcile refused it forever. Refused at construction instead."""
    from typing import Any, cast

    with pytest.raises(ValueError, match="not journalled"):
        config(tmp_path, funding=cast(Any, object()))


def test_a_working_limit_order_does_not_fill_after_the_kill_switch(tmp_path: Path) -> None:
    """Gauntlet, 2026-09-03: `cancel_all` had no caller, so an order queued
    before the touch could fill bars later. The run now ends at the touch."""
    from trading_platform.core.types import OrderType

    cfg = config(tmp_path)

    class LimitThenKill(Strategy):
        name = "limit-then-kill"

        def __init__(self) -> None:
            self.bars = 0

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            self.bars += 1
            if self.bars == 1:
                # A resting buy far below the market: it would fill only when
                # the price gets there, several bars on.
                ctx.submit(
                    SYMBOL,
                    Side.BUY,
                    Decimal("1"),
                    order_type=OrderType.LIMIT,
                    limit_price=Decimal("50"),
                )
            if self.bars == 2:
                assert cfg.kill_switch is not None
                cfg.kill_switch.touch()

    strat = LimitThenKill()
    out = PaperSupervisor(cfg, strat, now=T0 - timedelta(hours=1)).run(
        InMemoryBarFeed(bars(["100", "100", "100", "40", "40"]))
    )
    assert out.stopped_by == "kill switch"
    assert out.fills == (), "the resting order must not fill on the bars after the touch"
    assert strat.bars == 2


def test_a_session_holding_a_leveraged_instrument_needs_a_gate(tmp_path: Path) -> None:
    """Leverage must not arrive because a keyword was omitted. A session that
    registers a 10x perpetual and passes no gate is refused at construction —
    the same place this repository puts every other impossible value.

    Mutation: drop the `leverage_gate is None` block from
    `SupervisorConfig.__post_init__`. Every session in this file then runs a
    10x instrument with nothing acknowledged, silently, which is how it was
    before workstream 3."""
    with pytest.raises(ValueError, match="was given no leverage_gate"):
        SupervisorConfig(
            root=tmp_path,
            account="a1",
            starting_cash=Decimal("10000"),
            instruments={PERP_SYMBOL: PERP},
            cost_model=ZERO_COST,
            limits=RiskLimits(),
            kill_switch=tmp_path / "KILL",
        )


def test_a_spot_only_session_needs_no_gate(tmp_path: Path) -> None:
    """The refusal must not become a general nuisance: nothing at 1x needs
    acknowledging, and requiring a gate for spot would teach people to pass an
    empty one everywhere, including where it matters."""
    cfg = SupervisorConfig(
        root=tmp_path,
        account="a1",
        starting_cash=Decimal("10000"),
        instruments={SYMBOL: BTC},
        cost_model=ZERO_COST,
        limits=RiskLimits(),
        kill_switch=tmp_path / "KILL",
    )
    assert cfg.leverage_gate is None


def test_a_session_cannot_declare_itself_live(tmp_path: Path) -> None:
    """Every broker adapter raises, so no session here can place a live order.
    A row claiming LIVE would be the first false thing in the record.

    Mutation: drop the check — a paper run can stamp `live` on every mark it
    writes, and the four stages stop being kept apart in the data."""
    from trading_platform.core.types import Stage

    with pytest.raises(ValueError, match="stage=LIVE is refused"):
        config(tmp_path, stage=Stage.LIVE)


def test_a_gate_belonging_to_another_account_is_refused(tmp_path: Path) -> None:
    """`LeverageGate.of` refuses to mix accounts and ADR 0011 says an
    acknowledgement does not carry between them — but the one place that could
    enforce it checked only that a gate was PRESENT. A gate built for a2 admitted
    a2's acknowledgements on a1: a 10x perpetual filled with zero rejections
    (adversarial review, 2026-09-04).

    Mutation: drop the account comparison from `SupervisorConfig.__post_init__`
    — red, and a copy-pasted gate becomes leverage arriving by accident."""
    other = LeverageAcknowledgement(
        account="a2",
        symbol=PERP_SYMBOL,
        max_leverage=Decimal("10"),
        reference_price=Decimal("100.00"),
        reference_equity=Decimal("10000.00"),
        stated_liquidation_price=Decimal("90.00"),
        stated_loss_at_20pct=Decimal("20000.00"),
        acknowledged_by="somebody else",
        acknowledged_at=T0,
    )
    with pytest.raises(ValueError, match="belonging to 'a2'"):
        config(tmp_path, leverage_gate=LeverageGate.of("a2", [other]))
