"""Market data connector skeletons.

**Nothing here performs I/O.** These are the declared *shape* of a data source:
what it is called, what credentials it would need, and what methods a working
implementation must provide. Every fetch method raises ``NotImplementedError``.

Why declare the shape now and implement it later:

- It fixes the interface while it is cheap to change, before three venues have
  each grown their own accidental one.
- It records, in code rather than a document, which venues need credentials and
  which do not. Binance klines are public; saying so explicitly stops someone
  adding keys that were never required.
- It makes the boundary obvious in review. A diff that turns one of these
  ``raise`` statements into a network call is very hard to miss.

The one working fetch path in this repository is ``scripts/fetch_binance.py``,
which is deliberately a standalone script rather than library code — see
``docs/PLAN.md`` on why live capability stays out of the package.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from trading_platform.core.credentials import CredentialSet
from trading_platform.core.events import Bar
from trading_platform.data.binance import FundingRate


class ConnectorNotImplementedError(NotImplementedError):
    """Raised by every skeleton connector method.

    A distinct type so a caller can tell "this venue is not wired up yet" apart
    from a genuine bug, and so tests can assert on it precisely.
    """


@runtime_checkable
class MarketDataConnector(Protocol):
    """What a working market-data source must provide."""

    @property
    def venue(self) -> str: ...

    @property
    def credentials(self) -> CredentialSet: ...

    def symbols(self) -> tuple[str, ...]:
        """Instruments this venue offers."""
        ...

    def fetch_bars(
        self, symbol: str, interval: str, start: datetime, end: datetime | None = None
    ) -> list[Bar]:
        """Historical OHLCV, close-stamped and in chronological order."""
        ...

    def fetch_funding(
        self, symbol: str, start: datetime, end: datetime | None = None
    ) -> list[FundingRate]:
        """Historical funding settlements. Empty for venues without perpetuals."""
        ...


class _SkeletonConnector:
    """Shared base: declares identity and credentials, refuses to do any work."""

    _venue: str = "unknown"
    _credentials: CredentialSet = CredentialSet(venue="unknown")
    _phase: str = "docs/PLAN.md Phase 2"

    @property
    def venue(self) -> str:
        return self._venue

    @property
    def credentials(self) -> CredentialSet:
        return self._credentials

    def _refuse(self, what: str) -> ConnectorNotImplementedError:
        return ConnectorNotImplementedError(
            f"{self._venue}.{what} is a skeleton and performs no I/O. "
            f"See {self._phase}. The one working fetch path is "
            f"scripts/fetch_binance.py, which is run manually."
        )

    def symbols(self) -> tuple[str, ...]:
        raise self._refuse("symbols")

    def fetch_bars(
        self, symbol: str, interval: str, start: datetime, end: datetime | None = None
    ) -> list[Bar]:
        raise self._refuse("fetch_bars")

    def fetch_funding(
        self, symbol: str, start: datetime, end: datetime | None = None
    ) -> list[FundingRate]:
        raise self._refuse("fetch_funding")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(venue={self._venue!r}, skeleton, {self._credentials})"


class BinanceConnector(_SkeletonConnector):
    """Binance spot and USD-M futures.

    **Declares no credentials on purpose.** Klines and funding history are
    public endpoints — `scripts/fetch_binance.py` reaches them with no key at
    all. Adding API keys here would grant account access this project has no
    use for, so the empty credential set is the security decision, not an
    oversight.
    """

    _venue = "binance"
    _credentials = CredentialSet(venue="binance")  # public endpoints only


class DeribitConnector(_SkeletonConnector):
    """Deribit crypto options (roadmap steps 41-46).

    Deribit's public market-data endpoints also need no credentials for
    historical candles, but the options chain and instrument metadata are
    easier to page with an authenticated client, so the read-only pair is
    declared here for when that work starts.
    """

    _venue = "deribit"
    _phase = "docs/PLAN.md Phase 3"
    _credentials = CredentialSet.from_env("deribit", "DERIBIT_CLIENT_ID", "DERIBIT_CLIENT_SECRET")


def available_connectors() -> dict[str, _SkeletonConnector]:
    """Every declared connector, keyed by venue. All are skeletons."""
    return {c.venue: c for c in (BinanceConnector(), DeribitConnector())}
