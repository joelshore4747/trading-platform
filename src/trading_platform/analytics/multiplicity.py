"""What to believe about the best of N trials.

A sweep reports its winner. The winner is, by construction, the maximum of a
sample — and the maximum of N draws from a distribution centred on zero is not
centred on zero. Search hard enough over noise and something will look good;
that is arithmetic, not bad luck.

One statistic here. The **Deflated Sharpe Ratio** (Bailey and López de Prado,
2014) asks: given that this Sharpe is the best of N, and given how skewed and
fat-tailed its returns are, what is the probability the true Sharpe exceeds
zero? It deflates the threshold by the expected maximum under the null rather
than deflating the estimate. The trial count that matters is the one
`SweepSurface.attempted` records — cells the strategy rejected still counted as
shots taken — so `attempted` is a parameter here rather than inferred from the
length of the scores.

**It is reported, never gated on.** The walk-forward's own `efficiency` is
already multiplicity-resistant: selecting across more cells raises the
in-sample score, which is the denominator, so efficiency falls. DSR answers the
question efficiency cannot — *how much of the in-sample figure was selection* —
and in Sharpe units, against a null model, where `efficiency`'s 0.5 threshold
is a convention with no derivation anywhere in this codebase. That is worth
having. It is not worth another gate on a pipeline that already rejects
almost everything.

**On the Probability of Backtest Overfitting, which was written here and then
deleted.** CSCV does not re-run a backtest per split: it computes each trial's
returns once over the full sample, then slices and reconcatenates blocks. This
engine is path-dependent — warm-up, position state, compounding, and a
`RiskManager` whose drawdown halt carries peak-equity state that `walk_forward`
is careful to rebuild per fold. Returns over a recombined training set are not
returns any strategy here would have produced, so PBO would grade a synthetic
object. Measured before it was removed, on white noise through the real engine:
PBO landed *below* its own 0.5 threshold in 8 of 12 seeds — it passed pure
noise two thirds of the time — and it moved the wrong way as a real edge was
planted, rising from 50.3% at zero momentum to 60.3% at an implied annualised
Sharpe of 18.7. The theory and the measurement agreed, so it went. ADR 0004
has the numbers.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "EULER_MASCHERONI",
    "DeflatedSharpe",
    "deflated_sharpe_ratio",
    "expected_max_sharpe",
    "probabilistic_sharpe_ratio",
]

EULER_MASCHERONI = 0.5772156649015329

_NORMAL = statistics.NormalDist()


def expected_max_sharpe(trial_sharpes: Sequence[float], *, attempted: int | None = None) -> float:
    """The Sharpe you should expect from the best of N trials on pure noise.

    This is the benchmark a winner has to beat, and it is not zero. For N
    independent draws whose Sharpes have standard deviation `s`, the expected
    maximum is approximately

        s * [ (1 - g) * Z_inv(1 - 1/N) + g * Z_inv(1 - 1/(N*e)) ]

    with `g` the Euler-Mascheroni constant. There is no mean term: the null is
    the paper's, that every trial has a true Sharpe of zero. Adding the sample
    mean of the trials would make the benchmark circular and much harsher.

    `attempted` is the count that goes into the `Z_inv` terms — pass
    `SweepSurface.attempted` when cells the strategy rejected should still
    count as shots taken. It defaults to the number of scores supplied.

    **The dispersion is a deliberate deviation from the literature.** The paper
    takes `s` from the trials and assumes N *independent* ones; the prescribed
    correction for a grid of near-identical cells is to shrink N, via the mean
    pairwise correlation of the trials' return series. This shrinks `s`
    instead, which moves the right way — a degenerate grid does have low
    cross-sectional dispersion — and needs only the scalar scores, which is all
    a `SweepSurface` retains. It is not the same estimator and it is not
    conservative in a known direction: `s` scales the benchmark linearly while
    N moves it through `Z_inv`, which is very flat, so on a tight grid this
    deflates harder than the published lever would.
    """
    n = attempted if attempted is not None else len(trial_sharpes)
    if n < 2 or len(trial_sharpes) < 2:
        return 0.0
    spread = statistics.stdev(trial_sharpes)
    if spread == 0:
        return 0.0
    first = _NORMAL.inv_cdf(1 - 1 / n)
    second = _NORMAL.inv_cdf(1 - 1 / (n * math.e))
    return spread * ((1 - EULER_MASCHERONI) * first + EULER_MASCHERONI * second)


def probabilistic_sharpe_ratio(
    sharpe: float,
    *,
    periods: int,
    skew: float = 0.0,
    excess_kurtosis: float = 0.0,
    benchmark: float = 0.0,
) -> float:
    """Probability the true Sharpe exceeds `benchmark`, given the estimate.

    `sharpe` and `benchmark` are **per-period**, not annualised. Mixing the
    two scales is the easy mistake: the `sqrt(periods - 1)` term below already
    does the work annualisation would, so feeding it an annualised Sharpe
    inflates the result by roughly the square root of the periods per year.

    The denominator is what makes this more than a t-test. A Sharpe estimator's
    own standard error widens with negative skew and with fat tails, so two
    strategies reporting the same Sharpe are not equally believable if one of
    them earns it by selling insurance.
    """
    if periods < 2:
        return 0.0
    # A per-period Sharpe above 1.0 is a mean bar return exceeding its own
    # standard deviation — roughly annualised Sharpe 19 at daily bars. It is
    # almost always an annualised figure fed in by mistake, and this repository
    # makes that mistake easy: `SweepCell.score` is annualised, so the obvious
    # wiring is wrong. The consequence is not a wrong number but a silently
    # constant one — at z of about 15 the normal CDF is 1.0 to machine
    # precision, so every strategy `survives`. That is the worst possible
    # failure for a statistic whose job is to disappoint you.
    if abs(sharpe) > 1.0 or abs(benchmark) > 1.0:
        raise ValueError(
            f"Sharpe {sharpe:.3f} (benchmark {benchmark:.3f}) is per-period, and a "
            "per-period Sharpe above 1.0 is almost certainly an annualised figure. "
            "Divide by sqrt(periods per year) — `SweepCell.score` is annualised."
        )
    # The published denominator is 1 - g3*SR + ((g4 - 1)/4)*SR^2 with g4 the
    # RAW kurtosis. This module carries excess kurtosis (0 for a Gaussian),
    # and raw = excess + 3, so (raw - 1)/4 = (excess + 2)/4 — not excess/4,
    # which is what I wrote first. At a Gaussian that error drops the SR^2
    # term entirely instead of giving it a coefficient of 0.5, so it
    # understates the estimator's variance and overstates confidence, by more
    # as the Sharpe grows.
    variance = 1 - skew * sharpe + ((excess_kurtosis + 2.0) / 4.0) * sharpe**2
    if variance <= 0:
        # Unreachable for moments that describe a real distribution. The
        # denominator equals (SR^2/4) * Var(Z^2 - (2/SR)Z), so it is
        # non-negative whenever the classical moment inequality
        # `raw kurtosis >= skew^2 + 1` holds — which it does for any
        # distribution, including an empirical one. Reaching here means the
        # moments passed in are not jointly possible.
        #
        # It raises rather than returning 0.0 because a probability of zero
        # asserts the true Sharpe is certainly below benchmark, which is a
        # fabricated certainty — and inside `DeflatedSharpe` it sets
        # `survives=False`, so it would masquerade as a legitimate rejection.
        raise ValueError(
            f"skew {skew:+.3f} and excess kurtosis {excess_kurtosis:+.3f} give a "
            "non-positive estimator variance. No distribution has these moments: "
            "they violate raw kurtosis >= skew^2 + 1."
        )
    return float(_NORMAL.cdf((sharpe - benchmark) * math.sqrt(periods - 1) / math.sqrt(variance)))


@dataclass(frozen=True, slots=True)
class DeflatedSharpe:
    """A winner's Sharpe, and how much of it the search explains."""

    #: Per-period Sharpe of the selected trial.
    observed: float
    #: Expected best-of-N under the null. The bar the winner must clear.
    expected_max: float
    #: Probability the true Sharpe exceeds `expected_max`.
    probability: float
    trials: int
    periods: int
    skew: float
    excess_kurtosis: float

    @property
    def survives(self) -> bool:
        """Convention from the literature: 95% confidence."""
        return self.probability > 0.95

    def report(self) -> str:
        return "\n".join(
            [
                f"Deflated Sharpe over {self.trials} trials, {self.periods} periods",
                f"  observed (per period) : {self.observed:+.4f}",
                f"  expected best of {self.trials:<4}: {self.expected_max:+.4f}",
                f"  skew / excess kurtosis: {self.skew:+.2f} / {self.excess_kurtosis:+.2f}",
                f"  P(true Sharpe > expected best) = {self.probability:.1%}"
                f"  -> {'survives' if self.survives else 'not distinguishable from search'}",
            ]
        )


def deflated_sharpe_ratio(
    trial_sharpes: Sequence[float],
    *,
    selected: float | None = None,
    attempted: int | None = None,
    periods: int,
    skew: float = 0.0,
    excess_kurtosis: float = 0.0,
) -> DeflatedSharpe:
    """Deflate the best trial's Sharpe by what searching would have produced.

    All Sharpes are **per-period**. `selected` defaults to the maximum, which
    is what a sweep reports; pass it explicitly when the chosen cell was not
    the top-scoring one. `attempted` defaults to the number of scores; pass
    `SweepSurface.attempted` to count cells the strategy rejected as shots
    taken, since `len(cells)` is smaller than the grid as written.
    """
    if not trial_sharpes:
        raise ValueError("Deflating a Sharpe needs the trials it was selected from")
    best = max(trial_sharpes) if selected is None else selected
    benchmark = expected_max_sharpe(trial_sharpes, attempted=attempted)
    return DeflatedSharpe(
        observed=best,
        expected_max=benchmark,
        probability=probabilistic_sharpe_ratio(
            best,
            periods=periods,
            skew=skew,
            excess_kurtosis=excess_kurtosis,
            benchmark=benchmark,
        ),
        trials=attempted if attempted is not None else len(trial_sharpes),
        periods=periods,
        skew=skew,
        excess_kurtosis=excess_kurtosis,
    )
