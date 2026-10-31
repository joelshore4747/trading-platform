"""Live-shaped concerns: streaming, capture, and replay.

Everything here is about the gap between a backtest and a running system, and
none of it can place a real order — ``brokers/base.py`` still refuses that.

The organising rule is that **there is no live-only code path through a
strategy**. A `LiveFeed` satisfies the same `DataFeed` protocol a file does, so
the same `Strategy` runs in both. The moment live gets its own path, paper and
backtest stop being comparable and step 50's divergence tracking is measuring
two different programs rather than one program in two settings.
"""

from trading_platform.live.accounts import (
    AccountCycles,
    CycleError,
    CycleOutcome,
    CycleRecord,
    CycleSnapshot,
    cycles_path,
    read_cycles,
)
from trading_platform.live.alpaca import (
    AlpacaMarketData,
    AlpacaPoller,
    PollStats,
    TransportError,
    urllib_transport,
)
from trading_platform.live.journal import (
    AccountState,
    DaySummary,
    DecisionJournal,
    EventKind,
    JournalEvent,
    decisions_path,
    read_decisions,
    summarise_day,
)
from trading_platform.live.recorder import (
    CompactionResult,
    JournalError,
    JournalRecorder,
    compact,
    journal_path,
    read_journal,
)
from trading_platform.live.replay import (
    DivergenceReport,
    FillDifference,
    compare,
    replay_feed,
)
from trading_platform.live.session import (
    ALWAYS_OPEN,
    CalendarUnknown,
    TradingCalendar,
    us_equities,
    us_market_holidays,
)
from trading_platform.live.stream import BarSink, LiveFeed, StreamStats
from trading_platform.live.supervisor import (
    AccountLocked,
    CycleFailed,
    PaperSupervisor,
    ReconciliationError,
    ReconciliationReport,
    SessionResult,
    SessionStopped,
    SupervisorConfig,
)

__all__ = [
    "ALWAYS_OPEN",
    "AccountCycles",
    "AccountLocked",
    "AccountState",
    "AlpacaMarketData",
    "AlpacaPoller",
    "BarSink",
    "CalendarUnknown",
    "CompactionResult",
    "CycleError",
    "CycleFailed",
    "CycleOutcome",
    "CycleRecord",
    "CycleSnapshot",
    "DaySummary",
    "DecisionJournal",
    "DivergenceReport",
    "EventKind",
    "FillDifference",
    "JournalError",
    "JournalEvent",
    "JournalRecorder",
    "LiveFeed",
    "PaperSupervisor",
    "PollStats",
    "ReconciliationError",
    "ReconciliationReport",
    "SessionResult",
    "SessionStopped",
    "StreamStats",
    "SupervisorConfig",
    "TradingCalendar",
    "TransportError",
    "compact",
    "compare",
    "cycles_path",
    "decisions_path",
    "journal_path",
    "read_cycles",
    "read_decisions",
    "read_journal",
    "replay_feed",
    "summarise_day",
    "urllib_transport",
    "us_equities",
    "us_market_holidays",
]
