"""What an option costs to trade, established before anything can price one.

The ordering is the point. Build a pricer first and you get beautiful
theoretical edges, then find out what they cost; build the costs first and the
pricer arrives into a world that already knows what a fill is worth.

The numbers here are Deribit's published schedule. The comparisons against the
spot model are not decoration — they are the reason this module exists, and
they are asserted so that nobody later "simplifies" the option path back onto
a rate applied to the premium.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from trading_platform.core.instruments import crypto_option, crypto_perpetual, crypto_spot
from trading_platform.core.types import OptionRight, Side
from trading_platform.engine.execution import (
    ZERO_COST,
    ZERO_OPTION_COST,
    CostModel,
    OptionCostModel,
    OptionCostsUnmodelledError,
)

EXPIRY = date(2026, 8, 30)
UNDERLYING = Decimal("30000")


def call(strike: str) -> object:
    return crypto_option(
        f"BTC-30AUG26-{strike}-C", "BTC", Decimal(strike), EXPIRY, OptionRight.CALL
    )


# -- The published schedule ---------------------------------------------------


def test_commission_is_charged_on_the_underlying_not_the_premium() -> None:
    """0.03% of underlying. The single fact the spot model gets wrong."""
    model = OptionCostModel()
    # At the money, 30 days: premium 1885.20, so the 12.5% cap is 235.65 and
    # the underlying fee of 9.00 is far below it.
    fee = model.commission_for(call("30000"), Decimal("1885.20"), Decimal("1"), UNDERLYING)
    assert fee == Decimal("9.00")


def test_the_premium_cap_binds_when_the_option_is_nearly_worthless() -> None:
    """Without the cap a 6.94 contract would pay 9.00 to trade — more than it
    is worth — and the model would reject strategies for the wrong reason."""
    model = OptionCostModel()
    fee = model.commission_for(call("36000"), Decimal("6.94"), Decimal("1"), UNDERLYING)
    assert fee == Decimal("6.94") * Decimal("0.125")
    assert fee < UNDERLYING * Decimal("0.0003")


def test_the_cap_crosses_over_where_the_schedule_says() -> None:
    """The cap binds below a premium of `underlying * rate / cap` = 240."""
    model = OptionCostModel()
    crossover = UNDERLYING * Decimal("0.0003") / Decimal("0.125")
    assert crossover == Decimal("72")
    just_above = model.commission_for(call("33000"), crossover * 2, Decimal("1"), UNDERLYING)
    just_below = model.commission_for(call("33000"), crossover / 2, Decimal("1"), UNDERLYING)
    assert just_above == UNDERLYING * Decimal("0.0003"), "underlying fee applies above"
    assert just_below < UNDERLYING * Decimal("0.0003"), "cap applies below"


def test_commission_scales_with_contracts_and_multiplier() -> None:
    model = OptionCostModel()
    one = model.commission_for(call("30000"), Decimal("1885.20"), Decimal("1"), UNDERLYING)
    ten = model.commission_for(call("30000"), Decimal("1885.20"), Decimal("10"), UNDERLYING)
    assert ten == one * 10


def test_the_half_spread_is_a_fraction_of_premium() -> None:
    model = OptionCostModel()
    premium = Decimal("100")
    paid = model.effective_price_for(call("33000"), premium, Side.BUY)
    received = model.effective_price_for(call("33000"), premium, Side.SELL)
    assert paid > premium and received < premium, "the spread is paid either way"
    assert paid - premium == pytest.approx(Decimal("5"), abs=Decimal("0.2"))


def test_settlement_is_charged_once_and_is_not_a_fill() -> None:
    """A strategy that holds to expiry pays this; one that closes early does
    not. That is a real difference between them and it is easy to forget."""
    model = OptionCostModel()
    fee = model.settlement_fee(call("30000"), Decimal("2"), UNDERLYING)
    assert fee == UNDERLYING * Decimal("2") * Decimal("0.00015")


# -- The refusal --------------------------------------------------------------


def test_a_spot_model_refuses_an_option_rather_than_approximating() -> None:
    """The approximation is not close, and it flatters."""
    with pytest.raises(OptionCostsUnmodelledError, match="OptionCostModel"):
        CostModel().commission_for(call("33000"), Decimal("120.84"), Decimal("1"), UNDERLYING)
    with pytest.raises(OptionCostsUnmodelledError, match="OptionCostModel"):
        CostModel().effective_price_for(call("33000"), Decimal("120.84"), Side.BUY)


def test_zero_cost_refuses_options_too() -> None:
    """ZERO_COST is a spot model. Letting it through would under-charge
    silently, where refusing sends you to ZERO_OPTION_COST, which charges
    nothing on purpose and says so."""
    with pytest.raises(OptionCostsUnmodelledError):
        ZERO_COST.commission_for(call("33000"), Decimal("120.84"), Decimal("1"), UNDERLYING)
    assert ZERO_OPTION_COST.commission_for(
        call("33000"), Decimal("120.84"), Decimal("1"), UNDERLYING
    ) == Decimal("0")


def test_an_option_model_without_an_underlying_price_refuses() -> None:
    """The commission is a rate on the underlying, so it cannot be computed
    without one, and falling back to the premium is the 125x mistake."""
    with pytest.raises(OptionCostsUnmodelledError, match="underlying_price"):
        OptionCostModel().commission_for(call("33000"), Decimal("120.84"), Decimal("1"))


def test_the_option_model_still_prices_spot_and_perps_the_ordinary_way() -> None:
    """A mixed book must not need two cost models."""
    model = OptionCostModel(commission_rate=Decimal("0.001"))
    spot_fee = model.commission_for(crypto_spot("BTCUSDT"), Decimal("30000"), Decimal("1"))
    perp_fee = model.commission_for(crypto_perpetual("BTCUSDT"), Decimal("30000"), Decimal("1"))
    assert spot_fee == perp_fee == Decimal("30")


# -- The distortion, pinned ---------------------------------------------------


@pytest.mark.parametrize(
    ("strike", "premium", "least_understated_by"),
    [
        ("30000", "1885.20", 4),  # at the money, 30 days
        ("33000", "829.40", 9),  # 10% out, 30 days
        ("36000", "6.94", 100),  # 20% out, 7 days — where options strategies live
    ],
)
def test_the_spot_model_would_understate_by_orders_of_magnitude(
    strike: str, premium: str, least_understated_by: int
) -> None:
    """Measured at BTC 30,000 and 55% IV. Asserted as a floor rather than a
    point, so the test pins the direction and the scale without breaking every
    time a rate is revised.

    This is the whole argument for building costs before a pricer: an options
    backtest run on spot costs produces an edge that is an artefact of not
    paying for the trade, and it is worst deep out of the money.
    """
    option = call(strike)
    spot_style = CostModel().commission(option.notional(Decimal(premium), Decimal("1")))
    honest = OptionCostModel().commission_for(option, Decimal(premium), Decimal("1"), UNDERLYING)
    assert honest > spot_style * least_understated_by


def test_the_half_spread_understatement_is_pinned_too() -> None:
    """1bp of premium against 5%: optimistic by 500x, at every strike."""
    option, premium = call("33000"), Decimal("829.40")
    spot_penalty = CostModel().effective_price(premium, Side.BUY) - premium
    option_penalty = OptionCostModel().effective_price_for(option, premium, Side.BUY) - premium
    assert option_penalty > spot_penalty * 100


# -- End to end ---------------------------------------------------------------


def test_the_execution_engine_refuses_to_fill_an_option_on_spot_costs() -> None:
    """The protection that matters, and the reason this step came before a pricer.

    There is no options data in this repository yet, so nothing here can be
    run end to end against a real chain. What can be guaranteed today is that
    when a chain does arrive, filling it against a spot cost model is not
    possible — you get an exception rather than a number that is optimistic by
    two orders of magnitude.
    """
    from datetime import UTC, datetime

    from trading_platform.core.events import Bar, Order
    from trading_platform.core.types import OrderType, TimeInForce
    from trading_platform.engine.execution import SimulatedExecutionEngine

    option = call("33000")
    engine = SimulatedExecutionEngine(cost_model=CostModel())
    engine.submit(
        Order(
            order_id="o1",
            symbol=option.symbol,
            side=Side.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        option,
    )
    bar = Bar(
        symbol=option.symbol,
        timestamp=datetime(2026, 8, 2, tzinfo=UTC),
        open=Decimal("120"),
        high=Decimal("130"),
        low=Decimal("110"),
        close=Decimal("125"),
        volume=Decimal("10"),
    )
    with pytest.raises(OptionCostsUnmodelledError, match="OptionCostModel"):
        engine.process(bar, bar.timestamp)


def test_an_option_model_in_the_engine_still_cannot_fill_without_an_underlying() -> None:
    """The remaining wiring, failing loudly rather than guessing.

    `SimulatedExecutionEngine.process` has no price map, so nothing can supply
    the underlying's price yet. That belongs with the chain data. Until then
    the refusal is the whole mechanism — and it refuses rather than falling
    back to the premium, which is the mistake the model exists to prevent.
    """
    from datetime import UTC, datetime

    from trading_platform.core.events import Bar, Order
    from trading_platform.core.types import OrderType, TimeInForce
    from trading_platform.engine.execution import SimulatedExecutionEngine

    option = call("33000")
    engine = SimulatedExecutionEngine(cost_model=OptionCostModel())
    engine.submit(
        Order(
            order_id="o1",
            symbol=option.symbol,
            side=Side.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        option,
    )
    bar = Bar(
        symbol=option.symbol,
        timestamp=datetime(2026, 8, 2, tzinfo=UTC),
        open=Decimal("120"),
        high=Decimal("130"),
        low=Decimal("110"),
        close=Decimal("125"),
        volume=Decimal("10"),
    )
    with pytest.raises(OptionCostsUnmodelledError, match="underlying_price"):
        engine.process(bar, bar.timestamp)
