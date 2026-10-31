---
description: Assume a strategy has already failed with real money. Work backwards to find why.
argument-hint: [strategy name or file]
---

# Pre-mortem: $ARGUMENTS

It is eighteen months from now. **$ARGUMENTS was traded with real money and
lost badly enough to stop.** The backtest looked good. Something was wrong
anyway.

You are writing the incident review. Do not hedge, do not say "it might" — the
failure has already happened and your job is to explain it.

Work through each candidate cause. For each, state whether the evidence in this
repository supports or rules it out, and cite the file and line you checked.

## 1. The backtest was wrong

- Lookahead bias the `lookahead-auditor` did not catch. Where would it hide?
- Survivorship or selection bias in the symbol universe or date range.
- Costs understated: real spread wider than `CostModel`, market impact at size,
  funding on perps held overnight, borrow cost on shorts.
- Fills that would not have happened: limit orders assumed filled at the touch,
  size larger than the bar's volume could absorb.

## 2. The edge was never real

- The parameters were fitted to this specific data. How many were tuned, and on
  what sample?
- The sample was too small for the statistics reported. How many trades?
- The result is a levered beta bet dressed as alpha — it made money because the
  underlying went up, not because the signal worked.

## 3. The edge was real and then died

- It was published, or became crowded.
- The market regime changed: volatility, correlation, or liquidity moved to a
  state the sample never contained.
- Venue mechanics changed: fee schedule, tick size, funding formula.

## 4. Implementation diverged from simulation

- The live system did something the backtest did not model — partial fills,
  rejects, reconnects, duplicate orders after a retry.
- Data pipeline differences: a revision, a different timestamp convention, a
  gap the backtest never saw.

## 5. Risk management failed

- Position size grew faster than the risk limits anticipated.
- The drawdown halt did not fire, fired too late, or was disabled to "let it
  recover".
- Correlated positions were treated as independent.

## 6. The human failed

- Overrode the system during a drawdown.
- Restarted it after the halt without diagnosing the cause.
- Kept adding capital because the recent numbers looked good.

## Deliverable

1. **The single most likely cause**, named specifically, with the evidence.
2. **The three next most likely**, ranked.
3. For each of the four: **the check that would detect it today**, concrete
   enough to run this week.
4. Anything you could not assess from the repository, stated plainly.

Rank by probability, not by how interesting the failure would be.
