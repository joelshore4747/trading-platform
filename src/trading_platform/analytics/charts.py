"""A minimal SVG chart kit.

Hand-written rather than delegated to a plotting library, for two reasons that
both matter more than convenience.

**Testability.** A PNG can only be asserted to exist. An SVG is a string, so a
test can check that the out-of-sample region is shaded, that the benchmark
series is drawn, and that a known point lands at a computable coordinate. The
charts on a tearsheet make claims; claims should be verified.

**Dependency weight.** ``matplotlib`` would be this project's first heavyweight
binary dependency, for line charts, an area, and a histogram. The whole kit is a
scale function and a ``<polyline>``.

Colours and typography follow the ``dataviz`` skill: thin marks, hairline
recessive grid, one axis per plot, and a categorical palette validated in both
light and dark modes (blue ``#2a78d6`` / orange ``#eb6834``, all six checks
passing at ΔE 24.7 protan). Colours are referenced through CSS custom
properties defined once in ``tearsheet.py``, so this module never emits a raw
hex value.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

#: Plot geometry. Margins leave room for axis labels without a layout engine.
#: Below this there is no distribution to show, so the figures are printed
#: instead of binned. See the dataviz skill's anti-pattern on one-bar charts.
MIN_TRADES_FOR_HISTOGRAM = 3

DEFAULT_WIDTH = 900
DEFAULT_HEIGHT = 280
MARGIN_LEFT = 68
MARGIN_RIGHT = 18
MARGIN_TOP = 14
MARGIN_BOTTOM = 30


@dataclass(frozen=True, slots=True)
class Box:
    """The pixel rectangle a plot draws into."""

    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    left: int = MARGIN_LEFT
    right: int = MARGIN_RIGHT
    top: int = MARGIN_TOP
    bottom: int = MARGIN_BOTTOM

    @property
    def plot_left(self) -> float:
        return float(self.left)

    @property
    def plot_right(self) -> float:
        return float(self.width - self.right)

    @property
    def plot_top(self) -> float:
        return float(self.top)

    @property
    def plot_bottom(self) -> float:
        return float(self.height - self.bottom)


class Scale:
    """Maps a data domain onto a pixel range, linearly or logarithmically.

    A log scale is the default for equity because on a linear axis a late 20%
    gain draws larger than an early 50% one, so the eye reads the right-hand
    end of every curve as the important part. Log makes equal percentage moves
    equal distances, which is what a return series actually means.
    """

    __slots__ = ("_hi", "_lo", "_range_hi", "_range_lo", "log")

    def __init__(
        self,
        domain_lo: float,
        domain_hi: float,
        range_lo: float,
        range_hi: float,
        *,
        log: bool = False,
    ) -> None:
        if log and domain_lo <= 0:
            raise ValueError(
                f"A log scale needs a positive domain; got {domain_lo}. Equity that "
                "reached zero cannot be drawn on a log axis — the account was gone."
            )
        # A flat series has no extent. Widen it rather than dividing by zero,
        # so a zero-variance run draws as a flat line instead of raising.
        if domain_hi == domain_lo:
            pad = abs(domain_lo) * 0.05 or 1.0
            domain_lo, domain_hi = domain_lo - pad, domain_hi + pad
        self.log = log
        self._lo = math.log10(domain_lo) if log else domain_lo
        self._hi = math.log10(domain_hi) if log else domain_hi
        self._range_lo = range_lo
        self._range_hi = range_hi

    def to(self, value: float) -> float:
        """Pixel position of a data value."""
        if self.log:
            if value <= 0:
                return self._range_lo
            value = math.log10(value)
        fraction = (value - self._lo) / (self._hi - self._lo)
        return self._range_lo + fraction * (self._range_hi - self._range_lo)

    def ticks(self, count: int = 5) -> list[float]:
        """Roughly ``count`` readable values across the domain.

        Rounded to 1/2/5 multiples rather than divided evenly, because an axis
        labelled 39,175 / 79,982 / 163,294 is arithmetically correct and
        useless to read. Fewer, rounder ticks beat more, exact ones.
        """
        if count < 2:
            raise ValueError(f"Need at least 2 ticks, got {count}")
        return self._log_ticks(count) if self.log else self._linear_ticks(count)

    def _linear_ticks(self, count: int) -> list[float]:
        step = nice_step((self._hi - self._lo) / (count - 1))
        value = math.ceil(self._lo / step) * step
        out: list[float] = []
        # A tolerance of one part in a billion, so a tick that should land on
        # the domain edge is not lost to binary representation.
        while value <= self._hi + step * 1e-9:
            out.append(value)
            value += step
        return out if len(out) >= 2 else [self._lo, self._hi]

    def _log_ticks(self, count: int) -> list[float]:
        lo, hi = 10**self._lo, 10**self._hi
        if self._hi - self._lo >= count - 1:
            stride = max(1, math.ceil((self._hi - self._lo) / (count - 1)))
            decade = [
                float(10**k)
                for k in range(math.floor(self._lo), math.ceil(self._hi) + 1, stride)
                if lo * (1 - 1e-9) <= 10**k <= hi * (1 + 1e-9)
            ]
            if len(decade) >= 2:
                return decade
        # Less than a decade of range: step within each decade instead.
        within = sorted(
            float(multiple * 10**k)
            for k in range(math.floor(self._lo), math.ceil(self._hi) + 1)
            for multiple in (1, 2, 5)
            if lo <= multiple * 10**k <= hi
        )
        return within if len(within) >= 2 else [lo, hi]


def points_to_path(points: Sequence[tuple[float, float]]) -> str:
    """An SVG path through the points. Empty input gives an empty path."""
    if not points:
        return ""
    head = f"M {points[0][0]:.2f} {points[0][1]:.2f}"
    tail = " ".join(f"L {x:.2f} {y:.2f}" for x, y in points[1:])
    return f"{head} {tail}".strip()


def points_to_area(points: Sequence[tuple[float, float]], baseline: float) -> str:
    """A closed path from the points down to ``baseline``, for a filled area."""
    if not points:
        return ""
    return (
        f"{points_to_path(points)} "
        f"L {points[-1][0]:.2f} {baseline:.2f} "
        f"L {points[0][0]:.2f} {baseline:.2f} Z"
    )


def line_chart(
    series: Sequence[tuple[str, str, Sequence[tuple[float, float]]]],
    *,
    box: Box,
    y_scale: Scale,
    x_scale: Scale,
    y_format: str = "{:,.0f}",
    zero_rule: bool = False,
    shaded: Sequence[tuple[float, float]] = (),
    fill_first: bool = False,
    label: str = "",
) -> str:
    """One or more line series on a single shared axis.

    ``series`` items are ``(name, css-variable, points)`` in data coordinates.
    A single shared axis is not a stylistic choice — two y-scales on one plot
    invent a correlation the data does not contain, which is the most misleading
    thing a chart can do.

    ``shaded`` marks x-ranges (in data units) as out-of-sample regions.
    """
    parts: list[str] = [
        f'<svg viewBox="0 0 {box.width} {box.height}" class="chart" '
        f'role="img" aria-label="{escape(label or "chart")}" '
        f'preserveAspectRatio="none">'
    ]

    # Out-of-sample shading sits behind everything, so the marks stay legible.
    for lo, hi in shaded:
        x0, x1 = x_scale.to(lo), x_scale.to(hi)
        parts.append(
            f'<rect class="oos" x="{x0:.2f}" y="{box.plot_top:.2f}" '
            f'width="{max(0.0, x1 - x0):.2f}" '
            f'height="{box.plot_bottom - box.plot_top:.2f}" />'
        )

    # Hairline grid, recessive on purpose — never dashed.
    for value in y_scale.ticks():
        y = y_scale.to(value)
        parts.append(
            f'<line class="grid" x1="{box.plot_left:.2f}" y1="{y:.2f}" '
            f'x2="{box.plot_right:.2f}" y2="{y:.2f}" />'
        )
        parts.append(
            f'<text class="tick" x="{box.plot_left - 8:.2f}" y="{y + 4:.2f}" '
            f'text-anchor="end">{escape(y_format.format(value))}</text>'
        )

    if zero_rule:
        y = y_scale.to(0.0)
        parts.append(
            f'<line class="zero" x1="{box.plot_left:.2f}" y1="{y:.2f}" '
            f'x2="{box.plot_right:.2f}" y2="{y:.2f}" />'
        )

    for index, (name, colour, points) in enumerate(series):
        pixels = [(x_scale.to(x), y_scale.to(y)) for x, y in points]
        if fill_first and index == 0:
            parts.append(
                f'<path class="fill" fill="var({colour})" '
                f'd="{points_to_area(pixels, box.plot_bottom)}" />'
            )
        parts.append(
            f'<path class="series" stroke="var({colour})" fill="none" '
            f'data-name="{escape(name)}" d="{points_to_path(pixels)}" />'
        )

    parts.append(
        f'<line class="axis" x1="{box.plot_left:.2f}" y1="{box.plot_bottom:.2f}" '
        f'x2="{box.plot_right:.2f}" y2="{box.plot_bottom:.2f}" />'
    )
    parts.append("</svg>")
    return "".join(parts)


def histogram(
    values: Sequence[float],
    *,
    bins: int = 21,
    box: Box,
    negative_colour: str = "--series-8",
    positive_colour: str = "--series-1",
    label: str = "",
) -> str:
    """Distribution of values, split at zero.

    Wins and losses are a *polarity*, so the two arms take the diverging pair
    (blue / red) rather than two arbitrary categorical slots.
    """
    if not values:
        return '<p class="empty">No closed trades to plot.</p>'
    if bins < 1:
        raise ValueError(f"Need at least 1 bin, got {bins}")
    if len(values) < MIN_TRADES_FOR_HISTOGRAM:
        # A one-bar histogram is a distribution chart with no distribution in
        # it — a stat tile written the long way. The numbers are the chart.
        return stat_row(values)

    lo, hi = min(values), max(values)
    if hi == lo:
        pad = abs(lo) * 0.05 or 1.0
        lo, hi = lo - pad, hi + pad
    width = (hi - lo) / bins
    counts = [0] * bins
    for value in values:
        index = min(bins - 1, int((value - lo) / width))
        counts[index] += 1

    x_scale = Scale(lo, hi, box.plot_left, box.plot_right)
    y_scale = Scale(0, max(counts), box.plot_bottom, box.plot_top)

    parts = [
        f'<svg viewBox="0 0 {box.width} {box.height}" class="chart" '
        f'role="img" aria-label="{escape(label or "trade distribution")}" '
        f'preserveAspectRatio="none">'
    ]
    for index, count in enumerate(counts):
        if count == 0:
            continue
        bin_lo = lo + width * index
        x0, x1 = x_scale.to(bin_lo), x_scale.to(bin_lo + width)
        y = y_scale.to(count)
        colour = negative_colour if bin_lo + width / 2 < 0 else positive_colour
        # A 2px surface gap between adjacent bars, per the mark spec.
        parts.append(
            f'<rect class="bar" fill="var({colour})" x="{x0 + 1:.2f}" y="{y:.2f}" '
            f'width="{max(0.5, x1 - x0 - 2):.2f}" '
            f'height="{box.plot_bottom - y:.2f}" rx="2">'
            f"<title>{count} trade(s), P&amp;L {bin_lo:,.0f} to {bin_lo + width:,.0f}</title>"
            f"</rect>"
        )

    zero_x = x_scale.to(0.0)
    if box.plot_left <= zero_x <= box.plot_right:
        parts.append(
            f'<line class="zero" x1="{zero_x:.2f}" y1="{box.plot_top:.2f}" '
            f'x2="{zero_x:.2f}" y2="{box.plot_bottom:.2f}" />'
        )
    for value in (lo, 0.0, hi) if lo < 0 < hi else (lo, hi):
        parts.append(
            f'<text class="tick" x="{x_scale.to(value):.2f}" '
            f'y="{box.plot_bottom + 18:.2f}" text-anchor="middle">{value:,.0f}</text>'
        )
    parts.append(
        f'<line class="axis" x1="{box.plot_left:.2f}" y1="{box.plot_bottom:.2f}" '
        f'x2="{box.plot_right:.2f}" y2="{box.plot_bottom:.2f}" />'
    )
    parts.append("</svg>")
    return "".join(parts)


def nice_step(rough: float) -> float:
    """Round a rough interval up to the nearest 1, 2, 2.5, or 5 times a power
    of ten — the intervals people read without decoding."""
    if rough <= 0:
        return 1.0
    exponent = math.floor(math.log10(rough))
    magnitude = 10.0**exponent
    base = rough / magnitude
    for candidate in (1.0, 2.0, 2.5, 5.0):
        if base <= candidate:
            return candidate * magnitude
    return 10.0 ** (exponent + 1)


def stat_row(values: Sequence[float]) -> str:
    """Too few values to form a distribution — show them as figures."""
    items = "".join(
        f'<div class="stat"><span class="stat-value">{value:,.2f}</span>'
        f'<span class="stat-label">trade {index} net P&amp;L</span></div>'
        for index, value in enumerate(values, start=1)
    )
    return (
        f'<div class="stat-row">{items}</div>'
        '<p class="empty">Too few trades to have a distribution. These are the trades.</p>'
    )


def legend(entries: Sequence[tuple[str, str]]) -> str:
    """A legend is always present for two or more series.

    Identity must never rest on colour alone — the swatch is paired with a text
    label, and the label wears text ink rather than the series colour.
    """
    if len(entries) < 2:
        return ""
    items = "".join(
        f'<span class="key"><span class="swatch" style="background:var({colour})"></span>'
        f"{escape(name)}</span>"
        for name, colour in entries
    )
    return f'<div class="legend">{items}</div>'
