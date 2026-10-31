"""Level 1 of the harness: does the test suite catch defects it claims to?

    .venv/bin/python scripts/run_mutation_eval.py            # whole corpus
    .venv/bin/python scripts/run_mutation_eval.py --targeted # only expected killers, fast
    .venv/bin/python scripts/run_mutation_eval.py --id L01 --id R01

Reintroduces each recorded defect into a throwaway copy of the tree and runs
pytest against it. The working tree is never modified.

**The control runs first and is not optional.** An unmutated tree must come
back SURVIVED — meaning the suite passed. If it does not, every KILLED below it
would be measuring a broken checkout rather than a defence, and the run aborts
rather than reporting a flattering number. This harness is subject to the rule
it exists to enforce: it must be able to report both outcomes, and it proves
that before claiming either.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.corpus import Defect, DefectError, load_corpus
from evals.mutate import PROJECT_DIR, materialise
from evals.suite import SuiteResult, run_suite


def _python() -> Path:
    candidate = PROJECT_DIR / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


def _one(defect: Defect | None, workdir: Path, targeted: bool) -> SuiteResult:
    name = defect.id if defect else "CONTROL"
    tree = materialise(defect, workdir / name)
    try:
        selection: tuple[str, ...] = ()
        if targeted and defect is not None:
            selection = defect.expected_killers
        return run_suite(tree, python=_python(), selection=selection)
    finally:
        tree.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", action="append", default=[], help="run only these defect ids")
    parser.add_argument(
        "--targeted",
        action="store_true",
        help="run only each defect's expected_killers — faster, and a weaker answer",
    )
    parser.add_argument("--jobs", type=int, default=3, help="mutants to run concurrently")
    parser.add_argument("--skip-control", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    try:
        corpus = load_corpus()
    except DefectError as exc:
        print(f"corpus is stale or malformed: {exc}", file=sys.stderr)
        return 2

    selected = [d for d in corpus if not args.id or d.id in args.id]
    if not selected:
        print(f"no defects matched {args.id}", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="mutation-eval-"))
    try:
        if not args.skip_control:
            print("control (unmutated tree) ... ", end="", flush=True)
            control = _one(None, workdir, targeted=False)
            print(f"{control.outcome} in {control.duration_s:.0f}s")
            if control.outcome != "SURVIVED":
                print(
                    "\nABORTING: the suite does not pass on an unmutated tree, so every\n"
                    "result below would measure the checkout rather than the defences.\n"
                    f"{control.tail}",
                    file=sys.stderr,
                )
                return 2
            print()

        results: dict[str, SuiteResult] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {
                pool.submit(_one, defect, workdir, args.targeted): defect for defect in selected
            }
            for future in concurrent.futures.as_completed(futures):
                defect = futures[future]
                try:
                    results[defect.id] = future.result()
                except DefectError as exc:
                    print(f"{defect.id}: {exc}", file=sys.stderr)
                    results[defect.id] = SuiteResult(defect.id, "ERROR", None, 0.0, -1, str(exc))
                print(f"  {defect.id} {results[defect.id].outcome}", flush=True)

        return _report(selected, results, targeted=args.targeted)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _report(selected: list[Defect], results: dict[str, SuiteResult], *, targeted: bool) -> int:
    print()
    print("=" * 78)
    print("MUTATION EVAL — does the test suite catch defects this repo has already had?")
    if targeted:
        print("(targeted mode: only each defect's expected killers ran; SURVIVED is weak)")
    print("=" * 78)
    print(f"{'id':5} {'category':14} {'outcome':9} {'killed by':40}")
    print("-" * 78)

    unexpected_survivors: list[Defect] = []
    for defect in selected:
        result = results[defect.id]
        killer = result.killed_by or ""
        if len(killer) > 40:
            killer = "..." + killer[-37:]
        flag = ""
        if result.outcome == "SKIPPED":
            flag = "  <-- NOT MEASURED (the killer suite skipped)"
        elif result.outcome == "SURVIVED" and not defect.known_survivor:
            flag = "  <-- HOLE"
            unexpected_survivors.append(defect)
        elif result.outcome == "SURVIVED":
            flag = "  (declared)"
        print(f"{defect.id:5} {defect.category:14} {result.outcome:9} {killer:40}{flag}")

    killed = sum(1 for r in results.values() if r.outcome == "KILLED")
    survived = sum(1 for r in results.values() if r.outcome == "SURVIVED")
    errored = sum(1 for r in results.values() if r.outcome == "ERROR")
    skipped = sum(1 for r in results.values() if r.outcome == "SKIPPED")
    total = len(selected)

    print("-" * 78)
    line = f"killed {killed}/{total}   survived {survived}   error {errored}"
    if skipped:
        line += f"   NOT MEASURED {skipped}"
    print(line)
    if skipped:
        print(
            f"\n{skipped} defect(s) were NOT MEASURED: their killer suite skipped every "
            "test, so the mutation never met anything. That is not a hole in the suite, "
            "it is a gap in this run — usually a database that is not up. "
            'Start one (scripts/dev_postgres.sh start && eval "$(scripts/dev_postgres.sh env)") '
            "and run again before quoting a rate."
        )

    # `-x` stops at the first failure in collection order, so `killed_by` names
    # ONE killer and never the only one. A defect caught by an earlier file than
    # its expected killer says the defect is coarser than the corpus claims — it
    # is NOT evidence that the expected killer missed it, and the note must not
    # be read that way. Re-run with --targeted to ask that question directly.
    for defect in selected:
        result = results[defect.id]
        if (
            result.outcome == "KILLED"
            and defect.expected_killers
            and result.killed_by
            and not any(result.killed_by.startswith(k) for k in defect.expected_killers)
        ):
            print(
                f"note: {defect.id} was caught first by {result.killed_by}, "
                f"before the expected {list(defect.expected_killers)} was reached. "
                "Collection order, not a coverage gap."
            )

    if unexpected_survivors:
        print()
        print("UNDECLARED HOLES — the suite claims these properties and does not check them:")
        for defect in unexpected_survivors:
            print(f"  {defect.id}  {defect.title}")
            print(f"        {defect.tell.strip().splitlines()[0]}")
        return 1

    if errored:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
