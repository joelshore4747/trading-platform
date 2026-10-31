"""The baseline a deep model has to beat, and the gate that stops one arriving early.

## Why a baseline at all

"The neural network made 40% a year" is not a result until you know what a
constant prediction made on the same data. Most of the time it made the same,
because the label is nearly balanced and the model learned the base rate. A
baseline that is embarrassingly simple is the only way to see that, and it has
to be run first — after the fact, everyone finds a reason the comparison was
unfair.

Two baselines, both trivial on purpose:

- `MajorityClass` predicts the training set's most common label sign, always.
  Anything that cannot beat this has learned nothing.
- `SingleFeatureThreshold` picks the one feature and cut point with the best
  training accuracy. One parameter and one choice of column — a model with a
  million parameters that ties with this has a million parameters too many.

## The gate

`load_model` raises for anything but these. Training a deep model on one month
of one venue's data would produce a beautiful backtest that means nothing: it
would memorise the noise, and there is not enough data for it to do anything
else. The condition for lifting the gate is written into the error, so nobody
has to go looking for it — and it is a data condition, not a code one.

This mirrors `brokers/`, where every adapter raises: a capability that exists
structurally and is refused deliberately is much harder to enable by accident
than one that is merely absent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "MajorityClass",
    "Model",
    "ModelGated",
    "SingleFeatureThreshold",
    "accuracy",
    "load_model",
]

#: What the gate is waiting for. A data condition, not an engineering one.
GATE_CONDITION = (
    "at least 2 years of bars from at least 20 instruments across at least 2 venues, "
    "AND the null harness (scripts/measure_ml_null.py) showing this pipeline finds no "
    "edge in pure noise at the horizon and embargo you intend to use"
)


class ModelGated(NotImplementedError):
    """A model this repository will not train yet, and why."""


class Model(Protocol):
    """Fit on rows and labels; predict a sign. Deliberately tiny."""

    def fit(self, rows: Sequence[Sequence[float]], labels: Sequence[int]) -> None: ...

    def predict(self, rows: Sequence[Sequence[float]]) -> list[int]: ...


@dataclass(slots=True)
class MajorityClass:
    """Always predicts the training set's most common label. The floor.

    Not a strawman: on a nearly balanced label it is what most models converge
    to, and a model that ties with it has found nothing while looking as though
    it has.
    """

    name: str = "majority-class"
    _prediction: int = 0

    def fit(self, rows: Sequence[Sequence[float]], labels: Sequence[int]) -> None:
        if not labels:
            raise ValueError("cannot fit on no labels")
        counts: dict[int, int] = {}
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
        # Ties resolve to 0 — "no opinion" — rather than to a direction. A coin
        # flip that always lands the same way is a position.
        best = max(counts.values())
        winners = sorted(k for k, v in counts.items() if v == best)
        self._prediction = winners[0] if len(winners) == 1 else 0

    def predict(self, rows: Sequence[Sequence[float]]) -> list[int]:
        return [self._prediction] * len(rows)


@dataclass(slots=True)
class SingleFeatureThreshold:
    """One feature, one cut point, chosen by training accuracy.

    The simplest thing that can be said to have learned. Its whole job is to be
    beaten — and when it is not, to say so before anyone builds on top.
    """

    name: str = "single-feature-threshold"
    #: How many candidate cut points to try per feature. Coarse on purpose:
    #: a finer grid fits the training set better and generalises no better.
    cuts: int = 20
    _column: int = 0
    _threshold: float = 0.0
    _above: int = 1
    _below: int = -1

    def fit(self, rows: Sequence[Sequence[float]], labels: Sequence[int]) -> None:
        if not rows or not labels:
            raise ValueError("cannot fit on no rows")
        if len(rows) != len(labels):
            raise ValueError(f"{len(rows)} rows against {len(labels)} labels")
        best_score = -1.0
        for column in range(len(rows[0])):
            values = sorted({row[column] for row in rows})
            if len(values) < 2:
                continue
            step = max(len(values) // self.cuts, 1)
            for candidate in values[::step]:
                for above, below in ((1, -1), (-1, 1)):
                    hits = sum(
                        1
                        for row, label in zip(rows, labels, strict=True)
                        if (above if row[column] > candidate else below) == label
                    )
                    score = hits / len(rows)
                    if score > best_score:
                        best_score = score
                        self._column, self._threshold = column, candidate
                        self._above, self._below = above, below
        if best_score < 0:
            raise ValueError(
                "no feature varies across the training set, so there is nothing to "
                "threshold. That is a fact about the data, not a failure to fit."
            )

    def predict(self, rows: Sequence[Sequence[float]]) -> list[int]:
        return [self._above if row[self._column] > self._threshold else self._below for row in rows]


def accuracy(predicted: Sequence[int], actual: Sequence[int]) -> float:
    """Share correct. Reported beside the majority-class rate, never alone."""
    if len(predicted) != len(actual):
        raise ValueError(f"{len(predicted)} predictions against {len(actual)} labels")
    if not actual:
        raise ValueError(
            "accuracy over no samples is not 1.0 and not 0.0; it is undefined, and "
            "returning a number here is how an empty fold becomes a perfect score"
        )
    return sum(1 for p, a in zip(predicted, actual, strict=True) if p == a) / len(actual)


def load_model(name: str) -> Model:
    """The gate. Only the baselines exist; everything else raises with the condition.

    Deliberately structural rather than a config flag. `brokers/` works the same
    way, and for the same reason: a capability that is refused in code is much
    harder to enable by accident than one that is merely absent.
    """
    if name == "majority-class":
        return MajorityClass()
    if name == "single-feature-threshold":
        return SingleFeatureThreshold()
    raise ModelGated(
        f"no model {name!r}. Only the baselines exist, on purpose: a deep model trained "
        "on the data this repository currently holds — one month of one venue — would "
        "memorise noise and produce a beautiful backtest that means nothing. The "
        f"pipeline is built and testable now; the model waits for {GATE_CONDITION}. "
        "Lifting this is an ADR, not an edit."
    )
