"""Append one charter §68 action record to ``harness/agent-log/<utc-date>.jsonl``.

Every agent brief ends by calling this. The record is the audit trail a later
session reads to reconstruct what an agent did and why; without it the only
trace of a subagent run is a transcript outside the repository.

Append-only by construction: the file is opened with ``"a"`` and nothing here
can rewrite or delete an earlier line. The agent name is checked against the
briefs on disk, so a typo cannot create a phantom agent in the log.

Usage::

    .venv/bin/python scripts/agent_log.py --agent risk-auditor --model opus \\
        --request "audit 9b fix" --decision "VERDICT: RISK LAYER SOUND" \\
        --confidence high --files src/trading_platform/risk/limits.py \\
        --tests tests/test_risk.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

CONFIDENCE = ("high", "medium", "low")

#: The §68 fields, in the order the charter lists them.
FIELDS = (
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


class AgentLogError(ValueError):
    """The record could not be written as asked."""


def known_agents(root: Path) -> frozenset[str]:
    return frozenset(p.stem for p in (root / ".claude" / "agents").glob("*.md"))


def log_path(root: Path, at: datetime) -> Path:
    return root / "harness" / "agent-log" / f"{at.date().isoformat()}.jsonl"


def build_record(
    *,
    root: Path,
    agent: str,
    model: str,
    request: str,
    decision: str,
    confidence: str,
    tools: list[str] | None = None,
    data_sources: list[str] | None = None,
    files_changed: list[str] | None = None,
    experiments_run: list[str] | None = None,
    tests_run: list[str] | None = None,
    results: str = "",
    at: datetime | None = None,
) -> dict[str, object]:
    agents = known_agents(root)
    if agent not in agents:
        raise AgentLogError(f"unknown agent {agent!r}; briefs on disk: {sorted(agents)}")
    if confidence not in CONFIDENCE:
        raise AgentLogError(f"confidence must be one of {CONFIDENCE}, got {confidence!r}")
    if not request.strip() or not decision.strip():
        raise AgentLogError("request and decision must both be non-empty")
    stamp = at or datetime.now(UTC)
    if stamp.tzinfo is None:
        raise AgentLogError("timestamp must be timezone-aware")
    record: dict[str, object] = {
        "agent": agent,
        "model": model,
        "timestamp": stamp.isoformat(),
        "request": request,
        "tools": list(tools or []),
        "data_sources": list(data_sources or []),
        "files_changed": list(files_changed or []),
        "experiments_run": list(experiments_run or []),
        "tests_run": list(tests_run or []),
        "results": results,
        "decision": decision,
        "confidence": confidence,
    }
    assert tuple(record) == FIELDS
    return record


def append(root: Path, record: dict[str, object]) -> Path:
    at = datetime.fromisoformat(str(record["timestamp"]))
    path = log_path(root, at)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=False) + "\n")
    return path


def read_log(path: Path) -> list[dict[str, object]]:
    """Every record in one day's file. A torn final line is a corrupt log: raise."""
    records: list[dict[str, object]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise AgentLogError(f"{path.name}:{n}: unreadable record: {exc}") from exc
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--agent", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--decision", required=True)
    parser.add_argument("--confidence", required=True, choices=CONFIDENCE)
    parser.add_argument("--tools", nargs="*", default=[])
    parser.add_argument("--data-sources", nargs="*", default=[])
    parser.add_argument("--files", nargs="*", default=[], dest="files_changed")
    parser.add_argument("--experiments", nargs="*", default=[], dest="experiments_run")
    parser.add_argument("--tests", nargs="*", default=[], dest="tests_run")
    parser.add_argument("--results", default="")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (tests point this at a scratch tree)",
    )
    args = parser.parse_args(argv)
    try:
        record = build_record(
            root=args.root,
            agent=args.agent,
            model=args.model,
            request=args.request,
            decision=args.decision,
            confidence=args.confidence,
            tools=args.tools,
            data_sources=args.data_sources,
            files_changed=args.files_changed,
            experiments_run=args.experiments_run,
            tests_run=args.tests_run,
            results=args.results,
        )
    except AgentLogError as exc:
        print(f"agent_log: {exc}", file=sys.stderr)
        return 2
    path = append(args.root, record)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
