"""Guards for the §69 defaults-diff hook.

The hook exists because "never silently change fees or risk limits" was prose,
and prose is not a control. So it is tested the way the rest of this repository
tests a guard: by doing the forbidden thing in a scratch git repo and checking
the caution fires — and by doing an innocent thing and checking it does not.
A caution that fires on every edit to `limits.py` would be switched off, which
is the failure `docs/PROMPTS.md` records for blocking guards.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "check-defaults.sh"


@pytest.fixture
def repo() -> Path:
    """A throwaway git repo holding copies of the two watched files."""
    root = Path(tempfile.mkdtemp(prefix="defaults-hook-"))
    src = Path(__file__).resolve().parent.parent / "src" / "trading_platform"
    for rel in (
        "risk/limits.py",
        "engine/execution.py",
        "core/instruments.py",
        "engine/backtester.py",
        "strategy/base.py",
    ):
        dest = root / "src" / "trading_platform" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src / rel, dest)
    #: Identity AND signing, because -c user.email alone still leaves git
    #: reading the developer's global commit.gpgsign. This fixture was signing
    #: its scratch commit with the real key, so the suite depended on an
    #: unlocked credential agent and went red — with an error naming 1Password,
    #: not this test — the moment one locked. T-4ab696ad.
    scratch_identity = [
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "tag.gpgsign=false",
    ]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", *scratch_identity, "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", *scratch_identity, "commit", "-qm", "base"],
        cwd=root,
        check=True,
    )
    yield root
    shutil.rmtree(root, ignore_errors=True)


def run_hook(repo: Path, rel_path: str) -> subprocess.CompletedProcess[str]:
    payload = json.dumps({"tool_input": {"file_path": str(repo / rel_path)}})
    return subprocess.run(
        [str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env={"CLAUDE_PROJECT_DIR": str(repo), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        timeout=30,
    )


def edit(repo: Path, rel_path: str, old: str, new: str) -> None:
    p = repo / rel_path
    text = p.read_text()
    assert text.count(old) == 1, f"anchor not unique in {rel_path}"
    p.write_text(text.replace(old, new))


def test_moving_the_drawdown_limit_draws_a_caution(repo: Path) -> None:
    rel = "src/trading_platform/risk/limits.py"
    edit(
        repo,
        rel,
        'max_drawdown_pct: Decimal | None = Decimal("0.25")',
        'max_drawdown_pct: Decimal | None = Decimal("0.90")',
    )
    result = run_hook(repo, rel)
    assert result.returncode == 2, "a moved risk default must reach Claude as a caution"
    assert "max_drawdown_pct" in result.stderr
    assert "§69" in result.stderr


def test_moving_the_commission_default_draws_a_caution(repo: Path) -> None:
    rel = "src/trading_platform/engine/execution.py"
    edit(
        repo,
        rel,
        'commission_rate: Decimal = Decimal("0.0010")',
        'commission_rate: Decimal = Decimal("0")',
    )
    result = run_hook(repo, rel)
    assert result.returncode == 2
    assert "commission_rate" in result.stderr


def test_an_innocent_edit_to_a_watched_file_is_silent(repo: Path) -> None:
    """The precision control. A caution on every edit to limits.py gets ignored."""
    rel = "src/trading_platform/risk/limits.py"
    edit(repo, rel, '"""Pre-trade risk limits.', '"""Pre-trade risk limits (comment touched).')
    result = run_hook(repo, rel)
    assert result.returncode == 0, f"fired on a docstring edit: {result.stderr}"
    assert result.stderr == ""


def test_a_default_edit_in_an_unwatched_file_is_silent(repo: Path) -> None:
    """Only the files that hold §69 defaults are watched."""
    rel = "src/trading_platform/strategy/base.py"
    edit(repo, rel, "class StrategyContext", "class StrategyContext  # touched")
    result = run_hook(repo, rel)
    assert result.returncode == 0


def test_an_unchanged_watched_file_is_silent(repo: Path) -> None:
    result = run_hook(repo, "src/trading_platform/risk/limits.py")
    assert result.returncode == 0


def test_a_malformed_payload_fails_open() -> None:
    """A crashed guard must never cost a turn."""
    result = subprocess.run(
        [str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        timeout=30,
        env={"CLAUDE_PROJECT_DIR": "/nonexistent", "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert result.returncode == 0


def retarget_default(repo: Path, rel_path: str, field: str) -> None:
    """Rewrite the right-hand side of `field`'s default line, whatever it is."""
    import re

    p = repo / rel_path
    text = p.read_text()
    pattern = re.compile(rf"^(\s*{re.escape(field)}\s*:.*?=\s*).*$", re.M)
    assert pattern.findall(text), f"{field} has no default line in {rel_path}"
    # First occurrence only: `initial_margin_rate` is declared on two classes.
    p.write_text(pattern.sub(lambda m: m.group(1) + 'Decimal("12345")', text, count=1))


def test_moving_the_initial_margin_default_draws_a_caution(repo: Path) -> None:
    """core/instruments.py is on the watch list; nothing exercised it
    (guard-auditor, 2026-09-02)."""
    retarget_default(repo, "src/trading_platform/core/instruments.py", "initial_margin_rate")
    result = run_hook(repo, "src/trading_platform/core/instruments.py")
    assert result.returncode == 2
    assert "initial_margin_rate" in result.stderr


def test_moving_starting_cash_draws_a_caution(repo: Path) -> None:
    retarget_default(repo, "src/trading_platform/engine/backtester.py", "starting_cash")
    result = run_hook(repo, "src/trading_platform/engine/backtester.py")
    assert result.returncode == 2
    assert "starting_cash" in result.stderr


def test_a_watched_shaped_line_in_an_unwatched_file_is_silent(repo: Path) -> None:
    """The line is exactly what the hook looks for; only the file is wrong.
    Widening the watch list to strategy/base.py turns this red, which the
    older unwatched-file test could not do (guard-auditor, 2026-09-02)."""
    p = repo / "src" / "trading_platform" / "strategy" / "base.py"
    p.write_text(p.read_text() + '\n    max_drawdown_pct: Decimal | None = Decimal("0.99")\n')
    result = run_hook(repo, "src/trading_platform/strategy/base.py")
    assert result.returncode == 0
    assert result.stderr == ""
