"""CR-002 stage 1: does BTC's one-minute return lead the alt cross-section? (E-014)

    .venv/bin/python scripts/measure_leadlag.py --cache data/cache --market um \
        --leader BTCUSDT --draws 1000 --diag-draws 200 --seed 0

Pre-registered in harness/CREATIVE_RESEARCH.md before this script existed. One
statistic decides: S(+1) = mean over followers of Spearman(r_leader(t),
r_follower(t+1)), computed only where t and t+1 lie in the same contiguous
segment of the intersection panel (no pair crosses a `panel.gaps` row). Its
null is a circular shift of the leader's returns within each segment by a
random offset of at least `min_shift` rows: the leader keeps its own
autocorrelation, the followers keep their contemporaneous structure, only the
alignment is destroyed. Lags 0 and -1 are diagnostics for the mechanism, and
the conditional-return line is the economics: a lead that does not pay one
round trip of the default cost model is not tradeable here.

Spearman is Pearson on ranks; ranks are standardised once per series and the
correlation is a dot product, so a thousand draws over 38 followers is a
minute, not an hour, without numpy.
"""

from __future__ import annotations

import argparse
import itertools
import math
import random
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.data.panel import Panel
from trading_platform.engine.execution import CostModel


def standardised_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    n = len(ranks)
    mean = sum(ranks) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in ranks) / n) or 1.0
    return [(r - mean) / sd for r in ranks]


def corr(z1: pa.Array, z2: pa.Array) -> float:
    return float(pc.sum(pc.multiply(z1, z2)).as_py()) / len(z1)


def segments_from_gaps(n_rows: int, gap_rows: list[int]) -> list[tuple[int, int]]:
    """Half-open [start, end) row ranges with no gap inside."""
    bounds = [0, *sorted(gap_rows), n_rows]
    return [(a, b) for a, b in itertools.pairwise(bounds) if b - a >= 2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cache", type=Path, default=Path("data/cache"))
    parser.add_argument("--market", default="um")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--leader", default="BTCUSDT")
    parser.add_argument("--draws", type=int, default=1000)
    parser.add_argument("--diag-draws", type=int, default=200)
    parser.add_argument("--min-shift", type=int, default=60)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    rng = random.Random(args.seed)

    symbols = sorted(p.name for p in (args.cache / args.market).iterdir() if p.is_dir())
    panel = Panel.load(args.cache, args.market, symbols, args.interval, fields=("close",))
    n_rows = panel.shape[0]
    gap_rows = [i for i, _ in panel.gaps]
    segs = segments_from_gaps(n_rows, gap_rows)
    print(f"{len(symbols)} symbols, {n_rows:,} rows, {len(gap_rows)} gaps, {len(segs)} segments")

    # Returns per symbol, valid only inside a segment (index t means r(t) = ln c_t / c_{t-1}).
    closes = {s: panel.series("close", s).to_pylist() for s in symbols}
    followers = [s for s in symbols if s != args.leader]

    # Aligned pairs for lag L: (t, t+L) with both r(t) and r(t+L) inside the same segment,
    # i.e. t-1, t, t+L-1, t+L all within [a, b).
    def pair_rows(lag: int) -> list[tuple[int, int]]:
        pairs = []
        for a, b in segs:
            lo = a + 1 + max(0, -lag)
            hi = b - max(0, lag)
            pairs.extend((t, t + lag) for t in range(lo, hi))
        return pairs

    def returns(sym: str, rows: list[int]) -> list[float]:
        c = closes[sym]
        return [math.log(c[t] / c[t - 1]) for t in rows]

    cost = CostModel()
    round_trip = 2 * float(cost.commission_rate + cost.slippage_rate + cost.half_spread_rate)

    results: dict[int, tuple[float, float, int]] = {}
    for lag, draws in ((1, args.draws), (0, args.diag_draws), (-1, args.diag_draws)):
        pairs = pair_rows(lag)
        lead_rows = [t for t, _ in pairs]
        follow_rows = [u for _, u in pairs]
        lead_r = returns(args.leader, lead_rows)
        lead_z = standardised_ranks(lead_r)
        follow_z = {s: pa.array(standardised_ranks(returns(s, follow_rows))) for s in followers}
        observed = sum(corr(pa.array(lead_z), follow_z[s]) for s in followers) / len(followers)

        # Segment boundaries in pair-index space, for the within-segment shift.
        seg_slices: list[tuple[int, int]] = []
        start = 0
        for a, b in segs:
            count = max(0, (b - max(0, lag)) - (a + 1 + max(0, -lag)))
            if count:
                seg_slices.append((start, start + count))
                start += count
        null: list[float] = []
        for _ in range(draws):
            shifted = lead_z[:]
            for s0, s1 in seg_slices:
                length = s1 - s0
                if length <= 2 * args.min_shift:
                    continue
                k = rng.randint(args.min_shift, length - args.min_shift)
                chunk = lead_z[s0:s1]
                shifted[s0:s1] = chunk[k:] + chunk[:k]
            z = pa.array(shifted)
            null.append(sum(corr(z, follow_z[s]) for s in followers) / len(followers))
        null.sort()
        p99 = null[int(0.99 * (len(null) - 1))]
        p025, p975 = null[int(0.025 * (len(null) - 1))], null[int(0.975 * (len(null) - 1))]
        pct = 100 * sum(1 for v in null if v <= observed) / len(null)
        results[lag] = (observed, p99, len(pairs))
        print(
            f"lag {lag:+d}: S = {observed:+.4f}  null 2.5/97.5/99 = "
            f"{p025:+.4f}/{p975:+.4f}/{p99:+.4f}  percentile {pct:.1f}  pairs {len(pairs):,}  draws {draws}"
        )

    # (3) economics: signed next-minute alt return when |r_BTC(t)| is in its top decile.
    pairs = pair_rows(1)
    lead_r = returns(args.leader, [t for t, _ in pairs])
    threshold = sorted(abs(r) for r in lead_r)[int(0.9 * (len(lead_r) - 1))]
    picks = [i for i, r in enumerate(lead_r) if abs(r) >= threshold]
    signed: list[float] = []
    for s in followers:
        fr = returns(s, [u for _, u in pairs])
        signed.extend(math.copysign(1.0, lead_r[i]) * fr[i] for i in picks)
    mean_signed = sum(signed) / len(signed)
    print(
        f"\n(3) |r_BTC| top decile (>= {threshold * 1e4:.1f} bp): mean signed next-minute alt return "
        f"{mean_signed * 1e4:+.2f} bp over {len(signed):,} observations; one round trip of "
        f"CostModel() = {round_trip * 1e4:.0f} bp"
    )

    s1, p99_1, _ = results[1]
    s0 = results[0][0]
    s_m1, p99_m1, _ = results[-1]
    fail_1 = s1 < p99_1
    fail_2 = not (s0 > 3 * abs(s1)) or s_m1 >= p99_m1
    fail_3 = mean_signed <= round_trip
    print(
        f"\n(1) S(+1) above the 99th pct: {'yes' if not fail_1 else 'NO'}   "
        f"(2) lag 0 dwarfs lag +1 and lag -1 inside its band: {'yes' if not fail_2 else 'NO'}   "
        f"(3) conditional return beats one round trip: {'yes' if not fail_3 else 'NO'}"
    )
    verdict = "SURVIVES stage 1" if not (fail_1 or fail_2 or fail_3) else "DISPROVED"
    print(
        f"VERDICT: {verdict} — failed: {[n for n, f in ((1, fail_1), (2, fail_2), (3, fail_3)) if f]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
