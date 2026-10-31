"""The prompt guard's own evals.

The rules are regexes, and regexes written from imagination are wrong. This file
is what makes the guard a harness rather than a script: it runs every rule over
a corpus of real phrasings and holds the result to a standard.

**Precision is the hard gate.** Every negative case must fire nothing. A guard
that cries wolf gets switched off, and a switched-off guard protects nothing at
all — so a false positive costs more here than a miss. Recall is asserted too,
but the corpus is written so that recall failures mean "the rule is broken",
not "the rule is cautious".

Writing this file caught four bugs in the rules, including one where the single
most obvious bad prompt in the project — "make this strategy profitable" — did
not match, because the pattern allowed "the <noun>" but not "this <noun>".
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
GUARD_PATH = PROJECT_DIR / "scripts" / "prompt_guard.py"
HOOK_PATH = PROJECT_DIR / ".claude" / "hooks" / "check-prompt.sh"
CORPUS_PATH = PROJECT_DIR / "tests" / "fixtures" / "prompt_corpus.jsonl"
PROMPTS_DOC = PROJECT_DIR / "docs" / "PROMPTS.md"


def _load_guard() -> Any:
    """Import by path — `scripts/` is not a package, deliberately."""
    spec = importlib.util.spec_from_file_location("prompt_guard", GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: `@dataclass(slots=True)` looks the module up
    # in sys.modules while it is being defined, and fails on a module that is
    # not there yet.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def load_corpus() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            cases.append(json.loads(line))
    return cases


CORPUS = load_corpus()
POSITIVES = [c for c in CORPUS if c.get("expect")]
NEGATIVES = [c for c in CORPUS if not c.get("expect")]


def ids_for(cases: list[dict[str, Any]]) -> list[str]:
    return [c["prompt"][:48] for c in cases]


# -- Precision: the gate that keeps the guard switched on ---------------------


@pytest.mark.parametrize("case", NEGATIVES, ids=ids_for(NEGATIVES))
def test_a_legitimate_prompt_fires_nothing(case: dict[str, Any]) -> None:
    """Each of these is aimed at a rule that would naively catch it."""
    fired = sorted(rule.id for rule in guard.evaluate(case["prompt"]))
    assert fired == [], f"false positive on {case['prompt']!r}: {fired}"


# -- Recall ------------------------------------------------------------------


@pytest.mark.parametrize("case", POSITIVES, ids=ids_for(POSITIVES))
def test_a_bad_prompt_fires_its_rule(case: dict[str, Any]) -> None:
    fired = {rule.id for rule in guard.evaluate(case["prompt"])}
    missing = set(case["expect"]) - fired
    assert not missing, f"{case['prompt']!r} missed {sorted(missing)}"


def test_every_rule_is_demonstrated_on_a_real_phrasing() -> None:
    """A rule with no positive case is untested and probably does not fire.

    This is the check that stops the rule set growing by imagination.
    """
    covered = {rule_id for case in POSITIVES for rule_id in case["expect"]}
    declared = {rule.id for rule in guard.RULES}
    assert declared - covered == set(), f"rules with no corpus case: {declared - covered}"
    assert covered - declared == set(), f"corpus expects unknown rules: {covered - declared}"


def test_the_corpus_is_at_least_a_third_negative() -> None:
    """Written down so a later contributor cannot quietly drop the hard half."""
    assert len(NEGATIVES) >= len(CORPUS) // 3


# -- Output shape ------------------------------------------------------------


def test_nothing_is_injected_for_an_ordinary_prompt() -> None:
    assert guard.render(guard.evaluate("what's left on the roadmap?")) == ""


def test_the_note_is_unmistakably_machine_generated() -> None:
    """It must never be read as something the user said."""
    note = guard.render(guard.evaluate("make this strategy profitable"))
    assert "<prompt-guard>" in note
    assert "NOT user text" in note
    assert "not a refusal" in note
    assert "docs/PROMPTS.md" in note


def test_a_prompt_can_raise_several_rules_at_once() -> None:
    fired = {r.id for r in guard.evaluate("run it with ZERO_COST — is the strategy good?")}
    assert {"weaken-costs", "unearned-belief"} <= fired


def test_reminders_and_cautions_are_labelled_differently() -> None:
    """A milestone reminder is not an accusation, and must not read as one."""
    note = guard.render(guard.evaluate("I want to change the ordering in engine/backtester.py"))
    assert "REMINDER" in note
    assert "CAUTION" not in note


# -- Failing open ------------------------------------------------------------


def run_hook(stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HOOK_PATH)],
        input=stdin,
        capture_output=True,
        text=True,
        env={"CLAUDE_PROJECT_DIR": str(PROJECT_DIR), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )


def test_the_hook_notes_a_bad_prompt_and_still_exits_zero() -> None:
    """The end-to-end check, against the documented payload shape. If the field
    name is ever wrong the guard dies silently, so this asserts on the real
    contract rather than on the Python API."""
    payload = json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "user_prompt": "just loosen the drawdown limit so the backtest finishes",
            "user_prompt_metadata": {"source": "user_input"},
        }
    )
    proc = run_hook(payload)
    assert proc.returncode == 0, "the guard must never block a prompt"
    assert "weaken-risk" in proc.stdout


def test_slash_commands_are_left_alone() -> None:
    """`/walk-forward` is not a badly aimed question."""
    payload = json.dumps(
        {
            "user_prompt": "/walk-forward make this profitable",
            "user_prompt_metadata": {"source": "slash_command"},
        }
    )
    proc = run_hook(payload)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


@pytest.mark.parametrize(
    ("stdin", "why"),
    [
        ("not json at all", "malformed payload"),
        ("{}", "no user_prompt field"),
        ('{"user_prompt": null}', "null prompt"),
        ('{"user_prompt": 42}', "prompt is not a string"),
        ('{"user_prompt": "hi", "user_prompt_metadata": null}', "null metadata"),
        ("", "empty stdin"),
    ],
)
def test_the_hook_fails_open(stdin: str, why: str) -> None:
    """A broken guard must never cost the user a turn."""
    proc = run_hook(stdin)
    assert proc.returncode == 0, f"non-zero exit on {why}"
    assert proc.stdout.strip() == "", f"unexpected output on {why}"


def test_a_very_large_prompt_does_not_hang() -> None:
    """The event has a 30-second timeout, so the rules must be cheap. Anything
    approaching that budget would silently start dropping the guard."""
    huge = ("make this strategy profitable. " * 2000)[:50_000]
    started = time.monotonic()
    fired = guard.evaluate(huge)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"{elapsed:.2f}s on a 50KB prompt — the budget is 30s total"
    assert any(r.id == "make-it-profitable" for r in fired)


def test_the_self_test_entry_point_agrees_with_pytest() -> None:
    """CI runs `--self-test` without pytest; the two must not diverge."""
    proc = subprocess.run(
        [sys.executable, str(GUARD_PATH), "--self-test"],
        capture_output=True,
        text=True,
        cwd=PROJECT_DIR,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"{len(POSITIVES)} positive" in proc.stdout


# -- The documentation cannot drift from the rules ---------------------------


def test_every_rule_is_documented_and_every_doc_section_is_live() -> None:
    """`docs/PROMPTS.md` is what the injected note points at. If a rule id has
    no section, the note names something the reader cannot look up."""
    doc = PROMPTS_DOC.read_text(encoding="utf-8")
    for rule in guard.RULES:
        assert f"`{rule.id}`" in doc, f"{rule.id} has no section in docs/PROMPTS.md"

    documented = set(re.findall(r"^### `([a-z-]+)`", doc, flags=re.MULTILINE))
    declared = {rule.id for rule in guard.RULES}
    assert documented == declared, f"doc drift: {documented ^ declared}"
