"""The §68 action log must record what was asked and refuse what it cannot audit.

Mutations these catch: opening the file with "w" instead of "a" (second record
overwrites the first); dropping the agent-name check (a typo creates a phantom
agent); accepting an arbitrary confidence string; a torn line read as a record.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.agent_log import (
    FIELDS,
    AgentLogError,
    append,
    build_record,
    log_path,
    read_log,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "agent_log.py"
AT = datetime(2026, 9, 1, 23, 59, tzinfo=UTC)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "risk-auditor.md").write_text("---\nname: risk-auditor\n---\n")
    return tmp_path


def _record(root: Path, **over: object) -> dict[str, object]:
    kwargs: dict[str, object] = dict(
        root=root,
        agent="risk-auditor",
        model="opus",
        request="audit the 9b fix",
        decision="VERDICT: RISK LAYER SOUND",
        confidence="high",
        at=AT,
    )
    kwargs.update(over)
    return build_record(**kwargs)  # type: ignore[arg-type]


#: Charter §68, typed out rather than imported, so the test cannot agree with
#: the code by construction (guard-auditor, 2026-09-02).
CHARTER_68 = (
    "agent",
    "model",
    "timestamp",
    "request",
    "tools",
    "data_sources",
    "files_changed",
    "experiments_run",
    "tests_run",
    "results",
    "decision",
    "confidence",
)


def test_record_carries_every_charter_field_in_order(root: Path) -> None:
    record = _record(root, tests_run=["tests/test_risk.py"])
    assert tuple(record) == CHARTER_68
    assert FIELDS == CHARTER_68
    assert record["timestamp"] == "2026-09-01T23:59:00+00:00"
    assert record["tests_run"] == ["tests/test_risk.py"]


def test_appending_twice_keeps_both_records(root: Path) -> None:
    """Opening with "w" would leave exactly one line and this test red."""
    append(root, _record(root, request="first"))
    path = append(root, _record(root, request="second"))
    assert path == log_path(root, AT)
    assert [r["request"] for r in read_log(path)] == ["first", "second"]


def test_unknown_agent_is_refused(root: Path) -> None:
    with pytest.raises(AgentLogError, match="unknown agent"):
        _record(root, agent="rsk-auditor")


def test_confidence_outside_the_three_levels_is_refused(root: Path) -> None:
    with pytest.raises(AgentLogError, match="confidence"):
        _record(root, confidence="certain")


def test_a_torn_final_line_raises_rather_than_reads_as_a_record(root: Path) -> None:
    path = append(root, _record(root))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"agent": "risk-audi')
    with pytest.raises(AgentLogError, match="unreadable"):
        read_log(path)


def test_the_cli_writes_a_readable_record(root: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(root),
            "--agent",
            "risk-auditor",
            "--model",
            "opus",
            "--request",
            "audit",
            "--decision",
            "sound",
            "--confidence",
            "medium",
            "--files",
            "a.py",
            "b.py",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    written = Path(result.stdout.strip())
    assert written.parent == root / "harness" / "agent-log"
    (record,) = [json.loads(line) for line in written.read_text().splitlines()]
    assert record["files_changed"] == ["a.py", "b.py"]
    assert record["confidence"] == "medium"


def test_the_cli_refuses_a_bad_agent_with_exit_2(root: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(root),
            "--agent",
            "nobody",
            "--model",
            "opus",
            "--request",
            "x",
            "--decision",
            "y",
            "--confidence",
            "low",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "unknown agent" in result.stderr
    assert not (root / "harness" / "agent-log").exists()
