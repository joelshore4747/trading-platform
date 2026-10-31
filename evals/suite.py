"""Level 1 — run the test suite against a mutant and ask whether it notices.

A mutant the suite does not kill is a **guard that cannot fail**: a property
the tests claim to protect and demonstrably do not. That failure has occurred
four separate times in this repository's recorded history, every time found by
a human or an agent reading assertions, and every time after the green suite
had already been believed. This module finds them mechanically.

The distinction that makes the result usable is between a mutant that *fails to
be caught* and one that *fails to run*. A syntax error kills every mutant
trivially and would report a perfect score, so an import-time collapse is
classified as ``ERROR`` and never as ``KILLED``.

``-x`` is deliberate. The question is binary — did anything object — so the run
can stop at the first objection. Survivors pay for the whole suite, which is
the correct place to spend the time.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from evals.mutate import MutantTree

Outcome = Literal["KILLED", "SURVIVED", "ERROR", "SKIPPED"]

#: The first failing test id, e.g. `tests/test_lookahead.py::test_foo`.
_FAILURE = re.compile(r"^(FAILED|ERROR)\s+(\S+)", re.MULTILINE)

#: pytest's own exit codes, which are the authority here.
#:
#: 0 all passed, 1 tests failed, 2 interrupted (collection errors land here),
#: 3 internal error, 4 usage error, 5 nothing collected.
#:
#: Classified by return code rather than by scanning output for "ImportError".
#: A test that legitimately asserts on an import-error message would match such
#: Every selected test skipped, or none was selected at all. Either way the
#: mutation was never executed against anything.
_NOTHING_RAN = re.compile(r"(no tests ran|^\d+ skipped)", re.MULTILINE)


def _ran_nothing(stdout: str) -> bool:
    """True when pytest passed without executing a single test body.

    Reads the summary line, which is why `addopts` is overridden above: without
    that, `-q -q` hides the line and every all-skipped run looks like a pass.
    """
    summary = [
        line for line in stdout.splitlines() if _NOTHING_RAN.search(line) or " passed" in line
    ]
    if not summary:
        return "no tests ran" in stdout
    return " passed" not in summary[-1]


#: a pattern and silently turn a real kill into an ERROR — a defect that hides
#: a hole, in the module written to find holes.
_RC_PASSED = 0
_RC_FAILED = 1
_RC_BROKEN = frozenset({2, 3, 4, 5})


@dataclass(frozen=True, slots=True)
class SuiteResult:
    defect_id: str
    outcome: Outcome
    killed_by: str | None
    duration_s: float
    returncode: int
    tail: str

    @property
    def is_hole(self) -> bool:
        return self.outcome == "SURVIVED"


def run_suite(
    tree: MutantTree,
    *,
    python: Path,
    selection: tuple[str, ...] = (),
    timeout_s: float = 1200.0,
) -> SuiteResult:
    """Run pytest inside `tree` and classify the outcome.

    `selection` narrows to specific test paths. Narrowing makes the run faster
    and the answer weaker: it can only tell you whether *those* tests catch the
    defect, so a SURVIVED under selection is not evidence of a hole. The
    reporting layer is responsible for saying which mode produced a number.
    """
    label = tree.label
    args = [
        str(python),
        "-m",
        "pytest",
        # The project's own `addopts` is overridden, not inherited. It carries
        # `-q`, which combined with the `-q` below suppressed pytest's summary
        # line entirely — so this harness could not tell "24 skipped" from
        # "24 passed" and reported four skipped store defects as undeclared
        # holes in the suite (2026-09-04). A measurement harness that reads its
        # subject's configuration is a harness whose answer changes when
        # somebody edits that configuration.
        "-o",
        "addopts=--strict-markers",
        "-x",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "-m",
        "not slow",
        *selection,
    ]

    started = time.monotonic()
    try:
        proc = subprocess.run(
            args,
            cwd=tree.root,
            env=tree.env(),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        stdout, returncode = proc.stdout + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        return SuiteResult(label, "ERROR", None, time.monotonic() - started, -1, "timed out")

    duration = time.monotonic() - started
    tail = "\n".join(stdout.strip().splitlines()[-25:])

    # A mutant that cannot even be imported has not been *caught* — it has
    # crashed the harness. Scoring that as a kill would inflate every rate.
    if returncode in _RC_BROKEN:
        return SuiteResult(label, "ERROR", None, duration, returncode, tail)

    if returncode == _RC_PASSED:
        # A suite that ran nothing did not survive the mutation; it never met it.
        # Reporting that as SURVIVED turns "we did not check" into "the suite
        # does not cover this" — the opposite kind of dishonesty from the usual
        # one, and still dishonesty. It happened for real: four store-backed
        # defects were reported as undeclared holes because Postgres was not
        # running (2026-09-04).
        if _ran_nothing(stdout):
            return SuiteResult(label, "SKIPPED", None, duration, returncode, tail)
        return SuiteResult(label, "SURVIVED", None, duration, returncode, tail)

    if returncode != _RC_FAILED:  # pragma: no cover - pytest grew a new code
        return SuiteResult(label, "ERROR", None, duration, returncode, tail)

    match = _FAILURE.search(stdout)
    return SuiteResult(
        label,
        "KILLED",
        match.group(2) if match else None,
        duration,
        returncode,
        tail,
    )
