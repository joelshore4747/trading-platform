# AGENT_PROTOCOL

## Current roster — VERIFIED (`.claude/agents/`)
| Agent | Model | Charter §63 role | Measured |
|---|---|---|---|
| `lookahead-auditor` | opus | ADVERSARIAL REVIEWER (lookahead lens) | 5/5 identified; clean on control |
| `risk-auditor` | opus | ADVERSARIAL REVIEWER (risk lens) | 2/2; found 9b on control; on the 9b fix (2026-09-02) found 4 gaps, 1 critical (cross-section double count), all fixed same day; agent eval 2026-09-03 on R04/L04 BLIND: **R04 identified**; **L04 NOT identified** on rerun (flag NO, $3.93) — a real miss: the restored-position-unpriced defect is outside the brief's eight checks; the control FAILED (no data) both times ($8.02 + $9.40) |
| `guard-auditor` | opus | ADVERSARIAL REVIEWER (test-integrity lens) | 1/1 after confound fix; found A01 on control; on Phase C/9b tests (2026-09-02) 22 mutations, 4 fake guards found and fixed; on the paper-stack tests (2026-09-03, **run on fable** because opus returned 529 twice) 9 mutants, 3 fake guards + 1 unasserted field found and fixed |
| `overfitting-auditor` | sonnet | QUANT RESEARCHER (evaluation) | 1/1 |
| `strategy-reviewer` | sonnet | QUANT RESEARCHER (logic) | routed, not yet mutant-tested |
| `quant-researcher` | opus | QUANT RESEARCHER / MARKET RESEARCHER (no web plugin yet — roadmap 39) | — |
| `test-writer` | sonnet | MECHANICAL ENGINEER | — |
| `data-validator` | haiku | MECHANICAL ENGINEER | caught TONUSDT; also mis-called stamping once |

## §63 roles added 2026-09-02 (Phase C) — VERIFIED on disk, **UNMEASURED**
| Agent | Model | Charter §63 role | Measured |
|---|---|---|---|
| `architect` | opus | ARCHITECT | not yet — no corpus entry routes here |
| `code-investigator` | opus | CODE INVESTIGATOR | not yet |
| `market-researcher` | fable | MARKET RESEARCHER | not yet; needs web access (sandbox: 403) |
| `adversarial-reviewer` | fable | ADVERSARIAL REVIEWER (coordinates the four auditors) | **first run 2026-09-02**: REFUTED the paper supervisor with 11 run-demonstrated findings (4 P1) that a green suite of 8 tests had not caught; all fixed same day |
| `creative-researcher` | fable | CREATIVE RESEARCHER | not yet; output is a CR entry, never code |
| `implementer` | opus | IMPLEMENTER | not yet |
| `mechanical-engineer` | sonnet | MECHANICAL ENGINEER (absorbs `test-writer`/`data-validator` routing) | not yet |
| `final-reviewer` | opus | FINAL REVIEWER | not yet |

Every new brief carries §69's prohibitions, the §2 classification rule, the §119 finding
shape, and ends by writing a §68 record with `scripts/agent_log.py`. All sixteen are in
`evals/corpus.py::KNOWN_AGENTS`; `tests/test_evals.py` guards both directions (a brief
with no registry entry, a registry entry with no brief) and that every declared model is
one of opus / sonnet / haiku / fable. A subagent cannot spawn subagents, so the
adversarial-reviewer *consumes* the auditors' reports handed to it rather than invoking
them; the `gauntlet` workflow is where the coordination actually runs.

## Skills (§65) — VERIFIED on disk
`backtest-review`, `adding-a-data-source` (reshaped to §65 headers) · `strategy-autopsy` (§51)
· `experiment-design` (§42, backed by `scripts/experiment.py`) · `data-quality-audit` (§73) ·
`portfolio-integrity` (§29) · `agent-evaluation` (§70, wraps `evals/`) · `source-validation`.
Eight skills; §66 says do not create thousands — each of these names a workflow already
repeated in the roadmap.

## §64 model selection — as applied
Opus where a false negative is catastrophic or reasoning is deep; Sonnet for checklist
work; Haiku for pattern-matching; Fable (new) for long-horizon, adversarial and creative
work. `docs/AI_WORKFLOW.md` §2 records the reasoning and the cache economics (a subagent is
a token firewall; switching the main model invalidates the cache).

## §68 action logging — EXISTS since 2026-09-02
`scripts/agent_log.py` appends one JSONL record per agent action to
`harness/agent-log/<utc-date>.jsonl`, all twelve §68 fields in charter order. Append-only;
agent name checked against the briefs on disk; tests in `tests/test_agent_log.py`. Every
brief — the eight originals since 2026-09-02 as well — ends by calling it. The workflows
(`gauntlet`, `guard-sweep`) do not yet write a record of their own; each agent they spawn
does, so a workflow run is visible as its agents' lines.

## §69 safety — enforcement status
Structurally enforced: no live path, no credential reads, no force-push/`rm -rf`/`sudo`, no
`ccxt`. Hook-enforced since 2026-09-01: fee/capital/slippage/risk defaults (`check-defaults.sh`, cautions).
Prose only: risk-limit relaxation by `None` outside tests, deleting experiments — see `.claude/rules/safety.md`.

## §70 agent evaluation — EXISTS
`evals/` is this. Benchmarks: the 11-defect corpus. Compared: two runs (hobbled vs full
tools; noisy vs clean tree) — the difference was the harness's own defect both times.
Recorded in ADR 0006. Extend the corpus, never invent entries.

## Gauntlet run 2026-09-03 on the paper-trading stack (wf_39cbd125-53b)
23 agents; 13 completed, 10 lost to the session limit (verify phase mostly, guards lens, pre-mortem).
Four lenses independently converged on the same critical defect — the blanket `release_pending`
that fixed the cross-section double count was itself bypassable across symbols — reproduced on
shipped defaults and confirmed by the verifiers that ran. Two medium findings (unjournalled
funding; stops not cancelling working orders) confirmed. Four low findings refuted on verification
(poller clock skew, daily-bar stamping, hard-coded expected interval, review replay gating). All
confirmed findings fixed the same day; the pre-mortem is still owed (resume the run id).
**Lesson recorded**: a fix to a per-instant invariant was enforced per event, and its regression
guard used one symbol — the same shape as R1's original defect.
