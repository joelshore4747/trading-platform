"""Load the Parquet cache into the store, then prove the round trip.

    scripts/dev_postgres.sh start && eval "$(scripts/dev_postgres.sh env)"
    .venv/bin/python scripts/store_load_cache.py --market um --interval 1m
    .venv/bin/python scripts/store_load_cache.py --market um --interval 1m --verify-only

The Parquet cache stays the archive; this copies it into the queryable layer.
`--verify-only` reads both back and compares them bar for bar, which is the
check that matters: if Postgres and Parquet disagree about a single price, the
store is not a place to run research from.

Comparison is on the `Bar` objects, not on formatted strings, and it includes
the Decimal exponent — `100.5` and `100.50` are equal numbers and different
prices, because the second says the venue quoted to the cent.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.core.events import Bar
from trading_platform.data.cache import cache_path, read_bars
from trading_platform.store.bars import ensure_source, ingest_run, write_bars
from trading_platform.store.bars import read_bars as read_stored
from trading_platform.store.connection import connect
from trading_platform.store.migrate import migrate, pending_migrations

ROOT = Path(__file__).resolve().parents[1]


def symbols_in(cache: Path, market: str) -> list[str]:
    directory = cache / market
    if not directory.is_dir():
        raise SystemExit(f"no cached market at {directory}")
    return sorted(p.name for p in directory.iterdir() if p.is_dir())


def identical(left: Sequence[Bar], right: Sequence[Bar]) -> tuple[bool, str]:
    """Bar-for-bar equality, reporting the first difference rather than a count."""
    if len(left) != len(right):
        return False, f"{len(left)} bars in Parquet, {len(right)} in the store"
    for index, (a, b) in enumerate(zip(left, right, strict=True)):
        if a != b:
            return False, f"bar {index} at {a.timestamp.isoformat()}: parquet {a} vs store {b}"
        for field in ("open", "high", "low", "close", "volume"):
            x, y = getattr(a, field), getattr(b, field)
            if str(x) != str(y):
                return False, f"bar {index} {field}: parquet {x!s} vs store {y!s} (precision lost)"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cache", type=Path, default=ROOT / "data" / "cache")
    parser.add_argument("--market", default="um")
    parser.add_argument("--interval", default="1m")
    parser.add_argument(
        "--source",
        default=None,
        help="store source name. Default: <vendor>-<market>, built from --vendor.",
    )
    parser.add_argument(
        "--vendor",
        required=True,
        help="who supplied these bars: binance, polygon, alpaca, ... Required and never "
        "assumed. This used to default to 'binance' for everything, so Polygon equity "
        "bars were filed as Binance's (T-f1c68f71).",
    )
    parser.add_argument(
        "--venue",
        required=True,
        help="which TAPE the bars describe: binance, sip, iex, nasdaq, ... NOT the vendor. "
        "Required because it decides whether a disagreement between two sources is a bad "
        "print or the basis, and a wrong one is undetectable.",
    )
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)

    name = args.source or f"{args.vendor}-{args.market}"
    symbols = args.symbols or symbols_in(args.cache, args.market)
    print(f"{len(symbols)} symbol(s) from {args.cache}/{args.market} at {args.interval}")

    started = time.time()
    with connect() as conn:
        if args.verify_only:
            # A verification must not change the thing it verifies. Applying a
            # migration here once silently applied an untracked working-tree
            # file to a live database during what was flagged a read-only run.
            outstanding = pending_migrations(conn)
            if outstanding:
                names = ", ".join(str(m) for m in outstanding)
                raise SystemExit(
                    f"--verify-only refuses to migrate, and {len(outstanding)} migration(s) "
                    f"are pending: {names}. Run without --verify-only first."
                )
        else:
            applied = migrate(conn)
            if applied:
                print(f"applied {len(applied)} migration(s): {', '.join(str(m) for m in applied)}")
        source = ensure_source(
            conn,
            name,
            kind="vendor",
            venue=args.venue,
            description=f"Parquet archive imported from data/cache ({args.vendor})",
        )

        written = 0
        if not args.verify_only:
            with ingest_run(
                conn,
                source,
                kind="import-cache",
                params={"market": args.market, "interval": args.interval, "symbols": len(symbols)},
            ) as run_id:
                for symbol in symbols:
                    bars = read_bars(
                        cache_path(args.cache, args.market, symbol, args.interval), symbol
                    )
                    written += write_bars(
                        conn,
                        source,
                        run_id,
                        market=args.market,
                        interval=args.interval,
                        bars=bars,
                    )
            print(f"wrote {written:,} new row(s) in {time.time() - started:.1f}s")

        print("verifying round trip, bar for bar ...")
        checked = 0
        for symbol in symbols:
            from_parquet = read_bars(
                cache_path(args.cache, args.market, symbol, args.interval), symbol
            )
            from_store = read_stored(
                conn, source, market=args.market, symbol=symbol, interval=args.interval
            )
            same, why = identical(from_parquet, from_store)
            if not same:
                print(f"MISMATCH {symbol}: {why}")
                return 1
            checked += len(from_parquet)

    print(
        f"IDENTICAL — {checked:,} bars across {len(symbols)} symbols, {time.time() - started:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
