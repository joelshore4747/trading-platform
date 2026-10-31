"""Features, computed only from bars that have already printed.

The whole discipline of this package is one sentence: **a feature at bar `i` may
read bars up to and including `i`; a label at bar `i` reads bars after it.** Get
that wrong by one bar and the model learns the answer, the backtest is
spectacular, and nothing about the failure looks like a bug.

So the shape here is deliberately awkward in one specific way: `compute` takes
the whole series and an index, and every function is written as "what did the
last `window` bars say" rather than as a vectorised column. It is slower. It is
also the shape in which an off-by-one is visible, and
`tests/test_ml.py::test_no_feature_can_see_a_bar_that_has_not_printed` walks the
series bar by bar and requires the same answer from a truncated series as from
the whole one — the same guard `strategy/patterns/` carries, for the same reason.

## No numpy, no pandas

Not asceticism: this repository's dependency list is `pyarrow`, and a feature
matrix of a few hundred thousand rows in lists of `float` is fast enough for a
pipeline whose bottleneck is the walk-forward loop. When a deep model is
actually trained — which is gated, see `ml/model.py` — that is the moment to
add a numeric stack, with a measurement showing why.

## Floats here, deliberately

`analytics/metrics.py` is the stated boundary where money becomes `float`, and a
feature is not money: it is a ratio or a z-score fed to arithmetic that has no
exact answer anyway. Prices arrive as `Decimal` and are converted once, at the
top of `compute`, and never converted back.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from trading_platform.core.events import Bar

__all__ = ["FEATURES", "FeatureSet", "FeatureSpec", "compute", "feature_names"]


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """One named feature and the window it needs.

    `window` is how many bars up to and including the current one it reads.
    Recorded rather than inferred, because it decides the warmup: a row computed
    before its window is full is a row computed from a shorter window, and
    silently mixing the two is how a model learns the calendar.
    """

    name: str
    window: int
    #: (bars, index) -> value. Reads `bars[index - window + 1 : index + 1]` and
    #: nothing later. Returns None when the window is not full.
    compute: Callable[[Sequence[Bar], int], float | None]

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ValueError(f"{self.name}: window must be at least 1, got {self.window}")


def _closes(bars: Sequence[Bar], index: int, window: int) -> list[float] | None:
    start = index - window + 1
    if start < 0:
        return None
    return [float(b.close) for b in bars[start : index + 1]]


def _returns(values: Sequence[float]) -> list[float]:
    return [
        (values[i] - values[i - 1]) / values[i - 1]
        for i in range(1, len(values))
        if values[i - 1] != 0
    ]


def _momentum(window: int) -> Callable[[Sequence[Bar], int], float | None]:
    def compute(bars: Sequence[Bar], index: int) -> float | None:
        values = _closes(bars, index, window)
        if values is None or values[0] == 0:
            return None
        return (values[-1] - values[0]) / values[0]

    return compute


def _volatility(window: int) -> Callable[[Sequence[Bar], int], float | None]:
    def compute(bars: Sequence[Bar], index: int) -> float | None:
        values = _closes(bars, index, window)
        if values is None:
            return None
        rets = _returns(values)
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        variance = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(variance)

    return compute


def _range_position(window: int) -> Callable[[Sequence[Bar], int], float | None]:
    """Where the close sits in the window's high-low range. 0 at the low, 1 at
    the high. Uses the CURRENT bar's high and low, which are known at its close."""

    def compute(bars: Sequence[Bar], index: int) -> float | None:
        start = index - window + 1
        if start < 0:
            return None
        window_bars = bars[start : index + 1]
        high = max(float(b.high) for b in window_bars)
        low = min(float(b.low) for b in window_bars)
        if high == low:
            return 0.5  # a flat window has no position; the middle is the honest answer
        return (float(bars[index].close) - low) / (high - low)

    return compute


def _body_fraction(bars: Sequence[Bar], index: int) -> float | None:
    """|close - open| over the bar's range. Single-bar, so window 1."""
    bar = bars[index]
    span = float(bar.high) - float(bar.low)
    if span == 0:
        return 0.0
    return abs(float(bar.close) - float(bar.open)) / span


#: The starting set. Small on purpose: every feature is a dimension the model can
#: fit noise in, and a pipeline that cannot find an edge with six features will
#: not be rescued by sixty. Widening this is a decision, not a default.
FEATURES: tuple[FeatureSpec, ...] = (
    FeatureSpec("mom_5", 5, _momentum(5)),
    FeatureSpec("mom_20", 20, _momentum(20)),
    FeatureSpec("mom_60", 60, _momentum(60)),
    FeatureSpec("vol_20", 20, _volatility(20)),
    FeatureSpec("range_pos_20", 20, _range_position(20)),
    FeatureSpec("body_frac", 1, _body_fraction),
)


def feature_names(specs: Sequence[FeatureSpec] = FEATURES) -> tuple[str, ...]:
    return tuple(spec.name for spec in specs)


@dataclass(frozen=True, slots=True)
class FeatureSet:
    """A feature matrix and the indices it was computed at.

    `indices` is kept because rows are dropped when a window is not yet full,
    so row `k` is not bar `k`. A pipeline that assumed it was would misalign
    every label by the warmup length — a silent, catastrophic, and entirely
    plausible mistake.
    """

    names: tuple[str, ...]
    indices: tuple[int, ...]
    rows: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        if len(self.indices) != len(self.rows):
            raise ValueError(
                f"{len(self.rows)} rows against {len(self.indices)} indices. A feature "
                "matrix that has lost track of which bar each row came from will "
                "misalign every label."
            )
        width = len(self.names)
        for position, row in enumerate(self.rows):
            if len(row) != width:
                raise ValueError(f"row {position} has {len(row)} values against {width} names")

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def warmup(self) -> int:
        """Bars consumed before the first complete row."""
        return self.indices[0] if self.indices else 0


def compute(bars: Sequence[Bar], specs: Sequence[FeatureSpec] = FEATURES) -> FeatureSet:
    """Every complete feature row, with the bar index each came from.

    A row appears only when EVERY feature's window is full. Emitting partial
    rows would mean the first few hundred samples are computed differently from
    the rest, which a model will happily learn as a signal about the calendar.
    """
    if not specs:
        raise ValueError("a feature set with no features is not a feature set")
    indices: list[int] = []
    rows: list[tuple[float, ...]] = []
    for index in range(len(bars)):
        values = [spec.compute(bars, index) for spec in specs]
        if any(value is None for value in values):
            continue
        indices.append(index)
        rows.append(tuple(float(v) for v in values if v is not None))
    return FeatureSet(feature_names(specs), tuple(indices), tuple(rows))
