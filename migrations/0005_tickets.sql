-- 0005_tickets — what the harness found, queryable.
--
-- **The log is the record; this is a projection.** `harness/tickets/events.jsonl`
-- is append-only, committed to git, and is what an agent writes to — it needs a
-- filesystem and nothing else, so an auditor with no database can still record a
-- finding. `tickets.sync_to_store` TRUNCATEs both tables here and rebuilds them
-- from that log. If the two ever disagree, the log is right.
--
-- Which means: **do not write to these tables.** A row inserted here is lost at
-- the next sync, silently, which is exactly the disappearing finding this
-- workstream exists to stop. File through `scripts/ticket.py`.
--
-- Two tables because a ticket's history is the interesting part. "Filed by the
-- lookahead-auditor on the 3rd, closed on the 4th citing commit abc123, reopened
-- on the 7th because the fix was wrong" is three facts, and a table of current
-- state keeps one of them.

CREATE TABLE tickets (
    id            TEXT        PRIMARY KEY,
    kind          TEXT        NOT NULL,
    severity      TEXT        NOT NULL,
    title         TEXT        NOT NULL,
    path          TEXT        NOT NULL,
    line          INTEGER,
    status        TEXT        NOT NULL,
    opened_at     TIMESTAMPTZ NOT NULL,
    closed_at     TIMESTAMPTZ,
    reproduction  TEXT        NOT NULL DEFAULT '',
    detail        TEXT        NOT NULL DEFAULT '',
    resolution    TEXT        NOT NULL DEFAULT '',
    commit        TEXT        NOT NULL DEFAULT '',
    duplicate_of  TEXT        NOT NULL DEFAULT '',
    -- Everyone who filed it. Two auditors finding the same thing independently
    -- is corroboration, and collapsing them to one would throw that away.
    sources       TEXT[]      NOT NULL DEFAULT '{}',
    reopens       INTEGER     NOT NULL DEFAULT 0,

    CONSTRAINT tickets_kind_known
        CHECK (kind IN ('defect', 'feature', 'question', 'debt')),
    CONSTRAINT tickets_severity_known
        CHECK (severity IN ('P0', 'P1', 'P2', 'P3')),
    CONSTRAINT tickets_status_known
        CHECK (status IN ('open', 'closed', 'wontfix', 'duplicate')),
    -- The one quality rule, enforced here as well as in Python: a defect with no
    -- reproduction is an opinion.
    CONSTRAINT tickets_defects_reproduce
        CHECK (kind <> 'defect' OR btrim(reproduction) <> ''),
    -- Closing without saying why reads as somebody tidying up.
    CONSTRAINT tickets_closures_explain
        CHECK (status NOT IN ('closed', 'wontfix') OR btrim(resolution) <> ''),
    CONSTRAINT tickets_have_a_source CHECK (cardinality(sources) > 0)
);

CREATE INDEX tickets_open_by_severity ON tickets (severity, opened_at) WHERE status = 'open';
CREATE INDEX tickets_by_path ON tickets (path);

COMMENT ON TABLE  tickets IS
    'Current state of every finding, folded from ticket_events. A PROJECTION: rebuilt by tickets.sync_to_store from harness/tickets/events.jsonl, which is the record. Do not INSERT here — the next sync discards it.';
COMMENT ON COLUMN tickets.id IS
    'Derived from kind, title and path (never the line, which drifts). Two agents finding the same thing therefore file one ticket with two sources, rather than two tickets.';
COMMENT ON COLUMN tickets.sources IS
    'Every agent, run or person that filed this. More than one is independent corroboration.';
COMMENT ON COLUMN tickets.reopens IS
    'How many times this closed and came back. Above zero means a fix did not work, which is worth seeing in a list.';
COMMENT ON COLUMN tickets.severity IS
    'P0 stop and fix, P3 worth knowing (charter §119). When two sources disagree the WORSE severity stands: a disagreement about how bad something is must not resolve downwards.';

CREATE TABLE ticket_events (
    id           BIGSERIAL   PRIMARY KEY,
    ticket       TEXT        NOT NULL,
    action       TEXT        NOT NULL,
    at           TIMESTAMPTZ NOT NULL,
    source       TEXT        NOT NULL,
    resolution   TEXT        NOT NULL DEFAULT '',
    commit       TEXT        NOT NULL DEFAULT '',
    detail       TEXT        NOT NULL DEFAULT '',
    duplicate_of TEXT        NOT NULL DEFAULT '',

    CONSTRAINT ticket_events_action_known
        CHECK (action IN ('filed', 'closed', 'wontfix', 'reopened', 'duplicate', 'note')),
    CONSTRAINT ticket_events_have_a_source CHECK (btrim(source) <> '')
);

CREATE INDEX ticket_events_by_ticket ON ticket_events (ticket, at);

COMMENT ON TABLE  ticket_events IS
    'Every thing that happened to every ticket, in order. Append-only in the log this is built from; nothing is ever edited, so a correction is another event and a wrong closure stays visible beside its reopening.';

CREATE VIEW open_debt AS
SELECT
    severity,
    kind,
    count(*)                                    AS tickets,
    count(*) FILTER (WHERE cardinality(sources) > 1) AS corroborated,
    count(*) FILTER (WHERE reopens > 0)         AS reopened,
    min(opened_at)                              AS oldest
FROM tickets
WHERE status = 'open'
GROUP BY severity, kind;

COMMENT ON VIEW open_debt IS
    'What is open, by severity and kind. `oldest` is the one to look at: a P1 that has been open for three weeks is a decision nobody made.';
