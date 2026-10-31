# 50-step roadmap

Every step to a working paper-trading platform, with the Claude Code
configuration interleaved where it belongs rather than kept in a separate
document. Configuration is not a side quest here — the agents, hooks, and
commands are what keep the platform honest, so they land next to the code they
guard.

`docs/PLAN.md` holds the *why* for each phase. This is the checklist.

| Marker | Meaning |
|---|---|
| ✅ | Done and verified |
| ▶ | Next up |
| ☐ | Not started |
| ◐ | Partially done |
| ⏸ | Deferred by decision, not blocked by work |
| 👤 | **Needs you** — I cannot do this one |
| ⚙ | Configuration step (Claude Code / tooling) rather than platform code |

**Current state: two branches merged 2026-09-07 into `main` as v0.0.1beta.**
1,307 tests with a live PostgreSQL (1,196 passed + 111 skipped without one),
mutation eval 26/28 killed with 2 declared survivors and **no undeclared
holes**, mypy --strict clean, ruff clean. The second survivor is new: R04's
defect is now prevented by construction, verified on the mutant. See the merge
note at step 35 for what the merge changed and what it found.

> **2026-08-27 — the perpetual retraction is resolved; every figure predating it
> stays retracted.** A derivative now reserves margin instead of paying its
> notional, so equity is cash plus marked positions for both instrument kinds.
> Re-running step 35 on the fixed ledger: **0 halted folds against 17**, **70
> out-of-sample fills against 1**, and the suspiciously constant 12.63 in-sample
> Sharpe replaced by figures varying from -9.26 to +5.00. The strategy's honest
> result is that its mean in-sample Sharpe is **negative** — it fails on the data
> it was fitted on. See steps 35a-c.

> **2026-08-27 — the defences are now measured, and the first run found a hole
> in the suite.** `evals/` reintroduces defects this repository has already had
> and asks two questions: does the test suite catch it, and does the auditor?
> Level 1 found L02 — a limit buy filling at the bar's low instead of
> `min(limit, open)` — surviving all 685 non-slow tests, because the guard for it was a
> one-sided bound. See ADR 0006 and the new out-of-sequence section below.

> **2026-08-23 — the risk layer was rebuilt after `risk-auditor` found seven
> gaps, two critical.** Every `SMACrossover` figure recorded below predates it
> and is superseded: they were measured through a layer that refused 99% of
> order intent. See step 9.

> **2026-08-20 — the generator was recalibrated and every earlier number moved.**
> See step 28. Figures recorded against the old generator are struck through
> below rather than deleted: a retracted result is still a result, and hiding
> the retraction would be the dishonesty this project is built against.

---

## Stage 0 — Engine core (steps 1–10) ✅ complete

The parts every later phase sits on. Instrument-agnostic from the start, so
options are a subclass rather than a rewrite.

1. ✅ **Project skeleton** — `pyproject.toml`, `uv` env, ruff + mypy strict, gitignore that excludes credentials and market data.
2. ✅ **Money types** — `core/types.py`. `Decimal` everywhere; float error across thousands of fills makes a run impossible to reconcile.
3. ✅ **Instrument hierarchy** — `core/instruments.py`. Spot, perpetual, option share one model. Options already carry strike/expiry/right/style/multiplier.
4. ✅ **Validated events** — `core/events.py`. `Bar`, `Order`, `Fill` reject impossible values at construction, so bad data fails loudly at the boundary.
5. ✅ **Feed protocol** — `data/feed.py`. Non-decreasing timestamps enforced, not trusted. Out-of-order data raises rather than sorting silently.
6. ✅ **Portfolio accounting** — `engine/portfolio.py`. Scale-in, partial close, and flip-through-zero — the last is the case implementations get wrong.
7. ✅ **Execution simulation** — `engine/execution.py`. Spread, slippage, commission charged by default. `ZERO_COST` exists only to isolate engine bugs.
8. ✅ **The event loop** — `engine/backtester.py`. Orders on bar T fill on bar T+1. Enforced by loop ordering, not a flag.
9. ✅ **Risk limits** — `risk/limits.py`. Order notional, position notional,
    gross leverage, orders-per-bar, drawdown halt.

    **Rebuilt 2026-08-23.** `risk-auditor` found seven gaps and every one had
    survived a green suite, because every existing risk test exercised **one
    limit against one order in isolation**. That test shape was the real
    defect; `tests/test_risk_composition.py` is the fix.

    - **R1, critical, and mine.** My earlier fix for the trapped-account bug
      returned `True` for any exposure-reducing order, skipping every other
      check. Since fills land next bar, each order in a batch was measured
      against the same unchanged position, so a halted account long 100 could
      be walked to **short 50** through sells each labelled "risk-reducing".
      The halt is meant to be the most restrictive state in the system; it was
      the least.
    - **R2, critical.** Position and leverage caps were defeated by splitting
      one order into several within a bar — five 1,000-notional orders reached
      5,000 against a 1,500 cap with **zero rejections logged**. Reachable
      position was 5× the stated cap. Fixed with per-timestamp pending deltas.
    - **R3.** Absolute currency caps froze at the starting cash and became an
      *equity gate*: all four permitted entries occurred while the account was
      below starting cash, and all 346 attempts once ahead were refused. The
      +62.51% was the return of a strategy allowed to trade only while losing.
      Caps are now a fraction of *current marked* equity, with absolute
      ceilings optional and the tighter bound applying.
    - **R4.** Leverage was silently disabled at equity ≤ 0 — a policy hole in
      the one state where it matters most. Now rejects with reason `insolvent`.
    - **R5.** The rate limit counted only *accepted* orders, so a runaway loop
      of invalid ones was never limited. Counts every order examined.
    - **R6.** `start_bar()` fired per bar *event*, so N symbols at one instant
      reset the counter N times: a 50-symbol universe turned a limit of 10 into
      500. Now gated on a timestamp watermark.
    - **R7.** Orders for a symbol with no price vanished untraced. Recorded.

    Plus `Strategy.on_reject` — a strategy resubmitting an identical order 346
    times was doing so only because nothing told it no — and a
    `BacktestResult.rejection_rate` that prints as loudly as the halt banner.

    **The composition tests then found an eighth defect, in the fix itself.**
    Equity-relative caps shrink with equity, so a position built at 100,000 of
    equity could exceed the cap it was measured against after a drawdown to
    20,000 — and every closing order was refused. The trapped-account bug
    arriving through the new units. Exposure-reducing orders are now exempt
    from size caps (they cannot overshoot past flat, so they can only shrink
    the book) while remaining subject to the rate limit.

    On the reproduction: **33 fills and 0 rejections**, where it was 7 fills
    and 346 refusals. The reported return moved from +62.51% to +95.58%, which
    is **not an improvement** — it is the first number that measures the
    strategy rather than the risk layer, and it is not trustworthy until the
    gauntlet re-runs.
9b. ✅ **The single-order flip through zero** — found 2026-08-27 by
    `risk-auditor` on an *unmutated* tree during the first agent-eval run, and
    verified independently.

    `_reduces_exposure` returns True whenever `abs(projected) < abs(current)`,
    which is satisfied by a flip that lands on a smaller opposite position. Such
    an order then takes the early return at `limits.py:204`, skipping every size
    cap, and passes the halt check because it is labelled risk-reducing.

    Reproduced: a **halted** account long 100 with `max_position_notional=1500`
    accepted a single sell of 130 at price 100 — **13,000 of notional, zero
    rejections logged** — landing short 30.

    This is R1's family. The pending-delta fix closed the path where a position
    was walked through zero by *slicing* one intent into several orders; the
    path where a **single** order does it is open. `target_notional` emits
    exactly one order for a rebalance, so any short-capable strategy reaches it
    without trying — the two shipped strategies are long-only, so no figure
    recorded here is affected, but `CrossSectionalMomentum`'s ranking shape is
    one sign change away.

    The code comment asserting safety is the part to fix first: *"a reducing
    order cannot overshoot past flat, so the largest one possible is a full
    flatten"*. A flip **is** an overshoot past flat, and the comment is why the
    early return looked safe.

    **Fixed 2026-09-02 — ADR 0008.** Refused in the risk layer (a sign
    change is never a reduction; exactly zero still is, so a full close while
    halted stays permitted), and split in `StrategyContext.target_notional`
    (a crossing target becomes a flatten plus an entry, and the entry is
    checked on its own). Four tests in `tests/test_risk_composition.py`
    including the 13,000-notional reproduction verbatim; corpus entry R03
    reintroduces the old line. No default changed.

10. ✅ **Performance metrics** — `analytics/metrics.py`. Return, CAGR, Sharpe, Sortino, max drawdown + duration, Calmar. Float only at this boundary.

---

## Stage 1 — AI harness (steps 11–18) ✅ complete

Built early on purpose. The reviewers should exist before there is anything
worth reviewing badly.

11. ⚙ ✅ **`CLAUDE.md`** — house rules loaded every session. Kept short because it is paid for on every turn.
12. ⚙ ✅ **Subagent roster** — six agents, tiered by stakes: `lookahead-auditor` and `risk-auditor` on Opus, `strategy-reviewer` and `test-writer` on Sonnet, `data-validator` on Haiku, `quant-researcher` on Opus.
13. ⚙ ✅ **Slash commands** — `/session-brief`, `/new-strategy`, `/deep-dive`, `/challenge`, `/pre-mortem`, `/cost-check`.
14. ⚙ ✅ **Skills** — `backtest-review`, `adding-a-data-source`. Loaded on demand, so detail costs nothing until relevant.
15. ⚙ ✅ **Hooks** — auto-format on write, block credential-shaped writes, refuse to end a session while lookahead guards fail.
16. ⚙ ✅ **Permissions** — allowlist for the routine toolchain; deny list for force push, hard reset, `rm -rf`, sudo, credential reads, and installing `ccxt`.
17. ⚙ ✅ **Documentation** — `PLAN.md`, `ARCHITECTURE.md`, `AI_WORKFLOW.md`, ADR 0001 on the fill-delay decision.
18. ⚙ ✅ **Milestone rule** — standing instruction to enter plan mode before each milestone, recorded in `CLAUDE.md` so it survives into future sessions.

---

## Stage 2 — Real data (steps 19–28) — in progress

Where synthetic prices are replaced by ones that actually happened.

19. ✅ **Binance parsers** — `data/binance.py`. Pure and network-free; a test asserts it opens no sockets.
20. ✅ **Trap: close-time off-by-one** — `closeTime` is `open + interval − 1ms`. Bars stamped at `open + interval`, computed rather than read.
21. ✅ **Trap: 2025 microsecond switch** — spot archives change units on 2025-01-01. Unnormalised, 2025 bars land ~50,000 years ahead and still sort monotonically.
22. ✅ **Trap: still-forming final bar** — dropped, with `now_ms` injected rather than read from the clock so it stays testable.
23. ✅ **Parquet cache** — `data/cache.py`. Prices as strings for exact `Decimal` round-trip; atomic writes; refuses poisoned series.
24. ✅ **Fetch script** — `scripts/fetch_binance.py`. Public endpoints only, no credentials, honours `Retry-After`, treats 418 as a hard stop.
25. ✅ **Real market data imported** — `historical.zip`, supplied directly
    rather than fetched: **40 Binance perpetual symbols, 1-minute bars,
    845,462 rows**. `data/vendor.py` loads it; `scripts/import_vendor.py`
    characterises and caches it. The sandbox still cannot reach any market data
    host (every one 403s at CONNECT), so `scripts/fetch_binance.py` remains
    **unverified against the live API**.

    Two properties decided the whole design, and both produce a plausible
    backtest rather than an error when got wrong.

    **The file is open-stamped; this repository stamps bars at close.** Loading
    it as-is would shift every bar one interval early — a silent one-bar
    lookahead, fatal at 1-minute frequency. The convention was *determined from
    the data*, not assumed: funding settles at 00:00/08:00/16:00 UTC and the
    trading burst lands in the minute starting at settlement. Median volume
    against baseline was **2.67x on bars stamped `:00`**, 2.00x on `:01`, and
    0.98x — normal — on `:59`. Under close-stamping the settlement minute would
    be labelled `:01`. It is labelled `:00`, so the file is open-stamped.

    **Coverage is 46%, and the gaps are not random.** 18 segments per symbol,
    longest contiguous run 2,900 bars (~2 days). Worse, the missing time is
    concentrated: hours 07-10 UTC average **56%** coverage against **25%** for
    18-23 UTC. That is a selection bias in the time dimension — the US
    afternoon and evening are under-sampled about two to one, so any intraday
    seasonality measured here is distorted and a strategy that bleeds in the US
    evening is under-penalised. `Segment` makes the holes structural, so no
    indicator can span one.

26. ✅ ⚙ **`data-validator` on the real file** — and it caught what a hand
    check had missed, by the exact method this project warns against.

    I had inspected BTCUSDT, found no zero-volume bars, and reported that as a
    property of the archive. **TONUSDT is a placeholder**: one distinct price
    (1.5901) across all 21,158 bars and zero volume throughout. QNTUSDT has 453
    zero-volume bars, CCUSDT 376. Generalising from one symbol is how a dead
    instrument reaches a backtest.

    It also found the symbols do **not share a timestamp grid** — bar counts run
    20,869 to 21,160 — so a cross-sectional strategy would trade a different
    universe every minute with nothing saying so.

    `screen()` and `align()` now handle both. Alignment is by **intersection,
    never forward-fill**: filling would fix the shape by inventing prices that
    never printed, and a strategy trading an invented price is trading a
    fiction. Intersecting costs **1.3%** of the data — 39 usable symbols on a
    common grid of 20,856 timestamps.

    One calibration lesson worth recording. The first stalled-feed check flagged
    any flat price run of 5+ bars and fired on **36 of 39 symbols**, BTCUSDT
    included — at 1-minute resolution a flat run is ordinary. A check that fires
    on everything says nothing. The real signature is a flat price **with zero
    volume**: a quiet market still prints trades, a broken collector does not.
    That flags 5 of 40, and they are the right five.

    The 2026-08-21 02:44 spike (XRPUSDT +21.97%, 1000PEPEUSDT +20.06%) is a
    **real market event, not a bad tick** — all 40 symbols print that minute and
    39 move the same way.

27. ✅ **Funding settlement in the event loop** — settlements in
    `(prev_bar_ts, bar_ts]` applied after fills, before the mark. Exactly once,
    never when flat, tracked apart from trading P&L. Gap-mistimed charges are
    counted. `test_lookahead.py` unedited and green.
28. ✅ **Synthetic data calibrated to documented BTC statistics** — five
    regimes, gap injection, funding with a mid-series cadence change,
    correlated multi-symbol series. Deterministic from a seed; a test asserts
    the module never reads the clock.

    **Revised 2026-08-20, and this is the most consequential correction in the
    project so far.** The first version was measured against published BTC
    figures (`data/calibration.py`, every constant sourced) and found to be
    generating a market nobody has ever traded:

    | Property | Old generator | Calibrated | Real BTC |
    |---|---|---|---|
    | `trend` annualised Sharpe | **6.36** | 0.98 | 0.3–1.2 |
    | `recovery` annualised Sharpe | **12.75** | 2.6 | — |
    | kurtosis | **2.82** | 4–8 | ≫ 3 |
    | vol clustering, ACF(r²) | **~0.00** | 0.12–0.18 | strongly positive |
    | `trend` annualised vol | 22.8% | 39% | 23–45% |

    Three defects, all premise-level. A **six- to thirteen-fold free lunch**, so
    anything holding long looked brilliant. Kurtosis *below* Gaussian — thinner
    tails than a normal distribution, in the asset whose fat tails are its
    defining feature, so tail behaviour was never tested. And i.i.d. returns, so
    no volatility clustering at all, leaving every strategy that implicitly
    assumes vol persistence untested.

    The engine is now GARCH(1,1) with standardised Student-t innovations and a
    Poisson jump component, and — the structural fix — **drift is derived from a
    stated target Sharpe rather than chosen by hand**, with a ceiling enforced
    in `RegimeParams.__post_init__` so the 6.36 cannot be reintroduced by
    editing a number. `tests/test_calibration.py` asserts the stylised facts
    hold, on medians across 30 seeds because a fat-tailed process has a genuinely
    wide sampling distribution and a single-seed assertion would be flaky.

    Two further bugs fell out of the work. **`Bar` did not reject a non-positive
    price** — a zero-price bar constructed cleanly and then divided by zero in a
    return calculation many bars later, exactly the failure mode
    `__post_init__` exists to prevent. And a long decline pinned the price at a
    constant floor, then manufactured fictional bounces off it: a 3000-bar crash
    spent 2194 bars at 0.01 and reported 219% annualised volatility that was
    pure clamping artefact. The generator now bounds the *return* and refuses a
    decline no market has ever made.

28b. ✅ **Cost model corrected to the published fee schedule.** `CostModel()`
    defaulted to 5 bp commission; Binance VIP 0 spot is **0.10% = 10 bp**. The
    "realistic" model had been optimistic by 2× for a retail spot trader. Added
    `BINANCE_SPOT_VIP0`, `BINANCE_SPOT_BNB`, `BINANCE_PERP_VIP0`, and
    `RETAIL_STRESSED` so a backtest states which venue and tier it assumed
    instead of implying one.

### Out of sequence — quote currency (ADR 0002)

Not on the original fifty, and found while planning the next ten. Every
`Instrument` carried a `quote_currency` that nothing read, so 39 of the 40
imported symbols — all USDT-quoted perpetuals — had their figures reported as
USD by assumption.

Measured on the real 32-day BTCUSDT sample with a simulated depeg to $0.8789:
**total return is exactly unchanged** when the depeg recovers inside the
sample, while annualised volatility goes from 20.07% to 54.61%. The metric
quoted first is the one metric blind to it. A depeg that does not recover moves
terminal wealth by 14.48 percentage points on buy-and-hold.

`core/fx.py` now refuses rather than assuming: unknown asset, pre-series
instant, and stale observation all raise, lookup steps back and never forward,
and `assume_pegged()` makes asserting the peg something a reviewer can see in a
diff. `scripts/measure_depeg.py` regenerates the tables in the ADR.

Deliberately not done: a multi-currency portfolio. The cash balance is still
single-currency — what needed fixing was the pretence that it was USD.

### Out of sequence — the data plane actually connected

Planned as "partitioned storage and windowed reads". Re-examining the premise
first, as a milestone requires, found the plan aimed at the wrong thing twice.

**Nothing in `src/` imported `data/cache.py`.** One script wrote the cache, one
script wrote a *second* layout nothing read at all, and every executable path —
examples, UI, walk-forward harness — was fed by the synthetic generator. **No
backtest had ever run on real data through package code.** `screen()` and
`align()` existed and were tested but no writer called them, so the on-disk
universe still contained TONUSDT, which those same checks had found dead.

The measurements were also wrong. Decode is **8.3 µs/bar, not the 81 recorded**
— and Parquet is 5% of it, so partitioning and pushdown would have optimised a
twentieth of the cost of a component with no readers. Half the cost was
`read_bars`' own loop.

What landed:

- `data/cache.py` — `CachedBarFeed`, the first reader in `src/`. `cache_path()`
  is now the only sanctioned path constructor, validated against a market
  vocabulary and the interval table, with a `_layout.json` version marker; a
  test asserts nothing else builds one by hand, and that test was checked
  against a deliberate offender. `read_bars` bisects instead of comparing per
  row: **8.30 → 4.60 µs/bar**, and a one-month window costs 1.62x its ideal
  per-bar rate rather than 3.50x.
- `data/panel.py` — the columnar cross-section. A `Bar` costs 664 bytes; a
  panel cell costs **8.0**. Five thousand symbols x a year of daily bars is
  1.21 GB as objects and **14.6 MB** as a panel, loaded in 10.8 s with +18 MB
  of peak RSS. Object-per-bar cannot hold a cross-section at any resolution
  worth ranking on, so this is not an optimisation.
- `data/intervals.py` — one vocabulary where there were five. This exposed a
  real silent-corruption path: `load_zip` stripped the interval from each
  filename, discarded it, and applied one caller-supplied interval to every
  member, so a mixed archive shifted every bar of the odd symbol out by the
  difference. It now raises.
- `scripts/import_vendor.py` writes through `cache_path` with screening wired
  in. TONUSDT no longer reaches disk.

No new dependency: `pyarrow.compute` does the cross-sectional work within 2x of
numpy, and pyarrow was already declared.

Then `lookahead-auditor` found a bug I had just pushed, and it was the same
failure pattern I had fixed two commits earlier. **Every windowed `Panel` read
prices from the start of the file.** The row offset was re-derived by bisecting
an already-sliced array, so it was always zero, and `take` applied
window-relative indices to the full column. The indices stayed in range, so
nothing raised. My tests checked a windowed panel's *grid* and an unwindowed
panel's *values*, and never a windowed panel's values — a guard that could not
fail. Fixed, and the agreement test is now parametrised over six windows,
verified by reintroducing the bug and watching four cases fail.

It also named three things the rectangle was hiding, all now reported rather
than repaired: an intersection deletes rows from *before* a symbol dies
(`rows_removed_by`, `survivorship_warning()`), it creates holes present in
neither input so adjacent rows can be weeks apart (`gaps`, `is_contiguous` —
18 of them on the real archive, largest 8d13h), and screening on whole-file
statistics is survivorship. The default screen is now `is_dead` only, with the
zero-volume threshold opt-in, and the importer says so in its own output.

And the guard that mattered most was in the wrong place: `load_zip` refused an
interval the *filename* contradicted, which is the case the caller already
knows, while `load_bars` read every raw stamp and threw them away. The modal
gap between stamps IS the interval. Now checked, and the message distinguishes
under-declaring (a lookahead) from over-declaring (merely late).

`data-validator` returned DO NOT USE on the imported cache, claiming it was
open-stamped. **That was wrong** — it compared the cache against the CSV's
second row rather than its first. Verified independently: all 824,304 bars
across all 39 symbols are shifted open-to-close by exactly one minute. Its gap
analysis was the useful part: the holes are synchronised collector outages, not
per-symbol, so the 39-way intersection retains **98.7%** — but that is only
**14.5 days** of full cross-section, in 18 segments.

### Out of sequence — point-in-time universe (step 48, brought forward)

`docs/PLAN.md:155-159` and step 48 below both file this under the last stage,
with equity corporate actions. Bringing it forward is a choice, and the reason
is that the data plane landed without any notion of *which* instruments a run
should consider — and `CLAUDE.md` names "a symbol universe chosen because those
symbols exist today" as a form of lookahead with no mechanism behind it.
`.claude/commands/challenge.md` asks a reviewer to confirm "the data is
point-in-time", which was unanswerable from this repository.

`data/universe.py` turns on one distinction: **observed absence is not
delisting.** From bars alone a delisting, a collector outage and "the archive
ends here" are indistinguishable, so a `Listing` carries observed bounds and
asserted dates as separate fields and a query reports which it used.

A design review killed my first draft, correctly. It had `members(as_of)` fall
back to observed bounds when asserted ones were missing — which is exactly the
silent default-to-1.0 that `core/fx.py` exists to forbid, and it would have been
taken on every call, because every registry buildable here has all-`None`
asserted dates. Worse, it **fabricates events**: all 39 symbols share a first
bar because that is where collection started, and reading that as 39
simultaneous listings asserts the universe was unlisted the day before. So
bounds at the archive's own edge are **censored**, and `members()` raises.
`assume_observed_bounds()` is the `assume_pegged()` of this module — and it does
not rewrite the dates as asserted, so the warning survives the assumption.

Verified against real listing dates (Coin Metrics, 38 assets, vendored as a
fixture): the universe grows **1 asset in 2011 to 38 in 2024**, ETH absent
before its real 2015-08-08 date. And one correction to my own claim that no
delistings were obtainable — **MKR's reference rate ends 2026-03-25** while
everything else runs to 2026-05-23, so the since-delisted path is exercised too.

Also landed, because a registry with no consumer is the `Panel` mistake again:

- **`Panel` gained a union grid.** An intersection handed a symbol that listed
  mid-window deletes every earlier row *for every other symbol* — silently
  undoing the point-in-time selection that produced it, and firing
  `survivorship_warning()` on data that is right. `grid="union"` keeps the rows
  with a validity mask; `rank_row`/`zscore_row` **refuse** there rather than
  deciding what a missing symbol's rank is. `retained` is `None` on a union,
  where the ratio would read 1.0 exactly when data is most missing.
- **`CachedBarFeed.from_registry`** emits only bars inside each symbol's
  membership. Without that it would be `list(members)` with extra steps.
- **`Panel.from_bars` reported less than `Panel.load`** — my defect from the
  previous step. It left `rows_removed_by` and `gaps` empty, so a panel built
  from a dead symbol plus a 20-day hole claimed `is_contiguous=True`. A parity
  test now pins the two constructors together.
- **An instrument filed under a key that is not its own symbol** now raises.
  `Portfolio.position()` binds by `instrument.symbol` while everything else
  keys by the dict key, so a Spot under a perpetual's key marked **1,100
  instead of 100** — full notional where unrealised PnL belongs, silently.
- **`stale_marks` on `BacktestResult`.** A symbol that stops printing is
  carried at its last close into every subsequent equity point. Measured: only
  **0.12%** of marks on the real archive are stale, because that collector's
  outages are synchronised, worst case **8 days 13 hours**. Proportionate to
  that, it is disclosed beside `funding_in_gaps` rather than redesigned around.

`examples/run_universe.py` is the first example in the repository that runs on
real data rather than the synthetic generator.

Then `lookahead-auditor` found four leaks, and **the worst was survivorship
bias inside the function written to prevent it.** `members_between` unioned
membership at the two window endpoints, so a symbol that listed after the start
and delisted before the end was a member at neither and was dropped from the
resolved universe entirely — selecting precisely for names alive at the window
edges, with no warning, producing a better-looking result. It is interval
overlap now.

Three more from the same audit:

- **`_bounds` was keyed by bare symbol** while `Listing.key` is
  `(market, symbol, interval)`, so a registry holding spot and perpetual
  BTCUSDT collapsed to whichever came last in the tuple — the answer flipped on
  ordering, making the perpetual a member fifteen months before it existed. And
  the test named `test_listings_are_keyed_by_market_symbol_and_interval`
  asserted only that construction did not raise, which it never did: **the
  third guard-that-cannot-fail this week.** Two markets quoting one symbol are
  now refused, with `for_market()` to slice.
- **An asserted `delisted` with no asserted `listed` was discarded** —
  all-or-nothing bounds threw away the strongest statement anyone can make, and
  the symbol fell back to observed bars running past its stated end. A zombie,
  the mirror of survivorship. The ends are graded independently now.
- **Censoring compared against archive-wide edges** that asserted listings also
  widened, so a long-history symbol pushed the boundary past a second import's
  window and every symbol in it read as OBSERVED — its collection boundaries
  fabricating listing events. Edges come from unasserted listings only.

Plus two smaller ones: `Basis.CENSORED` was truthy, so `if is_member(...)` read
a non-answer as a yes; and a union grid's `is_contiguous` was True over a
symbol's own three-day outage, because `gaps` is grid-level and a union axis is
regular whenever *any* symbol printed. `missing_by` and `is_complete` now carry
that.

`is_member` stays O(1) — measured flat at ~140 ns from 10 to 10,000 listings —
so membership filtering cannot make the event loop quadratic.

### Out of sequence — the false-discovery harness (ADR 0003)

The check nothing had ever run: put data with **no predictability by
construction** through the whole pipeline and count how often it claims an
edge. Until that number is small, no other number here means anything.

Three things had to be fixed before the measurement was even possible.
`Regime.CHOP` declares zero Sharpe but carries `mean_reversion=0.18` and
measured **ACF(1) = −0.156** — exploitable, so a "false discovery" there would
have been a true one. `Regime.FLAT` is unpredictable but runs at 5% vol, so
nothing trades. And the first attempt returned **0% discovery with 85%
UNDERPOWERED**: the strategy barely traded and the pipeline was never tested.

**The finding: the false-discovery rate is governed by how much out-of-sample
evidence a verdict rests on — and NOT by how finely that evidence is split into
folds.** At 100 trials per row, white noise, realistic costs:

| out-of-sample bars | rate | 95% upper |
|-------------------:|-----:|----------:|
| 300 | 7.0% | 12.7% |
| 900 | 8.0% | 14.0% |
| 1,800 | 4.0% | 8.9% |
| **4,800** | **1.0%** | **4.7%** |

A first version of this claimed the driver was fold count, on a sweep that grew
the series with `test_size` fixed — moving both variables at once. Holding the
series at 5,200 bars and varying `test_size` instead, so all rows share the
same 4,800 out-of-sample bars, gives **1.0% at sixteen folds, 0.0% at eight,
1.0% at four and 1.0% at two.** Fold count makes no difference; splitting the
same data more finely does not buy a more trustworthy verdict. The fold claim
is withdrawn in ADR 0003.

Below roughly a thousand out-of-sample bars the rate is 7-8%, twice the target,
while looking exactly like a result — a 12-seed run there returned zero
discoveries and read as a clean pass.

**And it is blind in the other direction.** Reported as the maximum annualised
Sharpe an optimal forecast could extract: an edge worth **Sharpe 4.93 is
discovered 5% of the time**, 7.14 twenty percent, 18.73 always. The detection
floor sits *above* `MAX_REGIME_SHARPE = 3.5`, the ceiling this project's own
generator refuses to exceed on the grounds that more "hands every strategy an
edge that does not exist".

Also corrected: **ten seeds cannot demonstrate a 5% rate.** Zero discoveries in
ten trials is consistent with a true rate of 25.9%; sixty is the fewest that
can support the claim, and the earlier plan asked for ten. The harness reports
the interval rather than the point estimate and refuses below sixty.

Guarded by three mutants that must each make it fail — lookahead, a relaxed
threshold, and one that never discovers anything (the power side). The
lookahead mutant runs the genuine pipeline with one line changed, `evaluate`
receiving a `TestWindow` built from the fold's *training* bars, and drives the
rate from **5% to 80%**. It shows the `TrainWindow`/`TestWindow` type split is
a **speed bump, not a proof**: `TestWindow(bars=fold.train.bars)` type-checks
perfectly.

An `overfitting-auditor` pass then found three things, two of them serious:

- **The lookahead mutant was a stub returning a constant `SURVIVES`** — which
  is exactly the mutant the ADR rejects as too easy, wearing a docstring about
  a type split it never exercised. The fourth guard-that-cannot-fail this
  week, inside the module built to catch them. Replaced with the real leak.
- **`Regime.NULL` was never used by the measurement it exists for.** Built,
  validated, documented — and both the script and the tests defined their own
  local Gaussian series instead, while the regime's own comment warned that a
  Gaussian null "would understate the false-discovery rate". Both arms run
  now, and they **agree at 1/100** — a useful negative result.
- **`judge()` pooled fills across folds.** Each fold is an independently
  simulated backtest starting flat, and with warm-up on, one fold's warm-up
  reaches into the previous fold's scored region — so FIFO pairing matched an
  entry from one simulation against an exit from another. At the default
  config it reported **19 trades and 130,849 net P&L against the correct 14
  and 19,756**, a 6.6x overstatement of a figure shown to the user. Paired per
  fold now.

Also softened: "the pipeline is blind below Sharpe 5" now names
`SMACrossover`, because without an oracle arm there is no way to separate a
conservative verdict layer from a weak detector.

`scripts/measure_null.py` regenerates every number; the gate runs at n=100
behind `@pytest.mark.slow` and is now a CI step.

### Out of sequence — measuring the harness (ADR 0006)

Not on the fifty, and the argument for it is the fifty's own history. Eight
agents, eight commands, five hooks, two skills — and apart from the prompt
guard's corpus, none of it had ever been measured. The prompt guard is the
exception that makes the case: writing its corpus found **four bugs in rules
that read perfectly well**, including the failure of *"make this strategy
profitable"* to match, which is the example the guard exists for.

Two questions, one corpus of defects that **actually happened here**.

**Level 1 — does the test suite catch it?** Free, deterministic, now a CI step.

| | outcome |
|---|---|
| killed | **6 of 8** |
| declared survivor | 1 (G01 — a weakened guard; no suite can catch a test that stopped checking) |
| **undeclared hole** | **1 (L02)** |

L02 substitutes `bar.low` for `min(limit, bar.open)` in `_resolve_price`, so a
limit buy fills at the best price the bar happened to print. **None of the 685 non-slow tests
objected.** The one that should have,
`test_limit_buy_fills_when_price_trades_through`, asserts
`price <= Decimal("96")` — a one-sided bound on a two-sided property, which a
fill at 90 satisfies comfortably. Its own message names the distinction it
cannot make. Closed by pinning the price exactly, plus the mirrored sell case.

**Level 2 — does the auditor catch it?** Opt-in, real money per run, never in
CI. Scored `flagged` / `localised` / `identified` separately, the last by a
judge shown the defect's `tell` but never the diff. Run `--blind` by default:
this document narrates most of the corpus, so a sighted run measures whether an
auditor can *read about* a bug rather than find it.

First run, blind, 8 mutants across four agents, **$36.10** including the two
re-runs that a 900-second ceiling had cut off:

| defect | agent | flagged | localised | identified |
|---|---|:-:|:-:|:-:|
| D01 stamp shift | lookahead-auditor | yes | yes | **yes** |
| L01 fill at close | lookahead-auditor | yes | yes | **yes** |
| L02 fill at bar low | lookahead-auditor | yes | yes | **yes** |
| L03 warm-up fills | overfitting-auditor | yes | yes | **yes** |
| R01 raw-position reduction | risk-auditor | yes | yes | **yes** |
| R02 insolvent boundary | risk-auditor | yes | yes | **yes** |
| S01 endpoint survivorship | lookahead-auditor | yes | yes | **yes** |
| G01 weakened guard | guard-auditor | yes | yes | **no**, then **yes** |

**7 of 8, then 8 of 8** — and the difference is the harness's own defect, which
is the most useful thing this run produced. See below.

Two arms of the measurement also held: `lookahead-auditor` returned
a genuinely **clean** verdict on an unmutated tree, so it is not simply flagging
everything; and the cross-judge — every report re-scored against a *different*
defect's `tell`, where the answer must always be no — came back **0 wrongly
identified**, so `identified` is measuring something rather than rubber-stamping.

**The miss was the harness's fault, and finding that out is what the harness is
for.** `guard-auditor` was given a `test_lookahead.py` whose headline assertions
had been replaced by `assert timestamp is not None`. It reported the file as
**"17/17 green"** and spent its report on unrelated failures in
`test_prompt_guard.py` — trusting a passing suite, the single thing its brief
forbids.

Those eight failures were **manufactured by the measurement**.
`prepare_for_agent` deleted `.claude/hooks` to stop the `SessionStart` hook
building a venv in a throwaway directory, and eight `test_the_hook_fails_open`
cases invoke `check-prompt.sh` on disk. So every auditor in the first run worked
in noise the harness created, with "the suite is green" removed as a usable
signal. Deleting was never necessary: dropping the `hooks` key from
`settings.json` is sufficient, since nothing fires a script the settings do not
name.

Re-run on the corrected tree, `guard-auditor` **identifies G01** — and does it
properly, noting that `Fill.timestamp` is non-optional so `is not None` holds
under every possible fill timing, that `Bar.__post_init__` rejects non-positive
prices so `> 0` holds under every possible fill price, and confirming with two
independent mutations. It also cost less: **$2.14 against $5.08**, having spent
no time on the manufactured failures.

One condition each, so this is a mechanism with evidence rather than a measured
effect — but the mechanism is visible in the agent's own report, and the
direction is not in doubt.

**The control arm found two live defects, and the scoring of them was wrong.**
Controls were labelled FALSE POSITIVE when flagged. But a control tree is not
clean code — it is *this repository*, which contains real defects:

- **A fifth guard-that-cannot-fail**, in `test_tearsheet.py`. The test named
  `test_the_underwater_panel_never_shows_a_positive_drawdown` never called
  `_underwater_panel`; it rebuilt the peak-and-depth loop inline and asserted on
  its own arithmetic. Flipping the sign in the real function left it green along
  with all 43 tests in the file. Fixed, and added to the corpus as **A01**.
- **A live risk gap.** `_reduces_exposure` treats any order landing on a
  *smaller* opposite position as risk-reducing, so a **single** order flipping
  long 100 to short 30 skips every size cap and passes the halt. Verified: a
  halted account with a 1,500 position cap accepted 13,000 of notional with
  **zero rejections logged**. R1's fix closed the multi-order slicing path; the
  single-order flip is open, and the code comment claims the opposite —
  *"a reducing order cannot overshoot past flat"*. A flip is exactly that.
  **Open — see step 9b below.**

Controls are now reported as needing adjudication rather than scored, and a
failed run no longer prints as clean.

**The harness found two defects in itself**, both the shape it exists to catch.
Excluding `evals/` from the mutant tree left `tests/test_evals.py` importing a
package that was gone, so every mutant died at collection — had `ERROR` been
scored as `KILLED`, it would have reported a flawless defence while measuring
nothing. And the first agent run withheld `Bash` while every auditor brief
demands proof by execution; the agent said so itself, unprompted. Mutants build
their own venv now, because the project's editable install points at the real
`src` and a linked one imports unmutated code.

Also landed: **`guard-auditor`** (the most repeated defect class here had no
reviewer), **`gauntlet`** and **`guard-sweep`** in `.claude/workflows/` — making
`AI_WORKFLOW.md` section 4 executable rather than described — and **`/harness`**.

What it does not establish is in ADR 0006 under its own heading: eight defects
this repository already had are, by construction, the ones its briefs are most
likely to anticipate. The number is a floor, not a rate, and the corpus earns
its keep as defects enter it that no brief foresaw.

---

## Stage 3 — Honest evaluation (steps 29–36)

The machinery that stops a backtest from flattering itself. This stage matters
more than any strategy that will ever run on it.

29. ✅ **Walk-forward harness** — `analytics/walkforward.py`. Fit on window N,
    evaluate once on N+1. Rolling or anchored. Fresh `Backtester` **and** fresh
    `RiskManager` per run, because peak-equity state would otherwise leak a
    drawdown halt from one fold into every later one.
30. ✅ **Parameter sweep** — reports the whole surface, never the peak alone:
    `profitable_fraction`, `spike_ratio`, and `neighbour_ratio` (best cell
    against its adjacent cells — a plateau is real, a spike is a coincidence).
    A grid where nothing traded raises rather than naming a winner out of a
    field of tied zeros.
31. ✅ **Out-of-sample split enforcement** — `analytics/windows.py`.
    `TrainWindow` and `TestWindow` are distinct types with no conversion;
    `sweep()` accepts only the first and `evaluate()` only the second, so
    fitting on test data is a `mypy --strict` error rather than a convention. A
    test runs mypy on a deliberately leaky snippet and fails if it type-checks.
    Windows are cut on timestamps, so a multi-symbol instant is never split
    across the boundary.
32. ✅ ⚙ **`overfitting-auditor` subagent** (Sonnet) — parameter count against
    sample size, sweep-surface sharpness, fills **per fold** rather than in
    total, parameter instability, and whether the reported window was ever
    fitted on.
33. ✅ ⚙ **`/walk-forward` command** — runs the harness, prints in-sample
    against out-of-sample per fold, then hands the result to
    `overfitting-auditor`.

    **What the stage actually found.** *(Figures below were measured on the
    pre-calibration generator and are superseded — see the revised numbers
    after this block. Kept because the reasoning still holds and a retracted
    result is still a result.)* `examples/run_walk_forward.py` runs
    `SMACrossover` on multi-regime synthetic data and reports the figure
    whatever it is. Rolling: efficiency **0.86**. Anchored: efficiency
    **1.86**, 100% of the grid profitable, 100% parameter stability. Both look
    like a pass and both are disqualified — 32 out-of-sample fills across six
    folds (median 5; one fold scored Sharpe 10.55 on a *single* fill), and
    buy-and-hold beaten in only 2 of 6 folds.

    On **pure chop**, where no edge exists by construction, efficiency came out
    at **1.24** — above 1.0 for a strategy with nothing to find. That is the
    stage's most useful result: with out-of-sample scores scattering around
    zero, their mean can beat a barely-positive in-sample mean, so *efficiency
    alone certifies noise*. The harness therefore reports a set of verdicts —
    efficiency, grid profitability, neighbour ratio, fills per fold, parameter
    stability, and a cost-free buy-and-hold benchmark — and a run must survive
    all of them. Even then the report says only that it "failed to falsify" an
    edge, never that it found one.

    **Re-measured 2026-08-20 on the calibrated generator at corrected costs.**
    `SMACrossover` got materially worse, which is the expected direction:

    | | Old generator, 5 bp | Calibrated, 10 bp |
    |---|---|---|
    | Efficiency (mixed, rolling) | 0.86 | **0.27** |
    | Parameter stability | 60% | **0%** (optimum moves every fold) |
    | Objections raised | 2 | **3** |
    | Pure-chop control | **1.24 — a false pass** | **0.18 — correctly fails** |

    Most of the old 0.86 was the free lunch. Note the control especially: on
    the old generator, pure noise scored 1.24 and only the surface and
    sample-size checks caught it. On a realistic market efficiency behaves
    sensibly — but the multi-verdict design stays, because it was right for a
    reason that has not gone away.
34. ✅ **Tearsheet output** — `analytics/tearsheet.py`, plus
    `analytics/trades.py` and `analytics/charts.py`.

    A tearsheet is a persuasion device: an equity curve rising left to right is
    the most convincing artefact in systematic trading and is exactly as
    convincing when the strategy does not work. So the design question was not
    which charts to draw but **what the page makes impossible to skip**. Four
    answers, all structural: the cost model is a *required* argument and prints
    in the header (`ZERO_COST` renders a banner saying the page is not
    evidence); walk-forward verdicts render **above** the equity curve, with a
    test asserting document order; cost-free buy-and-hold is always drawn on
    the same axis; and the equity axis is logarithmic by default.

    **Round-trip trades** came first, because the repository had no concept of
    a trade — only fills. FIFO pairing, reconciled against the portfolio's
    average-cost accounting (they partition the same units, so the totals must
    match exactly). The metric it exists for is `pnl_without_best_n`: on the
    SMACrossover run, total P&L falls from **51,206.72 to 0.00** when the single
    best trade is removed, because there was only one. That is the whole
    finding, and the page says it in words above the chart.

    Charts are hand-written SVG — no `matplotlib`. A PNG can only be asserted
    to exist; an SVG string can be tested for the out-of-sample shading, the
    benchmark series, and a known point's coordinate. Palette from the
    `dataviz` skill, validated in both light and dark (`validate_palette.js`,
    all six checks pass, worst CVD ΔE 24.7). Rendered and inspected, which
    caught two of the skill's own anti-patterns: a one-bar histogram (now a
    stat readout) and unreadable log-axis labels (now 1/2/5 rounded).
### Step 35 ran twice, on two branches, on two strategies

`claude/trading-platform-bih2rm` and `claude/review-yesterday-chat-hx7ays`
diverged on 2026-08-26 and both ran a full gauntlet before merging on
2026-09-07. Neither supersedes the other and neither is deleted: they tested
different strategies and reached different verdicts, and the charter's rule is
that a retracted result stays in the record. They are labelled **35-A** and
**35-B** rather than renumbered, because renumbering is how a lab notebook
starts lying about what happened when.

**The merge also settled a contradiction between them**, and the losing side
was this branch's. Both fixed the same defect — a derivative fill destroying
equity — and only 35-B's fix credits realised P&L. Measured on merge day:
open a perp at 100, mark at 150 (equity 100,050, correct), then *close* the
winner and equity falls back to 100,000. Every closed derivative trade booked
exactly zero, gains and losses alike. 35-B's `apply_fill` is what `main`
carries; see `engine/portfolio.py`.

---

## 35-A — the gauntlet on `SMACrossover` (branch bih2rm)

35. ✅ **The full gauntlet ran on `SMACrossover` with real BTC data — and the
    result it was given was fiction.** 41 agents across five lenses, each
    finding handed to an independent skeptic told to refute it: **27 confirmed,
    8 refuted.** Five agents including the pre-mortem died on a session limit,
    so the sweep is thorough but not complete. Full report in
    `.harness-reports/gauntlet-step35.json` (gitignored; regenerate with the
    `gauntlet` workflow).

    **The run reported efficiency 0.10, mean in-sample Sharpe 12.63, and ONE
    out-of-sample fill across 18 folds. Every one of those numbers is an
    artefact of step 35a below.** They are retracted, not merely doubted.

    Three of the five lenses converged independently on the same root cause,
    which is the strongest signal the harness has produced: `lookahead`,
    `risk`, `strategy` and `guards` each arrived at `Portfolio.apply_fill`
    from different directions.

    What the verification stage refuted matters as much. Eight findings died
    there — among them "a Sharpe of 0.00 means never traded and is averaged in
    as a measurement", which is plausible, was my own first reading, and is
    wrong. An audit nobody contradicts is an audit nobody checked.

35a. ✅ **Derivative cash accounting fixed with a real margin model.**

    `Fill.cash_impact` (`core/events.py:138`) debits `price * quantity *
    multiplier` for **every** instrument type, while `Portfolio._refresh` and
    `positions_value` mark a derivative at `unrealised_pnl` rather than
    `market_value` — the latter's own docstring says "the notional is not held
    in cash". Both halves cannot be true. There is no margin or collateral
    asset anywhere to reconcile them; the notional simply leaves equity while
    the position is open.

    Reproduced at an **unchanged price**, 1.46 contracts at 65,000 on 100,000
    starting cash:

    | instrument | cash | positions | equity |
    |---|---:|---:|---:|
    | perpetual | 5,100.00 | 0.00 | **5,100.00** |
    | spot | 5,100.00 | 94,900.00 | 100,000.00 |

    Opening a position at fair value moves equity by 95%.

    And end to end on the real archive — same strategy, same 4,000 bars, only
    the instrument class differing:

    | instrument | halted | fills | rejections | min equity |
    |---|:-:|---:|---:|---:|
    | perpetual | **True** | 2 | **355** | **4,786.61** |
    | spot | False | 7 | 0 | 99,815.25 |

    **This is the whole explanation for both anomalies in step 35.** The 95%
    collapse breaches `max_drawdown_pct=0.25` on the first fill, `halted`
    latches, and every later order is refused — so "one out-of-sample fill in
    eighteen folds" is not a strategy that declined to trade, it is one that
    was forbidden to. The constant ~12.6 in-sample Sharpe is the annualisation
    of a single round trip.

    **The reason it survived every green suite is an anti-guard.**
    `tests/test_portfolio.py:135` is named
    `test_derivative_equity_counts_unrealised_not_notional` — the correct
    property — and its body asserts `equity == 9910.00`, which is
    `10000 - 100 + 10`: the notional debited *and* only unrealised credited
    back. The correct value is `10010.00`. Its comment, "Cash paid 100",
    states the defect as intent. It does not merely fail to catch the bug; it
    **would reject the fix**. Sixth instance of this repository's most repeated
    defect, and the first that actively defends the thing it is named after.

    **Not fixed here, because the fix is a design decision.** "Debit only
    commission for derivatives" makes the arithmetic consistent, but a real
    perpetual reserves *initial margin*, and this repository has no margin
    concept at all. Choosing between them changes what leverage means
    throughout `risk/`, so it is a milestone under the `CLAUDE.md` rule rather
    than a patch.

35b. ✅ **The walk-forward harness now reports the halt it records.** `SweepCell.halted` and `FoldResult.halted_out_of_sample` are
    written (`walkforward.py:634`) and read by nothing in `src/`: `report()`
    never prints them and `verdicts()` never tests them. So 17 of 18 folds
    scored a halted account and the printed table said only "0 fills".
    `BacktestResult.rejection_rate` exists precisely to print "these figures
    describe the risk layer, not the strategy", and the walk-forward path never
    consults it. A fold with zero out-of-sample fills is currently averaged
    into `mean_out_of_sample` as a measured 0.00.

35c. ✅ **The annualisation guard now covers the metrics the sweep ranks on.** The bound added
    for step 35 covers `cagr` and `calmar_ratio` — and not `sharpe_ratio`,
    `sortino_ratio` or `annualised_volatility`, which are the numbers the sweep
    actually optimises on. `annualisation_is_meaningful` exists on
    `PerformanceSummary` and has no reader outside `metrics.py`. Same shape as
    35b: the signal is computed and nothing consults it.
**Resolution, 2026-08-27.** `Instrument.initial_margin_rate` defaults to
    `None` = fully collateralised, so leverage is opt-in and visible in a diff;
    margin is re-measured at the current price rather than fixed at entry;
    `Portfolio.margin_reserved` and `free_cash` are reported, and `free_cash` is
    allowed to go negative because an unfundable book is the state a reader most
    needs to see. `RiskLimits.max_margin_utilisation` carries its own pending
    accumulator so the R2 slicing defeat cannot be replayed against collateral.
    The model's cross-check is a test: a 1x derivative leaves exactly the free
    cash that buying the notional outright would. Corpus defect **P01**.

    The anti-guard was corrected rather than deleted — `10010.00`, the figure
    the gauntlet predicted before the change existed.

36. ☐ **Write up the result as an ADR** (now **0003**, since 0002 went to the
    quote-currency decision) — including, and especially, if it fails. Expect it
    to. A recorded negative result is the most reusable artefact this project
    produces.

---

## 35-B — the gauntlet on `CrossSectionalMomentum` (branch hx7ays)

35. ✅ **The full gauntlet, on real data** — and **not** on `SMACrossover`,
    which ADR 0005 had just shown cannot reach the gates at all: at two to
    three fills per fold the fill objection fires before any other gate speaks,
    so the result would have been a guaranteed `UNDERPOWERED` carrying no
    information. It ran on `CrossSectionalMomentum` against the real 37-symbol
    archive instead. Lookahead audit → strategy review → `/cost-check` →
    `/pre-mortem`, all four.

    **REJECTED on four objections.** Mean out-of-sample −5.78, 8% of the grid
    profitable, neighbour ratio −2.17, stability 31%. The cost ladder is the
    cleanest part: out-of-sample 1.13 at `ZERO_COST`, **0.12 at 1bp all-in**,
    −1.98 at 4bp, −5.78 at the VIP 0 default of 13bp. The edge crosses zero
    between one and four basis points — an order of magnitude below the
    cheapest cost anyone really pays.

    Three findings that outlive the strategy. A prediction of mine the data
    refused: coverage runs 58% at 08:00 UTC against 20% at 22:00, so I expected
    the frictionless edge to sit in the over-sampled hours; the twelve
    worst-covered hours produced *more* P&L than the twelve best. A full-series
    pass returns +17.10% and **one minute is 51% of it** — the real 08-21 02:44
    spike — which **no fold ever evaluated**, because 4.1% of the archive lies
    beyond the final test window. And the beats-benchmark gate never fired at
    all: `walk_forward` takes one `benchmark_symbol` and a 37-symbol ranker has
    no honest single-symbol answer, so `hold` read `n/a` in all seventeen folds.
36. ✅ **Written up as ADR 0006** — plus two corrections of mine it forced.
    Every run passed `crypto_spot` for 40 *perpetual* symbols, past
    `vendor.instrument_for`, whose docstring names that exact mistake; and I
    quoted one grid cell's −23.87% as though it characterised the strategy when
    seven of nine cells halt and the two the sweep actually chose return −0.02%
    and −7.57%.

---

### Out of sequence — the cross-sectional API (ADR 0005)

Brought forward because the fill gate made every single-symbol verdict
uninformative. `Strategy.on_cross_section` fires once per instant, after every
bar at that instant is observed: ranking from `on_bar` compares the current
symbol's close against every other symbol's *previous* close, and since bars
arrive sorted by `(timestamp, symbol)` the alphabetically-first symbol carries
an extra bar of information into every ranking, forever.

It inverted ADR 0004's headline. "The gate is closed at Sharpe 18.7" was
`SMACrossover`'s trade count, not the pipeline: a ranker over five symbols
gives 25 fills per 100-bar window against its two to three, and once the fill
gate stops binding the cascade discriminates — 0/5 survive a null, 4/5 survive
a strong edge. Detection turns on near **IC 0.05** while real cross-sectional
momentum runs 0.02–0.05.

### Out of sequence — two engine defects the audits found

**The engine could not backtest a perpetual at all.** `apply_fill` deducted the
full notional from cash while `positions_value` credited a derivative back as
unrealised P&L only — so entering a perp destroyed equity equal to the position
size: 100,000 to 8,302 in 400 instants, no market move, tripping a one-way
drawdown latch. `positions_value`'s docstring had said "the notional is not
held in cash" all along; only half the decision was implemented. The test that
encoded the bug asserted a 90 loss on a position that gained 10.

**Risk caps could be breached N times over.** `_pending_delta` is cleared when
the timestamp advances, which assumes an order submitted at T has filled by
T+1 — true only while the symbol prints every instant. With a sparsely-printing
symbol, 10 orders each sized to the cap reached 10x it with **zero rejections**,
on both `max_position_notional` and `max_gross_leverage`. The archive is 46%
covered with no shared grid, so that is the ordinary case there.

### Out of sequence — options costs before any pricer (ADR 0007)

Stage 5's ordering, pulled forward on its own logic. `CostModel` applies a rate
to the traded notional, and for an option that notional is the *premium*;
Deribit charges on the *underlying*, capped at 12.5% of premium. At BTC 30,000
and 55% IV that understates commission by 5x at the money and **125x twenty
percent out**, and understates the half-spread by a flat **500x** at every
strike — both flattering, both worst deep OTM where options strategies live.
A spot cost model now **refuses** an option rather than approximating it.

---

## Stage 4 — Engineering hygiene (steps 37–40)

Small, cheap, and they compound. Slotted here because Stage 3 is when the
repository starts having results worth protecting.

37. ✅ ⚙ **GitHub Actions CI** — `.github/workflows/ci.yml`. The Stop hook only
    protects sessions where it actually runs (it needs a built `.venv`, and it
    can be bypassed), so nothing stopped a red lookahead guard reaching
    `origin`. Runs the lookahead guards as their own step first so a failure is
    unmissable, then tests, mypy, ruff, the prompt-guard corpus, all three
    examples, and a check that no `.parquet`, `runs/`, or `.env` file is
    tracked. Every step is a command from `CLAUDE.md`, so a CI failure is always
    reproducible locally.
38. ✅ ⚙ **`SessionStart` hook** — `.claude/hooks/session-start.sh`. Builds
    `.venv` when absent (2s with `uv`) and reports branch, last commit,
    uncommitted count, and the roadmap's current state. The real motivation is
    subtler than convenience: **the Stop hook silently does nothing when
    `.venv` is missing**, so an unprepared session lost its most important
    guard without saying so. Synchronous on purpose — async starts the session
    sooner but races, and a guard that is sometimes present is worse than one
    that always is. Verified end to end against a venv-less copy: build, ruff,
    mypy, lookahead tests, and an idempotent re-run.
39. ☐ 👤 ⚙ **Add a research plugin** — `exa` **or** `tavily` (one, not both),
    to give `quant-researcher` real web access. Strategy literature moves;
    training data does not. **I cannot install this** — it needs a plugin
    install on your account. Run `/plugin` and pick one; `exa` is the better
    fit for paper-shaped sources.
40. ✅ ⚙ **Prompt hygiene: a guard for bad prompts, and an audit of the agent
    definitions.**

    **The guard** (`scripts/prompt_guard.py`, `.claude/hooks/check-prompt.sh`,
    `docs/PROMPTS.md`). Nothing caught a prompt whose honest answer differs from
    the answer it invites. *"Make this strategy profitable"* is perfectly clear;
    the only way to comply literally is to search until a good number appears.
    Nine cautions and four milestone reminders, the latter making the
    `CLAUDE.md` plan-mode rule mechanical rather than remembered. **It never
    blocks** — exit 2 on `UserPromptSubmit` erases what the human typed, and a
    guard that deletes your sentence is switched off within a day.

    What makes it a harness is the corpus: 39 bad prompts with expected rules
    and 27 legitimate ones that must fire nothing, the negatives each aimed at
    a rule that would naively catch them. Precision is the hard gate. Writing
    it caught four bugs in the rules, including the worst possible one — *"make
    this strategy profitable"* did not match, because the pattern allowed
    "the &lt;noun&gt;" but not "this &lt;noun&gt;".

    **The audit** found one instruction that would now actively cause harm:
    `risk-auditor` still said *"once halted, no subsequent order may pass"* —
    which was the trapped-account bug, since fixed. An agent following it would
    have flagged the correct behaviour as a defect. Also corrected:
    `lookahead-auditor` hunting pandas idioms after pandas was removed and not
    knowing about `analytics/windows.py`; `strategy-reviewer` predating trade
    concentration and the buy-and-hold benchmark; `test-writer` recommending
    the weaker synthetic generator; `overfitting-auditor` predating the
    efficiency-1.24-on-noise finding that came from using it.

---

## Stage 5 — Crypto options (steps 41–46)

Deribit-style: coin-settled, European, multiplier 1.

41. ☐ ⚙ **Plan-mode gate** — adding an instrument class is a milestone under the `CLAUDE.md` rule. Stop and re-derive before writing code.
42. ☐ **Options chain data** — larger and messier than spot. Expect gaps and stale quotes; the `adding-a-data-source` skill applies.
43. ☐ **Black-76 pricing + IV solver** — vectorised, but never precomputed across the full series. Precomputing indicators is lookahead wearing a hat.
44. ☐ **Volatility surface** — per-expiry smile with interpolation. Extrapolation beyond observed strikes is a modelling choice, not data, and must be labelled as one.
45. ☐ **Multi-leg positions** — spreads, straddles, calendars as first-class objects so risk sees the net position rather than the legs.
46. ☐ **Greeks-aware risk + `options-auditor` agent** (Opus) — net delta, vega, gamma limits. A delta-neutral book can still be dangerously short vega. **Quoted IV is frequently unmarketable in size — prove the edge survives a wide spread.**

---

## Stage 6 — Equity options (steps 47–48)

Everything from Stage 5, plus what makes equities different.

47. ☐ **Market calendar + OCC symbology + American exercise** — sessions, holidays, half-days; early assignment driven by dividends; pin risk at expiry. An engine assuming continuous time mis-handles overnight gaps silently.
48. ☐ **Point-in-time universe and unadjusted prices** — store unadjusted and adjust forward-only. **Adjusted historical prices are a lookahead vector**: the adjustment factor is derived from future events. This is where survivorship bias enters equities, below the level any strategy review would catch.

---

## Stage 7 — Paper trading (steps 49–50)

A live-shaped loop with no money in it.

49. ◐ **Broker protocol + paper broker + live feed** — *protocol and skeletons
    done* (`brokers/base.py`, `data/connectors.py`, `core/credentials.py`):
    env-only credential reads with redacted reprs, every adapter raising, and
    tests standing guard against a live implementation landing quietly. Binance
    declares no credentials because klines are public.

    **The data plane landed 2026-09-01** (`src/trading_platform/live/`), built
    vendor-agnostic and verifiable offline — which matters, because this
    sandbox's network policy answers `403 to CONNECT` for every market-data host,
    so no adapter can be verified here at all.

    - `stream.py` — `LiveFeed` bridges a venue's push to the engine's pull while
      keeping non-decreasing timestamps, the property the whole lookahead
      defence rests on. A bar arriving after its slot was emitted is **refused
      and counted**, never emitted backwards. It satisfies the same `DataFeed`
      protocol a file does, so a strategy cannot tell which it is running on —
      the condition for step 50 to compare one program in two settings rather
      than two programs.
    - `publish()` **records before it enqueues**, so a slow strategy loop costs
      decisions and never data. Every loss mode has a counter, because a feed
      silently dropping 3% of its bars looks exactly like a quiet market.
    - `recorder.py` — append-only JSONL per symbol per UTC day, because
      `write_bars` overwrites whole files and would rewrite the day on every
      bar. `compact()` folds a finished day into the Parquet cache through
      `write_bars`, whose ordering and duplicate checks are the audit the split
      exists to reach. A reconnect replaying identical bars is tolerated; one
      replaying *different* values raises rather than picking for you.
    - `journal.py` — the decision journal. Every event carries the account as it
      stood, because "nearest snapshot" is the approximation that makes a
      post-mortem argue with itself. **Refusals are records, not absences**:
      three fills and four hundred rejections is a different day from three
      fills, and keeping only fills makes them indistinguishable.
    - `session.py` — trading calendars. US holidays are *derived* (including
      Good Friday from Easter) so they cannot go stale; ad-hoc closures have no
      rule, so they are supplied, and a query outside the asserted range
      **raises**. `ALWAYS_OPEN` covers crypto.
    - `replay.py` — a capture replays through the ordinary `Backtester`. Reads
      the journal, not the compacted Parquet: comparing against the compacted
      file would compare the live run to a cleaned copy of its own input and
      credit the cleaning with the agreement.

    **The supervisor landed 2026-09-02** (`live/supervisor.py`, `live/accounts.py`).
    `PaperSupervisor` runs a strategy through the ordinary `Backtester` over any
    `DataFeed`, with the open account cycle's ledger passed in as the engine's
    portfolio — one set of books, not two that must agree. It journals every
    order (intent, before the risk layer), fill, rejection, halt and mark with
    the account as it stood; on boot it rebuilds the cycle from the journal's
    fills and **refuses to start** if the rebuilt cash disagrees with the cash
    the journal last recorded. A kill-switch file stops intent before the
    strategy is called; a daily loss limit halts against the day's opening
    equity; equity reaching zero fails the cycle (charter §28), which is frozen,
    and the next cycle opens with the same starting cash. A supervised session's
    fills replay bit-for-bit through a plain `Backtester` over the recorded bars
    (`test_a_live_session_replays_through_a_plain_backtester_exactly`) — step 50
    in miniature. Eight tests, then eleven more after the `adversarial-reviewer`
    refuted the first version (see `harness/KNOWN_FAILURES.md`, 2026-09-02).

    **The Alpaca adapter landed 2026-09-02, unverified.** `data/alpaca.py` is
    the network-free parser: Alpaca stamps bars at their *open*, so the close
    is computed (the D01 trap, pinned by the first test), and prices arrive
    as JSON floats, so the best precision is `Decimal(str(x))` — stated, not
    hidden. `live/alpaca.py` is the urllib client (stocks and crypto paths,
    IEX/SIP feed recorded as provenance, never adjusted, paged) and a poller
    that **withholds the bar in progress**: a bar is published only once its
    close time has passed, or the strategy would see a close that has not
    happened. `scripts/run_paper.py` is the VPS entrypoint; `docs/RUNBOOK.md`
    is the 3am procedure. Every market-data host answers 403 here, so the
    wire format is written from the API reference and pinned by tests; the
    first real response on the VPS is the verification, and until then no
    figure from this path is believed. **Nothing has run continuously anywhere yet.**

    Proven end to end: venue → `LiveFeed` → recorder → disk → replay →
    `Backtester` reproduces the live fills and ending equity **exactly**, with a
    control that replays under different parameters and requires divergence —
    an agreement is only evidence if disagreement was reachable.

    **Remaining:** the venue adapter (Alpaca paper first, pluggable behind the
    existing `Broker` protocol), simulated fills against the live stream, and
    the supervisor that reconciles positions on boot and halts on divergence
    rather than papering over it. None of it is verifiable in this sandbox.

    `live/` has no corpus entries in `evals/` yet, deliberately: the corpus
    holds defects that **actually happened**, and inventing one here would
    measure the reviewer against my imagination rather than against a bug that
    got past a real review.
50. ◐ **Divergence tracking** — run the same strategy in backtest and paper over the same period and compare. **Any divergence is a bug in one of them**, and finding out which is the entire point of the stage. Exit criterion: 30 days with no unexplained divergence.

    *Mechanism built 2026-09-02, days counted: 0.* `live/review.py` replays each
    day's recorded bars through a plain `Backtester` and compares fills
    (`live/replay.compare`), reported first in every daily review and only
    when the check is valid — one session that began flat. Replay identity
    holds per session, not across a restart. The 30-day count starts on the
    first real day on the VPS.

---

## Not on this list

**Live trading.** There is no step 51. `docs/PLAN.md` Phase 6 explains why it is
deliberately unplanned, and nothing in steps 1–50 should be shaped by
anticipating it.

## How to work the list

- Start each session with `/session-brief` — bounded token cost, then it stops.
- Steps marked ⚙ are cheap and compound; do not let them slide behind the
  platform work.
- Steps 27, 41, and 47 are milestones under the `CLAUDE.md` rule: enter plan
  mode first.
- Believe no result until `lookahead-auditor`, `strategy-reviewer`,
  `/cost-check`, and `/walk-forward` have all run. The checklist is in
  `.claude/skills/backtest-review/`.

## 2026-09-04 — ICT sweep reversal: REJECTED (E-016)

Workstream 6 built the ICT primitives (`strategy/patterns/`: swings, market
structure, fair value gaps, order blocks, liquidity sweeps, killzones) and one
strategy composed from them, then walk-forwarded it on the real BTCUSDT archive.

**0 of 18 folds profitable, and 0% of the average training grid profitable.**
Mean in-sample Sharpe -23.83; out of sample -31.37. The decisive figure is the
in-sample one: the strategy loses on the data it was *fitted on*, so there is no
edge to degrade and efficiency is undefined. This is not a failure to generalise,
it is nothing having been there.

The killzone filter did not carry the result either way — the winning cell used
it in 14 of 18 folds and did without it in 4, and both arms are uniformly
negative.

Not tuned, and will not be: tuning a strategy with no in-sample edge is fitting
noise by definition. The primitives are kept — they are pure, tested, and
`tests/test_patterns.py` walks the series bar by bar to prove none of them reads
a bar that has not printed, which is worth having regardless of what is built on
top. If ICT is revisited it needs a different mechanism and a pre-registered
hypothesis of its own, not different parameters.

Record: `harness/experiments/E-016-ict-sweep-reversal-on-real-btcusdt.toml`.

## 2026-09-04 — macro data, stored by vintage (W8)

FRED/ALFRED ingestion, and the two rules that make it safe to have.

**Every value is `(series, period, vintage, value)`.** Macro data is revised —
GDP for a quarter is published once, revised twice within three months, and
revised again in annual benchmarks. Storing only the current figure means a
backtest reads a number nobody had at the time, which is lookahead in its most
insidious form because it looks like ordinary data. `read_as_of` returns the most
recent vintage *available* at an instant, and availability is later than the
vintage date: a figure dated 2026-02-01 was released at some hour of that day, so
an unstamped value becomes available at the END of it.

**Collected freely, used only when named.** The external-data deferral is lifted
for collection and kept for use. A strategy may read only a series its
pre-registered `Trial` named in advance, enforced by the reader itself with no
argument that turns it off. With ~800k series available, a permissive default
would be a mining machine.

Nothing has been fetched: `api.stlouisfed.org` answers 403 here, so the parser
and the URL construction are pinned by tests and the transport is UNVERIFIED
until it runs somewhere with a network. `harness/source-validation/fred.md` is
the card.

## 2026-09-04 — the ML pipeline, measured on noise before anything else (W9, E-017)

Built: features that cannot see the future, open-to-open labels that say what
they measure, purged and embargoed splits, and two baselines whose job is to be
beaten. **Not built: a model.** `load_model` refuses anything else and states the
condition — a data condition, not an engineering one.

The measurement that makes the rest worth having: the whole pipeline run on a
driftless random walk, where there is nothing to find.

    mean edge over the majority-class floor   -0.00070
    spread across 30 trials                    0.02551
    folds with a positive edge               245/510 (48.0%)

And the detector was tested against a leak: a feature reading the label's own
forward window moved the edge from +0.03 to +0.31. An earlier version of that
check peeked one bar ahead while the label measured ten, the two were barely
related, and it nearly passed for the wrong reason — recorded because a leak
detector tested against a weak leak is a detector nobody has tested.

A clean null run is necessary and nowhere near sufficient for any result the
pipeline later produces.
