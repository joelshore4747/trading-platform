"""What to believe about the best of N trials.

The arithmetic is checked against closed forms where they exist. The
behavioural claims — a real edge drives PBO to zero, pure noise leaves it at a
half — are checked by planting an edge of known size and measuring, because a
statistic that has only ever been run on one dataset has not been shown to
discriminate anything.
"""

from __future__ import annotations

import math
import random
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.analytics.metrics import to_returns
from trading_platform.analytics.multiplicity import (
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
)
from trading_platform.analytics.trials import family_wise_error
from trading_platform.core.events import Bar
from trading_platform.core.instruments import crypto_spot
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.engine.backtester import Backtester
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.examples.sma_crossover import SMACrossover
from trading_platform.ui.runner import RunConfig, judge


def noise(trials: int, periods: int, rng: random.Random) -> list[list[float]]:
    return [[rng.gauss(0.0, 1.0) for _ in range(periods)] for _ in range(trials)]


def with_edge(trials: int, periods: int, rng: random.Random, mu: float) -> list[list[float]]:
    """Trial 0 has a genuine per-period edge of `mu`; the rest are noise."""
    series = noise(trials, periods, rng)
    series[0] = [value + mu for value in series[0]]
    return series


# -- Expected maximum ---------------------------------------------------------


def test_expected_max_rises_with_the_number_of_trials() -> None:
    """More shots, higher bar. This is the whole point of deflating."""
    rng = random.Random(1)
    draws = [rng.gauss(0.0, 1.0) for _ in range(200)]
    few = expected_max_sharpe(draws[:5])
    many = expected_max_sharpe(draws[:200])
    assert few < many


def test_expected_max_tracks_the_sample_maximum_on_noise() -> None:
    """On unit-variance noise the best of N should land near the actual best."""
    rng = random.Random(3)
    draws = [rng.gauss(0.0, 1.0) for _ in range(20)]
    assert expected_max_sharpe(draws) == pytest.approx(max(draws), abs=0.35)


def test_identical_trials_deflate_by_nothing() -> None:
    """A grid whose cells all score alike is one experiment, not N.

    The dispersion comes from the trials rather than being assumed, so
    correlated parameter settings are correctly not counted as independent
    attempts — which is the case that matters, since adjacent grid points
    never are.
    """
    assert expected_max_sharpe([0.4] * 50) == 0.0
    assert expected_max_sharpe([0.4]) == 0.0
    assert expected_max_sharpe([]) == 0.0


# -- Probabilistic Sharpe -----------------------------------------------------


def test_psr_at_the_benchmark_is_a_coin_flip() -> None:
    assert probabilistic_sharpe_ratio(0.1, periods=500, benchmark=0.1) == pytest.approx(0.5)


def test_psr_rises_with_track_record_length() -> None:
    short = probabilistic_sharpe_ratio(0.1, periods=50)
    long = probabilistic_sharpe_ratio(0.1, periods=2000)
    assert short < long
    assert long > 0.99


def test_psr_matches_the_published_denominator_not_my_first_guess() -> None:
    """The denominator is 1 - g3*SR + ((g4 - 1)/4)*SR^2 with g4 RAW kurtosis.

    This module carries EXCESS kurtosis, so the coefficient is (excess + 2)/4.
    Written as excess/4 the SR^2 term vanishes at a Gaussian instead of taking
    a coefficient of 0.5 — which understates the estimator's variance and
    overstates confidence. Pinned here against the closed form so it cannot
    drift back.
    """
    sharpe, periods = 0.15, 300
    variance = 1.0 - 0.0 * sharpe + ((0.0 + 2.0) / 4.0) * sharpe**2
    expected = statistics.NormalDist().cdf(sharpe * math.sqrt(periods - 1) / math.sqrt(variance))
    assert probabilistic_sharpe_ratio(sharpe, periods=periods) == pytest.approx(expected)
    # And the wrong form really is more confident, in the direction that flatters.
    wrong = statistics.NormalDist().cdf(sharpe * math.sqrt(periods - 1) / math.sqrt(1.0))
    assert wrong > probabilistic_sharpe_ratio(sharpe, periods=periods)


def test_negative_skew_and_fat_tails_cost_confidence() -> None:
    """Two strategies with the same Sharpe are not equally believable."""
    plain = probabilistic_sharpe_ratio(0.12, periods=400)
    insurance = probabilistic_sharpe_ratio(0.12, periods=400, skew=-1.5, excess_kurtosis=8.0)
    assert insurance < plain


def test_psr_reduces_to_lo_2002_under_normality() -> None:
    """The decisive check, and it needs no source: at skew 0 and excess 0 the
    denominator must be Lo's `1 + SR^2/2`. The form I first wrote gives 1."""
    sharpe, periods = 0.2, 400
    expected = statistics.NormalDist().cdf(
        sharpe * math.sqrt(periods - 1) / math.sqrt(1 + sharpe**2 / 2)
    )
    assert probabilistic_sharpe_ratio(sharpe, periods=periods) == pytest.approx(expected)


def test_psr_needs_more_than_one_period() -> None:
    assert probabilistic_sharpe_ratio(0.5, periods=1) == 0.0


def test_an_annualised_sharpe_is_refused_rather_than_silently_saturating() -> None:
    """The trap this codebase sets: `SweepCell.score` is annualised.

    Fed in, it puts z around 15, where the normal CDF is 1.0 to machine
    precision — so every strategy `survives` and the statistic is silently
    constant in the flattering direction.
    """
    with pytest.raises(ValueError, match="per-period"):
        probabilistic_sharpe_ratio(1.9, periods=365)
    with pytest.raises(ValueError, match="per-period"):
        probabilistic_sharpe_ratio(0.1, periods=365, benchmark=1.9)


def test_impossible_moments_raise_rather_than_reporting_certainty() -> None:
    """No distribution has skew 3 and excess kurtosis 0.

    The denominator is `(SR^2/4) * Var(Z^2 - (2/SR)Z)`, so it is non-negative
    for any real distribution — reaching a non-positive value means the
    moments are not jointly possible. Returning 0.0 there would assert the
    true Sharpe is certainly below benchmark, a fabricated certainty that
    `DeflatedSharpe` would then report as a legitimate rejection.
    """
    with pytest.raises(ValueError, match="violate raw kurtosis"):
        probabilistic_sharpe_ratio(0.9, periods=500, skew=3.0, excess_kurtosis=0.0)


# -- Deflated Sharpe ----------------------------------------------------------


def test_deflating_needs_the_trials_the_winner_came_from() -> None:
    with pytest.raises(ValueError, match="trials it was selected from"):
        deflated_sharpe_ratio([], periods=500)


def test_the_best_of_pure_noise_does_not_survive_deflation() -> None:
    """The case the statistic exists for: search hard enough and something wins."""
    rng = random.Random(5)
    sharpes = [rng.gauss(0.0, 0.05) for _ in range(64)]
    result = deflated_sharpe_ratio(sharpes, periods=500)
    assert result.observed == max(sharpes)
    assert result.trials == 64
    assert not result.survives


def test_an_edge_far_above_the_search_bar_survives() -> None:
    rng = random.Random(5)
    sharpes = [rng.gauss(0.0, 0.02) for _ in range(64)]
    result = deflated_sharpe_ratio([*sharpes, 0.30], periods=2000)
    assert result.observed == 0.30
    assert result.expected_max < 0.30
    assert result.survives


def test_rejected_cells_still_count_as_shots_taken() -> None:
    """A 3x3 SMA grid has seven live cells, not nine — but nine were aimed at.

    `SweepSurface.skipped` exists because of this gap, and the deflation has
    to be able to express it: the dispersion can only come from cells that
    actually scored, while the count that raises the bar is the grid as
    written.
    """
    rng = random.Random(9)
    scored = [rng.gauss(0.0, 0.05) for _ in range(7)]
    honest = deflated_sharpe_ratio(scored, periods=500, attempted=9)
    understated = deflated_sharpe_ratio(scored, periods=500)
    assert honest.trials == 9
    assert understated.trials == 7
    assert honest.expected_max > understated.expected_max
    assert honest.probability < understated.probability


def test_a_selected_cell_that_is_not_the_winner_is_honoured() -> None:
    """A sweep sometimes picks for stability rather than for the top score."""
    result = deflated_sharpe_ratio([0.1, 0.9, 0.2], selected=0.1, periods=500)
    assert result.observed == 0.1


# -- The exit criterion: a known-overfit sweep, through every gate ------------


def null_bars(seed: int, count: int = 900) -> list[Bar]:
    """White-noise returns. Nothing here is predictable, by construction."""
    rng = random.Random(seed)
    price = 100.0
    out: list[Bar] = []
    for i in range(count):
        open_ = price
        price = max(1.0, price * (1 + rng.gauss(0.0, 0.01)))
        out.append(
            Bar(
                symbol="BTCUSDT",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i),
                open=Decimal(f"{open_:.4f}"),
                high=Decimal(f"{max(open_, price) * 1.001:.4f}"),
                low=Decimal(f"{min(open_, price) * 0.999:.4f}"),
                close=Decimal(f"{price:.4f}"),
                volume=Decimal("1000"),
            )
        )
    return out


def cell_returns(bars: list[Bar], config: RunConfig) -> list[list[float]]:
    """Per-period returns for every live cell of the grid, on one window.

    Live cells only — `fast >= slow` is rejected by the strategy, so a 4x4
    grid is not sixteen trials. That difference is why `SweepSurface.skipped`
    exists.
    """
    instruments = {"BTCUSDT": crypto_spot("BTCUSDT")}
    series: list[list[float]] = []
    for fast in config.fast_windows:
        for slow in config.slow_windows:
            if fast >= slow:
                continue
            engine = Backtester(
                instruments=instruments,
                starting_cash=config.starting_cash,
                cost_model=config.cost_model,
                risk_manager=RiskManager(
                    limits=RiskLimits(max_drawdown_pct=config.max_drawdown_pct)
                ),
            )
            result = engine.run(
                SMACrossover("BTCUSDT", fast=fast, slow=slow), InMemoryBarFeed(bars)
            )
            series.append(to_returns(result.equity_curve))
    return series


@pytest.mark.slow
def test_a_known_overfit_sweep_is_rejected_by_every_gate() -> None:
    """Step 6's exit criterion, run end to end on data with no edge in it.

    A sixteen-cell SMA grid over white noise. Whatever wins, won by searching.
    The criterion as planned was "rejected by at least two of the four gates";
    there are three, because PBO was deleted after measurement — and the two
    that judge a single run both reject, on every seed tried, so the bar is
    raised to `all` rather than left at two of three.

    What this does *not* show is that the gates have power. They reject a
    genuine edge at an implied annualised Sharpe of 18.7 just as reliably; see
    ADR 0004. A run that fails this test would be alarming, but passing it is
    weak evidence, and it is recorded here so nobody reads it as strong.
    """
    config = RunConfig(seed=17)
    bars = null_bars(seed=17, count=900)

    _, report = judge(bars, config)
    returns = cell_returns(bars, config)
    sharpes = [_sharpe(r) for r in returns]
    dsr = deflated_sharpe_ratio(sharpes, periods=len(returns[0]), attempted=config.grid_cells)

    rejected = {"walk-forward": not report.survives, "deflated sharpe": not dsr.survives}
    assert all(rejected.values()), (
        f"A sweep over pure noise got past {[k for k, v in rejected.items() if not v]}\n"
        f"{report.report()}\n{dsr.report()}"
    )
    # This particular grid happens to have no invalid cells — every fast
    # window is below every slow one — so live and attempted agree. A grid
    # where they differ is covered in the unit tests.
    assert len(returns) == config.grid_cells == 16
    assert dsr.trials == 16

    # The third gate is not a property of the run at all: sixteen cells at the
    # measured per-run rate is a different number from one.
    assert family_wise_error(0.047, config.grid_cells) > 0.5


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    spread = statistics.stdev(returns)
    return statistics.mean(returns) / spread if spread else 0.0
