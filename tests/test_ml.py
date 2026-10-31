"""The ML pipeline: features that cannot see the future, splits that cannot leak.

The two tests that matter are at the bottom. One walks the series bar by bar and
requires every feature to give the same answer from a truncated series as from
the whole one. The other **deliberately introduces a leak** and requires the null
harness to catch it — because a leak detector that has never detected a leak is
a decoration.
"""

from __future__ import annotations

import importlib.util
import statistics
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.core.events import Bar
from trading_platform.ml.baseline import (
    GATE_CONDITION,
    MajorityClass,
    ModelGated,
    SingleFeatureThreshold,
    accuracy,
    load_model,
)
from trading_platform.ml.features import FEATURES, FeatureSet, FeatureSpec, compute
from trading_platform.ml.labels import LabelKind, forward_return, label_series
from trading_platform.ml.pipeline import run_pipeline
from trading_platform.ml.splits import purged_splits

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def bar(i: int, close: float, *, open_: float | None = None) -> Bar:
    opening = close if open_ is None else open_
    high = max(opening, close) * 1.001
    low = min(opening, close) * 0.999
    return Bar(
        symbol="X",
        timestamp=T0 + timedelta(minutes=i),
        open=Decimal(f"{opening:.4f}"),
        high=Decimal(f"{high:.4f}"),
        low=Decimal(f"{low:.4f}"),
        close=Decimal(f"{close:.4f}"),
        volume=Decimal("1"),
    )


def ramp(count: int, start: float = 100.0, step: float = 0.1) -> list[Bar]:
    return [bar(i, start + i * step) for i in range(count)]


def null_harness() -> object:
    spec = importlib.util.spec_from_file_location(
        "ml_null_under_test", ROOT / "scripts" / "measure_ml_null.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# -- labels say what they measure ------------------------------------------------------------


def test_a_label_is_open_to_open_because_that_is_what_could_be_traded() -> None:
    """The engine fills an order from bar T at T+1's open. A close-to-close label
    describes a trade at a price that was already history when the decision was
    taken.

    Mutation: make OPEN_TO_OPEN use closes — the two assertions below become
    equal and this goes red."""
    bars = [bar(0, 100), bar(1, 110, open_=105), bar(2, 120, open_=115)]
    tradeable = forward_return(bars, 0, 1, LabelKind.OPEN_TO_OPEN)
    process = forward_return(bars, 0, 1, LabelKind.CLOSE_TO_CLOSE)
    assert tradeable == pytest.approx((115 - 105) / 105)
    assert process == pytest.approx((110 - 100) / 100)
    assert tradeable != process


def test_a_label_whose_horizon_runs_off_the_end_is_absent_not_zero() -> None:
    """Zero would be a claim that the market did not move.

    Mutation: `return 0.0` instead of None — the last rows get a fabricated
    label and this goes red."""
    bars = ramp(5)
    assert forward_return(bars, 3, 10) is None
    labels = label_series(bars, range(len(bars)), horizon=2)
    assert len(labels) < len(bars)
    assert max(labels.indices) <= len(bars) - 3


def test_a_zero_return_is_its_own_class() -> None:
    """On a coarse instrument a flat bar is common, and calling it "up" puts a
    thumb on the scale."""
    flat = [bar(i, 100.0) for i in range(5)]
    labels = label_series(flat, range(5), horizon=1)
    assert set(labels.signs()) == {0}


# -- splits cannot leak -----------------------------------------------------------------------


def test_a_training_sample_whose_label_reaches_the_test_window_is_purged() -> None:
    """No feature is shared and the leak is total anyway: that sample has seen
    the test period.

    Mutation: drop the purge branch — the last `horizon` training samples before
    each test window survive, and `purged` goes to zero."""
    indices = list(range(100))
    splits = purged_splits(indices, horizon=5, train_size=40, test_size=20)
    assert splits
    first = splits[0]
    assert first.purged > 0
    test_first = first.test[0]
    for index in first.train:
        assert not (index < test_first <= index + 5), (
            f"sample {index} labels into the test window starting at {test_first}"
        )


def test_the_embargo_drops_samples_adjacent_to_the_test_window() -> None:
    """Adjacent bars are serially correlated with the test period; purging alone
    leaves that in.

    Mutation: ignore `embargo` — `embargoed` is 0 and the gap below closes."""
    indices = list(range(200))
    without = purged_splits(indices, horizon=5, train_size=80, test_size=20, embargo=0)
    with_embargo = purged_splits(indices, horizon=5, train_size=80, test_size=20, embargo=10)
    assert with_embargo[0].embargoed > 0
    assert len(with_embargo[0].train) < len(without[0].train)


def test_a_rolling_split_never_trains_on_the_future() -> None:
    """The mistake the module is shaped to make impossible, and there is no
    argument that enables it.

    Mutation: drop the `index > test_last` guard — later samples enter the
    rolling training set and this goes red."""
    indices = list(range(300))
    for split in purged_splits(indices, horizon=5, train_size=100, test_size=50):
        assert all(i < split.test[0] for i in split.train), "trained on data after the test window"


def test_an_anchored_split_still_purges_the_future_it_is_allowed_to_see() -> None:
    """Anchored means "everything from the start", not "everything"."""
    indices = list(range(300))
    for split in purged_splits(indices, horizon=5, train_size=100, test_size=50, anchored=True):
        assert not set(split.train) & set(split.test)
        for index in split.train:
            assert not (index < split.test[0] <= index + 5)


def test_train_and_test_can_never_overlap() -> None:
    """Enforced at construction, so no future splitter can produce one quietly."""
    from trading_platform.ml.splits import Split

    with pytest.raises(ValueError, match="not a split"):
        Split(train=(1, 2, 3), test=(3, 4), purged=0, embargoed=0)


def test_shuffled_indices_are_refused() -> None:
    with pytest.raises(ValueError, match="ascending order"):
        purged_splits([5, 1, 3], horizon=1, train_size=1, test_size=1)


# -- the model is gated -------------------------------------------------------------------------


def test_only_the_baselines_exist_and_the_gate_states_its_condition() -> None:
    """Structural, like `brokers/`: a capability refused in code is much harder
    to enable by accident than one that is merely absent.

    Mutation: return a model for any name — red, and a deep model can be trained
    on one month of data by typing its name."""
    assert isinstance(load_model("majority-class"), MajorityClass)
    assert isinstance(load_model("single-feature-threshold"), SingleFeatureThreshold)
    with pytest.raises(ModelGated) as raised:
        load_model("lstm")
    message = str(raised.value)
    assert GATE_CONDITION in message, "the gate must say what lifts it"
    assert "ADR" in message


def test_the_majority_class_baseline_refuses_to_pick_a_side_on_a_tie() -> None:
    """A coin flip that always lands the same way is a position.

    Mutation: `max(counts)` without the tie check — the model takes a direction
    on balanced data and this goes red."""
    model = MajorityClass()
    model.fit([[0.0], [0.0]], [1, -1])
    assert model.predict([[0.0]]) == [0]
    model.fit([[0.0], [0.0], [0.0]], [1, 1, -1])
    assert model.predict([[0.0]]) == [1]


def test_accuracy_over_no_samples_is_refused_rather_than_returned_as_a_number() -> None:
    """Returning anything here is how an empty fold becomes a perfect score."""
    with pytest.raises(ValueError, match="undefined"):
        accuracy([], [])


def test_a_feature_that_does_not_vary_is_a_fact_about_the_data() -> None:
    with pytest.raises(ValueError, match="nothing to threshold"):
        SingleFeatureThreshold().fit([[1.0], [1.0], [1.0]], [1, -1, 1])


# -- the pipeline refuses rather than reporting a zero edge --------------------------------------


def test_too_short_a_series_is_refused_and_says_why() -> None:
    """ "No edge" and "no measurement" are different answers, and conflating them
    would let a broken run look like an honest negative result."""
    assert "shorter than" in run_pipeline(ramp(10)).refused
    result = run_pipeline(ramp(300), train_size=500, test_size=200)
    assert "no fold could be formed" in result.refused
    assert result.mean_edge is None
    assert "NO MEASUREMENT" in result.report()


def test_the_edge_is_reported_over_the_majority_class_floor_not_as_raw_accuracy() -> None:
    """On a 55/45 label, always saying "up" scores 55% and looks like a
    discovery. The floor is computed on every fold and the headline is the
    difference.

    Mutation: report `model_accuracy` as the edge — a trending series makes every
    fold look excellent and this goes red."""
    result = run_pipeline(ramp(2000), horizon=5, train_size=300, test_size=100)
    assert not result.refused
    assert result.folds
    for fold in result.folds:
        assert fold.edge == fold.model_accuracy - fold.baseline_accuracy
    # A pure ramp is trivially predictable, so the FLOOR is already perfect and
    # there is no edge left to find. That is the point of measuring against it.
    assert result.mean_edge is not None
    assert abs(result.mean_edge) < 0.05


# -- the two that matter --------------------------------------------------------------------------


def test_no_feature_can_see_a_bar_that_has_not_printed() -> None:
    """Walk the series bar by bar. Every feature must give the same answer from
    the bars that have printed as from the whole series.

    Mutation: in any feature, read `bars[index + 1]` — red immediately. This is
    the guard the whole package is shaped around, because an off-by-one here
    makes the model learn the answer and nothing about the failure looks like a
    bug."""
    import random

    rng = random.Random(7)
    price = 100.0
    bars = []
    for i in range(200):
        price = max(price * (1 + rng.gauss(0, 0.002)), 1.0)
        bars.append(bar(i, price, open_=price * (1 + rng.gauss(0, 0.001))))

    full = compute(bars)
    by_index = dict(zip(full.indices, full.rows, strict=True))
    for n in range(len(bars)):
        live = compute(bars[: n + 1])
        for index, row in zip(live.indices, live.rows, strict=True):
            assert row == pytest.approx(by_index[index]), (
                f"feature row for bar {index} changed once bar {n} had printed"
            )


def test_the_null_harness_catches_a_leak_that_is_deliberately_introduced() -> None:
    """A leak detector that has never detected a leak is a decoration.

    A feature is added that reads ONE BAR AHEAD — the exact mistake the rest of
    this package exists to prevent — and the pipeline is run on pure noise, where
    there is nothing legitimate to find. The clean pipeline must report an edge
    near zero; the leaking one must report a large one.

    Mutation: make `run_pipeline` ignore the `specs` argument, or make the null
    generator produce a drifting series — the two runs stop differing and this
    goes red."""
    module = null_harness()
    bars = module.noise_bars(4000, seed=11)  # type: ignore[attr-defined]

    horizon = 10

    def peek(series, index):  # type: ignore[no-untyped-def]
        """Reads the label's own window. This is the bug, written on purpose.

        Matched to the horizon deliberately. An earlier version of this test
        peeked one bar ahead while the label measured ten, and the two were
        barely related — so the "leak" was weak and the test nearly passed for
        the wrong reason. A leak detector has to be tested against a leak that
        is actually there.
        """
        return forward_return(series, index, horizon, LabelKind.OPEN_TO_OPEN)

    leaking = (*FEATURES, FeatureSpec("tomorrow", 1, peek))

    clean = run_pipeline(bars, horizon=horizon, train_size=500, test_size=200)
    leaked = run_pipeline(bars, horizon=horizon, train_size=500, test_size=200, specs=leaking)

    assert clean.mean_edge is not None and leaked.mean_edge is not None
    assert abs(clean.mean_edge) < 0.10, f"the clean pipeline found {clean.mean_edge:+.4f} in noise"
    assert leaked.mean_edge > clean.mean_edge + 0.20, (
        f"the leaking pipeline scored {leaked.mean_edge:+.4f} against the clean "
        f"{clean.mean_edge:+.4f} — the null harness did not notice a feature that reads "
        "the label itself, so it would not notice a real leak either"
    )


def test_the_pipeline_finds_nothing_in_noise_across_several_seeds() -> None:
    """The measurement `scripts/measure_ml_null.py` makes, in miniature so it
    runs in the suite. There is no structure, so a correct pipeline reports an
    edge near zero.

    Mutation: remove the purge from `purged_splits` — the edge on noise moves
    away from zero, which is exactly what this is for."""
    module = null_harness()
    edges = []
    for seed in range(10):
        result = run_pipeline(
            module.noise_bars(4000, seed=200 + seed),  # type: ignore[attr-defined]
            horizon=10,
            train_size=500,
            test_size=200,
        )
        assert not result.refused, result.refused
        assert result.mean_edge is not None
        edges.append(result.mean_edge)
    mean = statistics.fmean(edges)
    # The band is chosen from the MEASURED spread, not picked to pass: across 20
    # seeds the per-trial standard deviation is about 0.029, so the standard
    # error over ten is roughly 0.009 and this is a four-sigma band. A tighter
    # one would fail on unlucky seeds and teach everyone to widen it, which is
    # how a guard dies. The real measurement is `scripts/measure_ml_null.py`
    # with many more trials; this is the version that fits in a test suite.
    assert abs(mean) < 0.037, f"found an edge of {mean:+.4f} in pure noise — the pipeline leaks"


def test_a_feature_matrix_that_lost_its_indices_is_refused() -> None:
    """Row `k` is not bar `k` — the warmup drops rows — and a pipeline that
    assumed otherwise would misalign every label."""
    with pytest.raises(ValueError, match="lost track"):
        FeatureSet(names=("a",), indices=(1, 2), rows=((1.0,),))
