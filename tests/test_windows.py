"""Train/test splitting.

These tests guard the harness's own lookahead property. `test_lookahead.py`
stops a *strategy* seeing the future; this file stops the *evaluator* doing it,
by scoring a strategy on bars it was fitted on. The second bug is quieter: it
produces a plausible number rather than an impossible one.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_platform.analytics.windows import (
    Fold,
    InsufficientDataError,
    TestWindow,
    TrainWindow,
    split_walk_forward,
)
from trading_platform.core.events import Bar
from trading_platform.data.feed import OutOfOrderDataError
from trading_platform.engine.portfolio import EquityPoint

START = datetime(2025, 1, 1, tzinfo=UTC)
SYMBOL = "BTCUSDT"


def make_bars(n: int, symbol: str = SYMBOL, start: datetime = START) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            timestamp=start + timedelta(days=i),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1000"),
        )
        for i in range(n)
    ]


# -- The invariant that matters ---------------------------------------------


def test_no_test_bar_ever_precedes_a_train_bar() -> None:
    """The harness's own lookahead guard, checked on every fold."""
    folds = split_walk_forward(make_bars(200), train_size=50, test_size=25)

    assert folds
    for fold in folds:
        latest_train = max(b.timestamp for b in fold.train.bars)
        earliest_test = min(b.timestamp for b in fold.test.bars)
        assert latest_train < earliest_test


def test_a_fold_that_would_leak_cannot_be_constructed() -> None:
    """Not a convention — the type refuses."""
    bars = make_bars(20)
    with pytest.raises(ValueError, match="fitted on"):
        Fold(
            index=0,
            train=TrainWindow(bars=tuple(bars[5:15])),
            test=TestWindow(bars=tuple(bars[:10])),  # overlaps and precedes
        )


def test_test_windows_never_overlap_across_folds() -> None:
    """Concatenating out-of-sample results must not double-count a bar."""
    folds = split_walk_forward(make_bars(300), train_size=100, test_size=30)

    seen: set[datetime] = set()
    for fold in folds:
        stamps = {b.timestamp for b in fold.test.bars}
        assert not (stamps & seen), "test windows overlap; aggregation would double-count"
        seen |= stamps


def test_test_windows_are_laid_end_to_end() -> None:
    folds = split_walk_forward(make_bars(300), train_size=100, test_size=30)
    for earlier, later in itertools.pairwise(folds):
        assert earlier.test.end < later.test.start


# -- Rolling vs anchored -----------------------------------------------------


def test_rolling_windows_stay_the_same_length() -> None:
    folds = split_walk_forward(make_bars(300), train_size=100, test_size=30, anchored=False)
    assert len({len(f.train) for f in folds}) == 1
    assert len(folds[0].train) == 100


def test_anchored_windows_grow_from_the_start() -> None:
    """The 'all history so far' case, which is what a live system actually has."""
    folds = split_walk_forward(make_bars(300), train_size=100, test_size=30, anchored=True)

    lengths = [len(f.train) for f in folds]
    assert lengths == sorted(lengths)
    assert lengths[0] < lengths[-1]
    assert all(f.train.start == folds[0].train.start for f in folds)


# -- Warm-up -----------------------------------------------------------------


def test_warmup_bars_come_from_before_the_scored_window() -> None:
    folds = split_walk_forward(make_bars(300), train_size=100, test_size=30, warmup=20)

    fold = folds[0]
    assert len(fold.test.warmup) == 20
    assert all(b.timestamp < fold.test.start for b in fold.test.warmup)
    # Warm-up primes history; it is not part of the measurement.
    assert len(fold.test) == 30
    assert len(list(fold.test.feed())) == 50


def test_warmup_is_excluded_from_the_scored_curve() -> None:
    """Metrics must describe the test window, not the priming run before it."""
    folds = split_walk_forward(make_bars(300), train_size=100, test_size=30, warmup=20)
    window = folds[0].test

    curve = [
        EquityPoint(
            timestamp=b.timestamp,
            equity=Decimal("100"),
            cash=Decimal("100"),
            positions_value=Decimal("0"),
        )
        for b in window.feed()
    ]
    scored = window.score_slice(curve)

    # 30 scored bars plus one baseline point taken from the end of warm-up.
    assert len(scored) == 31
    assert scored[0].timestamp < window.start
    assert scored[1].timestamp == window.start
    assert scored[-1].timestamp == window.end


def test_the_baseline_point_is_what_makes_the_first_return_real() -> None:
    """Without it the first scored bar's move vanishes from the metrics."""
    folds = split_walk_forward(make_bars(120), train_size=50, test_size=20, warmup=10)
    window = folds[0].test

    equities = {b.timestamp: Decimal("100") for b in window.feed()}
    equities[window.start] = Decimal("150")  # a +50% move on the first scored bar
    curve = [
        EquityPoint(timestamp=ts, equity=eq, cash=eq, positions_value=Decimal("0"))
        for ts, eq in sorted(equities.items())
    ]

    scored = window.score_slice(curve)
    assert scored[0].equity == Decimal("100")
    assert scored[1].equity == Decimal("150")


def test_warmup_is_clipped_at_the_start_of_the_series() -> None:
    """A warm-up longer than the available history must not wrap or raise."""
    folds = split_walk_forward(make_bars(120), train_size=40, test_size=20, warmup=1000)
    assert len(folds[0].test.warmup) == 40  # everything before the window, no more


def test_zero_warmup_scores_every_point_in_the_window() -> None:
    folds = split_walk_forward(make_bars(120), train_size=50, test_size=20)
    window = folds[0].test
    curve = [
        EquityPoint(
            timestamp=b.timestamp,
            equity=Decimal("100"),
            cash=Decimal("100"),
            positions_value=Decimal("0"),
        )
        for b in window.feed()
    ]
    assert len(window.score_slice(curve)) == 20


# -- Refusing to produce a fake fold -----------------------------------------


def test_a_series_too_short_for_one_fold_raises() -> None:
    """An empty fold list would read as 'nothing degraded'."""
    with pytest.raises(InsufficientDataError, match="at least 130"):
        split_walk_forward(make_bars(100), train_size=100, test_size=30)


def test_a_partial_final_fold_is_dropped_rather_than_shortened() -> None:
    """135 timestamps gives one 30-bar test window, not one and a fragment."""
    folds = split_walk_forward(make_bars(135), train_size=100, test_size=30)
    assert len(folds) == 1
    assert all(len(f.test) == 30 for f in folds)


def test_empty_series_raises() -> None:
    with pytest.raises(InsufficientDataError):
        split_walk_forward([], train_size=10, test_size=5)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"train_size": 0, "test_size": 5}, "train_size"),
        ({"train_size": 10, "test_size": 0}, "test_size"),
        ({"train_size": 10, "test_size": 5, "warmup": -1}, "warmup"),
    ],
)
def test_nonsense_window_sizes_are_rejected(kwargs: dict[str, int], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        split_walk_forward(make_bars(100), **kwargs)  # type: ignore[arg-type]


def test_out_of_order_input_raises_rather_than_being_sorted() -> None:
    """Silently sorting here would hide a feed bug rather than surface it."""
    bars = make_bars(100)
    bars[40], bars[10] = bars[10], bars[40]
    with pytest.raises(OutOfOrderDataError):
        split_walk_forward(bars, train_size=50, test_size=20)


def test_empty_windows_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least one bar"):
        TrainWindow(bars=())
    with pytest.raises(ValueError, match="at least one bar"):
        TestWindow(bars=())


def test_warmup_overlapping_the_scored_window_is_rejected() -> None:
    bars = make_bars(20)
    with pytest.raises(ValueError, match="must all precede"):
        TestWindow(bars=tuple(bars[5:15]), warmup=tuple(bars[:10]))


# -- Multi-symbol ------------------------------------------------------------


def test_a_timestamp_is_never_split_across_the_boundary() -> None:
    """Slicing by list position would hand the fitting side a partial view of a
    moment the test side is also scored on."""
    bars = sorted(
        make_bars(200, symbol="BTCUSDT") + make_bars(200, symbol="ETHUSDT"),
        key=lambda b: (b.timestamp, b.symbol),
    )
    folds = split_walk_forward(bars, train_size=50, test_size=25)

    for fold in folds:
        train_stamps = {b.timestamp for b in fold.train.bars}
        test_stamps = {b.timestamp for b in fold.test.bars}
        assert not (train_stamps & test_stamps)
        # Both symbols present on every timestamp in both windows.
        assert len(fold.train.bars) == 2 * len(train_stamps)
        assert len(fold.test.bars) == 2 * len(test_stamps)


def test_window_sizes_count_timestamps_not_rows() -> None:
    bars = sorted(
        make_bars(120, symbol="BTCUSDT") + make_bars(120, symbol="ETHUSDT"),
        key=lambda b: (b.timestamp, b.symbol),
    )
    fold = split_walk_forward(bars, train_size=50, test_size=25)[0]
    assert len(fold.train) == 100  # 50 timestamps x 2 symbols
    assert len({b.timestamp for b in fold.train.bars}) == 50
