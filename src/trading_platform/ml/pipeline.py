"""The whole pipeline in one call, so the null harness can point at all of it.

Features, labels, purged and embargoed splits, a baseline, and a score — run
end to end. It exists as one function because that is what has to be attacked:
each piece is testable in isolation and a leak lives in the joins between them,
which is exactly what unit tests do not see.

`scripts/measure_ml_null.py` runs this on **pure noise**. A pipeline that finds
an edge there is leaking, and the number it reports is the size of the leak. That
measurement gates everything else: until it comes back clean there is no point
running this on real data, because a positive result would be indistinguishable
from the leak.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from trading_platform.core.events import Bar
from trading_platform.ml.baseline import Model, accuracy, load_model
from trading_platform.ml.features import FEATURES, FeatureSpec, compute
from trading_platform.ml.labels import LabelKind, label_series
from trading_platform.ml.splits import purged_splits

__all__ = ["FoldScore", "PipelineResult", "run_pipeline"]


@dataclass(frozen=True, slots=True)
class FoldScore:
    """One fold's result, always beside the floor it has to beat."""

    train_samples: int
    test_samples: int
    purged: int
    embargoed: int
    model_accuracy: float
    #: What always predicting the training majority scored on the same test set.
    #: A model that does not beat this has learned nothing, however good it looks.
    baseline_accuracy: float

    @property
    def edge(self) -> float:
        return self.model_accuracy - self.baseline_accuracy


@dataclass(slots=True)
class PipelineResult:
    """What the run found, and what it is allowed to claim.

    `mean_edge` is the only headline, and it is accuracy over the majority-class
    floor. Raw accuracy is not reported alone anywhere, because on a 55/45 label
    a model that always says "up" scores 55% and looks like a discovery.
    """

    folds: list[FoldScore] = field(default_factory=list)
    samples: int = 0
    warmup: int = 0
    #: Set when the run could not be made. Reported, never silently returned as
    #: a zero edge — "no edge" and "no measurement" are different answers.
    refused: str = ""

    @property
    def mean_edge(self) -> float | None:
        usable = [f for f in self.folds if f.test_samples]
        if not usable:
            return None
        return sum(f.edge for f in usable) / len(usable)

    @property
    def positive_folds(self) -> int:
        return sum(1 for f in self.folds if f.edge > 0)

    def report(self) -> str:
        if self.refused:
            return f"NO MEASUREMENT: {self.refused}"
        edge = self.mean_edge
        if edge is None:
            return "NO MEASUREMENT: no fold had a test set"
        return (
            f"{len(self.folds)} fold(s), {self.samples:,} samples "
            f"(warmup {self.warmup} bars)\n"
            f"  mean edge over majority class: {edge:+.4f}\n"
            f"  folds with a positive edge: {self.positive_folds}/{len(self.folds)}\n"
            f"  purged {sum(f.purged for f in self.folds):,}, "
            f"embargoed {sum(f.embargoed for f in self.folds):,}"
        )


def run_pipeline(
    bars: Sequence[Bar],
    *,
    model_name: str = "single-feature-threshold",
    horizon: int = 10,
    train_size: int = 500,
    test_size: int = 200,
    embargo: int = 0,
    specs: Sequence[FeatureSpec] = FEATURES,
    kind: LabelKind = LabelKind.OPEN_TO_OPEN,
) -> PipelineResult:
    """Features to score, in one pass. Refuses rather than returning a zero edge.

    `model_name` goes through `load_model`, which is the gate: anything but a
    baseline raises with the condition for lifting it.
    """
    features = compute(bars, specs)
    if not len(features):
        return PipelineResult(
            refused="no complete feature rows — the series is shorter than the widest window"
        )

    labels = label_series(bars, features.indices, horizon, kind)
    if not len(labels):
        return PipelineResult(
            refused=f"no labelled samples — the series is shorter than warmup + horizon ({horizon})"
        )

    # Realign: labelling drops the tail whose horizon runs off the end.
    by_index = dict(zip(features.indices, features.rows, strict=True))
    rows_by_index = {i: by_index[i] for i in labels.indices}
    signs_by_index = dict(zip(labels.indices, labels.signs(), strict=True))

    splits = purged_splits(
        labels.indices,
        horizon=horizon,
        train_size=train_size,
        test_size=test_size,
        embargo=embargo,
    )
    if not splits:
        return PipelineResult(
            refused=f"{len(labels)} samples is fewer than train_size + test_size "
            f"({train_size} + {test_size}); no fold could be formed"
        )

    result = PipelineResult(samples=len(labels), warmup=features.warmup)
    for split in splits:
        if not split.is_usable:
            result.folds.append(
                FoldScore(
                    len(split.train), len(split.test), split.purged, split.embargoed, 0.0, 0.0
                )
            )
            continue
        train_rows = [rows_by_index[i] for i in split.train]
        train_labels = [signs_by_index[i] for i in split.train]
        test_rows = [rows_by_index[i] for i in split.test]
        test_labels = [signs_by_index[i] for i in split.test]

        model: Model = load_model(model_name)
        model.fit(train_rows, train_labels)
        floor: Model = load_model("majority-class")
        floor.fit(train_rows, train_labels)

        result.folds.append(
            FoldScore(
                train_samples=len(split.train),
                test_samples=len(split.test),
                purged=split.purged,
                embargoed=split.embargoed,
                model_accuracy=accuracy(model.predict(test_rows), test_labels),
                baseline_accuracy=accuracy(floor.predict(test_rows), test_labels),
            )
        )
    return result
