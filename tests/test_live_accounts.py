"""Paper account cycles (charter §28) and the §29 invariants they add.

Mutations these catch: `mark` not closing at equity <= 0 (first test); the
store opened with "w" (history shrinks to one line); the next cycle numbered
from the file's line count instead of the closed count; `in_currency`
converting both ends at one rate; a default rate of 1 appearing anywhere.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.core.events import Fill, Order
from trading_platform.core.fx import QuoteRates, RateObservation, UnknownQuoteRate
from trading_platform.core.instruments import crypto_perpetual, crypto_spot
from trading_platform.core.types import Side
from trading_platform.live.accounts import (
    AccountCycles,
    CycleError,
    CycleOutcome,
    CycleRecord,
    cycles_path,
    read_cycles,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)
BTC = crypto_spot("BTCUSDT")
PERP = crypto_perpetual("BTCUSDT-PERP", initial_margin_rate=Decimal("0.1"))


def fill(side: Side, qty: str, price: str, at: datetime, symbol: str = "BTCUSDT") -> Fill:
    return Fill(
        order_id="o",
        symbol=symbol,
        timestamp=at,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        commission=Decimal("1"),
    )


def account(tmp_path: Path) -> AccountCycles:
    return AccountCycles(root=tmp_path, account="a1", starting_cash=Decimal("1000"), now=T0)


def wipe_out(acct: AccountCycles, at: datetime) -> CycleRecord | None:
    """Short 10 perps at 100 with 10x margin, then mark at 250: -1,500 on 1,000."""
    acct.observe_price(PERP, Decimal("100"), at)
    acct.apply_fill(PERP, fill(Side.SELL, "10", "100", at, PERP.symbol))
    acct.observe_price(PERP, Decimal("250"), at + timedelta(minutes=1))
    out = acct.mark(at + timedelta(minutes=1))
    return out if isinstance(out, CycleRecord) else None


# -- §28: cycle on zero, preserve history, increment, never delete -------------


def test_equity_reaching_zero_closes_the_cycle_and_opens_the_next(tmp_path: Path) -> None:
    acct = account(tmp_path)
    assert acct.number == 1
    closed = wipe_out(acct, T0)
    assert closed is not None
    assert closed.outcome is CycleOutcome.FAILED
    assert closed.number == 1
    assert closed.ending_equity <= 0
    assert closed.fills == 1
    assert closed.fees == Decimal("1")
    # 1,000 -> -501: the drawdown recorded on the closed cycle is the whole way down.
    assert closed.max_drawdown_pct == Decimal("1.501")
    # The next cycle: fresh books, same terms, nothing carried over.
    assert acct.number == 2
    assert acct.equity == Decimal("1000.00")
    assert acct.portfolio.positions == {}
    assert acct.snapshot(T0).drawdown_pct == 0


def test_a_cycle_above_zero_is_not_closed_by_a_mark(tmp_path: Path) -> None:
    acct = account(tmp_path)
    acct.observe_price(BTC, Decimal("100"), T0)
    acct.apply_fill(BTC, fill(Side.BUY, "5", "100", T0))
    acct.observe_price(BTC, Decimal("50"), T0 + timedelta(hours=1))
    out = acct.mark(T0 + timedelta(hours=1))
    assert not isinstance(out, CycleRecord)
    assert acct.number == 1
    assert out.drawdown_pct == Decimal("0.251")  # 1000 -> 749 (5*50 + 500 - 1 fee)


def test_history_survives_a_restart_and_the_numbering_continues(tmp_path: Path) -> None:
    acct = account(tmp_path)
    wipe_out(acct, T0)
    wipe_out(acct, T0 + timedelta(days=1))
    assert acct.number == 3

    again = account(tmp_path)
    assert again.number == 3
    assert [r.number for r in again.history] == [1, 2]
    assert all(r.outcome is CycleOutcome.FAILED for r in again.history)
    # Both closes and every open are in the file: history is not compacted.
    lines = read_cycles(cycles_path(tmp_path, "a1"))
    assert [(r.number, str(r.outcome)) for r in lines] == [
        (1, "OPEN"),
        (1, "FAILED"),
        (2, "OPEN"),
        (2, "FAILED"),
        (3, "OPEN"),
    ]


def test_a_closed_cycle_cannot_be_edited(tmp_path: Path) -> None:
    acct = account(tmp_path)
    closed = wipe_out(acct, T0)
    assert closed is not None
    with pytest.raises(FrozenInstanceError):
        closed.ending_equity = Decimal("1340")  # type: ignore[misc]
    # Nothing on the store can rewrite it either: there is no such method.
    assert not any(name.startswith(("delete", "reset", "rewrite")) for name in dir(acct))


def test_the_store_refuses_changed_terms(tmp_path: Path) -> None:
    account(tmp_path)
    with pytest.raises(CycleError, match="do not change terms"):
        AccountCycles(root=tmp_path, account="a1", starting_cash=Decimal("2000"), now=T0)


def test_starting_cash_has_no_default_and_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        AccountCycles(root=tmp_path, account="a1")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="positive"):
        AccountCycles(root=tmp_path, account="a1", starting_cash=Decimal("0"))


def test_a_deliberate_close_needs_a_reason_and_keeps_equity(tmp_path: Path) -> None:
    acct = account(tmp_path)
    acct.observe_price(BTC, Decimal("100"), T0)
    acct.apply_fill(BTC, fill(Side.BUY, "2", "100", T0))
    acct.observe_price(BTC, Decimal("270"), T0 + timedelta(days=30))
    acct.mark(T0 + timedelta(days=30))
    with pytest.raises(CycleError, match="reason"):
        acct.close(T0 + timedelta(days=30), "")
    closed = acct.close(T0 + timedelta(days=30), "strategy retired")
    assert closed.outcome is CycleOutcome.CLOSED
    assert closed.ending_equity == Decimal("1339.00")  # 800 cash + 540 - 1 fee
    assert closed.return_pct == Decimal("0.339")
    assert acct.number == 2


# -- §29 on the open cycle -----------------------------------------------------


def test_snapshot_carries_every_tracked_field_and_reconciles(tmp_path: Path) -> None:
    acct = account(tmp_path)
    acct.record_order(Order(order_id="o", symbol="BTCUSDT", side=Side.BUY, quantity=Decimal("3")))
    acct.record_order(Order(order_id="o", symbol="BTCUSDT", side=Side.BUY, quantity=Decimal("3")))
    acct.observe_price(BTC, Decimal("100"), T0)
    acct.apply_fill(BTC, fill(Side.BUY, "3", "100", T0))
    acct.observe_price(BTC, Decimal("110"), T0 + timedelta(hours=1))
    snap = acct.mark(T0 + timedelta(hours=1))
    assert not isinstance(snap, CycleRecord)
    assert snap.cycle == 1
    assert snap.orders == 2 and snap.fills == 1  # the risk layer's gap is visible
    assert snap.positions == {"BTCUSDT": Decimal("3")}
    assert snap.fees == Decimal("1")
    assert snap.cash == Decimal("699.00")
    assert snap.unrealised_pnl == Decimal("30.00")
    assert snap.realised_pnl == Decimal("0")
    assert snap.exposure == Decimal("330.00")
    assert snap.equity == snap.cash + Decimal("330.00")  # cash + market value
    assert snap.drawdown_pct == Decimal("0")


def test_the_new_cycle_does_not_inherit_the_old_peak(tmp_path: Path) -> None:
    """Drawdown is per cycle: a fresh 1,000 after a failure is not 'down 100%'."""
    acct = account(tmp_path)
    acct.observe_price(BTC, Decimal("100"), T0)
    acct.apply_fill(BTC, fill(Side.BUY, "5", "100", T0))
    acct.observe_price(BTC, Decimal("200"), T0 + timedelta(hours=1))
    acct.mark(T0 + timedelta(hours=1))  # peak 1,499
    acct.close(T0 + timedelta(hours=2), "reset")
    snap = acct.mark(T0 + timedelta(hours=3))
    assert not isinstance(snap, CycleRecord)
    assert snap.drawdown_pct == Decimal("0")


# -- GBP display through core.fx ------------------------------------------------


def test_display_in_gbp_refuses_without_a_rate_series(tmp_path: Path) -> None:
    acct = account(tmp_path)
    closed = wipe_out(acct, T0)
    assert closed is not None
    with pytest.raises(UnknownQuoteRate):
        closed.in_currency(QuoteRates(reporting_currency="GBP"))


def test_display_in_gbp_uses_the_rate_at_each_end(tmp_path: Path) -> None:
    """Opening cash at the opening rate, ending equity at the closing rate.
    Converting both at one rate would report 0.75 * 1339 = 1004.25 here."""
    acct = account(tmp_path)
    acct.observe_price(BTC, Decimal("100"), T0)
    acct.apply_fill(BTC, fill(Side.BUY, "2", "100", T0))
    end = T0 + timedelta(days=30)
    acct.observe_price(BTC, Decimal("270"), end)
    acct.mark(end)
    closed = acct.close(end, "month end")

    gbp = QuoteRates(reporting_currency="GBP")
    gbp.add(
        "USD",
        [
            RateObservation(timestamp=T0, rate=Decimal("0.75")),
            RateObservation(timestamp=end, rate=Decimal("0.80")),
        ],
    )
    shown = closed.in_currency(gbp)
    assert shown.currency == "GBP"
    assert shown.starting_cash == Decimal("750.00")
    assert shown.ending_equity == Decimal("1071.20")  # 1339 * 0.80
    assert shown.max_drawdown_pct == closed.max_drawdown_pct
    assert "GBP" in acct.report(gbp)
    # The ledger itself is untouched.
    assert closed.currency == "USD"
    assert closed.ending_equity == Decimal("1339.00")


def test_report_reads_like_the_charter_example(tmp_path: Path) -> None:
    acct = account(tmp_path)
    wipe_out(acct, T0)
    text = acct.report()
    assert text.splitlines()[0].startswith("Account 1  1000 USD -> ")
    assert text.splitlines()[0].endswith("FAILED")
    assert text.splitlines()[1].endswith("OPEN")


def test_equity_at_exactly_zero_closes_the_cycle(tmp_path: Path) -> None:
    """The boundary. `equity <= 0` is the stated policy; every other test
    reaches well below zero, so `< 0` passed them all (guard-auditor,
    2026-09-03). Cash 999 after the fee, short 10 perps from 100: at 199.90
    the unrealised loss is exactly 999.00."""
    acct = account(tmp_path)
    acct.observe_price(PERP, Decimal("100"), T0)
    acct.apply_fill(PERP, fill(Side.SELL, "10", "100", T0, PERP.symbol))
    acct.observe_price(PERP, Decimal("199.90"), T0 + timedelta(minutes=1))
    assert acct.equity == Decimal("0.00")
    out = acct.mark(T0 + timedelta(minutes=1))
    assert isinstance(out, CycleRecord)
    assert out.outcome is CycleOutcome.FAILED and out.ending_equity == Decimal("0.00")
    assert acct.number == 2
