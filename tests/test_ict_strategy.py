"""The ICT sweep-reversal strategy: does it do what its docstring says.

The primitives are tested in `tests/test_patterns.py`, including the bar-by-bar
lookahead walk. This file tests the composition — the inversion (sweeping a HIGH
is a bearish signal), the killzone filter, the time stop, and the rule that only
a sweep on the CURRENT bar is a signal.

None of this is evidence that the strategy works. It is evidence that a run of
it is a run of the thing described, which is the precondition for the result
meaning anything either way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.core.events import Bar
from trading_platform.core.instruments import crypto_perpetual
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.engine.backtester import Backtester
from trading_platform.engine.execution import ZERO_COST
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.examples.ict_sweep_reversal import ICTSweepReversal

SYMBOL = "BTCUSDT"
#: 14:00 UTC is 10:00 New York in summer — just OUTSIDE the new-york-am zone,
#: so a test can step into and out of a killzone by shifting the start.
IN_ZONE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)  # 08:00 NY — inside
OUT_OF_ZONE = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)  # 12:00 NY — outside


def bars_from(start: datetime, spec: list[tuple[str, str, str, str]]) -> list[Bar]:
    out = []
    for i, (o, h, low, c) in enumerate(spec):
        high = max(Decimal(h), Decimal(o), Decimal(c))
        lo = min(Decimal(low), Decimal(o), Decimal(c))
        out.append(
            Bar(
                symbol=SYMBOL,
                timestamp=start + timedelta(minutes=i),
                open=Decimal(o),
                high=high,
                low=lo,
                close=Decimal(c),
                volume=Decimal("1"),
            )
        )
    return out


#: A swing high of 110 at index 3, confirmed at 5, swept by a wick at index 6
#: that closes back inside. The folklore reads that as bearish.
SWEEP_HIGH = [
    ("100", "101", "99", "100"),
    ("100", "102", "100", "101"),
    ("101", "104", "101", "103"),
    ("103", "110", "103", "108"),
    ("107", "107", "104", "105"),
    ("105", "106", "102", "103"),
    ("103", "111", "102", "104"),  # 6: the sweep
    ("104", "105", "102", "103"),
    ("103", "104", "101", "102"),
]


def run(strategy: ICTSweepReversal, bars: list[Bar]) -> object:
    engine = Backtester(
        instruments={SYMBOL: crypto_perpetual(SYMBOL, initial_margin_rate=None)},
        starting_cash=Decimal("100000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits()),
    )
    return engine.run(strategy, InMemoryBarFeed(bars))


def test_sweeping_a_high_opens_a_short_not_a_long() -> None:
    """The inversion is the whole idea: the buy stops above the level were
    filled and the move failed, so the signal is DOWN.

    Mutation: use `latest.direction` instead of `latest.suggests` — the
    strategy goes long into the fade and this goes red."""
    strategy = ICTSweepReversal(SYMBOL, strength=2, hold=50, allocation=Decimal("0.5"))
    result = run(strategy, bars_from(IN_ZONE, SWEEP_HIGH))
    assert strategy.entries == 1, strategy.report()
    assert result.fills, "the entry never filled"
    assert all(f.side.sign < 0 for f in result.fills[:1]), "the entry must be a SELL"


def test_the_killzone_filter_actually_filters() -> None:
    """Exposed as a flag so the null — the same strategy with the filter off —
    is one argument away. If the filter is the whole result, that is the first
    thing to find out.

    Mutation: drop the `in_killzone` check — the out-of-zone run below enters
    and this goes red."""
    outside = ICTSweepReversal(SYMBOL, strength=2, hold=50)
    run(outside, bars_from(OUT_OF_ZONE, SWEEP_HIGH))
    assert outside.sweeps_seen == 1
    assert outside.sweeps_outside_killzone == 1
    assert outside.entries == 0

    unfiltered = ICTSweepReversal(SYMBOL, strength=2, hold=50, use_killzones=False)
    run(unfiltered, bars_from(OUT_OF_ZONE, SWEEP_HIGH))
    assert unfiltered.entries == 1


def test_only_a_sweep_on_the_current_bar_is_a_signal() -> None:
    """An older sweep has already been acted on or missed. Re-entering on it
    every bar would turn one signal into a hundred and make the trade count
    meaningless.

    Mutation: drop the `latest.index != len(history) - 1` guard — entries climb
    with the number of trailing bars and this goes red."""
    trailing = [*SWEEP_HIGH, *[("102", "103", "101", "102")] * 20]
    strategy = ICTSweepReversal(SYMBOL, strength=2, hold=500)
    run(strategy, bars_from(IN_ZONE, trailing))
    assert strategy.entries == 1, strategy.report()


def test_the_time_stop_flattens_and_is_counted() -> None:
    """A time stop rather than a price stop, because simulating a price stop
    from bar data means deciding whether the high or the low came first inside
    the bar — which the data does not say.

    Mutation: never decrement or check `_held` — the position stays open and
    the final position assertion goes red."""
    trailing = [*SWEEP_HIGH, *[("102", "103", "101", "102")] * 10]
    strategy = ICTSweepReversal(SYMBOL, strength=2, hold=3)
    result = run(strategy, bars_from(IN_ZONE, trailing))
    assert strategy.entries == 1
    assert strategy.time_stops == 1
    # Two fills: the entry, and the time stop closing it.
    net = sum((f.quantity * f.side.sign for f in result.fills), Decimal("0"))
    assert len(result.fills) == 2, [f.side for f in result.fills]
    assert net == 0, "the time stop did not flatten"


def test_a_run_with_no_sweeps_says_so_rather_than_looking_like_a_flat_result() -> None:
    """A strategy that traded nothing and a strategy that traded badly look the
    same in an equity curve. The counters are the difference."""
    quiet = [("100", "101", "99", "100")] * 30
    strategy = ICTSweepReversal(SYMBOL, strength=2)
    run(strategy, bars_from(IN_ZONE, quiet))
    assert strategy.sweeps_seen == 0 and strategy.entries == 0
    assert "0 sweep(s) seen" in strategy.report()


def test_impossible_parameters_are_refused_at_construction() -> None:
    for kwargs, match in (
        ({"strength": 0}, "strength"),
        ({"hold": 0}, "hold"),
        ({"allocation": Decimal("0")}, "allocation"),
        ({"allocation": Decimal("2")}, "allocation"),
        ({"lookback": 5}, "too short"),
    ):
        with pytest.raises(ValueError, match=match):
            ICTSweepReversal(SYMBOL, **kwargs)  # type: ignore[arg-type]
