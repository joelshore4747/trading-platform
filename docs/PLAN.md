# trading_platform — plan

**Status:** Phase 1 complete. Engine runs end to end, 47 tests green, mypy
strict clean.

**Step-by-step checklist:** `docs/ROADMAP.md` — 50 numbered steps with the
Claude Code configuration interleaved. This document holds the reasoning; that
one holds the order of work.

**Purpose:** a place to practise, test, and falsify systematic strategies across
crypto spot, perpetuals, and options — with equity options reachable later
without a rewrite.

**Non-purpose:** making money by accident. This repository cannot trade real
money and will not until every phase below is complete and deliberately signed
off.

---

## Design commitments

These are decided. Revisit them only with a reason, not a preference.

| Decision | Choice | Why |
|---|---|---|
| Money type | `Decimal` everywhere in core/engine/risk | Float error across thousands of fills makes a run impossible to reconcile |
| Bar timestamps | Close time, tz-aware UTC | Open-stamped bars silently grant a bar of hindsight |
| Execution timing | Fill on bar T+1 for a signal on bar T | The one property that makes results meaningful |
| Instrument model | One `Instrument` hierarchy, engine agnostic | Adding equity options is a subclass, not an engine change |
| Costs | Pessimistic by default; `ZERO_COST` for debugging only | A frictionless backtest is a plot of intentions |
| Risk | Enforced pre-trade in the order path, not advisory | A limit that only logs is worse than none — it creates false confidence |
| Live trading | Absent by design | Nothing to misconfigure while the engine is still being proven |

---

## Phase 1 — Core engine ✅ COMPLETE

Instrument-agnostic backtester with honest accounting.

- `core/` — `Decimal` types, instruments (spot, perpetual, option), validated
  events. Options already model strike, expiry, right, style, and multiplier,
  so Phase 3 and 4 add data and pricing, not new concepts.
- `data/` — feed protocol, CSV loader, deterministic synthetic generator,
  ordering enforcement.
- `engine/` — event loop with the one-bar delay, position accounting including
  flip-through-zero, execution simulation with spread/slippage/commission.
- `risk/` — order notional, position notional, gross leverage, orders-per-bar,
  drawdown halt.
- `analytics/` — return, CAGR, Sharpe, Sortino, max drawdown and duration,
  Calmar, win rate.
- `tests/` — 47 tests. `test_lookahead.py` is the load-bearing file.

**Deliberately deferred:** funding on perpetuals, partial fills, multi-symbol
portfolio marking across venues, Greeks.

---

## Phase 2 — Real data and a strategy that is honestly evaluated

**Status: data layer in progress.** Parsers and cache are done and tested;
funding settlement into the event loop and the walk-forward harness are not.

### Environment constraint (discovered 2026-08-19)

This project's Claude Code sandbox **cannot reach Binance**. The egress policy
returns 403 at CONNECT for `api.binance.com`, `fapi.binance.com`, and
`data.binance.vision`; `pypi.org` and `github.com` are allowed. So the code is
split accordingly:

- **Parsing is pure and offline** (`data/binance.py`) — fully tested here against
  fixtures taken verbatim from Binance's published doc examples. A test asserts
  the module imports nothing that opens a socket.
- **Fetching is a standalone script** (`scripts/fetch_binance.py`) that you run
  on your own machine. It is **unverified against the live API** and says so in
  its docstring. If a real response differs from the fixtures, that is the
  finding to bring back.

To validate the fetch path inside a session, the environment's network policy
would need `api.binance.com` and `data.binance.vision` allowlisted.

### Done

1. **Kline and funding parsers** — `data/binance.py`. Handles the three wire
   traps: `closeTime` being one millisecond early, the 2025 spot archive switch
   from milliseconds to microseconds, and the still-forming final bar.
   `1M` is rejected rather than approximated. Funding interval is derived from
   observed settlement gaps, never assumed — Binance moved selected perps from
   8-hourly to 4-hourly in October 2023.
2. **Parquet cache** — `data/cache.py`. Prices stored as strings so `Decimal`
   round-trips exactly at any scale; atomic writes; refuses empty, mixed-symbol,
   duplicated, or out-of-order series.
3. **Fetch script** — `scripts/fetch_binance.py`. Public endpoints only, no
   credentials, stdlib HTTP, honours `Retry-After` on 429 and treats 418 as a
   hard stop.

### Remaining

4. **Funding settlement in the event loop.** Design drafted: `Backtester` takes
   an optional `FundingSchedule`; settlements in the half-open interval
   `(previous_bar_ts, bar_ts]` are applied after fills and before the mark, so a
   position opened at this bar's open genuinely exists when funding settles.
   Funding is tracked separately from trading P&L, because a perp strategy that
   is really a funding carry trade should be visible as one. **Not yet
   implemented** — awaiting an independent review of the loop placement, since
   this is the only part that touches the ordering `tests/test_lookahead.py`
   guards.
5. **Walk-forward harness** — fit on window N, evaluate on N+1. Never report an
   in-sample number as a result.
6. **Run the gauntlet** on `SMACrossover` with real BTC data once you have
   fetched it: lookahead audit, strategy review, `/cost-check`, `/pre-mortem`.

**Expected outcome: the strategy does not work.** That is the correct result for
an SMA crossover net of costs, and confirming the platform reports it honestly is
worth more than a strategy that appears to work.

**Exit criterion:** a real-data backtest whose every number survives the checklist
in `.claude/skills/backtest-review/`.

## Phase 3 — Crypto options

Deribit-style options: coin-settled, European, multiplier 1.

1. **Options chain data** — Deribit historical, or a vendor. Chain data is
   larger and messier than spot; expect gaps and stale quotes.
2. **Pricing and Greeks** — Black-76 for European options, with an implied
   volatility solver. Vectorised in `analytics/`, but never precomputed across
   the full series (that is lookahead).
3. **Volatility surface** — per-expiry smile, interpolated. Be explicit about
   extrapolation beyond observed strikes: it is a modelling choice, not data.
4. **Multi-leg positions** — spreads, straddles, calendars as first-class
   objects so risk sees the net position, not the legs.
5. **Expiry and exercise** — cash settlement at expiry, intrinsic value
   booking, position removal.
6. **Options-aware risk** — net delta, vega, and gamma limits alongside the
   existing notional limits. A delta-neutral book can still be dangerously
   short vega.

**The trap to watch:** quoted IV surfaces are frequently unmarketable in size.
An options backtest that fills at mid is far more optimistic than a spot one.
Model a wide spread and prove the edge survives it.

---

## Phase 4 — Equity options

Everything from Phase 3 plus what makes equities different.

1. **Market-hours calendar** — sessions, holidays, half-days. Not a detail:
   an engine that assumes continuous time will silently mis-handle overnight
   gaps.
2. **OCC symbology** and chain construction.
3. **American exercise** — early assignment risk, dividend-driven early
   exercise on calls.
4. **Pin risk** at expiry.
5. **Corporate actions** — splits and dividends. **Adjusted historical prices
   are a lookahead vector**: the adjustment factor is derived from future
   events. Store unadjusted, adjust forward-only.
6. **Point-in-time universe** — the list of tradable symbols as it was on each
   date, not as it is today. This is where survivorship bias enters equities.

---

## Phase 5 — Paper trading

A live-shaped loop with no money in it.

1. **Broker protocol** — the interface a live adapter would implement.
2. **Paper broker** in `brokers/` — same interface, simulated fills against
   live market data.
3. **Live data feed** — websocket, with reconnection and gap detection. The
   backtest engine consumes bars; the paper loop must produce the same `Bar`
   objects so a strategy runs unchanged in both.
4. **State persistence** — positions survive a restart, and reconcile on boot.
5. **Divergence tracking** — run the same strategy in backtest and paper over
   the same period and compare. **Any divergence is a bug in one of them**, and
   finding out which is the entire point of this phase.

**Exit criterion:** 30 days of paper trading with no unexplained divergence
from the backtest.

---

## Phase 6 — Live (not scheduled)

Deliberately unplanned. Do not build toward this. When and if it is opened,
it needs at minimum: credential handling outside the repository, a hardware
kill switch, per-order and per-day loss limits enforced broker-side as well as
locally, full order audit logging, and a written procedure for what to do when
it misbehaves at 3am.

Nothing about Phases 1–5 should be shaped by anticipating this.

---

## How work gets done here

See `docs/AI_WORKFLOW.md` for model tiering and token economics, and
`CLAUDE.md` for the house rules. The short version:

- Start with `/session-brief`.
- New ideas go through `/new-strategy`, which gates on hypothesis before code.
- Nothing is believed until `lookahead-auditor`, `strategy-reviewer`, and
  `/cost-check` have all run.
- When a result looks good, `/pre-mortem` it before celebrating.
