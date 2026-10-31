"""Charter §42 experiment records: create, validate, list.

One TOML file per experiment under ``harness/experiments/``, named
``E-NNN-<slug>.toml``. The record is written **before** the run, with the
hypothesis and what would disprove it, so the result cannot rewrite the
question. After the run, ``result``, ``metrics``, ``confidence``, ``status``
and ``next_step`` are filled in — and from then on ``result`` is frozen:
``validate`` compares every record against the committed version and refuses
one whose filled result has changed. A changed fee, dataset or parameter is a
new experiment, not an edit.

    .venv/bin/python scripts/experiment.py new --name "..." --hypothesis "..." ...
    .venv/bin/python scripts/experiment.py validate [--against-git]
    .venv/bin/python scripts/experiment.py list
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORDS_DIR = ROOT / "harness" / "experiments"

STATUSES = (
    "IDEA",
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "REJECTED",
    "PROMISING",
    "VALIDATED",
    "RETIRED",
)
#: Statuses that mean the run happened: a record in one of these must carry a result.
TERMINAL = frozenset({"COMPLETED", "FAILED", "REJECTED", "PROMISING", "VALIDATED", "RETIRED"})
#: Statuses that mean the run has not happened: a record in one of these must NOT carry a
#: result, or the hypothesis was written after the answer.
UNRUN = frozenset({"IDEA", "QUEUED"})
CONFIDENCE = ("high", "medium", "low")

#: Charter §42, in order, plus the provenance fields the audit found missing.
REQUIRED = (
    "id",
    "name",
    "hypothesis",
    "motivation",
    "dataset",
    "dataset_version",
    "strategy",
    "strategy_version",
    "commit",
    "parameters",
    "date_range",
    "method",
    "result",
    "metrics",
    "confidence",
    "limitations",
    "status",
    "next_step",
    "created",
    "trial_count",
)

ID_RE = re.compile(r"^E-(\d{3})$")
FILE_RE = re.compile(r"^(E-\d{3})-[a-z0-9][a-z0-9-]*\.toml$")


class ExperimentError(ValueError):
    """A record is malformed, or a frozen field was rewritten."""


# -- TOML writing ------------------------------------------------------------
# tomllib reads; nothing in the standard library writes. The records are flat
# (strings, numbers, booleans, lists of strings, one-level tables), so a small
# serializer is enough and avoids a dependency for a text file.


def _toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        if "\n" in value:
            return '"""\n' + value.replace('"""', '\\"""') + '"""'
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    raise ExperimentError(f"cannot serialise {type(value).__name__}")


def dump_toml(record: dict[str, object]) -> str:
    lines: list[str] = []
    tables: list[tuple[str, dict[str, object]]] = []
    for key, value in record.items():
        if isinstance(value, dict):
            tables.append((key, value))
        else:
            lines.append(f"{key} = {_toml_scalar(value)}")
    for key, table in tables:
        lines.append("")
        lines.append(f"[{key}]")
        for k, v in table.items():
            lines.append(f"{k} = {_toml_scalar(v)}")
    return "\n".join(lines) + "\n"


# -- Validation --------------------------------------------------------------


def load_record(path: Path) -> dict[str, object]:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ExperimentError(f"{path.name}: {exc}") from exc
    validate_record(raw, path.name)
    return raw


def validate_record(raw: dict[str, object], filename: str) -> None:
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise ExperimentError(f"{filename}: missing {missing}")

    ident = str(raw["id"])
    if not ID_RE.match(ident):
        raise ExperimentError(f"{filename}: id {ident!r} is not E-NNN")
    m = FILE_RE.match(filename)
    if not m or m.group(1) != ident:
        raise ExperimentError(f"{filename}: filename must be {ident}-<slug>.toml")

    status = str(raw["status"])
    if status not in STATUSES:
        raise ExperimentError(f"{filename}: status {status!r} not in {STATUSES}")

    result = str(raw["result"]).strip()
    if status in TERMINAL and not result:
        raise ExperimentError(f"{filename}: status {status} needs a non-empty result")
    if status in UNRUN and result:
        raise ExperimentError(
            f"{filename}: status {status} with a result — the hypothesis must come first"
        )

    confidence = raw["confidence"]
    backfilled = bool(raw.get("backfilled", False))
    if status in TERMINAL:
        if confidence not in CONFIDENCE and not (backfilled and confidence == "unknown"):
            raise ExperimentError(
                f"{filename}: confidence must be one of {CONFIDENCE} once the run is done"
            )
    elif confidence != "":
        raise ExperimentError(f"{filename}: confidence is filled after the run, not before")

    if not str(raw["hypothesis"]).strip():
        raise ExperimentError(f"{filename}: hypothesis is empty")
    for key in ("parameters", "metrics"):
        if not isinstance(raw[key], dict):
            raise ExperimentError(f"{filename}: {key} must be a table")
    trials = raw["trial_count"]
    if not isinstance(trials, int) or isinstance(trials, bool) or trials < 1:
        raise ExperimentError(f"{filename}: trial_count must be a positive integer")


def load_all(records_dir: Path = RECORDS_DIR) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    seen: dict[str, str] = {}
    for path in sorted(records_dir.glob("E-*.toml")):
        record = load_record(path)
        ident = str(record["id"])
        if ident in seen:
            raise ExperimentError(f"{path.name}: duplicate id {ident} (also {seen[ident]})")
        seen[ident] = path.name
        records.append(record)
    return records


def check_frozen_against_git(records_dir: Path = RECORDS_DIR, *, root: Path = ROOT) -> None:
    """A result that was committed non-empty must not have changed.

    Reads each record's committed version with ``git show HEAD:<path>``. A
    record that is new (not at HEAD) is exempt; one whose committed result was
    empty may be filled once; one whose committed result was non-empty must
    match byte for byte.
    """
    for path in sorted(records_dir.glob("E-*.toml")):
        rel = path.resolve().relative_to(root.resolve()).as_posix()
        proc = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{rel}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            continue  # not committed yet
        committed = tomllib.loads(proc.stdout)
        before = str(committed.get("result", "")).strip()
        if not before:
            continue
        after = str(load_record(path)["result"]).strip()
        if after != before:
            raise ExperimentError(
                f"{path.name}: result was rewritten after being recorded. A changed "
                f"result is a new experiment; this one stays as it was."
            )


# -- Creation ----------------------------------------------------------------


def next_id(records_dir: Path = RECORDS_DIR) -> str:
    highest = 0
    for path in records_dir.glob("E-*.toml"):
        m = FILE_RE.match(path.name)
        if m:
            highest = max(highest, int(m.group(1)[2:]))
    return f"E-{highest + 1:03d}"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:40].rstrip("-") or "experiment"


def current_commit(root: Path = ROOT) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else "UNKNOWN"


def new_record(
    *,
    name: str,
    hypothesis: str,
    motivation: str,
    dataset: str,
    dataset_version: str,
    strategy: str,
    strategy_version: str,
    date_range: str,
    method: str,
    limitations: str,
    next_step: str,
    trial_count: int,
    parameters: dict[str, object] | None = None,
    metrics: dict[str, object] | None = None,
    records_dir: Path = RECORDS_DIR,
    root: Path = ROOT,
    now: datetime | None = None,
) -> Path:
    ident = next_id(records_dir)
    stamp = (now or datetime.now(UTC)).date().isoformat()
    record: dict[str, object] = {
        "id": ident,
        "name": name,
        "hypothesis": hypothesis,
        "motivation": motivation,
        "dataset": dataset,
        "dataset_version": dataset_version,
        "strategy": strategy,
        "strategy_version": strategy_version,
        "commit": current_commit(root),
        "date_range": date_range,
        "method": method,
        "result": "",
        "confidence": "",
        "limitations": limitations,
        "status": "QUEUED",
        "next_step": next_step,
        "created": stamp,
        "trial_count": trial_count,
        "parameters": dict(parameters or {}),
        "metrics": dict(metrics or {}),
    }
    filename = f"{ident}-{slugify(name)}.toml"
    validate_record(record, filename)
    records_dir.mkdir(parents=True, exist_ok=True)
    path = records_dir / filename
    if path.exists():
        raise ExperimentError(f"{path.name} already exists")
    path.write_text(dump_toml(record), encoding="utf-8")
    return path


# -- CLI ---------------------------------------------------------------------


def _list(records_dir: Path) -> str:
    rows = [("id", "status", "confidence", "name")]
    for r in load_all(records_dir):
        rows.append((str(r["id"]), str(r["status"]), str(r["confidence"]) or "-", str(r["name"])))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    out = []
    for row in rows:
        out.append(f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]:<{widths[2]}}  {row[3]}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--records-dir", type=Path, default=RECORDS_DIR)
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="write a QUEUED record before the run")
    for flag in (
        "name",
        "hypothesis",
        "motivation",
        "dataset",
        "dataset-version",
        "strategy",
        "strategy-version",
        "date-range",
        "method",
        "limitations",
        "next-step",
    ):
        new.add_argument(f"--{flag}", required=True)
    new.add_argument("--trial-count", type=int, required=True)
    new.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")

    val = sub.add_parser("validate", help="check every record; refuse rewritten results")
    val.add_argument("--against-git", action="store_true")

    sub.add_parser("list", help="one line per record")

    args = parser.parse_args(argv)
    try:
        if args.command == "new":
            params: dict[str, object] = {}
            for item in args.param:
                if "=" not in item:
                    raise ExperimentError(f"--param needs KEY=VALUE, got {item!r}")
                k, v = item.split("=", 1)
                params[k.strip()] = v.strip()
            path = new_record(
                name=args.name,
                hypothesis=args.hypothesis,
                motivation=args.motivation,
                dataset=args.dataset,
                dataset_version=args.dataset_version,
                strategy=args.strategy,
                strategy_version=args.strategy_version,
                date_range=args.date_range,
                method=args.method,
                limitations=args.limitations,
                next_step=args.next_step,
                trial_count=args.trial_count,
                parameters=params,
                records_dir=args.records_dir,
                root=args.root,
            )
            print(path)
        elif args.command == "validate":
            records = load_all(args.records_dir)
            if args.against_git:
                check_frozen_against_git(args.records_dir, root=args.root)
            print(f"{len(records)} record(s) valid")
        else:
            print(_list(args.records_dir))
    except ExperimentError as exc:
        print(f"experiment: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
