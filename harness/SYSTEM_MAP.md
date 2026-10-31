# SYSTEM_MAP

All entries VERIFIED from source unless marked. Line counts from `wc -l`, 2026-09-01.

## Dependency direction — VERIFIED downward, no cycles

Measured by `grep -rhoE "^from trading_platform\.([a-z]+)"` per package:

```
core      -> (nothing)
data      -> core
strategy  -> core
risk      -> core
brokers   -> core
live      -> core, data
engine    -> core, data, risk, strategy
analytics -> core, data, engine, strategy
ui        -> analytics, core, data, engine, risk, strategy
```

`core` imports nothing; nothing imports `ui`. Matches `CLAUDE.md` "dependencies point
downward only". Package-level only — intra-package cycles not checked (**UNKNOWN**).

## Packages

### `core/` — 732 lines. Types, instruments, events, FX, credentials.
| Module | Responsibility | Consumers | Tests |
|---|---|---|---|
| `types.py` (98) | `Side`, `OrderType`, `AssetClass`, enums; `quantize_cash` | everything | via consumers |
| `events.py` (164) | `Bar`, `Order`, `Fill` with `__post_init__` validation; `Fill.cash_impact` (derivative-aware since 2026-09-01) | engine, data, live | `test_engine`, `test_portfolio` |
| `instruments.py` (239) | `Instrument` hierarchy; `initial_margin_rate`, `margin_requirement`, `leverage` | engine, risk, strategy | `test_portfolio`, `test_risk*` |
| `fx.py` (203) | Rate lookup that **refuses** unknown asset / pre-series / stale; `assume_pegged()` | analytics | `test_fx` (ADR 0002) |
| `credentials.py` (127) | Env-only secrets, redacted repr, fail-loud `require()` | brokers, connectors | `test_connectors` |
| State: none. Failure mode: `ValueError` at construction — by design. |

### `data/` — 3,575 lines. Feeds, cache, vendor loading, universe, synthetics.
| Module | Responsibility | Notes |
|---|---|---|
| `feed.py` (154) | `DataFeed` protocol; `_validate_ordering` raises on backwards timestamps | the lookahead defence's foundation |
| `binance.py` (266) | Pure parsers; three documented traps (closeTime −1ms, 2025 µs switch, forming bar) | network-free, tested |
| `cache.py` (421) | Parquet, prices as strings, `cache_path` sole path constructor, `CachedBarFeed` | `write_bars` **overwrites** — no append (why `live/recorder` exists) |
| `vendor.py` (608) | Loads `historical.zip`; stamp-convention detection; `screen()`, `align()` | 46% coverage, 18 segments measured |
| `universe.py` (507) | Point-in-time membership; **refuses** censored bounds | survivorship guard, ADR-adjacent |
| `panel.py` (541) | Columnar cross-section, 8 bytes/cell vs 664/`Bar` | measured |
| `synthetic.py` (590) | GARCH(1,1)+Student-t+jumps, Sharpe-ceiling enforced | never reads clock (tested) |
| `calibration.py` (140) | Sourced BTC constants | 0 public symbols — data module |
| `intervals.py`, `funding.py`, `connectors.py` | vocabulary; funding settlement; **skeleton** connectors that raise | |
| State: on-disk Parquet cache (`data/cache/`, gitignored). |

### `engine/` — 1,079 lines. The event loop.
| Module | Responsibility | Guard |
|---|---|---|
| `backtester.py` (530) | Orders on bar T fill at T+1 open; funding; cross-section hook; stale marks | `tests/test_lookahead.py` — **17 tests**, CI runs first |
| `execution.py` (223) | `SimulatedExecutionEngine`; `CostModel` + presets `ZERO_COST`, `BINANCE_SPOT_BNB`, `RETAIL_STRESSED` (+2 more per roadmap 28b, INFERRED) | L01/L02 corpus |
| `portfolio.py` (381) | Ledger; O(1) incremental mark; **margin_reserved / free_cash** since 2026-09-01 | P01 corpus |

### `risk/limits.py` — 355 lines. Pre-trade limits, drawdown halt, margin utilisation.
Rebuilt 2026-08-23 after 7 audit gaps (R1–R7). **9b flip-through-zero remains OPEN.**

### `strategy/` — 358 lines. `Strategy` base, narrow `StrategyContext`, 2 examples.
Context exposes: history, equity, **cash, margin_reserved, free_capital** (2026-09-01), positions, universe, order helpers.

### `analytics/` — 3,485 lines. Metrics, walk-forward, sweep, windows (typed train/test), null harness, multiplicity, trades, tearsheet (hand-written SVG), charts.

### `live/` — 1,405 lines. New 2026-09-01. `stream` (push→pull bridge), `recorder` (append-only), `journal` (decisions), `session` (calendars), `replay` (capture→backtester). Round trip proven exact (`test_live_roundtrip`).

### `brokers/base.py` — 156 lines. `Broker` protocol; **every adapter raises.** `PaperBroker` skeleton. `is_live` False everywhere.

### `ui/` — 495 lines. `runner.py` composes a run into a `Verdict`. Not a UI. **Misnamed** (P3, see `CODE_QUALITY_REVIEW.md`).

## Cross-cutting — VERIFIED
- Module-level mutable state: **none** (`grep -rnE "^[a-z_]+ = (\{\}|\[\])" src/` → none).
- Global singletons: none found.
- Shared mutable state: `RiskManager` carries peak-equity; walk-forward creates a fresh one per fold (`walkforward.py`, tested).

## Scheduled processes, workers, queues, caches (in-memory) — ABSENT
Only CI (`.github/workflows/ci.yml`, 11 steps) runs on a schedule (push).
