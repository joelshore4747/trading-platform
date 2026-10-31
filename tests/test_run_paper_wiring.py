"""The paper entrypoint wires the store, or says what it costs not to.

This exists because the mechanisms did not reach production. The account tracker
and the leverage gate were built, tested and wired by hand in the test suite,
while `scripts/run_paper.py` passed neither — so a real paper run wrote no
`account_marks` and gated no leverage, and every test that "proved" the gate was
proving something the entrypoint did not do (ticket T-5beee1ed, from an
adversarial review of workstream 3).

The rule these tests pin: **a session holding a leveraged instrument with no
store is refused, not warned about.** The gate exists so that leverage cannot
arrive by omission, and "the database was not configured" is an omission.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from trading_platform.core.types import Stage

ROOT = Path(__file__).resolve().parents[1]


def load() -> object:
    spec = importlib.util.spec_from_file_location(
        "run_paper_under_test", ROOT / "scripts" / "run_paper.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


paper = load()


def args(**over: object) -> argparse.Namespace:
    values: dict[str, object] = {"account": "a1", "no_store": False}
    values.update(over)
    return argparse.Namespace(**values)


def test_a_leveraged_account_with_no_store_is_refused_not_warned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing would check that anyone had acknowledged what the leverage does,
    and a warning is a thing people scroll past.

    Mutation: print a warning and return `(None, None)` instead of raising —
    a 10x paper account runs ungated and this goes red."""
    monkeypatch.setattr(paper, "is_configured", lambda: False)
    with ExitStack() as stack, pytest.raises(SystemExit) as raised:
        paper._store_wiring(stack, args(no_store=True), Stage.PAPER, ["BTCUSDT-PERP"])
    message = str(raised.value)
    assert "BTCUSDT-PERP" in message
    assert "acknowledged" in message
    assert "ADR" in message


def test_a_spot_account_with_no_store_runs_and_says_what_it_costs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal must not become a general nuisance: nothing at 1x needs
    acknowledging, and requiring a database for a spot account would teach
    people to pass `--no-store` everywhere, including where it matters.

    But it is not silent either — the journal being the only record is a real
    cost and the operator is told."""
    monkeypatch.setattr(paper, "is_configured", lambda: False)
    with ExitStack() as stack:
        tracker, gate = paper._store_wiring(stack, args(no_store=True), Stage.PAPER, [])
    assert tracker is None and gate is None
    printed = capsys.readouterr().out
    assert "no store" in printed
    assert "no leverage is gated" in printed
    assert "only record" in printed


def test_the_entrypoint_builds_a_config_carrying_the_tracker_the_gate_and_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point, tested by running the entrypoint and looking at what it
    built — not by reading its source, which would pass on a file that no longer
    compiles.

    Mutation: drop `tracker=tracker`, `leverage_gate=gate` or `stage=stage` from
    the `SupervisorConfig(...)` call — red, and a real paper run silently stops
    recording, stops gating, or mislabels every row it writes."""
    captured: dict[str, object] = {}

    class FakeSupervisor:
        def __init__(self, cfg: object, strategy: object) -> None:
            captured["cfg"] = cfg

        def reconcile(self) -> object:
            return type("R", (), {"report": staticmethod(lambda: "reconciled")})()

    from trading_platform.risk.leverage import LeverageGate

    sentinel_tracker = object()
    # A real gate, for this account: `SupervisorConfig` checks the account it
    # belongs to, which is R07 and is worth not defeating with a stub.
    sentinel_gate = LeverageGate(account="a1")
    monkeypatch.setattr(paper, "PaperSupervisor", FakeSupervisor)
    monkeypatch.setattr(paper, "_store_wiring", lambda *a, **k: (sentinel_tracker, sentinel_gate))
    monkeypatch.setenv("APCA_API_KEY_ID", "not-a-real-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "not-a-real-key")

    paper.main(
        [
            "--root",
            str(tmp_path),
            "--account",
            "a1",
            "--market",
            "crypto",
            "--symbols",
            "BTC/USD",
            "--starting-cash",
            "1000",
            "--stage",
            "simulation",
            "--dry-run",
        ]
    )

    cfg = captured["cfg"]
    assert cfg.tracker is sentinel_tracker, "the entrypoint built a tracker and did not pass it"
    assert cfg.leverage_gate is sentinel_gate, "the entrypoint built a gate and did not pass it"
    assert cfg.stage is Stage.SIMULATION, "the stage does not reach the config"


def test_live_is_not_a_stage_this_script_will_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SupervisorConfig` refuses it anyway, but an option that exists gets
    tried, and the error a person should meet is the one that explains why —
    not an argparse usage line they read as a typo.

    Mutation: add "live" to the choices — argparse accepts it and the failure
    moves to a place with less to say."""
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "k")
    with pytest.raises(SystemExit) as raised:
        paper.main(
            [
                "--root",
                str(tmp_path),
                "--account",
                "a1",
                "--market",
                "crypto",
                "--symbols",
                "BTC/USD",
                "--starting-cash",
                "1000",
                "--stage",
                "live",
                "--dry-run",
            ]
        )
    assert raised.value.code == 2, "argparse should refuse the choice outright"
