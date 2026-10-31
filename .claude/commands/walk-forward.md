---
description: Run the walk-forward harness and report in-sample against out-of-sample, because the gap is the finding.
argument-hint: [strategy name or file]
---

# Walk-forward: $ARGUMENTS

An in-sample number is not a result. This command produces the out-of-sample
one, alongside it, so the difference is visible rather than reported away.

## 1. Set it up

Read the strategy and identify every sweepable parameter (they are the
`__init__` keyword arguments — see `strategy/examples/sma_crossover.py`).

Write a short script under the scratchpad directory that calls
`trading_platform.analytics.walkforward.walk_forward`. Requirements, all of
which the harness enforces but which you should get right anyway:

- **Factories, not instances.** `backtester_factory` must build a *new*
  `Backtester` with a *new* `RiskManager` on each call — it carries peak-equity
  state, and a shared one leaks a drawdown halt from one fold into every later
  one.
- **Realistic costs.** Default `CostModel()`. Never `ZERO_COST` — that is for
  isolating an engine bug, not for producing a result.
- **`warmup` at least as long as the strategy's longest lookback**, or short
  folds measure mostly silence.
- **Pass `benchmark_symbol`.** "Made money" means nothing if holding the
  instrument would have made more for no commission and no work.
- **A grid that is coarse before it is fine.** 16 cells first. If the surface
  is flat and uninteresting, a 200-cell grid will not rescue it — it will just
  find a coincidence.

Run both `anchored=False` (rolling) and `anchored=True` (all history so far).
They answer different questions and disagreeing is informative.

## 2. Report

Print `report.report()` verbatim. Do not summarise it into prose — the table is
the evidence and the reader needs the per-fold numbers.

Then state, in this order:

1. **Efficiency** (out-of-sample over in-sample). Below ~0.5 the strategy is
   fitted. `None` means it failed in-sample and there is nothing to discuss.
2. **Every verdict the report raised**, quoted. Do not filter them.
3. **The worst fold**, by name and number, and what happened in it.
4. **Fills per fold.** If the median is under ~10, say plainly that the ratios
   above are unsupported by the sample, whatever they read.
5. **Buy-and-hold comparison** — folds beaten, out of folds run.

## 3. Hand it to the auditor

Invoke the `overfitting-auditor` subagent with the report and the script you
wrote. Report its verdict verbatim, including when it contradicts the numbers
looking good.

## 4. Do not

- Do not re-run with a different grid, seed, objective, or window size to
  improve the result. If you want to test robustness, run the *same*
  configuration on a different seed and report **both** — a result that only
  appears at one seed is not a result.
- Do not report the best fold. Report the distribution.
- Do not describe a surviving run as an edge. The strongest honest phrasing is
  that this run failed to falsify one, on this data, at this cost model.
