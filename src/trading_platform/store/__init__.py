"""The queryable store: Postgres, beside the Parquet archive rather than instead of it.

Two stores, two jobs, and the split is deliberate.

**Parquet stays the archive.** It is what the backtester reads, it is immutable
once written, and it needs no server to be running. A backtest three years from
now must not depend on a database being up.

**Postgres is the live and queryable layer.** It exists because the workload
changed: many feeds writing at once, government series to join against prices,
and a page that has to answer "is this working" in under a second. Those are the
three conditions `harness/DATA_MAP.md` set for revisiting the no-database rule,
and all three are now met (ADR 0010).

The shape follows from one idea. A fact in this database is always attributable:
which source said it, which run fetched it, when it landed. Bars from two feeds
are two rows, never one, because the disagreement between them is the reason for
running two feeds. Nothing here merges sources, and nothing here repairs data.

Credentials come from the environment only, through the same `Credential` type
the venue adapters use, so a connection string cannot be committed by accident
and never prints itself.
"""

from trading_platform.store.bars import (
    BarConflict,
    Source,
    ensure_source,
    heartbeat,
    ingest_run,
    read_bars,
    source_coverage,
    write_bars,
)
from trading_platform.store.connection import (
    DSN_VAR,
    StoreError,
    connect,
    dsn,
    is_configured,
)
from trading_platform.store.migrate import (
    MigrationError,
    applied_migrations,
    migrate,
    pending_migrations,
)

__all__ = [
    "DSN_VAR",
    "BarConflict",
    "MigrationError",
    "Source",
    "StoreError",
    "applied_migrations",
    "connect",
    "dsn",
    "ensure_source",
    "heartbeat",
    "ingest_run",
    "is_configured",
    "migrate",
    "pending_migrations",
    "read_bars",
    "source_coverage",
    "write_bars",
]
