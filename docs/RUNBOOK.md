# RUNBOOK — the paper trader, and what to do at 3am

The PAPER stage. Nothing here can place an order at a venue: fills are simulated by the
same engine a backtest uses, over bars polled from Alpaca. The one thing that is real is
the record — the journal, the bar capture, the account cycles — and the runbook exists to
keep that record honest when something goes wrong unattended.

**Status: UNVERIFIED against Alpaca.** The sandbox that wrote this answers 403 to every
market-data host. The first run on the VPS is the first contact, and its job is to find
out where the published API reference and the wire disagree.

## Start

```bash
export APCA_API_KEY_ID=...        # read-only market-data key; never a trading key
export APCA_API_SECRET_KEY=...
.venv/bin/python scripts/run_paper.py --root ./paper --account a1 --market crypto \
    --symbols BTC/USD --interval 1m --strategy sma --starting-cash 1000 --daily-loss 0.05 --dry-run
```

`--dry-run` builds everything, prints the reconciliation report and the first URL it would
fetch, and touches no network. Drop the flag to run. Run it under `systemd` or `nohup`; it
exits with 0 when the feed ends, 3 when the account cycle failed.

**With `TRADING_PLATFORM_DSN` set**, every mark and every risk refusal is written to
Postgres, the operations page can see the account, and the account's leverage
acknowledgements gate its orders. The session prints how many acknowledgements it found on
start and the tracker's counters on exit.

**Without it**, the journal remains the durable record and nothing else happens — nothing is
queryable and **nothing gates leverage**. That is printed, not assumed. A session holding a
leveraged instrument in that state is **refused**, because the gate exists so leverage
cannot arrive by omission and "the database was not configured" is an omission. Pass
`--no-store` to say deliberately that this account is spot only.

`--stage` records which of the four stages the session is (`research`, `simulation`,
`paper`). `live` is not offered: every broker adapter raises, so no session here can place
a live order, and a row claiming otherwise would be the first false thing in the record.

## What it writes (all append-only)

| Path | What |
|---|---|
| `paper/accounts/<account>/cycles.jsonl` | one line per cycle open and close |
| `paper/accounts/<account>/decisions/<day>.jsonl` | every order, fill, rejection, halt, mark, note |
| `paper/accounts/<account>/LOCK` | held while a session runs |
| `paper/accounts/<account>/KILL` | **you** create this to stop the strategy |
| `paper/bars/alpaca-<market>/<symbol>/<day>.jsonl` | every bar as it arrived |

## The 3am procedure

1. **Stop generating intent.** `touch paper/accounts/<account>/KILL`. The strategy is not
   called again; orders already queued fill on the next bar; the session ends. Nothing is
   flattened for you — a paper position left open is a fact for the review, not a risk.
2. **Do not delete anything.** Not the journal, not a cycle, not the lock. Every one is
   history. If you must correct something, add a note; the reviewer reads notes.
3. **Read the last lines** of today's decisions file. The last `note` says why the session
   stopped: `kill switch`, `daily loss limit`, `cycle failed`, or `feed ended` (the poller
   stopped delivering — check `poller` stats in the process output for errors and 401/429).
4. **Restart** only after `reconcile` says AGREES. It runs on every start and refuses to
   trade if the ledger rebuilt from the journal disagrees with what the journal last
   recorded. A refusal is a finding: something wrote to the journal that the ledger cannot
   explain. Investigate; do not edit the journal to make it agree.
5. **A stale `LOCK`** (process died without releasing it) is removed by hand after you have
   confirmed with `ps` that no supervisor is running. Two writers split the books, and the
   cycle store refuses to load a book that was split.
6. **A failed cycle** (equity reached zero) is closed and frozen. The next start opens the
   next cycle with the same starting cash. The failure is the result; write the autopsy
   (`strategy-autopsy` skill) before restarting.

## What "diverged" means

Every session's fills must replay exactly through a plain `Backtester` over the bars in
`paper/bars/` (`live.replay.compare`). A divergence is a bug in the engine or the supervisor,
never something to tolerate. Replay identity holds per session, not across a restart: the
strategy's own history starts empty at boot while the ledger does not.

## The collector — many sources, never merged

A separate process from the paper trader, with a separate job: capture the same
market from several vendors at once and keep every vendor's view apart, so that
where they disagree the disagreement is recorded rather than resolved. Two feeds
that agree tell you nothing you did not already know from one.

**Status: UNVERIFIED against every vendor.** Same reason as above — 403 to
`data.alpaca.markets`, `api.polygon.io` and everything else from the sandbox
that wrote it. The first run on your machine is the first contact. What HAS been
proved offline, against a real PostgreSQL:
`tests/test_reconcile.py::test_two_captures_run_concurrently_land_separately_and_reconcile_to_a_number`
runs two captures on two threads with two connections, lands ten rows for five
instants (five per source — a merge would give five in total), and reports the
agreement rate as 4/5. The network half is what the first real run establishes.

### First run

```bash
# 1. A database. Locally:
scripts/dev_postgres.sh start && eval "$(scripts/dev_postgres.sh env)"
#    On a VPS: a real Postgres, and TRADING_PLATFORM_DSN pointing at it.

# 2. Keys. Read-only market-data scopes, in the environment, never in a file.
export APCA_API_KEY_ID=...  APCA_API_SECRET_KEY=...
export POLYGON_API_KEY=...

# 3. Config, then check it WITHOUT touching the network. --check validates the
#    store market, the interval, every vendor, every venue and every credential.
cp config/collector.example.toml collector.toml   # edit the symbols and venues
.venv/bin/python scripts/run_collector.py --config collector.toml --check

# 4. Run it. Ctrl-C stops it cleanly: captures stop, sinks flush, counts print.
.venv/bin/python scripts/run_collector.py --config collector.toml
```

### Reading what it captured

```bash
.venv/bin/python scripts/run_reconcile.py --hours 1
```

The rate always comes with its denominator. **100% agreement over three
comparisons is not agreement**, and instants only one source reached are not
counted at all — they cannot be compared, so they are not evidence.

Two kinds of row come out, and they must never be added together:

| `same_venue` | Meaning | What to do |
|---|---|---|
| `true` | Both sources claim the same venue, so the prices SHOULD be identical | One of them is a bad print. Look at both rows in `bars`. |
| `false` | Different venues, so they SHOULD differ | That gap is the **basis**: a market fact, not a fault. |

**`venue` is a thing you declare, and getting it wrong is the easiest way to
make this useless.** In `collector.toml`, `vendor` says which client fetches
(`alpaca`, `polygon`) and `venue` says which *tape* the data describes (`sip`,
`iex`, `nasdaq`, `binance`). They are not the same, and the collector refuses a
source that omits the venue: stamping it with the vendor made every
Alpaca-versus-Polygon gap read as basis, so every genuine error became a shrug.
If your two sources have different venues, the reconciler will tell you it
compared nothing on-venue and no error rate can be stated — believe it, and fix
the config.

Note also that Alpaca's `feed = "iex"` is the IEX tape alone, a small share of
volume, and is genuinely not the consolidated market. On the free tier set
`venue = "iex"` and expect the disagreements against Polygon to be basis,
because they are.

Useful queries are in `src/trading_platform/store/SCHEMA.md`. `feed_health` says
whether bars have *arrived*; `collector_health` says whether the process is
still *working*, which is a different question — a capture that died a minute
ago keeps satisfying `feed_health` from the bars it already wrote, for the whole
ten minutes until `silent_for` crosses.

### Being told, rather than looking

```bash
scripts/check_health.py            # one line per problem; exit 1 if any ALARM
scripts/check_health.py --quiet    # silent when fine — what cron wants
```

It runs the same assessment the operations page renders, deliberately: two
definitions of "healthy" drift within a fortnight and then disagree in front of
you at 3am. It alarms on an unreachable store, an empty database, a silent feed,
a run whose heartbeat has gone stale, a capture erroring every poll, and an
ingest run that ended failed. Bars that only reached the journal are a WARN,
because they are recoverable and an alarm that fires for a recoverable condition
is an alarm you switch off.

```bash
deploy/install.sh --layout user --apply      # or --layout system on a VPS
```

`OnFailure=` now points at `trading-alert@.service`, which this repository
ships: it logs the failure at error level (`journalctl -p err`) and POSTs it
if `TRADING_PLATFORM_ALERT_WEBHOOK` is set in the environment file. It used to
point at `status-email@%n.service`, a unit that exists in the Arch wiki and not
here — so the check ran, failed, and nobody found out, which is the state it
was written to replace. Point it somewhere you actually read with
`--on-failure`.

### As a service

```bash
deploy/install.sh --layout system --prefix /opt/trading_platform --apply
journalctl -u trading-collector -f
```

The installer creates `/etc/trading_platform/env` at 0600 with the variable
names and no values; put the keys there, or in a `systemctl edit` drop-in,
which still takes precedence. Neither is in git. It will not enable anything
until `migrate.py --check` and `run_collector.py --check` both pass.

### Looking at it

```bash
.venv/bin/python scripts/run_ops.py       # http://127.0.0.1:8099
```

One page: how far behind the quietest feed is (largest thing on the page), the
feeds, same-venue disagreements, account state, open tickets. It refreshes
itself every 30s with a `<meta refresh>` — no JavaScript, so it works in a
terminal browser over a phone tether.

It binds to loopback and refuses anything else without `--i-mean-it`: it shows
account equity, open positions and every filed defect. From a VPS, tunnel it:
`ssh -L 8099:127.0.0.1:8099 you@host`.

**It shows no strategy result, by design.** The roadmap defers a results
frontend until a strategy survives out of sample, because a dashboard makes an
unvalidated strategy look authoritative. That deferral stands; this page answers
"is the machinery alive" and nothing else.

### The 3am procedure, collector edition

**A source has stopped.** `feed_health` shows `silent_for` climbing for one
source and not the others. That is the source's problem, not yours: the other
sources are still capturing and their rows are unaffected. Check
`journalctl -u trading-collector` for that capture's `last error`. Do **not**
restart to "fix" it — the capture retries on its own schedule and a restart
loses the other sources' in-flight batches.

**Every source has stopped.** Network or credentials. `--check` first; it names
the missing environment variable rather than waiting for a 401.

**The database was down.** Expected, and survivable by design: the JSONL journal
under `data/journal/<source>/` took every bar, and `StoreSink.dropped` says how
many rows the store is missing. Put them back with
`scripts/journal_to_store.py --source <name> --interval 1m` once Postgres is up
(`--dry-run` first to see how many). The sink also reconnects on its own when
the server comes back, so a short restart needs no backfill at all — check
`reconnects` in the shutdown report before running anything. **The journal is the record. The store is a queryable
copy of it.** If those two ever disagree, believe the journal.

**A `BarConflict` in the log.** A source contradicted itself: the same instant,
twice, with different prices. Not an outage and not retried — it is a finding.
The batch was not written, and the bars are in the journal. Look at what the
vendor sent before deciding which value was real; do not delete either.

**"N bar(s) never reached the store" on exit.** The count of what the journal
has and Postgres does not. Backfill it; the exit code is 1 until you do.

## Before LIVE — not scheduled

Reaching LIVE requires all of: credentials held outside the repository; this kill switch
enforced venue-side as well; per-order and per-day loss limits enforced venue-side; full
order audit logging; this procedure rehearsed; and an explicit human decision recorded as
an ADR. None of these is done. `brokers/` still refuses every order.
