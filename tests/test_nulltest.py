"""The harness that measures whether the pipeline can tell signal from noise.

Two kinds of test here. The first kind checks the harness's own arithmetic
against a stub, without running a backtest. The second kind is the important
one: **deliberately broken pipelines that the harness must reject.** A guard
that has never been shown to fail is not a guard, and this repository has
shipped three of those.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.analytics.nulltest import (
    MIN_CLASSIFIED,
    Judgement,
    detection_curve,
    measure,
    null_discovery_rate,
    upper_bound,
)
from trading_platform.core.events import Bar

START = datetime(2026, 1, 1, tzinfo=UTC)


def ar1_series(momentum: float) -> object:
    """Returns with a known, tunable amount of directional predictability.

    `r_t = m * r_{t-1} + e`. At m=0 it is white noise and nothing can beat it;
    the maximum annualised Sharpe an optimal linear forecast could extract is
    `m / sqrt(1 - m^2) * sqrt(365)`, which is the unit the detection floor is
    reported in.

    Lives here rather than in `data/synthetic.py` because it is a calibration
    instrument, not market data.
    """

    def factory(seed: int, bars: int = 900) -> list[Bar]:
        rng = random.Random(seed)
        price, previous, out = 100.0, 0.0, []
        for i in range(bars):
            step = momentum * previous + rng.gauss(0, 0.01)
            previous = step
            open_ = price
            price = max(1.0, price * (1 + step))
            out.append(
                Bar(
                    symbol="NULL",
                    timestamp=START + timedelta(days=i),
                    open=Decimal(f"{open_:.4f}"),
                    high=Decimal(f"{max(open_, price) * 1.001:.4f}"),
                    low=Decimal(f"{min(open_, price) * 0.999:.4f}"),
                    close=Decimal(f"{price:.4f}"),
                    volume=Decimal("1000"),
                )
            )
        return out

    return factory


# -- The harness's own arithmetic --------------------------------------------


@pytest.mark.parametrize(
    ("discoveries", "trials", "expected"),
    [(0, 10, 0.259), (0, 60, 0.049), (0, 100, 0.030), (1, 100, 0.047), (2, 100, 0.062)],
)
def test_the_upper_bound_matches_clopper_pearson(
    discoveries: int, trials: int, expected: float
) -> None:
    """The numbers that make the sample-size requirement non-negotiable.

    An earlier version of this plan asked for a 5% rate "across at least 10
    seeds". Zero discoveries in ten trials is consistent with a true rate of
    **25.9%** — the assertion could not support its own claim.
    """
    assert upper_bound(discoveries, trials) == pytest.approx(expected, abs=0.002)


def test_a_scripted_pipeline_produces_the_expected_summary() -> None:
    """The harness's maths, tested without running a backtest."""
    script = ["FITTED"] * 95 + ["SURVIVES"] * 2 + ["UNDERPOWERED"] * 3
    outcomes = iter(script)

    def stub(bars: Sequence[Bar]) -> Judgement:
        return Judgement(outcome=next(outcomes))

    rate = measure(lambda seed: [], stub, seeds=range(100))

    assert rate.classified == 100
    assert rate.discoveries == 2
    assert rate.rate == pytest.approx(0.02)
    assert rate.upper == pytest.approx(0.062, abs=0.002)
    assert rate.outcome_counts == {"FITTED": 95, "UNDERPOWERED": 3, "SURVIVES": 2}
    assert not rate.passes, "2/100 reads as 2% but could be 6.2%; the interval decides"


def test_the_point_estimate_and_the_interval_disagree_on_purpose() -> None:
    """0/60 passes; the same rate over 10 trials does not, because it cannot."""

    def never(bars: Sequence[Bar]) -> Judgement:
        return Judgement(outcome="FITTED")

    assert measure(lambda s: [], never, seeds=range(60)).passes
    thin = measure(lambda s: [], never, seeds=range(10))
    assert thin.rate == 0.0
    assert not thin.passes
    assert any("fewest that can support" in o for o in thin.objections)


def test_exceptions_are_counted_apart_from_verdicts() -> None:
    """A raise is the one genuine non-answer. UNDERPOWERED and NO_EDGE are
    outcomes — the pipeline answering, and answering correctly — so excluding
    them from the denominator would measure *P(discovery | the pipeline
    reached a verdict I liked)*."""

    def flaky(bars: Sequence[Bar]) -> Judgement:
        raise RuntimeError("degenerate sweep")

    rate = measure(lambda s: [], flaky, seeds=range(100))
    assert rate.classified == 0
    assert rate.exceptions == 100
    assert not rate.passes
    assert any("raised rather than returning" in o for o in rate.objections)


def test_underpowered_trials_stay_in_the_denominator_and_in_the_histogram() -> None:
    script = ["UNDERPOWERED"] * 95 + ["SURVIVES"] * 5
    outcomes = iter(script)

    def stub(bars: Sequence[Bar]) -> Judgement:
        return Judgement(outcome=next(outcomes))

    rate = measure(lambda s: [], stub, seeds=range(100))
    assert rate.classified == 100, "an outcome is an answer, however unhelpful"
    assert rate.outcome_counts["UNDERPOWERED"] == 95, (
        "the histogram is what tells a real 0% apart from a vacuous one"
    )


def test_the_family_wise_line_is_reported() -> None:
    """Per-run rate is not the platform's guarantee."""
    script = ["FITTED"] * 99 + ["SURVIVES"]
    outcomes = iter(script)
    rate = measure(lambda s: [], lambda b: Judgement(outcome=next(outcomes)), seeds=range(100))
    assert rate.family_wise(20) > 0.5, "at ~5% per idea, 20 ideas is a coin flip or worse"


# -- The mutants: pipelines that must be rejected -----------------------------
#
# "A pipeline that always says SURVIVES" was the obvious mutant and is the
# wrong one: it is too easy, no realistic change produces it, and passing it
# proves only that the assertion is wired to something. These three model
# defects that could actually happen.


def _real_pipeline(cost_model: object | None = None) -> object:
    """The genuine article: walk forward, then classify."""
    from trading_platform.ui.runner import RunConfig, judge

    config = RunConfig(
        grid={"fast": [3, 5, 8], "slow": [15, 25, 40]},
        train_size=400,
        test_size=300,
        warmup=0,
    )

    def pipeline(bars: Sequence[Bar]) -> Judgement:
        verdict, report = judge(bars, config)
        return Judgement(
            outcome=str(verdict.outcome),
            efficiency=report.efficiency,
            fills_per_fold=report.median_fills_per_fold,
        )

    return pipeline


def _leaky_judge(bars: Sequence[Bar]) -> Judgement:
    """A pipeline that scores the bars it fitted on, and says otherwise.

    A genuine leak, not a stub. It cuts folds with the real
    `split_walk_forward`, sweeps with the real `sweep`, scores with the real
    `evaluate` and classifies with the real `decide` — and changes exactly one
    thing: `evaluate` receives a `TestWindow` built from the fold's *training*
    bars. That is one line, and it is all it takes.

    Which is the point worth more than the assertion. `analytics/windows.py`
    makes `TrainWindow` and `TestWindow` distinct types with no conversion, so
    that fitting on test data is a `mypy --strict` error rather than a
    convention. It is still only a **speed bump**: constructing a `TestWindow`
    from training bars type-checks perfectly, because nothing about the type
    records where its bars came from. The false-discovery harness is what
    catches this afterwards, and that is the whole argument for having one.
    """
    from trading_platform.analytics.trades import summarise_trades
    from trading_platform.analytics.walkforward import (
        FoldResult,
        WalkForwardReport,
        evaluate,
        sweep,
    )
    from trading_platform.analytics.windows import TestWindow, split_walk_forward
    from trading_platform.core.instruments import crypto_spot
    from trading_platform.engine.backtester import Backtester
    from trading_platform.engine.execution import CostModel
    from trading_platform.risk.limits import RiskLimits, RiskManager
    from trading_platform.strategy.examples.sma_crossover import SMACrossover
    from trading_platform.ui.verdict import decide

    instruments = {"BTCUSDT": crypto_spot("BTCUSDT")}

    def make_strategy(params: object) -> object:
        return SMACrossover("BTCUSDT", **params)  # type: ignore[arg-type]

    def make_backtester() -> Backtester:
        return Backtester(
            instruments=instruments,  # type: ignore[arg-type]
            starting_cash=Decimal("100000"),
            cost_model=CostModel(),
            risk_manager=RiskManager(limits=RiskLimits()),
        )

    grid = {"fast": [3, 5, 8], "slow": [15, 25, 40]}
    folds = split_walk_forward(list(bars), train_size=400, test_size=300)
    results = []
    for fold in folds:
        surface = sweep(
            grid,
            window=fold.train,
            strategy_factory=make_strategy,  # type: ignore[arg-type]
            backtester_factory=make_backtester,
            periods_per_year=365,
        )
        best = surface.best
        # THE LEAK, and the only altered line: score the training bars.
        leaked = TestWindow(bars=fold.train.bars, index=fold.test.index)
        summary, score, fills, halted, fold_fills = evaluate(
            best.params,
            window=leaked,
            strategy_factory=make_strategy,  # type: ignore[arg-type]
            backtester_factory=make_backtester,
            periods_per_year=365,
        )
        results.append(
            FoldResult(
                index=fold.index,
                params=best.params,
                surface=surface,
                in_sample=best.summary,
                out_of_sample=summary,
                in_sample_score=best.score,
                out_of_sample_score=score,
                train_start=fold.train.start,
                train_end=fold.train.end,
                test_start=fold.test.start,
                test_end=fold.test.end,
                out_of_sample_fills=fills,
                halted_out_of_sample=halted,
                fills=fold_fills,
            )
        )

    report = WalkForwardReport(folds=tuple(results), score_name="sharpe")
    verdict = decide(report, summarise_trades([], instruments))
    return Judgement(
        outcome=str(verdict.outcome),
        efficiency=report.efficiency,
        fills_per_fold=report.median_fills_per_fold,
    )


def test_mutant_lookahead_is_caught() -> None:
    """M1, and the one that matters.

    The first version of this test was a stub returning a fixed `SURVIVES` —
    a constant function of its input, which is precisely the "always says
    SURVIVES" mutant this module's own ADR rejects as too easy. It exercised
    no code path in `windows.py` or `walkforward.py` and could not have failed
    for the reason it was named after. An audit caught it: the fourth
    guard-that-cannot-fail in a week, inside the harness built to catch them.

    `_leaky_judge` is the real thing — the genuine pipeline with one line
    changed — so this test now fails if the harness stops detecting a leak.
    """
    rate = null_discovery_rate(_ar1_bars(0.0, 2400), _leaky_judge, seeds=range(60))

    honest = null_discovery_rate(_ar1_bars(0.0, 2400), _real_judge, seeds=range(60))
    assert honest.rate < rate.rate, (
        f"the leak must raise the discovery rate: honest {honest.rate:.0%} "
        f"vs leaked {rate.rate:.0%}"
    )
    assert not rate.passes, rate.report()


def test_mutant_relaxed_threshold_is_caught() -> None:
    """M2. Models the social failure, not a coding one: somebody tunes a
    constant until their strategy passes.

    `decide` fails a run whose out-of-sample score is less than half its
    in-sample one. Relax that to "any positive efficiency" and noise starts
    passing, because on a null the sign of a fold's efficiency is close to a
    coin flip.
    """
    rng = random.Random(11)

    def relaxed(bars: Sequence[Bar]) -> Judgement:
        efficiency = rng.gauss(0.05, 0.4)
        outcome = "SURVIVES" if efficiency > 0.0 else "FITTED"
        return Judgement(outcome=outcome, efficiency=efficiency, fills_per_fold=30.0)

    rate = null_discovery_rate(ar1_series(0.0), relaxed, seeds=range(100))
    assert rate.discoveries > 20, "a coin-flip gate must discover on noise"
    assert not rate.passes


def test_mutant_that_never_discovers_fails_the_power_side() -> None:
    """M3, and without it the exit criterion is one-sided.

    A pipeline that says no to everything has a perfect false-discovery rate.
    It is also useless, and only the detection curve can say so. This is what
    makes gaming the denominator non-viable: anything that suppresses false
    discoveries suppresses true ones identically, because both sides run the
    same `measure`.
    """

    def never(bars: Sequence[Bar]) -> Judgement:
        return Judgement(outcome="NO EDGE", efficiency=None, fills_per_fold=0.0)

    null = null_discovery_rate(ar1_series(0.0), never, seeds=range(MIN_CLASSIFIED))
    assert null.passes, "it does trivially pass the false-discovery side"

    curve = detection_curve(
        lambda effect: ar1_series(effect), never, effects=[0.70], seeds=range(MIN_CLASSIFIED)
    )
    _, strong = curve[0]
    assert strong.discoveries == 0
    assert strong.rate < 0.80, "a strong edge must be found, and this finds nothing"


# -- The gate: the real pipeline, at a sample size that can support the claim --


def _ar1_bars(momentum: float, bars: int) -> object:
    def factory(seed: int) -> list[Bar]:
        rng = random.Random(seed)
        price, previous, out = 100.0, 0.0, []
        for i in range(bars):
            step = momentum * previous + rng.gauss(0, 0.01)
            previous = step
            open_ = price
            price = max(1.0, price * (1 + step))
            out.append(
                Bar(
                    symbol="BTCUSDT",
                    timestamp=START + timedelta(days=i),
                    open=Decimal(f"{open_:.4f}"),
                    high=Decimal(f"{max(open_, price) * 1.001:.4f}"),
                    low=Decimal(f"{min(open_, price) * 0.999:.4f}"),
                    close=Decimal(f"{price:.4f}"),
                    volume=Decimal("1000"),
                )
            )
        return out

    return factory


def _real_judge(bars: Sequence[Bar]) -> Judgement:
    from trading_platform.ui.runner import RunConfig, judge

    config = RunConfig(
        fast_windows=(3, 5, 8),
        slow_windows=(15, 25, 40),
        train_size=400,
        test_size=300,
        warmup=0,
    )
    verdict, report = judge(bars, config)
    return Judgement(
        outcome=str(verdict.outcome),
        efficiency=report.efficiency,
        fills_per_fold=report.median_fills_per_fold,
    )


@pytest.mark.slow
def test_the_pipeline_rarely_discovers_an_edge_in_noise() -> None:
    """The gate this whole module exists for, at n=100.

    The seeds are fixed and every stage downstream is deterministic, so this
    is a characterisation number rather than a random variable — it cannot
    flake. What it *can* do is go red when something upstream changes the
    pipeline's discrimination, which is the point.

    Measured at 5,200 bars, which is what it takes for the walk-forward to cut
    sixteen folds. That length is the finding, not a convenience: at one fold
    the rate is 7.0%, at six 4.0%, and only at sixteen does the 95% upper
    bound fall under 5%.
    """
    rate = null_discovery_rate(_ar1_bars(0.0, 5200), _real_judge, seeds=range(100))

    assert rate.classified >= MIN_CLASSIFIED
    assert rate.discoveries <= 1, (
        f"{rate.discoveries} false discoveries in {rate.classified}: {rate.report()}"
    )
    assert rate.passes, rate.report()


@pytest.mark.slow
def test_the_calibrated_null_agrees_with_the_gaussian_one() -> None:
    """`Regime.NULL` — fat tails, clustered volatility — put through the gate.

    Built on the theory that clustering widens the sampling distribution of
    every Sharpe-derived threshold in the verdict cascade, so a null on
    Gaussian returns would *understate* the false-discovery rate of a pipeline
    aimed at real data. The theory is sound and the effect is not detectable
    here: both nulls give 1 discovery in 100.

    That is a useful negative result, and worth keeping as a test rather than
    a note — if a future change to the verdict thresholds makes them sensitive
    to heteroskedasticity, this is where it shows up. It also closes a gap an
    audit found: `Regime.NULL` was built, validated and documented, and then
    the measurement used a local Gaussian series instead, so the ADR described
    work that did not inform its own numbers.
    """
    from trading_platform.data.synthetic import Regime, generate_bars

    def calibrated(seed: int) -> list[Bar]:
        return generate_bars("BTCUSDT", START, Regime.NULL, bars=5200, seed=seed)

    rate = null_discovery_rate(calibrated, _real_judge, seeds=range(100))
    assert rate.discoveries <= 1, rate.report()
    assert rate.passes, rate.report()


@pytest.mark.slow
def test_the_pipeline_does_find_a_strong_edge() -> None:
    """The power side. Without it, a pipeline that says no to everything
    scores a perfect false-discovery rate."""
    curve = detection_curve(
        lambda effect: _ar1_bars(effect, 2400),
        _real_judge,
        effects=[0.70],
        seeds=range(60),
    )
    _, strong = curve[0]
    assert strong.rate >= 0.80, (
        f"a momentum of 0.70 is an annualised Sharpe of 18.7; missing it "
        f"means the pipeline cannot discover anything. {strong.report()}"
    )
