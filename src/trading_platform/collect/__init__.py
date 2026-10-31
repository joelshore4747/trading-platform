"""Many independent connections to the same markets, and what they disagree about.

`capture` runs N sources concurrently and writes every bar tagged with the
source that produced it. `reconcile` compares them afterwards. Nothing here
merges two sources into one series: a merge is a decision about who to believe,
and that decision does not get made silently inside a collector.
"""

from trading_platform.collect.capture import (
    BarFetcher,
    CaptureStats,
    Collector,
    SourceCapture,
)
from trading_platform.collect.reconcile import (
    Disagreement,
    ReconcileResult,
    compare_bars,
    reconcile,
)

__all__ = [
    "BarFetcher",
    "CaptureStats",
    "Collector",
    "Disagreement",
    "ReconcileResult",
    "SourceCapture",
    "compare_bars",
    "reconcile",
]
