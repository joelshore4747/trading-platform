"""Account state, refusals, and leverage acknowledgements, in the store.

Three things are made durable here, and each is durable for a different reason.

**Every mark.** `engine/portfolio.py` holds the ledger and `live/accounts.py`
holds the cycles, and until now nothing wrote either anywhere a query could
reach. `write_mark` stores the full state — not a summary — at every mark, with
the §29 accounting identity (`equity = cash + positions_value`) enforced as a
database CHECK. A ledger that cannot balance cannot be written down at all,
which is a stronger guarantee than a test: the test runs when someone runs it.

**Every refusal.** `RiskManager` accumulates `RiskRejection`s in a list that
dies with the process. `write_rejections` makes "the strategy wanted to breach
a limit 400 times today" a query. A rejection is a **result** — the limit
working — not an error to be cleaned up.

**Every leverage acknowledgement.** `risk/leverage.py` refuses a leveraged
instrument on an account with no acknowledgement; this is where the record
lives. `leverage_gate` builds the gate a `RiskManager` is constructed with, so
the refusal is driven by rows rather than by a constructor argument somebody
remembered to pass.

## Where this sits

`store/` is the persistence adapter and is the TOP layer: it imports the domain
packages (`core`, `engine`, `risk`, `live`) and none of them import it. That
direction is deliberate — a `Portfolio` that knew about Postgres would be a
`Portfolio` you could not run a backtest with.

## What is deliberately not here

No update, and no delete. A mark is an observation at an instant and observations
do not change; a re-mark of the same instant is refused by the `marks_once`
constraint rather than overwriting. An acknowledgement is *revoked*, never
removed, because "who could take 10x on this account in August" has to stay
answerable in December.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading_platform.core.types import Stage
from trading_platform.live.tracker import AccountMark
from trading_platform.risk.leverage import LeverageAcknowledgement, LeverageGate
from trading_platform.risk.limits import RiskRejection

__all__ = [
    "StoreTracker",
    "acknowledgement_history",
    "acknowledgements",
    "all_acknowledgements",
    "latest_mark",
    "leverage_gate",
    "marks_disagree",
    "rejection_pressure",
    "revoke_acknowledgement",
    "write_acknowledgement",
    "write_mark",
    "write_rejections",
]


def write_mark(connection: Any, mark: AccountMark) -> bool:
    """Store one mark. Returns False when this instant was already recorded.

    A restart replaying the last minute re-marks instants it already wrote, and
    that is normal. A *different* state for the same instant would be a bug, and
    is not silently kept: the row is skipped and the caller is told nothing
    changed, which is the honest answer — see `marks_disagree` below for finding
    them.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO account_marks (account, cycle, stage, at, cash, positions_value, "
            "equity, realised_pnl, unrealised_pnl, fees, funding, exposure, margin_used, "
            "free_cash, gross_leverage, peak_equity, drawdown_pct, orders, fills, positions) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s) "
            "ON CONFLICT ON CONSTRAINT marks_once DO NOTHING",
            (
                mark.account,
                mark.cycle,
                str(mark.stage),
                mark.at,
                mark.cash,
                mark.positions_value,
                mark.equity,
                mark.realised_pnl,
                mark.unrealised_pnl,
                mark.fees,
                mark.funding,
                mark.exposure,
                mark.margin_used,
                mark.free_cash,
                mark.gross_leverage,
                mark.peak_equity,
                mark.drawdown_pct,
                mark.orders,
                mark.fills,
                json.dumps({s: str(q) for s, q in mark.positions.items()}),
            ),
        )
        written = cursor.rowcount
    connection.commit()
    return bool(written)


def write_rejections(
    connection: Any,
    rejections: Sequence[RiskRejection],
    *,
    account: str,
    cycle: int,
    stage: Stage,
) -> int:
    """Store refusals. Returns how many rows were written.

    Append-only and deliberately not deduplicated: the same limit refusing the
    same symbol twice at the same instant happened twice, and collapsing it
    would understate exactly the pressure this table measures.
    """
    if not rejections:
        return 0
    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO risk_rejections (account, cycle, stage, ts, symbol, reason, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [
                (account, cycle, str(stage), r.timestamp, r.symbol, r.reason, r.detail)
                for r in rejections
            ],
        )
    connection.commit()
    return len(rejections)


def write_acknowledgement(connection: Any, ack: LeverageAcknowledgement) -> int:
    """Store an acknowledgement. Returns its id.

    Refuses a second LIVE acknowledgement for the same account and symbol: the
    existing one must be revoked first, with a reason, so the history says what
    changed and why rather than showing two rows and leaving a reader to guess
    which bound.

    The figures were already verified when the `LeverageAcknowledgement` was
    constructed — that is what makes it one — so this stores rather than checks.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, max_leverage FROM leverage_acknowledgements "
            "WHERE account = %s AND symbol = %s AND revoked_at IS NULL",
            (ack.account, ack.symbol),
        )
        existing = cursor.fetchone()
        if existing is not None:
            raise ValueError(
                f"{ack.account}/{ack.symbol} already has a live acknowledgement (id "
                f"{existing[0]}, {existing[1]}x). Revoke it with a reason before writing "
                "another; two live acknowledgements would leave which one binds to chance."
            )
        cursor.execute(
            "INSERT INTO leverage_acknowledgements (account, symbol, max_leverage, "
            "reference_price, reference_equity, stated_liquidation_price, "
            "stated_loss_at_20pct, acknowledged_by, acknowledged_at, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                ack.account,
                ack.symbol,
                ack.max_leverage,
                ack.reference_price,
                ack.reference_equity,
                ack.stated_liquidation_price,
                ack.stated_loss_at_20pct,
                ack.acknowledged_by,
                ack.acknowledged_at,
                ack.notes,
            ),
        )
        new_id = int(cursor.fetchone()[0])
    connection.commit()
    return new_id


def revoke_acknowledgement(
    connection: Any, account: str, symbol: str, *, reason: str, at: datetime | None = None
) -> None:
    """Withdraw permission. The row stays; only its revocation is added."""
    if not reason.strip():
        raise ValueError(
            "revoking an acknowledgement needs a reason; a revocation with none reads "
            "as somebody tidying up"
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE leverage_acknowledgements SET revoked_at = %s, revoked_reason = %s "
            "WHERE account = %s AND symbol = %s AND revoked_at IS NULL",
            (at or datetime.now(UTC), reason, account, symbol),
        )
        changed = cursor.rowcount
    connection.commit()
    if not changed:
        raise ValueError(
            f"no live acknowledgement for {account}/{symbol} to revoke. "
            "Refusing rather than reporting success for something that did not happen."
        )


def acknowledgements(connection: Any, account: str) -> list[LeverageAcknowledgement]:
    """Every LIVE acknowledgement for one account. Revoked ones are not returned.

    Reconstructed through `LeverageAcknowledgement.__post_init__`, so a row whose
    stored figures no longer match the arithmetic raises on the way out. Storage
    is not trusted more than input: a hand-edited row is exactly the case worth
    catching.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT account, symbol, max_leverage, reference_price, reference_equity, "
            "stated_liquidation_price, stated_loss_at_20pct, acknowledged_by, "
            "acknowledged_at, notes FROM leverage_acknowledgements "
            "WHERE account = %s AND revoked_at IS NULL ORDER BY symbol",
            (account,),
        )
        rows = cursor.fetchall()
    return [
        LeverageAcknowledgement(
            account=row[0],
            symbol=row[1],
            max_leverage=Decimal(row[2]),
            reference_price=Decimal(row[3]),
            reference_equity=Decimal(row[4]),
            stated_liquidation_price=Decimal(row[5]),
            stated_loss_at_20pct=Decimal(row[6]),
            acknowledged_by=row[7],
            acknowledged_at=row[8].astimezone(UTC),
            notes=row[9],
        )
        for row in rows
    ]


def leverage_gate(connection: Any, account: str) -> LeverageGate:
    """The gate for one account, built from its rows.

    An account with no rows gets a gate that refuses all leverage, which is
    correct: the absence of a record is the absence of an acknowledgement.
    """
    return LeverageGate.of(account, acknowledgements(connection, account))


def latest_mark(connection: Any, account: str) -> dict[str, Any] | None:
    """The most recent mark for one account, or None if it has never marked."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT account, cycle, stage, at, equity, cash, exposure, margin_used, "
            "free_cash, gross_leverage, peak_equity, drawdown_pct, positions "
            "FROM account_latest WHERE account = %s",
            (account,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cursor.description]
    return dict(zip(columns, row, strict=True))


def marks_disagree(connection: Any, account: str) -> list[dict[str, Any]]:
    """Instants recorded more than once for this account with different equity.

    Cannot happen through `write_mark` — `marks_once` refuses the second row —
    so a non-empty result means somebody wrote directly. Provided because
    "impossible" is a hypothesis, and the query costs nothing.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT cycle, at, count(DISTINCT equity) AS equities, count(*) AS rows "
            "FROM account_marks WHERE account = %s "
            "GROUP BY cycle, at HAVING count(DISTINCT equity) > 1 ORDER BY at",
            (account,),
        )
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def rejection_pressure(connection: Any, account: str) -> list[dict[str, Any]]:
    """How hard this account pushed against each limit, per day.

    A reason appearing every day is a strategy sized for a cap it does not have.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT day, reason, rejections, symbols, first_at, last_at FROM risk_pressure "
            "WHERE account = %s ORDER BY day DESC, rejections DESC",
            (account,),
        )
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def acknowledgement_history(connection: Any, account: str) -> list[dict[str, Any]]:
    """Every acknowledgement this account has ever had, revoked ones included."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT symbol, max_leverage, acknowledged_by, acknowledged_at, revoked_at, "
            "revoked_reason, notes FROM leverage_acknowledgements WHERE account = %s "
            "ORDER BY acknowledged_at",
            (account,),
        )
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def all_acknowledgements(connection: Any) -> Iterable[dict[str, Any]]:
    """Every live acknowledgement across every account, for the operations page."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT account, symbol, max_leverage, acknowledged_by, acknowledged_at "
            "FROM leverage_acknowledgements WHERE revoked_at IS NULL "
            "ORDER BY account, symbol"
        )
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


@dataclass(slots=True)
class StoreTracker:
    """A `MarkSink` writing to Postgres, which never stops a session.

    An observer that can take down the thing it observes is a liability, not a
    control — the same rule `store/sink.py` follows for bars. So every failure
    here is swallowed and counted, and `report()` says so. The journal remains
    the durable record; this is the queryable copy of it.

    `duplicate_marks` counts instants already recorded, which a restart replaying
    produces normally. A rising `failures` is a database problem; a rising
    `duplicates` after a steady state is a re-mark loop.
    """

    connection: Any
    account: str
    cycle: int
    stage: Stage
    marks: int = 0
    duplicate_marks: int = 0
    rejections: int = 0
    failures: int = 0
    last_error: str = ""

    @property
    def healthy(self) -> bool:
        return self.failures == 0

    def record_mark(self, mark: AccountMark) -> None:
        try:
            if write_mark(self.connection, mark):
                self.marks += 1
            else:
                self.duplicate_marks += 1
        except Exception as exc:
            self._failed(exc)

    def record_rejections(
        self,
        rejections: Sequence[RiskRejection],
        *,
        account: str,
        cycle: int,
        stage: Stage,
    ) -> None:
        try:
            self.rejections += write_rejections(
                self.connection, rejections, account=account, cycle=cycle, stage=stage
            )
        except Exception as exc:
            self._failed(exc)

    def _failed(self, exc: Exception) -> None:
        self.failures += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        with suppress(Exception):  # the connection may already be gone
            self.connection.rollback()

    def report(self) -> str:
        state = "ok" if self.healthy else "DEGRADED"
        line = (
            f"{state}: marks {self.marks:,} (+{self.duplicate_marks:,} already recorded), "
            f"rejections {self.rejections:,}, failures {self.failures}"
        )
        return line if not self.last_error else f"{line}\n  last error: {self.last_error}"
