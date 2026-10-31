# PORTFOLIO_MAP

## Ledger — VERIFIED (`engine/portfolio.py`, 381 lines)
`equity = cash + Σ spot market_value + Σ derivative unrealised_pnl`, maintained O(1) via
`_positions_total`. Since 2026-09-01: `margin_reserved` (Σ `Instrument.margin_requirement`
at the **current** price) and `free_cash = cash − margin_reserved`, allowed negative.
A derivative fill moves cash by **commission only**; spot pays notional (`Fill.cash_impact
(is_derivative=...)`). Positions use average cost; flip-through-zero realises the closed
portion and opens fresh (tested).

## §29 invariants — tested or not
| Invariant | Test | Status |
|---|---|---|
| cash + market value = equity (spot) | `test_spot_equity_is_cash_plus_market_value`, `test_full_backtest_runs_and_reconciles`, `test_reconciliation_holds_with_a_position_still_open` | ✅ |
| derivative equity = cash + unrealised, notional not debited | `test_derivative_equity_counts_unrealised_not_notional` (corrected 9910→10010, was an anti-guard) | ✅ |
| 1x derivative has spot's capacity | `test_a_fully_collateralised_derivative_has_the_capacity_of_spot` | ✅ |
| margin ≠ equity charge; re-measured at price | `test_a_derivative_reserves_margin_without_charging_equity`, `test_margin_is_remeasured_at_the_current_price_not_at_entry` | ✅ |
| fees reduce cash | `test_portfolio_cash_tracks_fills`, `test_commission_is_charged_from_both_ends` | ✅ |
| realised not double-counted | `test_flip_through_zero_realises_only_closed_portion`, `test_commission_splits_pro_rata…` | ✅ |
| unrealised moves with price | covered by equity tests | ✅ |
| one equity point per timestamp | `test_the_equity_curve_has_one_point_per_timestamp` | ✅ |
| orders cannot create impossible positions | `Order.__post_init__`; `_reduces_exposure` sign test; `test_a_halted_account_cannot_flip_to_a_smaller_short_in_one_order` | ✅ (9b closed 2026-09-02, ADR 0008) |
| account history immutable | `live/journal` append-only; `equity_curve` list is mutable in-process | PARTIAL |
| account cycles never overwrite (§28) | `test_history_survives_a_restart_and_the_numbering_continues`, `test_a_closed_cycle_cannot_be_edited` | ✅ since 2026-09-02 (`live/accounts.py`) |

## Risk layer — VERIFIED (`risk/limits.py`)
Equity-relative caps (order, position), optional absolute ceilings (tighter wins), gross
leverage (2x), **margin utilisation (1.0)**, drawdown halt (25%), orders-per-timestamp (10,
counts rejected too). Pending-delta and pending-margin accumulators defeat slicing (R2).
Reducing orders exempt from size caps, subject to rate limit. `everything_disabled` is the
isolation config and is never production.

## CLOSED 2026-09-02 — 9b single-order flip through zero (ADR 0008) — was VERIFIED OPEN
`_reduces_exposure` returns True whenever `|projected| < |current|`, satisfied by a flip to a
smaller opposite side. Reproduced: **halted** account long 100, cap 1,500, accepted one sell
of 130 at 100 — 13,000 notional, lands short 30, **zero rejections**. R1's fix closed the
multi-order path; the single-order path is open, and the code comment asserts the opposite.
Fix has a design choice (refuse the flip, or split into flatten + capped entry) — its own
plan-mode gate. Corpus entry to follow the fix.

## §28 paper-account cycles — VERIFIED (`live/accounts.py`, 2026-09-02)
`AccountCycles(root, account, starting_cash, currency="USD")` wraps a `Portfolio` per cycle.
`mark(at)` returns the snapshot (§28 fields: cash, positions, orders, fills, fees, realised,
unrealised, equity, drawdown, exposure, cycle) or — at equity ≤ 0 — the **closed** `CycleRecord`
with the next cycle already open, so a supervisor cannot trade a dead cycle by forgetting a
flag. Store: `accounts/<name>/cycles.jsonl`, append-only, one line per open and per close;
`starting_cash` has **no default**. GBP is a display conversion: `CycleRecord.in_currency
(QuoteRates(reporting_currency="GBP"))` converts each end at its own instant and raises
`UnknownQuoteRate` without a series. 13 tests in `tests/test_live_accounts.py`.
