# EVALUATION_PROTOCOL

What must run before anything is believed, and what each check does and does not establish.
All four layers exist and are VERIFIED; the protocol is the order.

## Layer 0 — the suite (every push)
`pytest` 907 tests (2026-09-02), lookahead guards **first and separately** (17 tests), `mypy --strict`,
`ruff`. Establishes: the code does what its tests say. Does not establish: that the tests can
fail — that is Layer 1.

## Layer 1 — mutation eval (every push, CI)
`scripts/run_mutation_eval.py`: 14 real defects reintroduced into throwaway trees; the suite
must object. Control runs first (unmutated tree must pass) or the run aborts. Current
(2026-09-02, after R03, R04, L04): **13/14 killed, 1 declared survivor (G01, by construction), 0
undeclared holes.** Two mutants are now caught first by the new `live/` tests (L01 by the
cross-section release test, P01 by the cycle-on-zero test) — collection order, not a gap.
Establishes: the suite catches these defects. Does not establish: it catches unlisted ones.
A SURVIVED is a finding; close it by writing the test, not by deleting the entry.

## Layer 2 — agent eval (opt-in, costs money)
`scripts/run_agent_eval.py`: each auditor, from its own definition with its declared tools,
against each mutant; a judge shown the `tell` but never the diff scores `identified`.
Controls: clean tree (must be clean or adjudicated), cross-judge (must be 0). Current:
**8/8 identified, controls held, $38.** Establishes: auditors catch defects the repo has
already had. Does not establish: they catch novel ones — the briefs anticipate this corpus.
Blind mode (default) removes ROADMAP/ADR from the tree; sighted measures reading-about.

## Layer 3 — the gauntlet (before any number informs a decision)
`.claude/workflows/gauntlet.js`: five lenses (lookahead, risk, overfitting, guards,
strategy) in parallel, every finding handed to an independent skeptic told to refute it,
pre-mortem over survivors. First run: 41 agents, 27 confirmed, 8 refuted, found 35a.
Establishes: a result survived adversarial review. Does not establish: it is right — a
defect in the engine is one all lenses would share.

## Layer 4 — the null harness (CI, `@slow`)
`scripts/measure_null.py`: unpredictable data through the whole pipeline, 100 trials; count
false discoveries. **1% at 4,800 OOS bars; 7–8% below ~1,000.** Establishes: the pipeline's
own false-discovery rate at a given sample size. Does not establish: power against real
edges above the generator ceiling.

## What "survives" means — and does not
`WalkForwardReport.survives` is true only when no verdict fires. It means **failed to
falsify on this data at this cost model**. Never "works". The strongest claim this repository
can make about a strategy is that one.

## Order for a new result
suite → mutation eval → walk-forward with `benchmark_symbol` → gauntlet → `/cost-check` →
`/pre-mortem` → deflated Sharpe against the real trial count → then say "failed to falsify".
