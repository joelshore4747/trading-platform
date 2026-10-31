# FINAL_AUDIT

Audit of `joelshore4747/trading_platform` at `531f24e`, 2026-09-01, against the Trading
Intelligence Platform charter v1.0; scores refreshed 2026-09-02 at `204a548` where the
evidence changed. Every score carries its evidence. Classification key: §2.

## The premise, corrected — VERIFIED
The charter assumed a hastily built codebase without a harness. **The opposite is true.** No
Codex; 63/64 commits Claude-co-authored under a human owner; the harness is the most rigorous
part of the repository and is itself measured (ADR 0006). The audit's job was therefore not
to find rigour missing but to map an honest, narrow engine against a very wide charter.

## Scorecard (§114) — 1–10, evidence beside each

| Dimension | Score | Evidence |
|---|---:|---|
| Architecture | 8 | downward-only deps verified; clean domain boundaries where they exist; single process by design |
| Code quality | 8 | mypy strict, ruff, ~1:1 tests, no god objects, no global state; debt is in versioning, not code |
| Frontend | 0 | ABSENT |
| UX | 0 | ABSENT |
| API | 0 | ABSENT (the unused `fastapi` extra removed 2026-09-02) |
| Data architecture | 6 | RAW/NORMALIZED honest and provenance-aware; DERIVED not persisted; INTELLIGENCE absent; no dataset versioning |
| Market data | 6 | one hand-supplied archive, 46% coverage; parsers strong. **A real response agreed on 2026-09-08**, which is the condition this row set for raising the score — Binance klines and funding verified field-for-field against the wire, including the close-stamped timestamp and the derived 8h funding interval. Raised 5→6 and no further: the subject of the original row, Alpaca, is still unverified, as are Polygon and FRED, and all three now wait on credentials rather than on the network |
| Backtesting | 9 | one-bar delay structural; 17 guards run first in CI; typed OOS split; null harness measured; multiplicity; verdict layer |
| Strategy engine | 5 | contract is excellent and narrow; two strategies, none validated; versioning by source-plus-parameter fingerprint since 2026-09-03; no composition |
| ML | 0 | ABSENT, deliberately |
| Portfolio | 8 | ledger + margin correct; 9b closed (ADR 0008); §28 cycles with frozen history; one shared ledger between engine and supervisor; `no_reference_price` now reaches `on_reject` |
| Security | 7 | no live path, env-only redacted secrets, deny-list, secret hook, §69 defaults hook; kill switch and single-writer lock in paper; venue-side controls ABSENT (none needed yet) |
| Testing | 9 | **1,328 tests** (917 when this was written; 1,307 before 2026-09-08), 22-defect mutation corpus in CI (26/28 killed, 2 declared survivors), thirteen fake guards found and fixed, one still declared; the adversarial review showed eight green tests hiding eleven defects, and the gauntlet showed a fix's own regression guard was single-symbol. On 2026-09-08 the first run on a machine other than the authoring sandbox found **two more tests that had never executed anywhere else** — one of them the guard on the train/test split — which is the sharpest available statement of why the number is not the defence |
| Observability | 4 | `StreamStats`, `stale_marks`, rejection rate, journal, §68 agent log (2026-09-02); no structured logging or metrics |
| Research | 8 | walk-forward, sweep, null harness, deflated Sharpe, gauntlet; 9 ADRs (one PROPOSED); 15 §42 records with frozen results, 3 of them run end-to-end from pre-registration on 2026-09-03 |
| Claude integration | 6 | 8 commands, 2 workflows, `/harness`; no tool layer (needs an API) |
| Agent harness | 9 | 8 agents measured 8/8; controls; judge control; of the eight §63 roles added 2026-09-02, `adversarial-reviewer` has one run (refuted the supervisor, 11 findings) and seven are unmeasured |
| Extensibility | 7 | `Broker`/`DataFeed`/`Strategy` protocols; context extension sanctioned (order observer, seeded portfolio added 2026-09-02); no plugin system |
| Performance | 6 | measured where it mattered (O(1) mark, 4.6 µs/bar, 8 B/cell); live throughput unmeasured |
| Reliability | 5 | deterministic engine; supervisor reconciles on boot, locks the account, ends a failed cycle by exception; **nothing has run continuously anywhere** |
| Research integrity | 10 | every result caveated; retractions recorded; "failed to falsify" is the ceiling claim; corpus of own defects |
| Creative research | 5 | three pre-registered entries written and all three run within a day (2026-09-03): two REJECTED cleanly (gap bias, BTC lead/lag), one PROMISING on synthetic data with a proposed ADR; every result deflated, none called "works" |

**Classification: EARLY PRODUCTION for the backtesting/research core; PROTOTYPE (or ABSENT)
for everything the charter adds above it.**

## §115

**KEEP.** The one-bar delay and its 17 guards. `Decimal` money. Validation at construction.
The narrow `StrategyContext`. The typed train/test split. The null harness and its 1,000-bar
finding. Deflated Sharpe. The verdict layer that says "failed to falsify" and never "works".
`evals/` — the corpus of the repo's own defects, in CI. The roadmap-as-lab-notebook that
keeps retractions. Every broker raising. No pandas/numpy in the engine.

**FIX.** §83–§86 versioning and commit stamps on results (still open). *Done 2026-09-02:*
9b (ADR 0008); §69 defaults hook; `run_walk_forward.py` docstring; the dead `ui` extra.

**REFACTOR.** Nothing structurally. `ui/` rename when a UI exists. Strategy composition seam
(§22) when a second real strategy exists to compose.

**REMOVE.** Nothing further — the repository is already minimal; the charter's §81 penalty
argues against adding, not removing. (`fastapi`/`uvicorn` removed 2026-09-02.)

**BUILD (in order).** *Done 2026-09-02, all offline:* §28 cycles; supervisor with
reconcile-on-boot; Alpaca adapter (unverified on the wire); daily review loop with trial
accounting; §42 records; §63 roles; §68 log; §65 skills; strategy fingerprints. **Next:** first
contact with Alpaca on the VPS per `docs/RUNBOOK.md`; 30 days of paper with the daily review;
measure the six still-unmeasured agents through `evals/`; a FUNDING journal event so perps can
be funded on paper; a human decision on ADR 0009.

**INVESTIGATE.** Live throughput and `fsync` cost at 40 symbols. The pre-mortem lens, lost to
the session limit in both gauntlet runs (`wf_4993d909-339`, `wf_39cbd125-53b`). *Closed
2026-09-03:* the plateau contradiction (E-015 → ADR 0009 proposed); the coverage bias is not
activity-conditioned (E-013), so per-hour figures stand and unconditional ones reweight.

**DO NOT CHANGE.** The fill delay. Decimal. The refusal pattern (`fx`, `universe`, `calendar`,
`cagr`): refuse rather than assume. The corpus rule that only real defects enter `evals/`.
`ZERO_COST` existing only for engine isolation.

**CREATIVE OPPORTUNITIES.** The three in `CREATIVE_RESEARCH.md` have been run: gap bias
REJECTED, BTC→alt one-minute lead REJECTED, plateau-aware stability PROMISING on synthetic
data. The next entries need data the repository does not hold (funding, tick, news).

**TECHNOLOGY.** Everything stays (`TECHNOLOGY_REVIEW.md`). Add nothing until a workload
demands it; a VPS `systemd` unit is the next infrastructure, not a database or container.

**ARCHITECTURE.** The charter's §6 layers are right; its build order is inverted for this
repository. Build up from the research engine as strategies earn it; defer P6/P8/P9/P13 with
the conditions in `ROADMAP.md`. Make LIVE a permitted stage (§8) by adding boundaries, not by
removing the ones that make every adapter raise today.

**STRATEGIES.** Trustworthy: none, and the repository says so. Questionable: both, honestly
measured — SMACrossover rejected on real data (in-sample Sharpe −2.52); CrossSectionalMomentum
synthetic-only.

**DATA.** Trusted: the Parquet cache's contents *as a record of what the archive contained*,
with its holes. Not trusted for: time-of-day effects (coverage bias), carry (no funding),
anything after 2026-08-21. Nothing live has been touched.

**CLAUDE.** Should be allowed to: inspect, backtest, compare, run the gauntlet, propose
changes, write experiment records, run paper. Should not, without explicit per-action review:
change `CostModel`/`RiskLimits` defaults, relax a limit, touch the event loop, enable any
`is_live=True`. Enforce the first group by the §69 hook rather than by prose.

**AGENTS.** Opus for auditors and financial correctness; Sonnet for checklists and mechanical
work; Haiku for validation; Fable for the new adversarial-coordinator, market-researcher and
creative roles. Every one measured through `evals/` before its verdicts are trusted.

## What this audit could not do
Verify any venue connection (sandbox 403s). Measure anything about a running system (none
has run). Score the charter's frontend/ML/intelligence layers as anything but ABSENT. State a
false-discovery rate for the auditors on *novel* defects — the corpus is their own history.
