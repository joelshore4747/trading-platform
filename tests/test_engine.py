"""Execution model, data validation, metrics, and an end-to-end run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.analytics.metrics import max_drawdown, sharpe_ratio, summarise, to_returns
from trading_platform.core.events import Bar, Order
from trading_platform.core.instruments import crypto_perpetual, crypto_spot
from trading_platform.core.types import OptionRight, OrderType, Side
from trading_platform.data.feed import CSVBarFeed, InMemoryBarFeed, synthetic_walk
from trading_platform.engine.backtester import Backtester
from trading_platform.engine.execution import ZERO_COST, CostModel, SimulatedExecutionEngine
from trading_platform.engine.portfolio import EquityPoint
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.base import Strategy, StrategyContext
from trading_platform.strategy.examples.sma_crossover import SMACrossover

TS = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOL = "BTCUSDT"
BTC = crypto_spot(SYMBOL)


def make_bar(i: int = 0, o: str = "100", h: str = "110", low: str = "90", c: str = "105") -> Bar:
    return Bar(
        symbol=SYMBOL,
        timestamp=TS + timedelta(days=i),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal("1000"),
    )


# -- Bar validation ---------------------------------------------------------


def test_bar_rejects_high_below_low() -> None:
    with pytest.raises(ValueError, match=r"high .* < low"):
        Bar(
            symbol=SYMBOL,
            timestamp=TS,
            open=Decimal("100"),
            high=Decimal("90"),
            low=Decimal("110"),
            close=Decimal("100"),
            volume=Decimal("1"),
        )


def test_bar_rejects_close_outside_range() -> None:
    with pytest.raises(ValueError, match=r"close .* outside"):
        Bar(
            symbol=SYMBOL,
            timestamp=TS,
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("999"),
            volume=Decimal("1"),
        )


def test_bar_rejects_negative_volume() -> None:
    with pytest.raises(ValueError, match="negative volume"):
        Bar(
            symbol=SYMBOL,
            timestamp=TS,
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("100"),
            volume=Decimal("-1"),
        )


# -- Order validation -------------------------------------------------------


def test_limit_order_requires_price() -> None:
    with pytest.raises(ValueError, match="requires a limit_price"):
        Order(
            order_id="o",
            symbol=SYMBOL,
            side=Side.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.LIMIT,
        )


def test_market_order_rejects_price() -> None:
    with pytest.raises(ValueError, match="must not carry a limit_price"):
        Order(
            order_id="o",
            symbol=SYMBOL,
            side=Side.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.MARKET,
            limit_price=Decimal("100"),
        )


def test_order_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValueError, match="quantity must be positive"):
        Order(order_id="o", symbol=SYMBOL, side=Side.BUY, quantity=Decimal("0"))


# -- Cost model -------------------------------------------------------------


def test_costs_always_work_against_the_trader() -> None:
    cm = CostModel(half_spread_rate=Decimal("0.001"), slippage_rate=Decimal("0.001"))
    ref = Decimal("100")
    assert cm.effective_price(ref, Side.BUY) > ref, "buys pay up"
    assert cm.effective_price(ref, Side.SELL) < ref, "sells receive less"


def test_commission_respects_minimum() -> None:
    cm = CostModel(commission_rate=Decimal("0.001"), minimum_commission=Decimal("1"))
    assert cm.commission(Decimal("100")) == Decimal("1")  # 0.10 -> floored to 1
    assert cm.commission(Decimal("100000")) == Decimal("100")


# -- Execution --------------------------------------------------------------


def test_limit_buy_does_not_fill_above_the_limit() -> None:
    ex = SimulatedExecutionEngine(cost_model=CostModel(commission_rate=Decimal("0")))
    ex.submit(
        Order(
            order_id="o",
            symbol=SYMBOL,
            side=Side.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("95"),
        ),
        BTC,
    )
    # Bar never trades at or below 95.
    fills = ex.process(make_bar(o="100", h="110", low="99", c="105"), TS)
    assert fills == []
    assert ex.pending_count == 1, "unfilled limit stays working"


def test_limit_buy_fills_when_price_trades_through() -> None:
    ex = SimulatedExecutionEngine(cost_model=CostModel(commission_rate=Decimal("0")))
    ex.submit(
        Order(
            order_id="o",
            symbol=SYMBOL,
            side=Side.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("95"),
        ),
        BTC,
    )
    fills = ex.process(make_bar(o="100", h="110", low="90", c="105"), TS)
    assert len(fills) == 1
    assert fills[0][0].price <= Decimal("96"), "fills at the limit, not the open"


def test_a_marketable_limit_buy_does_not_fill_at_the_bar_low() -> None:
    """The fill price must not be the best price the bar happened to print.

    Found by `scripts/run_mutation_eval.py` (defect L02): substituting
    ``bar.low`` for ``min(limit, bar.open)`` in ``_resolve_price`` left the
    whole suite green. The test above cannot catch it — ``price <= 96`` is
    one-sided, and a fill at the low of 90 satisfies it comfortably while
    assuming the order was placed at the single best tick of a bar that had not
    finished printing.

    Pinned exactly rather than bounded, because a bound is what failed.
    """
    ex = SimulatedExecutionEngine(cost_model=ZERO_COST)
    ex.submit(
        Order(
            order_id="o",
            symbol=SYMBOL,
            side=Side.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("95"),
        ),
        BTC,
    )
    bar = make_bar(o="100", h="110", low="90", c="105")
    fills = ex.process(bar, TS)

    assert len(fills) == 1
    assert fills[0][0].price == Decimal("95"), (
        "a limit below the open fills at the limit, never at the bar's low"
    )
    assert fills[0][0].price > bar.low


def test_a_marketable_limit_sell_does_not_fill_at_the_bar_high() -> None:
    """The mirror of the above. L02 mutated the buy path; the sell path is the
    same shape and would have been the same silent improvement."""
    ex = SimulatedExecutionEngine(cost_model=ZERO_COST)
    ex.submit(
        Order(
            order_id="o",
            symbol=SYMBOL,
            side=Side.SELL,
            quantity=Decimal("1"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("105"),
        ),
        BTC,
    )
    bar = make_bar(o="100", h="110", low="90", c="105")
    fills = ex.process(bar, TS)

    assert len(fills) == 1
    assert fills[0][0].price == Decimal("105"), (
        "a limit above the open fills at the limit, never at the bar's high"
    )
    assert fills[0][0].price < bar.high


def test_orders_for_other_symbols_stay_queued() -> None:
    ex = SimulatedExecutionEngine()
    other = crypto_spot("ETHUSDT")
    ex.submit(Order(order_id="o", symbol="ETHUSDT", side=Side.BUY, quantity=Decimal("1")), other)
    assert ex.process(make_bar(), TS) == []
    assert ex.pending_count == 1, "must not drop orders for symbols not in this bar"


# -- Feeds ------------------------------------------------------------------


def test_feed_rejects_unknown_instrument() -> None:
    bt = Backtester(instruments={SYMBOL: BTC})
    feed = InMemoryBarFeed(
        [
            Bar(
                symbol="DOGEUSDT",
                timestamp=TS,
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=Decimal("1"),
            )
        ]
    )

    class Noop(SMACrossover):
        pass

    with pytest.raises(KeyError, match="no registered instrument"):
        bt.run(Noop(SYMBOL), feed)


def test_csv_feed_requires_expected_columns(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "bad.csv"
    bad.write_text("timestamp,symbol,close\n2026-01-01,BTC,100\n")
    with pytest.raises(ValueError, match="missing required columns"):
        CSVBarFeed(bad)


def test_csv_feed_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "bars.csv"
    path.write_text(
        "timestamp,symbol,open,high,low,close,volume\n"
        "2026-01-01T00:00:00,BTCUSDT,100,110,90,105,1000\n"
        "2026-01-02T00:00:00,BTCUSDT,105,115,95,110,1200\n"
    )
    feed = CSVBarFeed(path)
    bars = list(feed)
    assert feed.symbols == ("BTCUSDT",)
    assert len(bars) == 2
    assert bars[0].close == Decimal("105")
    assert bars[0].timestamp.tzinfo is not None, "naive timestamps must be made UTC-aware"


def test_synthetic_walk_is_deterministic() -> None:
    a = list(synthetic_walk(SYMBOL, TS, 20))
    b = list(synthetic_walk(SYMBOL, TS, 20))
    assert [x.close for x in a] == [x.close for x in b]


# -- Metrics ----------------------------------------------------------------


def curve(values: list[str]) -> list[EquityPoint]:
    return [
        EquityPoint(
            timestamp=TS + timedelta(days=i),
            equity=Decimal(v),
            cash=Decimal(v),
            positions_value=Decimal("0"),
        )
        for i, v in enumerate(values)
    ]


def test_max_drawdown_finds_worst_peak_to_trough() -> None:
    dd, duration = max_drawdown(curve(["100", "120", "60", "80", "130"]))
    assert dd == pytest.approx(0.5)  # 120 -> 60
    assert duration == 2


def test_metrics_handle_degenerate_series() -> None:
    """Short and flat series must return zeros, not raise."""
    assert to_returns(curve(["100"])) == []
    assert sharpe_ratio([], 252) == 0.0
    flat = summarise(curve(["100", "100", "100"]))
    assert flat.sharpe_ratio == 0.0
    assert flat.max_drawdown == 0.0
    assert flat.total_return == 0.0


def test_summarise_reports_growth() -> None:
    summary = summarise(curve(["100", "110", "121"]), periods_per_year=252)
    assert summary.total_return == pytest.approx(0.21)
    assert summary.win_rate == 1.0
    assert summary.num_periods == 3


# -- End to end -------------------------------------------------------------


def test_full_backtest_runs_and_reconciles() -> None:
    feed = synthetic_walk(SYMBOL, TS, periods=200, start_price=Decimal("100"))
    bt = Backtester(instruments={SYMBOL: BTC}, starting_cash=Decimal("100000"))
    strategy = SMACrossover(SYMBOL, fast=5, slow=20)
    result = bt.run(strategy, feed)

    assert result.bars_processed == 200
    assert len(result.equity_curve) == 200
    assert result.ending_equity > 0
    assert strategy.fills_seen == len(result.fills)

    # Equity must reconcile: cash + marked positions, computed independently.
    final = result.equity_curve[-1]
    assert final.equity == final.cash + final.positions_value

    summary = summarise(result.equity_curve, periods_per_year=365)
    assert -1.0 <= summary.max_drawdown <= 1.0
    assert isinstance(summary.report(), str)


def test_costs_reduce_returns_versus_frictionless() -> None:
    """Sanity check that the cost model is actually wired in."""
    from trading_platform.engine.execution import ZERO_COST

    def run(cost_model: CostModel) -> Decimal:
        feed = synthetic_walk(SYMBOL, TS, periods=200, start_price=Decimal("100"))
        bt = Backtester(
            instruments={SYMBOL: BTC},
            starting_cash=Decimal("100000"),
            cost_model=cost_model,
        )
        return bt.run(SMACrossover(SYMBOL, fast=5, slow=20), feed).ending_equity

    assert run(CostModel()) < run(ZERO_COST), "trading costs must reduce ending equity"


def test_sma_crossover_validates_its_parameters() -> None:
    with pytest.raises(ValueError, match="must be shorter than slow"):
        SMACrossover(SYMBOL, fast=30, slow=10)
    with pytest.raises(ValueError, match="allocation must be in"):
        SMACrossover(SYMBOL, fast=5, slow=20, allocation=Decimal("2"))


def test_option_intrinsic_value() -> None:
    from trading_platform.core.instruments import equity_option

    call = equity_option(
        "SPY", "SPY", Decimal("500"), datetime(2026, 6, 19).date(), OptionRight.CALL
    )
    put = equity_option("SPY", "SPY", Decimal("500"), datetime(2026, 6, 19).date(), OptionRight.PUT)
    assert call.intrinsic_value(Decimal("520")) == Decimal("20")
    assert call.intrinsic_value(Decimal("480")) == Decimal("0")
    assert put.intrinsic_value(Decimal("480")) == Decimal("20")
    assert put.intrinsic_value(Decimal("520")) == Decimal("0")


def test_a_position_held_through_a_long_silence_is_reported_as_stale() -> None:
    """A symbol that stops printing keeps its last close forever: `_refresh`
    goes on folding it into equity and `mark()` writes it into every
    subsequent point. That is right while the gap is an outage and a
    fabrication once the instrument has stopped trading, and bar data cannot
    tell those apart — so the age is disclosed alongside `funding_in_gaps`
    and the rejection rate rather than silently corrected.

    Measured on the real archive, only 0.12% of marks are stale at all,
    because that collector's outages are synchronised. The worst is 8 days
    13 hours, which is the shape this reproduces.
    """
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def bar_at(symbol: str, minutes: int, price: str) -> Bar:
        value = Decimal(price)
        return Bar(
            symbol=symbol,
            timestamp=start + timedelta(minutes=minutes),
            open=value,
            high=value,
            low=value,
            close=value,
            volume=Decimal("1"),
        )

    bars = [b for m in range(5) for b in (bar_at("DARK", m, "100"), bar_at("LIVE", m, "200"))]
    # DARK goes silent; LIVE keeps printing for another nine days.
    bars += [bar_at("LIVE", 60 * 24 * 9 + m, "200") for m in range(5, 20)]
    bars.sort(key=lambda b: (b.timestamp, b.symbol))

    class BuyDark(Strategy):
        name = "BuyDark"

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            if bar.symbol == "DARK" and ctx.is_flat("DARK"):
                ctx.buy("DARK", Decimal("1"))

    result = Backtester(
        instruments={s: crypto_perpetual(s) for s in ("DARK", "LIVE")},
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=RiskLimits()),
    ).run(BuyDark(), InMemoryBarFeed(bars))

    assert len(result.stale_marks) == 1
    symbol, age = result.stale_marks[0]
    assert symbol == "DARK"
    assert age > timedelta(days=8)
    assert "STALE MARKS" in result.summary()


def test_a_run_with_no_silence_reports_no_stale_marks() -> None:
    """A threshold that fires on ordinary data says nothing."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [
        Bar(
            symbol="AAA",
            timestamp=start + timedelta(minutes=m),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1"),
        )
        for m in range(60)
    ]

    class BuyOnce(Strategy):
        name = "BuyOnce"

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            if ctx.is_flat(bar.symbol):
                ctx.buy(bar.symbol, Decimal("1"))

    result = Backtester(
        instruments={"AAA": crypto_perpetual("AAA")},
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=RiskLimits()),
    ).run(BuyOnce(), InMemoryBarFeed(bars))

    assert result.stale_marks == ()
    assert "STALE MARKS" not in result.summary()
