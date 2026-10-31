# AI workflow: orchestration, model tiering, and token economics

This document covers how to get good work out of Claude Code on this project
without burning tokens re-deriving things it already established.

---

## 1. The mechanic that determines everything: prompt caching

Claude Code caches the conversation prefix. A cache read costs roughly **10% of
a fresh input token**. That single fact drives every recommendation below.

The cache is a **prefix match**: it survives only while the beginning of the
conversation stays byte-identical. Three things invalidate it completely:

| Action | Effect on cache |
|---|---|
| Switching the model mid-session | **Total invalidation** — caches are per-model |
| Editing `CLAUDE.md` mid-session | Invalidates from that point forward |
| Adding or removing tools/MCP servers | **Total invalidation** — tools render first |

### Why this kills the obvious version of "cheap workers"

The intuitive plan — *run the main loop on a frontier model, switch to a cheap
model for grunt work, switch back* — **costs more than not doing it.** Each
switch throws away the cached prefix, so the next turn re-pays full price for
the entire conversation history. On a long session that history is the dominant
cost, and it dwarfs whatever you saved on the cheap turns.

### What works instead: subagents

A subagent runs in **its own context window** with its own model. The main
session's cache is never touched. The parent pays only for the subagent's short
final report — not for everything the subagent read to produce it.

```
Main session (Opus/Fable)  ← cache stays warm, never invalidated
  │
  ├─→ data-validator   (Haiku)   reads 50k tokens of CSV → returns 300 tokens
  ├─→ test-writer      (Sonnet)  reads 20k of source     → returns 500 tokens
  └─→ lookahead-auditor (Opus)   reads 30k of engine     → returns 800 tokens
```

**This is the whole trick.** A subagent is a token firewall: expensive reading
happens in a context you throw away, and only the conclusion comes back.

---

## 2. Model tiering

Current per-million-token pricing:

| Model | Input | Output | Role here |
|---|---|---|---|
| Claude Fable 5 | $10 | $50 | Hardest long-horizon work only |
| Claude Opus 5 | $5 | $25 | **Default orchestrator** |
| Claude Sonnet 5 | $3 | $15 | Most subagent work |
| Claude Haiku 4.5 | $1 | $5 | Mechanical checks |

### The roster, and why each sits where it does

| Agent | Model | Why this tier |
|---|---|---|
| `lookahead-auditor` | **Opus** | The one review where a false negative is catastrophic. A missed leak does not crash — it produces a beautiful, fictional equity curve you then trade. Not worth saving $4/M on. |
| `risk-auditor` | **Opus** | Reasoning about whether a limit is *reachable* across call paths is genuinely hard. Cheap models confirm limits exist without checking they fire. |
| `quant-researcher` | **Opus** | Judging whether a claimed edge has a real mechanism requires domain reasoning and healthy scepticism. |
| `strategy-reviewer` | Sonnet | Checklist-driven with some judgement. Sonnet 5 is near-Opus on this shape of task at 60% of the price. |
| `test-writer` | Sonnet | Well-specified mechanical work with clear conventions. |
| `data-validator` | **Haiku** | Pure pattern-matching: sorted? duplicated? OHLC consistent? Spending frontier tokens here is pure waste. |

**On Fable as orchestrator:** Fable 5 is the most capable model available, and
it is the right choice for genuinely hard long-horizon work — a large migration,
a from-scratch options pricing layer. But it costs 2× Opus 5 on both input and
output, and the orchestrator's context is the *longest-lived* thing in the
session. Default to **Opus 5** and escalate to Fable deliberately, per task,
rather than sitting in it. If you do run Fable, lean harder on subagents — the
delegation savings scale with the orchestrator's price.

### Effort as the second dial

Independent of model choice, `effort` controls how much thinking happens.
`low` and `medium` are unusually strong on the current generation. Reach for
`effort` before reaching for a bigger model — it is often the cheaper win.

---

## 3. Persistent knowledge: stop re-explaining the project

Every session that re-derives the architecture is money set on fire. Three
layers hold knowledge so that never happens:

| Layer | Loaded | Holds |
|---|---|---|
| `CLAUDE.md` | Every session, automatically | House rules, prime directive, layout, commands |
| `.claude/skills/*/SKILL.md` | On demand, when relevant | Procedures — reviewing a backtest, adding a feed |
| `.claude/commands/*.md` | When you type `/name` | The deep-question prompts you would otherwise retype |

The split matters. `CLAUDE.md` is paid for on **every** turn, so it stays short
and covers only what is always true. Skills are paid for **only when triggered**,
so detailed procedures live there. Putting a 400-line backtest-review checklist
in `CLAUDE.md` would tax every unrelated conversation.

**Rule: when you correct Claude on something structural twice, that correction
belongs in `CLAUDE.md`.** Once it is there, you never pay for that correction
again.

### Start sessions with `/session-brief`

It loads git state and test status in a bounded, predictable number of tokens,
then stops. Without it, a session typically opens by reading half the codebase
to reconstruct what it could have learned from `git log -8`.

---

## 3b. The layer above: measuring the harness itself

Everything in section 3 is prose, and so is every agent in section 2. Prose is
not a control. The one part of this layer that was ever measured — the prompt
guard's corpus — found four bugs in rules that read perfectly well, which is
the argument for measuring the rest.

`evals/` holds a corpus of defects that **actually happened in this
repository**, each reintroducible into a throwaway copy of the tree, and runs
two questions over it:

| Level | Question | Cost | Where |
|---|---|---|---|
| Mutation | does the **test suite** catch it? | free | CI, every push |
| Agent | does the **auditor** catch it? | real money per run | opt-in, `/harness` |

The second is the one nobody had asked. `lookahead-auditor` runs on a frontier
model precisely because a false negative there is catastrophic, and until this
existed its false-negative rate was unknown.

Three practical consequences for how you work:

- **Delegation is still the trick** (section 1), but a subagent's report is
  evidence only to the extent the subagent can be checked. Route a defect class
  to the agent the corpus routes it to; if no defect in `evals/defects/` covers
  a class, no agent has been shown to catch it.
- **Give an auditor the tools its definition declares.** The first run of the
  agent eval withheld `Bash` and measured a reviewer that could not run a test
  — every auditor brief here demands proof by execution, so the number was of a
  hobbled agent. It said so in its own report.
- **`--blind` versus `--sighted` is a real distinction.** `docs/ROADMAP.md`
  narrates most of this corpus. An auditor that can read it may be finding the
  bug or reading about it, and those are different capabilities. Both are worth
  having; only one is worth calling detection.

## 4. The working loop

```
/session-brief                  cheap orientation
      ↓
/deep-dive or /challenge        think before building
      ↓
/new-strategy <idea>            hypothesis → gates → implementation
      ↓
lookahead-auditor  ─┐
strategy-reviewer  ─┼─ run in parallel, one message, three tool calls
/cost-check        ─┘
      ↓
/pre-mortem                     assume it failed; find out why
```

Launch independent subagents **in a single message** so they run concurrently
rather than serially. Three sequential audits take three times as long for the
same tokens.

Two of those loops are now scripts rather than instructions, in
`.claude/workflows/`:

- **`gauntlet`** — the loop above, executable. Five audit lenses in parallel,
  every finding handed to an independent skeptic told to refute it, then a
  pre-mortem over whatever survived. It pipelines rather than barriers, so a
  lookahead finding is being attacked while the overfitting audit is still
  reading.
- **`guard-sweep`** — `guard-auditor` over the test suite, each suspicion
  proved by mutating the covered code in a scratch copy and showing the test
  stayed green. A suspicion nobody tested is a suspicion.

Both cost real money and neither runs itself. The verification stage is the
point: an audit that is never contradicted is an audit nobody checked.

---

## 5. Plugins

### Already enabled on this account — use these

| Plugin | Use here |
|---|---|
| **lseg** | Market data. The most directly relevant thing you have — reference prices, instrument metadata, cross-checking a vendor file against an authoritative source. |
| **daloopa** | Fundamentals. Relevant when equity options work starts (Phase 4); not for crypto. |
| **engineering** | `/engineering:review` for PRs, `/engineering:architecture` for ADRs, `tech-debt` skill for periodic audits. |
| **data** | `statistical-analysis` for return distributions and outlier detection; `explore-data` for profiling a new price series. |
| **finance** | Accounting-oriented (close, reconciliation, SOX). Mostly not applicable to systematic trading — do not force it. |

### Worth adding

| Plugin | Why |
|---|---|
| **exa** or **tavily** | Web research for the `quant-researcher` agent. Strategy literature and venue documentation change; training data goes stale. Pick one, not both. |
| **signoz** | Observability. Only once something runs continuously — premature today. |

### Not worth adding

**stackhawk-hawkscan** scans web applications; this is a library with no HTTP
surface. **cockroachdb** and **miro** have no role here.

### On MCP servers generally

Every connected MCP server's tool definitions render into the prompt prefix on
**every request**, and adding one mid-session invalidates the whole cache.
Connect what you use; disconnect what you do not. An exchange-data MCP server is
worth its weight; five you never call are a standing tax.

---

## 6. Practical token discipline

**Do**
- Delegate anything that requires reading a lot to return a little.
- Batch independent subagent calls into one message.
- Put durable corrections in `CLAUDE.md`; put procedures in skills.
- Use `/session-brief` instead of ad-hoc exploration.
- Prefer `effort: low/medium` for routine work.

**Don't**
- Switch the main-loop model mid-session to save money. It does the opposite.
- Edit `CLAUDE.md` mid-session unless you accept the cache cost.
- Ask for a summary of code you can point at with a file path.
- Run an Opus subagent for a task with a fixed checklist and no judgement.
