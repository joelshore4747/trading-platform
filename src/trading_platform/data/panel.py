"""Columnar cross-section: many symbols on one timestamp grid.

``list[Bar]`` is the right shape for the event loop and the wrong shape for
ranking five thousand instruments. Measured on this repository's own data, a
``Bar`` costs **664 bytes**; the same row as float64 columns costs about 48.

============================  =========  ==========  ==========
target                            bars    as ``Bar``   columnar
============================  =========  ==========  ==========
5,000 x 1y daily                  1.8 M      1.2 GB     0.09 GB
5,000 x 1y 15-minute            175.2 M    116.4 GB      8.4 GB
1,500 x 1y 1-minute             788.4 M    523.6 GB     37.8 GB
============================  =========  ==========  ==========

So this is not an optimisation. Object-per-bar cannot hold the cross-section at
any resolution worth ranking on, and no storage layout changes that.

**Where the float boundary is.** ``data/cache.py`` stores prices as strings so
``Decimal`` round-trips exactly, and that stays true — a ``Panel`` is *derived*
from the cache and never written back to it. The rule, stated so it can be
enforced rather than remembered:

    A Panel may not hold cash, price a fill, or reach the portfolio.

It exists to compute cross-sectional statistics, which are float quantities
already: ``analytics/metrics.py`` draws the same boundary for the same reason.
Anything that moves money goes through ``Bar`` and ``Decimal``.

**The grid is an intersection, never a fill.** A rectangle is exactly the shape
that invites forward-filling a hole, and a forward-filled price is a price
nobody could have traded. ``retained`` reports what alignment cost so the
choice stays visible.
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from trading_platform.data.cache import cache_path
from trading_platform.data.intervals import interval_to_timedelta

__all__ = ["FIELDS", "Panel", "PanelError"]

#: Bar attributes a panel can carry. Volume is included because liquidity
#: screens need it; it is not a price and is not treated as one.
FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


class PanelError(ValueError):
    """A panel could not be built, or was asked for something it does not hold."""


@dataclass(frozen=True, slots=True)
class Panel:
    """Symbols x timestamps, one float64 column per symbol per field."""

    symbols: tuple[str, ...]
    #: int64 epoch milliseconds, strictly increasing. Milliseconds rather than
    #: `datetime` objects because 5,000 x 525,600 of the latter is 40 GB.
    timestamps: pa.Array
    interval: timedelta
    #: field name -> Table whose column names are `symbols`, in that order.
    columns: Mapping[str, pa.Table]
    #: How the timestamp axis was built. ``"intersection"`` keeps only
    #: instants every symbol printed — rectangular, and the default. ``"union"``
    #: keeps every instant any symbol printed, with `valid` saying which cells
    #: are real. A point-in-time universe NEEDS the union: hand an
    #: intersection a symbol that listed mid-window and it deletes every
    #: earlier row for every other symbol too, silently undoing the
    #: point-in-time selection that produced it.
    grid: Literal["intersection", "union"] = "intersection"
    #: Share of input bars surviving alignment. 1.0 means the symbols already
    #: shared a grid; anything lower is the price of the intersection.
    #: ``None`` on a union grid, where nothing is dropped and the ratio would
    #: read 1.0 — "no data lost" — precisely when the data is most missing.
    retained: float | None = 1.0
    #: Grid rows each symbol's absence removed, worst first. A symbol
    #: responsible for a large share is not an outage — it is a listing or a
    #: delisting, and it deleted history for every other symbol including
    #: instants long before it died. See `survivorship_warning`.
    rows_removed_by: tuple[tuple[str, int], ...] = ()
    #: (row index, step) for every place the grid jumps more than one
    #: interval. Row `i` and `i - 1` are adjacent in the panel and this far
    #: apart in time.
    gaps: tuple[tuple[int, timedelta], ...] = ()
    #: field name -> boolean Table, same shape as `columns`, true where the
    #: symbol actually printed. Only populated on a union grid; ``None`` on an
    #: intersection, where every cell is real by construction.
    valid: Mapping[str, pa.Table] | None = None
    #: On a union grid, how many rows each symbol did NOT print, worst first.
    #: `gaps` is grid-level and a union grid is contiguous by construction
    #: whenever *any* symbol printed, so a symbol's own three-day outage
    #: vanishes from `is_contiguous` and reappears only here. Differencing
    #: across one of those holes gives a multi-day move labelled as one bar —
    #: the same defect `gaps` exists to surface on an intersection.
    missing_by: tuple[tuple[str, int], ...] = ()

    @property
    def shape(self) -> tuple[int, int]:
        """``(timestamps, symbols)``."""
        return len(self.timestamps), len(self.symbols)

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(self.columns)

    @property
    def is_contiguous(self) -> bool:
        """True when consecutive rows really are one interval apart.

        Grid-level, and on a union grid that is a weaker claim than it sounds:
        the axis can be perfectly regular while an individual symbol is missing
        from a long run of it. Check `missing_by` too on a union panel.
        """
        return not self.gaps

    @property
    def is_complete(self) -> bool:
        """True when the axis is regular AND every symbol printed every row."""
        return not self.gaps and not self.missing_by

    @property
    def largest_gap(self) -> timedelta:
        return max((step for _, step in self.gaps), default=self.interval)

    def survivorship_warning(self, threshold: float = 0.05) -> str | None:
        """What the grid's rectangular shape is hiding, if anything.

        A panel is an intersection, so it guarantees every symbol prints a
        price at every row. That guarantee is itself a future observation: a
        symbol that dies at the midpoint removes rows from *before* it died,
        and a strategy walking the panel from row 0 stands on a rectangle
        whose existence it could not have known about.

        For a universe with genuine entries and exits this is the wrong shape,
        and the honest fix is a per-row validity mask over the union grid
        rather than an intersection. Until that exists, this says so out loud
        when the pattern looks like a listing change rather than an outage.
        """
        if not self.rows_removed_by:
            return None
        rows = len(self.timestamps)
        worst_symbol, worst_rows = self.rows_removed_by[0]
        share = worst_rows / (rows + worst_rows) if rows + worst_rows else 0.0
        if share < threshold:
            return None
        return (
            f"{worst_symbol} alone removed {worst_rows:,} grid rows "
            f"({share:.1%} of the union). That is the signature of a listing or "
            "delisting rather than a collector outage, and an intersection grid "
            "deletes those rows for every other symbol too — including instants "
            "before the symbol died. Cross-sectional results on this panel are "
            "survivorship-conditioned."
        )

    def field(self, name: str) -> pa.Table:
        try:
            return self.columns[name]
        except KeyError:
            raise PanelError(
                f"Panel does not carry {name!r}; it has {list(self.columns)}. "
                "Load it with fields=(...) if you need it."
            ) from None

    def series(self, field: str, symbol: str) -> pa.ChunkedArray:
        """One symbol's column.

        Raises on an unknown symbol. On a **union** grid the column carries
        nulls where the symbol did not print, and Arrow's aggregations skip
        nulls rather than propagating them — so a mean over such a column is a
        mean of the rows that exist, which is usually not what a caller who did
        not check `valid` intends.
        """
        if symbol not in self.symbols:
            raise PanelError(f"{symbol!r} is not in this panel: {list(self.symbols)}")
        column: pa.ChunkedArray = self.field(field).column(symbol)
        return column

    def datetimes(self) -> list[datetime]:
        """The grid as `datetime`s. Materialises; use `timestamps` in hot paths."""
        return [datetime.fromtimestamp(ms / 1000, UTC) for ms in self.timestamps.to_pylist()]

    # -- Cross-sectional operations -------------------------------------------
    #
    # One timestamp at a time, across symbols. Every one of these is blind to
    # the future by construction: a row is a single instant, and nothing here
    # can see the row below it.

    def row(self, field: str, index: int) -> pa.Array:
        """The whole cross-section at one instant, in `symbols` order.

        On a union grid a cell the symbol did not print comes back as null,
        never as a number. Filling it would invent a price nobody could have
        traded, which is the same prohibition `align` carries.
        """
        table = self.field(field)
        if not 0 <= index < len(self.timestamps):
            raise PanelError(f"row {index} outside 0..{len(self.timestamps) - 1}")
        values = [table.column(s)[index].as_py() for s in self.symbols]
        if self.valid is not None:
            mask = self.valid[field]
            values = [
                value if mask.column(symbol)[index].as_py() else None
                for value, symbol in zip(values, self.symbols, strict=True)
            ]
        return pa.array(values, type=pa.float64())

    def _refuse_if_masked(self, operation: str) -> None:
        """Union panels carry holes, and ranking a hole is a modelling choice.

        The shape exists so a point-in-time universe can be represented; the
        operations that would quietly decide what a missing symbol's rank is —
        excluded, worst, median — refuse instead. That decision belongs to the
        cross-sectional API, against a mask that already exists, rather than
        being retrofitted onto one that does not.
        """
        if self.valid is None:
            return
        raise PanelError(
            f"{operation} on a union grid would rank a cross-section containing "
            "cells no symbol printed. Deciding what a missing symbol's rank is "
            "is a modelling choice, not a default. Use grid='intersection' for "
            "the rectangle, or rank through the cross-sectional API."
        )

    def rank_row(self, field: str, index: int, *, descending: bool = True) -> pa.Array:
        """Cross-sectional rank at one instant. 1 is best."""
        self._refuse_if_masked("rank_row")
        return pc.rank(
            self.row(field, index), sort_keys="descending" if descending else "ascending"
        )

    def zscore_row(self, field: str, index: int) -> pa.Array:
        """Cross-sectional z-score at one instant.

        A flat cross-section has zero dispersion and no meaningful z-score, so
        this returns zeros rather than dividing by zero and producing infinities
        that would later read as enormous signals.
        """
        self._refuse_if_masked("zscore_row")
        values = self.row(field, index)
        spread = pc.stddev(values, ddof=1).as_py()
        if not spread:
            return pa.array([0.0] * len(self.symbols), type=pa.float64())
        return pc.divide(pc.subtract(values, pc.mean(values)), pa.scalar(spread))

    # -- Construction ----------------------------------------------------------

    @classmethod
    def load(
        cls,
        root: Path | str,
        market: str,
        symbols: Sequence[str],
        interval: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        fields: Sequence[str] = ("close",),
        grid: Literal["intersection", "union"] = "intersection",
    ) -> Panel:
        """Build a panel from the Parquet cache, aligned on the common grid.

        Reads columns straight out of Parquet and casts them to float64 in
        Arrow. It deliberately does **not** go through ``read_bars``: doing so
        would materialise a ``Bar`` per row, and at the scale a panel exists
        for — 5,000 symbols x a year of daily bars — that is 1.8 million
        objects and 1.2 GB before the panel is even assembled. Building a
        columnar structure by way of one Python object per cell defeats the
        only reason to have one.

        Only the requested columns are decoded, so a close-only panel does not
        pay for open, high, low and volume.
        """
        unknown = [f for f in fields if f not in FIELDS]
        if unknown:
            raise PanelError(f"Unknown field(s) {unknown}. Valid: {list(FIELDS)}")
        if not fields:
            raise PanelError("A panel needs at least one field")
        if not symbols:
            raise PanelError("A panel needs at least one symbol")

        ordered = tuple(sorted(set(symbols)))
        start_ms = None if start is None else int(start.timestamp() * 1000)
        end_ms = None if end is None else int(end.timestamp() * 1000)

        # Pass one: timestamps only. Cheap, and it settles the grid before any
        # price column is decoded, so the columns are only ever built at their
        # final length rather than built wide and filtered down.
        per_symbol_stamps: dict[str, pa.Array] = {}
        #: Where each symbol's window begins in its own file. Kept from pass
        #: one rather than re-derived in pass two: bisecting the already-sliced
        #: array always returns 0, so `picks` held window-relative indices
        #: while `take` applied them to the full column, and every windowed
        #: panel silently read prices from `lo` rows too early. The indices
        #: stayed in range, so nothing raised.
        offsets: dict[str, int] = {}
        for symbol in ordered:
            table = pq.read_table(
                cache_path(root, market, symbol, interval), columns=["timestamp_ms"]
            )
            stamps = table.column("timestamp_ms").combine_chunks()
            lo, hi = _window_bounds(stamps, start_ms, end_ms)
            offsets[symbol] = lo
            per_symbol_stamps[symbol] = stamps.slice(lo, hi - lo)

        # Intersect one symbol at a time and discard each set as it is
        # consumed. Materialising all 5,000 sets to unpack into
        # `set.intersection(*sets)` peaked at 296 MB to produce a 14.6 MB
        # panel — the transient was twenty times the result.
        #
        # The union is accumulated in the same pass because it is needed twice:
        # as the grid itself when `grid="union"`, and to report what each
        # symbol's absence cost when it is not. After the intersection that
        # evidence is gone.
        common_set: set[int] | None = None
        union: set[int] = set()
        for stamps_array in per_symbol_stamps.values():
            one = set(stamps_array.to_pylist())
            union |= one
            common_set = one if common_set is None else (common_set & one)
            del one
        common_set = common_set or set()

        removed = sorted(
            ((symbol, len(union - set(a.to_pylist()))) for symbol, a in per_symbol_stamps.items()),
            key=lambda pair: (-pair[1], pair[0]),
        )
        rows_removed_by = tuple((s_, n) for s_, n in removed if n)

        common = sorted(union) if grid == "union" else sorted(common_set)
        del union
        if not common:
            raise PanelError(
                f"The {len(ordered)} symbols share no timestamp in this window. "
                "An intersection grid cannot be built, and filling the gaps "
                "would invent prices nobody could have traded."
            )
        total = sum(len(a) for a in per_symbol_stamps.values())

        # Row positions of the common grid within each symbol's own file, so
        # pass two never builds a Python object per cell. Each symbol's
        # position map is built and dropped inside the loop for the same
        # reason as the intersection above.
        picks: dict[str, pa.Array] = {}
        present: dict[str, pa.Array] = {}
        for symbol in ordered:
            offset = offsets[symbol]
            position = {t: i for i, t in enumerate(per_symbol_stamps[symbol].to_pylist())}
            if grid == "union":
                # `None` in a take-index yields null, which is exactly what a
                # cell the symbol never printed should read as.
                rows = [None if t not in position else position[t] + offset for t in common]
                picks[symbol] = pa.array(rows, type=pa.int64())
                present[symbol] = pa.array([r is not None for r in rows], type=pa.bool_())
            else:
                picks[symbol] = pa.array([position[t] + offset for t in common], type=pa.int64())
            del position
        del per_symbol_stamps

        # Pass two: one field column per symbol, in Arrow from Parquet to
        # panel. `take` selects the grid rows and `cast` converts the stored
        # strings to float64 without a Python float in between.
        columns: dict[str, pa.Table] = {}
        for field_name in fields:
            arrays = []
            for symbol in ordered:
                table = pq.read_table(
                    cache_path(root, market, symbol, interval), columns=[field_name]
                )
                column = table.column(field_name).combine_chunks()
                arrays.append(pc.cast(column.take(picks[symbol]), pa.float64()))
            columns[field_name] = pa.Table.from_arrays(arrays, names=list(ordered))

        step = interval_to_timedelta(interval)
        if grid == "union":
            absent = sorted(
                ((symbol, present[symbol].to_pylist().count(False)) for symbol in ordered),
                key=lambda pair: (-pair[1], pair[0]),
            )
            missing_by = tuple((symbol, n) for symbol, n in absent if n)
            mask = {
                name: pa.Table.from_arrays(
                    [present[symbol] for symbol in ordered], names=list(ordered)
                )
                for name in fields
            }
            return cls(
                symbols=ordered,
                timestamps=pa.array(common, type=pa.int64()),
                interval=step,
                columns=columns,
                grid="union",
                # Nothing is dropped on a union, so a retention ratio would
                # read 1.0 — "no data lost" — exactly when data is most
                # missing. `valid` carries the truth instead.
                retained=None,
                rows_removed_by=(),
                gaps=_find_gaps(common, step),
                valid=mask,
                missing_by=missing_by,
            )
        return cls(
            symbols=ordered,
            timestamps=pa.array(common, type=pa.int64()),
            interval=step,
            columns=columns,
            grid="intersection",
            retained=(len(common) * len(ordered) / total) if total else 1.0,
            rows_removed_by=rows_removed_by,
            gaps=_find_gaps(common, step),
        )

    @classmethod
    def from_bars(
        cls,
        loaded: Mapping[str, Sequence[object]],
        *,
        interval: timedelta,
        fields: Sequence[str] = ("close",),
    ) -> Panel:
        """Build from in-memory bars, reporting exactly what `load` reports.

        It used to report less: `rows_removed_by` and `gaps` defaulted to
        empty, so a panel built from a symbol that died mid-sample plus a
        twenty-day hole came back claiming `is_contiguous=True` and
        `survivorship_warning()=None`. Two constructors for one type with
        different honesty guarantees, and the quieter one was the untested one.
        A parity test now asserts they agree.
        """
        empty = sorted(s for s, bars in loaded.items() if not bars)
        if empty:
            raise PanelError(
                f"No bars for {empty}. Dropping them silently would make "
                "`panel.symbols` differ from what was asked for with nothing "
                "recording it; pass only symbols you have data for."
            )
        present = {s: list(bars) for s, bars in loaded.items()}
        if not present:
            raise PanelError("Every symbol was empty; there is no grid to build")

        grids = [{b.timestamp for b in bars} for bars in present.values()]  # type: ignore[attr-defined]
        union: set[datetime] = set().union(*grids)
        common = sorted(set.intersection(*grids))
        if not common:
            raise PanelError(
                f"The {len(present)} symbols share no timestamp. An intersection "
                "grid cannot be built, and filling the gaps would invent prices "
                "nobody could have traded."
            )

        total = sum(len(b) for b in present.values())
        kept = set(common)
        symbols = tuple(sorted(present))
        removed = sorted(
            (
                (symbol, len(union - {b.timestamp for b in bars}))  # type: ignore[attr-defined]
                for symbol, bars in present.items()
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )

        columns: dict[str, pa.Table] = {}
        for field_name in fields:
            arrays = []
            for symbol in symbols:
                by_ts = {
                    b.timestamp: getattr(b, field_name)  # type: ignore[attr-defined]
                    for b in present[symbol]
                    if b.timestamp in kept  # type: ignore[attr-defined]
                }
                arrays.append(pa.array([float(by_ts[t]) for t in common], type=pa.float64()))
            columns[field_name] = pa.Table.from_arrays(arrays, names=list(symbols))

        stamps_ms = [int(t.timestamp() * 1000) for t in common]
        return cls(
            symbols=symbols,
            timestamps=pa.array(stamps_ms, type=pa.int64()),
            interval=interval,
            columns=columns,
            retained=(len(common) * len(symbols) / total) if total else 1.0,
            rows_removed_by=tuple((s_, n) for s_, n in removed if n),
            gaps=_find_gaps(stamps_ms, interval),
        )

    def __repr__(self) -> str:
        rows, cols = self.shape
        parts = [
            f"Panel({cols} symbols x {rows} timestamps",
            f"fields={list(self.columns)}",
            f"retained={self.retained:.1%}" if self.retained is not None else f"grid={self.grid}",
        ]
        if self.gaps:
            parts.append(f"gaps={len(self.gaps)} (largest {self.largest_gap})")
        if self.missing_by:
            worst, count = self.missing_by[0]
            parts.append(f"missing={len(self.missing_by)} symbols (worst {worst}: {count})")
        if self.survivorship_warning() is not None:
            parts.append("SURVIVORSHIP-CONDITIONED")
        return ", ".join(parts) + ")"


def _window_bounds(stamps: pa.Array, start_ms: int | None, end_ms: int | None) -> tuple[int, int]:
    """Half-open slice bounds for ``[start_ms, end_ms]`` inclusive.

    Bisection over the sorted column rather than a row-wise comparison, for the
    same reason ``cache.read_bars`` does it: the comparison is the cost.
    """
    values = stamps.to_pylist()
    lo = 0 if start_ms is None else bisect.bisect_left(values, start_ms)
    hi = len(values) if end_ms is None else bisect.bisect_right(values, end_ms)
    return lo, max(lo, hi)


def _find_gaps(stamps_ms: Sequence[int], interval: timedelta) -> tuple[tuple[int, timedelta], ...]:
    """Rows whose predecessor is more than one interval behind.

    A panel's `interval` is a declaration; this checks it against the grid
    actually built. It matters because an intersection *creates* holes present
    in neither input, and a cross-sectional return computed as
    ``row(i) - row(i - 1)`` across one of them is a multi-day move labelled as
    a one-bar move. `vendor.load_segments` exists for the same reason on the
    object path — the archive this project holds has seventeen such holes, the
    largest 8.5 days.
    """
    expected = round(interval.total_seconds() * 1000)
    out: list[tuple[int, timedelta]] = []
    for index in range(1, len(stamps_ms)):
        delta = stamps_ms[index] - stamps_ms[index - 1]
        if delta != expected:
            out.append((index, timedelta(milliseconds=delta)))
    return tuple(out)
