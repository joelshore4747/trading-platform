"""The verdict as a person reads it, and the count it must be read against.

`execute` is the thing anyone actually touches: change a knob, run again,
repeat until something says SURVIVES. That loop is a machine for family-wise
error, and until this step nothing counted the attempts — the registry built
in ADR 0004 existed and was never connected to the UI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_platform.analytics.trials import (
    FAMILY_WISE_CONCERN,
    MultiplicitySummary,
    NoMechanismError,
    TrialRegistry,
)
from trading_platform.ui.runner import RunConfig, execute
from trading_platform.ui.verdict import Outcome

MECHANISM = (
    "trend followers must buy after a move has begun, so a persistent trend "
    "pays the late entrant more than it costs"
)


def small(**kwargs: object) -> RunConfig:
    """A config small enough to run repeatedly in a test."""
    return RunConfig(
        fast_windows=(5, 10),
        slow_windows=(30, 50),
        train_size=200,
        test_size=100,
        warmup=0,
        **kwargs,  # type: ignore[arg-type]
    )


# -- The summary --------------------------------------------------------------


def test_the_family_wise_number_is_the_one_that_governs() -> None:
    m = MultiplicitySummary(
        trials=20, parameter_points=320, completed=20, discoveries=0, per_run_rate=0.047
    )
    assert m.family_wise == pytest.approx(0.617, abs=0.005)
    assert m.is_concerning
    warning = m.warning()
    assert warning is not None and "62%" in warning


def test_a_single_trial_raises_no_alarm() -> None:
    m = MultiplicitySummary(
        trials=1, parameter_points=16, completed=1, discoveries=0, per_run_rate=0.047
    )
    assert not m.is_concerning
    assert m.warning() is None
    assert m.family_wise == pytest.approx(0.047)


def test_the_warning_does_not_quote_the_by_points_figure() -> None:
    """Because it says 100% almost immediately, and a caveat that always says
    100% is one nobody reads.

    ADR 0003 measured the false-discovery rate flat across grid sizes from 9
    to 144 cells at n=100 per arm, so the cells of one sweep are demonstrably
    not independent attempts. The figure is kept on the summary — the gap is
    worth seeing — and kept out of the sentence a person reads.
    """
    m = MultiplicitySummary(
        trials=20, parameter_points=320, completed=20, discoveries=0, per_run_rate=0.047
    )
    assert m.family_wise_by_points > 0.99
    warning = m.warning()
    assert warning is not None
    assert "100%" not in warning


def test_the_concern_threshold_is_where_the_summary_says() -> None:
    below = MultiplicitySummary(
        trials=5, parameter_points=5, completed=5, discoveries=0, per_run_rate=0.047
    )
    assert below.family_wise < FAMILY_WISE_CONCERN
    assert below.warning() is None


# -- Recording ----------------------------------------------------------------


def test_a_run_is_recorded_before_it_runs_and_completed_after(tmp_path: Path) -> None:
    """Recording afterwards counts only the runs somebody liked."""
    registry = TrialRegistry.open(tmp_path / "trials.jsonl")
    outcome = execute(small(seed=3, hypothesis="SMA crossover", mechanism=MECHANISM), registry)

    assert outcome.trial_id == "t00001"
    assert len(registry) == 1
    trial = registry.trials[0]
    assert trial.is_complete
    assert trial.outcome == str(outcome.verdict.outcome)
    assert trial.mechanism == MECHANISM
    assert trial.parameter_points == outcome.config.grid_cells

    # Append-only: the open row and the completed row both survive on disk.
    rows = (tmp_path / "trials.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 2


def test_the_ui_refuses_to_count_a_run_with_no_stated_mechanism(tmp_path: Path) -> None:
    """No stated mechanism, no run — and the refusal happens before any
    compute is spent, not after."""
    registry = TrialRegistry.open(tmp_path / "trials.jsonl")
    with pytest.raises(NoMechanismError):
        execute(small(seed=3, hypothesis="A hunch"), registry)
    assert len(registry) == 0


def test_repeated_runs_accumulate(tmp_path: Path) -> None:
    """The behaviour the count exists to make visible."""
    registry = TrialRegistry.open(tmp_path / "trials.jsonl")
    for seed in range(1, 6):
        execute(small(seed=seed, hypothesis=f"seed {seed}", mechanism=MECHANISM), registry)

    summary = registry.summary()
    assert summary.trials == 5
    assert summary.completed == 5
    assert summary.parameter_points == 5 * 4
    assert summary.family_wise > 5 * 0.047 * 0.9  # compounds, roughly linearly at this size


# -- What the reader sees -----------------------------------------------------


def test_an_uncounted_run_says_so_rather_than_looking_counted(tmp_path: Path) -> None:
    """An uncounted run is allowed. One that looks counted is not."""
    outcome = execute(small(seed=3))
    assert outcome.multiplicity is None
    assert outcome.trial_id is None
    assert "NOT COUNTED" in outcome.verdict_report()


def test_the_report_leads_with_the_verdict(tmp_path: Path) -> None:
    """Document order is the design: judgement, then what qualifies it, then
    the reasons, then the evidence. The tearsheet holds the same rule."""
    registry = TrialRegistry.open(tmp_path / "trials.jsonl")
    outcome = execute(small(seed=3, hypothesis="SMA", mechanism=MECHANISM), registry)
    text = outcome.verdict_report()

    assert text.startswith(str(outcome.verdict.outcome))
    verdict_at = text.index(str(outcome.verdict.outcome))
    count_at = text.index("Trial t00001")
    evidence_at = text.index("efficiency (oos/is)")
    assert verdict_at < count_at < evidence_at
    if outcome.verdict.objections:
        assert count_at < text.index("objection(s):")


def test_the_report_states_the_count_even_when_it_is_small(tmp_path: Path) -> None:
    """Below the concern threshold the line is quiet, not absent — otherwise a
    reader cannot tell a counted run from an uncounted one."""
    registry = TrialRegistry.open(tmp_path / "trials.jsonl")
    outcome = execute(small(seed=3, hypothesis="SMA", mechanism=MECHANISM), registry)
    text = outcome.verdict_report()
    assert "NOT COUNTED" not in text
    assert "Trial t00001 of 1 recorded" in text


def test_a_pass_under_a_heavy_search_shouts_and_a_rejection_does_not() -> None:
    """The branch that matters most, and the one no real run reaches.

    `SMACrossover` never survives, so every test above exercises the quiet
    path. A pass is the only outcome a false-discovery rate can corrupt — a
    FITTED verdict needs no family-wise caveat — so the loud branch is the one
    the design turns on, and it would otherwise ship having never run.

    Built directly rather than searched for, because finding a genuine
    SURVIVES would mean running until one appeared, which is the exact
    behaviour this line exists to warn about.
    """
    from dataclasses import replace

    registry_summary = MultiplicitySummary(
        trials=20, parameter_points=320, completed=20, discoveries=1, per_run_rate=0.047
    )
    outcome = execute(small(seed=3))
    outcome.multiplicity = registry_summary
    outcome.trial_id = "t00020"

    rejected = outcome.verdict_report()
    assert "READ THIS FIRST" not in rejected, "a rejection cannot be a false discovery"
    assert "62%" in rejected, "but the count is still stated"

    outcome.verdict = replace(outcome.verdict, outcome=Outcome.SURVIVES)
    passed = outcome.verdict_report()
    assert passed.startswith("SURVIVES")
    assert "READ THIS FIRST" in passed
    assert passed.index("READ THIS FIRST") < passed.index("efficiency (oos/is)")
