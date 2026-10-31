"""The decision journal: what the system did, and what it knew when it did it.

The recorder captures the market. This captures the *reasoning* — and the
difference decides whether tomorrow's review is possible at all.

A fill on its own is not reviewable. "Bought 0.4 BTC at 09:14" answers nothing
the next morning: not whether the strategy wanted more and the risk layer
refused, not what equity it was sizing against, not whether it was one of forty
attempts or the only one. Every question worth asking a day later is about the
*state at the moment of the decision*, and that state is gone unless it was
written down beside the outcome.

So each event carries the account as it stood, and refusals are first-class
records rather than absences. A day with three fills and four hundred rejections
is a completely different object of study from a day with three fills — and in
the archive, if only fills are kept, they are indistinguishable.

Append-only JSONL, one file per UTC day, prices as strings. A restart inside a
day appends to the same file with a new `session` id, so the seam is visible
rather than silently healed.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from trading_platform.core.events import Fill, Order

DECISIONS_DIRNAME = "decisions"


class EventKind(StrEnum):
    """What a journal line is about.

    `REJECTION` and `HALT` are the reason this is an enum rather than a fill
    log. They are the events that explain the absences, and absences are what a
    reader misinterprets.
    """

    ORDER = "order"
    FILL = "fill"
    REJECTION = "rejection"
    HALT = "halt"
    MARK = "mark"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class AccountState:
    """The account as it stood when a decision was taken.

    Carried on every event rather than sampled periodically. Sampling would
    leave each decision to be matched against the nearest snapshot, and "nearest"
    is exactly the kind of approximation that makes a post-mortem argue with
    itself.
    """

    cash: Decimal
    equity: Decimal
    margin_reserved: Decimal
    free_cash: Decimal
    #: Signed quantity per held symbol. Added 2026-09-02 because cash alone
    #: cannot reconcile a derivative book: a perpetual fill moves cash by
    #: commission only, so a journal whose fill quantities had been rewritten
    #: still "agreed" on cash. Older lines without it read back as empty.
    positions: Mapping[str, Decimal] = field(default_factory=dict)

    def as_dict(self) -> dict[str, str]:
        out = {
            "cash": str(self.cash),
            "equity": str(self.equity),
            "margin_reserved": str(self.margin_reserved),
            "free_cash": str(self.free_cash),
        }
        for symbol, qty in sorted(self.positions.items()):
            if qty != 0:
                out[f"pos:{symbol}"] = str(qty)
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, str]) -> AccountState:
        return cls(
            cash=Decimal(raw["cash"]),
            equity=Decimal(raw["equity"]),
            margin_reserved=Decimal(raw["margin_reserved"]),
            free_cash=Decimal(raw["free_cash"]),
            positions={k[4:]: Decimal(v) for k, v in raw.items() if k.startswith("pos:")},
        )


@dataclass(frozen=True, slots=True)
class JournalEvent:
    timestamp: datetime
    kind: EventKind
    session: str
    symbol: str = ""
    #: Free-form, kind-specific. Kept as strings so a reader never has to guess
    #: whether a number was a float or a Decimal on the way in.
    detail: dict[str, str] = None  # type: ignore[assignment]
    account: AccountState | None = None

    def __post_init__(self) -> None:
        if self.detail is None:
            object.__setattr__(self, "detail", {})
        if self.timestamp.tzinfo is None:
            raise ValueError(f"journal timestamps must be timezone-aware, got {self.timestamp!r}")

    def as_line(self) -> str:
        payload: dict[str, object] = {
            "t": int(self.timestamp.timestamp() * 1000),
            "k": str(self.kind),
            "s": self.session,
        }
        if self.symbol:
            payload["sym"] = self.symbol
        if self.detail:
            payload["d"] = self.detail
        if self.account is not None:
            payload["a"] = self.account.as_dict()
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_line(cls, line: str) -> JournalEvent:
        raw = json.loads(line)
        account = raw.get("a")
        return cls(
            timestamp=datetime.fromtimestamp(raw["t"] / 1000, tz=UTC),
            kind=EventKind(raw["k"]),
            session=raw["s"],
            symbol=raw.get("sym", ""),
            detail=raw.get("d", {}),
            account=AccountState.from_dict(account) if account else None,
        )


def decisions_path(root: Path | str, day: datetime) -> Path:
    if day.tzinfo is None:
        raise ValueError(f"day must be timezone-aware, got {day!r}")
    return Path(root) / DECISIONS_DIRNAME / f"{day.astimezone(UTC):%Y-%m-%d}.jsonl"


class DecisionJournal:
    """Append-only record of decisions, keyed by UTC day.

    `session` distinguishes runs. A supervisor restarting at 14:03 appends to
    the same day's file under a new id, so a reader sees a seam where one
    happened instead of a continuous history that never existed.
    """

    def __init__(self, root: Path | str, session: str, *, flush: bool = True) -> None:
        if not session:
            raise ValueError("a journal needs a session id; an unattributed record is not one")
        self._root = Path(root)
        self._session = session
        self._flush = flush
        self._handles: dict[str, object] = {}
        self.written = 0

    @property
    def session(self) -> str:
        return self._session

    def append(self, event: JournalEvent) -> None:
        day = event.timestamp.astimezone(UTC).strftime("%Y-%m-%d")
        handle = self._handles.get(day)
        if handle is None:
            path = decisions_path(self._root, event.timestamp)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8")
            self._handles[day] = handle
        handle.write(event.as_line() + "\n")  # type: ignore[attr-defined]
        if self._flush:
            handle.flush()  # type: ignore[attr-defined]
            os.fsync(handle.fileno())  # type: ignore[attr-defined]
        self.written += 1

    # -- the four things worth recording, named so a caller cannot mix them up --

    def order(self, order: Order, at: datetime, account: AccountState | None = None) -> None:
        """Intent. Recorded before the risk layer sees it, so what the strategy
        WANTED survives even when nothing came of it."""
        detail = {
            "order_id": order.order_id,
            "side": str(order.side.name),
            "quantity": str(order.quantity),
            "type": str(order.order_type.name),
        }
        if order.limit_price is not None:
            detail["limit_price"] = str(order.limit_price)
        if order.tag:
            detail["tag"] = order.tag
        self.append(JournalEvent(at, EventKind.ORDER, self._session, order.symbol, detail, account))

    def fill(self, fill: Fill, account: AccountState | None = None) -> None:
        self.append(
            JournalEvent(
                fill.timestamp,
                EventKind.FILL,
                self._session,
                fill.symbol,
                {
                    "order_id": fill.order_id,
                    "side": str(fill.side.name),
                    "quantity": str(fill.quantity),
                    "price": str(fill.price),
                    "commission": str(fill.commission),
                    "slippage": str(fill.slippage),
                },
                account,
            )
        )

    def rejection(
        self,
        at: datetime,
        symbol: str,
        reason: str,
        detail: str,
        account: AccountState | None = None,
    ) -> None:
        """A refusal is a record, not an absence.

        Four hundred rejections and three fills is a different day from three
        fills, and only one of those two days is the strategy's fault.
        """
        self.append(
            JournalEvent(
                at,
                EventKind.REJECTION,
                self._session,
                symbol,
                {"reason": reason, "detail": detail},
                account,
            )
        )

    def halt(
        self,
        at: datetime,
        reason: str,
        account: AccountState | None = None,
        *,
        kind: str = "drawdown",
    ) -> None:
        """`kind` says which control tripped: "drawdown" (the risk layer's
        terminal halt, restored on restart), "daily_loss" (resets with the
        day), "cycle_failed". Without it a daily limit read back as a
        cycle-long halt (adversarial-reviewer, 2026-09-02)."""
        self.append(
            JournalEvent(
                at, EventKind.HALT, self._session, "", {"reason": reason, "kind": kind}, account
            )
        )

    def mark(
        self,
        at: datetime,
        account: AccountState,
        *,
        prices: Mapping[str, Decimal] | None = None,
        peak_equity: Decimal | None = None,
    ) -> None:
        """Periodic equity snapshot, so a flat day still has a curve.

        Carries the marks (`px:<symbol>`) and the risk layer's peak equity so a
        restart can re-mark the book at what it was last worth and restore the
        drawdown reference, rather than at stale fill prices and an
        under-sampled peak (adversarial-reviewer, 2026-09-02)."""
        detail: dict[str, str] = {}
        for symbol, price in sorted((prices or {}).items()):
            detail[f"px:{symbol}"] = str(price)
        if peak_equity is not None:
            detail["peak"] = str(peak_equity)
        self.append(JournalEvent(at, EventKind.MARK, self._session, "", detail, account))

    def note(self, at: datetime, message: str, **fields: str) -> None:
        """Lifecycle and transport: startup, reconnects, gaps, shutdown."""
        self.append(
            JournalEvent(
                at, EventKind.NOTE, self._session, "", {"message": message, **fields}, None
            )
        )

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()  # type: ignore[attr-defined]
        self._handles.clear()

    def __enter__(self) -> DecisionJournal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_decisions(path: Path | str) -> list[JournalEvent]:
    """Read a day's decisions, discarding only a torn final line.

    Same rule as the bar journal: a kill tears the last line and nothing else,
    so damage elsewhere is corruption and raises rather than silently shortening
    the record a review is about to draw conclusions from.
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"no decision journal at {source}")

    lines = source.read_text(encoding="utf-8").splitlines()
    events: list[JournalEvent] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            events.append(JournalEvent.from_line(line))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            if index == len(lines) - 1:
                break
            raise ValueError(
                f"{source}: line {index + 1} of {len(lines)} is unreadable ({exc}). "
                "Only a torn final line is expected."
            ) from exc
    return events


@dataclass(frozen=True, slots=True)
class DaySummary:
    """One day, reduced to what a review actually opens with."""

    day: str
    sessions: tuple[str, ...]
    counts: dict[str, int]
    rejections_by_reason: dict[str, int]
    fills_by_symbol: dict[str, int]
    halts: int
    opening_equity: Decimal | None
    closing_equity: Decimal | None
    notes: tuple[str, ...]

    @property
    def intent_refused(self) -> float | None:
        """Share of order intent the risk layer refused.

        `BacktestResult.rejection_rate` exists to print "these figures describe
        the risk layer, not the strategy". The same question, asked of a live
        day, and it is the first thing worth knowing.
        """
        orders = self.counts.get(str(EventKind.ORDER), 0)
        rejections = self.counts.get(str(EventKind.REJECTION), 0)
        total = orders
        return rejections / total if total else None

    @property
    def equity_change(self) -> Decimal | None:
        if self.opening_equity is None or self.closing_equity is None:
            return None
        return self.closing_equity - self.opening_equity

    def report(self) -> str:
        lines = [f"{self.day} — {len(self.sessions)} session(s): {', '.join(self.sessions)}"]
        if self.opening_equity is not None and self.closing_equity is not None:
            change = self.equity_change
            assert change is not None
            lines.append(
                f"Equity          : {self.opening_equity} -> {self.closing_equity} ({change:+})"
            )
        lines.append(
            f"Orders          : {self.counts.get(str(EventKind.ORDER), 0)}  "
            f"fills {self.counts.get(str(EventKind.FILL), 0)}  "
            f"rejections {self.counts.get(str(EventKind.REJECTION), 0)}"
        )
        refused = self.intent_refused
        if refused is not None and refused > 0:
            lines.append(f"Intent refused  : {refused:.0%}")
            if refused > 0.5:
                lines.append(
                    "                  Over half of what the strategy asked for was refused. "
                    "These figures describe the risk layer, not the strategy."
                )
        if self.rejections_by_reason:
            ordered = sorted(self.rejections_by_reason.items(), key=lambda kv: -kv[1])
            lines.append("Refusals        : " + ", ".join(f"{r} x{n}" for r, n in ordered))
        if self.fills_by_symbol:
            ordered = sorted(self.fills_by_symbol.items(), key=lambda kv: -kv[1])
            lines.append("Fills by symbol : " + ", ".join(f"{s} x{n}" for s, n in ordered))
        if self.halts:
            lines.append(f"HALTED          : {self.halts} time(s) — the run stopped taking risk")
        if len(self.sessions) > 1:
            lines.append(
                f"RESTARTED       : {len(self.sessions) - 1} time(s) mid-day. The record has "
                "seams; positions were reconciled at each one."
            )
        for note in self.notes[:10]:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


def summarise_day(events: Sequence[JournalEvent]) -> DaySummary:
    """Reduce a day's events to the summary a review opens with."""
    if not events:
        return DaySummary("(empty)", (), {}, {}, {}, 0, None, None, ())

    day = events[0].timestamp.astimezone(UTC).strftime("%Y-%m-%d")
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    symbols: Counter[str] = Counter()
    sessions: dict[str, None] = {}
    notes: list[str] = []
    equities: list[Decimal] = []

    for event in events:
        counts[str(event.kind)] += 1
        sessions.setdefault(event.session, None)
        if event.account is not None:
            equities.append(event.account.equity)
        if event.kind is EventKind.REJECTION:
            reasons[event.detail.get("reason", "unknown")] += 1
        elif event.kind is EventKind.FILL:
            symbols[event.symbol] += 1
        elif event.kind is EventKind.NOTE:
            notes.append(event.detail.get("message", ""))

    return DaySummary(
        day=day,
        sessions=tuple(sessions),
        counts=dict(counts),
        rejections_by_reason=dict(reasons),
        fills_by_symbol=dict(symbols),
        halts=counts[str(EventKind.HALT)],
        opening_equity=equities[0] if equities else None,
        closing_equity=equities[-1] if equities else None,
        notes=tuple(notes),
    )


def iter_decision_days(root: Path | str) -> Iterator[Path]:
    directory = Path(root) / DECISIONS_DIRNAME
    if not directory.is_dir():
        return iter(())
    return iter(sorted(directory.glob("*.jsonl")))
