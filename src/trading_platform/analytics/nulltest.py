"""Measuring whether the evaluation pipeline can tell signal from noise.

`CLAUDE.md` says a backtest's job is to disappoint you. This is the thing that
checks the machinery does that — by running the whole pipeline over data with
**no predictability by construction** and counting how often it says the
strongest word it has. Until that count is small, no number any later stage
reports means anything.

**It is two-sided, and that is not decoration.** A harness measuring only the
false-discovery rate grades a pipeline that says "no" to everything as perfect.
Measured on this project's own pipeline: the false-discovery rate is 1 in 100,
and `SMACrossover` through it is **blind to any real edge below roughly
annualised Sharpe 5**. Only the pair of numbers says whether it discriminates
— and that second figure names the strategy deliberately, because without an
oracle arm to compare against there is no way to tell a conservative verdict
layer from a weak detector. So
`null_discovery_rate` and `detection_curve` are the same function with the same
classification policy, differing only in the series handed to it — anything
that suppresses false discoveries suppresses true ones identically, and the
power side fires.

**On the denominator.** A first design excluded UNDERPOWERED and NO_EDGE from
it as "vacuous". That was a category error: those are `Outcome`s, the pipeline
answering, and answering correctly. Excluding them measures
*P(discovery | null, the pipeline reached a verdict I liked)*. Only a raised
exception is a genuine non-answer. So the denominator is every trial that
returned an outcome, exceptions are counted separately with their own gate, and
the **full outcome histogram is reported** — because without it "0% discovery"
cannot be told apart from ninety-five trials that never traded.

**On sample size.** With zero discoveries in *n* trials the exact one-sided 95%
upper bound on the true rate is roughly 3/n:

    0 of  10  ->  the rate could still be 25.9%
    0 of  60  ->  4.9%
    0 of 100  ->  3.0%
    1 of 100  ->  4.7%

Ten seeds cannot demonstrate a 5% rate, which is what an earlier version of
this plan asked for. The harness therefore reports the interval rather than the
point estimate, and refuses to draw a conclusion from too few trials.

This module never imports `trading_platform.ui`: that would make
`analytics -> ui -> analytics`. The pipeline is injected, which is also what
lets a deliberately broken one be substituted to prove these tests can fail.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from trading_platform.core.events import Bar

__all__ = [
    "MIN_CLASSIFIED",
    "DiscoveryRate",
    "Judgement",
    "Trial",
    "detection_curve",
    "measure",
    "null_discovery_rate",
    "upper_bound",
]

#: The word the pipeline says when it failed to falsify an edge. A discovery.
DISCOVERY = "SURVIVES"

#: Below this many classified trials the harness draws no conclusion. Chosen
#: from the binomial: 0 of 60 bounds the true rate at 4.9%, which is the
#: smallest sample that can support a 5% claim at all.
MIN_CLASSIFIED = 60

#: Above this share of trials raising rather than returning, the configuration
#: is broken and the measurement is about the configuration, not the pipeline.
MAX_EXCEPTION_RATE = 0.10

#: What the harness is asked to demonstrate about noise.
TARGET_RATE = 0.05

Failure = Literal["exception"]


@dataclass(frozen=True, slots=True)
class Judgement:
    """What one run of the pipeline concluded. Deliberately not `Outcome`.

    A plain string, so this module does not import the layer that defines the
    enum. `Outcome` is a `StrEnum`, so `str(outcome)` round-trips exactly.
    """

    outcome: str
    efficiency: float | None = None
    fills_per_fold: float = 0.0


#: Bars in, judgement out. Injected so a broken pipeline can be substituted.
Pipeline = Callable[[Sequence[Bar]], Judgement]

#: Seed in, bars out.
SeriesFactory = Callable[[int], Sequence[Bar]]


@dataclass(frozen=True, slots=True)
class Trial:
    """One seed's result, including the ways it can fail to be one."""

    seed: int
    #: `None` only when the pipeline raised.
    outcome: str | None
    failure: Failure | None = None
    detail: str = ""
    efficiency: float | None = None
    fills_per_fold: float = 0.0

    @property
    def classified(self) -> bool:
        return self.outcome is not None

    @property
    def discovered(self) -> bool:
        return self.outcome == DISCOVERY


def upper_bound(discoveries: int, trials: int, alpha: float = 0.05) -> float:
    """Exact one-sided upper confidence bound on a binomial rate.

    Clopper-Pearson by bisection, using `math.comb`. Stdlib only — `pyproject`
    declares no numpy or scipy, and adding one for a single inverse CDF would
    be a dependency bought for a convenience.
    """
    if trials <= 0:
        return 1.0
    if discoveries >= trials:
        return 1.0
    low, high = 0.0, 1.0
    for _ in range(200):
        mid = (low + high) / 2
        cdf = sum(
            math.comb(trials, i) * mid**i * (1 - mid) ** (trials - i)
            for i in range(discoveries + 1)
        )
        if cdf > alpha:
            low = mid
        else:
            high = mid
    return high


@dataclass(frozen=True, slots=True)
class DiscoveryRate:
    """How often the pipeline said the strongest word it has."""

    trials: tuple[Trial, ...]
    label: str = ""

    @property
    def classified(self) -> int:
        return sum(1 for t in self.trials if t.classified)

    @property
    def exceptions(self) -> int:
        return sum(1 for t in self.trials if t.failure is not None)

    @property
    def discoveries(self) -> int:
        return sum(1 for t in self.trials if t.discovered)

    @property
    def rate(self) -> float:
        """The point estimate. Read `upper` instead; this is the number that
        fools people, for the same reason `WalkForwardReport` leads with
        `efficiency` rather than the raw out-of-sample score."""
        return self.discoveries / self.classified if self.classified else 0.0

    @property
    def upper(self) -> float:
        """95% upper bound on the true rate. The honest headline."""
        return upper_bound(self.discoveries, self.classified)

    @property
    def outcome_counts(self) -> Mapping[str, int]:
        """Every verdict, in order of frequency.

        The output whose absence would make a 0% rate unattributable: it is
        what distinguishes a pipeline that discriminated from one where
        ninety-five trials never traded.
        """
        counts: dict[str, int] = {}
        for trial in self.trials:
            if trial.outcome is not None:
                counts[trial.outcome] = counts.get(trial.outcome, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def family_wise(self, ideas: int = 20) -> float:
        """Probability that `ideas` independent attempts yield one false pass.

        Per-run rate is not the platform's guarantee. At 5% per idea, twenty
        ideas produce a false discovery 64% of the time — which is what makes
        a trial registry the next thing to build, not an optional extra.
        """
        return 1.0 - (1.0 - self.upper) ** ideas

    @property
    def objections(self) -> tuple[str, ...]:
        """What is wrong with this measurement, if anything."""
        out: list[str] = []
        if self.classified < MIN_CLASSIFIED:
            out.append(
                f"{self.classified} trials reached a verdict, out of "
                f"{len(self.trials)} run. {MIN_CLASSIFIED} is the fewest that can "
                f"support a {TARGET_RATE:.0%} claim at all: zero discoveries in 60 "
                "bounds the true rate at 4.9%, and in 10 it bounds it at 25.9%."
            )
        if self.trials and self.exceptions / len(self.trials) > MAX_EXCEPTION_RATE:
            out.append(
                f"{self.exceptions} of {len(self.trials)} trials raised rather than "
                "returning a verdict. Above one in ten, this measures the "
                "configuration rather than the pipeline."
            )
        if self.classified and self.upper > TARGET_RATE:
            out.append(
                f"the discovery rate could be as high as {self.upper:.1%} "
                f"({self.discoveries}/{self.classified} observed), against a target "
                f"of {TARGET_RATE:.0%}."
            )
        return tuple(out)

    @property
    def passes(self) -> bool:
        return not self.objections

    def report(self) -> str:
        lines = [
            f"{self.label or 'discovery rate'}",
            f"  trials        : {len(self.trials)} ({self.classified} classified, "
            f"{self.exceptions} raised)",
            f"  discoveries   : {self.discoveries}",
            f"  rate          : {self.rate:.1%}  (95% upper bound {self.upper:.1%})",
            f"  outcomes      : {self.outcome_counts}",
            f"  20 ideas      : {self.family_wise():.0%} chance of one false pass",
        ]
        for objection in self.objections:
            lines.append(f"  OBJECTION     : {objection}")
        return "\n".join(lines)


def measure(
    series_factory: SeriesFactory,
    pipeline: Pipeline,
    *,
    seeds: Sequence[int],
    label: str = "",
) -> DiscoveryRate:
    """Run the pipeline once per seed and count what it said.

    The single entry point. `null_discovery_rate` and `detection_curve` are
    thin wrappers over it with no policy of their own, so the two sides of the
    test cannot drift apart — which is what stops a pipeline gaming the
    measurement by degenerating every trial.
    """
    trials: list[Trial] = []
    for seed in seeds:
        bars = series_factory(seed)
        try:
            judgement = pipeline(bars)
        except Exception as exc:
            trials.append(
                Trial(seed=seed, outcome=None, failure="exception", detail=f"{type(exc).__name__}")
            )
            continue
        trials.append(
            Trial(
                seed=seed,
                outcome=judgement.outcome,
                efficiency=judgement.efficiency,
                fills_per_fold=judgement.fills_per_fold,
            )
        )
    return DiscoveryRate(trials=tuple(trials), label=label)


def null_discovery_rate(
    series_factory: SeriesFactory,
    pipeline: Pipeline,
    *,
    seeds: Sequence[int],
    label: str = "null",
) -> DiscoveryRate:
    """How often the pipeline discovers an edge in data that has none."""
    return measure(series_factory, pipeline, seeds=seeds, label=label)


def detection_curve(
    factory_for: Callable[[float], SeriesFactory],
    pipeline: Pipeline,
    *,
    effects: Sequence[float],
    seeds: Sequence[int],
) -> tuple[tuple[float, DiscoveryRate], ...]:
    """The same measurement at increasing effect sizes.

    Without this the harness is one-sided and a pipeline that never discovers
    anything scores perfectly.
    """
    return tuple(
        (effect, measure(factory_for(effect), pipeline, seeds=seeds, label=f"effect={effect:g}"))
        for effect in effects
    )
