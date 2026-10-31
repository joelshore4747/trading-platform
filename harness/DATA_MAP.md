# DATA_MAP

Charter §11's four layers, mapped onto what exists. VERIFIED unless marked.

| Layer | What exists | Where | Provenance fields present |
|---|---|---|---|
| **RAW** | `historical.zip` (40 Binance perp symbols, 1m, 845,462 rows); `live/recorder` JSONL journals (what arrived, holes included) | repo root; `data/cache/journal/<market>/<symbol>/<day>.jsonl` | source column in CSV; timestamp; symbol. **No ingestion timestamp** on the vendor file (UNKNOWN when it was fetched). Journals: arrival implied by file, not stamped per line (gap, PROPOSED) |
| **NORMALIZED** | Parquet cache, prices as strings for exact Decimal; `_layout.json` version marker; `_universe.json` | `data/cache/<market>/<symbol>/<interval>.parquet` | market, symbol, interval in path; close-stamped (convention determined from data, roadmap 25) |
| **DERIVED** | Indicators inside `StrategyContext.history()`; `analytics/` metrics, walk-forward, trades, multiplicity; `data/panel` cross-section | in-memory per run | none persisted — a derived figure is recomputed, never stored (reproducible by construction; no feature store, §24 ABSENT) |
| **INTELLIGENCE** | — | — | **ABSENT** |

## Raw is never overwritten — VERIFIED
`write_bars` refuses empty, multi-symbol, duplicate or out-of-order input rather than repairing.
`live/recorder.compact()` refuses two different bars at one timestamp. `historical.zip` is
committed and never modified (git history). Vendor loader interpolates nothing.

## Known data-quality facts about the only dataset — VERIFIED (roadmap 25–26)
- Coverage **46%**, 18 segments/symbol, longest run ~2,900 bars (~2 days).
- Missing time is **not random**: 07–10 UTC 56% covered vs 18–23 UTC 25%.
- Open-stamped; shifted to close on load (a one-bar lookahead if not).
- TONUSDT is a placeholder (1 price, 0 volume) — screened out.
- Symbols share no timestamp grid (20,869–21,160 bars); intersection costs 1.3%.
- No funding series ships with it; carry is unmodelled on every perp result.

## §73 data-quality checks — which exist
| Check | Status |
|---|---|
| duplicates, ordering | VERIFIED (`write_bars`, `_validate_ordering`) |
| future timestamps | VERIFIED (`binance.py` drops forming bar; `Bar` rejects nothing about future — UNKNOWN whether a future-stamped bar is refused at load) |
| gaps | VERIFIED counted (`live/stream`, `vendor.coverage`), never filled |
| zero/negative values | VERIFIED (`Bar.__post_init__`) |
| stalled feed (flat price + zero volume) | VERIFIED (`vendor.screen`) |
| provider disagreement (§75) | ABSENT — single provider |
| schema changes | ABSENT |

## Storage strategy (§10) — the revisit condition FIRED, 2026-09-03
The rule was: Parquet + JSONL is sufficient; **do not introduce a database** until a workload
demands it; revisit when >1 concurrent writer, or queries across >1 GB, or a frontend needs
sub-second reads. The approved plan of 2026-09-03 asks for many redundant feeds writing at
once, macro series joined against prices, and an operations page — **all three at once**. The
condition fired as designed rather than being argued around, and Postgres was added beside
Parquet, not instead of it (ADR 0010).

| | Parquet `data/cache/` | Postgres `store/` |
|---|---|---|
| Job | the archive, and the backtester's input | the live and queryable layer |
| Needs a server | no | yes |
| Read by | every backtest | the reconciler, the page, research queries |

**A backtest must not depend on a server being up**, so nothing in `engine/` or `analytics/`
imports `store/`, and `psycopg` is an optional dependency. The two are compared bar for bar by
`scripts/store_load_cache.py --verify-only`: IDENTICAL across 824,304 bars on 2026-09-03.

## Missingness — VERIFIED 2026-09-03 (E-013 / CR-001)
The archive's holes are **one collector's outage pattern**: 98.8% of missing minutes are
missing from all 39 symbols at once. Gap-edge bars are no more active than hour-matched
interior bars (38/39 symbols inside the null band on both |return| and volume), and
low-coverage days carry no more realised variance than high-coverage ones (median Spearman
−0.026, inside the shuffle band). Missingness is MCAR *conditional on hour of day*. Two
consequences: the per-hour coverage measured on the BTC file generalises to the whole
archive, and any **unconditional** intraday figure must be reweighted by 1/coverage(hour)
before it is read; per-hour figures need nothing. Volatility, fill and cost figures on this
archive are not biased low by the holes.
