"""File, close and list the harness's findings. What an agent actually runs.

    scripts/ticket.py file --kind defect --severity P1 \
        --title "read_bars returns [] for an unknown source" \
        --path src/trading_platform/store/bars.py --line 240 \
        --repro "read_bars(conn, 'typo', market='um', symbol='BTCUSDT', interval='1m') -> []" \
        --source risk-auditor

    scripts/ticket.py list                       # open, worst first
    scripts/ticket.py list --all --kind feature
    scripts/ticket.py show T-1a2b3c4d
    scripts/ticket.py close T-1a2b3c4d --commit abc1234 \
        --resolution "read_bars now raises on an unknown source; test added"
    scripts/ticket.py wontfix T-1a2b3c4d --resolution "..." --source joel
    scripts/ticket.py reopen T-1a2b3c4d --resolution "the fix missed the crypto path"
    scripts/ticket.py debt                       # regenerate harness/TECHNICAL_DEBT.md
    scripts/ticket.py sync                       # rebuild the Postgres projection

**Filing needs no database.** The record is `harness/tickets/events.jsonl`,
committed to git, so an auditor in a container with no Postgres can still record
what it found — which is the entire point: a finding that depends on somebody
transcribing a transcript is a finding that gets lost, and this repository has
the hand-maintained `TECHNICAL_DEBT.md` with two items numbered 11 to prove it.

**A defect needs a reproduction.** Refused without one, at construction. "I think
this might be wrong" is an opinion; §119 asks for evidence.

**Nothing is ever edited or deleted.** A correction is another event, so a wrong
closure stays visible beside the reopening that undid it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.tickets.model import (
    Severity,
    Ticket,
    TicketError,
    TicketEvent,
    TicketKind,
    TicketStatus,
    open_tickets,
)
from trading_platform.tickets.store import append, file_ticket, load, read_events

ROOT = Path(__file__).resolve().parents[1]
DEBT_FILE = ROOT / "harness" / "TECHNICAL_DEBT.md"


def current_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return out.stdout.strip()


def cmd_file(args: argparse.Namespace) -> int:
    filing = file_ticket(
        kind=TicketKind(args.kind),
        severity=Severity(args.severity),
        title=args.title,
        path=args.path,
        line=args.line,
        source=args.source,
        reproduction=args.repro or "",
        detail=args.detail or "",
        log=args.log,
    )
    if filing.unreadable:
        # The event was written anyway; only the "was this new" answer is a
        # guess. Said out loud rather than swallowed.
        print(f"warning: could not read the log ({filing.unreadable})", file=sys.stderr)
    if filing.is_new:
        print(f"{filing.id}  filed ({args.severity} {args.kind})")
    elif filing.previous_status in (TicketStatus.CLOSED, TicketStatus.WONTFIX):
        print(
            f"{filing.id}  REOPENED — it was {filing.previous_status} and you have just "
            "found it again. A re-discovery after a fix is the strongest evidence the fix "
            "was wrong, so it is back on the open list."
        )
    else:
        print(
            f"{filing.id}  already known — recorded {args.source} as a second source. "
            "Independent corroboration is information; it is not a duplicate."
        )
    return 0


def _event(args: argparse.Namespace, action: str, **extra: object) -> int:
    known = load(args.log)
    if args.id not in known:
        near = [t for t in known if t.startswith(args.id)]
        hint = f" Did you mean {near[0]}?" if len(near) == 1 else ""
        print(f"no ticket {args.id}.{hint}", file=sys.stderr)
        return 1
    append(
        [
            TicketEvent(
                ticket=args.id,
                action=action,
                at=datetime.now(UTC),
                source=args.source,
                **extra,  # type: ignore[arg-type]
            )
        ],
        args.log,
    )
    print(f"{args.id}  {action}")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    commit = args.commit if args.commit is not None else current_commit()
    return _event(args, "closed", resolution=args.resolution, commit=commit)


def cmd_wontfix(args: argparse.Namespace) -> int:
    return _event(args, "wontfix", resolution=args.resolution)


def cmd_reopen(args: argparse.Namespace) -> int:
    return _event(args, "reopened", resolution=args.resolution)


def cmd_duplicate(args: argparse.Namespace) -> int:
    # The point of `duplicate` is to keep the second finding's sources attached
    # to the first. Pointing at a ticket that does not exist keeps nothing and
    # loses the finding, so it is refused rather than recorded.
    if args.of not in load(args.log):
        print(f"no ticket {args.of} to be a duplicate of", file=sys.stderr)
        return 1
    if args.of == args.id:
        print(f"{args.id} cannot be a duplicate of itself", file=sys.stderr)
        return 1
    return _event(args, "duplicate", duplicate_of=args.of)


def cmd_note(args: argparse.Namespace) -> int:
    return _event(args, "note", detail=args.detail)


def _selected(args: argparse.Namespace) -> list[Ticket]:
    known = load(args.log)
    chosen = list(known.values()) if args.all else open_tickets(known)
    if args.kind:
        chosen = [t for t in chosen if str(t.kind) == args.kind]
    if args.severity:
        chosen = [t for t in chosen if str(t.severity) == args.severity]
    if args.path:
        chosen = [t for t in chosen if args.path in t.path]
    return sorted(chosen, key=lambda t: (t.severity, t.opened_at))


def cmd_list(args: argparse.Namespace) -> int:
    chosen = _selected(args)
    if not chosen:
        print("nothing matches. That is a claim about the filter, not about the code.")
        return 0
    for ticket in chosen:
        print(ticket.one_line())
    counts: dict[str, int] = {}
    for ticket in chosen:
        counts[str(ticket.severity)] = counts.get(str(ticket.severity), 0) + 1
    print(
        "\n" + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f" — {len(chosen)} total"
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    known = load(args.log)
    ticket = known.get(args.id)
    if ticket is None:
        print(f"no ticket {args.id}", file=sys.stderr)
        return 1
    where = ticket.path + (f":{ticket.line}" if ticket.line else "")
    print(f"{ticket.id}  {ticket.severity}  {ticket.kind}  {ticket.status}")
    print(f"  {ticket.title}")
    print(f"  {where}")
    print(f"  filed {ticket.opened_at:%Y-%m-%d} by {', '.join(ticket.sources)}")
    if ticket.reproduction:
        print(f"\n  reproduction:\n    {ticket.reproduction}")
    if ticket.detail:
        print(f"\n  detail:\n    {ticket.detail}")
    if ticket.resolution:
        stamp = f"{ticket.closed_at:%Y-%m-%d}" if ticket.closed_at else "?"
        commit = f" ({ticket.commit})" if ticket.commit else ""
        print(f"\n  {ticket.status} {stamp}{commit}: {ticket.resolution}")
    for note in ticket.notes:
        print(f"  note: {note}")
    print("\n  history:")
    for event in read_events(args.log):
        if event.ticket == ticket.id:
            print(f"    {event.at:%Y-%m-%d %H:%M}  {event.action:<10} {event.source}")
    return 0


def _cell(value: str) -> str:
    """One markdown table cell, from text that was never asked to be one.

    A `|` ends the cell and a newline ends the *table* — every ticket below it
    vanishes from `harness/TECHNICAL_DEBT.md` while `debt --check` still reports
    it up to date, because it compares generated text against generated text
    (guard-auditor, 2026-09-04). `TicketEvent` now refuses a line break in a
    title or a path, but a `resolution` is free prose and may legitimately wrap,
    so the renderer has to be safe on its own rather than trusting its input.
    """
    return " ".join(value.replace("|", "\\|").split())


def render_debt(known: dict[str, Ticket]) -> str:
    """`harness/TECHNICAL_DEBT.md` as a view of the log rather than a hand-list.

    The hand-maintained version had two items numbered 11 and two numbered 12,
    which is what a table maintained by copying looks like after a fortnight.
    """
    open_ones = open_tickets(known)
    closed = [t for t in known.values() if not t.is_open]
    lines = [
        "# TECHNICAL_DEBT",
        "",
        "**Generated. Do not edit.** `scripts/ticket.py debt` rewrites this from",
        "`harness/tickets/events.jsonl`, which is the record. File with",
        "`scripts/ticket.py file`; close with `scripts/ticket.py close`.",
        "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC from {len(known)} ticket(s): "
        f"{len(open_ones)} open, {len(closed)} closed or accepted.",
        "",
    ]
    if not open_ones:
        lines += [
            "Nothing open. That is a statement about what has been *found*, not",
            "about what is there.",
            "",
        ]
    else:
        lines += [
            "## Open",
            "",
            "| id | sev | kind | item | where | since | sources |",
            "|---|---|---|---|---|---|---|",
        ]
        for t in open_ones:
            where = t.path + (f":{t.line}" if t.line else "")
            marks = f"{len(t.sources)}" + (f", reopened {t.reopens}" if t.reopens else "")
            lines.append(
                f"| `{t.id}` | {t.severity} | {t.kind} | {_cell(t.title)} | "
                f"`{_cell(where)}` | {t.opened_at:%Y-%m-%d} | {marks} |"
            )
        lines.append("")
    if closed:
        lines += [
            "## Closed and accepted",
            "",
            "| id | sev | item | outcome | commit |",
            "|---|---|---|---|---|",
        ]
        for t in sorted(closed, key=lambda t: t.closed_at or t.opened_at, reverse=True):
            lines.append(
                f"| `{t.id}` | {t.severity} | {_cell(t.title)} | **{t.status}** — "
                f"{_cell(t.resolution)} | {_cell(t.commit) or '—'} |"
            )
        lines.append("")
    lines += [
        "Retracted and reopened tickets stay in this table on purpose. A closure that",
        "turned out to be wrong is the most useful thing in a defect log, and hiding it",
        "is the dishonesty this repository is built against.",
        "",
    ]
    return "\n".join(lines)


def _without_stamp(body: str) -> str:
    """The generation timestamp changes on every run and is not drift."""
    return "\n".join(line for line in body.splitlines() if not line.startswith("Generated 2"))


def cmd_debt(args: argparse.Namespace) -> int:
    text = render_debt(load(args.log))
    if args.check:
        current = DEBT_FILE.read_text(encoding="utf-8") if DEBT_FILE.exists() else ""

        if _without_stamp(current) != _without_stamp(text):
            print(
                f"{DEBT_FILE} is out of date with the ticket log. Run `scripts/ticket.py debt`.",
                file=sys.stderr,
            )
            return 1
        print(f"{DEBT_FILE} matches the ticket log")
        return 0
    DEBT_FILE.write_text(text, encoding="utf-8")
    print(f"wrote {DEBT_FILE}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from trading_platform.store.connection import connect, is_configured
    from trading_platform.store.migrate import migrate
    from trading_platform.tickets.store import sync_to_store

    if not is_configured():
        print(
            "TRADING_PLATFORM_DSN is unset. The log is the record and needs no database; "
            "sync only builds the queryable projection.",
            file=sys.stderr,
        )
        return 1
    with connect() as conn:
        migrate(conn)
        events, tickets = sync_to_store(conn, args.log)
    print(f"projected {events} event(s) into {tickets} ticket(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--log", default=None, help="ticket log (default harness/tickets/events.jsonl)"
    )
    subs = parser.add_subparsers(dest="command", required=True)

    filed = subs.add_parser("file", help="record a finding")
    filed.add_argument("--kind", required=True, choices=[str(k) for k in TicketKind])
    filed.add_argument("--severity", required=True, choices=[str(s) for s in Severity])
    filed.add_argument("--title", required=True)
    filed.add_argument(
        "--path",
        required=True,
        help="the file it is in, relative to the repository root; no :LINE suffix (use --line)",
    )
    filed.add_argument("--line", type=int, default=None)
    filed.add_argument("--repro", default="", help="required for a defect")
    filed.add_argument("--detail", default="")
    filed.add_argument("--source", required=True, help="which agent, run or person")
    filed.set_defaults(func=cmd_file)

    for name, handler, needs in (
        ("close", cmd_close, "resolution"),
        ("wontfix", cmd_wontfix, "resolution"),
        ("reopen", cmd_reopen, "resolution"),
    ):
        sub = subs.add_parser(name)
        sub.add_argument("id")
        sub.add_argument(f"--{needs}", required=True)
        sub.add_argument("--source", default="human")
        if name == "close":
            sub.add_argument("--commit", default=None, help="default: the current HEAD")
        sub.set_defaults(func=handler)

    dup = subs.add_parser("duplicate")
    dup.add_argument("id")
    dup.add_argument("--of", required=True)
    dup.add_argument("--source", default="human")
    dup.set_defaults(func=cmd_duplicate)

    note = subs.add_parser("note")
    note.add_argument("id")
    note.add_argument("--detail", required=True)
    note.add_argument("--source", default="human")
    note.set_defaults(func=cmd_note)

    listing = subs.add_parser("list")
    listing.add_argument("--all", action="store_true", help="closed ones too")
    listing.add_argument("--kind", default=None, choices=[str(k) for k in TicketKind])
    listing.add_argument("--severity", default=None, choices=[str(s) for s in Severity])
    listing.add_argument("--path", default=None, help="substring match")
    listing.set_defaults(func=cmd_list)

    show = subs.add_parser("show")
    show.add_argument("id")
    show.set_defaults(func=cmd_show)

    debt = subs.add_parser("debt", help="regenerate harness/TECHNICAL_DEBT.md")
    debt.add_argument("--check", action="store_true", help="fail if it is out of date")
    debt.set_defaults(func=cmd_debt)

    sync = subs.add_parser("sync", help="rebuild the Postgres projection from the log")
    sync.set_defaults(func=cmd_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except TicketError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return result


if __name__ == "__main__":
    raise SystemExit(main())
