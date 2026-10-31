"""Labels: what happened NEXT, which is exactly what a feature must never see.

A label at bar `i` reads bars `i+1 .. i+horizon`. That is the point of a label
and it is also why the last `horizon` rows of any series have none — and why a
training set that touches a test set's label window is contaminated even though
no feature was shared. `ml/splits.py` is about that.

**Fills are at the next open, here as everywhere.** The engine's rule is that an
order submitted on bar T fills at T+1's open. A label measured close-to-close
therefore describes a trade nobody could have made: the decision is taken at T's
close, so the entry is T+1's open. `forward_return` uses open-to-open by
default, and the close-to-close variant exists only for research on the price
process itself, with a name that says so.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from trading_platform.core.events import Bar

__all__ = ["LabelKind", "Labels", "forward_return", "label_series"]


class LabelKind(StrEnum):
    #: Entry at the next open, exit at the open `horizon` bars later. What a
    #: strategy acting on this bar's close could actually have traded.
    OPEN_TO_OPEN = "open_to_open"
    #: Close to close. A property of the price process, NOT a tradeable return:
    #: it assumes a fill at a price that was already history when the decision
    #: was taken. For studying the series, never for sizing a position.
    CLOSE_TO_CLOSE = "close_to_close"


def forward_return(
    bars: Sequence[Bar],
    index: int,
    horizon: int,
    kind: LabelKind = LabelKind.OPEN_TO_OPEN,
) -> float | None:
    """The return a decision at `index`'s close would have earned.

    None when the horizon runs past the end of the series — an absent label, not
    a zero. A zero would be a claim that the market did not move.
    """
    if horizon < 1:
        raise ValueError(f"horizon must be at least 1 bar, got {horizon}")
    if kind is LabelKind.OPEN_TO_OPEN:
        entry_index, exit_index = index + 1, index + 1 + horizon
    else:
        entry_index, exit_index = index, index + horizon
    if exit_index >= len(bars) or entry_index >= len(bars):
        return None
    entry = float(
        bars[entry_index].open if kind is LabelKind.OPEN_TO_OPEN else bars[entry_index].close
    )
    exit_price = float(
        bars[exit_index].open if kind is LabelKind.OPEN_TO_OPEN else bars[exit_index].close
    )
    if entry == 0:
        return None
    return (exit_price - entry) / entry


@dataclass(frozen=True, slots=True)
class Labels:
    """Labels aligned to bar indices, with the horizon they cover.

    `horizon` travels with the labels because every later decision needs it:
    purging a split, embargoing one, and knowing how many rows at the end have
    no answer. A pipeline that had to be told the horizon twice would eventually
    be told two different numbers.
    """

    kind: LabelKind
    horizon: int
    indices: tuple[int, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.indices) != len(self.values):
            raise ValueError("labels and indices are different lengths")
        if self.horizon < 1:
            raise ValueError(f"horizon must be at least 1, got {self.horizon}")

    def __len__(self) -> int:
        return len(self.values)

    def signs(self) -> tuple[int, ...]:
        """+1, -1 or 0. A zero return is its own class and is not folded into
        either side: on a coarse instrument it is common, and calling it "up"
        would put a thumb on the scale."""
        return tuple(0 if v == 0 else (1 if v > 0 else -1) for v in self.values)


def label_series(
    bars: Sequence[Bar],
    indices: Sequence[int],
    horizon: int,
    kind: LabelKind = LabelKind.OPEN_TO_OPEN,
) -> Labels:
    """Label the given bar indices, dropping those whose horizon runs off the end."""
    kept: list[int] = []
    values: list[float] = []
    for index in indices:
        value = forward_return(bars, index, horizon, kind)
        if value is None:
            continue
        kept.append(index)
        values.append(value)
    return Labels(kind, horizon, tuple(kept), tuple(values))
