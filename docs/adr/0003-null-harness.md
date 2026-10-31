# ADR 0003 — The false-discovery rate is governed by out-of-sample evidence, not by folds

**Status:** Accepted
**Date:** 2026-08-25

## Context

This project's product is the word "no", delivered fast, with evidence. That
only has value if the machinery producing it actually distinguishes signal from
noise. Nothing had ever checked.

The check is: run the whole pipeline over data with **no predictability by
construction** and count how often it says `SURVIVES`, the strongest word it
has. Every such discovery is false.

Three things had to be built or fixed before that measurement was possible, and
each was a defect in its own right.

### The generator had no null in it

`Regime.CHOP` declares an annual Sharpe of 0.0, which reads as "no edge". It
also carries `mean_reversion=0.18`, giving measured return autocorrelation of
**−0.156** — directly exploitable. A false-discovery test run against CHOP
would have been measuring a *true* discovery and calling it false.
`Regime.FLAT` is genuinely unpredictable but runs at 5% annualised volatility,
so nothing trades.

`Regime.NULL` now exists: zero drift, zero mean reversion, 45% volatility, fat
tails and genuine volatility clustering. Measured over 16 seeds — ACF(1) of
returns **+0.008**, ACF(1) of |returns| **+0.155**, kurtosis **12.2**.
Direction unpredictable, magnitude clustered.

### The power gate was reading an inflated number

`evaluate` counted fills over the whole feed, and a `TestWindow`'s feed is
`warmup + bars`. At the shape the UI runs — 120 warm-up bars against a 100-bar
window — it reported 13 fills where 6 were in the scored region. The
`median_fills_per_fold < 10` guard, which decides whether a fold has enough
trades to mean anything, was an effective five-fill guard.

### The first measurement passed for the wrong reason

Twenty seeds returned **0% discovery and 85% UNDERPOWERED**. The strategy
barely traded, so the pipeline was never tested. A harness reporting no false
discoveries because nothing happened is a guard that cannot fail.

## Decision

`analytics/nulltest.py` measures the false-discovery rate, and:

**It is two-sided.** `null_discovery_rate` and `detection_curve` are the same
function with the same classification policy, differing only in the series
handed to them. A harness measuring only the false-discovery rate grades a
pipeline that says "no" to everything as perfect — and anything that suppresses
false discoveries under this design suppresses true ones identically, so the
power side fires.

**Every trial that returned a verdict counts.** An earlier design excluded
UNDERPOWERED and NO_EDGE from the denominator as "vacuous". That was a category
error: they are outcomes, the pipeline answering, and answering correctly.
Excluding them measures *P(discovery | the pipeline reached a verdict I liked)*.
Only a raised exception is a genuine non-answer, and those are counted
separately with their own gate.

**It reports the interval, not the point estimate.** With zero discoveries in
*n* trials the exact one-sided 95% upper bound is roughly 3/n:

| observed | bounds the true rate at |
|----------|------------------------:|
| 0 of 10 | **25.9%** |
| 0 of 60 | 4.9% |
| 0 of 100 | 3.0% |
| 1 of 100 | 4.7% |
| 2 of 100 | 6.2% |

**Ten seeds cannot demonstrate a 5% rate**, which is what this project's own
plan asked for. Sixty is the fewest that can, and the harness refuses to draw a
conclusion below that.

**The full outcome histogram is reported**, because without it a 0% rate cannot
be told apart from ninety-five trials that never traded.

## The finding

### First, a retraction

The first version of this ADR was titled *"a walk-forward needs sixteen folds
before its verdict means anything"*, on this table — 100 trials per row, white
noise, realistic costs:

| bars | folds | discoveries | rate | 95% upper |
|-----:|------:|------------:|-----:|----------:|
| 900 | 1 | 7 | 7.0% | 12.7% |
| 1,500 | 3 | 8 | 8.0% | 14.0% |
| 2,400 | 6 | 4 | 4.0% | 8.9% |
| 3,600 | 10 | 4 | 4.0% | 8.9% |
| 5,200 | 16 | 1 | 1.0% | 4.7% |

**That claim was confounded and is withdrawn.** Every row held
`test_size=300` and grew the series, so fold count and *total out-of-sample
bars* rose together. Holding the series at 5,200 bars and varying `test_size`
instead — same data, same training size, same 4,800 out-of-sample bars in every
row, only the number of folds changing:

| test_size | folds | discoveries | rate | 95% upper |
|----------:|------:|------------:|-----:|----------:|
| 300 | 16 | 1 | 1.0% | 4.7% |
| 600 | 8 | 0 | 0.0% | 3.0% |
| 1,200 | 4 | 1 | 1.0% | 4.7% |
| 2,400 | **2** | 1 | 1.0% | 4.7% |

**Fold count makes no difference at all.** Two folds is as good as sixteen.

### What actually drives it

Re-reading the first table by out-of-sample bars rather than folds gives a
monotone relationship, and it is the same variable the second table holds
fixed:

| out-of-sample bars | rate | 95% upper |
|-------------------:|-----:|----------:|
| 300 | 7.0% | 12.7% |
| 900 | 8.0% | 14.0% |
| 1,800 | 4.0% | 8.9% |
| 3,000 | 4.0% | 8.9% |
| 4,800 | 1.0% | 4.7% |

**The false-discovery rate is governed by how much out-of-sample evidence the
verdict rests on, and not by how that evidence is divided into folds.** Below
roughly a thousand out-of-sample bars the rate is 7-8% — worse than target by a
factor of two while looking exactly like a result. At around 4,800 it is 1%,
and the 95% upper bound finally falls under 5%.

That is a less satisfying headline than the one it replaces and a more useful
one, because it comes with a negative result: **adding folds does not buy
reliability.** Splitting the same data more finely is not a way to earn a more
trustworthy verdict, which is exactly the sort of thing a practitioner would
assume without checking.

The confound is worth recording as a method note. Growing a series with the
test window fixed changes two things at once, and the first table read
beautifully as a fold-count story. It took holding one of them still to find
out which mattered — and the answer was the one I had not been looking at.

### And SMACrossover through it is blind below Sharpe 5

The other half. Effect sizes are reported as the maximum annualised Sharpe an
optimal linear forecast could extract, `m/√(1−m²)·√365`, because an AR(1)
coefficient transfers to nothing:

| implied Sharpe | discovered |
|---------------:|-----------:|
| 0.00 | 0% |
| 2.90 | 5% |
| 4.93 | 5% |
| 7.14 | 20% |
| 9.63 | 45% |
| 18.73 | 100% |

An edge worth an annualised Sharpe of 5 is invisible **to `SMACrossover`
through this pipeline**. The strategy is named deliberately: it is a lagging
two-moving-average crossover, its own docstring calls it "the hello world" of
the field, and it is a weak detector for a first-order AR(1) signal by
construction. Without an oracle arm — a strategy that trades the sign of
`m·r_(t-1)` directly — there is no way to separate "the verdict thresholds are
conservative" from "this detector is weak" from "the 3x3 grid is too coarse".
An earlier version of this ADR stated the floor as a property of the pipeline;
that is more than the evidence supports, and the oracle arm is the next thing
to build. For scale,
`data/calibration.py` caps its own generator at `MAX_REGIME_SHARPE = 3.5` on
the grounds that anything larger "hands every strategy an edge that does not
exist" — so **the detection floor sits above the ceiling the generator will
emit.** Those are different quantities, a drift Sharpe against a timing Sharpe,
so this is a sanity anchor rather than a proof. It is still the right order of
magnitude to worry about.

### Per idea is not per project

At the measured upper bound, twenty independent ideas produce at least one
false pass with probability **62%**. That is the number that matters
practically, and it is why a trial registry is the next thing to build rather
than an optional extra.

## Consequences

### Two nulls, and they agree

`Regime.NULL` — fat tails, clustered volatility — gives **1 discovery in 100,
the same as the Gaussian series**. The theory behind building it was that
clustering widens the sampling distribution of every Sharpe-derived threshold
in the verdict cascade, so a Gaussian null would understate the rate. The
theory is sound; the effect is not detectable at this configuration.

Worth recording as a negative result, and worth keeping as a gate: if a future
change makes the thresholds sensitive to heteroskedasticity, that is where it
surfaces.

An audit found that the first version of this harness measured only the
Gaussian series, while this document's opening section described building
`Regime.NULL` and quoted its ACF and kurtosis figures. The regime was built,
validated, documented — and then not used by the measurement it exists for.
Both arms run now.

### The margin is one seed

The passing result is 1 discovery in 100: upper bound 4.7%, under the 5%
target. **2 in 100 bounds at 6.2% and fails.** The seeds are fixed and
everything downstream is deterministic, so this cannot flake — but it can go
red from a change to the grid, the cost model or the risk limits, none of which
need be wrong. Treat "this configuration passes" as provisional until
re-measured at larger n.

Also worth stating about the tables above: the rows are **not independent
replications.** A 900-bar series at seed 0 is the first 900 draws of the
5,200-bar series at seed 0, so each longer row extends the shorter one rather
than sampling afresh.

**Accepted costs**
- The gate runs at n=100 on 5,200-bar series, about three minutes, behind
  `@pytest.mark.slow`. That length is chosen for its out-of-sample volume, not
  its fold count.
- A verdict resting on fewer than roughly a thousand out-of-sample bars should
  be treated as untrustworthy whatever it says, and the number of folds is not
  a mitigation.

**Guarded against itself.** Three mutant pipelines must each drive the harness
to fail, and they are tests rather than claims:
- **Lookahead** — a real pipeline that cuts folds, sweeps and classifies
  through the genuine machinery, with one line changed: `evaluate` receives a
  `TestWindow` built from the fold's *training* bars. It drives the
  false-discovery rate from **5% to 80%**.

  This one is worth more than its assertion. `analytics/windows.py` makes
  `TrainWindow` and `TestWindow` distinct types with no conversion so that
  fitting on test data is a `mypy --strict` error rather than a convention. It
  is still only a **speed bump**: `TestWindow(bars=fold.train.bars)`
  type-checks perfectly, because nothing in the type records where its bars
  came from.

  The first version of this mutant was a stub returning a constant `SURVIVES`
  — which is precisely the "always says SURVIVES" mutant rejected below as too
  easy, wearing a docstring about the type split it never exercised. An audit
  caught it. That is the fourth guard-that-cannot-fail in a week, and it was
  inside the module built to catch them.
- **Relaxed threshold** — models the social failure, somebody tuning a constant
  until their strategy passes.
- **Never discovers** — must fail the *power* side, which is what makes gaming
  the denominator non-viable.

"A pipeline that always says SURVIVES" was the obvious mutant and was rejected
as too easy: no realistic change produces it, and passing it would prove only
that the assertion is wired to something.

**Rejected alternative:** asserting the detection floor's location. It is a
finding, not an invariant, and pinning it in a test would make every legitimate
improvement a failure. It lives here and in `scripts/measure_null.py`, which
regenerates every number above.
