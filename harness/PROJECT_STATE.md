# PROJECT_STATE

Audit date: 2026-09-01, refreshed 2026-09-04 at `949c6dc` (branch `claude/trading-platform-bih2rm`).
Classification key: **VERIFIED** / **INFERRED** / **UNKNOWN** / **PROPOSED** (charter §2).

## What this repository is — VERIFIED

A Python backtesting and paper-trading research engine for systematic crypto strategies
(spot, perpetuals; options scaffolded but unbuilt). Built to falsify strategies.

| Fact | Value | Evidence |
|---|---|---|
| Source | **20,138 lines** under `src/trading_platform/` (2026-09-04; `store/`, `collect/`, `tickets/`, `web/`, `ml/`, `strategy/patterns/` are new) | `wc -l src/trading_platform/*/*.py` |
| Tests | **1,328 collected, all green** with a live PostgreSQL (2026-09-08; 1,307 before this session, 1,216 recorded here on 2026-09-04, 940 before the nine workstreams) | `pytest --co -q`; full run 2026-09-08 |
| Types | `mypy --strict` clean, 112 files incl. scripts | `.venv/bin/mypy` |
| Lint | ruff clean | `.venv/bin/ruff check .` |
| Runtime deps | **`pyarrow` only**; `psycopg` is an optional `store` extra, imported nowhere in `engine/`, `analytics/` or `strategy/` | `pyproject.toml`; import-graph probe |
| Commits | 99, 2026-08-05 → 2026-09-04; all Claude-co-authored under a human owner | `git log --format='%an'` |
| Roadmap | 37 ✅ · 1 ◐ of 50 numbered steps; 9b closed 2026-09-02 | `docs/ROADMAP.md` markers |
| ADRs | 11: ten Accepted, 0009 PROPOSED (plateau-aware stability, awaiting a human) | `docs/adr/` |

## The nine workstreams, phase 1 — 2026-09-04

Built in one session, each phase 1 finished before the next began, each attacked
by an adversarial reviewer afterwards.

| | what | state |
|---|---|---|
| W1 | Postgres spine — migrations, provenance, archive round-trip | 824,304 bars round-trip IDENTICAL |
| W2 | Multi-source capture and reconciler | offline VERIFIED; **UNVERIFIED on any wire** |
| W3 | Account tracker, risk rejections, leverage acknowledgement | VERIFIED against a live database |
| W4 | The harness files its own findings as tickets | VERIFIED — an auditor filed two P1s in it |
| W5 | Operations page (no strategy results, by design) | VERIFIED on live data |
| W6 | ICT primitives and one strategy | strategy **REJECTED** (E-016) |
| W7 | Document library | VERIFIED |
| W8 | Macro by vintage, readable only when pre-registered | **UNVERIFIED on the wire** |
| W9 | ML pipeline, model gated | null run clean (E-017) |

**Three adversarial reviews, three REFUTED verdicts, twelve defects fixed** — the
most serious being a store that silently merged contradicting writers (299 of 300
races), a collector that wrote zero rows run exactly as documented, a transport
that handed the API key to a redirect's origin, and an account tracker that
crashed the session it observed on 2.6% of ordinary marks. All are in the
mutation corpus.

Mutation eval **26/28 killed, 2 declared survivors (G01, R04), 0 undeclared holes**,
re-measured 2026-09-08 against a live database — 22/23 when this paragraph was written. The
run also demonstrated the value of its own control step: a guard added the same day aborted
it, and an aborted eval measures nothing (T-8e47cfd7). The eval
reports NOT MEASURED rather than SURVIVED when a killer suite skips — it had been
reporting four skipped store defects as holes in the suite.

## The charter's premise, checked — VERIFIED

Charter §0: "developed without a sufficiently rigorous … harness … Codex has been making
changes rapidly." **Neither holds.** No commit is Codex-attributed. The harness comprises
8 specialist agents (16 since Phase C, 2026-09-02), 8 commands, 2 workflows, 2 skills (8
since Phase C), 6 hooks, CI, `evals/` — a mutation-testing corpus of 11 real defects and a
measured agent evaluation — plus, since Phase C, §42 experiment records and a §68 agent log. Details in
`EVALUATION_PROTOCOL.md` and `AGENT_PROTOCOL.md`.

## What does not exist — REVISED 2026-09-08

This paragraph was written on 2026-09-01 and listed database, migrations, Docker and ML as
VERIFIED ABSENT. All four shipped in the nine workstreams recorded immediately above it, in
the same document, three days later. The audit was not re-run and the line stood.

**Still genuinely absent**, re-checked 2026-09-08: an API server (`grep -rn
"FastAPI\|APIRouter" src/` → none; the `fastapi`/`uvicorn` extra was deleted 2026-09-02), a
Dockerfile or any application image (`find . -iname 'Dockerfile*'` → none; the only
`docker-compose.yml` service is a development Postgres), Alembic (the migration runner is
`store/migrate.py`, hand-written), workers, queues, authentication, and a frontend
application (`find . -name package.json -o -name '*.tsx'` → none, and correct only since the
vestigial WebStorm scaffold was deleted this session).

**Present, contrary to the line above**: a database (Postgres, ADR 0010), migrations (seven,
in `migrations/`), Docker Compose (the store), and ML (`src/trading_platform/ml/`, pipeline
built and model gated).

**Two rendered HTML pages**, not none: `web/ops.py` served by `scripts/run_ops.py`, and
`analytics/tearsheet.py`. Neither is an application; see `harness/FRONTEND_MAP.md`.
`src/trading_platform/ui/` remains a run executor producing a `Verdict` dataclass, not a user
interface (`ui/runner.py:1-8`) — ticket `T-4e781b8c` records that the name misleads.

## This session's headline — VERIFIED

Every perpetual figure the repository had ever produced was **retracted** on 2026-08-27
(roadmap 35a): a derivative fill debited full notional from cash while being marked back as
unrealised PnL only. Fixed 2026-09-01 with a real margin model; the re-run moved from 17/18
halted folds to 0/18 and from 1 out-of-sample fill to 70. Full record: `KNOWN_FAILURES.md`.

## 2026-09-02 — the paper-trading stack, built and refuted once — VERIFIED

In one night: 9b closed (ADR 0008, corpus R03); §28 account cycles; the paper supervisor
(shared seeded ledger, reconcile-on-boot, kill switch, daily loss halt, cycle-on-zero);
the Alpaca parser, client and poller (**unverified on the wire**); the daily review loop;
§63 agent roles, §65 skills, §42 records, §68 log. The harness earned its keep: the
`risk-auditor` found a critical cross-section double count on the 9b fix, the
`guard-auditor` four fake guards, and the `adversarial-reviewer` **refuted** the first
supervisor with eleven run-demonstrated findings that eight green tests had not caught. All
fixed the same day, each with a test. Then the gauntlet showed the first cross-section fix was
itself bypassable across symbols; fixed with per-order release. Mutation eval: 13/14 killed,
G01 declared. The creative lab's three entries were all run on 2026-09-03: two rejected, one
promising on synthetic data (ADR 0009 proposed).
**Nothing has run continuously anywhere; no venue has been contacted.**

## Environment constraint — SUPERSEDED 2026-09-08

**As written:** "This sandbox's network policy answers `403 to CONNECT` for every
market-data host (`api.binance.com`, `query1.finance.yahoo.com`, `api.polygon.io`; proxy log
2026-09-01). No venue adapter can be verified here."

That was true of the container this repository was written in, and it was recorded as though
it were a property of the project. Measured on the owner's machine 2026-09-08:
`api.binance.com` **200**, `fapi.binance.com` **200**, `data.alpaca.markets` **401**,
`api.polygon.io` **401**, `query1.finance.yahoo.com` **429**. 401 and 429 are answers.

**First contact happened the same day.** `scripts/fetch_binance.py`, which carried a warning
that it had never been run against the live API, fetched 10 daily BTCUSDT klines and 12
funding settlements; every OHLCV field and every close-stamped timestamp equals the wire as
`Decimal` with the exponent preserved, and the 8h funding interval the script derives is
what the wire shows. `data/binance.py` is now VERIFIED on the wire.

Alpaca, Polygon and FRED remain unverified and are waiting on **credentials, not on the
network**. A VPS is still what *continuous* running needs — roadmap step 50's 30-day
divergence count — but it is no longer what first contact needs. See
`harness/EXTERNAL_DATA_MAP.md`.

## Governance decisions taken 2026-09-01

1. Charter governs; `CLAUDE.md` **rewritten** (131 lines) preserving falsification-first via charter §81/§103/§121 — ADR 0007.
2. Ledger stays USD; GBP is a display conversion through `core/fx.py`.
3. `/harness/` holds new audit files; `docs/ROADMAP.md` and `docs/adr/` remain canonical.
