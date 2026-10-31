"""Simulated execution.

The fill model is deliberately pessimistic. A backtest that fills at the close
price with zero cost is not a backtest — it is a plot of the strategy's
intentions. Defaults here charge spread, slippage, and commission on every
trade, and it should stay that way: the burden of proof is on making the model
*more* optimistic, not less.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_platform.core.events import Bar, Fill, Order
from trading_platform.core.instruments import Instrument
from trading_platform.core.types import AssetClass, OrderType, Side, quantize_price
from trading_platform.data.calibration import (
    CRYPTO_OPTION_HALF_SPREAD,
    DERIBIT_OPTION_PREMIUM_CAP,
    DERIBIT_OPTION_SETTLEMENT_RATE,
    DERIBIT_OPTION_UNDERLYING_RATE,
)


class OptionCostsUnmodelledError(ValueError):
    """A spot cost model was handed an option.

    Raised rather than approximated, because the approximation is not close.
    An option's commission is charged on the **underlying**, not the premium,
    so a premium-based rate understates it by 5x at the money and 125x deep out
    of it — worst exactly where options strategies live. The half-spread is
    worse: 1bp of premium against a realistic 5% is optimistic by 500x.

    Both errors flatter the strategy, and an options backtest run on spot costs
    would produce an edge that is entirely an artefact of not paying for the
    trade. So this is an exception and not a default.
    """


@dataclass(frozen=True, slots=True)
class CostModel:
    """Transaction cost assumptions.

    Defaults are **Binance spot VIP 0**, the tier essentially every individual
    trades on: 10 bp commission, plus 2 bp of slippage and a 1 bp half-spread.

    This default was 5 bp until it was checked against the published fee
    schedule, which puts VIP 0 spot at 0.10% maker and taker. The "realistic"
    cost model had been optimistic by a factor of two for a retail spot trader
    — so every result this project produced before the correction understated
    what trading actually costs. Named presets below cover the other tiers.

    Override per venue — never silently zero these out to make a strategy look
    viable. See `data/calibration.py` for sources.
    """

    #: Commission as a fraction of notional (0.0010 = 10 bp = Binance VIP 0).
    commission_rate: Decimal = Decimal("0.0010")
    #: Minimum commission per fill, in quote currency.
    minimum_commission: Decimal = Decimal("0")
    #: Market-impact slippage as a fraction of price.
    slippage_rate: Decimal = Decimal("0.0002")
    #: Half the bid-ask spread as a fraction of price.
    half_spread_rate: Decimal = Decimal("0.0001")

    def commission(self, notional: Decimal) -> Decimal:
        return max(abs(notional) * self.commission_rate, self.minimum_commission)

    def commission_for(
        self,
        instrument: Instrument,
        price: Decimal,
        quantity: Decimal,
        underlying_price: Decimal | None = None,
    ) -> Decimal:
        """Commission for one fill, given what is being filled.

        The instrument-aware entry point. Spot and perpetuals delegate to
        `commission()` unchanged — the notional is the traded value and a rate
        on it is the right shape. Options do not: see `OptionCostsUnmodelledError`.
        """
        if instrument.asset_class is AssetClass.OPTION:
            raise OptionCostsUnmodelledError(
                f"{instrument.symbol} is an option and {type(self).__name__} prices "
                "commission as a rate on the traded notional, which for an option is "
                "the premium. Deribit charges on the UNDERLYING, capped at 12.5% of "
                "premium — a difference of up to 125x, always in the flattering "
                "direction. Use OptionCostModel."
            )
        return self.commission(instrument.notional(price, quantity))

    def effective_price_for(
        self, instrument: Instrument, reference_price: Decimal, side: Side
    ) -> Decimal:
        """Spread and slippage for one fill, given what is being filled."""
        if instrument.asset_class is AssetClass.OPTION:
            raise OptionCostsUnmodelledError(
                f"{instrument.symbol} is an option and {type(self).__name__} charges a "
                "half-spread of a fraction of the traded price. On an option that is a "
                "fraction of the premium, and 1bp against a realistic 5% is optimistic "
                "by 500x. Use OptionCostModel."
            )
        return self.effective_price(reference_price, side)

    def effective_price(self, reference_price: Decimal, side: Side) -> Decimal:
        """Apply spread and slippage against the trader, whichever way they trade."""
        penalty = self.half_spread_rate + self.slippage_rate
        return quantize_price(reference_price * (Decimal("1") + penalty * side.sign))


BINANCE_SPOT_VIP0 = CostModel(
    commission_rate=Decimal("0.0010"),
    slippage_rate=Decimal("0.0002"),
    half_spread_rate=Decimal("0.0001"),
)
"""Binance spot, VIP 0 — 0.10% taker. The same as the default, named so a
backtest can state which venue and tier it assumed rather than implying one."""

BINANCE_SPOT_BNB = CostModel(
    commission_rate=Decimal("0.00075"),
    slippage_rate=Decimal("0.0002"),
    half_spread_rate=Decimal("0.0001"),
)
"""Binance spot, VIP 0, fees paid in BNB — a 25% discount, so 0.075%.

Cheaper, but it requires holding BNB, which is an unhedged position in a
second asset. A strategy that only works at this tier is really a strategy
plus a long-BNB position, and should be evaluated as one."""

BINANCE_PERP_VIP0 = CostModel(
    commission_rate=Decimal("0.0005"),
    slippage_rate=Decimal("0.0002"),
    half_spread_rate=Decimal("0.0001"),
)
"""Binance USD-margined perpetuals, VIP 0 — 0.05% taker.

Cheaper per trade than spot, which is exactly why perpetual strategies overtrade.
Funding is charged separately in the event loop and is not part of this model."""

RETAIL_STRESSED = CostModel(
    commission_rate=Decimal("0.0010"),
    slippage_rate=Decimal("0.0015"),
    half_spread_rate=Decimal("0.0010"),
)
"""A small account in a fast market or an illiquid pair.

Not pessimism — this is what fills look like during the moves a strategy makes
most of its money on, which is precisely when the optimistic model is furthest
from the truth. If an edge survives here it is probably real."""

ZERO_COST = CostModel(
    commission_rate=Decimal("0"),
    slippage_rate=Decimal("0"),
    half_spread_rate=Decimal("0"),
)
"""Frictionless model. Use ONLY to isolate a bug in strategy logic — never to
evaluate whether a strategy is profitable."""


@dataclass(frozen=True, slots=True)
class OptionCostModel(CostModel):
    """What an option actually costs to trade, on a Deribit-style venue.

    Two things differ from spot and both are large.

    **Commission is charged on the underlying, capped by the premium.**
    `min(underlying * rate, premium * cap)`. The cap is what stops a far OTM
    contract paying more to trade than it is worth: a 36,000 strike seven days
    out on BTC at 30,000 has a premium of 6.94, an uncapped underlying fee of
    9.00, and a capped one of 0.87.

    **The half-spread is a fraction of premium and it is wide.** 5% is a tight
    quote on a liquid near-dated strike. Deep OTM and long-dated contracts
    routinely quote 20-50% wide, and in size the touch is not the price — so
    this default is the optimistic end, and `/cost-check` exists for the rest.

    The inherited `commission_rate`, `slippage_rate` and `half_spread_rate`
    are unused on this path; the option rates below replace them.

    Not modelled here, and stated so nobody assumes otherwise: spreads widening
    with moneyness and tenor, exercise and assignment, pin risk at expiry, and
    the fact that a quoted IV is frequently unmarketable in any size.
    """

    #: Fraction of the underlying's value, per contract.
    underlying_rate: Decimal = DERIBIT_OPTION_UNDERLYING_RATE
    #: Ceiling, as a fraction of the option's own premium.
    premium_cap: Decimal = DERIBIT_OPTION_PREMIUM_CAP
    #: Delivery fee at expiry, on the underlying, under the same cap.
    settlement_rate: Decimal = DERIBIT_OPTION_SETTLEMENT_RATE
    #: Half the bid-ask, as a fraction of PREMIUM.
    half_spread_premium_rate: Decimal = CRYPTO_OPTION_HALF_SPREAD

    def commission_for(
        self,
        instrument: Instrument,
        price: Decimal,
        quantity: Decimal,
        underlying_price: Decimal | None = None,
    ) -> Decimal:
        if instrument.asset_class is not AssetClass.OPTION:
            # Named base rather than zero-arg `super()`: `@dataclass(slots=True)`
            # rebuilds the class, so the implicit `__class__` cell still points
            # at the pre-rebuild object and `super()` raises TypeError.
            return CostModel.commission_for(self, instrument, price, quantity, underlying_price)
        if underlying_price is None:
            raise OptionCostsUnmodelledError(
                f"{instrument.symbol}: an option's commission is a rate on the "
                "underlying, so it cannot be computed without the underlying's price. "
                "Pass underlying_price. Falling back to the premium would understate "
                "it by up to 125x, which is the mistake this model exists to prevent."
            )
        contracts = abs(quantity) * instrument.multiplier
        on_underlying = abs(underlying_price) * contracts * self.underlying_rate
        cap = abs(price) * contracts * self.premium_cap
        return max(min(on_underlying, cap), self.minimum_commission)

    def settlement_fee(
        self, instrument: Instrument, quantity: Decimal, underlying_price: Decimal
    ) -> Decimal:
        """Delivery at expiry. Charged once, under the same premium cap.

        Separate from `commission_for` because it is not a fill — nothing is
        traded. A strategy that holds to expiry pays this and a strategy that
        closes early does not, which is a real difference between them.
        """
        contracts = abs(quantity) * instrument.multiplier
        return abs(underlying_price) * contracts * self.settlement_rate

    def effective_price_for(
        self, instrument: Instrument, reference_price: Decimal, side: Side
    ) -> Decimal:
        if instrument.asset_class is not AssetClass.OPTION:
            return CostModel.effective_price_for(self, instrument, reference_price, side)
        penalty = self.half_spread_premium_rate + self.slippage_rate
        return quantize_price(reference_price * (Decimal("1") + penalty * side.sign))


ZERO_OPTION_COST = OptionCostModel(
    commission_rate=Decimal("0"),
    slippage_rate=Decimal("0"),
    half_spread_rate=Decimal("0"),
    underlying_rate=Decimal("0"),
    premium_cap=Decimal("0"),
    settlement_rate=Decimal("0"),
    half_spread_premium_rate=Decimal("0"),
)
"""Frictionless options. Exists so that isolating a bug does not require
handing an option to a spot model, which would silently under-charge rather
than charge nothing. Never use it to decide whether a strategy is profitable."""


class SimulatedExecutionEngine:
    """Fills orders against the *next* bar.

    Orders submitted while processing the bar stamped T are filled using the
    bar stamped T+1. Filling against the same bar the signal was computed on
    means trading on information not yet available — the single most common
    backtest bug, and the reason this engine queues rather than fills inline.
    """

    def __init__(self, cost_model: CostModel | None = None) -> None:
        self.cost_model = cost_model if cost_model is not None else CostModel()
        self._pending: list[tuple[Order, Instrument]] = []
        #: Signed unfilled quantity per symbol, maintained as orders are
        #: submitted and filled. Kept incrementally rather than summed on
        #: demand: the risk layer asks once per bar event, so rebuilding it
        #: from `_pending` is O(orders) inside an O(symbols) loop and turns
        #: quadratic on a large universe.
        self._working: dict[str, Decimal] = {}
        self._ids = itertools.count(1)

    def next_order_id(self) -> str:
        return f"ord-{next(self._ids):06d}"

    def submit(self, order: Order, instrument: Instrument) -> None:
        """Queue an order for execution on the next bar."""
        self._pending.append((order, instrument))
        self._track(order, Decimal("1"))

    def _track(self, order: Order, sign: Decimal) -> None:
        delta = order.quantity * order.side.sign * sign
        total = self._working.get(order.symbol, Decimal("0")) + delta
        if total:
            self._working[order.symbol] = total
        else:
            self._working.pop(order.symbol, None)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def working_quantity(self) -> dict[str, Decimal]:
        """Signed quantity submitted and not yet filled, per symbol.

        The risk layer checks an order against *filled* position, and
        `RiskManager._pending_delta` covers orders approved earlier in the same
        batch — but it is cleared when the timestamp advances, on the
        assumption that an order submitted at T has filled by T+1. That holds
        only while the symbol prints every instant. A symbol that goes quiet
        leaves its order working and invisible, and the next order is checked
        against a position that does not yet include it.

        This is the authoritative answer, and it does not depend on any
        assumption about when a fill arrives: an order is working until it
        fills or is cancelled, however many instants that takes.
        """
        return self._working

    def process(self, bar: Bar, timestamp: datetime) -> list[tuple[Fill, Instrument]]:
        """Execute queued orders for ``bar.symbol`` against this bar.

        Returns ``(fill, instrument)`` pairs. Orders for other symbols stay
        queued so a multi-symbol run does not drop them.

        Two conditions must BOTH hold for an order to fill here: the symbol
        must match, and the bar must be stamped strictly after the order was
        created. Matching on symbol alone is a lookahead leak — see below.
        """
        if not self._pending:
            return []

        fills: list[tuple[Fill, Instrument]] = []
        still_pending: list[tuple[Order, Instrument]] = []

        for order, instrument in self._pending:
            if order.symbol != bar.symbol:
                still_pending.append((order, instrument))
                continue

            # The one-bar delay, enforced per order rather than per loop
            # iteration. A multi-symbol feed emits several bars at the same
            # instant, sorted by (timestamp, symbol). Without this check, an
            # order submitted while processing AAA's bar at time T fills on the
            # very next iteration against ZZZ's bar *stamped T* — at that bar's
            # OPEN, which is the price at the start of the interval that ended
            # at T. The delay inverts into a one-bar advantage.
            #
            # Measured on `correlated_series` at default costs, same fill count:
            # the leak returned 18,004% at Sharpe 7.97 with a 0.62% max
            # drawdown, against 34.4% / 0.49 / 25.6% for the honest control.
            # A 0.62% drawdown is the signature of a fictional equity curve.
            #
            # Single-symbol behaviour is unchanged: T+1 > T still fills.
            if order.created_at is not None and bar.timestamp <= order.created_at:
                still_pending.append((order, instrument))
                continue

            fill_price = self._resolve_price(order, bar)
            if fill_price is None:
                # Limit order not marketable on this bar; keep it working.
                still_pending.append((order, instrument))
                continue

            commission = self.cost_model.commission_for(instrument, fill_price, order.quantity)
            # Off the working book the moment it fills, so the risk layer never
            # counts a filled order twice — once as a position and once as
            # still-working intent.
            self._track(order, Decimal("-1"))
            fills.append(
                (
                    Fill(
                        order_id=order.order_id,
                        symbol=order.symbol,
                        timestamp=timestamp,
                        side=order.side,
                        quantity=order.quantity,
                        price=fill_price,
                        commission=commission,
                        slippage=abs(fill_price - bar.open),
                        tag=order.tag,
                    ),
                    instrument,
                )
            )

        self._pending = still_pending
        return fills

    def _resolve_price(self, order: Order, bar: Bar) -> Decimal | None:
        """Determine the fill price, or None if the order cannot fill."""
        if order.order_type is OrderType.MARKET:
            # Market orders fill at the open of the bar following the signal.
            return self.cost_model.effective_price(bar.open, order.side)

        assert order.limit_price is not None  # guaranteed by Order.__post_init__
        limit = order.limit_price

        if order.side is Side.BUY:
            if bar.low <= limit:
                # Fill at the better of the limit and the bar's open.
                raw = min(limit, bar.open)
                return self.cost_model.effective_price(raw, order.side)
            return None

        if bar.high >= limit:
            raw = max(limit, bar.open)
            return self.cost_model.effective_price(raw, order.side)
        return None

    def cancel_all(self) -> int:
        """Drop all working orders. Returns how many were cancelled."""
        count = len(self._pending)
        self._pending.clear()
        return count
