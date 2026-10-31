"""Tickets: the mechanism that stops a finding dying with the transcript.

The properties that matter, in order:

1. **A defect with no reproduction is refused.** Not warned about — refused, at
   construction and again by a database CHECK. "I think this might be wrong" is
   an opinion, and §119 asks for evidence.
2. **Two agents finding the same thing file one ticket with two sources.**
   Corroboration is information. A store that deduplicated it away, or one that
   made three tickets out of it, would both lose something.
3. **Nothing is ever edited or deleted.** A wrong closure stays visible beside
   the reopening that undid it, because that is the most useful thing in a
   defect log.
4. **Filing needs no database.** An auditor in a container with no Postgres must
   still be able to record what it found, or the whole mechanism fails in
   exactly the situation it exists for.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_platform.tickets.model import (
    PATH_MAX,
    Severity,
    TicketError,
    TicketEvent,
    TicketKind,
    TicketStatus,
    normalise_path,
    open_tickets,
    ticket_id,
)
from trading_platform.tickets.store import append, file_ticket, load, read_events

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def filed(log: Path, **over: object) -> tuple[str, bool]:
    kwargs: dict[str, object] = {
        "kind": TicketKind.DEFECT,
        "severity": Severity.P1,
        "title": "read_bars returns [] for an unknown source",
        "path": "src/trading_platform/store/bars.py",
        "line": 240,
        "source": "risk-auditor",
        "reproduction": "read_bars(conn, 'typo', ...) -> []",
        "at": T0,
        "log": log,
    }
    kwargs.update(over)
    result = file_ticket(**kwargs)  # type: ignore[arg-type]
    return result.id, result.is_new


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "events.jsonl"


# -- the one quality rule ------------------------------------------------------------------


def test_a_defect_with_no_reproduction_is_refused(log: Path) -> None:
    """The rule the whole thing rests on. Mutation: drop the `reproduction`
    check from `_validate_filed` — an unreproducible claim becomes a P1 ticket
    somebody has to triage, and the log fills with opinions."""
    with pytest.raises(TicketError, match="needs a reproduction"):
        filed(log, reproduction="")
    with pytest.raises(TicketError, match="needs a reproduction"):
        filed(log, reproduction="   ")
    assert not log.exists(), "a refused filing must not write a partial event"


def test_a_feature_or_question_needs_no_reproduction(log: Path) -> None:
    """The rule must not become a general nuisance: there is nothing to
    reproduce about a missing feature, and requiring one would teach people to
    write 'n/a'."""
    for kind in (TicketKind.FEATURE, TicketKind.QUESTION, TicketKind.DEBT):
        identifier, is_new = filed(log, kind=kind, title=f"want {kind}", reproduction="")
        assert is_new and identifier.startswith("T-")


def test_an_event_with_no_source_is_refused(log: Path) -> None:
    """An unattributed finding cannot be followed up and an unattributed closure
    cannot be questioned."""
    with pytest.raises(TicketError, match="needs a source"):
        filed(log, source="  ")


def test_closing_needs_a_reason(log: Path) -> None:
    """A ticket that closes with no reason reads as somebody tidying up, and the
    next person cannot tell whether the thing stopped being true or stopped
    being convenient."""
    identifier, _ = filed(log)
    for action in ("closed", "wontfix"):
        with pytest.raises(TicketError, match="needs a resolution"):
            TicketEvent(ticket=identifier, action=action, at=T0, source="me", resolution="")


def test_reopening_needs_a_reason(log: Path) -> None:
    """A ticket that closed and reopened silently hides that the first fix was
    wrong, which is the most useful thing in a defect log."""
    identifier, _ = filed(log)
    with pytest.raises(TicketError, match="reopening needs a reason"):
        TicketEvent(ticket=identifier, action="reopened", at=T0, source="me")


# -- identity and corroboration -------------------------------------------------------------


def test_the_same_finding_from_two_agents_is_one_ticket_with_two_sources(log: Path) -> None:
    """Several auditors run over one diff and find the same thing. A sequential
    id would give one finding three tickets and three triages.

    Mutation: make `ticket_id` include a counter or the line — the second filing
    creates a second ticket and this goes red."""
    first, was_new = filed(log)
    second, was_new_again = filed(log, source="lookahead-auditor", line=999)
    assert first == second
    assert was_new and not was_new_again

    ticket = load(log)[first]
    assert ticket.sources == ("risk-auditor", "lookahead-auditor")
    assert ticket.corroborated


def test_the_line_number_is_not_part_of_the_identity(log: Path) -> None:
    """Lines drift with every edit above them. A ticket that changes identity
    when somebody adds an import is useless.

    Mutation: put `line` into `ticket_id` — red."""
    assert ticket_id(TicketKind.DEFECT, "x", "a.py") == ticket_id(TicketKind.DEFECT, "x", "a.py")
    first, _ = filed(log)
    second, is_new = filed(log, line=1)
    assert first == second and not is_new


def test_two_sources_disagreeing_on_severity_resolve_to_the_worse_one(log: Path) -> None:
    """A disagreement about how bad something is must not resolve downwards.

    Mutation: `max(...)` instead of `min(...)` in the fold, or take the latest —
    a P0 filed after a P3 reads as P3 and gets triaged last."""
    identifier, _ = filed(log, severity=Severity.P3)
    filed(log, severity=Severity.P0, source="adversarial-reviewer")
    assert load(log)[identifier].severity is Severity.P0

    # And the other order, so this is not passing by accident of ordering.
    other, _ = filed(log, title="another", severity=Severity.P0)
    filed(log, title="another", severity=Severity.P3, source="someone-else")
    assert load(log)[other].severity is Severity.P0


def test_a_hand_written_id_that_does_not_match_its_content_is_refused() -> None:
    """Ids are derived so that two agents finding one thing file one ticket. A
    hand-written id defeats that silently."""
    with pytest.raises(TicketError, match="does not match its own content"):
        TicketEvent(
            ticket="T-deadbeef",
            action="filed",
            at=T0,
            source="me",
            kind=TicketKind.FEATURE,
            severity=Severity.P3,
            title="x",
            path="a.py",
        )


# -- the log is append-only ------------------------------------------------------------------


def test_a_closure_and_its_reopening_both_stay_visible(log: Path) -> None:
    """Mutation: have `reopened` reset `reopens` to 0, or drop the note — the
    record stops saying that a fix did not work."""
    identifier, _ = filed(log)
    append(
        [
            TicketEvent(
                ticket=identifier,
                action="closed",
                at=T0 + timedelta(days=1),
                source="implementer",
                resolution="read_bars now raises",
                commit="abc1234",
            ),
            TicketEvent(
                ticket=identifier,
                action="reopened",
                at=T0 + timedelta(days=3),
                source="guard-auditor",
                resolution="the fix missed the crypto path",
            ),
        ],
        log,
    )
    ticket = load(log)[identifier]
    assert ticket.status is TicketStatus.OPEN
    assert ticket.reopens == 1
    assert any("crypto path" in note for note in ticket.notes)

    actions = [e.action for e in read_events(log) if e.ticket == identifier]
    assert actions == ["filed", "closed", "reopened"], "nothing is ever removed"


def test_wontfix_is_not_the_same_as_closed(log: Path) -> None:
    """Closed means it stopped being true. Wontfix means it is still true and
    somebody decided to live with it. Collapsing them loses the decision."""
    identifier, _ = filed(log)
    append(
        [
            TicketEvent(
                ticket=identifier,
                action="wontfix",
                at=T0,
                source="joel",
                resolution="the cost of the fix exceeds the cost of the bug",
            )
        ],
        log,
    )
    ticket = load(log)[identifier]
    assert ticket.status is TicketStatus.WONTFIX
    assert not ticket.is_open


def test_an_event_for_a_ticket_that_was_never_filed_raises(log: Path) -> None:
    """A `closed` with no `filed` before it means the log is truncated or was
    edited. Materialising a ticket out of a closure would hide that.

    Mutation: create the ticket on demand in `fold` — red, and a truncated log
    becomes invisible."""
    append(
        [
            TicketEvent(
                ticket="T-00000000",
                action="closed",
                at=T0,
                source="me",
                resolution="whatever",
            )
        ],
        log,
    )
    with pytest.raises(TicketError, match="never filed"):
        load(log)


def test_a_malformed_line_names_its_number_rather_than_being_skipped(log: Path) -> None:
    """A log that quietly tolerated holes is a log nobody could count from."""
    filed(log)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("this is not json\n")
    with pytest.raises(TicketError, match=r"events\.jsonl:2"):
        load(log)


def test_filing_needs_no_database(log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole mechanism fails in exactly the situation it exists for if an
    auditor with no Postgres cannot record what it found.

    Mutation: make `file_ticket` require a connection — red, and every agent
    running without a DSN silently stops filing."""
    monkeypatch.delenv("TRADING_PLATFORM_DSN", raising=False)
    monkeypatch.delenv("TRADING_PLATFORM_TEST_DSN", raising=False)
    identifier, is_new = filed(log)
    assert is_new
    assert load(log)[identifier].id == identifier


def test_open_tickets_come_back_worst_first(log: Path) -> None:
    filed(log, title="a p3", severity=Severity.P3, at=T0)
    filed(log, title="a p0", severity=Severity.P0, at=T0 + timedelta(days=1))
    filed(log, title="an older p0", severity=Severity.P0, at=T0 - timedelta(days=1))
    order = [t.title for t in open_tickets(load(log))]
    assert order == ["an older p0", "a p0", "a p3"]


def test_a_closed_ticket_is_not_in_the_open_list(log: Path) -> None:
    identifier, _ = filed(log)
    append(
        [TicketEvent(ticket=identifier, action="closed", at=T0, source="me", resolution="fixed")],
        log,
    )
    assert open_tickets(load(log)) == []


def test_an_event_round_trips_through_json(log: Path) -> None:
    """Mutation: drop a field from `to_json`'s loop — the round trip loses it."""
    identifier, _ = filed(log, detail="impact: a typo returns [] and looks like no data")
    event = next(e for e in read_events(log) if e.ticket == identifier)
    assert TicketEvent.from_json(event.to_json()) == event
    assert "impact" in event.detail


def test_a_naive_timestamp_is_refused(log: Path) -> None:
    with pytest.raises(TicketError, match="timezone-aware"):
        TicketEvent(
            ticket=ticket_id(TicketKind.FEATURE, "x", "a.py"),
            action="filed",
            at=datetime(2026, 9, 1),  # naive on purpose
            source="me",
            kind=TicketKind.FEATURE,
            title="x",
            path="a.py",
        )


def test_a_title_long_enough_to_be_an_argument_is_refused(log: Path) -> None:
    """The title is what appears in a list of forty. The argument goes in
    `detail`."""
    with pytest.raises(TicketError, match="characters"):
        filed(log, title="x" * 200)


# -- the generated debt file ------------------------------------------------------------------


def test_the_debt_file_is_generated_from_the_log_and_is_up_to_date() -> None:
    """`harness/TECHNICAL_DEBT.md` was hand-maintained and by 2026-09-04 had two
    items numbered 11 and two numbered 12 — which is what a table maintained by
    copying looks like after a fortnight. It is now a view of the ticket log.

    Mutation: edit a title in the generated file by hand — red, which is the
    point: the file is not the record."""
    import importlib.util
    import sys as _sys

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "ticket_cli_under_test", root / "scripts" / "ticket.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    generated = module.render_debt(load(root / "harness" / "tickets" / "events.jsonl"))
    on_disk = (root / "harness" / "TECHNICAL_DEBT.md").read_text(encoding="utf-8")

    def without_stamp(body: str) -> str:
        return "\n".join(line for line in body.splitlines() if not line.startswith("Generated 2"))

    assert without_stamp(generated) == without_stamp(on_disk), (
        "harness/TECHNICAL_DEBT.md is out of date with harness/tickets/events.jsonl. "
        "Run `scripts/ticket.py debt`."
    )


def test_the_repositorys_own_ticket_log_is_readable_and_every_defect_reproduces() -> None:
    """The log this repository actually carries, checked by its own rules.

    Mutation: hand-edit a defect's reproduction out of `events.jsonl` — red."""
    root = Path(__file__).resolve().parents[1]
    tickets = load(root / "harness" / "tickets" / "events.jsonl")
    assert tickets, "the ticket log is empty; the backfill did not land"
    for ticket in tickets.values():
        if ticket.kind is TicketKind.DEFECT:
            assert ticket.reproduction.strip(), f"{ticket.id} is a defect with no reproduction"
        assert ticket.sources, f"{ticket.id} has no source"


def test_every_agent_brief_tells_the_agent_to_file_what_it_finds() -> None:
    """A mechanism nobody is told to use is not a mechanism. Adding an agent
    without this line means its findings die with its transcript, exactly as
    they did before.

    Mutation: delete the block from one brief — red, naming the file."""
    root = Path(__file__).resolve().parents[1]
    briefs = sorted((root / ".claude" / "agents").glob("*.md"))
    assert len(briefs) >= 16, "the agent briefs moved"
    missing = [b.name for b in briefs if "scripts/ticket.py file" not in b.read_text()]
    assert missing == [], f"these briefs do not tell the agent to file findings: {missing}"


# -- what the guard-auditor found on 2026-09-04 ---------------------------------------------


def test_re_filing_a_closed_finding_reopens_it(log: Path) -> None:
    """The exact failure this mechanism exists to prevent, found in the
    mechanism itself. Filing onto a closed ticket used to leave it closed: the
    auditor was told its finding had been recorded, it appeared in no list and
    no generated table, and it died — with the CLI printing success.

    Someone re-finding a defect after a fix is the strongest evidence the fix
    was wrong.

    Mutation: drop the reopen branch from `fold`'s `filed` path — `is_open` is
    False, `open_tickets` is empty, and every assertion here goes red."""
    identifier, _ = filed(log)
    append(
        [
            TicketEvent(
                ticket=identifier,
                action="closed",
                at=T0 + timedelta(days=1),
                source="implementer",
                resolution="fixed in abc1234",
                commit="abc1234",
            )
        ],
        log,
    )
    assert not load(log)[identifier].is_open

    result = file_ticket(
        kind=TicketKind.DEFECT,
        severity=Severity.P0,
        title="read_bars returns [] for an unknown source",
        path="src/trading_platform/store/bars.py",
        source="guard-auditor",
        reproduction="still reproduces at HEAD",
        at=T0 + timedelta(days=5),
        log=log,
    )
    assert result.id == identifier
    assert result.previous_status is TicketStatus.CLOSED

    ticket = load(log)[identifier]
    assert ticket.is_open, "a re-discovery must reopen"
    assert ticket.reopens == 1
    assert ticket.closed_at is None and ticket.resolution == "" and ticket.commit == ""
    assert ticket.severity is Severity.P0
    assert any("re-filed" in note and "guard-auditor" in note for note in ticket.notes)
    assert [t.id for t in open_tickets(load(log))] == [identifier]


def test_re_filing_an_already_open_finding_does_not_count_as_a_reopen(log: Path) -> None:
    """Corroboration on an open ticket is not a reopen. Mutation: reopen
    unconditionally on a `filed` event — `reopens` becomes 1 and this goes
    red, and every corroborated ticket would look like a failed fix."""
    identifier, _ = filed(log)
    result = filed(log, source="lookahead-auditor")
    assert result == (identifier, False)
    ticket = load(log)[identifier]
    assert ticket.reopens == 0 and ticket.notes == ()


def test_a_read_failure_never_costs_the_finding(log: Path) -> None:
    """`file_ticket` used to read the whole log first, purely to compute a
    cosmetic flag, and a torn read from a concurrent writer aborted it before it
    had written anything. Measured: 1 to 4 of 8 concurrent writers lost their
    finding.

    Mutation: let the `TicketError` out of `file_ticket` instead of recording it
    on the result — nothing is appended and this goes red."""
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"ticket": "T-broken", "action": "fil\n', encoding="utf-8")

    result = file_ticket(
        kind=TicketKind.FEATURE,
        severity=Severity.P2,
        title="something worth having",
        path="x.py",
        source="an-agent",
        at=T0,
        log=log,
    )
    assert result.unreadable, "the read failure must be reported, not hidden"
    body = log.read_text(encoding="utf-8")
    assert result.id in body, "the finding must be on disk despite the unreadable log"


def test_concurrent_filers_all_land_and_the_log_stays_parseable(log: Path) -> None:
    """Several auditors file at once by design — the gauntlet runs five in
    parallel — and an event whose JSON exceeds the write buffer reaches the file
    in more than one syscall.

    Three things make this pass, and it is worth being exact about which does
    what rather than claiming one guard does all of it:

    - the exclusive `flock` serialises the read-then-append, so no reader sees a
      partial record in the first place;
    - `_write_record` puts each record on disk in ONE `os.write` to an O_APPEND
      descriptor, so the file stays parseable even without the lock;
    - `file_ticket` records a read failure instead of raising, so a torn read
      can never cost the finding it was about to write.

    Mutation: revert the third — let the `TicketError` out of `file_ticket` —
    and a writer aborts before appending. That is pinned independently by
    `test_a_read_failure_never_costs_the_finding`, which is the sharper test;
    this one is the realistic shape of it, six writers on one log."""
    import subprocess
    import sys as _sys
    import textwrap

    root = Path(__file__).resolve().parents[1]
    writer = log.parent / "writer.py"
    writer.write_text(
        textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(root / "src")!r})
        from datetime import UTC, datetime
        from trading_platform.tickets.store import file_ticket
        from trading_platform.tickets.model import Severity, TicketKind
        tag = sys.argv[1]
        for n in range(6):
            file_ticket(
                kind=TicketKind.FEATURE, severity=Severity.P3,
                title=f"{{tag}}-{{n}}", path="x.py", source=tag,
                detail="d" * 200_000,
                at=datetime(2026, 9, 1, tzinfo=UTC), log={str(log)!r},
            )
        """),
        encoding="utf-8",
    )
    procs = [
        subprocess.Popen([_sys.executable, str(writer), tag])
        for tag in ("a", "b", "c", "d", "e", "f")
    ]
    codes = [p.wait(timeout=120) for p in procs]
    assert codes == [0] * 6, f"a writer aborted: {codes}"

    tickets = load(log)  # must parse: a torn line would raise here
    assert len(tickets) == 36, f"expected 36 findings, found {len(tickets)}"


# -- the path is the identity (guard-auditor, 2026-09-04) ----------------------------------


def test_five_spellings_of_one_file_are_one_ticket(log: Path) -> None:
    """The id exists so that several auditors finding one thing file one ticket.
    A leading `./`, a doubled slash or the `:142` an auditor pastes out of a
    traceback each forked it silently — five ids for one finding, each agent
    told "filed", the corroboration lost.

    Mutation: drop the `_LINE_SUFFIX.sub` or the `./` strip from
    `normalise_path` — that spelling forks off its own ticket and the source
    count falls below five."""
    canonical = "src/trading_platform/tickets/store.py"
    spellings = [
        canonical,
        f"./{canonical}",
        f"{canonical}:142",
        f"  {canonical}  ",
        canonical.replace("/tickets/", "//tickets//"),
    ]
    for n, spelling in enumerate(spellings):
        filed(log, path=spelling, title="sync loses a row", source=f"auditor-{n}")

    known = load(log)
    assert len(known) == 1, f"one finding, {len(known)} tickets: {sorted(known)}"
    only = next(iter(known.values()))
    assert only.path == canonical, "the log keeps the canonical spelling, not the noisy one"
    assert len(only.sources) == 5


def test_normalise_path_keeps_a_trailing_slash(log: Path) -> None:
    """`harness/` means the directory. Stripping the slash would merge a finding
    about a directory with one about a file of the same name.

    Mutation: add a `rstrip('/')` to `normalise_path` — this goes red."""
    assert normalise_path("./harness//") == "harness/"
    identifier, _ = filed(log, kind=TicketKind.FEATURE, path="harness/", reproduction="")
    assert load(log)[identifier].path == "harness/"


@pytest.mark.parametrize(
    ("path", "because"),
    [
        ("", "needs a path"),
        ("   ", "needs a path"),
        ("/etc/passwd", "absolute"),
        ("C:\\keys\\id_rsa", "absolute"),
        ("../../etc/passwd", "escapes"),
        ("src/../../x.py", "escapes"),
        ("src/" + "x" * 600 + ".py", "over the"),
        ("src/a\nb.py", "line break"),
    ],
)
def test_a_path_that_cannot_be_a_repository_file_is_refused(
    log: Path, path: str, because: str
) -> None:
    """Empty, absolute, escaping, oversized and multi-line paths were all
    accepted. The oversized one is the sharp case: the log is append-only and
    `sync_to_store` truncates and re-inserts everything, so one path over the
    btree index limit made every later sync fail forever with no sanctioned way
    to remove the line.

    Mutation: delete any one branch of `_validate_path` — that row goes red."""
    with pytest.raises(TicketError, match=because):
        filed(log, path=path)


def test_a_title_with_a_line_break_is_refused(log: Path) -> None:
    """A newline in a title ends the markdown table in TECHNICAL_DEBT.md and
    every ticket below it disappears, while `debt --check` still says it is up
    to date because it compares generated text against generated text.

    Mutation: drop the `_validate_single_line` call for the title — red."""
    with pytest.raises(TicketError, match="line break"):
        filed(log, title="line one\nline two")


def test_the_oversized_path_limit_is_the_one_the_index_can_hold() -> None:
    """The bound is not arbitrary: `tickets_by_path` is a plain btree, capped at
    2704 bytes per index row, and the limit is counted in bytes because a path
    of non-ASCII characters is longer encoded than it looks.

    Mutation: count characters instead of bytes — a 400-character path of
    3-byte characters passes here and then wedges the projection."""
    assert PATH_MAX < 2704
    wide = "src/" + "\u4e2d" * (PATH_MAX // 2) + ".py"
    assert len(wide) <= PATH_MAX, "a character count would let this through"
    assert len(wide.encode("utf-8")) > PATH_MAX
    with pytest.raises(TicketError, match="bytes, over the"):
        TicketEvent(
            ticket=ticket_id(TicketKind.FEATURE, "x", wide),
            action="filed",
            at=T0,
            source="me",
            kind=TicketKind.FEATURE,
            severity=Severity.P3,
            title="x",
            path=wide,
        )


# -- duplicate, end to end (guard-auditor, 2026-09-04) -------------------------------------


def test_marking_a_duplicate_survives_a_round_trip_through_the_log(log: Path) -> None:
    """A whole CLI subcommand and a whole `fold` branch shipped with no test:
    dropping `duplicate_of` from `to_json` left the entire suite green, and it
    would have made `read_events` raise on the line the CLI had just written —
    the whole log unloadable, for every command.

    Mutation: drop `duplicate_of` from `to_json`'s field loop, or make the
    `duplicate` branch of `fold` leave the ticket OPEN — both go red here."""
    first, _ = filed(log, title="sync loses a row", path="src/a.py")
    second, _ = filed(log, title="the projection loses a row", path="src/a.py")
    append(
        [
            TicketEvent(
                ticket=second,
                action="duplicate",
                at=T0,
                source="triage",
                duplicate_of=first,
            )
        ],
        log,
    )

    reloaded = load(log)
    assert reloaded[second].status is TicketStatus.DUPLICATE
    assert reloaded[second].duplicate_of == first
    assert [t.id for t in open_tickets(reloaded)] == [first]


def test_marking_a_duplicate_needs_the_other_ticket(log: Path) -> None:
    """Mutation: drop the `duplicate_of` check from `__post_init__` — red."""
    identifier, _ = filed(log)
    with pytest.raises(TicketError, match="needs the other ticket"):
        TicketEvent(ticket=identifier, action="duplicate", at=T0, source="triage")


@pytest.mark.parametrize(
    "event",
    [
        {"action": "closed", "resolution": "fixed", "commit": "abc1234"},
        {"action": "wontfix", "resolution": "accepted, the cost is the recovery not the odds"},
        {"action": "reopened", "resolution": "the fix was wrong"},
        {"action": "duplicate", "duplicate_of": "T-00000000"},
        {"action": "note", "detail": "still true on 2026-09-05"},
    ],
)
def test_every_action_round_trips_through_json(event: dict[str, str]) -> None:
    """The original round-trip test built only a `filed` event, so `resolution`,
    `commit` and `duplicate_of` were reachable by no test at all.

    Mutation: drop any field from `to_json`'s loop — the row that carries it
    goes red."""
    built = TicketEvent(ticket="T-deadbeef", at=T0, source="me", **event)  # type: ignore[arg-type]
    assert TicketEvent.from_json(built.to_json()) == built


# -- the generated table (guard-auditor, 2026-09-04) ----------------------------------------


def _cli_module() -> object:
    import importlib.util
    import sys as _sys

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "ticket_cli_render_test", root / "scripts" / "ticket.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_pipe_in_any_cell_does_not_add_a_column(log: Path) -> None:
    """Titles and resolutions were escaped; `where` was not, so a path
    containing a pipe produced an 8-cell row in a 7-column table. The escaping
    was itself unguarded — no ticket in the real log contains a pipe, so
    dropping the escape left the whole suite green (mutation D01b).

    Mutation: drop `.replace('|', '\\\\|')` from `_cell` — every row below goes
    to the wrong column count and this goes red."""
    module = _cli_module()
    filed(log, title="a|b in the title", path="src/a|b.py", line=7)
    closed, _ = filed(log, title="closed with a pipe", path="src/c.py")
    append(
        [
            TicketEvent(
                ticket=closed,
                action="closed",
                at=T0,
                source="me",
                resolution="fixed | properly",
                commit="abc1234",
            )
        ],
        log,
    )

    rows = [
        line
        for line in module.render_debt(load(log)).splitlines()  # type: ignore[attr-defined]
        if line.startswith("| `T-")
    ]
    assert len(rows) == 2
    for row in rows:
        unescaped = re.sub(r"\\\|", "", row)
        assert unescaped.count("|") in (6, 8), f"row has the wrong column count: {row}"


def test_a_resolution_that_wraps_does_not_end_the_table(log: Path) -> None:
    """A `resolution` is free prose and may legitimately contain a newline; a
    newline in a rendered cell ends the markdown table, so every ticket below it
    vanishes from the generated file while `debt --check` still reports it up to
    date. Titles and paths are refused at the source; this is the renderer
    defending itself against the field that cannot be.

    Mutation: drop the `" ".join(value.split())` from `_cell` — the table ends
    early and the second row is missing."""
    module = _cli_module()
    first, _ = filed(log, title="closed with a wrapped reason", path="src/a.py")
    second, _ = filed(log, title="closed after it", path="src/b.py")
    for identifier in (first, second):
        append(
            [
                TicketEvent(
                    ticket=identifier,
                    action="closed",
                    at=T0,
                    source="me",
                    resolution="fixed\nand the reason wrapped onto a second line",
                )
            ],
            log,
        )

    body = module.render_debt(load(log))  # type: ignore[attr-defined]
    rows = [line for line in body.splitlines() if line.startswith("| `T-")]
    assert len(rows) == 2
    for row in rows:
        # Not "two rows are present": a broken row still *starts* on its own
        # line, and only its tail falls out of the table. The row has to be
        # whole — ending in the cell delimiter, carrying the whole resolution.
        assert row.rstrip().endswith("|"), f"row truncated by a line break: {row}"
        assert "wrapped onto a second line" in row
