"""Leverage, and the record that has to exist before an account may use it.

Leverage in this codebase is one field: `Instrument.initial_margin_rate`, where
`None` means fully collateralised and `Decimal("0.1")` means 10x. That is a
number in a diff, which is good, but it is not an acknowledgement. Nothing in
the repository currently makes anyone state, before the fact, what 10x does to
an account when the market moves 20% the wrong way.

This module is that gate. Before an account may hold an instrument whose
leverage exceeds 1x, a `LeverageAcknowledgement` must exist for that account and
that symbol, and it must state three things the holder had to work out first:

1. the maximum leverage they are accepting,
2. the price at which a position at that leverage is liquidated, and
3. what a 20% adverse move does to the account.

**The stated figures are checked, not trusted.** An acknowledgement whose
liquidation price or 20%-move loss disagrees with the arithmetic is refused at
construction. A form you can fill in wrongly is a form that gets filled in
wrongly, and an acknowledgement nobody had to compute is a checkbox.

## What the arithmetic assumes, and where it is optimistic

Every figure here is for a position **at full utilisation of the stated
leverage** — the worst case the acknowledgement is about. A smaller position is
further from liquidation, and that is the holder's margin of safety to keep.

`liquidation_move` is the adverse move that wipes the *initial* margin posted.
**A real venue liquidates before this.** Venues liquidate at a *maintenance*
margin, which is lower than initial, so the position is closed while some margin
remains — at an adverse move of roughly `initial - maintenance`, not `initial`.
This platform does not model maintenance margin (no venue is connected and no
schedule has been read), so the number here is an **upper bound**: the real
liquidation is closer than it says. That is stated in the acknowledgement text
because a leverage disclosure that is quietly optimistic is worse than none.

Funding is also excluded. A perpetual at 10x pays or receives funding on the
full notional, which moves the liquidation price every funding interval. That is
modelled elsewhere (`data/funding.py`, `engine/portfolio.apply_funding`) and is
not folded into these figures, which are point-in-time.

Nothing here enables leverage. It only refuses it in the absence of a record.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from trading_platform.core.instruments import Instrument

__all__ = [
    "ACKNOWLEDGEMENT_MOVE",
    "LeverageAcknowledgement",
    "LeverageGate",
    "LeverageNotAcknowledged",
    "liquidation_move",
    "liquidation_price",
    "loss_from_move",
    "needs_acknowledgement",
]

#: The adverse move every acknowledgement must state the consequence of. Fixed
#: at 20% so two acknowledgements are comparable, and because 20% is an ordinary
#: week in crypto rather than a tail.
ACKNOWLEDGEMENT_MOVE = Decimal("0.20")

#: Cash figures round here, matching `engine/portfolio.quantize_cash`.
_CENTS = Decimal("0.01")


class LeverageNotAcknowledged(RuntimeError):
    """An account tried to take leverage it has no acknowledgement for.

    A refusal, not a warning. The fix is to write the acknowledgement, which
    means working out the three numbers — which is the point.
    """


def needs_acknowledgement(instrument: Instrument) -> bool:
    """True when this instrument permits more than 1x.

    Spot is never leveraged; a derivative with no `initial_margin_rate` is fully
    collateralised and also 1x. Only an explicitly set rate below 1 gets here.
    """
    return instrument.leverage > Decimal("1")


def liquidation_move(leverage: Decimal) -> Decimal:
    """Adverse move, as a fraction, that wipes the initial margin at `leverage`.

    `1 / leverage`: at 10x you posted a tenth of the notional, so a 10% move
    against you has consumed it. **A venue liquidates before this** — see the
    module docstring — so treat it as an upper bound on your room.
    """
    if leverage <= 0:
        raise ValueError(f"leverage must be positive, got {leverage}")
    return Decimal("1") / leverage


def liquidation_price(entry_price: Decimal, leverage: Decimal, *, long: bool) -> Decimal:
    """Price at which a position opened at `entry_price` loses its initial margin.

    Long: below entry. Short: above it, and unbounded — a short at 1x has no
    liquidation price in this arithmetic because the move that wipes it is 100%,
    which for a short means the price doubling, not going to zero. The asymmetry
    is real and is why the two are computed rather than mirrored.
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive, got {entry_price}")
    move = liquidation_move(leverage)
    price = entry_price * (Decimal("1") - move) if long else entry_price * (Decimal("1") + move)
    return price.quantize(_CENTS)


def loss_from_move(equity: Decimal, leverage: Decimal, move: Decimal) -> Decimal:
    """Cash lost on `equity` fully deployed at `leverage` when the market moves `move`.

    `equity * leverage * move`. At 5x a 20% adverse move is the whole account. At
    10x it is twice the account — a number that only means something if you have
    read that it means you owe the difference, which is why it is in the text of
    the acknowledgement and not only in a column.
    """
    if equity <= 0:
        raise ValueError(f"equity must be positive, got {equity}")
    if move < 0:
        raise ValueError(f"move is an adverse magnitude and must not be negative, got {move}")
    return (equity * leverage * move).quantize(_CENTS)


@dataclass(frozen=True, slots=True)
class LeverageAcknowledgement:
    """One account's recorded acknowledgement for one leveraged symbol.

    Written once, by a person, and stored. Not a dialog box — a row that has to
    be there. The figures are recomputed at construction and a disagreement
    raises, so this cannot be filled in without doing the arithmetic.
    """

    account: str
    symbol: str
    #: The most leverage this account may take on this symbol. Checked against
    #: the instrument, so an acknowledgement for 5x does not admit a 10x one.
    max_leverage: Decimal
    #: The price the holder was looking at when they worked the numbers out.
    reference_price: Decimal
    #: Equity the loss figure is stated against.
    reference_equity: Decimal
    #: Stated by the holder, verified here: the long liquidation price.
    stated_liquidation_price: Decimal
    #: Stated by the holder, verified here: cash lost on a 20% adverse move.
    stated_loss_at_20pct: Decimal
    #: Who acknowledged it. A name, not a service account.
    acknowledged_by: str
    acknowledged_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.account or not self.symbol:
            raise ValueError("an acknowledgement needs an account and a symbol")
        if not self.acknowledged_by.strip():
            raise ValueError(
                "an acknowledgement needs a person's name. An unattributed one is a "
                "checkbox, and the point of this record is that somebody accepted it."
            )
        if self.max_leverage <= Decimal("1"):
            raise ValueError(
                f"max_leverage must exceed 1, got {self.max_leverage}. Nothing at or below "
                "1x needs acknowledging, and recording one would suggest the gate is "
                "checking something it is not."
            )
        if self.acknowledged_at.tzinfo is None:
            raise ValueError("acknowledged_at must be timezone-aware")

        expected_price = liquidation_price(self.reference_price, self.max_leverage, long=True)
        if self.stated_liquidation_price != expected_price:
            raise ValueError(
                f"{self.account}/{self.symbol}: stated liquidation price "
                f"{self.stated_liquidation_price} does not match {expected_price}, which is "
                f"what {self.max_leverage}x from {self.reference_price} liquidates at "
                f"(a {liquidation_move(self.max_leverage) * 100:.2f}% move). "
                "One of the two is wrong, and an acknowledgement of the wrong number "
                "acknowledges nothing."
            )
        expected_loss = loss_from_move(
            self.reference_equity, self.max_leverage, ACKNOWLEDGEMENT_MOVE
        )
        if self.stated_loss_at_20pct != expected_loss:
            raise ValueError(
                f"{self.account}/{self.symbol}: stated loss on a 20% adverse move "
                f"{self.stated_loss_at_20pct} does not match {expected_loss}, which is what "
                f"{self.reference_equity} fully deployed at {self.max_leverage}x loses. "
                "One of the two is wrong."
            )

    @property
    def wipes_the_account(self) -> bool:
        """True when a 20% adverse move costs at least the whole reference equity."""
        return self.stated_loss_at_20pct >= self.reference_equity

    def text(self) -> str:
        """The acknowledgement as prose, for the record and for a human to read back."""
        move = liquidation_move(self.max_leverage)
        lines = [
            f"{self.acknowledged_by} acknowledges, on {self.acknowledged_at:%Y-%m-%d}, that "
            f"account {self.account} may hold {self.symbol} at up to {self.max_leverage}x.",
            f"At {self.max_leverage}x a {move * 100:.2f}% move against the position wipes the "
            f"margin posted: from {self.reference_price} that is "
            f"{self.stated_liquidation_price} long.",
            f"A 20% adverse move on {self.reference_equity} fully deployed loses "
            f"{self.stated_loss_at_20pct}"
            + (" — the entire account, and the excess is owed." if self.wipes_the_account else "."),
            "A venue liquidates at its maintenance margin, which is lower than initial "
            "margin and is not modelled here, so liquidation arrives BEFORE the price "
            "above. Funding is excluded and moves it further.",
        ]
        if self.notes:
            lines.append(self.notes)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class LeverageGate:
    """Refuses a leveraged instrument on an account with no acknowledgement.

    Constructed from whatever acknowledgements exist — in production, the rows
    in `leverage_acknowledgements`; in a test, a list. An empty gate refuses all
    leverage, which is the correct default: the absence of a record is the
    absence of an acknowledgement, not permission.
    """

    account: str
    acknowledgements: Mapping[str, LeverageAcknowledgement] = field(default_factory=dict)

    @classmethod
    def of(cls, account: str, acknowledgements: Iterable[LeverageAcknowledgement]) -> LeverageGate:
        by_symbol: dict[str, LeverageAcknowledgement] = {}
        for ack in acknowledgements:
            if ack.account != account:
                raise ValueError(
                    f"acknowledgement for account {ack.account!r} passed to a gate for "
                    f"{account!r}. One account's acknowledgement does not carry to another."
                )
            if ack.symbol in by_symbol:
                raise ValueError(
                    f"two acknowledgements for {account}/{ack.symbol}. Which one binds is "
                    "not something this should decide; keep one."
                )
            by_symbol[ack.symbol] = ack
        return cls(account=account, acknowledgements=by_symbol)

    def refusal(self, instrument: Instrument) -> str | None:
        """Why this instrument may not be held, or None if it may.

        Returns a reason rather than raising so a caller on an order path can
        record it as a rejection with the others.
        """
        if not needs_acknowledgement(instrument):
            return None
        ack = self.acknowledgements.get(instrument.symbol)
        if ack is None:
            return (
                f"{instrument.symbol} permits {instrument.leverage}x and account "
                f"{self.account} has no leverage acknowledgement for it. Write one "
                "(risk/leverage.py) before this account holds it."
            )
        if instrument.leverage > ack.max_leverage:
            return (
                f"{instrument.symbol} permits {instrument.leverage}x; account "
                f"{self.account} acknowledged {ack.max_leverage}x. The instrument's "
                "leverage was raised after the acknowledgement was written, or the "
                "wrong instrument is configured."
            )
        return None

    def admit(self, instrument: Instrument) -> None:
        """Raise unless this account may hold this instrument."""
        reason = self.refusal(instrument)
        if reason is not None:
            raise LeverageNotAcknowledged(reason)
