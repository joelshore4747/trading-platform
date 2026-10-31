---
name: library
description: Read, search and add to the document library in library/ — what this repository knows about a subject and where it came from. Use before researching a subject from scratch, and after learning something worth the next session knowing.
---

<!-- charter §65 shape -->
**purpose** — stop each session relearning what the last one established, and stop a document sitting unread in an inbox.
**when_to_use** — before researching any subject; after any experiment whose result a later session would want; when the user drops a file into `library/inbox/`.
**inputs** — `library/MANIFEST.md` first, then the one subject you need.
**tools** — `scripts/library.py` (`index`, `search`, `new`, `file`, `check`).
**validation** — every claim added carries a status and a link; `scripts/library.py check` passes.
**outputs** — an updated `NOTES.md`, filed sources, a regenerated `MANIFEST.md`.

## Reading

**Read `library/MANIFEST.md` first and nothing else.** It is one screen and it
says what exists. Then read only the subject you need. Loading the whole library
is how a session runs out of context before it starts.

```bash
.venv/bin/python scripts/library.py search "order block"
```

Grep, deliberately — no embeddings, no index to rebuild, no similarity threshold,
and nothing that can return the wrong passage with high confidence.

## Writing

**Every claim carries a status** — VERIFIED, INFERRED, UNKNOWN or PROPOSED
(charter §2) — **and a link to its evidence**: an experiment record, a test, a
measurement. A library of unattributed assertions is worse than none, because the
next session will believe it.

**Record rejections at least as prominently as successes.** A note saying "this
was tried, here is the record, it failed and here is why" saves a later session
the whole cost of trying it again, and it is the thing a library like this is
uniquely good for. `library/subjects/ict/NOTES.md` is the shape.

**Say what a result does NOT establish.** "Rejected on one instrument over 32
days" is a different claim from "refuted", and eliding the difference is how a
library becomes a source of false confidence.

## Filing what a person dropped in

```bash
.venv/bin/python scripts/library.py file library/inbox/whatever.pdf --subject ict
.venv/bin/python scripts/library.py index
```

Filing **moves** the file; nothing here copies and nothing deletes. Then say in
that subject's `NOTES.md` what the document is worth — a source nobody has read
is a file, not knowledge. If no subject fits, `scripts/library.py new <name>`.
