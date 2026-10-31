"""The leverage gate: what it refuses, and why the stated figures are checked.

The property under test is narrow and absolute: **an account with no
acknowledgement row cannot hold a leveraged instrument.** Everything else here
exists to stop the acknowledgement becoming a checkbox — a form nobody had to
compute anything to fill in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_platform.core.events import Order
from trading_platform.core.instruments import AssetClass, Perpetual, Spot
from trading_platform.core.types import Side
from trading_platform.risk.leverage import (
    ACKNOWLEDGEMENT_MOVE,
    LeverageAcknowledgement,
    LeverageGate,
    LeverageNotAcknowledged,
    liquidation_move,
    liquidation_price,
    loss_from_move,
    needs_acknowledgement,
)
from trading_platform.risk.limits import RiskLimits, RiskManager

AT = datetime(2026, 9, 1, tzinfo=UTC)


def perp(symbol: str = "BTCUSDT", margin: str | None = "0.1") -> Perpetual:
    return Perpetual(
        symbol=symbol,
        asset_class=AssetClass.PERPETUAL,
        initial_margin_rate=Decimal(margin) if margin is not None else None,
    )


def ack(**over: object) -> LeverageAcknowledgement:
    values: dict[str, object] = {
        "account": "a1",
        "symbol": "BTCUSDT",
        "max_leverage": Decimal("10"),
        "reference_price": Decimal("100.00"),
        "reference_equity": Decimal("1000.00"),
        "stated_liquidation_price": Decimal("90.00"),
        "stated_loss_at_20pct": Decimal("2000.00"),
        "acknowledged_by": "Joel",
        "acknowledged_at": AT,
    }
    values.update(over)
    return LeverageAcknowledgement(**values)  # type: ignore[arg-type]


# -- the arithmetic ----------------------------------------------------------------------


def test_the_liquidation_move_is_the_reciprocal_of_the_leverage() -> None:
    """At 10x you posted a tenth of the notional, so a 10% adverse move has
    consumed it. Mutation: `leverage / 100` or any other shape — red at 4x."""
    assert liquidation_move(Decimal("10")) == Decimal("0.1")
    assert liquidation_move(Decimal("4")) == Decimal("0.25")
    assert liquidation_move(Decimal("1")) == Decimal("1")
    with pytest.raises(ValueError, match="must be positive"):
        liquidation_move(Decimal("0"))


def test_long_and_short_liquidation_prices_are_not_mirror_images_of_each_other() -> None:
    """Both are entry ± entry/leverage, which looks symmetric and is not: the
    long's floor approaches zero as leverage falls while the short's ceiling
    runs away. Mutation: compute the short as `entry - move` — red."""
    assert liquidation_price(Decimal("100"), Decimal("10"), long=True) == Decimal("90.00")
    assert liquidation_price(Decimal("100"), Decimal("10"), long=False) == Decimal("110.00")
    assert liquidation_price(Decimal("100"), Decimal("1"), long=True) == Decimal("0.00")
    assert liquidation_price(Decimal("100"), Decimal("1"), long=False) == Decimal("200.00")


def test_a_20_percent_move_at_5x_costs_the_whole_account_and_at_10x_costs_two() -> None:
    """The number the acknowledgement exists to make somebody read.

    Mutation: `equity * move` without the leverage factor — 5x reports 200 and
    both assertions go red."""
    assert loss_from_move(Decimal("1000"), Decimal("5"), ACKNOWLEDGEMENT_MOVE) == Decimal("1000.00")
    assert loss_from_move(Decimal("1000"), Decimal("10"), ACKNOWLEDGEMENT_MOVE) == Decimal(
        "2000.00"
    )


def test_only_an_instrument_that_actually_permits_more_than_1x_needs_acknowledging() -> None:
    """Spot is never leveraged; a derivative with no rate set is fully
    collateralised and also 1x. Mutation: `is_derivative` instead of
    `leverage > 1` — the unset-rate perpetual below starts demanding an
    acknowledgement it does not need, and this goes red."""
    assert needs_acknowledgement(perp(margin="0.1"))
    assert not needs_acknowledgement(perp(margin=None))
    assert not needs_acknowledgement(perp(margin="1"))
    assert not needs_acknowledgement(Spot(symbol="BTCUSDT", asset_class=AssetClass.SPOT))


# -- the acknowledgement cannot be filled in without doing the arithmetic ------------------


def test_an_acknowledgement_with_a_wrong_liquidation_price_is_refused() -> None:
    """The figures are recomputed, not trusted. A form you can fill in wrongly
    is a form that gets filled in wrongly.

    Mutation: drop the liquidation check from `__post_init__` — a holder can
    then acknowledge 10x while believing they are liquidated at 50."""
    with pytest.raises(ValueError, match="stated liquidation price"):
        ack(stated_liquidation_price=Decimal("50.00"))


def test_an_acknowledgement_with_a_wrong_loss_figure_is_refused() -> None:
    """Mutation: drop the loss check — the acknowledgement can then understate
    by an order of magnitude, which is exactly the mistake it exists to stop."""
    with pytest.raises(ValueError, match="stated loss"):
        ack(stated_loss_at_20pct=Decimal("200.00"))


def test_an_acknowledgement_needs_a_person() -> None:
    with pytest.raises(ValueError, match="needs a person's name"):
        ack(acknowledged_by="   ")


def test_acknowledging_1x_is_refused_because_it_suggests_a_check_that_is_not_happening() -> None:
    with pytest.raises(ValueError, match="must exceed 1"):
        ack(
            max_leverage=Decimal("1"),
            stated_liquidation_price=Decimal("0.00"),
            stated_loss_at_20pct=Decimal("200.00"),
        )


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ack(acknowledged_at=datetime(2026, 9, 1))  # naive on purpose


def test_the_text_says_that_liquidation_arrives_before_the_stated_price() -> None:
    """The arithmetic here is an upper bound: venues liquidate at maintenance
    margin, which is lower than initial and is not modelled. An acknowledgement
    that is quietly optimistic is worse than none.

    Mutation: delete that sentence from `text()` — red, deliberately, because
    the caveat is the part a reader most needs."""
    body = ack().text()
    assert "BEFORE" in body
    assert "maintenance margin" in body
    assert "the entire account" in body  # 1000 at 10x loses 2000 on a 20% move
    assert "Funding is excluded" in body


def test_the_text_does_not_claim_the_account_is_wiped_when_it_is_not() -> None:
    """Mutation: make `wipes_the_account` always True — red. A disclosure that
    cries wolf on a 2x position gets ignored on a 20x one."""
    modest = ack(
        max_leverage=Decimal("2"),
        stated_liquidation_price=Decimal("50.00"),
        stated_loss_at_20pct=Decimal("400.00"),
    )
    assert not modest.wipes_the_account
    assert "the entire account" not in modest.text()


# -- the gate ----------------------------------------------------------------------------


def test_an_account_with_no_acknowledgement_cannot_hold_a_leveraged_instrument() -> None:
    """The whole point. An empty gate refuses all leverage: the absence of a
    record is the absence of an acknowledgement, not permission.

    Mutation: return None from `refusal` when no acknowledgement is found —
    every account silently gains leverage and this goes red."""
    gate = LeverageGate(account="a1")
    with pytest.raises(LeverageNotAcknowledged, match="no leverage acknowledgement"):
        gate.admit(perp())


def test_an_unleveraged_instrument_passes_a_gate_with_nothing_in_it() -> None:
    """The gate must not become a general permission system: spot and fully
    collateralised derivatives are not what it is for."""
    gate = LeverageGate(account="a1")
    gate.admit(Spot(symbol="BTCUSDT", asset_class=AssetClass.SPOT))
    gate.admit(perp(margin=None))


def test_an_acknowledgement_admits_exactly_its_own_symbol() -> None:
    """Mutation: key the gate on the account alone — acknowledging BTCUSDT would
    admit ETHUSDT, and this goes red."""
    gate = LeverageGate.of("a1", [ack()])
    gate.admit(perp("BTCUSDT"))
    with pytest.raises(LeverageNotAcknowledged, match="ETHUSDT"):
        gate.admit(perp("ETHUSDT"))


def test_an_instrument_leveraged_beyond_what_was_acknowledged_is_refused() -> None:
    """Acknowledging 5x does not admit 10x. This is the case that bites in
    practice: the instrument's margin rate is edited later and the
    acknowledgement, written in good faith, no longer covers it.

    Mutation: check only for the acknowledgement's existence — red."""
    five_x = ack(
        max_leverage=Decimal("5"),
        stated_liquidation_price=Decimal("80.00"),
        stated_loss_at_20pct=Decimal("1000.00"),
    )
    gate = LeverageGate.of("a1", [five_x])
    gate.admit(perp(margin="0.2"))  # exactly 5x
    with pytest.raises(LeverageNotAcknowledged, match="acknowledged 5x"):
        gate.admit(perp(margin="0.1"))  # 10x


def test_one_accounts_acknowledgement_does_not_carry_to_another() -> None:
    with pytest.raises(ValueError, match="does not carry"):
        LeverageGate.of("a2", [ack()])


def test_two_acknowledgements_for_one_symbol_are_refused_rather_than_ranked() -> None:
    """Which one binds is not something a constructor should decide quietly."""
    with pytest.raises(ValueError, match="two acknowledgements"):
        LeverageGate.of("a1", [ack(), ack(notes="second")])


def test_refusal_returns_a_reason_so_it_can_be_recorded_beside_other_rejections() -> None:
    """An order path needs the reason as a value, not an exception: a refusal
    that only ever raises cannot be counted in `rejection_summary`."""
    gate = LeverageGate(account="a1")
    reason = gate.refusal(perp())
    assert reason is not None and "BTCUSDT" in reason
    assert gate.refusal(Spot(symbol="X", asset_class=AssetClass.SPOT)) is None


# -- the gate on the order path -----------------------------------------------------------


def test_the_risk_manager_refuses_an_unacknowledged_leveraged_order() -> None:
    """The gate has to bite where orders are checked, not only where accounts
    are constructed, or a strategy reaches the instrument another way.

    Mutation: delete the `leverage_gate` block from `RiskManager.check` — the
    order is approved and both assertions go red."""
    manager = RiskManager(limits=RiskLimits(), leverage_gate=LeverageGate(account="a1"))
    order = Order(order_id="o1", symbol="BTCUSDT", side=Side.BUY, quantity=Decimal("1"))
    approved = manager.check(
        order=order,
        instrument=perp(),
        reference_price=Decimal("100"),
        current_quantity=Decimal("0"),
        equity=Decimal("10000"),
        gross_exposure=Decimal("0"),
        timestamp=AT,
    )
    assert approved is False
    assert manager.rejection_summary == {"leverage_unacknowledged": 1}


def test_an_unacknowledged_leveraged_position_can_still_be_closed() -> None:
    """A gate that trapped a position would be a risk control that creates risk.
    Reducing orders are exempt, like `halted` and `insolvent` above it.

    Mutation: drop `and not reduces_exposure` — the account cannot exit and
    this goes red."""
    manager = RiskManager(limits=RiskLimits(), leverage_gate=LeverageGate(account="a1"))
    closing = Order(order_id="o1", symbol="BTCUSDT", side=Side.SELL, quantity=Decimal("1"))
    assert manager.check(
        order=closing,
        instrument=perp(),
        reference_price=Decimal("100"),
        current_quantity=Decimal("2"),  # long 2, selling 1: reducing
        equity=Decimal("10000"),
        gross_exposure=Decimal("200"),
        timestamp=AT,
    )
    assert manager.rejections == []


def test_an_acknowledged_leveraged_order_proceeds() -> None:
    """The gate must not be a blanket refusal: with the record in place the
    order goes through the ordinary limits."""
    manager = RiskManager(limits=RiskLimits(), leverage_gate=LeverageGate.of("a1", [ack()]))
    order = Order(order_id="o1", symbol="BTCUSDT", side=Side.BUY, quantity=Decimal("1"))
    assert manager.check(
        order=order,
        instrument=perp(),
        reference_price=Decimal("100"),
        current_quantity=Decimal("0"),
        equity=Decimal("10000"),
        gross_exposure=Decimal("0"),
        timestamp=AT,
    )


def test_without_a_gate_a_research_backtest_is_not_blocked() -> None:
    """`leverage_gate=None` is no account context, which is what a RESEARCH
    backtest has. Gating it would stop nothing dangerous — a backtest holds no
    capital — and would teach people to switch the gate off. PAPER and above
    always build one. ADR 0011.

    Mutation: refuse leverage when the gate is None — every existing leveraged
    backtest in the suite goes red, which is how you would find out."""
    manager = RiskManager(limits=RiskLimits())
    assert manager.leverage_gate is None
    order = Order(order_id="o1", symbol="BTCUSDT", side=Side.BUY, quantity=Decimal("1"))
    assert manager.check(
        order=order,
        instrument=perp(),
        reference_price=Decimal("100"),
        current_quantity=Decimal("0"),
        equity=Decimal("10000"),
        gross_exposure=Decimal("0"),
        timestamp=AT,
    )
