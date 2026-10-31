# experiments

Charter §42 records, one TOML per experiment, written by `scripts/experiment.py` **before the
run**. Fields: id · name · hypothesis (with what would disprove it) · motivation · dataset ·
dataset_version · strategy · strategy_version · commit · parameters · date_range · method ·
result · metrics · confidence · limitations · status · next_step · created · trial_count.

Statuses: IDEA QUEUED RUNNING COMPLETED FAILED REJECTED PROMISING VALIDATED RETIRED.

Rules the validator enforces: an unrun record has no result; a finished record has one and a
confidence; a result once committed is frozen (`validate --against-git`); ids are unique and
match the filename. A changed fee, dataset, or parameter is a **new** record. Nothing here is
deleted — a REJECTED record is the most useful kind.

E-001 to E-012 were backfilled 2026-09-01 from `harness/EXPERIMENT_INDEX.md` and carry
`backfilled = true`; their `commit` and `dataset_version` are `"UNKNOWN"` where the roadmap
did not record them, and their confidence is `"unknown"` for the same reason.
