---
name: experiment-design
description: Write a charter §42 experiment record before running anything — hypothesis, disproof, dataset, method, baselines — and file it in harness/experiments/. Use before every backtest or paper run whose number might inform a decision.
---

# Experiment design (charter §42)

## purpose
No number without a hypothesis stated first. The record exists before the run so
the result cannot rewrite the question. Records live in `harness/experiments/`
as TOML, one file per experiment, append-only.

## when_to_use
Before any run of `Backtester`, the walk-forward harness, a sweep, the null
harness, or a paper session, when the outcome could change what gets built,
kept, or believed. Every parameter tweak during a review loop is a new experiment.

## inputs
The idea in one sentence; the strategy version (git commit of the strategy file);
the dataset id and window; the cost preset; the risk limits; the trial count so
far against this data.

## procedure
1. Allocate the next id: `E-NNN` (see `harness/EXPERIMENT_INDEX.md`).
2. Fill the record: `id`, `name`, `hypothesis`, `motivation`, `dataset`,
   `strategy` (name + version + commit), `parameters`, `date_range`, `method`
   (walk-forward folds, null trials, cost sweep), `result` (empty), `metrics`
   (which ones, and `periods_per_year`), `confidence` (empty), `limitations`
   (known before running: coverage, window length, single venue), `status = "QUEUED"`,
   `next_step`.
3. **State what would disprove it** in `hypothesis` — the observation that would
   make you stop. If you cannot, the experiment is not ready.
4. Name the baselines: buy-and-hold, random control (`analytics/null.py`), the
   simplest existing strategy.
5. Record the trial count. Deflate the acceptance threshold for it
   (`analytics/multiplicity.py`).
6. Run. Then, and only then, fill `result`, `metrics`, `confidence`, `status`
   (COMPLETED → REJECTED / PROMISING / VALIDATED / FAILED) and `next_step`.

## tools
`scripts/experiment.py` (create, validate, list), `harness/experiments/`,
`analytics/multiplicity.py`, `analytics/null.py`.

## validation
`scripts/experiment.py validate` passes: every required field present, status is
one of the nine, a COMPLETED record has a non-empty result, the file was never
rewritten to change a filled `result` (the validator compares against git).

## outputs
`harness/experiments/E-NNN-<slug>.toml`; a line in `harness/EXPERIMENT_INDEX.md`.

## failure_modes
Writing the hypothesis after seeing the result. Omitting the trial count.
Recording a Sharpe without `periods_per_year`. Changing fees to make the record
look better (a new experiment, never an edit). Deleting a REJECTED record.
