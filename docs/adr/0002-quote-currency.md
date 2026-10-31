# ADR 0002 — A quote currency is a traded asset, and its rate is never assumed

**Status:** Accepted
**Date:** 2026-08-24

## Context

`BTCUSDT` is not `BTC/USD`. It is bitcoin priced in Tether, and Tether is a
security with its own price and its own failure modes. Until this change every
`Instrument` in the repository carried a `quote_currency` field that nothing
read: the portfolio held one cash number, the equity curve carried no
denomination, and every reported figure was labelled USD by wishful thinking.

Thirty-nine of the forty symbols in the imported archive are USDT-quoted
perpetuals. So this was not a hypothetical about some future venue — it was the
denomination of every number the project had produced.

### What it costs, measured

Measured on the real 32-day BTCUSDT sample in `historical.zip` (2026-07-20 to
2026-08-21, 21,158 one-minute bars), running `SMACrossover(20/60)` under
`CostModel()` and restating the resulting equity curve at a simulated
USDT/USD rate. The trades are identical in every row; only the denomination of
the report changes.

A depeg to 0.8789 over six hours, recovering over three days — a 12.11%
fall, chosen to match the scale of the March 2023 USDC episode:

| metric                 | USDT figures | USD figures | change   |
|------------------------|-------------:|------------:|---------:|
| total return           |     -23.7462% |   -23.7462% | **none** |
| max drawdown           |      25.1243% |    27.6404% | +2.52 pp |
| annualised volatility  |      20.0666% |    54.6106% | **2.7x** |
| Sharpe                 |       -33.462 |     -12.043 |          |

The first row is the trap. **A depeg that recovers inside the sample moves
total return not at all** — the number quoted first, and the number a sweep
usually optimises, is the one number blind to the problem. Meanwhile reported
volatility is understated by a factor of 2.7, which flows directly into Sharpe,
into the Deflated Sharpe Ratio that step 6 of the plan depends on, and into
every risk limit expressed as a fraction of equity.

A depeg that does *not* recover inside the sample moves terminal wealth by the
full amount. On buy-and-hold over the same sample:

| depeg (no recovery) | quoted return | USD return | error       |
|---------------------|--------------:|-----------:|------------:|
| 5.00%               |       19.5306% |   13.5541% |  +5.98 pp   |
| 12.11%              |       19.5306% |    5.0554% | +14.48 pp   |

The error exceeds the depeg size because it is proportional to terminal wealth,
not to starting capital: it is `(1 + r) x depeg`, so `1.195306 x 0.1211 =
14.4752` percentage points on the buy-and-hold row. Applying it to the
strategy's own return instead gives a different and wrong figure, which is
worth stating because a reviewer of this ADR made exactly that substitution.

### The second effect: signal contamination

A depeg raises the quoted price of *every* asset quoted in that currency,
simultaneously, without any of them moving. That is a common factor injected
into the whole cross-section. Measured on the same sample, converting prices to
USD before running the strategy changed its fill count from 224 to 294 — a 31%
change in trading activity — while moving total return by only 0.08 to 0.16
percentage points.

So the contamination is real and measurable but, for this probe, small in P&L.
The honest summary is that the accounting effect is large and the signal effect
is small-but-present. A cross-sectional strategy that demeans its universe
(step 7 of the current plan) should be largely immune to it, since a common
factor cancels in the cross-section; a single-symbol trend strategy is not.

### What the historical figures actually are, and are not

An earlier draft of this ADR and of `core/fx.py` gave "$0.9485 on 2022-05-12"
for USDT and "$0.8789 on 2023-03-11" for USDC as though they were venue prints.
**They are not.** They are cross-venue index values, and neither traces to a
primary source. Corrected:

- USDT traded around **$0.94-0.95** on 2022-05-12 (Terra/LUNA).
- USDC traded around **$0.87-0.88** on 2023-03-11 (Circle's ~$3.3bn of reserves
  at Silicon Valley Bank).

And the correction is a *better* argument than the claim it replaces:
**there was no single price.** Reported venue lows on 2023-03-11 ran from about
$0.87 on Kraken to about $0.74 on Binance, with junk prints far below on
trivial size — while Coinbase, the venue one would most want as the USD
reference, had suspended USDC/USD conversion. On 2022-05-12 USDT was
simultaneously below $1 on Kraken and above $1 on Bitfinex; that spread was the
arbitrage. If the rate cannot be stated as a number, an object that refuses to
guess one is the only honest design.

### A daily rate series understates the event, and must say so

The free sources reachable from this sandbox are daily, and a daily figure is
an average. Checked against the real Coin Metrics community series
(`raw.githubusercontent.com/coinmetrics/data`, the first market data this
project has been able to fetch at all):

| asset | date       | daily figure | intraday scale | understated by |
|-------|------------|-------------:|---------------:|---------------:|
| USDC  | 2023-03-11 |       0.9706 |          ~0.87 |          4.4x  |
| USDT  | 2022-05-12 |       0.9972 |     ~0.94-0.95 |  not visible   |

That is load-bearing. Feeding a daily series into `QuoteRates` produces USD
figures that are *better* than assuming the peg and still materially optimistic
— the same class of error this project exists to catch. `max_staleness` bounds
how stale an observation may be; it cannot make a daily average into an
intraday low. Whichever series is used, the run should say which.

Incidentally, the same data corrects a claim about direction: USDT went **up**
during the SVB episode (1.0078 on 2023-03-11), not down. Its ~$1.01 premium
belongs to March 2020 (1.0102 on 2020-03-13).

## Decision

**A rate is known or it is an error. There is no fallback to 1.0.**

- `core/fx.py` provides `QuoteRates`, holding per-asset series of that asset's
  price in one reporting currency. `rate(asset, at)` raises `UnknownQuoteRate`
  for an unknown asset, `UnknownQuoteRate` for an instant before the series
  begins, and `StaleQuoteRate` when the newest observation is older than a
  configured `max_staleness`.
- **Lookup steps back, never forward.** The most recent observation at or
  before the requested instant, never interpolated toward the next one. This is
  the same discipline as ADR 0001: a rate observed on day 5 informing a mark on
  day 3 is the lookahead bug wearing an FX hat.
- **Asserting the peg is a line of code someone typed.** `assume_pegged("USDT")`
  produces rates of exactly 1.0 for the named assets. It is an assumption, and
  a false one during precisely the episodes that matter, so it is visible in a
  diff and answerable in review. It is never a default.
- `BacktestResult` records `quote_currencies`, derived from the run's
  instruments. `curve_in(currency, rates)` restates the equity curve **point by
  point** — converting endpoints alone would reproduce the exact blind spot in
  the first table.
- A run mixing quote currencies cannot name one: `quote_currency` raises
  `MixedCurrencyError`, and `summary()` prints a `REFUSED` line. Such a run has
  been adding unlike units into a single cash balance, so its figures do not
  describe anything.
- `PerformanceSummary` carries a `currency` field, so a report says what its
  figures are denominated in rather than leaving the reader to assume.

Default output now states the denomination, which makes the previously silent
assumption visible on every run:

```
Starting cash : 100,000.00 USDT
Ending equity :  72,969.14 USDT
NOTE          : figures are in USDT, not USD. USDT is a
                traded asset with its own price; call curve_in('USD',
                rates) for USD figures.
```

## Consequences

**Accepted costs**
- USD figures for the existing crypto archive are currently unobtainable: no
  USDT/USD series can be fetched in this environment (every market-data host
  returns 403 at CONNECT). Runs must either report in USDT or state
  `assume_pegged("USDT")` explicitly. That is the correct outcome — the
  alternative is the silent assumption this ADR exists to remove.
- Multi-quote-currency runs are now refused at the reporting boundary rather
  than producing a number. They were always meaningless; now they say so.
- USD-quoted instruments (equities) pay nothing: when the two currencies match,
  no rates are consulted and translation is the identity.

**Deliberately not done**
- **A multi-currency portfolio.** The portfolio still holds one cash balance.
  Making it hold several is a much larger change to the engine, and nothing in
  the project needs it yet: every run today is single-quote-currency. What was
  needed was to stop *pretending* the single balance was USD.
- **Converting prices before the strategy sees them.** Legitimate — the rate is
  observable in real time, so it is not lookahead — but it changes what
  strategy is being tested, and the measurement above says the effect is small
  relative to the accounting effect. Left for when there is a rate series to do
  it with.

**Known limit:** `QuoteRates` is keyed on asset alone. Since USDT genuinely
traded at different prices on different venues at the same instant, a
multi-venue run needs an `(asset, venue)` key. Nothing here is multi-venue, so
the simpler key stands — recorded now rather than discovered later.

**Prior art.** NautilusTrader solves this problem and reaches a similar place
from the opposite direction: it carries an account-level `base_currency`, and
when no rate is available it keeps the last valid one and lists the currency in
`stale_currencies` rather than falling back to 1.0. It degrades and flags;
this module refuses and raises. Theirs is right for a live system that cannot
halt on a missing tick, ours for a backtester whose job is to disappoint you.
The divergence is deliberate, and worth more than a claim of novelty.

**Rejected alternative:** defaulting an unknown rate to 1.0 with a warning. The
project has already learned this lesson twice — a warning that fires on a
routine path becomes noise within a week, and the thing being warned about is
exactly what a run under time pressure will accept. An exception cannot be
skimmed past.
