"""Performance metrics.

Decimal is used for money; these statistics convert to float at the boundary
because float is the right type for ratios and the precision argument does not
apply. Every function is defensive about short or degenerate series — a metric
that raises on a 3-bar backtest is a metric nobody runs.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from trading_platform.engine.portfolio import EquityPoint

TRADING_DAYS_PER_YEAR = 252
CRYPTO_DAYS_PER_YEAR = 365

#: Shortest window this module will annualise a growth rate over: one twelfth
#: of a year, about 30.4 days.
#:
#: CAGR raises the period return to the power ``1 / years``, so a short window
#: is not merely imprecise — it is an extrapolation whose exponent grows without
#: bound as the window shrinks. Over 1,000 one-minute bars (about 17 hours) the
#: exponent is 526, and a 2% gain reports a CAGR of **3,300,000%**; a 4% gain
#: raises ``OverflowError`` outright. The crash is the kinder outcome, because
#: the other one prints.
#:
#: A month is where "compound *annual* growth" stops being a category error.
#: Below it the honest answer is that the question does not have one, so `cagr`
#: refuses, and `total_return` — which needs no time base — is what a reader
#: should look at instead.
MIN_ANNUALISABLE_YEARS = 1 / 12


@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    total_return: float
    #: None when the window is too short to annualise; see MIN_ANNUALISABLE_YEARS.
    cagr: float | None
    annualised_volatility: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    max_drawdown_duration: int
    #: None whenever `cagr` is, since it is CAGR over max drawdown.
    calmar_ratio: float | None
    win_rate: float
    num_periods: int
    #: Share of periods whose return was exactly zero — usually because the
    #: strategy held no position. Above ~0.8 the annualised ratios below stop
    #: meaning anything; see `annualisation_is_meaningful`.
    flat_period_fraction: float = 0.0
    #: Denomination of the equity curve these figures came from. Empty means
    #: unstated, which is not the same as USD — see `trading_platform.core.fx`.
    currency: str = ""
    #: Third and fourth moments of the period returns. Carried because the
    #: Deflated Sharpe Ratio needs them: a Sharpe estimator's own standard
    #: error widens with negative skew and fat tails, so two strategies with
    #: identical Sharpes are not equally believable if one of them makes its
    #: money in a way that occasionally explodes.
    skew: float = 0.0
    #: Excess kurtosis — 0.0 for a Gaussian, not 3.0.
    excess_kurtosis: float = 0.0
    #: The scaling factor these figures were annualised by. Carried so the
    #: window can be judged against a year at all; 0 means unstated.
    periods_per_year: int = 0

    @property
    def window_years(self) -> float | None:
        """Length of the measured window in years, or None if unstated."""
        if self.periods_per_year <= 0 or self.num_periods < 2:
            return None
        return (self.num_periods - 1) / self.periods_per_year

    @property
    def window_is_annualisable(self) -> bool:
        """False when the window is too short to carry an annual figure.

        Separate from `cagr`'s refusal, because the two failure modes differ.
        CAGR *compounds* — the exponent is 1/years, so a short window does not
        merely widen the estimate, it explodes it, and refusing is the only
        honest answer. Sharpe and volatility *scale*, by sqrt(periods_per_year):
        they stay finite and merely acquire an error bar far wider than the
        figure. So these are reported and flagged rather than withheld.

        The distinction matters because the sweep optimises on Sharpe. A grid
        searched over windows too short to annualise is ranking noise, and
        nothing in the report said so.
        """
        years = self.window_years
        return years is not None and years >= MIN_ANNUALISABLE_YEARS

    @property
    def annualisation_is_meaningful(self) -> bool:
        """False when the Sharpe and volatility figures should not be believed.

        Two independent ways to fail, and either is disqualifying.

        **Flatness.** Annualising by sqrt(periods_per_year) assumes returns are
        i.i.d. A strategy that is flat most of the time violates that badly:
        the standard deviation collapses toward zero while the scaling factor
        stays enormous, and the ratio explodes. Measured on real 1-minute BTC
        data, a strategy holding a position for 6% of bars produced a per-bar
        mean/sd of 0.0177 — 0.34 annualised daily and **12.81** annualised at
        sqrt(365 x 1440). The second number is an artefact of the scaling.

        **Window length.** A figure annualised from less than a month is an
        extrapolation whose error bar dwarfs it, however well-behaved the
        returns. Unstated `periods_per_year` does not fail this check: a caller
        that never said what it scaled by is not thereby making a short-window
        claim, and failing there would fire on every legitimate summary built
        by hand.
        """
        if self.periods_per_year > 0 and not self.window_is_annualisable:
            return False
        return self.flat_period_fraction < 0.8

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "total_return": self.total_return,
            "cagr": self.cagr,
            "annualised_volatility": self.annualised_volatility,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_duration": self.max_drawdown_duration,
            "calmar_ratio": self.calmar_ratio,
            "win_rate": self.win_rate,
            "num_periods": self.num_periods,
            "flat_period_fraction": self.flat_period_fraction,
            "skew": self.skew,
            "excess_kurtosis": self.excess_kurtosis,
        }

    def report(self) -> str:
        lines = [
            f"Total return        : {self.total_return:>10.2%}",
            f"CAGR                : {_pct_or_na(self.cagr):>10}",
            f"Volatility (ann.)   : {self.annualised_volatility:>10.2%}",
            f"Sharpe              : {self.sharpe_ratio:>10.2f}",
            f"Sortino             : {self.sortino_ratio:>10.2f}",
            f"Max drawdown        : {self.max_drawdown:>10.2%}",
            f"Max DD duration     : {self.max_drawdown_duration:>10d} periods",
            f"Calmar              : {_num_or_na(self.calmar_ratio):>10}",
            f"Win rate (periods)  : {self.win_rate:>10.2%}",
            f"Periods             : {self.num_periods:>10d}",
            f"Flat periods        : {self.flat_period_fraction:>10.2%}",
        ]
        if self.currency:
            lines.append(f"Denominated in      : {self.currency:>10}")
        if not self.annualisation_is_meaningful:
            lines += [
                "",
                f"WARNING: the equity curve is flat for {self.flat_period_fraction:.0%} of "
                "periods, so the",
                "annualised Sharpe, Sortino and volatility above are artefacts of the",
                "sqrt(periods_per_year) scaling rather than properties of the strategy.",
                "Compare returns and trade counts instead, or resample to a lower",
                "frequency where the position is actually held.",
            ]
        return "\n".join(lines)


def _pct_or_na(value: float | None) -> str:
    """Render a percentage, or `n/a` where the statistic has no honest value.

    `n/a` rather than a dash or a blank: a reader scanning a column of numbers
    should see a word, not something that could be mistaken for zero.
    """
    return "n/a"


def _num_or_na(value: float | None) -> str:
    return "n/a"


def to_returns(equity_curve: Sequence[EquityPoint]) -> list[float]:
    """Period-over-period simple returns."""
    values = [float(p.equity) for p in equity_curve]
    returns: list[float] = []
    for prev, curr in itertools.pairwise(values):
        if prev == 0:
            returns.append(0.0)
        else:
            returns.append((curr - prev) / prev)
    return returns


def total_return(equity_curve: Sequence[EquityPoint]) -> float:
    if len(equity_curve) < 2:
        return 0.0
    start = float(equity_curve[0].equity)
    if start == 0:
        return 0.0
    return (float(equity_curve[-1].equity) - start) / start


def cagr(equity_curve: Sequence[EquityPoint], periods_per_year: int) -> float | None:
    """Compound annual growth rate, or None when the window is too short.

    Refuses rather than extrapolating. See `MIN_ANNUALISABLE_YEARS` for why:
    below about a month the exponent `1 / years` is large enough that the
    result describes the compounding, not the strategy — and on 1-minute bars
    it either overflows or prints a number in the millions of percent.

    Returning None rather than 0.0 is deliberate. A zero growth rate is a
    claim; the absence of one is not, and this repository's habit is to make a
    missing answer visible instead of substituting a plausible default.
    """
    if len(equity_curve) < 2:
        return None
    start = float(equity_curve[0].equity)
    end = float(equity_curve[-1].equity)
    if start <= 0 or end <= 0:
        return None
    years = (len(equity_curve) - 1) / periods_per_year
    if years < MIN_ANNUALISABLE_YEARS:
        return None
    try:
        return float((end / start) ** (1 / years) - 1)
    except OverflowError:  # pragma: no cover - guarded by the bound above
        return None


def annualised_volatility(returns: Sequence[float], periods_per_year: int) -> float:
    if len(returns) < 2:
        return 0.0
    return _stdev(returns) * math.sqrt(periods_per_year)


def sharpe_ratio(
    returns: Sequence[float], periods_per_year: int, risk_free_rate: float = 0.0
) -> float:
    """Annualised Sharpe. ``risk_free_rate`` is the ANNUAL rate."""
    if len(returns) < 2:
        return 0.0
    per_period_rf = risk_free_rate / periods_per_year
    excess = [r - per_period_rf for r in returns]
    sd = _stdev(excess)
    if sd == 0:
        return 0.0
    return (_mean(excess) / sd) * math.sqrt(periods_per_year)


def sortino_ratio(
    returns: Sequence[float], periods_per_year: int, risk_free_rate: float = 0.0
) -> float:
    """Like Sharpe, but penalises only downside deviation."""
    if len(returns) < 2:
        return 0.0
    per_period_rf = risk_free_rate / periods_per_year
    excess = [r - per_period_rf for r in returns]
    downside = [r for r in excess if r < 0]
    if not downside:
        return 0.0
    downside_dev = math.sqrt(sum(r * r for r in downside) / len(excess))
    if downside_dev == 0:
        return 0.0
    return (_mean(excess) / downside_dev) * math.sqrt(periods_per_year)


def max_drawdown(equity_curve: Sequence[EquityPoint]) -> tuple[float, int]:
    """Worst peak-to-trough decline, and its duration in periods."""
    if not equity_curve:
        return 0.0, 0
    peak = float(equity_curve[0].equity)
    worst = 0.0
    peak_index = 0
    worst_duration = 0
    for i, point in enumerate(equity_curve):
        value = float(point.equity)
        if value > peak:
            peak = value
            peak_index = i
        elif peak > 0:
            dd = (peak - value) / peak
            if dd > worst:
                worst = dd
            worst_duration = max(worst_duration, i - peak_index)
    return worst, worst_duration


def flat_period_fraction(returns: Sequence[float]) -> float:
    """Share of periods with exactly zero return.

    High values mean the strategy was out of the market, which makes every
    annualised ratio unreliable rather than merely small.
    """
    if not returns:
        return 0.0
    return sum(1 for r in returns if r == 0.0) / len(returns)


def skewness(returns: Sequence[float]) -> float:
    """Sample skew of period returns. Zero for a symmetric distribution."""
    if len(returns) < 3:
        return 0.0
    mu = _mean(returns)
    sd = _stdev(returns)
    if sd == 0:
        return 0.0
    n = len(returns)
    return sum(((r - mu) / sd) ** 3 for r in returns) / n


def excess_kurtosis(returns: Sequence[float]) -> float:
    """Sample excess kurtosis: 0.0 for a Gaussian, positive for fat tails."""
    if len(returns) < 4:
        return 0.0
    mu = _mean(returns)
    sd = _stdev(returns)
    if sd == 0:
        return 0.0
    n = len(returns)
    return sum(((r - mu) / sd) ** 4 for r in returns) / n - 3.0


def win_rate(returns: Sequence[float]) -> float:
    if not returns:
        return 0.0
    return sum(1 for r in returns if r > 0) / len(returns)


def summarise(
    equity_curve: Sequence[EquityPoint],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    risk_free_rate: float = 0.0,
    currency: str = "",
) -> PerformanceSummary:
    """Compute the full metric set from an equity curve.

    ``currency`` is carried through to the report unchanged. These statistics
    are ratios and so dimensionless, but *which curve* they were computed on
    is not: the same trades measured in USDT and in USD through a stablecoin
    depeg differ by 2.7x in volatility while agreeing exactly on total return.
    """
    returns = to_returns(equity_curve)
    dd, dd_duration = max_drawdown(equity_curve)
    growth = cagr(equity_curve, periods_per_year)
    return PerformanceSummary(
        total_return=total_return(equity_curve),
        cagr=growth,
        annualised_volatility=annualised_volatility(returns, periods_per_year),
        sharpe_ratio=sharpe_ratio(returns, periods_per_year, risk_free_rate),
        sortino_ratio=sortino_ratio(returns, periods_per_year, risk_free_rate),
        max_drawdown=dd,
        max_drawdown_duration=dd_duration,
        calmar_ratio=(growth / dd) if (growth is not None and dd > 0) else None,
        win_rate=win_rate(returns),
        num_periods=len(equity_curve),
        flat_period_fraction=flat_period_fraction(returns),
        periods_per_year=periods_per_year,
        currency=currency,
        skew=skewness(returns),
        excess_kurtosis=excess_kurtosis(returns),
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    """Sample standard deviation (n-1 denominator)."""
    n = len(values)
    if n < 2:
        return 0.0
    mu = _mean(values)
    variance = sum((v - mu) ** 2 for v in values) / (n - 1)
    return math.sqrt(variance)


def decimal_to_float(value: Decimal) -> float:
    """Explicit conversion at the money/statistics boundary."""
    return float(value)
