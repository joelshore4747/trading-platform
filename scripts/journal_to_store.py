"""Backfill the store from the capture journal, after a database outage.

    .venv/bin/python scripts/journal_to_store.py --source alpaca-sip --interval 1m
    .venv/bin/python scripts/journal_to_store.py --source alpaca-sip --interval 1m --dry-run

The JSONL journal under `data/journal/<source>/<market>/<symbol>/<day>.jsonl` is
the durable record: it needs only a filesystem, and `StoreSink` deliberately
drops rows rather than take the collector down when Postgres is unreachable.
`StoreSink.stats.dropped` counts what the store is therefore missing. **This is
what puts them back.**

Until it existed, three places in the codebase told an operator to backfill with
`scripts/store_load_cache.py`, which reads the Parquet archive and knows nothing
about the journal — so the instruction, followed exactly, would either fail or
attribute one vendor's bars to another's source (adversarial review, 2026-09-04).

Safe to run repeatedly: bars already present with identical values are skipped
and counted. A bar that CONTRADICTS what is stored raises `BarConflict` and
writes nothing for that symbol, which is the finding, not an error to work
around — the journal and the store disagree about a price and somebody should
look at why.

The source must already exist in the store. This does not create one: the venue
a source describes is a decision made in the collector's configuration, and
guessing it here is how a bad print becomes "basis".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.data.cache import MARKETS
from trading_platform.data.intervals import interval_ms
from trading_platform.live.recorder import iter_journal_days, read_journal
from trading_platform.store.bars import BarConflict, Source, ingest_run, write_bars
from trading_platform.store.connection import connect
from trading_platform.store.migrate import pending_migrations

ROOT = Path(__file__).resolve().parents[1]


def known_source(connection: object, name: str) -> Source:
    with connection.cursor() as cursor:  # type: ignore[attr-defined]
        cursor.execute(
            "SELECT id, name, kind, venue, description FROM sources WHERE name = %s", (name,)
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute("SELECT name FROM sources ORDER BY name")
            known = [r[0] for r in cursor.fetchall()]
            raise SystemExit(
                f"no source named {name!r} in the store; known: {known}. "
                "Run the collector once so the source and its venue are recorded — this "
                "script will not invent a venue, because the venue decides whether a "
                "disagreement is an error or the basis."
            )
    return Source(*row)


def symbols_in(journal: Path, source: str, market: str) -> list[str]:
    directory = journal / source / market
    if not directory.is_dir():
        raise SystemExit(f"no journal at {directory}")
    return sorted(p.name for p in directory.iterdir() if p.is_dir())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--journal", type=Path, default=ROOT / "data" / "journal")
    parser.add_argument("--source", required=True, help="the capture's name, e.g. alpaca-sip")
    parser.add_argument("--market", default="equity", choices=sorted(MARKETS))
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--dry-run", action="store_true", help="count, write nothing")
    args = parser.parse_args(argv)

    interval_ms(args.interval)  # raises on a made-up interval, before any connection
    symbols = args.symbols or symbols_in(args.journal, args.source, args.market)
    print(f"{len(symbols)} symbol(s) under {args.journal / args.source / args.market}")

    with connect() as conn:
        outstanding = pending_migrations(conn)
        if outstanding:
            names = ", ".join(str(m) for m in outstanding)
            raise SystemExit(f"{len(outstanding)} migration(s) pending: {names}")
        source = known_source(conn, args.source)
        print(f"source {source.name!r} (id {source.id}, venue {source.venue!r})")

        read = 0
        written = 0
        conflicts: list[str] = []
        if args.dry_run:
            for symbol in symbols:
                for path in iter_journal_days(args.journal / args.source, args.market, symbol):
                    read += len(read_journal(path, symbol))
            print(f"dry run: {read:,} bar(s) in the journal, nothing written")
            return 0

        with ingest_run(
            conn,
            source,
            kind="backfill-journal",
            params={
                "journal": str(args.journal),
                "market": args.market,
                "interval": args.interval,
                "symbols": len(symbols),
            },
        ) as run_id:
            for symbol in symbols:
                for path in iter_journal_days(args.journal / args.source, args.market, symbol):
                    bars = read_journal(path, symbol)
                    read += len(bars)
                    try:
                        written += write_bars(
                            conn,
                            source,
                            run_id,
                            market=args.market,
                            interval=args.interval,
                            bars=bars,
                        )
                    except BarConflict as exc:
                        # Not worked around. The journal and the store disagree
                        # about a price that was already written, and which one
                        # is right is not a decision for a backfill script.
                        conflicts.append(f"{path.name}: {exc}")

    print(f"read {read:,} bar(s) from the journal, wrote {written:,} new row(s)")
    if conflicts:
        print(f"\n{len(conflicts)} file(s) CONTRADICT the store and were not written:")
        for line in conflicts[:10]:
            print(f"  {line}")
        print(
            "\nThe journal and the store disagree about a bar already recorded. Look at "
            "both before deciding which is right; neither was changed."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
