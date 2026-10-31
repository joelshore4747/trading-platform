"""The cross-sectional API: what a strategy ranking symbols is allowed to see.

The lookahead guards for `on_cross_section` live in `test_lookahead.py`, where
anything touching the event loop belongs. These cover the rest: which symbols
are in the cross-section, what happens to the ones that are not, and whether
the ranker built on top behaves.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.core.events import Bar
from trading_platform.core.instruments import crypto_spot
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.data.universe import Listing, UniverseRegistry
from trading_platform.engine.backtester import Backtester
from trading_platform.engine.execution import ZERO_COST
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.base import Strategy, StrategyContext
from trading_platform.strategy.examples.cross_sectional_momentum import CrossSectionalMomentum

START = datetime(2026, 1, 1, tzinfo=UTC)


def bar_at(symbol: str, day: int, close: str) -> Bar:
    price = Decimal(close)
    return Bar(
        symbol=symbol,
        timestamp=START + timedelta(days=day),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("100"),
    )


class Recorder(Strategy):
    """Records the cross-section at each instant, and nothing else."""

    name = "Recorder"

    def __init__(self, ask_universe: bool = False) -> None:
        self.seen: dict[datetime, tuple[str, ...]] = {}
        self.universes: dict[datetime, tuple[str, ...]] = {}
        self.ask_universe = ask_universe

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
        pass

    def on_cross_section(self, timestamp: datetime, ctx: StrategyContext) -> None:
        self.seen[timestamp] = ctx.cross_section()
        if self.ask_universe:
            self.universes[timestamp] = tuple(ctx.universe())


def run(bars: list[Bar], strategy: Strategy, universe: UniverseRegistry | None = None) -> object:
    symbols = sorted({b.symbol for b in bars})
    engine = Backtester(
        instruments={s: crypto_spot(s) for s in symbols},
        starting_cash=Decimal("100000"),
        cost_model=ZERO_COST,
        risk_manager=RiskManager(limits=RiskLimits()),
        universe=universe,
    )
    return engine.run(strategy, InMemoryBarFeed(bars))


def listing(symbol: str, days: int, listed: date | None = None) -> Listing:
    return Listing(
        market="spot",
        symbol=symbol,
        interval="1d",
        first_seen=START,
        last_seen=START + timedelta(days=days),
        bars=days + 1,
        listed=listed or date(2025, 1, 1),
        delisted=None,
    )


# -- Which symbols are in the cross-section -----------------------------------


def test_the_cross_section_is_the_symbols_that_printed() -> None:
    bars = [bar_at(s, d, "100") for d in range(4) for s in ("AAA", "BBB", "CCC")]
    strategy = Recorder()
    run(bars, strategy)
    assert all(seen == ("AAA", "BBB", "CCC") for seen in strategy.seen.values())


def test_a_symbol_that_did_not_print_is_absent_not_ranked() -> None:
    """The decision `Panel._refuse_if_masked` defers to this API.

    Never worst, never median, never forward-filled — absent. Inventing a rank
    for a symbol that did not trade is the same act as inventing its price.
    """
    bars = [bar_at(s, d, "100") for d in range(4) for s in ("AAA", "BBB", "CCC")]
    # BBB goes dark on day 1 only.
    bars = [b for b in bars if not (b.symbol == "BBB" and b.timestamp == START + timedelta(days=1))]
    strategy = Recorder()
    run(bars, strategy)

    assert strategy.seen[START + timedelta(days=1)] == ("AAA", "CCC")
    assert strategy.seen[START] == ("AAA", "BBB", "CCC")


def test_the_hook_still_fires_when_a_symbol_is_missing() -> None:
    """The case that breaks the obvious workaround.

    Acting on `bar.symbol == symbols[-1]` to get one call per instant silently
    skips every instant where the sentinel symbol has no bar. The vendor
    archive is 46% covered and its symbols do not share a grid, so that is the
    normal case there, not a corner one.
    """
    bars = [bar_at(s, d, "100") for d in range(4) for s in ("AAA", "ZZZ")]
    bars = [b for b in bars if not (b.symbol == "ZZZ" and b.timestamp == START + timedelta(days=1))]
    strategy = Recorder()
    run(bars, strategy)

    # Instants 0, 1, 2 fire; the last never does. Instant 1 has no ZZZ at all.
    assert sorted(strategy.seen) == [START + timedelta(days=d) for d in range(3)]
    assert strategy.seen[START + timedelta(days=1)] == ("AAA",)


def test_the_cross_section_does_not_accumulate_across_instants() -> None:
    """A symbol that printed yesterday and not today is not in today's."""
    bars = [bar_at("AAA", d, "100") for d in range(4)]
    bars += [bar_at("BBB", 0, "100")]
    strategy = Recorder()
    run(bars, strategy)
    assert strategy.seen[START] == ("AAA", "BBB")
    assert strategy.seen[START + timedelta(days=1)] == ("AAA",)


# -- The universe -------------------------------------------------------------


def test_asking_for_the_universe_without_a_registry_raises() -> None:
    """Falling back to the registered instruments would be survivorship bias
    inside the call written to prevent it."""
    bars = [bar_at(s, d, "100") for d in range(3) for s in ("AAA", "BBB")]
    with pytest.raises(RuntimeError, match="survivorship"):
        run(bars, Recorder(ask_universe=True))


def test_the_universe_is_point_in_time() -> None:
    """A symbol listing mid-run is absent before its listing date."""
    bars = [bar_at(s, d, "100") for d in range(6) for s in ("AAA", "BBB")]
    registry = UniverseRegistry(
        listings=(
            listing("AAA", 5, listed=date(2025, 1, 1)),
            # Listed on day 3 of the run.
            listing("BBB", 5, listed=(START + timedelta(days=3)).date()),
        )
    )
    strategy = Recorder(ask_universe=True)
    run(bars, strategy, universe=registry)

    assert strategy.universes[START] == ("AAA",)
    assert strategy.universes[START + timedelta(days=4)] == ("AAA", "BBB")


def test_a_symbol_outside_the_universe_is_excluded_even_though_it_printed() -> None:
    """The archive holding bars for a symbol is not evidence it was tradeable.

    This is the survivorship case with teeth: the data is there, so nothing
    fails, and the strategy quietly ranks an instrument nobody could have
    bought.
    """
    bars = [bar_at(s, d, "100") for d in range(6) for s in ("AAA", "BBB")]
    registry = UniverseRegistry(
        listings=(
            listing("AAA", 5, listed=date(2025, 1, 1)),
            listing("BBB", 5, listed=(START + timedelta(days=3)).date()),
        )
    )
    strategy = Recorder()
    run(bars, strategy, universe=registry)

    assert strategy.seen[START] == ("AAA",)
    assert strategy.seen[START + timedelta(days=4)] == ("AAA", "BBB")


# -- Holdings -----------------------------------------------------------------


def test_holdings_reports_what_is_actually_held() -> None:
    class BuyOne(Strategy):
        name = "BuyOne"

        def __init__(self) -> None:
            self.holdings_at_end: tuple[str, ...] = ()

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            pass

        def on_cross_section(self, timestamp: datetime, ctx: StrategyContext) -> None:
            if ctx.is_flat("AAA"):
                ctx.buy("AAA", Decimal("1"))
            self.holdings_at_end = ctx.holdings()

    bars = [bar_at(s, d, "100") for d in range(5) for s in ("AAA", "BBB")]
    strategy = BuyOne()
    run(bars, strategy)
    assert strategy.holdings_at_end == ("AAA",)


# -- The ranker ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"lookback": 1}, "lookback"),
        ({"top_k": 0}, "top_k"),
        ({"rebalance_every": 0}, "rebalance_every"),
        ({"allocation": Decimal("0")}, "allocation"),
        ({"allocation": Decimal("1.5")}, "allocation"),
    ],
)
def test_the_ranker_refuses_impossible_parameters(kwargs: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        CrossSectionalMomentum(**kwargs)


def test_the_ranker_holds_the_top_k_by_trailing_return() -> None:
    """DDD rises fastest, AAA is flat: with top_k=1 only DDD should be held."""
    bars: list[Bar] = []
    for d in range(12):
        bars.append(bar_at("AAA", d, "100"))
        bars.append(bar_at("BBB", d, str(100 + d)))
        bars.append(bar_at("DDD", d, str(100 + 5 * d)))
    strategy = CrossSectionalMomentum(lookback=3, top_k=1, rebalance_every=3)
    result = run(bars, strategy)

    assert strategy.rebalances > 0
    held = {f.symbol for f in result.fills}  # type: ignore[attr-defined]
    assert held == {"DDD"}


def test_the_ranker_does_not_rank_a_thin_cross_section() -> None:
    """Two names is not a cross-section, and the skip is counted not hidden.

    A ranker that quietly did nothing looks identical in the equity curve to
    one that found nothing worth doing.
    """
    bars = [bar_at(s, d, "100") for d in range(10) for s in ("AAA", "BBB")]
    strategy = CrossSectionalMomentum(lookback=3, top_k=5, rebalance_every=2)
    result = run(bars, strategy)

    assert strategy.rebalances == 0
    assert strategy.skipped_thin > 0
    assert not result.fills  # type: ignore[attr-defined]


def test_the_ranker_tries_to_exit_a_symbol_that_left_the_cross_section() -> None:
    """Otherwise the position is stranded, marked on stale data, for the run.

    Exiting only the symbols it can still *rank* never even submits the order:
    a symbol that stopped printing is absent from the cross-section, so it is
    absent from the ranking too, and the loop that would have closed it never
    sees it. With the vendor archive at 46% coverage a symbol going dark while
    held is routine.

    Whether the exit fills is the market's business — an order for an
    instrument with no further bars cannot fill, and the engine reports it as
    unfilled, which is the visible signal. What the strategy owes is the
    attempt. Measured: the correct version leaves 2 orders unfilled here, the
    rank-only version leaves 0, because it never tried.
    """
    bars: list[Bar] = []
    for d in range(14):
        bars.append(bar_at("AAA", d, str(100 + d)))
        bars.append(bar_at("BBB", d, str(100 + 2 * d)))
        bars.append(bar_at("CCC", d, str(100 + 3 * d)))
        # DDD is the runaway winner, then goes dark from day 8.
        if d < 8:
            bars.append(bar_at("DDD", d, str(100 + 20 * d)))

    strategy = CrossSectionalMomentum(lookback=3, top_k=1, rebalance_every=3)
    result = run(bars, strategy)

    assert any(f.symbol == "DDD" for f in result.fills)  # type: ignore[attr-defined]
    assert result.unfilled_orders > 0, (  # type: ignore[attr-defined]
        "DDD went dark while held and no exit was ever submitted for it"
    )


def test_the_exit_completes_when_the_symbol_prints_again() -> None:
    """The same path, with the order able to fill.

    DDD goes dark while held and returns three days later. The exit submitted
    while it was absent must fill on its next bar — proving the previous test
    is measuring a real attempt rather than an order the strategy never
    intended to complete.
    """
    bars: list[Bar] = []
    for d in range(16):
        bars.append(bar_at("AAA", d, str(100 + d)))
        bars.append(bar_at("BBB", d, str(100 + 2 * d)))
        bars.append(bar_at("CCC", d, str(100 + 3 * d)))
        if d < 8 or d >= 11:
            bars.append(bar_at("DDD", d, str(100 + 20 * d)))

    strategy = CrossSectionalMomentum(lookback=3, top_k=1, rebalance_every=3)
    result = run(bars, strategy)

    exits = [
        f
        for f in result.fills  # type: ignore[attr-defined]
        if f.symbol == "DDD" and f.tag == "rank_exit"
    ]
    assert exits, "the exit for DDD never filled even though it printed again"


def test_a_hook_assigned_on_the_instance_still_fires() -> None:
    """Otherwise it silently never fires and the run looks like a quiet market.

    The engine decides whether to run the hook by comparing
    `type(strategy).on_cross_section` against the base method, which by design
    skips the instance `__dict__`. A strategy that assigns the hook per
    instance — a wrapper, a patch, a test double — is then never called, no
    error is raised, and the run finishes with zero fills. In the fill record
    that is indistinguishable from a ranker that found nothing worth trading,
    which is the exact failure `skipped_thin` exists to prevent, arriving
    through a different door.
    """
    calls: list[datetime] = []

    class Blank(Strategy):
        name = "Blank"

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            pass

    strategy = Blank()
    strategy.on_cross_section = lambda timestamp, ctx: calls.append(timestamp)  # type: ignore[method-assign]

    bars = [bar_at(s, d, "100") for d in range(4) for s in ("AAA", "BBB")]
    run(bars, strategy)

    assert calls, "an instance-assigned on_cross_section was never called"


def test_a_subclass_that_does_not_override_stays_on_the_old_path() -> None:
    """The other side: the gate must not fire for a strategy that inherits it.

    Without this the fix for the test above passes trivially by always firing,
    which would cost the bit-identity every earlier result depends on.
    """

    class Base(Strategy):
        name = "Base"

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            pass

    class Child(Base):
        name = "Child"

    bars = [bar_at(s, d, "100") for d in range(4) for s in ("AAA", "BBB")]
    result = run(bars, Child())
    assert not result.fills  # type: ignore[attr-defined]


def test_the_cross_section_is_empty_before_the_first_bar() -> None:
    """And is empty the same way with a registry as without one.

    It used to return () with no registry and raise "no bar has been processed
    yet" with one, so the same call behaved differently depending on how the
    Backtester was configured.
    """
    seen: list[tuple[str, ...]] = []

    class AsksEarly(Strategy):
        name = "AsksEarly"

        def on_start(self, ctx: StrategyContext) -> None:
            seen.append(ctx.cross_section())

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            pass

    bars = [bar_at(s, d, "100") for d in range(3) for s in ("AAA", "BBB")]
    registry = UniverseRegistry(listings=(listing("AAA", 2), listing("BBB", 2)))
    run(bars, AsksEarly())
    run(bars, AsksEarly(), universe=registry)
    assert seen == [(), ()]
