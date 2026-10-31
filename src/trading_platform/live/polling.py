"""The one rule every poller shares: do not publish a bar that has not closed.

A polling client asks a venue for "bars from X to now". Every vendor tested
here answers with the bar currently in progress included — Alpaca and Polygon
both do. Its ``close`` is not the close; it is the last trade so far. Publishing
it hands a strategy a price that will still change, which is lookahead of the
purest kind, and it also writes a bar to the archive that the next poll will
contradict.

So the decision of what a poll may publish is factored out here rather than
written once per vendor, because it is the same decision and getting it wrong
in one place would be invisible next to a correct one elsewhere.

Two callers: ``live/alpaca.py``'s poller, which publishes into a ``LiveFeed``
for a strategy, and ``collect/capture.py``, which records into sinks for the
archive. They differ in destination and in nothing else that matters here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from trading_platform.core.events import Bar

__all__ = ["PollDecision", "due_bars"]


@dataclass(slots=True)
class PollDecision:
    """What a poll's response may be published, and what was held back and why."""

    due: list[Bar] = field(default_factory=list)
    #: Held because the bar's close time has not passed. Not lost: the next
    #: poll after its close will carry it again.
    withheld_open: int = 0
    #: Held because this symbol has already been published at or past that
    #: timestamp, with the SAME values. Poll windows overlap on purpose so a
    #: restart loses nothing; the overlap re-delivering bars is normal.
    already_seen: int = 0
    #: Re-delivered with DIFFERENT values: the vendor revised a bar it had
    #: already published. Not held — a revision is a fact about the feed and
    #: goes through, where the journal appends it and the store refuses to
    #: merge it, recording a conflict. Counted separately so an operator sees
    #: "revised 37" rather than inferring it from a conflict count. Before this
    #: existed a revision was indistinguishable from an ordinary repeat and was
    #: silently discarded (adversarial review, 2026-09-04).
    revised: int = 0


def due_bars(
    bars: Sequence[Bar],
    now: datetime,
    last_seen: Mapping[str, Bar],
) -> PollDecision:
    """Split a poll's bars into publishable, withheld, and revised.

    A bar is due when its close time is at or before ``now`` and it is newer
    than the last bar already published for its symbol. ``now`` is passed in
    rather than read, so the boundary is testable: a bar stamped exactly ``now``
    has closed and is due, one stamped a millisecond later has not.

    ``last_seen`` holds the whole bar, not just its timestamp, so that a vendor
    re-sending an instant with DIFFERENT prices is told apart from one
    re-sending the same instant unchanged. The first is a revision and goes
    through; the second is the overlap working as intended and is dropped.

    Output is sorted by (timestamp, symbol). A recorder that receives a poll's
    bars out of order writes them out of order, and the reorder buffer that
    would fix that is on the strategy path, not this one.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    decision = PollDecision()
    for bar in bars:
        if bar.timestamp.tzinfo is None:
            raise ValueError(f"bar timestamps must be timezone-aware, got {bar.timestamp!r}")
        if bar.timestamp > now:
            decision.withheld_open += 1
            continue
        seen = last_seen.get(bar.symbol)
        if seen is not None and bar.timestamp <= seen.timestamp:
            if bar.timestamp == seen.timestamp and bar != seen:
                decision.revised += 1
                decision.due.append(bar)
            else:
                decision.already_seen += 1
            continue
        decision.due.append(bar)
    decision.due.sort(key=lambda b: (b.timestamp, b.symbol))
    return decision
