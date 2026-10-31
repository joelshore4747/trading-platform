# ML_MAP — ABSENT

**Classification: VERIFIED ABSENT.**

Evidence: `grep -rln "sklearn\|torch\|tensorflow\|xgboost\|lightgbm\|keras" src/ tests/` →
nothing. No model, no feature store, no training script. `numpy` and `pandas` were
deliberately **removed** as dependencies (roadmap; `lookahead-auditor` brief §3) because
their whole-array idioms make precomputed-indicator lookahead a one-liner.

What exists that §23 would build on: the typed train/test split (`analytics/windows.py`,
`TrainWindow`/`TestWindow` — fitting on test data is a `mypy` error), the null harness
(`analytics/nulltest.py`, false-discovery rate measured at 1/100 on 4,800 OOS bars), and
multiplicity accounting (`analytics/multiplicity.py`, deflated Sharpe). These are the §23
leakage/overfitting checks, already built for strategies; a model would reuse them.

**P8, DEFERRED.** Condition: a feature that is predictive under the null harness at all,
which no hand-written signal here has yet cleared. Adding ML before that measures the
model's ability to overfit, not to predict.
