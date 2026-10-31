# library — what this repository knows, and where it came from

A place to put documents so that a Claude session can find them, and a place for
what has been learned about a subject so that the next session does not learn it
again.

**Files, not a vector database.** Grep is an excellent search engine over a few
hundred documents and it has no failure modes: no embedding drift, no index to
rebuild, no similarity threshold to tune, and nothing that can silently return
the wrong passage with high confidence. When this outgrows grep — and it will
not do so at a few hundred files — that is the moment to reconsider, not before.

## Layout

```
library/
  MANIFEST.md          generated: every document, its subject, when it arrived
  inbox/               YOU drop things here. Anything: PDFs, notes, exports.
  subjects/<name>/
    NOTES.md           what we know, written by whoever read the sources
    sources/           the documents that support it
```

## For a person

Drop anything into `inbox/`. Then:

```bash
.venv/bin/python scripts/library.py index      # regenerate MANIFEST.md
.venv/bin/python scripts/library.py file <path> --subject ict   # move it into a subject
```

Nothing is deleted, ever. Filing moves a document out of the inbox into a
subject's `sources/`, and the manifest records when it arrived and where it went.

## For an agent

**Read `MANIFEST.md` first, not the whole library.** It is one screen and it says
what exists. Then read only the subject you need. Loading everything is how a
session runs out of context before it starts.

```bash
.venv/bin/python scripts/library.py search "order block"   # grep, with the subject named
```

## The rule that makes this worth having

**A claim in `NOTES.md` carries its status: VERIFIED, INFERRED, UNKNOWN or
PROPOSED (charter §2), and a link to the evidence.** A library of unattributed
assertions is worse than no library, because the next session will believe it.
Where this repository has actually tested something, the note says so and cites
the experiment record — and where a test *rejected* the idea, the note says that
too, prominently, because that is the most valuable thing a library like this can
hold.

## For a Claude session

There is a `library` skill (`.claude/skills/library/`) that says how to read and
write this without loading all of it. The short version is above: manifest first,
one subject after.
