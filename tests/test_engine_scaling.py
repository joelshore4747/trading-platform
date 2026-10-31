"""The engine must stay linear in universe size, and the cache must not drift.

Before this, per-event cost doubled every time the universe doubled — 12.3
microseconds at 10 symbols, 289.1 at 400 — because `positions_value` re-walked
every position on every bar event. On a multi-symbol feed that is
O(symbols x bars x positions), and it put one 5,000-symbol backtest at roughly
six hours and a 16-cell sweep at twenty-four days. No scanner is possible on
top of that.

Two properties are pinned here:

1. **Scaling stays flat.** A regression to quadratic must fail CI rather than
   be discovered when a sweep does not finish.
2. **The incremental mark never diverges from a full recompute.** A cache that
   silently drifts from the truth is worse than no cache, because every number
   downstream is quietly wrong instead of slow.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.core.events import Bar, Fill
from trading_platform.core.instruments import crypto_perpetual, crypto_spot
from trading_platform.core.types import Side
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.engine.backtester import Backtester
from trading_platform.engine.portfolio import Portfolio
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.base import Strategy, StrategyContext

START = datetime(2026, 1, 1, tzinfo=UTC)

UNCONSTRAINED = RiskLimits(
    max_order_notional=None,
    max_position_notional=None,
    max_order_notional_pct=None,
    max_position_notional_pct=None,
    max_gross_leverage=None,
    max_drawdown_pct=None,
    max_margin_utilisation=None,
    max_orders_per_bar=None,
)


class BuyAndHoldAll(Strategy):
    name = "BuyAndHoldAll"

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
        if ctx.is_flat(bar.symbol):
            ctx.buy(bar.symbol, Decimal("1"))


def universe(symbols: int, bars: int) -> list[Bar]:
    names = [f"S{i:04d}" for i in range(symbols)]
    out = [
        Bar(
            symbol=name,
            timestamp=START + timedelta(days=day),
            open=Decimal("100"),
            high=Decimal("107"),
            low=Decimal("99"),
            close=Decimal(str(100 + day % 7)),
            volume=Decimal("1"),
        )
        for day in range(bars)
        for name in names
    ]
    out.sort(key=lambda b: (b.timestamp, b.symbol))
    return out


def run(symbols: int, bars: int) -> tuple[float, int]:
    """Returns microseconds per bar event, and the event count."""
    feed_bars = universe(symbols, bars)
    backtester = Backtester(
        instruments={f"S{i:04d}": crypto_spot(f"S{i:04d}") for i in range(symbols)},
        starting_cash=Decimal("100000000"),
        risk_manager=RiskManager(limits=UNCONSTRAINED),
    )
    started = time.monotonic()
    backtester.run(BuyAndHoldAll(), InMemoryBarFeed(feed_bars))
    elapsed = time.monotonic() - started
    return elapsed / len(feed_bars) * 1e6, len(feed_bars)


# -- Scaling -----------------------------------------------------------------


@pytest.mark.slow
def test_per_event_cost_does_not_grow_with_the_universe() -> None:
    """The guard. Quadratic growth shows up here as a ratio near 2.0 per
    doubling; linear shows up near 1.0.

    The threshold is deliberately loose (3x across a 16x increase in universe
    size) because this is wall-clock on shared CI. Quadratic would be ~16x, so
    the test still separates the two regimes by a wide margin.
    """
    small, _ = run(50, 40)
    large, _ = run(800, 40)

    assert large < small * 3.0, (
        f"{small:.1f} us/event at 50 symbols against {large:.1f} at 800 — a 16x "
        "universe should not cost 3x per event. The engine has regressed toward "
        "O(symbols^2)."
    )


@pytest.mark.slow
def test_a_large_universe_completes_in_reasonable_time() -> None:
    """An absolute floor under the relative check above, in case both ends
    regress together."""
    per_event, events = run(400, 40)
    assert per_event < 40.0, f"{per_event:.1f} us/event at 400 symbols ({events:,} events)"


# -- The incremental mark must equal the full recompute ----------------------


def fill(symbol: str, qty: str, price: str, side: Side = Side.BUY) -> Fill:
    return Fill(
        order_id="o",
        symbol=symbol,
        timestamp=START,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
    )


def test_incremental_mark_tracks_the_full_recompute_through_a_whole_run() -> None:
    """Checked after every single mutation, not just at the end. A cache that
    is right at the end and wrong in the middle still poisons the equity curve.
    """
    spot, perp = crypto_spot("AAA"), crypto_perpetual("BBB")
    portfolio = Portfolio(starting_cash=Decimal("100000"))
    prices: dict[str, Decimal] = {}

    steps: list[tuple[str, object]] = [
        ("price", ("AAA", "100")),
        ("fill", (spot, fill("AAA", "10", "100"))),
        ("price", ("AAA", "110")),
        ("price", ("BBB", "50")),
        ("fill", (perp, fill("BBB", "20", "50"))),
        ("price", ("BBB", "45")),
        ("fill", (spot, fill("AAA", "4", "110", Side.SELL))),
        ("price", ("AAA", "90")),
        ("fill", (perp, fill("BBB", "20", "45", Side.SELL))),  # flat
        ("price", ("BBB", "60")),
        ("fill", (spot, fill("AAA", "6", "90", Side.SELL))),  # flat
        ("price", ("AAA", "200")),
    ]

    for kind, payload in steps:
        if kind == "price":
            symbol, value = payload  # type: ignore[misc]
            prices[str(symbol)] = Decimal(str(value))
            portfolio.observe_price(str(symbol), Decimal(str(value)))
        else:
            instrument, f = payload  # type: ignore[misc]
            portfolio.apply_fill(instrument, f)  # type: ignore[arg-type]

        assert portfolio.marked_positions_value == portfolio.positions_value(prices), (
            f"cache diverged after {kind} {payload}"
        )
        assert portfolio.marked_equity == portfolio.equity(prices)


def test_incremental_mark_agrees_with_the_recompute_on_fractional_quantities() -> None:
    """The case exact equality cannot cover.

    Scaling in at fractional sizes makes `average_price` a 28-digit quotient,
    and a running sum of those rounds differently from a sum taken afresh. The
    property that matters is not bit-identity but that the gap stays far below
    the cent `quantize_cash` rounds to — so `marked_equity` and `equity` never
    disagree on a reported number.
    """
    spot, perp = crypto_spot("AAA"), crypto_perpetual("BBB")
    portfolio = Portfolio(starting_cash=Decimal("100000"))
    prices: dict[str, Decimal] = {}
    worst = Decimal("0")

    thirds = Decimal("1") / Decimal("3")
    sevenths = Decimal("1") / Decimal("7")

    for step in range(40):
        symbol, instrument, qty = (
            ("AAA", spot, thirds) if step % 2 == 0 else ("BBB", perp, sevenths)
        )
        price = Decimal("100") + Decimal(step) * Decimal("1") / Decimal("11")
        prices[symbol] = price
        portfolio.observe_price(symbol, price)
        side = Side.BUY if step % 7 != 6 else Side.SELL
        portfolio.apply_fill(instrument, fill(symbol, str(qty), str(price), side))

        gap = abs(portfolio.marked_positions_value - portfolio.positions_value(prices))
        worst = max(worst, gap)
        # The reported figure — what any caller actually sees — is identical.
        assert portfolio.marked_equity == portfolio.equity(prices), (
            f"reported equity diverged at step {step} by "
            f"{portfolio.marked_equity - portfolio.equity(prices)}"
        )

    assert worst < Decimal("1e-12"), f"cache drifted by {worst}, far more than rounding"


def test_funding_keeps_the_mark_consistent() -> None:
    perp = crypto_perpetual("BBB")
    portfolio = Portfolio(starting_cash=Decimal("100000"))
    prices = {"BBB": Decimal("50")}
    portfolio.observe_price("BBB", Decimal("50"))
    portfolio.apply_fill(perp, fill("BBB", "10", "50"))

    portfolio.apply_funding(perp, Decimal("0.0001"), Decimal("50"))
    assert portfolio.marked_positions_value == portfolio.positions_value(prices)
    assert portfolio.marked_equity == portfolio.equity(prices)


def test_a_symbol_with_no_price_contributes_nothing() -> None:
    spot = crypto_spot("AAA")
    portfolio = Portfolio(starting_cash=Decimal("100000"))
    portfolio.apply_fill(spot, fill("AAA", "10", "100"))
    assert portfolio.marked_positions_value == Decimal("0")
    assert portfolio.marked_positions_value == portfolio.positions_value({})


# -- One equity point per instant --------------------------------------------


@pytest.mark.parametrize("symbols", [1, 5, 20])
def test_the_equity_curve_has_one_point_per_timestamp(symbols: int) -> None:
    """It used to carry one point per bar EVENT, so twenty symbols produced
    600 points for 30 days. That inflated `num_periods`, chopped each instant's
    move into fragments, and moved the reported Sharpe from 87.50 to 67.36 on
    identical price paths. A portfolio has one value per instant.
    """
    bars = universe(symbols, 30)
    result = Backtester(
        instruments={f"S{i:04d}": crypto_spot(f"S{i:04d}") for i in range(symbols)},
        starting_cash=Decimal("10000000"),
        risk_manager=RiskManager(limits=UNCONSTRAINED),
    ).run(BuyAndHoldAll(), InMemoryBarFeed(bars))

    stamps = {bar.timestamp for bar in bars}
    assert len(result.equity_curve) == len(stamps)
    assert len({p.timestamp for p in result.equity_curve}) == len(stamps)


def test_the_final_instant_is_not_dropped() -> None:
    """Marking is lazy — a streaming feed only knows an instant ended when the
    next one arrives — so the last one needs closing explicitly."""
    bars = universe(3, 10)
    result = Backtester(
        instruments={f"S{i:04d}": crypto_spot(f"S{i:04d}") for i in range(3)},
        starting_cash=Decimal("1000000"),
        risk_manager=RiskManager(limits=UNCONSTRAINED),
    ).run(BuyAndHoldAll(), InMemoryBarFeed(bars))

    assert result.equity_curve[-1].timestamp == bars[-1].timestamp


def test_a_single_symbol_run_is_unchanged() -> None:
    """One bar per instant, so the new behaviour must be identical to the old."""
    bars = universe(1, 25)
    result = Backtester(
        instruments={"S0000": crypto_spot("S0000")},
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=UNCONSTRAINED),
    ).run(BuyAndHoldAll(), InMemoryBarFeed(bars))
    assert len(result.equity_curve) == 25


# -- The context must not leak the rest of the instant -----------------------


def test_equity_seen_by_a_strategy_excludes_symbols_not_yet_revealed() -> None:
    """The reason marking per instant does NOT feed `ctx.equity`.

    A strategy acting on (T, AAA) must not see equity that already includes
    (T, ZZZ)'s close — the feed has not revealed it yet, and folding it in
    would be a cross-symbol lookahead of exactly the kind the fill ordering
    exists to prevent.
    """
    seen: list[Decimal] = []

    class Watcher(Strategy):
        name = "Watcher"

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            if bar.symbol == "AAA":
                seen.append(ctx.equity)
            if ctx.is_flat(bar.symbol):
                ctx.buy(bar.symbol, Decimal("1"))

    bars = sorted(
        [
            Bar(
                symbol=sym,
                timestamp=START + timedelta(days=d),
                open=Decimal("100"),
                high=Decimal(str(price)),
                low=Decimal("50"),
                close=Decimal(str(price)),
                volume=Decimal("1"),
            )
            for d in range(4)
            for sym, price in (("AAA", 100), ("ZZZ", 100 + d * 500))
        ],
        key=lambda b: (b.timestamp, b.symbol),
    )
    Backtester(
        instruments={"AAA": crypto_spot("AAA"), "ZZZ": crypto_spot("ZZZ")},
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=UNCONSTRAINED),
    ).run(Watcher(), InMemoryBarFeed(bars))

    # ZZZ rockets 100 -> 1600, one 500 step per instant. AAA's reading at
    # instant T must carry ZZZ's price at T-1, never T.
    #
    # Asserted as exact values rather than an upper bound. The bound this
    # replaced was `all(e <= 110000)` against a 100,000 account holding ONE
    # unit of a instrument that moves 1,500 in total — it could not be
    # breached, and a lookahead audit confirmed it passed unchanged on a
    # deliberately leaked engine. A guard on the property a whole commit rests
    # on has to be able to fail. This one does: the same leaked engine
    # produces [100000.00, 99999.87, 100999.74, 101499.74], differing from the
    # second reading onward.
    assert seen, "the watcher should have acted"
    assert seen == [
        Decimal("100000.00"),
        Decimal("99999.87"),
        Decimal("100499.74"),
        Decimal("100999.74"),
    ], (
        f"equity seen while processing AAA: {seen} — a same-instant ZZZ price has "
        "leaked into the strategy's view"
    )


def test_a_strategy_sees_its_own_fill_immediately_in_on_fill() -> None:
    """A deliberate behaviour change from holding a live reference.

    `_sync` used to hand the context a *copy* of the position dict, taken at
    step 4 of the loop. `on_fill` runs at step 1, so a strategy asking "what is
    my position?" right after being told it had filled received the previous
    bar's snapshot — quantity 0 immediately after buying 1. Holding a
    reference makes that answer correct.

    Not a leak: the fill is strictly in the past when `on_fill` runs. It is
    pinned here because it is a change, and because a stale answer to that
    question is the sort of thing a strategy quietly builds a bug on.
    """
    seen_in_fill: list[Decimal] = []

    class Probe(Strategy):
        name = "Probe"

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            if ctx.is_flat(bar.symbol):
                ctx.buy(bar.symbol, Decimal("1"))

        def on_fill(self, f, ctx: StrategyContext) -> None:  # type: ignore[no-untyped-def]
            seen_in_fill.append(ctx.position(f.symbol))

    bars = universe(1, 4)
    Backtester(
        instruments={"S0000": crypto_spot("S0000")},
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=UNCONSTRAINED),
    ).run(Probe(), InMemoryBarFeed(bars))

    assert seen_in_fill == [Decimal("1")], f"expected the post-fill quantity, got {seen_in_fill}"


def test_probing_an_unknown_symbol_does_not_create_state() -> None:
    """`_portfolio_quantity` was a defaultdict, so asking about a symbol
    created an entry for it — a scanner touching 5,000 tickers materialised
    5,000 of them whether or not any had data."""
    context = StrategyContext(instruments={})
    assert context.position("NEVER_TRADED") == Decimal("0")
    assert context.is_flat("ALSO_NEVER")
    assert context._positions == {}, "querying must not mutate"


def test_cash_and_margin_seen_by_a_strategy_carry_no_more_than_equity_does() -> None:
    """The context's new fields must share `ctx.equity`'s lookahead property.

    `cash`, `margin_reserved` and `free_capital` were added so a leveraged
    strategy can size against capital it actually has, and so the decision
    journal can record the state behind each order. They are set from the same
    `_sync` call as `equity`, at the same moment in the loop — this asserts
    that rather than trusting it, because a second path into engine state is
    exactly how the context stopped being narrow the last time.

    Structured as the equity test above: AAA reads while ZZZ rockets, and every
    reading must lag ZZZ by one instant.
    """
    seen: list[tuple[Decimal, Decimal, Decimal]] = []

    class Watcher(Strategy):
        name = "Watcher"

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            if bar.symbol == "AAA":
                seen.append((ctx.equity, ctx.cash, ctx.free_capital))
            if ctx.is_flat(bar.symbol):
                ctx.buy(bar.symbol, Decimal("1"))

    bars = sorted(
        [
            Bar(
                symbol=sym,
                timestamp=START + timedelta(days=d),
                open=Decimal("100"),
                high=Decimal(str(price)),
                low=Decimal("50"),
                close=Decimal(str(price)),
                volume=Decimal("1"),
            )
            for d in range(4)
            for sym, price in (("AAA", 100), ("ZZZ", 100 + d * 500))
        ],
        key=lambda b: (b.timestamp, b.symbol),
    )
    Backtester(
        instruments={"AAA": crypto_spot("AAA"), "ZZZ": crypto_spot("ZZZ")},
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=UNCONSTRAINED),
    ).run(Watcher(), InMemoryBarFeed(bars))

    assert seen, "the watcher should have acted"
    equities = [row[0] for row in seen]
    assert equities == [
        Decimal("100000.00"),
        Decimal("99999.87"),
        Decimal("100499.74"),
        Decimal("100999.74"),
    ], f"equity moved: {equities}"

    # Spot only, so nothing is margined and cash is what has actually been
    # spent. Pinned exactly: a bound here could not distinguish a correct
    # reading from a leaked one, which is the failure the equity test above
    # records having already made once.
    #
    # **Index 1 is the evidence.** Bars within an instant are ordered
    # (timestamp, symbol), so at (T1, AAA) only AAA's fill has settled — one
    # purchase of ~100.13 against the starting 100,000. ZZZ's fill at the same
    # instant is still ahead in the feed and correctly absent. By (T2, AAA)
    # both have landed and cash has fallen twice. A reading of 99,799.74 at
    # index 1 would mean the strategy saw a fill the feed had not reached.
    cash = [row[1] for row in seen]
    assert cash == [
        Decimal("100000"),
        Decimal("99899.87"),
        Decimal("99799.74"),
        Decimal("99799.74"),
    ], f"cash seen while processing AAA: {cash}"

    # No derivatives, so margin is zero throughout and free capital IS cash.
    # If these ever diverge on a spot-only run, the margin model has leaked
    # into a path it does not belong in.
    assert [row[2] for row in seen] == cash
