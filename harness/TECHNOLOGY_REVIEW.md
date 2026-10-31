# TECHNOLOGY_REVIEW

Charter §9 format per technology. Every CURRENT line is **VERIFIED** from `pyproject.toml` and imports; every RECOMMENDATION is **PROPOSED**; CONFIDENCE reflects the evidence behind the recommendation, not opinion.

### Python 3.11 — KEEP
CURRENT: sole language; `requires-python >=3.11`. ALTERNATIVES: Rust/Go for the engine.
TRADEOFFS: Python is slow per-bar but the measured bottleneck was algorithmic (O(positions)
mark → O(1), 94% of runtime, roadmap), not the interpreter. MIGRATION COST: total rewrite.
BENEFIT: none demonstrated. RECOMMENDATION: keep. CONFIDENCE: HIGH.

### `Decimal` for money — KEEP, DO NOT CHANGE
CURRENT: all cash/prices/quantities in `core/engine/risk`; float only in `analytics/metrics.py`.
ALTERNATIVES: float, fixed-point int. TRADEOFFS: slower; exact. The roadmap records a 1.9e-25
incremental-vs-fresh gap — 23 orders of magnitude below a cent. MIGRATION: would reintroduce
the reconciliation error the rule exists to prevent. RECOMMENDATION: keep. CONFIDENCE: HIGH.

### `pyarrow` / Parquet — KEEP
CURRENT: only runtime dependency; prices as strings for exact round-trip. ALTERNATIVES:
SQLite, DuckDB, CSV. TRADEOFFS: no append (`write_bars` overwrites — why `live/recorder`
splits journal/compact); excellent columnar read (decode 4.60 µs/bar measured). DuckDB would
add analytical SQL at the cost of a second engine over the same files. RECOMMENDATION: keep;
revisit only if cross-symbol analytical queries outgrow `data/panel.py`. CONFIDENCE: HIGH.

### No pandas / numpy — KEEP (a decision, not an omission)
CURRENT: removed deliberately; `lookahead-auditor` treats their reappearance as a finding.
WHY: `.rolling()`/`.shift(-n)` make precomputed-indicator lookahead a one-liner; float
pushes money toward the boundary the house rules protect. `pyarrow.compute` does the
cross-section within 2x of numpy (measured). COST OF ADDING: reintroduces the leak surface;
BENEFIT: convenience. RECOMMENDATION: keep out of `core/engine/risk/strategy`; if ML (P8)
ever lands, confine numpy to `analytics/` behind the typed window split. CONFIDENCE: HIGH.

### Hand-written SVG tearsheets — KEEP
CURRENT: `analytics/charts.py`, 370 lines, no matplotlib. WHY: a PNG can only be asserted to
exist; an SVG string is tested for OOS shading, benchmark series, known coordinates.
ALTERNATIVES: matplotlib/plotly. MIGRATION: cheap. BENEFIT: none until a frontend exists.
RECOMMENDATION: keep; a P6 frontend would render the same data, not these files. CONFIDENCE: MEDIUM.

### `fastapi` / `uvicorn` (declared, unused) — REMOVE or rename
CURRENT: optional `ui` extra, zero imports. RECOMMENDATION: remove the extra now (P3) and
re-add as `api` when P6/P10 starts. CONFIDENCE: HIGH.

### `uv`, `ruff`, `mypy --strict`, `pytest` — KEEP
All wired into CI in the order `CLAUDE.md` tells a human to run them; every CI failure is
reproducible locally. `mypy --strict` is load-bearing: `TrainWindow`/`TestWindow` make
fitting on test data a type error. CONFIDENCE: HIGH.

### Storage / DB / cache / queue / containers / monitoring — NONE, correctly
Workload is one user, one process, ~10 MB. §10 argues against distributed infrastructure
that need not exist. RECOMMENDATION: none until the VPS supervisor (P2/P4) needs a systemd
unit and a health file — not a container, not a queue. CONFIDENCE: HIGH for now; revisit at
>1 writer or a frontend.

### Claude Code as the agent framework — KEEP, it is measured
8 agents, 8 commands, 2 workflows, 5 hooks, and `evals/` measuring the agents themselves
(8/8 detection, controls held, $38). No other framework offers that here. CONFIDENCE: HIGH.
