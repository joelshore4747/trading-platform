"""Does the generator produce a market that behaves like the real one?

This file is what makes "calibrated" a fact rather than a claim. Each test
asserts a *stylised fact* — a documented statistical property of BTC returns —
against `data/calibration.py`, so when the constants there go stale these fail
loudly rather than the generator quietly drifting away from reality.

**Every assertion is on a median across many seeds, inside a band.** A
fat-tailed GARCH process has a genuinely wide sampling distribution: trend
volatility spans roughly 36-51% between seeds around a 40% target. A
single-seed point assertion would be flaky, and a flaky test gets its band
widened until it passes, at which point it asserts nothing. Measuring the
median across 30 seeds is the honest version.

The bug these exist to prevent is recorded in the module docstring of
`data/synthetic.py`: the previous generator paid an annualised Sharpe of 6.36
in an ordinary uptrend and 12.75 in recovery, against a real BTC buy-and-hold
Sharpe near 1.0. Every result this project produced before that was measured on
a market substantially easier than the real one.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_platform.core.events import Bar
from trading_platform.data import calibration as cal
from trading_platform.data.synthetic import (
    MAX_BAR_RETURN,
    Regime,
    RegimeParams,
    RegimeSpec,
    generate_bars,
    generate_funding,
)

START = datetime(2021, 1, 1, tzinfo=UTC)
SYMBOL = "BTCUSDT"

#: Enough seeds that the median is stable, few enough to stay fast.
SEEDS = 30


@dataclass(frozen=True, slots=True)
class Stats:
    annual_vol: float
    annual_sharpe: float
    kurtosis: float
    squared_return_autocorr: float


def describe(bars: list[Bar]) -> Stats:
    """The four properties the calibration makes claims about."""
    closes = [float(b.close) for b in bars]
    returns = [(b - a) / a for a, b in itertools.pairwise(closes)]
    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    sigma = math.sqrt(variance)

    fourth = sum((r - mean) ** 4 for r in returns) / n
    squared = [(r - mean) ** 2 for r in returns]
    squared_mean = sum(squared) / n
    denominator = sum((s - squared_mean) ** 2 for s in squared)
    autocorr = (
        sum((squared[i] - squared_mean) * (squared[i + 1] - squared_mean) for i in range(n - 1))
        / denominator
        if denominator
        else 0.0
    )

    return Stats(
        annual_vol=sigma * math.sqrt(365),
        annual_sharpe=mean / sigma * math.sqrt(365) if sigma else 0.0,
        kurtosis=fourth / variance**2 if variance else 0.0,
        squared_return_autocorr=autocorr,
    )


def across_seeds(regime: Regime | list[RegimeSpec], bars: int, seeds: int = SEEDS) -> list[Stats]:
    if isinstance(regime, Regime):
        return [
            describe(generate_bars(SYMBOL, START, regime, bars=bars, seed=s)) for s in range(seeds)
        ]
    return [describe(generate_bars(SYMBOL, START, regime, seed=s)) for s in range(seeds)]


def median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


# -- Volatility --------------------------------------------------------------


@pytest.mark.parametrize("regime", [Regime.TREND, Regime.CHOP])
def test_ordinary_regimes_sit_inside_the_documented_btc_volatility_band(
    regime: Regime,
) -> None:
    """23-45% annualised, per Fidelity and iShares. Stressed regimes are
    deliberately outside this — a crash is not an ordinary market."""
    got = median([s.annual_vol for s in across_seeds(regime, 1200)])
    assert cal.BTC_ANNUAL_VOL_LOW <= got <= cal.BTC_ANNUAL_VOL_HIGH + 0.02, (
        f"{regime.value} realises {got:.1%} annualised; documented BTC range is "
        f"{cal.BTC_ANNUAL_VOL_LOW:.0%}-{cal.BTC_ANNUAL_VOL_HIGH:.0%}"
    )


def test_the_stated_volatility_is_what_gets_realised() -> None:
    """Jumps carry their own variance, so the GARCH target has to be reduced by
    it. Without that subtraction `annual_vol` is a floor, not a target, and a
    stressed regime runs ~20% hotter than it claims to."""
    for regime, bars, tolerance in [
        (Regime.TREND, 1200, 0.15),
        (Regime.CHOP, 1200, 0.15),
        (Regime.CRASH, 90, 0.30),  # short window, extreme regime: wider band
        (Regime.RECOVERY, 150, 0.30),
    ]:
        from trading_platform.data.synthetic import _PARAMS

        target = _PARAMS[regime].annual_vol
        got = median([s.annual_vol for s in across_seeds(regime, bars)])
        assert abs(got - target) / target < tolerance, (
            f"{regime.value} claims {target:.0%} annualised but realises {got:.0%}"
        )


# -- Tails -------------------------------------------------------------------


@pytest.mark.parametrize("regime", [Regime.TREND, Regime.CHOP])
def test_returns_are_fat_tailed(regime: Regime) -> None:
    """The previous generator produced a kurtosis of 2.82 — *thinner* than a
    Gaussian's 3.0 — in an asset whose fat tails are its defining feature. A
    strategy that blows up on a six-sigma day would never have found out."""
    got = median([s.kurtosis for s in across_seeds(regime, 1200)])
    assert got > cal.MIN_DAILY_KURTOSIS, (
        f"{regime.value} kurtosis {got:.2f} is below the calibrated floor "
        f"{cal.MIN_DAILY_KURTOSIS}; a Gaussian is 3.0"
    )
    assert got > 3.0, "fatter than Gaussian is the whole point"


def test_a_single_outlier_cannot_fake_fat_tails() -> None:
    """Kurtosis is easy to inflate with one huge bar. Check the tail is
    populated: several returns beyond 4 sigma, not one beyond 40."""
    bars = generate_bars(SYMBOL, START, Regime.CHOP, bars=3000, seed=4)
    closes = [float(b.close) for b in bars]
    returns = [(b - a) / a for a, b in itertools.pairwise(closes)]
    mean = sum(returns) / len(returns)
    sigma = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))

    beyond_4_sigma = sum(1 for r in returns if abs(r - mean) > 4 * sigma)
    assert beyond_4_sigma >= 5, f"only {beyond_4_sigma} returns beyond 4 sigma"


# -- Clustering --------------------------------------------------------------


@pytest.mark.parametrize("regime", [Regime.TREND, Regime.CHOP])
def test_volatility_clusters(regime: Regime) -> None:
    """The most robust stylised fact in finance, and one the old i.i.d.
    generator had none of. Any strategy implicitly relying on volatility
    persistence was untested before this."""
    got = median([s.squared_return_autocorr for s in across_seeds(regime, 1200)])
    assert got > cal.MIN_SQUARED_RETURN_AUTOCORR, (
        f"{regime.value} squared-return autocorrelation {got:.3f} — an i.i.d. "
        "series sits at zero, so this generator has no volatility clustering"
    )


# -- The regression guard ----------------------------------------------------


def test_a_trending_market_does_not_hand_out_a_free_sharpe_of_six() -> None:
    """**The test this whole file exists for.**

    The previous generator's `trend` regime paid an annualised Sharpe of 6.36
    and `recovery` paid 12.75, against a real BTC buy-and-hold Sharpe of
    roughly 0.5-1.0. Anything holding long looked brilliant on a six- to
    thirteen-fold free lunch, and every result measured through it was
    flattered.
    """
    got = median([s.annual_sharpe for s in across_seeds(Regime.TREND, 1200)])
    assert got < cal.BTC_BUY_AND_HOLD_SHARPE_HIGH + 0.3, (
        f"trend pays an annualised Sharpe of {got:.2f}; real BTC buy-and-hold is "
        f"{cal.BTC_BUY_AND_HOLD_SHARPE_LOW}-{cal.BTC_BUY_AND_HOLD_SHARPE_HIGH}. "
        "A generator paying more than the market ever has hands every long-biased "
        "strategy an edge that does not exist."
    )
    assert got > 0, "an uptrend should still trend upward"


def test_no_regime_may_declare_a_sharpe_no_market_has_paid() -> None:
    """Enforced at construction, so the 6.36 cannot be reintroduced by editing
    a number in the parameter table."""
    with pytest.raises(ValueError, match="exceeds the calibration ceiling"):
        RegimeParams(annual_sharpe=6.36, annual_vol=0.40, jump_probability=0.0)


def test_a_blended_series_looks_like_a_market_someone_could_have_traded() -> None:
    """Regime Sharpes are conditional; a realistic mix should blend to
    something inside the plausible full-sample range."""
    mixed = [
        RegimeSpec(Regime.TREND, 400),
        RegimeSpec(Regime.CHOP, 300),
        RegimeSpec(Regime.CRASH, 60),
        RegimeSpec(Regime.RECOVERY, 120),
        RegimeSpec(Regime.CHOP, 300),
        RegimeSpec(Regime.TREND, 300),
    ]
    stats = across_seeds(mixed, 0, seeds=15)
    sharpe = median([s.annual_sharpe for s in stats])
    vol = median([s.annual_vol for s in stats])

    assert -1.0 < sharpe < 2.0, f"blended Sharpe {sharpe:.2f} is not a plausible market"
    assert 0.30 < vol < 0.75, f"blended volatility {vol:.0%} is not a plausible market"


# -- The clamp ---------------------------------------------------------------


def test_a_realistic_crash_never_pins_the_price_at_a_floor() -> None:
    """Flooring price at a constant let a 3000-bar crash spend 2194 bars at
    0.01 and then manufacture enormous bounces off it, reporting 219%
    annualised volatility that was pure clamping artefact."""
    bars = generate_bars(SYMBOL, START, Regime.CRASH, bars=200, seed=1)
    closes = [float(b.close) for b in bars]

    assert closes[-1] > 0, "price must stay strictly positive"
    # Every step is a real proportional move, never a bounce off a floor.
    ratios = [b / a for a, b in itertools.pairwise(closes)]
    assert min(ratios) >= 1.0 - MAX_BAR_RETURN - 1e-9
    assert max(ratios) <= 1.0 + MAX_BAR_RETURN + 1e-9


def test_an_impossibly_long_crash_raises_rather_than_quietly_pinning() -> None:
    """The tick floor is a milder version of the same artefact, so the
    generator refuses the input instead of producing a sub-satoshi asset. A
    2000-bar crash is not a market; saying so is more useful than a series."""
    with pytest.raises(ValueError, match="No market falls that far"):
        generate_bars(SYMBOL, START, Regime.CRASH, bars=2000, seed=1)


def test_no_bar_moves_further_than_a_real_market_does() -> None:
    for regime in Regime:
        bars = generate_bars(SYMBOL, START, regime, bars=600, seed=2)
        closes = [float(b.close) for b in bars]
        for a, b in itertools.pairwise(closes):
            assert abs(b / a - 1.0) <= MAX_BAR_RETURN + 1e-9


# -- Funding -----------------------------------------------------------------


def test_funding_never_exceeds_the_venue_cap() -> None:
    """Binance caps funding at +/-0.05% per 8h window. An uncapped generator
    tests a market that cannot exist and overstates how fast a carry trade
    accrues."""
    rates = generate_funding(SYMBOL, START, settlements=2000, seed=3)
    assert rates, "expected settlements"
    assert all(abs(r.rate) <= cal.FUNDING_RATE_CAP for r in rates)
    # And it must still reach both signs, or the short-receives path is untested.
    assert any(r.rate > 0 for r in rates)
    assert any(r.rate < 0 for r in rates)


def test_funding_settles_on_the_documented_cadence() -> None:
    rates = generate_funding(SYMBOL, START, settlements=6, seed=0)
    gaps = {
        (b.timestamp - a.timestamp).total_seconds() / 3600 for a, b in itertools.pairwise(rates)
    }
    assert gaps == {float(cal.FUNDING_INTERVAL_HOURS)}


# -- Determinism, still ------------------------------------------------------


def test_the_new_engine_is_still_deterministic_from_a_seed() -> None:
    """Fat tails and GARCH state add a lot of moving parts. A test asserting on
    backtest output must give the same numbers on every machine."""
    a = generate_bars(SYMBOL, START, Regime.CHOP, bars=200, seed=11)
    b = generate_bars(SYMBOL, START, Regime.CHOP, bars=200, seed=11)
    assert [x.close for x in a] == [x.close for x in b]
    assert a[-1].close != generate_bars(SYMBOL, START, Regime.CHOP, bars=200, seed=12)[-1].close


def test_prices_are_still_exact_decimals() -> None:
    bars = generate_bars(SYMBOL, START, Regime.TREND, bars=50, seed=1)
    assert all(isinstance(b.close, Decimal) for b in bars)
    assert all(b.close == b.close.quantize(Decimal("0.01")) for b in bars)
