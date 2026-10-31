"""§42 experiment records: the hypothesis comes first and the result is frozen.

Mutations these catch: dropping the UNRUN check (a QUEUED record with a result
passes); dropping the TERMINAL check (a REJECTED record with no result passes);
the git comparison reading the working copy instead of HEAD; `next_id` counting
files instead of parsing ids; an unknown status accepted.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.experiment import (
    RECORDS_DIR,
    ExperimentError,
    check_frozen_against_git,
    dump_toml,
    load_all,
    load_record,
    new_record,
    next_id,
)

NOW = datetime(2026, 9, 2, tzinfo=UTC)


def _new(records_dir: Path, root: Path, **over: object) -> Path:
    kwargs: dict[str, object] = dict(
        name="Funding half-life",
        hypothesis="Post-settlement drift decays within 30 minutes; disproved if the "
        "return autocorrelation at 30m is indistinguishable from the null",
        motivation="CR-001",
        dataset="BTCUSDT perp 1m",
        dataset_version="archive-2026-08",
        strategy="none (study)",
        strategy_version="-",
        date_range="2026-08",
        method="event study around :00 settlements, null harness control",
        limitations="46% coverage",
        next_step="if PROMISING, /new-strategy",
        trial_count=1,
        records_dir=records_dir,
        root=root,
        now=NOW,
    )
    kwargs.update(over)
    return new_record(**kwargs)  # type: ignore[arg-type]


def test_the_committed_records_all_validate() -> None:
    records = load_all(RECORDS_DIR)
    assert len(records) >= 12
    assert {r["id"] for r in records} >= {f"E-{n:03d}" for n in range(1, 13)}


def test_a_new_record_is_queued_with_no_result_and_the_next_id(tmp_path: Path) -> None:
    (tmp_path / "E-007-old.toml").write_text(
        dump_toml(load_record(sorted(RECORDS_DIR.glob("E-007*.toml"))[0]))
    )
    assert next_id(tmp_path) == "E-008"
    path = _new(tmp_path, tmp_path)
    record = load_record(path)
    assert path.name.startswith("E-008-funding-half-life")
    assert record["status"] == "QUEUED"
    assert record["result"] == ""
    assert record["confidence"] == ""
    assert record["created"] == "2026-09-02"


def _write(records_dir: Path, **fields: object) -> Path:
    base = load_record(sorted(RECORDS_DIR.glob("E-003*.toml"))[0])
    base.update(fields)
    path = records_dir / f"{base['id']}-x.toml"
    path.write_text(dump_toml(base))
    return path


def test_a_queued_record_with_a_result_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, status="QUEUED", result="Sharpe 3.1", confidence="")
    with pytest.raises(ExperimentError, match="hypothesis must come first"):
        load_record(path)


def test_a_finished_record_without_a_result_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, status="REJECTED", result="", confidence="high")
    with pytest.raises(ExperimentError, match="non-empty result"):
        load_record(path)


def test_an_unknown_status_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, status="WORKS")
    with pytest.raises(ExperimentError, match="status"):
        load_record(path)


def test_confidence_before_the_run_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, status="QUEUED", result="", confidence="high")
    with pytest.raises(ExperimentError, match="after the run"):
        load_record(path)


def test_duplicate_ids_are_refused(tmp_path: Path) -> None:
    _write(tmp_path)
    src = sorted(RECORDS_DIR.glob("E-003*.toml"))[0]
    (tmp_path / "E-003-y.toml").write_text(src.read_text())
    with pytest.raises(ExperimentError, match="duplicate id"):
        load_all(tmp_path)


def test_a_committed_result_cannot_be_rewritten(tmp_path: Path) -> None:
    """Reading the working copy instead of HEAD would leave this green."""
    root = tmp_path
    records = root / "harness" / "experiments"
    records.mkdir(parents=True)
    git = ["git", "-C", str(root)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "config", "user.email", "t@e.st"], check=True)
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    # An identity is not enough: commit.gpgsign is global on a machine that
    # signs, so a scratch commit here would be signed with the developer's real
    # key and fail whenever their agent is locked. T-4ab696ad.
    subprocess.run([*git, "config", "commit.gpgsign", "false"], check=True)
    path = _write(records, status="REJECTED", result="efficiency 0.27", confidence="medium")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "record"], check=True)

    check_frozen_against_git(records, root=root)  # unchanged: fine

    _write(records, status="PROMISING", result="efficiency 0.91", confidence="high")
    with pytest.raises(ExperimentError, match="rewritten"):
        check_frozen_against_git(records, root=root)
    assert path.exists()


def test_filling_an_empty_committed_result_once_is_allowed(tmp_path: Path) -> None:
    root = tmp_path
    records = root / "harness" / "experiments"
    records.mkdir(parents=True)
    git = ["git", "-C", str(root)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "config", "user.email", "t@e.st"], check=True)
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    # An identity is not enough: commit.gpgsign is global on a machine that
    # signs, so a scratch commit here would be signed with the developer's real
    # key and fail whenever their agent is locked. T-4ab696ad.
    subprocess.run([*git, "config", "commit.gpgsign", "false"], check=True)
    _new(records, root)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "queued"], check=True)
    path = next(records.glob("E-001*.toml"))
    record = load_record(path)
    record.update(status="REJECTED", result="no drift beyond the null", confidence="medium")
    path.write_text(dump_toml(record))
    check_frozen_against_git(records, root=root)
