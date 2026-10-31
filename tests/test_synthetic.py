"""Synthetic data generator.

Since generated series are now the primary test instrument, the generator
itself needs testing. Two properties matter most: it is deterministic (a test
that cannot be reproduced is worse than no test), and it produces bars that
actually stress the engine rather than trivially satisfying it.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.core.events import Bar
from trading_platform.core.instruments import crypto_perpetual, crypto_spot
from trading_platform.data.binance import observed_funding_interval_hours
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.data.funding import FundingSchedule
from trading_platform.data.synthetic import (
    _PARAMS,
    BARS_PER_YEAR,
    Regime,
    RegimeSpec,
    bars_per_year,
    correlated_series,
    generate_bars,
    generate_feed,
    generate_funding,
    inject_gaps,
)
from trading_platform.engine.backtester import Backtester
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.base import Strategy, StrategyContext

START = datetime(2025, 1, 1, tzinfo=UTC)
SYMBOL = "BTCUSDT"


# -- Determinism ------------------------------------------------------------


def test_same_seed_gives_an_identical_series() -> None:
    a = generate_bars(SYMBOL, START, Regime.CHOP, bars=100, seed=42)
    b = generate_bars(SYMBOL, START, Regime.CHOP, bars=100, seed=42)
    assert [x.close for x in a] == [x.close for x in b]
    assert [x.high for x in a] == [x.high for x in b]
    assert [x.volume for x in a] == [x.volume for x in b]


def test_different_seed_gives_a_different_series() -> None:
    a = generate_bars(SYMBOL, START, Regime.CHOP, bars=100, seed=1)
    b = generate_bars(SYMBOL, START, Regime.CHOP, bars=100, seed=2)
    assert [x.close for x in a] != [x.close for x in b]


def test_generator_never_reads_the_clock() -> None:
    from pathlib import Path

    import trading_platform.data.synthetic as mod

    source = Path(mod.__file__).read_text()
    for forbidden in ("time.time", "datetime.now", "utcnow", "random.seed("):
        assert forbidden not in source, f"{forbidden} would make output unreproducible"


# -- Bars that actually exercise the engine ---------------------------------


def test_open_and_close_genuinely_differ() -> None:
    """synthetic_walk has open == close, so limit fills are never tested.
    This generator must not share that weakness."""
    bars = generate_bars(SYMBOL, START, Regime.TREND, bars=200, seed=7)
    differing = sum(1 for b in bars if b.open != b.close)
    assert differing > 190, f"only {differing}/200 bars had intrabar movement"


def test_high_and_low_extend_beyond_the_body() -> None:
    """A limit order at the touch must be able to fill."""
    bars = generate_bars(SYMBOL, START, Regime.CHOP, bars=200, seed=7)
    with_wicks = sum(
        1 for b in bars if b.high > max(b.open, b.close) or b.low < min(b.open, b.close)
    )
    assert with_wicks > 150, f"only {with_wicks}/200 bars had wicks"


def test_all_generated_bars_are_valid() -> None:
    """Construction goes through Bar.__post_init__, so this cannot regress
    silently — but assert it for every regime anyway."""
    for regime in Regime:
        bars = generate_bars(SYMBOL, START, regime, bars=150, seed=3)
        assert len(bars) == 150
        for b in bars:
            assert b.low <= b.open <= b.high
            assert b.low <= b.close <= b.high
            assert b.volume > 0
            assert b.close > 0


def test_timestamps_are_ordered_and_evenly_spaced() -> None:
    bars = generate_bars(SYMBOL, START, Regime.TREND, bars=50, seed=1)
    deltas = {bars[i + 1].timestamp - bars[i].timestamp for i in range(len(bars) - 1)}
    assert deltas == {timedelta(days=1)}


# -- Regimes behave as advertised -------------------------------------------


def test_crash_falls_and_trend_rises() -> None:
    crash = generate_bars(SYMBOL, START, Regime.CRASH, bars=60, seed=11)
    trend = generate_bars(SYMBOL, START, Regime.TREND, bars=60, seed=11)
    assert crash[-1].close < crash[0].close * Decimal("0.5"), "a crash must actually crash"
    assert trend[-1].close > trend[0].close, "a trend must actually trend"


def test_flat_barely_moves_and_chop_goes_nowhere() -> None:
    flat = generate_bars(SYMBOL, START, Regime.FLAT, bars=100, seed=5)
    moved = abs(flat[-1].close - flat[0].close) / flat[0].close
    assert moved < Decimal("0.15"), f"flat regime moved {moved:.1%}"

    chop = generate_bars(SYMBOL, START, Regime.CHOP, bars=300, seed=5)
    drift = abs(chop[-1].close - chop[0].close) / chop[0].close
    assert drift < Decimal("1.5"), "chop should not become a strong trend"


def test_regimes_compose_into_one_series() -> None:
    bars = generate_bars(
        SYMBOL,
        START,
        [
            RegimeSpec(Regime.TREND, 40),
            RegimeSpec(Regime.CRASH, 20),
            RegimeSpec(Regime.RECOVERY, 30),
        ],
        seed=9,
    )
    assert len(bars) == 90
    peak = max(b.close for b in bars[:40])
    trough = min(b.close for b in bars[40:60])
    assert trough < peak, "the crash section must draw down from the trend peak"


def test_crash_regime_trips_the_drawdown_halt() -> None:
    """The point of having a crash regime: it must reach the risk layer."""

    class BuyAndHold(Strategy):
        name = "BuyAndHold"

        def __init__(self) -> None:
            self.done = False

        def on_bar(self, b, ctx: StrategyContext) -> None:  # type: ignore[no-untyped-def]
            if not self.done:
                ctx.target_notional(SYMBOL, ctx.equity * Decimal("0.9"))
                self.done = True

    # Across seeds, not one. This asserted a single seed at 40 crash bars,
    # which halts on only 16 of 20 seeds — so it was sampling a coin flip and
    # calling it a property. Making the generator deliver its declared Sharpe
    # exactly (rather than that plus an unaccounted jump drag) moved seed 4
    # from the lucky side to the unlucky one, which is how the weakness
    # surfaced. At 80 bars every seed breaches, so the claim is real.
    for seed in range(5):
        bars = generate_bars(
            SYMBOL,
            START,
            [RegimeSpec(Regime.TREND, 10), RegimeSpec(Regime.CRASH, 80)],
            seed=seed,
        )
        bt = Backtester(
            instruments={SYMBOL: crypto_spot(SYMBOL)},
            starting_cash=Decimal("100000"),
            risk_manager=RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.20"))),
        )
        result = bt.run(BuyAndHold(), InMemoryBarFeed(bars))
        assert result.halted, f"seed {seed}: an 80-bar crash must breach a 20% limit"


def test_single_regime_requires_a_bar_count() -> None:
    with pytest.raises(ValueError, match="Pass `bars`"):
        generate_bars(SYMBOL, START, Regime.TREND)


def test_empty_regime_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one regime"):
        generate_bars(SYMBOL, START, [])


def test_regime_spec_rejects_zero_bars() -> None:
    with pytest.raises(ValueError, match="at least 1 bar"):
        RegimeSpec(Regime.TREND, 0)


def test_generate_feed_is_engine_ready() -> None:
    feed = generate_feed(SYMBOL, START, Regime.TREND, bars=20, seed=1)
    assert feed.symbols == (SYMBOL,)
    assert len(list(feed)) == 20


# -- Gaps -------------------------------------------------------------------


def test_inject_gaps_removes_bars_but_keeps_the_span() -> None:
    bars = generate_bars(SYMBOL, START, Regime.TREND, bars=100, seed=2)
    gapped = inject_gaps(bars, seed=2, fraction=0.2)

    assert len(gapped) < len(bars)
    assert gapped[0].timestamp == bars[0].timestamp, "first bar kept"
    assert gapped[-1].timestamp == bars[-1].timestamp, "last bar kept"
    assert [b.timestamp for b in gapped] == sorted(b.timestamp for b in gapped)


def test_gapped_series_still_loads_through_the_feed() -> None:
    """Gaps are permitted by design — the feed must not reject them."""
    bars = inject_gaps(generate_bars(SYMBOL, START, Regime.TREND, bars=60, seed=2), seed=2)
    assert len(list(InMemoryBarFeed(bars))) == len(bars)


def test_zero_fraction_is_a_no_op() -> None:
    bars = generate_bars(SYMBOL, START, Regime.TREND, bars=20, seed=1)
    assert inject_gaps(bars, fraction=0.0) == bars


def test_invalid_gap_fraction_is_rejected() -> None:
    bars = generate_bars(SYMBOL, START, Regime.TREND, bars=20, seed=1)
    with pytest.raises(ValueError, match="fraction must be"):
        inject_gaps(bars, fraction=1.0)


# -- Funding ----------------------------------------------------------------


def test_generated_funding_is_deterministic_and_signed_both_ways() -> None:
    a = generate_funding("BTC-PERP", START, settlements=200, seed=8)
    b = generate_funding("BTC-PERP", START, settlements=200, seed=8)
    assert [r.rate for r in a] == [r.rate for r in b]
    assert any(r.rate > 0 for r in a), "must exercise the long-pays path"
    assert any(r.rate < 0 for r in a), "must exercise the short-pays path"


def test_generated_funding_contains_both_cadences() -> None:
    """Mirrors Binance's October 2023 switch. Code that assumes a fixed
    interval must be tested against a series that changes."""
    rates = generate_funding(
        "BTC-PERP",
        START,
        settlements=60,
        seed=1,
        interval_hours=8,
        switch_to_hours=4,
        switch_after=30,
    )
    early = {
        int((rates[i + 1].timestamp - rates[i].timestamp).total_seconds() / 3600) for i in range(28)
    }
    late = {
        int((rates[i + 1].timestamp - rates[i].timestamp).total_seconds() / 3600)
        for i in range(31, 58)
    }
    assert early == {8}
    assert late == {4}
    # The existing detector reports the dominant interval, not a fiction.
    assert observed_funding_interval_hours(rates) in (Decimal(4), Decimal(8))


def test_generated_funding_drives_the_engine() -> None:
    """End to end: generated bars + generated funding through the backtester."""
    perp = crypto_perpetual("BTC-PERP")
    bars = generate_bars("BTC-PERP", START, Regime.CHOP, bars=30, seed=6)
    schedule = FundingSchedule(generate_funding("BTC-PERP", START, settlements=90, seed=6))

    class BuyAndHold(Strategy):
        name = "BuyAndHold"

        def __init__(self) -> None:
            self.done = False

        def on_bar(self, b, ctx: StrategyContext) -> None:  # type: ignore[no-untyped-def]
            if not self.done:
                ctx.buy("BTC-PERP", Decimal("5"))
                self.done = True

    bt = Backtester(
        instruments={"BTC-PERP": perp},
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=RiskLimits(max_drawdown_pct=None)),
        funding=schedule,
    )
    result = bt.run(BuyAndHold(), InMemoryBarFeed(bars))
    assert result.funding_settlements > 0
    assert result.total_funding != Decimal("0")


def test_funding_requires_at_least_one_settlement() -> None:
    with pytest.raises(ValueError, match="at least 1 settlement"):
        generate_funding("BTC-PERP", START, settlements=0)


# -- Correlated multi-symbol ------------------------------------------------


def test_correlated_series_is_feed_ordered_and_multi_symbol() -> None:
    bars = correlated_series(["BTCUSDT", "ETHUSDT"], START, bars=50, seed=3)
    assert len(bars) == 100
    assert {b.symbol for b in bars} == {"BTCUSDT", "ETHUSDT"}
    assert [(b.timestamp, b.symbol) for b in bars] == sorted((b.timestamp, b.symbol) for b in bars)


def test_correlated_symbols_move_together_more_than_independent_ones() -> None:
    high = correlated_series(["A", "B"], START, bars=200, seed=4, correlation=0.95)
    low = correlated_series(["A", "B"], START, bars=200, seed=4, correlation=0.0)

    def agreement(bars):  # type: ignore[no-untyped-def]
        by_symbol: dict[str, list[Decimal]] = {}
        for b in bars:
            by_symbol.setdefault(b.symbol, []).append(b.close)
        a, b_ = by_symbol["A"], by_symbol["B"]
        moves_a = [a[i + 1] - a[i] for i in range(len(a) - 1)]
        moves_b = [b_[i + 1] - b_[i] for i in range(len(b_) - 1)]
        return sum(1 for x, y in zip(moves_a, moves_b, strict=True) if (x > 0) == (y > 0))

    assert agreement(high) > agreement(low), "correlation must actually correlate"


def test_correlated_series_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="at least one symbol"):
        correlated_series([], START, bars=10)
    with pytest.raises(ValueError, match="correlation must be"):
        correlated_series(["A"], START, bars=10, correlation=1.5)


# -- The generator must mean the same thing at every bar size ----------------


def _annualised(bars: list[Bar], periods: float) -> tuple[float, float]:
    prices = [float(b.close) for b in bars]
    returns = [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))]
    mean = sum(returns) / len(returns)
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    return mean / sd * math.sqrt(periods), sd * math.sqrt(periods)


@pytest.mark.parametrize(
    "step", [timedelta(days=1), timedelta(hours=4), timedelta(hours=1), timedelta(minutes=1)]
)
def test_declared_volatility_holds_at_every_bar_size(step: timedelta) -> None:
    """`BARS_PER_YEAR` used to be a module constant applied at every
    resolution, so `step` reached only the timestamp line and a minute series
    was scaled as though each minute were a day. TREND declares 40% annual
    volatility and delivered 43.4% daily, 212.8% hourly and **1,648.1% at one
    minute** — while `MAX_REGIME_SHARPE`, the ceiling built to stop exactly
    this, checks the declared figure and so saw nothing.
    """
    periods = bars_per_year(step)
    vols = []
    for seed in range(6):
        bars = generate_bars(SYMBOL, START, Regime.TREND, bars=8000, seed=seed, step=step)
        vols.append(_annualised(bars, periods)[1])
    average = sum(vols) / len(vols)
    assert 0.30 < average < 0.55, f"{step}: {average:.1%} against a declared 40%"


def test_declared_sharpe_holds_over_equal_calendar_time() -> None:
    """Compared over equal *years*, not equal bar counts.

    Sharpe's standard error is about 1/sqrt(years), so eight thousand minute
    bars is 0.015 years and a measurement over them carries an error of ±8.
    Comparing bar-for-bar across resolutions would read that noise as a
    scaling bug — which is exactly what it looked like mid-fix.
    """
    years = 3.0
    tolerance = 1.0 / math.sqrt(years)
    for step in (timedelta(days=1), timedelta(hours=4), timedelta(hours=1)):
        periods = bars_per_year(step)
        sharpes = []
        for seed in range(6):
            bars = generate_bars(
                SYMBOL, START, Regime.TREND, bars=int(periods * years), seed=seed, step=step
            )
            sharpes.append(_annualised(bars, periods)[0])
        average = sum(sharpes) / len(sharpes)
        assert abs(average - 0.9) < 2 * tolerance, (
            f"{step}: Sharpe {average:.2f} against a declared 0.9, "
            f"tolerance {2 * tolerance:.2f} at {years} years"
        )


def test_a_jump_is_an_event_per_year_not_per_bar() -> None:
    """Jump probability is declared per day and scaled to the bar.

    Left per-bar, a 5% jump drawn at a daily rate on minute bars swamped the
    diffusion completely: the jump variance alone exceeded the regime's whole
    volatility target, pinning the GARCH target at its floor. The invariant is
    jumps per year.
    """
    params = _PARAMS[Regime.TREND]
    rates = {
        step: params.bar_jump_probability(bars_per_year(step)) * bars_per_year(step)
        for step in (timedelta(days=1), timedelta(hours=1), timedelta(minutes=1))
    }
    assert len(set(round(r, 9) for r in rates.values())) == 1, rates


def test_a_jump_with_a_mean_no_longer_shaves_the_declared_drift() -> None:
    """`bar_drift` derived from `annual_sharpe` alone and never subtracted the
    jump component's own drift, so a regime with `jump_mean != 0` delivered
    something other than what it declared. CRASH ran **34% more negative** than
    its stated Sharpe and RECOVERY 35% more positive.
    """
    for regime in (Regime.CHOP, Regime.FLAT):
        assert _PARAMS[regime].jump_drift() == 0.0, "no jump mean, nothing to subtract"

    crash = _PARAMS[Regime.CRASH]
    assert crash.jump_drift() < 0
    # The declared Sharpe is what the total drift must produce.
    total = crash.bar_drift() + crash.jump_drift()
    expected = crash.annual_sharpe * crash.bar_sigma() / math.sqrt(BARS_PER_YEAR)
    assert abs(total - expected) < 1e-12


# -- The regime that exists to be unprofitable -------------------------------


def _autocorrelation(values: list[float], lag: int = 1) -> float:
    mean = sum(values) / len(values)
    denominator = sum((v - mean) ** 2 for v in values)
    if not denominator:
        return 0.0
    return sum((values[i] - mean) * (values[i + lag] - mean) for i in range(len(values) - lag)) / (
        denominator
    )


def _returns(bars: list[Bar]) -> list[float]:
    prices = [float(b.close) for b in bars]
    return [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))]


def _mean_acf(regime: Regime, *, seeds: int = 16, bars: int = 4000) -> float:
    values = []
    for seed in range(seeds):
        series = generate_bars(SYMBOL, START, regime, bars=bars, seed=seed)
        values.append(_autocorrelation(_returns(series)))
    return sum(values) / len(values)


def test_the_null_regime_has_no_directional_structure() -> None:
    """The property the false-discovery harness rests on.

    Asserted on the mean across seeds, not per seed: at n=4000 the worst
    single seed reaches 0.073 through sampling noise alone, so a per-seed
    bound of 0.02 would fail on correct data.
    """
    assert abs(_mean_acf(Regime.NULL)) < 0.02


def test_the_null_regime_still_has_fat_tails_and_clustered_volatility() -> None:
    """Realistic in every respect that does not confer directional edge.

    Clustering is in deliberately. It adds no directional structure — the sign
    path is still a martingale — but it widens the sampling distribution of
    every Sharpe-derived threshold in the verdict cascade, so a null built on
    Gaussian returns would *understate* the false-discovery rate of a pipeline
    that will be run on real data.
    """
    series = generate_bars(SYMBOL, START, Regime.NULL, bars=6000, seed=0)
    returns = _returns(series)
    mean = sum(returns) / len(returns)
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))

    kurtosis = sum((r - mean) ** 4 for r in returns) / len(returns) / sd**4
    assert kurtosis > 4.0, "a Gaussian would sit at 3"
    assert _autocorrelation([abs(r) for r in returns]) > 0.05, "volatility must cluster"


def test_the_null_assertion_would_reject_chop() -> None:
    """The control that makes the threshold mean something.

    `Regime.CHOP` also declares an annual Sharpe of 0.0, which reads as "no
    edge" — and carries `mean_reversion=0.18`, giving return autocorrelation
    around -0.17. That is directly exploitable: buy after a down bar, sell
    after an up bar. A false-discovery test run against CHOP would be
    measuring a *true* discovery and calling it a false one.
    """
    assert abs(_mean_acf(Regime.CHOP)) > 0.10
    assert abs(_mean_acf(Regime.NULL)) < 0.02


def test_the_null_regime_actually_moves() -> None:
    """`Regime.FLAT` is also unpredictable, and useless as a null: at 5%
    annualised volatility nothing trades, so a harness run against it reports
    no false discoveries because nothing happened."""
    null_series = generate_bars(SYMBOL, START, Regime.NULL, bars=2000, seed=0)
    flat_series = generate_bars(SYMBOL, START, Regime.FLAT, bars=2000, seed=0)

    def spread(bars: list[Bar]) -> float:
        returns = _returns(bars)
        mean = sum(returns) / len(returns)
        return math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))

    assert spread(null_series) > 5 * spread(flat_series)
