"""Risk limits under composition: many orders, many symbols, many limits.

`risk-auditor` found seven gaps, two critical, and every one of them survived a
green test suite. The reason is visible in `tests/test_risk.py`: every test
there exercises **one limit against one order in isolation**. Not one tests two
orders in the same bar, two limits interacting, the halted path against any
limit other than the halt, or an equity level other than the one the limit was
written for.

That test shape is the actual defect. This file is the fix.

Each test below names the gap it guards (R1-R7) and states the failure it
reproduced before the change, so a future reader can tell a regression from a
deliberate redesign.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.core.events import Bar, Order
from trading_platform.core.instruments import crypto_spot
from trading_platform.core.types import Side
from trading_platform.risk.limits import RiskLimits, RiskManager

TS = datetime(2026, 1, 1, tzinfo=UTC)
BTC = crypto_spot("BTCUSDT")
ETH = crypto_spot("ETHUSDT")


def submit(
    rm: RiskManager,
    qty: str,
    *,
    side: Side = Side.BUY,
    symbol: str = "BTCUSDT",
    price: str = "100",
    current: str = "0",
    equity: str = "10000",
    gross: str = "0",
    timestamp: datetime = TS,
) -> bool:
    return rm.check(
        order=Order(order_id="o", symbol=symbol, side=side, quantity=Decimal(qty)),
        instrument=BTC if symbol == "BTCUSDT" else ETH,
        reference_price=Decimal(price),
        current_quantity=Decimal(current),
        equity=Decimal(equity),
        gross_exposure=Decimal(gross),
        timestamp=timestamp,
    )


# -- R1: the halt is the most restrictive state, not the least ---------------


def test_a_halted_account_cannot_be_walked_short_by_slicing_sells() -> None:
    """The critical one, and it was my own bug.

    Fills land next bar, so every order in a bar was measured against the same
    unchanged position — which made each slice individually look
    "risk-reducing". A halted account long 100 finished **short 50** through
    sells each labelled as flattening. The halt is meant to be the most
    restrictive state in the system; that made it the least.
    """
    rm = RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.10")))
    rm.observe_equity(Decimal("10000"))
    rm.observe_equity(Decimal("8000"))
    assert rm.halted

    accepted = [submit(rm, "40", side=Side.SELL, current="100", price="1") for _ in range(5)]
    net_sold = sum(Decimal("40") for ok in accepted if ok)

    assert net_sold <= Decimal("100"), (
        f"halted account approved sells totalling {net_sold} against a long of 100 — "
        "it would end up short"
    )
    assert accepted[0] is True, "flattening must remain possible"
    assert not all(accepted), "but not without limit"


def test_a_halted_account_is_still_bound_by_every_other_limit() -> None:
    """The halt used to `return True` for reducing orders, skipping order
    notional, position notional, leverage and the rate counter entirely."""
    rm = RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.10"), max_orders_per_bar=3))
    rm.observe_equity(Decimal("10000"))
    rm.observe_equity(Decimal("8000"))
    assert rm.halted

    results = [submit(rm, "1", side=Side.SELL, current="100", price="1") for _ in range(6)]
    assert results[:3] == [True, True, True]
    assert any(r is False for r in results[3:]), "the rate limit must still apply"
    assert any(r.reason == "order_rate" for r in rm.rejections)


def test_flattening_is_still_permitted_while_halted() -> None:
    """The property the halt exemption exists for, and it must survive the fix:
    an earlier version blocked every order and trapped the account in the
    position that caused the halt, inflating one run's ending equity from
    72,969 to 111,610 because the position could not be sold."""
    rm = RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.10")))
    rm.observe_equity(Decimal("10000"))
    rm.observe_equity(Decimal("8000"))
    assert submit(rm, "50", side=Side.SELL, current="100", price="1") is True


def test_a_flip_through_zero_is_not_a_reduction() -> None:
    rm = RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.10")))
    rm.observe_equity(Decimal("10000"))
    rm.observe_equity(Decimal("8000"))
    assert submit(rm, "250", side=Side.SELL, current="100", price="1") is False


# -- R2: caps cannot be defeated by slicing ----------------------------------


def test_splitting_one_order_into_several_cannot_exceed_the_position_cap() -> None:
    """Five 1,000-notional orders reached 5,000 against a 1,500 cap with **zero
    rejections logged**, because each was checked against the same stale
    position. With shipped defaults the reachable position was 1,250,000
    notional — 5x the stated cap. No malice needed: any strategy that legs into
    a position hits it by accident."""
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional=Decimal("1000"),
            max_position_notional=Decimal("1500"),
            max_order_notional_pct=None,
            max_position_notional_pct=None,
            max_gross_leverage=None,
        )
    )
    accepted = sum(1 for _ in range(5) if submit(rm, "10", price="100"))
    assert accepted * 1000 <= 1500, f"{accepted} orders x 1000 breached a 1500 cap"
    assert any(r.reason == "position_notional" for r in rm.rejections)


def test_a_large_batch_cannot_collectively_breach_leverage() -> None:
    """`gross` is computed once before the order loop, so at scale every order
    in a 5,000-symbol rebalance could individually pass a 2x check while the
    batch collectively breached it without bound."""
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional=None,
            max_position_notional=None,
            max_order_notional_pct=None,
            max_position_notional_pct=None,
            max_gross_leverage=Decimal("2"),
        )
    )
    equity = Decimal("1000")
    approved = sum(1 for _ in range(20) if submit(rm, "5", price="100", equity=str(equity)))

    assert approved * 500 <= 2 * equity, (
        f"{approved} orders x 500 notional = {approved * 500} against a 2x cap on equity {equity}"
    )
    assert any(r.reason == "gross_leverage" for r in rm.rejections)


def test_pending_state_clears_per_batch_and_per_timestamp() -> None:
    """Pending approvals are scoped to one call of `_queue_orders`.

    The engine snapshots still-working orders once per batch, and an instant
    runs one batch per bar event — plus a separate one for `on_cross_section`.
    Clearing only when the timestamp advanced left an order approved in an
    earlier batch of the same instant counted twice, once in the snapshot and
    once in `_pending_delta`: two orders of 500 against a 1,000 cap were
    refused as "1500 > 1000".

    A new timestamp still clears as well, since it is necessarily a new batch.
    `_orders_this_bar` remains per timestamp and is not affected.
    """
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional=Decimal("1000"),
            max_position_notional=Decimal("1500"),
            max_order_notional_pct=None,
            max_position_notional_pct=None,
            max_gross_leverage=None,
        )
    )
    assert submit(rm, "10") is True
    assert submit(rm, "10") is False, "second order breaches within the bar"

    rm.start_batch()
    assert submit(rm, "10") is True, "a new batch clears the approvals"

    assert submit(rm, "10") is False, "and accumulates again within it"
    rm.start_bar(TS + timedelta(minutes=1))
    assert submit(rm, "10") is True, "a new timestamp is necessarily a new batch"


# -- R3: limits are a risk appetite, not a currency amount -------------------


def test_the_order_cap_scales_with_equity() -> None:
    """The absolute cap froze at the starting cash and became an equity gate:
    on a real run it admitted an entry only while the account was underwater
    and refused all 346 attempts once it was ahead. The reported +62.5% was the
    return of a strategy permitted to trade only while losing."""
    rm = RiskManager(limits=RiskLimits(max_order_notional_pct=Decimal("0.95")))

    # 95% of equity is permitted at any scale, and 105% never is.
    for equity in ("10000", "100000", "1000000"):
        rm.start_bar(TS + timedelta(seconds=int(equity[:3])))
        rm.start_batch()
        allowed = Decimal(equity) * Decimal("0.9") / Decimal("100")
        assert submit(rm, str(allowed), equity=equity) is True, f"90% of {equity}"

    rm.start_bar(TS + timedelta(hours=1))
    rm.start_batch()
    assert submit(rm, "1050", equity="100000") is False, "105% of equity"


def test_the_tighter_of_the_two_bounds_applies() -> None:
    """Adding the percentage form must never permit more than the absolute
    form did alone."""
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional=Decimal("500"),
            max_order_notional_pct=Decimal("1.0"),
            max_position_notional_pct=None,
            max_gross_leverage=None,
        )
    )
    # 1.0 x 10,000 equity would allow 10,000; the absolute 500 binds.
    assert submit(rm, "6", price="100", equity="10000") is False
    assert rm.rejections[-1].reason == "order_notional"
    assert "500" in rm.rejections[-1].detail


def test_sizing_uses_current_equity_not_the_peak() -> None:
    """Sizing off a stale high-water mark would let a drawn-down account keep
    trading at its old size — a genuine loosening, not a change of units."""
    rm = RiskManager(limits=RiskLimits(max_order_notional_pct=Decimal("1.0")))
    rm.observe_equity(Decimal("100000"))
    assert rm.peak_equity == Decimal("100000")
    assert submit(rm, "500", price="100", equity="20000") is False, (
        "50,000 notional against 20,000 of current equity"
    )


# -- R4: insolvency is not a hole ---------------------------------------------


@pytest.mark.parametrize("equity", ["0", "-5000"])
def test_a_wiped_out_account_cannot_open_new_risk(equity: str) -> None:
    """The `equity > 0` guard avoided a division by zero and converted it into
    a policy hole in the one state where leverage matters most."""
    rm = RiskManager(limits=RiskLimits())
    assert submit(rm, "1", price="100", equity=equity) is False
    assert rm.rejections[-1].reason == "insolvent"


def test_a_wiped_out_account_may_still_reduce() -> None:
    rm = RiskManager(limits=RiskLimits())
    assert submit(rm, "5", side=Side.SELL, current="10", price="100", equity="0") is True


# -- R5: the rate limit counts attempts, not successes ------------------------


def test_rejected_orders_count_against_the_rate_limit() -> None:
    """The counter incremented only after every check passed, so a loop
    emitting invalid orders was never rate-limited — which is why a real run
    logged 346 rejections instead of tripping a breaker on the first bar."""
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional=Decimal("10"),
            max_order_notional_pct=None,
            max_position_notional_pct=None,
            max_gross_leverage=None,
            max_orders_per_bar=3,
        )
    )
    # Every one of these breaches the notional cap.
    for _ in range(10):
        assert submit(rm, "1000", price="100") is False

    reasons = [r.reason for r in rm.rejections]
    assert "order_rate" in reasons, "a runaway loop of invalid orders must be stopped"
    assert reasons.count("order_notional") <= 3


# -- R6: per timestamp, not per bar event -------------------------------------


def test_the_rate_limit_is_per_timestamp_across_a_multi_symbol_feed() -> None:
    """The engine calls `start_bar` once per bar EVENT. With N symbols at one
    instant the counter reset N times, so a limit of 10 became 10 x N — a
    50-symbol universe turned a limit of 10 into 500."""
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional=None,
            max_position_notional=None,
            max_order_notional_pct=None,
            max_position_notional_pct=None,
            max_gross_leverage=None,
            max_orders_per_bar=2,
        )
    )
    accepted = 0
    for _ in range(6):
        # Two symbols arriving at the SAME instant, as a merged feed emits them.
        rm.start_bar(TS)
        if submit(rm, "1", symbol="BTCUSDT"):
            accepted += 1
    assert accepted == 2, f"{accepted} orders through a limit of 2 at one timestamp"


def test_counters_do_reset_when_the_instant_advances() -> None:
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional=None,
            max_position_notional=None,
            max_order_notional_pct=None,
            max_position_notional_pct=None,
            max_gross_leverage=None,
            max_orders_per_bar=2,
        )
    )
    for minute in range(3):
        rm.start_bar(TS + timedelta(minutes=minute))
        rm.start_batch()
        assert submit(rm, "1") is True
        assert submit(rm, "1") is True
        assert submit(rm, "1") is False


def test_the_rate_limit_is_still_per_symbol() -> None:
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional=None,
            max_position_notional=None,
            max_order_notional_pct=None,
            max_position_notional_pct=None,
            max_gross_leverage=None,
            max_orders_per_bar=1,
        )
    )
    rm.start_bar(TS)
    rm.start_batch()
    assert submit(rm, "1", symbol="BTCUSDT") is True
    assert submit(rm, "1", symbol="ETHUSDT") is True, "a different symbol has its own budget"
    assert submit(rm, "1", symbol="BTCUSDT") is False


# -- Limits in combination ----------------------------------------------------


def test_the_tightest_limit_wins_and_says_which_it_was() -> None:
    """Passing one check must never implicitly bypass another, and the
    rejection has to name the binding constraint or it is not diagnosable."""
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional_pct=Decimal("0.5"),
            max_position_notional_pct=Decimal("0.6"),
            max_gross_leverage=Decimal("1"),
        )
    )
    assert submit(rm, "60", price="100", equity="10000") is False
    assert rm.rejections[-1].reason == "order_notional", "the 50% order cap is tightest"


def test_every_rejection_names_a_symbol_a_reason_and_a_number() -> None:
    """A rejection log that cannot be aggregated is a log nobody reads."""
    rm = RiskManager(limits=RiskLimits(max_order_notional_pct=Decimal("0.1")))
    submit(rm, "1000", price="100", equity="10000")

    (rejection,) = rm.rejections
    assert rejection.symbol == "BTCUSDT"
    assert rejection.reason == "order_notional"
    assert any(ch.isdigit() for ch in rejection.detail)
    assert rm.rejection_summary == {"order_notional": 1}


def test_a_drawdown_can_never_trap_a_position_it_cannot_close() -> None:
    """Found by these tests, in the fix for R3/R4 rather than in the original.

    Equity-relative caps shrink with equity. A position built at 100,000 of
    equity can exceed the cap it is measured against after a drawdown to
    20,000, at which point every closing order is refused and the account is
    trapped in exactly the position that hurt it — the same bug the halt
    exemption was written to prevent, arriving through the new units.
    """
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional_pct=Decimal("1.0"),
            max_position_notional_pct=Decimal("1.5"),
        )
    )
    # Long 1,000 units at 100 = 100,000 notional, built when equity supported it.
    # Equity has since collapsed to 20,000, so every cap is now 5x too small.
    assert submit(rm, "1000", side=Side.SELL, current="1000", price="100", equity="20000") is True
    rm.start_bar(TS + timedelta(minutes=1))
    rm.start_batch()
    assert submit(rm, "100", side=Side.SELL, current="1000", price="100", equity="20000") is True


def test_but_a_reducing_order_still_cannot_open_the_other_side() -> None:
    """The exemption is for shrinking the book, not for trading through it."""
    rm = RiskManager(limits=RiskLimits())
    assert submit(rm, "3000", side=Side.SELL, current="1000", price="100", equity="20000") is False


# -- 9b: a flip to a SMALLER opposite side is still a flip ---------------------


def test_a_halted_account_cannot_flip_to_a_smaller_short_in_one_order() -> None:
    """The 9b reproduction, verbatim: halted, long 100, position cap 1,500,
    one sell of 130 at 100. Before the fix this was accepted — 13,000 of
    notional, landing short 30, zero rejections — because |-30| < |100|.
    Reverting `_reduces_exposure` to that test alone turns this red."""
    rm = RiskManager(
        limits=RiskLimits(max_drawdown_pct=Decimal("0.10"), max_position_notional=Decimal("1500"))
    )
    rm.observe_equity(Decimal("10000"))
    rm.observe_equity(Decimal("8000"))
    assert rm.halted is True
    assert submit(rm, "130", side=Side.SELL, current="100", price="100", equity="8000") is False
    assert rm.rejections[-1].reason == "halted"


def test_a_flip_to_a_smaller_opposite_side_is_measured_by_every_size_cap() -> None:
    """Not halted. Long 100 at 100 with the position cap at 1,500: the 130-unit
    sell used to take the reducing exemption and skip the caps entirely. It
    is now measured like any other risk-taking order, and 13,000 of order
    notional fails the order cap.

    **The flatten-then-enter route is NOT open in the same batch, as of the
    2026-09-07 merge**, and this expectation is changed rather than the bound
    relaxed. The merged model takes the worst subset of unfilled orders, so the
    entry behind a flatten is measured as though the flatten might not fill —
    true of a limit order, and the risk layer cannot tell one from a market
    order. Committing the flatten instead was tried and measured: it took the
    working-order breach from none back to 2x the position cap. A limit that is
    too tight refuses a trade; one that is too loose lets a halted account open
    fresh risk, so the tight one stands and the cost is filed as T-51cca14b.

    Mutation: drop the sign check from `_reduces_exposure_between` — the first
    assertion goes green, which is defect 9b returning."""
    rm = RiskManager(
        limits=RiskLimits(max_order_notional=Decimal("5000"), max_position_notional=Decimal("1500"))
    )
    assert submit(rm, "130", side=Side.SELL, current="100", price="100") is False
    assert rm.rejections[-1].reason == "order_notional"
    rm.start_bar(TS + timedelta(minutes=1))
    assert submit(rm, "100", side=Side.SELL, current="100", price="100") is True  # flatten
    # Refused: the worst subset still carries the un-flattened 100.
    assert submit(rm, "12", side=Side.SELL, current="100", price="100") is False
    assert rm.rejections[-1].reason == "position_notional"
    # The route reopens once the flatten has actually filled, which is the next
    # instant under the one-bar fill rule.
    rm.start_bar(TS + timedelta(minutes=2))
    assert submit(rm, "12", side=Side.SELL, current="0", price="100") is True  # 1,200, clear


def test_a_flip_landing_exactly_flat_is_a_reduction() -> None:
    """The boundary the sign test must not break: selling the whole position
    projects to exactly zero, which has no sign and is the largest permitted
    reduction. Writing the check as `projected > 0 != current > 0` without
    the zero exemption would refuse every full close while halted."""
    rm = RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.10")))
    rm.observe_equity(Decimal("10000"))
    rm.observe_equity(Decimal("8000"))
    assert submit(rm, "100", side=Side.SELL, current="100", price="100", equity="8000") is True
    assert submit(rm, "100", side=Side.BUY, current="-100", price="100", equity="8000") is True


def test_the_flip_refusal_is_symmetric_for_a_short_book() -> None:
    rm = RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.10")))
    rm.observe_equity(Decimal("10000"))
    rm.observe_equity(Decimal("8000"))
    assert submit(rm, "130", side=Side.BUY, current="-100", price="100", equity="8000") is False
    assert rm.rejections[-1].reason == "halted"


def test_reducing_orders_are_still_rate_limited() -> None:
    """Exempt from size caps, not from the runaway-loop guard."""
    rm = RiskManager(limits=RiskLimits(max_orders_per_bar=2))
    results = [
        submit(rm, "10", side=Side.SELL, current="1000", price="100", equity="20000")
        for _ in range(5)
    ]
    assert results[:2] == [True, True]
    assert results[2:] == [False, False, False]


def test_a_flatten_releases_gross_exposure_for_the_entry_behind_it() -> None:
    """Long 150 at 100 on 10,000 (1.5x), gross cap 2x. Flatten, then short 150.
    The book never exceeds 1.5x, so the entry must pass. Before the reducing
    branch credited `_pending_gross`, the entry was measured against the
    15,000 the flatten had already released and refused at a fictional 3x
    (risk-auditor on the 9b fix, 2026-09-02)."""
    rm = RiskManager(
        limits=RiskLimits(
            max_order_notional_pct=None,
            max_position_notional_pct=None,
            max_gross_leverage=Decimal("2"),
        )
    )
    assert submit(rm, "150", side=Side.SELL, current="150", gross="15000") is True
    assert submit(rm, "150", side=Side.SELL, current="150", gross="15000") is True
    assert rm.rejections == []


def test_orders_still_working_across_an_instant_cannot_double_the_position_cap() -> None:
    """The cap is checked against filled quantity, so an order that has not
    filled yet is invisible to the next one.

    `_pending_delta` exists for exactly this and is cleared when the timestamp
    advances — which assumes an order submitted at T has filled by T+1. That
    holds only while the symbol prints every instant. Give it a symbol that
    goes quiet and two orders each sized to the cap both pass, both fill later,
    and the position lands at twice the limit with **zero rejections logged**.

    Not hypothetical: the vendor archive is 46% covered and its symbols share
    no timestamp grid, so a symbol going quiet between two decisions is the
    normal case there.

    This became more serious after derivatives stopped moving cash by their
    notional — before that, cash accidentally bounded position size, and now
    the risk layer is the only thing that does.

    And it is not bounded at 2x. Measured before the fix, the breach scales
    with however many instants the symbol stays quiet: 2 orders reached 2x,
    5 reached 5x, 10 reached 10x, every one of them with zero rejections.
    """
    from trading_platform.data.feed import InMemoryBarFeed
    from trading_platform.engine.backtester import Backtester
    from trading_platform.engine.execution import ZERO_COST
    from trading_platform.strategy.base import Strategy, StrategyContext

    cap = Decimal("1000")
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def bar_at(symbol: str, day: int) -> Bar:
        price = Decimal("100")
        return Bar(
            symbol=symbol,
            timestamp=start + timedelta(days=day),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal("100"),
        )

    class TwoOrdersAcrossAGap(Strategy):
        name = "TwoOrdersAcrossAGap"

        def __init__(self) -> None:
            self.seen = 0

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            # Driven by TICK, which prints every instant; orders GAPPY, which
            # does not — so the first order is still working when the second
            # is checked.
            if bar.symbol != "TICK":
                return
            self.seen += 1
            if self.seen in (2, 3):
                ctx.buy("GAPPY", Decimal("10"))  # 10 x 100 = exactly the cap

    bars = [bar_at("TICK", d) for d in range(8)]
    bars += [bar_at("GAPPY", d) for d in (0, 6)]
    bars.sort(key=lambda b: (b.timestamp, b.symbol))

    result = Backtester(
        instruments={"TICK": crypto_spot("TICK"), "GAPPY": crypto_spot("GAPPY")},
        starting_cash=Decimal("100000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits(max_position_notional=cap)),
    ).run(TwoOrdersAcrossAGap(), InMemoryBarFeed(bars))

    filled = sum(
        f.quantity if f.side is Side.BUY else -f.quantity
        for f in result.fills
        if f.symbol == "GAPPY"
    )
    assert filled * Decimal("100") <= cap, (
        f"position notional {filled * 100} exceeded the {cap} cap with "
        f"{len(result.rejections)} rejection(s) logged"
    )


@pytest.mark.parametrize("orders", [2, 5, 10])
def test_the_breach_does_not_scale_with_the_length_of_the_gap(orders: int) -> None:
    """It was N times the cap, not twice it.

    Each order was checked against a filled position that none of its
    predecessors had reached yet, so every one of them passed. The number of
    orders that fit is just the number of instants the symbol stays quiet.
    """
    from trading_platform.data.feed import InMemoryBarFeed
    from trading_platform.engine.backtester import Backtester
    from trading_platform.engine.execution import ZERO_COST
    from trading_platform.strategy.base import Strategy, StrategyContext

    cap = Decimal("1000")
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def bar_at(symbol: str, day: int) -> Bar:
        price = Decimal("100")
        return Bar(
            symbol=symbol,
            timestamp=start + timedelta(days=day),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal("100"),
        )

    class ManyOrders(Strategy):
        name = "ManyOrders"

        def __init__(self, count: int) -> None:
            self.count = count
            self.seen = 0

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            if bar.symbol != "TICK":
                return
            self.seen += 1
            if 2 <= self.seen < 2 + self.count:
                ctx.buy("GAPPY", Decimal("10"))

    bars = [bar_at("TICK", d) for d in range(20)] + [bar_at("GAPPY", d) for d in (0, 18)]
    bars.sort(key=lambda b: (b.timestamp, b.symbol))

    result = Backtester(
        instruments={"TICK": crypto_spot("TICK"), "GAPPY": crypto_spot("GAPPY")},
        starting_cash=Decimal("100000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits(max_position_notional=cap)),
    ).run(ManyOrders(orders), InMemoryBarFeed(bars))

    filled = sum(
        f.quantity if f.side is Side.BUY else -f.quantity
        for f in result.fills
        if f.symbol == "GAPPY"
    )
    assert filled * Decimal("100") == cap
    assert len(result.rejections) == orders - 1, "every order past the first must be refused"


def test_gross_leverage_has_the_same_hole_closed() -> None:
    """A separate limit with the identical defect.

    `_gross_exposure` sums filled positions, so working orders were missing
    from the leverage projection exactly as they were from the position cap.
    Measured before the fix: 10 orders reached 10x a 0.02x cap, zero
    rejections. Tested separately because closing one did not have to close
    the other, and a limit nobody checked is how the first one survived.
    """
    from trading_platform.data.feed import InMemoryBarFeed
    from trading_platform.engine.backtester import Backtester
    from trading_platform.engine.execution import ZERO_COST
    from trading_platform.strategy.base import Strategy, StrategyContext

    start = datetime(2026, 1, 1, tzinfo=UTC)

    def bar_at(symbol: str, day: int) -> Bar:
        price = Decimal("100")
        return Bar(
            symbol=symbol,
            timestamp=start + timedelta(days=day),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal("100"),
        )

    class ManyOrders(Strategy):
        name = "ManyOrders"

        def __init__(self) -> None:
            self.seen = 0

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            if bar.symbol != "TICK":
                return
            self.seen += 1
            if 2 <= self.seen < 12:
                ctx.buy("GAPPY", Decimal("10"))

    bars = [bar_at("TICK", d) for d in range(20)] + [bar_at("GAPPY", d) for d in (0, 18)]
    bars.sort(key=lambda b: (b.timestamp, b.symbol))

    # Only the leverage cap is live: 0.02x of 100,000 equity is 2,000.
    result = Backtester(
        instruments={"TICK": crypto_spot("TICK"), "GAPPY": crypto_spot("GAPPY")},
        starting_cash=Decimal("100000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(
            limits=RiskLimits(
                max_position_notional=None,
                max_position_notional_pct=None,
                max_order_notional=None,
                max_order_notional_pct=None,
                max_gross_leverage=Decimal("0.02"),
            )
        ),
    ).run(ManyOrders(), InMemoryBarFeed(bars))

    filled = sum(
        f.quantity if f.side is Side.BUY else -f.quantity
        for f in result.fills
        if f.symbol == "GAPPY"
    )
    assert filled * Decimal("100") <= Decimal("2000")
    assert result.rejections, "the leverage cap must actually refuse something"


# -- Working orders must not be netted into a credit ---------------------------


def _park_and_trade(limits: RiskLimits, cycles: int = 10) -> object:
    """Park an unmarketable limit BUY, then market SELL, repeatedly.

    The limit order never fills and nothing cancels it, so it works forever.
    """
    from trading_platform.core.types import OrderType
    from trading_platform.data.feed import InMemoryBarFeed
    from trading_platform.engine.backtester import Backtester
    from trading_platform.engine.execution import ZERO_COST
    from trading_platform.strategy.base import Strategy, StrategyContext

    start = datetime(2026, 1, 1, tzinfo=UTC)

    def bar_at(day: int) -> Bar:
        price = Decimal("100")
        return Bar(
            symbol="AAA",
            timestamp=start + timedelta(days=day),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal("100"),
        )

    class ParkAndSell(Strategy):
        name = "ParkAndSell"

        def __init__(self) -> None:
            self.seen = 0

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            self.seen += 1
            if self.seen > cycles:
                return
            ctx.submit(
                "AAA",
                Side.BUY,
                Decimal("10"),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("1"),
            )
            ctx.submit("AAA", Side.SELL, Decimal("10"))

    return Backtester(
        instruments={"AAA": crypto_spot("AAA")},
        starting_cash=Decimal("100000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=limits),
    ).run(ParkAndSell(), InMemoryBarFeed([bar_at(d) for d in range(cycles + 4)]))


def test_a_working_order_cannot_offset_a_real_position() -> None:
    """A regression I introduced, found by re-auditing my own fix.

    Counting still-working orders toward the caps closed a hole where they
    were invisible. It opened the mirror one: an order that *opposes* the
    position nets against it, and since nothing cancels a working order —
    `time_in_force` is stored and never enforced — an unmarketable limit buy
    grants permanent credit against the position cap.

    Measured before the repair: **10x the cap with zero rejections.** The cap
    must apply to the worst case an order could produce, not to a net that
    assumes a fill nobody is obliged to give.
    """
    result = _park_and_trade(RiskLimits(max_position_notional=Decimal("1000")))
    net = sum(
        f.quantity if f.side is Side.BUY else -f.quantity
        for f in result.fills  # type: ignore[attr-defined]
    )
    assert abs(net) * Decimal("100") <= Decimal("1000"), (
        f"position reached {abs(net) * 100} against a 1,000 cap with "
        f"{len(result.rejections)} rejection(s)"  # type: ignore[attr-defined]
    )


def test_a_halted_account_cannot_open_new_risk_through_a_working_order() -> None:
    """The trapped-account fix, inverted.

    Once halted, only exposure-reducing orders may pass. A working order that
    nets the position toward flat makes an opening order *look* reducing — so
    the halt, which is meant to be the most restrictive state in the system,
    becomes a route to new exposure.
    """
    result = _park_and_trade(
        RiskLimits(max_position_notional=Decimal("1000"), max_drawdown_pct=Decimal("0.25")),
        cycles=10,
    )
    net = sum(
        f.quantity if f.side is Side.BUY else -f.quantity
        for f in result.fills  # type: ignore[attr-defined]
    )
    assert abs(net) * Decimal("100") <= Decimal("1000")


def test_two_batches_at_one_instant_are_not_double_counted() -> None:
    """The mirror failure: caps too TIGHT.

    `_pending_delta` is cleared per timestamp; the working-order snapshot is
    taken per batch. A multi-symbol instant runs one batch per bar event, so an
    order approved in an earlier batch sits in both — counted twice. Two orders
    of 500 against a 1,000 cap were refused as "1500 > 1000".

    Over-tightening is not the safe direction. It silently removes trades a
    strategy was entitled to make, and every number downstream is then a
    measurement of the risk layer rather than of the strategy.
    """
    from trading_platform.data.feed import InMemoryBarFeed
    from trading_platform.engine.backtester import Backtester
    from trading_platform.engine.execution import ZERO_COST
    from trading_platform.strategy.base import Strategy, StrategyContext

    start = datetime(2026, 1, 1, tzinfo=UTC)

    def bar_at(symbol: str, day: int) -> Bar:
        price = Decimal("100")
        return Bar(
            symbol=symbol,
            timestamp=start + timedelta(days=day),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal("100"),
        )

    class OnePerBatch(Strategy):
        name = "OnePerBatch"

        def __init__(self) -> None:
            self.done = False

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            if self.done:
                return
            ctx.buy("AAA", Decimal("5"))  # 500 notional
            if bar.symbol == "BBB":
                self.done = True

    bars = sorted(
        (bar_at(s, d) for d in range(6) for s in ("AAA", "BBB")),
        key=lambda b: (b.timestamp, b.symbol),
    )
    result = Backtester(
        instruments={"AAA": crypto_spot("AAA"), "BBB": crypto_spot("BBB")},
        starting_cash=Decimal("100000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits(max_position_notional=Decimal("1000"))),
    ).run(OnePerBatch(), InMemoryBarFeed(bars))

    filled = sum(f.quantity for f in result.fills if f.symbol == "AAA" and f.side is Side.BUY)
    assert filled * Decimal("100") == Decimal("1000"), (
        f"only {filled * 100} of an intended 1,000 filled; rejections: "
        f"{[(r.reason, r.detail) for r in result.rejections]}"
    )
