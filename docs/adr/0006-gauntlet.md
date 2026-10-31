# ADR 0006 — The first real-data gauntlet: rejected, and the reasons are worth more than the verdict

**Status:** Accepted
**Date:** 2026-08-27

## Context

The roadmap's step 35 says to run the full gauntlet on `SMACrossover` with real
data. ADR 0005 had just established that `SMACrossover` is the wrong probe for
anything about the verdict layer: at two to three out-of-sample fills per fold
the fill objection fires before any other gate can speak, so the result would
have been a guaranteed `UNDERPOWERED` carrying no information.

So the gauntlet ran on `CrossSectionalMomentum` instead, against the real
37-symbol archive — which is also the thing step 7's lookahead audit said it
could not test, having only the three-symbol vendor parquet available to it.

## The data, and what it can support

`historical.zip`: 40 Binance perpetual symbols, 1-minute bars. `screen()`
rejects three — TONUSDT (one price, 1.5901, across all 21,158 bars and zero
volume), QNTUSDT and CCUSDT. `align()` by intersection keeps 98.7%, leaving
**37 symbols on 20,856 shared timestamps**.

The span is **32 calendar days** at 45% coverage. That is the binding
constraint on everything below: this archive can support an *intraday* test and
nothing else. The stated mechanism for cross-sectional momentum — slow
diffusion of information across a correlated asset class — is a multi-day
story, and a 30-to-120-minute lookback is not testing it.

### An annualisation that would have been silently wrong by 2.2x

Bars are stamped one minute apart, so the obvious `periods_per_year` is
525,600. But 55% of those minutes are missing: the effective bar interval is
**2m13s**, and the honest figure is **236,624**. Using the naive one inflates
every annualised Sharpe by `sqrt(525600/236624)` — a factor of **2.22**, in the
flattering direction, on a number nobody would think to check.

## The verdict

Walk-forward, 17 folds, train 3,000 / test 1,000 / warm-up 240 instants,
`CostModel()` (Binance VIP 0: 10bp commission, 2bp slippage, 1bp half-spread),
nine cells over `lookback` × `rebalance_every`. The trial was recorded in
`docs/trials.jsonl` before it ran, with its mechanism, as the house rule
requires.

```
Mean in-sample       :    -1.31
Mean out-of-sample   :    -5.78
EFFICIENCY (oos/is)  :      n/a
Profitable folds     :        7/17
Out-of-sample fills  :      701  (median 42/fold)
Grid profitable      :       8%
Neighbour ratio      :    -2.17
Parameter stability  :      31%
```

**REJECTED**, on four objections: no in-sample edge to degrade, 8% of the
training grid profitable, peaks are spikes not plateaus, and the optimum moves
almost every fold.

**`rebalance_every: 240` was selected in all seventeen folds.** The sweep's
answer is "trade as little as this grid permits", which means the grid may not
contain a viable cell at all and the search is really a search for doing
nothing. It also means the 31% parameter-stability figure is one axis perfectly
stable and one axis pure noise, averaged into a single number that describes
neither.

## The cost check: where it dies

| cost model | mean IS | mean OOS | profitable folds |
|------------|--------:|---------:|-----------------:|
| `ZERO_COST` (isolates logic only) | 5.62 | **1.13** | 9/17 |
| 1bp all-in | 4.70 | **0.12** | 9/17 |
| 3bp commission + 1bp friction | 2.72 | −1.98 | 8/17 |
| VIP 3 (6bp + 3bp) | 0.39 | −4.11 | 7/17 |
| **VIP 0, the default (10bp + 3bp)** | −1.31 | **−5.78** | 7/17 |
| harsh (20bp + 6bp) | −6.44 | −12.83 | 4/17 |

The out-of-sample edge crosses zero **between 1 and 4 basis points all-in**.
The cheapest cost any real retail participant faces is 13. The edge does not
die at realistic costs; it dies an order of magnitude below the cheapest
unrealistic ones. Per the house rule: profitable at `ZERO_COST` and
unprofitable at `CostModel()` means unprofitable.

Note also that **nothing survives at any rung**, including `ZERO_COST`. Costs
are not what the verdict is objecting to.

## The pre-mortem

### A prediction I made that the data refused

Coverage is strongly uneven by hour — 58% at 08:00 UTC against 20% at 22:00, a
**2.84x** ratio — so I predicted the frictionless edge would concentrate in the
over-sampled hours and be an artefact of which minutes survived collection.

It does not. The twelve best-covered hours produced 7,365 of P&L; the twelve
worst-covered produced **9,736**, from half as many instants. The hypothesis
was wrong and is recorded because a pre-mortem that only lists confirmed fears
is a worse instrument than one that also reports its misses.

### The number someone would quote, and why it is not the number

A single full-series `ZERO_COST` pass returns **+17.10%**. That figure is
worthless, for a reason that has nothing to do with the strategy:

- **One minute is more than half of it.** 2026-08-21 02:44 contributes +8,737,
  **51.1%** of the total. That is the coordinated spike ROADMAP step 26
  documents as a real market event (XRPUSDT +21.97%, 1000PEPEUSDT +20.06%, all
  40 symbols printing, 39 moving together). Not bad data — but a momentum
  ranker holding the movers into one such event is a single draw, not an edge.
- **No fold ever evaluated it.** The final test window ends 2026-08-19 12:43
  and the archive runs to 08-21 11:49, so **4.1% of the archive lies beyond
  every test window**. The full-series number includes a period the
  walk-forward never validated.

Inside the evaluated span the frictionless P&L is **+1,519** — +1.5%, not
+17.1% — and it is dominated by large *losses*: removing the ten biggest
single-instant moves takes it from +1,519 to **+13,268**. The tails are net
strongly negative, which is the opposite shape from the full-series picture.

**The general lesson, which outlives this strategy:** a full-series backtest
and a walk-forward over the same file can differ by 10x, because the
full-series run scores a period no fold evaluated. Any number quoted from this
project must say which span produced it.

### A gate that never fired

`walk_forward` takes a single `benchmark_symbol`, and a 37-symbol ranker has no
honest single-symbol benchmark — beating BTC is not the claim, beating "hold
everything" is. None was passed, so `hold` read `n/a` in all seventeen folds
and the beats-benchmark objection **could not fire at all**. A gate that is
silently absent is worse than one that is wrong, because the report looks
complete.

Computed by hand over the evaluated span: equal-weight basket of all 37
**−1.81%**, BTCUSDT alone **−1.13%**, strategy at `ZERO_COST` **+1.52%**. So
frictionlessly it beats holding everything by about 3.3 points over 27 days,
and with any real cost it loses to both.

## A defect found by using my own machinery

Step 6 built the trial registry on the argument that the count of attempts is
what governs project-level honesty. Its first real use wrote to
`runs/trials.jsonl` — and `runs/` is gitignored, with CI actively failing if
anything under it is tracked. **The paper trail would have been discarded at
the end of the session.** A count that does not persist cannot support any
multiplicity correction, which is the only thing the module is for. Now
`docs/trials.jsonl`, with a `DEFAULT_PATH` constant and a test that asserts it
is not under `runs/`.

## Corrections the strategy review forced

A `strategy-reviewer` pass found three things. Two are mine and one is the
engine's, and the engine's is worse than anything else in this document.

### The engine cannot backtest a perpetual at all

`Portfolio.apply_fill` deducts the **full notional** from cash on every fill,
unconditionally. `Portfolio._refresh` then credits a derivative position back
into equity as **unrealised P&L only**, never as an asset. There is no margin
or collateral concept anywhere in the codebase. So opening a perp spends the
notional and books nothing in return, and equity falls by the size of the
position the instant it is opened.

Measured on this archive, same strategy, same parameters, changing only the
instrument type:

| instrument | return | fills | halted | rejections | equity by instant 397 |
|------------|-------:|------:|:------:|-----------:|----------------------:|
| `crypto_spot` | −0.02% | 765 | no | 0 | 98,334 |
| `crypto_perpetual` | −0.05% | 11 | **yes** | **424** | **8,302** |

Equity falls from 100,000 to 8,302 in 400 instants with no market move to
justify it, trips the 25% drawdown halt, and the halt is a one-way latch — so
every later entry is rejected while exits still pass. Eleven fills in a
20,856-instant run.

`tests/test_portfolio.py::test_derivative_equity_counts_unrealised_not_notional`
**asserts this behaviour**: buy one perp at 100 against 10,000 cash, price
rises to 110, and the test requires equity of 9,910 — a reported 90 loss on a
position that gained 10. The bug was written down as the specification, which
is why nothing caught it. Nobody had sized a position large enough for the
missing notional to show.

This is filed as blocking. Until it is fixed, **no perpetual backtest this
project produces means anything**, and that is most of what the archive is.

### I modelled 40 perpetuals as spot

Every run in this document passes `crypto_spot`. `data/vendor.py` has
`instrument_for`, which exists to pick the type from the vendor's own source
tag, and whose docstring names this exact mistake: *"perpetuals accrue funding,
and a backtest that treats one as spot silently omits a carry cost that runs to
double digits annualised."* I hardcoded the wrong one past a function written
to stop me.

The accidental consequence is that these numbers dodge the accounting bug above
and are internally consistent. The real consequence is that they are a spot
backtest running on perpetual price data with **no funding charged at all** —
so every figure here is missing a carry cost, in the flattering direction, on
top of everything else.

### The full-series figure I quoted was one cell of nine

I reported −23.87% with a halt as though it characterised the strategy. It is
the `lookback=60, rebalance_every=60` cell. Across the whole grid on spot:

| | rebalance 30 | rebalance 60 | rebalance 240 |
|---|---:|---:|---:|
| **lookback 30** | −24.73% halt | −25.35% halt | −22.70% halt |
| **lookback 60** | −24.63% halt | −23.87% halt | **−0.02%** |
| **lookback 120** | −24.37% halt | −29.70% halt | **−7.57%** |

Seven of nine cells trip the drawdown halt; the two that do not are at the
rebalance frequency the walk-forward selected in all seventeen folds. Quoting
one cell as the strategy's behaviour, when the sweep chose a different one, is
the same error as quoting a full-series number for a span no fold evaluated —
committed twice in one document.

### What survives

The 17-fold walk-forward stands. It ran on spot throughout, so it never touched
the derivative path, and the review's independent reproduction lands within
noise of every headline figure — including an exact match on median fills per
fold. The rejection is real. The cost ladder stands for the same reason.

What does not stand: any claim about this strategy's *risk profile* from a
full-series pass, and any claim at all about perpetuals until the engine is
fixed.

## Consequences

- The strategy is rejected and is not worth fixing. It was built as a reference
  implementation of the cross-sectional API, not as a candidate, and it did its
  job.
- **Add an equal-weight-basket benchmark to `walk_forward`.** A single
  `benchmark_symbol` cannot express the right comparison for a ranker, and the
  gate silently not firing is the worst available failure mode.
- **The walk-forward should report the span it did not evaluate.** 4.1% of this
  archive was never scored and nothing said so.
- **Fix `Portfolio`'s derivative accounting before any other strategy work.**
  It is a milestone-class change — it needs a margin model, or a decision that
  a perp fill moves only commission — and the test that encodes the bug must be
  rewritten with the reasoning stated, not quietly flipped.
- **Use `vendor.instrument_for`, never a hardcoded constructor**, and supply a
  `FundingSchedule` when the instruments are perps.
- The archive supports intraday tests over 32 days and nothing longer. Any
  multi-day hypothesis — including the one this strategy claims — needs data
  this project does not have. That belongs on the roadmap as a data problem,
  not a strategy problem.
