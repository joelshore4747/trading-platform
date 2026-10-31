# ADR 0001 — Orders fill on the next bar, not the signal bar

**Status:** Accepted
**Date:** 2026-08-19

## Context

A backtest must decide when an order submitted during bar T executes. Two
options:

1. Fill immediately against bar T (typically at its close).
2. Fill against bar T+1 (at its open).

Option 1 is simpler and produces better-looking results. It is also wrong: the
signal that generated the order was computed *from* bar T's close, so filling at
that same close means trading on a price that was not knowable until the moment
the bar ended — and in practice, not actionable until after it.

This is the most common defect in backtest engines. It does not announce itself:
there is no error, only an equity curve that is quietly optimistic. In a
mean-reversion strategy it can invert the sign of the reported edge entirely.

## Decision

Orders submitted while processing the bar stamped T fill against the bar stamped
T+1, at that bar's open, plus modelled spread and slippage.

This is enforced structurally by the ordering of the event loop in
`engine/backtester.py`: pending orders are executed *before* the new bar is
revealed to the strategy. It is not a flag or an option — there is no
configuration that turns it off.

`tests/test_lookahead.py` pins the behaviour, and a `Stop` hook prevents
finishing a session while those tests fail.

## Consequences

**Accepted costs**
- An order placed on the final bar never fills. Reported via
  `BacktestResult.unfilled_orders` rather than silently dropped.
- Strategies reacting to intra-bar events cannot be modelled at bar resolution.
  That is correct — they need tick or quote data, which is a separate feed type.
- Results are worse than the naive implementation would report. This is the
  point.

**Rejected alternative:** a configurable `fill_delay` parameter. A setting that
can be set to zero will eventually be set to zero, most likely while chasing a
result. The guarantee is worth more than the flexibility.
