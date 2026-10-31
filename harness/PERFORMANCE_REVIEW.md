# PERFORMANCE_REVIEW

Charter §35/§97: measure, don't guess. Only measured figures appear; everything else is UNKNOWN.

| Component | Baseline → after | Measured how | Source |
|---|---|---|---|
| Portfolio mark per bar event | O(positions) → **O(1)**; was 94% of engine runtime; 9.04M property calls to service 400 fills | profiling, roadmap | `engine/portfolio.py` `_positions_total` |
| Cache decode | 8.30 → **4.60 µs/bar**; 1-month window 1.62x ideal (was 3.50x) | timed `read_bars` | roadmap "data plane" |
| Cross-section memory | `Bar` 664 B → panel **8.0 B/cell**; 5,000 × 1y daily = 14.6 MB vs 1.21 GB | RSS, roadmap | `data/panel.py` |
| Universe membership | `is_member` **~140 ns**, flat 10→10,000 listings | timed | `data/universe.py` |
| Real-data walk-forward (18 folds, 16 cells, 21k bars) | ~minutes (ran in background; not timed precisely) | wall clock | UNKNOWN precisely |
| Agent eval per Opus run | 2.5–13 min; $1.5–$9.4 | `claude -p` JSON | ADR 0006 |
| Full test suite (815) | ~90 s wall | pytest | 2026-09-01 |

## Not measured — UNKNOWN
Frontend (absent), API latency (absent), ingestion throughput on a live feed (no venue reachable),
`LiveFeed` throughput under load (single-thread test only, 50 bars), memory over a 24h
capture, `compact()` on a full day. **Recommendation:** instrument the supervisor (P2/P4)
with the §35 ingestion metrics before the first VPS run; do not optimise anything not yet
measured.

## Finding
**F-PERF-1 · P3 · INFERRED.** `JournalRecorder` `fsync`s every line (`journal.py`) and flushes
every bar (`recorder.py`). Correct for crash safety; cost unmeasured at 1-minute cadence on 40
symbols (~0.7 writes/s — almost certainly fine). Measure before changing.
