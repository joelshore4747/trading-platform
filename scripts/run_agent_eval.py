"""Level 2 of the harness: does the auditor catch what it claims to?

    .venv/bin/python scripts/run_agent_eval.py --dry-run          # plan and cost, no calls
    .venv/bin/python scripts/run_agent_eval.py --id L01 --id S01
    .venv/bin/python scripts/run_agent_eval.py --agent lookahead-auditor --controls 2

Each defect is reintroduced into a throwaway tree and the agent named in the
corpus is run against it headlessly, with its own definition as the system
prompt — the artefact that runs in real sessions, unedited.

**This costs real money and runs no checks in CI.** Every invocation is a
frontier-model review of a repository. It is opt-in, bounded by ``--limit``,
and ``--dry-run`` prints the plan without calling anything.

Two arms, and the second is not optional:

*Mutants* measure the false-negative rate — what the auditor misses.
*Controls* are unmutated trees where the only correct answer is clean. An
auditor that flags a control is manufacturing findings, and one that does so
reliably is worse than useless: it trains its reader to ignore it.

``--blind`` (the default) removes ``docs/ROADMAP.md`` and ``docs/adr/`` from
the tree first. Most of this corpus is narrated there in detail, so without it
the run measures whether the auditor can *read about* the bug. Both numbers are
real; they answer different questions, and a report must say which it is.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.agent import (
    AGENT_TIMEOUT_S,
    AgentRun,
    Judgement,
    build_venv,
    judge,
    load_agent,
    prepare_for_agent,
    run_agent,
)
from evals.corpus import Defect, DefectError, load_corpus
from evals.mutate import materialise

TASK = (
    "Audit this repository for defects in your area of responsibility. "
    "Exactly one deliberate defect may have been introduced, or the code may be "
    "unmodified — both are possible and you are not told which. "
    "Report what you actually find, and end with your VERDICT line. "
    "A clean verdict on clean code is a correct and useful answer; do not "
    "manufacture findings to appear thorough."
)


NO_VENV_NOTE = (
    "\n\nNote: this checkout has no virtualenv, so you cannot run the test suite. "
    "Audit by reading. Say explicitly that your findings are unverified by execution."
)


@dataclass(slots=True)
class Scored:
    label: str
    defect: Defect | None
    run: AgentRun
    judgement: Judgement | None
    #: The judge's verdict on this same report against a DIFFERENT defect's
    #: tell. It should be "not identified" every time; see `_cross_judge`.
    cross: Judgement | None = None

    @property
    def is_control(self) -> bool:
        return self.defect is None


def _evaluate(
    defect: Defect | None,
    agent_name: str,
    workdir: Path,
    blind: bool,
    with_venv: bool,
    timeout_s: float,
) -> Scored:
    spec = load_agent(agent_name)
    label = defect.id if defect else f"CONTROL-{agent_name}"
    tree = materialise(defect, workdir / label.replace("/", "_"))
    try:
        prepare_for_agent(tree, blind=blind)
        # Recorded, not asserted: an auditor that could not run the suite is a
        # weaker measurement than one that could, and the report must be able
        # to say which happened rather than averaging the two silently.
        has_venv = build_venv(tree) if with_venv else False
        run = run_agent(
            spec,
            tree,
            task=TASK if has_venv else TASK + NO_VENV_NOTE,
            timeout_s=timeout_s,
        )
    finally:
        tree.cleanup()

    verdict = judge(run, defect) if (defect is not None and run.ok) else None
    return Scored(label=label, defect=defect, run=run, judgement=verdict)


def _cross_judge(scored: list[Scored], corpus: tuple[Defect, ...]) -> None:
    """Ask the judge to identify each report as a defect it does not describe.

    The judge decides every `identified` number in this report, and until now
    nothing decided the judge. A judge that answers yes to anything plausible
    would make the headline rate meaningless while looking excellent — the same
    failure as an auditor that flags everything, one level further out.

    Free of extra agent runs: it re-scores reports already produced, against a
    mismatched `tell`. Every answer here should be "not identified". Any that
    are not are printed, because a rubber-stamping judge invalidates the run
    rather than merely adding noise to it.
    """
    by_id = {d.id: d for d in corpus}
    ordered = [d.id for d in corpus]
    for item in scored:
        if item.defect is None or not item.run.ok:
            continue
        position = ordered.index(item.defect.id)
        other = by_id[ordered[(position + 1) % len(ordered)]]
        if other.id == item.defect.id:  # pragma: no cover - single-defect corpus
            continue
        item.cross = judge(item.run, other)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", action="append", default=[], help="run only these defect ids")
    parser.add_argument("--agent", action="append", default=[], help="run only these agents")
    parser.add_argument("--limit", type=int, default=0, help="cap the number of mutants")
    parser.add_argument("--controls", type=int, default=1, help="clean trees per agent involved")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument(
        "--sighted",
        action="store_true",
        help="leave ROADMAP.md and docs/adr in place — measures detection with memory",
    )
    parser.add_argument(
        "--no-venv",
        action="store_true",
        help="skip building a venv in each mutant — faster, and the agent cannot run tests",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=AGENT_TIMEOUT_S,
        help="seconds per agent run; a timeout scores as a failed run, never as clean",
    )
    parser.add_argument(
        "--no-cross-judge",
        action="store_true",
        help="skip re-scoring each report against a mismatched defect (the judge's own control)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    parser.add_argument("--out", type=Path, default=None, help="write full reports here")
    args = parser.parse_args()

    try:
        corpus = load_corpus()
    except DefectError as exc:
        print(f"corpus is stale or malformed: {exc}", file=sys.stderr)
        return 2

    selected = [d for d in corpus if not args.id or d.id in args.id]
    if args.agent:
        selected = [d for d in selected if d.expected_agent in args.agent]
    if args.limit:
        selected = selected[: args.limit]
    if not selected:
        print("no defects matched the filters", file=sys.stderr)
        return 2

    agents = sorted({d.expected_agent for d in selected})
    control_jobs = [(None, agent) for agent in agents for _ in range(max(0, args.controls))]
    mutant_jobs: list[tuple[Defect | None, str]] = [(d, d.expected_agent) for d in selected]
    jobs = mutant_jobs + control_jobs

    blind = not args.sighted
    print(f"{len(mutant_jobs)} mutant run(s), {len(control_jobs)} control run(s)")
    print(f"agents: {', '.join(agents)}")
    print(f"mode: {'BLIND (project bug history removed)' if blind else 'SIGHTED'}")
    for defect in selected:
        print(f"  {defect.id:5} -> {defect.expected_agent}")
    if args.dry_run:
        print("\ndry run: nothing was called")
        return 0

    workdir = Path(tempfile.mkdtemp(prefix="agent-eval-"))
    scored: list[Scored] = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [
                pool.submit(
                    _evaluate, defect, agent, workdir, blind, not args.no_venv, args.timeout
                )
                for defect, agent in jobs
            ]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                scored.append(result)
                mark = "ok" if result.run.ok else "FAILED"
                print(f"  {result.label:22} {mark}  ${result.run.cost_usd:.3f}", flush=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if not args.no_cross_judge:
        print("\ncross-judging (the judge's own control) ...", flush=True)
        _cross_judge(scored, corpus)

    if args.out:
        _dump(scored, args.out)
    return _report(scored, blind=blind)


def _dump(scored: list[Scored], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for item in scored:
        (out / f"{item.label}.md").write_text(
            f"# {item.label} ({item.run.agent})\n\n"
            f"verdict: {item.run.verdict or '(none)'}\n"
            f"cost: ${item.run.cost_usd:.3f}  duration: {item.run.duration_s:.0f}s\n\n"
            + (
                f"judge: {item.judgement.identified} — {item.judgement.why}\n\n"
                if item.judgement
                else ""
            )
            + "---\n\n"
            + item.run.report,
            encoding="utf-8",
        )
    print(f"\nfull reports written to {out}")


def _report(scored: list[Scored], *, blind: bool) -> int:
    mutants = [s for s in scored if not s.is_control]
    controls = [s for s in scored if s.is_control]

    print()
    print("=" * 78)
    print("AGENT EVAL — what do the auditors actually catch?")
    print(f"mode: {'BLIND' if blind else 'SIGHTED'}")
    print("=" * 78)
    print(f"{'id':7} {'agent':20} {'flag':5} {'local':6} {'ident':6} {'cost':>7}")
    print("-" * 78)

    for item in sorted(mutants, key=lambda s: s.label):
        assert item.defect is not None
        localised = item.run.localised(item.defect.files)
        identified = bool(item.judgement and item.judgement.identified)
        print(
            f"{item.label:7} {item.run.agent:20} "
            f"{'yes' if item.run.flagged else 'NO':5} "
            f"{'yes' if localised else 'no':6} "
            f"{'YES' if identified else 'no':6} "
            f"${item.run.cost_usd:6.3f}"
        )

    print("-" * 78)
    for item in sorted(controls, key=lambda s: s.label):
        if not item.run.ok:
            state = "FAILED (no data)"
        elif item.run.flagged:
            state = "flagged — ADJUDICATE"
        else:
            state = "clean"
        print(f"{item.label:28} {state:20} ${item.run.cost_usd:6.3f}")

    n = len(mutants)
    n_identified = sum(1 for s in mutants if s.judgement and s.judgement.identified)
    n_localised = sum(1 for s in mutants if s.defect and s.run.localised(s.defect.files))
    n_flagged = sum(1 for s in mutants if s.run.flagged)
    n_failed = sum(1 for s in scored if not s.run.ok)
    control_flags = sum(1 for s in controls if s.run.ok and s.run.flagged)
    controls_ok = sum(1 for s in controls if s.run.ok)
    total_cost = sum(s.run.cost_usd for s in scored) + sum(
        s.judgement.cost_usd for s in scored if s.judgement
    )

    print("-" * 78)
    if n:
        print(
            f"identified {n_identified}/{n} ({n_identified / n:.0%})   "
            f"localised {n_localised}/{n}   flagged {n_flagged}/{n}"
        )
    if controls:
        # NOT a false-positive rate, and calling it one was this harness's own
        # measurement bug. A control tree is not clean code — it is the real
        # repository, which contains real defects. The first run labelled two
        # controls "FALSE POSITIVE"; both were genuine findings, one of them a
        # confirmed fake guard in tests/test_tearsheet.py and one a live
        # single-order flip through the halt. A flagged control needs
        # adjudicating, and only an adjudicated one is a false positive.
        print(f"controls flagged {control_flags}/{controls_ok} — read them before scoring")
    if n_failed:
        print(f"FAILED runs {n_failed}/{len(scored)} — no data, not a clean verdict")

    # The judge's control. Every one of these should be "no": the report is
    # being scored against a defect it does not describe. A non-zero count here
    # does not add noise to the rate above — it invalidates it.
    crossed = [s for s in mutants if s.cross is not None]
    if crossed:
        rubber = [s for s in crossed if s.cross and s.cross.identified]
        print(f"cross-judge {len(rubber)}/{len(crossed)} mismatched reports wrongly identified")
        for item in rubber:
            print(f"  JUDGE RUBBER-STAMPED {item.label}: {item.cross.why if item.cross else ''}")
    print(f"total cost ${total_cost:.2f}")

    print()
    for item in sorted(mutants, key=lambda s: s.label):
        if item.judgement and not item.judgement.identified:
            print(f"MISSED {item.label}: {item.judgement.why}")

    # Small samples are the standing trap in this repository — ADR 0003 records
    # that ten seeds cannot demonstrate a 5% rate. Say so rather than letting a
    # tidy percentage imply a precision it does not have.
    if n < 20:
        print(
            f"\nNote: {n} mutant(s). This locates the auditor's behaviour, not its rate. "
            "Treat an ordering as evidence and a percentage as an anecdote."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
