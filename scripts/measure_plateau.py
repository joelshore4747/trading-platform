"""CR-003: is `parameter_stability` too strict on a plateau? (E-015)

    .venv/bin/python scripts/measure_plateau.py --seeds 5 --symbols 12 --instants 900 \
        --persistence 0.0 0.9 0.95

ADR 0005 left one item open: on a broad genuine edge, `neighbour_ratio` reads
a plateau as evidence while `parameter_stability` counts every one-step wander
of the argmax as a change. This script computes an OFFLINE alternative from
each `WalkForwardReport`'s folds — the share of transitions that stayed on the
same cell or moved to an adjacent one (`_is_adjacent`) — and asks two things,
both required (ADR 0003's two-sided rule): does the alternative admit more
noise at zero persistence, and does it recover seeds that failed on stability
alone where an edge exists? Nothing in `verdicts()` is changed here; a gate is
a safety control and changing one is a decision, not a result.

The latent-drift generator ADR 0005 used is not in the repository, so it is
rebuilt here, seeded: a shared factor plus a per-symbol AR(1) drift whose
persistence sets how long a symbol's relative strength lasts. Its information
coefficient (rank correlation of trailing vs forward return across symbols)
is reported so the rows can be read against ADR 0005's table.
"""

from __future__ import annotations

import argparse
import itertools
import math
import random
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.analytics.walkforward import (
    Params,
    WalkForwardReport,
    _is_adjacent,
    walk_forward,
)
from trading_platform.core.events import Bar
from trading_platform.core.instruments import crypto_spot
from trading_platform.engine.backtester import Backtester
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.base import Strategy
from trading_platform.strategy.examples.cross_sectional_momentum import (
    CrossSectionalMomentum,
)

START = datetime(2024, 1, 1, tzinfo=UTC)
GRID: dict[str, list[object]] = {"lookback": [10, 20, 40], "rebalance_every": [5, 10, 20]}


def _q(x: float) -> Decimal:
    return Decimal(str(round(x, 4)))


def persistent_cross_section(
    symbols: list[str], instants: int, *, seed: int, persistence: float
) -> tuple[list[Bar], float]:
    """Bars for a cross-section with a persistent per-symbol drift, and its IC.

    r_i,t = 0.6 * f_t + mu_i,t + 0.010 * e_i,t, with
    mu_i,t = p * mu_i,t-1 + sqrt(1 - p^2) * 0.004 * z_i,t.
    At p = 0 the drift is white noise and no ranking carries forward; as p
    rises, last month's leaders stay leaders. The IC is the mean across
    instants of the Spearman correlation between trailing 20-bar and forward
    20-bar returns across symbols.
    """
    rng = random.Random(seed)
    factor = [rng.gauss(0.0, 0.012) for _ in range(instants)]
    returns: dict[str, list[float]] = {}
    for k, sym in enumerate(symbols):
        local = random.Random(seed * 1000 + k)
        mu = 0.0
        series = []
        for t in range(instants):
            mu = persistence * mu + math.sqrt(1 - persistence**2) * 0.004 * local.gauss(0, 1)
            series.append(0.6 * factor[t] + mu + 0.010 * local.gauss(0, 1))
        returns[sym] = series

    bars: list[Bar] = []
    for k, sym in enumerate(symbols):
        price = 100.0 * (1 + 0.1 * k)
        for t in range(instants):
            open_px = price
            close_px = max(0.5, open_px * (1 + returns[sym][t]))
            hi, lo = max(open_px, close_px), min(open_px, close_px)
            pad = abs(close_px - open_px) * 0.3 + 0.01
            bars.append(
                Bar(
                    symbol=sym,
                    timestamp=START + timedelta(days=t),
                    open=_q(open_px),
                    high=_q(hi + pad),
                    low=_q(max(0.1, lo - pad)),
                    close=_q(close_px),
                    volume=Decimal("1000"),
                )
            )
            price = close_px
    bars.sort(key=lambda b: (b.timestamp, b.symbol))

    # IC: trailing 20 vs forward 20, across symbols, per instant.
    window = 20
    ics = []
    for t in range(window, instants - window, 5):
        trailing = [sum(returns[s][t - window : t]) for s in symbols]
        forward = [sum(returns[s][t : t + window]) for s in symbols]
        ics.append(_spearman(trailing, forward))
    return bars, sum(ics) / len(ics)


def _rank(v: list[float]) -> list[float]:
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    for pos, i in enumerate(order):
        r[i] = float(pos)
    return r


def _spearman(x: list[float], y: list[float]) -> float:
    rx, ry = _rank(x), _rank(y)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (vx * vy) if vx and vy else 0.0


def coord(params: Params) -> tuple[int, ...]:
    return tuple(GRID[axis].index(params[axis]) for axis in GRID)


def plateau_stability(report: WalkForwardReport) -> float:
    """Share of fold transitions that stayed put or moved one step."""
    folds = report.folds
    if len(folds) < 2:
        return 1.0
    kept = 0
    for a, b in itertools.pairwise(folds):
        ca, cb = coord(a.params), coord(b.params)
        if ca == cb or _is_adjacent(ca, cb):
            kept += 1
    return kept / (len(folds) - 1)


def adjacency_of_moves(report: WalkForwardReport) -> float | None:
    """Among transitions that MOVED, the share that moved one step."""
    folds = report.folds
    moves = [(coord(a.params), coord(b.params)) for a, b in itertools.pairwise(folds)]
    moves = [(x, y) for x, y in moves if x != y]
    if not moves:
        return None
    return sum(1 for x, y in moves if _is_adjacent(x, y)) / len(moves)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--symbols", type=int, default=12)
    parser.add_argument("--instants", type=int, default=900)
    parser.add_argument("--persistence", type=float, nargs="+", default=[0.0, 0.9, 0.95])
    args = parser.parse_args(argv)

    symbols = [f"S{k:02d}" for k in range(args.symbols)]
    instruments = {s: crypto_spot(s) for s in symbols}

    def make_strategy(params: Params) -> Strategy:
        return CrossSectionalMomentum(top_k=3, **params)  # type: ignore[arg-type]

    def make_backtester() -> Backtester:
        return Backtester(
            instruments=instruments,
            starting_cash=Decimal("100000"),
            risk_manager=RiskManager(limits=RiskLimits()),
        )

    print(
        f"{'p':>5} {'seed':>4} {'IC':>6} {'stab':>6} {'plat':>6} {'adjmv':>6} "
        f"{'cur':>4} {'alt':>4}  objections(current)"
    )
    summary: dict[float, dict[str, int]] = {}
    started = time.time()
    for p in args.persistence:
        row = {"n": 0, "cur": 0, "alt": 0, "recovered": 0, "stab_only": 0}
        for seed in range(args.seeds):
            bars, ic = persistent_cross_section(symbols, args.instants, seed=seed, persistence=p)
            report = walk_forward(
                bars,
                GRID,
                strategy_factory=make_strategy,
                backtester_factory=make_backtester,
                train_size=300,
                test_size=100,
                warmup=120,
                periods_per_year=365,
                benchmark_symbol=symbols[0],
            )
            verdicts = report.verdicts
            stab_v = [v for v in verdicts if v.startswith("Parameter stability")]
            others = [v for v in verdicts if not v.startswith("Parameter stability")]
            plat = plateau_stability(report)
            alt_verdicts = others + (["plateau stability"] if plat < 0.5 else [])
            cur_ok = not verdicts
            alt_ok = not alt_verdicts
            stab_only = bool(stab_v) and not others
            adj = adjacency_of_moves(report)
            row["n"] += 1
            row["cur"] += cur_ok
            row["alt"] += alt_ok
            row["stab_only"] += stab_only
            row["recovered"] += stab_only and alt_ok
            tags = ";".join(v.split(":")[0].split(" is")[0][:28] for v in verdicts) or "-"
            print(
                f"{p:>5.2f} {seed:>4} {ic:>6.3f} {report.parameter_stability:>6.2f} {plat:>6.2f} "
                f"{'n/a' if adj is None else f'{adj:.2f}':>6} {'ok' if cur_ok else '-':>4} "
                f"{'ok' if alt_ok else '-':>4}  {tags}"
            )
        summary[p] = row
    print(f"\nruntime {time.time() - started:.0f}s")
    print(
        "\nrow   discoveries(current)  discoveries(plateau-aware)  failed-on-stability-alone  recovered"
    )
    for p, row in summary.items():
        print(
            f"{p:>4.2f}  {row['cur']}/{row['n']:<20} {row['alt']}/{row['n']:<26} "
            f"{row['stab_only']}/{row['n']:<25} {row['recovered']}"
        )
    null = summary.get(0.0)
    if null is not None:
        # (1) the null arm: the alternative must not admit more noise than the current gate's rate.
        admitted_more = null["alt"] > null["cur"]
        print(
            f"\n(1) null arm: plateau-aware admits {'MORE' if admitted_more else 'no more'} noise "
            f"({null['alt']} vs {null['cur']} of {null['n']})"
        )
    edge_rows = {p: r for p, r in summary.items() if p > 0}
    recovered = sum(r["recovered"] for r in edge_rows.values())
    stab_only = sum(r["stab_only"] for r in edge_rows.values())
    print(f"(2) detection arm: {recovered} of {stab_only} stability-only failures recovered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
