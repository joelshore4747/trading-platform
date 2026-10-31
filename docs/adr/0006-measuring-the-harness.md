# ADR 0006 — The defences were never measured

**Status:** Accepted
**Date:** 2026-08-27

## Context

This repository is unusually well defended. Eight subagents, eight commands,
two skills, five hooks, a CI pipeline that runs the lookahead guards as their
own step, and a false-discovery gate that puts unpredictable data through the
whole evaluation pipeline a hundred times to check the pipeline can tell signal
from noise.

Almost none of it had ever been measured.

`scripts/prompt_guard.py` was the exception, and it is the exception that makes
the argument: it is the only part of the AI layer with a corpus behind it, and
writing that corpus **found four bugs in rules that read perfectly well**,
including the worst one available — *"make this strategy profitable"*, the
example the whole guard exists for, did not match.

Everything else in `.claude/` is prose. Prose is not a control. And the specific
thing nobody had asked is this: **what does `lookahead-auditor` miss?** It runs
on a frontier model precisely because a false negative there is catastrophic —
a missed leak does not crash, it produces a beautiful fictional equity curve
that then informs a decision. Its false-negative rate was unknown. So was its
false-positive rate, which matters for a different reason: a reviewer that
cries wolf gets switched off, and a switched-off reviewer detects nothing.

There is a second, sharper reason to do this now. The most repeated defect in
this project's history is not lookahead and not a risk gap. It is the **guard
that cannot fail** — four separate instances, each surviving a green suite and
a careful review:

1. A windowed `Panel` read prices from the start of the file. Tests checked a
   windowed panel's *grid* and an unwindowed panel's *values*, never a windowed
   panel's values.
2. `test_listings_are_keyed_by_market_symbol_and_interval` asserted only that
   construction did not raise, which it never did under any keying.
3. The null harness's lookahead mutant was a stub returning a constant
   `SURVIVES` — inside the module built to catch exactly this.
4. Every risk test exercised one limit against one order in isolation. Seven
   gaps, two critical, all in composition. No individual test was wrong; the
   *shape* could not reach the bug class.

Four instances, four different discovery routes, none of them systematic.

## Decision

Build `evals/`: a corpus of defects that **actually happened here**, each
reintroducible into a throwaway copy of the tree, and run two questions over it.

| Level | Question | Cost | Where |
|---|---|---|---|
| Mutation | does the **test suite** catch it? | free | CI, every push |
| Agent | does the **auditor** catch it? | real money per run | opt-in |

The methodology is ADR 0003's, pointed at the reviewers instead of the
strategies. ADR 0003 put data with no predictability through the pipeline and
counted false discoveries. This puts code with known defects through the
defences and counts false negatives — and clean code through them and counts
false positives.

**Only real bugs go in the corpus.** A corpus of invented bugs measures a
reviewer against the imagination of whoever wrote the corpus, which is the same
person whose blind spots the reviewer exists to cover. Bugs that got past a
real review, into a real commit, are the only ones known to be capable of it.
`docs/ROADMAP.md` documents this project's own defects unusually well, and that
archive is the corpus's supply.

**Anchors, not patches.** A unified diff applies with fuzz and can land subtly
wrong once the code moves. An anchor that no longer appears exactly once raises.
The corpus reports its own staleness rather than silently mutating the wrong
line.

## What the measurement found

### The suite has a hole, and it is the shape the corpus predicted

First full run: **6 of 8 killed, 1 declared survivor, 1 undeclared hole.**

The hole is L02 — substituting `bar.low` for `min(limit, bar.open)` in
`_resolve_price`, so a limit buy fills at the best price the bar happened to
print. **No test in the suite objected.** Optimistic limit-order fills were
invisible to all 685 non-slow tests.

The reason is worth more than the bug. The test that should have caught it,
`test_limit_buy_fills_when_price_trades_through`, asserts:

```python
assert fills[0][0].price <= Decimal("96"), "fills at the limit, not the open"
```

A one-sided bound on a two-sided property. A fill at the low of 90 satisfies it
comfortably. The test's own message names the distinction it cannot make —
*"fills at the limit, not the open"* — while the failure mode is *fills at
neither, at something better than both*. That is pattern 2 from the list above,
found mechanically this time rather than by luck, in code nobody had flagged.

Closed by pinning the price exactly rather than bounding it, plus the mirrored
sell case, since L02 mutated only the buy path and the sell path is the same
shape.

### The harness caught a defect in itself, twice

Both worth recording, because both are the failure mode this project keeps
finding and neither would have raised.

**`ERROR` is not `KILLED`.** Excluding `evals/` from the mutant tree — which is
load-bearing, since an auditor that can read the answer key would score
perfectly while detecting nothing — left `tests/test_evals.py` in place,
importing a package that was no longer there. Every mutant died at collection
with `ImportError`. Had the runner scored an unimportable tree as "caught", the
harness would have reported a **flawless defence** while measuring nothing at
all. The three-way outcome caught it. The fix belongs in the tree construction,
not the classifier.

**The first agent run measured a hobbled reviewer.** It passed
`--allowedTools Read,Grep,Glob`, while every auditor definition here declares
`Bash` and instructs the agent to *prove* a finding by running a test — "a
suspicion you did not test is a suspicion". The agent said so in its own report,
unprompted: *"I could not execute the test suite... I flag that explicitly
rather than implying I ran it."* The number would have understated every rate.

That fix has a trap of its own, and it is a stale-reference bug of exactly the
kind this repository keeps finding: the project venv holds an **editable
install pointing at the real `src`**, so `.venv/bin/python -m pytest` inside a
*linked* mutant tree imports the unmutated package and reports every mutant
clean. Each mutant builds its own venv.

### The auditors, so far

Measured `--blind`: `docs/ROADMAP.md` and `docs/adr/` are removed from the tree
first, because they narrate most of this corpus in detail and a sighted run
would measure whether the auditor can *read about* the bug rather than find it.
Both are legitimate capabilities; only one is detection.

Three scores are kept separate, because collapsing them hides the failure that
matters. `flagged` — a non-clean verdict — is nearly meaningless alone, since an
agent that flags everything scores 100%. `localised` — names the mutated file —
is weak, since a lookahead audit names `backtester.py` most times. `identified`
is the one worth quoting: a separate judge, shown the defect's `tell` and the
report but **never the diff**, says the report describes *this* defect.

First run, blind, 8 mutants, $36.10: **7 identified, 1 missed**. Two of the
measurement's own arms held — an unmutated tree drew a genuinely clean verdict
from `lookahead-auditor`, and the cross-judge wrongly identified none, so the
`identified` column is discriminating rather than rubber-stamping. Per-defect
figures are in `docs/ROADMAP.md` rather than frozen here, because they will move
as the agents are edited. Three things about that run will not move.

**The miss was the harness's own defect, and that is the run's best result.**
`guard-auditor` was handed a `test_lookahead.py` whose headline assertions had
been replaced by `assert timestamp is not None`, and reported the file as
*"17/17 green"* before spending its report on unrelated `test_prompt_guard.py`
failures — trusting a passing suite, the one thing its brief forbids.

Those failures were manufactured by the measurement: `prepare_for_agent` deleted
`.claude/hooks`, and eight tests invoke `check-prompt.sh` on disk. Every auditor
in the first run was handed a tree with eight unrelated red tests. Re-run on a
corrected tree the agent **identifies G01**, reasoning that `Fill.timestamp` is
non-optional so `is not None` holds under any fill timing, and confirming with
two independent mutations — at $2.14 against $5.08, having spent nothing on the
noise.

The general lesson outranks the number. **Preparing a tree for measurement is
itself a change to the thing being measured**, and the harness's setup code
needs the same scepticism as the corpus. Nothing in the prose of either the
agent or the harness would have revealed this; only running it did.

**The control arm found two live defects, and I had scored them as errors.**
The runner labelled a flagged control `FALSE POSITIVE` — but a control tree is
not clean code, it is *this repository*. Both flags were real: a fifth
guard-that-cannot-fail in `test_tearsheet.py`, and a single-order flip through
zero that passes the halt with every size cap skipped (roadmap 9b). A metric
that would have recorded two genuine findings as noise is worse than no metric,
because it argues for ignoring the reviewer.

**Failures were being printed as clean verdicts.** Two runs hit the timeout and
returned no data; `AgentRun.flagged` is False when a run failed, so the control
table rendered them as `clean`. There was already a test asserting a failed run
is not scored as clean — and the *report* did it anyway, in a different
function. A guard covering the intent while the output contradicted it.

The finding that outranks all of these is structural: before this existed, none
of these numbers had a value at all.

## Consequences

- **A new agent.** `guard-auditor` (Opus) — the most repeated defect class in
  this repository had no reviewer. Its brief is built from the four instances
  above as patterns rather than anecdotes, and it is told to prove a suspicion
  by mutation rather than report it.
- **Two workflows** in `.claude/workflows/`. `AI_WORKFLOW.md` section 4
  described an audit loop in prose and nothing executed it. `gauntlet` runs five
  lenses in parallel, hands every finding to an independent skeptic told to
  refute it, and pre-mortems whatever survives. `guard-sweep` runs
  `guard-auditor` over the suite and proves each suspicion by mutation.
- **CI runs level 1 on every push.** Free and deterministic. Level 2 is
  deliberately absent: it costs money per run.
- **A `known_survivor` flag.** A declared hole is a finding, not a corpus error
  — but it must be *declared*, so that a new hole still fails the build. G01,
  the weakened lookahead guard, is one by construction: no suite can catch a
  test that stopped checking anything.
- **The corpus is small and the runner says so.** ADR 0003 recorded that ten
  seeds cannot demonstrate a 5% rate; the same arithmetic applies here. At this
  size an *ordering* is evidence and a *percentage* is an anecdote. The runner
  prints that caveat below twenty mutants rather than trusting a reader to
  remember it.

## What this does not establish

It does not show the auditors are good. It shows what they do against eight
defects this repository has already had — which is, by construction, the class
of defect most likely to be *anticipated* by the very briefs being measured,
since several of those briefs were written in response to these bugs.
`lookahead-auditor`'s section 4 names the exact timestamp traps D01 exercises.

The honest reading is a floor, not a rate: an auditor that misses a defect its
own brief describes has a real problem, and one that catches it has cleared a
low bar. The corpus's value grows as defects enter it that **no brief
anticipated** — which is what `guard-sweep` is for, and why a confirmed finding
from it should become a corpus entry rather than just a fix.
