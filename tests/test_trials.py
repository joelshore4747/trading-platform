"""The count of attempts, and what a file can and cannot enforce about it.

The per-run false-discovery rate is roughly 1 in 100. Twenty ideas at that rate
produce a false pass 62% of the time, so the number that governs this project's
honesty is the trial count — and a count is only worth keeping if it is kept
before the results are known.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_platform.analytics.trials import (
    NoMechanismError,
    Trial,
    TrialRegistry,
    family_wise_error,
)

START = datetime(2026, 1, 1, tzinfo=UTC)

MECHANISM = "funding is paid by longs so shorts collect a premium in contango"


def registry(tmp_path: Path) -> TrialRegistry:
    return TrialRegistry.open(tmp_path / "trials.jsonl")


# -- The arithmetic that motivates the file -----------------------------------


def test_family_wise_error_is_the_number_that_governs() -> None:
    """One honest run is not an honest project."""
    assert family_wise_error(0.047, 1) == pytest.approx(0.047)
    assert family_wise_error(0.047, 20) == pytest.approx(0.617, abs=0.005)
    assert family_wise_error(0.047, 50) == pytest.approx(0.911, abs=0.005)


def test_no_trials_means_no_chance_of_a_false_discovery() -> None:
    assert family_wise_error(0.5, 0) == 0.0
    assert family_wise_error(0.5, -3) == 0.0


# -- Recording ----------------------------------------------------------------


def test_a_trial_is_recorded_with_its_mechanism(tmp_path: Path) -> None:
    book = registry(tmp_path)
    trial = book.record(
        "Short perp funding in contango",
        mechanism=MECHANISM,
        parameter_points=9,
        config={"symbol": "BTCUSDT", "fast": 10},
        now=START,
    )
    assert trial.trial_id == "t00001"
    assert trial.mechanism == MECHANISM
    assert trial.parameter_points == 9
    assert not trial.is_complete
    assert not trial.is_discovery


@pytest.mark.parametrize(
    "mechanism",
    ["", "   ", "it works", "worth trying I think", "TBD", "see what happens on the chart"],
)
def test_a_mechanism_shaped_hole_is_refused(tmp_path: Path, mechanism: str) -> None:
    """No stated mechanism, no run.

    A strategy that works for no articulable reason is indistinguishable from
    one that worked by chance, and counting trials is how you tell.
    """
    with pytest.raises(NoMechanismError):
        registry(tmp_path).record("Some idea", mechanism=mechanism, parameter_points=1)


def test_the_registry_cannot_tell_a_good_mechanism_from_a_fluent_one(tmp_path: Path) -> None:
    """Stated plainly, because pretending otherwise is how guards rot.

    This sentence is long enough, grammatical, and says nothing. It is
    accepted. The file's value is the count and the paper trail — putting the
    claim next to the result where a reviewer sees it — not policing prose. A
    length check dressed up as a quality bar would be the fifth
    guard-that-cannot-fail in this repository.
    """
    trial = registry(tmp_path).record(
        "Momentum",
        mechanism="the market exhibits persistent directional tendencies over time",
        parameter_points=1,
    )
    assert trial.is_complete is False


def test_a_trial_that_evaluates_nothing_is_not_a_trial(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="parameter_points"):
        registry(tmp_path).record("Idea", mechanism=MECHANISM, parameter_points=0)


def test_a_grid_counts_once_per_cell(tmp_path: Path) -> None:
    """Ten sweeps of sixty-four cells is 640 shots, not ten."""
    book = registry(tmp_path)
    for _ in range(10):
        book.record("Sweep", mechanism=MECHANISM, parameter_points=64)
    assert len(book) == 10
    assert book.parameter_points == 640
    by_idea = book.family_wise_error(0.047)
    by_point = book.family_wise_error(0.047, by_points=True)
    assert by_idea == pytest.approx(0.379, abs=0.005)
    assert by_point > 0.999
    assert by_point > by_idea


# -- Completion ---------------------------------------------------------------


def test_completing_a_trial_records_the_outcome(tmp_path: Path) -> None:
    book = registry(tmp_path)
    trial = book.record("Idea", mechanism=MECHANISM, parameter_points=4, now=START)
    done = book.complete(trial.trial_id, "SURVIVES", now=START + timedelta(hours=2))
    assert done.is_complete
    assert done.is_discovery
    assert done.recorded_at == START
    assert book.discoveries == (done,)
    assert book.completed == (done,)


def test_a_finished_trial_cannot_be_finished_again(tmp_path: Path) -> None:
    """Re-running until it passes is exactly what this file exists to expose."""
    book = registry(tmp_path)
    trial = book.record("Idea", mechanism=MECHANISM, parameter_points=1)
    book.complete(trial.trial_id, "NO EDGE")
    with pytest.raises(ValueError, match="already finished"):
        book.complete(trial.trial_id, "SURVIVES")


def test_completing_an_unknown_trial_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        registry(tmp_path).complete("t99999", "SURVIVES")


# -- The file itself ----------------------------------------------------------


def test_the_file_is_append_only(tmp_path: Path) -> None:
    """Deleting a trial is deleting evidence, so completion appends a row."""
    path = tmp_path / "trials.jsonl"
    book = TrialRegistry.open(path)
    trial = book.record("Idea", mechanism=MECHANISM, parameter_points=1)
    book.complete(trial.trial_id, "FITTED")
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 2
    assert Trial.from_json(rows[0]).outcome is None
    assert Trial.from_json(rows[1]).outcome == "FITTED"


def test_a_reopened_registry_sees_the_finished_state(tmp_path: Path) -> None:
    """Replaying the log has to land on the same place the writer was."""
    path = tmp_path / "trials.jsonl"
    book = TrialRegistry.open(path)
    first = book.record("One", mechanism=MECHANISM, parameter_points=3)
    book.record("Two", mechanism=MECHANISM, parameter_points=5)
    book.complete(first.trial_id, "SURVIVES")

    reopened = TrialRegistry.open(path)
    assert len(reopened) == 2
    assert reopened.parameter_points == 8
    assert [t.trial_id for t in reopened.discoveries] == [first.trial_id]
    assert reopened.trials[1].trial_id == "t00002"

    # And the next trial recorded does not reuse an identifier.
    assert reopened.record("Three", mechanism=MECHANISM, parameter_points=1).trial_id == "t00003"


def test_opening_a_registry_that_does_not_exist_yet_is_empty(tmp_path: Path) -> None:
    book = TrialRegistry.open(tmp_path / "nested" / "trials.jsonl")
    assert len(book) == 0
    assert book.parameter_points == 0
    assert book.family_wise_error(0.047) == 0.0


def test_a_trial_round_trips_through_json(tmp_path: Path) -> None:
    book = registry(tmp_path)
    trial = book.record(
        "Idea", mechanism=MECHANISM, parameter_points=7, notes="ran at warmup=120", now=START
    )
    assert Trial.from_json(trial.to_json()) == trial
    done = book.complete(trial.trial_id, "LOSES TO HOLDING", now=START)
    assert Trial.from_json(done.to_json()) == done


def test_the_config_digest_distinguishes_configurations(tmp_path: Path) -> None:
    book = registry(tmp_path)
    a = book.record("A", mechanism=MECHANISM, parameter_points=1, config={"fast": 10})
    b = book.record("B", mechanism=MECHANISM, parameter_points=1, config={"fast": 20})
    c = book.record("C", mechanism=MECHANISM, parameter_points=1, config={"fast": 10})
    assert a.config_digest != b.config_digest
    assert a.config_digest == c.config_digest


def test_the_report_names_the_discoveries_and_their_mechanisms(tmp_path: Path) -> None:
    """A discovery and the reason claimed for it belong on the same page."""
    book = registry(tmp_path)
    trial = book.record("Funding carry", mechanism=MECHANISM, parameter_points=9)
    book.record("Momentum", mechanism=MECHANISM, parameter_points=9)
    book.complete(trial.trial_id, "SURVIVES")
    report = book.report()
    assert "2 trials recorded" in report
    assert "18" in report
    assert "Funding carry" in report
    assert MECHANISM in report


def test_the_default_path_is_not_gitignored() -> None:
    """The first real use wrote the trial count somewhere git discards.

    `runs/` is gitignored and CI actively fails if anything under it is
    tracked, so a registry kept there survives exactly one session. A count of
    attempts that does not persist cannot support a multiplicity correction,
    and the correction is the only reason the count exists.
    """
    from trading_platform.analytics.trials import DEFAULT_PATH

    assert not DEFAULT_PATH.startswith("runs/")
    assert DEFAULT_PATH.startswith("docs/")
