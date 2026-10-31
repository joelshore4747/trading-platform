"""The verdict: what this platform is actually for.

Every quant tool is a dashboard with forty knobs and a beautiful equity curve.
This one is built against that. Its product — the thing nothing else ships — is
**the word "no", delivered fast, with evidence.**

So the domain object is not a "result". It is a judgement, with a defensible
outcome and the reasons underneath it. And the strongest outcome available is
deliberately not "this works": it is ``SURVIVES``, which means only that this
run failed to falsify an edge, on this data, at this cost model.

Nothing here computes new statistics. It classifies what
``WalkForwardReport.verdicts`` and ``TradeStats.concentration_verdict`` already
found, so there is exactly one place where "is this any good" is decided.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading_platform.analytics.trades import TradeStats
from trading_platform.analytics.walkforward import WalkForwardReport


class Outcome(StrEnum):
    """Ordered worst to best. There is no outcome meaning "this works"."""

    NO_EDGE = "NO EDGE"
    """Failed on the data it was fitted on. Nothing further to discuss."""

    FITTED = "FITTED"
    """Worked in-sample and did not survive unseen data."""

    UNDERPOWERED = "UNDERPOWERED"
    """Too few trades for any of the statistics to mean anything."""

    LOSES_TO_HOLDING = "LOSES TO HOLDING"
    """Made money, but less than doing nothing would have."""

    SURVIVES = "SURVIVES"
    """Failed to falsify an edge — on this data, at this cost model. That is
    the strongest thing this platform will say, and it is not a green light."""

    @property
    def is_pass(self) -> bool:
        return self is Outcome.SURVIVES

    @property
    def tone(self) -> str:
        """Status token for the UI. Never colour alone — always with the word."""
        return {
            Outcome.NO_EDGE: "critical",
            Outcome.FITTED: "critical",
            Outcome.UNDERPOWERED: "serious",
            Outcome.LOSES_TO_HOLDING: "serious",
            Outcome.SURVIVES: "good",
        }[self]


#: What each outcome means, in the words a person should read.
HEADLINES: dict[Outcome, str] = {
    Outcome.NO_EDGE: (
        "This strategy failed on the very data its parameters were chosen from. "
        "There is no in-sample result to degrade, so there is nothing to test "
        "out of sample."
    ),
    Outcome.FITTED: (
        "The in-sample result did not survive data the parameters had not seen. "
        "What the sweep found was a coincidence in this sample, not an effect in "
        "the market."
    ),
    Outcome.UNDERPOWERED: (
        "There were not enough trades for any of these numbers to be a "
        "measurement. A Sharpe ratio computed over a handful of fills is "
        "arithmetic, not evidence."
    ),
    Outcome.LOSES_TO_HOLDING: (
        "It made money and still lost to doing nothing. Commission was paid and "
        "risk was taken to underperform buying and holding the instrument."
    ),
    Outcome.SURVIVES: (
        "This run failed to falsify an edge, on this data, at this cost model. "
        "That is not the same as finding one — it means the cheapest tests did "
        "not kill it. Run it again on a different seed before believing anything."
    ),
}


@dataclass(frozen=True, slots=True)
class Verdict:
    """A judgement about one run, with its reasons."""

    outcome: Outcome
    objections: tuple[str, ...] = ()
    #: Populated only when a walk-forward was run at all.
    efficiency: float | None = None
    out_of_sample_score: float | None = None
    benchmark_beaten: str | None = None
    trades: int = 0
    net_pnl: Decimal = Decimal("0")
    decided_at: datetime | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def headline(self) -> str:
        return HEADLINES[self.outcome]

    @property
    def objection_count(self) -> int:
        return len(self.objections)


def decide(
    report: WalkForwardReport,
    trades: TradeStats,
    *,
    decided_at: datetime | None = None,
) -> Verdict:
    """Classify a completed walk-forward run.

    The order of the checks is the order of severity, and the first one that
    fires wins — a strategy with no in-sample edge is not additionally
    "underpowered", it is simply finished.
    """
    objections = list(report.verdicts)
    concentration = trades.concentration_verdict
    if concentration is not None:
        objections.append(concentration)

    efficiency = report.efficiency
    beaten = report.folds_beating_benchmark

    if efficiency is None:
        outcome = Outcome.NO_EDGE
    elif report.median_fills_per_fold < 10:
        # Checked before efficiency: a ratio built on four trades per fold is
        # not a small edge, it is not a measurement.
        outcome = Outcome.UNDERPOWERED
    elif efficiency < 0.5:
        outcome = Outcome.FITTED
    elif beaten is not None and beaten * 2 <= len(report.folds):
        outcome = Outcome.LOSES_TO_HOLDING
    else:
        outcome = Outcome.SURVIVES

    return Verdict(
        outcome=outcome,
        objections=tuple(objections),
        efficiency=efficiency,
        out_of_sample_score=report.mean_out_of_sample,
        benchmark_beaten=(None if beaten is None else f"{beaten}/{len(report.folds)} folds"),
        trades=trades.count,
        net_pnl=trades.total_net_pnl,
        decided_at=decided_at,
    )
