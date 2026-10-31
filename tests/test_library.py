"""The document library: a manifest that stays true, and notes that carry status.

The library is files and grep on purpose, so there is little machinery to test.
What is worth testing is the discipline: that the manifest matches what is on
disk, that a claim without a status cannot pass unnoticed, and that filing moves
rather than copies — because a library with two copies of a document and no
record of which is current is worse than no library.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "library"


def load() -> object:
    spec = importlib.util.spec_from_file_location(
        "library_cli_under_test", ROOT / "scripts" / "library.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


library = load()


def test_the_manifest_matches_what_is_on_disk() -> None:
    """It is read first by every agent, so a stale one sends a session to a
    subject that does not exist or hides one that does.

    Mutation: add a subject directory without running `index` — red."""
    assert library.cmd_check(None) == 0, "run `scripts/library.py index`"


def test_the_manifest_fits_on_a_screen() -> None:
    """The whole point is that an agent reads this instead of the library. Once
    it stops fitting it has stopped doing its job.

    Mutation: raise MANIFEST_BUDGET_LINES to something unbounded — this stops
    being a guard, which is the failure it exists to prevent."""
    lines = (LIBRARY / "MANIFEST.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= library.MANIFEST_BUDGET_LINES


def test_every_subject_has_notes_and_every_note_carries_a_status() -> None:
    """A claim with no status is an assertion the next session will believe.

    Mutation: delete the status line from a NOTES.md — red, naming the file."""
    statuses = ("VERIFIED", "INFERRED", "UNKNOWN", "PROPOSED", "REJECTED")
    for subject in library.subject_dirs():
        notes = subject / "NOTES.md"
        assert notes.is_file(), f"{subject.name} has sources and no NOTES.md"
        body = notes.read_text(encoding="utf-8")
        assert any(s in body for s in statuses), (
            f"{notes.relative_to(ROOT)} states no claim status. Every claim carries "
            f"one of {statuses} and a link to its evidence."
        )


def test_a_rejection_is_recorded_with_what_it_does_not_establish() -> None:
    """The most valuable thing a library like this holds is "we tried that and
    here is the record". It is also the easiest thing to overstate, so the note
    has to say what the result does NOT establish.

    Mutation: delete the "does not refute ICT in general" paragraph — red, and
    the library becomes a source of false confidence."""
    notes = (LIBRARY / "subjects" / "ict" / "NOTES.md").read_text(encoding="utf-8")
    assert "REJECTED" in notes
    assert "E-016" in notes, "a rejection must cite its experiment record"
    assert "does not refute ICT in general" in notes


def test_filing_moves_a_document_and_never_overwrites(tmp_path: Path) -> None:
    """Two copies with no record of which is current is worse than none.

    Mutation: `shutil.copy` instead of `shutil.move` — the inbox keeps a ghost
    and the manifest reports it as unfiled forever."""
    subject = LIBRARY / "subjects" / "ict"
    dropped = tmp_path / "note.md"
    dropped.write_text("a source", encoding="utf-8")

    import argparse

    args = argparse.Namespace(path=str(dropped), subject="ict")
    landed = subject / "sources" / "note.md"
    try:
        library.cmd_file(args)
        assert landed.is_file()
        assert not dropped.exists(), "filing must MOVE, not copy"

        again = tmp_path / "note.md"
        again.write_text("a different source with the same name", encoding="utf-8")
        with pytest.raises(SystemExit, match="already exists"):
            library.cmd_file(argparse.Namespace(path=str(again), subject="ict"))
    finally:
        landed.unlink(missing_ok=True)
        library.cmd_index(args)


def test_an_unknown_subject_is_refused_and_names_the_ones_that_exist(tmp_path: Path) -> None:
    import argparse

    dropped = tmp_path / "x.md"
    dropped.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match="no subject"):
        library.cmd_file(argparse.Namespace(path=str(dropped), subject="not-a-subject"))
    assert dropped.exists(), "a refused filing must leave the document where it was"


def test_a_subject_name_that_is_not_a_directory_name_is_refused() -> None:
    import argparse

    with pytest.raises(SystemExit, match="lower case"):
        library.cmd_new(argparse.Namespace(name="Not A Subject", title=""))


def test_the_skill_tells_an_agent_to_read_the_manifest_first() -> None:
    """The one instruction that keeps this cheap. Without it an agent greps the
    whole library into its context and the library becomes a liability."""
    skill = (ROOT / ".claude" / "skills" / "library" / "SKILL.md").read_text(encoding="utf-8")
    assert "MANIFEST.md" in skill
    assert "rejections" in skill.lower()
