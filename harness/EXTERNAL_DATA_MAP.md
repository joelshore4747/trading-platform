# EXTERNAL_DATA_MAP

| Source | Status | Evidence |
|---|---|---|
| Binance perpetual archive (`historical.zip`) | **USED.** 40 symbols, 1m, 2026-07-20→08-21, supplied by hand | committed file; `scripts/import_vendor.py` |
| Binance public klines/funding API | **VERIFIED ON THE WIRE 2026-09-08.** First contact from the owner's machine: 10 daily BTCUSDT klines and 12 funding settlements. Every OHLCV field and every close-stamped timestamp equal to the wire as `Decimal`, exponent preserved; the 8h funding interval the script derives is confirmed by the wire (settlements at 00/08/16 UTC, ~1ms jitter). No key needed. | this session; `data/cache/spot/BTCUSDT/1d.parquet`, `data/cache/um/BTCUSDT/funding.json` |
| Coin Metrics listing dates | vendored fixture (38 assets) for universe tests | `tests/fixtures/coinmetrics_listings.json` |
| Deribit | credentials declared in `.env.example`; **no code** | grep |
| Coinbase Exchange | **VERIFIED ON THE WIRE 2026-09-08**, parser and transport. 38 daily BTC-USD candles: 38/38 match the raw response field for field, with low/high/open/close each in the right slot. `data/coinbase.py` + `live/coinbase.py`, `scripts/fetch_coinbase.py`, and `vendor = "coinbase"` in the collector. No key — the candles endpoint is public and the client deliberately reads none. **Was recorded as 403/geoblocked earlier the same day; that was wrong** — urllib's default User-Agent is what got refused. Wire quirks, all differing from both other vendors: array is `[time, LOW, HIGH, OPEN, CLOSE, volume]` (not OHLC), time in **seconds**, rows **newest-first**, 350 per request, and the in-progress candle returned unflagged (T-aae4a1eb). **`BTC-USD` is not `BTCUSDT`** — dollars against Tether, stored verbatim so the reconciler never compares them. | this session |
| Kraken | **REACHABLE 2026-09-08, no key.** `api.kraken.com/0/public/OHLC` returns 200. No adapter. | this session |
| CoinGecko | **NOT USABLE as a price source.** `/coins/{id}/ohlc` returns `[ts, o, h, l, c]` with **no volume and no venue** — it is a cross-exchange aggregate, so there is no tape to name and `run_collector.py:134` refuses a source that declares none. Useful for reference data (listings, market cap), not for reconciliation. | this session |
| Alpaca (stocks IEX/SIP, crypto US) | **PARSER VERIFIED on the wire 2026-09-08; TRANSPORT still unverified.** `/v1beta3/crypto/us/bars` needs no credentials, so `data/alpaca.py` was checked against a real response: shape exactly as documented (`t o h l c v` plus `n`, `vw`), prices are JSON floats as assumed, `Decimal(str())` exact, OHLC consistent, and **every bar stamped at wire-`t` + timeframe** — wire `2026-09-01T00:00:00Z` renders as `2026-09-02T00:00:00+00:00`, so defect D01 (open-stamped bars granting a bar of hindsight) is not repeated on real data. Both parser guards fire. **STILL UNVERIFIED**: `live/alpaca.py` entirely (auth headers, the pagination loop — a 5-row page already returns a non-null `next_page_token` — and the poller that withholds the bar in progress), and the stocks endpoint with its `feed`/`adjustment` parameters. Both need a key. | this session; `data.alpaca.markets` 401 for stocks, 200 for crypto |
| Databento / IBKR (futures) | none | — |
| FRED / ALFRED (macro) | `data/fred.py` (parser), `store/macro.py` (vintages), `scripts/fetch_macro.py` | **UNVERIFIED** on the wire (403 here); stored by VINTAGE so a backtest reads what was known then; `harness/source-validation/fred.md` |
| Polygon | **VERIFIED ON THE WIRE 2026-09-08**, parser *and* transport, with a real free-tier key. Also serves **crypto** (`X:BTCUSD`, 200 on the same key) and **hourly/minute** aggregates; ticker reference covers the whole US market including microcaps, ADRs, warrants and bonds. **Indices are a separate product** — `I:SPX` returns 403 NOT_AUTHORIZED. 9 daily AAPL bars and 31 1m bars through `PolygonMarketData.fetch_symbol`: `adjusted=false` in the URL as intended, wire `t` is the window START and every bar is stamped at `t + timespan` (defect D01 not repeated), `Decimal(str(x))` exact on every field, OHLC consistent, 1m spacing exactly 60s with the closing-auction volume spike at 20:00Z. | this session. **Three things the reference did not say** — see the notes below |

## What the wire said and the reference did not — Polygon, VERIFIED 2026-09-08

**1. A daily US equity stamp is midnight exchange-local, and it moves with DST.**
Measured across the 2026-03-08 transition: bars before it are stamped `05:00Z`, after it
`04:00Z`. So the stamp is not a fixed UTC offset, and it trails the session's real 16:00 ET
close by about eight hours. **Not a lookahead risk** — the stamp is always later than the
information, never earlier — but a daily equity bar is *not* the same window as a 24h crypto
bar, and any code that assumes a constant offset is wrong twice a year. 1m bars carry none of
this ambiguity: exactly 60s apart, no midnight involved.

**2. The free tier refuses recent data.** A window ending 5 minutes ago returns **403**; the
identical request ending on a historical date returns 573 bars. This is the standard
delayed-data restriction, and it is fatal to live capture as the collector is currently
written: `CaptureConfig.poll_once` always requests `[since, now]` and has no way to lag the
window, so every poll 403s and the capture records nothing — visibly, with counted errors and
a correct ALARM, but permanently. Ticket `T-73a2d1bc`.

**3. The free tier allows about 5 requests a minute.** Seven in quick succession returned
**429**; one 75 seconds later succeeded. `config/collector.example.toml` asks for 8 a minute
across its two sources, so the documented first run rate-limits itself on the documented free
tier. Ticket `T-d2d4e55a`.

None of these is a defect in the parser, which was right about everything it claimed. They are
the difference between a published API reference and an account with a particular plan on it —
which is what first contact is for.

## Provider health (§74) — PARTIAL, durable since W2
`collect/capture.CaptureStats` counts per source: polls, fetched, recorded, bars withheld
as unclosed, repeats, errors, **consecutive** errors (a hiccup vs an outage), last poll and
last bar — the difference between those two being how far behind the market that source is.
Durable in Postgres via the `feed_health` view (migration 0002): `silent_for` per source and
`median_lag` over the last day. `live/stream.StreamStats` remains the in-memory seam for the
strategy-facing feed, which is a different path with different rules.

Alerting, since 2026-09-08: `scripts/check_health.py` exits non-zero on any ALARM, and
`deploy/trading-alert@.service` is what turns that into something a person sees — it logs at
error priority (`journalctl -p err`) and POSTs to `TRADING_PLATFORM_ALERT_WEBHOOK` when one
is set. Verified end to end by failing the health check on purpose and watching systemd
trigger the alert. Before that the `OnFailure=` line named a unit this repository does not
contain, so the numbers existed, nothing watched them, and the thing that was supposed to
watch them failed silently.

## Multi-source validation (§75) — PRESENT since W2, UNVERIFIED on the wire
`collect/reconcile.py` compares every pair of sources on each shared instant and records the
disagreements in `source_disagreements`, with the distinction that makes the table readable:
`same_venue = true` means the prices should have been identical and one is a bad print;
`same_venue = false` means the two describe different markets and the gap is the **basis**, a
market fact. The two are never summed. A source with no recorded venue is refused rather than
classified by guess.

Instants only one source covered are not counted as agreement — they cannot be compared. The
agreement rate is always reported with its denominator, and is `None`, not 1.0, when nothing
was compared.

**VERIFIED offline** (`tests/test_reconcile.py`, against a real PostgreSQL): two captures on
two threads with two connections land ten rows for five instants — five per source, never
merged — and reconcile to 4/5 stated as a number.
**UNVERIFIED**: that any real vendor's response parses, and that two real feeds agree in the
field. Both need a reachable venue; see the constraint below and `docs/RUNBOOK.md`.

## Constraint — SUPERSEDED 2026-09-08, and it was never a fact about the world

**What was true:** every market-data host answered `403 to CONNECT` **from the sandbox that
wrote this repository** (proxy log 2026-09-01). That is a fact about one container, and it
was recorded here as though it were a property of the project.

**What is true on the owner's machine**, measured 2026-09-08:

| Host | | |
|---|---|---|
| `api.binance.com` | **200** | public, no key |
| `fapi.binance.com` | **200** | public, no key |
| `data.alpaca.markets` | **401** | reachable, needs a key |
| `api.polygon.io` | **401** | reachable, needs a key |
| `query1.finance.yahoo.com` | **429** | reachable, rate-limited |

401 and 429 are answers from the host. **First contact is not gated on a VPS**; a VPS is
what continuous running needs, which is a different requirement, and roadmap step 50's
30-day divergence count is the thing that actually wants one.

What has not changed: an adapter that has not met a real response is unverified and must
say so. Binance now has met one. Alpaca, Polygon and FRED have not, and they are waiting on
credentials rather than on the network.

## Credentials — VERIFIED safe pattern
`core/credentials.py`: env-only, blank treated as absent, redacted `repr`/`str`, `require()`
names the missing variable. `.env` gitignored; PreToolUse hook blocks credential-shaped
writes; deny-list blocks reading `.env*`. Read-only scopes only, by policy.
