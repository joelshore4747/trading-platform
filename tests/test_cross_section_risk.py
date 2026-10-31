"""Cross-section orders fill inside the timestamp they were approved under.

`RiskManager.start_bar` clears pending approvals when the timestamp advances.
`on_cross_section` orders are approved under the arriving bar's timestamp and
fill at its open, so the watermark never clears them: without `release_pending`
after the fills, a flatten stays pending while the position is already flat,
and the next order on that symbol is measured against a phantom short.

Mutation that turns the first test red: delete the `risk.release_pending()`
call in `Backtester.run`. Found by `risk-auditor` on the 9b fix, 2026-09-02.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_platform.core.events import Bar, Fill, Order
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


class FlattenInCrossSectionThenReenter(Strategy):
    """Buy 10 on the first bar; once long, flatten in the cross-section hook;
    whenever flat afterwards, try to buy 10 again in `on_bar`."""

    name = "reenter"

    def __init__(self) -> None:
        self.bars = 0

    def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
        self.bars += 1
        if self.bars == 1 or (self.bars >= 3 and ctx.position(SYMBOL) == 0):
            ctx.buy(SYMBOL, Decimal("10"))

    def on_cross_section(self, timestamp: datetime, ctx: StrategyContext) -> None:
        held = ctx.position(SYMBOL)
        if held > 0:
            ctx.sell(SYMBOL, held)


def _bars() -> list[Bar]:
    closes = ["100", "50", "50", "50", "50"]
    opens = ["100", "100", "50", "50", "50"]
    return [
        Bar(
            symbol=SYMBOL,
            timestamp=TS + timedelta(days=i),
            open=Decimal(o),
            high=Decimal(max(o, c, key=Decimal)),
            low=Decimal(min(o, c, key=Decimal)),
            close=Decimal(c),
            volume=Decimal("1"),
        )
        for i, (o, c) in enumerate(zip(opens, closes, strict=True))
    ]


def test_a_halted_account_cannot_reenter_after_a_cross_section_flatten() -> None:
    """Long 10 at 100 on 10,000; the close halves and trips a 1% halt. The
    cross-section flatten is allowed (reducing). The re-entry on the same
    instant must be refused as `halted` — under the double count it was
    labelled reducing against a phantom short of 10 and went through."""
    bt = Backtester(
        instruments={SYMBOL: BTC},
        starting_cash=Decimal("10000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.01"))),
    )
    result = bt.run(FlattenInCrossSectionThenReenter(), InMemoryBarFeed(_bars()))

    assert result.halted is True
    assert [(f.side.name, f.quantity) for f in result.fills] == [
        ("BUY", Decimal("10")),
        ("SELL", Decimal("10")),
    ]
    assert result.rejections, "the re-entries must be refused, not silently absent"
    assert {r.reason for r in result.rejections} == {"halted"}
    assert result.ending_equity == Decimal("9500.00")


def test_release_leaves_the_rate_counter_alone() -> None:

    rm = RiskManager(limits=RiskLimits(max_orders_per_bar=2))
    order = Order(order_id="o", symbol=SYMBOL, side=Side.BUY, quantity=Decimal("1"))

    def check() -> bool:
        return rm.check(
            order=order,
            instrument=BTC,
            reference_price=Decimal("100"),
            current_quantity=Decimal("0"),
            equity=Decimal("10000"),
            gross_exposure=Decimal("0"),
            timestamp=TS,
        )

    assert check() is True
    rm.release_filled(
        Fill(
            order_id="o",
            symbol=SYMBOL,
            timestamp=TS,
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
        )
    )
    assert check() is True
    assert check() is False, "the per-instant budget must survive a release"


# -- the gauntlet's finding, 2026-09-03: release per order, not per bar event ------------


AAA = crypto_spot("AAAUSDT")
BBB = crypto_spot("BBBUSDT")


def _two_symbol_bars(n: int, price: str = "100") -> list[Bar]:
    out = []
    for i in range(n):
        for sym in ("AAAUSDT", "BBBUSDT"):
            out.append(
                Bar(
                    symbol=sym,
                    timestamp=TS + timedelta(days=i),
                    open=Decimal(price),
                    high=Decimal(price),
                    low=Decimal(price),
                    close=Decimal(price),
                    volume=Decimal("1"),
                )
            )
    return out


def test_intent_split_across_symbols_bars_cannot_breach_the_position_cap() -> None:
    """Two buys of AAA in one instant, one from AAA's `on_bar` and one from
    BBB's. Neither has filled when BBB's bar arrives, and the blanket release
    forgot the first: 160,000 of AAA passed a 150,000 cap with the second
    order labelled as if the book were empty (gauntlet repro, shipped
    defaults). Per-order release keeps the first approval pending until its
    fill, so the second is measured against 80,000 already committed."""

    class BuyAAAFromBothBars(Strategy):
        name = "both-bars"

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            if b.timestamp == TS:
                ctx.buy("AAAUSDT", Decimal("800"))  # 80,000 at 100; cap 1.5 x 100,000

    bt = Backtester(
        instruments={"AAAUSDT": AAA, "BBBUSDT": BBB},
        starting_cash=Decimal("100000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits()),
    )
    result = bt.run(BuyAAAFromBothBars(), InMemoryBarFeed(_two_symbol_bars(3)))
    assert [(f.symbol, f.quantity) for f in result.fills] == [("AAAUSDT", Decimal("800"))]
    assert [r.reason for r in result.rejections] == ["position_notional"]


def test_a_halted_account_cannot_be_walked_through_flat_across_symbols_bars() -> None:
    """Halted, long 10 AAA. Sell 10 from AAA's bar (a flatten: allowed), then
    sell 10 again from BBB's bar in the same instant. With the first approval
    forgotten the second also read as a flatten and the account ended short
    10 with zero rejections. Now it is a flip through zero: refused, halted."""

    class FlattenTwice(Strategy):
        name = "flatten-twice"

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            if b.timestamp == TS and b.symbol == "AAAUSDT":
                ctx.buy("AAAUSDT", Decimal("10"))
            if b.timestamp == TS + timedelta(days=2):
                ctx.sell("AAAUSDT", Decimal("10"))  # once from each symbol's bar

    prices = ["100", "100", "50", "50"]
    feed_bars = []
    for i, p in enumerate(prices):
        for sym in ("AAAUSDT", "BBBUSDT"):
            feed_bars.append(
                Bar(
                    symbol=sym,
                    timestamp=TS + timedelta(days=i),
                    open=Decimal("100" if i < 2 else "50"),
                    high=Decimal("100"),
                    low=Decimal("50"),
                    close=Decimal(p),
                    volume=Decimal("1"),
                )
            )
    bt = Backtester(
        instruments={"AAAUSDT": AAA, "BBBUSDT": BBB},
        starting_cash=Decimal("10000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.01"))),
    )
    result = bt.run(FlattenTwice(), InMemoryBarFeed(feed_bars))
    assert result.halted is True
    assert [(f.side.name, f.quantity) for f in result.fills] == [
        ("BUY", Decimal("10")),
        ("SELL", Decimal("10")),
    ]
    assert {r.reason for r in result.rejections} == {"halted"}


def test_a_partial_fill_releases_only_its_share() -> None:

    rm = RiskManager(limits=RiskLimits(max_position_notional=Decimal("1500")))
    order = Order(order_id="o1", symbol=SYMBOL, side=Side.BUY, quantity=Decimal("10"))
    assert (
        rm.check(
            order=order,
            instrument=BTC,
            reference_price=Decimal("100"),
            current_quantity=Decimal("0"),
            equity=Decimal("10000"),
            gross_exposure=Decimal("0"),
            timestamp=TS,
        )
        is True
    )
    rm.release_filled(
        Fill(
            order_id="o1",
            symbol=SYMBOL,
            timestamp=TS,
            side=Side.BUY,
            quantity=Decimal("4"),
            price=Decimal("100"),
        )
    )
    # 4 filled (now in the position), 6 still pending: a further 6 projects to
    # 16 = 1,600 notional, over the 1,500 cap.
    more = Order(order_id="o2", symbol=SYMBOL, side=Side.BUY, quantity=Decimal("6"))
    assert (
        rm.check(
            order=more,
            instrument=BTC,
            reference_price=Decimal("100"),
            current_quantity=Decimal("4"),
            equity=Decimal("10000"),
            gross_exposure=Decimal("400"),
            timestamp=TS,
        )
        is False
    )
    assert rm.rejections[-1].reason == "position_notional"
