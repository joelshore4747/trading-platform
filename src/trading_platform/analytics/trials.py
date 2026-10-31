"""Every hypothesis this project has tested, written down before it ran.

The false-discovery harness in `analytics/nulltest.py` measured the pipeline's
per-run rate at roughly 1 in 100. That is the honest number for **one** idea,
and it is not the number that matters. At a 4.7% upper bound, twenty
independent ideas produce at least one false `SURVIVES` with probability
**62%**; fifty produce one with probability 91%.

So per-run honesty is not project honesty, and nothing in a per-run verdict can
fix that — the run does not know how many siblings it has. This does.

**What a registry can enforce, and what it cannot.**

It can enforce three things, and they are the three that matter:

1. **The trial is recorded before it runs.** Recording afterwards means the
   ones that disappointed you quietly never get recorded, and the count that
   drives every multiplicity correction is the count of *attempts*, not of
   results worth keeping.
2. **The count includes hyperparameter points.** A sixty-four cell sweep is
   sixty-four trials, not one. `SweepSurface.attempted` already knows the
   difference between the grid as written and the cells that actually ran.
3. **The file is append-only.** Deleting a trial is deleting evidence, so the
   writer refuses to rewrite history and a reader can check the chain.

It cannot enforce that a stated mechanism is any *good*. That is a human
judgement, and a length check pretending otherwise would be theatre — the
fourth guard-that-cannot-fail in a week already taught that lesson at some
cost. What this does is require the field, refuse the obviously-empty cases,
and put the sentence next to the result where a reviewer will see it. The
value is the count and the paper trail, not policing prose.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "DEFAULT_PATH",
    "MIN_MECHANISM_WORDS",
    "MultiplicitySummary",
    "NoMechanismError",
    "Trial",
    "TrialRegistry",
    "family_wise_error",
]

#: A mechanism shorter than this is not a hypothesis, it is a label. Not a
#: quality bar — no length can be one — just a refusal of the empty case.
MIN_MECHANISM_WORDS = 6

#: Phrases that are a mechanism-shaped hole rather than a mechanism. Rejected
#: because they are the ones people reach for when they do not have one.
_EMPTY_MECHANISMS = (
    "it works",
    "it makes money",
    "looks good",
    "worth trying",
    "should work",
    "test it",
    "see what happens",
    "no idea",
    "tbd",
)


#: Above this, the chance the project has produced at least one false
#: discovery is no longer a footnote. Not a threshold anything is allowed to
#: fail on — it decides when the number is stated loudly rather than quietly,
#: because there is no honest action that "too many trials" implies except
#: knowing it.
FAMILY_WISE_CONCERN = 0.25


@dataclass(frozen=True, slots=True)
class MultiplicitySummary:
    """How many times this question has been asked, and what that costs.

    Structured rather than a formatted string, because the UI needs to render
    it beside a verdict and parsing prose back out of `report()` is how a
    number ends up displayed wrong.
    """

    trials: int
    parameter_points: int
    completed: int
    discoveries: int
    per_run_rate: float

    @property
    def family_wise(self) -> float:
        """Chance of at least one false pass, counting distinct hypotheses."""
        return family_wise_error(self.per_run_rate, self.trials)

    @property
    def family_wise_by_points(self) -> float:
        """The same, counting every parameter set as its own trial.

        **An upper bound that assumes independence this project has measured to
        be false**, and it saturates fast: twenty runs of a sixteen-cell grid
        is 320 points, which reads as 100%. ADR 0003 measured the pipeline's
        false-discovery rate against grid size at n=100 per arm and found it
        flat — 1.0% at 9 cells, 0.0% at 25, 64 and 144 — because `efficiency`
        is out-of-sample over in-sample, so searching more cells raises the
        denominator too. The cells of one sweep are not independent attempts.

        Kept because the gap between this and `family_wise` is worth seeing,
        and deliberately NOT the number `warning()` reports: a caveat that
        always says 100% is a caveat nobody reads.
        """
        return family_wise_error(self.per_run_rate, self.parameter_points)

    @property
    def is_concerning(self) -> bool:
        return self.family_wise >= FAMILY_WISE_CONCERN

    def warning(self) -> str | None:
        """What the count means, in the words a person should read.

        `None` when the count is low enough that the per-run rate is still a
        fair description of the project.
        """
        if not self.is_concerning:
            return None
        return (
            f"{self.trials} trials have been recorded. At a per-run false-discovery "
            f"rate of {self.per_run_rate:.1%}, the chance this project has already "
            f"produced at least one false SURVIVES is {self.family_wise:.0%}. A "
            "verdict on this run is not a verdict on the search that led to it."
        )


class NoMechanismError(ValueError):
    """A trial was submitted without a stated reason to expect an edge.

    The rule is *no stated mechanism, no run* — because a strategy that works
    for no articulable reason is indistinguishable from one that worked by
    chance, and the whole point of counting trials is to find out which.
    """


def family_wise_error(rate: float, trials: int) -> float:
    """Probability of at least one false pass across `trials` independent ideas."""
    if trials <= 0:
        return 0.0
    return 1.0 - (1.0 - rate) ** trials


@dataclass(frozen=True, slots=True)
class Trial:
    """One hypothesis, as recorded before anyone knew how it would turn out."""

    trial_id: str
    recorded_at: datetime
    hypothesis: str
    #: Why an edge should exist. Required, and shown beside the result.
    mechanism: str
    #: Parameter sets actually evaluated. A grid counts once per cell.
    parameter_points: int
    #: Fingerprint of the configuration, so two trials can be compared.
    config_digest: str
    #: External series this hypothesis is permitted to read, named BEFORE the
    #: run. `store/macro.py` refuses any series not in this tuple. The rule is
    #: "collected freely, used only when named": ingesting a warehouse of
    #: government data is cheap and harmless, and mining it until something
    #: correlates is neither. Empty means the hypothesis reads none.
    series: tuple[str, ...] = ()
    #: Filled in when the run finishes. `None` means it never did.
    outcome: str | None = None
    completed_at: datetime | None = None
    notes: str = ""

    @property
    def is_complete(self) -> bool:
        return self.outcome is not None

    @property
    def is_discovery(self) -> bool:
        return self.outcome == "SURVIVES"

    def to_json(self) -> str:
        return json.dumps(
            {
                "trial_id": self.trial_id,
                "recorded_at": self.recorded_at.isoformat(),
                "hypothesis": self.hypothesis,
                "mechanism": self.mechanism,
                "parameter_points": self.parameter_points,
                "config_digest": self.config_digest,
                "series": list(self.series),
                "outcome": self.outcome,
                "completed_at": (self.completed_at.isoformat() if self.completed_at else None),
                "notes": self.notes,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> Trial:
        row = json.loads(raw)
        return cls(
            trial_id=row["trial_id"],
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
            hypothesis=row["hypothesis"],
            mechanism=row["mechanism"],
            parameter_points=row["parameter_points"],
            config_digest=row["config_digest"],
            # Absent on trials recorded before series pre-registration existed.
            # Empty is the honest reading: they named none, so they may read none.
            series=tuple(row.get("series", ())),
            outcome=row["outcome"],
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
            ),
            notes=row.get("notes", ""),
        )


def _check_mechanism(mechanism: str) -> None:
    cleaned = " ".join(mechanism.split())
    if not cleaned:
        raise NoMechanismError(
            "This trial has no stated mechanism. A strategy that works for no "
            "articulable reason is indistinguishable from one that worked by "
            "chance — and counting trials only helps if the count is of "
            "hypotheses rather than of attempts to find something."
        )
    if len(cleaned.split()) < MIN_MECHANISM_WORDS:
        raise NoMechanismError(
            f"Mechanism is {len(cleaned.split())} words: {cleaned!r}. At least "
            f"{MIN_MECHANISM_WORDS} are wanted — not because length is quality, "
            "but because a phrase this short is a label rather than a reason."
        )
    lowered = cleaned.lower()
    for empty in _EMPTY_MECHANISMS:
        if lowered.startswith(empty) or lowered == empty:
            raise NoMechanismError(
                f"{cleaned!r} states that you expect an edge, not why one should "
                "exist. Who is on the other side of the trade, and why are they "
                "willing to lose?"
            )


#: Where the project's own registry lives. Under `docs/`, not `runs/`, and the
#: distinction is not cosmetic: `runs/` is gitignored, so the first real use of
#: this module wrote the trial count to a file git would silently discard. A
#: count of attempts that does not survive the session cannot support any
#: multiplicity correction, which is the only thing this module is for.
DEFAULT_PATH = "docs/trials.jsonl"


@dataclass(slots=True)
class TrialRegistry:
    """An append-only record of every hypothesis put through the pipeline."""

    path: Path
    _trials: list[Trial] = field(default_factory=list)

    @classmethod
    def open(cls, path: Path | str) -> TrialRegistry:
        """Replay the log, folding each trial's later rows onto its first.

        The file is append-only, so a completed trial appears twice — once as
        recorded and once as finished. Reading rows as trials counts it twice,
        which inflates the trial count and the parameter-set total, and those
        two numbers are the entire point of the file: a registry that reads
        high on reopen reports a family-wise error rate that is wrong in the
        flattering direction for anyone who trusts it.
        """
        target = Path(path)
        registry = cls(path=target)
        if not target.exists():
            return registry
        seen: dict[str, int] = {}
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            trial = Trial.from_json(line)
            index = seen.get(trial.trial_id)
            if index is None:
                seen[trial.trial_id] = len(registry._trials)
                registry._trials.append(trial)
            else:
                registry._trials[index] = trial
        return registry

    # -- Recording -------------------------------------------------------------

    def record(
        self,
        hypothesis: str,
        *,
        mechanism: str,
        parameter_points: int,
        config: Mapping[str, object] | None = None,
        series: Sequence[str] = (),
        notes: str = "",
        now: datetime | None = None,
    ) -> Trial:
        """Log a trial **before** running it.

        Recording afterwards is how a registry ends up counting only the runs
        somebody liked, which is precisely the bias every multiplicity
        correction downstream is trying to undo.

        `series` names the external data this hypothesis may read, before it is
        run. Government and macro data is collected freely — ingesting it is
        cheap and harmless — and used only when named, because the alternative
        is mining a warehouse until something correlates. `store/macro.py`
        enforces it.
        """
        _check_mechanism(mechanism)
        if parameter_points < 1:
            raise ValueError(
                f"parameter_points is {parameter_points}; a trial that evaluates "
                "nothing is not a trial. A grid counts once per cell — that is "
                "the whole reason this number is recorded separately."
            )

        stamp = now or datetime.now(UTC)
        digest = hashlib.sha256(
            json.dumps(config or {}, sort_keys=True, default=str).encode()
        ).hexdigest()[:12]
        trial = Trial(
            trial_id=f"t{len(self._trials) + 1:05d}",
            recorded_at=stamp,
            hypothesis=hypothesis.strip(),
            mechanism=" ".join(mechanism.split()),
            parameter_points=parameter_points,
            config_digest=digest,
            series=tuple(dict.fromkeys(series)),
            notes=notes,
        )
        self._trials.append(trial)
        self._append(trial)
        return trial

    def complete(self, trial_id: str, outcome: str, *, now: datetime | None = None) -> Trial:
        """Record how a trial turned out. Appends; never rewrites."""
        for index, existing in enumerate(self._trials):
            if existing.trial_id != trial_id:
                continue
            if existing.is_complete:
                raise ValueError(
                    f"{trial_id} already finished as {existing.outcome!r}. Re-running "
                    "a trial until it passes is the behaviour this file exists to "
                    "make visible; record a NEW trial instead."
                )
            done = Trial(
                trial_id=existing.trial_id,
                recorded_at=existing.recorded_at,
                hypothesis=existing.hypothesis,
                mechanism=existing.mechanism,
                parameter_points=existing.parameter_points,
                config_digest=existing.config_digest,
                outcome=outcome,
                completed_at=now or datetime.now(UTC),
                notes=existing.notes,
            )
            self._trials[index] = done
            self._append(done)
            return done
        raise KeyError(f"No trial {trial_id!r} in {self.path}")

    def _append(self, trial: Trial) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(trial.to_json() + "\n")

    # -- Reading ---------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._trials)

    def __iter__(self) -> Iterator[Trial]:
        return iter(self._trials)

    @property
    def trials(self) -> Sequence[Trial]:
        return tuple(self._trials)

    @property
    def completed(self) -> tuple[Trial, ...]:
        return tuple(t for t in self._trials if t.is_complete)

    @property
    def discoveries(self) -> tuple[Trial, ...]:
        return tuple(t for t in self._trials if t.is_discovery)

    @property
    def parameter_points(self) -> int:
        """Total parameter sets evaluated, across every trial.

        The number that governs, and the one nobody keeps: a project that has
        run ten sweeps of sixty-four cells has taken 640 shots, not ten.
        """
        return sum(t.parameter_points for t in self._trials)

    def family_wise_error(self, per_run_rate: float, *, by_points: bool = False) -> float:
        """Chance this project has produced at least one false discovery.

        `by_points=False` counts distinct hypotheses, which is the right unit
        when a sweep's own multiplicity is already handled inside the run.
        `by_points=True` counts every parameter set, which is the right unit
        when it is not — and the gap between the two is worth looking at.
        """
        return family_wise_error(
            per_run_rate, self.parameter_points if by_points else len(self._trials)
        )

    def summary(self, per_run_rate: float = 0.047) -> MultiplicitySummary:
        """The counts a verdict should be read against."""
        return MultiplicitySummary(
            trials=len(self._trials),
            parameter_points=self.parameter_points,
            completed=len(self.completed),
            discoveries=len(self.discoveries),
            per_run_rate=per_run_rate,
        )

    def report(self, per_run_rate: float = 0.047) -> str:
        lines = [
            f"{len(self._trials)} trials recorded, {len(self.completed)} finished",
            f"  parameter sets evaluated : {self.parameter_points:,}",
            f"  discoveries              : {len(self.discoveries)}",
            f"  at a per-run rate of {per_run_rate:.1%}, the chance this project has",
            f"  produced at least one false discovery is "
            f"{self.family_wise_error(per_run_rate):.0%} counting hypotheses,",
            f"  {self.family_wise_error(per_run_rate, by_points=True):.0%} counting "
            "every parameter set.",
        ]
        if self.discoveries:
            lines.append("")
            lines.append("  Discoveries, with the mechanism claimed for each:")
            for trial in self.discoveries:
                lines.append(f"    {trial.trial_id}  {trial.hypothesis}")
                lines.append(f"             {trial.mechanism}")
        return "\n".join(lines)
