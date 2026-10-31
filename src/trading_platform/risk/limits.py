"""Pre-trade risk limits.

Every order passes through ``RiskManager.check`` before reaching the execution
engine. Rejections are recorded rather than raised, so a backtest surfaces
"this strategy wanted to trade 40x its equity" as data instead of a crash.

This module is the seam where live trading would enforce hard limits. It is
written now, while nothing is live, precisely so the limits exist before the
temptation to skip them does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_platform.core.events import Fill, Order
from trading_platform.core.instruments import Instrument
from trading_platform.risk.leverage import LeverageGate


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Hard bounds on what a strategy may do.

    ``None`` disables an individual check. Disabling all of them is supported
    for isolating engine behaviour in tests, and is not a sane production
    configuration.
    """

    #: Max notional of any single order, as a FRACTION OF CURRENT EQUITY.
    #: Equity-relative because a limit is a risk appetite, and a risk appetite
    #: expressed as a constant currency amount only means what it says at one
    #: equity level. The absolute form below froze at the starting cash and
    #: turned into an equity gate: on a real run it admitted an entry only
    #: while the account was underwater and refused all 346 attempts once it
    #: was ahead, so the reported +62.5% was the return of a strategy
    #: permitted to trade only while losing.
    max_order_notional_pct: Decimal | None = Decimal("1.0")
    #: Max absolute notional in one instrument, as a fraction of equity.
    max_position_notional_pct: Decimal | None = Decimal("1.5")
    #: Optional hard currency ceilings. Whichever bound is TIGHTER applies, so
    #: adding the percentage form can never permit more than these alone did.
    #: Default None: absolute caps are venue- and mandate-specific and should
    #: be set deliberately, not inherited from a number that happened to match
    #: a `starting_cash` default in another module.
    max_order_notional: Decimal | None = None
    max_position_notional: Decimal | None = None
    #: Max sum of absolute position notionals, as a multiple of equity.
    max_gross_leverage: Decimal | None = Decimal("2")
    #: Max fraction of equity committed as initial margin.
    #:
    #: Distinct from `max_gross_leverage`, which bounds notional *exposure*.
    #: This bounds *collateral*, and the two stop agreeing exactly where it
    #: matters: margin is re-measured at the current price, so a book that has
    #: moved against the account can become unfundable with no new order placed
    #: at all. Gross leverage cannot see that state; `free_cash` going negative
    #: is what it looks like.
    #:
    #: Default 1.0 — an account may commit all of its equity as collateral and
    #: no more. Loose on purpose: on the shipped defaults `max_gross_leverage`
    #: is the tighter bound, so this binds only on genuinely leveraged
    #: instruments, which is the case it exists for.
    max_margin_utilisation: Decimal | None = Decimal("1.0")
    #: Trading halts once drawdown from peak equity exceeds this fraction.
    max_drawdown_pct: Decimal | None = Decimal("0.25")
    #: Max orders EXAMINED per timestamp, per symbol. Counts rejected orders
    #: too: a runaway loop is far likelier to be emitting invalid orders than
    #: valid ones, and counting only accepted orders meant the guard never
    #: engaged while a real run logged 346 rejections.
    max_orders_per_bar: int | None = 10

    @property
    def everything_disabled(self) -> bool:
        """The isolation configuration: no limit of any kind. Never production.

        There is deliberately no `__post_init__` guard forbidding partially
        disabled configurations. I wrote one, and it fired on correct usage —
        disabling the notional caps to isolate the leverage check is exactly
        how a focused test is written. That is the same failure as the
        stalled-feed check that flagged 36 of 39 symbols: a guard that fires on
        legitimate use gets switched off, and then guards nothing.

        The real protection is that the DEFAULTS bind. Reaching an unbounded
        configuration takes two explicit `None`s, which is a statement of
        intent, not an oversight.
        """
        return all(
            getattr(self, name) is None
            for name in (
                "max_order_notional_pct",
                "max_position_notional_pct",
                "max_order_notional",
                "max_position_notional",
                "max_gross_leverage",
                "max_margin_utilisation",
                "max_drawdown_pct",
                "max_orders_per_bar",
            )
        )


@dataclass(slots=True)
class RiskRejection:
    timestamp: datetime
    symbol: str
    reason: str
    detail: str


@dataclass(slots=True)
class _Approval:
    """What one approved order added to the pending accumulators.

    Kept per order so a fill can take back exactly its own contribution. A
    blanket clear after each bar event forgot approvals for orders that had
    not filled yet — in a multi-symbol instant every other symbol's bar event
    wiped them — and the caps and the halt could be walked through again by
    splitting intent across symbols' `on_bar` calls (gauntlet, 2026-09-03).
    """

    order_id: str
    symbol: str
    quantity: Decimal
    signed_delta: Decimal
    gross_delta: Decimal
    margin_delta: Decimal


@dataclass(slots=True)
class RiskManager:
    """Stateful limit enforcement across a run."""

    limits: RiskLimits = field(default_factory=RiskLimits)
    #: Refuses an instrument whose leverage this account has never acknowledged.
    #: `None` means no account context and therefore no gate — which is correct
    #: for a RESEARCH backtest, where no capital is at stake and gating would
    #: only teach people to switch the gate off. It is **not** correct at PAPER
    #: or above, where whoever constructs the manager for an account builds the
    #: gate from that account's rows (`store/accounts.py`). See ADR 0011.
    leverage_gate: LeverageGate | None = None
    rejections: list[RiskRejection] = field(default_factory=list)
    peak_equity: Decimal = Decimal("0")
    halted: bool = False
    _orders_this_bar: dict[str, int] = field(default_factory=dict)
    #: Signed quantity approved this timestamp but not yet filled. Without it
    #: every order in a bar is checked against the same stale position, so
    #: splitting one oversized order into several defeats the position and
    #: leverage caps entirely — five 1,000-notional orders reached 5,000
    #: against a 1,500 cap with zero rejections logged.
    _pending_delta: dict[str, Decimal] = field(default_factory=dict)
    #: Notional approved this timestamp, so the batch is checked against the
    #: book it is creating rather than the one it started from.
    _pending_gross: Decimal = Decimal("0")
    #: Margin approved this timestamp but not yet filled — the R2 fix applied
    #: to collateral. Without it, one oversized position split across several
    #: orders passes the margin check as many times as it is sliced.
    _pending_margin: Decimal = Decimal("0")
    #: Counters reset when the TIMESTAMP advances, not on every bar event. The
    #: engine emits one event per symbol per instant, so clearing per event
    #: turned a limit of 10 into 10 x symbols at each instant.
    _bar_watermark: datetime | None = None
    #: Approvals this timestamp that have not yet been consumed by a fill.
    _approved: list[_Approval] = field(default_factory=list)

    def start_bar(self, timestamp: datetime | None = None) -> None:
        """Reset the per-timestamp order counter.

        Idempotent within a timestamp: called once per bar event by the engine,
        but only the first call at a new instant actually clears. Passing no
        timestamp preserves the old unconditional behaviour for callers that
        genuinely mean "new bar".

        It also clears the pending accumulators, because a new instant is
        necessarily a new batch. That is belt-and-braces for the engine, which
        calls `start_batch` on every batch anyway, and it keeps the contract
        direct callers have always had.
        """
        if timestamp is not None and timestamp == self._bar_watermark:
            return
        self._bar_watermark = timestamp
        self._orders_this_bar.clear()
        self.start_batch()

    def start_batch(self) -> None:
        """Reset what is accumulated across one call to `_queue_orders`.

        Scoped to the batch, not the timestamp, because the engine snapshots
        still-working orders once per batch. An instant runs one batch per bar
        event — plus a separate one for `on_cross_section` — so an order
        approved in an earlier batch is already in that snapshot. Clearing per
        timestamp left it in `_pending_delta` as well and counted it twice:
        two orders of 500 against a 1,000 cap were refused as "1500 > 1000".

        Over-tightening is not the safe direction. It removes trades a strategy
        was entitled to make, and every number downstream then measures the
        risk layer rather than the strategy.
        """
        self._pending_delta.clear()
        self._pending_gross = Decimal("0")
        self._pending_margin = Decimal("0")
        self._approved.clear()

    def release_filled(self, fill: Fill) -> None:
        """Take one fill's contribution out of the pending accumulators.

        Pending deltas exist so a batch is checked against the book it is
        building. Once an order has filled, the position holds it, and keeping
        its delta counts it twice — the engine hits this where `on_cross_section`
        orders are approved under the *arriving* bar's timestamp and fill inside
        that same timestamp, so `start_bar`'s watermark never clears them
        (`risk-auditor`, 2026-09-02). The first fix cleared everything after
        each bar event; the gauntlet showed that forgets approvals for orders
        that have NOT filled yet whenever another symbol's bar arrives in the
        same instant, and the caps and the halt were bypassable again by
        splitting intent across symbols. So the release is per order, scaled
        by the filled quantity, and an unknown order id (approved in an earlier
        instant and already cleared) is a no-op. Counters are untouched: the
        rate limit is per instant regardless of fills.
        """
        for index, approval in enumerate(self._approved):
            if approval.order_id != fill.order_id or approval.symbol != fill.symbol:
                continue
            share = min(fill.quantity, approval.quantity) / approval.quantity
            self._pending_delta[approval.symbol] = (
                self._pending_delta.get(approval.symbol, Decimal("0"))
                - approval.signed_delta * share
            )
            self._pending_gross -= approval.gross_delta * share
            self._pending_margin -= approval.margin_delta * share
            remaining = approval.quantity - fill.quantity
            if remaining <= 0:
                del self._approved[index]
            else:
                approval.quantity = remaining
                approval.signed_delta *= 1 - share
                approval.gross_delta *= 1 - share
                approval.margin_delta *= 1 - share
            return

    def observe_equity(self, equity: Decimal) -> None:
        """Update the drawdown tracker and trip the halt if breached."""
        if equity > self.peak_equity:
            self.peak_equity = equity
        if self.limits.max_drawdown_pct is None or self.peak_equity <= 0:
            return
        drawdown = (self.peak_equity - equity) / self.peak_equity
        if drawdown > self.limits.max_drawdown_pct:
            self.halted = True

    def check(
        self,
        order: Order,
        instrument: Instrument,
        reference_price: Decimal,
        current_quantity: Decimal,
        equity: Decimal,
        gross_exposure: Decimal,
        timestamp: datetime,
        working_quantity: Decimal = Decimal("0"),
        margin_reserved: Decimal = Decimal("0"),
    ) -> bool:
        """Return True if the order may proceed; record a rejection otherwise.

        `working_quantity` is signed quantity submitted and not yet filled. It
        is passed separately rather than folded into `current_quantity`, and
        the difference is a hole I opened and then had to close: netted in, an
        order that OPPOSES the position cancels it on paper, and since nothing
        cancels a working order — `time_in_force` is stored and never enforced
        — an unmarketable limit buy grants permanent credit. Measured at **10x
        the position cap with zero rejections**, and worse, it let a halted
        account open fresh exposure by making an opening order look reducing.

        So the caps are applied to the WORST case: the larger of what the order
        produces if the working orders fill and if they never do. A limit is a
        bound on what can happen, not on the average of what might.

        `margin_reserved` is what the book already commits as collateral.
        Defaulted to zero so spot-only callers are unaffected: with no
        derivative positions the margin check can never bind, and requiring
        every caller to pass a zero would be noise.

        Both defaults are zero and both were added independently on two
        branches (merged 2026-09-07). They constrain different things — one
        bounds quantity that might yet arrive, the other bounds collateral
        already committed — so keeping only one would silently drop a limit.
        """
        # R5: counted before any check, because rate-limiting is about
        # submission volume, not outcomes. Counting only accepted orders meant
        # a loop emitting invalid orders was never limited at all.
        count = self._orders_this_bar.get(order.symbol, 0)
        self._orders_this_bar[order.symbol] = count + 1

        pending = self._pending_delta.get(order.symbol, Decimal("0"))
        delta = order.quantity * order.side.sign
        # Nothing obliges an unfilled order to fill, so the worst case is over
        # every SUBSET of them: take the positive ones together, and the
        # negative ones together, and keep whichever lands further from flat.
        #
        # Two bases -- "all fill" and "none fill" -- is not enough, which cost
        # a round of this fix. With a short position, an approved buy and a
        # working buy, both bases net near zero while the true exposure walks
        # away: the breach went from 10x the cap to 2x rather than to none.
        # **Both `pending` and `working_quantity` get the worst-subset
        # treatment**, and the 2026-09-07 merge tried the alternative before
        # settling here. Treating `pending` as committed — on the reasoning that
        # an order approved in this batch fills at the next bar's open — is true
        # of a MARKET order and false of a limit one, and the risk layer cannot
        # tell them apart. Measured: it took the working-order breach from none
        # back to 2x the position cap, which is the same 2x the other branch
        # recorded when it tried two bases instead of subsets.
        #
        # The cost is a false rejection, not a hole: a flatten and the entry
        # behind it (the pair `target_notional` emits, ADR 0008) are measured as
        # though the flatten might not fill, so the entry can be refused against
        # a book that is really flat. Filed as a ticket rather than fixed by
        # relaxing the bound — a limit that is too tight refuses a trade, and one
        # that is too loose lets a halted account open fresh risk.
        unfilled = (pending, working_quantity)
        upward = sum((q for q in unfilled if q > 0), Decimal("0"))
        downward = sum((q for q in unfilled if q < 0), Decimal("0"))

        high_base = current_quantity + upward
        low_base = current_quantity + downward

        def worst(extra: Decimal) -> Decimal:
            """How far from flat the book can end up. Caps read this."""
            high = high_base + extra
            low = low_base + extra
            return high if abs(high) >= abs(low) else low

        # Reducing asks a DIFFERENT question from the caps, and conflating them
        # cost a round of this fix. A cap asks how big the position could get,
        # so it takes the worst subset. The exemption asks whether the order can
        # only shrink the book, so it must hold under EVERY subset — checking
        # the worst one alone let the empty subset dominate the baseline, and a
        # halted account long 100 was walked short again through sells each
        # individually labelled as flattening.
        reduces_exposure = self._reduces_exposure_between(
            high_base, high_base + delta
        ) and self._reduces_exposure_between(low_base, low_base + delta)

        if self.halted and not reduces_exposure:
            self._reject(timestamp, order.symbol, "halted", "max drawdown breached")
            return False

        # R4: equity at or below zero is where leverage matters most, and the
        # old `equity > 0` guard skipped the whole check there — turning a
        # division-by-zero edge case into a policy hole in the one state that
        # cannot tolerate one.
        if equity <= 0 and not reduces_exposure:
            self._reject(
                timestamp,
                order.symbol,
                "insolvent",
                f"equity {equity} <= 0; only exposure-reducing orders permitted",
            )
            return False

        # Leverage this account never acknowledged. Placed with `halted` and
        # `insolvent` rather than among the numeric caps because it is the same
        # kind of rule: a condition on whether the account may take a position
        # at all, not on how large. Exempt for reducing orders for the same
        # reason those two are — an account that somehow holds an
        # unacknowledged leveraged position must still be able to close it. A
        # gate that trapped a position would be a risk control that creates
        # risk.
        if self.leverage_gate is not None and not reduces_exposure:
            refusal = self.leverage_gate.refusal(instrument)
            if refusal is not None:
                self._reject(timestamp, order.symbol, "leverage_unacknowledged", refusal)
                return False

        # Considered and kept (risk-auditor, 2026-09-02): the rate limit sits
        # ABOVE the reducing exemption, so a strategy that burns its per-symbol
        # budget on refused orders cannot exit until the next instant. That is
        # the runaway-loop guard doing its job — a loop emitting ten orders a
        # bar is not one whose eleventh should be trusted to be the exit — and
        # the account is never trapped: the budget resets with the timestamp.
        # `test_reducing_orders_are_still_rate_limited` pins the choice.
        if self.limits.max_orders_per_bar is not None and count >= self.limits.max_orders_per_bar:
            self._reject(
                timestamp,
                order.symbol,
                "order_rate",
                f"{count} orders already examined this timestamp",
            )
            return False

        # Size caps bound how much risk may be TAKEN, so an order that can only
        # shrink the book is exempt from them. Not a convenience: with
        # equity-relative caps, a drawdown shrinks every cap proportionally, so
        # a position built at higher equity can exceed the cap it is now
        # measured against — and the account becomes unable to close it. That
        # is the trapped-account bug returning through the new units, and a
        # composition test caught it at equity 0 where the cap collapses to
        # exactly zero.
        #
        # Safe because `_reduces_exposure_between` requires the projected position to
        # keep the current sign (or reach exactly zero) AND shrink. A flip to
        # the other side — even a smaller one — is refused that label (9b):
        # the earlier `|projected| < |current|` test alone called long 100 →
        # short 30 a reduction, and a halted account accepted 13,000 of
        # notional past a 1,500 cap in one order with nothing logged. So the
        # largest exempt order is a full flatten, and that never adds risk.
        # Reducing orders remain subject to the rate limit above.
        if reduces_exposure:
            # Credit the book the batch is building, as the risk-taking path
            # does below. Without this a flatten followed by an entry on the
            # far side (the two orders `target_notional` now emits, ADR 0008)
            # measured the entry against gross exposure and margin that the
            # flatten had already released — a false rejection at exactly 2x
            # on a book that was really at 1x. Found by `risk-auditor` on the
            # 9b fix, 2026-09-02.
            #
            # Merge note (2026-09-07): this branch wrote `effective_quantity =
            # current_quantity + pending`. The other branch generalised that to
            # `worst()`, which takes the worst SUBSET of unfilled orders rather
            # than their sum, because nothing obliges an unfilled order to fill.
            # `worst(0)` is the same quantity when there are no working orders
            # and a more conservative one when there are, so substituting it can
            # only make a limit stricter — the safe direction for a risk
            # control, and the only one worth taking without a measurement.
            projected_qty = worst(delta)
            held_notional = abs(instrument.notional(reference_price, worst(Decimal("0"))))
            projected_notional = abs(instrument.notional(reference_price, projected_qty))
            gross_delta = projected_notional - held_notional
            margin_delta = Decimal("0")
            if instrument.is_derivative:
                margin_delta = instrument.margin_requirement(
                    reference_price, projected_qty
                ) - instrument.margin_requirement(reference_price, worst(Decimal("0")))
            self._pending_gross += gross_delta
            self._pending_margin += margin_delta
            self._approve(order, gross_delta, margin_delta)
            return True

        order_notional = abs(instrument.notional(reference_price, order.quantity))
        order_cap = self._cap(
            self.limits.max_order_notional, self.limits.max_order_notional_pct, equity
        )
        if order_cap is not None and order_notional > order_cap:
            self._reject(
                timestamp, order.symbol, "order_notional", f"{order_notional} > {order_cap}"
            )
            return False

        # R2: projected from the position this batch is BUILDING, not the one
        # it started from, and from the worst subset of unfilled orders.
        #
        # Bound once here rather than inline, because the margin check below is
        # on this same path and needs the identical quantity. It read a
        # `projected_qty` that the merge left defined only inside the
        # exposure-reducing branch above — which returns — so every derivative
        # order that reached the margin check raised `UnboundLocalError`
        # (20 tests, 2026-09-07 merge). Two callers, one binding.
        projected_qty = worst(delta)
        projected_notional = abs(instrument.notional(reference_price, projected_qty))
        position_cap = self._cap(
            self.limits.max_position_notional, self.limits.max_position_notional_pct, equity
        )
        if position_cap is not None and projected_notional > position_cap:
            self._reject(
                timestamp,
                order.symbol,
                "position_notional",
                f"{projected_notional} > {position_cap}",
            )
            return False

        held_notional = abs(instrument.notional(reference_price, worst(Decimal("0"))))
        gross_delta = projected_notional - held_notional
        margin_delta = Decimal("0")
        if self.limits.max_gross_leverage is not None:
            if equity <= 0:
                self._reject(timestamp, order.symbol, "insolvent", f"equity {equity} <= 0")
                return False
            # The same worst case on both sides of the subtraction, matching
            # how the engine builds `gross_exposure`. Adding working orders as
            # a sum of absolutes while subtracting them as an absolute of a sum
            # counted an offsetting order twice and refused a true 0.01x as
            # 0.03x.
            held_notional = abs(instrument.notional(reference_price, worst(Decimal("0"))))
            projected_gross = (
                gross_exposure + self._pending_gross - held_notional + projected_notional
            )
            leverage = projected_gross / equity
            if leverage > self.limits.max_gross_leverage:
                self._reject(
                    timestamp,
                    order.symbol,
                    "gross_leverage",
                    f"{leverage:.2f}x > {self.limits.max_gross_leverage}x",
                )
                return False
            self._pending_gross += gross_delta

        # Collateral, not exposure. Runs after the leverage check because on
        # the shipped defaults leverage is the tighter bound and rejecting
        # there gives the more useful reason; this binds on genuinely
        # leveraged instruments, where notional says little about whether the
        # account can actually fund the book.
        if self.limits.max_margin_utilisation is not None and instrument.is_derivative:
            if equity <= 0:
                self._reject(timestamp, order.symbol, "insolvent", f"equity {equity} <= 0")
                return False
            held_margin = instrument.margin_requirement(reference_price, worst(Decimal("0")))
            projected_margin = instrument.margin_requirement(reference_price, projected_qty)
            total_margin = margin_reserved + self._pending_margin - held_margin + projected_margin
            utilisation = total_margin / equity
            if utilisation > self.limits.max_margin_utilisation:
                self._reject(
                    timestamp,
                    order.symbol,
                    "margin",
                    f"{utilisation:.2f} of equity committed as margin > "
                    f"{self.limits.max_margin_utilisation}",
                )
                return False
            margin_delta = projected_margin - held_margin
            self._pending_margin += margin_delta

        self._approve(order, gross_delta, margin_delta)
        return True

    def _approve(self, order: Order, gross_delta: Decimal, margin_delta: Decimal) -> None:
        signed = order.quantity * order.side.sign
        self._pending_delta[order.symbol] = (
            self._pending_delta.get(order.symbol, Decimal("0")) + signed
        )
        self._approved.append(
            _Approval(
                order_id=order.order_id,
                symbol=order.symbol,
                quantity=order.quantity,
                signed_delta=signed,
                gross_delta=gross_delta,
                margin_delta=margin_delta,
            )
        )

    @staticmethod
    def _cap(absolute: Decimal | None, fraction: Decimal | None, equity: Decimal) -> Decimal | None:
        """The tighter of an absolute ceiling and a fraction of current equity.

        Uses *current marked* equity, never peak: sizing off a stale high-water
        mark would let a drawn-down account keep trading at its old size, which
        is a genuine loosening rather than a change of units.
        """
        relative = None if fraction is None else max(Decimal("0"), fraction * equity)
        candidates = [c for c in (absolute, relative) if c is not None]
        return min(candidates) if candidates else None

    @staticmethod
    def _reduces_exposure_between(current: Decimal, projected: Decimal) -> bool:
        """True when the order moves the worst-case position closer to flat.

        Taken between two already-computed worst cases rather than from the
        order, so the exemption is granted on the same basis the caps are
        applied on. Granting it on a netted position is how a halted account
        could open fresh exposure: a working order that offsets the book makes
        an opening order look reducing.

        **A sign change is never a reduction (9b).** Long 100 → short 30 passes
        `|projected| < |current|` and is an overshoot past flat: it closes 100
        and opens 30 on the far side, and the 30 is new risk that took no size
        cap and — while halted — no halt. Measured on the original: a halted
        account accepted 13,000 of notional past a 1,500 cap in one order with
        nothing logged.

        This guard came from the other branch at the 2026-09-07 merge, where
        this function did not have it and three tests went red. The two
        branches each had half: the worst-case framing above is this side's,
        the sign check is the other's, and both are needed. A strategy that
        wants to flip sends a flatten and an entry — `target_notional` does
        that for it — so the entry is checked on its own.
        """
        if current == 0:
            return False
        if projected != 0 and (projected > 0) != (current > 0):
            return False  # 9b: a flip through zero opens the other side
        return abs(projected) < abs(current)

    def record_rejection(
        self, *, timestamp: datetime, symbol: str, reason: str, detail: str
    ) -> None:
        """Log a refusal that happened outside ``check`` — the engine's own.

        Public because the backtester needs it: an order it cannot risk-check
        at all still has to leave a trace, or intent disappears silently.
        """
        self._reject(timestamp, symbol, reason, detail)

    def _reject(self, timestamp: datetime, symbol: str, reason: str, detail: str) -> None:
        self.rejections.append(
            RiskRejection(timestamp=timestamp, symbol=symbol, reason=reason, detail=detail)
        )

    @property
    def rejection_summary(self) -> dict[str, int]:
        summary: dict[str, int] = {}
        for r in self.rejections:
            summary[r.reason] = summary.get(r.reason, 0) + 1
        return summary
