"""`target_notional` reaches the other side of flat in two orders (ADR 0008).

Mutation that turns these red: restoring the single-delta implementation. The
first test would then see two fills instead of three; the second would see the
flatten refused along with the entry, leaving the account long when it asked
to be short and could at least have been flat.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_platform.core.events import Bar
from trading_platform.core.instruments import Spot, crypto_spot
from trading_platform.core.types import AssetClass
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.engine.backtester import Backtester
from trading_platform.engine.execution import ZERO_COST
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.base import Strategy, StrategyContext

TS = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOL = "BTCUSDT"
BTC = crypto_spot(SYMBOL)


class Flipper(Strategy):
    """Long `first` on the first bar, then `second` on the third."""

    name = "Flipper"

    def __init__(self, first: str = "5000", second: str = "-5000") -> None:
        self.seen = 0
        self.first = Decimal(first)
        self.second = Decimal(second)

    def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
        self.seen += 1
        if self.seen == 1:
            ctx.target_notional(SYMBOL, self.first)
        elif self.seen == 3:
            ctx.target_notional(SYMBOL, self.second)


def _bars(n: int = 5) -> list[Bar]:
    return [
        Bar(
            symbol=SYMBOL,
            timestamp=TS + timedelta(days=i),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("1"),
        )
        for i in range(n)
    ]


def test_a_crossing_target_is_a_flatten_then_an_entry() -> None:
    bt = Backtester(
        instruments={SYMBOL: BTC},
        starting_cash=Decimal("10000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits()),
    )
    result = bt.run(Flipper(), InMemoryBarFeed(_bars()))

    assert result.rejections == []
    quantities = [(f.side.name, f.quantity) for f in result.fills]
    assert quantities == [("BUY", Decimal("50")), ("SELL", Decimal("50")), ("SELL", Decimal("50"))]
    assert result.equity_curve[-1].equity == Decimal("10000.00")


def test_the_entry_is_capped_on_its_own_and_the_flatten_still_lands() -> None:
    """Position cap 3,000: long 2,500, then target short 5,000. The 5,000 entry
    is refused; the flatten is not. Under the old single-order flip the whole
    7,500 move was one order, and with the reducing label gone it would have
    been refused entire — leaving the account long 2,500 it had asked to leave."""
    bt = Backtester(
        instruments={SYMBOL: BTC},
        starting_cash=Decimal("10000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(
            limits=RiskLimits(max_order_notional=None, max_position_notional=Decimal("3000"))
        ),
    )
    result = bt.run(Flipper("2500", "-5000"), InMemoryBarFeed(_bars()))

    quantities = [(f.side.name, f.quantity) for f in result.fills]
    assert quantities == [("BUY", Decimal("25")), ("SELL", Decimal("25"))]
    assert [r.reason for r in result.rejections] == ["position_notional"]


def test_a_short_to_long_flip_is_a_buy_flatten_then_a_buy_entry() -> None:
    """The mirror cell. With `flatten_side` hard-wired to SELL the flatten
    would deepen the short, and the full suite stayed green on exactly that
    mutation until this test existed (guard-auditor, 2026-09-02)."""
    bt = Backtester(
        instruments={SYMBOL: BTC},
        starting_cash=Decimal("10000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits()),
    )
    result = bt.run(Flipper("-5000", "5000"), InMemoryBarFeed(_bars()))

    assert result.rejections == []
    quantities = [(f.side.name, f.quantity) for f in result.fills]
    assert quantities == [("SELL", Decimal("50")), ("BUY", Decimal("50")), ("BUY", Decimal("50"))]


def test_the_delta_is_rounded_to_the_lot_not_the_target() -> None:
    """Off-lot position 1.5 (lot 1), target 240 at 100 = 2.4: the delta 0.9
    rounds to one whole lot. Rounding the target to 2 first would emit an
    order for 0.5 — a sub-lot order the venue would refuse
    (risk-auditor, 2026-09-02)."""
    whole_lots = Spot(
        symbol=SYMBOL,
        asset_class=AssetClass.SPOT,
        quote_currency="USDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("1"),
    )

    class OddThenTarget(Strategy):
        name = "odd"

        def __init__(self) -> None:
            self.seen = 0

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            self.seen += 1
            if self.seen == 1:
                ctx.buy(SYMBOL, Decimal("1.5"))
            elif self.seen == 3:
                ctx.target_notional(SYMBOL, Decimal("240"))

    bt = Backtester(
        instruments={SYMBOL: whole_lots},
        starting_cash=Decimal("10000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits()),
    )
    result = bt.run(OddThenTarget(), InMemoryBarFeed(_bars()))
    assert [(f.side.name, f.quantity) for f in result.fills] == [
        ("BUY", Decimal("1.5")),
        ("BUY", Decimal("1")),
    ]
