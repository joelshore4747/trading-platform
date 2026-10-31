"""Primitive value types shared across the platform.

Money and quantities use ``Decimal``, never ``float``. Accumulating float error
across thousands of simulated fills produces cash balances that silently drift
from the sum of their trades, which makes a backtest impossible to reconcile.
Analytics (Sharpe, drawdown) convert to float at the boundary — see
``analytics.metrics``.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import TypeAlias

Quantity: TypeAlias = Decimal
Price: TypeAlias = Decimal
Cash: TypeAlias = Decimal

# Quantization targets. Venue-specific tick/lot sizes override these per instrument.
PRICE_PRECISION = Decimal("0.00000001")
QUANTITY_PRECISION = Decimal("0.00000001")
CASH_PRECISION = Decimal("0.01")


class AssetClass(StrEnum):
    """Broad instrument family. Drives which pricing and margin rules apply."""

    SPOT = "spot"
    PERPETUAL = "perpetual"
    FUTURE = "future"
    OPTION = "option"
    EQUITY = "equity"


class Stage(StrEnum):
    """The four stages, kept apart (charter §8).

    RESEARCH → SIMULATION → PAPER → LIVE. A strategy gains no execution
    authority by being researched, and a paper account never places a live
    order. This enum **records** which stage produced something; it grants
    nothing and gates nothing on its own. Today every broker adapter raises,
    so no artefact can legitimately carry `LIVE`.

    It exists because a row that cannot say which stage produced it is a row
    that will eventually be read as the wrong one — a paper equity curve shown
    as a live one is the specific mistake this repository is built against.
    """

    RESEARCH = "research"
    SIMULATION = "simulation"
    PAPER = "paper"
    LIVE = "live"


class OptionRight(StrEnum):
    CALL = "call"
    PUT = "put"


class OptionStyle(StrEnum):
    EUROPEAN = "european"
    AMERICAN = "american"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """+1 for buys, -1 for sells. Used to signed-ify quantities."""
        return 1 if self is Side.BUY else -1


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"


class OrderStatus(StrEnum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


def funding_payment(signed_notional: Decimal, rate: Decimal) -> Decimal:
    """Cash impact of one perpetual funding settlement.

    ``signed_notional`` is positive when long, negative when short. The single
    negation covers all four cases: a long pays on a positive rate, a short
    receives, and both invert when the rate is negative.

    Defined here, in the layer with no dependencies, because both the data
    layer (``FundingRate.payment_on``) and the engine (``Portfolio``) need it
    and a duplicated sign convention is exactly the kind of thing that drifts.
    """
    return -(signed_notional * rate)


def quantize_price(value: Decimal, precision: Decimal = PRICE_PRECISION) -> Decimal:
    return value.quantize(precision)


def quantize_quantity(value: Decimal, precision: Decimal = QUANTITY_PRECISION) -> Decimal:
    return value.quantize(precision)


def quantize_cash(value: Decimal, precision: Decimal = CASH_PRECISION) -> Decimal:
    return value.quantize(precision)
