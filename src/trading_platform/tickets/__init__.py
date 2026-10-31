"""Findings that survive the session.

An auditor that reports into a transcript has found nothing, because the
transcript ends. `model.py` defines what a finding is; `store.py` decides where
it lives (a committed JSONL log, with Postgres as a rebuildable projection);
`scripts/ticket.py` is what an agent actually runs.
"""

from trading_platform.tickets.model import (
    SEVERITIES,
    Severity,
    Ticket,
    TicketError,
    TicketEvent,
    TicketKind,
    TicketStatus,
    by_severity,
    fold,
    open_tickets,
    ticket_id,
)
from trading_platform.tickets.store import (
    DEFAULT_LOG,
    append,
    file_ticket,
    load,
    read_events,
    sync_to_store,
)

__all__ = [
    "DEFAULT_LOG",
    "SEVERITIES",
    "Severity",
    "Ticket",
    "TicketError",
    "TicketEvent",
    "TicketKind",
    "TicketStatus",
    "append",
    "by_severity",
    "file_ticket",
    "fold",
    "load",
    "open_tickets",
    "read_events",
    "sync_to_store",
    "ticket_id",
]
