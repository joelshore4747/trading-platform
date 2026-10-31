"""Which instruments existed, as of a date — and refusing to guess.

`CLAUDE.md` lists among the forms of lookahead: *"Survivorship: a symbol
universe chosen because those symbols exist today."* This module is the
mechanism that makes that answerable, and its whole design turns on one
distinction:

    **Observed absence is not delisting.**

From bar data alone you cannot tell a delisting from a collector outage from
"our archive ends here". They look identical. So an instrument's lifetime has
two independent pairs of fields — what the data shows (`first_seen`,
`last_seen`) and what someone has asserted (`listed`, `delisted`, default
`None` meaning nobody has said) — and a query reports which of the two it used.

**Censoring.** A symbol whose `first_seen` is the earliest bar in the whole
archive tells you nothing about when it listed; the boundary is the archive's,
not the instrument's. Reading it as a listing date does not merely guess, it
*fabricates an event*: on this project's own cache all 39 symbols share a
first bar, and treating that as 39 simultaneous listings would assert the
entire universe was unlisted the previous day, on a venue that has traded them
since 2019. Such bounds are `CENSORED`, and censored is not a date.

So `members()` raises rather than falling back. If you want to treat observed
bounds as real, `assume_observed_bounds()` makes that a line of code a reviewer
can grep for — the same shape as `assume_pegged()` in `core.fx`, and for the
same reason.

**What this can and cannot be tested against.** Real listing dates exist and
are reachable: the Coin Metrics community archive gives first-priced dates for
38 assets spanning 2010-2026, and `members()` over them grows 1 asset in 2011
to 38 in 2024, with ETH crossing exactly at its real 2015-08-08 date. But it
records no delistings — a reference rate keeps publishing for FTT and LUNA
alike — so the "excludes not-yet-listed" half is verifiable against history and
the "returns since-delisted" half is not. And every one of those dates predates
this project's 2026 bar window, so for any `as_of` actually backtestable here
membership returns everything: **the real dates exercise the lookup and cannot
change a backtest number in this repository.**
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from pathlib import Path

__all__ = [
    "Basis",
    "Listing",
    "Membership",
    "UniverseRegistry",
    "UnknownListing",
]


class UnknownListing(LookupError):
    """Membership was asked for where the answer is not known.

    Raised rather than answered, because the plausible answer is the dangerous
    one: it is indistinguishable from a real one and there is no exception to
    notice.
    """


class Basis(Enum):
    """How a membership answer was arrived at."""

    #: A vendor or an operator stated the dates. The only trustworthy basis.
    ASSERTED = "asserted"
    #: Inferred from the bars held, with both edges strictly inside the
    #: archive — so the instrument genuinely started and stopped printing
    #: within a window that extends past it on both sides.
    OBSERVED = "observed"
    #: The observed bound sits at the archive's own edge and is therefore
    #: evidence of nothing.
    CENSORED = "censored"

    def __bool__(self) -> bool:
        """`CENSORED` is falsy, so `if is_member(...)` cannot read a
        non-answer as a yes. The enum is truthy by default, which made the
        obvious idiom quietly wrong."""
        return self is not Basis.CENSORED


@dataclass(frozen=True, slots=True)
class Listing:
    """One instrument's lifetime, observed and asserted kept apart."""

    market: str
    symbol: str
    interval: str
    #: OBSERVED — the first and last bars actually held, and how many.
    first_seen: datetime
    last_seen: datetime
    bars: int
    #: ASSERTED — `None` means nobody has said, which is not the same as "no".
    listed: date | None = None
    delisted: date | None = None

    def __post_init__(self) -> None:
        if self.first_seen.tzinfo is None or self.last_seen.tzinfo is None:
            raise ValueError(f"{self.symbol}: naive timestamps cannot be compared to UTC bars")
        if self.last_seen < self.first_seen:
            raise ValueError(f"{self.symbol}: last_seen {self.last_seen} precedes first_seen")
        if self.bars < 0:
            raise ValueError(f"{self.symbol}: negative bar count {self.bars}")
        if self.listed is not None and self.delisted is not None and self.delisted < self.listed:
            raise ValueError(f"{self.symbol}: delisted {self.delisted} precedes listed")

    @property
    def key(self) -> tuple[str, str, str]:
        """Keyed as the cache is keyed. The same symbol trades on several
        markets at different prices, so symbol alone is not an identity."""
        return (self.market, self.symbol, self.interval)

    def asserted_bounds(self) -> tuple[datetime | None, datetime | None]:
        """The asserted lifetime as instants. Each end independently `None`.

        **The ends are independent on purpose.** They used to be all-or-nothing
        — `listed is None` discarded an asserted `delisted` entirely — so the
        strongest statement anyone can make about an instrument, "it stopped
        trading on this date", was thrown away, and the symbol fell back to
        observed bars that ran well past it. That is the mirror image of
        survivorship: a zombie, tradeable long after it died.

        **Convention, stated because an unstated one is a one-day lookahead in
        the universe dimension.** An asserted `listed` date means first
        tradeable at the *close* of that UTC day, matching this project's
        close-stamped bars: a symbol listed 2024-03-01 is not a member at
        2024-03-01T00:30 and is a member at 2024-03-01T24:00. `delisted` is
        the last tradeable day, so membership ends at that day's close. At
        intraday resolution the `listed` end rounds forward, which excludes a
        symbol for part of its first day — conservative, and therefore safe.
        """
        start = (
            None
            if self.listed is None
            else datetime.combine(self.listed, time.min, tzinfo=UTC) + timedelta(days=1)
        )
        end = (
            None
            if self.delisted is None
            else datetime.combine(self.delisted, time.min, tzinfo=UTC) + timedelta(days=1)
        )
        return start, end


@dataclass(frozen=True, slots=True)
class Membership:
    """The universe at one instant, with how each answer was reached."""

    as_of: datetime
    symbols: tuple[str, ...]
    basis: Mapping[str, Basis]
    #: Symbols whose status at `as_of` could not be determined.
    censored: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.symbols)

    def __contains__(self, symbol: str) -> bool:
        return symbol in self.symbols

    def __iter__(self) -> Iterator[str]:
        return iter(self.symbols)

    def warning(self) -> str | None:
        """What is unsound about this answer, if anything.

        Non-`None` whenever any membership rests on observation. That includes
        bounds accepted through `assume_observed_bounds` — accepting an
        assumption does not convert it into a fact, so the warning survives the
        acceptance and fires on every query.
        """
        observed = sorted(s for s, b in self.basis.items() if b is Basis.OBSERVED)
        if not observed:
            return None
        shown = observed[:5]
        return (
            f"{len(observed)} of {len(self.symbols)} memberships rest on observed "
            f"bars rather than asserted dates ({shown}"
            f"{'...' if len(observed) > 5 else ''}). A symbol that stopped printing "
            "may have delisted, or the collector may have stopped, or the archive "
            "may simply end there. Those are three different events and bar data "
            "cannot tell them apart."
        )


@dataclass(frozen=True, slots=True)
class UniverseRegistry:
    """Point-in-time membership. Frozen: a registry mutated mid-run is the
    worst kind of point-in-time violation."""

    listings: tuple[Listing, ...]
    #: Symbols whose observed bounds the caller has explicitly accepted as
    #: real, via `assume_observed_bounds`. Their basis stays OBSERVED rather
    #: than becoming ASSERTED — the assumption is recorded, not laundered into
    #: a fact, so `Membership.warning()` keeps saying so on every query.
    assumed: frozenset[str] = frozenset()
    #: Symbol -> (start, end, basis), precomputed so `is_member` is O(1).
    #: Declared as a field rather than stashed on the instance because
    #: `slots=True` means an undeclared attribute has nowhere to live.
    _bounds: dict[str, tuple[datetime, datetime, Basis]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        seen = set()
        by_symbol: dict[str, tuple[str, str, str]] = {}
        for listing in self.listings:
            if listing.key in seen:
                raise ValueError(f"duplicate listing for {listing.key}")
            seen.add(listing.key)
            # Membership is answered by bare symbol, because that is what a
            # feed and a panel are keyed by. So two markets quoting the same
            # symbol cannot coexist here: `_bounds` would keep whichever came
            # last in the tuple, and `is_member` would answer from one market's
            # listing dates about another's instrument — flipping on tuple
            # order, with `listing()` (first match) disagreeing. Rejected
            # rather than silently resolved; slice with `for_market` first.
            if listing.symbol in by_symbol:
                raise ValueError(
                    f"{listing.symbol!r} appears under {by_symbol[listing.symbol]} and "
                    f"{listing.key}. Membership is answered by symbol alone, so one "
                    "registry cannot hold two markets' versions of it. Build one "
                    "registry per market, or call for_market() to slice."
                )
            by_symbol[listing.symbol] = listing.key
        # A tuple-building `members()` called per bar event would be
        # O(symbols) per event and blow the engine's per-event budget, so the
        # primitive is the cheap one and the reporting call is built on top.
        object.__setattr__(self, "_bounds", self._compute_bounds())

    # -- Archive edges, which is where censoring is decided --------------------

    @property
    def archive_start(self) -> datetime | None:
        return min((x.first_seen for x in self.listings), default=None)

    @property
    def archive_end(self) -> datetime | None:
        return max((x.last_seen for x in self.listings), default=None)

    def _compute_bounds(self) -> dict[str, tuple[datetime, datetime, Basis]]:
        # Edges computed over listings with NO asserted dates. An asserted
        # listing that happens to span the archive would otherwise widen these
        # bounds and un-censor every other symbol — so a second import's own
        # window edges would read as real listing events, which is exactly the
        # fabrication this class exists to prevent.
        observed_only = [x for x in self.listings if x.listed is None and x.delisted is None]
        edge_start = min((x.first_seen for x in observed_only), default=None)
        edge_end = max((x.last_seen for x in observed_only), default=None)

        out: dict[str, tuple[datetime, datetime, Basis]] = {}
        for listing in self.listings:
            asserted_start, asserted_end = listing.asserted_bounds()
            start = asserted_start if asserted_start is not None else listing.first_seen
            end = asserted_end if asserted_end is not None else listing.last_seen

            # Each end is graded on its own, so half an assertion still counts.
            left_censored = (
                asserted_start is None
                and edge_start is not None
                and listing.first_seen <= edge_start
            )
            right_censored = (
                asserted_end is None and edge_end is not None and listing.last_seen >= edge_end
            )
            if asserted_start is not None and asserted_end is not None:
                basis = Basis.ASSERTED
            elif (left_censored or right_censored) and listing.symbol not in self.assumed:
                basis = Basis.CENSORED
            else:
                basis = Basis.OBSERVED
            out[listing.symbol] = (start, end, basis)
        return out

    @property
    def censored(self) -> tuple[str, ...]:
        """Symbols whose lifetime is bounded by the archive rather than by an
        event. On a single-window import this is usually everything."""
        return tuple(sorted(s for s, (_, _, b) in self._bounds.items() if b is Basis.CENSORED))

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(x.symbol for x in self.listings))

    def listing(self, symbol: str) -> Listing:
        for candidate in self.listings:
            if candidate.symbol == symbol:
                return candidate
        raise UnknownListing(f"{symbol!r} is not in this registry")

    # -- The primitive ---------------------------------------------------------

    def is_member(self, symbol: str, at: datetime) -> Basis | None:
        """`Basis` if `symbol` was a member at `at`, else `None`. O(1).

        Returns the basis rather than a bool so a caller cannot use the answer
        without seeing how it was reached. `Basis.CENSORED` means the answer is
        a guess: it is `None`-adjacent, and `members()` refuses on it.
        """
        found = self._bounds.get(symbol)
        if found is None:
            return None
        start, end, basis = found
        return basis if start <= at <= end else None

    # -- The reporting call ----------------------------------------------------

    def members(self, as_of: datetime) -> Membership:
        """The universe at `as_of`. Raises when any answer would be a guess."""
        censored = self.censored
        if censored:
            raise UnknownListing(
                f"{len(censored)} symbol(s) have lifetimes bounded by the archive's "
                f"own edges rather than by a listing event: {list(censored[:8])}"
                f"{'...' if len(censored) > 8 else ''}. Their observed first and last "
                "bars are evidence of when collection started and stopped, not of "
                "when the instrument listed and delisted. Supply asserted dates, or "
                "call assume_observed_bounds() to state that you are treating the "
                "archive's edges as real."
            )
        basis: dict[str, Basis] = {}
        for listing in self.listings:
            found = self.is_member(listing.symbol, as_of)
            if found is not None:
                basis[listing.symbol] = found
        return Membership(as_of=as_of, symbols=tuple(sorted(basis)), basis=basis, censored=())

    def members_between(self, start: datetime, end: datetime) -> tuple[str, ...]:
        """Symbols that were members at any point in `[start, end]`.

        The selection a windowed load needs: a symbol that listed mid-window
        belongs in the panel, with its pre-listing rows absent rather than the
        whole symbol absent.
        """
        censored = self.censored
        if censored:
            raise UnknownListing(
                f"{len(censored)} symbol(s) have lifetimes bounded by the archive's "
                f"own edges: {list(censored[:8])}{'...' if len(censored) > 8 else ''}. "
                "Supply asserted dates, or call assume_observed_bounds()."
            )
        # Interval OVERLAP, not membership at the two endpoints. Unioning the
        # endpoints excluded exactly the symbols that listed after `start` and
        # delisted before `end` — which is not an approximation of survivorship
        # bias, it IS survivorship bias, selecting for names alive at the window
        # edges, inside the function written to prevent it.
        return tuple(
            sorted(
                symbol for symbol, (lo, hi, _) in self._bounds.items() if lo <= end and hi >= start
            )
        )

    # -- Turning an observation into an assertion, visibly ---------------------

    def for_market(self, market: str, interval: str | None = None) -> UniverseRegistry:
        """A registry narrowed to one market, so bare symbols are unambiguous."""
        return UniverseRegistry(
            listings=tuple(
                x
                for x in self.listings
                if x.market == market and (interval is None or x.interval == interval)
            ),
            assumed=self.assumed,
        )

    def assume_observed_bounds(self) -> UniverseRegistry:
        """A registry that treats observed first and last bars as real dates.

        An assumption, and a false one wherever the archive's edge is not the
        instrument's: on this project's own cache it asserts that all 39
        symbols listed on the same minute and delisted on the same minute,
        neither of which happened. It exists so that making the assumption is
        something someone typed and a reviewer can find, rather than a
        fallback taken silently on every call.
        """
        return UniverseRegistry(
            listings=self.listings,
            assumed=frozenset(
                x.symbol for x in self.listings if x.listed is None or x.delisted is None
            ),
        )

    # -- Building from what is on disk ----------------------------------------

    @classmethod
    def from_cache(cls, root: Path | str, market: str, interval: str) -> UniverseRegistry:
        """Observe every cached series' first bar, last bar, and count.

        Reads the `timestamp_ms` column only — the prices are irrelevant to a
        lifetime and decoding them would cost the whole file. Asserted dates
        are left `None`, which is the honest state: a cache records what was
        collected, and nothing in it is a statement about listing.
        """
        import pyarrow.parquet as pq

        from trading_platform.data.cache import cache_path

        base = Path(root) / market
        if not base.exists():
            raise UnknownListing(f"No cached market at {base}")

        listings: list[Listing] = []
        for directory in sorted(p for p in base.iterdir() if p.is_dir()):
            path = cache_path(root, market, directory.name, interval)
            if not path.exists():
                continue
            stamps = (
                pq.read_table(path, columns=["timestamp_ms"]).column("timestamp_ms").to_pylist()
            )
            if not stamps:
                continue
            listings.append(
                Listing(
                    market=market,
                    symbol=directory.name,
                    interval=interval,
                    first_seen=datetime.fromtimestamp(stamps[0] / 1000, UTC),
                    last_seen=datetime.fromtimestamp(stamps[-1] / 1000, UTC),
                    bars=len(stamps),
                )
            )
        return cls(listings=tuple(listings))

    # -- Persistence -----------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(
            {
                "listings": [
                    {
                        "market": x.market,
                        "symbol": x.symbol,
                        "interval": x.interval,
                        "first_seen": x.first_seen.isoformat(),
                        "last_seen": x.last_seen.isoformat(),
                        "bars": x.bars,
                        "listed": x.listed.isoformat() if x.listed else None,
                        "delisted": x.delisted.isoformat() if x.delisted else None,
                    }
                    for x in sorted(self.listings, key=lambda x: x.key)
                ]
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> UniverseRegistry:
        payload = json.loads(raw)
        return cls(
            listings=tuple(
                Listing(
                    market=row["market"],
                    symbol=row["symbol"],
                    interval=row["interval"],
                    first_seen=datetime.fromisoformat(row["first_seen"]),
                    last_seen=datetime.fromisoformat(row["last_seen"]),
                    bars=row["bars"],
                    listed=date.fromisoformat(row["listed"]) if row["listed"] else None,
                    delisted=date.fromisoformat(row["delisted"]) if row["delisted"] else None,
                )
                for row in payload["listings"]
            )
        )

    def save_to_cache(self, root: Path | str) -> Path:
        """Write beside the cache it describes."""
        from trading_platform.data.cache import universe_path

        return self.save(universe_path(root))

    @classmethod
    def load_from_cache(cls, root: Path | str) -> UniverseRegistry:
        from trading_platform.data.cache import universe_path

        return cls.load(universe_path(root))

    def save(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_suffix(".json.tmp")
        staging.write_text(self.to_json() + "\n", encoding="utf-8")
        staging.replace(target)
        return target

    @classmethod
    def load(cls, path: Path | str) -> UniverseRegistry:
        source = Path(path)
        if not source.exists():
            raise UnknownListing(
                f"No universe registry at {source}. Import data first with "
                "scripts/import_vendor.py, which writes one."
            )
        return cls.from_json(source.read_text(encoding="utf-8"))

    def __repr__(self) -> str:
        censored = len(self.censored)
        return (
            f"UniverseRegistry({len(self.listings)} listings"
            + (f", {censored} censored" if censored else "")
            + ")"
        )
