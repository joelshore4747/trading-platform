---
description: Measure this repository's own defences — run the mutation eval, and optionally the agent eval, then read the result honestly.
argument-hint: [mutation | agents | both] [extra flags]
---

# Harness: $ARGUMENTS

Everything under `.claude/` is prose, and prose is not a control. This command
runs the two measurements that put numbers on it. Details in `evals/README.md`;
the standing findings are in `docs/adr/0006-measuring-the-harness.md`.

## What to run

Default to **mutation** unless the argument says otherwise. It is free and
fast; the agent eval costs real money per run.

```bash
.venv/bin/python scripts/run_mutation_eval.py                 # level 1, ~6 min
.venv/bin/python scripts/run_mutation_eval.py --targeted      # faster, weaker answer
.venv/bin/python scripts/run_agent_eval.py --dry-run          # plan + routing, no calls
.venv/bin/python scripts/run_agent_eval.py --id L01 --controls 1
```

Never run the agent eval without saying what it will cost first, and never run
the full corpus without being asked to.

## Reading the mutation result

| Outcome | Means |
|---|---|
| `KILLED` | Some test objected. The defence holds for that defect. |
| `SURVIVED` + `(declared)` | A known hole, recorded in the corpus on purpose. |
| `SURVIVED` + `<-- HOLE` | **The finding.** A property the suite claims and does not check. |
| `ERROR` | The mutant did not run. Never a pass — diagnose it. |

An undeclared hole is the whole point of the run. Do not close it by deleting
the corpus entry. Close it by writing the test that kills the mutant, then
re-run that single id to prove it:

```bash
.venv/bin/python scripts/run_mutation_eval.py --id L02 --skip-control
```

Two traps in the output:

- **`killed by` names one killer, not the only one.** `-x` stops at the first
  failure in collection order. It is not evidence that any other test missed it.
- **A stale corpus raises rather than skipping.** `DefectError: anchor appears
  0 times` means the source moved, not that the defect is gone. Re-anchor it.

## Reading the agent result

Three columns, and only one is worth quoting:

- `flag` — the agent returned a non-clean verdict. Nearly meaningless alone: an
  agent that flags everything scores 100%.
- `local` — the report names the mutated file. Weak; a lookahead audit mentions
  `backtester.py` almost every time.
- `ident` — a separate judge, shown the defect's `tell` but never the diff, says
  the report describes *this* defect. **Quote this one.**

`cross-judge` is the judge's own control: each report re-scored against a
defect it does not describe, where the answer should always be no. A non-zero
count invalidates the `ident` column rather than merely adding noise to it —
report that, do not average past it.

The control rows matter as much as the mutant rows. A false positive on a clean
tree is not a harmless excess of caution — a reviewer that cries wolf gets
switched off, and a switched-off reviewer detects nothing. That is the same
reasoning that keeps `prompt_guard` non-blocking.

`--sighted` leaves `docs/ROADMAP.md` and `docs/adr/` in the tree. Most of the
corpus is narrated there in detail, so a sighted run measures whether the agent
can *read about* the bug. Both numbers are real and they answer different
questions. **Always say which mode produced a number.**

## What not to conclude

The corpus is small. ADR 0003 already recorded that ten seeds cannot
demonstrate a 5% rate, and the same arithmetic applies here: at this size an
*ordering* between agents or categories is evidence and a *percentage* is an
anecdote. Report the counts, not a rate dressed up as a measurement.

A defect that survives both levels is the useful output — no test catches it
and no reviewer catches it. Say so plainly and propose the cheapest check that
would.
