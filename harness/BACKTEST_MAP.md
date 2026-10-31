# BACKTEST_MAP

## The event loop — VERIFIED (`engine/backtester.py:264-376`)
Per bar: (0) lazily close the previous instant — mark once per timestamp, fire
`on_cross_section`; (1) execute orders queued on earlier bars against **this bar's open**;
(2) settle funding in `(prev, bar]`; (3) reveal bar to context; (4) risk observes the
**running** mark; (5) `on_bar`, then risk-check its orders. Orders from bar T therefore fill
at T+1 — ADR 0001. Enforced by ordering, not a flag.

Guarded by `tests/test_lookahead.py` (17 tests, CI step 1, Stop hook). Corpus defects
L01/L02/S01/D01 reintroduce leaks and are killed.

## Fill model — VERIFIED (`engine/execution.py`)
Market: next open ± half-spread + slippage. Limit buy: `min(limit, open)` if `low ≤ limit`
(the bar's low is **not** used — L02 found the suite could not tell, fixed). Commission on
notional with a minimum. Presets: `ZERO_COST` (engine isolation only), `BINANCE_SPOT_BNB`,
`RETAIL_STRESSED`, plus `BINANCE_SPOT_VIP0` / `BINANCE_PERP_VIP0` (INFERRED from roadmap 28b;
grep pattern did not match their definition form). Default = Binance VIP0 spot, 10 bp.

## §25 checklist — what a run records
| Field | Status |
|---|---|
| strategy version / parameters | params ✅; version ✗ (§84) |
| data source / dataset version | source implicit in cache path; **dataset version ✗** (§85 ABSENT) |
| date range, universe, fees, slippage, execution assumptions | ✅ in `BacktestResult` + `CostModel` |
| random seed | ✅ synthetics seeded; real data n/a |
| commit hash, timestamp | ✗ not stamped on results (§83 reproducibility gap, PROPOSED) |
| metrics | return, CAGR (refuses <1 month), vol, Sharpe, Sortino, max DD + duration, Calmar, win rate, flat fraction, skew, kurtosis; per-fold fills; **halted folds** (35b) |

## §26 validity — what exists
| Check | Where | Status |
|---|---|---|
| in-sample vs OOS, walk-forward | `analytics/walkforward.py` — rolling/anchored, fresh engine+risk per fold | ✅ |
| parameter sensitivity | sweep: `profitable_fraction`, `spike_ratio`, `neighbour_ratio`, stability | ✅ |
| regime sensitivity | synthetic regimes only | PARTIAL |
| costs / slippage / spread | `/cost-check`, presets | ✅ |
| liquidity | ✗ | ABSENT |
| survivorship | `data/universe.py` point-in-time; refuses censored | ✅ |
| lookahead | ADR 0001, 17 guards, `lookahead-auditor` | ✅ |
| leakage (eval-level) | `TrainWindow`/`TestWindow` typed; mypy-enforced | ✅ |
| false-discovery rate | `analytics/nulltest.py`: 1/100 at 4,800 OOS bars; 7–8% below ~1,000 (ADR 0003) | ✅ measured |
| multiplicity | `analytics/multiplicity.py` deflated Sharpe (ADR 0004) | ✅ |
| annualisation validity | `annualisation_is_meaningful` (flatness + window length), now a walk-forward verdict (35c) | ✅ |
| event-aware (§27) | ✗ | ABSENT |

## Verdict layer — VERIFIED
`WalkForwardReport.verdicts()` fires on: halted folds (first), undefined/low efficiency, grid
profitability <25%, neighbour ratio <0.5, stability <50%, median fills <10, unsound
annualisation, losing to buy-and-hold. `survives` is true only when none fire — and means
"failed to falsify", never "works". ADR 0005: which gate binds depends on edge strength;
`neighbour_ratio` and `parameter_stability` read a plateau in opposite directions (open
question).
