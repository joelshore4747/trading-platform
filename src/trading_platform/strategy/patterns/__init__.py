"""ICT primitives as pure, testable functions.

Pure so each can be tested in isolation and reused by any strategy, and because
the interesting failure — a function that quietly reads a bar it should not have
— is far easier to see in a function that takes a list of bars and returns a
list of findings than in a strategy that does five things at once.

The vocabulary, and what each thing claims:

| primitive | claim | knowable at |
|---|---|---|
| `swing_points` | here is a pivot | `strength` bars later |
| `market_structure` | trend continued (BOS) or turned (CHoCH) | the closing bar |
| `fair_value_gaps` | this span was skipped and price returns to it | its third bar |
| `order_blocks` | the move started here and price returns to it | the gap's bar |
| `liquidity_sweeps` | stops beyond this level were taken and the move failed | the closing bar |
| `in_killzone` | this hour matters and the rest do not | immediately |

Two of the six carry a confirmation lag, and both say so in a `confirmed_at`
field. Reading `index` where `confirmed_at` belongs is the lookahead this whole
package is shaped to make visible.
"""

from trading_platform.strategy.patterns.imbalance import (
    FairValueGap,
    OrderBlock,
    fair_value_gaps,
    order_blocks,
    unfilled,
)
from trading_platform.strategy.patterns.liquidity import LiquiditySweep, liquidity_sweeps
from trading_platform.strategy.patterns.sessions import (
    KILLZONES,
    Killzone,
    active_killzone,
    in_killzone,
)
from trading_platform.strategy.patterns.structure import (
    Break,
    BreakKind,
    Direction,
    Swing,
    confirmed_by,
    market_structure,
    swing_points,
)

__all__ = [
    "KILLZONES",
    "Break",
    "BreakKind",
    "Direction",
    "FairValueGap",
    "Killzone",
    "LiquiditySweep",
    "OrderBlock",
    "Swing",
    "active_killzone",
    "confirmed_by",
    "fair_value_gaps",
    "in_killzone",
    "liquidity_sweeps",
    "market_structure",
    "order_blocks",
    "swing_points",
    "unfilled",
]
