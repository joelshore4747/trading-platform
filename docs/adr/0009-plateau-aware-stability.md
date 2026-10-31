# ADR 0009 — Plateau-aware parameter stability

**Status:** PROPOSED — awaiting a human decision. `verdicts()` is unchanged.
**Date:** 2026-09-03

## Problem

`WalkForwardReport.parameter_stability` counts a fold transition as unstable
whenever the chosen parameters differ at all. `neighbour_ratio` reads a broad
plateau of near-equal cells as evidence of a real effect. On exactly that
plateau the in-sample argmax wanders between adjacent cells by noise, so the two
gates read the same evidence in opposite directions. ADR 0005 recorded the
contradiction and left it open.

## Evidence — E-015 (synthetic), VERIFIED

`scripts/measure_plateau.py`, 12 symbols, 900 daily instants, per-symbol AR(1)
latent drift, `CrossSectionalMomentum` on a 9-cell grid, 10 seeds per row:

| persistence | IC range | discoveries, current gate | discoveries, plateau-aware | stability-only failures recovered |
|---|---|---|---|---|
| 0.00 | −0.06..0.04 | 0/10 | 0/10 | — |
| 0.50 | −0.06..0.05 | 0/10 | 0/10 | — |
| 0.70 | −0.04..0.09 | 1/10 | 1/10 | 0 of 0 |
| 0.90 | 0.11..0.25 | 7/10 | 9/10 | 2 of 3 |
| 0.95 | 0.27..0.42 | 7/10 | 10/10 | 3 of 3 |

Plateau-aware stability = share of fold transitions that stayed on the same cell
or moved one step along one axis (`_is_adjacent`), with the same 50% threshold.
In every recovered seed the moves were adjacent; the one seed not recovered had
jumped across the grid, and the current gate was right to refuse it.

## Options

**A. Replace** the current stability verdict with the plateau-aware one.
**B. Accompany**: report both; the current verdict stays the gate, the
plateau-aware figure is printed beside it so a reviewer can see when the
objection is a wander rather than a jump.
**C. Leave as is.**

## Recommendation — B, and only after a human says so

The null arm shows no extra noise admitted, on this generator. That is one
generator, synthetic, and the recoveries all sit at information coefficients
two to eight times what real cross-sectional momentum offers. Relaxing a gate on
that evidence would be exactly the move this repository exists to refuse:
accompany, do not replace, and re-run on real data the first time a strategy
actually reaches the stability objection. Changing `verdicts()` is a §69 act
(a safety control) and is not done by this ADR.

## What would make this wrong

A generator whose noise produces adjacent wanders by construction (the 3×3 grid
has few non-adjacent moves available: of 8 other cells, 2–4 are adjacent). A
real strategy whose stability failures are jumps, where B changes nothing. A
null rate that differs at higher seed counts (10 seeds locate behaviour, not a
rate).

## Revisit when

A strategy on real data fails the stability gate; or E-015 is re-run with 100
seeds per row and a 4×4 grid.
