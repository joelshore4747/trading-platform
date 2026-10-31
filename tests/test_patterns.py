"""ICT primitives. The test that matters is the last one.

These concepts are widely taught and thinly evidenced. Mechanising them is what
this platform is for, and the first job is to make sure the mechanisation does
not cheat: two of the six primitives can only be known some bars after the event
they describe, and a strategy that reads the event's own index has given itself
hindsight. `test_no_primitive_can_see_a_bar_that_has_not_printed` walks a series
bar by bar and requires every function to give the same answer from a truncated
series as from the whole one.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from trading_platform.core.events import Bar
from trading_platform.strategy.patterns import (
    BreakKind,
    Direction,
    Killzone,
    active_killzone,
    confirmed_by,
    fair_value_gaps,
    in_killzone,
    liquidity_sweeps,
    market_structure,
    order_blocks,
    swing_points,
    unfilled,
)

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def bar(i: int, o: str, h: str, low: str, c: str) -> Bar:
    return Bar(
        symbol="BTCUSDT",
        timestamp=T0 + timedelta(minutes=i),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal("1"),
    )


def series(spec: list[tuple[str, str, str, str]]) -> list[Bar]:
    return [bar(i, *values) for i, values in enumerate(spec)]


#: A hand-built series with one clean swing high at index 3 and one swing low at 7.
PIVOTS = series(
    [
        ("100", "101", "99", "100"),
        ("100", "102", "100", "101"),
        ("101", "104", "101", "103"),
        ("103", "110", "103", "108"),  # 3: swing high at 110
        ("108", "108", "104", "105"),
        ("105", "106", "102", "103"),
        ("103", "104", "100", "101"),
        ("101", "102", "90", "95"),  # 7: swing low at 90
        ("95", "99", "94", "98"),
        ("98", "101", "97", "100"),
        ("100", "103", "99", "102"),
    ]
)


# -- swings ---------------------------------------------------------------------------------


def test_a_swing_high_is_found_and_carries_its_confirmation_lag() -> None:
    """The pivot printed at bar 3; nobody could know it until bar 5.

    Mutation: set `confirmed_at=index` — the lookahead test below goes red, and
    so does this."""
    highs = [s for s in swing_points(PIVOTS, strength=2) if s.direction is Direction.UP]
    assert [s.index for s in highs] == [3]
    assert highs[0].price == Decimal("110")
    assert highs[0].confirmed_at == 5
    assert highs[0].lag == 2


def test_a_swing_low_is_the_mirror_and_not_an_afterthought() -> None:
    lows = [s for s in swing_points(PIVOTS, strength=2) if s.direction is Direction.DOWN]
    assert [s.index for s in lows] == [7]
    assert lows[0].price == Decimal("90")


def test_a_flat_top_is_not_a_swing() -> None:
    """A tie is genuinely ambiguous — the pivot could be any of the equal bars —
    and picking one would invent structure the market did not offer.

    Mutation: `>=` instead of `>` on either side — the flat top below becomes
    two swings and this goes red."""
    flat = series(
        [
            ("100", "101", "99", "100"),
            ("100", "105", "100", "104"),
            ("104", "105", "103", "104"),  # equal high
            ("104", "105", "103", "104"),  # equal high
            ("104", "104", "100", "101"),
            ("101", "101", "99", "100"),
        ]
    )
    assert [s.index for s in swing_points(flat, strength=2) if s.direction is Direction.UP] == []


def test_strength_widens_the_window_and_the_lag_with_it() -> None:
    assert swing_points(PIVOTS, strength=1)[0].lag == 1
    assert swing_points(PIVOTS, strength=3)[0].lag == 3
    with pytest.raises(ValueError, match="at least 1"):
        swing_points(PIVOTS, strength=0)


def test_confirmed_by_filters_on_the_confirmation_not_the_event() -> None:
    """The whole reason `Swing` carries two indices. Filtering on `index` looks
    identical in a diff and is lookahead.

    Mutation: filter on `s.index <= index` — the swing at bar 3 becomes visible
    at bar 3 and this goes red."""
    swings = swing_points(PIVOTS, strength=2)
    assert confirmed_by(swings, 4) == []
    assert [s.index for s in confirmed_by(swings, 5)] == [3]


# -- structure -------------------------------------------------------------------------------


def test_the_first_break_is_a_bos_and_a_reversal_is_a_choch() -> None:
    """Only the second and later breaks can be a change of character: the first
    has no prevailing direction to contradict.

    Mutation: make every break a CHoCH — red."""
    trending = series(
        [
            ("100", "101", "99", "100"),
            ("100", "102", "100", "101"),
            ("101", "104", "101", "103"),
            ("103", "110", "103", "108"),  # 3: swing high 110, confirmed at 5
            ("107", "107", "104", "105"),
            ("105", "106", "102", "103"),
            ("103", "112", "103", "111"),  # 6: closes 111 above 110 — BOS up
            ("111", "113", "110", "112"),
            ("112", "114", "111", "113"),
            ("113", "114", "108", "109"),  # 9: swing low 108, confirmed at 11
            ("109", "112", "109", "111"),
            ("111", "113", "110", "112"),
            ("112", "113", "105", "106"),  # 12: closes 106 below 108 — CHoCH down
        ]
    )
    breaks = market_structure(trending, strength=2)
    assert [(b.index, b.kind, b.direction) for b in breaks] == [
        (6, BreakKind.BOS, Direction.UP),
        (12, BreakKind.CHOCH, Direction.DOWN),
    ]


def test_a_break_needs_a_close_beyond_the_level_not_a_wick() -> None:
    """A wick through a level that closes back inside is a liquidity sweep,
    which the same folklore reads as the OPPOSITE signal. Counting it as a break
    too would make the vocabulary self-contradictory.

    Mutation: compare `bar.high > level` instead of `bar.close > level` — the
    wick-only series below produces a break and this goes red."""
    wick_only = series(
        [
            ("100", "101", "99", "100"),
            ("100", "102", "100", "101"),
            ("101", "104", "101", "103"),
            ("103", "110", "103", "108"),  # swing high 110, confirmed at 5
            ("108", "108", "104", "105"),
            ("105", "106", "102", "103"),
            ("103", "111", "102", "104"),  # wick through 110, closes back at 104
        ]
    )
    ups = [b for b in market_structure(wick_only, strength=2) if b.direction is Direction.UP]
    assert ups == []


# -- imbalance --------------------------------------------------------------------------------


def test_a_three_bar_gap_is_found_and_is_knowable_at_its_third_bar() -> None:
    """No confirmation lag, unlike a swing — which is what makes it the one
    primitive here a strategy can act on the moment it appears."""
    gapped = series(
        [
            ("100", "101", "99", "100"),
            ("100", "108", "100", "107"),
            ("107", "112", "104", "111"),  # low 104 > bar 0 high 101: bullish gap
        ]
    )
    gaps = fair_value_gaps(gapped)
    assert len(gaps) == 1
    found = gaps[0]
    assert found.index == 2
    assert found.direction is Direction.UP
    assert (found.lower, found.upper) == (Decimal("101"), Decimal("104"))
    assert found.size == Decimal("3")
    assert found.midpoint == Decimal("102.5")
    assert found.contains(Decimal("102")) and not found.contains(Decimal("105"))


def test_bars_that_overlap_leave_no_gap() -> None:
    """Mutation: `>=` instead of `>` — a touching bar becomes a zero-width gap,
    the CHECK on positive size would be violated downstream, and this goes red."""
    touching = series(
        [
            ("100", "101", "99", "100"),
            ("100", "108", "100", "107"),
            ("107", "112", "101", "111"),  # low exactly equals bar 0's high
        ]
    )
    assert fair_value_gaps(touching) == []


def test_a_gap_is_filled_only_when_price_reaches_its_far_edge() -> None:
    """A touch of the near edge is not a fill, and treating it as one would
    discard most of them.

    Mutation: compare against `gap.upper` for a bullish gap — the near-touch
    below counts as filled and this goes red."""
    gapped = series(
        [
            ("100", "101", "99", "100"),
            ("100", "108", "100", "107"),
            ("107", "112", "104", "111"),  # bullish gap 101..104
            ("111", "112", "104", "110"),  # touches the near edge only
        ]
    )
    gaps = fair_value_gaps(gapped)
    assert unfilled(gaps, gapped, at=3) == gaps

    filled = [*gapped, bar(4, "110", "111", "100", "102")]  # reaches 100 < 101
    assert unfilled(gaps, filled, at=4) == []


def test_an_order_block_is_the_last_opposing_candle_and_lags_its_gap() -> None:
    """Mutation: use `confirmed_at=index` — the lookahead test goes red, because
    the block is only a block once the displacement has happened."""
    with_block = series(
        [
            ("100", "101", "99", "100"),
            ("101", "101", "97", "98"),  # 1: down close — the block
            ("98", "108", "98", "107"),  # 2: displacement begins
            ("107", "112", "104", "111"),  # 3: gap 101..104 completes
        ]
    )
    blocks = order_blocks(with_block, window=3)
    assert len(blocks) == 1
    block = blocks[0]
    assert block.index == 1
    assert block.confirmed_at == 3
    assert block.direction is Direction.UP
    assert block.contains(Decimal("98"))


def test_a_displacement_with_no_opposing_candle_in_the_window_yields_no_block() -> None:
    """`window` is small on purpose: the idea claims the block CAUSED the move,
    and a candle five bars earlier is a different event that happens to precede
    it. Mutation: widen the window default — red."""
    all_up = series(
        [
            ("100", "103", "100", "102"),
            ("102", "106", "102", "105"),
            ("105", "110", "104", "109"),
        ]
    )
    assert order_blocks(all_up, window=1) == []


# -- liquidity ---------------------------------------------------------------------------------


def test_a_sweep_is_a_wick_through_that_closes_back_inside() -> None:
    """And the direction names the LEVEL taken, not the trade suggested — the
    two are opposite, and `suggests` makes the inversion explicit rather than
    leaving it to a strategy to get backwards.

    Mutation: have `suggests` return `direction` — red."""
    swept = series(
        [
            ("100", "101", "99", "100"),
            ("100", "102", "100", "101"),
            ("101", "104", "101", "103"),
            ("103", "110", "103", "108"),  # swing high 110
            ("108", "108", "104", "105"),
            ("105", "106", "102", "103"),
            ("103", "111", "102", "104"),  # wick to 111, closes 104
        ]
    )
    sweeps = liquidity_sweeps(swept, strength=2)
    assert len(sweeps) == 1
    sweep = sweeps[0]
    assert sweep.index == 6
    assert sweep.direction is Direction.UP
    assert sweep.level == Decimal("110")
    assert sweep.suggests is Direction.DOWN
    assert sweep.penetration == Decimal("1") / Decimal("110")


def test_a_close_beyond_the_level_is_a_break_and_not_a_sweep() -> None:
    """The two must stay mutually exclusive or neither is testable.

    Mutation: drop the `close <= level` condition — the breakout below is
    reported as a sweep and this goes red."""
    broke = series(
        [
            ("100", "101", "99", "100"),
            ("100", "102", "100", "101"),
            ("101", "104", "101", "103"),
            ("103", "110", "103", "108"),
            ("108", "108", "104", "105"),
            ("105", "106", "102", "103"),
            ("103", "115", "102", "114"),  # closes beyond 110
        ]
    )
    assert liquidity_sweeps(broke, strength=2) == []


def test_min_penetration_defaults_to_zero_because_a_threshold_is_a_parameter() -> None:
    """A threshold chosen after seeing the results is how a backtest gets
    fitted. Raising it must be deliberate."""
    swept = series(
        [
            ("100", "101", "99", "100"),
            ("100", "102", "100", "101"),
            ("101", "104", "101", "103"),
            ("103", "110", "103", "108"),
            ("108", "108", "104", "105"),
            ("105", "106", "102", "103"),
            ("103", "111", "102", "104"),
        ]
    )
    assert len(liquidity_sweeps(swept, strength=2)) == 1
    assert liquidity_sweeps(swept, strength=2, min_penetration=Decimal("0.05")) == []
    with pytest.raises(ValueError, match="must not be negative"):
        liquidity_sweeps(swept, min_penetration=Decimal("-1"))


# -- sessions ------------------------------------------------------------------------------------


def test_a_killzone_is_a_local_time_window_and_survives_the_clock_change() -> None:
    """Converting to a fixed UTC offset would be wrong twice a year, and a
    strategy trading an hour late every winter looks like one that does not work.

    Mutation: store the window as a UTC offset — one of the two assertions
    below goes red, because 13:00 UTC is 09:00 in New York in summer and 08:00
    in winter."""
    summer = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)  # 09:00 New York
    winter = datetime(2026, 1, 5, 13, 0, tzinfo=UTC)  # 08:00 New York
    assert in_killzone(summer)
    assert in_killzone(winter)
    assert active_killzone(summer) is not None
    assert active_killzone(summer).name == "new-york-am"  # type: ignore[union-attr]


def test_a_naive_timestamp_is_refused_rather_than_assumed() -> None:
    """There is no sensible default timezone for a question that is entirely
    about what time it is somewhere."""
    with pytest.raises(ValueError, match="naive timestamp"):
        in_killzone(datetime(2026, 7, 1, 13, 0))  # naive on purpose


def test_a_window_that_wraps_midnight_is_refused() -> None:
    with pytest.raises(ValueError, match="not before"):
        Killzone("overnight", time(22, 0), time(2, 0))


def test_a_quiet_hour_is_outside_every_zone() -> None:
    quiet = datetime(2026, 7, 1, 11, 30, tzinfo=ZoneInfo("America/New_York"))
    assert not in_killzone(quiet)
    assert active_killzone(quiet) is None


# -- the one that matters ------------------------------------------------------------------------


def test_no_primitive_can_see_a_bar_that_has_not_printed() -> None:
    """Walk the series bar by bar. At every step, what each function reports from
    the bars that have printed must match what it reports from the whole series,
    filtered to what was knowable then.

    This is the check the whole package is shaped around. Two of the six
    primitives carry a confirmation lag, and reading the event's own index
    instead of its confirmation is lookahead that looks identical in a diff.

    Mutation: in `swing_points`, emit a swing at `confirmed_at=index`; or in
    `liquidity_sweeps`, drop the `confirmed_by` filter so an unconfirmed level
    can be swept. Either goes red here, and both are the mistakes a strategy
    built on this vocabulary actually makes."""
    full = PIVOTS
    for n in range(len(full)):
        prefix = full[: n + 1]

        live_swings = swing_points(prefix, strength=2)
        expected_swings = confirmed_by(swing_points(full, strength=2), n)
        assert live_swings == expected_swings, f"swings disagree at bar {n}"

        live_gaps = fair_value_gaps(prefix)
        expected_gaps = [g for g in fair_value_gaps(full) if g.index <= n]
        assert live_gaps == expected_gaps, f"gaps disagree at bar {n}"

        live_sweeps = liquidity_sweeps(prefix, strength=2)
        expected_sweeps = [s for s in liquidity_sweeps(full, strength=2) if s.index <= n]
        assert live_sweeps == expected_sweeps, f"sweeps disagree at bar {n}"

        live_breaks = market_structure(prefix, strength=2)
        expected_breaks = [b for b in market_structure(full, strength=2) if b.index <= n]
        assert live_breaks == expected_breaks, f"structure disagrees at bar {n}"

        live_blocks = order_blocks(prefix)
        expected_blocks = [b for b in order_blocks(full) if b.confirmed_at <= n]
        assert live_blocks == expected_blocks, f"order blocks disagree at bar {n}"
