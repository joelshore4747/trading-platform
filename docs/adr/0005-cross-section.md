# ADR 0005

> **Addendum 2026-09-03:** the open item on `neighbour_ratio` versus `parameter_stability` was measured in E-015 (CR-003) and is the subject of ADR 0009 (PROPOSED). Nothing below is changed.
 — The gate was never closed; the probe could not reach it

**Status:** Accepted
**Date:** 2026-08-26

## Context

ADR 0004 ended with a finding I ranked above the rest of the roadmap: at the
UI's default configuration nothing survived at an implied annualised Sharpe of
18.7, at any sample length. `SMACrossover` produced two to three out-of-sample
fills per fold against a `median_fills_per_fold < 10` gate, so the fill
objection fired whatever was in the data. I filed it as "the gate is closed"
and said it outranked steps 7–10.

Step 7 was to build the cross-sectional API and a first ranker. Before
planning it I checked whether it was blocked by that finding. It is not, and
checking turned the finding around.

## What a ranker does to the trade count

A throwaway top-k ranker on correlated synthetic series, fills per 100-bar
window — the walk-forward's `test_size`:

| symbols | median fills per 100-bar window |
|--------:|--------------------------------:|
| 5 | 25.0 |
| 10 | 39.0 |
| 20 | 46.0 |
| 39 | 46.0 |

Five symbols is already ten times `SMACrossover`'s trade count. So the fill
gate stops binding immediately, and the other five objections — which had
never once been the binding one — become measurable for the first time.

## The measurement

`CrossSectionalMomentum` through the real walk-forward at the UI's own default
windows (`train=300`, `test=100`, `warmup=120`), 12 symbols, 900 instants, a
nine-cell grid over `lookback` and `rebalance_every`, realistic `CostModel()`,
five seeds per row.

Each symbol carries a slow-moving latent drift; `persistence` is how much of
it survives to the next bar. At zero the drift is fresh noise every bar and no
ranking can beat any other. Reported against the **information coefficient**,
the rank correlation between a symbol's trailing return and its forward
return, because "persistence 0.9" means nothing to a reader and an IC does:
real cross-sectional equity momentum runs an IC of roughly **0.02 to 0.05**.

| persistence | IC | survived | median fills/fold | median efficiency | binding objection |
|------------:|------:|---------:|------------------:|------------------:|-------------------|
| 0.00 | 0.001 | 0 of 5 | 36.5 | −0.10 | efficiency |
| 0.50 | 0.007 | 0 of 5 | 37.5 | −0.13 | efficiency |
| 0.70 | 0.019 | 0 of 5 | 74.5 | 0.54 | neighbour ratio |
| 0.80 | 0.053 | 1 of 5 | 133.0 | 0.83 | neighbour ratio |
| 0.90 | 0.182 | 4 of 5 | 130.5 | 1.02 | parameter stability |
| 0.95 | 0.393 | 4 of 5 | 121.5 | 1.09 | parameter stability |

**The pipeline discriminates.** Nothing survives a genuine null, and four in
five survive a strong edge. That is the two-sided result ADR 0003 said a
harness must produce and this project had never once obtained.

## What this retracts, and what replaces it

ADR 0004's headline was wrong in its explanation and its priority. The gate was
not closed. `SMACrossover` could not generate enough evidence to reach it, and
the fill gate was doing exactly its job — refusing to compute a Sharpe from
three trades. I read "the probe cannot produce evidence" as "the pipeline
cannot detect anything", which is the same error as reading a wide confidence
interval as a measurement, and I made it in the same document where I corrected
myself for doing that with sweep size.

The replacement finding is narrower and worse in a different way. Detection
turns on somewhere between **IC 0.019 and IC 0.182**, with the crossing near
0.05. Real cross-sectional momentum lives at 0.02 to 0.05. **So the pipeline
starts to see an edge at roughly the top of the range that actually exists,
and would miss a typical one.** It is at the edge of usefulness rather than
blind — a much better position than ADR 0004 described, and not yet a good one.

Five seeds per row cannot locate that threshold precisely; the ordering is
solid and the crossing point is not. It is a finding, not a constant, and it
is deliberately not asserted in any test.

## The objection that migrates

Which gate refuses changes with the strength of the edge: efficiency on pure
noise, neighbour ratio on a weak edge, parameter stability on a strong one.
The first two are the cascade working. The third is not obviously right.

At IC 0.39 — an edge eight times anything real — one seed in five still fails
on *parameter stability*, because the best `(lookback, rebalance_every)` cell
moves between folds. When an effect is strong and broad, many cells work and
the argmax wanders between near-equals. That is a plateau, which
`neighbour_ratio` treats as the mark of a real effect and `parameter_stability`
treats as the mark of a fitted one. The two gates read the same evidence in
opposite directions, and on a genuine edge the stricter one wins. That is a
false-negative source with a plausible mechanism and it is the next thing to
look at.

## Consequences

- Task #11 is rewritten rather than closed. The work is not "the gate is
  closed" but "detection turns on an order of magnitude above where real
  effects live", and the candidates are the verdict thresholds, not the fill
  gate.
- ADR 0003's oracle arm is still unrun, and is now less urgent: the ranker
  itself did the decomposition the oracle was meant to do, by being a strategy
  that trades enough to reach the gates.
- The detection floor's location is a finding and is never asserted. Pinning it
  would make every legitimate improvement a test failure.
- `SMACrossover` remains the wrong probe for anything about the verdict layer.
  Any future statement about what this pipeline can or cannot detect must say
  which strategy produced it, because ADR 0004 shows the answer is mostly a
  property of the probe.
