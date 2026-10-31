"""Measure whether the evaluation pipeline can tell signal from noise.

    .venv/bin/python scripts/measure_null.py [--trials N] [--quick]

Produces the numbers in `docs/adr/0003-null-harness.md`. Run it before
believing them.

Two arms, because one is not a measurement:

- **The null arm.** Series with no directional predictability by
  construction. How often does the pipeline say `SURVIVES`? Every discovery
  here is false.
- **The detection arm.** The same series with a tunable amount of real
  predictability. How large must an edge be before the pipeline sees it?

A harness measuring only the first grades a pipeline that says "no" to
everything as perfect. Only the pair says whether it discriminates.

The effect size is reported in annualised Sharpe rather than as an AR(1)
coefficient, because a coefficient transfers to nothing: for
`r_t = m*r_(t-1) + e`, the best an optimal linear forecast can do is
`m / sqrt(1 - m^2)` per bar, annualised by `sqrt(365)`.
"""

from __future__ import annotations

import argparse
import math
import random
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_platform.analytics.nulltest import (
    DiscoveryRate,
    Judgement,
    detection_curve,
    null_discovery_rate,
)
from trading_platform.core.events import Bar
from trading_platform.data.synthetic import Regime, generate_bars
from trading_platform.ui.runner import RunConfig, judge

START = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOL = "BTCUSDT"

#: Long enough to leave roughly 4,800 bars out of sample, which is what it
#: takes for the false-discovery rate to be demonstrably below 5%. It is the
#: out-of-sample VOLUME that matters, not the fold count: holding this length
#: fixed and varying `test_size` from 300 to 2,400 — sixteen folds down to two
#: — leaves the rate flat at 0-1%. See `docs/adr/0003-null-harness.md`, which
#: records a fold-count claim that was confounded and withdrawn.
BARS = 5200

#: Effect sizes to probe, as AR(1) coefficients.
EFFECTS = (0.0, 0.15, 0.25, 0.35, 0.45, 0.55, 0.70)

CONFIG = RunConfig(
    fast_windows=(3, 5, 8),
    slow_windows=(15, 25, 40),
    train_size=400,
    test_size=300,
    warmup=0,
)


def implied_sharpe(momentum: float) -> float:
    """The most an optimal linear forecast could extract, annualised."""
    if momentum <= 0:
        return 0.0
    return momentum / math.sqrt(1 - momentum**2) * math.sqrt(365)


def ar1_factory(momentum: float, bars: int = BARS):  # type: ignore[no-untyped-def]
    """Returns with a known, tunable amount of directional predictability."""

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
                    symbol=SYMBOL,
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


def calibrated_null_factory(bars: int = BARS):  # type: ignore[no-untyped-def]
    """`Regime.NULL`: no directional structure, but fat tails and clustered
    volatility from the GARCH machinery.

    The second null, and the comparison is the point. An earlier version of
    this script measured only the Gaussian AR(1) series above — while
    `Regime.NULL`'s own code comment warned that *"a null built on Gaussian
    returns would understate the false-discovery rate of a pipeline that will
    be run on real data"*. It was built, validated, documented, and then not
    used by the measurement it exists for. An audit caught that.

    Clustering adds no directional edge — the sign path is still a martingale
    — but it widens the sampling distribution of every Sharpe-derived
    threshold in the verdict cascade. If this arm's rate exceeds the Gaussian
    one, those thresholds are miscalibrated under conditions real data
    always has.
    """

    def factory(seed: int) -> list[Bar]:
        return generate_bars(SYMBOL, START, Regime.NULL, bars=bars, seed=seed)

    return factory


def pipeline(bars: Sequence[Bar]) -> Judgement:
    """The real thing — the same `judge` the UI runs."""
    verdict, report = judge(bars, CONFIG)
    return Judgement(
        outcome=str(verdict.outcome),
        efficiency=report.efficiency,
        fills_per_fold=report.median_fills_per_fold,
    )


def fold_sensitivity(trials: int) -> None:
    """The rate is not one number; it depends on how many folds there are."""
    print(f"\n{'=' * 74}\nFALSE DISCOVERY RATE vs OUT-OF-SAMPLE EVIDENCE\n{'=' * 74}")
    print(f"{'bars':>7}{'discoveries':>13}{'rate':>8}{'95% upper':>11}   top outcomes")
    for bars in (900, 1500, 2400, 3600, 5200):
        rate = null_discovery_rate(ar1_factory(0.0, bars), pipeline, seeds=range(trials))
        top = dict(list(rate.outcome_counts.items())[:3])
        print(f"{bars:>7}{rate.discoveries:>13}{rate.rate:>8.1%}{rate.upper:>11.1%}   {top}")
    print(
        "\nThe rate falls with out-of-sample VOLUME. It does NOT fall with fold\n"
        "count: holding the series at 5,200 bars and varying test_size from 300\n"
        "to 2,400 — sixteen folds down to two, same 4,800 out-of-sample bars —\n"
        "leaves the rate flat at 0-1%.\n\n"
        "So splitting the same data more finely does not buy a more trustworthy\n"
        "verdict. A first version of this claimed it did; that measurement grew\n"
        "the series with the test window fixed and moved both variables at once.\n"
        "See docs/adr/0003-null-harness.md."
    )


def null_arm(trials: int) -> DiscoveryRate:
    """Two nulls, because the delta between them is itself a finding."""
    print(f"\n{'=' * 74}\nTHE NULL ARM — every discovery here is false\n{'=' * 74}")

    gaussian = null_discovery_rate(
        ar1_factory(0.0), pipeline, seeds=range(trials), label=f"Gaussian i.i.d., {BARS} bars"
    )
    print(gaussian.report())

    calibrated = null_discovery_rate(
        calibrated_null_factory(),
        pipeline,
        seeds=range(trials),
        label=f"Regime.NULL — fat tails, clustered volatility, {BARS} bars",
    )
    print()
    print(calibrated.report())

    print(
        f"\n  Gaussian {gaussian.rate:.1%} against calibrated {calibrated.rate:.1%}.\n"
        "  If the calibrated arm is materially worse, the verdict thresholds are\n"
        "  miscalibrated under conditions real data always has — and a null test\n"
        "  run only on Gaussian returns would have said everything was fine."
    )
    # The calibrated arm is the one that governs, because it is the one that
    # resembles what the pipeline will actually be pointed at.
    return calibrated


def detection_arm(trials: int) -> None:
    print(f"\n{'=' * 74}\nTHE DETECTION ARM — how big must a real edge be?\n{'=' * 74}")
    print(f"{'momentum':>10}{'implied Sharpe':>17}{'found':>8}{'rate':>8}")
    curve = detection_curve(ar1_factory, pipeline, effects=EFFECTS, seeds=range(trials))
    for effect, rate in curve:
        print(
            f"{effect:>10.2f}{implied_sharpe(effect):>17.2f}{rate.discoveries:>8}{rate.rate:>8.0%}"
        )
    print(
        "\nRead the middle rows. An edge worth an annualised Sharpe of 5 is\n"
        "invisible to this pipeline. For scale, `data/calibration.py` caps its\n"
        "own generator at MAX_REGIME_SHARPE = 3.5 on the grounds that anything\n"
        "larger 'hands every strategy an edge that does not exist' — so the\n"
        "detection floor sits above the ceiling the generator will emit.\n"
        "(Different quantities — a drift Sharpe against a timing Sharpe — so\n"
        "this is a sanity anchor, not a proof.)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument(
        "--quick", action="store_true", help="20 trials and no fold sweep; for a smoke check"
    )
    args = parser.parse_args()
    trials = 20 if args.quick else args.trials

    started = time.perf_counter()
    rate = null_arm(trials)
    if not args.quick:
        fold_sensitivity(trials)
    detection_arm(trials)

    print(f"\n{'=' * 74}\nWHAT THIS MEANS\n{'=' * 74}")
    print(
        f"Per idea, the false-discovery rate is at most {rate.upper:.1%}.\n"
        f"Across 20 independent ideas that is a {rate.family_wise(20):.0%} chance of at\n"
        "least one false pass — which is why a trial registry is the next thing\n"
        "to build and not an optional extra."
    )
    if not rate.passes:
        print("\nThe null arm did NOT pass:")
        for objection in rate.objections:
            print(f"  - {objection}")
    print(f"\n[{time.perf_counter() - started:.0f}s, {trials} trials per point]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
