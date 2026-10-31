"""Point-in-time membership, and the refusal to guess it.

The property under test throughout: **observed absence is not delisting**. A
symbol that stops printing may have delisted, or the collector may have
stopped, or the archive may simply end there. Bar data cannot tell those apart,
so the registry does not pretend to.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.core.events import Bar
from trading_platform.data.cache import cache_path, ensure_layout, universe_path, write_bars
from trading_platform.data.universe import (
    Basis,
    Listing,
    UniverseRegistry,
    UnknownListing,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "coinmetrics_listings.json"


def listing(
    symbol: str,
    first_day: int,
    last_day: int,
    *,
    listed: date | None = None,
    delisted: date | None = None,
    bars: int = 10,
) -> Listing:
    return Listing(
        market="um",
        symbol=symbol,
        interval="1d",
        first_seen=START + timedelta(days=first_day),
        last_seen=START + timedelta(days=last_day),
        bars=bars,
        listed=listed,
        delisted=delisted,
    )


@pytest.fixture
def registry() -> UniverseRegistry:
    """One fully asserted lifetime, one asserted delisting, one that nobody
    has stated anything about.

    Note `ASSERTED` carries BOTH ends. A listing date without a delisting date
    leaves the *end* unasserted, and if that end sits at the archive's edge it
    is censored like any other — the ends are graded independently, so half an
    assertion is half an answer.
    """
    return UniverseRegistry(
        listings=(
            listing("ASSERTED", 0, 20, listed=date(2026, 1, 5), delisted=date(2026, 1, 19)),
            listing("DIED", 0, 20, listed=date(2026, 1, 1), delisted=date(2026, 1, 9)),
            listing("UNKNOWN", 0, 20),
        )
    )


# -- The refusal --------------------------------------------------------------


def test_members_refuses_while_any_lifetime_is_bounded_by_the_archive(
    registry: UniverseRegistry,
) -> None:
    """`UNKNOWN` starts at the archive's first bar and ends at its last, so its
    observed bounds are evidence of when collection ran, not of a listing."""
    assert registry.censored == ("UNKNOWN",)
    with pytest.raises(UnknownListing, match="bounded by the archive"):
        registry.members(START + timedelta(days=10))


def test_the_refusal_names_what_it_could_not_answer(registry: UniverseRegistry) -> None:
    with pytest.raises(UnknownListing) as excinfo:
        registry.members(START)
    assert "UNKNOWN" in str(excinfo.value)


def test_accepting_the_assumption_does_not_convert_it_into_a_fact(
    registry: UniverseRegistry,
) -> None:
    """`assume_observed_bounds` is the `assume_pegged` of this module: it makes
    the assumption a line someone typed. It must not launder that into an
    asserted date, or the warning would go quiet exactly when it is needed."""
    accepted = registry.assume_observed_bounds()
    membership = accepted.members(START + timedelta(days=10))

    assert membership.basis["UNKNOWN"] is Basis.OBSERVED
    assert membership.basis["ASSERTED"] is Basis.ASSERTED
    warning = membership.warning()
    assert warning is not None
    assert "UNKNOWN" in warning


def test_a_fully_asserted_registry_warns_about_nothing() -> None:
    fully = UniverseRegistry(
        listings=(
            listing("A", 0, 20, listed=date(2026, 1, 2), delisted=date(2026, 1, 19)),
            listing("B", 0, 20, listed=date(2026, 1, 3), delisted=date(2026, 1, 19)),
        )
    )
    assert fully.censored == ()
    assert fully.members(START + timedelta(days=10)).warning() is None


# -- The stated convention ----------------------------------------------------


def test_an_asserted_listing_date_takes_effect_at_that_day_s_close(
    registry: UniverseRegistry,
) -> None:
    """Bars are close-stamped, so a symbol listed on the 5th is first tradeable
    at the close of the 5th. Leaving this unstated would be a one-day lookahead
    in the universe dimension — the same defect as an open-stamped bar."""
    accepted = registry.assume_observed_bounds()
    close_of_4th = datetime(2026, 1, 5, tzinfo=UTC)
    close_of_5th = datetime(2026, 1, 6, tzinfo=UTC)

    assert accepted.is_member("ASSERTED", close_of_4th) is None
    assert accepted.is_member("ASSERTED", close_of_5th) is Basis.ASSERTED


def test_an_asserted_delisting_date_is_the_last_tradeable_day(
    registry: UniverseRegistry,
) -> None:
    accepted = registry.assume_observed_bounds()
    close_of_9th = datetime(2026, 1, 10, tzinfo=UTC)

    assert accepted.is_member("DIED", close_of_9th) is Basis.ASSERTED
    assert accepted.is_member("DIED", close_of_9th + timedelta(minutes=1)) is None


def test_is_member_returns_the_basis_rather_than_a_bool(
    registry: UniverseRegistry,
) -> None:
    """A caller cannot use the answer without seeing how it was reached."""
    assert registry.is_member("ASSERTED", START + timedelta(days=10)) is Basis.ASSERTED
    assert registry.is_member("UNKNOWN", START + timedelta(days=10)) is Basis.CENSORED
    assert registry.is_member("NOSUCH", START) is None


# -- Construction and identity ------------------------------------------------


def test_a_listing_rejects_impossible_lifetimes() -> None:
    with pytest.raises(ValueError, match="precedes first_seen"):
        listing("X", 10, 0)
    with pytest.raises(ValueError, match="precedes listed"):
        listing("X", 0, 10, listed=date(2026, 1, 9), delisted=date(2026, 1, 2))
    with pytest.raises(ValueError, match="naive timestamps"):
        Listing(
            market="um",
            symbol="X",
            interval="1d",
            first_seen=datetime(2026, 1, 1),
            last_seen=datetime(2026, 1, 2, tzinfo=UTC),
            bars=1,
        )


def test_two_markets_quoting_one_symbol_are_refused(registry: UniverseRegistry) -> None:
    """This test used to assert only that construction did not raise — and
    construction never did. The collision was in the bounds table, which the
    test never queried, so it passed under any behaviour at all.

    What it was missing: membership is answered by bare symbol, because that is
    what a feed and a panel are keyed by. Two markets' versions of one symbol
    therefore cannot coexist, and holding both silently kept whichever came
    last in the tuple. An audit demonstrated the answer flipping on tuple
    order: spot BTCUSDT listed 2017-08-17 and perpetual BTCUSDT listed
    2019-09-08 gave `None` in one order and `ASSERTED` in the other, making
    the perpetual a member fifteen months before it existed.
    """
    spot = Listing(
        market="spot",
        symbol="BTCUSDT",
        interval="1d",
        first_seen=START,
        last_seen=START + timedelta(days=10),
        bars=10,
        listed=date(2017, 8, 17),
        delisted=date(2030, 1, 1),
    )
    perpetual = Listing(
        market="um",
        symbol="BTCUSDT",
        interval="1d",
        first_seen=START,
        last_seen=START + timedelta(days=10),
        bars=10,
        listed=date(2019, 9, 8),
        delisted=date(2030, 1, 1),
    )
    assert spot.key != perpetual.key

    with pytest.raises(ValueError, match="cannot hold two markets"):
        UniverseRegistry(listings=(spot, perpetual))

    # And the same key twice is still a plain duplicate.
    with pytest.raises(ValueError, match="duplicate listing"):
        UniverseRegistry(listings=(spot, spot))


def test_for_market_slices_a_registry_down_to_unambiguous_symbols() -> None:
    """The sanctioned way to hold several markets: one registry each."""
    both = UniverseRegistry(
        listings=(
            listing("AAA", 0, 10, listed=date(2025, 1, 1), delisted=date(2030, 1, 1)),
            Listing(
                market="spot",
                symbol="BBB",
                interval="1d",
                first_seen=START,
                last_seen=START + timedelta(days=10),
                bars=10,
                listed=date(2025, 1, 1),
                delisted=date(2030, 1, 1),
            ),
        )
    )
    assert both.for_market("um").symbols == ("AAA",)
    assert both.for_market("spot").symbols == ("BBB",)


# -- Building from the cache, and persistence ---------------------------------


def build_cache(tmp_path: Path, spans: dict[str, range]) -> Path:
    ensure_layout(tmp_path)
    for symbol, days in spans.items():
        write_bars(
            cache_path(tmp_path, "um", symbol, "1d"),
            [
                Bar(
                    symbol=symbol,
                    timestamp=START + timedelta(days=d),
                    open=Decimal("100"),
                    high=Decimal("100"),
                    low=Decimal("100"),
                    close=Decimal("100"),
                    volume=Decimal("1"),
                )
                for d in days
            ],
        )
    return tmp_path


def test_from_cache_observes_first_last_and_count(tmp_path: Path) -> None:
    root = build_cache(tmp_path, {"AAA": range(10), "BBB": range(3, 8)})
    built = UniverseRegistry.from_cache(root, "um", "1d")

    aaa = built.listing("AAA")
    assert (aaa.first_seen, aaa.last_seen, aaa.bars) == (
        START,
        START + timedelta(days=9),
        10,
    )
    assert built.listing("BBB").bars == 5
    # Nothing in a cache is a statement about listing.
    assert all(x.listed is None and x.delisted is None for x in built.listings)


def test_a_symbol_strictly_inside_the_archive_is_observed_not_censored(
    tmp_path: Path,
) -> None:
    """The distinction the whole module turns on. AAA spans the archive, so its
    bounds are the archive's. BBB starts after and ends before, so its bounds
    are its own."""
    root = build_cache(tmp_path, {"AAA": range(10), "BBB": range(3, 8)})
    built = UniverseRegistry.from_cache(root, "um", "1d")

    assert built.censored == ("AAA",)
    assert built.is_member("BBB", START + timedelta(days=5)) is Basis.OBSERVED
    assert built.is_member("BBB", START) is None


def test_the_registry_round_trips_through_the_cache_root(tmp_path: Path) -> None:
    root = build_cache(tmp_path, {"AAA": range(10), "BBB": range(3, 8)})
    built = UniverseRegistry.from_cache(root, "um", "1d")
    written = built.save_to_cache(root)

    assert written == universe_path(root)
    assert UniverseRegistry.load_from_cache(root) == built


def test_loading_a_missing_registry_says_how_to_make_one(tmp_path: Path) -> None:
    with pytest.raises(UnknownListing, match="import_vendor"):
        UniverseRegistry.load_from_cache(tmp_path)


# -- Against real listing dates -----------------------------------------------


def real_listings() -> UniverseRegistry:
    """Coin Metrics first-priced dates, vendored so this does not need network.

    These are the closest thing to real listing dates obtainable here. Note the
    limit recorded in the fixture: `last_priced` is not a delisting — the
    source keeps publishing a reference rate indefinitely.
    """
    payload = json.loads(FIXTURE.read_text())
    return UniverseRegistry(
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


def test_the_universe_grows_over_real_history() -> None:
    """The point of the whole exercise: the universe as it was, not as it is."""
    registry = real_listings()
    counts = {
        year: len(registry.members(datetime(year, 6, 1, tzinfo=UTC)))
        for year in (2011, 2015, 2017, 2019, 2021, 2024)
    }
    assert counts[2011] == 1, "only bitcoin had a reference price in 2011"
    assert counts[2011] < counts[2015] < counts[2017] < counts[2019] < counts[2021]
    assert counts[2024] == 38


def test_a_symbol_is_absent_before_its_real_listing_date() -> None:
    """ETH first carried a USD reference price on 2015-08-08. A universe that
    includes it in 2014 is one chosen by knowing who survived."""
    registry = real_listings()
    assert "ETH" not in registry.members(datetime(2014, 6, 1, tzinfo=UTC))
    assert "ETH" in registry.members(datetime(2016, 6, 1, tzinfo=UTC))
    assert "BTC" in registry.members(datetime(2014, 6, 1, tzinfo=UTC))


def test_a_symbol_that_stopped_being_priced_leaves_the_universe() -> None:
    """One real exit exists in this fixture — MKR's reference rate ends
    2026-03-25 while every other asset runs to 2026-05-23. It exercises the
    'returns since-delisted names' path that I had recorded as untestable.

    It is still an *observed* end, though: the data does not say whether MKR
    delisted, rebranded, or merely fell out of coverage. That is exactly the
    distinction this module refuses to collapse.
    """
    registry = real_listings()
    # Close-stamped, per the convention: the last tradeable day 2026-03-25
    # ends at 2026-03-26T00:00, so that instant is still membership.
    close_of_last_day = datetime(2026, 3, 26, tzinfo=UTC)
    close_of_next_day = datetime(2026, 3, 27, tzinfo=UTC)

    assert "MKR" in registry.members(close_of_last_day)
    assert "MKR" not in registry.members(close_of_next_day)
    assert len(registry.members(close_of_next_day)) == len(registry.members(close_of_last_day)) - 1


def test_members_between_keeps_a_symbol_that_lived_and_died_inside_the_window() -> None:
    """The worst defect an audit found in this module, and it was inside the
    function written to prevent exactly it.

    `members_between` unioned membership at the two endpoints. A symbol that
    listed after `start` and delisted before `end` is a member at neither, so
    it was excluded from the resolved universe — which is not an approximation
    of survivorship bias, it *is* survivorship bias, selecting precisely for
    names alive at the window edges. It emitted no warning, and it produced a
    better-looking result.
    """
    window = UniverseRegistry(
        listings=(
            listing("SURVIVOR", 0, 100, listed=date(2025, 12, 31), delisted=date(2030, 1, 1)),
            listing("DIED", 0, 100, listed=date(2026, 1, 5), delisted=date(2026, 1, 15)),
        )
    )
    start, end = START, START + timedelta(days=100)

    assert window.is_member("DIED", START + timedelta(days=10)) is Basis.ASSERTED
    assert "DIED" not in window.members(start).symbols
    assert "DIED" not in window.members(end).symbols
    assert window.members_between(start, end) == ("DIED", "SURVIVOR")


def test_an_asserted_delisting_survives_a_missing_listing_date() -> None:
    """The mirror of survivorship: a zombie.

    `asserted_bounds` used to be all-or-nothing, so a `delisted` date with no
    `listed` date was discarded entirely and the symbol fell back to observed
    bars running well past its stated end — tradeable long after it died. The
    ends are graded independently now.
    """
    zombie = UniverseRegistry(
        listings=(
            listing("GONE", 0, 100, delisted=date(2026, 1, 10)),
            listing("OTHER", 0, 100, listed=date(2025, 12, 31)),
        )
    )
    long_after = START + timedelta(days=60)
    assert zombie.is_member("GONE", long_after) is None
    # And accepting observed bounds must not resurrect it either.
    assert zombie.assume_observed_bounds().is_member("GONE", long_after) is None


def test_an_asserted_listing_cannot_un_censor_an_unrelated_import() -> None:
    """Censoring is decided against the edges of the *observed* data.

    It used to compare against the archive-wide min and max, which asserted
    listings also widened. So a long-history symbol with known dates pushed the
    edges out past a second import's window, and every symbol in that import
    was graded OBSERVED — its own collection boundaries reading as real listing
    events, which is the fabrication this module exists to refuse.
    """
    mixed = UniverseRegistry(
        listings=(
            listing("BTC", 0, 100, listed=date(2010, 1, 1)),
            listing("NEWA", 30, 70),
            listing("NEWB", 30, 70),
        )
    )
    assert "NEWA" in mixed.censored
    assert "NEWB" in mixed.censored
    with pytest.raises(UnknownListing):
        mixed.members(START + timedelta(days=5))


def test_a_censored_basis_is_falsy_so_the_obvious_idiom_is_not_wrong() -> None:
    """`if registry.is_member(s, t):` is what anyone writes. An enum is truthy
    by default, so a non-answer read as a yes."""
    solo = UniverseRegistry(listings=(listing("SOLO", 0, 100),))
    verdict = solo.is_member("SOLO", START + timedelta(days=50))

    assert verdict is Basis.CENSORED
    assert not verdict
    assert bool(Basis.ASSERTED) and bool(Basis.OBSERVED)
