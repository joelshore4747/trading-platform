"""The operations page. One question: is this thing running.

Most of these need no database, because `render_page` is pure — a snapshot in,
HTML out. That is the reason it is shaped that way: the interesting cases (every
feed dead, the store unreachable, an account undercollateralised) are ones you
cannot conveniently arrange in a live system, and a page whose failure modes can
only be seen in production is a page whose failure modes are never seen.

The property that matters most is negative: **this page shows no strategy
result.** The roadmap's frontend deferral guards against a dashboard making an
unvalidated strategy look authoritative, and that deferral stands.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from trading_platform.web.ops import (
    Level,
    OpsSnapshot,
    assess,
    read_snapshot,
    render_page,
)

T0 = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def snapshot(**over: Any) -> OpsSnapshot:
    values: dict[str, Any] = {
        "at": T0,
        "feeds": [
            {
                "source": "alpaca-sip",
                "market": "equity",
                "interval": "1m",
                "symbols": 4,
                "bars": 1200,
                "last_bar": T0,
                "silent_for": timedelta(seconds=45),
                "bars_last_day": 1200,
                "median_lag": timedelta(seconds=8),
            }
        ],
        "disagreements": [],
        "same_venue_errors": 0,
        "cross_venue_gaps": 0,
        "accounts": [],
        "tickets": [],
        "ticket_counts": {},
    }
    values.update(over)
    return OpsSnapshot(**values)


def account(**over: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "account": "a1",
        "cycle": 1,
        "stage": "paper",
        "at": T0 - timedelta(minutes=2),
        "equity": Decimal("10050.00"),
        "cash": Decimal("9500.00"),
        "exposure": Decimal("550.00"),
        "margin_used": Decimal("55.00"),
        "free_cash": Decimal("9445.00"),
        "gross_leverage": Decimal("0.0547"),
        "drawdown_pct": Decimal("0.0125"),
    }
    values.update(over)
    return values


# -- the headline is the answer -------------------------------------------------------------


def test_a_live_feed_leads_with_how_far_behind_it_is() -> None:
    page = render_page(snapshot())
    assert "45s BEHIND" in page
    assert "1 feeds live" in page


def test_a_stopped_feed_leads_with_the_silence_and_names_it() -> None:
    """The most important number is the largest thing on the page.

    Mutation: make `_headline` report the healthy string regardless — the page
    says "BEHIND" while a feed has been dead for two hours, red."""
    dead = snapshot(
        feeds=[{**snapshot().feeds[0], "silent_for": timedelta(hours=2), "source": "polygon"}]
    )
    page = render_page(dead)
    assert "2h SILENT" in page
    assert "polygon" in page
    assert not dead.healthy


def test_no_feeds_at_all_is_not_reported_as_healthy() -> None:
    """Zero feeds looks exactly like zero problems to anything that counts
    failures. It is nothing having run yet.

    Mutation: return the healthy headline when `feeds` is empty — red."""
    empty = snapshot(feeds=[])
    page = render_page(empty)
    assert "NO FEEDS" in page
    assert "BEHIND" not in page
    assert not empty.healthy


def test_an_unreachable_store_says_so_instead_of_rendering_zeros() -> None:
    """A broken page that looks healthy is worse than one that is down. It also
    has to say the collector may still be capturing, because it does not need
    the database — otherwise this page causes a panic it should prevent.

    Mutation: swallow the error and render an empty snapshot — the page reads
    "NO FEEDS" and nobody learns the store is down, red."""
    broken = snapshot(feeds=[], error="OperationalError: connection refused")
    page = render_page(broken)
    assert "STORE UNREACHABLE" in page
    assert "connection refused" in page
    assert "needs no database" in page
    assert not broken.healthy


def test_the_silence_threshold_is_generous_enough_not_to_cry_wolf() -> None:
    """A 1-minute feed is a minute behind by definition. An alarm that fires on
    that is an alarm nobody reads.

    Mutation: drop SILENT_AFTER to a minute — the two-minute feed below is
    flagged and this goes red."""
    ordinary = snapshot(feeds=[{**snapshot().feeds[0], "silent_for": timedelta(minutes=2)}])
    assert ordinary.silent == []
    assert ordinary.healthy


# -- what it must not show ---------------------------------------------------------------


def test_the_page_shows_no_strategy_result() -> None:
    """The roadmap defers the frontend until a strategy survives out of sample.
    That deferral guards against a dashboard making an unvalidated strategy look
    authoritative, and it stands. This page answers "is the machinery alive".

    Mutation: add a Sharpe, a return or an equity curve to `render_page` — red,
    and it should be, because that is a roadmap decision and not a UI one."""
    page = render_page(snapshot(accounts=[account()], tickets=[])).lower()
    for forbidden in ("sharpe", "return %", "pnl %", "win rate", "profit factor", "equity curve"):
        assert forbidden not in page, f"the operations page must not show {forbidden!r}"
    assert "operations only" in page


def test_every_value_is_escaped() -> None:
    """A ticket title is written by an agent and a source name comes from a
    config file. Neither is trusted markup.

    Mutation: drop `html.escape` from `_e` — red."""
    page = render_page(
        snapshot(
            tickets=[
                {
                    "id": "T-1",
                    "severity": "P1",
                    "kind": "defect",
                    "title": "<script>alert(1)</script>",
                    "path": "x.py",
                    "opened_at": T0,
                    "sources": ["a"],
                    "reopens": 0,
                }
            ],
            ticket_counts={"P1": 1},
        )
    )
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# -- the sections -------------------------------------------------------------------------


def test_a_same_venue_disagreement_is_called_an_error_and_basis_is_not() -> None:
    """The distinction the whole multi-source workstream rests on. A page that
    blurred it would train everyone to ignore the section.

    Mutation: count cross-venue gaps into `same_venue_errors` — the healthy
    branch below stops being reached and this goes red."""
    clean = render_page(snapshot(cross_venue_gaps=4000))
    assert "No source has contradicted another on the same venue" in clean
    assert "are basis" in clean

    wrong = render_page(
        snapshot(
            same_venue_errors=3,
            cross_venue_gaps=4000,
            disagreements=[
                {
                    "symbol": "AAPL",
                    "ts": T0,
                    "source_a_id": 1,
                    "source_b_id": 2,
                    "same_venue": True,
                    "worst_field": "close",
                    "worst_gap": Decimal("0.0025"),
                    "close_a": Decimal("100.50"),
                    "close_b": Decimal("100.75"),
                }
            ],
        )
    )
    assert "3 same-venue disagreements" in wrong
    assert "25.0bp" in wrong
    assert "not errors" in wrong


def test_negative_free_cash_is_marked_rather_than_shown_as_an_ordinary_number() -> None:
    """It means the book requires more collateral than the account holds.

    Mutation: drop the `bad` class on negative free cash — red."""
    page = render_page(snapshot(accounts=[account(free_cash=Decimal("-600.00"))]))
    assert '<td class="n"><span class="bad">-600.00</span></td>' in page


def test_an_empty_section_says_so_in_one_line() -> None:
    """Rather than rendering an empty frame, which reads as broken."""
    page = render_page(snapshot(feeds=[], accounts=[], tickets=[]))
    assert "No account has marked." in page
    assert "Nothing open." in page
    assert "not about what is there" in page  # no open tickets is not "no defects"


def test_open_p0_and_p1_tickets_are_marked_urgent() -> None:
    page = render_page(
        snapshot(
            tickets=[
                {
                    "id": "T-1",
                    "severity": "P0",
                    "kind": "defect",
                    "title": "x",
                    "path": "a.py",
                    "opened_at": T0 - timedelta(days=3),
                    "sources": ["a"],
                    "reopens": 0,
                }
            ],
            ticket_counts={"P0": 1, "P3": 9},
        )
    )
    assert 'class="bad">P0 1, P3 9' in page
    assert "3d" in page


def test_the_page_refreshes_itself_without_any_javascript() -> None:
    """No JS at all: the page must work in a terminal browser at 3am over a
    phone tether. Mutation: replace the meta refresh with a script tag — red."""
    page = render_page(snapshot(), refresh_s=15)
    assert 'http-equiv=refresh content="15"' in page
    assert "<script" not in page.lower()


# -- against a real store -------------------------------------------------------------------

psycopg = pytest.importorskip("psycopg", reason="the store needs psycopg")
TEST_DSN_VAR = "TRADING_PLATFORM_TEST_DSN"


def _dsn() -> str | None:
    return os.environ.get(TEST_DSN_VAR) or None


def _reachable(dsn: str) -> bool:
    try:
        psycopg.connect(dsn).close()
    except Exception:
        return False
    return True


@pytest.mark.skipif(_dsn() is None or not _reachable(_dsn() or ""), reason=f"set {TEST_DSN_VAR}")
def test_read_snapshot_runs_every_query_against_a_real_schema() -> None:
    """Five queries against five views and tables. A typo in one of them is
    invisible to every test above, because those take a snapshot as input.

    Mutation: misspell a column in any query — `error` is set and this goes
    red."""
    from trading_platform.store.migrate import migrate

    dsn = _dsn()
    assert dsn is not None
    conn = psycopg.connect(dsn)
    schema = "w_ops_probe"
    try:
        with conn.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cursor.execute(f'CREATE SCHEMA "{schema}"')
            cursor.execute(f'SET search_path TO "{schema}"')
        conn.commit()
        migrate(conn)
        snap = read_snapshot(conn)
        assert snap.error == "", snap.error
        assert snap.feeds == [] and snap.accounts == [] and snap.tickets == []
        assert "NO FEEDS" in render_page(snap)
    finally:
        with conn.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()


# -- alerting: the numbers existed and nobody read them (T-f280e8f0) -----------------------


def run(**over: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "run_id": 7,
        "source": "alpaca-sip",
        "kind": "capture",
        "started_at": T0 - timedelta(hours=3),
        "heartbeat_at": T0 - timedelta(seconds=20),
        "rows_written": 1200,
        "consecutive_errors": 0,
        "sink_failures": 0,
        "dropped": 0,
    }
    values.update(over)
    return values


def test_a_working_collector_raises_nothing() -> None:
    """The control. Without it every assertion below could pass because `assess`
    returns an alert for everything.

    Mutation: make `assess` append an unconditional alert — red here, green in
    every other test in this section, which is how a broken alarm hides."""
    assert assess(snapshot(runs=[run()])) == ()
    assert snapshot(runs=[run()]).healthy


def test_a_wedged_process_is_caught_even_though_bars_are_arriving() -> None:
    """`feed_health` answers "has anything arrived", and a process that died
    thirty seconds ago still satisfies it from the bars it wrote — for ten
    minutes, until `silent_for` crosses. The heartbeat is what distinguishes a
    run that is working from one that is merely open (T-f280e8f0).

    Mutation: drop the `heartbeat_at` branch from `assess` — this goes green
    because the feed itself looks perfectly current, which is the whole bug."""
    snap = snapshot(runs=[run(heartbeat_at=T0 - timedelta(hours=1))])
    assert not snap.silent, "the feed is current; only the process is dead"
    alarms = [a for a in snap.alerts if a.level is Level.ALARM]
    assert [a.subject for a in alarms] == ["alpaca-sip heartbeat stale"]
    assert not snap.healthy


def test_a_capture_erroring_every_poll_is_an_alarm_not_a_footnote() -> None:
    """`consecutive_errors` was printed at exit by a process designed never to
    exit.

    Mutation: drop the `consecutive_errors` branch — red."""
    snap = snapshot(runs=[run(consecutive_errors=40)])
    assert any("failing" in a.subject for a in snap.alerts if a.level is Level.ALARM)


def test_bars_that_never_reached_the_store_are_a_warning_with_the_recovery() -> None:
    """They are not lost — the journal has them — so this is a WARN, and it
    names the command that puts them back rather than leaving somebody to find
    it. A WARN must not affect health: an alarm that fires for a recoverable
    condition is an alarm somebody silences.

    Mutation: raise this to ALARM — `healthy` goes False and this goes red."""
    snap = snapshot(runs=[run(sink_failures=3)])
    warns = [a for a in snap.alerts if a.level is Level.WARN]
    assert len(warns) == 1
    assert "journal_to_store.py" in warns[0].detail
    assert snap.healthy, "a recoverable condition must not page anybody"


def test_a_failed_ingest_run_is_loud() -> None:
    """C01 shipped a collector that wrote zero rows on every poll while both
    ingest runs closed and `--check` passed. A run that ended `failed` is the
    one signal that would have caught it from the database alone.

    Mutation: drop the `failed_runs` loop — red."""
    snap = snapshot(
        failed_runs=[
            {
                "run_id": 4,
                "source": "polygon",
                "kind": "capture",
                "rows_written": 0,
                "error": "ValueError: unknown market 'stocks'",
            }
        ]
    )
    alarms = [a for a in snap.alerts if a.level is Level.ALARM]
    assert len(alarms) == 1
    assert "unknown market" in alarms[0].detail


def test_an_unreachable_store_says_so_and_stops() -> None:
    """Every other rule reads rows that were never fetched, so reporting "no
    feeds" as well would be inventing a second failure out of the first.

    Mutation: return the store alert without the early return — a second alert
    appears and this goes red."""
    alerts = assess(snapshot(error="OperationalError: connection refused", feeds=[]))
    assert len(alerts) == 1
    assert alerts[0].subject == "store unreachable"


def test_an_empty_database_is_not_health() -> None:
    """Mutation: drop the `not snap.feeds` branch — an empty store reports
    healthy, which is the shape of every monitoring system that has ever missed
    an outage on its first night."""
    snap = snapshot(feeds=[])
    assert not snap.healthy
    assert [a.subject for a in snap.alerts] == ["no feeds"]


def test_the_headline_and_the_exit_code_cannot_disagree() -> None:
    """The page and `scripts/check_health.py` share one `assess`. If the page
    could say BEHIND while the check exits non-zero, the check is what gets
    switched off.

    Mutation: give `_headline` its own silence test again (`if snap.silent:`)
    instead of reading the alerts — the heartbeat case renders as green while
    `healthy` is False, and this goes red."""
    wedged = snapshot(runs=[run(heartbeat_at=T0 - timedelta(hours=1))])
    assert not wedged.healthy
    assert 'class="ok"' not in render_page(wedged)

    fine = snapshot(runs=[run()])
    assert fine.healthy
    assert 'class="ok"' in render_page(fine)


def test_alarms_sort_above_warnings() -> None:
    """Somebody reads the first line. Mutation: drop the sort key — the WARN can
    come first and the headline announces a recoverable condition as the
    emergency."""
    # The WARN is emitted BEFORE the ALARM in source order — a run's sink
    # failures are read before the failed-run list — so only the sort puts them
    # the right way round. A case where they already happen to be in order
    # proves nothing.
    snap = snapshot(
        runs=[run(sink_failures=2)],
        failed_runs=[{"run_id": 4, "source": "polygon", "kind": "capture", "error": "boom"}],
    )
    assert [a.level for a in snap.alerts] == [Level.ALARM, Level.WARN]


def test_a_run_that_never_reported_is_only_an_alarm_once_it_is_old() -> None:
    """A capture that has just started has no heartbeat yet and is not broken.

    Mutation: alarm on `heartbeat_at is None` unconditionally — every collector
    alarms for its first poll, every restart pages somebody, and the alarm is
    off by the end of the week."""
    fresh = snapshot(runs=[run(heartbeat_at=None, started_at=T0 - timedelta(seconds=30))])
    assert fresh.healthy
    stale = snapshot(runs=[run(heartbeat_at=None, started_at=T0 - timedelta(hours=2))])
    assert not stale.healthy
    assert "never reported" in stale.alerts[0].subject


def test_a_database_that_answers_nothing_is_reported_not_raised() -> None:
    """The branch that exists so the page says "the store is down" rather than
    rendering zeros had itself never run: every test built `OpsSnapshot(error=)`
    by hand, and the real path raised `AttributeError` on a `slots=True`
    dataclass's absent `__dict__` (T-0e3f8555, found by pointing
    scripts/check_health.py at a database one migration behind).

    Mutation: rebuild the snapshot with `**empty.__dict__` again — red, whereas
    every hand-built-snapshot test in this file stays green, which is how it
    survived in the first place."""

    class Refuses:
        def cursor(self) -> Any:
            raise RuntimeError('relation "collector_health" does not exist')

    snap = read_snapshot(Refuses())
    assert "collector_health" in snap.error
    assert snap.feeds == [] and snap.runs == []
    assert not snap.healthy
    assert [a.subject for a in snap.alerts] == ["store unreachable"]
