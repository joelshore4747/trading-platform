"""Guards against lookahead bias.

These are the most important tests in the repository. A backtest engine that
leaks future information produces beautiful, worthless results. If one of these
fails, nothing else the platform reports can be trusted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.core.events import Bar
from trading_platform.core.instruments import crypto_spot
from trading_platform.data.feed import InMemoryBarFeed, OutOfOrderDataError
from trading_platform.engine.backtester import Backtester
from trading_platform.engine.execution import ZERO_COST
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.base import Strategy, StrategyContext

START = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOL = "BTCUSDT"
BTC = crypto_spot(SYMBOL)


def bar(i: int, close: str) -> Bar:
    price = Decimal(close)
    return Bar(
        symbol=SYMBOL,
        timestamp=START + timedelta(days=i),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("100"),
    )


def unconstrained_backtester(starting_cash: str = "100000") -> Backtester:
    """No costs and no risk limits, so the assertion isolates timing alone."""
    return Backtester(
        instruments={SYMBOL: BTC},
        starting_cash=Decimal(starting_cash),
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
    )


class BuyOnFirstBar(Strategy):
    name = "BuyOnFirstBar"

    def __init__(self) -> None:
        self.fill_timestamps: list[datetime] = []
        self.fill_prices: list[Decimal] = []
        self.submitted = False

    def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
        if not self.submitted:
            ctx.buy(SYMBOL, Decimal("1"))
            self.submitted = True

    def on_fill(self, fill, ctx: StrategyContext) -> None:  # type: ignore[no-untyped-def]
        self.fill_timestamps.append(fill.timestamp)
        self.fill_prices.append(fill.price)


def test_order_fills_on_the_next_bar_not_the_signal_bar() -> None:
    """An order submitted while seeing bar 0 must fill on bar 1."""
    feed = InMemoryBarFeed([bar(0, "100"), bar(1, "200"), bar(2, "300")])
    strategy = BuyOnFirstBar()
    unconstrained_backtester().run(strategy, feed)

    assert len(strategy.fill_timestamps) == 1
    assert strategy.fill_timestamps[0] == START + timedelta(days=1), (
        "fill must be stamped with bar 1, not the bar the signal was computed on"
    )
    assert strategy.fill_prices[0] == Decimal("200"), (
        "fill price must come from bar 1's open, not bar 0's close"
    )


def test_strategy_cannot_see_future_bars() -> None:
    """ctx.history must contain only bars at or before the current timestamp."""
    seen: list[tuple[datetime, int, datetime]] = []

    class HistoryInspector(Strategy):
        name = "HistoryInspector"

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            history = ctx.history(SYMBOL)
            seen.append((b.timestamp, len(history), history[-1].timestamp))

    feed = InMemoryBarFeed([bar(i, str(100 + i)) for i in range(5)])
    unconstrained_backtester().run(HistoryInspector(), feed)

    assert len(seen) == 5
    for i, (current_ts, history_len, latest_ts) in enumerate(seen):
        assert history_len == i + 1, "history must grow by exactly one bar per step"
        assert latest_ts == current_ts, "newest history bar must be the current bar"


def test_position_is_zero_on_the_bar_the_order_was_submitted() -> None:
    """Position must not reflect an order until it has actually filled."""
    positions: list[Decimal] = []

    class PositionWatcher(Strategy):
        name = "PositionWatcher"

        def __init__(self) -> None:
            self.done = False

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            positions.append(ctx.position(SYMBOL))
            if not self.done:
                ctx.buy(SYMBOL, Decimal("1"))
                self.done = True

    feed = InMemoryBarFeed([bar(0, "100"), bar(1, "100"), bar(2, "100")])
    unconstrained_backtester().run(PositionWatcher(), feed)

    assert positions[0] == Decimal("0"), "flat when the order is submitted"
    assert positions[1] == Decimal("1"), "filled by the time bar 1 is shown"
    assert positions[2] == Decimal("1")


def test_out_of_order_bars_are_rejected() -> None:
    """A feed that goes backwards in time must fail loudly, not silently."""
    bars = [bar(0, "100"), bar(5, "100"), bar(2, "100")]
    # InMemoryBarFeed sorts, so build the violation by bypassing the sort.
    feed = InMemoryBarFeed([])
    feed._bars = bars  # type: ignore[attr-defined]

    with pytest.raises(OutOfOrderDataError, match="Sort the source data"):
        list(feed)


def test_no_fills_when_strategy_never_trades() -> None:
    class DoNothing(Strategy):
        name = "DoNothing"

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            return

    feed = InMemoryBarFeed([bar(i, "100") for i in range(10)])
    result = unconstrained_backtester().run(DoNothing(), feed)

    assert result.fills == []
    assert result.ending_equity == result.starting_cash
    assert result.bars_processed == 10


def test_final_order_remains_unfilled_and_is_reported() -> None:
    """An order placed on the last bar has no next bar to fill against."""

    class BuyEveryBar(Strategy):
        name = "BuyEveryBar"

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            ctx.buy(SYMBOL, Decimal("1"))

    feed = InMemoryBarFeed([bar(0, "100"), bar(1, "100")])
    result = unconstrained_backtester().run(BuyEveryBar(), feed)

    assert len(result.fills) == 1, "only the first bar's order had a next bar"
    assert result.unfilled_orders == 1, "the last bar's order must be reported, not dropped"


# -- Multi-symbol: the blind spot that let a real leak through ---------------
#
# Everything above this line is single-symbol, and that is exactly why a
# cross-symbol leak survived every one of these tests. A multi-symbol feed
# emits several bars at the same instant, sorted by (timestamp, symbol), and
# the fill logic matched pending orders by symbol alone. An order submitted
# while processing AAA's bar at time T filled on the next iteration against
# ZZZ's bar *stamped T* — at that bar's open, the price at the start of the
# interval that had just ended. The one-bar delay inverted into a one-bar
# advantage.
#
# On the repository's own `correlated_series`, at default costs and with an
# identical fill count, the leak returned 18,004% at Sharpe 7.97 with a 0.62%
# max drawdown, against 34.4% / 0.49 / 25.6% honest.

EARLY = "AAAUSDT"
LATE = "ZZZUSDT"


def pair_bar(symbol: str, i: int, open_: str, close: str) -> Bar:
    lo, hi = min(Decimal(open_), Decimal(close)), max(Decimal(open_), Decimal(close))
    return Bar(
        symbol=symbol,
        timestamp=START + timedelta(days=i),
        open=Decimal(open_),
        high=hi,
        low=lo,
        close=Decimal(close),
        volume=Decimal("100"),
    )


class TradeLateOnEarlySignal(Strategy):
    """Decides on one symbol and trades another, in the same instant.

    The natural shape of a pairs or cross-sectional strategy, and the exact
    access pattern the leak rewarded.
    """

    name = "TradeLateOnEarlySignal"

    def __init__(self) -> None:
        self.fill_prices: list[Decimal] = []

    def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
        if b.symbol == EARLY and ctx.is_flat(LATE):
            ctx.buy(LATE, Decimal("1"))

    def on_fill(self, f, ctx: StrategyContext) -> None:  # type: ignore[no-untyped-def]
        self.fill_prices.append(f.price)


def two_symbol_backtester() -> Backtester:
    return Backtester(
        instruments={EARLY: crypto_spot(EARLY), LATE: crypto_spot(LATE)},
        starting_cash=Decimal("1000000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits()),
    )


def test_an_order_never_fills_on_a_bar_stamped_at_its_own_creation_instant() -> None:
    """The guard for the cross-symbol leak.

    ZZZ's day-1 bar runs 100 -> 200 and its day-2 bar opens at 200. A signal
    computed from day 1's close cannot be filled at 100, which is where that
    interval *started*.
    """
    bars = [
        # Bar 0 exists only so both symbols have a known price; an order for a
        # symbol the loop has not reached yet cannot be risk-checked and is
        # refused separately, which would otherwise mask what this test guards.
        pair_bar(EARLY, 0, "10", "10"),
        pair_bar(LATE, 0, "100", "100"),
        pair_bar(EARLY, 1, "10", "10"),
        pair_bar(LATE, 1, "100", "200"),
        pair_bar(EARLY, 2, "10", "10"),
        pair_bar(LATE, 2, "200", "200"),
    ]
    strategy = TradeLateOnEarlySignal()
    two_symbol_backtester().run(strategy, InMemoryBarFeed(bars))

    assert strategy.fill_prices, "the order should still fill, just one bar later"
    assert strategy.fill_prices[0] == Decimal("200"), (
        f"filled at {strategy.fill_prices[0]} — that is the open of the SAME-instant "
        "bar, a price from before the information that triggered the order"
    )


def test_a_cross_symbol_signal_cannot_beat_a_same_symbol_one() -> None:
    """The economic version: free money must not be available.

    Deciding on an alphabetically earlier symbol and trading a later one is the
    exploitable direction. If it outperforms the identical rule applied to one
    symbol, the engine is paying for sort order rather than for signal.
    """
    bars: list[Bar] = []
    price = Decimal("100")
    bars.append(pair_bar(EARLY, 0, "10", "10"))
    bars.append(pair_bar(LATE, 0, "100", "100"))
    for i in range(1, 60):
        # LATE moves hard within each bar, so a same-instant fill at its open
        # would be worth a great deal.
        nxt = price * (Decimal("1.10") if i % 2 else Decimal("0.94"))
        bars.append(pair_bar(EARLY, i, "10", "10"))
        bars.append(pair_bar(LATE, i, str(price), str(nxt)))
        price = nxt

    strategy = TradeLateOnEarlySignal()
    result = two_symbol_backtester().run(
        strategy, InMemoryBarFeed(sorted(bars, key=lambda b: (b.timestamp, b.symbol)))
    )

    for fill in result.fills:
        matching = [b for b in bars if b.symbol == fill.symbol and b.timestamp == fill.timestamp]
        assert matching, "every fill must land on a real bar"
        assert fill.timestamp > START, "no fill on the very first instant"


def test_orders_for_other_symbols_are_still_not_dropped() -> None:
    """The delay must postpone an order, never discard it — the original
    reason `process` keeps non-matching symbols queued."""
    bars = [
        pair_bar(EARLY, 0, "10", "10"),
        pair_bar(LATE, 0, "100", "100"),
        pair_bar(EARLY, 1, "10", "10"),
        pair_bar(LATE, 1, "100", "100"),
        pair_bar(EARLY, 2, "10", "10"),
        pair_bar(LATE, 2, "100", "100"),
        pair_bar(EARLY, 3, "10", "10"),
        pair_bar(LATE, 3, "100", "100"),
    ]
    strategy = TradeLateOnEarlySignal()
    result = two_symbol_backtester().run(strategy, InMemoryBarFeed(bars))

    assert len(result.fills) >= 1, "the entry must eventually fill"
    assert result.unfilled_orders == 0, "nothing left stranded in the queue"


def test_the_delay_holds_when_the_traded_symbol_sorts_first() -> None:
    """The safe direction must not have regressed into an extra bar of delay."""

    class TradeEarlyOnLateSignal(Strategy):
        name = "TradeEarlyOnLateSignal"

        def __init__(self) -> None:
            self.fill_prices: list[Decimal] = []

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            if b.symbol == LATE and ctx.is_flat(EARLY):
                ctx.buy(EARLY, Decimal("1"))

        def on_fill(self, f, ctx: StrategyContext) -> None:  # type: ignore[no-untyped-def]
            self.fill_prices.append(f.price)

    bars = [
        pair_bar(EARLY, 0, "10", "10"),
        pair_bar(LATE, 0, "100", "100"),
        # The signal fires on LATE's day-0 bar. EARLY sorts first, so its
        # day-0 bar has already gone by — the fill must land on day 1's open.
        pair_bar(EARLY, 1, "77", "80"),
        pair_bar(LATE, 1, "100", "100"),
    ]
    strategy = TradeEarlyOnLateSignal()
    two_symbol_backtester().run(strategy, InMemoryBarFeed(bars))

    assert strategy.fill_prices == [Decimal("77")], (
        "the safe direction must still fill on the next bar's open — not be "
        "pushed out an extra bar by the new guard"
    )


# -- The once-per-instant hook ------------------------------------------------
#
# `on_cross_section` is the one place a strategy is handed the whole instant at
# once, which makes it the one place a whole-instant leak could hide. These are
# the guards for it.


class RecordingRanker(Strategy):
    """Records what the cross-section looked like each time it was called."""

    name = "RecordingRanker"

    def __init__(self) -> None:
        self.seen: list[tuple[datetime, tuple[str, ...], dict[str, Decimal]]] = []
        self.bar_calls = 0

    def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
        self.bar_calls += 1

    def on_cross_section(self, timestamp: datetime, ctx: StrategyContext) -> None:
        prices = {s: p for s in ctx.cross_section() if (p := ctx.price(s)) is not None}
        self.seen.append((timestamp, ctx.cross_section(), prices))
        if ctx.is_flat(LATE):
            ctx.buy(LATE, Decimal("1"))


def paired_feed(instants: int) -> list[Bar]:
    bars: list[Bar] = []
    for i in range(instants):
        bars.append(pair_bar(EARLY, i, str(10 + i), str(11 + i)))
        bars.append(pair_bar(LATE, i, str(100 + 10 * i), str(105 + 10 * i)))
    return bars


def test_the_cross_section_hook_never_sees_a_price_from_a_later_instant() -> None:
    """The whole-instant leak, stated directly.

    Called for instant T, every price the context reports must be T's close —
    never T+1's. The hook fires while the feed is positioned on T+1's first
    bar, so an off-by-one in the placement would show up here and nowhere else.
    """
    bars = paired_feed(6)
    closes = {(b.symbol, b.timestamp): b.close for b in bars}
    strategy = RecordingRanker()
    two_symbol_backtester().run(strategy, InMemoryBarFeed(bars))

    assert strategy.seen, "the hook never fired"
    for timestamp, symbols, prices in strategy.seen:
        for symbol in symbols:
            assert prices[symbol] == closes[(symbol, timestamp)], (
                f"{symbol} at {timestamp} was priced at {prices[symbol]}, not its own "
                f"close {closes[(symbol, timestamp)]}"
            )


def test_the_cross_section_hook_sees_every_symbol_of_its_instant() -> None:
    """The reason the hook exists: no symbol is a bar behind any other.

    Ranking from `on_bar` would give the alphabetically-first symbol an extra
    bar of information in every comparison.
    """
    strategy = RecordingRanker()
    two_symbol_backtester().run(strategy, InMemoryBarFeed(paired_feed(6)))
    for _, symbols, _ in strategy.seen:
        assert symbols == (EARLY, LATE)


def test_an_order_from_the_hook_fills_on_the_next_instant() -> None:
    """The one-bar delay, which the hook must obey exactly as `on_bar` does."""
    bars = paired_feed(6)
    opens = {(b.symbol, b.timestamp): b.open for b in bars}
    strategy = RecordingRanker()
    result = two_symbol_backtester().run(strategy, InMemoryBarFeed(bars))

    assert result.fills, "the hook's order never filled"
    instants = sorted({b.timestamp for b in bars})
    decided_at = strategy.seen[0][0]
    first = result.fills[0]
    # The NEXT instant exactly. `> decided_at` would pass for a two-instant
    # delay too, and an over-delayed fill is a silent, plausible-looking
    # result rather than an error -- the same shape of bug as an early one,
    # costing return instead of inventing it.
    expected = instants[instants.index(decided_at) + 1]
    assert first.timestamp == expected, (
        f"decided at {decided_at}, filled at {first.timestamp}, expected {expected}"
    )
    assert first.price == opens[(first.symbol, first.timestamp)]


def test_the_final_instant_never_fires_the_hook() -> None:
    """Correct, not an omission: an order from it could never fill.

    Firing it would let a strategy act on information it can never trade on,
    and every such action would be silently discarded — which looks in the
    fill record exactly like a strategy that chose not to trade.
    """
    bars = paired_feed(6)
    last = max(b.timestamp for b in bars)
    strategy = RecordingRanker()
    two_symbol_backtester().run(strategy, InMemoryBarFeed(bars))

    assert [t for t, _, _ in strategy.seen] == sorted({b.timestamp for b in bars})[:-1]
    assert last not in [t for t, _, _ in strategy.seen]


def test_a_strategy_without_the_hook_runs_the_old_loop() -> None:
    """Bit-identity, asserted rather than assumed.

    Every result recorded before the hook existed was produced by a strategy
    that does not override it. If the loop is not additive, all of them need
    re-running before anything new can be believed.
    """

    class NoHook(Strategy):
        name = "NoHook"

        def __init__(self) -> None:
            self.equities: list[Decimal] = []

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            self.equities.append(ctx.equity)
            if b.symbol == LATE and ctx.is_flat(LATE):
                ctx.buy(LATE, Decimal("1"))

    bars = paired_feed(8)
    strategy = NoHook()
    result = two_symbol_backtester().run(strategy, InMemoryBarFeed(bars))

    # The equity a strategy without the hook observes is the RUNNING mark, not
    # the end-of-instant one -- backtester.py refuses to fold the current
    # instant's later closes into `ctx.equity`. One observation per bar event,
    # not one per instant, is what proves that path is untouched.
    assert len(strategy.equities) == len(bars)
    assert result.fills


def test_the_hook_fills_on_the_next_instant_for_the_earliest_symbol_too() -> None:
    """The sort-order guard, and the one the hook exists to make unnecessary.

    Buying the alphabetically FIRST symbol is the strict case. If the hook
    fires after that symbol's bar has already been through `execution.process`,
    its order misses the fill window and slips an extra instant — while an
    order for a later-sorting symbol still fills on time. That is the same
    dependence on sort order the hook was built to remove, reappearing on the
    execution side instead of the data side, and it is invisible in any test
    that trades a late-sorting symbol.
    """

    class BuyEarliest(Strategy):
        name = "BuyEarliest"

        def __init__(self) -> None:
            self.decided_at: datetime | None = None

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            pass

        def on_cross_section(self, timestamp: datetime, ctx: StrategyContext) -> None:
            if self.decided_at is None and ctx.is_flat(EARLY):
                self.decided_at = timestamp
                ctx.buy(EARLY, Decimal("1"))

    bars = paired_feed(6)
    instants = sorted({b.timestamp for b in bars})
    strategy = BuyEarliest()
    result = two_symbol_backtester().run(strategy, InMemoryBarFeed(bars))

    assert result.fills, "the order never filled"
    assert strategy.decided_at is not None
    expected = instants[instants.index(strategy.decided_at) + 1]
    assert result.fills[0].timestamp == expected, (
        f"decided at {strategy.decided_at}, filled at {result.fills[0].timestamp}, "
        f"expected the very next instant {expected}"
    )


def test_a_rejection_shown_to_the_hook_is_stamped_with_the_hook_s_own_instant() -> None:
    """The hook must not learn WHEN the next instant is.

    `_queue_orders` writes the timestamp it is given onto every
    `RiskRejection`, and `on_reject` fires inside the hook's own queueing
    pass — so passing the arriving bar's timestamp told a strategy the next
    instant's stamp while `ctx.now` still reported the previous one, before
    any bar of it had been observed.

    On a regular daily grid that is worth nothing. On the vendor archive it is
    not: coverage is 46% and the symbols share no grid, so "when does anything
    print next" is genuinely unknowable at T's close, and a strategy could
    store it and condition the following instant's decision on it. Intent
    formed at T is stamped T, which is also what `on_bar` does.
    """

    class RejectedRanker(Strategy):
        name = "RejectedRanker"

        def __init__(self) -> None:
            self.pairs: list[tuple[datetime, datetime]] = []
            self.called_for: datetime | None = None

        def on_bar(self, b: Bar, ctx: StrategyContext) -> None:
            pass

        def on_cross_section(self, timestamp: datetime, ctx: StrategyContext) -> None:
            self.called_for = timestamp
            ctx.buy(LATE, Decimal("100000000"))  # certain to breach the notional cap

        def on_reject(self, rejection, ctx: StrategyContext) -> None:  # type: ignore[no-untyped-def]
            assert self.called_for is not None
            self.pairs.append((self.called_for, rejection.timestamp))

    strategy = RejectedRanker()
    Backtester(
        instruments={EARLY: crypto_spot(EARLY), LATE: crypto_spot(LATE)},
        starting_cash=Decimal("100000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits(max_order_notional=Decimal("1000"))),
    ).run(strategy, InMemoryBarFeed(paired_feed(5)))

    assert strategy.pairs, "no rejection reached the hook, so nothing was tested"
    for called_for, stamped in strategy.pairs:
        assert stamped == called_for, (
            f"hook called for {called_for} was shown a rejection stamped {stamped} — "
            "that is the next instant, which it cannot know yet"
        )
