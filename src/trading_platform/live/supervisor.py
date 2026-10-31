"""The paper supervisor: one strategy, one account, one journal, one ledger.

This is the PAPER stage of the four (RESEARCH → SIMULATION → PAPER → LIVE).
Nothing here can place a real order; the only execution is the same
`SimulatedExecutionEngine` a backtest uses, driven by the same `Backtester`
over a `LiveFeed` instead of a file. That is the rule the whole of `live/`
keeps: **no live-only path through a strategy**, so the divergence tracking of
roadmap step 50 compares one program in two settings rather than two programs.

What the supervisor adds around the engine:

- **A decision journal** of every order (intent, before the risk layer),
  fill, rejection, halt and mark, each carrying the account as it stood,
  including its positions and the marks it stood at.
- **Account cycles** (charter §28): the engine writes to the open cycle's own
  ledger — `Backtester.run(portfolio=cycles.portfolio)` — so there is one set
  of books. Equity reaching zero fails the cycle and **ends the session at
  once**: the engine is stopped by exception before any further fill can land
  in a dead book.
- **Reconcile on boot**: the open cycle's ledger is rebuilt from the journal's
  fills, re-marked at the last journalled prices, and compared with the cash
  *and positions* the journal recorded last. A mismatch is a
  `ReconciliationError` and the session refuses to start. The day's opening
  equity and the risk layer's peak and halt state are restored from the same
  journal, so a restart neither re-bases the daily loss limit nor forgets a
  drawdown halt. A held symbol the feed will not print is refused too: a
  position nobody prices cannot be risk-managed.
- **A kill switch**: a file whose presence stops the strategy being called —
  in `on_bar` and in `on_cross_section` alike. The 3am control.
- **A daily loss limit**, enforced here in addition to the risk layer's
  drawdown halt: equity below the day's opening equity by more than the limit
  journals a HALT and stops the session. It resets with the UTC day, and a
  restart mid-day picks the day's open up from the journal.
- **One writer per account**: a lock file under the account directory. A
  second supervisor on the same account refuses to start.

Realism (charter §93) is inherited from the engine: fees, spread and slippage
from `CostModel`; the one-bar execution delay; funding if a schedule is passed;
market hours only in so far as the venue stops printing bars. **Not
modelled**: partial fills, liquidity, queue position. Paper is not live, and
this module does not pretend it is.

Replay identity (`live.replay.compare`) holds **per session**: the same
strategy over this session's recorded bars through a plain `Backtester`
reproduces this session's fills. It does not hold across a restart, because
`ctx.history` and the strategy's own state start empty at boot while the
ledger does not — that is a property of restarting, not a defect, and it is
why the daily review compares session by session.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from trading_platform.core.events import Bar, Fill, Order
from trading_platform.core.instruments import Instrument
from trading_platform.core.types import Side, Stage
from trading_platform.data.feed import DataFeed
from trading_platform.data.funding import FundingSchedule
from trading_platform.engine.backtester import Backtester, BacktestResult
from trading_platform.engine.execution import CostModel
from trading_platform.live.accounts import ACCOUNTS_DIRNAME, AccountCycles, CycleRecord
from trading_platform.live.journal import (
    AccountState,
    DecisionJournal,
    EventKind,
    JournalEvent,
    iter_decision_days,
    read_decisions,
)
from trading_platform.live.tracker import MarkSink, mark_from_portfolio
from trading_platform.risk.leverage import LeverageGate, needs_acknowledgement
from trading_platform.risk.limits import RiskLimits, RiskManager, RiskRejection
from trading_platform.strategy.base import Strategy, StrategyContext

__all__ = [
    "AccountLocked",
    "CycleFailed",
    "PaperSupervisor",
    "ReconciliationError",
    "ReconciliationReport",
    "SessionResult",
    "SessionStopped",
    "SupervisorConfig",
]

LOCK_FILENAME = "LOCK"


class ReconciliationError(RuntimeError):
    """The journal and the ledger rebuilt from it disagree. Do not trade on top of it."""


class AccountLocked(RuntimeError):
    """Another supervisor holds this account. One writer, or the books split."""


class SessionStopped(RuntimeError):
    """Raised inside the engine loop when the kill switch or the daily loss
    limit fires, so the session ends at once: no further bar is processed and
    no working order fills. A flag left the engine running, and a limit order
    queued before the touch could still fill bars later (gauntlet, 2026-09-03:
    `cancel_all` had no caller). Ending the run is the cancel."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CycleFailed(RuntimeError):
    """Raised inside the engine loop to end the session the instant a cycle fails.

    An exception rather than a flag because a flag leaves the engine running:
    orders already queued for other symbols would fill into the dead ledger on
    the same bar, journalled against the new cycle's account state, and
    reconciled into neither (adversarial-reviewer, 2026-09-02).
    """

    def __init__(self, record: CycleRecord) -> None:
        super().__init__(f"cycle {record.number} failed at {record.ending_equity}")
        self.record = record


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """Everything a session needs, stated. No defaults for money."""

    root: Path
    account: str
    starting_cash: Decimal
    instruments: Mapping[str, Instrument]
    cost_model: CostModel
    limits: RiskLimits
    currency: str = "USD"
    #: A file: while it exists the strategy is not called and no intent is
    #: generated. None disables the switch, which is not a sane paper setup.
    kill_switch: Path | None = None
    #: Fraction of the day's opening equity the account may lose before the
    #: session halts. None disables this check (the drawdown halt remains).
    max_daily_loss_pct: Decimal | None = None
    #: Perpetual funding, passed straight to the engine. None means paper
    #: perps pay no funding — a stated divergence from any funded backtest.
    funding: FundingSchedule | None = None
    #: Where account state and refusals are durably recorded, beside the
    #: journal. `None` means the journal is the only record, which is what it
    #: was before this existed — a paper session must run with no database.
    #: The tracker is an observer and may never stop a session; `MarkSink`
    #: says so and `store/accounts.StoreTracker` swallows and counts.
    tracker: MarkSink | None = None
    #: Which leveraged instruments this account has acknowledged. `None` means
    #: no gate, which is correct only for RESEARCH; a PAPER session on a real
    #: account should pass `store.accounts.leverage_gate(conn, account)`, and
    #: `__post_init__` refuses a leveraged instrument without one so that
    #: "nobody remembered to pass it" cannot be how leverage gets used.
    leverage_gate: LeverageGate | None = None
    #: Which of the four stages this session is. Recorded on every row so a
    #: paper equity curve can never be read as a live one (charter §8).
    #: `LIVE` is refused here: nothing in this repository can place an order,
    #: and a row claiming otherwise would be the first lie in the record.
    stage: Stage = Stage.PAPER

    def __post_init__(self) -> None:
        if self.starting_cash <= 0:
            raise ValueError(f"starting_cash must be positive, got {self.starting_cash}")
        if self.leverage_gate is not None and self.leverage_gate.account != self.account:
            raise ValueError(
                f"this session is for account {self.account!r} and was given a leverage "
                f"gate belonging to {self.leverage_gate.account!r}. One account's "
                "acknowledgement does not carry to another (ADR 0011), and a "
                "copy-pasted gate is exactly leverage arriving because a keyword was "
                "wrong."
            )
        if self.leverage_gate is None:
            leveraged = sorted(
                symbol
                for symbol, instrument in self.instruments.items()
                if needs_acknowledgement(instrument)
            )
            if leveraged:
                raise ValueError(
                    f"this session holds leveraged instruments ({', '.join(leveraged)}) and "
                    "was given no leverage_gate. Pass "
                    "store.accounts.leverage_gate(connection, account) — an account that has "
                    "acknowledged nothing gets a gate that refuses everything, which is the "
                    "correct answer. Leverage must not arrive because a keyword was omitted."
                )
        if self.stage is Stage.LIVE:
            raise ValueError(
                "stage=LIVE is refused: every broker adapter in this repository raises, so "
                "no session here can place a live order. A row claiming LIVE would be the "
                "first false thing in the record. Reaching LIVE needs an ADR, not a flag."
            )
        if not self.instruments:
            raise ValueError("a supervisor with no instruments has nothing to supervise")
        if self.max_daily_loss_pct is not None and not (0 < self.max_daily_loss_pct < 1):
            raise ValueError(f"max_daily_loss_pct must be in (0, 1), got {self.max_daily_loss_pct}")
        if self.funding is not None:
            # Funding moves cash with no journal event, so a funded account can
            # never be rebuilt on boot and refuses to start forever (gauntlet,
            # 2026-09-03). Refused here, at construction, until a FUNDING
            # event exists and `restore` replays it.
            raise ValueError(
                "funding is not journalled yet: a funded paper account cannot be reconciled "
                "on restart. Run perpetuals without funding, and record that the paper result "
                "omits it, until the journal carries funding events."
            )

    @property
    def account_dir(self) -> Path:
        return Path(self.root) / ACCOUNTS_DIRNAME / self.account


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """What the boot-time rebuild found."""

    account: str
    cycle: int
    fills_replayed: int
    orders_seen: int
    cash: Decimal
    positions: dict[str, Decimal]
    journalled_cash: Decimal | None
    journalled_positions: dict[str, Decimal] | None
    halted: bool
    peak_equity: Decimal
    day_open_equity: Decimal | None
    marked_equity: Decimal

    @property
    def agrees(self) -> bool:
        if self.journalled_cash is None:
            return True
        if self.journalled_cash != self.cash:
            return False
        return self.journalled_positions is None or self.journalled_positions == self.positions

    def report(self) -> str:
        lines = [
            f"{self.account} cycle {self.cycle}: {self.fills_replayed} fill(s) replayed from "
            f"{self.orders_seen} order(s)",
            f"  cash {self.cash} (journal last said "
            f"{'nothing' if self.journalled_cash is None else self.journalled_cash})",
            f"  positions {self.positions or 'flat'} (journal last said "
            f"{'nothing' if self.journalled_positions is None else self.journalled_positions or 'flat'})",
            f"  marked equity {self.marked_equity}; halted {self.halted}; peak {self.peak_equity}",
            "  AGREES" if self.agrees else "  DISAGREES — refusing to start",
        ]
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class SessionResult:
    session: str
    #: The engine's result for the whole open cycle: `result.fills` includes
    #: fills restored from earlier sessions, because the ledger is the cycle's.
    #: None when the session ended by exception — a failed cycle, the kill
    #: switch, or the daily loss limit — because the engine never reached its
    #: result. `fills`, `stopped_by` and `cycle_failed` carry what happened.
    result: BacktestResult | None
    #: Fills that happened in THIS session only — what a replay of this
    #: session's bars should reproduce (`live.replay.compare`).
    fills: tuple[Fill, ...]
    cycle: int
    cycle_failed: CycleRecord | None
    stopped_by: str
    journal_events: int


class _Supervised(Strategy):
    """Wraps the user's strategy: forwards every hook, journals what happens,
    and refuses to forward once a stop condition has fired.

    A wrapper rather than a change to the engine, so the engine's hook order
    (fills, then funding, then reveal, then risk, then the strategy) is the
    one every backtest has been measured against."""

    def __init__(self, inner: Strategy, owner: PaperSupervisor) -> None:
        self.name = inner.name
        self._inner = inner
        self._owner = owner
        # Only override the cross-section hook when the inner strategy does,
        # so the engine's `fires_cross_section` test sees the truth.
        hook = inner.on_cross_section
        if getattr(hook, "__func__", hook) is not Strategy.on_cross_section:
            self.on_cross_section = self._on_cross_section  # type: ignore[method-assign]

    def on_start(self, ctx: StrategyContext) -> None:
        self._inner.on_start(ctx)

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
        if self._owner._gate(bar.timestamp, ctx, new_bar=True):
            self._inner.on_bar(bar, ctx)

    def _on_cross_section(self, timestamp: datetime, ctx: StrategyContext) -> None:
        # The same gate as `on_bar`. Checking only `_stopped` here let a kill
        # switch touched during `on_bar` be followed by a full cross-section
        # rebalance on the next instant (adversarial-reviewer, 2026-09-02).
        if self._owner._gate(timestamp, ctx, new_bar=False):
            self._inner.on_cross_section(timestamp, ctx)

    def on_fill(self, fill: Fill, ctx: StrategyContext) -> None:
        self._owner._on_fill(fill)
        self._inner.on_fill(fill, ctx)

    def on_reject(self, rejection: RiskRejection, ctx: StrategyContext) -> None:
        self._owner._on_reject(rejection)
        self._inner.on_reject(rejection, ctx)

    def on_finish(self, ctx: StrategyContext) -> None:
        self._inner.on_finish(ctx)
        self._owner._on_finish()


@dataclass(slots=True)
class PaperSupervisor:
    """Runs one strategy on one account, journalled, with the stops above."""

    config: SupervisorConfig
    strategy: Strategy
    now: datetime | None = None
    _cycles: AccountCycles | None = field(init=False, default=None)
    _risk: RiskManager | None = field(init=False, default=None)
    _journal: DecisionJournal | None = field(init=False, default=None)
    _stopped: str = field(init=False, default="")
    _halt_logged: bool = field(init=False, default=False)
    _watermark: datetime | None = field(init=False, default=None)
    _first_bar_checked: bool = field(init=False, default=False)
    _day: date | None = field(init=False, default=None)
    _day_open_equity: Decimal | None = field(init=False, default=None)
    _cycle_failed: CycleRecord | None = field(init=False, default=None)
    _feed: DataFeed | None = field(init=False, default=None)
    _session_fills: list[Fill] = field(init=False, default_factory=list)
    #: The instant whose mark has not yet been tracked. The tracker always
    #: records the previous, fully-revealed instant.
    _tracked_watermark: datetime | None = field(init=False, default=None)
    #: Marks the tracker could not be given. Counted rather than raised: an
    #: observer must never stop the session it observes.
    _track_failures: int = field(init=False, default=0)
    _track_last_error: str = field(init=False, default="")

    # -- boot ----------------------------------------------------------------------

    def reconcile(self) -> ReconciliationReport:
        """Rebuild the open cycle from the journal and check it against itself.

        Compares cash and positions against the last journal event that
        carried an account state. Re-marks every held symbol at the price the
        last MARK recorded, so the book at boot is worth what it was last
        known to be worth, not what it cost. Restores the drawdown peak, the
        drawdown halt (and only that kind of halt), and today's opening
        equity. Raises `ReconciliationError` on a mismatch, or when the
        journal holds fills for this cycle that the cycle's own clock would
        exclude — which means the clock is wrong, and a wrong clock would make
        the book silently flat.
        """
        cfg = self.config
        cycles = AccountCycles(
            root=cfg.root,
            account=cfg.account,
            starting_cash=cfg.starting_cash,
            currency=cfg.currency,
            now=self.now,
        )
        opened = cycles.current.opened_at
        previous_close = cycles.history[-1].closed_at if cycles.history else None
        fills: list[Fill] = []
        orders = 0
        halted = False
        peak = cfg.starting_cash
        last_state: AccountState | None = None
        last_marks: dict[str, Decimal] = {}
        last_mark_at: datetime | None = None
        excluded_fills = 0
        today = (self.now or datetime.now(UTC)).astimezone(UTC).date()
        day_open: Decimal | None = None
        for path in iter_decision_days(cfg.account_dir):
            for event in read_decisions(path):
                if event.timestamp <= opened:
                    # Belongs to an earlier cycle — unless nothing closed
                    # between it and this cycle's opening, in which case the
                    # cycle's clock is simply wrong.
                    if event.kind is EventKind.FILL and (
                        previous_close is None or event.timestamp > previous_close
                    ):
                        excluded_fills += 1
                    continue
                if event.kind is EventKind.FILL:
                    fills.append(_fill_from(event))
                elif event.kind is EventKind.ORDER:
                    orders += 1
                elif (
                    event.kind is EventKind.HALT
                    and event.detail.get("kind", "drawdown") == "drawdown"
                ):
                    halted = True
                elif event.kind is EventKind.MARK:
                    marks = {
                        k[3:]: Decimal(v) for k, v in event.detail.items() if k.startswith("px:")
                    }
                    if marks:
                        last_marks = marks
                        last_mark_at = event.timestamp
                    if "peak" in event.detail:
                        peak = max(peak, Decimal(event.detail["peak"]))
                if event.account is not None:
                    last_state = event.account
                    peak = max(peak, event.account.equity)
                    if event.timestamp.astimezone(UTC).date() == today and day_open is None:
                        day_open = event.account.equity
        if excluded_fills:
            raise ReconciliationError(
                f"{cfg.account}: {excluded_fills} journalled fill(s) fall before cycle "
                f"{cycles.number} opened at {opened.isoformat()} with no cycle closed between. "
                "The cycle's clock is wrong (pass now= at or before the first bar); refusing "
                "to start on a book that would be silently flat."
            )
        cycles.restore(cfg.instruments, fills, orders=orders, peak_equity=peak)
        if last_mark_at is not None:
            for symbol, price in last_marks.items():
                if symbol in cfg.instruments:
                    cycles.portfolio.observe_price(symbol, price, last_mark_at)
        positions = {s: p.quantity for s, p in cycles.portfolio.positions.items() if not p.is_flat}
        report = ReconciliationReport(
            account=cfg.account,
            cycle=cycles.number,
            fills_replayed=len(fills),
            orders_seen=orders,
            cash=cycles.portfolio.cash,
            positions=positions,
            journalled_cash=None if last_state is None else last_state.cash,
            journalled_positions=None if last_state is None else dict(last_state.positions),
            halted=halted,
            peak_equity=peak,
            day_open_equity=day_open,
            marked_equity=cycles.portfolio.marked_equity,
        )
        if not report.agrees:
            raise ReconciliationError(report.report())
        self._cycles = cycles
        self._risk = RiskManager(
            limits=cfg.limits,
            leverage_gate=cfg.leverage_gate,
            peak_equity=peak,
            halted=halted,
        )
        if day_open is not None:
            self._day = today
            self._day_open_equity = day_open
        return report

    # -- the session ---------------------------------------------------------------

    def run(self, feed: DataFeed) -> SessionResult:
        if self._cycles is None:
            self.reconcile()
        cycles = self._cycles
        risk = self._risk
        assert cycles is not None and risk is not None
        cfg = self.config

        held = {s for s, p in cycles.portfolio.positions.items() if not p.is_flat}
        unpriced = sorted(held - set(feed.symbols))
        if unpriced:
            raise ReconciliationError(
                f"{cfg.account} holds {unpriced} but the feed carries {sorted(feed.symbols)}; "
                "a position nobody prices cannot be risk-managed. Add the symbol or close it."
            )

        lock = self._acquire_lock()
        session = (
            f"{cfg.account}-{(self.now or datetime.now(UTC)):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        )
        journal = DecisionJournal(cfg.account_dir, session)
        self._journal = journal
        self._stopped = ""
        self._halt_logged = risk.halted
        self._feed = feed
        self._session_fills = []
        self._first_bar_checked = False
        self._cycle_failed = None

        engine = Backtester(
            instruments=dict(cfg.instruments),
            starting_cash=cfg.starting_cash,
            cost_model=cfg.cost_model,
            risk_manager=risk,
            funding=cfg.funding,
            order_observer=self._on_order,
        )
        journal.note(
            self.now or datetime.now(UTC),
            "session start",
            cycle=str(cycles.number),
            strategy=self.strategy.name,
            halted=str(risk.halted),
        )
        result: BacktestResult | None = None
        try:
            result = engine.run(_Supervised(self.strategy, self), feed, portfolio=cycles.portfolio)
        except CycleFailed as failed:
            self._cycle_failed = failed.record
            journal.note(
                failed.record.closed_at or self.now or datetime.now(UTC),
                "session end",
                stopped_by="cycle failed",
                cycle=str(cycles.number),
            )
        except SessionStopped as stopped:
            journal.note(
                self._watermark or self.now or datetime.now(UTC),
                "session end",
                stopped_by=stopped.reason,
                cycle=str(cycles.number),
            )
        finally:
            journal.close()
            self._release_lock(lock)
        return SessionResult(
            session=session,
            result=result,
            fills=tuple(self._session_fills),
            cycle=cycles.number,
            cycle_failed=self._cycle_failed,
            stopped_by=self._stopped or "feed ended",
            journal_events=journal.written,
        )

    def _acquire_lock(self) -> Path:
        path = self.config.account_dir / LOCK_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise AccountLocked(
                f"{path} exists: another supervisor holds {self.config.account!r}. If it is "
                "dead, remove the file by hand — that is a decision, not a retry."
            ) from None
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {datetime.now(UTC).isoformat()}\n")
        return path

    @staticmethod
    def _release_lock(path: Path) -> None:
        path.unlink(missing_ok=True)

    # -- hooks the wrapper calls -----------------------------------------------------

    def _state(self) -> AccountState:
        assert self._cycles is not None
        pf = self._cycles.portfolio
        return AccountState(
            cash=pf.cash,
            equity=pf.marked_equity,
            margin_reserved=pf.margin_reserved,
            free_cash=pf.free_cash,
            positions={s: p.quantity for s, p in pf.positions.items() if not p.is_flat},
        )

    def _marks(self) -> dict[str, Decimal]:
        assert self._cycles is not None
        pf = self._cycles.portfolio
        return {
            s: price
            for s in pf.positions
            if (price := pf.last_price(s)) is not None and not pf.positions[s].is_flat
        }

    def _on_order(self, order: Order) -> None:
        assert self._journal is not None and self._cycles is not None
        at = order.created_at or self.now or datetime.now(UTC)
        self._cycles.record_order(order)
        self._journal.order(order, at, self._state())

    def _on_fill(self, fill: Fill) -> None:
        assert self._journal is not None
        self._session_fills.append(fill)
        self._journal.fill(fill, self._state())

    def _on_reject(self, rejection: RiskRejection) -> None:
        assert self._journal is not None
        self._journal.rejection(
            rejection.timestamp, rejection.symbol, rejection.reason, rejection.detail, self._state()
        )
        tracker = self.config.tracker
        if tracker is not None and self._cycles is not None:
            try:
                tracker.record_rejections(
                    [rejection],
                    account=self.config.account,
                    cycle=self._cycles.number,
                    stage=self.config.stage,
                )
            except Exception as exc:  # an observer never stops the session
                self._track_failures += 1
                self._track_last_error = f"{type(exc).__name__}: {exc}"

    def _track_mark(self, at: datetime) -> None:
        """One mark to the tracker, if there is one. Never raises.

        **Building the mark is inside the boundary too.** It used to sit outside,
        so a `ValueError` from `mark_from_portfolio` killed the session — an
        observer taking down the thing it observes, which is exactly what the
        `MarkSink` contract forbids. The identity that raised has been fixed at
        source, and this is the belt: whatever a future mark builder does, a
        session with a tracker attached must behave like one without.
        """
        tracker = self.config.tracker
        if tracker is None or self._cycles is None or self._risk is None:
            return
        try:
            mark = mark_from_portfolio(
                self._cycles.portfolio,
                account=self.config.account,
                cycle=self._cycles.number,
                stage=self.config.stage,
                at=at,
                peak_equity=self._risk.peak_equity,
                orders=self._cycles.current.orders,
            )
            tracker.record_mark(mark)
        except Exception as exc:
            # Both the BUILD and the RECORD are inside the boundary. `MarkSink`
            # says neither may raise, and `StoreTracker` honours that — but a
            # contract the caller does not enforce is a contract that holds
            # until someone writes a second implementation. An observer must
            # never stop the session it observes.
            self._track_failures += 1
            self._track_last_error = f"{type(exc).__name__}: {exc}"

    def _gate(self, at: datetime, ctx: StrategyContext, *, new_bar: bool) -> bool:
        """Every check that decides whether the strategy may run, in one place.

        Called from `on_bar` (after the engine has revealed the bar and
        re-marked) and from `on_cross_section`. Order: the account must be
        alive; a halt is journalled the moment it is seen, even if a stop has
        already fired; then the kill switch; then the daily loss limit.
        """
        assert self._journal is not None and self._cycles is not None and self._risk is not None
        journal = self._journal

        if new_bar and not self._first_bar_checked:
            self._first_bar_checked = True
            opened = self._cycles.current.opened_at
            if at <= opened:
                raise ValueError(
                    f"the feed's first bar ({at.isoformat()}) is not after the cycle opened "
                    f"({opened.isoformat()}). Pass now= at or before the first bar; a cycle "
                    "cannot close before it opened, and a fill before it would be lost."
                )
        new_instant = new_bar and at != self._watermark
        if new_bar:
            self._watermark = at
        if new_instant:
            journal.mark(
                at, self._state(), prices=self._marks(), peak_equity=self._risk.peak_equity
            )
            # The journal first, always: it needs only a filesystem. The tracker
            # is the queryable copy and is allowed to be absent or broken.
            #
            # The mark tracked here is the PREVIOUS instant's, not this one's.
            # The engine emits one bar event per symbol per instant, so at the
            # first bar of a new instant only that symbol has been revealed and
            # every other holding is still marked at its last price. Writing
            # then produced a row that claimed to be the account's full state
            # and carried stale prices for all but one symbol — and
            # `marks_once` made any later correction a silently dropped
            # duplicate (adversarial review, 2026-09-04). By the time a new
            # instant begins, the previous one is complete.
            if self._tracked_watermark is not None:
                self._track_mark(self._tracked_watermark)
            self._tracked_watermark = at

        outcome = self._cycles.mark(at)
        if isinstance(outcome, CycleRecord):
            journal.halt(
                at,
                f"cycle {outcome.number} failed: equity {outcome.ending_equity}",
                kind="cycle_failed",
            )
            journal.note(at, "cycle opened", cycle=str(self._cycles.number))
            self._stopped = "cycle failed"
            journal.note(
                at, "session stop", reason="cycle failed", detail=str(outcome.ending_equity)
            )
            raise CycleFailed(outcome)

        if self._risk.halted and not self._halt_logged:
            self._halt_logged = True
            journal.halt(at, "max drawdown breached", self._state(), kind="drawdown")

        if self._stopped:
            return False

        if self.config.kill_switch is not None and self.config.kill_switch.exists():
            self._stop(at, "kill switch", f"{self.config.kill_switch} present")

        equity = ctx.equity
        day = at.astimezone(UTC).date()
        if day != self._day:
            self._day = day
            self._day_open_equity = equity
        limit = self.config.max_daily_loss_pct
        if limit is not None and self._day_open_equity is not None and self._day_open_equity > 0:
            loss = (self._day_open_equity - equity) / self._day_open_equity
            if loss > limit:
                journal.halt(
                    at, f"daily loss {loss:.4f} > {limit}", self._state(), kind="daily_loss"
                )
                self._stop(at, "daily loss limit", f"{loss:.4f} of opening equity lost")
        return True

    def _stop(self, at: datetime, why: str, detail: str) -> None:
        """Journal the stop, stop the feed, and end the engine run by exception.

        Nothing queued fills after this: a kill switch that let working orders
        fill on later bars was a kill switch in name only."""
        self._stopped = why
        assert self._journal is not None
        self._journal.note(at, "session stop", reason=why, detail=detail)
        stop = getattr(self._feed, "stop", None)
        if callable(stop):
            stop()
        raise SessionStopped(why)

    def _on_finish(self) -> None:
        assert self._journal is not None and self._cycles is not None and self._risk is not None
        if self._watermark is not None and not self._stopped:
            # The closing mark, with every bar of the last instant revealed.
            self._journal.mark(
                self._watermark,
                self._state(),
                prices=self._marks(),
                peak_equity=self._risk.peak_equity,
            )
            # And the tracker gets it too. Without this the last instant of every
            # session was journalled and never stored, so the database's view of
            # an account stopped one instant before the session did.
            self._track_mark(self._watermark)
        self._journal.note(
            self._watermark or self.now or datetime.now(UTC),
            "session end",
            stopped_by=self._stopped or "feed ended",
            cycle=str(self._cycles.number),
        )


def _fill_from(event: JournalEvent) -> Fill:
    d = event.detail
    return Fill(
        order_id=d["order_id"],
        symbol=event.symbol,
        timestamp=event.timestamp,
        side=Side[d["side"]],
        quantity=Decimal(d["quantity"]),
        price=Decimal(d["price"]),
        commission=Decimal(d.get("commission", "0")),
        slippage=Decimal(d.get("slippage", "0")),
    )
