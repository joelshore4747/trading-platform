"""Broker skeletons.

**No broker here can place an order.** Every method raises. This module defines
the interface a live adapter would implement so that the boundary is explicit
and reviewable, not so that trading can begin.

The distinction from ``data/connectors.py`` is the one that matters: a market
data connector reads, a broker *acts*. Reading the wrong thing wastes time;
acting wrongly costs money. They are separate modules so nothing accidentally
gains order-routing capability while being given read access.

``docs/PLAN.md`` Phase 5 fills in ``PaperBroker`` against live market data with
simulated fills. Phase 6 — live trading — is deliberately unplanned, and the
guardrails it would need are listed there. Do not implement ``BinanceBroker``
without an explicit request; the roadmap has no step 51.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from trading_platform.core.credentials import CredentialSet
from trading_platform.core.events import Fill, Order


class BrokerNotImplementedError(NotImplementedError):
    """Raised by every skeleton broker method."""


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Cash and exposure as the venue reports it.

    Deliberately minimal. This is the reconciliation surface: at every restart,
    what the broker says must be compared against what the engine believes, and
    any divergence investigated rather than papered over.
    """

    cash: Decimal
    equity: Decimal
    positions: dict[str, Decimal]


@runtime_checkable
class Broker(Protocol):
    """What a working broker adapter must provide."""

    @property
    def name(self) -> str: ...

    @property
    def credentials(self) -> CredentialSet: ...

    @property
    def is_live(self) -> bool:
        """True only for an adapter that can move real money.

        Anything reading this should treat True as requiring confirmation.
        """
        ...

    def submit(self, order: Order) -> str:
        """Send an order. Returns the venue's order id."""
        ...

    def cancel(self, order_id: str) -> bool:
        """Cancel a working order. Returns whether it was still cancellable."""
        ...

    def fills_since(self, cursor: str | None) -> list[Fill]:
        """Fills after ``cursor``, for reconciliation after a restart."""
        ...

    def account(self) -> AccountSnapshot:
        """Current cash, equity, and positions as the venue reports them."""
        ...


class _SkeletonBroker:
    """Shared base: declares identity, refuses to act."""

    _name: str = "unknown"
    _credentials: CredentialSet = CredentialSet(venue="unknown")
    _is_live: bool = False
    _phase: str = "docs/PLAN.md Phase 5"

    @property
    def name(self) -> str:
        return self._name

    @property
    def credentials(self) -> CredentialSet:
        return self._credentials

    @property
    def is_live(self) -> bool:
        return self._is_live

    def _refuse(self, what: str) -> BrokerNotImplementedError:
        return BrokerNotImplementedError(
            f"{self._name}.{what} is a skeleton and cannot act. See {self._phase}."
        )

    def submit(self, order: Order) -> str:
        raise self._refuse("submit")

    def cancel(self, order_id: str) -> bool:
        raise self._refuse("cancel")

    def fills_since(self, cursor: str | None) -> list[Fill]:
        raise self._refuse("fills_since")

    def account(self) -> AccountSnapshot:
        raise self._refuse("account")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self._name!r}, skeleton, live={self._is_live})"


class PaperBroker(_SkeletonBroker):
    """Simulated fills against live market data (roadmap step 49).

    Never touches real money, which is why it is the only broker this project
    plans to implement. The exit criterion for Phase 5 is 30 days of paper
    trading with no unexplained divergence from the backtest — any divergence
    is a bug in one of them, and finding out which is the entire point.
    """

    _name = "paper"
    _credentials = CredentialSet(venue="paper")  # simulated: none needed
    _is_live = False


class BinanceBroker(_SkeletonBroker):
    """Live Binance order routing. **Intentionally not implemented.**

    Declared so the interface is complete and the absence is visible, rather
    than looking like an oversight. Implementing this needs, at minimum:
    credentials held outside the repository, a kill switch, per-order and
    per-day loss limits enforced venue-side as well as locally, full order
    audit logging, and a written procedure for when it misbehaves unattended.

    ``docs/PLAN.md`` Phase 6 covers why this is unplanned. There is no step 51.
    """

    _name = "binance-live"
    _phase = "docs/PLAN.md Phase 6 (unplanned — do not implement without an explicit request)"
    _is_live = True
    _credentials = CredentialSet.from_env("binance-live", "BINANCE_API_KEY", "BINANCE_API_SECRET")


def available_brokers() -> dict[str, _SkeletonBroker]:
    """Every declared broker, keyed by name. All are skeletons."""
    return {b.name: b for b in (PaperBroker(), BinanceBroker())}
