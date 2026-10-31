---
name: strategy-autopsy
description: Structured investigation of why a strategy performed badly — regime, signal, execution, fees, slippage, liquidity, data, parameter sensitivity, prior failures — producing a reusable failure report. Use after any run that lost, halted, or diverged from expectation.
---

# Strategy autopsy (charter §51)

## purpose
Turn a bad result into knowledge that outlives the strategy. The output is a
structured failure report appended to `harness/KNOWN_FAILURES.md` (pattern) and
the experiment record (instance), never a tweak to the strategy.

## when_to_use
A backtest, walk-forward fold, or paper day that lost money, halted on drawdown,
or diverged from its backtest twin. Also when a result *improved* unexpectedly: an
improvement nobody can explain is a defect until explained.

## inputs
The `BacktestResult` or `DaySummary`; the strategy version and parameters; the
dataset id and window; the cost preset; the risk limits; the prior experiment record.

## procedure
1. **Regime.** What did the market do over the window? Trend, chop, crash. Compare
   with buy-and-hold on the same bars. A strategy that lost less than the market
   in a crash did not fail; one that lost in a trend it was built for did.
2. **Signal quality.** Hit rate, average win vs average loss, how many signals
   fired versus how many the design expected. Was the signal ever *right* and the
   trade still lost? Then the failure is downstream of the signal.
3. **Execution.** Fill count vs order count; `rejection_summary` by reason; unfilled
   orders at the end. Four hundred rejections is a different day from three fills.
4. **Fees and slippage.** Re-run at `ZERO_COST` to isolate, then `/cost-check` to
   find the breakeven. If the edge exists only at zero cost, the strategy is a fee
   donor, not a loser.
5. **Liquidity and data.** Coverage of the window (this archive: 46%, 07–10 UTC
   biased); gaps; late bars refused (`StreamStats`); marks older than one bar.
6. **Parameter sensitivity.** Neighbouring parameters on the sweep surface: does
   the loss vanish one step away? Then the original number was the spike.
7. **Similar historical failures.** Grep `harness/KNOWN_FAILURES.md` and
   `docs/ROADMAP.md` for the pattern. Most failures here have happened before.
8. **Decide** which one or two causes account for most of the loss, with the
   number that shows it.

## tools
`analytics/metrics.py` (`summarise`), `analytics/walkforward.py`, `/cost-check`,
`live/journal.py` (`summarise_day`), `strategy-reviewer` and `risk-auditor` agents.

## validation
Each claimed cause has a number and a reproducible command. Re-running with the
cause removed (for example at `ZERO_COST`, or excluding the crash bars) moves the
result in the direction the report predicts. If it does not, the cause is wrong.

## outputs
A report: window / regime / dominant cause (with number) / contributing causes /
what would have had to be true for the strategy to work / pattern name for
`KNOWN_FAILURES.md` / status for the experiment record (REJECTED, RETIRED, or
PROMISING with the condition).

## failure_modes
Blaming "the market" without a buy-and-hold comparison. Fixing the parameter that
would have won this window (that is a new trial; count it). Reading a loss off a
truncated equity curve when the run halted. Confusing a data-coverage artefact
with a strategy property.
