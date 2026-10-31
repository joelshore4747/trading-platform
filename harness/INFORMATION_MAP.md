# INFORMATION_MAP

Charter §18–§20: fundamentals, news, macro, government data, entity resolution.

**Classification: VERIFIED ABSENT for every item.** No ingestion, storage, entity model or
relationship graph exists. `grep -rin "news\|fundamental\|earnings\|filing\|procurement"
src/` → only prose in docstrings.

What the platform *does* know about information timing, and why it matters for §27
event-aware backtesting:
- Bars are close-stamped and the engine fills on T+1 (ADR 0001). A news item stamped at
  publication time would slot into the same ordering guarantee unchanged.
- `live/session.py` knows when venues are shut, so an overnight release is not read as a
  one-bar move.
- Funding settlements are applied in `(prev_bar, bar]` after fills — the template for any
  timestamped external event.

The seam for §16 macro releases (GDP, CPI): an **event calendar** with publication time,
actual/expected/previous — PROPOSED, unbuilt. GDP is not a stream; it is a scheduled
release, and the previous plan noted this.

Priority: **P9, DEFERRED.** Condition: a strategy exists that a stated hypothesis says would
use the information — otherwise ingestion is cost with no consumer, the `Panel`-without-a-
reader mistake the roadmap already records once.
