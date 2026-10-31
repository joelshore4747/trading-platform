"""One page: is this thing running, and what has it noticed.

**Not a results page.** `harness/ROADMAP.md` defers the frontend until a strategy
survives out of sample and thirty days of paper divergence, and the risk that
deferral guards is real: a dashboard makes an unvalidated strategy look
authoritative. That deferral stands. This page shows no strategy performance,
no equity curve and no Sharpe. It answers one question — *is the machinery
alive* — and it can no more overstate a result than a `df -h` can.

## Why no framework

FastAPI was the plan. One read-only page over five queries does not need a
dependency, a build step or an ASGI server, and `harness/TECHNOLOGY_REVIEW.md`
asks every dependency to earn its place. `http.server` from the standard library
serves this. There is no JavaScript at all: the page refreshes itself with a
`<meta refresh>`, which cannot break, cannot be blocked, and works in a terminal
browser at 3am over a phone tether. When a page needs to accept input — a
leverage acknowledgement, say — that is the moment to reconsider, not before.

## What "simple" means here

One column. No tabs, no modals, no cards for their own sake, nothing that is not
an answer to "is this working". The most important number is the largest thing on
the page and it is at the top: how long the quietest feed has been silent. Every
other section is a plain table, ordered worst-first, and a section with nothing
in it says so in one line rather than rendering an empty frame.

Colour carries no information that the text does not also carry, so this is
readable in monochrome and by anyone who cannot distinguish red from green.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

__all__ = ["OpsSnapshot", "read_snapshot", "render_page"]

#: A feed quiet for longer than this is called out. Deliberately generous: a
#: 1-minute feed is silent for a minute by definition, and an alarm that cries
#: wolf is an alarm nobody reads.
SILENT_AFTER = timedelta(minutes=10)

#: A running capture that has not reported for this long is a dead process. It
#: is generous on purpose: the collector heartbeats far more often, and an alarm
#: that fires on a slow poll is an alarm somebody switches off.
HEARTBEAT_STALE_AFTER = timedelta(minutes=15)


class Level(StrEnum):
    """How loud. There are two, deliberately.

    A third level is a level nobody agrees on, and the useful question is only
    ever "does somebody have to do something now".
    """

    #: Something is broken now. This is what makes `check_health` exit non-zero.
    ALARM = "ALARM"
    #: Worth reading; not worth waking anybody. Never affects the exit code.
    WARN = "WARN"


@dataclass(frozen=True, slots=True)
class Alert:
    """One thing wrong, named so that reading it tells you what to do."""

    level: Level
    subject: str
    detail: str
    #: How this reads as the page's `<h1>` when it is the worst alert. Defaults
    #: to the subject shouted, which is right for a phrase and wrong for one
    #: carrying a formatted age — "2H SILENT" is not what `_fmt_age` produced.
    headline: str = ""

    def as_headline(self) -> str:
        return self.headline or self.subject.upper()

    def one_line(self) -> str:
        return f"{self.level}  {self.subject} — {self.detail}"


@dataclass(frozen=True, slots=True)
class OpsSnapshot:
    """Everything the page shows, read in one pass so the sections agree."""

    at: datetime
    feeds: list[dict[str, Any]]
    disagreements: list[dict[str, Any]]
    same_venue_errors: int
    cross_venue_gaps: int
    accounts: list[dict[str, Any]]
    tickets: list[dict[str, Any]]
    ticket_counts: dict[str, int]
    #: Every capture or fetch still claiming to be running, with the counters it
    #: last reported. `feed_health` says whether anything has ARRIVED; a wedged
    #: process keeps satisfying that from the bars it wrote yesterday.
    runs: list[dict[str, Any]] = field(default_factory=list)
    #: Ingest runs that ended `failed` in the last day. C01 shipped a collector
    #: that wrote zero rows while `--check` passed, so a failed run is loud here.
    failed_runs: list[dict[str, Any]] = field(default_factory=list)
    #: Set when a query failed. The page says so rather than showing zeros,
    #: because a broken page that looks healthy is worse than one that is down.
    error: str = ""

    @property
    def silent(self) -> list[dict[str, Any]]:
        return [f for f in self.feeds if _silent_for(f) > SILENT_AFTER]

    @property
    def worst_silence(self) -> timedelta | None:
        if not self.feeds:
            return None
        return max(_silent_for(f) for f in self.feeds)

    @property
    def alerts(self) -> tuple[Alert, ...]:
        return assess(self)

    @property
    def healthy(self) -> bool:
        """No ALARM. One definition, used by the page and by the exit code.

        A second definition of "healthy" living in the alerting script would
        drift from the page's within a fortnight and the two would disagree in
        front of somebody at 3am, so there is only this one.
        """
        return not any(a.level is Level.ALARM for a in self.alerts)


def assess(snap: OpsSnapshot, *, now: datetime | None = None) -> tuple[Alert, ...]:
    """What is wrong, worst first. Pure: no clock of its own unless asked.

    Each rule below exists because the thing it catches has already happened
    here, or because the store cannot distinguish it from working:

    * **the store is unreachable** — the page would otherwise render zeros, and
      zeros look calm;
    * **nothing has captured anything** — an empty database is not health;
    * **a feed has gone silent** — the question the page leads with;
    * **a run heartbeat has gone stale** — a `running` row is indistinguishable
      from a working one without it, which is T-f280e8f0 itself;
    * **a running capture is erroring or losing bars to the store** — the
      counters existed and nothing read them until they were printed at an exit
      that a month-long collector never reaches;
    * **an ingest run ended failed** — C01 shipped a collector that wrote zero
      rows for every poll while both runs closed and `--check` passed;
    * **two sources contradict each other on one venue** — a bad print in one of
      them. WARN, not ALARM: nothing is down, but somebody should look.
    """
    at = now or snap.at
    alerts: list[Alert] = []

    if snap.error:
        alerts.append(
            Alert(
                Level.ALARM,
                "store unreachable",
                f"{snap.error}. Nothing else here is current; the collector may still be "
                "capturing, because the journal needs no database.",
            )
        )
        return tuple(alerts)

    if not snap.feeds:
        alerts.append(
            Alert(
                Level.ALARM,
                "no feeds",
                "no source has written a bar. An empty database is not health.",
            )
        )

    for feed in sorted(snap.feeds, key=_silent_for, reverse=True):
        silence = _silent_for(feed)
        if silence > SILENT_AFTER:
            # The AGE is the subject, because the headline is built from the
            # first alert's subject and the age is the number somebody reads
            # from the doorway. The source is named in the detail, which the
            # page renders directly underneath it.
            alerts.append(
                Alert(
                    Level.ALARM,
                    f"{feed['source']} silent",
                    f"{feed['source']} has sent no {feed.get('interval', '?')} bar on "
                    f"{feed.get('market', '?')} for {_fmt_age(silence)}.",
                    headline=f"{_fmt_age(silence)} SILENT",
                )
            )

    for run in snap.runs:
        source = run.get("source", "?")
        beat = run.get("heartbeat_at")
        if beat is None:
            age = at - run["started_at"] if run.get("started_at") else None
            if age is not None and age > HEARTBEAT_STALE_AFTER:
                alerts.append(
                    Alert(
                        Level.ALARM,
                        f"{source} never reported",
                        f"run {run.get('run_id')} opened {_fmt_age(age)} ago and has never "
                        "sent a heartbeat. It died before its first one, or it predates "
                        "the heartbeat and should be closed by hand.",
                    )
                )
            continue
        age = at - beat
        if age > HEARTBEAT_STALE_AFTER:
            alerts.append(
                Alert(
                    Level.ALARM,
                    f"{source} heartbeat stale",
                    f"last reported {_fmt_age(age)} ago and the run is still open. The "
                    "process died without closing it.",
                )
            )
        errors = int(run.get("consecutive_errors") or 0)
        if errors:
            alerts.append(
                Alert(
                    Level.ALARM,
                    f"{source} failing",
                    f"{errors} poll(s) in a row have raised. It is running and fetching nothing.",
                )
            )
        lost = int(run.get("sink_failures") or 0)
        if lost:
            alerts.append(
                Alert(
                    Level.WARN,
                    f"{source} store failures",
                    f"{lost} batch(es) did not reach the store. They are in the journal — "
                    "scripts/journal_to_store.py --source <name> puts them back.",
                )
            )

    for run in snap.failed_runs:
        alerts.append(
            Alert(
                Level.ALARM,
                f"{run.get('source', '?')} run failed",
                f"{run.get('kind', '?')} run {run.get('run_id')} ended failed after "
                f"{int(run.get('rows_written') or 0):,} row(s): {run.get('error', '')}"[:400],
            )
        )

    if snap.same_venue_errors:
        alerts.append(
            Alert(
                Level.WARN,
                "sources contradict each other",
                f"{snap.same_venue_errors:,} same-venue disagreement(s) — one of the two "
                "sources is wrong each time. The cross-venue gaps are basis, not errors.",
            )
        )

    return tuple(sorted(alerts, key=lambda a: (a.level is not Level.ALARM,)))


def _silent_for(feed: dict[str, Any]) -> timedelta:
    value = feed.get("silent_for")
    return value if isinstance(value, timedelta) else timedelta(0)


def _fmt_age(delta: timedelta | None) -> str:
    if delta is None:
        return "—"
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "0s"
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def read_snapshot(connection: Any, *, now: datetime | None = None) -> OpsSnapshot:
    """One read of everything. A failed query is reported, never rendered as zero."""
    at = now or datetime.now(UTC)
    empty = OpsSnapshot(
        at=at,
        feeds=[],
        disagreements=[],
        same_venue_errors=0,
        cross_venue_gaps=0,
        accounts=[],
        tickets=[],
        ticket_counts={},
    )
    try:
        with connection.cursor() as cursor:
            feeds = _rows(
                cursor,
                "SELECT source, market, interval, symbols, bars, last_bar, silent_for, "
                "bars_last_day, median_lag FROM feed_health ORDER BY silent_for DESC NULLS FIRST",
            )
            disagreements = _rows(
                cursor,
                "SELECT symbol, ts, source_a_id, source_b_id, same_venue, worst_field, "
                "worst_gap, close_a, close_b FROM source_disagreements "
                "WHERE same_venue ORDER BY worst_gap DESC LIMIT 10",
            )
            cursor.execute(
                "SELECT same_venue, count(*) FROM source_disagreements GROUP BY same_venue"
            )
            counts = dict(cursor.fetchall())
            accounts = _rows(
                cursor,
                "SELECT account, cycle, stage, at, equity, cash, exposure, margin_used, "
                "free_cash, gross_leverage, drawdown_pct FROM account_latest ORDER BY account",
            )
            tickets = _rows(
                cursor,
                "SELECT id, severity, kind, title, path, opened_at, sources, reopens "
                "FROM tickets WHERE status = 'open' ORDER BY severity, opened_at LIMIT 20",
            )
            cursor.execute(
                "SELECT severity, count(*) FROM tickets WHERE status = 'open' GROUP BY severity"
            )
            ticket_counts = {str(s): int(n) for s, n in cursor.fetchall()}
            runs = _rows(
                cursor,
                "SELECT run_id, source, kind, started_at, heartbeat_at, rows_written, "
                "consecutive_errors, sink_failures, dropped FROM collector_health",
            )
            failed_runs = _rows(
                cursor,
                "SELECT r.id AS run_id, s.name AS source, r.kind, r.finished_at, "
                "r.rows_written, r.error FROM ingest_runs r JOIN sources s ON s.id = r.source_id "
                "WHERE r.status = 'failed' AND r.finished_at > now() - interval '1 day' "
                "ORDER BY r.finished_at DESC LIMIT 10",
            )
    except Exception as exc:
        # `replace`, not `**empty.__dict__`: this dataclass is `slots=True` and
        # has no `__dict__`, so the branch whose whole job is "say the database
        # is down rather than rendering zeros" raised an AttributeError of its
        # own instead. Every test of it built `OpsSnapshot(error=...)` by hand,
        # so the branch had never once run (T-0e3f8555).
        return replace(empty, error=f"{type(exc).__name__}: {exc}")

    return OpsSnapshot(
        at=at,
        feeds=feeds,
        disagreements=disagreements,
        same_venue_errors=int(counts.get(True, 0)),
        cross_venue_gaps=int(counts.get(False, 0)),
        accounts=accounts,
        tickets=tickets,
        ticket_counts=ticket_counts,
        runs=runs,
        failed_runs=failed_runs,
    )


def _rows(cursor: Any, sql: str) -> list[dict[str, Any]]:
    cursor.execute(sql)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


# -- rendering ------------------------------------------------------------------------------

_STYLE = """
:root { color-scheme: light dark; --fg:#111; --dim:#666; --bg:#fbfbfa; --line:#ddd;
        --bad:#8a1c1c; --ok:#1c5c2e; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8e8e6; --dim:#9a9a94; --bg:#141413; --line:#333; --bad:#e88; --ok:#8d8; }
}
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1rem 5rem; background:var(--bg); color:var(--fg);
       font:15px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }
main { max-width: 62rem; margin: 0 auto; }
h1 { font-size:2.6rem; line-height:1.1; margin:0 0 .2rem; font-weight:600; letter-spacing:-.02em; }
h2 { font-size:.82rem; text-transform:uppercase; letter-spacing:.09em; color:var(--dim);
     margin:2.6rem 0 .5rem; font-weight:600; }
p.sub { color:var(--dim); margin:0 0 .2rem; }
table { width:100%; border-collapse:collapse; margin:0; }
th { text-align:left; font-weight:400; color:var(--dim); font-size:.8rem;
     padding:.2rem .6rem .2rem 0; border-bottom:1px solid var(--line); }
td { padding:.3rem .6rem .3rem 0; border-bottom:1px solid var(--line); vertical-align:top; }
td.n, th.n { text-align:right; padding-right:1.2rem; }
.bad { color:var(--bad); }
.ok { color:var(--ok); }
.none { color:var(--dim); padding:.3rem 0; }
footer { color:var(--dim); margin-top:3.5rem; font-size:.82rem; }
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _table(headers: list[str], rows: list[list[str]], numeric: set[int] | None = None) -> str:
    numeric = numeric or set()
    head = "".join(
        f'<th class="n">{_e(h)}</th>' if i in numeric else f"<th>{_e(h)}</th>"
        for i, h in enumerate(headers)
    )
    body = []
    for row in rows:
        cells = "".join(
            f'<td class="n">{c}</td>' if i in numeric else f"<td>{c}</td>"
            for i, c in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _headline(snap: OpsSnapshot) -> str:
    """The one line somebody reads from the doorway.

    It is derived from `assess`, not from a second opinion about silence: the
    page saying BEHIND while `check_health` exits non-zero is exactly the kind
    of disagreement that gets an alarm switched off.
    """
    alarms = [a for a in snap.alerts if a.level is Level.ALARM]
    if alarms:
        first = alarms[0]
        rest = f" (+{len(alarms) - 1} more)" if len(alarms) > 1 else ""
        return (
            f'<h1 class="bad">{_e(first.as_headline())}{rest}</h1>'
            f'<p class="sub">{_e(first.detail)}</p>'
        )
    worst = _fmt_age(snap.worst_silence)
    return (
        f'<h1 class="ok">{worst} BEHIND</h1>'
        f'<p class="sub">{len(snap.feeds)} feeds live. That is the oldest bar any of them '
        "has; a 1m feed is a minute behind by definition.</p>"
    )


def _alerts_section(snap: OpsSnapshot) -> str:
    if not snap.alerts:
        return ""
    rows = "".join(
        f'<li class="{"bad" if a.level is Level.ALARM else "sub"}">'
        f"<b>{_e(a.level)}</b> {_e(a.subject)} — {_e(a.detail)}</li>"
        for a in snap.alerts
    )
    return f"<h2>Alerts</h2><ul>{rows}</ul>"


def render_page(snap: OpsSnapshot, *, refresh_s: int = 30) -> str:
    """The whole page. Pure: takes a snapshot, returns HTML, touches nothing."""
    parts = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width,initial-scale=1">',
        f'<meta http-equiv=refresh content="{int(refresh_s)}">',
        "<title>operations</title>",
        f"<style>{_STYLE}</style></head><body><main>",
        _headline(snap),
        _alerts_section(snap),
    ]

    parts.append("<h2>Feeds</h2>")
    if not snap.feeds:
        parts.append('<p class="none">Nothing has captured anything.</p>')
    else:
        rows = []
        for f in snap.feeds:
            silence = _silent_for(f)
            cls = "bad" if silence > SILENT_AFTER else ""
            rows.append(
                [
                    f'<span class="{cls}">{_e(f["source"])}</span>',
                    f"{_e(f['market'])} {_e(f['interval'])}",
                    f'<span class="{cls}">{_fmt_age(silence)}</span>',
                    _e(f"{f['symbols']:,}"),
                    _e(f"{f['bars']:,}"),
                    _e(f"{f.get('bars_last_day') or 0:,}"),
                    _fmt_age(f.get("median_lag")),
                ]
            )
        parts.append(
            _table(
                ["source", "market", "silent", "symbols", "bars", "24h", "lag"],
                rows,
                numeric={2, 3, 4, 5, 6},
            )
        )

    parts.append("<h2>Disagreements</h2>")
    if snap.same_venue_errors == 0:
        parts.append(
            f'<p class="none">No source has contradicted another on the same venue. '
            f"{snap.cross_venue_gaps:,} cross-venue gaps recorded, which are basis and "
            "are not errors.</p>"
        )
    else:
        parts.append(
            f'<p class="sub bad">{snap.same_venue_errors:,} same-venue disagreements — '
            "one of the two sources is wrong each time. "
            f"({snap.cross_venue_gaps:,} cross-venue gaps are basis, not errors.)</p>"
        )
        parts.append(
            _table(
                ["symbol", "when", "field", "gap", "a", "b", "sources"],
                [
                    [
                        _e(d["symbol"]),
                        _e(d["ts"].strftime("%m-%d %H:%M")),
                        _e(d["worst_field"]),
                        f"{float(d['worst_gap']) * 10000:.1f}bp",
                        _e(d["close_a"]),
                        _e(d["close_b"]),
                        f"{_e(d['source_a_id'])}/{_e(d['source_b_id'])}",
                    ]
                    for d in snap.disagreements
                ],
                numeric={3, 4, 5},
            )
        )

    parts.append("<h2>Accounts</h2>")
    if not snap.accounts:
        parts.append('<p class="none">No account has marked.</p>')
    else:
        rows = []
        for a in snap.accounts:
            free = a["free_cash"]
            free_cell = f'<span class="bad">{_e(free)}</span>' if free < 0 else _e(free)
            rows.append(
                [
                    _e(a["account"]),
                    f"{_e(a['stage'])} c{_e(a['cycle'])}",
                    _fmt_age(snap.at - a["at"]),
                    _e(a["equity"]),
                    _e(a["exposure"]),
                    free_cell,
                    f"{float(a['gross_leverage']):.2f}x",
                    f"{float(a['drawdown_pct']) * 100:.2f}%",
                ]
            )
        parts.append(
            _table(
                ["account", "stage", "marked", "equity", "exposure", "free", "lev", "dd"],
                rows,
                numeric={2, 3, 4, 5, 6, 7},
            )
        )

    parts.append("<h2>Open tickets</h2>")
    if not snap.tickets:
        parts.append(
            '<p class="none">Nothing open. That is a statement about what has been '
            "<em>found</em>, not about what is there.</p>"
        )
    else:
        summary = ", ".join(f"{k} {v}" for k, v in sorted(snap.ticket_counts.items()))
        urgent = snap.ticket_counts.get("P0", 0) + snap.ticket_counts.get("P1", 0)
        cls = "bad" if urgent else "sub"
        parts.append(f'<p class="{cls}">{_e(summary)}</p>')
        parts.append(
            _table(
                ["id", "sev", "kind", "item", "where", "age"],
                [
                    [
                        _e(t["id"]),
                        f'<span class="{"bad" if t["severity"] in ("P0", "P1") else ""}">'
                        f"{_e(t['severity'])}</span>",
                        _e(t["kind"]),
                        _e(t["title"]),
                        _e(t["path"]),
                        _fmt_age(snap.at - t["opened_at"]),
                    ]
                    for t in snap.tickets
                ],
                numeric={5},
            )
        )

    parts.append(
        "<footer>"
        f"{_e(snap.at.strftime('%Y-%m-%d %H:%M:%S'))} UTC · refreshes every "
        f"{int(refresh_s)}s · operations only — this page shows no strategy result, "
        "by design</footer></main></body></html>"
    )
    return "".join(parts)
