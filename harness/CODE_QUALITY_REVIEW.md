# CODE_QUALITY_REVIEW

Findings in charter §119 format. Severity P0 dangerous · P1 significant · P2 worthwhile · P3 cosmetic.

## F-CQ-1 · P0 · VERIFIED · HIGH
FILE `src/trading_platform/risk/limits.py` FUNCTION `_reduces_exposure`, early return at the
size-cap exemption. PROBLEM: a single order flipping long→smaller short passes as
"reducing"; halted account accepted 13,000 notional past a 1,500 cap, zero rejections.
IMPACT: the halt — the most restrictive state — is bypassable in one order. RECOMMENDATION:
roadmap 9b; design choice required. VERIFICATION: the reproduction script in roadmap 9b.

## F-CQ-2 · P2 · VERIFIED · HIGH
FILE `examples/run_walk_forward.py`. PROBLEM: docstring and `MIXED` regimes reference the
pre-calibration generator ("TREND regime carries an embedded annualised Sharpe near 6");
generator was recalibrated 2026-08-20. IMPACT: a reader is told to expect a result that no
longer occurs. RECOMMENDATION: update the docstring to the calibrated figures (roadmap 33's
revised table). VERIFICATION: run it; compare printed efficiency to docstring.

## F-CQ-3 · P3 · VERIFIED · HIGH
FILE `pyproject.toml` `[project.optional-dependencies] ui`. PROBLEM: `fastapi`, `uvicorn`
never imported. IMPACT: implies a capability that does not exist. RECOMMENDATION: remove;
re-add as `api` when built.

## F-CQ-4 · P3 · VERIFIED · MEDIUM
DIRECTORY `src/trading_platform/ui/`. PROBLEM: named "ui", contains a run executor and a
`Verdict` type; no interface. IMPACT: misleads the charter's own FRONTEND_MAP reader.
RECOMMENDATION: rename to `runs/` or `verdicts/` when an actual UI layer appears; not
before (churn without benefit).

## F-CQ-5 · P2 · VERIFIED · HIGH
FILES `src/trading_platform/live/*.py`. PROBLEM: no `evals/` corpus entry covers the new
package. IMPACT: its defences are unmeasured — the exact state ADR 0006 found the rest of
the repo in. Deliberate (corpus holds only real defects); becomes a gap the first time a
real defect appears there. RECOMMENDATION: promote the first `live/` bug to a corpus entry.

## Structural properties — VERIFIED
- Largest files: `walkforward.py` 736, `vendor.py` 608, `synthetic.py` 590, `tearsheet.py`
  581, `panel.py` 541, `backtester.py` 530. None exceeds ~750 lines; no god object. `ui/
  runner.py` composes but computes no statistics (docstring, checked).
- Duplicated logic: none found by grep for repeated metric formulas; `summarise` is the sole
  metrics path (INFERRED — no dedicated duplication tool run).
- Global mutable state: none. Circular deps: none at package level.
- Error handling: consistent — `ValueError` at construction, domain exceptions (`OutOfOrder
  DataError`, `UnknownListing`, `CalendarUnknown`, `JournalError`, `DefectError`) named.
- Naming: consistent snake_case; docstrings explain *why*, unusually so.
- Test shape: ~1:1 lines source:tests; every module has a test file; 17 lookahead guards
  run first in CI.

Overall: **high**. The debt is in what is missing (versions, dataset ids, commit stamps on
results — §83–§86), not in what exists.
