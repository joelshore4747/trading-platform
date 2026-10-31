# ADR 0010 — A queryable store, beside the archive

**Status:** Accepted
**Date:** 2026-09-03

## Problem

This repository has deliberately had no database. `harness/DATA_MAP.md` said so
in as many words, and gave the condition for changing its mind:

> Do not introduce a database until a workload demands it. Revisit when: >1
> concurrent writer, or queries across >1 GB, or a frontend needs sub-second
> reads.

The approved plan of 2026-09-03 asks for many redundant market-data feeds
running at once, government and macro series to join against prices, and a web
page that answers "is this working" at a glance. That is all three conditions at
once, not one of them arguably.

## Evidence — VERIFIED

- Concurrent writers: the plan's collector runs one connection per source per
  symbol, by design, so that sources can be compared. Parquet's writer
  (`data/cache.write_bars`) overwrites whole files and has no append path; two
  writers on one symbol would destroy each other's work.
- Volume: the current archive is 824,304 bars for one venue and one month.
  Several sources, several years and several asset classes is gigabytes.
- Sub-second reads: the operations page has to answer across sources and
  symbols. Answering it from Parquet means opening every file.

## Decision

Add Postgres as a second store, and keep Parquet.

| | Parquet (`data/cache/`) | Postgres (`store/`) |
|---|---|---|
| Job | the archive, and the backtester's input | the live and queryable layer |
| Written by | `write_bars`, whole file at a time | the collector, continuously |
| Read by | `CachedBarFeed`, every backtest | the reconciler, the page, research queries |
| Needs a server | no | yes |

The split is the point. **A backtest must not depend on a server being up.** A
result reproduced three years from now needs a file and a Python interpreter,
not a running database with a schema that has moved on. So the research path
keeps reading Parquet, and nothing in `engine/` or `analytics/` imports `store/`.

Shape, decided by two rules:

1. **A bar is identified by its source.** The primary key on `bars` is
   `(source_id, market, symbol, interval, ts)`. Two venues quoting BTC at 09:31
   are two rows. Merging them would discard the disagreement, and the
   disagreement is the entire reason for running more than one feed.
2. **Every row is traceable.** Each bar points at the `ingest_run` that fetched
   it, which points at a `source` and records what was asked for and how it
   ended. A run that dies mid-way stays `status='running'` with no `finished_at`,
   so a crash looks like a crash rather than a short read.

Supporting choices:

- Prices are `NUMERIC`, never `double precision`, matching the `Decimal` rule
  everywhere else. `tests/test_store.py` pins the round trip including the
  exponent, because `100.5` and `100.50` are the same number and different
  prices.
- Migrations are forward-only, checksummed, one transaction each. There is no
  down-migration: a down-migration is a plan for destroying data written since
  the up-migration ran, written at the moment nobody knows what that data is.
  Editing an applied migration raises, because otherwise the file and the live
  schema disagree forever and nothing notices.
- Every table and column carries a SQL `COMMENT`, so the data model can be read
  out of the database itself rather than from a document that may have drifted.
- `psycopg` is an **optional** dependency (`.[store]`). Everything that does not
  touch the store keeps working without it, and says what to install if it does.

## Consequences

- One new runtime dependency, optional, added at the moment it was first
  imported as `pyproject.toml`'s standing rule requires.
- `harness/DATA_MAP.md`'s no-database rule is superseded, and its revisit
  condition is recorded as having fired rather than been ignored.
- The store tests need a real Postgres and are skipped without one. A skipped
  suite is indistinguishable from a passing one in a summary line, so
  `test_the_store_tests_are_not_quietly_disabled` fails loudly when a database
  is reachable but the suite was about to skip. `scripts/dev_postgres.sh` makes
  running one a single command on any machine with the Postgres binaries.
- Two stores can drift. The guard is `scripts/store_load_cache.py --verify-only`,
  which compares them bar for bar. On the archive it reports IDENTICAL across
  824,304 bars.

## Revisit when

The research path ever wants to read from Postgres. That would be the moment to
stop and ask what happens to a result when the schema changes underneath it,
and the answer had better not be "nobody can reproduce it".
