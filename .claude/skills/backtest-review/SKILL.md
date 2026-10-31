---
name: backtest-review
description: The checklist to run before believing a backtest result. Use whenever a backtest produces numbers that will inform a decision, and especially when the result looks good.
---

<!-- charter §65 shape -->
**purpose** — decide whether a backtest number may inform a decision.
**when_to_use** — any run whose figure will be quoted, compared, or built on; always when it looks good.
**inputs** — `BacktestResult` or walk-forward report; cost preset; strategy version; trial count.
**tools** — `/cost-check`, `lookahead-auditor`, `strategy-reviewer`, `overfitting-auditor`, `analytics/multiplicity.py`.
**validation** — every gate below produces a number; the report format at the end is filled, not summarised.
**outputs** — the report block in §7 plus one paragraph naming what would change the conclusion.
**failure_modes** — reading Sharpe without `periods_per_year`; trusting a run that halted; forgetting the trials that came before this one.

# Reviewing a backtest result

A good-looking result is evidence of a bug until proven otherwise. Work this
checklist in order. Stop and report at the first hard failure — later checks are
meaningless if an earlier one fails.

## 1. Is the number even meaningful?

| Check | Threshold | If it fails |
|---|---|---|
| Trade count | ≥ 30 | No performance statistic means anything. Report the count and stop. |
| Bars processed | Enough to cover ≥ 2 market regimes | Result describes one regime, not an edge. |
| Unfilled orders at end | Small relative to fills | A large number means orders were queued and never executed — the strategy is not doing what it looks like. |
| Risk rejections | Understand every one | Rejections mean the strategy wanted to breach a limit. Report `rejection_summary`. |
| Halted flag | Should be False | If True, the run stopped early on drawdown and the equity curve is truncated. |

## 2. Does it reconcile?

```python
final = result.equity_curve[-1]
assert final.equity == final.cash + final.positions_value
```

If cash plus marked positions does not equal reported equity, the accounting is
wrong and every downstream number is wrong with it.

## 3. Does it survive costs?

Run `/cost-check`. A result that only exists at `ZERO_COST` is not a result. The
question is not "is it profitable" but "how much cost does it tolerate before it
is not", and how that compares to the real venue.

## 4. Did the future leak in?

Run the `lookahead-auditor` subagent. Do this **before** getting attached to the
number, not after.

## 5. Is it fitted?

Run the `strategy-reviewer` subagent. Specifically: how many parameters, and
were any of them chosen by looking at this same data?

## 6. Is the annualisation right?

`summarise(..., periods_per_year=N)` — **365 for crypto**, 252 for equities,
and the bar interval must match (daily bars with `periods_per_year=365`, hourly
bars with `24*365`). Getting this wrong scales Sharpe by a constant and is easy
to miss.

## 7. Report format

Numbers, not adjectives. Always include, in this order:

```
Trades         : N          <- lead with this; it gates everything else
Total return   : X%
CAGR           : X%
Sharpe         : X.XX
Max drawdown   : X%
Breakeven cost : X bp       <- from /cost-check
Rejections     : {...}
```

Then one paragraph of interpretation that explicitly names what would change
your conclusion.

## The bar for "this looks promising"

All of: ≥30 trades, survives `CostModel()` with headroom, clean lookahead
audit, parameters not tuned in-sample, and a stated mechanism for why the edge
exists. Anything short of all five is "interesting, not validated" — say so in
those words.
