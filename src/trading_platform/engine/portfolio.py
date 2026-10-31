"""Positions, cash, and mark-to-market accounting."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from trading_platform.core.events import Fill
from trading_platform.core.fx import QuoteRates, UnknownQuoteRate
from trading_platform.core.instruments import Instrument
from trading_platform.core.types import funding_payment, quantize_cash


@dataclass(slots=True)
class Position:
    """A holding in one instrument.

    ``average_price`` is cost basis, maintained across scale-ins and partial
    exits. Realised P&L is booked at the moment quantity moves toward zero.
    """

    instrument: Instrument
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    realised_pnl: Decimal = Decimal("0")
    total_commission: Decimal = Decimal("0")
    #: Cumulative funding. Negative means this position has paid out. Kept
    #: separate from realised_pnl so a perp strategy that is really a funding
    #: carry trade is visible as one rather than hidden inside trading P&L.
    funding_paid: Decimal = Decimal("0")

    @property
    def symbol(self) -> str:
        return self.instrument.symbol

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    def market_value(self, price: Decimal) -> Decimal:
        return self.instrument.notional(price, self.quantity)

    def unrealised_pnl(self, price: Decimal) -> Decimal:
        if self.is_flat:
            return Decimal("0")
        return self.instrument.notional(price - self.average_price, self.quantity)

    def apply(self, fill: Fill) -> Decimal:
        """Apply a fill; return the realised P&L it produced.

        Four cases: opening from flat, adding to an existing side, reducing a
        position, and flipping through zero. The flip case is the one that
        usually gets written wrong — it must realise P&L on the closed portion
        only, then re-base the average price on the remainder at the fill price.
        """
        delta = fill.signed_quantity
        self.total_commission += fill.commission
        realised = Decimal("0")

        if self.is_flat:
            # Opening a new position.
            self.quantity = delta
            self.average_price = fill.price

        elif (self.quantity > 0) == (delta > 0):
            # Adding to the existing side — blend the cost basis.
            total_cost = self.average_price * self.quantity + fill.price * delta
            self.quantity += delta
            self.average_price = total_cost / self.quantity

        else:
            # Reducing, closing, or flipping.
            closing_qty = min(abs(delta), abs(self.quantity))
            direction = Decimal("1") if self.quantity > 0 else Decimal("-1")
            realised = self.instrument.notional(
                fill.price - self.average_price, closing_qty * direction
            )
            self.realised_pnl += realised
            new_quantity = self.quantity + delta

            if new_quantity == 0:
                self.quantity = Decimal("0")
                self.average_price = Decimal("0")
            elif (new_quantity > 0) == (self.quantity > 0):
                # Partial reduction — cost basis is unchanged.
                self.quantity = new_quantity
            else:
                # Flipped through zero — the remainder is a fresh position.
                self.quantity = new_quantity
                self.average_price = fill.price

        return realised


@dataclass(slots=True)
class EquityPoint:
    timestamp: datetime
    equity: Decimal
    cash: Decimal
    positions_value: Decimal


@dataclass(slots=True)
class Portfolio:
    """Cash plus positions, with an equity curve recorded per mark."""

    starting_cash: Decimal
    cash: Decimal = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)

    #: Last seen price per symbol, owned here so marking can be incremental.
    _prices: dict[str, Decimal] = field(default_factory=dict)
    #: When each `_prices` entry was observed, so staleness is measurable.
    _priced_at: dict[str, datetime] = field(default_factory=dict)
    #: Each symbol's contribution to positions value at its last known price.
    _marked: dict[str, Decimal] = field(default_factory=dict)
    #: Running sum of `_marked`. Maintained on every price and fill so the
    #: per-event cost is O(1) instead of O(positions).
    #:
    #: This was 94% of engine runtime. Re-walking every position on every bar
    #: event makes the loop O(symbols x bars x positions) on a multi-symbol
    #: feed — 9.04 million property calls to service 400 fills, and per-event
    #: cost doubling every time the universe doubles.
    _positions_total: Decimal = Decimal("0")
    #: Capital reserved per symbol to hold its current position.
    _margin: dict[str, Decimal] = field(default_factory=dict)
    #: Running sum of `_margin`, maintained beside `_positions_total` so the
    #: per-event cost stays O(1).
    _margin_total: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.starting_cash <= 0:
            raise ValueError(f"starting_cash must be positive, got {self.starting_cash}")
        self.cash = self.starting_cash

    def position(self, instrument: Instrument) -> Position:
        """Get or create the position for an instrument."""
        pos = self.positions.get(instrument.symbol)
        if pos is None:
            pos = Position(instrument=instrument)
            self.positions[instrument.symbol] = pos
        return pos

    def quantity_of(self, symbol: str) -> Decimal:
        pos = self.positions.get(symbol)
        return pos.quantity if pos else Decimal("0")

    def apply_fill(self, instrument: Instrument, fill: Fill) -> None:
        """Book a fill: cash first, then the position's contribution to equity.

        The two branches are not symmetric because the instruments are not.

        **Spot** exchanges cash for a thing you now hold. Cash falls by the
        notional and `market_value` books the thing, so equity is unchanged at
        the moment of the fill.

        **A derivative is not bought, it is entered.** No notional changes
        hands — `positions_value` has always said so, in as many words, and
        contributes only unrealised P&L for these. But cash was being reduced
        by the full notional anyway, so entering a perp destroyed equity equal
        to the position size: on the real archive, 100,000 to 8,302 in 400
        instants with no market move, tripping a drawdown halt that is a
        one-way latch and refusing every entry thereafter. Half of a decision
        had been implemented, and the half that was missing was this one.

        **Which of two branches this is.** Both lines of work fixed the same
        defect independently and only this one is correct; the other moved the
        branch into `Fill.cash_impact(is_derivative=True)`, which returns
        `-commission` and discards the realised P&L that `Position.apply`
        computes. Measured on merge day: open a perp at 100, mark at 150 —
        equity 100,050, right — then CLOSE the winner, and equity falls back to
        100,000. Every closed derivative trade booked exactly zero, gains and
        losses alike, so a perp strategy reported flat whatever it did. Kept
        here because it credits `realised`.

        So a derivative fill moves cash by realised P&L and commission only.
        Crediting the realised part is not optional once the notional stops
        moving: `Position.apply` already computes it and the return value was
        being discarded, and without it a winning perp's gain would never
        reach cash at all.

        **What this deliberately does not model is margin.** A real perp posts
        initial and maintenance margin and can be liquidated. The consequence
        is that cash no longer bounds position size for derivatives — that job
        now belongs entirely to `RiskLimits.max_gross_leverage`, which is
        equity-relative and is the right place for it, but which goes from
        decorative to load-bearing the moment a perp is traded.
        """
        pos = self.position(instrument)
        realised = pos.apply(fill)
        if instrument.is_derivative:
            self.cash = quantize_cash(self.cash + realised - fill.commission)
        else:
            self.cash = quantize_cash(self.cash + fill.cash_impact(instrument.multiplier))
        self.fills.append(fill)
        self._refresh(instrument.symbol)

    def observe_price(self, symbol: str, price: Decimal, at: datetime | None = None) -> None:
        """Record a new price and re-mark that symbol alone. O(1)."""
        self._prices[symbol] = price
        if at is not None:
            self._priced_at[symbol] = at
        self._refresh(symbol)

    def last_price(self, symbol: str) -> Decimal | None:
        """The price this symbol is currently marked at, or None if never priced."""
        return self._prices.get(symbol)

    def mark_age(self, symbol: str, now: datetime) -> timedelta | None:
        """How old the price this symbol is marked at is. `None` if unpriced.

        A symbol that stops printing keeps its last close forever: `_refresh`
        goes on folding it into `marked_equity`, and `mark()` writes it into
        every subsequent equity point. That is correct while the gap is an
        outage and a fabrication once the instrument has actually stopped
        trading — and bar data cannot tell those apart, which is why the age
        is reported rather than acted on here.
        """
        seen = self._priced_at.get(symbol)
        return None if seen is None else now - seen

    def _refresh(self, symbol: str) -> None:
        """Recompute one symbol's mark and margin, adjusting both running totals.

        Margin is recomputed at the **current** price rather than fixed at
        entry. That is the conservative reading and the honest one: a position
        that has moved against you requires more collateral, not the amount it
        needed on the day it was opened.
        """
        previous = self._marked.get(symbol, Decimal("0"))
        previous_margin = self._margin.get(symbol, Decimal("0"))
        current = Decimal("0")
        current_margin = Decimal("0")
        pos = self.positions.get(symbol)
        price = self._prices.get(symbol)
        if pos is not None and not pos.is_flat and price is not None:
            current = (
                pos.unrealised_pnl(price)
                if pos.instrument.is_derivative
                else pos.market_value(price)
            )
            current_margin = pos.instrument.margin_requirement(price, pos.quantity)
        self._marked[symbol] = current
        self._positions_total += current - previous
        self._margin[symbol] = current_margin
        self._margin_total += current_margin - previous_margin

    @property
    def marked_positions_value(self) -> Decimal:
        """Positions value from the incremental mark. O(1).

        `positions_value` recomputes from scratch and remains the authority.
        Tests assert the two agree after every mutation, because a cache that
        silently drifts from the truth is worse than no cache.

        "Agree" is not always "are bit-identical". Scaling into a position
        makes `average_price` a 28-significant-digit quotient, and summing
        those incrementally rounds differently from summing them afresh. Over
        a 4,800-event run with fractional quantities the gap reached 1.9e-25 —
        about 23 orders of magnitude below the cent that `quantize_cash`
        rounds to, so it cannot reach a reported figure. Round quantities
        agree exactly.
        """
        return self._positions_total

    @property
    def marked_equity(self) -> Decimal:
        return quantize_cash(self.cash + self._positions_total)

    @property
    def margin_reserved(self) -> Decimal:
        """Capital currently reserved against open derivative positions.

        Not subtracted from equity. Equity is cash plus marked positions either
        way; margin decides how much *more* can be opened, which is the only
        thing leverage actually is.
        """
        return quantize_cash(self._margin_total)

    @property
    def free_cash(self) -> Decimal:
        """Cash not reserved as margin — the capacity left to open with.

        **Allowed to go negative, and reported rather than clamped.** A
        negative figure means the book requires more collateral than the
        account holds, which is precisely the state a reader needs to see. It
        is the margin-model analogue of the drawdown halt: a result, not a
        failure.
        """
        return quantize_cash(self.cash - self._margin_total)

    def apply_funding(self, instrument: Instrument, rate: Decimal, price: Decimal) -> Decimal:
        """Settle one funding payment against an open position.

        Returns the signed cash impact, or zero when flat — a settlement must
        never charge a position that did not exist at that instant.
        """
        pos = self.positions.get(instrument.symbol)
        if pos is None or pos.is_flat:
            return Decimal("0")

        signed_notional = instrument.notional(price, pos.quantity)
        payment = funding_payment(signed_notional, rate)
        self.cash = quantize_cash(self.cash + payment)
        pos.funding_paid += payment
        self._refresh(instrument.symbol)
        return payment

    def positions_value(self, prices: dict[str, Decimal]) -> Decimal:
        """Mark-to-market value of open positions.

        Derivatives contribute unrealised P&L (the notional is not held in
        cash); spot positions contribute full market value. Conflating the two
        is the classic way to double-count equity in a mixed portfolio.
        """
        total = Decimal("0")
        for pos in self.positions.values():
            if pos.is_flat:
                continue
            price = prices.get(pos.symbol)
            if price is None:
                continue
            if pos.instrument.is_derivative:
                total += pos.unrealised_pnl(price)
            else:
                total += pos.market_value(price)
        return total

    def equity(self, prices: dict[str, Decimal]) -> Decimal:
        return quantize_cash(self.cash + self.positions_value(prices))

    def mark(self, timestamp: datetime) -> EquityPoint:
        """Record one point on the equity curve.

        Called **once per timestamp**, not once per bar event. On a
        multi-symbol feed the old behaviour produced one point per symbol per
        instant — 600 points for 30 days across 20 symbols — which inflated
        `num_periods`, chopped each instant's move into fragments, and changed
        the reported Sharpe from 87.50 to 67.36 on identical price paths. A
        portfolio has one value per instant.
        """
        pv = self._positions_total
        point = EquityPoint(
            timestamp=timestamp,
            equity=quantize_cash(self.cash + pv),
            cash=self.cash,
            positions_value=quantize_cash(pv),
        )
        self.equity_curve.append(point)
        return point

    @property
    def total_realised_pnl(self) -> Decimal:
        return sum((p.realised_pnl for p in self.positions.values()), Decimal("0"))

    @property
    def total_commission(self) -> Decimal:
        return sum((p.total_commission for p in self.positions.values()), Decimal("0"))

    @property
    def total_funding(self) -> Decimal:
        """Cumulative funding across all positions. Negative means net paid."""
        return sum((p.funding_paid for p in self.positions.values()), Decimal("0"))


def translate_curve(
    curve: Sequence[EquityPoint],
    *,
    from_currency: str,
    to_currency: str,
    rates: QuoteRates | None = None,
) -> list[EquityPoint]:
    """Restate an equity curve in another currency, point by point.

    Every point is converted at the rate known *at that point*, so the result
    carries the quote asset's own path — which is the whole reason to do this.
    Converting the endpoints alone would reproduce the exact blind spot this
    exists to close: a depeg that recovers inside the sample leaves terminal
    wealth untouched while inflating measured volatility 2.7x.

    Raises ``UnknownQuoteRate`` rather than assuming parity. When the two
    currencies are the same no rates are needed and none are consulted, so
    USD-quoted equities cost nothing here.
    """
    if from_currency == to_currency:
        return list(curve)

    if rates is None:
        raise UnknownQuoteRate(
            f"cannot restate a {from_currency} curve in {to_currency} without a "
            f"rate series. Pass QuoteRates, or assume_pegged({from_currency!r}) "
            "to assert the peg held over this run."
        )
    if rates.reporting_currency != to_currency:
        raise ValueError(f"rates report into {rates.reporting_currency!r}, not {to_currency!r}")

    return [
        EquityPoint(
            timestamp=point.timestamp,
            equity=point.equity * (rate := rates.rate(from_currency, point.timestamp)),
            cash=point.cash * rate,
            positions_value=point.positions_value * rate,
        )
        for point in curve
    ]
