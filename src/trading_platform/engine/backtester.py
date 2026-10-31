"""The backtest event loop.

Ordering within each bar is the whole ballgame:

1. Fill orders queued on *previous* bars, using this bar's open.
2. Show the bar to the strategy.
3. Mark the portfolio.
4. Collect the strategy's new orders and queue them for the *next* bar.

Step 1 preceding step 2 is what enforces the one-bar delay between signal and
execution. Reordering these steps produces a strategy that trades on
information it could not have had, which is indistinguishable from a very good
strategy right up until it loses money.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from trading_platform.core.events import Fill, Order
from trading_platform.core.fx import MixedCurrencyError, QuoteRates
from trading_platform.core.instruments import Instrument
from trading_platform.data.feed import DataFeed
from trading_platform.data.funding import FundingSchedule
from trading_platform.data.universe import UniverseRegistry
from trading_platform.engine.execution import CostModel, SimulatedExecutionEngine
from trading_platform.engine.portfolio import EquityPoint, Portfolio, translate_curve
from trading_platform.risk.limits import RiskManager, RiskRejection
from trading_platform.strategy.base import Strategy, StrategyContext

#: How old a mark must be before it is worth reporting. Deliberately generous:
#: a gap shorter than this is ordinary microstructure, and a threshold that
#: fires on everything says nothing. The real archive's synchronised outages
#: leave only 0.12% of marks stale at all, worst case 8 days 13 hours.
STALE_MARK_THRESHOLD = timedelta(hours=6)


@dataclass(slots=True)
class BacktestResult:
    """Everything a run produced. Analysis lives in ``analytics.metrics``."""

    strategy_name: str
    starting_cash: Decimal
    ending_equity: Decimal
    equity_curve: list[EquityPoint]
    fills: list[Fill]
    rejections: list[RiskRejection]
    bars_processed: int
    start: datetime | None = None
    end: datetime | None = None
    halted: bool = False
    unfilled_orders: int = 0
    #: Cumulative funding across the run. Negative means net paid out.
    total_funding: Decimal = Decimal("0")
    funding_settlements: int = 0
    #: Settlements that landed inside a hole in the bar series. Applied once,
    #: but against a later bar than they truly occurred on — so they are
    #: counted rather than passed over in silence.
    funding_in_gaps: int = 0
    #: Symbols that held a position while their mark was older than one
    #: reporting threshold, with the worst age reached. A stale mark is not
    #: wrong on its own — an outage ends and the price resumes — but a symbol
    #: that never resumes is carried at a fabricated price into every
    #: subsequent equity point, and into `total_return`.
    stale_marks: tuple[tuple[str, timedelta], ...] = ()
    #: Every distinct ``quote_currency`` among the run's instruments, sorted.
    #: More than one means the portfolio has been adding unlike units; exactly
    #: one is the denomination of every cash figure below.
    quote_currencies: tuple[str, ...] = ()

    @property
    def quote_currency(self) -> str:
        """The single currency these figures are in.

        Raises when the run mixed several, because there is no honest answer
        in that case — not because the question is unusual.
        """
        if len(self.quote_currencies) == 1:
            return self.quote_currencies[0]
        if not self.quote_currencies:
            raise MixedCurrencyError(
                "this result records no quote currency; it predates the "
                "denomination check or was built by hand"
            )
        raise MixedCurrencyError(
            f"run mixed {list(self.quote_currencies)}. The portfolio holds one "
            "cash balance, so these figures added unlike units together and "
            "describe nothing. Split the run by quote currency."
        )

    def curve_in(self, currency: str, rates: QuoteRates | None = None) -> list[EquityPoint]:
        """The equity curve restated in ``currency``.

        Raises unless the rate is genuinely known. A USDT-quoted run asked for
        USD figures without a USDT/USD series is the case this exists to stop.
        """
        return translate_curve(
            self.equity_curve,
            from_currency=self.quote_currency,
            to_currency=currency,
            rates=rates,
        )

    @property
    def rejection_rate(self) -> float:
        """Share of order intent the risk layer refused.

        Reported prominently because a run where 99% of orders were rejected is
        not a run with a footnote — it is a measurement of the risk layer, not
        of the strategy. One real run showed 7 fills against 346 rejections and
        still printed a +62.5% return beside them as though the two were
        comparable.
        """
        attempted = len(self.fills) + len(self.rejections)
        return len(self.rejections) / attempted if attempted else 0.0

    @property
    def total_return(self) -> Decimal:
        if self.starting_cash == 0:
            return Decimal("0")
        return (self.ending_equity - self.starting_cash) / self.starting_cash

    def rejection_summary_text(self) -> str:
        """Counts of risk rejections by reason, as a readable string."""
        counts: dict[str, int] = {}
        for r in self.rejections:
            counts[r.reason] = counts.get(r.reason, 0) + 1
        if not counts:
            return "none"
        return ", ".join(f"{reason}={n}" for reason, n in sorted(counts.items()))

    def summary(self) -> str:
        unit = f" {self.quote_currencies[0]}" if len(self.quote_currencies) == 1 else ""
        lines = [
            f"Strategy      : {self.strategy_name}",
            f"Period        : {self.start} -> {self.end}",
            f"Bars          : {self.bars_processed}",
            f"Starting cash : {self.starting_cash:,.2f}{unit}",
            f"Ending equity : {self.ending_equity:,.2f}{unit}",
            f"Total return  : {self.total_return:.2%}",
            f"Fills         : {len(self.fills)}",
            f"Risk rejects  : {len(self.rejections)}",
        ]
        if self.funding_settlements:
            lines.append(
                f"Funding       : {self.total_funding:,.2f} over {self.funding_settlements} settlements"
            )
            if self.funding_in_gaps:
                lines.append(f"  of which in data gaps: {self.funding_in_gaps}")
        if len(self.quote_currencies) > 1:
            lines.append(f"REFUSED       : instruments quoted in {list(self.quote_currencies)}.")
            lines.append("                One cash balance cannot hold several currencies;")
            lines.append("                the cash figures above are unlike units summed.")
        elif unit.strip() and unit.strip() != "USD":
            lines.append(
                f"NOTE          : figures are in {unit.strip()}, not USD. {unit.strip()} is a"
            )
            lines.append("                traded asset with its own price; call curve_in('USD',")
            lines.append("                rates) for USD figures.")
        if self.stale_marks:
            worst_symbol, worst_age = self.stale_marks[0]
            lines.append(
                f"STALE MARKS   : {len(self.stale_marks)} symbol(s) held a position while priced"
            )
            lines.append(
                f"                on data over {STALE_MARK_THRESHOLD} old; worst "
                f"{worst_symbol} at {worst_age}."
            )
            lines.append(
                "                Those marks are in the equity curve and in the return above."
            )
        if self.rejection_rate > 0.5:
            lines.append(
                f"REFUSED       : {self.rejection_rate:.0%} of order intent "
                f"({len(self.rejections)} rejections vs {len(self.fills)} fills)."
            )
            lines.append("                These figures describe the risk layer, not the strategy.")
        if self.halted:
            lines.append("STATUS        : HALTED (max drawdown breached)")
        if self.unfilled_orders:
            lines.append(f"Unfilled      : {self.unfilled_orders} order(s) still working at end")
        return "\n".join(lines)


@dataclass(slots=True)
class Backtester:
    """Runs one strategy over one feed."""

    instruments: dict[str, Instrument]
    starting_cash: Decimal = Decimal("100000")
    cost_model: CostModel = field(default_factory=CostModel)
    risk_manager: RiskManager = field(default_factory=RiskManager)
    history_size: int = 512
    #: Optional perpetual funding schedule. When None the loop is unchanged and
    #: spot backtests are bit-identical to a run without this feature.
    funding: FundingSchedule | None = None
    #: Point-in-time universe membership, for strategies that rank symbols
    #: against each other. Without it `ctx.universe()` raises rather than
    #: falling back to `instruments`, which is a set of symbols chosen because
    #: they exist today.
    universe: UniverseRegistry | None = None
    #: Told about every order as the strategy queues it. Observation only —
    #: the paper supervisor journals intent through it. None by default, and
    #: the loop is unchanged when it is.
    order_observer: Callable[[Order], None] | None = None

    def run(
        self, strategy: Strategy, feed: DataFeed, *, portfolio: Portfolio | None = None
    ) -> BacktestResult:
        """Run `strategy` over `feed`.

        `portfolio` seeds the run with an existing ledger — the paper
        supervisor's account cycle, rebuilt from its journal on boot — so a
        restarted session continues the book it left rather than starting
        flat with fresh cash. Fills, marks and the equity curve go to that
        object. Left None, a fresh ledger with `starting_cash` is used and
        every recorded result is bit-identical to before this parameter.
        """
        if portfolio is None:
            portfolio = Portfolio(starting_cash=self.starting_cash)
        execution = SimulatedExecutionEngine(cost_model=self.cost_model)
        ctx = StrategyContext(
            instruments=self.instruments,
            history_size=self.history_size,
            universe=self.universe,
            order_observer=self.order_observer,
        )
        risk = self.risk_manager

        self._validate_feed_symbols(feed)

        strategy.on_start(ctx)

        # Whether this strategy actually wants the once-per-instant hook.
        # Checked once, and the loop below skips the whole block when it does
        # not — so a strategy that never overrides `on_cross_section` produces
        # a bit-identical RESULT, and every number recorded against the old
        # loop stays valid. Same standard the `funding` field holds itself to.
        #
        # Result-identity, not instruction identity: `_observe` now does a
        # timestamp comparison and a `set.add` for every strategy, hook or not.
        # Neither can move a number, and the claim is deliberately stated at
        # the strength that is actually true.
        # Compared through `__func__` rather than on the class, because
        # `type(strategy)` skips the instance `__dict__` by design: a hook
        # assigned per instance -- a wrapper, a patch, a test double -- would
        # never be called, no error would be raised, and the run would finish
        # with zero fills. In the fill record that is indistinguishable from a
        # ranker that found nothing worth trading. A bound method of a
        # non-overriding subclass still has `__func__ is Strategy...`, so
        # inheritance keeps the old path and bit-identity holds.
        _hook = strategy.on_cross_section
        fires_cross_section = getattr(_hook, "__func__", _hook) is not Strategy.on_cross_section

        bars_processed = 0
        first_ts: datetime | None = None
        last_ts: datetime | None = None
        # Seeded from the portfolio's own marks so a restored position is
        # visible to the risk layer from the first order, not from the first
        # bar its symbol happens to print. Without this a restarted account
        # short 60 perps that had lost 90% read as fully solvent with zero
        # gross exposure, and a 24x buy passed (adversarial-reviewer,
        # 2026-09-02). A fresh portfolio has no marks, so this is empty and
        # the loop is unchanged.
        prices: dict[str, Decimal] = {
            symbol: price
            for symbol in portfolio.positions
            if (price := portfolio.last_price(symbol)) is not None
        }
        # Advances only when the feed's timestamp advances, so a multi-symbol
        # feed emitting several bars at the same instant settles funding once.
        funding_watermark: datetime | None = None
        total_funding = Decimal("0")
        funding_settlements = 0
        funding_in_gaps = 0
        # Bars can never be closer together than the true bar interval, so the
        # smallest gap observed so far IS that interval. Any step larger than
        # it means bars are missing — which is how a funding settlement ends up
        # charged against a later bar than the one it truly occurred in.
        min_bar_step: timedelta | None = None

        marked_watermark: datetime | None = None
        # Worst mark age reached by any symbol while it held a position.
        # Sampled once per instant rather than per bar event: a multi-symbol
        # feed emits many bars at one timestamp and the age is a property of
        # the instant, not of the event.
        worst_mark_age: dict[str, timedelta] = {}
        staleness_watermark: datetime | None = None

        for bar in feed:
            # Close out the previous instant before touching this one, so the
            # equity curve carries one point per timestamp rather than one per
            # bar event. Done lazily because a streaming feed cannot know it
            # has reached the end of an instant until the next one arrives.
            if marked_watermark is not None and bar.timestamp != marked_watermark:
                portfolio.mark(marked_watermark)

                # The instant just closed, so every bar stamped
                # `marked_watermark` has been observed and the strategy can
                # compare symbols to each other without one of them being a
                # bar behind. This is the only place that is true.
                #
                # Placement earns the delay for free: orders queued here enter
                # `execution` before step 1 below runs for this bar, so they
                # fill against THIS bar's open — the next instant after the one
                # that generated them, exactly what `on_bar` gets. Nothing is
                # peeked from the feed; the instant is closed lazily when its
                # successor arrives, the same idiom the mark above uses.
                #
                # Equity is the end-of-instant mark, which is correct here and
                # would not be inside `on_bar`: at (T, symbol A) the later
                # symbols' T closes are not yet revealed, but once T has closed
                # they all are.
                if fires_cross_section:
                    risk.start_bar(bar.timestamp)
                    ctx._sync(
                        portfolio.positions,
                        portfolio.marked_equity,
                        portfolio.cash,
                        portfolio.margin_reserved,
                    )
                    strategy.on_cross_section(marked_watermark, ctx)
                    # Stamped with the instant the decision was taken, not the
                    # one arriving. `_queue_orders` writes this onto every
                    # RiskRejection and `on_reject` fires inside this pass, so
                    # `bar.timestamp` told the strategy when the next instant
                    # was -- before any bar of it had been observed and while
                    # `ctx.now` still reported the previous one. Worth nothing
                    # on a regular grid; on the vendor archive, where coverage
                    # is 46% and the symbols share no grid, "when does anything
                    # print next" is genuinely not knowable at T's close.
                    self._queue_orders(
                        ctx, execution, portfolio, risk, prices, marked_watermark, strategy
                    )
            marked_watermark = bar.timestamp

            bars_processed += 1
            if first_ts is None:
                first_ts = bar.timestamp
            last_ts = bar.timestamp
            risk.start_bar(bar.timestamp)

            # 1. Execute orders queued on previous bars, against this bar's open.
            for fill, instrument in execution.process(bar, bar.timestamp):
                portfolio.apply_fill(instrument, fill)
                # A cross-section order approved under THIS timestamp has now
                # filled, so its delta lives in the position: release exactly
                # that order's share, and nothing approved for a symbol that
                # has not printed yet (gauntlet, 2026-09-03).
                risk.release_filled(fill)
                strategy.on_fill(fill, ctx)

            # 2. Settle any funding that fell in (watermark, bar.timestamp].
            #    After the fills, because a position opened at this bar's open
            #    genuinely exists while funding settles during the interval.
            #    Before the reveal, so the strategy sees post-funding equity.
            #    Every settlement applied is stamped <= bar.timestamp, which is
            #    information the strategy is about to be shown anyway — so this
            #    cannot introduce lookahead.
            if self.funding is not None and bar.timestamp != funding_watermark:
                step = None if funding_watermark is None else bar.timestamp - funding_watermark
                if step is not None:
                    min_bar_step = step if min_bar_step is None else min(min_bar_step, step)
                window_spans_a_gap = (
                    step is not None and min_bar_step is not None and step > min_bar_step
                )

                settled, paid = self._settle_funding(
                    portfolio, funding_watermark, bar.timestamp, prices
                )
                funding_settlements += settled
                total_funding += paid
                if window_spans_a_gap:
                    funding_in_gaps += settled
                funding_watermark = bar.timestamp

            # 3. Reveal the bar to the strategy.
            ctx._observe(bar)
            prices[bar.symbol] = bar.close
            portfolio.observe_price(bar.symbol, bar.close, bar.timestamp)

            # 4. Update risk state against the running mark.
            #
            #    Deliberately NOT the end-of-instant value. A strategy acting
            #    on (T, symbol A) must not see equity that already includes
            #    (T, symbol Z)'s close — the feed has not revealed it yet, and
            #    folding it into ctx.equity would be a cross-symbol lookahead
            #    of exactly the kind the fill ordering exists to prevent.
            if bar.timestamp != staleness_watermark:
                staleness_watermark = bar.timestamp
                for held, position in portfolio.positions.items():
                    if position.is_flat:
                        continue
                    age = portfolio.mark_age(held, bar.timestamp)
                    if age is not None and age > STALE_MARK_THRESHOLD:
                        previous = worst_mark_age.get(held)
                        if previous is None or age > previous:
                            worst_mark_age[held] = age

            equity = portfolio.marked_equity
            risk.observe_equity(equity)
            ctx._sync(portfolio.positions, equity, portfolio.cash, portfolio.margin_reserved)

            # 5. Let the strategy act, then risk-check what it asked for.
            strategy.on_bar(bar, ctx)
            self._queue_orders(ctx, execution, portfolio, risk, prices, bar.timestamp, strategy)

        # The final instant never sees a successor, so close it here.
        if marked_watermark is not None:
            portfolio.mark(marked_watermark)

        strategy.on_finish(ctx)

        ending_equity = portfolio.equity(prices)
        return BacktestResult(
            strategy_name=strategy.name,
            starting_cash=portfolio.starting_cash,
            ending_equity=ending_equity,
            equity_curve=portfolio.equity_curve,
            fills=portfolio.fills,
            rejections=risk.rejections,
            bars_processed=bars_processed,
            start=first_ts,
            end=last_ts,
            halted=risk.halted,
            unfilled_orders=execution.pending_count,
            total_funding=total_funding,
            funding_settlements=funding_settlements,
            funding_in_gaps=funding_in_gaps,
            stale_marks=tuple(sorted(worst_mark_age.items(), key=lambda pair: (-pair[1], pair[0]))),
            quote_currencies=tuple(sorted({i.quote_currency for i in self.instruments.values()})),
        )

    def _settle_funding(
        self,
        portfolio: Portfolio,
        after: datetime | None,
        upto: datetime,
        prices: dict[str, Decimal],
    ) -> tuple[int, Decimal]:
        """Apply every settlement in ``(after, upto]``.

        Returns ``(count_applied, total_paid)``. Whether the window spanned a
        gap in the bar series is decided by the caller, which knows the spacing.

        A settlement is priced at the most recent close available for its
        symbol. That is the best information that existed at the settlement
        instant — using this bar's close instead would be reaching into the
        future for a payment that already happened.
        """
        assert self.funding is not None
        settled = 0
        paid = Decimal("0")

        for rate in self.funding.settlements_in(after, upto):
            instrument = self.instruments.get(rate.symbol)
            if instrument is None:
                continue
            price = prices.get(rate.symbol)
            if price is None:
                # No bar seen for this symbol yet, so no position can exist.
                continue

            payment = portfolio.apply_funding(instrument, rate.rate, price)
            if payment == 0:
                continue  # flat at the settlement instant

            settled += 1
            paid += payment

        return settled, paid

    def _validate_feed_symbols(self, feed: DataFeed) -> None:
        # An instrument filed under a key that is not its own symbol is a
        # silent order-of-magnitude error, not a naming inconvenience.
        # `Portfolio.position()` binds by `instrument.symbol` while everything
        # else looks up by key, and `_refresh` branches on the bound
        # instrument's `is_derivative` — so a Spot registered under a
        # perpetual's key marks full notional where unrealised PnL belongs.
        # Measured on a 10-unit position at 100 that moved to 110: 1,100
        # instead of 100. Nothing raises, and no reported figure looks wrong.
        mismatched = [
            (key, instrument.symbol)
            for key, instrument in self.instruments.items()
            if key != instrument.symbol
        ]
        if mismatched:
            raise KeyError(
                f"Instruments registered under a key that is not their symbol: "
                f"{mismatched}. The portfolio keys positions by the instrument's "
                "own symbol and everything else keys by the dict key, so the two "
                "would describe different contracts."
            )

        unknown = set(feed.symbols) - set(self.instruments)
        if unknown:
            raise KeyError(
                f"Feed contains symbols with no registered instrument: {sorted(unknown)}. "
                f"Registered: {sorted(self.instruments)}"
            )

    def _queue_orders(
        self,
        ctx: StrategyContext,
        execution: SimulatedExecutionEngine,
        portfolio: Portfolio,
        risk: RiskManager,
        prices: dict[str, Decimal],
        timestamp: datetime,
        strategy: Strategy,
    ) -> None:
        pending = ctx._drain_orders()
        if not pending:
            return

        equity = portfolio.equity(prices)

        # Snapshotted ONCE, before anything in this batch is submitted, so
        # orders approved below are counted by `RiskManager._pending_delta`
        # and not a second time here. What this adds is the orders still
        # working from EARLIER instants — invisible to both the filled
        # position and to `_pending_delta`, which the risk manager clears
        # whenever the timestamp advances.
        #
        # Without it, two orders each sized to `max_position_notional`, split
        # across an instant where the symbol did not print, both passed and
        # both filled: twice the cap, zero rejections. With 46% archive
        # coverage and no shared timestamp grid, a symbol going quiet between
        # two decisions is ordinary rather than exotic.
        live = execution.working_quantity()
        # Worst case per symbol, matching what `RiskManager.check` subtracts:
        # the larger of the book with the working orders and without them.
        # Adding them as a sum of absolutes while `check` subtracted an
        # absolute of a sum counted an offsetting order twice and refused a
        # true 0.01x of leverage as 0.03x.
        gross = self._gross_exposure(portfolio, prices, live)
        # Snapshotted for the symbols this batch touches, and only those. The
        # mapping the engine hands back is live, so reading it inside the loop
        # would pick up orders this batch just submitted — which
        # `_pending_delta` is already counting, and counting both is what made
        # two orders of 500 breach a 1,000 cap.
        working = {order.symbol: live.get(order.symbol, Decimal("0")) for order, _ in pending}

        # Scoped to this batch, because `working` above is snapshotted per
        # batch. Clearing per timestamp left an order approved in an earlier
        # batch of the same instant in `_pending_delta` as well, counted twice:
        # two orders of 500 against a 1,000 cap were refused as "1500 > 1000".
        risk.start_batch()

        for order, instrument in pending:
            reference = prices.get(order.symbol)
            if reference is None:
                # No price yet for this symbol, so the order cannot be sized or
                # risk-checked. Recorded rather than dropped: this was the one
                # path in the whole pipeline where intent disappeared with no
                # trace, which is precisely what the rejection log exists to
                # prevent. It bites hardest on the first bar of a
                # cross-symbol strategy, whose opening order is for a symbol
                # the loop has not reached yet.
                risk.record_rejection(
                    timestamp=timestamp,
                    symbol=order.symbol,
                    reason="no_reference_price",
                    detail=(
                        f"no bar seen for {order.symbol} yet; "
                        "it may sort after the symbol that triggered this order"
                    ),
                )
                # Told to the strategy like any other refusal, so a journal
                # built on `on_reject` sees the outcome of every order.
                strategy.on_reject(risk.rejections[-1], ctx)
                continue
            allowed = risk.check(
                order=order,
                instrument=instrument,
                reference_price=reference,
                current_quantity=portfolio.quantity_of(order.symbol),
                working_quantity=working.get(order.symbol, Decimal("0")),
                equity=equity,
                gross_exposure=gross,
                timestamp=timestamp,
                margin_reserved=portfolio.margin_reserved,
            )
            if allowed:
                execution.submit(order, instrument)
            elif risk.rejections:
                strategy.on_reject(risk.rejections[-1], ctx)

    def _gross_exposure(
        self,
        portfolio: Portfolio,
        prices: dict[str, Decimal],
        working: dict[str, Decimal] | None = None,
    ) -> Decimal:
        """Absolute notional across the book, counting orders still working.

        One traversal, not two. This runs once per bar event and is O(universe),
        so it is quadratic overall — that is pre-existing and the scaling guard
        tolerates it, but a *second* O(universe) pass for the working orders
        roughly doubled the constant and pushed it over.

        `notional` is linear in quantity at a fixed price, so the worst case of
        "these fill" against "they do not" collapses to one multiply on a
        scalar difference rather than two notionals and a subtraction.
        """
        working = working or {}
        total = Decimal("0")
        for pos in portfolio.positions.values():
            price = prices.get(pos.symbol)
            if price is None or pos.is_flat:
                continue
            held = pos.quantity
            pending = working.get(pos.symbol, Decimal("0"))
            units = max(abs(held), abs(held + pending))
            total += pos.instrument.notional(price, units)
        # Working orders in symbols the book is flat in are missed by the loop
        # above, and they are exactly the ones about to open a position.
        for symbol, quantity in working.items():
            if portfolio.quantity_of(symbol) != 0:
                continue
            price = prices.get(symbol)
            instrument = self.instruments.get(symbol)
            if price is None or instrument is None:
                continue
            total += abs(instrument.notional(price, quantity))
        return total
