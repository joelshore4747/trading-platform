---
name: data-quality-audit
description: The charter §73 checks on a dataset — missing, duplicate, future-stamped, gapped, impossible, stale, disagreeing — run and reported with counts before any backtest uses the data. Use on every new dataset, every compacted live day, and whenever a result looks too good.
---

# Data quality audit (charter §73)

## purpose
Make data problems visible as numbers before they become strategy "results".
A gap is a gap: this audit reports it; nothing here repairs it.

## when_to_use
A new vendor file or feed; a compacted day from `live/recorder`; a result that
improved without a code change; before any figure enters an experiment record.

## inputs
A Parquet file or JSONL journal; the expected interval; the venue calendar
(`live/session.py`) for anything with sessions.

## procedure
1. **Ordering and duplicates.** Timestamps non-decreasing; duplicate stamps with
   *disagreeing* values are an error, identical ones a reconnect.
2. **Future timestamps.** Nothing stamped after now, and nothing stamped before
   the file's own open-time plus interval (open-stamped data reads a bar early).
3. **Gaps.** Count missing intervals against the calendar. Report the count and
   the coverage fraction; the archive here covers 46% and is biased to 07–10 UTC.
4. **Impossible values.** `Bar.__post_init__` already rejects OHLC inconsistency;
   count how many rows it would reject. Zero or negative prices; negative volume.
5. **Outliers.** Returns beyond N sigma listed, not removed. Check the neighbours:
   a single-bar spike that reverts is a print; a level shift is a listing event.
6. **Stale feed.** For live capture: `StreamStats` late-bar count and the longest
   silence; marks older than one interval.
7. **Provider disagreement.** Where two sources cover the same bars, the count
   and size of disagreements. One source: UNKNOWN, say so.
8. **Schema.** Column names and types against the loader's expectation.

## tools
`data-validator` agent (cheap model; always run it); `data/panel.py`;
`live/recorder.py` (`compact` refuses disagreeing duplicates); `live/session.py`.

## validation
Every check produces a count, even when zero. Re-running the audit on the same
file gives the same counts. A check that cannot report a non-zero value on a
deliberately corrupted copy is a guard that cannot fail — fix the check.

## outputs
A table: check / count / classification (VERIFIED) / severity. Coverage fraction.
Named list of bars that would be rejected. Recommendation: USE / USE WITH STATED
LIMITATIONS / DO NOT USE.

## failure_modes
Forward-filling or interpolating to make the counts zero. Treating a session gap
in equities as missing data. Auditing the compacted copy instead of the raw
journal. Reporting "clean" without the counts that show it.
