"""Train/test windows for walk-forward evaluation.

Every performance number this platform produced before this module was
in-sample. Nothing stopped a strategy being tuned on the same data its result
was reported on, which is the most common way a backtest lies without anyone
lying.

The split here is enforced by the **type system**, not by convention.
``TrainWindow`` and ``TestWindow`` are distinct types with no implicit
conversion between them, so fitting on test data requires writing an explicit,
greppable call — exactly the diff a reviewer should catch, and one that
``mypy --strict`` rejects at build time rather than at read time.

Windows are cut on **timestamps**, not on list positions. A multi-symbol series
emits several bars at the same instant, and slicing by position would split
that instant across the train/test boundary — handing the fitting side a
partial view of a moment the test side also sees.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from trading_platform.core.events import Bar, Fill
from trading_platform.data.feed import InMemoryBarFeed, OutOfOrderDataError
from trading_platform.engine.portfolio import EquityPoint


class InsufficientDataError(ValueError):
    """Raised when a series is too short to yield even one complete fold.

    Loud on purpose. Returning an empty fold list would let a caller report
    "no degradation observed" from a run that never happened.
    """


@dataclass(frozen=True, slots=True)
class TrainWindow:
    """Bars a strategy may be **fitted** on.

    Only ``optimise``-shaped code should ever receive one of these. There is no
    conversion to ``TestWindow`` and adding one would defeat the module.
    """

    bars: tuple[Bar, ...]
    index: int = 0

    def __post_init__(self) -> None:
        if not self.bars:
            raise ValueError("A TrainWindow needs at least one bar")

    @property
    def start(self) -> datetime:
        return self.bars[0].timestamp

    @property
    def end(self) -> datetime:
        return self.bars[-1].timestamp

    def __len__(self) -> int:
        return len(self.bars)

    def feed(self) -> InMemoryBarFeed:
        return InMemoryBarFeed(self.bars)


@dataclass(frozen=True, slots=True)
class TestWindow:
    """Bars a strategy may be **evaluated** on, and never fitted on.

    ``warmup`` holds bars from *before* the window that exist only to prime a
    strategy's history. A strategy needing 30 bars of lookback trades nothing
    in the first 30 bars of a window, so without warm-up a short fold measures
    mostly silence. Warm-up bars are fed to the engine but excluded from the
    metrics — see ``score_slice``.

    Positions opened during warm-up may carry into the scored region. That is
    what live trading does, so it is documented rather than engineered away.
    """

    #: Stops pytest trying to collect this as a test class. It is a window.
    __test__: ClassVar[bool] = False

    bars: tuple[Bar, ...]
    warmup: tuple[Bar, ...] = ()
    index: int = 0

    def __post_init__(self) -> None:
        if not self.bars:
            raise ValueError("A TestWindow needs at least one bar")
        if self.warmup and self.warmup[-1].timestamp >= self.bars[0].timestamp:
            raise ValueError(
                "Warm-up bars must all precede the scored window; "
                f"last warm-up bar {self.warmup[-1].timestamp} is not before "
                f"window start {self.bars[0].timestamp}"
            )

    @property
    def start(self) -> datetime:
        """First **scored** timestamp. Warm-up sits before this."""
        return self.bars[0].timestamp

    @property
    def end(self) -> datetime:
        return self.bars[-1].timestamp

    def __len__(self) -> int:
        """Scored length. Warm-up is not part of the measurement."""
        return len(self.bars)

    def feed(self) -> InMemoryBarFeed:
        """Warm-up followed by the scored bars, as one continuous feed."""
        return InMemoryBarFeed(self.warmup + self.bars)

    def scored_fills(self, fills: Sequence[Fill]) -> list[Fill]:
        """The fills this window is measured on.

        A `TestWindow`'s feed is `warmup + bars`, so a run over it produces
        fills in both regions. The equity curve is trimmed by `score_slice`;
        the fill count was not, and `analytics.walkforward.evaluate` reported
        `len(result.fills)` over the whole feed. Measured on 120 warm-up bars
        against 100 scored ones, that overstated by **2.17x** — and
        `median_fills_per_fold < 10`, the guard that decides whether a fold
        has enough trades to mean anything, was reading the inflated number.

        This is the *count*. Round-trip attribution is a different question —
        pairing only these fills would orphan an exit whose entry happened in
        warm-up, and FIFO reads a lone sell as opening a short, inventing a
        round trip that never existed. Callers that need trades pair over the
        full fold and keep what closed inside the window.
        """
        return [f for f in fills if f.timestamp >= self.start]

    def score_slice(self, curve: Sequence[EquityPoint]) -> list[EquityPoint]:
        """The part of an equity curve this window is measured on.

        Includes the last pre-window point when warm-up ran, because a return
        needs a baseline: without it, the first scored bar's move would be
        silently dropped and the window would under-report both gain and loss.
        """
        if not self.warmup:
            return [p for p in curve if p.timestamp >= self.start]

        baseline: EquityPoint | None = None
        for point in curve:
            if point.timestamp < self.start:
                baseline = point
            else:
                break
        inside = [p for p in curve if p.timestamp >= self.start]
        return ([baseline] if baseline is not None else []) + inside


@dataclass(frozen=True, slots=True)
class Fold:
    """One training window and the out-of-sample window that follows it."""

    index: int
    train: TrainWindow
    test: TestWindow

    def __post_init__(self) -> None:
        # The harness's own lookahead guard. If this ever fires, the split is
        # feeding the fitting side data the test side is scored on.
        if self.train.end >= self.test.start:
            raise ValueError(
                f"Fold {self.index}: training data ends at {self.train.end}, which is "
                f"not before the test window start {self.test.start}. This fold would "
                "score a strategy on bars it was fitted on."
            )

    def describe(self) -> str:
        return (
            f"fold {self.index}: train {self.train.start:%Y-%m-%d}..{self.train.end:%Y-%m-%d} "
            f"({len(self.train)} bars) -> test {self.test.start:%Y-%m-%d}..{self.test.end:%Y-%m-%d} "
            f"({len(self.test)} bars)"
        )


def split_walk_forward(
    bars: Iterable[Bar],
    *,
    train_size: int,
    test_size: int,
    anchored: bool = False,
    warmup: int = 0,
) -> list[Fold]:
    """Cut a bar series into successive train/test folds.

    Args:
        bars: the full series, in non-decreasing timestamp order.
        train_size: training length, counted in **distinct timestamps**.
        test_size: out-of-sample length, in distinct timestamps. Test windows
            are laid end to end, so concatenating fold results double-counts
            nothing.
        anchored: when True the training window grows from the start of the
            series — the "all history so far" case a live system actually has.
            When False (default) it is a fixed-length window that slides.
        warmup: timestamps of history prepended to each test window for
            priming only. Excluded from that window's metrics.

    Raises:
        InsufficientDataError: if the series cannot yield one complete fold.
            An empty list would read as "nothing degraded", which is a lie.
    """
    if train_size < 1:
        raise ValueError(f"train_size must be >= 1, got {train_size}")
    if test_size < 1:
        raise ValueError(f"test_size must be >= 1, got {test_size}")
    if warmup < 0:
        raise ValueError(f"warmup must be >= 0, got {warmup}")

    ordered = list(bars)
    if not ordered:
        raise InsufficientDataError("No bars to split")

    by_timestamp: dict[datetime, list[Bar]] = {}
    last: datetime | None = None
    for bar in ordered:
        if last is not None and bar.timestamp < last:
            raise OutOfOrderDataError(
                f"{bar.symbol}: bar at {bar.timestamp} follows {last}. Sort the series "
                "before splitting it; silently sorting here would hide a feed bug."
            )
        last = bar.timestamp
        by_timestamp.setdefault(bar.timestamp, []).append(bar)

    stamps = list(by_timestamp)
    total = len(stamps)
    if total < train_size + test_size:
        raise InsufficientDataError(
            f"Need at least {train_size + test_size} distinct timestamps for one fold "
            f"(train {train_size} + test {test_size}), got {total}. Lengthen the series "
            "or shorten the windows — a partial fold is not a fold."
        )

    def bars_between(lo: int, hi: int) -> tuple[Bar, ...]:
        """Bars at timestamps ``stamps[lo:hi]``, in feed order."""
        out: list[Bar] = []
        for stamp in stamps[lo:hi]:
            out.extend(by_timestamp[stamp])
        return tuple(out)

    folds: list[Fold] = []
    train_end = train_size
    index = 0
    while train_end + test_size <= total:
        train_start = 0 if anchored else train_end - train_size
        test_start = train_end
        test_end = test_start + test_size

        warmup_start = max(0, test_start - warmup) if warmup else test_start
        folds.append(
            Fold(
                index=index,
                train=TrainWindow(bars=bars_between(train_start, train_end), index=index),
                test=TestWindow(
                    bars=bars_between(test_start, test_end),
                    warmup=bars_between(warmup_start, test_start),
                    index=index,
                ),
            )
        )
        train_end += test_size
        index += 1

    return folds
