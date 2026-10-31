"""Tickets: what the harness found, in a form that survives the session.

The weakest link in this harness has been the last one: an auditor finds a real
defect, reports it into a transcript, and it exists only for as long as somebody
copies it into markdown by hand. `harness/TECHNICAL_DEBT.md` is the evidence —
hand-maintained, and by 2026-09-04 it had two items numbered 11 and two numbered
12. A finding that depends on transcription is a finding that gets lost.

So findings become **events in an append-only log**, and the log is the record.

## Why events rather than rows

A ticket's history is the interesting part. "This was filed by the
lookahead-auditor on the 3rd, closed on the 4th citing commit abc123, and
reopened on the 7th because the fix was wrong" is three facts, and a table of
current state keeps one of them. The charter's rule that retracted results stay
in the record applies here too: a closed ticket that was wrong to close must
leave both the closing and the reopening visible.

So this module defines events; `Ticket` is the fold, not the storage.

## Why the id is a hash

Several auditors run over the same diff and find the same thing. A sequential id
would give one finding three tickets. The id is derived from what the finding
*is* — its kind, its title and the file it is in — so the same finding filed
three times is one ticket with three sources, and `already_known` is answerable
without a database.

Line numbers are deliberately NOT in the id: they drift with every edit above
them, and a ticket that changes identity when someone adds an import is useless.

## The one quality rule

**A defect with no reproduction is refused.** Not a warning — a refusal, at
construction. "I think this might be wrong" is an opinion; the charter's §119
finding format requires evidence, and the cheapest place to enforce it is where
the finding is written down. Features and questions have no such requirement,
because there is nothing to reproduce.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

__all__ = [
    "PATH_MAX",
    "SEVERITIES",
    "TITLE_MAX",
    "Severity",
    "Ticket",
    "TicketError",
    "TicketEvent",
    "TicketKind",
    "TicketStatus",
    "by_severity",
    "fold",
    "normalise_path",
    "open_tickets",
    "ticket_id",
]


class TicketKind(StrEnum):
    """What kind of thing was found."""

    #: Something is wrong. Needs a reproduction.
    DEFECT = "defect"
    #: Something is missing that would be worth having.
    FEATURE = "feature"
    #: Something nobody here knows the answer to yet.
    QUESTION = "question"
    #: A known, accepted shortcut with a cost. The old TECHNICAL_DEBT rows.
    DEBT = "debt"


class TicketStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    #: Deliberately not going to be fixed, with a reason. Not the same as closed:
    #: closed means it stopped being true, wontfix means it is still true and we
    #: decided to live with it.
    WONTFIX = "wontfix"
    #: The same finding as another ticket, kept so its sources are not lost.
    DUPLICATE = "duplicate"


class Severity(StrEnum):
    """Charter §119 severities. P0 is "stop and fix"; P3 is "worth knowing"."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


SEVERITIES = tuple(Severity)


class TicketError(ValueError):
    """A ticket or event that cannot mean anything."""


#: The title is what appears in a list of forty. Longer belongs in `detail`.
TITLE_MAX = 160

#: The longest path this will accept. Two reasons, and the second is the sharp
#: one: `migrations/0005_tickets.sql` indexes `tickets(path)` with a plain btree,
#: which caps an index row at 2704 bytes, and the log is append-only — so a
#: single oversized path, once written, makes `sync_to_store` (which truncates
#: and re-inserts everything) fail forever, with no sanctioned way to remove the
#: offending line. The recovery cost is the problem, not the likelihood.
PATH_MAX = 512

_LINE_SUFFIX = re.compile(r":\d+(?::\d+)?$")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def normalise_path(path: str) -> str:
    """One spelling for one file, so one finding is one ticket.

    The id is a hash of (kind, title, path) precisely so that several auditors
    finding the same thing produce one ticket with several sources. A leading
    `./`, a doubled slash or the `:142` suffix an auditor naturally pastes from
    a traceback each fork that id silently — the agent is told "filed", and the
    corroboration is lost (guard-auditor, 2026-09-04, five ids for one finding).

    This removes only what is unambiguously noise. It does NOT touch the
    filesystem and does not try to resolve `tickets/store.py` against
    `src/trading_platform/tickets/store.py`: that needs a repository root and a
    guess about which of several matches was meant, and a wrong guess would
    merge two genuinely different findings — worse than forking one.

    A trailing slash is kept: `harness/` means the directory, not the file.
    """
    cleaned = path.strip()
    cleaned = _LINE_SUFFIX.sub("", cleaned)
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    while "//" in cleaned:
        cleaned = cleaned.replace("//", "/")
    return cleaned


def ticket_id(kind: TicketKind, title: str, path: str) -> str:
    """A stable id for one finding, so two agents finding it produce one ticket.

    Derived from kind, title and file — never the line, which drifts with every
    edit above it.
    """
    seed = f"{kind}|{title.strip().lower()}|{normalise_path(path)}"
    return "T-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class TicketEvent:
    """One thing that happened to a ticket. Append-only; never edited.

    `filed` carries the whole finding. `closed`, `wontfix`, `reopened`,
    `duplicate` and `note` carry only what changed.
    """

    ticket: str
    action: str
    at: datetime
    #: Which agent, run or person. Never blank: an unattributed finding cannot
    #: be followed up, and an unattributed closure cannot be questioned.
    source: str
    kind: TicketKind | None = None
    severity: Severity | None = None
    title: str = ""
    path: str = ""
    line: int | None = None
    #: How to see it happen. Required to FILE a defect.
    reproduction: str = ""
    detail: str = ""
    #: Why it was closed, or why it will not be fixed. Required to close.
    resolution: str = ""
    #: The commit that closed it, when there is one.
    commit: str = ""
    #: For `duplicate`: the ticket this one is the same as.
    duplicate_of: str = ""

    ACTIONS = ("filed", "closed", "wontfix", "reopened", "duplicate", "note")

    def __post_init__(self) -> None:
        # Canonicalise before anything reads it, so what is written to the log
        # is the same spelling the id was derived from. `frozen=True` blocks
        # ordinary assignment; this is the sanctioned exception, and it happens
        # once, at construction, before the value can be observed.
        normalised = normalise_path(self.path)
        if normalised != self.path:
            object.__setattr__(self, "path", normalised)

        if self.action not in self.ACTIONS:
            raise TicketError(f"unknown action {self.action!r}; valid: {self.ACTIONS}")
        if not self.ticket:
            raise TicketError("an event needs a ticket id")
        if not self.source.strip():
            raise TicketError(
                f"{self.ticket}: an event needs a source — which agent, run or person. "
                "An unattributed finding cannot be followed up and an unattributed "
                "closure cannot be questioned."
            )
        if self.at.tzinfo is None:
            raise TicketError(f"{self.ticket}: event timestamps must be timezone-aware")
        if self.line is not None and self.line < 1:
            raise TicketError(f"{self.ticket}: line numbers start at 1, got {self.line}")

        if self.action == "filed":
            self._validate_filed()
        elif self.action in ("closed", "wontfix"):
            if not self.resolution.strip():
                raise TicketError(
                    f"{self.ticket}: {self.action} needs a resolution. A ticket that "
                    "closes with no reason reads as somebody tidying up, and the next "
                    "person cannot tell whether the thing stopped being true or stopped "
                    "being convenient."
                )
        elif self.action == "duplicate" and not self.duplicate_of:
            raise TicketError(f"{self.ticket}: marking a duplicate needs the other ticket's id")
        elif self.action == "reopened" and not self.resolution.strip():
            raise TicketError(
                f"{self.ticket}: reopening needs a reason. A ticket that closed and "
                "reopened silently hides that the first fix was wrong."
            )

    def _validate_filed(self) -> None:
        if self.kind is None or self.severity is None:
            raise TicketError(f"{self.ticket}: filing needs a kind and a severity")
        if not self.title.strip():
            raise TicketError(f"{self.ticket}: filing needs a title")
        if len(self.title) > TITLE_MAX:
            raise TicketError(
                f"{self.ticket}: title is {len(self.title)} characters. Put the argument in "
                "`detail`; the title is what appears in a list of forty."
            )
        self._validate_single_line("title", self.title)
        self._validate_path()
        if self.kind is TicketKind.DEFECT and not self.reproduction.strip():
            raise TicketError(
                f"{self.ticket}: a defect needs a reproduction — the command, the input, "
                "or the test that shows it. Without one this is an opinion, and this "
                "repository has been wrong often enough to know the difference."
            )
        expected = ticket_id(self.kind, self.title, self.path)
        if self.ticket != expected:
            raise TicketError(
                f"ticket id {self.ticket} does not match its own content (expected "
                f"{expected}). Ids are derived so that two agents finding the same thing "
                "file one ticket; a hand-written id defeats that."
            )

    def _validate_single_line(self, field: str, value: str) -> None:
        """A newline in a one-line field breaks the generated table below it.

        `render_debt` writes one markdown row per ticket, and a row containing a
        newline ends the table — every ticket after it disappears from
        `harness/TECHNICAL_DEBT.md`, while `debt --check` still reports it up to
        date, because it compares generated text against generated text. The
        renderer now escapes as well; this refuses at the source, because a
        title with a line break in it is not a title.
        """
        if "\n" in value or "\r" in value:
            raise TicketError(
                f"{self.ticket}: {field} contains a line break. A {field} is one line — "
                "put the argument in `detail`, which is free-form."
            )

    def _validate_path(self) -> None:
        """Where the finding is. Required, relative, bounded, one line.

        Empty was accepted and rendered as an empty `where` cell; `/etc/passwd`
        and `../../` were accepted too. None of the three is ever what somebody
        meant, and all three defeat the id's one job (guard-auditor, 2026-09-04).
        """
        if not self.path:
            raise TicketError(
                f"{self.ticket}: filing needs a path — where the finding is. A ticket "
                "nobody can locate is a ticket nobody can act on."
            )
        self._validate_single_line("path", self.path)
        if self.path.startswith("/") or _WINDOWS_ABSOLUTE.match(self.path):
            raise TicketError(
                f"{self.ticket}: path {self.path!r} is absolute. Paths are relative to the "
                "repository root, so that the same finding on two machines is one ticket."
            )
        if ".." in self.path.split("/"):
            raise TicketError(
                f"{self.ticket}: path {self.path!r} escapes the repository root. Two "
                "spellings of one file fork the id, which is the one thing it must not do."
            )
        if len(self.path.encode("utf-8")) > PATH_MAX:
            raise TicketError(
                f"{self.ticket}: path is {len(self.path.encode('utf-8'))} bytes, over the "
                f"{PATH_MAX}-byte limit. The log is append-only and the Postgres projection "
                "indexes this column, so an oversized path cannot be written and then "
                "cannot be removed — every later `sync` fails."
            )

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "ticket": self.ticket,
            "action": self.action,
            "at": self.at.astimezone(UTC).isoformat(),
            "source": self.source,
        }
        for name in (
            "kind",
            "severity",
            "title",
            "path",
            "line",
            "reproduction",
            "detail",
            "resolution",
            "commit",
            "duplicate_of",
        ):
            value = getattr(self, name)
            if value not in (None, "", 0):
                payload[name] = str(value) if isinstance(value, StrEnum) else value
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> TicketEvent:
        try:
            raw: Mapping[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TicketError(f"not a ticket event: {exc}") from None
        try:
            return cls(
                ticket=raw["ticket"],
                action=raw["action"],
                at=datetime.fromisoformat(raw["at"]),
                source=raw["source"],
                kind=TicketKind(raw["kind"]) if "kind" in raw else None,
                severity=Severity(raw["severity"]) if "severity" in raw else None,
                title=raw.get("title", ""),
                path=raw.get("path", ""),
                line=raw.get("line"),
                reproduction=raw.get("reproduction", ""),
                detail=raw.get("detail", ""),
                resolution=raw.get("resolution", ""),
                commit=raw.get("commit", ""),
                duplicate_of=raw.get("duplicate_of", ""),
            )
        except KeyError as exc:
            raise TicketError(f"ticket event is missing {exc}") from None


@dataclass(frozen=True, slots=True)
class Ticket:
    """The current state of one ticket: a fold over its events, not a row."""

    id: str
    kind: TicketKind
    severity: Severity
    title: str
    path: str
    status: TicketStatus
    opened_at: datetime
    line: int | None = None
    reproduction: str = ""
    detail: str = ""
    #: Everyone who has filed this finding. Two auditors finding the same thing
    #: is corroboration, and losing the second one loses that.
    sources: tuple[str, ...] = ()
    closed_at: datetime | None = None
    resolution: str = ""
    commit: str = ""
    duplicate_of: str = ""
    notes: tuple[str, ...] = ()
    #: How many times this has been closed and reopened. Above zero means a fix
    #: did not work, which is worth seeing in a list.
    reopens: int = 0

    @property
    def is_open(self) -> bool:
        return self.status is TicketStatus.OPEN

    @property
    def corroborated(self) -> bool:
        """Found independently by more than one source."""
        return len(self.sources) > 1

    def one_line(self) -> str:
        where = self.path + (f":{self.line}" if self.line else "")
        marks = []
        if self.corroborated:
            marks.append(f"x{len(self.sources)}")
        if self.reopens:
            marks.append(f"reopened {self.reopens}")
        suffix = f" [{', '.join(marks)}]" if marks else ""
        return f"{self.id}  {self.severity}  {self.status:<9} {self.title}  ({where}){suffix}"


def fold(events: Iterable[TicketEvent]) -> dict[str, Ticket]:
    """Replay events into current state, in the order they were written.

    An event for a ticket that was never filed raises rather than inventing one:
    a `closed` with no `filed` before it means the log is truncated or somebody
    edited it, and quietly materialising a ticket out of a closure would hide
    that.
    """
    tickets: dict[str, Ticket] = {}
    for event in events:
        if event.action == "filed":
            existing = tickets.get(event.ticket)
            if existing is None:
                assert event.kind is not None and event.severity is not None
                tickets[event.ticket] = Ticket(
                    id=event.ticket,
                    kind=event.kind,
                    severity=event.severity,
                    title=event.title,
                    path=event.path,
                    line=event.line,
                    status=TicketStatus.OPEN,
                    opened_at=event.at,
                    reproduction=event.reproduction,
                    detail=event.detail,
                    sources=(event.source,),
                )
                continue
            # Filed again: corroboration. The severity takes the worst of the
            # two — two auditors disagreeing about how bad something is should
            # not resolve downwards — and the first filing's text stands.
            assert event.severity is not None
            sources = (
                existing.sources
                if event.source in existing.sources
                else (*existing.sources, event.source)
            )
            updated = replace(
                existing,
                sources=sources,
                severity=min(existing.severity, event.severity),
                reproduction=existing.reproduction or event.reproduction,
                detail=existing.detail or event.detail,
            )
            if existing.status is not TicketStatus.OPEN:
                # **A re-discovery reopens.** Filing onto a closed ticket used
                # to leave it closed: the auditor was told its finding had been
                # recorded, the finding appeared in no list and no generated
                # table, and it died — which is the exact failure this whole
                # mechanism exists to prevent (guard-auditor, 2026-09-04).
                # Someone re-finding a defect after a fix is the strongest
                # evidence the fix was wrong, so it is treated as such.
                was = existing.status
                updated = replace(
                    updated,
                    status=TicketStatus.OPEN,
                    closed_at=None,
                    resolution="",
                    commit="",
                    reopens=existing.reopens + 1,
                    notes=(
                        *existing.notes,
                        f"re-filed {event.at:%Y-%m-%d} by {event.source} while {was}"
                        + (f" ({existing.resolution})" if existing.resolution else "")
                        + " — a re-discovery reopens it",
                    ),
                )
            tickets[event.ticket] = updated
            continue

        current = tickets.get(event.ticket)
        if current is None:
            raise TicketError(
                f"{event.action} for {event.ticket}, which was never filed. The log is "
                "truncated or was edited; materialising a ticket from a closure would "
                "hide that."
            )
        if event.action == "closed":
            tickets[event.ticket] = replace(
                current,
                status=TicketStatus.CLOSED,
                closed_at=event.at,
                resolution=event.resolution,
                commit=event.commit,
            )
        elif event.action == "wontfix":
            tickets[event.ticket] = replace(
                current,
                status=TicketStatus.WONTFIX,
                closed_at=event.at,
                resolution=event.resolution,
            )
        elif event.action == "duplicate":
            tickets[event.ticket] = replace(
                current, status=TicketStatus.DUPLICATE, duplicate_of=event.duplicate_of
            )
        elif event.action == "reopened":
            tickets[event.ticket] = replace(
                current,
                status=TicketStatus.OPEN,
                closed_at=None,
                resolution="",
                commit="",
                reopens=current.reopens + 1,
                notes=(*current.notes, f"reopened {event.at:%Y-%m-%d}: {event.resolution}"),
            )
        elif event.action == "note":
            tickets[event.ticket] = replace(
                current,
                notes=(*current.notes, f"{event.at:%Y-%m-%d} {event.source}: {event.detail}"),
            )
    return tickets


def open_tickets(tickets: Mapping[str, Ticket]) -> list[Ticket]:
    """Open tickets, worst first, oldest first within a severity."""
    return sorted(
        (t for t in tickets.values() if t.is_open),
        key=lambda t: (t.severity, t.opened_at),
    )


def by_severity(tickets: Sequence[Ticket]) -> dict[Severity, int]:
    counts: dict[Severity, int] = {}
    for ticket in tickets:
        counts[ticket.severity] = counts.get(ticket.severity, 0) + 1
    return counts
