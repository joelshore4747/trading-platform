"""Risk limits must actually stop orders, not merely log about them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_platform.core.events import Bar, Order
from trading_platform.core.instruments import crypto_spot
from trading_platform.core.types import Side
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.engine.backtester import Backtester
from trading_platform.engine.execution import ZERO_COST
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.base import Strategy, StrategyContext

TS = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOL = "BTCUSDT"
BTC = crypto_spot(SYMBOL)


def order(qty: str, side: Side = Side.BUY) -> Order:
    return Order(order_id="o1", symbol=SYMBOL, side=side, quantity=Decimal(qty))


def check(
    rm: RiskManager,
    qty: str,
    price: str = "100",
    current: str = "0",
    equity: str = "10000",
    gross: str = "0",
) -> bool:
    return rm.check(
        order=order(qty),
        instrument=BTC,
        reference_price=Decimal(price),
        current_quantity=Decimal(current),
        equity=Decimal(equity),
        gross_exposure=Decimal(gross),
        timestamp=TS,
    )


def test_order_notional_limit_blocks_oversized_order() -> None:
    rm = RiskManager(limits=RiskLimits(max_order_notional=Decimal("1000")))
    assert check(rm, "5") is True  # 500 notional
    assert check(rm, "50") is False  # 5000 notional
    assert rm.rejection_summary == {"order_notional": 1}


def test_position_notional_limit_accounts_for_existing_position() -> None:
    rm = RiskManager(
        limits=RiskLimits(max_order_notional=None, max_position_notional=Decimal("1000"))
    )
    # Already long 8 (800 notional); buying 5 more would reach 1300.
    assert check(rm, "5", current="8") is False
    assert rm.rejections[0].reason == "position_notional"


def test_gross_leverage_limit() -> None:
    # Notional caps disabled in both forms so leverage is what is under test;
    # otherwise the tighter equity-relative cap binds first and the rejection
    # reason would be order_notional.
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional=None,
            max_position_notional=None,
            max_order_notional_pct=None,
            max_position_notional_pct=None,
            max_gross_leverage=Decimal("2"),
        )
    )
    # Equity 1000, buying 30 units at 100 = 3000 notional = 3x.
    assert check(rm, "30", equity="1000") is False
    assert rm.rejections[0].reason == "gross_leverage"
    # 15 units = 1500 = 1.5x, allowed.
    assert check(rm, "15", equity="1000") is True


def test_orders_per_bar_limit_catches_runaway_loops() -> None:
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional=None,
            max_position_notional=None,
            max_order_notional_pct=None,
            max_position_notional_pct=None,
            max_gross_leverage=None,
            max_orders_per_bar=3,
        )
    )
    results = [check(rm, "1") for _ in range(5)]
    assert results == [True, True, True, False, False]
    rm.start_bar()
    assert check(rm, "1") is True, "counter must reset each bar"


def test_drawdown_halt_stops_all_trading() -> None:
    rm = RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.10")))
    rm.observe_equity(Decimal("10000"))
    rm.observe_equity(Decimal("9500"))  # -5%, fine
    assert rm.halted is False
    rm.observe_equity(Decimal("8500"))  # -15%, breach
    assert rm.halted is True
    assert check(rm, "1") is False
    assert rm.rejections[-1].reason == "halted"


def test_limits_can_be_fully_disabled_for_isolation() -> None:
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional=None,
            max_position_notional=None,
            max_order_notional_pct=None,
            max_position_notional_pct=None,
            max_gross_leverage=None,
            max_drawdown_pct=None,
            max_margin_utilisation=None,
            max_orders_per_bar=None,
        )
    )
    assert rm.limits.everything_disabled
    assert check(rm, "1000000", equity="1") is True
    assert rm.rejections == []


def test_engine_does_not_execute_rejected_orders() -> None:
    """End-to-end: a blocked order must produce no fill."""

    class GreedyBuyer(Strategy):
        name = "GreedyBuyer"

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            ctx.buy(SYMBOL, Decimal("100000"))  # absurd size

    bars = [
        Bar(
            symbol=SYMBOL,
            timestamp=TS + timedelta(days=i),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("1"),
        )
        for i in range(3)
    ]
    bt = Backtester(
        instruments={SYMBOL: BTC},
        starting_cash=Decimal("10000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits(max_order_notional=Decimal("5000"))),
    )
    result = bt.run(GreedyBuyer(), InMemoryBarFeed(bars))

    assert result.fills == [], "risk-rejected orders must never reach execution"
    assert len(result.rejections) == 3
    assert result.ending_equity == Decimal("10000.00")


def test_halt_still_permits_closing_the_position() -> None:
    """A drawdown halt must not trap the account in the losing position."""
    rm = RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.10")))
    rm.observe_equity(Decimal("10000"))
    rm.observe_equity(Decimal("8000"))  # -20%, breach
    assert rm.halted is True

    # Long 10; selling 10 flattens -> must be allowed.
    assert (
        rm.check(
            order=order("10", Side.SELL),
            instrument=BTC,
            reference_price=Decimal("100"),
            current_quantity=Decimal("10"),
            equity=Decimal("8000"),
            gross_exposure=Decimal("1000"),
            timestamp=TS,
        )
        is True
    ), "must be able to flatten while halted"

    # Buying more adds exposure -> must still be blocked.
    assert check(rm, "5", current="10") is False
    assert rm.rejections[-1].reason == "halted"


def test_halt_blocks_new_positions_from_flat() -> None:
    """Flat plus halted means no new exposure, in either direction."""
    rm = RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.10")))
    rm.observe_equity(Decimal("10000"))
    rm.observe_equity(Decimal("8000"))
    assert rm.halted is True
    assert check(rm, "1", current="0") is False


def test_partial_reduction_is_permitted_while_halted() -> None:
    """Reducing 10 -> 3 is closer to flat, so it passes."""
    rm = RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.10")))
    rm.observe_equity(Decimal("10000"))
    rm.observe_equity(Decimal("8000"))
    assert (
        rm.check(
            order=order("7", Side.SELL),
            instrument=BTC,
            reference_price=Decimal("100"),
            current_quantity=Decimal("10"),
            equity=Decimal("8000"),
            gross_exposure=Decimal("1000"),
            timestamp=TS,
        )
        is True
    )


def test_flipping_through_zero_is_not_a_reduction_while_halted() -> None:
    """Long 10 -> short 5 ends further from flat on the other side: blocked."""
    rm = RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.10")))
    rm.observe_equity(Decimal("10000"))
    rm.observe_equity(Decimal("8000"))
    assert (
        rm.check(
            order=order("25", Side.SELL),
            instrument=BTC,
            reference_price=Decimal("100"),
            current_quantity=Decimal("10"),
            equity=Decimal("8000"),
            gross_exposure=Decimal("1000"),
            timestamp=TS,
        )
        is False
    ), "10 long -> 15 short is not risk-reducing"
