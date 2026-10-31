"""Account marks, refusals and leverage acknowledgements in Postgres.

Needs a real database, like `tests/test_store.py`; the guard in
`tests/test_store_guard.py` polices this module's skip too.

Two properties carry the weight here:

**A ledger that does not balance cannot be stored.** `equity = cash +
positions_value` is a database CHECK, not only a Python assertion, so the
guarantee holds against anything that writes to the table — including a future
migration script, a psql session, or a bug in this module.

**An acknowledgement is revoked, never deleted.** "Who could take 10x on this
account in August" has to stay answerable in December.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trading_platform.core.types import Stage
from trading_platform.live.supervisor import PaperSupervisor, SupervisorConfig
from trading_platform.live.tracker import AccountMark
from trading_platform.risk.leverage import LeverageAcknowledgement, LeverageNotAcknowledged
from trading_platform.risk.limits import RiskRejection
from trading_platform.store.accounts import (
    acknowledgement_history,
    acknowledgements,
    latest_mark,
    leverage_gate,
    marks_disagree,
    rejection_pressure,
    revoke_acknowledgement,
    write_acknowledgement,
    write_mark,
    write_rejections,
)
from trading_platform.store.migrate import migrate

TEST_DSN_VAR = "TRADING_PLATFORM_TEST_DSN"
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

psycopg = pytest.importorskip("psycopg", reason="the store needs psycopg")


def _dsn() -> str | None:
    return os.environ.get(TEST_DSN_VAR) or None


def _reachable(dsn: str) -> bool:
    try:
        psycopg.connect(dsn).close()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    _dsn() is None or not _reachable(_dsn() or ""),
    reason=f'set {TEST_DSN_VAR} — scripts/dev_postgres.sh start && eval "$(scripts/dev_postgres.sh env)"',
)


@pytest.fixture
def conn(request: pytest.FixtureRequest) -> Iterator[Any]:
    dsn = _dsn()
    assert dsn is not None
    schema = "a_" + str(abs(hash(request.node.nodeid)) % 10**12)
    connection = psycopg.connect(dsn)
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{schema}"')
        cursor.execute(f'SET search_path TO "{schema}"')
    connection.commit()
    migrate(connection)
    try:
        yield connection
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA "{schema}" CASCADE')
        connection.commit()
        connection.close()


def mark(minute: int = 0, **over: Any) -> AccountMark:
    values: dict[str, Any] = {
        "account": "a1",
        "cycle": 1,
        "stage": Stage.PAPER,
        "at": T0 + timedelta(minutes=minute),
        "cash": Decimal("900.00"),
        "positions_value": Decimal("120.00"),
        "equity": Decimal("1020.00"),
        "realised_pnl": Decimal("15.00"),
        "unrealised_pnl": Decimal("5.00"),
        "fees": Decimal("1.20"),
        "funding": Decimal("-0.30"),
        "exposure": Decimal("1200.00"),
        "margin_used": Decimal("120.00"),
        "free_cash": Decimal("780.00"),
        "gross_leverage": Decimal("1.176"),
        "peak_equity": Decimal("1050.00"),
        "drawdown_pct": Decimal("0.0285714"),
        "orders": 4,
        "fills": 3,
        "positions": {"BTCUSDT": Decimal("0.5")},
    }
    values.update(over)
    return AccountMark(**values)


def ack(**over: Any) -> LeverageAcknowledgement:
    values: dict[str, Any] = {
        "account": "a1",
        "symbol": "BTCUSDT",
        "max_leverage": Decimal("10"),
        "reference_price": Decimal("100.00"),
        "reference_equity": Decimal("1000.00"),
        "stated_liquidation_price": Decimal("90.00"),
        "stated_loss_at_20pct": Decimal("2000.00"),
        "acknowledged_by": "Joel",
        "acknowledged_at": T0,
    }
    values.update(over)
    return LeverageAcknowledgement(**values)


# -- marks --------------------------------------------------------------------------------


def test_a_mark_round_trips_and_the_latest_one_is_the_latest(conn: Any) -> None:
    assert write_mark(conn, mark(0))
    assert write_mark(conn, mark(5, equity=Decimal("1030.00"), cash=Decimal("910.00")))
    latest = latest_mark(conn, "a1")
    assert latest is not None
    assert latest["at"].astimezone(UTC) == T0 + timedelta(minutes=5)
    assert latest["equity"] == Decimal("1030.00")
    assert latest["positions"] == {"BTCUSDT": "0.5"}
    assert latest_mark(conn, "never-marked") is None


def test_a_ledger_that_does_not_balance_is_refused_by_python_and_by_the_database(
    conn: Any,
) -> None:
    """§29: equity = cash + marked positions. A CHECK, not only an assertion, so
    the guarantee survives anything that writes to the table.

    Mutation: delete the identity check from `AccountMark.__post_init__` — the
    first half goes red; delete `marks_equity_balances` from the migration and
    the second half does."""
    with pytest.raises(ValueError, match="is not"):
        mark(equity=Decimal("9999.00"))

    with conn.cursor() as cursor, pytest.raises(psycopg.errors.CheckViolation):
        cursor.execute(
            "INSERT INTO account_marks (account, cycle, stage, at, cash, positions_value, "
            "equity, realised_pnl, unrealised_pnl, fees, funding, exposure, margin_used, "
            "free_cash, gross_leverage, peak_equity, drawdown_pct, orders, fills, positions) "
            "VALUES ('a1', 1, 'paper', %s, 900, 120, 9999, 0, 0, 0, 0, 0, 0, 900, 0, 1000, "
            "0, 0, 0, '{}')",
            (T0,),
        )
    conn.rollback()


def test_the_same_instant_twice_is_skipped_rather_than_doubling_every_aggregate(
    conn: Any,
) -> None:
    """A restart replaying the last minute re-marks instants it already wrote.
    A second row would double the count in every aggregate over this table.

    Mutation: drop `ON CONFLICT ON CONSTRAINT marks_once DO NOTHING` — the
    second write raises instead of returning False, and a normal restart
    becomes a crash."""
    assert write_mark(conn, mark(0)) is True
    assert write_mark(conn, mark(0)) is False
    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM account_marks")
        assert cursor.fetchone()[0] == 1


def test_a_disagreeing_re_mark_cannot_be_written_through_the_api(conn: Any) -> None:
    """The second write of one instant with a DIFFERENT equity is skipped, not
    kept. `marks_disagree` exists to find rows somebody wrote around the API,
    and must be empty here."""
    write_mark(conn, mark(0))
    assert write_mark(conn, mark(0, equity=Decimal("2020.00"), cash=Decimal("1900.00"))) is False
    assert marks_disagree(conn, "a1") == []
    latest = latest_mark(conn, "a1")
    assert latest is not None and latest["equity"] == Decimal("1020.00")


def test_negative_free_cash_is_stored_and_flagged_rather_than_clamped(conn: Any) -> None:
    """The book requiring more collateral than the account holds is precisely
    the state a reader needs to see.

    Mutation: add a `free_cash >= 0` CHECK to the migration — the insert below
    fails, and the platform loses its only signal for undercollateralisation."""
    undercollateralised = mark(margin_used=Decimal("1500.00"), free_cash=Decimal("-600.00"))
    assert undercollateralised.is_undercollateralised
    assert write_mark(conn, undercollateralised)
    latest = latest_mark(conn, "a1")
    assert latest is not None and latest["free_cash"] == Decimal("-600.00")


def test_an_impossible_mark_is_refused_at_construction(conn: Any) -> None:
    with pytest.raises(ValueError, match="cycle numbers start at 1"):
        mark(cycle=0)
    with pytest.raises(ValueError, match="timezone-aware"):
        mark(at=datetime(2026, 9, 1))  # naive on purpose
    with pytest.raises(ValueError, match="margin_used"):
        mark(margin_used=Decimal("-1"))
    with pytest.raises(ValueError, match="drawdown"):
        mark(drawdown_pct=Decimal("-0.5"))


def test_the_stage_is_recorded_and_an_unknown_one_is_refused(conn: Any) -> None:
    """A row that cannot say which stage produced it will eventually be read as
    the wrong one. Mutation: drop `marks_stage_known` — 'production' stores."""
    write_mark(conn, mark(stage=Stage.SIMULATION))
    latest = latest_mark(conn, "a1")
    assert latest is not None and latest["stage"] == "simulation"

    with conn.cursor() as cursor, pytest.raises(psycopg.errors.CheckViolation):
        cursor.execute(
            "INSERT INTO account_marks (account, cycle, stage, at, cash, positions_value, "
            "equity, realised_pnl, unrealised_pnl, fees, funding, exposure, margin_used, "
            "free_cash, gross_leverage, peak_equity, drawdown_pct, orders, fills, positions) "
            "VALUES ('a1', 1, 'production', %s, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "
            "'{}')",
            (T0 + timedelta(hours=1),),
        )
    conn.rollback()


# -- rejections ---------------------------------------------------------------------------


def test_every_refusal_is_stored_and_counted_by_reason(conn: Any) -> None:
    """ "The strategy wanted to breach a limit 400 times today" becomes a query.

    Mutation: deduplicate the inserts — the two `max_notional` rows below
    collapse to one and the count goes red. They happened twice."""
    rejections = [
        RiskRejection(timestamp=T0, symbol="BTCUSDT", reason="max_notional", detail="1500 > 1000"),
        RiskRejection(timestamp=T0, symbol="BTCUSDT", reason="max_notional", detail="1500 > 1000"),
        RiskRejection(timestamp=T0, symbol="ETHUSDT", reason="halted", detail="drawdown"),
    ]
    assert write_rejections(conn, rejections, account="a1", cycle=1, stage=Stage.PAPER) == 3
    assert write_rejections(conn, [], account="a1", cycle=1, stage=Stage.PAPER) == 0

    pressure = {row["reason"]: row for row in rejection_pressure(conn, "a1")}
    assert pressure["max_notional"]["rejections"] == 2
    assert pressure["max_notional"]["symbols"] == 1
    assert pressure["halted"]["rejections"] == 1


def test_a_rejection_with_no_reason_is_refused_by_the_database(conn: Any) -> None:
    """A refusal whose reason is blank tells you an order was stopped and
    nothing else, which is worse than not recording it."""
    with conn.cursor() as cursor, pytest.raises(psycopg.errors.CheckViolation):
        cursor.execute(
            "INSERT INTO risk_rejections (account, cycle, stage, ts, symbol, reason, detail) "
            "VALUES ('a1', 1, 'paper', %s, 'BTCUSDT', '  ', 'x')",
            (T0,),
        )
    conn.rollback()


# -- acknowledgements ---------------------------------------------------------------------


def test_an_acknowledgement_round_trips_and_builds_a_gate_that_admits_its_symbol(
    conn: Any,
) -> None:
    from trading_platform.core.instruments import AssetClass, Perpetual

    write_acknowledgement(conn, ack())
    stored = acknowledgements(conn, "a1")
    assert [a.symbol for a in stored] == ["BTCUSDT"]
    assert stored[0].max_leverage == Decimal("10")
    assert stored[0].acknowledged_by == "Joel"

    gate = leverage_gate(conn, "a1")
    gate.admit(
        Perpetual(
            symbol="BTCUSDT",
            asset_class=AssetClass.PERPETUAL,
            initial_margin_rate=Decimal("0.1"),
        )
    )
    with pytest.raises(LeverageNotAcknowledged):
        gate.admit(
            Perpetual(
                symbol="ETHUSDT",
                asset_class=AssetClass.PERPETUAL,
                initial_margin_rate=Decimal("0.1"),
            )
        )


def test_an_account_with_no_rows_gets_a_gate_that_refuses_all_leverage(conn: Any) -> None:
    """The absence of a record is the absence of an acknowledgement, not
    permission. Mutation: return an all-permitting gate when there are no rows —
    every account silently gains leverage and this goes red."""
    from trading_platform.core.instruments import AssetClass, Perpetual

    gate = leverage_gate(conn, "never-acknowledged")
    with pytest.raises(LeverageNotAcknowledged, match="no leverage acknowledgement"):
        gate.admit(
            Perpetual(
                symbol="BTCUSDT",
                asset_class=AssetClass.PERPETUAL,
                initial_margin_rate=Decimal("0.1"),
            )
        )


def test_a_second_live_acknowledgement_is_refused_rather_than_stacked(conn: Any) -> None:
    """Two live rows would leave which one binds to chance.

    Mutation: drop the existence check in `write_acknowledgement` — the partial
    unique index still catches it, but with a constraint error instead of a
    message naming the existing row. Drop the index too and both are gone."""
    write_acknowledgement(conn, ack())
    with pytest.raises(ValueError, match="already has a live acknowledgement"):
        write_acknowledgement(
            conn,
            ack(
                max_leverage=Decimal("5"),
                stated_liquidation_price=Decimal("80.00"),
                stated_loss_at_20pct=Decimal("1000.00"),
            ),
        )


def test_revoking_keeps_the_row_and_frees_the_symbol_for_a_new_one(conn: Any) -> None:
    """Revoked, never deleted: "who could take 10x in August" stays answerable
    in December. Mutation: DELETE instead of UPDATE — the history below is
    empty and this goes red."""
    write_acknowledgement(conn, ack())
    revoke_acknowledgement(conn, "a1", "BTCUSDT", reason="reducing risk before the halving")
    assert acknowledgements(conn, "a1") == []

    history = acknowledgement_history(conn, "a1")
    assert len(history) == 1
    assert history[0]["revoked_at"] is not None
    assert "halving" in history[0]["revoked_reason"]

    # The symbol is free again, and the new row does not erase the old one.
    write_acknowledgement(
        conn,
        ack(
            max_leverage=Decimal("2"),
            stated_liquidation_price=Decimal("50.00"),
            stated_loss_at_20pct=Decimal("400.00"),
        ),
    )
    assert [a.max_leverage for a in acknowledgements(conn, "a1")] == [Decimal("2")]
    assert len(acknowledgement_history(conn, "a1")) == 2


def test_revoking_needs_a_reason_and_something_to_revoke(conn: Any) -> None:
    with pytest.raises(ValueError, match="needs a reason"):
        revoke_acknowledgement(conn, "a1", "BTCUSDT", reason="  ")
    with pytest.raises(ValueError, match="no live acknowledgement"):
        revoke_acknowledgement(conn, "a1", "BTCUSDT", reason="there is nothing there")


def test_a_hand_edited_row_whose_figures_no_longer_add_up_raises_on_the_way_out(
    conn: Any,
) -> None:
    """Storage is not trusted more than input. Someone editing the liquidation
    price in psql to make a position look safer is exactly the case worth
    catching, and it is caught when the row is read.

    Mutation: build the acknowledgement with `object.__new__` or otherwise skip
    `__post_init__` in `acknowledgements()` — the tampered row loads silently."""
    write_acknowledgement(conn, ack())
    with conn.cursor() as cursor:
        cursor.execute(
            "UPDATE leverage_acknowledgements SET stated_liquidation_price = 10 "
            "WHERE account = 'a1'"
        )
    conn.commit()
    with pytest.raises(ValueError, match="stated liquidation price"):
        acknowledgements(conn, "a1")


# -- end to end: the phase-1 acceptance criterion ------------------------------------------


def test_a_paper_session_writes_its_full_state_to_the_database_on_every_mark(
    conn: Any, tmp_path: Any
) -> None:
    """The first half of workstream 3 phase 1: an account's state stops living
    only in memory and a JSONL file.

    Runs a real `PaperSupervisor` over three bars with a `StoreTracker` attached
    and checks that every instant is a row, that the row agrees with the run's
    own final equity, and that the ledger balanced at every one of them — the
    last being enforced by the database, so it cannot be true by accident.

    Mutation: remove the `self._track_mark(at)` call from
    `PaperSupervisor._gate` — no rows are written and this goes red at the first
    assertion."""
    from tests.test_live_supervisor import BuyOnce, bars
    from trading_platform.core.instruments import crypto_spot
    from trading_platform.data.feed import InMemoryBarFeed
    from trading_platform.engine.execution import ZERO_COST
    from trading_platform.risk.limits import RiskLimits
    from trading_platform.store.accounts import StoreTracker

    tracker = StoreTracker(connection=conn, account="a1", cycle=1, stage=Stage.PAPER)
    config = SupervisorConfig(
        root=tmp_path,
        account="a1",
        starting_cash=Decimal("10000"),
        instruments={"BTCUSDT": crypto_spot("BTCUSDT")},
        cost_model=ZERO_COST,
        limits=RiskLimits(),
        kill_switch=tmp_path / "KILL",
        tracker=tracker,
    )
    supervisor = PaperSupervisor(
        config, BuyOnce(), now=datetime(2026, 9, 1, tzinfo=UTC) - timedelta(hours=1)
    )
    result = supervisor.run(InMemoryBarFeed(bars(["100", "100", "110"])))

    assert result.stopped_by == "feed ended"
    assert tracker.healthy, tracker.report()
    assert tracker.marks == 3, tracker.report()

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT at, cash, positions_value, equity, exposure, positions "
            "FROM account_marks WHERE account = 'a1' ORDER BY at"
        )
        rows = cursor.fetchall()
    assert len(rows) == 3
    # The ledger balanced at every mark. The CHECK made that unavoidable, which
    # is the point of it being a CHECK; asserting it here says so out loud.
    for _at, cash, positions_value, equity, _exposure, _positions in rows:
        assert equity == cash + positions_value

    # Bought 5 at 100 on bar 2 (one-bar fill delay), marked at 110 on bar 3.
    last_at, last_cash, last_positions_value, last_equity, last_exposure, positions = rows[-1]
    assert positions == {"BTCUSDT": "5"}
    assert last_cash == Decimal("9500.00")
    assert last_positions_value == Decimal("550.00")
    assert last_equity == Decimal("10050.00")
    assert last_exposure == Decimal("550.00")

    latest = latest_mark(conn, "a1")
    assert latest is not None and latest["at"] == last_at
    assert latest["equity"] == last_equity


def test_a_paper_session_refuses_a_leveraged_order_through_the_real_order_path(
    conn: Any, tmp_path: Any
) -> None:
    """The second half of workstream 3 phase 1, end to end, through a real
    session — replacing an earlier test that defined a helper and never called
    it, and whose helper could not have run (adversarial review, 2026-09-04).

    Mutation: build the `RiskManager` in `PaperSupervisor.reconcile` without
    `leverage_gate=cfg.leverage_gate` — the order fills and this goes red. That
    mutation survived the previous version of this file, which is how a test
    that is never called looks from the outside."""
    from tests.test_live_supervisor import BuyOnce, bars
    from trading_platform.core.instruments import crypto_perpetual
    from trading_platform.data.feed import InMemoryBarFeed
    from trading_platform.engine.execution import ZERO_COST
    from trading_platform.risk.limits import RiskLimits
    from trading_platform.store.accounts import StoreTracker

    perp = crypto_perpetual("BTCUSDT-PERP", initial_margin_rate=Decimal("0.1"))

    def session(account: str) -> Any:
        root = tmp_path / account
        root.mkdir(parents=True, exist_ok=True)
        tracker = StoreTracker(connection=conn, account=account, cycle=1, stage=Stage.PAPER)
        config = SupervisorConfig(
            root=root,
            account=account,
            starting_cash=Decimal("10000"),
            instruments={"BTCUSDT-PERP": perp},
            cost_model=ZERO_COST,
            limits=RiskLimits(),
            kill_switch=root / "KILL",
            tracker=tracker,
            leverage_gate=leverage_gate(conn, account),
        )
        supervisor = PaperSupervisor(
            config,
            BuyOnce(symbol="BTCUSDT-PERP"),
            now=datetime(2026, 9, 1, tzinfo=UTC) - timedelta(hours=1),
        )
        return supervisor.run(
            InMemoryBarFeed(bars(["100", "100", "110"], symbol="BTCUSDT-PERP"))
        ), tracker

    # No acknowledgement row: nothing fills, and the refusal is in the database.
    refused, refused_tracker = session("no-ack")
    assert not refused.fills, "a 10x perpetual filled on an unacknowledged account"
    assert refused_tracker.healthy, refused_tracker.report()
    pressure = {r["reason"]: r["rejections"] for r in rejection_pressure(conn, "no-ack")}
    assert pressure.get("leverage_unacknowledged", 0) >= 1, pressure

    # With the row, the same session trades.
    write_acknowledgement(conn, ack(account="acked", symbol="BTCUSDT-PERP"))
    permitted, _ = session("acked")
    assert len(permitted.fills) == 1


def test_the_tracker_records_every_refusal_not_only_the_marks(conn: Any, tmp_path: Any) -> None:
    """ "Every risk rejection stored with its reason" was in the commit message
    and nothing pinned it: the mutation that stops the supervisor forwarding
    rejections survived the whole suite.

    Mutation: delete the `tracker.record_rejections(...)` call from
    `PaperSupervisor._on_reject` — red."""
    from tests.test_live_supervisor import BuyEveryBar, bars
    from trading_platform.core.instruments import crypto_spot
    from trading_platform.data.feed import InMemoryBarFeed
    from trading_platform.engine.execution import ZERO_COST
    from trading_platform.risk.limits import RiskLimits
    from trading_platform.store.accounts import StoreTracker

    tracker = StoreTracker(connection=conn, account="chatty", cycle=1, stage=Stage.PAPER)
    config = SupervisorConfig(
        root=tmp_path,
        account="chatty",
        starting_cash=Decimal("10000"),
        instruments={"BTCUSDT": crypto_spot("BTCUSDT")},
        cost_model=ZERO_COST,
        # One order per bar allowed; BuyEveryBar sends one per bar and the
        # notional cap refuses once the book is full.
        limits=RiskLimits(max_position_notional=Decimal("150")),
        kill_switch=tmp_path / "KILL",
        tracker=tracker,
    )
    supervisor = PaperSupervisor(
        config, BuyEveryBar(), now=datetime(2026, 9, 1, tzinfo=UTC) - timedelta(hours=1)
    )
    result = supervisor.run(InMemoryBarFeed(bars(["100"] * 8)))
    assert result.result.rejections, "this configuration was supposed to provoke refusals"
    assert tracker.rejections >= 1, tracker.report()

    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*), min(stage) FROM risk_rejections WHERE account = 'chatty'")
        count, stage = cursor.fetchone()
    assert count == len(result.result.rejections)
    assert stage == "paper", "the stage of a supervisor-written row is not recorded"


def test_a_mark_carries_the_stage_the_session_declared(conn: Any, tmp_path: Any) -> None:
    """Two mutations survived here: defaulting the stage to RESEARCH, and
    hard-coding PAPER. Nothing read the stage of a row a supervisor wrote.

    Mutation: hard-code `stage=Stage.PAPER` in `_track_mark` — red."""
    from tests.test_live_supervisor import BuyOnce, bars
    from trading_platform.core.instruments import crypto_spot
    from trading_platform.data.feed import InMemoryBarFeed
    from trading_platform.engine.execution import ZERO_COST
    from trading_platform.risk.limits import RiskLimits
    from trading_platform.store.accounts import StoreTracker

    tracker = StoreTracker(connection=conn, account="sim", cycle=1, stage=Stage.SIMULATION)
    config = SupervisorConfig(
        root=tmp_path,
        account="sim",
        starting_cash=Decimal("10000"),
        instruments={"BTCUSDT": crypto_spot("BTCUSDT")},
        cost_model=ZERO_COST,
        limits=RiskLimits(),
        kill_switch=tmp_path / "KILL",
        tracker=tracker,
        stage=Stage.SIMULATION,
    )
    PaperSupervisor(
        config, BuyOnce(), now=datetime(2026, 9, 1, tzinfo=UTC) - timedelta(hours=1)
    ).run(InMemoryBarFeed(bars(["100", "100", "110"])))

    with conn.cursor() as cursor:
        cursor.execute("SELECT DISTINCT stage FROM account_marks WHERE account = 'sim'")
        assert [r[0] for r in cursor.fetchall()] == ["simulation"]


def test_a_multi_symbol_mark_records_every_holding_at_the_instants_own_price(
    conn: Any, tmp_path: Any
) -> None:
    """The mark used to be written at the FIRST bar of a new instant, when only
    that symbol had been revealed — so every row for a multi-symbol account
    carried stale prices for all but one holding, and `marks_once` made any
    later correction a silently dropped duplicate.

    Mutation: track `at` instead of the previous instant's watermark, or drop
    the closing `_track_mark` from `_on_finish` — the stored equity disagrees
    with the run's own and this goes red."""
    from trading_platform.core.events import Bar
    from trading_platform.core.instruments import crypto_spot
    from trading_platform.data.feed import InMemoryBarFeed
    from trading_platform.engine.execution import ZERO_COST
    from trading_platform.risk.limits import RiskLimits
    from trading_platform.store.accounts import StoreTracker
    from trading_platform.strategy.base import Strategy, StrategyContext

    class BuyBoth(Strategy):
        name = "buy-both"

        def __init__(self) -> None:
            self.done = False

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            if not self.done and bar.symbol == "BBB":
                ctx.buy("AAA", Decimal("1"))
                ctx.buy("BBB", Decimal("1"))
                self.done = True

    start = datetime(2026, 9, 1, tzinfo=UTC)
    prices = {"AAA": ["100", "100", "110"], "BBB": ["100", "100", "150"]}
    feed_bars = []
    for i in range(3):
        for symbol in ("AAA", "BBB"):
            price = Decimal(prices[symbol][i])
            feed_bars.append(
                Bar(
                    symbol=symbol,
                    timestamp=start + timedelta(days=i),
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=Decimal("1"),
                )
            )

    tracker = StoreTracker(connection=conn, account="multi", cycle=1, stage=Stage.PAPER)
    config = SupervisorConfig(
        root=tmp_path,
        account="multi",
        starting_cash=Decimal("10000"),
        instruments={"AAA": crypto_spot("AAA"), "BBB": crypto_spot("BBB")},
        cost_model=ZERO_COST,
        limits=RiskLimits(),
        kill_switch=tmp_path / "KILL",
        tracker=tracker,
    )
    result = PaperSupervisor(config, BuyBoth(), now=start - timedelta(hours=1)).run(
        InMemoryBarFeed(feed_bars)
    )

    latest = latest_mark(conn, "multi")
    assert latest is not None
    assert latest["equity"] == result.result.ending_equity, (
        f"the stored mark says {latest['equity']} and the run ended at "
        f"{result.result.ending_equity} — the row was written before every symbol of "
        "the instant had been revealed"
    )


def test_a_tracker_that_raises_does_not_stop_the_session(conn: Any, tmp_path: Any) -> None:
    """The `MarkSink` contract's whole point, and it was untested: the mutation
    that makes `StoreTracker` re-raise survived, and separately a `ValueError`
    from building the mark killed sessions in ordinary use.

    Mutation: let the exception out of `StoreTracker.record_mark`, or move
    `mark_from_portfolio` back outside the try in `_track_mark` — the session
    dies and this goes red."""
    from tests.test_live_supervisor import BuyOnce, bars
    from trading_platform.core.instruments import crypto_spot
    from trading_platform.data.feed import InMemoryBarFeed
    from trading_platform.engine.execution import ZERO_COST
    from trading_platform.risk.limits import RiskLimits

    class Hostile:
        def record_mark(self, mark: Any) -> None:
            raise RuntimeError("the database is on fire")

        def record_rejections(self, rejections: Any, **kwargs: Any) -> None:
            raise RuntimeError("still on fire")

    config = SupervisorConfig(
        root=tmp_path,
        account="a1",
        starting_cash=Decimal("10000"),
        instruments={"BTCUSDT": crypto_spot("BTCUSDT")},
        cost_model=ZERO_COST,
        limits=RiskLimits(),
        kill_switch=tmp_path / "KILL",
        tracker=Hostile(),  # type: ignore[arg-type]
    )
    result = PaperSupervisor(
        config, BuyOnce(), now=datetime(2026, 9, 1, tzinfo=UTC) - timedelta(hours=1)
    ).run(InMemoryBarFeed(bars(["100", "100", "110"])))
    assert result.stopped_by == "feed ended", "an observer stopped the session it observes"
    assert len(result.fills) == 1


def test_a_fractional_position_at_an_odd_price_does_not_break_the_identity() -> None:
    """The P0. `marked_equity` is `quantize(cash + pv)`, so rounding `pv`
    separately broke `equity = cash + positions_value` on half-cent ties — 52 of
    2,000 ordinary (quantity, price) combinations — and the exception blamed the
    ledger for a rounding artefact.

    Mutation: go back to `positions_value=quantize_cash(portfolio.marked_positions_value)`
    — this goes red on dozens of the combinations below."""
    from trading_platform.core.events import Fill
    from trading_platform.core.instruments import crypto_spot
    from trading_platform.core.types import Side
    from trading_platform.engine.portfolio import Portfolio
    from trading_platform.live.tracker import mark_from_portfolio

    instrument = crypto_spot("X")
    at = datetime(2026, 1, 1, tzinfo=UTC)
    checked = 0
    for tenths in range(1, 21):
        for cents in range(1, 26):
            quantity = Decimal(tenths) / Decimal(10)
            price = Decimal("100") + Decimal(cents) / Decimal(100)
            portfolio = Portfolio(starting_cash=Decimal("10000"))
            portfolio.apply_fill(
                instrument,
                Fill(
                    order_id="o",
                    symbol="X",
                    side=Side.BUY,
                    quantity=quantity,
                    price=price,
                    timestamp=at,
                    commission=Decimal("0"),
                ),
            )
            portfolio.observe_price("X", price + Decimal("0.04"))
            mark = mark_from_portfolio(
                portfolio,
                account="a",
                cycle=1,
                stage=Stage.PAPER,
                at=at,
                peak_equity=Decimal("10000"),
                orders=1,
            )
            assert mark.equity == mark.cash + mark.positions_value
            checked += 1
    assert checked == 500


def test_the_mark_reports_margin_free_cash_and_leverage_rather_than_zeros() -> None:
    """Three mutations survived here — clamping `free_cash` to zero, hard-coding
    `gross_leverage` to 0, and hard-coding `margin_used` to 0 — because nothing
    read them off a real portfolio.

    Mutation: any of those three — red."""
    from trading_platform.core.events import Fill
    from trading_platform.core.instruments import crypto_perpetual
    from trading_platform.core.types import Side
    from trading_platform.engine.portfolio import Portfolio
    from trading_platform.live.tracker import mark_from_portfolio

    perp = crypto_perpetual("P", initial_margin_rate=Decimal("0.1"))
    at = datetime(2026, 1, 1, tzinfo=UTC)
    portfolio = Portfolio(starting_cash=Decimal("1000"))
    portfolio.apply_fill(
        perp,
        Fill(
            order_id="o",
            symbol="P",
            side=Side.BUY,
            quantity=Decimal("50"),
            price=Decimal("100"),
            timestamp=at,
            commission=Decimal("0"),
        ),
    )
    portfolio.observe_price("P", Decimal("100"))
    mark = mark_from_portfolio(
        portfolio,
        account="a",
        cycle=1,
        stage=Stage.PAPER,
        at=at,
        peak_equity=Decimal("1000"),
        orders=1,
    )
    assert mark.exposure == Decimal("5000.00")
    assert mark.margin_used == Decimal("500.00"), "margin_used is not being read"
    assert mark.gross_leverage > Decimal("1"), "gross_leverage is not being computed"
    assert mark.free_cash == portfolio.free_cash


def test_marks_disagree_actually_queries_rather_than_returning_empty(conn: Any) -> None:
    """It is the check for rows somebody wrote around the API, so returning `[]`
    unconditionally makes it a decoration — and that mutation survived.

    Mutation: `return []` in `marks_disagree` — red."""
    write_mark(conn, mark(0))
    assert marks_disagree(conn, "a1") == [], "a single mark cannot disagree with itself"

    with conn.cursor() as cursor:
        # Around the API on purpose: `marks_once` makes this impossible through
        # `write_mark`, which is the point — this query is a check on the world,
        # not on this module, and the only way to exercise it is to do what
        # somebody with a psql prompt could do.
        cursor.execute("ALTER TABLE account_marks DROP CONSTRAINT marks_once")
        cursor.execute(
            "INSERT INTO account_marks (account, cycle, stage, at, cash, positions_value, "
            "equity, realised_pnl, unrealised_pnl, fees, funding, exposure, margin_used, "
            "free_cash, gross_leverage, peak_equity, drawdown_pct, orders, fills, positions) "
            "VALUES ('a1', 1, 'paper', %s, 1, 1, 2, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, '{}')",
            (T0,),
        )
    conn.commit()
    found = marks_disagree(conn, "a1")
    assert found and found[0]["equities"] == 2, found
