# STRATEGY_MAP

## The contract — VERIFIED
`strategy/base.py` (398 lines). A `Strategy` implements `on_bar(bar, ctx)` and optionally
`on_start`, `on_fill`, `on_reject`, `on_finish`, `on_cross_section(instant, ctx)`. It reaches
the market **only** through `StrategyContext`: `history()`, `closes()`, `equity`, `cash`,
`margin_reserved`, `free_capital`, `positions`, `is_flat`, `universe()`, `buy/sell/
target_notional`. The context is a `_QuantityView` over live positions (O(1)), synced at the
same point in the loop as equity — the running mark, never end-of-instant
(`test_engine_scaling::test_cash_and_margin_seen_by_a_strategy_carry_no_more_than_equity_does`).

## Strategies — VERIFIED, with their honest results
| Strategy | File | Free params | Measured result | Status (§44) |
|---|---|---|---|---|
| `SMACrossover` | `strategy/examples/sma_crossover.py` | `fast`, `slow`, `allocation` (0.95) | Real BTCUSDT 1m, 18 rolling folds, 1x collateralised, `CostModel()`: **mean in-sample Sharpe −2.52**, 7/18 profitable folds, 70 OOS fills, 6 verdicts fired. Fails on the data it was fitted to. | **REJECTED** on this data. Retained as the harness probe; ADR 0005 says it is the wrong probe for the verdict layer. |
| `CrossSectionalMomentum` | `strategy/examples/cross_sectional_momentum.py` | `lookback`, `rebalance_every`, top-k | Synthetic only (ADR 0005): detection turns on near IC 0.05, at the top of where real cross-sectional momentum lives. Never run on real data. | **EXPERIMENT** |

Both are long-only. Neither has survived out-of-sample on real data. **No strategy in this
repository is validated** — VERIFIED, and consistent with the prime directive.

## §21 strategy anatomy — coverage
identity ✅ (`name`) · version ✗ (§84 immutable versions ABSENT) · universe ✅ (`ctx.universe()`,
point-in-time) · timeframe — implicit in feed · inputs/indicators ✅ via `ctx.history()` ·
signals/filters/entry/exit — inline in `on_bar`, not decomposed · risk model ✅ external
(`RiskManager`) · position sizing ✅ `target_notional` · evaluation config ✅ walk-forward.

## §22 composition — ABSENT
No UNIVERSE/SIGNAL/FILTER/REGIME/ENTRY/SIZE/RISK/EXIT component model. Each strategy is one
class. PROPOSED for P7; the `on_cross_section` hook (ADR 0005) is the natural seam for a
ranker component.

## The lookahead audit surface — VERIFIED
`lookahead-auditor` (Opus) covers 7 leak categories; `evals/` measured it 8/8 on real
defects. `guard-auditor` covers tests that cannot fail. Both are the §26 checks for
strategies; any new strategy runs `/new-strategy` → gauntlet before being believed.
