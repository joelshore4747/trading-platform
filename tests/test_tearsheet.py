"""Chart maths and the tearsheet page.

A tearsheet makes claims — that this region was out-of-sample, that this line is
the benchmark, that the axis is logarithmic. Claims should be verified, which is
the whole reason the charts are hand-written SVG rather than an opaque PNG.

The page tests are mostly about what the page *refuses to omit*: the cost model,
the verdicts, and the warning that a lone run is in-sample.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.analytics.charts import (
    Box,
    Scale,
    histogram,
    legend,
    line_chart,
    nice_step,
    points_to_area,
    points_to_path,
)
from trading_platform.analytics.tearsheet import (
    _underwater_panel,
    render_tearsheet,
    write_tearsheet,
)
from trading_platform.analytics.walkforward import buy_and_hold_curve, walk_forward
from trading_platform.analytics.windows import TestWindow
from trading_platform.core.instruments import crypto_spot
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.data.synthetic import Regime, RegimeSpec, generate_bars
from trading_platform.engine.backtester import Backtester
from trading_platform.engine.execution import ZERO_COST, CostModel
from trading_platform.engine.portfolio import EquityPoint
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.base import Strategy
from trading_platform.strategy.examples.sma_crossover import SMACrossover

SYMBOL = "BTCUSDT"
START = datetime(2021, 1, 1, tzinfo=UTC)
INSTRUMENTS = {SYMBOL: crypto_spot(SYMBOL)}
BOX = Box()


# -- Scales ------------------------------------------------------------------


def test_a_linear_scale_maps_the_domain_onto_the_range() -> None:
    scale = Scale(0.0, 100.0, 0.0, 200.0)
    assert scale.to(0.0) == 0.0
    assert scale.to(50.0) == 100.0
    assert scale.to(100.0) == 200.0


def test_a_scale_can_run_backwards_for_svg_y_coordinates() -> None:
    """SVG y grows downward, so a value scale runs bottom to top."""
    scale = Scale(0.0, 100.0, 250.0, 10.0)
    assert scale.to(0.0) == 250.0
    assert scale.to(100.0) == 10.0


def test_log_and_linear_place_the_same_point_differently() -> None:
    """The claim the equity panel makes: on a linear axis a late gain draws
    larger than an equal-percentage early one. Log removes that."""
    linear = Scale(100.0, 10000.0, 0.0, 100.0)
    logarithmic = Scale(100.0, 10000.0, 0.0, 100.0, log=True)

    # 1000 is the geometric midpoint of 100..10000 — halfway on a log axis.
    assert logarithmic.to(1000.0) == pytest.approx(50.0)
    # On a linear axis it sits in the first tenth.
    assert linear.to(1000.0) == pytest.approx(9.09, abs=0.01)
    assert logarithmic.to(1000.0) != pytest.approx(linear.to(1000.0))


def test_a_log_scale_refuses_a_non_positive_domain() -> None:
    with pytest.raises(ValueError, match="the account was gone"):
        Scale(0.0, 100.0, 0.0, 10.0, log=True)


def test_a_flat_series_draws_instead_of_dividing_by_zero() -> None:
    """A zero-variance run is a result; it must render, not raise."""
    scale = Scale(100.0, 100.0, 0.0, 200.0)
    assert scale.to(100.0) == pytest.approx(100.0)


def test_log_ticks_are_evenly_spaced_in_pixels_not_in_value() -> None:
    scale = Scale(100.0, 10000.0, 0.0, 100.0, log=True)
    ticks = scale.ticks(3)
    assert ticks[0] == pytest.approx(100.0)
    assert ticks[1] == pytest.approx(1000.0)
    assert ticks[2] == pytest.approx(10000.0)
    positions = [scale.to(t) for t in ticks]
    assert positions[1] - positions[0] == pytest.approx(positions[2] - positions[1])


@pytest.mark.parametrize(
    ("rough", "expected"),
    [(0.0665, 0.1), (4.0, 5.0), (16000.0, 20000.0), (1.0, 1.0), (0.0, 1.0)],
)
def test_a_rough_interval_rounds_up_to_a_readable_one(rough: float, expected: float) -> None:
    assert nice_step(rough) == pytest.approx(expected)


def test_axis_ticks_are_round_numbers_not_exact_divisions() -> None:
    """An axis labelled 39,175 / 79,982 / 163,294 is correct and unreadable."""
    ticks = Scale(39175.0, 700000.0, 0.0, 100.0, log=True).ticks()
    assert ticks == [50000.0, 100000.0, 200000.0, 500000.0]

    linear = Scale(-0.266, 0.0, 100.0, 0.0).ticks()
    assert linear == pytest.approx([-0.2, -0.1, 0.0])


def test_ticks_never_fall_outside_the_domain() -> None:
    """A tick outside the domain draws its gridline outside the plot."""
    for scale in (
        Scale(37.0, 912.0, 0.0, 100.0),
        Scale(37.0, 912.0, 0.0, 100.0, log=True),
    ):
        assert all(37.0 <= t <= 912.0 for t in scale.ticks())


def test_a_scale_needs_at_least_two_ticks() -> None:
    with pytest.raises(ValueError, match="at least 2 ticks"):
        Scale(0.0, 1.0, 0.0, 1.0).ticks(1)


# -- Paths -------------------------------------------------------------------


def test_a_path_visits_every_point_in_order() -> None:
    assert points_to_path([(0.0, 1.0), (2.0, 3.0)]) == "M 0.00 1.00 L 2.00 3.00"


def test_an_area_closes_back_to_the_baseline() -> None:
    path = points_to_area([(0.0, 10.0), (5.0, 20.0)], baseline=100.0)
    assert path.startswith("M 0.00 10.00")
    assert path.endswith("Z")
    assert "L 5.00 100.00" in path
    assert "L 0.00 100.00" in path


@pytest.mark.parametrize("fn", [points_to_path, lambda p: points_to_area(p, 0.0)])
def test_an_empty_series_produces_an_empty_path(fn) -> None:  # type: ignore[no-untyped-def]
    assert fn([]) == ""


# -- Charts ------------------------------------------------------------------


def simple_chart(**kwargs: object) -> str:
    return line_chart(
        [("Strategy", "--series-1", [(0.0, 1.0), (1.0, 2.0)])],
        box=BOX,
        y_scale=Scale(0.0, 3.0, BOX.plot_bottom, BOX.plot_top),
        x_scale=Scale(0.0, 1.0, BOX.plot_left, BOX.plot_right),
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_chart_names_its_series_for_a_screen_reader() -> None:
    svg = simple_chart(label="Equity curve")
    assert 'role="img"' in svg
    assert 'aria-label="Equity curve"' in svg
    assert 'data-name="Strategy"' in svg


def test_out_of_sample_regions_are_shaded_behind_the_marks() -> None:
    """The shading is the page's claim about which data was unseen. If it stops
    rendering, the page silently starts presenting fitted data as held out."""
    svg = simple_chart(shaded=[(0.25, 0.75)])
    assert 'class="oos"' in svg
    # Behind: the shading must precede the series in document order.
    assert svg.index('class="oos"') < svg.index('class="series"')


def test_a_chart_uses_css_variables_rather_than_baked_in_colour() -> None:
    """Hard-coded hex would break dark mode, which the palette is validated for."""
    svg = simple_chart()
    assert "var(--series-1)" in svg
    assert not re.search(r"#[0-9a-fA-F]{6}", svg)


def test_a_zero_rule_is_drawn_only_when_asked() -> None:
    assert 'class="zero"' in simple_chart(zero_rule=True)
    assert 'class="zero"' not in simple_chart()


def test_a_histogram_splits_wins_and_losses_at_zero() -> None:
    """Polarity, so the two arms take the diverging pair rather than two
    arbitrary categorical slots."""
    svg = histogram([-5.0, -3.0, 4.0, 6.0], box=BOX)
    assert "var(--series-8)" in svg  # losing arm
    assert "var(--series-1)" in svg  # winning arm
    assert 'class="zero"' in svg


def test_a_histogram_with_no_trades_says_so_rather_than_drawing_nothing() -> None:
    assert "No closed trades" in histogram([], box=BOX)


def test_too_few_trades_prints_the_figures_instead_of_binning_them() -> None:
    """The dataviz anti-pattern: a one-bar bar chart is a stat tile written the
    long way, and it implies a distribution that is not there."""
    output = histogram([51206.72], box=BOX)
    assert "<svg" not in output
    assert "51,206.72" in output
    assert "Too few trades to have a distribution" in output

    assert "<svg" in histogram([-5.0, 3.0, 8.0], box=BOX)


def test_a_histogram_of_identical_values_still_renders() -> None:
    assert "<svg" in histogram([5.0, 5.0, 5.0], box=BOX)


def test_a_legend_appears_for_two_series_and_not_for_one() -> None:
    """Identity must never rest on colour alone."""
    assert legend([("Strategy", "--series-1")]) == ""
    two = legend([("Strategy", "--series-1"), ("Buy and hold", "--series-2")])
    assert "Buy and hold" in two and "Strategy" in two


# -- The page ----------------------------------------------------------------


def make_strategy(params: dict[str, object]) -> Strategy:
    return SMACrossover(SYMBOL, **params)  # type: ignore[arg-type]


def make_backtester(cost_model: CostModel | None = None) -> Backtester:
    return Backtester(
        instruments=INSTRUMENTS,
        starting_cash=Decimal("100000"),
        cost_model=cost_model or CostModel(),
        risk_manager=RiskManager(limits=RiskLimits()),
    )


@pytest.fixture(scope="module")
def bars() -> list:  # type: ignore[type-arg]
    return generate_bars(
        SYMBOL,
        START,
        [
            RegimeSpec(Regime.TREND, 150),
            RegimeSpec(Regime.CHOP, 150),
            RegimeSpec(Regime.CRASH, 50),
            RegimeSpec(Regime.RECOVERY, 100),
        ],
        seed=3,
        start_price=Decimal("30000"),
    )


@pytest.fixture(scope="module")
def result(bars):  # type: ignore[no-untyped-def]
    return make_backtester().run(SMACrossover(SYMBOL, fast=5, slow=30), InMemoryBarFeed(bars))


def test_the_page_is_entirely_self_contained(result) -> None:  # type: ignore[no-untyped-def]
    """No external stylesheet, script, font, or image. A tearsheet that needs
    the network is not a deliverable."""
    page = render_tearsheet(result, cost_model=CostModel(), instruments=INSTRUMENTS)

    # The SVG XML namespace is an identifier, not a fetch — nothing resolves it.
    # Everything else that looks like a URL would be a network dependency.
    remaining = page.replace("http://www.w3.org/2000/svg", "")
    assert "http://" not in remaining
    assert "https://" not in remaining
    assert "<img" not in page
    assert "@import" not in page
    assert "<link" not in page
    assert "url(" not in page
    assert "src=" not in page


def test_the_cost_model_numbers_appear_on_the_page(result) -> None:  # type: ignore[no-untyped-def]
    """A chart with unstated costs is not a result."""
    page = render_tearsheet(
        result,
        cost_model=CostModel(commission_rate=Decimal("0.0025")),
        instruments=INSTRUMENTS,
    )
    assert "commission 25.0 bp" in page


def test_a_frictionless_run_renders_a_banner_saying_it_is_not_a_result(result) -> None:  # type: ignore[no-untyped-def]
    page = render_tearsheet(result, cost_model=ZERO_COST, instruments=INSTRUMENTS)
    assert "Frictionless run" in page
    assert "not a result" in page


def test_a_lone_run_is_labelled_in_sample(result) -> None:  # type: ignore[no-untyped-def]
    """Without a walk-forward report the page must say the number is in-sample
    by construction, rather than letting the chart imply otherwise."""
    page = render_tearsheet(result, cost_model=CostModel(), instruments=INSTRUMENTS)
    assert "In-sample" in page
    assert "/walk-forward" in page


def test_every_walk_forward_verdict_appears_above_the_equity_chart(bars, result) -> None:  # type: ignore[no-untyped-def]
    """Caveats placed under a chart are caveats nobody reads. This test is the
    only thing keeping them above it."""
    report = walk_forward(
        bars,
        {"fast": [5, 10], "slow": [30, 50]},
        strategy_factory=make_strategy,
        backtester_factory=make_backtester,
        train_size=200,
        test_size=75,
        warmup=50,
        benchmark_symbol=SYMBOL,
    )
    assert report.verdicts, "this fixture is meant to raise objections"

    page = render_tearsheet(
        result, cost_model=CostModel(), instruments=INSTRUMENTS, walk_forward=report
    )
    first_chart = page.index("<svg")
    for verdict in report.verdicts:
        position = page.find(verdict[:60])
        assert position != -1, f"verdict missing from the page: {verdict}"
        assert position < first_chart, "a verdict rendered below the equity chart"


def test_out_of_sample_windows_are_shaded_on_the_equity_chart(bars, result) -> None:  # type: ignore[no-untyped-def]
    report = walk_forward(
        bars,
        {"fast": [5], "slow": [30]},
        strategy_factory=make_strategy,
        backtester_factory=make_backtester,
        train_size=200,
        test_size=75,
        warmup=50,
    )
    page = render_tearsheet(
        result, cost_model=CostModel(), instruments=INSTRUMENTS, walk_forward=report
    )
    assert page.count('class="oos"') == len(report.folds)
    assert "Shaded bands are out-of-sample windows" in page


def test_the_benchmark_is_drawn_on_the_same_axis(bars, result) -> None:  # type: ignore[no-untyped-def]
    """One axis, always. Two y-scales would invent a relationship."""
    benchmark = buy_and_hold_curve(
        TestWindow(bars=tuple(bars)), SYMBOL, starting_cash=Decimal("100000")
    )
    page = render_tearsheet(
        result, cost_model=CostModel(), instruments=INSTRUMENTS, benchmark=benchmark
    )
    assert "Buy and hold (cost-free)" in page
    assert "var(--series-2)" in page


def test_the_equity_axis_is_logarithmic_by_default(result) -> None:  # type: ignore[no-untyped-def]
    default = render_tearsheet(result, cost_model=CostModel(), instruments=INSTRUMENTS)
    linear = render_tearsheet(
        result, cost_model=CostModel(), instruments=INSTRUMENTS, log_scale=False
    )
    assert "Logarithmic equity axis" in default
    assert "Linear equity axis" in linear


def test_both_win_rates_are_labelled_so_they_cannot_be_confused(result) -> None:  # type: ignore[no-untyped-def]
    page = render_tearsheet(result, cost_model=CostModel(), instruments=INSTRUMENTS)
    assert "Win rate (per period)" in page
    assert "Win rate (per trade)" in page


def test_the_page_carries_a_dark_mode_definition(result) -> None:  # type: ignore[no-untyped-def]
    """The palette is validated in both modes; the page must actually use it."""
    page = render_tearsheet(result, cost_model=CostModel(), instruments=INSTRUMENTS)
    assert "prefers-color-scheme: dark" in page
    assert '[data-theme="dark"]' in page


def test_an_empty_run_refuses_to_render(result) -> None:  # type: ignore[no-untyped-def]
    """A tearsheet of nothing would imply a result that does not exist."""
    empty = make_backtester().run(SMACrossover(SYMBOL), InMemoryBarFeed([]))
    with pytest.raises(ValueError, match="nothing to draw"):
        render_tearsheet(empty, cost_model=CostModel(), instruments=INSTRUMENTS)


def test_writing_creates_missing_directories(result, tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = write_tearsheet(
        result,
        tmp_path / "nested" / "deeper" / "sheet.html",
        cost_model=CostModel(),
        instruments=INSTRUMENTS,
    )
    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_a_run_whose_profit_is_one_trade_says_so_on_the_page(result) -> None:  # type: ignore[no-untyped-def]
    """The concentration verdict must reach the reader, not just the API."""
    page = render_tearsheet(result, cost_model=CostModel(), instruments=INSTRUMENTS)
    assert "Removing the single best takes net P&amp;L" in page


def test_html_in_a_strategy_name_is_escaped(result) -> None:  # type: ignore[no-untyped-def]
    page = render_tearsheet(
        result,
        cost_model=CostModel(),
        instruments=INSTRUMENTS,
        title="<script>alert(1)</script>",
    )
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_rolling_sharpe_is_skipped_when_the_series_is_too_short(result) -> None:  # type: ignore[no-untyped-def]
    page = render_tearsheet(
        result, cost_model=CostModel(), instruments=INSTRUMENTS, rolling_window=100_000
    )
    assert "Series too short for this window" in page


def test_the_underwater_panel_never_shows_a_positive_drawdown() -> None:
    """Drawdown is measured from the running peak, so it cannot be above zero.
    A positive value would mean the peak logic is wrong.

    This test previously reimplemented the peak-and-depth loop inline and
    asserted on its own arithmetic, never calling `_underwater_panel` at all —
    arithmetic asserting itself. Flipping the sign in the real function left it
    green, along with all 43 tests in this file. Found by `guard-auditor` on an
    unmutated tree during the first agent-eval run.

    It reads the rendered axis now, and the assertion was checked against that
    same mutation: with the sign flipped every depth is positive,
    `min([*depths, 0.0])` collapses the scale to 0..0, and the axis renders
    +50%/+100% ticks. So it fails by the intended mechanism, not by luck.
    """
    curve = [
        EquityPoint(
            timestamp=START + timedelta(days=i),
            equity=Decimal(str(v)),
            cash=Decimal(str(v)),
            positions_value=Decimal("0"),
        )
        for i, v in enumerate([100, 120, 90, 130, 110])
    ]

    labels = [float(m) for m in re.findall(r"(-?\d+\.\d)%", _underwater_panel(curve).body)]

    assert labels, "the panel rendered no axis labels at all"
    assert any(value < 0 for value in labels), (
        "a curve that fell 25% from its peak must render a negative tick; "
        "an all-zero axis would pass the next assertion vacuously"
    )
    assert max(labels) <= 0.0, f"drawdown axis shows a positive value: {sorted(labels)}"
