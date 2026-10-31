"""Strategy interface.

A strategy sees market data through ``StrategyContext`` and expresses intent by
submitting orders. It has no access to the feed, the future, or the wall clock
— the context deliberately exposes only what is knowable at the current bar.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict, deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from trading_platform.core.events import Bar, Fill, Order
from trading_platform.core.instruments import Instrument
from trading_platform.core.types import OrderType, Side, TimeInForce


class _QuantityView(Mapping[str, Decimal]):
    """Read-only quantities over a live position mapping.

    Constructed once per run, so reads stay O(1) and no dict is rebuilt per
    bar event. Its job is to be the *only* thing a strategy can reach: it
    yields Decimals, never `Position` objects, so the engine's books cannot be
    written through the context and new `Position` fields do not silently
    become part of the strategy-facing API.
    """

    __slots__ = ("_source",)

    def __init__(self, source: Mapping[str, HasQuantity]) -> None:
        self._source = source

    def __getitem__(self, symbol: str) -> Decimal:
        return self._source[symbol].quantity

    def __iter__(self) -> Iterator[str]:
        return iter(self._source)

    def __len__(self) -> int:
        return len(self._source)


@runtime_checkable
class HasQuantity(Protocol):
    """The only thing the context needs from a position.

    A structural type rather than an import of `engine.portfolio.Position`,
    because `engine` already imports `strategy` and the reverse would close a
    cycle — and because `CLAUDE.md` requires dependencies to point one way.
    The context genuinely needs one attribute; asking for more would be asking
    for a back-channel into the engine, which is how lookahead gets in.
    """

    @property
    def quantity(self) -> Decimal: ...


if TYPE_CHECKING:
    from trading_platform.data.universe import Membership, UniverseRegistry
    from trading_platform.risk.limits import RiskRejection


class StrategyContext:
    """The strategy's view of the world and its only route to the market.

    Deliberately narrow. If a strategy needs data this class does not expose,
    that is a signal to extend it explicitly rather than to reach around it —
    every back-channel into the engine is a potential lookahead bug.
    """

    def __init__(
        self,
        instruments: dict[str, Instrument],
        history_size: int = 512,
        universe: UniverseRegistry | None = None,
        order_observer: Callable[[Order], None] | None = None,
    ) -> None:
        self._instruments = instruments
        #: Told about every order the instant it is queued, before the risk
        #: layer sees it. Exists so a supervisor can journal *intent* without
        #: reaching into `_pending_orders`; it can only observe, never alter.
        self._order_observer = order_observer
        self._history: dict[str, deque[Bar]] = defaultdict(lambda: deque(maxlen=history_size))
        self._prices: dict[str, Decimal] = {}
        self._now: datetime | None = None
        self._universe = universe
        #: Symbols that printed a bar at `_now`. Reset when the instant
        #: advances, so it is the cross-section as of this instant and never
        #: an accumulation across several.
        self._printed: set[str] = set()
        self._pending_orders: list[tuple[Order, Instrument]] = []
        self._positions: Mapping[str, Decimal] = {}
        self._positions_source: Mapping[str, HasQuantity] | None = None
        self._equity: Decimal = Decimal("0")
        self._cash: Decimal = Decimal("0")
        self._margin_reserved: Decimal = Decimal("0")
        self._order_seq = 0

    # -- Engine-facing plumbing -------------------------------------------------

    def _observe(self, bar: Bar) -> None:
        if bar.timestamp != self._now:
            self._printed = set()
        self._history[bar.symbol].append(bar)
        self._prices[bar.symbol] = bar.close
        self._printed.add(bar.symbol)
        self._now = bar.timestamp

    def _sync(
        self,
        positions: Mapping[str, HasQuantity],
        equity: Decimal,
        cash: Decimal = Decimal("0"),
        margin_reserved: Decimal = Decimal("0"),
    ) -> None:
        """Point at the live positions rather than copying them.

        Rebuilding a dict of every position on every bar event was O(positions)
        per event, so O(symbols x bars x positions) across a multi-symbol run.
        The context reads quantities lazily instead.

        Wrapped in a read-only quantity view, rebuilt only when the underlying
        mapping object changes — which is once per run, so the O(1) win
        stands. Handing the raw dict over made every `Position` attribute
        strategy-visible and strategy-*writable*: a strategy could assign to
        `ctx._positions[sym].quantity` and move the engine's own books. That
        is not lookahead, since the state exposed is all historical, but it
        dissolves the barrier that keeps lookahead out — and any field added
        to `Position` later would become visible by default.
        """
        if self._positions_source is not positions:
            self._positions_source = positions
            self._positions = _QuantityView(positions)
        self._equity = equity
        self._cash = cash
        self._margin_reserved = margin_reserved

    def _drain_orders(self) -> list[tuple[Order, Instrument]]:
        orders = self._pending_orders
        self._pending_orders = []
        return orders

    # -- Strategy-facing API ----------------------------------------------------

    @property
    def now(self) -> datetime:
        """Timestamp of the bar currently being processed."""
        if self._now is None:
            raise RuntimeError("No bar has been processed yet")
        return self._now

    @property
    def equity(self) -> Decimal:
        """Total account equity as of the last mark."""
        return self._equity

    @property
    def cash(self) -> Decimal:
        """Uncommitted cash as of the last mark.

        Point-in-time like `equity`, and carrying no more information than it
        does: both are the running mark at this instant, deliberately not the
        end-of-instant value, so a strategy acting on (T, symbol A) still cannot
        see (T, symbol Z)'s close.
        """
        return self._cash

    @property
    def margin_reserved(self) -> Decimal:
        """Capital committed as collateral against open derivative positions."""
        return self._margin_reserved

    @property
    def free_capital(self) -> Decimal:
        """Cash not reserved as margin — what is left to open with.

        The figure a leveraged strategy should size against. Sizing off
        `equity` while holding margined positions asks for capacity the account
        does not have, and the refusal arrives from the risk layer as a
        rejection rather than from the strategy as restraint.

        May be negative. Reported, not clamped: an unfundable book is the state
        a strategy most needs to stop adding to.
        """
        return self._cash - self._margin_reserved

    def instrument(self, symbol: str) -> Instrument:
        try:
            return self._instruments[symbol]
        except KeyError:
            raise KeyError(
                f"Unknown instrument {symbol!r}. Register it with the Backtester "
                f"before the strategy references it."
            ) from None

    def price(self, symbol: str) -> Decimal | None:
        """Most recent close for ``symbol``, or None if never seen."""
        return self._prices.get(symbol)

    def history(self, symbol: str, lookback: int | None = None) -> Sequence[Bar]:
        """Bars seen so far, oldest first. Never includes future bars."""
        bars = list(self._history[symbol])
        if lookback is None:
            return bars
        return bars[-lookback:]

    def closes(self, symbol: str, lookback: int | None = None) -> list[Decimal]:
        return [b.close for b in self.history(symbol, lookback)]

    # -- The cross-section ------------------------------------------------------

    def universe(self) -> Membership:
        """Symbols tradeable at `now`, with how each answer was reached.

        Requires a `UniverseRegistry` on the `Backtester`. It raises without
        one rather than falling back to the registered instruments, because
        that dict is a set of symbols chosen because they exist *today* —
        handing it back as "the universe" is survivorship bias wearing the
        name of the function written to prevent it.

        `Membership.warning()` is non-None whenever any membership rests on
        observed bars rather than asserted listing dates. Surface it; do not
        swallow it.
        """
        if self._universe is None:
            raise RuntimeError(
                "No UniverseRegistry was supplied to the Backtester, so the "
                "tradeable universe at this instant is unknown. The registered "
                "instruments are not an answer: that set was chosen because those "
                "symbols exist today, which is the definition of survivorship "
                "bias. Pass universe=... to Backtester."
            )
        return self._universe.members(self.now)

    def cross_section(self) -> tuple[str, ...]:
        """Symbols with a printed bar at `now`, in symbol order.

        **A symbol that did not print is excluded** — never ranked worst,
        never given the median, never forward-filled. `Panel._refuse_if_masked`
        defers this decision to "the cross-sectional API"; this is that API and
        this is the decision. Inventing a rank for a symbol that did not trade
        is the same prohibition as inventing its price.

        Absence is already point-in-time: a symbol that has not listed does not
        print, and neither does one that has delisted. What the registry adds
        is the ability to tell those apart from a collector outage — which is a
        warning about the data, not a different cross-section. So this works
        without a registry, while `universe()` does not.
        """
        if self._now is None:
            # Before the first bar there is no cross-section. Returning ()
            # without a registry while raising with one made the same call
            # behave differently depending on configuration, which is the kind
            # of asymmetry that gets discovered in a strategy rather than here.
            return ()
        if self._universe is None:
            return tuple(sorted(self._printed))
        members = self._universe.members(self.now)
        return tuple(sorted(s for s in self._printed if s in members))

    def holdings(self) -> tuple[str, ...]:
        """Symbols currently held, long or short, in symbol order.

        A ranker needs this and cannot derive it. Exiting only the symbols it
        can still rank strands any position whose symbol stopped printing or
        fell out of the universe — the position stays open, marked on stale
        data, for the rest of the run. With the vendor archive at 46% coverage
        that is not a corner case.
        """
        return tuple(sorted(s for s, q in self._positions.items() if q != 0))

    def position(self, symbol: str) -> Decimal:
        """Signed position size. Positive is long, negative is short.

        A plain lookup, not a defaultdict: probing an unknown symbol used to
        create an entry, so a scanner touching 5,000 tickers materialised 5,000
        of them whether or not any had data.
        """
        return self._positions.get(symbol, Decimal("0"))

    def is_flat(self, symbol: str) -> bool:
        return self.position(symbol) == 0

    def _next_order_id(self) -> str:
        self._order_seq += 1
        return f"str-{self._order_seq:06d}"

    def submit(
        self,
        symbol: str,
        side: Side,
        quantity: Decimal,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Decimal | None = None,
        time_in_force: TimeInForce = TimeInForce.DAY,
        tag: str = "",
    ) -> Order:
        """Queue an order. It executes against the NEXT bar, not this one."""
        instrument = self.instrument(symbol)
        order = Order(
            order_id=self._next_order_id(),
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            time_in_force=time_in_force,
            created_at=self._now,
            tag=tag,
        )
        self._pending_orders.append((order, instrument))
        if self._order_observer is not None:
            self._order_observer(order)
        return order

    def buy(self, symbol: str, quantity: Decimal, **kwargs: object) -> Order:
        return self.submit(symbol, Side.BUY, quantity, **kwargs)  # type: ignore[arg-type]

    def sell(self, symbol: str, quantity: Decimal, **kwargs: object) -> Order:
        return self.submit(symbol, Side.SELL, quantity, **kwargs)  # type: ignore[arg-type]

    def close_position(self, symbol: str, tag: str = "close") -> Order | None:
        """Flatten ``symbol``. Returns None if already flat."""
        qty = self.position(symbol)
        if qty == 0:
            return None
        side = Side.SELL if qty > 0 else Side.BUY
        return self.submit(symbol, side, abs(qty), tag=tag)

    def target_notional(self, symbol: str, notional: Decimal, tag: str = "") -> Order | None:
        """Trade toward a target signed notional exposure.

        Convenience for equity-relative sizing. Returns None when the required
        adjustment rounds to zero at the instrument's lot size.

        A target on the other side of flat is reached in **two** orders: a
        flatten, then the entry, and the entry is what is returned. One order
        for the whole move would be a flip through zero, which the risk layer
        no longer treats as exposure-reducing (9b) — so the single order would
        be measured by its full size against the order cap and refused, and
        the account could never change side. Split, the flatten is exempt
        (it only shrinks the book) and the entry is checked on its own, which
        is the check the far side was always owed.
        """
        price = self.price(symbol)
        if price is None or price == 0:
            return None
        instrument = self.instrument(symbol)
        lot = instrument.lot_size
        current = self.position(symbol)
        target_qty = notional / (price * instrument.multiplier)

        crosses = current != 0 and target_qty != 0 and (target_qty > 0) != (current > 0)
        if crosses:
            # The flatten is the whole current position, lot-aligned or not:
            # a position built by odd fills is closed by its own size.
            flatten_side = Side.SELL if current > 0 else Side.BUY
            self.submit(symbol, flatten_side, abs(current), tag=tag or "target_notional")
            current = Decimal("0")

        # Round the DELTA, as before ADR 0008. Rounding the target instead
        # emitted a sub-lot order whenever the position was itself off-lot
        # (risk-auditor, 2026-09-02).
        delta = target_qty - current
        delta = (delta / lot).quantize(Decimal("1")) * lot
        if delta == 0:
            return None
        side = Side.BUY if delta > 0 else Side.SELL
        return self.submit(symbol, side, abs(delta), tag=tag or "target_notional")


class Strategy(ABC):
    """Base class for all strategies.

    Only ``on_bar`` is required. Override the lifecycle hooks as needed; they
    default to no-ops so a minimal strategy stays minimal.
    """

    #: Human-readable name used in reports.
    name: str = "unnamed"

    def on_start(self, ctx: StrategyContext) -> None:
        """Called once before the first bar."""

    @abstractmethod
    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
        """Called for each bar, in timestamp order.

        Fires once per **bar**, so a multi-symbol feed calls it several times
        per instant. A strategy that compares symbols to each other wants
        `on_cross_section` instead — see the warning there.
        """

    def on_cross_section(self, timestamp: datetime, ctx: StrategyContext) -> None:
        """Called once per instant, after every bar at `timestamp` is observed.

        The hook a strategy that ranks symbols against each other must use.
        Reading the whole cross-section from inside `on_bar` compares the
        current symbol's `timestamp` close against every other symbol's
        *previous* close, because the feed has not revealed them yet. That is
        not lookahead — it is the reverse — but it is systematic: bars arrive
        sorted by `(timestamp, symbol)`, so the alphabetically-first symbol
        carries one extra bar of information into every ranking, forever.

        Orders submitted here fill at the next instant's open, exactly as they
        do from `on_bar`. The final instant never fires this hook, because an
        order from it could never fill.
        """

    def on_fill(self, fill: Fill, ctx: StrategyContext) -> None:
        """Called after each of this strategy's orders executes."""

    def on_reject(self, rejection: RiskRejection, ctx: StrategyContext) -> None:
        """Called when the risk layer refuses one of this strategy's orders.

        Optional, like the other lifecycle hooks. It exists because without it
        a rejection is invisible to the strategy: `SMACrossover` resubmitted an
        identical order on 346 consecutive bars, not because it was ignoring a
        signal but because nothing ever told it no.
        """

    def on_finish(self, ctx: StrategyContext) -> None:
        """Called once after the last bar."""
