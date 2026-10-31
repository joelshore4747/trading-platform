# ADR 0008 — Parameter stability was measuring identity, not stability

**Status:** Accepted
**Date:** 2026-08-28

## Context

ADR 0005 measured the detection floor and found it sitting near **IC 0.05**
while real cross-sectional momentum runs **IC 0.02–0.05** — the pipeline
starting to see an edge at the top of the range that actually exists. It named
one suspect: `parameter_stability` and `neighbour_ratio` read a plateau in
opposite directions, and on a genuine edge the stricter one wins.

Per-gate firing rates confirmed it. Eight seeds per arm, the ranker through the
real walk-forward at the UI's default windows:

| IC | survived | efficiency<0.5 | grid profitable | neighbour ratio | **param stability** |
|------:|------:|------:|------:|------:|------:|
| −0.003 | 0/8 | 7/8 | 1/8 | 8/8 | **8/8** |
| 0.016 | 0/8 | 4/8 | 1/8 | 7/8 | **6/8** |
| 0.053 | 3/8 | 2/8 | 0/8 | 4/8 | **4/8** |
| 0.188 | 6/8 | 0/8 | 0/8 | 0/8 | **2/8** |
| 0.395 | 6/8 | 0/8 | 0/8 | 0/8 | **2/8** |

At IC 0.188 and 0.395 — four to eight times anything real — every other
objection has gone silent and `parameter_stability` alone still refuses.

## The diagnosis, measured

`parameter_stability` counted how often the chosen parameter *tuple* changed. A
broad effect makes many cells work, so the argmax wanders between near-equals.
`neighbour_ratio` reads that as a plateau and calls it evidence of a real
effect; identity-based stability read the same evidence as fitting.

The discriminating question is not "did the tuple change" but "would carrying
the previous fold's parameters forward have cost anything" — which is what the
gate's own docstring always claimed to care about, and which is answerable from
data already retained, since `FoldResult.surface` holds every cell's score for
every fold.

Scoring the previous fold's parameters on the next fold's surface separates the
cases completely:

| | parameter changes (8 seeds) | carried-forward score ÷ re-optimised best | share ≥ 0.8 |
|---|---:|---:|---:|
| null | 32 | median **0.13** | **7%** |
| IC 0.188 | 11 | median **0.95** | **100%** |

On noise, yesterday's parameters retain 13% of today's best — real instability.
On a genuine edge they retain **95%**, on every single transition.

## Decision

`WalkForwardReport.parameter_stability` is the share of fold transitions where
the previous fold's parameters, scored on the new fold's surface, retain at
least `CARRY_FORWARD_TOLERANCE = 0.8` of the new best. A transition is unstable
when those parameters are absent from the new surface, or when the new best is
not positive — a fold with no positive edge has nothing to carry, and a ratio
against a non-positive denominator is not a number anyone should act on.

**The 0.5 gate did not move.** What changed is the statistic it reads. This is
not a threshold relaxation, and the distinction is the whole reason the change
is defensible: loosening a gate until a strategy passes is the failure this
repository exists to prevent.

`parameter_changes` is unchanged and still reported. It is a fact worth having;
it simply stops being what a gate fires on.

The tolerance is sourced to the measurement above rather than to taste, and it
is not load-bearing: the null sits at 0.13 and a real edge at 0.95, so anything
from roughly 0.3 to 0.9 divides them. A test asserts the classification holds
across that whole range.

## Both sides, because one side is worthless

**Detection improved.**

| IC | survived before | survived after | stability fired before → after |
|------:|------:|------:|------:|
| −0.003 | 0/8 | 0/8 | 8/8 → 7/8 |
| 0.016 | 0/8 | 0/8 | 6/8 → 5/8 |
| 0.053 | 3/8 | 3/8 | 4/8 → 2/8 |
| 0.188 | 6/8 | **8/8** | 2/8 → **0/8** |
| 0.395 | 6/8 | **8/8** | 2/8 → **0/8** |

**The null did not move at all.** 100 trials per arm, both nulls, before and
after:

```
Gaussian i.i.d.  1/100 = 1.0%  upper 4.7%   {FITTED 83, NO EDGE 13, LOSES TO HOLDING 3, SURVIVES 1}
Regime.NULL      1/100 = 1.0%  upper 4.7%   {FITTED 91, NO EDGE 6, LOSES TO HOLDING 1, SURVIVES 1, UNDERPOWERED 1}
```

Identical on both arms — the same rate, the same upper bound, the same outcome
histogram down to individual counts. The stability gate was never the binding
objection on a null run; efficiency and the neighbour ratio were already firing
there, which is why removing its false negatives costs nothing.

## What this does NOT fix, stated plainly

**The detection floor has not moved.** At IC 0.053 survival is 3/8 before and
3/8 after. What this change removes is the *ceiling* — the residual refusal at
edges far above anything real — not the floor that ADR 0005's headline was
about.

The binding gate at IC 0.053 is now `neighbour_ratio`, firing 4/8. Task #11 is
therefore **partly** addressed: the pipeline no longer refuses an unmistakable
edge, but it still turns on around IC 0.05 against a real-world 0.02–0.05.
Whether `neighbour_ratio`'s 0.5 threshold is calibrated, or is making the same
category error one level down, is the next question and is not answered here.

## Consequences

- ADR 0005's detection table is superseded at the top end: 6/8 becomes 8/8 at
  IC 0.188 and 0.395. Its headline — detection turning on near IC 0.05 — still
  stands.
- A guard's meaning changed, so its test was rewritten rather than adjusted, and
  says in its docstring that the expectation was reversed and why. Varying
  parameters on a plateau is now *stable*, which is the point.
- Rewriting that test exposed a fixture that had never been internally
  consistent: its folds chose a parameter key its surface had never contained.
  That went unnoticed for as long as stability compared tuples and never
  consulted the surface.
