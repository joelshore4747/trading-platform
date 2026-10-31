"""Point the null harness at the ML pipeline. **Run this before believing anything.**

    .venv/bin/python scripts/measure_ml_null.py
    .venv/bin/python scripts/measure_ml_null.py --trials 200 --horizon 20 --embargo 20

The pipeline is run on **pure noise** — a random walk with no structure at all,
generated from a seeded PRNG so the run is reproducible. There is nothing to
find, so a correct pipeline reports an edge of approximately zero and finds a
positive edge in about half of its folds by chance.

**If it finds a systematic edge, the pipeline is leaking**, and the mean edge is
the size of the leak. That is the whole value of this measurement: a leak here is
invisible on real data, because on real data a positive result is what you were
hoping for.

The same reasoning as `analytics/nulltest.py` applies to the strategy pipeline;
this is that idea aimed at the ML one, where the failure modes are worse because
a model will find any leak that exists and exploit it perfectly.

## Reading the output

The reported figure is **accuracy over the majority-class floor**, not accuracy.
On a label that is 55% "up", a model that always says "up" scores 55%, and
reporting that alone would make every run look like a discovery.

A mean edge whose magnitude is small relative to its own spread across trials is
consistent with no leak. The script prints both, and the decision is the reader's
— there is deliberately no threshold that turns this into a green tick, because a
threshold would become the thing people optimise against.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.core.events import Bar
from trading_platform.ml.pipeline import run_pipeline

START = datetime(2026, 1, 1, tzinfo=UTC)


def noise_bars(count: int, seed: int, *, sigma: float = 0.001) -> list[Bar]:
    """A driftless random walk. No structure, so nothing to find.

    Bars are built so that open, high, low and close are mutually consistent —
    `Bar` refuses an inconsistent one — but carry no information beyond the walk.
    Deliberately no drift: a drifting series has a real edge (buy and hold), and
    a pipeline that found it would be right, which tells us nothing.
    """
    rng = random.Random(seed)
    price = 100.0
    bars: list[Bar] = []
    for i in range(count):
        opening = price
        price = max(price * (1.0 + rng.gauss(0.0, sigma)), 0.01)
        closing = price
        high = max(opening, closing) * (1.0 + abs(rng.gauss(0.0, sigma / 2)))
        low = min(opening, closing) * (1.0 - abs(rng.gauss(0.0, sigma / 2)))
        bars.append(
            Bar(
                symbol="NOISE",
                timestamp=START + timedelta(minutes=i),
                open=Decimal(f"{opening:.4f}"),
                high=Decimal(f"{high:.4f}"),
                low=Decimal(f"{max(low, 0.0001):.4f}"),
                close=Decimal(f"{closing:.4f}"),
                volume=Decimal("1"),
            )
        )
    return bars


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--trials", type=int, default=50)
    parser.add_argument("--bars", type=int, default=4000)
    parser.add_argument("--horizon", type=int, default=10)
    parser.add_argument("--train", type=int, default=500)
    parser.add_argument("--test", type=int, default=200)
    parser.add_argument("--embargo", type=int, default=0)
    parser.add_argument("--model", default="single-feature-threshold")
    parser.add_argument("--seed", type=int, default=20260904)
    args = parser.parse_args(argv)

    print(
        f"{args.trials} trials of {args.bars:,} noise bars, horizon {args.horizon}, "
        f"train {args.train} / test {args.test}, embargo {args.embargo}, model {args.model}"
    )
    print("There is nothing to find. A mean edge near zero is the correct answer.\n")

    edges: list[float] = []
    positive_folds = 0
    total_folds = 0
    refusals = 0
    for trial in range(args.trials):
        result = run_pipeline(
            noise_bars(args.bars, args.seed + trial),
            model_name=args.model,
            horizon=args.horizon,
            train_size=args.train,
            test_size=args.test,
            embargo=args.embargo,
        )
        if result.refused:
            refusals += 1
            continue
        edge = result.mean_edge
        if edge is None:
            refusals += 1
            continue
        edges.append(edge)
        positive_folds += result.positive_folds
        total_folds += len(result.folds)

    if not edges:
        print(f"NO MEASUREMENT: every trial was refused ({refusals}).")
        return 1

    mean = statistics.fmean(edges)
    spread = statistics.stdev(edges) if len(edges) > 1 else 0.0
    share = positive_folds / total_folds if total_folds else 0.0

    print(f"trials measured      : {len(edges)} ({refusals} refused)")
    print(f"mean edge over floor : {mean:+.5f}")
    print(f"spread across trials : {spread:.5f}")
    print(f"folds positive       : {positive_folds}/{total_folds} ({share:.1%})")
    print(
        "\nHow to read this. There is no structure in the data, so the mean edge should\n"
        "sit near zero and roughly half the folds should be positive by chance. A mean\n"
        "edge that is large relative to the spread, or a share of positive folds far\n"
        "from 50%, means the PIPELINE is finding something — and since the data has\n"
        "nothing in it, what it has found is a leak. The size of the mean edge is the\n"
        "size of the leak.\n\n"
        "There is deliberately no pass/fail threshold here. A threshold becomes the\n"
        "thing people optimise against, and this measurement is worth having only for\n"
        "as long as nobody is trying to satisfy it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
