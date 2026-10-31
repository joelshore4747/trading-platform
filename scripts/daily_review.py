"""Write the daily review for one paper account.

    .venv/bin/python scripts/daily_review.py --root ./paper --account a1 [--day 2026-09-02]

Reads the account's journal, cycles and recorded bars; replays the day through
a plain Backtester with the same strategy; writes
harness/reviews/<account>/<day>.md, never overwriting an earlier review.
Then read it — `/daily-review` is the session that acts on it.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.live.review import review_day, write_review

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--day", default=None, help="UTC date; default yesterday")
    parser.add_argument("--reviews-dir", type=Path, default=ROOT / "harness" / "reviews")
    args = parser.parse_args(argv)
    if args.day is None:
        day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=1
        )
    else:
        day = datetime.fromisoformat(args.day).replace(tzinfo=UTC)
    review = review_day(
        args.root, args.account, day, experiments_dir=ROOT / "harness" / "experiments"
    )
    path = write_review(review, args.reviews_dir)
    print(review.report())
    print(f"\nwritten: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
