"""Performance metrics, and the conditions under which they lie.

Most of the metric arithmetic is exercised through the engine tests. This file
covers the part that arithmetic alone cannot: when a correctly-computed number
should not be believed.

The motivating case is real. Running SMACrossover on genuine 1-minute BTC data,
the strategy held a position for 6% of bars and the annualised Sharpe came out
at **12.81**, with an annualised volatility of 24,807%. Nothing was wrong with
the calculation — the per-bar mean/sd ratio was 0.0177, and sqrt(365 x 1440) is
725. Annualisation assumes i.i.d. returns, and a curve that is flat 94% of the
time violates that so badly the output stops describing the strategy.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.analytics.metrics import (
    MIN_ANNUALISABLE_YEARS,
    PerformanceSummary,
    cagr,
    flat_period_fraction,
    summarise,
)
from trading_platform.engine.portfolio import EquityPoint

START = datetime(2026, 1, 1, tzinfo=UTC)


def curve(values: list[str]) -> list[EquityPoint]:
    return [
        EquityPoint(
            timestamp=START + timedelta(minutes=i),
            equity=Decimal(v),
            cash=Decimal(v),
            positions_value=Decimal("0"),
        )
        for i, v in enumerate(values)
    ]


# -- The flat-period guard ---------------------------------------------------


def test_a_mostly_flat_curve_refuses_to_vouch_for_its_own_sharpe() -> None:
    """The real failure: 94% flat, Sharpe 12.81, and the calculation correct."""
    values = ["100"] * 50 + ["101"] + ["101"] * 49
    summary = summarise(curve(values), periods_per_year=365 * 1440)

    assert summary.flat_period_fraction > 0.9
    assert not summary.annualisation_is_meaningful
    assert "WARNING" in summary.report()
    assert "artefacts of the" in summary.report()


def test_an_actively_traded_curve_vouches_for_itself() -> None:
    values = [str(100 + (i % 7) - 3) for i in range(200)]
    summary = summarise(curve(values), periods_per_year=365)

    assert summary.flat_period_fraction < 0.2
    assert summary.annualisation_is_meaningful
    assert "WARNING" not in summary.report()


def test_the_warning_threshold_is_where_it_is_documented() -> None:
    """80% flat is the stated line; both sides of it are pinned."""
    assert PerformanceSummary(
        *[0.0] * 9, num_periods=100, flat_period_fraction=0.79
    ).annualisation_is_meaningful
    assert not PerformanceSummary(
        *[0.0] * 9, num_periods=100, flat_period_fraction=0.80
    ).annualisation_is_meaningful


@pytest.mark.parametrize(
    ("returns", "expected"),
    [([], 0.0), ([0.0, 0.0], 1.0), ([0.0, 0.1], 0.5), ([0.1, 0.2], 0.0)],
)
def test_flat_period_fraction_counts_exact_zeros(returns: list[float], expected: float) -> None:
    assert flat_period_fraction(returns) == expected


def test_the_fraction_survives_the_dict_and_report_round_trip() -> None:
    summary = summarise(curve(["100"] * 20), periods_per_year=365)
    assert summary.as_dict()["flat_period_fraction"] == 1.0
    assert "Flat periods" in summary.report()


# -- Why the guard exists, demonstrated ---------------------------------------


def test_annualisation_scales_a_tiny_ratio_into_a_large_one() -> None:
    """The mechanism, in one assertion. A per-bar ratio no one would look at
    twice becomes a headline Sharpe purely through sqrt(periods_per_year)."""
    per_bar_ratio = 0.0177  # measured on the real BTC run

    daily = per_bar_ratio * math.sqrt(365)
    minutely = per_bar_ratio * math.sqrt(365 * 1440)

    assert daily == pytest.approx(0.34, abs=0.01)
    assert minutely == pytest.approx(12.8, abs=0.1)
    assert minutely / daily == pytest.approx(math.sqrt(1440), rel=1e-6)


def test_an_empty_or_single_point_curve_does_not_explode() -> None:
    for values in ([], ["100"]):
        summary = summarise(curve(values), periods_per_year=365)
        assert summary.sharpe_ratio == 0.0
        assert summary.flat_period_fraction == 0.0


# -- Annualising a window too short to annualise -----------------------------
#
# Found by running examples/run_walk_forward_btc.py — roadmap step 35 — on the
# real archive. The first fold raised OverflowError out of `cagr` before any
# verdict could be reached.


MINUTES_PER_YEAR = 365 * 24 * 60


def test_cagr_refuses_a_window_shorter_than_a_month() -> None:
    """1,000 one-minute bars is 17 hours. There is no annual growth rate in it.

    The exponent is `1 / years` = 526 here, so the output describes the
    compounding rather than the strategy.
    """
    assert cagr(curve(["100"] * 999 + ["102"]), MINUTES_PER_YEAR) is None


def test_cagr_does_not_raise_on_the_case_that_used_to_overflow() -> None:
    """The bug that stopped step 35: a 4% gain over 1,000 minutes.

    `(1.04) ** 526` exceeds a float, so this raised OverflowError out of the
    walk-forward harness. The crash was the kind outcome — a 2% gain returned a
    finite 3,300,000% and would have been printed as a result.
    """
    assert cagr(curve(["100"] * 999 + ["104"]), MINUTES_PER_YEAR) is None


def test_cagr_still_answers_when_the_window_is_long_enough() -> None:
    """The bound must not swallow the legitimate case.

    Two years of daily bars, doubling: about 41.4% a year.
    """
    values = ["100"] * 730 + ["200"]
    result = cagr(curve(values), 365)
    assert result is not None
    assert math.isclose(result, 0.4142, abs_tol=0.001)


def test_the_bound_is_a_boundary_not_a_blanket() -> None:
    """Just over a month answers; just under refuses.

    Pinned on both sides, because a one-sided check would pass just as happily
    against a function that always returned None.
    """
    minimum = int(MINUTES_PER_YEAR * MIN_ANNUALISABLE_YEARS)
    assert cagr(curve(["100"] * (minimum + 10) + ["110"]), MINUTES_PER_YEAR) is not None
    assert cagr(curve(["100"] * (minimum - 200) + ["110"]), MINUTES_PER_YEAR) is None


def test_calmar_is_absent_whenever_cagr_is() -> None:
    """Calmar is CAGR over max drawdown, so it cannot outlive its numerator."""
    summary = summarise(
        curve(["100", "120", "90", "130", "110"]), periods_per_year=MINUTES_PER_YEAR
    )
    assert summary.cagr is None
    assert summary.calmar_ratio is None


def test_a_missing_cagr_reads_as_n_a_rather_than_zero() -> None:
    """A reader scanning a column must not mistake "no answer" for "no growth"."""
    summary = summarise(
        curve(["100", "120", "90", "130", "110"]), periods_per_year=MINUTES_PER_YEAR
    )
    report = summary.report()
    assert "CAGR                :        n/a" in report
    assert "Calmar              :        n/a" in report
    assert "0.00%" not in report.split("CAGR")[1].split("\n")[0]


def test_total_return_still_answers_when_cagr_will_not() -> None:
    """The statistic a reader should fall back to needs no time base at all."""
    summary = summarise(curve(["100"] * 999 + ["104"]), periods_per_year=MINUTES_PER_YEAR)
    assert summary.cagr is None
    assert math.isclose(summary.total_return, 0.04, abs_tol=1e-9)
