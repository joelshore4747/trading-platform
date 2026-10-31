"""A machine-learning pipeline that is built, tested, and has no model yet.

The pipeline is the hard part and it is useful immediately: features that cannot
see the future, labels that say what they measure, splits that are purged and
embargoed, and a baseline whose job is to be beaten. The model is gated, because
training a deep network on one month of one venue's data would memorise noise
and produce a beautiful backtest that means nothing — see `baseline.load_model`
for the condition that lifts it.

The check that makes the rest worth having is `scripts/measure_ml_null.py`: the
whole pipeline, run on pure noise. If it finds an edge there it is leaking, and
the number is the size of the leak.
"""

from trading_platform.ml.baseline import (
    GATE_CONDITION,
    MajorityClass,
    Model,
    ModelGated,
    SingleFeatureThreshold,
    accuracy,
    load_model,
)
from trading_platform.ml.features import FEATURES, FeatureSet, FeatureSpec, compute, feature_names
from trading_platform.ml.labels import LabelKind, Labels, forward_return, label_series
from trading_platform.ml.pipeline import FoldScore, PipelineResult, run_pipeline
from trading_platform.ml.splits import Split, purged_splits

__all__ = [
    "FEATURES",
    "GATE_CONDITION",
    "FeatureSet",
    "FeatureSpec",
    "FoldScore",
    "LabelKind",
    "Labels",
    "MajorityClass",
    "Model",
    "ModelGated",
    "PipelineResult",
    "SingleFeatureThreshold",
    "Split",
    "accuracy",
    "compute",
    "feature_names",
    "forward_return",
    "label_series",
    "load_model",
    "purged_splits",
    "run_pipeline",
]
