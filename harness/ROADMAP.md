# ROADMAP (harness view)

**Canonical roadmap: `docs/ROADMAP.md`** — the 50-step lab notebook, with every measured
figure and every retraction. This file maps it onto the charter's §116 priority ladder and
states the **condition that lifts each deferral**. One source of truth (§112).

| Priority | Charter area | Repository steps | State | Next / condition |
|---|---|---|---|---|
| **P0** integrity | §29, §69 | 9b flip-through-zero | **CLOSED 2026-09-02** (ADR 0008, corpus R03) | keep the corpus growing with every new defect |
| **P1** architecture | §5–§8, §96 | steps 1–8, 11–18 | DONE | rewrite `CLAUDE.md` (Phase B); `.claude/rules/` |
| **P2** data foundation | §11–§17, §73–§75 | 19–28, `live/` data plane, **the store** (`store/`, Postgres, ADR 0010, 2026-09-03), **paper supervisor** (`live/supervisor.py`, 2026-09-02: shared ledger, reconcile-on-boot, kill switch, daily loss halt, cycle-on-zero) | DONE offline; **adapters unverified** | Alpaca on VPS; funding feed; §85 dataset ids |
| **P3** backtest correctness | §25–§27 | 29–35, 35a–c | DONE; 36 (ADR) pending | ADR 0007 for the retraction; event-aware (§27) after a calendar of releases exists |
| **P4** portfolio correctness | §28–§29 | ledger + margin; §28 cycles (`live/accounts.py`, 2026-09-02) | done offline | wire cycles into the supervisor; run one for real |
| **P5** research infra | §41–§45, §83–§86 | roadmap + ADRs + `harness/experiments/` (12 §42 records, validator, frozen results) | partial | commit + dataset stamps on *new* runs (backfilled ones say UNKNOWN); strategy versioning §83 |
| **P6** frontend | §30–§34, §76 | `web/ops.py` (operations only) | PARTIAL | **RESULTS PAGES STILL DEFERRED** until one strategy survives OOS **and** 30 days of paper divergence (step 50). The operations page shows no strategy result and a test asserts it — it answers "is the machinery alive", which is a different question and carries none of the risk the deferral guards |
| **P7** strategy engine | §21–§22, §44, §52 | 2 strategies, no composition | partial | composition seam via `on_cross_section`; versioning first |
| **P8** ML | §23–§24 | `ml/` (features, labels, purged splits, baselines) | PARTIAL | **PIPELINE built, MODEL still gated.** The pipeline is the hard part and is useful now; `load_model` refuses anything but a baseline and states the DATA condition that lifts it (2 years, 20 instruments, 2 venues, plus a clean null run). The null harness pointed at the pipeline reports a mean edge of −0.0007 on pure noise against a spread of 0.026, and catches a deliberately introduced leak (E-017) |
| **P9** information intelligence | §18–§20, §47–§48 | `data/fred.py`, `store/macro.py`, `scripts/fetch_macro.py` | PARTIAL | **COLLECTION lifted, USE still deferred.** Ingesting is cheap and harmless; mining a warehouse until something correlates is not. A strategy may read only a series its pre-registered `Trial` named in advance — `store/macro.SeriesGrant`, enforced by the reader with no argument that turns it off. The deferral's actual protection is intact and is now mechanical rather than a rule in a document |
| **P10** Claude integration | §59–§60 | commands/workflows; no tool layer | partial | tool layer only when an API exists (P6/P10 together) |
| **P11** agent harness | §63–§70 | 16 agents, 8 skills, `evals/`, §68 log, §69 hook | strong; 8 roles unmeasured | route a corpus entry to each new role and measure it |
| **P12** creative research | §57–§58 | charter written, 0 entries | ready | first CR entry from the "already equipped" list |
| **P13** advanced | §46, §49–§56 | none | ABSENT | **DEFER**; each needs P9 data |

## The live-data plan, resumed 2026-09-02
Supervisor and cycles are built and tested offline (provable here). Remaining: Alpaca adapter
(provable only on a VPS — this sandbox answers 403 to every market-data host), the daily
review loop with trial accounting, and VPS deployment with the 3am procedure.

## Options (roadmap steps 41–48) — unchanged priority
Crypto options (Deribit) and equity options remain after paper trading proves the loop.
