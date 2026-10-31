---
description: Scaffold a new strategy correctly — hypothesis first, then code, then adversarial review.
argument-hint: [strategy name and one-line idea]
---

# New strategy: $ARGUMENTS

Do **not** start by writing code. Work through these gates in order. Stop and
report if a gate fails — abandoning an idea at gate 1 is the cheapest thing
this project can do.

## Gate 1 — State the hypothesis

Write it down before anything else, in this form:

> **Claim:** [instrument] exhibits [specific, measurable behaviour] because
> [mechanism].
> **Therefore:** [entry rule] should produce [expected return profile].
> **This is wrong if:** [the observation that would falsify it].

If you cannot name a *mechanism* — a reason someone is systematically on the
other side of this trade and willing to lose — say so explicitly. Trading a
pattern with no mechanism is legitimate, but it must be labelled as such,
because it decays without warning.

## Gate 2 — Check prior art

Has this been done? Use the `quant-researcher` subagent. What is already known
about when it works and when it stops? An idea that was published in 2015 has
had a decade to be arbitraged.

## Gate 3 — Design before implementing

Specify, in prose, before writing code:
- Instrument and bar interval
- Entry condition, exit condition, and what happens in between
- Position sizing rule (fraction of equity, not fixed quantity)
- Every free parameter, with the value you will use and why *that* value
- The minimum history required before the first trade

Count the free parameters. More than three on a few years of daily data means
you will fit noise. If you have more, cut them now.

## Gate 4 — Implement

Create `src/trading_platform/strategy/examples/<name>.py` following
`sma_crossover.py`:
- Subclass `Strategy`; implement `on_bar`
- All parameters in `__init__`, validated there
- Read history only through `ctx` — never import a feed
- Return early until `len(closes) >= lookback`
- Size with `ctx.target_notional(...)`, not a hardcoded quantity

## Gate 5 — Test

Use the `test-writer` subagent. At minimum: parameter validation, the
no-trade-before-full-window rule, and one end-to-end run on multi-regime
data from `data/synthetic.py` (`synthetic_walk` has `open == close` on every
bar, so it cannot exercise limit fills or a drawdown halt).

## Gate 6 — Audit before believing anything

Run all three, in this order:
1. `lookahead-auditor` — did we leak the future?
2. `strategy-reviewer` — is this overfitted?
3. `/cost-check` — does it survive realistic costs?

## Gate 7 — Report honestly

Give me numbers, not adjectives: total return, Sharpe, max drawdown, trade
count, and the cost level at which it breaks even. State the trade count
prominently — under ~30 trades, none of the other numbers mean anything.

Then state your own view of whether this is worth pursuing further. "Probably
not" is a good and common answer.
