---
name: adding-a-data-source
description: How to add a new market data feed (exchange API, CSV export, vendor file) to the platform without introducing lookahead or timestamp bugs. Use when wiring up a new data source or adapter.
---

<!-- charter §65 shape -->
**purpose** — bring a new feed in without a timestamp, ordering, or survivorship bug.
**when_to_use** — any new vendor file, exchange API, or live adapter; before its first backtest.
**inputs** — a sample of the raw data; the venue's documentation of stamping and interval; a `source-validation` card.
**tools** — `data/feed.py` (`_validate_ordering`), `data-validator` agent, `data-quality-audit` skill, `live/recorder.py` for live capture.
**validation** — tests for ordering, tz-aware close stamps, malformed rows rejected, symbol discovery; the first bar's timestamp asserted against the documented convention.
**outputs** — the loader in `data/`, its tests, and a row in `harness/EXTERNAL_DATA_MAP.md`.
**failure_modes** — open-stamped bars passed through (D01); forward-fill inside the loader; a universe drawn from today's listings.

# Adding a data source

Every data source is a chance to introduce a silent bug. The engine trusts
feeds to be honest about time; this checklist is how a feed earns that trust.

## The contract

A feed must satisfy the `DataFeed` protocol in `data/feed.py`:

```python
def __iter__(self) -> Iterator[Bar]: ...
@property
def symbols(self) -> tuple[str, ...]: ...
```

and it must guarantee:

1. **Bars are yielded in non-decreasing timestamp order.** Wrap your generator
   in `_validate_ordering` — do not assume the source is sorted. Vendors ship
   unsorted data more often than you would expect.
2. **Timestamps are the bar's CLOSE time, timezone-aware, UTC.** This is the
   convention the whole engine depends on.
3. **No future data is reachable.** The feed is a stream, not a table. Do not
   expose a method that returns the full series.

## The timestamp trap

This is the bug worth the most attention. Many exchange APIs stamp bars with
their **open** time. Binance klines, for example, return `openTime` first.

If you pass open time straight through, every signal fires one bar early and
your backtest gains a bar of hindsight on every trade. The result looks
excellent.

**Convert explicitly and comment it:**

```python
# Binance klines are open-stamped; the engine expects close-stamped bars.
close_time = open_time + interval
```

Then write a test asserting the first bar's timestamp equals the expected close
time. Not a comment — a test.

## Steps

1. **Fetch a sample and look at it by hand.** Before writing an adapter, print
   ten rows. Check the column meanings against the venue's documentation rather
   than guessing from names.
2. **Write the loader** in `data/`, following `CSVBarFeed`. Parse to `Decimal`
   directly from the string — never via `float`, which loses precision before
   you have a chance to keep it.
3. **Validate at the boundary.** `Bar.__post_init__` already rejects
   inconsistent OHLC. Let it. Do not catch and skip — a source producing
   invalid bars is a source you cannot trust for the valid-looking ones either.
4. **Run the `data-validator` subagent** on a sample file. It runs on a cheap
   model; there is no reason to skip it.
5. **Cache to disk, never to the repo.** `data/cache/` is gitignored. Raw
   market data does not belong in git.
6. **Write the tests**: ordering enforced, timestamps tz-aware and close-
   stamped, malformed rows rejected, symbols discovered correctly.

## Frequency and alignment

- Crypto trades continuously: no market-hours gaps, 365 days/year.
- Equities have sessions, holidays, and half-days. A "missing" bar may be
  correct.
- When combining sources, align on the **close** timestamp, and be explicit
  about what happens when one source has a bar the other does not. Forward-
  filling is a decision with consequences — make it deliberately.

## What not to do

- Do not interpolate or forward-fill inside the loader to "clean" the data.
  Gaps are information. Silently repaired data hides the problem it creates.
- Do not adjust prices for splits or dividends inside the feed without saying
  so loudly. Adjusted historical prices are a classic lookahead vector: the
  adjustment factor is computed from future events.
- Do not select a symbol universe from today's listings. That is survivorship
  bias baked into the data layer, where no strategy review will find it.
