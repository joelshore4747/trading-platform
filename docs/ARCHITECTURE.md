# Architecture

## Dependency direction

```
core/      types, instruments, events        no internal dependencies
  ↑
data/      feeds, validation                 depends on core
  ↑
risk/      pre-trade limits                  depends on core
  ↑
engine/    backtester, portfolio, execution  depends on core, data, risk
  ↑
strategy/  base class, context, examples     depends on core (and engine types)
  ↑
analytics/ metrics                           depends on engine (EquityPoint)
```

Dependencies point **downward only**. `strategy` may import `core`; `core` may
not import `strategy`. A required back-reference means the design is wrong.

## The event loop

`engine/backtester.py::Backtester.run` — the ordering here is the product.

```
for bar in feed:                    # feed guarantees non-decreasing timestamps
    risk.start_bar()                # reset per-bar counters

    ① execution.process(bar)        # fill orders queued on PREVIOUS bars,
       → portfolio.apply_fill       #   at THIS bar's open
       → strategy.on_fill

    ② ctx._observe(bar)             # only now does the strategy see this bar

    ③ portfolio.mark(...)           # equity point; risk observes drawdown

    ④ strategy.on_bar(bar, ctx)     # strategy acts on what it can know
       → ctx._drain_orders()
       → risk.check(...)            # rejected orders never reach execution
       → execution.submit(...)      # queued for the NEXT bar
```

**① before ② is the whole safety property.** A signal computed in ④ on bar T
cannot fill until ① of bar T+1. Reordering these steps is how a backtest
engine starts lying.

## Why the context is narrow

`StrategyContext` exposes history, current price, position, equity, and order
submission. It does not expose the feed, the portfolio object, or any way to
index forward.

This is not encapsulation for its own sake. Every additional door into engine
state is a way for future information to reach a decision. When a strategy
needs something new, extend the context **explicitly** — the review that
accompanies that change is the point.

## Equity accounting

Spot and derivative positions contribute differently:

- **Spot**: full market value. You bought the asset; you hold the asset.
- **Derivative**: unrealised P&L only. The notional was never paid in cash.

Conflating them double-counts equity in a mixed book. `Portfolio.positions_value`
branches on `instrument.is_derivative`; `tests/test_portfolio.py` pins both.

## Position accounting

`Position.apply` handles four cases. The fourth is the one implementations get
wrong:

| Case | Effect |
|---|---|
| Opening from flat | Quantity set, basis = fill price |
| Adding to a side | Basis blended by quantity |
| Reducing | Realise P&L on the closed portion; **basis unchanged** |
| Flipping through zero | Realise on the closed portion only; remainder **re-bases** at the fill price |

## Where to extend

| Need | Add to |
|---|---|
| New venue or file format | `data/` — implement `DataFeed`, wrap in `_validate_ordering` |
| New instrument type | `core/instruments.py` — subclass `Instrument` |
| New order type | `core/events.py` + `execution.py::_resolve_price` |
| New risk control | `risk/limits.py` — field on `RiskLimits`, branch in `check` |
| New metric | `analytics/metrics.py` — pure function on the equity curve |
| New strategy | `strategy/examples/` — see `/new-strategy` |

Adding a live broker is **not** on this list. See `docs/PLAN.md` Phase 6.
