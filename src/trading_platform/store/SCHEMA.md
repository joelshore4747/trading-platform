# The store's data model

Written for someone — a person or an agent — meeting this database for the first
time. The schema also describes itself: every table and column carries a SQL
`COMMENT`, so this is readable from the database directly and cannot drift out of
sync with it:

```sql
SELECT c.relname AS table, a.attname AS column, d.description
FROM pg_description d
JOIN pg_class c ON c.oid = d.objoid
LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.objsubid
WHERE c.relnamespace = 'public'::regnamespace
ORDER BY c.relname, a.attnum;
```

## The one idea

Every fact in here is attributable. A row says which **source** claimed it,
which **run** fetched it, and when it landed. A number nobody can trace back to
a fetch is a number nobody should act on.

The corollary is the thing most likely to surprise you: **the same bar from two
feeds is two rows.** `bars` is keyed on `(source_id, market, symbol, interval,
ts)`, and nothing in this schema merges sources. If Alpaca and Polygon disagree
about BTC at 09:31, both numbers are kept and the disagreement is queryable.
That is the reason for running more than one feed at all — a single feed cannot
tell you it is wrong.

## Tables

### `sources` — where data comes from

One row per distinct origin. `name` is the stable identifier used in code
(`binance-um`, `alpaca-crypto`, `fred`), lower case, never renamed once rows
reference it. `kind` is one of `venue` (a trading venue's own feed), `vendor` (a
redistributor or archive), `government` (a public statistical series), or
`derived` (computed here). `venue` records which exchange the data *describes*
when that differs from who supplied it: a vendor redistributing Binance prices
has `venue = 'binance'`.

`ensure_source` refuses to change an existing source's kind or venue, because
those are part of what its rows mean and changing them would silently restate
every bar already attributed to it.

### `ingest_runs` — one row per fetch or capture session

Opened before any row is written and closed when it ends. `params` is the JSON
of what was asked for — symbols, interval, window, feed tier — and is meant to
be enough to repeat the run.

`status` tells you how it ended, and the useful state is the one that is not an
end: a run left at `running` with no `finished_at` **is a crash**, and is meant
to look like one rather than like a short read that completed. Find them with:

```sql
SELECT * FROM ingest_runs WHERE status = 'running' ORDER BY started_at DESC;
```

`heartbeat_at` and `stats` are what a long-running capture writes onto its own
open row every poll: when it last knew it was alive, and its live counters
(`polls`, `recorded`, `consecutive_errors`, `sink_failures`, …). Without them a
`running` row looks identical whether the process is capturing a bar a minute or
failing every single poll, because the counters were only ever printed at exit
by a process built to run for a month. `heartbeat_at` is NULL, never defaulted:
"has never reported" and "reported nothing" are different states and only one is
an alarm.

### `bars` — OHLCV

Close-stamped, timezone-aware. `ts` is the **close** time of the bar; the whole
engine assumes this, and a feed that stamps at the open must be shifted before
it gets here or every signal fires one bar early. That mistake has a corpus
entry (`evals/defects/D01`).

Prices and volume are `NUMERIC`, never floating point, so they round-trip to
Python `Decimal` exactly — including the exponent, because `100.5` and `100.50`
are the same number and different prices: the second says the venue quoted to
the cent.

`ingested_at` is when the row was written here, which is not when the bar
happened. The gap between `ts` and `ingested_at` is your capture latency.

Check constraints reject bars that cannot exist: a high below the open or close,
a low above them, a non-positive price, a negative volume. The same rules live
in `Bar.__post_init__`, deliberately in both places — bad data should be stopped
at whichever boundary it reaches first.

### `schema_migrations` — what has run

Version, name, and the sha256 of the migration as it was when it ran. A checksum
that no longer matches the file means the migration was edited after being
applied, so the schema in this database is not the one the repository describes.
`pending_migrations` raises on that rather than stacking a new migration on top
of a schema nobody can characterise.

## Views

### `source_coverage`

What each source has actually delivered: rows, first bar, last bar, and when it
last wrote. The first question to ask of this database:

```sql
SELECT * FROM source_coverage ORDER BY last_written DESC;
```

### `collector_health` — is it working *right now*

Every capture or fetch that has not finished, with the counters it last reported
and how long ago. This is the view to alarm on. `feed_health` answers "has
anything arrived", which a wedged process keeps satisfying from the bars it
wrote yesterday — for as long as the silence threshold, which is exactly when
you would want to know.

```sql
SELECT source, since_heartbeat, consecutive_errors, sink_failures, dropped
FROM collector_health;
```

`scripts/check_health.py` turns all of this into an exit code, because a page is
not an alert: nobody was watching it at 04:00 when the feed stopped.

### `leverage_acknowledgements` — permission to use leverage at all

The row `risk/leverage.py` looks for. An account may not hold an instrument above
1x without one for that account and that symbol. It states the maximum leverage
accepted, the liquidation price at it, and the cash lost on a 20% adverse move —
and the figures are **recomputed** when the row is written and again when it is
read, so a hand-edited row that makes a position look safer raises rather than
loads. Written once by a person; `acknowledged_by` is a name, and a blank one is
refused by a CHECK.

**Revoked, never deleted.** A partial unique index keeps one live row per
(account, symbol) while the revoked ones stay, because "who could take 10x on
this account in August" has to stay answerable in December. Revoking needs a
reason.

The stated liquidation price is an **upper bound** on the room available: a venue
liquidates at its maintenance margin, which is lower than initial margin and is
not modelled here, so real liquidation arrives sooner. ADR 0011.

### `account_marks` — the whole account, every mark

Not a summary. Cash, positions value, equity, realised and unrealised P&L, fees,
funding, exposure, margin used, free cash, gross leverage, peak equity, drawdown,
orders, fills and the position map.

`equity = cash + positions_value` is a **CHECK**, so a ledger that does not
balance cannot be stored at all — a stronger guarantee than a test, which runs
when someone runs it.

`free_cash` is allowed to go negative and that is deliberate: it means the book
requires more collateral than the account holds, which is precisely the state a
reader needs to see. Reported, never clamped.

`gross_leverage` is `exposure / equity` — what the account is *actually* running
at, as opposed to what its instruments permit.

One mark per (account, cycle, instant): a restart replaying re-marks instants it
already wrote, and a second row would double every aggregate over this table.

### `risk_rejections` — every refusal, with its reason

A rejection is a **result**, not an error: it is the limit working. "The strategy
wanted to breach a cap 400 times today" is a fact about the strategy and this is
where you count it. Append-only and deliberately not deduplicated — the same
limit refusing the same symbol twice at one instant happened twice, and
collapsing it would understate exactly the pressure this table measures. The
`risk_pressure` view aggregates it per account per day.

### `stage` on both tables

`research | simulation | paper | live`. It records which of the four stages
produced a row and grants nothing. A row that cannot say which stage produced it
will eventually be read as the wrong one, and a paper equity curve shown as a
live one is the specific mistake this repository is built against.
`SupervisorConfig` refuses `stage=LIVE` outright: every broker adapter raises, so
no session here can place a live order, and a row claiming otherwise would be the
first false thing in the record.

## What the writer guarantees, and what it does not

**A source cannot contradict itself, even under concurrency.** `write_bars`
checks the incoming batch against itself, then against what is stored, then —
after its insert — against what is stored *again*. The third check is the one
that matters: two collector processes can both be told "nothing is there",
and the first to commit wins the primary key while the second's
`ON CONFLICT DO NOTHING` quietly discards its value. Before that check existed,
299 of 300 concurrent contradictions were lost with no exception and a return
value indistinguishable from an ordinary replay. Now the second writer gets a
`BarConflict`. Corpus entries S02 and S03; tests in `tests/test_store.py` under
"concurrency".

**A run is not a transaction.** `ingest_run` groups batches for provenance, not
atomicity. A run that fails after three batches leaves those three batches in
`bars`, and its `rows_written` says so on the failure path as well as the
success one. If you need "all or nothing", that is one `write_bars` call — the
batch is the atomic unit.

**Precision is preserved downwards only.** NUMERIC has no positive exponent, so
`Decimal("1E+2")` would be stored and read back as `100`: equal as a number,
different as a quote. `write_bars` refuses a positive exponent rather than
normalising it, so the round trip stays a round trip. Everything with an
exponent of zero or less — `100.50`, `0.1000`, `12.25000000` — is preserved
exactly, trailing zeros included.

**Migrations are serialised.** `migrate()` takes an advisory lock, so N
collector processes starting together queue rather than racing on the bootstrap
table. It is safe to call on every start; that is the intent.

**One schema.** The migration state lives in `schema_migrations` in whatever
schema the connection's `search_path` points at. A DSN carrying
`options=-c search_path=...` therefore gets its own independent copy of
everything, and the checksum guard only guards the schema it was applied in.
Do not point two DSNs with different search paths at one database and expect
one store.

## Questions worth asking it

Where two sources disagree about the same instant:

```sql
SELECT a.symbol, a.ts, sa.name, a.close, sb.name, b.close,
       abs(a.close - b.close) / a.close AS relative_gap
FROM bars a
JOIN bars b ON b.market = a.market AND b.symbol = a.symbol
           AND b.interval = a.interval AND b.ts = a.ts AND b.source_id > a.source_id
JOIN sources sa ON sa.id = a.source_id
JOIN sources sb ON sb.id = b.source_id
WHERE a.close <> b.close
ORDER BY relative_gap DESC
LIMIT 50;
```

Where a source stopped delivering:

```sql
SELECT source, symbol, last_bar, now() - last_bar AS silent_for
FROM source_coverage
WHERE now() - last_bar > interval '10 minutes'
ORDER BY silent_for DESC;
```

Capture latency by source:

```sql
SELECT s.name, percentile_disc(0.5) WITHIN GROUP (ORDER BY b.ingested_at - b.ts) AS median_lag
FROM bars b JOIN sources s ON s.id = b.source_id
WHERE b.ingested_at > now() - interval '1 day'
GROUP BY s.name;
```

## What is deliberately not here

**No merged or cleaned price series.** There is no "the" price of BTC at 09:31
in this database, only what each source said it was. Any code that needs one
value has to choose, and choosing is a decision that belongs where it can be
seen, not hidden in a view.

**No repaired data.** Gaps stay gaps, here as everywhere else in this
repository. Nothing interpolates.

**Not the backtester's input.** Backtests read the Parquet archive
(`data/cache/`), which needs no server and does not change under a result. See
ADR 0010 for why the split exists and what each store is for.
