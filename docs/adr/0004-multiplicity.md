# ADR 0004 — Counting trials is worth it; the statistics that count them are not the problem

**Status:** Accepted
**Date:** 2026-08-25

## Context

ADR 0003 measured this pipeline's per-run false-discovery rate at roughly 1 in
100, with a 4.7% upper bound. That is the honest number for **one** idea, and
it is not the number that governs a project. At the upper bound, twenty
independent ideas produce at least one false `SURVIVES` with probability 62%;
fifty produce one with probability 91%. Nothing in a per-run verdict can fix
that, because the run does not know how many siblings it has.

Step 6 was to build multiplicity control. Three things came out of it, and the
third is the one that matters.

## Decision 1 — the trial registry, in `analytics/trials.py`

An append-only JSONL log: hypothesis, stated mechanism, parameter points,
config digest, outcome. It enforces three things and no more.

1. **Recorded before it runs.** Recording afterwards silently drops the trials
   that disappointed you, and the count that drives every correction is of
   attempts.
2. **A grid counts once per cell.** Ten sweeps of sixty-four cells is 640
   shots, not ten. `SweepSurface.attempted` already knows the difference
   between the grid as written and the cells that ran.
3. **Append-only.** Completion appends a row rather than rewriting one, and a
   finished trial refuses to be finished again — re-running until it passes is
   the behaviour the file exists to make visible.

It **cannot** enforce that a mechanism is any good. That is a human judgement,
and a length check dressed up as a quality bar would have been the fifth
guard-that-cannot-fail in a fortnight. The test suite says so out loud: a
fluent, grammatical, entirely empty mechanism is accepted, and the test that
demonstrates it is named for that fact. The value is the count and the paper
trail, not policing prose.

Writing the tests found a real defect in the reader. Replaying the log counted
a completed trial twice — once as recorded, once as finished — so a reopened
registry overstated both the trial count and the parameter-set total. Those two
numbers are the entire point of the file, and it read high, which is the
flattering direction for anyone trusting it.

## Decision 2 — the Deflated Sharpe Ratio is reported, never gated on

`analytics/multiplicity.py` keeps one statistic. It answers what `efficiency`
cannot — how much of an in-sample figure the search explains — in Sharpe units
against a null model, where `efficiency`'s 0.5 threshold is a convention with
no derivation anywhere in this codebase.

Three defects were found and fixed before it shipped, one of them serious.

- **The PSR denominator.** Written as `1 - g3*SR + (excess/4)*SR^2`. The
  published form takes **raw** kurtosis, so in excess units the coefficient is
  `(excess + 2)/4`. The decisive check needs no source: under normality the
  denominator must reduce to Lo's (2002) `1 + SR^2/2`, and the wrong form gives
  `1`. It understated the estimator's variance and overstated confidence, by
  more as the Sharpe grew. Pinned by a test against the closed form.
- **The annualised-Sharpe trap, and the worst of the three.** PSR needs a
  per-period Sharpe, and `SweepCell.score` is annualised — so the obvious
  wiring is wrong. Fed an annualised figure, z lands around 15, where the
  normal CDF is 1.0 to machine precision: the statistic does not become wrong,
  it becomes *silently constant*, and every strategy survives. A per-period
  Sharpe above 1.0 now raises.
- **A branch that could not be reached honestly.** `variance <= 0` returned
  `0.0`. The denominator equals `(SR^2/4) * Var(Z^2 - (2/SR)Z)`, so it is
  non-negative for any real distribution; reaching it means the moments passed
  in are not jointly possible. Returning zero asserted the true Sharpe is
  certainly below benchmark — a fabricated certainty that `DeflatedSharpe`
  would have reported as a legitimate rejection. It raises.

One deliberate deviation from the literature is recorded in the docstring
rather than hidden: the expected maximum shrinks the *dispersion* to handle a
grid of near-identical cells, where the papers shrink the *trial count* via
mean pairwise correlation of the trials' return series. The dispersion lever
needs only the scalar scores, which is all a `SweepSurface` retains — but it
scales the benchmark linearly where N moves it through `Z_inv`, which is very
flat, so on a tight grid this deflates harder than the published lever would.
It is not conservative in a known direction, and it is labelled as ours.

## Decision 3 — the Probability of Backtest Overfitting was built, measured, and deleted

CSCV does not re-run a backtest per split. It computes each trial's returns
once over the full sample, then slices and reconcatenates blocks. This engine
is path-dependent: warm-up, position state, compounding, and a `RiskManager`
whose drawdown halt carries peak-equity state that `walk_forward` is careful
enough to rebuild per fold. Returns over a recombined training set are not
returns any strategy here would have produced, so PBO grades a synthetic
object.

That is the theoretical objection. The measurement agreed, which is why the
code is gone rather than merely unused. Sixteen-cell SMA grid, real engine,
900 daily bars, eight to twelve seeds per arm:

| series | implied max Sharpe | mean PBO | rejected at PBO < 0.5 |
|--------|-------------------:|---------:|----------------------:|
| white noise | 0.00 | 50.3% | 4 of 12 |
| AR(1), m=0.35 | 7.14 | 52.0% | 4 of 8 |
| AR(1), m=0.70 | 18.73 | 60.3% | 6 of 8 |

**PBO passed pure noise two thirds of the time, and moved the wrong way as a
real edge was planted.** Both are explained by the same thing: adjacent SMA
windows produce nearly identical fills, so the cells are not independent trials
and the winner's out-of-sample rank is close to arbitrary. A statistic about
whether *selection between cells* generalises has nothing to work with.

Two implementation defects were found and fixed before the deletion, and they
are worth recording because both are the same mistake in different clothes.
Ranking counted only strictly-worse trials, so a tie ranked *last*; and a logit
of exactly zero counted wholly as overfit. Together they reported PBO 100% for
twenty identical cells — "your selection is maximally overfit" for a sweep that
expressed no preference at all. Midranks and a half-count fix both, and both
cases are real here: cells that never trade all score exactly 0.0.

## The finding that outranks all three

Step 6's exit criterion was "a known-overfit sweep is rejected by at least two
of the four gates". It passes, on every seed tried, with room to spare. That
turns out to be weak evidence, because **the gates reject a genuine edge just
as reliably.**

Running the UI's own default `RunConfig` — `warmup=120`, `train_size=300`,
`test_size=100` — against AR(1) series with a planted edge, eight seeds per
row:

| bars | warm-up | momentum | implied Sharpe | folds | `SURVIVES` | median fills/fold | median efficiency |
|-----:|--------:|---------:|---------------:|------:|-----------:|------------------:|------------------:|
| 900 | 120 | 0.00 | 0.00 | 6 | 0 of 8 | 3.2 | −1.22 |
| 900 | 120 | 0.70 | 18.73 | 6 | 0 of 8 | 2.5 | 0.39 |
| 2400 | 120 | 0.00 | 0.00 | 21 | 0 of 8 | 3.0 | −0.44 |
| 2400 | 120 | 0.70 | 18.73 | 21 | 0 of 8 | 3.0 | 0.48 |
| 2400 | 0 | 0.00 | 0.00 | 21 | 0 of 8 | 2.0 | −0.68 |
| 2400 | 0 | 0.70 | 18.73 | 21 | 0 of 8 | 2.0 | 0.08 |

**Nothing survives, ever, at any sample length, at an implied annualised Sharpe
of 18.7.** ADR 0003 put the detection floor at "roughly annualised Sharpe 5";
that was measured at `warmup=0` with different windows. At the configuration
the product actually ships, there is no floor — the gate is closed.

The binding constraint is visible in the table: `SMACrossover` produces two to
three out-of-sample fills per fold against a `median_fills_per_fold < 10` gate,
so the fill objection fires on every run regardless of what is in the data.
Efficiency does respond to the planted edge, 0.48 against −0.44, but it never
gets a chance to matter.

This also settles a claim ADR 0003 made too confidently. That the false-
discovery rate "does not rise with sweep size" was originally observed at
around a dozen seeds per arm, where the Clopper–Pearson intervals overlap
completely — absence of an observed trend at that sample size is not evidence
of absence, which is precisely the error this repository exists to prevent.
Re-run at n=100 per arm, which is the sample size that can support the claim:

| grid | live cells | discoveries | rate | upper bound |
|------|-----------:|------------:|-----:|------------:|
| 3x3 | 9 | 1 | 1.0% | 4.7% |
| 5x5 | 25 | 0 | 0.0% | 3.0% |
| 8x8 | 64 | 0 | 0.0% | 3.0% |
| 12x12 | 144 | 0 | 0.0% | 3.0% |

The conclusion survives with the power to support it, over a sixteen-fold
range of grid sizes. But the better explanation for a flat, low rate is not that `efficiency` is
multiplicity-resistant. **It is that a pipeline rejecting essentially
everything cannot produce more false positives when you search harder.** A 1%
false-discovery rate against a 5% target is not a virtue here; it is
saturation, and the two numbers have the same cause.

## Consequences

- The registry is the honest bookkeeping and it stays. `family_wise_error` is
  the number to quote, not the per-run rate.
- DSR is reported alongside a verdict. It is not a gate, and after this
  measurement it should not become one: adding gates to an already-saturated
  pipeline costs power it cannot spare and buys no measurable protection.
- PBO is deleted rather than kept unused, because a statistic sitting in the
  tree is a statistic somebody wires up.
- **The next work is power, not multiplicity.** A pipeline blind to an edge at
  Sharpe 18.7 will reject every real strategy anyone writes here, and that is a
  more expensive failure than one false positive in a hundred. The two
  candidates are the fill gate against `SMACrossover`'s trade frequency, and
  the verdict thresholds themselves; ADR 0003's oracle arm — a strategy trading
  `sign(m·r_{t-1})` — is the instrument that separates them, and it has still
  not been run.
