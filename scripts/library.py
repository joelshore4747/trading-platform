"""The document library: index it, file into it, search it.

    .venv/bin/python scripts/library.py index          # regenerate MANIFEST.md
    .venv/bin/python scripts/library.py search "order block"
    .venv/bin/python scripts/library.py file inbox/paper.pdf --subject ict
    .venv/bin/python scripts/library.py new ict-futures --title "ICT on index futures"
    .venv/bin/python scripts/library.py check           # fail if MANIFEST is stale

**Files, not a vector database.** Grep is an excellent search engine over a few
hundred documents and it has no failure modes — no embedding drift, no index to
rebuild, no similarity threshold, and nothing that can return the wrong passage
with high confidence. When this outgrows grep, that is the moment to reconsider.

**Nothing is deleted.** Filing MOVES a document out of the inbox into a subject's
`sources/`; it does not copy and it does not remove. A document that turns out to
be wrong is annotated in the subject's NOTES.md, not thrown away — the same rule
the rest of this repository follows about retractions.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "library"
MANIFEST = LIBRARY / "MANIFEST.md"
INBOX = LIBRARY / "inbox"
SUBJECTS = LIBRARY / "subjects"

#: Read by an agent before anything else, so it has to stay short enough to read.
MANIFEST_BUDGET_LINES = 200

SUBJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def subject_dirs() -> list[Path]:
    if not SUBJECTS.is_dir():
        return []
    return sorted(p for p in SUBJECTS.iterdir() if p.is_dir())


def documents(subject: Path) -> list[Path]:
    sources = subject / "sources"
    if not sources.is_dir():
        return []
    return sorted(p for p in sources.rglob("*") if p.is_file() and p.name != ".gitkeep")


def first_line(notes: Path) -> str:
    if not notes.is_file():
        return "**no NOTES.md** — a subject with sources and no notes is a pile of files"
    for line in notes.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("**Status") or (stripped and not stripped.startswith("#")):
            return stripped
    return "(no summary line)"


def added_on(path: Path) -> str:
    """When the file entered the repository, from git; mtime is a fallback.

    Git because a checkout rewrites every mtime, and "added 2026-09-04" on every
    document in a fresh clone would be a manifest that lies about its history.
    """
    try:
        out = subprocess.run(
            [
                "git",
                "log",
                "--diff-filter=A",
                "--format=%ad",
                "--date=short",
                "-1",
                "--",
                str(path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        stamp = out.stdout.strip()
        if stamp:
            return stamp
    except (subprocess.SubprocessError, OSError):
        pass
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).strftime("%Y-%m-%d")


def render_manifest() -> str:
    lines = [
        "# MANIFEST — read this before reading anything else in the library",
        "",
        "**Generated. Do not edit.** `scripts/library.py index` rewrites it.",
        "",
        "One screen, on purpose. An agent that loads the whole library runs out of",
        "context before it starts; this says what exists so it can read only what it",
        "needs. Every claim in a subject's NOTES.md carries a status (VERIFIED,",
        "INFERRED, UNKNOWN, PROPOSED) and a link to its evidence — an unattributed",
        "assertion is worse than nothing, because the next session will believe it.",
        "",
    ]
    subjects = subject_dirs()
    if not subjects:
        lines += ["No subjects yet. `scripts/library.py new <name>` starts one.", ""]
    else:
        lines += ["| subject | notes | documents |", "|---|---|---|"]
        for subject in subjects:
            docs = documents(subject)
            summary = first_line(subject / "NOTES.md").replace("|", "\\|")
            if len(summary) > 130:
                summary = summary[:127] + "..."
            lines.append(
                f"| [`{subject.name}`]({subject.relative_to(LIBRARY)}/NOTES.md) | "
                f"{summary} | {len(docs)} |"
            )
        lines.append("")
        for subject in subjects:
            docs = documents(subject)
            if not docs:
                continue
            lines += [f"### {subject.name}", ""]
            for doc in docs:
                lines.append(f"- `{doc.relative_to(LIBRARY)}` — added {added_on(doc)}")
            lines.append("")

    waiting = (
        [p for p in INBOX.iterdir() if p.is_file() and p.name != ".gitkeep"]
        if INBOX.is_dir()
        else []
    )
    if waiting:
        lines += [
            f"## Inbox — {len(waiting)} document(s) not yet filed",
            "",
            "Unfiled means nobody has said what subject they belong to, so nobody can",
            "find them. `scripts/library.py file <path> --subject <name>`.",
            "",
        ]
        lines += [f"- `inbox/{p.name}` — added {added_on(p)}" for p in sorted(waiting)]
        lines.append("")

    lines += [
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC.",
        "",
    ]
    return "\n".join(lines)


def _without_stamp(body: str) -> str:
    return "\n".join(line for line in body.splitlines() if not line.startswith("Generated 2"))


def cmd_index(args: argparse.Namespace) -> int:
    text = render_manifest()
    count = len(text.splitlines())
    if count > MANIFEST_BUDGET_LINES:
        print(
            f"warning: MANIFEST is {count} lines, over the {MANIFEST_BUDGET_LINES}-line "
            "budget. It is read first by every agent; if it no longer fits on a screen "
            "it has stopped doing its job and the subjects need consolidating.",
            file=sys.stderr,
        )
    MANIFEST.write_text(text, encoding="utf-8")
    print(f"wrote {MANIFEST.relative_to(ROOT)} — {len(subject_dirs())} subject(s)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    current = MANIFEST.read_text(encoding="utf-8") if MANIFEST.exists() else ""
    if _without_stamp(current) != _without_stamp(render_manifest()):
        print(
            f"{MANIFEST.relative_to(ROOT)} is stale. Run `scripts/library.py index`.",
            file=sys.stderr,
        )
        return 1
    print(f"{MANIFEST.relative_to(ROOT)} matches the library")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    if not SUBJECT_NAME.match(args.name):
        raise SystemExit(
            f"subject name {args.name!r} must be lower case letters, digits and hyphens — "
            "it is a directory name and a grep target"
        )
    subject = SUBJECTS / args.name
    if subject.exists():
        raise SystemExit(f"{subject.relative_to(ROOT)} already exists")
    (subject / "sources").mkdir(parents=True)
    (subject / "sources" / ".gitkeep").touch()
    (subject / "NOTES.md").write_text(
        f"# {args.title or args.name}\n\n"
        "**Status: nothing established yet.**\n\n"
        "## What we know\n\n"
        "Nothing. Every claim added here carries a status — VERIFIED, INFERRED,\n"
        "UNKNOWN or PROPOSED (charter §2) — and a link to its evidence. A library of\n"
        "unattributed assertions is worse than no library, because the next session\n"
        "will believe it.\n\n"
        "## Open questions — UNKNOWN\n\n"
        "## Sources\n\n"
        "None filed yet.\n",
        encoding="utf-8",
    )
    print(f"created {subject.relative_to(ROOT)}")
    return cmd_index(args)


def cmd_file(args: argparse.Namespace) -> int:
    source = Path(args.path)
    if not source.is_absolute():
        source = ROOT / source
    if not source.is_file():
        raise SystemExit(f"no document at {source}")
    subject = SUBJECTS / args.subject
    if not subject.is_dir():
        known = [p.name for p in subject_dirs()]
        raise SystemExit(
            f"no subject {args.subject!r}; known: {known}. "
            f"`scripts/library.py new {args.subject}` to start one."
        )
    target = subject / "sources" / source.name
    if target.exists():
        raise SystemExit(
            f"{target.relative_to(ROOT)} already exists. Nothing here overwrites a "
            "document; rename the new one if it is genuinely different."
        )
    (subject / "sources").mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    print(f"filed {target.relative_to(ROOT)}")
    print(
        f"  now say what it is worth in {(subject / 'NOTES.md').relative_to(ROOT)} — "
        "a source nobody has read is a file, not knowledge"
    )
    return cmd_index(args)


def cmd_search(args: argparse.Namespace) -> int:
    """Grep, with the subject named. No index, nothing to rebuild, no threshold."""
    try:
        out = subprocess.run(
            [
                "grep",
                "-rniI",
                "--include=*.md",
                "--include=*.txt",
                "--include=*.csv",
                args.query,
                str(LIBRARY),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise SystemExit(f"grep failed: {exc}") from None
    hits = [line for line in out.stdout.splitlines() if line.strip()]
    if not hits:
        print(
            f"nothing matches {args.query!r}. That is a claim about the library, not "
            "about the world — and an empty library is the usual reason."
        )
        return 0
    for line in hits[: args.limit]:
        path, _, rest = line.partition(":")
        try:
            shown = Path(path).relative_to(LIBRARY)
        except ValueError:
            shown = Path(path)
        print(f"{shown}: {rest.strip()[:160]}")
    if len(hits) > args.limit:
        print(f"\n... and {len(hits) - args.limit} more. Narrow the query or raise --limit.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    subs = parser.add_subparsers(dest="command", required=True)

    index = subs.add_parser("index", help="regenerate MANIFEST.md")
    index.set_defaults(func=cmd_index)

    check = subs.add_parser("check", help="fail if MANIFEST.md is stale")
    check.set_defaults(func=cmd_check)

    new = subs.add_parser("new", help="start a subject")
    new.add_argument("name")
    new.add_argument("--title", default="")
    new.set_defaults(func=cmd_new)

    filed = subs.add_parser("file", help="move a document into a subject")
    filed.add_argument("path")
    filed.add_argument("--subject", required=True)
    filed.set_defaults(func=cmd_file)

    search = subs.add_parser("search", help="grep the library")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(func=cmd_search)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
