---
name: portfolio-integrity
description: Check the charter §29 accounting invariants on a ledger, run or account cycle — cash + marked value = equity, fees reduce cash, no double-counted realised P&L, immutable history, cycles never overwrite. Use after any change to engine/portfolio.py, core/events.py, core/instruments.py, or before believing an equity curve.
---

# Portfolio integrity (charter §29)

## purpose
Accounting is where a plausible wrong number is born. This skill checks the
invariants that make the ledger deterministic, by running them, not reading.

## when_to_use
After any change under `engine/portfolio.py`, `core/events.py`,
`core/instruments.py`, `risk/`, or the account-cycle code; before an equity curve
informs a decision; after every paper day.

## inputs
A `BacktestResult` or `Portfolio`, or a cycle store; the instrument definitions
(spot vs derivative, margin rate); the fill list.

## procedure
1. **cash + market value = equity** at every `EquityPoint`, exactly, in `Decimal`.
   For derivatives: cash moves only by commission on a fill (`Fill.cash_impact`
   with `is_derivative=True`); notional is not debited. The 35a retraction was
   this invariant failing.
2. **Fees reduce available cash** by exactly the commission, per fill.
3. **Fills create positions correctly**: quantity, average price, side, flip
   handling; a full close leaves the position flat and its realised P&L booked once.
4. **Realised P&L is not double-counted**: sum of per-fill realised equals
   `total_realised_pnl`; unrealised at the last mark plus realised plus starting
   cash minus fees equals ending equity.
5. **Unrealised P&L moves with price** and only with price; a mark with no price
   change changes nothing.
6. **Orders cannot create impossible positions**: risk rejections prevent them;
   `_reduces_exposure` is false for a flip through zero (9b).
7. **Timestamps are consistent**: no mark before a fill it reflects; marks are
   non-decreasing; a mark older than one interval is reported by `mark_age`.
8. **History is immutable**: journal files only grow; `compact()` never rewrites
   a day; a cycle, once closed, is never modified.
9. **Cycles never overwrite**: cycle N+1 is a new ledger with a new number; cycle
   N's final state is still readable and equals what was recorded at close.

## tools
`tests/test_portfolio*.py`, `tests/test_derivative_accounting.py`,
`tests/test_risk*.py`; `risk-auditor` agent; `guard-auditor` on any test you add.

## validation
Each invariant has a test that would fail under a named mutation (drop the
commission line, flip the derivative flag, open the journal with "w"). Run
`scripts/run_mutation_eval.py --id P01 --id R01 --id R02` as the floor.

## outputs
Table of the nine invariants: holds / test that shows it / mutation that would
break it. Any violation in §119 shape with the exact fill and point that breaks it.

## failure_modes
Checking with floats. Checking equity only at the end. Testing one limit against
one order (the shape that hid R1). Reading "immutable" off a docstring.
