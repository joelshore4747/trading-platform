"""Import a vendor archive into the Parquet cache, and report what it is.

    .venv/bin/python scripts/import_vendor.py historical.zip

Prints a characterisation before it writes anything. That ordering is the
point: the numbers below decide whether a result computed on this data means
anything, and they are easy not to look at once the import has "worked".

Reports rather than repairs. Gaps stay gaps, coverage stays whatever it is, and
nothing is interpolated — a hole that gets filled silently is a hole you find
out about from a backtest instead of from a loader.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

from trading_platform.data.cache import cache_path, ensure_layout, write_bars
from trading_platform.data.intervals import timedelta_to_interval
from trading_platform.data.universe import Listing, UniverseRegistry
from trading_platform.data.vendor import (
    align,
    coverage,
    coverage_by_hour,
    load_segments,
    load_zip,
    screen,
)

#: Below this a run is measuring a specific slice of the day, not the market.
EVENING_HOURS = range(18, 24)
MORNING_HOURS = range(7, 11)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="zip of per-symbol CSV files")
    parser.add_argument("--out", type=Path, default=Path("data/cache"))
    parser.add_argument(
        "--market",
        default="um",
        help="cache key component. These archives are USD-margined perpetuals, "
        "so 'um' — not 'spot'. Getting it wrong files them where nothing looks.",
    )
    parser.add_argument(
        "--max-zero-volume-fraction",
        type=float,
        default=None,
        help="also drop symbols whose whole-file zero-volume share exceeds "
        "this. Off by default: it is a whole-file average, so it judges a "
        "symbol's early history by its later liquidity, which is not "
        "point-in-time. The default screen drops only symbols dead across the "
        "entire sample.",
    )
    parser.add_argument(
        "--no-screen",
        action="store_true",
        help="write every symbol, including ones screening rejects. For "
        "inspecting what was dropped, not for producing a universe to trade.",
    )
    parser.add_argument(
        "--align",
        action="store_true",
        help="intersect to a common timestamp grid before writing. Costs bars; "
        "reports how many. Needed for cross-sectional work, wrong for "
        "single-symbol runs, which is why it is off by default.",
    )
    parser.add_argument("--interval-minutes", type=int, default=1)
    parser.add_argument(
        "--stamp",
        choices=("open", "close"),
        default="open",
        help="convention THE FILE uses. Binance writes openTime; getting this "
        "wrong is a one-bar lookahead, not an offset.",
    )
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--dry-run", action="store_true", help="characterise, write nothing")
    args = parser.parse_args(argv)

    if not args.archive.exists():
        print(f"No such archive: {args.archive}", file=sys.stderr)
        return 1

    interval = timedelta(minutes=args.interval_minutes)
    print(f"Reading {args.archive} (stamp={args.stamp}, interval={interval})...")
    loaded = load_zip(args.archive, interval=interval, stamp=args.stamp, symbols=args.symbols)
    if not loaded:
        print("No CSV members matched.", file=sys.stderr)
        return 1

    print(f"\n{len(loaded)} symbols\n")
    print(f"{'symbol':<16}{'bars':>8}{'cov':>7}{'segs':>6}{'longest':>9}  {'span'}")
    print("-" * 88)

    worst_coverage = 1.0
    total_bars = 0
    for symbol in sorted(loaded):
        bars = loaded[symbol]
        total_bars += len(bars)
        cov = coverage(bars, interval=interval)
        worst_coverage = min(worst_coverage, cov)
        segments = load_segments(bars, interval=interval)
        longest = max(len(s) for s in segments)
        print(
            f"{symbol:<16}{len(bars):>8,}{cov:>6.0%}{len(segments):>6}{longest:>9,}  "
            f"{bars[0].timestamp:%Y-%m-%d %H:%M} -> {bars[-1].timestamp:%Y-%m-%d %H:%M}"
        )

    reference = loaded[sorted(loaded)[0]]
    by_hour = coverage_by_hour(reference, interval=interval)
    morning = sum(by_hour[h] for h in MORNING_HOURS) / len(MORNING_HOURS)
    evening = sum(by_hour[h] for h in EVENING_HOURS) / len(EVENING_HOURS)

    print(f"\n{total_bars:,} bars total")
    print("\nWhat this data is NOT, stated up front:")
    print(
        f"  - NOT continuous. Worst per-symbol coverage {worst_coverage:.0%}; the span "
        "reads far longer than the data inside it."
    )
    print(
        f"  - NOT uniformly sampled across the day. Hours 07-10 UTC average "
        f"{morning:.0%} coverage against {evening:.0%} for 18-23 UTC. Anything "
        "time-of-day sensitive measured here is biased, and a strategy that loses "
        "money in the US evening is under-penalised because it barely trades there."
    )
    print(
        "  - NOT a point-in-time universe, if screening is on. Membership is "
        "decided\n    from statistics over the WHOLE file, so how a symbol "
        "behaved at the end of\n    the sample determines whether it exists at "
        "the start. The default screen\n    drops only symbols dead throughout, "
        "which is the defensible case; anything\n    stricter is opt-in via "
        "--max-zero-volume-fraction."
    )
    firsts = {bars[0].timestamp for bars in loaded.values()}
    lasts = {bars[-1].timestamp for bars in loaded.values()}
    print(
        f"  - NOT a universe with a history. All {len(loaded)} symbols share "
        f"{len(firsts)} distinct\n    first bar(s) and {len(lasts)} distinct last "
        "bar(s), so there are zero listing and\n    zero delisting events in this "
        "file. Both edges are the ARCHIVE's, not the\n    instruments' — the "
        "universe registry marks them censored and refuses to\n    answer "
        "members(as_of) until someone asserts real dates or explicitly\n    accepts "
        "the observed bounds."
    )
    print(
        "  - NOT spot. These are perpetuals, so funding applies — and no funding "
        "series is included, so carry-sensitive strategies are unevaluable on this "
        "file and should say so rather than assume zero."
    )

    # Screening runs before the write, not after, so a symbol the quality
    # checks reject never reaches disk. It existed and was tested for weeks
    # while this script called neither it nor align() — so the on-disk
    # universe still carried TONUSDT, which those same checks had identified
    # as dead. Machinery nothing calls is not a safeguard.
    writing = loaded
    if not args.no_screen:
        writing, quality = screen(loaded, max_zero_volume_fraction=args.max_zero_volume_fraction)
        dropped = sorted(set(loaded) - set(writing))
        if dropped:
            print(f"\nScreened out {len(dropped)} of {len(loaded)} symbols:")
            for symbol in dropped:
                for concern in quality[symbol].concerns():
                    print(f"  {symbol:<16}{concern}")
        else:
            print(f"\nScreening dropped nothing; all {len(loaded)} symbols pass.")

    if args.align:
        before = sum(len(b) for b in writing.values())
        writing, retained = align(writing)
        after = sum(len(b) for b in writing.values())
        print(
            f"\nAligned to a common grid: {after:,} of {before:,} bars retained "
            f"({retained:.1%}). The remainder are timestamps some symbol lacked; "
            "they are dropped, never filled."
        )

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    if not writing:
        print("\nNothing left to write after screening.", file=sys.stderr)
        return 1

    interval_name = timedelta_to_interval(interval)
    ensure_layout(args.out)
    for symbol in sorted(writing):
        write_bars(cache_path(args.out, args.market, symbol, interval_name), writing[symbol])

    # The registry falls out of the loop above rather than being a separate
    # pass: first_seen, last_seen and bars are exactly what the
    # characterisation already computed and printed. Asserted listing dates
    # stay None because a vendor archive is a record of collection, not a
    # statement about when anything listed.
    registry = UniverseRegistry(
        listings=tuple(
            Listing(
                market=args.market,
                symbol=symbol,
                interval=interval_name,
                first_seen=bars[0].timestamp,
                last_seen=bars[-1].timestamp,
                bars=len(bars),
            )
            for symbol, bars in sorted(writing.items())
        )
    )
    written = registry.save_to_cache(args.out)

    print(
        f"\nWrote {len(writing)} symbols to "
        f"{args.out}/{args.market}/<symbol>/{interval_name}.parquet"
    )
    print(f"Wrote the universe registry to {written}")
    print("Read them back with data.cache.CachedBarFeed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
