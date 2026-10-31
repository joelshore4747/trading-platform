---
description: Re-run a backtest under progressively harsher cost assumptions to find where the edge dies.
argument-hint: [strategy file or name]
---

# Cost check: $ARGUMENTS

A strategy's profitability is a function of assumed costs. Find the point where
this one stops working, and compare that point to reality.

## Run the ladder

Execute the same backtest at each cost level and tabulate the results. Use
`Bash` and report actual output — do not estimate.

| Level | Commission | Slippage | Half-spread | Purpose |
|---|---|---|---|---|
| `ZERO_COST` | 0 | 0 | 0 | Engine sanity only — never evidence |
| Maker | 1 bp | 1 bp | 0 | Optimistic: assumes every fill is passive |
| Taker (default) | 5 bp | 2 bp | 1 bp | The realistic baseline |
| Retail | 10 bp | 5 bp | 3 bp | Small account, wide market |
| Stress | 25 bp | 15 bp | 10 bp | Illiquid hours or a fast market |

For each level report: ending equity, total return, Sharpe, max drawdown,
number of fills, and total commission paid.

## Then answer

**1. Where does it break?** The cost level at which the strategy stops being
profitable. State it as a number.

**2. How much headroom is there?** The gap between that breakeven level and the
realistic baseline. A strategy that breaks at 6 bp when the baseline is 5 bp has
no margin and should be treated as unprofitable.

**3. What is the cost per trade as a fraction of the average gain per trade?**
If costs consume more than about a third of gross edge, the strategy is a
transaction-cost bet more than a signal bet.

**4. Does turnover explain the sensitivity?** Report trades per year. A
high-turnover strategy needs a per-trade edge that survives being multiplied by
its trade count.

**5. What is missing from the model?** Our `CostModel` does not model market
impact scaling with size, funding on perpetuals held overnight, borrow cost on
shorts, or partial fills. State which of these would materially affect this
strategy and roughly how much.

## Verdict

One paragraph: is this strategy's edge robust to realistic costs, marginal, or
an artefact of optimistic assumptions? Be direct. If the honest answer is that
it only works frictionlessly, say exactly that.
