"""CR-001: are the archive's missing minutes activity-conditioned? (E-013)

    .venv/bin/python scripts/measure_gap_bias.py --cache data/cache --market um --draws 1000 --seed 0

Two pre-registered statistics, written in harness/CREATIVE_RESEARCH.md before this
script existed. Every return is computed strictly within a `load_segments`
segment, never across a hole.

(a) Gap-edge test. For each symbol, the LAST bar before each gap (its |1m log
    return| from the previous bar in the segment) and the FIRST bar after each
    gap (its forward |1m log return| to the next bar in the segment — decided
    before running, because a first bar has no backward return that does not
    cross the hole), plus each edge bar's volume. Compared against `draws`
    samples of hour-matched interior bars (same symbol, same UTC hour): the
    percentile of the edge median inside the null distribution of medians.
    DISPROVED if both medians sit inside [2.5, 97.5] for >= 35 of the symbols.
(b) Day-level test. Per symbol, Spearman rho between a day's coverage fraction
    and its realised variance (mean squared within-segment 1m log return);
    cross-symbol median rho against `draws` shuffles of the day labels.
    DISPROVED if the observed median sits inside the null's [2.5, 97.5] band.
(c) Recorded, not a disproof: the share of missing minutes common to every
    symbol over the common span (intersection / union of the missing sets).

Pure Python on purpose: the repository has no numpy, and 845k rows fit.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.core.events import Bar
from trading_platform.data.cache import cache_path, read_bars
from trading_platform.data.vendor import load_segments

MINUTE = timedelta(minutes=1)


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float:
    if len(x) < 3:
        return float("nan")
    rx, ry = _rank(x), _rank(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (vx * vy) if vx and vy else float("nan")


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return float("nan")
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def percentile_of(value: float, null: list[float]) -> float:
    return 100.0 * sum(1 for v in null if v <= value) / len(null)


def band(null: list[float]) -> tuple[float, float]:
    s = sorted(null)
    return s[int(0.025 * (len(s) - 1))], s[int(0.975 * (len(s) - 1))]


def edge_statistics(
    bars: list[Bar], rng: random.Random, draws: int
) -> tuple[int, float, float, int] | None:
    """(gaps, |r| percentile, volume percentile, edges) or None if too few gaps."""
    segments = load_segments(bars, interval=MINUTE)
    if len(segments) < 2:
        return None
    edge_r: list[float] = []
    edge_v: list[float] = []
    interior_by_hour: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for si, seg in enumerate(segments):
        b = seg.bars
        for i in range(len(b)):
            is_last_before_gap = i == len(b) - 1 and si < len(segments) - 1
            is_first_after_gap = i == 0 and seg.preceded_by_gap is not None
            vol = float(b[i].volume)
            if is_last_before_gap and i >= 1:
                r = abs(math.log(float(b[i].close) / float(b[i - 1].close)))
                edge_r.append(r)
                edge_v.append(vol)
            elif is_first_after_gap and i + 1 < len(b):
                r = abs(math.log(float(b[i + 1].close) / float(b[i].close)))
                edge_r.append(r)
                edge_v.append(vol)
            elif 1 <= i < len(b) - 1:
                r = abs(math.log(float(b[i].close) / float(b[i - 1].close)))
                interior_by_hour[b[i].timestamp.hour].append((r, vol))
    if len(edge_r) < 5:
        return None
    edge_hours = []
    for si, seg in enumerate(segments):
        b = seg.bars
        if si < len(segments) - 1 and len(b) >= 2:
            edge_hours.append(b[-1].timestamp.hour)
        if seg.preceded_by_gap is not None and len(b) >= 2:
            edge_hours.append(b[0].timestamp.hour)
    pools = [interior_by_hour[h] for h in edge_hours if interior_by_hour[h]]
    if len(pools) < 5:
        return None
    null_r: list[float] = []
    null_v: list[float] = []
    for _ in range(draws):
        sample = [rng.choice(pool) for pool in pools]
        null_r.append(median([s[0] for s in sample]))
        null_v.append(median([s[1] for s in sample]))
    return (
        len(segments) - 1,
        percentile_of(median(edge_r), null_r),
        percentile_of(median(edge_v), null_v),
        len(edge_r),
    )


def day_statistics(bars: list[Bar]) -> tuple[list[float], list[float]]:
    """Per day: coverage fraction and realised variance (within-segment)."""
    per_day_count: dict[datetime, int] = defaultdict(int)
    per_day_sq: dict[datetime, list[float]] = defaultdict(list)
    for seg in load_segments(bars, interval=MINUTE):
        b = seg.bars
        for i in range(len(b)):
            day = b[i].timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
            per_day_count[day] += 1
            if i >= 1:
                per_day_sq[day].append(math.log(float(b[i].close) / float(b[i - 1].close)) ** 2)
    days = sorted(d for d in per_day_count if len(per_day_sq[d]) >= 30)
    cov = [per_day_count[d] / 1440 for d in days]
    var = [sum(per_day_sq[d]) / len(per_day_sq[d]) for d in days]
    return cov, var


def missing_minutes(bars: list[Bar], start: datetime, end: datetime) -> set[int]:
    have = {int(b.timestamp.timestamp() // 60) for b in bars}
    lo, hi = int(start.timestamp() // 60), int(end.timestamp() // 60)
    return {m for m in range(lo, hi + 1) if m not in have}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cache", type=Path, default=Path("data/cache"))
    parser.add_argument("--market", default="um")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--draws", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    rng = random.Random(args.seed)

    symbols = sorted(p.name for p in (args.cache / args.market).iterdir() if p.is_dir())
    loaded = {
        s: read_bars(cache_path(args.cache, args.market, s, args.interval), s) for s in symbols
    }
    loaded = {s: b for s, b in loaded.items() if b}
    print(
        f"{len(loaded)} symbols, {sum(len(b) for b in loaded.values()):,} bars, draws={args.draws}"
    )

    # (a)
    rows = []
    inside_both = 0
    fired_r = fired_v = 0
    for sym, bars in loaded.items():
        stats = edge_statistics(bars, rng, args.draws)
        if stats is None:
            rows.append((sym, "too few gaps", "", "", ""))
            continue
        gaps, pr, pv, n_edges = stats
        r_in = 2.5 <= pr <= 97.5
        v_in = 2.5 <= pv <= 97.5
        inside_both += r_in and v_in
        fired_r += pr > 97.5
        fired_v += pv > 97.5
        rows.append((sym, gaps, f"{pr:5.1f}", f"{pv:5.1f}", n_edges))
    print("\n(a) gap-edge test — percentile of the edge median inside the hour-matched null")
    print(f"{'symbol':<14}{'gaps':>6}{'|r| pct':>9}{'vol pct':>9}{'edges':>7}")
    for r in rows:
        print(f"{r[0]:<14}{r[1]!s:>6}{r[2]!s:>9}{r[3]!s:>9}{r[4]!s:>7}")
    measured = sum(1 for r in rows if r[1] != "too few gaps")
    print(
        f"inside both bands: {inside_both}/{measured}   |r| above 97.5: {fired_r}   "
        f"volume above 97.5: {fired_v}"
    )
    verdict_a = "DISPROVED (a)" if inside_both >= 35 else "NOT disproved by (a)"

    # (b)
    rhos = []
    per_symbol_days: list[tuple[list[float], list[float]]] = []
    for bars in loaded.values():
        cov, var = day_statistics(bars)
        if len(cov) >= 8:
            rhos.append(spearman(cov, var))
            per_symbol_days.append((cov, var))
    observed = median(rhos)
    null_medians = []
    for _ in range(args.draws):
        shuffled = []
        for cov, var in per_symbol_days:
            v = var[:]
            rng.shuffle(v)
            shuffled.append(spearman(cov, v))
        null_medians.append(median(shuffled))
    lo, hi = band(null_medians)
    print(
        f"\n(b) day-level test — median Spearman(coverage, realised variance) over {len(rhos)} symbols"
    )
    print(
        f"observed {observed:+.3f}; null 2.5-97.5 band [{lo:+.3f}, {hi:+.3f}]; "
        f"percentile {percentile_of(observed, null_medians):.1f}"
    )
    verdict_b = "DISPROVED (b)" if lo <= observed <= hi else "NOT disproved by (b)"

    # (c)
    start = max(b[0].timestamp for b in loaded.values())
    end = min(b[-1].timestamp for b in loaded.values())
    missing = [missing_minutes(b, start, end) for b in loaded.values()]
    common = set.intersection(*missing)
    union = set.union(*missing)
    print(
        f"\n(c) common span {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}: "
        f"missing minutes common to all {len(loaded)} symbols {len(common):,} of "
        f"{len(union):,} in the union ({len(common) / len(union):.1%}); "
        f"median per-symbol missing {int(median([len(m) for m in missing])):,}"
    )

    print(f"\nVERDICT: {verdict_a}; {verdict_b}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
