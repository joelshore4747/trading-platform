"""Position accounting. The flip-through-zero case is the one that breaks."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_platform.core.events import Fill
from trading_platform.core.instruments import crypto_spot, equity_option
from trading_platform.core.types import OptionRight, Side
from trading_platform.engine.portfolio import Portfolio, Position

TS = datetime(2026, 1, 1, tzinfo=UTC)
BTC = crypto_spot("BTCUSDT")


def make_fill(side: Side, qty: str, price: str, commission: str = "0") -> Fill:
    return Fill(
        order_id="o1",
        symbol="BTCUSDT",
        timestamp=TS,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        commission=Decimal(commission),
    )


def test_open_long_sets_cost_basis() -> None:
    pos = Position(instrument=BTC)
    pos.apply(make_fill(Side.BUY, "2", "100"))
    assert pos.quantity == Decimal("2")
    assert pos.average_price == Decimal("100")
    assert pos.realised_pnl == Decimal("0")


def test_scaling_in_blends_average_price() -> None:
    pos = Position(instrument=BTC)
    pos.apply(make_fill(Side.BUY, "1", "100"))
    pos.apply(make_fill(Side.BUY, "1", "200"))
    assert pos.quantity == Decimal("2")
    assert pos.average_price == Decimal("150")


def test_partial_close_realises_pnl_and_keeps_basis() -> None:
    pos = Position(instrument=BTC)
    pos.apply(make_fill(Side.BUY, "4", "100"))
    realised = pos.apply(make_fill(Side.SELL, "1", "120"))
    assert realised == Decimal("20")  # 1 unit * 20 gain
    assert pos.quantity == Decimal("3")
    assert pos.average_price == Decimal("100")  # basis unchanged on a reduction


def test_full_close_flattens() -> None:
    pos = Position(instrument=BTC)
    pos.apply(make_fill(Side.BUY, "2", "100"))
    realised = pos.apply(make_fill(Side.SELL, "2", "110"))
    assert realised == Decimal("20")
    assert pos.is_flat
    assert pos.average_price == Decimal("0")


def test_flip_through_zero_realises_only_closed_portion() -> None:
    """Long 2 @ 100, sell 5 @ 110 -> realise on 2, end short 3 based at 110."""
    pos = Position(instrument=BTC)
    pos.apply(make_fill(Side.BUY, "2", "100"))
    realised = pos.apply(make_fill(Side.SELL, "5", "110"))
    assert realised == Decimal("20"), "must realise only the 2 units that closed"
    assert pos.quantity == Decimal("-3")
    assert pos.average_price == Decimal("110"), "remainder re-bases at the fill price"


def test_short_profits_when_price_falls() -> None:
    pos = Position(instrument=BTC)
    pos.apply(make_fill(Side.SELL, "1", "100"))
    assert pos.is_short
    realised = pos.apply(make_fill(Side.BUY, "1", "90"))
    assert realised == Decimal("10")


def test_option_multiplier_scales_pnl() -> None:
    """An OCC option controls 100 shares — P&L must scale by the multiplier."""
    opt = equity_option(
        symbol="SPY260220C00500000",
        underlying="SPY",
        strike=Decimal("500"),
        expiry=datetime(2026, 2, 20).date(),
        right=OptionRight.CALL,
    )
    pos = Position(instrument=opt)
    buy = Fill(
        order_id="o1",
        symbol=opt.symbol,
        timestamp=TS,
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("5.00"),
    )
    sell = Fill(
        order_id="o2",
        symbol=opt.symbol,
        timestamp=TS,
        side=Side.SELL,
        quantity=Decimal("1"),
        price=Decimal("7.00"),
    )
    pos.apply(buy)
    realised = pos.apply(sell)
    assert realised == Decimal("200"), "2.00 premium gain * 100 multiplier"


def test_cash_impact_directions() -> None:
    buy = make_fill(Side.BUY, "1", "100", commission="1")
    sell = make_fill(Side.SELL, "1", "100", commission="1")
    assert buy.cash_impact() == Decimal("-101")  # pay 100, pay 1 fee
    assert sell.cash_impact() == Decimal("99")  # receive 100, pay 1 fee


def test_portfolio_cash_tracks_fills() -> None:
    pf = Portfolio(starting_cash=Decimal("10000"))
    pf.apply_fill(BTC, make_fill(Side.BUY, "10", "100", commission="5"))
    assert pf.cash == Decimal("8995.00")
    assert pf.quantity_of("BTCUSDT") == Decimal("10")


def test_spot_equity_is_cash_plus_market_value() -> None:
    pf = Portfolio(starting_cash=Decimal("10000"))
    pf.apply_fill(BTC, make_fill(Side.BUY, "10", "100"))
    # Spent 1000, hold 10 units now worth 110 each.
    assert pf.equity({"BTCUSDT": Decimal("110")}) == Decimal("10100.00")


def perp_fill(side: Side, quantity: str, price: str, commission: str = "0") -> Fill:
    return Fill(
        order_id="o1",
        symbol="BTC-PERP",
        timestamp=TS,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        commission=Decimal(commission),
    )


def test_entering_a_derivative_does_not_move_cash_by_the_notional() -> None:
    """**This expectation was reversed, deliberately.**

    It used to assert equity of 9,910 here — a 90 loss on a position that had
    gained 10 — because `apply_fill` deducted the full notional from cash
    while `positions_value` credited only unrealised P&L back. The bug was
    written down as the specification, which is why nothing caught it: no test
    sized a position large enough for the missing notional to show.

    On the real archive it was not subtle. A ranker at 90% allocation took
    equity from 100,000 to 8,302 in 400 instants with no market move, tripped
    the drawdown halt, and — the halt being a one-way latch — produced 11 fills
    and 424 rejections over a 20,856-instant run.

    A derivative is entered, not bought. No notional changes hands, which is
    what `positions_value`'s docstring had said all along.
    """
    from trading_platform.core.instruments import crypto_perpetual

    perp = crypto_perpetual("BTC-PERP")
    pf = Portfolio(starting_cash=Decimal("10000"))
    pf.apply_fill(perp, perp_fill(Side.BUY, "1", "100"))

    assert pf.cash == Decimal("10000.00"), "entering a perp must not spend cash"
    assert pf.equity({"BTC-PERP": Decimal("100")}) == Decimal("10000.00")
    assert pf.equity({"BTC-PERP": Decimal("110")}) == Decimal("10010.00")


def test_a_derivative_round_trip_reconciles_long_and_short() -> None:
    """Realised P&L must reach cash, or a winning perp's gain never arrives."""
    from trading_platform.core.instruments import crypto_perpetual

    perp = crypto_perpetual("BTC-PERP")

    long_pf = Portfolio(starting_cash=Decimal("10000"))
    long_pf.apply_fill(perp, perp_fill(Side.BUY, "1", "100"))
    long_pf.apply_fill(perp, perp_fill(Side.SELL, "1", "110"))
    assert long_pf.cash == Decimal("10010.00")
    assert long_pf.equity({"BTC-PERP": Decimal("999")}) == Decimal("10010.00")

    # A short that wins as the price falls, by the same amount.
    short_pf = Portfolio(starting_cash=Decimal("10000"))
    short_pf.apply_fill(perp, perp_fill(Side.SELL, "1", "100"))
    assert short_pf.equity({"BTC-PERP": Decimal("90")}) == Decimal("10010.00")
    short_pf.apply_fill(perp, perp_fill(Side.BUY, "1", "90"))
    assert short_pf.cash == Decimal("10010.00")


def test_commission_is_the_only_cash_a_derivative_entry_costs() -> None:
    from trading_platform.core.instruments import crypto_perpetual

    perp = crypto_perpetual("BTC-PERP")
    pf = Portfolio(starting_cash=Decimal("10000"))
    pf.apply_fill(perp, perp_fill(Side.BUY, "2", "100", commission="3"))
    assert pf.cash == Decimal("9997.00")
    assert pf.equity({"BTC-PERP": Decimal("100")}) == Decimal("9997.00")


def test_a_derivative_flip_through_zero_realises_only_the_closed_part() -> None:
    """The case that usually gets written wrong, now that cash depends on it."""
    from trading_platform.core.instruments import crypto_perpetual

    perp = crypto_perpetual("BTC-PERP")
    pf = Portfolio(starting_cash=Decimal("10000"))
    pf.apply_fill(perp, perp_fill(Side.BUY, "1", "100"))
    # Sell 3 at 110: closes the 1 long for +10, opens a 2 short at 110.
    pf.apply_fill(perp, perp_fill(Side.SELL, "3", "110"))
    assert pf.cash == Decimal("10010.00"), "only the closed unit is realised"
    assert pf.positions["BTC-PERP"].quantity == Decimal("-2")
    # The new short is flat at its own entry price.
    assert pf.equity({"BTC-PERP": Decimal("110")}) == Decimal("10010.00")
    assert pf.equity({"BTC-PERP": Decimal("100")}) == Decimal("10030.00")


def test_spot_and_a_derivative_together_are_not_double_counted() -> None:
    """The case `positions_value` names: conflating the two double-counts."""
    from trading_platform.core.instruments import crypto_perpetual, crypto_spot

    spot, perp = crypto_spot("AAA"), crypto_perpetual("BTC-PERP")
    pf = Portfolio(starting_cash=Decimal("10000"))
    pf.apply_fill(
        spot,
        Fill(
            order_id="s1",
            symbol="AAA",
            timestamp=TS,
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100"),
        ),
    )
    pf.apply_fill(perp, perp_fill(Side.BUY, "1", "100"))
    # Spot: 1,000 of cash became 1,000 of asset. Perp: nothing moved.
    assert pf.cash == Decimal("9000.00")
    prices = {"AAA": Decimal("100"), "BTC-PERP": Decimal("100")}
    assert pf.equity(prices) == Decimal("10000.00")
    # Each leg moves equity by its own P&L and no more.
    assert pf.equity({"AAA": Decimal("110"), "BTC-PERP": Decimal("100")}) == Decimal("10100.00")
    assert pf.equity({"AAA": Decimal("100"), "BTC-PERP": Decimal("110")}) == Decimal("10010.00")


def test_the_incremental_mark_agrees_after_derivative_fills() -> None:
    """`marked_positions_value` is a cache; `positions_value` is the authority."""
    from trading_platform.core.instruments import crypto_perpetual

    perp = crypto_perpetual("BTC-PERP")
    pf = Portfolio(starting_cash=Decimal("10000"))
    for side, qty, price in (
        (Side.BUY, "2", "100"),
        (Side.SELL, "1", "105"),
        (Side.SELL, "3", "110"),
        (Side.BUY, "1", "95"),
    ):
        pf.apply_fill(perp, perp_fill(side, qty, price))
        pf.observe_price("BTC-PERP", Decimal(price))
        assert pf.marked_positions_value == pf.positions_value({"BTC-PERP": Decimal(price)})


def test_portfolio_rejects_non_positive_starting_cash() -> None:
    with pytest.raises(ValueError, match="starting_cash must be positive"):
        Portfolio(starting_cash=Decimal("0"))


# -- margin, brought over from branch bih2rm at the 2026-09-07 merge -----------------------
#
# These six were written against that branch's derivative accounting and pass
# unchanged against this one, which is the point of keeping them: they pin
# margin as a RESERVATION against capacity rather than a charge against equity,
# and that property is orthogonal to which branch's `apply_fill` won.


def test_derivative_equity_counts_unrealised_not_notional() -> None:
    """A perp position must not add its full notional to equity — nor remove it.

    This test previously asserted ``9910.00``, which is the notional debited
    from cash *and* only unrealised credited back: the same money counted twice
    in opposite directions. Its comment, "Cash paid 100", stated the defect as
    intent, and it would have rejected the fix rather than catching the bug.
    Found by the gauntlet (roadmap 35a), which predicted ``10010.00`` before
    the change was made.

    It now pins the property in its own name from both sides.
    """
    from trading_platform.core.instruments import crypto_perpetual

    perp = crypto_perpetual("BTC-PERP")
    pf = Portfolio(starting_cash=Decimal("10000"))
    fill = Fill(
        order_id="o1",
        symbol="BTC-PERP",
        timestamp=TS,
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
    )
    pf.apply_fill(perp, fill)

    # Opening at fair value moves nothing. This is the assertion whose absence
    # let a 95% equity collapse pass every suite for the life of the project.
    assert pf.equity({"BTC-PERP": Decimal("100")}) == Decimal("10000.00")

    # Unrealised only: +10 on one contract, never the 100 of notional.
    assert pf.equity({"BTC-PERP": Decimal("110")}) == Decimal("10010.00")
    assert pf.equity({"BTC-PERP": Decimal("90")}) == Decimal("9990.00")

    # And the notional did not leave cash.
    assert pf.cash == Decimal("10000.00")


def test_a_derivative_reserves_margin_without_charging_equity() -> None:
    """Margin bounds capacity; it is not a cost.

    The distinction is the whole margin model: two accounts holding the same
    position at the same price have the same equity and different room to add
    to it.
    """
    from trading_platform.core.instruments import crypto_perpetual

    tenx = crypto_perpetual("BTC-PERP", initial_margin_rate=Decimal("0.1"))
    pf = Portfolio(starting_cash=Decimal("10000"))
    pf.observe_price("BTC-PERP", Decimal("100"), TS)
    pf.apply_fill(
        tenx,
        Fill(
            order_id="o1",
            symbol="BTC-PERP",
            timestamp=TS,
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100"),
        ),
    )
    pf.observe_price("BTC-PERP", Decimal("100"), TS)

    assert pf.marked_equity == Decimal("10000.00"), "margin is not a charge against equity"
    assert pf.margin_reserved == Decimal("100.00"), "1,000 of notional at 10x"
    assert pf.free_cash == Decimal("9900.00")


def test_a_short_derivative_reserves_margin_on_absolute_exposure() -> None:
    """A short is exposure, not negative exposure that needs no collateral."""
    from trading_platform.core.instruments import crypto_perpetual

    perp = crypto_perpetual("BTC-PERP", initial_margin_rate=Decimal("0.1"))
    pf = Portfolio(starting_cash=Decimal("10000"))
    pf.observe_price("BTC-PERP", Decimal("100"), TS)
    pf.apply_fill(
        perp,
        Fill(
            order_id="o1",
            symbol="BTC-PERP",
            timestamp=TS,
            side=Side.SELL,
            quantity=Decimal("10"),
            price=Decimal("100"),
        ),
    )
    pf.observe_price("BTC-PERP", Decimal("100"), TS)

    assert pf.quantity_of("BTC-PERP") == Decimal("-10")
    assert pf.margin_reserved == Decimal("100.00"), "same requirement as the equivalent long"


def test_a_fully_collateralised_derivative_has_the_capacity_of_spot() -> None:
    """The model's own cross-check.

    A derivative at 1x should leave exactly the free cash that buying the same
    notional outright would. If these two ever disagree, the margin model and
    the cash model have drifted apart.
    """
    from trading_platform.core.instruments import crypto_perpetual, crypto_spot

    price, qty = Decimal("65000"), Decimal("1")
    results = {}
    for label, inst in (("perp", crypto_perpetual("X")), ("spot", crypto_spot("X"))):
        pf = Portfolio(starting_cash=Decimal("100000"))
        pf.observe_price("X", price, TS)
        pf.apply_fill(
            inst,
            Fill(
                order_id="o",
                symbol="X",
                timestamp=TS,
                side=Side.BUY,
                quantity=qty,
                price=price,
            ),
        )
        pf.observe_price("X", price, TS)
        results[label] = (pf.marked_equity, pf.free_cash)

    assert results["perp"] == results["spot"], f"models disagree: {results}"
    assert results["perp"][0] == Decimal("100000.00"), "opening at fair value moved equity"


def test_margin_is_remeasured_at_the_current_price_not_at_entry() -> None:
    """A position that moved against you needs more collateral, not less.

    Fixing the reservation at entry would under-reserve exactly when it
    matters. Checked in both directions so a function returning a constant
    cannot pass.
    """
    from trading_platform.core.instruments import crypto_perpetual

    perp = crypto_perpetual("BTC-PERP", initial_margin_rate=Decimal("0.5"))
    pf = Portfolio(starting_cash=Decimal("10000"))
    pf.observe_price("BTC-PERP", Decimal("100"), TS)
    pf.apply_fill(
        perp,
        Fill(
            order_id="o1",
            symbol="BTC-PERP",
            timestamp=TS,
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100"),
        ),
    )
    pf.observe_price("BTC-PERP", Decimal("100"), TS)
    assert pf.margin_reserved == Decimal("500.00")

    pf.observe_price("BTC-PERP", Decimal("200"), TS)
    assert pf.margin_reserved == Decimal("1000.00"), "a doubled price doubles the requirement"

    pf.observe_price("BTC-PERP", Decimal("50"), TS)
    assert pf.margin_reserved == Decimal("250.00"), "and a halved price halves it"


def test_free_cash_goes_negative_rather_than_hiding_an_over_margined_book() -> None:
    """The state a reader most needs to see is the one a clamp would erase."""
    from trading_platform.core.instruments import crypto_perpetual

    perp = crypto_perpetual("BTC-PERP", initial_margin_rate=Decimal("0.5"))
    pf = Portfolio(starting_cash=Decimal("1000"))
    pf.observe_price("BTC-PERP", Decimal("100"), TS)
    pf.apply_fill(
        perp,
        Fill(
            order_id="o1",
            symbol="BTC-PERP",
            timestamp=TS,
            side=Side.BUY,
            quantity=Decimal("30"),
            price=Decimal("100"),
        ),
    )
    pf.observe_price("BTC-PERP", Decimal("100"), TS)

    assert pf.margin_reserved == Decimal("1500.00")
    assert pf.free_cash == Decimal("-500.00"), "reported, not clamped to zero"
