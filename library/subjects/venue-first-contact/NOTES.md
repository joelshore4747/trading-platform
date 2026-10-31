# Venue first contact — what the wire actually said

**Status: Binance VERIFIED on the wire 2026-09-08. Alpaca, Polygon and FRED
still UNVERIFIED, and now waiting on credentials rather than on the network.**

This note exists so the next session does not re-establish reachability, and
does not repeat the mistake that made it necessary.

## The mistake worth reading first — VERIFIED

Every document in this repository said the network was the blocker. Twelve
files, `docs/RUNBOOK.md`, `harness/PROJECT_STATE.md`, `docs/ROADMAP.md` steps 49
and 50, ticket `T-524425f9` and six source docstrings, all carrying some form of:

> This sandbox's network policy answers `403 to CONNECT` for every market-data
> host. **No venue adapter can be verified here.**

That was true, and it was a fact about **one container**, recorded as though it
were a property of the project. It was load-bearing: roadmap steps 49 and 50
were both blocked on it, priority tiers P2 and P4 were "done offline" with
nothing left to prove offline, P6 was deferred behind step 50, and first contact
was scheduled for a VPS nobody had.

Measured on the owner's machine, 2026-09-08:

| host | | |
|---|---|---|
| `api.binance.com` | **200** | public, no key |
| `fapi.binance.com` | **200** | public, no key |
| `data.alpaca.markets` | **401** | reachable, needs a key |
| `api.polygon.io` | **401** | reachable, needs a key |
| `query1.finance.yahoo.com` | **429** | reachable, rate-limited |

**401 and 429 are answers from the host.** A VPS is what *continuous* running
needs — roadmap step 50's 30-day divergence count — and that is a different
requirement from first contact. See `harness/KNOWN_FAILURES.md`, "the
environment is not the project".

## Binance — VERIFIED on the wire

`scripts/fetch_binance.py` carried "⚠️ **This script has never been run against
the live Binance API**". It has now.

**Klines.** `BTCUSDT --interval 1d`, 10 daily bars. Every field compared against
an independent raw request to `/api/v3/klines`:

- every OHLCV value equal as `Decimal`, **with the exponent preserved** —
  `62887.88000000`, not `62887.88`, because the second loses the fact that the
  venue quoted to eight places
- every timestamp equal to the wire's `closeTime + 1ms`, i.e. **close-stamped**,
  tz-aware UTC. A bar opening 2026-08-01 is stamped 2026-08-02. This is the
  repository's convention (`docs/PLAN.md`: open-stamped bars silently grant a
  bar of hindsight) and the parser applies it correctly.

Note the fetch reports its progress in the API's frame ("through 2026-08-10")
and its result in the repository's ("2026-08-02 → 2026-08-11"). Both are
correct; reading them as the same frame would look like an off-by-one and is
not.

**Funding.** `--market um --funding`, 12 settlements. Identical to the wire
field for field. The 8h interval the script prints is **derived from the
observed settlement times, not assumed** — the wire confirms settlements at
00/08/16 UTC. `fundingTime` values carry ~1ms jitter (they end in `001`), so an
interval computed by exact equality would find none; the script rounds, which is
why the derived value is right.

**No key is needed for either.** Anyone can repeat this in a minute, which is
the reason to start here rather than with a vendor that needs a signup.

## Alpaca — PARSER VERIFIED, transport still UNVERIFIED

`/v1beta3/crypto/us/bars` needs **no credentials**, so half the adapter was checkable
before any key existed. Wire shape is exactly what `data/alpaca.py` assumes: `t o h l c v`
plus `n` and `vw`, prices as JSON floats. `Decimal(str(x))` exact, OHLC consistent, both
`AlpacaParseError` guards fire, and every bar is stamped at wire-`t` **plus** the timeframe —
wire `2026-09-01T00:00:00Z` becomes `2026-09-02T00:00:00+00:00`, so defect D01 does not recur
on real data.

Still unverified, and needing a key: `live/alpaca.py` entirely — auth headers, the pagination
loop (a 5-row page already returns a non-null `next_page_token`, so paging is not
hypothetical), the poller that withholds the bar in progress — and the stocks endpoint, whose
`feed` and `adjustment` parameters have no crypto equivalent. `live/alpaca.py:172` calls
`require_all()` unconditionally, which is why the transport could not be exercised on the
public path.

## Polygon — VERIFIED, parser and transport, and three surprises

The adapter was right about everything it claimed. What it could not know was the shape of a
particular **account plan**, and all three findings are of that kind.

**A daily US equity stamp is midnight exchange-local and moves with DST.** Across the
2026-03-08 transition: `05:00Z` before, `04:00Z` after. The stamp trails the session's real
16:00 ET close by ~8 hours. Not a lookahead risk — always later, never earlier — but a daily
equity bar is not the same window as a 24h crypto bar, and code assuming a constant UTC
offset is wrong twice a year. 1m bars have none of this: exactly 60s apart.

**The free tier refuses recent data — 403.** A window ending 5 minutes ago is refused; the
same request ending on a historical date returns 573 bars. This is fatal to live capture as
written, because `CaptureConfig.poll_once` always requests `[since, now]` with no way to lag
the end. Every poll 403s, the error is counted, `consecutive_errors` climbs, the health check
correctly ALARMs — and the capture records nothing, forever. `T-73a2d1bc`.

**The free tier allows ~5 requests a minute — 429.** `config/collector.example.toml` asks for
8 across its two sources, so the documented first run rate-limits itself on the documented
free tier. `T-d2d4e55a`.

**The lesson worth carrying:** a key that is *present* is not a key that *works*, and a key
that works is not a plan that serves what you need. The key used here was written into the
env file three times by a duplicated paste — one line, correct permissions, and
`Credential.from_env(...).is_present` returned `True`. Only the venue disagreed
(`Unknown API Key`). Check credentials at the wire, never at the file.

## What this does NOT establish — the part that matters

- **Not that perpetual results are now funded.** `T-2ccfc6c0` (P1) stands. The
  vendor archive (`historical.zip`, 824k bars) still carries no funding, so
  every perpetual figure produced from it still understates the cost of holding.
  A funding series is now *obtainable*; nothing yet feeds one into a backtest,
  and `T-33d481b6` (no `FUNDING` journal event) is the other half.
- **Not that the other adapters work.** `live/alpaca.py`, `data/alpaca.py`,
  `live/polygon.py`, `data/polygon.py` and `data/fred.py` have still never met a
  real response. `T-524425f9` (P1) stays open until they do.
- **Not that anything has run continuously.** Step 50's count is still 0 of 30.

## For whoever does Alpaca next — UNKNOWN until it is run

The free tier serves the **IEX tape alone**, perhaps 2–3% of volume, not the
consolidated market. In `collector.toml` that means `venue = "iex"`, not
`"sip"`. The collector refuses the config that says otherwise, and the reason is
worth keeping: stamping the venue with the vendor made every Alpaca-versus-
Polygon gap read as *basis* — a market fact — so every genuine bad print became
a shrug. `docs/SETUP.md` §4 has the signup; use a **paper** key, since nothing
here routes orders.

Expect the first run to find disagreements between the published reference and
the wire. `docs/SETUP.md` says so outright: "Expect that to take a round of
fixes." That is the point of the exercise, not a setback — file each one with a
reproduction rather than patching it inline.
