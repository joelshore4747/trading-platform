"""Compare what the sources said, and print the agreement rate with its denominator.

    .venv/bin/python scripts/run_reconcile.py --hours 1
    .venv/bin/python scripts/run_reconcile.py --hours 24 --dry-run
    .venv/bin/python scripts/run_reconcile.py --hours 24 --worst 20

Run it after the collector has been up for a while. `--dry-run` compares and
records nothing, for a look before committing rows.

The number this prints is the answer to "are my feeds telling me the same
thing", and it is printed with the count of comparisons it was computed over,
always. 100% agreement over three comparisons is not agreement, and a rate
without its denominator invites exactly that mistake.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.collect.reconcile import reconcile
from trading_platform.store.connection import connect
from trading_platform.store.migrate import pending_migrations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--market", default="equity")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--hours", type=float, default=1.0, help="window back from now")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument(
        "--min-gap",
        type=Decimal,
        default=Decimal(0),
        help="floor on what is worth a row, as a fraction (0.0001 = 1bp). "
        "Defaults to 0: a threshold is how a systematic error becomes invisible.",
    )
    parser.add_argument("--dry-run", action="store_true", help="compare, record nothing")
    parser.add_argument("--worst", type=int, default=10, help="how many rows to list")
    args = parser.parse_args(argv)

    end = datetime.now(UTC)
    start = end - timedelta(hours=args.hours)

    with connect() as conn:
        outstanding = pending_migrations(conn)
        if outstanding:
            names = ", ".join(str(m) for m in outstanding)
            raise SystemExit(
                f"{len(outstanding)} migration(s) pending: {names}. Run the collector once."
            )
        result = reconcile(
            conn,
            market=args.market,
            interval=args.interval,
            window_start=start,
            window_end=end,
            symbols=args.symbols,
            min_gap=args.min_gap,
            record=not args.dry_run,
        )

    print(result.report())

    if result.same_venue_compared == 0 and result.compared:
        print(
            "\nNo two sources in this window claim the same venue, so nothing here is an "
            "error — every difference is basis. If you meant them to be the same tape, the "
            "`venue` in your collector config is wrong.",
        )

    errors = result.same_venue_disagreements
    if errors:
        print(f"\nworst {min(args.worst, len(errors))} same-venue gaps (one of the two is wrong):")
        for found in sorted(errors, key=lambda d: d.worst_gap, reverse=True)[: args.worst]:
            print(
                f"  {found.ts.isoformat()}  {found.symbol:<8} {found.worst_field:<5} "
                f"{found.basis_points:>10.2f} bp   sources {found.source_a_id} vs {found.source_b_id}"
            )
        print(
            "\nA same-venue gap is a data error, not a market fact. Look at the bar in "
            "`bars` for both sources before believing either."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
