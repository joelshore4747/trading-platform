"""Synthetic market data, calibrated to documented BTC statistics.

**This is not real market data.** It is generated data whose *distributional
properties* match published measurements of Bitcoin - volatility, tail
thickness, volatility clustering, and drift. That distinction is load-bearing
and is repeated wherever these series surface. Calibrated synthetic is honest;
synthetic dressed as real is the dishonesty this repository exists to prevent.

Real data is not on the critical path yet, so these series carry the testing
load. That raises the bar twice over.

First, against ``feed.synthetic_walk``, a sinusoid where ``open == close`` on
every bar, which cannot exercise limit fills, drawdown halts, or gaps.

Second - and this was the expensive one - against the previous version of this
module. Measured against the figures in ``data/calibration.py`` it produced an
annualised Sharpe of **6.36** in its trending regime and **12.75** in recovery,
where real BTC buy-and-hold pays roughly 0.5-1.0. Anything holding long through
those regimes looked brilliant on a six- to thirteen-fold free lunch. Its
returns had a kurtosis of 2.82 - *thinner*-tailed than a Gaussian, in an asset
famous for fat tails - and, being i.i.d., no volatility clustering at all.

So the engine is now:

- ``r_t = mu + sigma_t * eps_t`` with standardised Student-t innovations, which
  produce the fat tails a Gaussian cannot.
- ``sigma^2_t = omega + alpha*r^2_{t-1} + beta*sigma^2_{t-1}`` - GARCH(1,1),
  giving the volatility clustering that is the most robust stylised fact in
  finance.
- A Poisson jump component, so a crash is a fat tail rather than the edge of a
  regime.
- **Drift derived from a target Sharpe**, never chosen by hand. That is the
  structural fix: the parameter that was silently wrong is now the parameter
  you have to state.

Everything is **deterministic from an explicit seed** and never reads the clock.
A test asserting on backtest output must give the same numbers on every machine;
a generator seeded from the current time makes failures unreproducible, which is
worse than no test.

All output goes through the real ``Bar`` constructor, so ``__post_init__``
validation applies and this module cannot emit an impossible bar.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from trading_platform.core.events import Bar
from trading_platform.data import calibration as cal
from trading_platform.data.binance import FundingRate
from trading_platform.data.feed import InMemoryBarFeed

_CENT = Decimal("0.01")


class Regime(StrEnum):
    """A market condition worth testing against.

    Named for what they break rather than what they look like — a generator
    whose only mode is "up and to the right" flatters every strategy.
    """

    TREND = "trend"
    """Directional drift. The easy case; most strategies look good here."""

    CHOP = "chop"
    """Mean-reverting noise. Where crossover strategies bleed to death on costs."""

    CRASH = "crash"
    """Fast sustained decline. Fires the drawdown halt and tests the exit path."""

    RECOVERY = "recovery"
    """Sharp rally. Catches strategies that exit on a crash and never re-enter."""

    FLAT = "flat"
    """Near-zero movement. Division-by-zero, zero-volatility, no-signal edges."""

    NULL = "null"
    """**No predictability, by construction.** The regime a strategy must fail
    on, and the one the false-discovery harness is measured against.

    Distinct from CHOP, which also declares zero Sharpe but carries mean
    reversion — real, exploitable structure that makes any "discovery" there a
    true one. Distinct from FLAT, which is unpredictable but barely moves, so
    nothing trades and the measurement is vacuous rather than clean.

    Realistic in every respect that does not confer directional edge: 45%
    annualised volatility, fat tails, and genuine volatility clustering."""


@dataclass(frozen=True, slots=True)
class RegimeSpec:
    """One stretch of a series."""

    regime: Regime
    bars: int

    def __post_init__(self) -> None:
        if self.bars < 1:
            raise ValueError(f"A regime needs at least 1 bar, got {self.bars}")


BARS_PER_YEAR = 365.0
"""Bars per year at the daily default. Crypto trades continuously, so 365.

Kept as the name callers know, but it is no longer what the generator uses:
`bars_per_year(step)` derives the figure from the bar size. It used to be a
module constant applied at every resolution, so a series generated at
`step=timedelta(minutes=1)` was scaled as though each minute were a day.
`Regime.TREND` declares an annual Sharpe of 0.9 and delivered:

    1 day     Sharpe  0.80   vol    43.4%
    1 hour    Sharpe  3.91   vol   212.8%
    1 minute  Sharpe 30.31   vol 1,648.1%

`MAX_REGIME_SHARPE` checks the *declared* figure, so the ceiling built to stop
exactly this let a 34x breach through untouched."""

SECONDS_PER_YEAR = 365.0 * 24 * 60 * 60
"""Continuous-market year, matching `BARS_PER_YEAR` at a daily step."""


def bars_per_year(step: timedelta) -> float:
    """How many bars of size `step` fall in a year."""
    seconds = step.total_seconds()
    if seconds <= 0:
        raise ValueError(f"step must be positive, got {step!r}")
    return SECONDS_PER_YEAR / seconds


COLLAPSE_FRACTION = 1e-6
"""Fraction of the starting price below which the series is refused.

A 99.9999% decline is not a market, it is a regime specified for longer than
any market has ever fallen. Continuing past it quantises the price to the
smallest tick and then manufactures fictional bounces off that floor, so the
generator raises instead - loudly, at the boundary, rather than producing a
plausible-looking result two hundred bars later."""

MAX_BAR_RETURN = 0.5
"""Hard bound on a single bar's return, in either direction.

Bitcoin's worst day is around -50%. Bounding the return rather than flooring
the price is what stops a long decline pinning price at a constant and then
generating fictional bounces off it."""


@dataclass(frozen=True, slots=True)
class RegimeParams:
    """What a regime is, stated in units anyone can check against reality.

    Volatility and Sharpe are **annualised**, because that is how the published
    figures in ``data/calibration.py`` are quoted. Expressing them per-bar is
    what let a drift of 0.004 and a vol of 0.012 sit in the old table looking
    entirely reasonable while paying an annualised Sharpe of 6.36.
    """

    #: Conditional on being in this regime. A crash really does run at an
    #: extreme rate while it lasts; an ordinary uptrend does not.
    annual_sharpe: float
    annual_vol: float
    #: Probability of a jump **per day**, not per bar. A jump is an event in
    #: the world: the rate per year is the invariant, and the per-bar chance
    #: has to shrink as bars get smaller. Left per-bar, a 5% jump drawn at a
    #: daily rate on minute bars swamped the diffusion entirely — the jump
    #: variance alone exceeded the regime's whole volatility target, pinning
    #: `target_var` at its floor and delivering 388% annualised vol where 40%
    #: was declared.
    jump_probability: float
    #: Mean and spread of a jump's return, when one occurs.
    jump_mean: float = 0.0
    jump_sigma: float = 0.05
    #: Pull back toward zero after a move. Chop is where crossover strategies
    #: bleed to death on costs, and that needs mean reversion, not just noise.
    mean_reversion: float = 0.0

    def __post_init__(self) -> None:
        if abs(self.annual_sharpe) > cal.MAX_REGIME_SHARPE:
            raise ValueError(
                f"annual_sharpe {self.annual_sharpe} exceeds the calibration ceiling "
                f"of {cal.MAX_REGIME_SHARPE}. A generator paying more than a market "
                "ever has hands every strategy an edge that does not exist - which is "
                "the bug this ceiling exists to prevent recurring."
            )
        if self.annual_vol <= 0:
            raise ValueError(f"annual_vol must be positive, got {self.annual_vol}")

    def bar_jump_probability(self, periods: float = BARS_PER_YEAR) -> float:
        """Per-bar jump chance, scaled from the declared per-day rate."""
        return self.jump_probability * (BARS_PER_YEAR / periods)

    def bar_sigma(self, periods: float = BARS_PER_YEAR) -> float:
        """Per-bar volatility for a series with `periods` bars per year.

        A method rather than a property because the answer depends on the bar
        size, and the regime table is shared across resolutions: a regime
        declares annual figures, and the generator — which is the thing that
        knows the step — converts.
        """
        return self.annual_vol / math.sqrt(periods)

    def jump_variance(self, periods: float = BARS_PER_YEAR) -> float:
        """Per-bar variance contributed by the jump component alone."""
        probability = self.bar_jump_probability(periods)
        if not probability:
            return 0.0
        second_moment = self.jump_mean**2 + self.jump_sigma**2
        return probability * second_moment - (probability * self.jump_mean) ** 2

    def jump_drift(self, periods: float = BARS_PER_YEAR) -> float:
        """Drift the jump component contributes on its own, per bar."""
        return self.bar_jump_probability(periods) * self.jump_mean

    def bar_drift(self, periods: float = BARS_PER_YEAR) -> float:
        """Derived, never chosen.

        `annual_sharpe = mu_bar * sqrt(periods) / sigma_bar`, so the drift that
        produces a stated Sharpe is fixed once volatility is.

        The jump component's own drift is subtracted, because a jump with a
        non-zero mean contributes `probability * mean` per bar whether anyone
        accounted for it or not. It was not accounted for, so `Regime.TREND`'s
        `jump_mean=-0.01` at probability 0.003 was quietly shaving the drift
        that its declared Sharpe of 0.9 was supposed to produce.
        """
        target = self.annual_sharpe * self.bar_sigma(periods) / math.sqrt(periods)
        return target - self.jump_drift(periods)


#: Calibrated against `data/calibration.py`. Central BTC volatility is ~40%
#: annualised; regimes sit around it. Sharpes are conditional on the regime and
#: are capped so no regime can hand out a free lunch again.
_PARAMS: dict[Regime, RegimeParams] = {
    Regime.TREND: RegimeParams(
        annual_sharpe=0.9,  # near the top of BTC's real buy-and-hold band
        annual_vol=0.40,
        jump_probability=cal.JUMP_PROBABILITY_CALM,
        jump_mean=-0.01,
        jump_sigma=0.05,
    ),
    Regime.CHOP: RegimeParams(
        annual_sharpe=0.0,
        annual_vol=0.45,
        jump_probability=cal.JUMP_PROBABILITY_CALM,
        jump_sigma=0.04,
        mean_reversion=0.18,
    ),
    Regime.CRASH: RegimeParams(
        annual_sharpe=-3.0,
        annual_vol=0.95,
        jump_probability=cal.JUMP_PROBABILITY_STRESSED,
        jump_mean=-0.05,
        jump_sigma=0.07,
    ),
    Regime.RECOVERY: RegimeParams(
        annual_sharpe=2.4,
        annual_vol=0.70,
        jump_probability=cal.JUMP_PROBABILITY_STRESSED,
        jump_mean=0.03,
        jump_sigma=0.06,
    ),
    Regime.NULL: RegimeParams(
        # The regime that exists to be unprofitable. Zero drift, and — the
        # part that matters — zero mean reversion, so returns carry no
        # directional structure at all.
        #
        # `Regime.CHOP` is the trap this avoids. It also declares an annual
        # Sharpe of 0.0, which reads as "no edge", but its `mean_reversion` of
        # 0.18 gives measured return autocorrelation of **-0.156**: buy after
        # a down bar, sell after an up bar, and the edge is real because it
        # was put there. A false-discovery test run against CHOP would be
        # measuring a true discovery.
        #
        # Volatility clustering and fat tails stay in DELIBERATELY. They add
        # no directional structure — the sign path is still a martingale — but
        # they widen the sampling distribution of every Sharpe-derived
        # threshold in the verdict cascade. A null built on Gaussian returns
        # would understate the false-discovery rate of a pipeline that will be
        # run on real data, which is the wrong direction to be wrong in.
        annual_sharpe=0.0,
        annual_vol=0.45,
        jump_probability=cal.JUMP_PROBABILITY_CALM,
        jump_mean=0.0,
        jump_sigma=0.04,
        mean_reversion=0.0,
    ),
    Regime.FLAT: RegimeParams(
        annual_sharpe=0.0,
        annual_vol=0.05,
        jump_probability=0.0,
        jump_sigma=0.01,
    ),
}


def _student_t(rng: random.Random, df: float) -> float:
    """A standardised Student-t draw: mean 0, variance 1, kurtosis 3 + 6/(df-4).

    Built from a normal over the root of a scaled chi-square, because
    ``random`` has no t. The final division rescales to unit variance - a raw t
    has variance df/(df-2), so skipping it would inflate volatility by ~29% at
    df=5 and quietly break the calibration this module exists to hold.
    """
    z = rng.gauss(0.0, 1.0)
    chi2 = rng.gammavariate(df / 2.0, 2.0)
    return (z / math.sqrt(chi2 / df)) / math.sqrt(df / (df - 2.0))


def generate_bars(
    symbol: str,
    start: datetime,
    regimes: Sequence[RegimeSpec] | Regime = Regime.TREND,
    *,
    bars: int | None = None,
    seed: int = 0,
    start_price: Decimal = Decimal("100"),
    step: timedelta = timedelta(days=1),
) -> list[Bar]:
    """Generate a bar series across one or more regimes.

    ``regimes`` accepts a bare ``Regime`` (with ``bars`` giving the length) or a
    sequence of ``RegimeSpec`` to compose several stretches into one series.

    Unlike ``synthetic_walk``, open and close genuinely differ and the high/low
    extend beyond both, so intrabar logic — limit fills especially — is actually
    exercised rather than trivially satisfied.
    """
    if isinstance(regimes, Regime):
        if bars is None:
            raise ValueError("Pass `bars` when giving a single regime")
        specs: Sequence[RegimeSpec] = [RegimeSpec(regimes, bars)]
    else:
        if not regimes:
            raise ValueError("Need at least one regime")
        specs = regimes

    if start_price <= 0:
        raise ValueError(f"start_price must be positive, got {start_price}")

    rng = random.Random(seed)
    out: list[Bar] = []
    price = float(start_price)
    index = 0

    # GARCH state carries ACROSS regimes on purpose. Volatility does not reset
    # at a regime boundary in a real market, and letting it persist is what
    # makes a crash bleed into the recovery that follows it.
    periods = bars_per_year(step)
    variance = _PARAMS[specs[0].regime].bar_sigma(periods) ** 2
    last_return = 0.0

    for spec in specs:
        params = _PARAMS[spec.regime]
        # Jumps carry their own variance, so the GARCH process must target
        # LESS than the regime's stated volatility for the total to land on it.
        # Without this the stated `annual_vol` is a floor rather than a target,
        # and a stressed regime silently runs ~20% hotter than it claims.
        target_var = max(params.bar_sigma(periods) ** 2 - params.jump_variance(periods), 1e-12)
        # Long-run variance of a GARCH(1,1) is omega / (1 - alpha - beta), so
        # omega is derived from the target rather than tuned by hand.
        omega = target_var * (1.0 - cal.GARCH_ALPHA - cal.GARCH_BETA)

        for _ in range(spec.bars):
            variance = omega + cal.GARCH_ALPHA * last_return**2 + cal.GARCH_BETA * variance
            sigma = math.sqrt(variance)

            shock = sigma * _student_t(rng, cal.STUDENT_T_DF)
            if params.mean_reversion:
                shock -= params.mean_reversion * last_return
            jump_chance = params.bar_jump_probability(periods)
            if jump_chance and rng.random() < jump_chance:
                shock += rng.gauss(params.jump_mean, params.jump_sigma)

            # Clamp the RETURN, not the price. Flooring price at a constant
            # lets a sustained decline pin it there and then manufacture
            # enormous fictional bounces off the floor - a 3000-bar crash
            # spent 2194 bars at 0.01, reporting 219% annualised volatility
            # that was pure clamping artefact. A daily move beyond +/-50% is
            # not something a liquid market does, so bounding the return keeps
            # the process well defined instead.
            bar_return = max(
                -MAX_BAR_RETURN, min(MAX_BAR_RETURN, params.bar_drift(periods) + shock)
            )
            last_return = bar_return

            open_px = price
            close_px = open_px * (1.0 + bar_return)

            if close_px < float(start_price) * COLLAPSE_FRACTION:
                raise ValueError(
                    f"{symbol}: price collapsed to {close_px:.3g} from "
                    f"{start_price} after {index} bars, {spec.bars} of them "
                    f"{spec.regime.value}. No market falls that far - shorten the "
                    "regime. Continuing would pin the price at the smallest tick "
                    "and then generate fictional bounces off it."
                )

            # High and low must contain both open and close, and extend a
            # little past them — otherwise a limit order at the touch never
            # fills and half the execution model goes untested. The wick scales
            # with the *current* conditional volatility, so intrabar range
            # widens in stress the way a real one does.
            body_hi, body_lo = max(open_px, close_px), min(open_px, close_px)
            wick = max(abs(close_px - open_px), open_px * sigma) * rng.uniform(0.2, 1.0)
            high_px = body_hi + wick * rng.uniform(0.0, 1.0)
            low_px = max(0.01, body_lo - wick * rng.uniform(0.0, 1.0))

            # Volume rises with volatility, because it does.
            volume = rng.uniform(500, 5000) * (1.0 + 8.0 * sigma)

            out.append(
                Bar(
                    symbol=symbol,
                    timestamp=start + step * index,
                    open=_q(open_px),
                    high=_q(max(high_px, body_hi)),
                    low=_q(min(low_px, body_lo)),
                    close=_q(close_px),
                    volume=Decimal(str(round(volume, 2))),
                )
            )
            price = close_px
            index += 1

    return out


def generate_feed(*args: object, **kwargs: object) -> InMemoryBarFeed:
    """``generate_bars`` wrapped as a feed, for handing straight to a Backtester."""
    return InMemoryBarFeed(generate_bars(*args, **kwargs))  # type: ignore[arg-type]


def inject_gaps(bars: Sequence[Bar], *, seed: int = 0, fraction: float = 0.05) -> list[Bar]:
    """Drop bars at random to simulate exchange downtime.

    Gaps are permitted by the feed by design — silently interpolating over a
    hole hides the problem it creates — so the engine's behaviour across one
    needs a test, particularly for funding settled inside the gap.

    The first and last bars are always kept so the series still spans the
    intended window.
    """
    if not 0.0 <= fraction < 1.0:
        raise ValueError(f"fraction must be in [0, 1), got {fraction}")
    if len(bars) < 3 or fraction == 0.0:
        return list(bars)

    rng = random.Random(seed)
    kept = [bars[0]]
    kept.extend(b for b in bars[1:-1] if rng.random() >= fraction)
    kept.append(bars[-1])
    return kept


def generate_funding(
    symbol: str,
    start: datetime,
    *,
    settlements: int,
    seed: int = 0,
    mean_rate: Decimal = cal.FUNDING_RATE_TYPICAL,
    interval_hours: int = cal.FUNDING_INTERVAL_HOURS,
    switch_to_hours: int | None = None,
    switch_after: int | None = None,
) -> list[FundingRate]:
    """Generate a funding settlement series.

    ``switch_to_hours`` / ``switch_after`` change the cadence partway through,
    mirroring Binance moving selected perpetuals from 8-hourly to 4-hourly in
    October 2023. A multi-year series really does contain both regimes, so any
    code assuming a fixed interval must be tested against one that is not.

    Rates vary in sign: a generator that only ever emits positive rates would
    never exercise the short-receives path.
    """
    if settlements < 1:
        raise ValueError(f"Need at least 1 settlement, got {settlements}")

    rng = random.Random(seed)
    out: list[FundingRate] = []
    when = start
    mean = float(mean_rate)

    for i in range(settlements):
        hours = interval_hours
        if switch_to_hours is not None and switch_after is not None and i >= switch_after:
            hours = switch_to_hours

        # Centred on `mean` but wide enough to go negative regularly, then
        # clamped to the venue cap — Binance caps funding at +/-0.05% per 8h
        # window, so an uncapped generator would test a market that cannot
        # exist and understate how long a carry trade must run to matter.
        value = rng.gauss(mean, abs(mean) * 2.5 if mean else 0.0002)
        cap = float(cal.FUNDING_RATE_CAP)
        value = max(-cap, min(cap, value))
        out.append(
            FundingRate(
                symbol=symbol,
                timestamp=when,
                rate=Decimal(str(round(value, 8))),
            )
        )
        when = when + timedelta(hours=hours)

    return out


def correlated_series(
    symbols: Sequence[str],
    start: datetime,
    *,
    bars: int,
    seed: int = 0,
    correlation: float = 0.8,
    step: timedelta = timedelta(days=1),
) -> list[Bar]:
    """Two or more symbols driven by a shared factor plus idiosyncratic noise.

    For multi-symbol portfolio marking and gross-exposure limits. Independent
    random series would understate correlated drawdown, which is the case a
    risk limit most needs to survive.

    Returns one merged list sorted by ``(timestamp, symbol)`` — the order a
    feed must emit.
    """
    if not symbols:
        raise ValueError("Need at least one symbol")
    if not 0.0 <= correlation <= 1.0:
        raise ValueError(f"correlation must be in [0, 1], got {correlation}")

    rng = random.Random(seed)
    shared = [rng.gauss(0.0015, 0.015) for _ in range(bars)]

    merged: list[Bar] = []
    for offset, symbol in enumerate(symbols):
        local = random.Random(seed + 1 + offset)
        price = 100.0 * (1.0 + offset * 0.5)
        for i in range(bars):
            move = correlation * shared[i] + (1 - correlation) * local.gauss(0.0, 0.02)
            open_px = price
            close_px = max(0.01, open_px * (1.0 + move))
            hi, lo = max(open_px, close_px), min(open_px, close_px)
            pad = abs(close_px - open_px) * local.uniform(0.1, 0.6) + 0.01
            merged.append(
                Bar(
                    symbol=symbol,
                    timestamp=start + step * i,
                    open=_q(open_px),
                    high=_q(hi + pad),
                    low=_q(max(0.01, lo - pad)),
                    close=_q(close_px),
                    volume=Decimal(str(round(local.uniform(500, 5000), 2))),
                )
            )
            price = close_px

    merged.sort(key=lambda b: (b.timestamp, b.symbol))
    return merged


#: Tick size by price magnitude. Real venues do exactly this — BTC quotes to
#: the cent, SHIB to eight decimals — and without it a long decline rounds a
#: price to 0.00, which `Bar` now rejects outright.
_TICKS: tuple[tuple[float, Decimal], ...] = (
    (1.0, Decimal("0.01")),
    (0.01, Decimal("0.0001")),
    (0.0001, Decimal("0.000001")),
)
_SMALLEST_TICK = Decimal("0.00000001")


def _q(value: float) -> Decimal:
    """Quantise to a tick appropriate to the price's magnitude.

    Never returns zero: a non-positive price is not a price, and the smallest
    representable tick is returned instead so the series stays valid rather
    than producing a bar the engine must reject.
    """
    for threshold, tick in _TICKS:
        if value >= threshold:
            return Decimal(str(value)).quantize(tick)
    quantised = Decimal(str(value)).quantize(_SMALLEST_TICK)
    return max(quantised, _SMALLEST_TICK)
