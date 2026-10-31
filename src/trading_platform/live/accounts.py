"""Paper account cycles (charter §28).

A paper account is a sequence of **cycles**. Each cycle starts with the same
cash, runs until its equity reaches zero, and is then closed as FAILED and
frozen; the next cycle opens with a fresh ledger and the next number. Nothing
is ever deleted or rewritten: the store is one append-only JSONL file per
account, and a closed cycle is a frozen record. That is what makes cycles
comparable across months — a strategy that fails three accounts in a row has
a history that says so, in the same units, with no way to tidy it.

The ledger is **USD**. The charter's £1,000 is honoured as a *display*
conversion through `core.fx` (`in_currency`), which refuses to convert without
a rate series rather than assume one — the same rule the rest of the
repository applies to quote assets, and for the same reason: a currency
mixed silently into a ledger is how a plausible wrong number is born
(ADR 0002, ADR 0007).

Cycles decide nothing about *trading*. Risk limits, fills and marks come
from the engine and the supervisor; this module keeps the books and the
history. The supervisor calls `apply_fill`, `observe_price` and `mark`; the
one policy here is *equity ≤ 0 closes the cycle*.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from trading_platform.core.events import Fill, Order
from trading_platform.core.fx import QuoteRates, UnknownQuoteRate
from trading_platform.core.instruments import Instrument
from trading_platform.core.types import quantize_cash
from trading_platform.engine.portfolio import EquityPoint, Portfolio

__all__ = [
    "AccountCycles",
    "CycleError",
    "CycleOutcome",
    "CycleRecord",
    "CycleSnapshot",
    "cycles_path",
    "read_cycles",
]

ACCOUNTS_DIRNAME = "accounts"


class CycleError(RuntimeError):
    """The store is inconsistent, or a caller asked for something that would rewrite history."""


class CycleOutcome(StrEnum):
    OPEN = "OPEN"
    #: Equity reached zero. The charter's word.
    FAILED = "FAILED"
    #: Closed deliberately with equity above zero (strategy retired, account
    #: reset by a human decision). Recorded with the reason.
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class CycleRecord:
    """One cycle's history. Frozen: a closed cycle cannot be edited, only read."""

    account: str
    number: int
    currency: str
    starting_cash: Decimal
    opened_at: datetime
    outcome: CycleOutcome
    closed_at: datetime | None = None
    ending_equity: Decimal | None = None
    orders: int = 0
    fills: int = 0
    fees: Decimal = Decimal("0")
    realised_pnl: Decimal = Decimal("0")
    max_drawdown_pct: Decimal = Decimal("0")
    reason: str = ""

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError(f"cycle numbers start at 1, got {self.number}")
        if self.starting_cash <= 0:
            raise ValueError(f"starting_cash must be positive, got {self.starting_cash}")
        if self.opened_at.tzinfo is None:
            raise ValueError("opened_at must be timezone-aware")
        if self.outcome is CycleOutcome.OPEN:
            if self.closed_at is not None or self.ending_equity is not None:
                raise ValueError("an OPEN cycle has no close")
        else:
            if self.closed_at is None or self.ending_equity is None:
                raise ValueError(f"a {self.outcome} cycle needs closed_at and ending_equity")
            if self.closed_at < self.opened_at:
                raise ValueError("a cycle cannot close before it opened")
            if self.outcome is CycleOutcome.FAILED and self.ending_equity > 0:
                raise ValueError(
                    f"FAILED means equity reached zero; ending_equity is {self.ending_equity}"
                )

    @property
    def is_open(self) -> bool:
        return self.outcome is CycleOutcome.OPEN

    @property
    def return_pct(self) -> Decimal | None:
        if self.ending_equity is None:
            return None
        return (self.ending_equity - self.starting_cash) / self.starting_cash

    def in_currency(self, rates: QuoteRates) -> CycleRecord:
        """The same record restated in `rates.reporting_currency`.

        Every amount is converted at the rate known at *its own* instant —
        the opening cash at `opened_at`, the ending equity at `closed_at` —
        because a cycle that ran for a month spans a month of exchange-rate
        path, and converting both ends at today's rate would invent a return.

        Raises `UnknownQuoteRate` when no series covers this cycle's currency.
        There is no default rate; see `core.fx`.
        """
        target = rates.reporting_currency
        if target == self.currency:
            return self
        if not rates.covers(self.currency):
            raise UnknownQuoteRate(
                f"no {self.currency}->{target} rate series; cannot display cycle "
                f"{self.number} in {target}. Supply QuoteRates(reporting_currency="
                f"{target!r}) with a {self.currency!r} series."
            )
        open_rate = rates.rate(self.currency, self.opened_at)
        close_at = self.closed_at
        ending = self.ending_equity
        if close_at is not None and ending is not None:
            close_rate = rates.rate(self.currency, close_at)
            ending = quantize_cash(ending * close_rate)
            realised = quantize_cash(self.realised_pnl * close_rate)
            fees = quantize_cash(self.fees * close_rate)
        else:
            realised = quantize_cash(self.realised_pnl * open_rate)
            fees = quantize_cash(self.fees * open_rate)
        return CycleRecord(
            account=self.account,
            number=self.number,
            currency=target,
            starting_cash=quantize_cash(self.starting_cash * open_rate),
            opened_at=self.opened_at,
            outcome=self.outcome,
            closed_at=close_at,
            ending_equity=ending,
            orders=self.orders,
            fills=self.fills,
            fees=fees,
            realised_pnl=realised,
            max_drawdown_pct=self.max_drawdown_pct,  # a ratio; currency-free
            reason=self.reason,
        )

    def as_line(self) -> str:
        payload: dict[str, object] = {
            "account": self.account,
            "number": self.number,
            "currency": self.currency,
            "starting_cash": str(self.starting_cash),
            "opened_at": self.opened_at.astimezone(UTC).isoformat(),
            "outcome": str(self.outcome),
            "closed_at": None
            if self.closed_at is None
            else self.closed_at.astimezone(UTC).isoformat(),
            "ending_equity": None if self.ending_equity is None else str(self.ending_equity),
            "orders": self.orders,
            "fills": self.fills,
            "fees": str(self.fees),
            "realised_pnl": str(self.realised_pnl),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "reason": self.reason,
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_line(cls, line: str) -> CycleRecord:
        raw = json.loads(line)
        closed = raw.get("closed_at")
        ending = raw.get("ending_equity")
        return cls(
            account=raw["account"],
            number=int(raw["number"]),
            currency=raw["currency"],
            starting_cash=Decimal(raw["starting_cash"]),
            opened_at=datetime.fromisoformat(raw["opened_at"]),
            outcome=CycleOutcome(raw["outcome"]),
            closed_at=None if closed is None else datetime.fromisoformat(closed),
            ending_equity=None if ending is None else Decimal(ending),
            orders=int(raw.get("orders", 0)),
            fills=int(raw.get("fills", 0)),
            fees=Decimal(raw.get("fees", "0")),
            realised_pnl=Decimal(raw.get("realised_pnl", "0")),
            max_drawdown_pct=Decimal(raw.get("max_drawdown_pct", "0")),
            reason=raw.get("reason", ""),
        )


@dataclass(frozen=True, slots=True)
class CycleSnapshot:
    """The §28 tracked fields for the open cycle at one instant."""

    account: str
    cycle: int
    at: datetime
    cash: Decimal
    positions: dict[str, Decimal]
    orders: int
    fills: int
    fees: Decimal
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    equity: Decimal
    drawdown_pct: Decimal
    exposure: Decimal


def cycles_path(root: Path | str, account: str) -> Path:
    if not account or "/" in account or account in (".", ".."):
        raise ValueError(f"account name must be a plain identifier, got {account!r}")
    return Path(root) / ACCOUNTS_DIRNAME / account / "cycles.jsonl"


def read_cycles(path: Path | str) -> list[CycleRecord]:
    """Every record in the file, in order. Each cycle appears once as OPEN and,
    if it has closed, once more with its outcome — the two lines are both kept,
    because the file is the history and history is not rewritten."""
    path = Path(path)
    if not path.exists():
        return []
    records = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(CycleRecord.from_line(line))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise CycleError(f"{path}:{n}: unreadable cycle record: {exc}") from exc
    return records


@dataclass(slots=True)
class AccountCycles:
    """The open cycle's ledger plus the frozen history of the ones before it.

    `starting_cash` has **no default**: it is the one number every cycle
    inherits, and a default here would be exactly the silently-inherited
    figure the never-silently list forbids. State it.
    """

    root: Path
    account: str
    starting_cash: Decimal
    currency: str = "USD"
    now: datetime | None = None
    _portfolio: Portfolio = field(init=False)
    _open: CycleRecord = field(init=False)
    _history: list[CycleRecord] = field(init=False, default_factory=list)
    _peak: Decimal = field(init=False)
    _max_dd: Decimal = field(init=False, default=Decimal("0"))
    _orders: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        if self.starting_cash <= 0:
            raise ValueError(f"starting_cash must be positive, got {self.starting_cash}")
        self.root = Path(self.root)
        path = cycles_path(self.root, self.account)
        records = read_cycles(path)
        closed = [r for r in records if not r.is_open]
        opens = [r for r in records if r.is_open]
        numbers = [r.number for r in closed]
        if len(set(numbers)) != len(numbers):
            raise CycleError(
                f"{path}: a cycle number closes twice ({numbers}); two writers have shared "
                "this account. The store is append-only, so this is history that must be "
                "read by a person, not skipped."
            )
        if numbers != list(range(1, len(numbers) + 1)):
            raise CycleError(f"{path}: closed cycles are not 1..n in order ({numbers})")
        self._history = closed
        expected_next = len(closed) + 1
        still_open = [r for r in opens if r.number == expected_next]
        if any(r.number > expected_next for r in opens):
            raise CycleError(
                f"{path}: an open cycle is numbered past the history; refusing to guess"
            )
        if still_open:
            self._open = still_open[0]
            if (
                self._open.starting_cash != self.starting_cash
                or self._open.currency != self.currency
            ):
                raise CycleError(
                    f"{path}: cycle {self._open.number} was opened with "
                    f"{self._open.starting_cash} {self._open.currency}, not "
                    f"{self.starting_cash} {self.currency}. Cycles do not change terms mid-way."
                )
        else:
            self._open = self._begin(expected_next)
        self._portfolio = Portfolio(starting_cash=self.starting_cash)
        self._peak = self.starting_cash

    # -- history ---------------------------------------------------------------

    @property
    def history(self) -> tuple[CycleRecord, ...]:
        """Closed cycles, oldest first. Frozen records; nothing here can be edited."""
        return tuple(self._history)

    @property
    def current(self) -> CycleRecord:
        return self._open

    @property
    def number(self) -> int:
        return self._open.number

    @property
    def portfolio(self) -> Portfolio:
        """The open cycle's ledger. Shared with the engine: the supervisor
        passes this very object to `Backtester.run(portfolio=...)`, so there
        is one set of books, not two that must agree."""
        return self._portfolio

    @property
    def equity_curve(self) -> list[EquityPoint]:
        return self._portfolio.equity_curve

    def report(self, rates: QuoteRates | None = None) -> str:
        """The charter's example, one line per cycle. `rates` restates it."""
        lines = []
        for record in (*self._history, self._open):
            shown = record if rates is None else record.in_currency(rates)
            cur = shown.currency
            if shown.is_open:
                lines.append(
                    f"Account {shown.number}  {shown.starting_cash} {cur} -> "
                    f"{self.equity} {self.currency}  OPEN"
                )
            else:
                lines.append(
                    f"Account {shown.number}  {shown.starting_cash} {cur} -> "
                    f"{shown.ending_equity} {cur}  {shown.outcome}"
                )
        return "\n".join(lines)

    # -- the open cycle's books --------------------------------------------------

    @property
    def equity(self) -> Decimal:
        return self._portfolio.marked_equity

    def record_order(self, order: Order) -> None:
        """Count intent. Fills are counted by `apply_fill`; the gap between the
        two is the risk layer, and §28 asks for both."""
        self._orders += 1

    def observe_price(self, instrument: Instrument, price: Decimal, at: datetime) -> None:
        self._portfolio.observe_price(instrument.symbol, price, at)

    def apply_fill(self, instrument: Instrument, fill: Fill) -> None:
        self._portfolio.apply_fill(instrument, fill)

    def restore(
        self,
        instruments: Mapping[str, Instrument],
        fills: Sequence[Fill],
        *,
        orders: int = 0,
        peak_equity: Decimal | None = None,
    ) -> None:
        """Rebuild the open cycle's books from its journalled fills, at boot.

        The supervisor's reconcile-on-boot: the journal is the record, the
        ledger is derived from it, and the two are then compared. Each fill's
        price becomes that symbol's mark, stamped with the fill's own time, so
        `mark_age` reports the staleness honestly until a fresh bar arrives.

        Refused once the cycle has any fills of its own — a restore is a
        boot-time act, not a way to rewrite a running book. A fill for a
        symbol not in `instruments` raises: guessing the instrument would
        guess its accounting.
        """
        if self._portfolio.fills:
            raise CycleError("restore() only on a fresh cycle; this one already has fills")
        for fill in fills:
            instrument = instruments.get(fill.symbol)
            if instrument is None:
                raise CycleError(
                    f"journal holds a fill for {fill.symbol!r}, which this account does not "
                    "trade; refusing to guess its instrument"
                )
            self._portfolio.apply_fill(instrument, fill)
            self._portfolio.observe_price(fill.symbol, fill.price, fill.timestamp)
        self._orders = orders
        if peak_equity is not None:
            self._peak = max(peak_equity, self.starting_cash)

    def mark(self, at: datetime) -> CycleSnapshot | CycleRecord:
        """Evaluate the cycle at the current marks. Returns the snapshot, or —
        when equity has reached zero — the **closed** record of the cycle that
        just failed, with the next cycle already open behind it.

        Does not append to the portfolio's equity curve: the engine owns the
        curve (one point per instant) and this may be called from inside it.
        The caller sees a failure as a return value rather than a flag so a
        supervisor cannot keep trading a dead cycle by forgetting to check.
        """
        equity = self.equity
        if equity > self._peak:
            self._peak = equity
        if self._peak > 0:
            drawdown = (self._peak - equity) / self._peak
            if drawdown > self._max_dd:
                self._max_dd = drawdown
        if equity <= 0:
            return self._close(at, CycleOutcome.FAILED, "equity reached zero")
        return self.snapshot(at)

    def snapshot(self, at: datetime) -> CycleSnapshot:
        pf = self._portfolio
        positions = {s: p.quantity for s, p in pf.positions.items() if not p.is_flat}
        unrealised = Decimal("0")
        exposure = Decimal("0")
        for symbol, pos in pf.positions.items():
            if pos.is_flat:
                continue
            price = pf.last_price(symbol)
            if price is None:
                continue
            unrealised += pos.unrealised_pnl(price)
            exposure += abs(pos.instrument.notional(price, pos.quantity))
        equity = self.equity
        drawdown = (self._peak - equity) / self._peak if self._peak > 0 else Decimal("0")
        return CycleSnapshot(
            account=self.account,
            cycle=self._open.number,
            at=at,
            cash=pf.cash,
            positions=positions,
            orders=self._orders,
            fills=len(pf.fills),
            fees=pf.total_commission,
            realised_pnl=pf.total_realised_pnl,
            unrealised_pnl=quantize_cash(unrealised),
            equity=equity,
            drawdown_pct=max(drawdown, Decimal("0")),
            exposure=quantize_cash(exposure),
        )

    def close(self, at: datetime, reason: str) -> CycleRecord:
        """Close the open cycle deliberately, with equity above zero.

        A human decision (strategy retired, terms changed). The reason is
        recorded because a closed cycle with no reason reads as a failure
        somebody tidied."""
        if not reason.strip():
            raise CycleError("closing a cycle needs a reason; it is part of the record")
        if self.equity <= 0:
            return self._close(at, CycleOutcome.FAILED, reason)
        return self._close(at, CycleOutcome.CLOSED, reason)

    # -- internals ---------------------------------------------------------------

    def _begin(self, number: int) -> CycleRecord:
        opened = self.now or datetime.now(UTC)
        record = CycleRecord(
            account=self.account,
            number=number,
            currency=self.currency,
            starting_cash=self.starting_cash,
            opened_at=opened,
            outcome=CycleOutcome.OPEN,
        )
        self._append(record)
        return record

    def _close(self, at: datetime, outcome: CycleOutcome, reason: str) -> CycleRecord:
        pf = self._portfolio
        closed = CycleRecord(
            account=self.account,
            number=self._open.number,
            currency=self.currency,
            starting_cash=self.starting_cash,
            opened_at=self._open.opened_at,
            outcome=outcome,
            closed_at=at,
            ending_equity=self.equity,
            orders=self._orders,
            fills=len(pf.fills),
            fees=pf.total_commission,
            realised_pnl=pf.total_realised_pnl,
            max_drawdown_pct=self._max_dd,
            reason=reason,
        )
        self._append(closed)
        self._history.append(closed)
        # The next cycle: same terms, fresh books, nothing carried over — not
        # the positions, not the prices' history, not the peak.
        self.now = at
        self._open = self._begin(closed.number + 1)
        self._portfolio = Portfolio(starting_cash=self.starting_cash)
        self._peak = self.starting_cash
        self._max_dd = Decimal("0")
        self._orders = 0
        return closed

    def _append(self, record: CycleRecord) -> None:
        path = cycles_path(self.root, self.account)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(record.as_line() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
