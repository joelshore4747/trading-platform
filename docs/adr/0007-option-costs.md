# ADR 0007 — What an option costs, established before anything can price one

**Status:** Accepted
**Date:** 2026-08-27

## Context

The ordering is the whole decision. Build a pricer first and you get beautiful
theoretical edges, then find out what they cost. Build the costs first and the
pricer arrives into a world that already knows what a fill is worth.

That ordering only earns its place if the costs actually differ enough to
change conclusions. They do, by more than I expected.

## The measurement

`CostModel` charges `commission(instrument.notional(fill_price, quantity))`
(`engine/execution.py`), and for an option `fill_price` is the **premium**.
Deribit charges 0.03% of the **underlying**, capped at 12.5% of premium. A
different basis, not a different rate.

Black-Scholes premiums at BTC 30,000 and 55% IV — the middle of the range
`data/calibration.py` already sources for BTC realised volatility. The pricing
here is throwaway arithmetic in a scratch script, deliberately not repo code:

| strike | days | premium | premium/spot | spot model | Deribit | understated |
|-------:|-----:|--------:|-------------:|-----------:|--------:|------------:|
| 30,000 | 30 | 1885.20 | 6.28% | 1.8852 | 9.00 | **5x** |
| 33,000 | 30 | 829.40 | 2.76% | 0.8294 | 9.00 | **11x** |
| 36,000 | 30 | 317.11 | 1.06% | 0.3171 | 9.00 | **28x** |
| 30,000 | 7 | 911.36 | 3.04% | 0.9114 | 9.00 | **10x** |
| 33,000 | 7 | 120.84 | 0.40% | 0.1208 | 9.00 | **74x** |
| 36,000 | 7 | 6.94 | 0.02% | 0.0069 | 0.87 | **125x** |
| 45,000 | 30 | 9.26 | 0.03% | 0.0093 | 1.16 | **125x** |

The half-spread is worse and simpler: 1bp of premium against a realistic 5%
for a liquid near-dated crypto strike — **optimistic by 500x**, at every
strike, before considering that deep OTM and long-dated contracts routinely
quote 20–50% wide.

**Both errors flatter the strategy, and both are worst deep out of the money,
which is exactly where options strategies live.** A pricer shipped before this
would have produced edges that are entirely an artefact of not paying for the
trade — and they would have looked best on precisely the contracts where the
model was most wrong.

## Decision

**`OptionCostModel`** (`engine/execution.py`), with rates sourced in
`data/calibration.py` beside the Binance block that was already there:

- commission `min(underlying × contracts × 0.0003, premium × contracts × 0.125)`
- `settlement_fee`, charged once at expiry on the underlying under the same cap
- half-spread as a fraction of **premium**, defaulting to 5%

**The cap is not a detail.** Without it a 6.94 contract would pay 9.00 to
trade — more than it is worth — and the model would reject strategies for the
wrong reason. It is what turns a 1300x understatement into a 125x one on the
far strikes, and it is tested at the crossover, which sits at a premium of
`underlying × 0.0003 / 0.125` = 72.

**A spot cost model now refuses an option.** `CostModel.commission_for` and
`effective_price_for` raise `OptionCostsUnmodelledError` for
`AssetClass.OPTION`, naming the replacement. `ZERO_COST` refuses too, because
it is a spot model and letting it through would under-charge silently;
`ZERO_OPTION_COST` exists for the isolate-a-bug case and charges nothing on
purpose.

This is what makes the step worth doing before there is any data: from here it
is impossible to run an options backtest against spot costs and get a number.
You get an exception.

## What is deliberately not built

No Black-76, no IV solver, no volatility surface, no chain data, no expiry or
exercise handling. Those are roadmap steps 42–46 and none of them are needed
to establish what a fill costs.

Not modelled inside the cost model either, and stated so nobody assumes
otherwise: spreads widening with moneyness and tenor, exercise and assignment,
pin risk at expiry, and the fact that a quoted IV is frequently unmarketable
in any size — the last of which roadmap step 46 already flags as the thing
that kills most options backtests.

## The wiring that is left, and why it fails loudly

`SimulatedExecutionEngine.process` has no price map, so nothing can supply the
underlying's price yet. Rather than plumb a signature change through the fill
logic — the file where a small change is most expensive — `commission_for`
takes `underlying_price` as an optional argument and `OptionCostModel`
**raises** when it is missing. The call site in `process` passes the
instrument, which was already in scope, so the fill logic changed by one line
and its behaviour for spot and perpetuals is bit-identical against fingerprints
taken before the change.

When the chain data arrives, plumbing the price map is the remaining step. Until
then the refusal is the entire mechanism, and it fails loudly rather than
guessing.

## Consequences

- An options backtest cannot silently run on spot costs. That was possible
  yesterday and would have been optimistic by up to 125x on commission and
  500x on spread.
- `crypto_option` joins `crypto_spot`, `crypto_perpetual` and `equity_option`:
  multiplier 1 and European, against the OCC's 100 and American. The difference
  between "one contract is one BTC" and "one contract is a hundred shares"
  matters to every cost and every Greek, so it is expressed rather than assumed.
- The distortion is pinned by tests, not only recorded here, so that nobody
  later simplifies the option path back onto a rate applied to the premium.
- When a pricer does arrive, the first question about any options result is
  already answered: the costs were set before anyone knew what the edge looked
  like.
