# ARCHITECTURE

## As it is — VERIFIED

```
          scripts/ examples/ ui/runner            (entry points; ui is a Verdict builder)
                      |
   +------------------+------------------+
   |                  |                  |
analytics/         engine/            live/       (analytics: walk-forward, null harness,
   |            backtester              |          multiplicity, tearsheet)
   |            execution           stream/recorder
   |            portfolio           journal/replay
   |                  |                  |
   +---------+--------+---------+--------+
             |                  |
          strategy/           risk/
          (narrow ctx)     (limits, halt, margin)
             |                  |
             +--------+---------+
                      |
                    data/  (feed, cache, vendor, universe, panel, synthetic)
                      |
                    core/  (Decimal money, events, instruments, fx, credentials)
```

Single process, single currency (USD/USDT), file-backed (Parquet + JSONL). No service
boundary anywhere; everything is a library call.

## The charter's §6 target

```
WEB FRONTEND → API → { MARKET INTELLIGENCE | RESEARCH ENGINE | PORTFOLIO ENGINE }
             → { DATA PLATFORM | STRATEGY ENGINE | EXECUTION SIMULATOR }
             → RESEARCH MEMORY → CLAUDE / AGENTS
```

## Gap, item by item

| Charter component | Repository today | Classification |
|---|---|---|
| Web frontend | none | ABSENT |
| API / application layer | none (`fastapi` declared, unused) | ABSENT |
| Market intelligence (§18–§20) | none | ABSENT |
| Research engine | walk-forward, sweep, null harness, multiplicity, gauntlet workflow | **EXISTS**, strong |
| Portfolio engine | ledger + margin + risk | **EXISTS**; 9b open |
| Data platform | Parquet cache, vendor loader, universe, live recorder | **EXISTS**, single-vendor archive |
| Strategy engine | base + context + 2 examples; no composition (§22) | PARTIAL |
| Execution simulator | `SimulatedExecutionEngine`, cost presets, one-bar delay | **EXISTS** |
| Research memory (§41) | `docs/ROADMAP.md` (narrative), `docs/adr/`, `live/journal` | PARTIAL — no §42 record shape, not queryable |
| Claude / agents | 8 agents, 8 commands, 2 workflows, `evals/` | **EXISTS**, measured |

## Where the target itself should change — PROPOSED (charter §124)

1. **The layer boundaries are right; the build order is wrong.** The charter reads
   top-down (frontend first in §30). The repository's evidence says nothing above the
   research engine is justified until one strategy survives out-of-sample — none has
   (SMACrossover: negative in-sample Sharpe on real BTC). Charter §81 (complexity
   penalty) and §105 ("do not automatically build every feature") support deferring
   §30, §31, §23 until that condition is met. Deferral conditions in `ROADMAP.md`.
2. **"Research memory" should be files before it is a database.** The journal, ADRs and
   roadmap are already append-only text with provenance. §42 records can be TOML/JSONL
   in `/harness/experiments/` and remain greppable by agents. A database (§10) is not
   justified by current workload — one user, one process, ~10 MB of market data.
3. **§8 separation is already structurally enforced** by `brokers/base.py` (every
   adapter raises) and the deny-list. Making LIVE a permitted stage should add
   boundaries, not remove those.

## Domain boundaries (§7) — VERIFIED clean where they exist
Ingestion (`data/`) is separate from analysis (`analytics/`), which is separate from
accounting (`engine/portfolio.py`). Strategy logic reaches the engine only through
`StrategyContext`. No financial logic in `ui/`. Boundaries the charter names that do not
yet exist (news, fundamentals, macro, monitors) cannot yet be evaluated — UNKNOWN.
