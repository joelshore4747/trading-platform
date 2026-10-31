# ICT — inner circle trader concepts

**Status: mechanised, tested, and REJECTED on the only data we have.**

## What the concepts claim

| concept | the claim | our implementation |
|---|---|---|
| swing point | this pivot is a level participants watch | `patterns/structure.swing_points` |
| BOS / CHoCH | trend continued / turned | `patterns/structure.market_structure` |
| fair value gap | this span was skipped and price returns to it | `patterns/imbalance.fair_value_gaps` |
| order block | the move started here and price returns to it | `patterns/imbalance.order_blocks` |
| liquidity sweep | stops beyond this level were taken and the move failed | `patterns/liquidity.liquidity_sweeps` |
| killzone | the London and New York hours carry the day's real moves | `patterns/sessions` |

## What we found — VERIFIED

**A sweep-reversal strategy composed from these primitives was rejected on real
BTCUSDT 1m data.** 0 of 18 walk-forward folds profitable, **and 0% of the average
training grid profitable**. Mean in-sample Sharpe -23.83. It lost on the data it
was fitted on, so there was no edge to degrade — not a failure to generalise, but
nothing having been there. Record: `harness/experiments/E-016-*.toml`.

The killzone filter carried nothing either way: the winning cell used it in 14
folds and did without it in 4, and both arms are uniformly negative.

**This does not refute ICT in general** and the note should not be read that way.
It refutes *one* mechanisation of *one* of its ideas on *one* instrument over
*32 days*. What it does establish is that the obvious composition does not work,
so anyone revisiting this needs a different mechanism and a pre-registered
hypothesis, not different parameters.

## What is worth keeping — VERIFIED

The primitives themselves are pure, tested, and reusable. In particular
`tests/test_patterns.py::test_no_primitive_can_see_a_bar_that_has_not_printed`
walks a series bar by bar and proves none of them reads a bar that has not
printed. That guard is worth having whatever is built on top.

## The trap anyone using these must know — VERIFIED

**A swing point is not knowable at the bar it prints on.** It needs `strength`
further bars to confirm. Every result carries `confirmed_at` beside `index`, and
using `index` where `confirmed_at` belongs gives the strategy `strength` bars of
hindsight. It is the most common way a backtest of these ideas lies, and it looks
identical in a diff.

## Open questions — UNKNOWN

- Whether the killzone idea means anything for a 24-hour crypto market. It comes
  from index futures, where session boundaries are real.
- Whether these ideas hold on the instrument class they came from (US index
  futures). We have no futures data; `harness/EXTERNAL_DATA_MAP.md` records
  Databento/IBKR as absent.
- Whether a longer holding period, or a level-touch entry rather than a
  sweep-bar entry, changes anything. Untested, and not worth testing until there
  is a reason beyond "try another parameter".

## Sources

None filed yet. `library/inbox/` is where documents go; `scripts/library.py file`
moves them here.
