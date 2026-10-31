"""Turning a run configuration into a verdict.

This is the only place the UI knows how to execute anything. It composes the
existing pieces — the generator, the walk-forward harness, the trade pairing —
and hands back a ``Verdict``. No new statistics are computed here; if a number
appears on screen it was produced by the same code path a script would use, so
the UI can never flatter a run that the command line would fail.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading_platform.analytics.metrics import PerformanceSummary, summarise
from trading_platform.analytics.trades import RoundTrip, TradeStats, summarise_trades
from trading_platform.analytics.trials import MultiplicitySummary, TrialRegistry
from trading_platform.analytics.walkforward import (
    Params,
    WalkForwardReport,
    buy_and_hold_curve,
    walk_forward,
)
from trading_platform.analytics.windows import TestWindow
from trading_platform.core.events import Bar
from trading_platform.core.instruments import Instrument, crypto_spot
from trading_platform.data.feed import InMemoryBarFeed
from trading_platform.data.synthetic import Regime, RegimeSpec, generate_bars
from trading_platform.engine.backtester import Backtester, BacktestResult
from trading_platform.engine.execution import (
    BINANCE_PERP_VIP0,
    BINANCE_SPOT_BNB,
    BINANCE_SPOT_VIP0,
    RETAIL_STRESSED,
    ZERO_COST,
    CostModel,
)
from trading_platform.engine.portfolio import EquityPoint
from trading_platform.risk.limits import RiskLimits, RiskManager
from trading_platform.strategy.base import Strategy
from trading_platform.strategy.examples.sma_crossover import SMACrossover
from trading_platform.ui.verdict import Verdict, decide

SYMBOL = "BTCUSDT"
PERIODS_PER_YEAR = 365

#: Named cost models the UI offers. `ZERO_COST` is present but last and
#: labelled, because hiding it would just mean someone reaches for the
#: constructor instead — better it is visible and captioned as not-evidence.
COST_PRESETS: dict[str, tuple[CostModel, str]] = {
    "binance_spot_vip0": (BINANCE_SPOT_VIP0, "Binance spot VIP 0 — 10 bp (default)"),
    "binance_spot_bnb": (BINANCE_SPOT_BNB, "Binance spot, fees in BNB — 7.5 bp"),
    "binance_perp_vip0": (BINANCE_PERP_VIP0, "Binance perpetuals VIP 0 — 5 bp"),
    "retail_stressed": (RETAIL_STRESSED, "Retail, fast market — 10 bp + wide fills"),
    "zero_cost": (ZERO_COST, "Frictionless — engine check only, NEVER evidence"),
}

#: Regime mixes, as compositions rather than free text. Named so a run records
#: which market it was asked to survive.
REGIME_MIXES: dict[str, tuple[list[RegimeSpec], str]] = {
    "full_cycle": (
        [
            RegimeSpec(Regime.TREND, 200),
            RegimeSpec(Regime.CHOP, 200),
            RegimeSpec(Regime.CRASH, 60),
            RegimeSpec(Regime.RECOVERY, 120),
            RegimeSpec(Regime.CHOP, 200),
            RegimeSpec(Regime.TREND, 120),
        ],
        "Full cycle — trend, chop, crash, recovery",
    ),
    "chop_only": (
        [RegimeSpec(Regime.CHOP, 900)],
        "Pure chop — the control. No edge exists here by construction",
    ),
    "trending": (
        [
            RegimeSpec(Regime.TREND, 500),
            RegimeSpec(Regime.CHOP, 200),
            RegimeSpec(Regime.TREND, 200),
        ],
        "Mostly trending — the easy case",
    ),
    "crash_heavy": (
        [
            RegimeSpec(Regime.TREND, 200),
            RegimeSpec(Regime.CRASH, 80),
            RegimeSpec(Regime.CHOP, 200),
            RegimeSpec(Regime.CRASH, 60),
            RegimeSpec(Regime.RECOVERY, 160),
            RegimeSpec(Regime.CHOP, 200),
        ],
        "Crash-heavy — does the risk layer hold?",
    ),
}


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Everything that determines a run. Persisted with the result, so a
    verdict can always be traced back to what produced it."""

    fast_windows: tuple[int, ...] = (5, 10, 15, 20)
    slow_windows: tuple[int, ...] = (30, 50, 80, 120)
    regime_mix: str = "full_cycle"
    cost_preset: str = "binance_spot_vip0"
    seed: int = 7
    start_price: Decimal = Decimal("30000")
    train_size: int = 300
    test_size: int = 100
    warmup: int = 120
    anchored: bool = False
    starting_cash: Decimal = Decimal("100000")
    max_drawdown_pct: Decimal = Decimal("0.25")
    #: What this run is testing, and why an edge should exist. Both required
    #: when the run is being counted — `TrialRegistry.record` refuses an empty
    #: mechanism, and that refusal is the point rather than an obstacle.
    hypothesis: str = ""
    mechanism: str = ""

    def __post_init__(self) -> None:
        if self.regime_mix not in REGIME_MIXES:
            raise ValueError(f"Unknown regime mix {self.regime_mix!r}")
        if self.cost_preset not in COST_PRESETS:
            raise ValueError(f"Unknown cost preset {self.cost_preset!r}")
        if not self.fast_windows or not self.slow_windows:
            raise ValueError("A grid needs at least one value on each axis")

    @property
    def grid(self) -> dict[str, list[object]]:
        return {"fast": list(self.fast_windows), "slow": list(self.slow_windows)}

    @property
    def grid_cells(self) -> int:
        return len(self.fast_windows) * len(self.slow_windows)

    @property
    def cost_model(self) -> CostModel:
        return COST_PRESETS[self.cost_preset][0]

    @property
    def cost_label(self) -> str:
        return COST_PRESETS[self.cost_preset][1]

    @property
    def regime_label(self) -> str:
        return REGIME_MIXES[self.regime_mix][1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fast_windows": list(self.fast_windows),
            "slow_windows": list(self.slow_windows),
            "regime_mix": self.regime_mix,
            "cost_preset": self.cost_preset,
            "seed": self.seed,
            "start_price": str(self.start_price),
            "train_size": self.train_size,
            "test_size": self.test_size,
            "warmup": self.warmup,
            "anchored": self.anchored,
            "starting_cash": str(self.starting_cash),
            "max_drawdown_pct": str(self.max_drawdown_pct),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunConfig:
        return cls(
            fast_windows=tuple(data["fast_windows"]),
            slow_windows=tuple(data["slow_windows"]),
            regime_mix=data["regime_mix"],
            cost_preset=data["cost_preset"],
            seed=int(data["seed"]),
            start_price=Decimal(data["start_price"]),
            train_size=int(data["train_size"]),
            test_size=int(data["test_size"]),
            warmup=int(data["warmup"]),
            anchored=bool(data["anchored"]),
            starting_cash=Decimal(data["starting_cash"]),
            max_drawdown_pct=Decimal(data["max_drawdown_pct"]),
        )


@dataclass(slots=True)
class RunOutcome:
    """A completed run: the judgement, and everything needed to show its work."""

    config: RunConfig
    verdict: Verdict
    report: WalkForwardReport
    result: BacktestResult
    summary: PerformanceSummary
    trades: TradeStats
    benchmark: list[EquityPoint]
    bars: list[Bar] = field(default_factory=list)
    #: The count this verdict should be read against. `None` means the run was
    #: **not** recorded — which the report says out loud rather than leaving a
    #: reader to assume a count exists.
    multiplicity: MultiplicitySummary | None = None
    trial_id: str | None = None

    def verdict_report(self) -> str:
        """The screen: judgement first, then what qualifies it, then evidence.

        Named for the verdict rather than called `report`, because `report` is
        already the `WalkForwardReport` field on this class.

        The multiplicity line sits directly under the verdict and above the
        objections, because it qualifies the verdict itself rather than any one
        of its reasons — and because on a `SURVIVES` it is the most important
        sentence on the page. A pass is the only outcome a false-discovery rate
        can corrupt; a rejection needs no such caveat, so the line states the
        count plainly there and shouts only where shouting is warranted.
        """
        v = self.verdict
        lines = [
            f"{v.outcome}  —  {self.config.regime_label}, {self.config.cost_label}",
            "",
            v.headline,
            "",
        ]

        if self.multiplicity is None:
            lines.append(
                "NOT COUNTED. This run was not recorded in a trial registry, so "
                "nothing here knows how many times this question has been asked. "
                "Pass one to `execute` to find out."
            )
        else:
            m = self.multiplicity
            warning = m.warning()
            if v.outcome.is_pass and warning is not None:
                lines.append(f"READ THIS FIRST: {warning}")
            elif warning is not None:
                lines.append(warning)
            else:
                lines.append(
                    f"Trial {self.trial_id} of {m.trials} recorded "
                    f"({m.parameter_points:,} parameter sets). Family-wise false-discovery "
                    f"chance {m.family_wise:.0%}."
                )
        lines.append("")

        if v.objections:
            lines.append(f"{len(v.objections)} objection(s):")
            lines.extend(f"  {i}. {o}" for i, o in enumerate(v.objections, start=1))
        else:
            lines.append("No objection fired. That is not the same as an edge.")
        lines.append("")

        efficiency = "n/a" if v.efficiency is None else f"{v.efficiency:.2f}"
        lines.append(f"  efficiency (oos/is) : {efficiency}")
        lines.append(f"  mean out-of-sample  : {v.out_of_sample_score:.2f}")
        lines.append(f"  round trips         : {v.trades}")
        lines.append(f"  net P&L             : {v.net_pnl:,.2f}")
        if v.benchmark_beaten is not None:
            lines.append(f"  beat buy-and-hold   : {v.benchmark_beaten}")
        return "\n".join(lines)


def judge(bars: Sequence[Bar], config: RunConfig) -> tuple[Verdict, WalkForwardReport]:
    """Walk a series forward and classify what came out.

    Extracted from `execute` so the false-discovery harness measures the code
    path the product runs rather than a parallel reimplementation of it. Takes
    bars rather than generating them, which is the whole point: the harness
    supplies its own null series.

    The extraction forced a correction. `execute` used to hand `decide()` a
    `TradeStats` built from a **full-series in-sample** backtest while the
    report it was judging was out-of-sample — so `Verdict.trades` and
    `Verdict.net_pnl` described a different run from `Verdict.efficiency`. The
    trades now come from the folds' own out-of-sample fills.
    """
    instruments: dict[str, Instrument] = {SYMBOL: crypto_spot(SYMBOL)}

    def make_strategy(params: Params) -> Strategy:
        return SMACrossover(SYMBOL, **params)  # type: ignore[arg-type]

    def make_backtester() -> Backtester:
        # Fresh every call: RiskManager carries peak-equity state, so a shared
        # instance would leak a halt from one fold into every later one.
        return Backtester(
            instruments=instruments,
            starting_cash=config.starting_cash,
            cost_model=config.cost_model,
            risk_manager=RiskManager(limits=RiskLimits(max_drawdown_pct=config.max_drawdown_pct)),
        )

    report = walk_forward(
        list(bars),
        config.grid,
        strategy_factory=make_strategy,
        backtester_factory=make_backtester,
        train_size=config.train_size,
        test_size=config.test_size,
        warmup=config.warmup,
        anchored=config.anchored,
        periods_per_year=PERIODS_PER_YEAR,
        benchmark_symbol=SYMBOL,
    )

    # Paired WITHIN each fold, then combined — never pooled and re-sorted.
    #
    # Each fold is an independently simulated backtest that starts flat, and
    # with `warmup > 0` a fold's warm-up window reaches back into calendar
    # bars the previous fold already scored. Pooling every fold's fills into
    # one globally sorted stream therefore interleaves two unrelated
    # simulations that happen to share a period, and FIFO pairing matches an
    # entry from one against an exit from the other — inventing round trips
    # that occurred in no single run. At the default 120-bar warm-up against
    # 100-bar test windows, four of five fold transitions overlap.
    #
    # Pairing per fold costs the trades that straddle a fold boundary, which
    # is the right price: a fold that starts flat has no position to straddle
    # with, so those pairings were never real either.
    rounds: list[RoundTrip] = []
    open_quantity: dict[str, Decimal] = {}
    for fold in report.folds:
        stats = summarise_trades(list(fold.fills), instruments)
        rounds.extend(stats.trades)
        for symbol, quantity in stats.open_quantity.items():
            open_quantity[symbol] = open_quantity.get(symbol, Decimal("0")) + quantity
    rounds.sort(key=lambda t: t.exit_time)
    out_of_sample_trades = TradeStats(
        trades=tuple(rounds),
        open_quantity={s: q for s, q in open_quantity.items() if q},
    )

    return decide(report, out_of_sample_trades, decided_at=datetime.now(UTC)), report


def execute(config: RunConfig, registry: TrialRegistry | None = None) -> RunOutcome:
    """Run the configuration and judge it.

    Raises rather than returning a partial result: a half-finished run has no
    verdict, and inventing one would be worse than the error.

    **On `registry`, and why it is a parameter rather than a default.** This
    function is the thing a person actually touches: change a knob, run again,
    repeat until something says `SURVIVES`. That loop is a machine for
    family-wise error — at the measured per-run rate of roughly 1 in 100 with a
    4.7% upper bound, twenty independent attempts produce a false `SURVIVES`
    62% of the time — and until now nothing counted the attempts. `analytics/
    trials.py` was built for exactly this and was never connected to it.

    Pass a registry and the trial is recorded **before** the run, with its
    stated mechanism, and completed with its outcome afterwards. Recording
    afterwards would count only the runs somebody liked, which is the bias the
    count exists to undo.

    It is not defaulted on because a test or an example that silently appended
    to the project's own registry would corrupt the very number it protects.
    Instead, a run without one is *labelled*: `RunOutcome.multiplicity` is
    `None` and `verdict_report` says the run was not counted. An uncounted run
    is allowed; an uncounted run that looks counted is not.
    """
    instruments: dict[str, Instrument] = {SYMBOL: crypto_spot(SYMBOL)}
    bars = generate_bars(
        SYMBOL,
        datetime(2021, 1, 1, tzinfo=UTC),
        REGIME_MIXES[config.regime_mix][0],
        seed=config.seed,
        start_price=config.start_price,
    )

    trial = None
    if registry is not None:
        # Before the run, not after: the count that drives every multiplicity
        # correction is of attempts, not of results worth keeping.
        trial = registry.record(
            config.hypothesis or f"{config.regime_label} / {config.cost_label}",
            mechanism=config.mechanism,
            parameter_points=config.grid_cells,
            config=config.as_dict(),
        )

    verdict, report = judge(bars, config)

    if registry is not None and trial is not None:
        registry.complete(trial.trial_id, str(verdict.outcome))

    # One full-series run at the parameters the LAST fold chose, for the
    # charts. Its equity curve is in-sample over most of its length, which is
    # what the out-of-sample shading on the chart exists to say out loud — and
    # why its trades are NOT what the verdict is computed from.
    chosen = report.folds[-1].params
    result = Backtester(
        instruments=instruments,
        starting_cash=config.starting_cash,
        cost_model=config.cost_model,
        risk_manager=RiskManager(limits=RiskLimits(max_drawdown_pct=config.max_drawdown_pct)),
    ).run(SMACrossover(SYMBOL, **chosen), InMemoryBarFeed(bars))  # type: ignore[arg-type]
    summary = summarise(result.equity_curve, periods_per_year=PERIODS_PER_YEAR)
    trades = summarise_trades(result.fills, instruments)
    benchmark = buy_and_hold_curve(
        TestWindow(bars=tuple(bars)), SYMBOL, starting_cash=config.starting_cash
    )

    return RunOutcome(
        config=config,
        verdict=verdict,
        report=report,
        result=result,
        summary=summary,
        trades=trades,
        benchmark=benchmark,
        bars=bars,
        multiplicity=None if registry is None else registry.summary(),
        trial_id=None if trial is None else trial.trial_id,
    )
