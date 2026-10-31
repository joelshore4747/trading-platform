"""Importing real vendor bar files.

Written against the first real market data this project has had: 40 Binance
perpetual symbols at 1-minute resolution. Two properties of that file decided
the whole design, and both are the kind of thing that produces a plausible
backtest rather than an error when got wrong.

**Timestamps are open-stamped; this repository stamps bars at their close.**
Binance's API returns ``openTime``, and a collector that writes it straight out
produces a file whose stamps are one interval early. Loading that as
close-stamped is a silent one-bar lookahead — at 1-minute frequency, on a
short-horizon strategy, that is the difference between a result and a fiction.

The convention was determined from the data rather than assumed. Funding settles
at exactly 00:00/08:00/16:00 UTC and the trading burst lands in the minute
*starting* at settlement. Median volume against baseline: bars stamped ``:00``
ran **2.67x**, ``:01`` 2.00x, ``:02`` 1.37x, and ``:59`` 0.98x — normal. Under
close-stamping the settlement minute would carry the label ``:01`` and ``:00``
would be quiet. It is the other way round, so the file is open-stamped.

**Gaps are segment boundaries, not continuity.** That file reads as 32 days but
covers 46% of them: seventeen holes, the largest 8.5 days. Binance perpetuals do
not halt for 8.5 days, so those are collector outages. Concatenating across them
would teach every strategy that 8.5 days is one minute — a 60-bar moving average
spanning the seam compares prices a week and a half apart. ``load_segments``
cuts the series at every hole so no indicator can span one.
"""

from __future__ import annotations

import csv
import io
import itertools
import zipfile
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from trading_platform.core.events import Bar
from trading_platform.core.instruments import Instrument, crypto_perpetual, crypto_spot
from trading_platform.data.feed import InMemoryBarFeed, OutOfOrderDataError
from trading_platform.data.intervals import INTERVAL_MS, interval_to_timedelta

#: Columns a vendor file must provide. `source` is optional but checked when present.
REQUIRED_COLUMNS = frozenset({"timestamp", "open", "high", "low", "close", "volume"})

Stamp = Literal["open", "close"]

#: What this project uses everywhere. Stated once so the reason is in one place.
NATIVE_STAMP: Stamp = "close"


class VendorFormatError(ValueError):
    """The file is not shaped the way the loader was told to expect."""


@dataclass(frozen=True, slots=True)
class Segment:
    """A contiguous run of bars, with the hole that precedes it.

    The unit a strategy should actually be run over. Feeding a gapped series as
    one stream is how a moving average ends up spanning an eight-day outage
    without anything raising.
    """

    symbol: str
    bars: tuple[Bar, ...]
    #: Duration of the hole immediately BEFORE this segment. None for the first.
    preceded_by_gap: timedelta | None = None

    def __post_init__(self) -> None:
        if not self.bars:
            raise ValueError("A segment needs at least one bar")

    @property
    def start(self) -> datetime:
        return self.bars[0].timestamp

    @property
    def end(self) -> datetime:
        return self.bars[-1].timestamp

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def __len__(self) -> int:
        return len(self.bars)

    def feed(self) -> InMemoryBarFeed:
        return InMemoryBarFeed(self.bars)

    def describe(self) -> str:
        before = "" if self.preceded_by_gap is None else f" (after a {self.preceded_by_gap} gap)"
        return (
            f"{self.symbol}: {len(self.bars)} bars, "
            f"{self.start:%Y-%m-%d %H:%M} -> {self.end:%Y-%m-%d %H:%M}{before}"
        )


def split_filename(name: str) -> tuple[str, str | None]:
    """``historical/btcusdt_1m.csv`` -> ``("BTCUSDT", "1m")``.

    The interval is returned rather than discarded. It used to be stripped and
    thrown away, which meant ``load_zip`` applied one caller-supplied interval
    to every member: an archive mixing ``btcusdt_1m.csv`` and ``ethusdt_1h.csv``
    parsed as all-1m and shifted every ETH close-stamp by an hour, silently.

    ``None`` when the filename carries no interval, which is not an error —
    plenty of archives name files by symbol alone.
    """
    stem = Path(name).stem
    for interval in INTERVAL_MS:
        suffix = f"_{interval}"
        if stem.endswith(suffix):
            return stem[: -len(suffix)].upper(), interval
    return stem.upper(), None


def symbol_from_filename(name: str) -> str:
    """``historical/btcusdt_1m.csv`` -> ``BTCUSDT``."""
    return split_filename(name)[0]


def instrument_for(symbol: str, source: str | None) -> Instrument:
    """Pick the instrument type from the vendor's own source tag.

    ``binance_futures:BTCUSDT`` is a perpetual, not spot. The difference is not
    cosmetic: perpetuals accrue funding, and a backtest that treats one as spot
    silently omits a carry cost that runs to double digits annualised.
    """
    venue = (source or "").partition(":")[0]
    if venue.endswith(("_futures", "_perp", "_perps", "_swap")):
        return crypto_perpetual(symbol)
    return crypto_spot(symbol)


def load_bars(
    source: Path | str | io.TextIOBase,
    *,
    symbol: str | None = None,
    interval: timedelta = timedelta(minutes=1),
    stamp: Stamp = "open",
    strict_symbol: bool = True,
) -> list[Bar]:
    """Read one vendor CSV into ``Bar`` objects, stamped at close.

    Args:
        source: path to a CSV, or an open text handle (so a zip member works).
        symbol: overrides the filename-derived symbol.
        interval: bar duration. Used to convert an open stamp to a close stamp,
            so passing the wrong one shifts every bar.
        stamp: the convention **the file uses**. ``"open"`` is Binance's, and
            this project's default because that is what real files carry.
            Getting this wrong is a one-bar lookahead, not an offset.
        strict_symbol: require the ``source`` column to agree with the symbol.

    Raises:
        VendorFormatError: missing columns, or a symbol the file contradicts.
        OutOfOrderDataError: timestamps that go backwards. Never silently sorted
            — that would hide a collector bug rather than surface it.
    """
    if stamp not in ("open", "close"):
        raise VendorFormatError(f"stamp must be 'open' or 'close', got {stamp!r}")

    handle, name, should_close = _open(source)
    resolved = symbol or symbol_from_filename(name)

    try:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise VendorFormatError(f"{name} is missing required column(s): {sorted(missing)}")

        shift = interval if stamp == "open" else timedelta(0)
        bars: list[Bar] = []
        last: datetime | None = None
        #: Raw stamps, kept so the declared interval can be checked against the
        #: spacing the file actually has. Under-declaring is a lookahead: the
        #: shift that turns an open stamp into a close stamp is exactly
        #: `interval`, so declaring 1m for a 5m file stamps every bar four
        #: minutes before its true close. Over-declaring is merely late.
        raw_stamps: list[datetime] = []

        for line, row in enumerate(reader, start=2):
            row_source = row.get("source")
            if strict_symbol and row_source:
                declared = row_source.partition(":")[2] or row_source
                if declared.upper() != resolved.upper():
                    raise VendorFormatError(
                        f"{name} line {line}: filename says {resolved} but the source "
                        f"column says {declared}. Two sources of truth that disagree is "
                        "how a BTC file gets loaded as ETH — refusing rather than picking."
                    )

            parsed = _parse_timestamp(row["timestamp"], name, line)
            raw_stamps.append(parsed)
            timestamp = parsed + shift
            if last is not None and timestamp < last:
                raise OutOfOrderDataError(
                    f"{name} line {line}: {timestamp} follows {last}. Sort the source "
                    "before importing; sorting here would hide a collector bug."
                )
            last = timestamp

            bars.append(
                Bar(
                    symbol=resolved,
                    timestamp=timestamp,
                    open=_decimal(row["open"], name, line, "open"),
                    high=_decimal(row["high"], name, line, "high"),
                    low=_decimal(row["low"], name, line, "low"),
                    close=_decimal(row["close"], name, line, "close"),
                    volume=_decimal(row["volume"], name, line, "volume"),
                )
            )
        _check_declared_interval(raw_stamps, interval, name)
        return bars
    finally:
        if should_close:
            handle.close()


def load_segments(
    bars: Iterable[Bar],
    *,
    interval: timedelta = timedelta(minutes=1),
    tolerance: timedelta | None = None,
) -> list[Segment]:
    """Cut a bar series at every hole.

    A step larger than ``interval`` (plus ``tolerance``) ends the current
    segment. Returning segments rather than one series is the whole point: it
    makes a gap impossible to accidentally treat as continuity.
    """
    ordered = list(bars)
    if not ordered:
        return []

    limit = interval + (tolerance or timedelta(0))
    segments: list[Segment] = []
    current: list[Bar] = [ordered[0]]
    gap_before: timedelta | None = None

    for previous, bar in itertools.pairwise(ordered):
        step = bar.timestamp - previous.timestamp
        if step > limit:
            segments.append(
                Segment(
                    symbol=current[0].symbol,
                    bars=tuple(current),
                    preceded_by_gap=gap_before,
                )
            )
            gap_before = step
            current = [bar]
        else:
            current.append(bar)

    segments.append(
        Segment(symbol=current[0].symbol, bars=tuple(current), preceded_by_gap=gap_before)
    )
    return segments


def load_zip(
    path: Path | str,
    *,
    interval: timedelta = timedelta(minutes=1),
    stamp: Stamp = "open",
    symbols: Iterable[str] | None = None,
) -> dict[str, list[Bar]]:
    """Load every CSV in a zip, keyed by symbol.

    Reads members directly rather than unpacking 68 MB of CSV to disk.
    """
    wanted = {s.upper() for s in symbols} if symbols is not None else None
    out: dict[str, list[Bar]] = {}

    with zipfile.ZipFile(path) as archive:
        for member in sorted(archive.namelist()):
            if not member.lower().endswith(".csv"):
                continue
            symbol, named_interval = split_filename(member)
            if wanted is not None and symbol not in wanted:
                continue
            if named_interval is not None:
                declared = interval_to_timedelta(named_interval)
                if declared != interval:
                    raise VendorFormatError(
                        f"{member}: filename says {named_interval} "
                        f"({declared}) but this load was asked for {interval}. "
                        "Refusing to guess — parsing at the wrong interval shifts "
                        "every close-stamp by the difference and nothing "
                        "downstream can detect it. Load each interval separately."
                    )
            with archive.open(member) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                out[symbol] = load_bars(text, symbol=symbol, interval=interval, stamp=stamp)
    return out


def merge(series: Iterable[Iterable[Bar]]) -> list[Bar]:
    """Merge several symbols into one feed-ordered stream.

    Sorted by ``(timestamp, symbol)``, which is the order a multi-symbol feed
    must emit. Safe as of the fix that stopped an order filling against a bar
    stamped at its own creation instant — before that, a cross-sectional run on
    this data would have been fictional.
    """
    merged = [bar for one in series for bar in one]
    merged.sort(key=lambda b: (b.timestamp, b.symbol))
    return merged


def coverage(bars: Iterable[Bar], *, interval: timedelta = timedelta(minutes=1)) -> float:
    """Fraction of expected bars actually present between first and last.

    Reported rather than repaired. The BTC file in ``historical.zip`` scores
    0.46 — it reads as 32 days and is really about 15.
    """
    ordered = list(bars)
    if len(ordered) < 2:
        return 1.0
    span = ordered[-1].timestamp - ordered[0].timestamp
    expected = int(span / interval) + 1
    return len(ordered) / expected if expected > 0 else 1.0


@dataclass(frozen=True, slots=True)
class SymbolQuality:
    """Mechanical health of one symbol's series.

    Exists because eyeballing one symbol and generalising is exactly how a dead
    instrument reaches a backtest. Checking BTCUSDT alone gave "zero
    zero-volume bars" for this archive; TONUSDT has 21,158 of them and QNTUSDT
    has 453.
    """

    symbol: str
    bars: int
    zero_volume_bars: int
    distinct_closes: int
    #: Longest run at one price. Reported, never judged on its own: at 1-minute
    #: resolution BTCUSDT itself sits at 8, so a bare threshold flags the whole
    #: universe, and a check that fires on everything says nothing.
    longest_flat_run: int
    #: Longest run at one price WITH no volume. This is the stalled-feed
    #: signature - a quiet market still prints trades, a broken collector does
    #: not - and it is what `concerns` actually acts on.
    longest_silent_run: int

    @property
    def zero_volume_fraction(self) -> float:
        return self.zero_volume_bars / self.bars if self.bars else 0.0

    @property
    def is_dead(self) -> bool:
        """A frozen price or a total absence of trading. Not a quiet market -
        a perpetual that never printed a trade in a month is a placeholder."""
        return self.distinct_closes <= 1 or self.zero_volume_fraction >= 0.999

    def concerns(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.is_dead:
            out.append(
                f"DEAD: {self.distinct_closes} distinct close(s) across {self.bars} bars, "
                f"{self.zero_volume_fraction:.0%} zero-volume. This is a placeholder, "
                "not market data."
            )
            return tuple(out)
        if self.zero_volume_fraction > 0.01:
            out.append(
                f"{self.zero_volume_bars} zero-volume bars ({self.zero_volume_fraction:.1%}). "
                "A perpetual that printed no trades did not have a price anyone could get."
            )
        if self.longest_silent_run >= 3:
            out.append(
                f"a run of {self.longest_silent_run} bars at one price with no volume. "
                "A quiet market still prints trades; this is a stalled feed."
            )
        return tuple(out)


def assess(bars: Iterable[Bar]) -> SymbolQuality:
    """Mechanical checks over one symbol. No judgement, just counts."""
    ordered = list(bars)
    if not ordered:
        raise ValueError("Cannot assess an empty series")

    longest_flat = flat = 1
    longest_silent = silent = 1 if ordered[0].volume == 0 else 0
    for previous, bar in itertools.pairwise(ordered):
        same_price = bar.close == previous.close
        flat = flat + 1 if same_price else 1
        longest_flat = max(longest_flat, flat)
        silent = silent + 1 if (same_price and bar.volume == 0) else 0
        longest_silent = max(longest_silent, silent)

    return SymbolQuality(
        symbol=ordered[0].symbol,
        bars=len(ordered),
        zero_volume_bars=sum(1 for b in ordered if b.volume == 0),
        distinct_closes=len({b.close for b in ordered}),
        longest_flat_run=longest_flat,
        longest_silent_run=longest_silent,
    )


def screen(
    loaded: dict[str, list[Bar]], *, max_zero_volume_fraction: float | None = None
) -> tuple[dict[str, list[Bar]], dict[str, SymbolQuality]]:
    """Split a universe into usable symbols and the rest.

    Returns ``(kept, quality)`` — quality covers every symbol, including the
    dropped ones, so what was excluded is always inspectable rather than
    silently gone.

    **This is not point-in-time, and the default is deliberately narrow.**
    Every criterion here is computed over the whole file, so membership at the
    start of a sample is decided by behaviour at the end. Two cases, with
    different answers:

    - ``is_dead`` — one distinct close, or essentially no volume, across the
      entire sample. Dropping these is defensible: the symbol was never
      tradeable at any instant, so a point-in-time observer would have
      excluded it too. This is the default and the only default.
    - ``max_zero_volume_fraction`` — a whole-file average. A symbol illiquid
      in month one and liquid in month two is either admitted throughout,
      using month two's liquidity to justify month one, or excluded
      throughout, discarding tradeable history. Neither is point-in-time, so
      it is now opt-in (``None`` disables it) rather than applied silently.

    The correct long-term answer is a rolling, backward-looking liquidity
    screen evaluated inside the event loop. Whole-file screening at import
    time cannot be made point-in-time, only honest about not being it.
    """
    quality = {sym: assess(bars) for sym, bars in loaded.items()}
    kept = {
        sym: bars
        for sym, bars in loaded.items()
        if not quality[sym].is_dead
        and (
            max_zero_volume_fraction is None
            or quality[sym].zero_volume_fraction <= max_zero_volume_fraction
        )
    }
    return kept, quality


def common_grid(loaded: dict[str, list[Bar]]) -> list[datetime]:
    # Survivorship note, stated here as it is on `Panel`: intersecting means
    # a symbol that stops printing removes timestamps from BEFORE it stopped,
    # for every other symbol too. The rectangle that comes out guarantees a
    # full cross-section at every instant, and that guarantee is a future
    # observation. It is still the honest choice against forward-filling, but
    # the object path should not say it more quietly than the columnar one.
    """Timestamps present in EVERY symbol, sorted."""
    if not loaded:
        return []
    stamps: set[datetime] | None = None
    for bars in loaded.values():
        theirs = {b.timestamp for b in bars}
        stamps = theirs if stamps is None else (stamps & theirs)
    return sorted(stamps or set())


def align(loaded: dict[str, list[Bar]]) -> tuple[dict[str, list[Bar]], float]:
    # See `common_grid`: the intersection is survivorship-conditioned, and
    # `retained` is what it cost. `data.panel.Panel` reports the same thing as
    # `rows_removed_by` and `survivorship_warning()`, and offers a union grid
    # for the case where a symbol genuinely lists or delists mid-window.
    """Restrict every symbol to the timestamps they all share.

    Returns the aligned universe and the fraction of bars retained.

    **Intersection, never forward-fill.** The symbols in this archive do not
    share a grid — bar counts run 20,869 to 21,160 — so a cross-sectional
    strategy would otherwise trade a different universe every minute without
    anything saying so. Forward-filling would fix the shape by inventing prices
    that never printed, and a strategy trading an invented price is trading a
    fiction. Dropping the unshared minutes costs data and states the cost.
    """
    grid = set(common_grid(loaded))
    total = sum(len(b) for b in loaded.values())
    aligned = {sym: [b for b in bars if b.timestamp in grid] for sym, bars in loaded.items()}
    kept = sum(len(b) for b in aligned.values())
    return aligned, (kept / total if total else 1.0)


def coverage_by_hour(
    bars: Iterable[Bar], *, interval: timedelta = timedelta(minutes=1)
) -> dict[int, float]:
    """Coverage per UTC hour of day. Uniform coverage is the assumption; check it.

    A total coverage figure hides the shape of what is missing, and the shape is
    what biases results. The BTC file in ``historical.zip`` runs 43-58% for
    hours 00-17 UTC and only **20-30% for hours 18-23** — the US afternoon and
    evening, under-sampled about two to one.

    That is a selection bias in the time dimension, not a random thinning. Any
    intraday seasonality measured on it is distorted, and a strategy that loses
    money in the US evening is under-penalised because it barely trades there.
    """
    ordered = list(bars)
    if not ordered:
        return {}
    span = ordered[-1].timestamp - ordered[0].timestamp
    days = max(1, span.days + 1)
    per_hour = int(timedelta(hours=1) / interval)
    counts: dict[int, int] = {}
    for bar in ordered:
        hour = bar.timestamp.hour
        counts[hour] = counts.get(hour, 0) + 1
    return {h: counts.get(h, 0) / (days * per_hour) for h in range(24)}


def _open(source: Path | str | io.TextIOBase) -> tuple[io.TextIOBase, str, bool]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        return path.open(newline="", encoding="utf-8"), path.name, True
    return source, getattr(source, "name", "<stream>"), False


def _check_declared_interval(stamps: Sequence[datetime], interval: timedelta, name: str) -> None:
    """Refuse a declared interval the file's own spacing contradicts.

    The guard that existed checked the *filename*, which is redundant — if the
    name says ``_5m`` the caller usually knows. The information that settles it
    is in the rows: the modal gap between consecutive stamps IS the interval,
    and it was read and thrown away. So the check lived where it was not
    needed and was missing where it was.

    Only the mode is used, because a real archive has gaps and every gap is a
    larger step. Under-declaring is the dangerous direction and it makes the
    mode *larger* than the declaration, which is what this catches.
    """
    if len(stamps) < 3:
        return
    steps = Counter(
        later - earlier for earlier, later in itertools.pairwise(stamps) if later > earlier
    )
    if not steps:
        return
    modal, count = steps.most_common(1)[0]
    if modal == interval or count < len(stamps) // 4:
        return

    if modal > interval:
        consequence = (
            f"declaring the shorter {interval} stamps every bar "
            f"{modal - interval} BEFORE its true close, which is a lookahead"
        )
    else:
        consequence = (
            f"declaring the longer {interval} stamps every bar "
            f"{interval - modal} AFTER its true close, which is not a lookahead "
            "but is still the wrong timestamp on every row"
        )
    raise VendorFormatError(
        f"{name}: loaded as {interval} but consecutive timestamps are most often "
        f"{modal} apart ({count:,} of {len(stamps):,}). The interval is what "
        f"shifts an open stamp to a close stamp, so {consequence}. Pass the "
        "interval the file actually has."
    )


def _parse_timestamp(raw: str, name: str, line: int) -> datetime:
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VendorFormatError(f"{name} line {line}: bad timestamp {raw!r}") from exc
    if parsed.tzinfo is None:
        raise VendorFormatError(
            f"{name} line {line}: timestamp {raw!r} has no timezone. A naive stamp in "
            "market data is an invitation to an off-by-one-session bug."
        )
    return parsed


def _decimal(raw: str, name: str, line: int, field: str) -> Decimal:
    """Parse straight from the string — never via float.

    ``Decimal(0.1)`` is not ``0.1``, and a price that round-trips through float
    cannot be reconciled against an exchange statement.
    """
    try:
        return Decimal(raw.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise VendorFormatError(f"{name} line {line}: bad {field} value {raw!r}") from exc


def segments_by_symbol(
    loaded: dict[str, list[Bar]], *, interval: timedelta = timedelta(minutes=1)
) -> dict[str, list[Segment]]:
    """``load_segments`` applied across a whole directory or archive."""
    return {sym: load_segments(bars, interval=interval) for sym, bars in loaded.items()}


def iter_bars(segments: Iterable[Segment]) -> Iterator[Bar]:
    """Flatten segments back to bars, for the cases that genuinely want that."""
    for segment in segments:
        yield from segment.bars
