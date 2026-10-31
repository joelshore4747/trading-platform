"""Splits that cannot leak: purged, embargoed, and forward-only.

An ordinary train/test split is wrong for a time series with forward-looking
labels, and wrong in a way that produces excellent results.

**Purging.** A training sample at bar `i` carries a label computed from bars
`i+1 .. i+horizon`. If any of those fall inside the test window, that sample has
seen the test period — no feature was shared, and the leak is total anyway. Every
training sample whose label window overlaps the test window is removed.

**Embargo.** Bars adjacent to the test window are serially correlated with it.
A training sample immediately after the test period is nearly the same
observation, so a further `embargo` bars are dropped on each side. Purging alone
leaves that in.

**Forward only.** Test windows advance through time and training data is always
before the test window (rolling) or from the start up to it (anchored). Training
on data after the test period is the mistake this whole module is shaped to make
impossible, and there is no argument that enables it.

Indices here are BAR indices, not row numbers. `FeatureSet` drops rows whose
windows are not full, so row `k` is not bar `k`, and a splitter that worked in
row numbers would misalign everything by the warmup length.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["Split", "purged_splits"]


@dataclass(frozen=True, slots=True)
class Split:
    """One fold: which bar indices train, which test, and what was dropped."""

    train: tuple[int, ...]
    test: tuple[int, ...]
    #: Samples removed because their label window reached into the test window.
    purged: int
    #: Samples removed for being adjacent to it.
    embargoed: int

    def __post_init__(self) -> None:
        overlap = set(self.train) & set(self.test)
        if overlap:
            raise ValueError(
                f"{len(overlap)} index/indices appear in both train and test "
                f"(first: {sorted(overlap)[0]}). That is not a split."
            )

    @property
    def is_usable(self) -> bool:
        """A fold with nothing left to train on measures nothing."""
        return bool(self.train) and bool(self.test)


def purged_splits(
    indices: Sequence[int],
    *,
    horizon: int,
    train_size: int,
    test_size: int,
    embargo: int = 0,
    anchored: bool = False,
) -> list[Split]:
    """Rolling (or anchored) folds with the label window purged and embargoed.

    `indices` are the bar indices that have both features and a label, in order.
    `train_size` and `test_size` count SAMPLES, not bars: with gaps in the series
    those differ, and samples are what a model actually sees.

    `embargo` defaults to 0 so it has to be chosen deliberately — but zero is
    rarely right, and the null harness in `scripts/measure_ml_null.py` is how you
    find out what it should be for a given horizon and instrument.
    """
    if horizon < 1:
        raise ValueError(f"horizon must be at least 1, got {horizon}")
    if train_size < 1 or test_size < 1:
        raise ValueError("train_size and test_size must both be at least 1")
    if embargo < 0:
        raise ValueError(f"embargo must not be negative, got {embargo}")

    ordered = list(indices)
    if ordered != sorted(ordered):
        raise ValueError(
            "indices must be in ascending order; a splitter given shuffled indices "
            "cannot tell which samples are before a test window and which are after"
        )

    splits: list[Split] = []
    position = train_size
    while position + test_size <= len(ordered):
        test = ordered[position : position + test_size]
        test_first, test_last = test[0], test[-1]

        candidates = ordered[:position] if anchored else ordered[position - train_size : position]
        # Anything after the test window as well, so an anchored run and a
        # rolling one are purged by the same rule.
        tail = ordered[position + test_size :]

        train: list[int] = []
        purged = 0
        embargoed = 0
        for index in [*candidates, *tail]:
            # Purge: this sample's label reaches into the test window.
            if index < test_first <= index + horizon:
                purged += 1
                continue
            # Purge the other way: a sample inside or after the test window whose
            # features overlap it.
            if test_first <= index <= test_last:
                purged += 1
                continue
            # Embargo: adjacent on either side.
            if (
                test_first - embargo <= index < test_first
                or test_last < index <= test_last + embargo
            ):
                embargoed += 1
                continue
            # Forward-only for the rolling case: never train on the future.
            if not anchored and index > test_last:
                continue
            train.append(index)

        splits.append(Split(tuple(train), tuple(test), purged, embargoed))
        position += test_size
    return splits
