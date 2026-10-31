"""Market and trading events flowing through the engine.

Every event carries an explicit ``timestamp``. The engine never reads the wall
clock — see ``engine.clock``. This is what makes a backtest reproducible and is
the single most important defence against lookahead bias.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_platform.core.types import OrderStatus, OrderType, Side, TimeInForce


@dataclass(frozen=True, slots=True)
class Bar:
    """OHLCV bar. ``timestamp`` is the bar's CLOSE time.

    Close-stamped bars are the convention throughout this codebase. A strategy
    receiving a bar stamped 09:30 has seen all trading up to and including
    09:30, and may act at 09:30 or later — never earlier. Open-stamped bars are
    the most common source of accidental lookahead in backtests.
    """

    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        # Checked first, and on `low` alone: low <= open, close <= high is
        # enforced below, so a positive low makes all four positive. A zero or
        # negative price does not raise anywhere downstream — it divides by
        # zero in a return, breaks a log axis, and silently produces a
        # plausible-looking result many bars later. Which is exactly what bad
        # data must not be allowed to do.
        if self.low <= 0:
            raise ValueError(
                f"{self.symbol} @ {self.timestamp}: non-positive price {self.low}. "
                "A traded instrument has a positive price; this is bad data."
            )
        if self.high < self.low:
            raise ValueError(f"{self.symbol} @ {self.timestamp}: high {self.high} < low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(
                f"{self.symbol} @ {self.timestamp}: open {self.open} outside [low, high]"
            )
        if not (self.low <= self.close <= self.high):
            raise ValueError(
                f"{self.symbol} @ {self.timestamp}: close {self.close} outside [low, high]"
            )
        if self.volume < 0:
            raise ValueError(f"{self.symbol} @ {self.timestamp}: negative volume {self.volume}")

    @property
    def typical_price(self) -> Decimal:
        return (self.high + self.low + self.close) / Decimal("3")

    @property
    def range(self) -> Decimal:
        return self.high - self.low


@dataclass(frozen=True, slots=True)
class Quote:
    """Top-of-book snapshot."""

    symbol: str
    timestamp: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal = Decimal("0")
    ask_size: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.ask < self.bid:
            raise ValueError(f"{self.symbol} @ {self.timestamp}: crossed book (ask < bid)")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class Order:
    """An order request. Immutable — status changes produce a new object."""

    order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime | None = None
    #: Free-form tag so a strategy can correlate fills back to its own intent.
    tag: str = ""

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"Order quantity must be positive, got {self.quantity}")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("Limit order requires a limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("Market order must not carry a limit_price")


@dataclass(frozen=True, slots=True)
class Fill:
    """A completed (or partial) execution."""

    order_id: str
    symbol: str
    timestamp: datetime
    side: Side
    quantity: Decimal
    price: Decimal
    commission: Decimal = Decimal("0")
    #: Difference between the reference price and the actual fill price.
    slippage: Decimal = Decimal("0")
    tag: str = ""

    @property
    def signed_quantity(self) -> Decimal:
        """Position delta produced by this fill."""
        return self.quantity * self.side.sign

    def cash_impact(
        self,
        multiplier: Decimal = Decimal("1"),
        *,
        is_derivative: bool = False,
    ) -> Decimal:
        """Change in cash. Buys consume cash, sells release it; fees always cost.

        **A derivative moves only the commission.** Opening a futures or
        perpetual position does not buy anything — no notional changes hands,
        and the position is marked at unrealised PnL rather than market value.
        Debiting the notional as well made the two halves of the ledger
        contradict each other: measured at an unchanged price, buying 1.46
        perpetual contracts at 65,000 on 100,000 of cash reported equity of
        5,100 against a spot control's 100,000.

        What a derivative *does* consume is margin, and margin is a reservation
        against capacity rather than a charge against equity — see
        ``Instrument.margin_requirement`` and ``Portfolio.margin_reserved``.

        Keyword-only, so a caller cannot pass it positionally where the
        multiplier goes and silently get the old behaviour back.
        """
        if is_derivative:
            return -self.commission
        gross = self.price * self.quantity * multiplier
        return -(gross * self.side.sign) - self.commission
