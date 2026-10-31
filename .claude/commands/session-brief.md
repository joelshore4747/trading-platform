---
description: Load current project state into context cheaply at the start of a session.
allowed-tools: Bash(git status:*), Bash(git log:*), Bash(git diff:*), Bash(.venv/bin/python -m pytest:*), Read, Glob
---

# Session brief

Orient in this repository without reading the whole thing. The point is to
spend a small, predictable number of tokens now so the rest of the session
does not waste large ones rediscovering state.

## Gather

Run these and report the output compactly:

```bash
git log --oneline -8
git status --short
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

Then list the strategies present:

```bash
ls src/trading_platform/strategy/examples/
```

## Report — keep this under 20 lines

1. **Branch and recent work** — what the last few commits changed, in one line
   each.
2. **Working tree** — clean, or what is modified.
3. **Test status** — pass count, or the names of failures. If
   `tests/test_lookahead.py` has any failure, lead with that; nothing else
   matters until it is fixed.
4. **Strategies present** — names only.
5. **Anything obviously in progress** — a half-finished file, a TODO added
   recently, a test marked skip.

## Then stop

Do not read source files, do not propose work, do not summarise the
architecture — `CLAUDE.md` already covers it and re-deriving it every session
is exactly the token waste this command exists to prevent.

End by asking what I want to work on.
