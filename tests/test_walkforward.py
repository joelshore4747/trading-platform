"""The sweep and the walk-forward harness.

The claim under test is not "the harness runs". It is that the harness
*refuses to certify* things it should refuse to certify — a spiky sweep
surface, a fold scored on two fills, a strategy that loses to buy-and-hold.
A harness that never says no is decoration.
"""

from __future__ import annotations

import random
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.analytics.metrics import PerformanceSummary
from trading_platform.analytics.trades import summarise_trades
from trading_platform.analytics.walkforward import (
    DegenerateSweepError,
    FoldResult,
    SweepCell,
    SweepSurface,
    WalkForwardReport,
    buy_and_hold_curve,
    evaluate,
    expand_grid,
    sweep,
    walk_forward,
)
from trading_platform.analytics.windows import TestWindow, TrainWindow, split_walk_forward
from trading_platform.core.events import Bar, Fill
from trading_platform.core.instruments import crypto_spot
from trading_platform.core.types import Side
from trading_platform.data.synthetic import Regime, RegimeSpec, generate_bars
from trading_platform.engine.backtester import Backtester
from trading_platform.engine.execution import ZERO_COST
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.base import Strategy, StrategyContext
from trading_platform.strategy.examples.sma_crossover import SMACrossover

SYMBOL = "BTCUSDT"
START = datetime(2021, 1, 1, tzinfo=UTC)

#: Risk limits fully out of the way, so a fills-accounting test measures fills
#: rather than the risk layer.
UNCONSTRAINED = RiskLimits(
    max_order_notional=None,
    max_position_notional=None,
    max_order_notional_pct=None,
    max_position_notional_pct=None,
    max_gross_leverage=None,
    max_drawdown_pct=None,
    max_margin_utilisation=None,
    max_orders_per_bar=None,
)


def make_backtester() -> Backtester:
    return Backtester(
        instruments={SYMBOL: crypto_spot(SYMBOL)},
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=RiskLimits()),
    )


def make_strategy(params: dict[str, object]) -> Strategy:
    return SMACrossover(SYMBOL, **params)  # type: ignore[arg-type]


def mixed_regime_bars(seed: int = 7, scale: int = 1) -> list[Bar]:
    return generate_bars(
        SYMBOL,
        START,
        [
            RegimeSpec(Regime.TREND, 120 * scale),
            RegimeSpec(Regime.CHOP, 120 * scale),
            RegimeSpec(Regime.CRASH, 40 * scale),
            RegimeSpec(Regime.RECOVERY, 80 * scale),
        ],
        seed=seed,
        start_price=Decimal("30000"),
    )


# -- Grid expansion ----------------------------------------------------------


def test_expand_grid_is_the_full_product_in_a_stable_order() -> None:
    grid = expand_grid({"fast": [5, 10], "slow": [30, 50, 80]})
    assert len(grid) == 6
    assert grid[0] == {"fast": 5, "slow": 30}
    assert expand_grid({"fast": [5, 10], "slow": [30, 50, 80]}) == grid


@pytest.mark.parametrize("grid", [{}, {"fast": []}])
def test_a_grid_with_nothing_in_it_raises(grid: dict[str, list[int]]) -> None:
    with pytest.raises(ValueError):
        expand_grid(grid)


# -- The sweep reports the surface, not the peak ------------------------------


def test_sweep_reports_every_cell_not_only_the_best() -> None:
    """The peak is the number that fools people; the shape is the evidence."""
    window = TrainWindow(bars=tuple(mixed_regime_bars()[:300]))
    surface = sweep(
        {"fast": [5, 10, 15], "slow": [30, 50, 80]},
        window=window,
        strategy_factory=make_strategy,
        backtester_factory=make_backtester,
    )
    assert len(surface.cells) == 9
    assert surface.best in surface.cells
    assert 0.0 <= surface.profitable_fraction <= 1.0
    assert 0.0 < surface.traded_fraction <= 1.0


def test_sweep_skips_parameter_sets_the_strategy_rejects() -> None:
    """A rectangular grid over a constrained space always contains some;
    fast >= slow is invalid for an SMA crossover."""
    window = TrainWindow(bars=tuple(mixed_regime_bars()[:300]))
    surface = sweep(
        {"fast": [10, 50], "slow": [30, 80]},
        window=window,
        strategy_factory=make_strategy,
        backtester_factory=make_backtester,
    )
    # (50, 30) is invalid and dropped; the other three survive.
    assert len(surface.cells) == 3
    assert all(int(str(c.params["fast"])) < int(str(c.params["slow"])) for c in surface.cells)


def test_sweep_can_be_told_not_to_skip_invalid_cells() -> None:
    window = TrainWindow(bars=tuple(mixed_regime_bars()[:300]))
    with pytest.raises(ValueError, match="must be shorter"):
        sweep(
            {"fast": [50], "slow": [30]},
            window=window,
            strategy_factory=make_strategy,
            backtester_factory=make_backtester,
            skip_invalid=False,
        )


def test_a_grid_entirely_incompatible_with_the_strategy_raises() -> None:
    window = TrainWindow(bars=tuple(mixed_regime_bars()[:300]))
    with pytest.raises(ValueError, match="do not overlap"):
        sweep(
            {"fast": [50, 60], "slow": [30, 40]},
            window=window,
            strategy_factory=make_strategy,
            backtester_factory=make_backtester,
        )


# -- The degenerate-grid trap -------------------------------------------------


class NeverTrades(Strategy):
    """Submits nothing. Scores 0.0, exactly like a break-even strategy."""

    def __init__(self, **params: object) -> None:
        self.name = "NeverTrades"

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
        return

    def on_fill(self, fill: Fill, ctx: StrategyContext) -> None:
        return


def test_a_grid_where_nothing_traded_refuses_to_name_a_winner() -> None:
    """`sharpe_ratio` returns 0.0 for a zero-variance series, so every silent
    cell ties. Naming one 'best' would fabricate an optimum out of a tie."""
    window = TrainWindow(bars=tuple(mixed_regime_bars()[:300]))
    surface = sweep(
        {"fast": [5, 10], "slow": [30, 50]},
        window=window,
        strategy_factory=lambda p: NeverTrades(**p),
        backtester_factory=make_backtester,
    )
    assert surface.traded_fraction == 0.0
    with pytest.raises(DegenerateSweepError, match="no best"):
        _ = surface.best
    assert "no cell traded" in surface.report()


def test_walk_forward_names_the_fold_when_a_grid_is_degenerate() -> None:
    with pytest.raises(DegenerateSweepError, match="fold 0"):
        walk_forward(
            mixed_regime_bars(),
            {"fast": [5], "slow": [30]},
            strategy_factory=lambda p: NeverTrades(**p),
            backtester_factory=make_backtester,
            train_size=150,
            test_size=50,
        )


# -- Surface metrics must actually discriminate -------------------------------


def surface_from_scores(scores: list[float], axis: str = "p") -> SweepSurface:
    """Build a surface directly, to test the metrics rather than the engine."""
    summary = PerformanceSummary(
        total_return=0.0,
        cagr=0.0,
        annualised_volatility=0.0,
        sharpe_ratio=0.0,
        sortino_ratio=0.0,
        max_drawdown=0.0,
        max_drawdown_duration=0,
        calmar_ratio=0.0,
        win_rate=0.0,
        num_periods=10,
    )
    cells = tuple(
        SweepCell(
            params={axis: i},
            coord=(i,),
            score=s,
            summary=summary,
            fills=1,
            halted=False,
        )
        for i, s in enumerate(scores)
    )
    return SweepSurface(
        cells=cells,
        axes=(axis,),
        axis_values=(tuple(range(len(scores))),),
    )


def test_neighbour_ratio_separates_a_plateau_from_a_spike() -> None:
    """If this metric does not discriminate it is decoration, not evidence."""
    plateau = surface_from_scores([1.9, 2.0, 2.1, 2.0, 1.9])
    spike = surface_from_scores([0.1, 0.0, 2.1, 0.0, 0.1])

    plateau_ratio = plateau.neighbour_ratio
    spike_ratio = spike.neighbour_ratio
    assert plateau_ratio is not None and spike_ratio is not None
    assert plateau_ratio > 0.9, "a flat surface should look like a plateau"
    assert spike_ratio < 0.1, "a lone peak should look like a coincidence"
    assert spike_ratio < plateau_ratio


def test_neighbour_ratio_uses_adjacency_not_the_whole_grid() -> None:
    """Only cells one step away on exactly one axis count as neighbours."""
    surface = surface_from_scores([0.0, 0.0, 2.0, 1.0, 0.0])
    ratio = surface.neighbour_ratio
    assert ratio is not None
    # Neighbours of index 2 are indices 1 and 3: mean 0.5, over a best of 2.0.
    assert ratio == pytest.approx(0.25)


def test_a_single_cell_grid_has_no_neighbours_to_report() -> None:
    assert surface_from_scores([1.0]).neighbour_ratio is None


def test_spike_ratio_is_withheld_when_the_median_is_not_positive() -> None:
    """A ratio against a negative median is sign-flipped nonsense; None is
    more honest than a misleading number."""
    assert surface_from_scores([-1.0, -1.0, 2.0, -1.0]).spike_ratio is None
    assert surface_from_scores([1.0, 1.0, 2.0, 1.0]).spike_ratio == pytest.approx(2.0)


# -- Fold isolation -----------------------------------------------------------


def test_each_fold_gets_a_fresh_backtester() -> None:
    """`RiskManager` carries peak-equity state. A shared instance would leak a
    drawdown halt out of one fold and silently flatten every later one."""
    built: list[Backtester] = []

    def factory() -> Backtester:
        bt = make_backtester()
        built.append(bt)
        return bt

    report = walk_forward(
        mixed_regime_bars(),
        {"fast": [5, 10], "slow": [30, 50]},
        strategy_factory=make_strategy,
        backtester_factory=factory,
        train_size=150,
        test_size=50,
    )

    assert len(built) == len({id(b) for b in built}), "a Backtester was reused across runs"
    assert len({id(b.risk_manager) for b in built}) == len(built)
    # One run per surviving grid cell per fold, plus one evaluation per fold.
    expected = sum(len(f.surface.cells) for f in report.folds) + len(report.folds)
    assert len(built) == expected


def test_a_halt_in_one_fold_does_not_carry_into_the_next() -> None:
    """The concrete version of the previous test: a punishing drawdown limit
    halts early folds without disabling later ones."""
    tight = Backtester(
        instruments={SYMBOL: crypto_spot(SYMBOL)},
        starting_cash=Decimal("100000"),
        risk_manager=RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.02"))),
    )
    assert tight.risk_manager.halted is False

    report = walk_forward(
        mixed_regime_bars(),
        {"fast": [5, 10], "slow": [30, 50]},
        strategy_factory=make_strategy,
        backtester_factory=lambda: Backtester(
            instruments={SYMBOL: crypto_spot(SYMBOL)},
            starting_cash=Decimal("100000"),
            risk_manager=RiskManager(limits=RiskLimits(max_drawdown_pct=Decimal("0.02"))),
        ),
        train_size=150,
        test_size=50,
    )
    # Later folds still trade; a leaked halt would freeze them at zero fills.
    assert any(f.out_of_sample_fills > 0 for f in report.folds[1:])


# -- The harness's headline ---------------------------------------------------


def test_out_of_sample_windows_are_scored_only_once_each() -> None:
    bars = mixed_regime_bars()
    folds = split_walk_forward(bars, train_size=150, test_size=50)
    report = walk_forward(
        bars,
        {"fast": [5, 10], "slow": [30, 50]},
        strategy_factory=make_strategy,
        backtester_factory=make_backtester,
        train_size=150,
        test_size=50,
    )
    assert len(report.folds) == len(folds)
    windows = [(f.test_start, f.test_end) for f in report.folds]
    assert len(windows) == len(set(windows))


def test_the_chosen_parameters_are_the_training_winner() -> None:
    report = walk_forward(
        mixed_regime_bars(),
        {"fast": [5, 10, 15], "slow": [30, 50]},
        strategy_factory=make_strategy,
        backtester_factory=make_backtester,
        train_size=150,
        test_size=50,
    )
    for fold in report.folds:
        assert fold.params == fold.surface.best.params
        assert fold.in_sample_score == fold.surface.best.score


def test_efficiency_is_withheld_when_there_was_no_in_sample_edge() -> None:
    """A strategy that failed on the data it was fitted on has already
    answered the question; a ratio would describe nothing."""
    report = _report_with_scores(in_sample=[-1.0, -0.5], out_of_sample=[0.4, 0.3])
    assert report.efficiency is None
    assert any("No in-sample edge" in v for v in report.verdicts)


def test_efficiency_below_half_is_called_out_as_fitting() -> None:
    report = _report_with_scores(in_sample=[3.0, 3.0], out_of_sample=[0.3, 0.3])
    assert report.efficiency == pytest.approx(0.1)
    assert any("fitted" in v for v in report.verdicts)
    assert "DO NOT TRUST" in report.report()


def test_efficiency_alone_does_not_certify_a_run() -> None:
    """The finding that shaped this module: on noise, out-of-sample scores
    scatter around zero and their mean can beat a barely-positive in-sample
    mean, yielding efficiency above 1.0 for a strategy with no edge. The
    surface and sample-size checks are what catch that case."""
    report = _report_with_scores(
        in_sample=[0.1, 0.2],
        out_of_sample=[1.5, 0.5],
        fills=2,
        profitable_fraction=0.0,
    )
    efficiency = report.efficiency
    assert efficiency is not None and efficiency > 1.0
    assert not report.survives
    reasons = " ".join(report.verdicts)
    assert "too small" in reasons
    assert "outlier" in reasons


def test_a_thin_fold_is_flagged_even_when_the_total_looks_healthy() -> None:
    """One fold with thirty fills and five with two is not a sample of 40."""
    report = _report_with_scores(
        in_sample=[1.0] * 6,
        out_of_sample=[1.0] * 6,
        fills=[30, 2, 2, 2, 2, 2],
    )
    assert report.total_out_of_sample_fills == 40
    assert report.median_fills_per_fold == 2
    assert any("too small" in v for v in report.verdicts)


def test_losing_to_buy_and_hold_is_a_disqualifying_result() -> None:
    report = _report_with_scores(
        in_sample=[2.0, 2.0],
        out_of_sample=[1.0, 1.0],
        benchmark=[3.0, 3.0],
        fills=20,
    )
    assert report.folds_beating_benchmark == 0
    assert any("buy-and-hold" in v for v in report.verdicts)


def test_no_benchmark_requested_is_not_the_same_as_failing_one() -> None:
    report = _report_with_scores(in_sample=[2.0], out_of_sample=[1.5], fills=20)
    assert report.folds_beating_benchmark is None
    assert not any("buy-and-hold" in v for v in report.verdicts)


def test_parameter_stability_measures_carrying_forward_not_identity() -> None:
    """**This test's meaning changed, deliberately.**

    It used to assert that varying parameters across folds gave a stability of
    0.0 — identity. Under the measured definition that is wrong: if every cell
    scores alike, moving between them costs nothing, and the thing the gate
    claims to care about ("nothing to carry into the next period") has not
    happened. Identity-based stability was the pipeline's last false negative,
    still refusing 2 runs in 8 at an information coefficient four to eight
    times anything real, after every other objection had gone silent.

    So varying-on-a-plateau is now stable, and instability means the previous
    fold's parameters genuinely stop working — which needs a surface with real
    dispersion to express.
    """
    plateau = _report_with_scores(
        in_sample=[1.0] * 4, out_of_sample=[1.0] * 4, fills=20, params_per_fold="varying"
    )
    assert plateau.parameter_changes == 3, "the tuple really does change every fold"
    assert plateau.parameter_stability == 1.0, "and it costs nothing, so it is stable"
    assert not any("carry into the next period" in v for v in plateau.verdicts)

    # A surface where only one cell works: leaving it is real instability.
    spiky = _report_with_scores(
        in_sample=[1.0] * 4,
        out_of_sample=[1.0] * 4,
        fills=20,
        params_per_fold="varying",
        neighbour_ratio_scores=[1.0, 0.1, 0.1],
    )
    assert spiky.parameter_stability < 0.5
    assert any("carry into the next period" in v for v in spiky.verdicts)


def test_holding_the_same_parameters_is_always_stable() -> None:
    stable = _report_with_scores(
        in_sample=[1.0] * 4, out_of_sample=[1.0] * 4, fills=20, params_per_fold="same"
    )
    assert stable.parameter_changes == 0
    assert stable.parameter_stability == 1.0


def test_a_clean_run_still_refuses_to_claim_an_edge() -> None:
    """The wording matters: failing to falsify is not evidence of profit."""
    report = _report_with_scores(
        in_sample=[1.0] * 4,
        out_of_sample=[0.9] * 4,
        fills=25,
        profitable_fraction=0.8,
        neighbour_ratio_scores=[0.9, 1.0, 0.95],
    )
    assert report.survives
    assert "failed to falsify" in report.report()
    assert "edge" in report.report()


# -- Buy-and-hold benchmark ---------------------------------------------------


def test_buy_and_hold_enters_at_the_first_scored_open() -> None:
    """The same bar an order placed on the last warm-up bar would fill on, so
    the benchmark gets no timing advantage the strategy is denied."""
    bars = mixed_regime_bars()
    window = split_walk_forward(bars, train_size=150, test_size=50, warmup=10)[0].test
    curve = buy_and_hold_curve(window, SYMBOL, starting_cash=Decimal("100000"))

    assert len(curve) == len(window.bars)
    assert curve[0].timestamp == window.start
    quantity = Decimal("100000") / window.bars[0].open
    assert curve[0].equity == quantity * window.bars[0].close
    assert curve[-1].equity == quantity * window.bars[-1].close


def test_buy_and_hold_never_touches_warmup_bars() -> None:
    bars = mixed_regime_bars()
    window = split_walk_forward(bars, train_size=150, test_size=50, warmup=30)[0].test
    curve = buy_and_hold_curve(window, SYMBOL)
    assert all(p.timestamp >= window.start for p in curve)


def test_buy_and_hold_on_an_absent_symbol_raises() -> None:
    window = TestWindow(bars=tuple(mixed_regime_bars()[:50]))
    with pytest.raises(ValueError, match="no bars"):
        buy_and_hold_curve(window, "ETHUSDT")


# -- The type-level split -----------------------------------------------------

_LEAK_SNIPPET = """
from trading_platform.analytics.walkforward import (
    BacktesterFactory,
    StrategyFactory,
    evaluate,
    sweep,
)
from trading_platform.analytics.windows import TestWindow, TrainWindow


def fit_on_the_test_set(
    test: TestWindow,
    train: TrainWindow,
    make_strategy: StrategyFactory,
    make_backtester: BacktesterFactory,
) -> None:
    sweep(
        {},
        window=test,
        strategy_factory=make_strategy,
        backtester_factory=make_backtester,
    )
    evaluate(
        {},
        window=train,
        strategy_factory=make_strategy,
        backtester_factory=make_backtester,
    )
"""


#: mypy must run from the repository root so it reads pyproject.toml's strict
#: settings and can resolve `trading_platform`. Derived from this file rather
#: than written down: the literal that used to be here was the authoring
#: sandbox's own path, so the guard raised FileNotFoundError on every other
#: machine, CI included, and had never once type-checked anything. T-23095c48.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_fitting_on_a_test_window_is_a_type_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The plan's central claim: the split is enforced by the type system, not
    by a comment. If this stops failing, the guard has been removed."""
    snippet = tmp_path / "leak.py"
    snippet.write_text(_LEAK_SNIPPET)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-error-summary",
            "--follow-imports=silent",
            str(snippet),
        ],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    if "mypy" in proc.stderr and "No module named" in proc.stderr:
        pytest.skip("mypy not importable in this interpreter")

    assert proc.returncode != 0, "fitting on the test set should not type-check"
    # Both directions of the swap must be rejected, and these are the ONLY
    # errors the snippet contains — so the guard is what failed it, not noise.
    assert 'Argument "window" to "sweep" has incompatible type "TestWindow"' in proc.stdout, (
        f"sweep() accepted a TestWindow:\n{proc.stdout}"
    )
    assert 'Argument "window" to "evaluate" has incompatible type "TrainWindow"' in proc.stdout, (
        f"evaluate() accepted a TrainWindow:\n{proc.stdout}"
    )
    assert proc.stdout.count("error:") == 2, f"unexpected extra errors:\n{proc.stdout}"


def test_the_two_window_types_are_not_interchangeable_at_runtime_either() -> None:
    bars = tuple(mixed_regime_bars()[:50])
    assert not isinstance(TrainWindow(bars=bars), TestWindow)
    assert not isinstance(TestWindow(bars=bars), TrainWindow)


# -- Test helper --------------------------------------------------------------


def _report_with_scores(
    in_sample: list[float],
    out_of_sample: list[float],
    *,
    fills: int | list[int] = 5,
    benchmark: list[float] | None = None,
    profitable_fraction: float = 1.0,
    neighbour_ratio_scores: list[float] | None = None,
    params_per_fold: str = "same",
) -> WalkForwardReport:
    """Build a report from chosen numbers, to test the verdict logic itself
    rather than re-testing the engine through it."""
    fill_counts = fills if isinstance(fills, list) else [fills] * len(in_sample)
    n_profitable = round(profitable_fraction * 4)
    scores = neighbour_ratio_scores or [1.0, 1.0, 1.0]

    def summary(total_return: float) -> PerformanceSummary:
        return PerformanceSummary(
            total_return=total_return,
            cagr=0.0,
            annualised_volatility=0.0,
            sharpe_ratio=0.0,
            sortino_ratio=0.0,
            max_drawdown=0.0,
            max_drawdown_duration=0,
            calmar_ratio=0.0,
            win_rate=0.0,
            num_periods=50,
        )

    cells = tuple(
        SweepCell(
            params={"p": i},
            coord=(i,),
            score=scores[i % len(scores)],
            summary=summary(1.0 if i < n_profitable else -1.0),
            fills=3,
            halted=False,
        )
        for i in range(4)
    )
    surface = SweepSurface(cells=cells, axes=("p",), axis_values=(tuple(range(4)),))
    # The chosen parameters must be a cell that exists on this surface. In a
    # real run `walk_forward` sets `params=best.params` from the surface it
    # just swept, so a fixture whose folds pick a key the surface has never
    # heard of is not a smaller version of a run — it is a different object.
    # It went unnoticed while `parameter_stability` compared tuples and never
    # consulted the surface.
    best_index = max(range(4), key=lambda i: cells[i].score)

    folds = tuple(
        FoldResult(
            index=i,
            params={"p": i % 4 if params_per_fold == "varying" else best_index},
            surface=surface,
            in_sample=summary(0.1),
            out_of_sample=summary(0.1),
            in_sample_score=is_score,
            out_of_sample_score=oos_score,
            train_start=START,
            train_end=START,
            test_start=START,
            test_end=START,
            out_of_sample_fills=fill_counts[i],
            halted_out_of_sample=False,
            benchmark=None if benchmark is None else summary(0.1),
            benchmark_score=None if benchmark is None else benchmark[i],
        )
        for i, (is_score, oos_score) in enumerate(zip(in_sample, out_of_sample, strict=True))
    )
    return WalkForwardReport(folds=folds)


# -- Fills accounting: the guard that decides whether a fold is a measurement --


def _drifting_bars(count: int, *, seed: int = 0, symbol: str = "AAA") -> list[Bar]:
    rng = random.Random(seed)
    price = 100.0
    out: list[Bar] = []
    for i in range(count):
        open_ = price
        price = max(1.0, price * (1 + rng.gauss(0, 0.02)))
        out.append(
            Bar(
                symbol=symbol,
                timestamp=START + timedelta(days=i),
                open=Decimal(f"{open_:.4f}"),
                high=Decimal(f"{max(open_, price) * 1.001:.4f}"),
                low=Decimal(f"{min(open_, price) * 0.999:.4f}"),
                close=Decimal(f"{price:.4f}"),
                volume=Decimal("1000"),
            )
        )
    return out


def test_evaluate_counts_only_fills_inside_the_scored_window() -> None:
    """A `TestWindow`'s feed is `warmup + bars`, so a run over it fills in both
    regions. `score_slice` trims the equity curve; the fill count did not.

    Measured on the shape the UI actually runs — 120 warm-up bars against a
    100-bar test window — the count read 13 where 6 fills were in the scored
    region, a 2.17x overstatement. That number feeds
    `median_fills_per_fold < 10`, which is what decides whether a fold has
    enough trades for its statistics to be a measurement, so the ten-fill
    guard was an effective five-fill guard.
    """
    bars = _drifting_bars(400)
    window = TestWindow(bars=tuple(bars[120:220]), warmup=tuple(bars[:120]))

    _, _, reported, _, all_fills = evaluate(
        {"fast": 5, "slow": 20},
        window=window,
        strategy_factory=lambda p: SMACrossover("AAA", fast=int(p["fast"]), slow=int(p["slow"])),
        backtester_factory=lambda: Backtester(
            instruments={"AAA": crypto_spot("AAA")},
            starting_cash=Decimal("100000"),
            cost_model=ZERO_COST,
            risk_manager=RiskManager(limits=UNCONSTRAINED),
        ),
    )

    inside = [f for f in all_fills if f.timestamp >= window.start]
    assert reported == len(inside)
    assert reported < len(all_fills), (
        "this fixture must produce warm-up fills, or the test proves nothing"
    )


def test_a_round_trip_opened_in_warmup_and_closed_in_window_is_not_a_short() -> None:
    """The trap in fixing the fill count.

    Pairing only the fills at or after the window start orphans an exit whose
    entry happened during warm-up, and FIFO pairing reads that lone sell as
    *opening a short* — inventing a round trip in the wrong direction, with
    P&L of the wrong sign. So the fold keeps every fill and attribution is by
    closing time.

    Built by hand rather than hoping a strategy holds a position across the
    boundary: at one seed it did not, and the guard silently proved nothing.
    """
    instruments = {"AAA": crypto_spot("AAA")}
    window_start = START + timedelta(days=120)
    entry = Fill(
        order_id="1",
        symbol="AAA",
        timestamp=START + timedelta(days=100),  # during warm-up
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
    )
    exit_ = Fill(
        order_id="2",
        symbol="AAA",
        timestamp=window_start + timedelta(days=5),  # inside the scored window
        side=Side.SELL,
        quantity=Decimal("1"),
        price=Decimal("110"),
    )

    naive = summarise_trades(
        [f for f in (entry, exit_) if f.timestamp >= window_start], instruments=instruments
    )
    correct = summarise_trades([entry, exit_], instruments=instruments)

    assert [t.direction for t in naive.trades] == [], (
        "an orphaned sell must not become a closed trade at all"
    )
    assert naive.open_quantity == {"AAA": Decimal("-1")}, (
        "the orphaned sell becomes a phantom SHORT position of -1"
    )
    assert [t.direction for t in correct.trades] == [Side.BUY]
    assert correct.trades[0].net_pnl == Decimal("10")


def test_a_sweep_records_the_cells_the_strategy_rejected() -> None:
    """`skip_invalid` drops parameter sets the strategy refuses, so the
    effective trial count is smaller than the grid — and nothing recorded the
    difference. A 3x3 SMA grid over overlapping values has three live cells,
    not nine, and any multiplicity correction computed from the nominal size
    would be wrong from the start.
    """
    surface = sweep(
        {"fast": [5, 10, 20], "slow": [5, 10, 20]},
        window=TrainWindow(bars=tuple(_drifting_bars(300))),
        strategy_factory=lambda p: SMACrossover("AAA", fast=int(p["fast"]), slow=int(p["slow"])),
        backtester_factory=lambda: Backtester(
            instruments={"AAA": crypto_spot("AAA")},
            starting_cash=Decimal("100000"),
            cost_model=ZERO_COST,
            risk_manager=RiskManager(limits=UNCONSTRAINED),
        ),
    )
    assert surface.attempted == 9
    assert len(surface.cells) == 3
    assert surface.skipped == 6


def test_fold_trades_are_paired_within_folds_not_pooled_across_them() -> None:
    """Each fold is an independently simulated backtest that starts flat.

    With `warmup > 0` a fold's warm-up window reaches back into calendar bars
    the previous fold already scored, so pooling every fold's fills into one
    globally sorted stream interleaves two unrelated simulations that happen
    to share a period. FIFO pairing then matches an entry from one against an
    exit from the other, inventing round trips that occurred in no single run.
    At the default 120-bar warm-up against 100-bar test windows, four of five
    fold transitions overlap.

    Measured at the default config: pooling reported 19 trades and 130,849 of
    net P&L where per-fold pairing gives 14 and 19,756 — a 6.6x overstatement
    of a figure shown to the user.
    """
    from trading_platform.ui.runner import RunConfig, execute

    outcome = execute(RunConfig())
    instruments = {"BTCUSDT": crypto_spot("BTCUSDT")}

    pooled_fills = [fill for fold in outcome.report.folds for fill in fold.fills]
    pooled_fills.sort(key=lambda f: (f.timestamp, f.order_id))
    pooled = summarise_trades(pooled_fills, instruments)

    per_fold = sum(
        len(summarise_trades(list(fold.fills), instruments).trades) for fold in outcome.report.folds
    )

    assert outcome.verdict.trades == per_fold
    assert pooled.count > per_fold, (
        "this fixture must actually produce phantom pairings, or the guard proves nothing"
    )
    assert abs(pooled.total_net_pnl) > abs(outcome.verdict.net_pnl)


# -- Carrying parameters forward ----------------------------------------------


def test_score_for_finds_a_cell_and_reports_absence_honestly() -> None:
    report = _report_with_scores(in_sample=[1.0] * 2, out_of_sample=[1.0] * 2)
    surface = report.folds[0].surface
    assert surface.score_for({"p": 0}) is not None
    assert surface.score_for({"p": 99}) is None, "a cell the grid never had"
    assert surface.score_for({"fast": 5}) is None, "a different axis entirely"


def test_a_fold_with_no_positive_edge_has_nothing_to_carry() -> None:
    """A ratio against a non-positive best is not a number to act on."""
    report = _report_with_scores(in_sample=[-1.0] * 4, out_of_sample=[-1.0] * 4, fills=20)
    assert report.parameter_stability == 0.0


def test_the_carry_forward_tolerance_is_not_load_bearing() -> None:
    """The null retains a median 13% of the re-optimised best and a genuine
    edge 95%, so any threshold between them classifies both the same way.

    Asserted because a constant chosen from a measurement should be shown to
    have slack — if the answer flipped at 0.75 or 0.85, the number would be
    doing the work rather than the evidence.
    """
    import trading_platform.analytics.walkforward as wf

    plateau = _report_with_scores(
        in_sample=[1.0] * 4, out_of_sample=[1.0] * 4, fills=20, params_per_fold="varying"
    )
    spiky = _report_with_scores(
        in_sample=[1.0] * 4,
        out_of_sample=[1.0] * 4,
        fills=20,
        params_per_fold="varying",
        neighbour_ratio_scores=[1.0, 0.1, 0.1],
    )
    original = wf.CARRY_FORWARD_TOLERANCE
    try:
        for tolerance in (0.3, 0.5, 0.7, 0.8, 0.9):
            wf.CARRY_FORWARD_TOLERANCE = tolerance
            assert plateau.parameter_stability == 1.0, f"plateau flipped at {tolerance}"
            assert spiky.parameter_stability < 0.5, f"spike flipped at {tolerance}"
    finally:
        wf.CARRY_FORWARD_TOLERANCE = original


def test_parameter_changes_still_counts_identity() -> None:
    """It stops being what a gate fires on; it stays a fact worth reporting."""
    plateau = _report_with_scores(
        in_sample=[1.0] * 4, out_of_sample=[1.0] * 4, fills=20, params_per_fold="varying"
    )
    assert plateau.parameter_changes == 3
    assert plateau.parameter_stability == 1.0, "changed every fold, and cost nothing"
