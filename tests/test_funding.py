"""Perpetual funding settlement.

Funding is frequently the dominant P&L term on a held perp, so these tests
guard a number that matters as much as the price series. The two properties
that must never break: a settlement is applied **exactly once**, and never to a
position that was flat at that instant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_platform.core.events import Bar
from trading_platform.core.instruments import crypto_perpetual, crypto_spot
from trading_platform.core.types import funding_payment
from trading_platform.data.binance import FundingRate
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.data.funding import FundingSchedule
from trading_platform.engine.backtester import Backtester
from trading_platform.engine.execution import ZERO_COST
from trading_platform.engine.portfolio import Portfolio
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.base import Strategy, StrategyContext

START = datetime(2025, 1, 1, tzinfo=UTC)
SYMBOL = "BTC-PERP"
PERP = crypto_perpetual(SYMBOL)


def bar(i: int, price: str = "100") -> Bar:
    p = Decimal(price)
    return Bar(
        symbol=SYMBOL,
        timestamp=START + timedelta(days=i),
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal("1000"),
    )


def rate(hours: int, value: str = "0.0001", symbol: str = SYMBOL) -> FundingRate:
    return FundingRate(symbol, START + timedelta(hours=hours), Decimal(value))


def unconstrained(funding: FundingSchedule | None = None) -> Backtester:
    return Backtester(
        instruments={SYMBOL: PERP},
        starting_cash=Decimal("100000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(
            limits=RiskLimits(
                max_order_notional=None,
                max_position_notional=None,
                max_gross_leverage=None,
                max_drawdown_pct=None,
                max_margin_utilisation=None,
                max_orders_per_bar=None,
            )
        ),
        funding=funding,
    )


class BuyAndHold(Strategy):
    name = "BuyAndHold"

    def __init__(self, quantity: str = "10") -> None:
        self.quantity = Decimal(quantity)
        self.done = False

    def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
        if not self.done:
            ctx.buy(SYMBOL, self.quantity)
            self.done = True


# -- Schedule lookup: the exactly-once property ----------------------------


def test_half_open_interval_applies_a_boundary_settlement_once() -> None:
    """A settlement exactly on a bar timestamp belongs to that bar alone."""
    t = START + timedelta(hours=24)
    schedule = FundingSchedule([FundingRate(SYMBOL, t, Decimal("0.0001"))])

    before = schedule.settlements_in(START, t)  # (START, t] — includes t
    after = schedule.settlements_in(t, t + timedelta(hours=24))  # (t, ...] — excludes t

    assert len(before) == 1, "the boundary settlement belongs to the earlier window"
    assert len(after) == 0, "and must not be picked up again by the next"


def test_first_window_takes_everything_up_to_the_first_bar() -> None:
    schedule = FundingSchedule([rate(0), rate(8), rate(16)])
    assert len(schedule.settlements_in(None, START + timedelta(hours=16))) == 3


def test_settlements_are_ordered_across_symbols() -> None:
    schedule = FundingSchedule(
        [
            rate(16, symbol="ETH-PERP"),
            rate(8, symbol=SYMBOL),
            rate(0, symbol="ETH-PERP"),
        ]
    )
    found = schedule.settlements_in(None, START + timedelta(hours=24))
    assert [f.timestamp for f in found] == sorted(f.timestamp for f in found)


def test_schedule_deduplicates_and_reports_interval() -> None:
    entries = [rate(h) for h in (0, 8, 16, 24)]
    schedule = FundingSchedule(entries + entries)  # overlapping pages
    assert len(schedule) == 4
    assert schedule.interval_hours(SYMBOL) == Decimal(8)
    assert schedule.symbols == (SYMBOL,)
    assert "4 settlements" in schedule.describe()


def test_empty_schedule_is_falsy() -> None:
    assert not FundingSchedule([])
    assert FundingSchedule([rate(0)])


# -- Payment direction ------------------------------------------------------


def test_long_pays_and_short_receives_on_a_positive_rate() -> None:
    pf = Portfolio(starting_cash=Decimal("100000"))
    pos = pf.position(PERP)
    pos.quantity = Decimal("10")  # long 10 @ 100 = 1000 notional

    paid = pf.apply_funding(PERP, Decimal("0.0001"), Decimal("100"))
    assert paid == Decimal("-0.1"), "long pays 1000 * 0.0001"
    assert pf.cash == Decimal("99999.90")

    pos.quantity = Decimal("-10")
    received = pf.apply_funding(PERP, Decimal("0.0001"), Decimal("100"))
    assert received == Decimal("0.1"), "short receives the same amount"


def test_negative_rate_inverts_both_sides() -> None:
    pf = Portfolio(starting_cash=Decimal("100000"))
    pos = pf.position(PERP)

    pos.quantity = Decimal("10")
    assert pf.apply_funding(PERP, Decimal("-0.0001"), Decimal("100")) == Decimal("0.1")

    pos.quantity = Decimal("-10")
    assert pf.apply_funding(PERP, Decimal("-0.0001"), Decimal("100")) == Decimal("-0.1")


def test_flat_position_is_never_charged() -> None:
    """The property that matters most: no position, no payment."""
    pf = Portfolio(starting_cash=Decimal("100000"))
    assert pf.apply_funding(PERP, Decimal("0.0001"), Decimal("100")) == Decimal("0")
    assert pf.cash == Decimal("100000")

    pf.position(PERP)  # exists but flat
    assert pf.apply_funding(PERP, Decimal("0.0001"), Decimal("100")) == Decimal("0")
    assert pf.cash == Decimal("100000")


def test_funding_is_tracked_apart_from_trading_pnl() -> None:
    pf = Portfolio(starting_cash=Decimal("100000"))
    pos = pf.position(PERP)
    pos.quantity = Decimal("10")
    pf.apply_funding(PERP, Decimal("0.0001"), Decimal("100"))

    assert pf.total_funding == Decimal("-0.1")
    assert pf.total_realised_pnl == Decimal("0"), "funding must not leak into trading P&L"


def test_shared_formula_has_one_definition() -> None:
    signed, r = Decimal("1000"), Decimal("0.0001")
    assert FundingRate(SYMBOL, START, r).payment_on(signed) == funding_payment(signed, r)


# -- End to end through the engine ------------------------------------------


def test_spot_backtest_is_bit_identical_without_funding() -> None:
    """The regression guard: adding this feature must not move any existing
    number when no funding schedule is supplied."""
    spot_symbol = "BTCUSDT"
    spot = crypto_spot(spot_symbol)
    bars = [
        Bar(
            symbol=spot_symbol,
            timestamp=START + timedelta(days=i),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal(str(95 + (i % 10))),  # stays inside [low, high]
            volume=Decimal("1000"),
        )
        for i in range(20)
    ]

    class Buyer(Strategy):
        name = "Buyer"

        def __init__(self) -> None:
            self.done = False

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            if not self.done:
                ctx.buy(spot_symbol, Decimal("10"))
                self.done = True

    def run(funding: FundingSchedule | None):  # type: ignore[no-untyped-def]
        bt = Backtester(
            instruments={spot_symbol: spot},
            starting_cash=Decimal("100000"),
            cost_model=ZERO_COST,
            risk_manager=RiskManager(limits=RiskLimits(max_drawdown_pct=None)),
            funding=funding,
        )
        return bt.run(Buyer(), InMemoryBarFeed(bars))

    without = run(None)
    with_empty = run(FundingSchedule([]))

    assert [p.equity for p in without.equity_curve] == [p.equity for p in with_empty.equity_curve]
    assert without.ending_equity == with_empty.ending_equity
    assert without.total_funding == Decimal("0")
    assert without.funding_settlements == 0


def test_each_settlement_is_applied_exactly_once_end_to_end() -> None:
    """Three daily bars, 8-hourly funding: 9 settlements, each charged once."""
    settlements = [rate(h) for h in range(0, 96, 8)]  # 0h..88h
    schedule = FundingSchedule(settlements)
    feed = InMemoryBarFeed([bar(i) for i in range(4)])

    result = unconstrained(schedule).run(BuyAndHold(), feed)

    # Position opens on bar 1 (order placed bar 0). Settlements at hours
    # 24, 32, 40, 48, 56, 64, 72, 80, 88 fall in windows where it is open.
    assert result.funding_settlements == 9
    assert result.total_funding < 0, "a long paying a positive rate loses cash"
    # 10 units @ 100 = 1000 notional, 0.0001 rate, 9 times.
    assert result.total_funding == Decimal("-0.9")


def test_settlements_inside_the_opening_bar_are_charged() -> None:
    """Bars are CLOSE-stamped, so bar 1 (stamped 24h) covers the interval
    (0h, 24h] and its open sits near 0h. A fill at that open means the position
    genuinely exists for settlements at 8h and 16h, so both are charged.

    This is exactly why funding is applied *after* the fills in the loop:
    placing it before would miss real payments on the opening bar.
    """
    schedule = FundingSchedule([rate(0), rate(8), rate(16)])
    feed = InMemoryBarFeed([bar(i) for i in range(3)])

    result = unconstrained(schedule).run(BuyAndHold(), feed)

    assert result.funding_settlements == 2, "8h and 16h fall inside the opening bar"
    assert result.total_funding == Decimal("-0.2")  # 1000 notional * 0.0001 * 2


def test_settlement_before_any_bar_finds_a_flat_book() -> None:
    """The 0h settlement lands on bar 0, where nothing has filled yet."""
    schedule = FundingSchedule([rate(0)])
    result = unconstrained(schedule).run(BuyAndHold(), InMemoryBarFeed([bar(i) for i in range(3)]))
    assert result.funding_settlements == 0, "no position existed at 0h"
    assert result.total_funding == Decimal("0")


def test_no_funding_when_strategy_never_trades() -> None:
    class DoNothing(Strategy):
        name = "DoNothing"

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            return

    schedule = FundingSchedule([rate(h) for h in range(0, 96, 8)])
    result = unconstrained(schedule).run(DoNothing(), InMemoryBarFeed([bar(i) for i in range(4)]))
    assert result.funding_settlements == 0


def test_funding_reduces_ending_equity_versus_no_funding() -> None:
    """The whole point: omitting funding overstates a perp strategy."""
    feed_bars = [bar(i) for i in range(6)]
    schedule = FundingSchedule([rate(h) for h in range(0, 144, 8)])

    without = unconstrained(None).run(BuyAndHold(), InMemoryBarFeed(feed_bars))
    with_funding = unconstrained(schedule).run(BuyAndHold(), InMemoryBarFeed(feed_bars))

    assert with_funding.ending_equity < without.ending_equity
    assert with_funding.funding_settlements > 0


def test_short_position_receives_funding() -> None:
    class Shorter(Strategy):
        name = "Shorter"

        def __init__(self) -> None:
            self.done = False

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            if not self.done:
                ctx.sell(SYMBOL, Decimal("10"))
                self.done = True

    schedule = FundingSchedule([rate(h) for h in range(0, 96, 8)])
    result = unconstrained(schedule).run(Shorter(), InMemoryBarFeed([bar(i) for i in range(4)]))

    assert result.total_funding > 0, "a short receives when the rate is positive"


def test_settlement_in_a_data_gap_is_applied_once_and_counted() -> None:
    """A hole in the bar series still settles funding, but the charge lands on
    a later bar than it truly occurred — so it is counted, not passed over."""
    bars = [bar(0), bar(1), bar(5)]  # days 2, 3, 4 missing
    schedule = FundingSchedule([rate(h) for h in range(0, 144, 8)])

    result = unconstrained(schedule).run(BuyAndHold(), InMemoryBarFeed(bars))

    assert result.funding_settlements > 0
    assert result.funding_in_gaps > 0, "the mis-timed charges must be reported"
    assert result.funding_in_gaps <= result.funding_settlements


def test_no_gaps_reported_on_an_evenly_spaced_series() -> None:
    schedule = FundingSchedule([rate(h) for h in range(0, 96, 8)])
    result = unconstrained(schedule).run(BuyAndHold(), InMemoryBarFeed([bar(i) for i in range(4)]))
    assert result.funding_in_gaps == 0


def test_summary_reports_funding_only_when_present() -> None:
    schedule = FundingSchedule([rate(h) for h in range(0, 96, 8)])
    feed_bars = [bar(i) for i in range(4)]

    with_funding = unconstrained(schedule).run(BuyAndHold(), InMemoryBarFeed(feed_bars))
    without = unconstrained(None).run(BuyAndHold(), InMemoryBarFeed(feed_bars))

    assert "Funding" in with_funding.summary()
    assert "Funding" not in without.summary()


def test_unknown_symbol_in_schedule_is_ignored() -> None:
    """A schedule covering symbols the run does not trade must not break it."""
    schedule = FundingSchedule([rate(h, symbol="DOGE-PERP") for h in range(0, 96, 8)])
    result = unconstrained(schedule).run(BuyAndHold(), InMemoryBarFeed([bar(i) for i in range(4)]))
    assert result.funding_settlements == 0
