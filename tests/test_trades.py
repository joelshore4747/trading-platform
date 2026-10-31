"""Round-trip pairing.

The load-bearing test here is reconciliation: FIFO pairing and the portfolio's
average-cost accounting must agree exactly on total P&L. They partition the same
units, so any disagreement means the pairing dropped, duplicated, or mis-signed
something — which is otherwise invisible, because a wrong trade distribution
still looks like a trade distribution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.analytics.trades import RoundTrip, TradeStats, pair_fills, summarise_trades
from trading_platform.core.events import Fill
from trading_platform.core.instruments import crypto_perpetual, crypto_spot
from trading_platform.core.types import Side
from trading_platform.engine.portfolio import Portfolio

SYMBOL = "BTCUSDT"
START = datetime(2025, 1, 1, tzinfo=UTC)
SPOT = {SYMBOL: crypto_spot(SYMBOL)}


def fill(
    day: int,
    side: Side,
    quantity: str,
    price: str,
    commission: str = "0",
    symbol: str = SYMBOL,
) -> Fill:
    return Fill(
        order_id=f"o{day}",
        symbol=symbol,
        timestamp=START + timedelta(days=day),
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        commission=Decimal(commission),
    )


BUY, SELL = Side.BUY, Side.SELL


# -- The simple shape --------------------------------------------------------


def test_one_entry_and_one_exit_is_one_trade() -> None:
    trades, still_open = pair_fills([fill(0, BUY, "2", "100"), fill(5, SELL, "2", "110")], SPOT)

    assert still_open == {}
    (trade,) = trades
    assert trade.direction is BUY
    assert trade.quantity == Decimal("2")
    assert trade.gross_pnl == Decimal("20")
    assert trade.net_pnl == Decimal("20")
    assert trade.duration == timedelta(days=5)
    assert trade.is_win


def test_a_short_makes_money_when_the_price_falls() -> None:
    trades, _ = pair_fills([fill(0, SELL, "2", "100"), fill(3, BUY, "2", "90")], SPOT)
    (trade,) = trades
    assert trade.direction is SELL
    assert trade.gross_pnl == Decimal("20")
    assert trade.is_win


def test_commission_is_charged_from_both_ends() -> None:
    trades, _ = pair_fills(
        [fill(0, BUY, "2", "100", commission="3"), fill(1, SELL, "2", "110", commission="4")],
        SPOT,
    )
    (trade,) = trades
    assert trade.gross_pnl == Decimal("20")
    assert trade.commission == Decimal("7")
    assert trade.net_pnl == Decimal("13")


def test_a_gross_win_eaten_by_commission_is_a_loss() -> None:
    """`is_win` is net. Anything else flatters a high-frequency strategy."""
    trades, _ = pair_fills(
        [fill(0, BUY, "1", "100", commission="6"), fill(1, SELL, "1", "105", commission="6")],
        SPOT,
    )
    (trade,) = trades
    assert trade.gross_pnl == Decimal("5")
    assert trade.net_pnl == Decimal("-7")
    assert not trade.is_win


# -- The cases implementations get wrong -------------------------------------


def test_scaling_in_produces_one_trade_per_lot_fifo() -> None:
    trades, still_open = pair_fills(
        [fill(0, BUY, "1", "100"), fill(1, BUY, "1", "120"), fill(5, SELL, "2", "130")],
        SPOT,
    )

    assert still_open == {}
    assert len(trades) == 2
    # Oldest lot closes first, and keeps its own entry price — not a blend.
    assert [t.entry_price for t in trades] == [Decimal("100"), Decimal("120")]
    assert [t.gross_pnl for t in trades] == [Decimal("30"), Decimal("10")]
    assert [t.duration for t in trades] == [timedelta(days=5), timedelta(days=4)]


def test_a_partial_close_splits_a_lot_and_leaves_the_remainder() -> None:
    trades, still_open = pair_fills([fill(0, BUY, "5", "100"), fill(2, SELL, "2", "110")], SPOT)

    (trade,) = trades
    assert trade.quantity == Decimal("2")
    assert trade.gross_pnl == Decimal("20")
    # The remainder stays open at the original entry price, not re-based.
    assert still_open == {SYMBOL: Decimal("3")}


def test_commission_splits_pro_rata_across_the_lots_a_fill_closes() -> None:
    """One exit closing two lots must not dump its whole fee on the first."""
    trades, _ = pair_fills(
        [
            fill(0, BUY, "1", "100"),
            fill(1, BUY, "3", "100"),
            fill(5, SELL, "4", "110", commission="8"),
        ],
        SPOT,
    )
    assert [t.commission for t in trades] == [Decimal("2"), Decimal("6")]
    assert sum(t.commission for t in trades) == Decimal("8")


def test_flipping_through_zero_closes_one_trade_and_opens_one_position() -> None:
    """`engine/portfolio.py` flags this as the case implementations get wrong:
    it must not produce two closed trades, nor one merged trade."""
    trades, still_open = pair_fills([fill(0, BUY, "2", "100"), fill(3, SELL, "5", "110")], SPOT)

    (trade,) = trades
    assert trade.direction is BUY
    assert trade.quantity == Decimal("2")
    assert trade.gross_pnl == Decimal("20")
    # The other 3 units are a new short, not a second closed trade.
    assert still_open == {SYMBOL: Decimal("-3")}


def test_flipping_back_and_forth_pairs_each_leg_separately() -> None:
    trades, still_open = pair_fills(
        [
            fill(0, BUY, "1", "100"),
            fill(1, SELL, "2", "110"),  # close long, open short 1
            fill(2, BUY, "2", "105"),  # close short, open long 1
            fill(3, SELL, "1", "115"),  # close long
        ],
        SPOT,
    )
    assert still_open == {}
    assert [t.direction for t in trades] == [BUY, SELL, BUY]
    assert [t.gross_pnl for t in trades] == [Decimal("10"), Decimal("5"), Decimal("10")]


def test_symbols_are_paired_independently() -> None:
    trades, still_open = pair_fills(
        [
            fill(0, BUY, "1", "100", symbol="BTCUSDT"),
            fill(0, BUY, "1", "50", symbol="ETHUSDT"),
            fill(2, SELL, "1", "110", symbol="BTCUSDT"),
        ],
        {"BTCUSDT": crypto_spot("BTCUSDT"), "ETHUSDT": crypto_spot("ETHUSDT")},
    )
    (trade,) = trades
    assert trade.symbol == "BTCUSDT"
    assert still_open == {"ETHUSDT": Decimal("1")}


def test_a_contract_multiplier_scales_the_pnl() -> None:
    perp = crypto_perpetual(SYMBOL)
    trades, _ = pair_fills([fill(0, BUY, "2", "100"), fill(1, SELL, "2", "110")], {SYMBOL: perp})
    (trade,) = trades
    assert trade.gross_pnl == Decimal("20") * perp.multiplier


def test_without_an_instrument_map_the_multiplier_is_one() -> None:
    trades, _ = pair_fills([fill(0, BUY, "2", "100"), fill(1, SELL, "2", "110")])
    assert trades[0].gross_pnl == Decimal("20")


# -- Reconciliation against the portfolio ------------------------------------

RECONCILIATION_CASES = [
    pytest.param([(0, BUY, "2", "100"), (5, SELL, "2", "110")], id="simple"),
    pytest.param(
        [(0, BUY, "1", "100"), (1, BUY, "3", "120"), (5, SELL, "4", "115")], id="scale-in"
    ),
    pytest.param(
        [(0, BUY, "5", "100"), (2, SELL, "2", "110"), (4, SELL, "3", "90")], id="partial-exits"
    ),
    pytest.param([(0, BUY, "2", "100"), (3, SELL, "5", "110"), (6, BUY, "3", "95")], id="flip"),
    pytest.param(
        [(0, SELL, "4", "100"), (1, BUY, "1", "90"), (2, BUY, "3", "105")], id="short-first"
    ),
]


@pytest.mark.parametrize("steps", RECONCILIATION_CASES)
def test_fifo_and_average_cost_agree_on_total_pnl(
    steps: list[tuple[int, Side, str, str]],
) -> None:
    """Both methods partition the same units, so the totals must match exactly.
    Any disagreement means the pairing lost, duplicated, or mis-signed a unit.
    """
    instrument = SPOT[SYMBOL]
    portfolio = Portfolio(starting_cash=Decimal("100000"))
    fills = [fill(*step) for step in steps]
    for f in fills:
        portfolio.apply_fill(instrument, f)

    trades, still_open = pair_fills(fills, SPOT)
    assert still_open == {}, "these cases all end flat"

    fifo_total = sum((t.gross_pnl for t in trades), Decimal("0"))
    assert fifo_total == portfolio.total_realised_pnl


def test_reconciliation_holds_with_a_position_still_open() -> None:
    """Realised plus unrealised is invariant to the accounting method."""
    instrument = SPOT[SYMBOL]
    last_price = Decimal("130")
    portfolio = Portfolio(starting_cash=Decimal("100000"))
    fills = [
        fill(0, BUY, "1", "100"),
        fill(1, BUY, "3", "120"),
        fill(5, SELL, "2", "115"),
    ]
    for f in fills:
        portfolio.apply_fill(instrument, f)

    trades, still_open = pair_fills(fills, SPOT)
    assert still_open == {SYMBOL: Decimal("2")}

    closed = sum((t.gross_pnl for t in trades), Decimal("0"))
    # Value the open remainder at its own FIFO entry prices: 2 units left, both
    # from the 120 lot.
    open_pnl = (last_price - Decimal("120")) * Decimal("2")
    portfolio_total = portfolio.total_realised_pnl + portfolio.positions[SYMBOL].unrealised_pnl(
        last_price
    )

    assert closed + open_pnl == portfolio_total


# -- The distribution --------------------------------------------------------


def stats_from(pnls: list[str]) -> TradeStats:
    """Build a distribution directly, to test the statistics themselves."""
    trades = tuple(
        RoundTrip(
            symbol=SYMBOL,
            direction=BUY,
            quantity=Decimal("1"),
            entry_time=START + timedelta(days=i),
            exit_time=START + timedelta(days=i + 1),
            entry_price=Decimal("100"),
            exit_price=Decimal("100") + Decimal(p),
            gross_pnl=Decimal(p),
            commission=Decimal("0"),
            net_pnl=Decimal(p),
        )
        for i, p in enumerate(pnls)
    )
    return TradeStats(trades=trades, open_quantity={})


def test_concentration_catches_a_run_that_is_one_trade() -> None:
    """The metric this module exists for. Nine small losses and one big win is
    a lucky trade with a strategy wrapped around it."""
    stats = stats_from(["-5"] * 9 + ["100"])

    assert stats.total_net_pnl == Decimal("55")
    assert stats.pnl_without_best_n(1) == Decimal("-45")
    assert stats.top_trade_share > 1.0
    verdict = stats.concentration_verdict
    assert verdict is not None and "one trade, not a strategy" in verdict


def test_a_broad_distribution_raises_no_concentration_verdict() -> None:
    stats = stats_from(["10", "12", "-8", "9", "-6", "11", "13", "-7", "10", "8", "-5", "9"])
    assert stats.total_net_pnl > 0
    assert stats.pnl_without_best_n(1) > 0
    assert stats.concentration_verdict is None


def test_too_few_trades_is_itself_the_verdict() -> None:
    stats = stats_from(["10", "12", "-8"])
    verdict = stats.concentration_verdict
    assert verdict is not None and "not an estimate" in verdict


def test_no_trades_at_all_says_so() -> None:
    stats = stats_from([])
    assert stats.count == 0
    assert stats.win_rate == 0.0
    assert stats.total_net_pnl == Decimal("0")
    verdict = stats.concentration_verdict
    assert verdict is not None and "Nothing here is a measurement" in verdict


def test_profit_factor_is_withheld_when_nothing_lost() -> None:
    """A run with no losing trade has not been tested; printing infinity would
    imply it passed."""
    assert stats_from(["10", "20", "30"]).profit_factor is None
    assert stats_from(["10", "20", "-15"]).profit_factor == pytest.approx(2.0)


def test_top_trade_share_is_withheld_when_the_run_lost_money() -> None:
    """A ratio against a negative total reads backwards."""
    assert stats_from(["-10", "-20", "5"]).top_trade_share == 0.0


def test_win_rate_is_per_trade_not_per_period() -> None:
    stats = stats_from(["10", "-5", "10", "-5"])
    assert stats.win_rate == 0.5
    assert stats.count == 4


@pytest.mark.parametrize(
    ("pnls", "expected"),
    [(["1", "2", "3"], timedelta(days=1)), ([], timedelta(0))],
)
def test_median_duration_handles_the_empty_case(pnls: list[str], expected: timedelta) -> None:
    assert stats_from(pnls).median_duration == expected


def test_open_positions_are_reported_not_folded_into_the_statistics() -> None:
    """Marking an open position into a realised distribution would be exactly
    the flattery this project bans."""
    stats = summarise_trades([fill(0, BUY, "5", "100"), fill(2, SELL, "2", "110")], SPOT)
    assert stats.count == 1
    assert stats.total_net_pnl == Decimal("20")
    assert stats.open_quantity == {SYMBOL: Decimal("3")}
    assert stats.unclosed_symbols == (SYMBOL,)
    assert "excluded above" in stats.report()


# -- Construction guards -----------------------------------------------------


def test_a_round_trip_cannot_exit_before_it_enters() -> None:
    with pytest.raises(ValueError, match="precedes entry"):
        RoundTrip(
            symbol=SYMBOL,
            direction=BUY,
            quantity=Decimal("1"),
            entry_time=START + timedelta(days=5),
            exit_time=START,
            entry_price=Decimal("100"),
            exit_price=Decimal("110"),
            gross_pnl=Decimal("10"),
            commission=Decimal("0"),
            net_pnl=Decimal("10"),
        )


def test_a_round_trip_needs_positive_quantity() -> None:
    with pytest.raises(ValueError, match="positive quantity"):
        RoundTrip(
            symbol=SYMBOL,
            direction=BUY,
            quantity=Decimal("0"),
            entry_time=START,
            exit_time=START,
            entry_price=Decimal("100"),
            exit_price=Decimal("110"),
            gross_pnl=Decimal("0"),
            commission=Decimal("0"),
            net_pnl=Decimal("0"),
        )


def test_largest_win_and_loss_are_taken_over_their_own_side() -> None:
    """A run with one winning trade must not report that trade as its largest
    loss, and a run of pure losses has no largest win."""
    only_wins = stats_from(["10", "25"])
    assert only_wins.largest_win == Decimal("25")
    assert only_wins.largest_loss == Decimal("0")

    only_losses = stats_from(["-10", "-25"])
    assert only_losses.largest_win == Decimal("0")
    assert only_losses.largest_loss == Decimal("-25")

    both = stats_from(["10", "-25", "40", "-5"])
    assert both.largest_win == Decimal("40")
    assert both.largest_loss == Decimal("-25")
