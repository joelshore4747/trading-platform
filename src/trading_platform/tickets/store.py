"""Where ticket events live: a file that always works, and a database that queries.

The same division as everywhere else in this repository. The **JSONL log is the
record**: it needs a filesystem and nothing else, it is committed to git, and an
agent with no database can still file a finding — which matters, because an
auditor that cannot record what it found is the failure this whole workstream
exists to fix. Postgres is a projection built from the log, and it can be rebuilt
from the log at any time.

That direction is deliberate and is the opposite of the usual one. If the two
ever disagree, **the log is right**: it is append-only and the projection is
derived.

`harness/tickets/events.jsonl` is the default location. It is in the repository
on purpose: a ticket filed in one session has to be visible in the next, and a
gitignored file in a container that is wiped at the end of the session would be
exactly the disappearing record this replaces.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

try:  # POSIX only, which is every machine this runs on. Absence degrades, never fails.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from trading_platform.tickets.model import (
    Severity,
    Ticket,
    TicketError,
    TicketEvent,
    TicketKind,
    TicketStatus,
    fold,
    ticket_id,
)

__all__ = [
    "DEFAULT_LOG",
    "Filing",
    "append",
    "file_ticket",
    "load",
    "open_from_store",
    "read_events",
    "sync_to_store",
]

#: Committed to git, deliberately: a finding must outlive the session.
DEFAULT_LOG = Path("harness/tickets/events.jsonl")


def _log_path(path: Path | str | None) -> Path:
    if path is not None:
        return Path(path)
    override = os.environ.get("TRADING_PLATFORM_TICKETS")
    return Path(override) if override else DEFAULT_LOG


@contextmanager
def _locked(handle: Any, exclusive: bool) -> Any:
    """Hold a POSIX advisory lock for the block.

    Several auditors file at once by design — the gauntlet runs five in
    parallel — and an event whose JSON exceeds the 8 KiB write buffer reaches
    the file in more than one syscall. A concurrent reader could see a
    half-written line, raise "not a ticket event", and abort *before* appending
    its own finding: measured at 1-4 of 8 writers losing their finding
    (guard-auditor, 2026-09-04). Advisory locks are enough because every writer
    goes through this module.
    """
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    try:
        yield handle
    finally:
        if fcntl is not None:
            with suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _append_bytes(log: Path, body: str) -> None:
    """Append in ONE syscall to an O_APPEND descriptor.

    Not `open("a").write(...)`: Python buffers text at 8 KiB, so an event
    carrying a pytest failure or a mutation table reaches the file in several
    writes and a concurrent reader can see half of it. A single `os.write` to an
    O_APPEND fd is placed at the end of the file atomically by the kernel, so
    two agents filing at once interleave whole records — which is what this
    module's docstring claimed before it was true.

    The lock is held as well, so the read-then-append in `file_ticket` sees a
    consistent file. The atomicity above is what makes the LOG safe; the lock is
    what makes `is_new` accurate.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        _write_record(fd, body)
    finally:
        os.close(fd)


def _write_record(fd: int, body: str) -> None:
    """One record, one `os.write`, to a descriptor already opened O_APPEND.

    Takes no lock of its own: `file_ticket` holds one on its own descriptor, and
    flock is held per open-file-description — a second `open` here would wait on
    the caller's own lock forever.
    """
    raw = body.encode("utf-8")
    written = os.write(fd, raw)
    while written < len(raw):  # pragma: no cover - short writes on a regular file
        written += os.write(fd, raw[written:])


def _parse(text: str, where: Path) -> list[TicketEvent]:
    """Parse a whole log. A malformed line raises with its number rather than
    being skipped: a log that quietly tolerated holes is a log nobody could
    count from."""
    events: list[TicketEvent] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(TicketEvent.from_json(line))
        except TicketError as exc:
            raise TicketError(f"{where}:{number}: {exc}") from None
    return events


def read_events(path: Path | str | None = None) -> list[TicketEvent]:
    """Every event in the log, in the order it was written.

    Reads the whole file under a shared lock and then parses, rather than
    yielding as it goes: a generator would hold the lock for as long as the
    caller took to consume it, and would tear against a concurrent append.
    """
    log = _log_path(path)
    if not log.exists():
        return []
    with log.open(encoding="utf-8") as handle, _locked(handle, exclusive=False):
        text = handle.read()
    return _parse(text, log)


def load(path: Path | str | None = None) -> dict[str, Ticket]:
    """Current state of every ticket."""
    return fold(read_events(path))


def append(events: Iterable[TicketEvent], path: Path | str | None = None) -> int:
    """Append events. Returns how many were written.

    Opened in append mode per call and flushed, so two agents filing at once
    interleave whole lines rather than corrupting each other's. Nothing is ever
    rewritten: a correction is another event.
    """
    batch = list(events)
    if not batch:
        return 0
    log = _log_path(path)
    log.parent.mkdir(parents=True, exist_ok=True)
    _append_bytes(log, "".join(event.to_json() + "\n" for event in batch))
    return len(batch)


class Filing(NamedTuple):
    """What filing did. Unpacks as (id, is_new) for the common case."""

    id: str
    is_new: bool
    #: What the ticket was before this filing, when it already existed. A
    #: re-filing onto a CLOSED or WONTFIX ticket reopens it, and the caller has
    #: to be able to say so loudly rather than reporting an ordinary success.
    previous_status: TicketStatus | None = None
    #: Set when the log could not be read. The event is still appended — a read
    #: failure must never cost a finding — but `is_new` is then a guess.
    unreadable: str = ""


def file_ticket(
    *,
    kind: TicketKind,
    severity: Severity,
    title: str,
    path: str,
    source: str,
    reproduction: str = "",
    detail: str = "",
    line: int | None = None,
    at: datetime | None = None,
    log: Path | str | None = None,
) -> Filing:
    """File a finding. The write happens whatever else goes wrong.

    Filing something already open is not an error and not a no-op: it appends a
    second `filed` event, so the ticket records that two sources found it
    independently. Corroboration is information, and a store that deduplicated
    it away would throw that information out.

    Filing onto a ticket that was CLOSED **reopens** it. Someone re-finding a
    defect after a fix is the strongest evidence the fix was wrong.

    The read and the append happen under one exclusive lock, and a read that
    fails does not stop the append: `file_ticket` used to read first, purely to
    compute a cosmetic flag, and a torn read from a concurrent writer aborted it
    before it had written anything — losing the finding, which is the one thing
    this must never do.
    """
    identifier = ticket_id(kind, title, path)
    event = TicketEvent(
        ticket=identifier,
        action="filed",
        at=at or datetime.now(UTC),
        source=source,
        kind=kind,
        severity=severity,
        title=title,
        path=path,
        line=line,
        reproduction=reproduction,
        detail=detail,
    )
    target = _log_path(log)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = event.to_json() + "\n"

    existing: Ticket | None = None
    unreadable = ""
    with target.open("a+", encoding="utf-8") as handle, _locked(handle, exclusive=True):
        try:
            handle.seek(0)
            existing = fold(_parse(handle.read(), target)).get(identifier)
        except TicketError as exc:
            unreadable = str(exc)
        # Written through the handle whose lock this block already holds. A
        # separate `open` would take a second flock on the same file and wait on
        # our own lock forever: flock is held per open-file-description.
        _write_record(handle.fileno(), body)

    return Filing(
        id=identifier,
        is_new=existing is None,
        previous_status=existing.status if existing is not None else None,
        unreadable=unreadable,
    )


def sync_to_store(connection: Any, path: Path | str | None = None) -> tuple[int, int]:
    """Rebuild the Postgres projection from the log. Returns (events, tickets).

    Rebuild, not merge: the log is the record, so the projection is replaced by
    what the log says. That makes the operation idempotent and makes a divergence
    impossible to accumulate — run it twice and nothing changes.
    """
    events = list(read_events(path))
    tickets = fold(events)

    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE ticket_events, tickets")
        if events:
            cursor.executemany(
                "INSERT INTO ticket_events (ticket, action, at, source, resolution, commit, "
                "detail, duplicate_of) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        e.ticket,
                        e.action,
                        e.at,
                        e.source,
                        e.resolution,
                        e.commit,
                        e.detail,
                        e.duplicate_of,
                    )
                    for e in events
                ],
            )
        if tickets:
            cursor.executemany(
                "INSERT INTO tickets (id, kind, severity, title, path, line, status, "
                "opened_at, closed_at, reproduction, detail, resolution, commit, "
                "duplicate_of, sources, reopens) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        t.id,
                        str(t.kind),
                        str(t.severity),
                        t.title,
                        t.path,
                        t.line,
                        str(t.status),
                        t.opened_at,
                        t.closed_at,
                        t.reproduction,
                        t.detail,
                        t.resolution,
                        t.commit,
                        t.duplicate_of,
                        list(t.sources),
                        t.reopens,
                    )
                    for t in tickets.values()
                ],
            )
    connection.commit()
    return len(events), len(tickets)


def open_from_store(connection: Any, severity: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Open tickets from the projection, worst first. For the operations page."""
    clauses = ["status = 'open'"]
    params: list[Any] = []
    if severity:
        clauses.append("severity = ANY(%s)")
        params.append(list(severity))
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, kind, severity, title, path, line, opened_at, sources, reopens "
            "FROM tickets WHERE " + " AND ".join(clauses) + " ORDER BY severity, opened_at",
            params,
        )
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
