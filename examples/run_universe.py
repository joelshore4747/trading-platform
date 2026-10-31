"""Point-in-time membership over real cached data.

Run with:  .venv/bin/python examples/run_universe.py [CACHE_ROOT]

The first example in this repository that touches real market data rather than
the synthetic generator, and an answer to a question worth asking before
building anything cross-sectional: **how do you decide which of thousands of
instruments a strategy should even look at, without deciding it by knowing who
survived?**

The answer this project takes is that you cannot always decide it, and the
useful thing is a system that says so. Three sections below:

1. What the registry knows about the imported archive — and why it refuses to
   answer `members(as_of)` for it at all.
2. What it looks like when the dates ARE known, using real listing dates.
3. Why the cross-section needs a union grid once membership varies.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from trading_platform.data.cache import universe_path
from trading_platform.data.panel import Panel
from trading_platform.data.universe import (
    Listing,
    UniverseRegistry,
    UnknownListing,
)

DEFAULT_CACHE = Path("data/cache")
REAL_LISTINGS = Path(__file__).parent.parent / "tests" / "fixtures" / "coinmetrics_listings.json"


def section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def imported_archive(root: Path) -> None:
    section("1. What the registry knows about the imported archive")

    if not universe_path(root).exists():
        print(f"No registry at {universe_path(root)}.")
        print("Import an archive first:")
        print("    .venv/bin/python scripts/import_vendor.py historical.zip")
        return

    registry = UniverseRegistry.load_from_cache(root)
    print(f"{registry}")
    print(f"archive spans {registry.archive_start} -> {registry.archive_end}")
    print(f"censored lifetimes: {len(registry.censored)} of {len(registry.listings)}")

    try:
        registry.members(registry.archive_end or datetime.now(UTC))
        print("members() answered — some lifetime is bounded by a real event")
    except UnknownListing as refusal:
        print(f"\nmembers() refuses, and this is the correct answer:\n\n  {refusal}\n")

    accepted = registry.assume_observed_bounds()
    membership = accepted.members(registry.archive_end or datetime.now(UTC))
    print(f"After assume_observed_bounds(): {len(membership)} members.")
    print("The assumption does not become a fact — the warning survives it:\n")
    print(f"  {membership.warning()}")


def real_history() -> None:
    section("2. The same query where the dates are actually known")

    payload = json.loads(REAL_LISTINGS.read_text())
    print(f"{payload['source']}\n")
    registry = UniverseRegistry(
        listings=tuple(
            Listing(
                market="spot",
                symbol=asset,
                interval="1d",
                first_seen=datetime.fromisoformat(row["first_priced"]).replace(tzinfo=UTC),
                last_seen=datetime.fromisoformat(row["last_priced"]).replace(tzinfo=UTC),
                bars=0,
                listed=date.fromisoformat(row["first_priced"]),
                delisted=date.fromisoformat(row["last_priced"]),
            )
            for asset, row in payload["assets"].items()
        )
    )

    previous: tuple[str, ...] = ()
    for year in (2011, 2013, 2015, 2017, 2019, 2021, 2023, 2025):
        members = registry.members(datetime(year, 6, 1, tzinfo=UTC)).symbols
        joined = sorted(set(members) - set(previous))
        print(
            f"  {year}   {len(members):>3} members  +{len(joined):<3} {joined[:6]}"
            f"{'...' if len(joined) > 6 else ''}"
        )
        previous = members

    print(
        "\nThat growth is the whole point. A universe picked from what exists now\n"
        "would have put all 38 of these in a 2013 backtest, and every one of the\n"
        "36 that had not launched yet would be there because it survived to be\n"
        "listed in the fixture."
    )


def why_the_union_grid(root: Path) -> None:
    section("3. Why a varying universe needs a union grid")
    print(
        "A Panel intersects timestamps by default, which guarantees every symbol\n"
        "prints at every row. Hand that a symbol that listed mid-window and it\n"
        "deletes every earlier row FOR EVERY OTHER SYMBOL — silently undoing the\n"
        "point-in-time selection that produced it.\n"
    )
    if not universe_path(root).exists():
        print("(Needs an imported archive to demonstrate on real data.)")
        return

    registry = UniverseRegistry.load_from_cache(root)
    symbols = sorted(x.symbol for x in registry.listings)[:6]
    interval = registry.listings[0].interval
    market = registry.listings[0].market

    intersection = Panel.load(root, market, symbols, interval)
    union = Panel.load(root, market, symbols, interval, grid="union")
    print(f"  intersection : {intersection}")
    print(f"  union        : {union}")
    print(
        f"\n  The intersection keeps {intersection.shape[0]:,} instants; the union "
        f"keeps {union.shape[0]:,}.\n"
        f"  This archive's outages are synchronised, so the gap is small — on a\n"
        f"  universe with real listings and delistings it would not be."
    )
    print(
        "\n  Ranking a union grid raises rather than guessing what a missing\n"
        "  symbol's rank should be. That is a modelling choice and it belongs to\n"
        "  whoever is doing the modelling."
    )


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CACHE
    imported_archive(root)
    real_history()
    why_the_union_grid(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
