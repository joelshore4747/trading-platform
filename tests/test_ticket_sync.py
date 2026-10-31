"""The Postgres projection of the ticket log.

Needs a real database; the guard in `tests/test_store_guard.py` polices this
module's skip too.

The property: **the log is the record and this is derived from it.** A sync is a
rebuild, not a merge, so it is idempotent and a divergence cannot accumulate.
Anything written directly into the tables is lost at the next sync — which is
why the migration's comment says not to, and why this file checks that it really
does get lost rather than silently surviving to confuse someone.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from trading_platform.store.migrate import migrate
from trading_platform.tickets.model import Severity, TicketEvent, TicketKind
from trading_platform.tickets.store import append, file_ticket, open_from_store, sync_to_store

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
    schema = "k_" + str(abs(hash(request.node.nodeid)) % 10**12)
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


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "events.jsonl"


def filed(log: Path, **over: Any) -> str:
    kwargs: dict[str, Any] = {
        "kind": TicketKind.DEFECT,
        "severity": Severity.P1,
        "title": "a thing is wrong",
        "path": "src/x.py",
        "line": 7,
        "source": "risk-auditor",
        "reproduction": "pytest -k thing",
        "at": T0,
        "log": log,
    }
    kwargs.update(over)
    return file_ticket(**kwargs)[0]


def test_a_sync_projects_the_log_and_running_it_twice_changes_nothing(conn: Any, log: Path) -> None:
    """A rebuild, not a merge: idempotent by construction, so the projection
    cannot drift from the log a little at a time.

    Mutation: change `TRUNCATE` to nothing — the second sync doubles every row
    and this goes red."""
    identifier = filed(log)
    filed(log, source="lookahead-auditor")  # corroboration
    filed(log, kind=TicketKind.FEATURE, title="want a thing", reproduction="")

    events, tickets = sync_to_store(conn, log)
    assert (events, tickets) == (3, 2)
    assert sync_to_store(conn, log) == (3, 2)

    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM tickets")
        assert cursor.fetchone()[0] == 2
        cursor.execute("SELECT sources FROM tickets WHERE id = %s", (identifier,))
        assert cursor.fetchone()[0] == ["risk-auditor", "lookahead-auditor"]


def test_a_row_written_straight_into_the_table_is_discarded_by_the_next_sync(
    conn: Any, log: Path
) -> None:
    """The migration says not to write here. This checks that ignoring it fails
    loudly at the next sync rather than leaving a row nobody can trace to the
    log — which is the disappearing finding this whole workstream replaces."""
    filed(log)
    sync_to_store(conn, log)
    with conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO tickets (id, kind, severity, title, path, status, opened_at, "
            "reproduction, sources) VALUES ('T-handmade', 'defect', 'P0', 'typed in by hand', "
            "'x.py', 'open', now(), 'none', ARRAY['a human'])"
        )
    conn.commit()
    assert len(open_from_store(conn)) == 2

    sync_to_store(conn, log)
    remaining = [t["id"] for t in open_from_store(conn)]
    assert "T-handmade" not in remaining


def test_the_database_refuses_a_defect_with_no_reproduction(conn: Any) -> None:
    """The Python constructor refuses it too. Both, because a CHECK holds against
    anything that writes to the table and a constructor holds only against code
    that goes through it.

    Mutation: drop `tickets_defects_reproduce` from the migration — red."""
    with conn.cursor() as cursor, pytest.raises(psycopg.errors.CheckViolation):
        cursor.execute(
            "INSERT INTO tickets (id, kind, severity, title, path, status, opened_at, "
            "sources) VALUES ('T-x', 'defect', 'P1', 't', 'x.py', 'open', now(), "
            "ARRAY['a'])"
        )
    conn.rollback()


def test_the_database_refuses_a_closure_with_no_reason(conn: Any) -> None:
    with conn.cursor() as cursor, pytest.raises(psycopg.errors.CheckViolation):
        cursor.execute(
            "INSERT INTO tickets (id, kind, severity, title, path, status, opened_at, "
            "sources) VALUES ('T-x', 'feature', 'P3', 't', 'x.py', 'closed', now(), "
            "ARRAY['a'])"
        )
    conn.rollback()


def test_the_database_refuses_a_ticket_with_no_source(conn: Any) -> None:
    with conn.cursor() as cursor, pytest.raises(psycopg.errors.CheckViolation):
        cursor.execute(
            "INSERT INTO tickets (id, kind, severity, title, path, status, opened_at, "
            "sources) VALUES ('T-x', 'feature', 'P3', 't', 'x.py', 'open', now(), '{}')"
        )
    conn.rollback()


def test_a_closed_ticket_leaves_the_open_list_and_keeps_its_history(conn: Any, log: Path) -> None:
    identifier = filed(log)
    append(
        [
            TicketEvent(
                ticket=identifier,
                action="closed",
                at=T0,
                source="implementer",
                resolution="fixed and tested",
                commit="abc1234",
            )
        ],
        log,
    )
    sync_to_store(conn, log)
    assert open_from_store(conn) == []
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT action FROM ticket_events WHERE ticket = %s ORDER BY id", (identifier,)
        )
        assert [row[0] for row in cursor.fetchall()] == ["filed", "closed"]
        cursor.execute("SELECT status, commit FROM tickets WHERE id = %s", (identifier,))
        assert cursor.fetchone() == ("closed", "abc1234")


def test_open_debt_groups_by_severity_and_shows_the_oldest(conn: Any, log: Path) -> None:
    """`oldest` is the number to look at: a P1 open for three weeks is a decision
    nobody made."""
    filed(log, title="old p1", at=datetime(2026, 8, 1, tzinfo=UTC))
    filed(log, title="new p1", at=datetime(2026, 9, 1, tzinfo=UTC))
    filed(log, title="a p3", severity=Severity.P3)
    sync_to_store(conn, log)
    with conn.cursor() as cursor:
        cursor.execute("SELECT severity, tickets, oldest FROM open_debt ORDER BY severity")
        rows = cursor.fetchall()
    assert rows[0][0] == "P1" and rows[0][1] == 2
    assert rows[0][2].astimezone(UTC) == datetime(2026, 8, 1, tzinfo=UTC)


def test_the_repositorys_own_log_syncs(conn: Any) -> None:
    """The log this repository actually carries, through the real projection.
    A ticket that violates a CHECK would be a ticket the Python side let through
    and the database would not."""
    root = Path(__file__).resolve().parents[1]
    events, tickets = sync_to_store(conn, root / "harness" / "tickets" / "events.jsonl")
    assert events >= tickets >= 1
    assert len(open_from_store(conn, severity=["P0", "P1"])) >= 1
