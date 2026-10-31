"""Guards for the harness that measures this repository's guards.

The recursion is the point. A harness that measures whether defences can fail
is worthless if it cannot itself fail, and it would be the fifth instance of
this project's most repeated defect — sitting inside the module built to find
the first four. So the checks below are chosen for what they would catch if
they broke, not for coverage:

- the corpus notices when the source moves under it, rather than mutating the
  wrong line;
- the mutant tree does not contain the answer key;
- the working tree is never touched;
- and the suite runner can return **both** KILLED and SURVIVED, demonstrated
  rather than asserted.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from evals.agent import AgentRun, load_agent, prepare_for_agent
from evals.corpus import (
    DEFECTS_DIR,
    KNOWN_AGENTS,
    Defect,
    DefectError,
    Mutation,
    load_corpus,
    load_defect,
)
from evals.mutate import EXCLUDED, PROJECT_DIR, materialise
from evals.suite import run_suite

AGENTS_DIR = PROJECT_DIR / ".claude" / "agents"


@pytest.fixture
def scratch() -> Path:
    path = Path(tempfile.mkdtemp(prefix="evals-test-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


# -- The corpus ---------------------------------------------------------------


def test_the_corpus_loads() -> None:
    corpus = load_corpus()
    assert corpus, "an empty corpus would report a perfect score on zero evidence"
    assert len({d.id for d in corpus}) == len(corpus)


def test_every_defect_still_applies_to_the_current_tree(scratch: Path) -> None:
    """The staleness guard, and the cheapest useful check in the harness.

    Source moves. An anchor that no longer matches means the defect is silently
    absent from every run while the report still counts it, so this must fail
    loudly the moment the tree drifts — not warn, and not skip.
    """
    for defect in load_corpus():
        tree = materialise(defect, scratch / defect.id)
        try:
            for mutation in defect.mutations:
                text = (tree.root / mutation.file).read_text(encoding="utf-8")
                assert mutation.replace.strip() in text, (
                    f"{defect.id}: mutation did not land in {mutation.file}"
                )
        finally:
            tree.cleanup()


def test_a_stale_anchor_raises_rather_than_matching_loosely() -> None:
    mutation = Mutation(file="x.py", find="text that is not there", replace="other")
    with pytest.raises(DefectError, match="expected exactly 1"):
        mutation.apply_to("some unrelated content", defect_id="TEST")


def test_an_anchor_matching_twice_is_refused() -> None:
    """Two matches means one of them was never inspected by the author."""
    mutation = Mutation(file="x.py", find="dup", replace="other")
    with pytest.raises(DefectError, match="appears 2 times"):
        mutation.apply_to("dup and dup", defect_id="TEST")


def test_a_mutation_that_changes_nothing_is_refused(scratch: Path) -> None:
    """The guard-that-cannot-fail check, applied to the corpus format itself."""
    path = scratch / "Z01-noop.toml"
    path.write_text(
        'id = "Z01"\ntitle = "t"\ncategory = "risk"\nseverity = "high"\n'
        'provenance = "p"\nexpected_agent = "risk-auditor"\n'
        'tell = "a tell long enough to pass validation checks"\n'
        '[[mutation]]\nfile = "f.py"\nfind = "same"\nreplace = "same"\n',
        encoding="utf-8",
    )
    with pytest.raises(DefectError, match="changes nothing"):
        load_defect(path)


def test_every_expected_agent_has_a_definition() -> None:
    """A defect routed to an agent that does not exist would never be scored."""
    for defect in load_corpus():
        assert (AGENTS_DIR / f"{defect.expected_agent}.md").is_file(), (
            f"{defect.id} routes to {defect.expected_agent}, which has no definition"
        )


def test_known_agents_matches_the_agent_directory() -> None:
    """Two sources of truth that must not drift apart."""
    on_disk = {p.stem for p in AGENTS_DIR.glob("*.md")}
    unknown = on_disk - KNOWN_AGENTS
    assert not unknown, f"agents exist that the corpus cannot route to: {sorted(unknown)}"


def test_every_registered_agent_has_a_brief_on_disk() -> None:
    """The other direction: a registry name with no file routes a defect to nobody.

    Deleting `.claude/agents/final-reviewer.md` without touching the registry
    would leave the first test green and this one red.
    """
    on_disk = {p.stem for p in AGENTS_DIR.glob("*.md")}
    phantom = KNOWN_AGENTS - on_disk
    assert not phantom, f"registered agents with no brief: {sorted(phantom)}"


def test_every_brief_declares_a_model_the_charter_names() -> None:
    """Charter §64: opus, sonnet, haiku, fable. A typo here would silently fall
    back to sonnet in `load_agent`, so the eval would measure the wrong model."""
    for path in sorted(AGENTS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---"), path.name
        frontmatter = text.split("---", 2)[1]
        declared = [
            line.split(":", 1)[1].strip()
            for line in frontmatter.splitlines()
            if line.strip().startswith("model:")
        ]
        # Read from the file, not via `load_agent`: that helper falls back to
        # "sonnet" when the line is missing, which would make a brief with no
        # model at all pass this test (guard-auditor, 2026-09-02).
        assert len(declared) == 1, (path.name, "needs exactly one model: line")
        assert declared[0] in {"opus", "sonnet", "haiku", "fable"}, (path.name, declared[0])
        assert load_agent(path.stem).model == declared[0]


def test_defect_filenames_carry_their_id() -> None:
    for path in DEFECTS_DIR.glob("*.toml"):
        defect = load_defect(path)
        assert path.name.startswith(defect.id)


# -- The mutant tree ----------------------------------------------------------


def test_the_mutant_tree_does_not_contain_the_answer_key(scratch: Path) -> None:
    """The load-bearing isolation guard.

    An auditor able to read `evals/` would find every defect by reading the
    corpus rather than the code, and score perfectly while detecting nothing.
    That is future information reaching a decision — the exact bug class this
    harness exists to measure — so it gets a test of its own.
    """
    defect = load_corpus()[0]
    tree = materialise(defect, scratch / "isolation")
    try:
        assert not (tree.root / "evals").exists()
        corpus_names = {p.name for p in DEFECTS_DIR.glob("*.toml")}
        leaked = {p.name for p in tree.root.rglob("*.toml")} & corpus_names
        assert not leaked, f"corpus files reached the mutant: {sorted(leaked)}"
        assert "evals" in EXCLUDED
    finally:
        tree.cleanup()


def test_materialising_does_not_touch_the_working_tree(scratch: Path) -> None:
    defect = next(d for d in load_corpus() if d.id == "L01")
    target = PROJECT_DIR / defect.mutations[0].file
    before = target.read_bytes()

    tree = materialise(defect, scratch / "notouch")
    try:
        assert target.read_bytes() == before, "the real source was modified"
        mutant = (tree.root / defect.mutations[0].file).read_bytes()
        assert mutant != before, "the mutant is identical to the original"
    finally:
        tree.cleanup()


def test_a_control_tree_is_byte_identical_to_the_source(scratch: Path) -> None:
    tree = materialise(None, scratch / "control")
    try:
        assert tree.is_control
        for relative in ("src/trading_platform/engine/execution.py", "CLAUDE.md"):
            assert (tree.root / relative).read_bytes() == (PROJECT_DIR / relative).read_bytes()
    finally:
        tree.cleanup()


def test_the_harness_own_tests_are_kept_out_of_the_mutant(scratch: Path) -> None:
    """They import `evals`, which the mutant deliberately lacks.

    Left in, they raise ImportError during collection in every mutant. The
    suite runner classifies that as ERROR rather than KILLED — which is the
    right call and exactly how this was found — but it makes every full-suite
    result unusable, so the fix belongs here rather than in the classifier.
    """
    tree = materialise(None, scratch / "no-self-tests")
    try:
        assert not (tree.root / "tests" / "test_evals.py").exists()
        assert (tree.root / "tests" / "test_lookahead.py").exists()
    finally:
        tree.cleanup()


def test_a_missing_file_is_reported_not_skipped(scratch: Path) -> None:
    defect = replace(
        load_corpus()[0],
        mutations=(Mutation(file="src/does/not/exist.py", find="a", replace="b"),),
    )
    with pytest.raises(DefectError, match="does not exist"):
        materialise(defect, scratch / "missing")


# -- The suite runner ---------------------------------------------------------


@pytest.mark.slow
def test_the_runner_reports_both_outcomes(scratch: Path) -> None:
    """The harness's own control: it must be able to say SURVIVED and KILLED.

    A runner that only ever returns KILLED would report a flawless defence and
    be indistinguishable from a working one. Both arms run here, against the
    same selection, so the difference is the mutation and nothing else.
    """
    python = PROJECT_DIR / ".venv" / "bin" / "python"
    if not python.exists():  # pragma: no cover - environment without a venv
        pytest.skip("no .venv to run the mutant suite with")

    selection = ("tests/test_lookahead.py",)

    control = materialise(None, scratch / "runner-control")
    try:
        clean = run_suite(control, python=python, selection=selection)
    finally:
        control.cleanup()
    assert clean.outcome == "SURVIVED", f"clean tree did not pass: {clean.tail}"

    defect = next(d for d in load_corpus() if d.id == "L01")
    mutant = materialise(defect, scratch / "runner-mutant")
    try:
        broken = run_suite(mutant, python=python, selection=selection)
    finally:
        mutant.cleanup()
    assert broken.outcome == "KILLED", f"L01 was not caught: {broken.tail}"
    assert broken.killed_by is not None


def test_the_classifier_reads_exit_codes_not_output_text(scratch: Path) -> None:
    """A collection error must be ERROR, and a failure mentioning it must not.

    The first version scanned stdout for "ImportError". A test asserting on an
    import-error message would have matched it, turning a real kill into an
    ERROR and hiding whatever hole that mutant was probing — a defect that
    conceals holes, inside the module written to find them.
    """
    python = PROJECT_DIR / ".venv" / "bin" / "python"
    if not python.exists():  # pragma: no cover
        pytest.skip("no .venv")

    tree = materialise(None, scratch / "classify")
    try:
        broken = tree.root / "tests" / "test_zz_collect_boom.py"
        broken.write_text("import a_module_that_does_not_exist_xyz\n", encoding="utf-8")
        result = run_suite(tree, python=python, selection=(str(broken),))
        assert result.outcome == "ERROR", "a tree that cannot be imported was not caught"
        assert result.returncode == 2
        broken.unlink()

        # A genuine failure whose message contains "ImportError" is a KILL.
        failing = tree.root / "tests" / "test_zz_mentions_importerror.py"
        failing.write_text(
            "def test_x() -> None:\n    assert False, 'ImportError SyntaxError IndentationError'\n",
            encoding="utf-8",
        )
        result = run_suite(tree, python=python, selection=(str(failing),))
        assert result.outcome == "KILLED", "a real failure was misread as a broken tree"
        assert result.returncode == 1
    finally:
        tree.cleanup()


# -- The agent arm ------------------------------------------------------------


def test_agent_definitions_parse() -> None:
    for name in sorted(KNOWN_AGENTS):
        spec = load_agent(name)
        assert spec.body, f"{name} has an empty system prompt"
        assert spec.model in {"opus", "sonnet", "haiku", "fable", "inherit"}


def test_preparing_a_tree_unwires_hooks_without_deleting_them(scratch: Path) -> None:
    """Deleting the scripts breaks eight tests that invoke them on disk.

    That handed every auditor in the first agent-eval run a tree with eight
    unrelated red tests — attention spent on a failure the harness itself
    manufactured, and "the suite is green" removed as a usable signal.
    Unwiring is sufficient: nothing fires a script settings.json does not name.
    """
    tree = materialise(None, scratch / "prepared")
    try:
        prepare_for_agent(tree, blind=True)

        settings = json.loads((tree.root / ".claude" / "settings.json").read_text())
        assert "hooks" not in settings, "hooks must not fire inside a throwaway tree"
        assert (tree.root / ".claude" / "hooks" / "check-prompt.sh").is_file(), (
            "the scripts must stay on disk; tests/test_prompt_guard.py runs them"
        )

        assert not (tree.root / "docs" / "ROADMAP.md").exists()
        assert not (tree.root / "docs" / "adr").exists()
        assert (tree.root / "CLAUDE.md").exists(), "house rules are legitimate context"
    finally:
        tree.cleanup()


def test_sighted_mode_keeps_the_history(scratch: Path) -> None:
    tree = materialise(None, scratch / "sighted")
    try:
        prepare_for_agent(tree, blind=False)
        assert (tree.root / "docs" / "ROADMAP.md").exists()
    finally:
        tree.cleanup()


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("CLEAN", False),
        ("clean", False),
        ("2 LEAK(S) FOUND", True),
        ("1 FAKE GUARD(S) FOUND", True),
        ("no leaks found", False),
    ],
)
def test_verdict_parsing_discriminates(verdict: str, expected: bool) -> None:
    run = AgentRun("x", "a", f"report\nVERDICT: {verdict}", verdict, 0.0, 0.0, True)
    assert run.flagged is expected


def test_a_failed_agent_run_is_not_scored_as_clean() -> None:
    """A crashed run must not be counted as a clean verdict.

    Silently reading an infrastructure failure as "the auditor found nothing"
    would move the false-negative rate in the flattering direction, which is
    the direction this repository distrusts by default.
    """
    run = AgentRun("x", "a", "", "", 0.0, 0.0, ok=False)
    assert run.flagged is False
    assert not run.ok


def test_localisation_requires_naming_the_file() -> None:
    defect = next(d for d in load_corpus() if d.id == "L01")
    hit = AgentRun("L01", "a", "problem in engine/execution.py line 200", "", 0.0, 0.0, True)
    miss = AgentRun("L01", "a", "something is wrong somewhere", "", 0.0, 0.0, True)
    assert hit.localised(defect.files)
    assert not miss.localised(defect.files)


# -- The workflows -----------------------------------------------------------
#
# A workflow is prose too, and a dud one fails at the moment it is dispatched —
# after the user asked for it, and usually after money has been spent on the
# first phase. These are cheap structural checks, not a substitute for running
# it, and they exist because `.claude/` had no automated check of any kind
# before this file.

WORKFLOWS_DIR = PROJECT_DIR / ".claude" / "workflows"


def _workflows() -> list[Path]:
    return sorted(WORKFLOWS_DIR.glob("*.js"))


def test_workflows_exist() -> None:
    assert _workflows(), "no workflow scripts found"


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.stem)
def test_a_workflow_declares_the_required_meta(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.lstrip().startswith("export const meta"), (
        f"{path.name}: the runtime requires `export const meta` as the first statement"
    )
    for key in ("name:", "description:", "phases:"):
        assert key in text, f"{path.name}: meta is missing {key}"
    assert f"name: '{path.stem}'" in text or f'name: "{path.stem}"' in text, (
        f"{path.name}: meta.name must match the filename, or the workflow cannot be invoked by name"
    )


def _meta_block(text: str) -> str:
    """The `export const meta = {...}` literal, brace-matched."""
    start = text.index("export const meta")
    open_brace = text.index("{", start)
    depth = 0
    for index in range(open_brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace : index + 1]
    raise AssertionError("meta block is not brace-balanced")


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.stem)
def test_a_workflow_meta_is_a_pure_literal(path: Path) -> None:
    """The runtime parses `meta` statically and rejects any computed node.

    Both workflows shipped with a `whenToUse` built by string concatenation and
    were refused at dispatch — after the user had asked for the run. The tests
    beside this one checked that meta carried the right *keys* and never that
    it was a literal, which is the shape of guard this repository keeps finding
    in itself: a check aimed one inch beside the failure.

    Textual rather than a real parse, deliberately: a `node --check` here would
    make a Python test depend on a JS toolchain in CI, and the whole class of
    failure that actually occurred is an operator inside the block.
    """
    block = _meta_block(path.read_text(encoding="utf-8"))

    for token, why in (
        ("+", "string concatenation is a BinaryExpression"),
        ("${", "template interpolation is not a literal"),
        ("...", "a spread is not a literal"),
        ("(", "a function call is not a literal"),
    ):
        assert token not in block, f"{path.name}: meta contains {token!r} — {why}"


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.stem)
def test_a_workflow_avoids_the_banned_nondeterministic_builtins(path: Path) -> None:
    """`Date.now()`, `new Date()` and `Math.random()` throw in a workflow script.

    They would break resume, which replays a run against cached agent results.
    A script using one fails partway through a fan-out rather than at dispatch,
    which is the expensive moment to find out.
    """
    body = path.read_text(encoding="utf-8")
    for banned in ("Date.now(", "Math.random(", "new Date("):
        assert banned not in body, f"{path.name}: {banned}) is unavailable in a workflow script"


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.stem)
def test_a_workflow_only_dispatches_to_agents_that_exist(path: Path) -> None:
    """A typo'd agentType surfaces as a runtime failure mid-fan-out."""
    text = path.read_text(encoding="utf-8")
    referenced = set(re.findall(r"agentType:\s*['\"]([a-z-]+)['\"]", text))
    referenced |= set(re.findall(r"agent:\s*['\"]([a-z-]+auditor|[a-z-]+reviewer)['\"]", text))
    for name in sorted(referenced):
        assert (AGENTS_DIR / f"{name}.md").is_file(), (
            f"{path.name} dispatches to {name!r}, which has no definition"
        )


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.stem)
def test_a_workflow_phase_labels_match_its_meta(path: Path) -> None:
    """Phases named in calls but absent from meta get their own ungrouped box."""
    text = path.read_text(encoding="utf-8")
    declared = set(re.findall(r"title:\s*'([^']+)'", text))
    used = set(re.findall(r"phase\(\s*'([^']+)'\s*\)", text))
    used |= set(re.findall(r"phase:\s*'([^']+)'", text))
    assert used <= declared, (
        f"{path.name}: phases used but not declared in meta: {sorted(used - declared)}"
    )


def test_defect_files_are_deduplicated() -> None:
    defect = Defect(
        id="T01",
        title="t",
        category="risk",
        severity="high",
        provenance="p",
        expected_agent="risk-auditor",
        tell="a tell long enough to pass validation",
        mutations=(
            Mutation(file="a.py", find="x", replace="y"),
            Mutation(file="a.py", find="p", replace="q"),
        ),
    )
    assert defect.files == ("a.py",)
