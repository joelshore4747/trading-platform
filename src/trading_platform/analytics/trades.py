"""Round-trip trades.

The engine records *fills*. A fill is not a trade: a strategy that scales into a
position over four bars and exits over two has six fills and one trade, and the
question a researcher actually asks — "how much did that idea make, and how
often does it work?" — is about the trade.

Pairing is **FIFO**. ``Portfolio`` uses average cost for realised P&L
(``engine/portfolio.py``), which is the right choice for accounting and the
wrong one for attribution: it cannot say which entry a given exit closed, so it
cannot produce a holding period or a per-trade return. The two agree exactly on
total P&L, which is what the reconciliation test pins.

The metric this module exists for is ``TradeStats.pnl_without_best_n``. Stage 3
caught a walk-forward fold that scored a Sharpe of 10.55 on a *single* fill.
This asks the same question of a whole run: if removing one trade destroys the
result, the equity curve is one trade wearing a strategy, and no ratio computed
over it means anything.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading_platform.core.events import Fill
from trading_platform.core.instruments import Instrument
from trading_platform.core.types import Side


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """One position, opened and fully closed.

    ``quantity`` is always positive; ``direction`` says which way it was held.
    """

    symbol: str
    direction: Side
    quantity: Decimal
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    #: Price movement only, before costs. Reconciles exactly against the
    #: portfolio's average-cost realised P&L once the position is flat.
    gross_pnl: Decimal
    #: Entry and exit commission, apportioned per unit from the fills that
    #: opened and closed this parcel.
    commission: Decimal
    net_pnl: Decimal

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"A round trip needs positive quantity, got {self.quantity}")
        if self.exit_time < self.entry_time:
            raise ValueError(
                f"{self.symbol}: exit at {self.exit_time} precedes entry at {self.entry_time}"
            )

    @property
    def duration(self) -> timedelta:
        return self.exit_time - self.entry_time

    @property
    def is_win(self) -> bool:
        """Net of costs. A gross win that paid more in commission is a loss."""
        return self.net_pnl > 0

    @property
    def return_pct(self) -> float:
        """Net P&L over the capital the entry tied up. Float at the boundary."""
        basis = self.entry_price * self.quantity
        if basis == 0:
            return 0.0
        return float(self.net_pnl / basis)


@dataclass(slots=True)
class _Lot:
    """An open parcel awaiting an exit."""

    quantity: Decimal  # always positive
    price: Decimal
    timestamp: datetime
    sign: Decimal  # +1 long, -1 short
    commission_per_unit: Decimal


def pair_fills(
    fills: Sequence[Fill],
    instruments: Mapping[str, Instrument] | None = None,
) -> tuple[list[RoundTrip], dict[str, Decimal]]:
    """Pair fills into round trips, FIFO, per symbol.

    Returns the closed trades and the quantity left open per symbol (signed;
    absent when flat). Open quantity is **returned, never marked to market**:
    folding an unrealised mark into a realised trade distribution is precisely
    the flattery this project exists to avoid. It does mean trade P&L will not
    tie to the equity curve while a position is open, and callers should say so
    rather than hide it.

    ``instruments`` supplies contract multipliers. Omitted, every instrument is
    treated as multiplier 1, which is correct for spot and wrong for anything
    with a contract size.
    """
    lots: dict[str, deque[_Lot]] = defaultdict(deque)
    trades: list[RoundTrip] = []

    for fill in fills:
        multiplier = _multiplier(fill.symbol, instruments)
        book = lots[fill.symbol]
        remaining = fill.quantity
        sign = Decimal(fill.side.sign)
        # Per-unit so a fill that closes several lots splits its cost across
        # them in proportion, rather than landing entirely on the first.
        fill_commission_per_unit = (
            fill.commission / fill.quantity if fill.quantity else Decimal("0")
        )

        # Close against opposing lots first, oldest to newest.
        while remaining > 0 and book and book[0].sign != sign:
            lot = book[0]
            closed = min(remaining, lot.quantity)

            gross = (fill.price - lot.price) * closed * multiplier * lot.sign
            commission = closed * (lot.commission_per_unit + fill_commission_per_unit)
            trades.append(
                RoundTrip(
                    symbol=fill.symbol,
                    direction=Side.BUY if lot.sign > 0 else Side.SELL,
                    quantity=closed,
                    entry_time=lot.timestamp,
                    exit_time=fill.timestamp,
                    entry_price=lot.price,
                    exit_price=fill.price,
                    gross_pnl=gross,
                    commission=commission,
                    net_pnl=gross - commission,
                )
            )

            lot.quantity -= closed
            remaining -= closed
            if lot.quantity == 0:
                book.popleft()

        # Whatever is left opens a new lot. When the fill closed everything and
        # kept going, this is the far side of a flip through zero — one closed
        # trade and one fresh position, never two closed trades.
        if remaining > 0:
            book.append(
                _Lot(
                    quantity=remaining,
                    price=fill.price,
                    timestamp=fill.timestamp,
                    sign=sign,
                    commission_per_unit=fill_commission_per_unit,
                )
            )

    open_quantity = {
        symbol: sum((lot.quantity * lot.sign for lot in book), Decimal("0"))
        for symbol, book in lots.items()
        if book
    }
    return trades, open_quantity


@dataclass(frozen=True, slots=True)
class TradeStats:
    """What the distribution of trades says, as opposed to their sum."""

    trades: tuple[RoundTrip, ...]
    #: Signed quantity still open per symbol, excluded from every statistic.
    open_quantity: dict[str, Decimal]

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def unclosed_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self.open_quantity))

    @property
    def wins(self) -> tuple[RoundTrip, ...]:
        return tuple(t for t in self.trades if t.is_win)

    @property
    def losses(self) -> tuple[RoundTrip, ...]:
        return tuple(t for t in self.trades if not t.is_win)

    @property
    def win_rate(self) -> float:
        """Per **trade**, not per period.

        ``PerformanceSummary.win_rate`` counts bars with positive returns,
        which is a different number and routinely mistaken for this one.
        """
        if not self.trades:
            return 0.0
        return len(self.wins) / len(self.trades)

    @property
    def total_net_pnl(self) -> Decimal:
        return sum((t.net_pnl for t in self.trades), Decimal("0"))

    @property
    def total_commission(self) -> Decimal:
        return sum((t.commission for t in self.trades), Decimal("0"))

    @property
    def average_win(self) -> Decimal:
        wins = self.wins
        return sum((t.net_pnl for t in wins), Decimal("0")) / len(wins) if wins else Decimal("0")

    @property
    def average_loss(self) -> Decimal:
        losses = self.losses
        if not losses:
            return Decimal("0")
        return sum((t.net_pnl for t in losses), Decimal("0")) / len(losses)

    @property
    def profit_factor(self) -> float | None:
        """Gross wins over gross losses. None when nothing lost — a run with no
        losing trade has not been tested, and printing infinity implies it has.
        """
        gained = sum((t.net_pnl for t in self.wins), Decimal("0"))
        lost = -sum((t.net_pnl for t in self.losses), Decimal("0"))
        if lost <= 0:
            return None
        return float(gained / lost)

    @property
    def largest_win(self) -> Decimal:
        """Best winning trade, or zero if none won.

        Taken over the winners rather than over all trades: a run of nothing
        but losses has no largest win, and reporting its least-bad loss as one
        reads as a profit that never happened.
        """
        return max((t.net_pnl for t in self.wins), default=Decimal("0"))

    @property
    def largest_loss(self) -> Decimal:
        """Worst losing trade, or zero if none lost. Same reasoning inverted —
        a single winning trade must not be reported as the largest loss."""
        return min((t.net_pnl for t in self.losses), default=Decimal("0"))

    @property
    def median_duration(self) -> timedelta:
        """Median holding period. Computed directly rather than via
        ``statistics.median``, which is not typed for ``timedelta``."""
        if not self.trades:
            return timedelta(0)
        ordered = sorted(t.duration for t in self.trades)
        middle = len(ordered) // 2
        if len(ordered) % 2 == 1:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2

    def pnl_without_best_n(self, n: int = 1) -> Decimal:
        """Total net P&L with the ``n`` best trades removed.

        The concentration test. A result that survives losing its best trade is
        a result; one that does not is a single lucky trade with a strategy
        wrapped around it, and the Sharpe computed over it describes nothing.
        """
        if n <= 0:
            return self.total_net_pnl
        ranked = sorted((t.net_pnl for t in self.trades), reverse=True)
        return sum(ranked[n:], Decimal("0"))

    @property
    def top_trade_share(self) -> float:
        """Best trade's net P&L as a share of the total. Above ~0.5 the run is
        one trade. Zero when the total is not positive — there is no profit to
        attribute, and a ratio against a loss reads backwards.
        """
        total = self.total_net_pnl
        if total <= 0 or not self.trades:
            return 0.0
        return float(self.largest_win / total)

    @property
    def concentration_verdict(self) -> str | None:
        """Plain words for the concentration finding, or None if it is fine."""
        if not self.trades:
            return "No closed trades. Nothing here is a measurement."
        if self.count < 10:
            return (
                f"Only {self.count} closed trade(s). Every statistic below is a description "
                "of this sample, not an estimate of anything."
            )
        without_best = self.pnl_without_best_n(1)
        if self.total_net_pnl > 0 and without_best <= 0:
            return (
                f"Removing the single best trade turns {self.total_net_pnl:,.2f} into "
                f"{without_best:,.2f}. This run is one trade, not a strategy."
            )
        if self.top_trade_share > 0.5:
            return (
                f"The best trade is {self.top_trade_share:.0%} of total profit. The result "
                "rests on one outcome rather than on a repeatable effect."
            )
        return None

    def report(self) -> str:
        lines = [
            f"Closed trades       : {self.count:>10d}",
            f"Win rate (trades)   : {self.win_rate:>10.2%}",
            f"Net P&L             : {self.total_net_pnl:>10,.2f}",
            f"Commission paid     : {self.total_commission:>10,.2f}",
            f"Average win         : {self.average_win:>10,.2f}",
            f"Average loss        : {self.average_loss:>10,.2f}",
            "Profit factor       : "
            + ("       n/a" if self.profit_factor is None else f"{self.profit_factor:>10.2f}"),
            f"Largest win         : {self.largest_win:>10,.2f}",
            f"Largest loss        : {self.largest_loss:>10,.2f}",
            f"Median duration     : {self.median_duration!s:>10}",
            f"Without best trade  : {self.pnl_without_best_n(1):>10,.2f}",
        ]
        if self.open_quantity:
            still_open = ", ".join(f"{s}={q}" for s, q in sorted(self.open_quantity.items()))
            lines.append(f"Still open          : {still_open} (excluded above)")
        verdict = self.concentration_verdict
        if verdict is not None:
            lines += ["", f"CONCENTRATION: {verdict}"]
        return "\n".join(lines)


def summarise_trades(
    fills: Sequence[Fill],
    instruments: Mapping[str, Instrument] | None = None,
) -> TradeStats:
    """Pair fills and describe the resulting distribution."""
    trades, open_quantity = pair_fills(fills, instruments)
    return TradeStats(trades=tuple(trades), open_quantity=open_quantity)


def _multiplier(symbol: str, instruments: Mapping[str, Instrument] | None) -> Decimal:
    if instruments is None:
        return Decimal("1")
    instrument = instruments.get(symbol)
    return Decimal("1") if instrument is None else instrument.multiplier
