"""Level 2 — run a real auditor agent against a mutant and score what it says.

``lookahead-auditor`` has never been measured. It is the highest-stakes review
in the repository, it runs on a frontier model for that reason, and nobody
knows what it misses. A missed leak does not crash anything; it produces an
excellent-looking equity curve that is entirely fictional. Its false-negative
rate is the most important unknown number here.

Three scores, deliberately separated, because collapsing them hides the failure
that matters:

``flagged``
    The agent returned a non-clean verdict. Necessary and nowhere near
    sufficient — an agent that flags everything scores 100% here and is
    worthless, which is why the control arm exists.
``localised``
    The report names the file that was actually mutated. Objective, cheap, and
    still weak: a lookahead audit mentions ``backtester.py`` in passing almost
    every time.
``identified``
    A separate judge, shown the defect's ``tell`` and the report but **never
    the diff**, says the report describes *this* defect. This is the number
    worth quoting.

The control arm is not a formality. A reviewer that cries wolf gets switched
off, and a switched-off reviewer detects nothing — the same reasoning that
keeps ``prompt_guard`` non-blocking, and the same lesson the stalled-feed check
taught when it fired on 36 of 39 symbols and therefore said nothing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from evals.corpus import Defect
from evals.mutate import MutantTree

AGENTS_DIR = Path(__file__).resolve().parent.parent / ".claude" / "agents"

#: Files whose presence would let an auditor read about the defect instead of
#: finding it. ROADMAP.md narrates most of this corpus in detail — legitimate
#: context for a real session, and an answer key for a measurement.
MEMORY_PATHS = ("docs/ROADMAP.md", "docs/adr")

_VERDICT = re.compile(r"VERDICT\s*:\s*(.+)", re.IGNORECASE)
_CLEAN = re.compile(r"\b(clean|no leaks?|none found|survives this audit)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AgentSpec:
    name: str
    model: str
    body: str


def load_agent(name: str) -> AgentSpec:
    """Read an agent definition, returning its model and system prompt.

    The prompt is the agent's own file, unedited. Measuring a paraphrase would
    measure the paraphrase — the whole point is to score the artefact that runs
    in real sessions.
    """
    path = AGENTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"no agent definition at {path}")

    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path.name}: expected YAML frontmatter")

    _, frontmatter, body = text.split("---", 2)
    model = "sonnet"
    for line in frontmatter.splitlines():
        if line.strip().startswith("model:"):
            model = line.split(":", 1)[1].strip()
    return AgentSpec(name=name, model=model, body=body.strip())


def build_venv(tree: MutantTree, *, timeout_s: float = 600.0) -> bool:
    """Give the mutant its own virtualenv, so the agent can actually run tests.

    Not optional, and not a convenience. Every auditor definition declares
    ``Bash`` and instructs the agent to *prove* a finding by writing a failing
    test and pasting the output — "a suspicion you did not test is a
    suspicion". Running them without that capability measures a hobbled
    reviewer and understates every rate.

    It must be a **real** venv in the mutant, never a link to the project's.
    The project venv holds an editable install pointing at the real ``src``, so
    ``.venv/bin/python -m pytest`` inside a symlinked tree would import the
    unmutated package and report the mutant clean — a silent, total corruption
    of the measurement, arriving through exactly the kind of stale-reference
    path this repository keeps finding in its own code.
    """
    try:
        for args in (["uv", "venv"], ["uv", "pip", "install", "-e", ".[dev]"]):
            proc = subprocess.run(
                args, cwd=tree.root, capture_output=True, text=True, timeout=timeout_s
            )
            if proc.returncode != 0:
                return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return True


def prepare_for_agent(tree: MutantTree, *, blind: bool) -> None:
    """Make a mutant tree safe and fair to run an agent inside.

    Hooks are **unwired, not deleted.** The copied ``SessionStart`` hook would
    try to build a virtualenv in a throwaway directory and the ``Stop`` hook
    would run the lookahead suite every turn, so the wiring has to go — but
    removing the scripts themselves breaks the eight ``test_the_hook_fails_open``
    cases in ``tests/test_prompt_guard.py``, which invoke ``check-prompt.sh`` on
    disk.

    That is not cosmetic. It hands every auditor a tree with eight unrelated red
    tests, spends their attention on a failure the harness manufactured, and
    removes "the suite is green" as a signal they can use. It confounded the
    first agent-eval run: ``guard-auditor`` spent its report on those failures
    and never read the assertions it was there to read.

    Dropping the ``hooks`` key from ``settings.json`` is sufficient on its own —
    nothing fires a script the settings do not reference.

    Under ``blind``, the project's written memory of its own bugs is removed as
    well. Both modes are legitimate and they measure different things; the
    reporting layer must say which one produced a number.
    """
    settings = tree.root / ".claude" / "settings.json"
    if settings.is_file():
        data = json.loads(settings.read_text(encoding="utf-8"))
        data.pop("hooks", None)
        settings.write_text(json.dumps(data, indent=2), encoding="utf-8")

    if blind:
        for relative in MEMORY_PATHS:
            target = tree.root / relative
            if target.is_dir():
                shutil.rmtree(target)
            elif target.is_file():
                target.unlink()


@dataclass(frozen=True, slots=True)
class AgentRun:
    label: str
    agent: str
    report: str
    verdict: str
    cost_usd: float
    duration_s: float
    ok: bool

    @property
    def flagged(self) -> bool:
        """True when the agent did not return a clean verdict."""
        if not self.ok:
            return False
        if self.verdict:
            return not _CLEAN.search(self.verdict)
        # No verdict line at all. Fall back to the report body rather than
        # scoring it clean: an agent that found something and formatted its
        # answer badly has still found something.
        return not _CLEAN.search(self.report[-400:])

    def localised(self, files: tuple[str, ...]) -> bool:
        return any(Path(f).name in self.report for f in files)


#: Default ceiling for one agent run.
#:
#: Measured rather than guessed: a Sonnet auditor reading only returns in about
#: 3 minutes, while an Opus auditor with `Bash` runs the suite — 60-90s per
#: pytest invocation, several times — and the first such runs pushed well past
#: the 15 minutes originally allowed. A timeout scores as a failed run, not a
#: clean verdict, so setting it too low would quietly discard the slowest and
#: most thorough audits and report the rate of the ones that finished.
AGENT_TIMEOUT_S = 2400.0


def run_agent(
    spec: AgentSpec,
    tree: MutantTree,
    *,
    task: str,
    timeout_s: float = AGENT_TIMEOUT_S,
) -> AgentRun:
    """Invoke the agent headlessly inside the mutant tree."""
    args = [
        "claude",
        "-p",
        task,
        "--model",
        spec.model,
        "--append-system-prompt",
        spec.body,
        # The tools each auditor definition declares. Narrowing them would
        # measure a different agent than the one that runs in real sessions —
        # and Bash is the one that turns a suspicion into a finding.
        "--allowedTools",
        "Read,Grep,Glob,Bash",
        "--output-format",
        "json",
    ]

    started = time.monotonic()
    try:
        proc = subprocess.run(
            args,
            cwd=tree.root,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return AgentRun(tree.label, spec.name, str(exc), "", 0.0, time.monotonic() - started, False)

    duration = time.monotonic() - started
    try:
        payload = json.loads(proc.stdout)
        report = str(payload.get("result", ""))
        cost = float(payload.get("total_cost_usd", 0.0))
        ok = not payload.get("is_error", False) and bool(report)
    except (json.JSONDecodeError, ValueError):
        return AgentRun(tree.label, spec.name, proc.stdout[-2000:], "", 0.0, duration, False)

    match = _VERDICT.search(report)
    return AgentRun(
        label=tree.label,
        agent=spec.name,
        report=report,
        verdict=match.group(1).strip() if match else "",
        cost_usd=cost,
        duration_s=duration,
        ok=ok,
    )


JUDGE_PROMPT = """\
You are scoring one code-review report against one specific defect.

THE DEFECT THAT WAS PLANTED (the reviewer was NOT told this):
{tell}

Files actually modified: {files}

THE REVIEWER'S REPORT:
<report>
{report}
</report>

Did the report identify THIS defect? Apply these rules strictly:

- YES only if the report describes this specific mechanism, not merely the
  general topic. "Check for lookahead bias" is not identifying a lookahead bug.
- A finding in the right file for the wrong reason is NO.
- A correct description buried among many speculative findings is still YES,
  but note the noise.
- The reviewer need not use the same words. Judge the mechanism, not phrasing.

Answer with a single line of JSON and nothing else:
{{"identified": true or false, "confidence": "high"|"low", "why": "one sentence"}}
"""


@dataclass(frozen=True, slots=True)
class Judgement:
    identified: bool
    confidence: str
    why: str
    cost_usd: float


def judge(
    run: AgentRun,
    defect: Defect,
    *,
    model: str = "sonnet",
    timeout_s: float = 300.0,
) -> Judgement:
    """Ask a separate model whether the report describes `defect`.

    The judge sees the ``tell`` and the report, never the diff. Given the diff
    it would score any report mentioning the right line as a find, which is the
    localisation number already measured more cheaply.
    """
    prompt = JUDGE_PROMPT.format(
        tell=defect.tell.strip(),
        files=", ".join(defect.files),
        report=run.report[:24000],
    )
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", model, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        payload = json.loads(proc.stdout)
        raw = str(payload.get("result", "")).strip()
        cost = float(payload.get("total_cost_usd", 0.0))
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, FileNotFoundError) as exc:
        return Judgement(False, "low", f"judge failed: {exc}", 0.0)

    block = re.search(r"\{.*\}", raw, re.DOTALL)
    if not block:
        return Judgement(False, "low", f"judge returned no JSON: {raw[:120]}", cost)
    try:
        parsed = json.loads(block.group(0))
    except json.JSONDecodeError:
        return Judgement(False, "low", f"judge JSON malformed: {raw[:120]}", cost)

    return Judgement(
        identified=bool(parsed.get("identified", False)),
        confidence=str(parsed.get("confidence", "low")),
        why=str(parsed.get("why", "")),
        cost_usd=cost,
    )
