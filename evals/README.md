# `evals/` — measuring this repository's own defences

Everything under `.claude/` is prose: eight agents, eight commands, two skills,
five hooks, two workflows. Prose is not a control. `scripts/prompt_guard.py`
was the only part of the AI layer with a corpus behind it, and writing that
corpus found four bugs in rules that read perfectly well.

This package extends that treatment to the rest, and it exists because of a
pattern in this project's own history. Four times, a defect has survived a
green suite and a careful review, because the test that claimed to cover it
**could not fail**. Each was found later, by accident or by an unrelated audit.
Meanwhile `lookahead-auditor` — the highest-stakes reviewer here, on a frontier
model for that reason — has never been measured at all. Nobody knows what it
misses, and a missed leak does not crash: it produces a beautiful, fictional
equity curve.

The methodology is ADR 0003's, pointed at the reviewers instead of the
strategies. ADR 0003 put data with no predictability through the pipeline and
counted false discoveries. This puts code with **known** defects through the
defences and counts false negatives — and clean code through them and counts
false positives, because a check that fires on everything says nothing.

## The corpus

`defects/*.toml`. Every entry is a bug that **actually happened here**, with
`provenance` naming the commit or ADR that records it.

That is a deliberate constraint, not sentiment. A corpus of invented bugs
measures a reviewer against the imagination of whoever wrote the corpus — the
same person whose blind spots the reviewer exists to cover. Bugs that got past
a real review, into a real commit, are the only ones known to be capable of it.

Anchor-based rather than patch-based. A unified diff applies with fuzz and can
land somewhere subtly wrong once the surrounding code moves; an anchor that no
longer appears **exactly once** raises `DefectError`. The corpus reports its own
staleness instead of silently mutating the wrong line.

```toml
id = "R01"
category = "risk"                    # routes the defect to a reviewer
severity = "critical"
provenance = "ROADMAP step 9, defect R1"
expected_agent = "risk-auditor"
tell = """                           # the rubric a judge scores a report against
The reviewer must say that because fills land on the next bar, every order in a
batch is measured against the same unchanged position...
"""
expected_killers = ["tests/test_risk_composition.py"]
known_survivor = false               # declared holes are not failures

[[mutation]]
file = "src/trading_platform/risk/limits.py"
find = "...exact text, must appear once..."
replace = "...the defect..."
```

## Two levels, one corpus

### Level 1 — does the test suite catch it?

```bash
.venv/bin/python scripts/run_mutation_eval.py
```

Free, deterministic, and wired into CI. A mutant the suite does not kill is a
**guard that cannot fail**, found mechanically rather than by luck.

A control runs first and is not optional: an unmutated tree must come back
`SURVIVED`, meaning the suite passed. If it does not, every `KILLED` below it
would measure a broken checkout rather than a defence, and the run aborts. The
harness is subject to the rule it enforces — it proves it can report both
outcomes before claiming either.

`ERROR` is a third outcome and never a pass. A mutant that cannot be imported
has crashed the harness, not been caught by it; scoring that as a kill would
inflate every rate. This distinction earned its keep immediately: excluding
`evals/` from the mutant tree broke `tests/test_evals.py` at collection inside
every mutant, and the classifier caught it instead of reporting a clean sweep.

### Level 2 — does the auditor catch it?

```bash
.venv/bin/python scripts/run_agent_eval.py --dry-run     # plan and routing
.venv/bin/python scripts/run_agent_eval.py --id L01 --controls 1
```

**Costs real money** — a frontier-model review of a repository, per run. Opt-in,
bounded by `--limit`, never in CI.

Each agent runs from its own definition file, unedited, with the tools that
definition declares. A paraphrased prompt would measure the paraphrase.

Three scores, kept separate because collapsing them hides the failure that
matters:

| Score | Question | Worth |
|---|---|---|
| `flagged` | non-clean verdict | Nearly none alone — an agent that flags everything scores 100% |
| `localised` | names the mutated file | Weak; a lookahead audit names `backtester.py` most times |
| `identified` | a judge, given the `tell` but **never the diff**, says the report describes *this* defect | **The number to quote** |

### The judge has a control too

`identified` is decided by a model, and until it had a control, nothing decided
the model. A judge that says yes to anything plausible produces an excellent
headline rate and means nothing — the same failure as an auditor that flags
everything, one level further out.

So every report is re-scored against a **different** defect's `tell`. The right
answer is always "not identified". This costs no extra agent runs; it re-reads
reports already produced. A non-zero count there does not add noise to the rate
above, it invalidates it, and the runner says so in those words.

## The three things that would corrupt the measurement

**The mutant must not contain the answer key.** `evals/` is excluded from the
copy and a test enforces it. An auditor that could read the corpus would score
perfectly while detecting nothing — future information reaching a decision, the
exact bug class this harness measures. It would be a poor joke to build a
lookahead detector with a lookahead in it.

**The mutant's venv must be its own.** The project venv holds an editable
install pointing at the real `src`, so `.venv/bin/python -m pytest` inside a
*linked* tree imports the unmutated package and reports every mutant clean.
`build_venv` creates a real one per tree.

**The agent must have the tools its definition declares.** The first run of
this harness passed `Read,Grep,Glob` and the auditor said so in its own report:
it could not run the suite, so it could not prove anything. Every auditor brief
here demands proof by execution — "a suspicion you did not test is a
suspicion". Measuring without `Bash` measures a hobbled reviewer and understates
every rate.

## Blind and sighted

`--blind` (the default) removes `docs/ROADMAP.md` and `docs/adr/` from the tree
first. Most of this corpus is narrated there in detail, so a sighted run
measures whether the auditor can **read about** the bug rather than find it.

Both numbers are real and they answer different questions — institutional
memory is a legitimate advantage in a real session. A report must say which
mode produced it.

## What not to conclude

The corpus is small. ADR 0003 already recorded that ten seeds cannot
demonstrate a 5% rate, and the same arithmetic applies here. At this size an
**ordering** between agents or categories is evidence; a **percentage** is an
anecdote. The runner prints that caveat itself below twenty mutants rather than
trusting a reader to remember it.

A defect that survives both levels is the useful output: no test catches it and
no reviewer catches it.

## Adding a defect

1. Find one in `git log` or `docs/ROADMAP.md` — this project documents its own
   bugs unusually well, and that archive is the corpus's supply.
2. Write the `.toml`. The `tell` is the hard part: state what a reviewer must
   *say* to have genuinely found it, not what the code does.
3. `.venv/bin/python -m pytest tests/test_evals.py -q` — the staleness guard
   confirms the anchor lands.
4. `scripts/run_mutation_eval.py --id <id> --skip-control` — see which level it
   probes. A `SURVIVED` here is a finding about the suite, not a broken defect.
