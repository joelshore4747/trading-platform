"""Instrument definitions.

The engine is instrument-agnostic: it moves ``Quantity`` of an ``Instrument``
and asks the instrument how to convert a price into notional value. Adding
equity options later means adding a subclass, not touching the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from trading_platform.core.types import AssetClass, OptionRight, OptionStyle


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradable contract.

    ``symbol`` must be unique within a run — it is the key used by feeds,
    portfolios, and order routing. Use venue-qualified symbols when combining
    sources (e.g. ``BINANCE:BTCUSDT`` and ``DERIBIT:BTC-PERPETUAL``).
    """

    symbol: str
    asset_class: AssetClass
    quote_currency: str = "USD"
    #: Contract size. One unit of quantity controls this much underlying.
    multiplier: Decimal = Decimal("1")
    #: Smallest tradable increment of quantity.
    lot_size: Decimal = Decimal("0.00000001")
    #: Smallest price increment.
    tick_size: Decimal = Decimal("0.01")
    #: Fraction of notional reserved as initial margin. **Derivatives only.**
    #:
    #: ``None`` on a derivative means fully collateralised — the whole notional
    #: is reserved, so leverage is 1x. That is the deliberate default: leverage
    #: has to be asked for, by a number a reviewer can see in a diff, rather
    #: than arriving because nobody set a field. ``Decimal("0.1")`` is 10x.
    #:
    #: Meaningless on spot, where the cash is simply paid, and rejected there
    #: rather than silently ignored.
    initial_margin_rate: Decimal | None = None
    metadata: dict[str, str] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        self._validate_margin()

    def _validate_margin(self) -> None:
        """Reject a margin rate that cannot mean anything.

        Every subclass calls this by name from its own ``__post_init__``.
        Not via ``super()``: ``slots=True`` makes the dataclass decorator build
        a replacement class, so a zero-arg ``super()`` closure still points at
        the discarded one and raises at construction.

        A rate above 1 is not
        leverage, it is over-collateralisation; a rate at or below 0 would make
        an unlimited position free. Both are rejected at construction, which is
        where this repository puts impossible values.
        """
        rate = self.initial_margin_rate
        if rate is None:
            return
        if not self.is_derivative:
            raise ValueError(
                f"{self.symbol}: initial_margin_rate is meaningless on a non-derivative "
                f"({self.asset_class}); spot pays its notional in cash."
            )
        if rate <= 0 or rate > 1:
            raise ValueError(
                f"{self.symbol}: initial_margin_rate must be in (0, 1], got {rate}. "
                "1 is fully collateralised; 0.1 is 10x leverage."
            )

    def notional(self, price: Decimal, quantity: Decimal) -> Decimal:
        """Cash value of ``quantity`` units at ``price``."""
        return price * quantity * self.multiplier

    def margin_requirement(self, price: Decimal, quantity: Decimal) -> Decimal:
        """Capital reserved to hold ``quantity`` at ``price``. Never negative.

        Zero for spot: the notional leaves cash outright, so there is nothing
        further to reserve. For a derivative it is ``|notional| x rate``, with
        an unset rate meaning full collateral.

        This is a reservation against *capacity*, not a charge against equity.
        Equity is cash plus marked positions either way; margin decides how much
        more can be opened, which is the only thing leverage actually is.
        """
        if not self.is_derivative:
            return Decimal("0")
        rate = self.initial_margin_rate if self.initial_margin_rate is not None else Decimal("1")
        return abs(self.notional(price, quantity)) * rate

    @property
    def leverage(self) -> Decimal:
        """Maximum leverage this instrument permits. 1 for spot."""
        if not self.is_derivative or self.initial_margin_rate is None:
            return Decimal("1")
        return Decimal("1") / self.initial_margin_rate

    @property
    def is_derivative(self) -> bool:
        return self.asset_class in (
            AssetClass.PERPETUAL,
            AssetClass.FUTURE,
            AssetClass.OPTION,
        )


@dataclass(frozen=True, slots=True)
class Spot(Instrument):
    """Cash instrument — crypto spot pair or a share of stock."""

    def __post_init__(self) -> None:
        if self.asset_class not in (AssetClass.SPOT, AssetClass.EQUITY):
            raise ValueError(f"Spot requires SPOT or EQUITY asset class, got {self.asset_class}")
        Instrument._validate_margin(self)


@dataclass(frozen=True, slots=True)
class Perpetual(Instrument):
    """Perpetual swap. Carries funding rather than an expiry."""

    #: Interval at which funding is exchanged, in hours.
    funding_interval_hours: int = 8

    def __post_init__(self) -> None:
        if self.asset_class is not AssetClass.PERPETUAL:
            raise ValueError(f"Perpetual requires PERPETUAL asset class, got {self.asset_class}")
        Instrument._validate_margin(self)


@dataclass(frozen=True, slots=True)
class Option(Instrument):
    """Option contract.

    Covers both crypto options (Deribit: inverse-settled, multiplier 1) and
    equity options (OCC: multiplier 100). The difference is expressed through
    ``multiplier`` and ``settlement_currency`` rather than separate classes.
    """

    underlying: str = ""
    strike: Decimal = Decimal("0")
    expiry: date | None = None
    right: OptionRight = OptionRight.CALL
    style: OptionStyle = OptionStyle.EUROPEAN
    settlement_currency: str = "USD"

    def __post_init__(self) -> None:
        if self.asset_class is not AssetClass.OPTION:
            raise ValueError(f"Option requires OPTION asset class, got {self.asset_class}")
        if not self.underlying:
            raise ValueError("Option requires an underlying symbol")
        if self.strike <= 0:
            raise ValueError(f"Option strike must be positive, got {self.strike}")
        if self.expiry is None:
            raise ValueError("Option requires an expiry date")
        Instrument._validate_margin(self)

    def intrinsic_value(self, underlying_price: Decimal) -> Decimal:
        """Payoff if exercised now. Never negative."""
        if self.right is OptionRight.CALL:
            return max(Decimal("0"), underlying_price - self.strike)
        return max(Decimal("0"), self.strike - underlying_price)

    def is_expired(self, as_of: date) -> bool:
        assert self.expiry is not None  # guaranteed by __post_init__
        return as_of > self.expiry

    def days_to_expiry(self, as_of: date) -> int:
        assert self.expiry is not None
        return (self.expiry - as_of).days


def crypto_spot(symbol: str, quote_currency: str = "USDT") -> Spot:
    """Convenience constructor for a crypto spot pair."""
    return Spot(
        symbol=symbol,
        asset_class=AssetClass.SPOT,
        quote_currency=quote_currency,
        tick_size=Decimal("0.01"),
    )


def crypto_perpetual(
    symbol: str,
    quote_currency: str = "USDT",
    initial_margin_rate: Decimal | None = None,
) -> Perpetual:
    """Convenience constructor for a crypto perpetual swap.

    ``initial_margin_rate`` defaults to ``None``, which is **fully
    collateralised** — 1x. Pass ``Decimal("0.1")`` for 10x. Leverage does not
    arrive by default here; it is asked for.
    """
    return Perpetual(
        symbol=symbol,
        asset_class=AssetClass.PERPETUAL,
        quote_currency=quote_currency,
        tick_size=Decimal("0.01"),
        initial_margin_rate=initial_margin_rate,
    )


def equity(symbol: str) -> Spot:
    """Convenience constructor for a US equity."""
    return Spot(
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        lot_size=Decimal("1"),
        tick_size=Decimal("0.01"),
    )


def crypto_option(
    symbol: str,
    underlying: str,
    strike: Decimal,
    expiry: date,
    right: OptionRight,
    quote_currency: str = "USD",
) -> Option:
    """Deribit-style crypto option: one unit of underlying, European style.

    Multiplier 1, against the OCC's 100 — the difference between "one contract
    is one BTC" and "one contract is a hundred shares". It matters to every
    cost and every Greek, which is why it is expressed here rather than
    assumed at the call site.
    """
    return Option(
        symbol=symbol,
        asset_class=AssetClass.OPTION,
        quote_currency=quote_currency,
        multiplier=Decimal("1"),
        lot_size=Decimal("0.1"),
        tick_size=Decimal("0.0001"),
        underlying=underlying,
        strike=strike,
        expiry=expiry,
        right=right,
        style=OptionStyle.EUROPEAN,
    )


def equity_option(
    symbol: str,
    underlying: str,
    strike: Decimal,
    expiry: date,
    right: OptionRight,
) -> Option:
    """OCC-style equity option: 100 shares per contract, American style."""
    return Option(
        symbol=symbol,
        asset_class=AssetClass.OPTION,
        quote_currency="USD",
        multiplier=Decimal("100"),
        lot_size=Decimal("1"),
        tick_size=Decimal("0.01"),
        underlying=underlying,
        strike=strike,
        expiry=expiry,
        right=right,
        style=OptionStyle.AMERICAN,
    )
