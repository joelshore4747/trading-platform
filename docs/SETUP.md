# SETUP — getting this running on your own machine

Written to be pasted a block at a time. Each block ends with something that
either prints a number or fails loudly; if it does neither, stop there rather
than continuing.

Everything below except step 5 works with no network access to any market-data
provider. That is deliberate: the backtester, the archive, the store and the
whole test suite must keep working without a vendor, or a research result would
depend on somebody's API being up.

---

## 0. What you need first

| | Why | Check |
|---|---|---|
| Python 3.11 or newer | `requires-python = ">=3.11"` | `python3 --version` |
| git | | `git --version` |
| Docker *or* PostgreSQL 16 | only for the store — steps 1–3 do not need it | `docker --version` |

`uv` is what the repo's own commands assume, and is the fastest path:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # macOS / Linux
```

Plain `pip` works too; the fallback is shown in step 1.

---

## 1. Clone and install

```bash
git clone https://github.com/joelshore4747/trading_platform.git
cd trading_platform

uv venv
uv pip install -e ".[dev,store]"
```

Without `uv`:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,store]"
```

`store` is the extra that pulls in `psycopg`. It is optional on purpose — every
backtest, every metric and the whole archive path work without it and must keep
working without it. Install it now because the collector and the operations page
are the parts you asked for, and both write to Postgres.

**Check it:**

```bash
.venv/bin/python -m pytest -q
```

You should see 1196 passed and 111 skipped. The skips are the
store tests, which need a database — step 3 turns them on. A failure here is a
real failure; nothing in this suite needs the network.

---

## 2. Run a backtest, on data already in the repository

```bash
.venv/bin/python examples/run_backtest.py
.venv/bin/python examples/run_walk_forward_btc.py
```

The second one is the honest one: it reports in-sample against out-of-sample,
and the gap between them is the finding. Read `harness/FINAL_AUDIT.md` before
believing any number this repository prints, including that one.

To load the full archive into the Parquet cache:

```bash
.venv/bin/python scripts/import_vendor.py historical.zip --market um --interval 1m
```

---

## 3. The store

**With Docker** — the portable path, and the one to prefer:

```bash
docker compose up -d postgres
export TRADING_PLATFORM_DSN='postgresql://trading:trading@localhost:5439/trading_platform'
```

Port 5439, not 5432, so it cannot collide with a Postgres you already depend on.
That password is a development credential and is meant to be visible: it
protects a database of public market prices on your own machine. **A production
DSN never goes in a file inside this repository** — it comes from the
environment, and `core/credentials.py` is what enforces that.

**Without Docker**, on Linux with the PostgreSQL 16 binaries installed:

```bash
scripts/dev_postgres.sh start
eval "$(scripts/dev_postgres.sh env)"
```

That one lives under `/tmp` and is expected to disappear. It is for tests, not
for anything you want to keep.

**Create the schema, then check it:**

```bash
.venv/bin/python scripts/migrate.py            # what is applied, what is pending
.venv/bin/python scripts/migrate.py --apply    # run them
```

Seven migrations. `--check` exits 1 while anything is pending and applies
nothing, which is what `deploy/install.sh` runs before it enables a unit.

Now re-run the suite and the skips should be gone:

```bash
export TRADING_PLATFORM_TEST_DSN="$TRADING_PLATFORM_DSN"
.venv/bin/python -m pytest
```

1307 passed, 0 skipped.

**Copy the archive into the store and prove the round trip:**

```bash
.venv/bin/python scripts/store_load_cache.py --market um --interval 1m
.venv/bin/python scripts/store_load_cache.py --market um --interval 1m --verify-only
```

`--verify-only` reads both back and compares them bar for bar, including the
Decimal exponent — `100.5` and `100.50` are equal numbers and different prices,
because the second says the venue quoted to the cent. If it disagrees about a
single price, the store is not a place to run research from.

---

## 4. Keys

This is the part nobody can do for you: creating an account, accepting terms and
entering payment details are acts by a person, and one of them is a legal
commitment in your name. Five minutes per provider.

| Provider | Tier | Where | Variables |
|---|---|---|---|
| Alpaca | Free | alpaca.markets → Paper account → API keys | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` |
| Polygon | Free or Starter | polygon.io → Dashboard → API keys | `POLYGON_API_KEY` |
| Binance | none needed | public endpoints | — |
| FRED | Free | fred.stlouisfed.org → My Account → API keys | `FRED_API_KEY` |

**Read-only market-data scopes only.** Alpaca's paper keys are the right ones;
a trading key is not needed by anything here, because every broker adapter
raises.

Put them somewhere outside the repository and source it:

```bash
mkdir -p ~/.config/trading_platform
cat > ~/.config/trading_platform/env <<'EOF'
TRADING_PLATFORM_DSN=postgresql://trading:trading@localhost:5439/trading_platform
APCA_API_KEY_ID=...
APCA_API_SECRET_KEY=...
POLYGON_API_KEY=...
EOF
chmod 600 ~/.config/trading_platform/env
set -a; . ~/.config/trading_platform/env; set +a
```

`.gitignore` does not cover a mistake made outside the repository, which is why
this path is outside it.

---

## 5. The collector — live capture, many sources

```bash
cp config/collector.example.toml collector.toml
$EDITOR collector.toml          # symbols, and read the venue notes
```

**`venue` is the thing to get right.** `vendor` says which client fetches;
`venue` says which *tape* the data describes. Two sources sharing a venue are
claiming to report the same market, so any difference between them is an error;
two with different venues are expected to differ, and that gap is the basis — a
market fact, not a fault. Alpaca's free tier serves IEX alone, so on the free
tier set `venue = "iex"`; the collector now refuses a config that calls it
`sip`.

```bash
# Validates the market, the interval, every vendor, every venue and every
# credential — and touches no network. Run this before every start.
.venv/bin/python scripts/run_collector.py --config collector.toml --check

# Run it. Ctrl-C stops it cleanly: captures stop, sinks flush, counts print.
.venv/bin/python scripts/run_collector.py --config collector.toml
```

**Read what it captured**, in another shell:

```bash
.venv/bin/python scripts/run_reconcile.py --hours 1
```

The rate always comes with its denominator. 100% agreement over three
comparisons is not agreement.

---

## 6. Watching it

The page — one column, no results, only "is this working":

```bash
.venv/bin/python scripts/run_ops.py          # http://127.0.0.1:8099
```

Loopback only unless you pass `--i-mean-it`. It binds to your machine, and
putting it on a public interface is a decision you have to state.

The check that does not need you to be looking:

```bash
.venv/bin/python scripts/check_health.py          # exits 1 if anything is wrong
.venv/bin/python scripts/check_health.py --quiet  # silent when fine
```

As a service, on Linux. One script, two layouts:

```bash
deploy/install.sh --layout user            # print the plan; changes nothing
deploy/install.sh --layout user --apply    # this machine, no sudo
```

On a VPS, where the checkout is at `/opt/trading_platform` and a `trading`
service account owns it:

```bash
sudo deploy/install.sh --layout system --apply
```

It prints every action before taking any, refuses to act without `--apply`, and
**refuses to enable a unit whose preflight fails** — it runs `migrate.py
--check` and `run_collector.py --check`, the same checks the units run in
`ExecStartPre`, so a broken install fails in front of you rather than in a
restart loop at 3am.

**Run it twice on a fresh machine.** The first run has no credentials to find,
so it creates `~/.config/trading_platform/env` at 0600 with variable names and
no values, installs the units, and stops before enabling. Fill the file in, run
it again. It never writes a credential and never prints the contents of that
file.

The units used to be four hand-installed files, and they had four defects that
only running them would find: two mutually incompatible layouts that this page
told you to install together, a reconciler with no way to reach
`TRADING_PLATFORM_DSN`, an `OnFailure=` pointing at a unit this repository does
not contain, and a config path neither document agreed on. They are generated
now, and `tests/test_deploy_units.py` reads the output back — each of its tests
fails on one of those four.

---

## 7. Paper trading

```bash
.venv/bin/python scripts/run_paper.py --root ./paper --account a1 \
    --market crypto --symbols BTC/USD --interval 1m --strategy sma \
    --starting-cash 1000 --daily-loss 0.05 --stage paper --dry-run
```

`--dry-run` builds everything, prints the reconciliation report and the first
URL it would fetch, and touches no network. Drop it to run.

Nothing here can place a real order: every broker adapter raises, `--stage live`
is not offered, and fills are simulated by the same engine a backtest uses. If
you hold a leveraged instrument the session is **refused** until an
acknowledgement row exists — see `docs/adr/0011`.

`docs/RUNBOOK.md` is the 3am procedure. Read it before you leave anything
running unattended.

---

## What is unverified

`live/` has never spoken to a real venue. The environment this was built in
answers 403 to every market-data host, so the first run on your machine is the
first contact, and its job is to find out where the published API reference and
the wire disagree. Expect that to take a round of fixes. Ticket `T-524425f9`
tracks it.
