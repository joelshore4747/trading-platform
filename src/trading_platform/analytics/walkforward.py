"""Parameter sweeps and the walk-forward harness.

Two failure modes are targeted here, and they are different:

1. **Reporting an in-sample number as a result.** Handled by
   ``analytics.windows``: the harness fits only on ``TrainWindow`` and scores
   only on ``TestWindow``.
2. **Finding a coincidence and calling it an edge.** Handled by ``sweep``,
   which reports the whole parameter surface rather than its peak. The peak is
   the number that fools people; the shape around it is the evidence. A broad
   plateau is a real effect. A lone spike surrounded by losses is a lucky cell
   in a grid, and reporting only that cell converts noise into a strategy.

The headline of a ``WalkForwardReport`` is deliberately not the out-of-sample
score. It is ``efficiency`` — out-of-sample over in-sample — because the *gap*
is the finding. A strategy scoring 2.1 in-sample and 0.3 out is a more useful
result than one scoring 0.8 twice, and the report should say so.
"""

from __future__ import annotations

import itertools
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_platform.analytics.metrics import (
    CRYPTO_DAYS_PER_YEAR,
    MIN_ANNUALISABLE_YEARS,
    PerformanceSummary,
    summarise,
)
from trading_platform.analytics.windows import Fold, TestWindow, TrainWindow, split_walk_forward
from trading_platform.core.events import Bar, Fill
from trading_platform.engine.backtester import Backtester
from trading_platform.engine.portfolio import EquityPoint
from trading_platform.strategy.base import Strategy

#: One point in a parameter grid, e.g. ``{"fast": 10, "slow": 30}``.
Params = Mapping[str, object]

#: Builds a fresh strategy from one parameter set. Must return a NEW instance
#: on every call — strategies accumulate state across a run.
StrategyFactory = Callable[[Params], Strategy]

#: Builds a fresh ``Backtester``. Must also return a new instance every time:
#: ``RiskManager`` carries peak-equity state, so a shared one would leak a
#: drawdown halt from one fold into the next and silently flatten later folds.
BacktesterFactory = Callable[[], Backtester]

#: Reduces a full performance summary to the single number being optimised.
ScoreFn = Callable[[PerformanceSummary], float]


#: How much of a fold's re-optimised best score the previous fold's parameters
#: must retain to count as carried forward. Sourced to measurement rather than
#: taste: the null retains a median 13% and a genuine edge 95%, so anything
#: from roughly 0.3 to 0.9 divides the two cases and this constant is not
#: load-bearing. A test asserts the classification holds across that range.
CARRY_FORWARD_TOLERANCE = 0.8


def sharpe(summary: PerformanceSummary) -> float:
    """Default objective. Named so reports can print what was optimised."""
    return summary.sharpe_ratio


class DegenerateSweepError(ValueError):
    """Raised when no cell in a sweep produced a single fill.

    ``sharpe_ratio`` returns 0.0 for a zero-variance series, so a parameter set
    that never traded scores identically to one that traded and broke even. If
    nothing traded anywhere, picking a "best" cell would report whichever came
    first in the grid as an optimum. That is a fabricated result, so it raises.
    """


@dataclass(frozen=True, slots=True)
class SweepCell:
    """One parameter set's result on one training window."""

    params: Params
    coord: tuple[int, ...]
    score: float
    summary: PerformanceSummary
    fills: int
    halted: bool

    @property
    def traded(self) -> bool:
        return self.fills > 0

    @property
    def profitable(self) -> bool:
        return self.summary.total_return > 0


@dataclass(frozen=True, slots=True)
class SweepSurface:
    """Every cell of a parameter grid, not just the winner.

    The aggregate statistics exist so that a sharp peak cannot be reported as
    though it were a plateau.
    """

    cells: tuple[SweepCell, ...]
    axes: tuple[str, ...]
    axis_values: tuple[tuple[object, ...], ...]
    score_name: str = "sharpe"
    #: Parameter sets the strategy itself rejected, so the sweep never ran
    #: them. Counted because `len(cells)` is the *effective* trial count and
    #: is smaller than the grid: a 3x3 SMA grid with the `fast < slow`
    #: constraint has seven live cells, not nine. Any multiplicity correction
    #: computed from the nominal grid size would be wrong from the start.
    skipped: int = 0

    @property
    def attempted(self) -> int:
        """Live cells plus rejected ones — the size of the grid as written."""
        return len(self.cells) + self.skipped

    def __post_init__(self) -> None:
        if not self.cells:
            raise ValueError("A sweep surface needs at least one cell")

    @property
    def traded_cells(self) -> tuple[SweepCell, ...]:
        return tuple(c for c in self.cells if c.traded)

    @property
    def best(self) -> SweepCell:
        """Highest-scoring cell that actually traded.

        Raises ``DegenerateSweepError`` rather than naming a winner out of a
        field of tied zeros.
        """
        traded = self.traded_cells
        if not traded:
            raise DegenerateSweepError(
                f"No parameter set in this {len(self.cells)}-cell grid produced a fill. "
                "Every cell scores the same by default, so there is no best. Check the "
                "window length against the strategy's warm-up requirement."
            )
        return max(traded, key=lambda c: c.score)

    def score_for(self, params: Params) -> float | None:
        """This surface's score for one parameter set, or None if it has none.

        `None` covers both a set the grid never contained and one `skip_invalid`
        rejected — from the caller's point of view those are the same thing:
        there is no evidence here about that cell.
        """
        key = _params_key(params)
        for cell in self.cells:
            if _params_key(cell.params) == key:
                return cell.score
        return None

    @property
    def traded_fraction(self) -> float:
        return len(self.traded_cells) / len(self.cells)

    @property
    def profitable_fraction(self) -> float:
        """Share of the whole grid that made money. One good cell in forty is
        noise, however good that one cell looks."""
        return sum(1 for c in self.cells if c.profitable) / len(self.cells)

    @property
    def median_score(self) -> float:
        return statistics.median(c.score for c in self.cells)

    @property
    def spike_ratio(self) -> float | None:
        """Best over median. Large means the peak is unlike its own grid.

        None when the median is not positive — the ratio would be meaningless
        or sign-flipped, and a misleading number is worse than an absent one.
        """
        median = self.median_score
        if median <= 0:
            return None
        return self.best.score / median

    @property
    def neighbour_ratio(self) -> float | None:
        """Mean score of the best cell's immediate neighbours, over its own.

        Near 1 means a plateau: the result is insensitive to small parameter
        changes and is likelier to be real. Near 0 or negative means a spike
        that a one-step change destroys, which is a coincidence.

        None when the best cell has no neighbours (a single-cell grid).
        """
        best = self.best
        neighbours = [c for c in self.cells if _is_adjacent(c.coord, best.coord)]
        if not neighbours or best.score <= 0:
            return None
        return statistics.fmean(c.score for c in neighbours) / best.score

    def report(self) -> str:
        best = None if not self.traded_cells else self.best
        lines = [
            f"Sweep surface ({len(self.cells)} cells, objective={self.score_name})",
            f"  traded            : {self.traded_fraction:>8.0%} of cells",
            f"  profitable        : {self.profitable_fraction:>8.0%} of cells",
            f"  median score      : {self.median_score:>8.2f}",
        ]
        if best is None:
            lines.append("  best              :   none — no cell traded")
            return "\n".join(lines)
        lines += [
            f"  best score        : {best.score:>8.2f}  at {dict(best.params)}",
            f"  best / median     : {_fmt(self.spike_ratio)}",
            f"  neighbour ratio   : {_fmt(self.neighbour_ratio)}  (near 1.0 = plateau)",
        ]
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class FoldResult:
    """What one fold produced, in-sample and out, side by side."""

    index: int
    params: Params
    surface: SweepSurface
    in_sample: PerformanceSummary
    out_of_sample: PerformanceSummary
    in_sample_score: float
    out_of_sample_score: float
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    #: Fills inside the SCORED window only. See `evaluate` — this used to
    #: include warm-up fills and overstated by up to 2.17x, which mattered
    #: because `median_fills_per_fold < 10` is what decides whether a fold has
    #: enough trades for its statistics to be a measurement.
    out_of_sample_fills: int
    halted_out_of_sample: bool
    #: Every fill in the fold, warm-up included. Round trips must be paired
    #: over all of them and then attributed by closing time; pairing only the
    #: scored fills would read an exit whose entry was in warm-up as opening a
    #: short.
    fills: tuple[Fill, ...] = ()
    #: Buy-and-hold over the same window, when a benchmark symbol was given.
    benchmark: PerformanceSummary | None = None
    benchmark_score: float | None = None

    @property
    def beats_benchmark(self) -> bool | None:
        """None when no benchmark was requested — not the same as False."""
        if self.benchmark_score is None:
            return None
        return self.out_of_sample_score > self.benchmark_score

    @property
    def degradation(self) -> float:
        """In-sample score minus out-of-sample. Positive is the normal case."""
        return self.in_sample_score - self.out_of_sample_score


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    """The result of the whole harness.

    ``efficiency`` leads because it is the number that says whether anything
    was learned or merely fitted.
    """

    folds: tuple[FoldResult, ...]
    score_name: str = "sharpe"
    anchored: bool = False

    def __post_init__(self) -> None:
        if not self.folds:
            raise ValueError("A walk-forward report needs at least one fold")

    @property
    def mean_in_sample(self) -> float:
        return statistics.fmean(f.in_sample_score for f in self.folds)

    @property
    def mean_out_of_sample(self) -> float:
        return statistics.fmean(f.out_of_sample_score for f in self.folds)

    @property
    def efficiency(self) -> float | None:
        """Out-of-sample over in-sample. Below ~0.5 the strategy is fitted.

        None when the in-sample score is not positive: there was no edge to
        degrade, so the ratio would describe nothing. A strategy that failed
        even in-sample has already answered the question.
        """
        in_sample = self.mean_in_sample
        if in_sample <= 0:
            return None
        return self.mean_out_of_sample / in_sample

    @property
    def parameter_changes(self) -> int:
        """Times the chosen parameters differed from the previous fold."""
        keys = [_params_key(f.params) for f in self.folds]
        return sum(1 for a, b in itertools.pairwise(keys) if a != b)

    @property
    def parameter_stability(self) -> float:
        """Share of transitions where the previous fold's parameters still work.

        Not whether the chosen tuple *changed* — whether carrying it forward
        would have cost anything. Those are different questions and the gate
        has always cared about the second: there is nothing to carry into the
        next period is a claim about performance, not about identity.

        **Measured, because the identity version was the pipeline's last
        false negative.** A broad effect makes many cells work, so the argmax
        wanders between near-equals — which `neighbour_ratio` reads as a
        plateau and treats as evidence of a real effect, while identity-based
        stability read the same evidence as fitting. On a genuine edge the
        stricter one won: at an information coefficient of 0.188 and 0.395,
        four to eight times anything real, every other objection had gone
        silent and this one still refused 2 runs in 8.

        Scoring the old parameters on the new fold's surface separates the two
        cases completely. Across 8 seeds, carrying the previous fold's
        parameters forward retained:

            null       median 13% of the re-optimised best, 7% of transitions >= 0.8
            IC 0.188   median 95%,                        100% of transitions >= 0.8

        A transition is unstable when the old parameters are absent from the
        new surface, or when the new best is not positive — a fold with no
        positive edge has nothing to carry, and a ratio against a non-positive
        denominator is not a number anyone should act on.
        """
        transitions = len(self.folds) - 1
        if transitions <= 0:
            return 1.0
        carried = 0
        for previous, current in itertools.pairwise(self.folds):
            best = current.in_sample_score
            if best <= 0:
                continue
            score = current.surface.score_for(previous.params)
            if score is not None and score >= best * CARRY_FORWARD_TOLERANCE:
                carried += 1
        return carried / transitions

    @property
    def profitable_folds(self) -> int:
        return sum(1 for f in self.folds if f.out_of_sample.total_return > 0)

    @property
    def total_out_of_sample_fills(self) -> int:
        return sum(f.out_of_sample_fills for f in self.folds)

    @property
    def median_fills_per_fold(self) -> float:
        """Sample size per fold, which is what each fold's score rests on.

        Checked per fold rather than in total: a run of six folds where one
        traded thirty times and five traded twice has a healthy-looking total
        and five meaningless scores. A Sharpe computed over two fills is an
        arithmetic result, not a measurement.
        """
        return statistics.median(f.out_of_sample_fills for f in self.folds)

    @property
    def thinnest_fold(self) -> FoldResult:
        return min(self.folds, key=lambda f: f.out_of_sample_fills)

    @property
    def folds_beating_benchmark(self) -> int | None:
        """Folds that beat buy-and-hold. None when no benchmark was requested."""
        verdicts = [f.beats_benchmark for f in self.folds]
        if any(v is None for v in verdicts):
            return None
        return sum(1 for v in verdicts if v)

    @property
    def mean_profitable_fraction(self) -> float:
        """Across folds, the share of each training grid that made money.

        Near zero means the winning cell was an outlier inside its own sweep,
        which is what a coincidence looks like from the inside.
        """
        return statistics.fmean(f.surface.profitable_fraction for f in self.folds)

    @property
    def median_neighbour_ratio(self) -> float | None:
        """Median plateau-ness of the chosen peaks. None if never computable."""
        ratios = [r for r in (f.surface.neighbour_ratio for f in self.folds) if r is not None]
        if not ratios:
            return None
        return statistics.median(ratios)

    @property
    def verdicts(self) -> tuple[str, ...]:
        """Every reason to disbelieve this run, in plain words.

        Deliberately several tests rather than one. Efficiency alone is not
        enough: on pure noise the out-of-sample scores scatter around zero and
        their mean can beat a barely-positive in-sample mean, producing an
        efficiency above 1.0 for a strategy with no edge whatsoever. The sweep
        surface catches that case; efficiency catches the classic overfit. A
        run has to survive both.
        """
        out: list[str] = []

        # First, because it invalidates the inputs every later verdict reads.
        # A halted fold measures the risk layer, not the strategy — the same
        # thing BacktestResult.rejection_rate exists to say, which this path
        # had never consulted.
        halted = [f.index for f in self.folds if f.halted_out_of_sample]
        if halted:
            shown = ", ".join(str(i) for i in halted[:8]) + ("..." if len(halted) > 8 else "")
            out.append(
                f"{len(halted)}/{len(self.folds)} fold(s) HALTED out-of-sample (fold {shown}). "
                "Once the drawdown halt latches every later order is refused, so those folds "
                "describe the risk layer rather than the strategy — and a fold that was "
                "forbidden to trade is not a fold that declined to."
            )

        efficiency = self.efficiency
        if efficiency is None:
            out.append(
                "No in-sample edge to degrade — the strategy failed on the data it was "
                "fitted on. Efficiency is undefined and nothing here is a result."
            )
        elif efficiency < 0.5:
            out.append(
                f"Efficiency {efficiency:.2f} is below 0.5. Most of the in-sample result "
                "did not survive unseen data — treat it as fitted, not as an edge."
            )
        if self.mean_profitable_fraction < 0.25:
            out.append(
                f"Only {self.mean_profitable_fraction:.0%} of the average training grid was "
                "profitable. The chosen parameters are an outlier within their own sweep, "
                "which is what picking noise looks like."
            )
        ratio = self.median_neighbour_ratio
        if ratio is not None and ratio < 0.5:
            out.append(
                f"Median neighbour ratio {ratio:.2f}: the peaks are spikes, not plateaus. A "
                "one-step parameter change destroys the result, so the parameters are "
                "fitted to this sample rather than to a market effect."
            )
        if self.parameter_stability < 0.5:
            out.append(
                f"Parameter stability {self.parameter_stability:.0%}: the parameters chosen "
                "on one fold do not hold up on the next one's own training data, so there "
                "is nothing to carry into the next period. This is about performance, not "
                "about the numbers changing — a different cell that works just as well "
                "counts as stable."
            )
        if self.median_fills_per_fold < 10:
            thinnest = self.thinnest_fold
            out.append(
                f"Median {self.median_fills_per_fold:.0f} out-of-sample fills per fold "
                f"({self.total_out_of_sample_fills} in total; fold {thinnest.index} scored "
                f"{thinnest.out_of_sample_score:.2f} on {thinnest.out_of_sample_fills}). "
                "These samples are too small to support a Sharpe figure."
            )
        # 35c: the sweep RANKS cells by this score. If the score is a figure
        # annualised from a window too short to carry one, or from a curve flat
        # enough that the scaling factor dominates it, the grid is ordering
        # noise — and `annualisation_is_meaningful` had no reader outside
        # metrics.py to say so.
        unsound = [f.index for f in self.folds if not f.out_of_sample.annualisation_is_meaningful]
        if unsound:
            shown = ", ".join(str(i) for i in unsound[:8]) + ("..." if len(unsound) > 8 else "")
            out.append(
                f"{len(unsound)}/{len(self.folds)} fold(s) report a score whose annualisation "
                f"is not meaningful (fold {shown}) — the window is shorter than "
                f"{MIN_ANNUALISABLE_YEARS:.2f} years, or the curve is flat enough that "
                "sqrt(periods_per_year) dominates it. The sweep ranked its grid on these."
            )

        beat = self.folds_beating_benchmark
        if beat is not None and beat * 2 <= len(self.folds):
            out.append(
                f"Beat cost-free buy-and-hold in only {beat}/{len(self.folds)} folds. The "
                "strategy spent costs and took risk to underperform doing nothing."
            )
        return tuple(out)

    @property
    def halted_folds(self) -> int:
        """Folds whose out-of-sample run hit the drawdown halt.

        `FoldResult.halted_out_of_sample` was written on every fold and read by
        nothing: `report()` never printed it and `verdicts()` never tested it.
        On the real archive that hid 17 halted folds behind a "0 fills" column.
        """
        return sum(1 for f in self.folds if f.halted_out_of_sample)

    @property
    def survives(self) -> bool:
        """True only when no verdict fired. The default answer is False."""
        return not self.verdicts

    def report(self) -> str:
        mode = "anchored" if self.anchored else "rolling"
        lines = [
            f"Walk-forward ({len(self.folds)} folds, {mode}, objective={self.score_name})",
            "",
            f"{'fold':>4}  {'test window':<23}  {'in-samp':>8}  {'out-samp':>9}  "
            f"{'hold':>8}  {'fills':>6}  params",
        ]
        for f in self.folds:
            window = f"{f.test_start:%Y-%m-%d}..{f.test_end:%Y-%m-%d}"
            hold = "     n/a" if f.benchmark_score is None else f"{f.benchmark_score:>8.2f}"
            # A halted fold scored a forbidden account, not a strategy. Printing
            # only its fill count read as "declined to trade" for 17 of 18 folds
            # on the real archive while the true cause was a latched risk halt.
            flag = " HALT" if f.halted_out_of_sample else ""
            lines.append(
                f"{f.index:>4}  {window:<23}  {f.in_sample_score:>8.2f}  "
                f"{f.out_of_sample_score:>9.2f}  {hold}  {f.out_of_sample_fills:>6}  "
                f"{dict(f.params)}{flag}"
            )
        lines += [
            "",
            f"Mean in-sample       : {self.mean_in_sample:>8.2f}",
            f"Mean out-of-sample   : {self.mean_out_of_sample:>8.2f}",
            f"EFFICIENCY (oos/is)  : {_fmt(self.efficiency)}",
            f"Profitable folds     : {self.profitable_folds:>8}/{len(self.folds)}",
            f"Halted folds         : {self.halted_folds:>8}/{len(self.folds)}",
            f"Out-of-sample fills  : {self.total_out_of_sample_fills:>8}  "
            f"(median {self.median_fills_per_fold:.0f}/fold)",
            f"Grid profitable      : {self.mean_profitable_fraction:>8.0%}  (mean across folds)",
            f"Neighbour ratio      : {_fmt(self.median_neighbour_ratio)}  (median; near 1.0 = plateau)",
            f"Parameter stability  : {self.parameter_stability:>8.0%}  "
            f"({self.parameter_changes} change(s))",
        ]
        beat = self.folds_beating_benchmark
        if beat is not None:
            lines.append(f"Beat buy-and-hold    : {beat:>8}/{len(self.folds)} folds")
        lines.append("")
        verdicts = self.verdicts
        if not verdicts:
            lines.append(
                "No disqualifying signal found. That is not the same as an edge — it means "
                "this run failed to falsify one, on this data, at this cost model."
            )
        else:
            lines.append(f"DO NOT TRUST THIS RESULT — {len(verdicts)} reason(s):")
            lines += [f"  {i}. {v}" for i, v in enumerate(verdicts, start=1)]
        return "\n".join(lines)


def expand_grid(grid: Mapping[str, Sequence[object]]) -> list[dict[str, object]]:
    """Cartesian product of a parameter grid, in deterministic order."""
    if not grid:
        raise ValueError("Empty parameter grid")
    axes = list(grid)
    for axis in axes:
        if not grid[axis]:
            raise ValueError(f"Parameter {axis!r} has no values")
    return [
        dict(zip(axes, combo, strict=True)) for combo in itertools.product(*(grid[a] for a in axes))
    ]


def sweep(
    grid: Mapping[str, Sequence[object]],
    *,
    window: TrainWindow,
    strategy_factory: StrategyFactory,
    backtester_factory: BacktesterFactory,
    score: ScoreFn = sharpe,
    periods_per_year: int = CRYPTO_DAYS_PER_YEAR,
    score_name: str = "sharpe",
    skip_invalid: bool = True,
) -> SweepSurface:
    """Score every parameter set in ``grid`` on a training window.

    Accepts **only** a ``TrainWindow``. Passing a ``TestWindow`` is a type
    error, which is the point: fitting on test data cannot happen by accident.

    ``skip_invalid`` drops parameter sets the strategy itself rejects (for
    example ``fast >= slow``), because a rectangular grid over a constrained
    space always contains some. Genuine errors still propagate.
    """
    axes = tuple(grid)
    axis_values = tuple(tuple(grid[a]) for a in axes)
    cells: list[SweepCell] = []
    skipped = 0

    for coord in itertools.product(*(range(len(v)) for v in axis_values)):
        params = {
            axis: axis_values[i][c] for i, (axis, c) in enumerate(zip(axes, coord, strict=True))
        }
        try:
            strategy = strategy_factory(params)
        except ValueError:
            if skip_invalid:
                skipped += 1
                continue
            raise

        result = backtester_factory().run(strategy, window.feed())
        summary = summarise(result.equity_curve, periods_per_year=periods_per_year)
        cells.append(
            SweepCell(
                params=params,
                coord=coord,
                score=score(summary),
                summary=summary,
                fills=len(result.fills),
                halted=result.halted,
            )
        )

    if not cells:
        raise ValueError(
            "Every parameter set in the grid was rejected by the strategy. "
            "The grid and the strategy's constraints do not overlap."
        )
    return SweepSurface(
        cells=tuple(cells),
        axes=axes,
        axis_values=axis_values,
        score_name=score_name,
        skipped=skipped,
    )


def evaluate(
    params: Params,
    *,
    window: TestWindow,
    strategy_factory: StrategyFactory,
    backtester_factory: BacktesterFactory,
    score: ScoreFn = sharpe,
    periods_per_year: int = CRYPTO_DAYS_PER_YEAR,
) -> tuple[PerformanceSummary, float, int, bool, tuple[Fill, ...]]:
    """Run one parameter set on an out-of-sample window, exactly once.

    Accepts **only** a ``TestWindow``. There is no tuning here and no loop —
    an evaluation that can be repeated with different parameters is a sweep
    wearing a disguise.

    The returned fill count covers the **scored** window only. It used to be
    `len(result.fills)` over the whole feed, and a `TestWindow`'s feed is
    `warmup + bars` — so with the 120-bar warm-up and 100-bar test window the
    UI runs, the count overstated by 2.17x and `median_fills_per_fold < 10`
    was an effective five-fill guard. The full fill tuple is returned
    alongside, because round trips must be paired over all of it: pairing only
    the scored fills orphans an exit whose entry was in warm-up, and FIFO
    would read that lone sell as opening a short.
    """
    result = backtester_factory().run(strategy_factory(params), window.feed())
    summary = summarise(window.score_slice(result.equity_curve), periods_per_year=periods_per_year)
    scored = window.scored_fills(result.fills)
    return summary, score(summary), len(scored), result.halted, tuple(result.fills)


def walk_forward(
    bars: Iterable[Bar],
    grid: Mapping[str, Sequence[object]],
    *,
    strategy_factory: StrategyFactory,
    backtester_factory: BacktesterFactory,
    train_size: int,
    test_size: int,
    anchored: bool = False,
    warmup: int = 0,
    score: ScoreFn = sharpe,
    periods_per_year: int = CRYPTO_DAYS_PER_YEAR,
    score_name: str = "sharpe",
    benchmark_symbol: str | None = None,
) -> WalkForwardReport:
    """Fit on each window, evaluate on the next, and report the gap.

    Both the strategy and the ``Backtester`` are rebuilt from factories for
    every single run — never reused. ``RiskManager`` carries peak-equity state
    across a run, so one shared instance would leak a drawdown halt from an
    early fold into every later one and quietly report zeros as results.
    """
    folds: list[Fold] = split_walk_forward(
        bars, train_size=train_size, test_size=test_size, anchored=anchored, warmup=warmup
    )

    results: list[FoldResult] = []
    for fold in folds:
        surface = sweep(
            grid,
            window=fold.train,
            strategy_factory=strategy_factory,
            backtester_factory=backtester_factory,
            score=score,
            periods_per_year=periods_per_year,
            score_name=score_name,
        )
        try:
            best = surface.best
        except DegenerateSweepError as exc:
            raise DegenerateSweepError(f"{fold.describe()}: {exc}") from exc

        oos_summary, oos_score, oos_fills, halted, fold_fills = evaluate(
            best.params,
            window=fold.test,
            strategy_factory=strategy_factory,
            backtester_factory=backtester_factory,
            score=score,
            periods_per_year=periods_per_year,
        )
        benchmark: PerformanceSummary | None = None
        benchmark_score: float | None = None
        if benchmark_symbol is not None:
            benchmark = summarise(
                buy_and_hold_curve(fold.test, benchmark_symbol),
                periods_per_year=periods_per_year,
            )
            benchmark_score = score(benchmark)

        results.append(
            FoldResult(
                index=fold.index,
                params=best.params,
                surface=surface,
                in_sample=best.summary,
                out_of_sample=oos_summary,
                in_sample_score=best.score,
                out_of_sample_score=oos_score,
                train_start=fold.train.start,
                train_end=fold.train.end,
                test_start=fold.test.start,
                test_end=fold.test.end,
                out_of_sample_fills=oos_fills,
                halted_out_of_sample=halted,
                fills=fold_fills,
                benchmark=benchmark,
                benchmark_score=benchmark_score,
            )
        )

    return WalkForwardReport(folds=tuple(results), score_name=score_name, anchored=anchored)


def buy_and_hold_curve(
    window: TestWindow, symbol: str, starting_cash: Decimal = Decimal("100000")
) -> list[EquityPoint]:
    """Equity from holding ``symbol`` across the scored window.

    The comparison that stops "it made money" from counting as a result. A
    strategy that trades all year and lands under buy-and-hold has spent costs
    and risk to underperform doing nothing.

    Deliberately **cost-free**, which makes it a slightly generous bar: a
    strategy that cannot clear a frictionless benchmark is not merely mediocre.
    Entry is at the first scored bar's open — the same bar an order placed on
    the final warm-up bar would fill on, so the timing comparison is fair and
    the benchmark gets no lookahead the strategy is denied.
    """
    bars = [b for b in window.bars if b.symbol == symbol]
    if not bars:
        raise ValueError(f"{symbol!r} has no bars in this test window")
    entry = bars[0].open
    if entry <= 0:
        raise ValueError(f"{symbol!r} opens at {entry}; cannot size a benchmark position")
    quantity = starting_cash / entry
    return [
        EquityPoint(
            timestamp=bar.timestamp,
            equity=quantity * bar.close,
            cash=Decimal("0"),
            positions_value=quantity * bar.close,
        )
        for bar in bars
    ]


def _is_adjacent(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    """True if the coordinates differ by one step along exactly one axis."""
    if len(a) != len(b):
        return False
    diffs = [abs(x - y) for x, y in zip(a, b, strict=True)]
    return sum(diffs) == 1


def _params_key(params: Params) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, repr(v)) for k, v in params.items()))


def _fmt(value: float | None) -> str:
    return "     n/a" if value is None else f"{value:>8.2f}"
