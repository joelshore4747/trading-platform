"""What an account was at an instant, and where that observation goes.

`engine/portfolio.py` holds the ledger and `live/accounts.py` holds the cycles.
Neither writes anything a query can reach: a paper session's state has lived in
memory and in a JSONL journal, and answering "what was my margin utilisation at
14:32 on Tuesday" has meant grepping. This module is the observation — the full
state at every mark, not a summary — and the narrow sink it goes to.

The sink is a Protocol on purpose. The supervisor must not import `store/`
(`store/` is the persistence adapter and imports the domain, never the reverse),
and more importantly a paper session must run with no database at all. A tracker
is optional everywhere it appears; `None` means the journal remains the only
record, which is exactly what it was before.

`store/accounts.py` implements the sink against Postgres.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from trading_platform.core.types import Stage, quantize_cash
from trading_platform.engine.portfolio import Portfolio
from trading_platform.risk.limits import RiskRejection

__all__ = ["AccountMark", "MarkSink", "mark_from_portfolio"]


@dataclass(frozen=True, slots=True)
class AccountMark:
    """One account's full state at one instant. Every field, no summary."""

    account: str
    cycle: int
    stage: Stage
    at: datetime
    cash: Decimal
    positions_value: Decimal
    equity: Decimal
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    fees: Decimal
    funding: Decimal
    exposure: Decimal
    margin_used: Decimal
    free_cash: Decimal
    #: exposure / equity. What the account is ACTUALLY running at, as opposed to
    #: what its instruments permit. Zero when flat.
    gross_leverage: Decimal
    peak_equity: Decimal
    drawdown_pct: Decimal
    orders: int
    fills: int
    #: symbol -> signed quantity. Flat positions omitted, so `{}` is flat.
    positions: dict[str, Decimal]

    def __post_init__(self) -> None:
        if not self.account:
            raise ValueError("a mark needs an account")
        if self.cycle < 1:
            raise ValueError(f"cycle numbers start at 1, got {self.cycle}")
        if self.at.tzinfo is None:
            raise ValueError("a mark's timestamp must be timezone-aware")
        # The same identity the database CHECKs. Raised here too so the failure
        # names the account and the instant rather than arriving as a constraint
        # violation with a row in it.
        if self.equity != quantize_cash(self.cash + self.positions_value):
            raise ValueError(
                f"{self.account} at {self.at.isoformat()}: equity {self.equity} is not "
                f"cash {self.cash} + positions {self.positions_value} "
                f"(= {quantize_cash(self.cash + self.positions_value)}). "
                "An account that does not balance is a ledger bug, not a mark."
            )
        if self.margin_used < 0:
            raise ValueError(f"{self.account}: margin_used {self.margin_used} is negative")
        if self.drawdown_pct < 0:
            raise ValueError(f"{self.account}: drawdown {self.drawdown_pct} is negative")

    @property
    def is_undercollateralised(self) -> bool:
        """The book requires more collateral than the account holds.

        Reported, never clamped: this is precisely the state a reader needs to
        see, and it is the margin-model analogue of the drawdown halt.
        """
        return self.free_cash < 0


def mark_from_portfolio(
    portfolio: Portfolio,
    *,
    account: str,
    cycle: int,
    stage: Stage,
    at: datetime,
    peak_equity: Decimal,
    orders: int,
) -> AccountMark:
    """Read an `AccountMark` off a live `Portfolio`.

    Uses the portfolio's own incremental mark (`marked_equity`,
    `marked_positions_value`) rather than recomputing, so the row records what
    the running system believed at that instant. A row that quietly disagrees
    with the process that produced it is worse than no row.

    Positions with no observed price are counted in neither exposure nor
    unrealised P&L, and that is a refusal rather than a zero: an unpriced
    position is an unknown, and `live/accounts.py` already halts on one.
    """
    unrealised = Decimal("0")
    exposure = Decimal("0")
    positions: dict[str, Decimal] = {}
    for symbol, position in portfolio.positions.items():
        if position.is_flat:
            continue
        positions[symbol] = position.quantity
        price = portfolio.last_price(symbol)
        if price is None:
            continue
        unrealised += position.unrealised_pnl(price)
        exposure += abs(position.instrument.notional(price, position.quantity))

    equity = portfolio.marked_equity
    # `positions_value` is DERIVED from equity and cash, never rounded
    # independently. `marked_equity` is `quantize(cash + pv)`, and
    # `quantize(a + b) != quantize(a) + quantize(b)` on a half-cent tie — so
    # rounding the two operands separately broke the §29 identity on about 2.6%
    # of ordinary fractional-quantity marks, and the resulting exception blamed
    # the ledger for a rounding artefact (adversarial review, 2026-09-04).
    # The identity now holds by construction; the database CHECK stays, because
    # it guards against everything else that might write to the table.
    cash = quantize_cash(portfolio.cash)
    positions_value = equity - cash
    exposure = quantize_cash(exposure)
    leverage = (exposure / equity) if equity > 0 else Decimal("0")
    drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else Decimal("0")

    return AccountMark(
        account=account,
        cycle=cycle,
        stage=stage,
        at=at,
        cash=cash,
        positions_value=positions_value,
        equity=equity,
        realised_pnl=quantize_cash(portfolio.total_realised_pnl),
        unrealised_pnl=quantize_cash(unrealised),
        fees=quantize_cash(portfolio.total_commission),
        funding=quantize_cash(portfolio.total_funding),
        exposure=exposure,
        margin_used=portfolio.margin_reserved,
        free_cash=portfolio.free_cash,
        gross_leverage=leverage,
        peak_equity=quantize_cash(peak_equity),
        drawdown_pct=max(drawdown, Decimal("0")),
        orders=orders,
        fills=len(portfolio.fills),
        positions=positions,
    )


class MarkSink(Protocol):
    """Somewhere an account's marks and refusals are durably recorded.

    Neither method may raise: a tracker is an observer, and an observer that can
    stop a paper session is a liability, not a control. Implementations count
    their own failures — the same rule `store/sink.py` follows for bars, and for
    the same reason.
    """

    def record_mark(self, mark: AccountMark) -> None: ...

    def record_rejections(
        self, rejections: Sequence[RiskRejection], *, account: str, cycle: int, stage: Stage
    ) -> None: ...
