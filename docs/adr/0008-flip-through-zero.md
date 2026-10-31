# ADR 0008 — A flip through zero is never a reduction

**Status:** Accepted
**Date:** 2026-09-02

## Problem

`RiskManager._reduces_exposure` decided whether an order was exempt from the
size caps and permitted while halted by testing `|projected| < |current|`. A
flip that lands on a smaller position of the opposite sign satisfies that test.
Such an order closes the whole book and opens the far side in one move, takes
the early return that skips every size cap, and passes the halt check because it
is labelled risk-reducing. Roadmap 9b; found 2026-08-27 by `risk-auditor` on an
unmutated tree; open since.

## Evidence — VERIFIED

Reproduced in `tests/test_risk_composition.py::test_a_halted_account_cannot_flip_to_a_smaller_short_in_one_order`:
a halted account long 100 with `max_position_notional=1500` accepted one sell of
130 at 100 — 13,000 of notional, landing short 30, zero rejections. The code
comment above the early return asserted the opposite ("a reducing order cannot
overshoot past flat"), which is why the return looked safe.

R1's fix (ADR-less, roadmap step 9) closed the path where a position is walked
through zero by *slicing* one intent into several orders. The single-order path
stayed open. `target_notional` emits exactly one order for a rebalance, so any
short-capable strategy reaches it without trying.

## Options

**A. Refuse.** `_reduces_exposure` returns False on any sign change. A flip is
then measured by its full order size against the caps, and refused while halted.
A strategy that wants to change side sends two orders. Cost: `target_notional`
(the sanctioned convenience) would make single-order flips that the caps now
refuse, so the context must split a crossing target into a flatten plus an
entry. No change to the event loop.

**B. Split in the risk layer.** `check` would have to return two decisions for
one order, or the engine would have to rewrite the order. That changes the
`check → submit` contract that `tests/test_lookahead.py` and the corpus rely on,
and touches `_queue_orders`, which the milestone rule says not to touch without
its own plan.

**C. Leave it, document it.** The two shipped strategies are long-only. Rejected:
`CrossSectionalMomentum` is one sign change away, and a limit that holds only
for the strategies that happen to exist is not a limit.

## Decision

**A.** The risk layer refuses; the context splits.

- `_reduces_exposure`: a projected position with a different sign from the
  current one is not a reduction. Exactly zero has no sign and remains the
  largest permitted reduction, so a full close while halted is still allowed —
  the trapped-account bug that the exemption exists to prevent stays prevented.
- `StrategyContext.target_notional`: when the target lies on the other side of
  flat, submit a flatten (exempt: it only shrinks the book) and then the entry
  (checked on its own, as the far side always should have been). The entry is
  what is returned.

This is a tightening. No default changed; no limit was relaxed.

## Consequences

- A single order that crosses zero is now refused when halted (reason `halted`),
  and when not halted it is measured by its full size against the order and
  position caps — which will refuse it under the shipped defaults for any
  position near the cap. Strategies must flatten then enter; the context does
  this for them.
- `target_notional` can now submit two orders for one call; both count against
  `max_orders_per_bar`.
- Every figure recorded before this date was produced by long-only strategies
  and is unaffected. **Not re-verified by re-running**: stated from the
  strategies' code, which never emits a sell beyond the position.
- Corpus entry R03 reintroduces the defect; `scripts/run_mutation_eval.py --id R03`
  must report KILLED.

## Follow-up found by the auditors the same day

`risk-auditor` on this change: (1) `on_cross_section` orders fill inside the
timestamp they were approved under, so their pending deltas were counted twice
for the rest of the instant — a halted account could re-enter at full size
after a cross-section flatten. `RiskManager.release_pending()` now runs after
the fill loop. (2) The reducing branch credited neither `_pending_gross` nor
`_pending_margin`, so a split flip's entry was falsely refused on leverage.
(3) `target_notional` rounded the target rather than the delta. (4) The rate
limit sits above the reducing exemption — considered and **kept**; see the
comment in `check`. `guard-auditor`: the short→long flip cell was untested
and the suite stayed green with the flatten side hard-wired. Fixed.

## Second follow-up, 2026-09-03 (gauntlet)

The `release_pending()` that closed finding (1) above was itself a defect: it ran per bar
event and cleared approvals for orders that had not filled, so in a multi-symbol instant
the caps and the halt were bypassable again by splitting intent across symbols. Replaced
by per-order approvals released by the fill that consumes them (`RiskManager.release_filled`),
with two multi-symbol regression tests. Corpus R04 updated to the new line.

## Revisit when

A strategy needs a one-order flip for execution reasons (crossing spread twice
costs a round trip). The answer is then a deliberate `flip` primitive that
submits the pair atomically, not a return to the old label.
