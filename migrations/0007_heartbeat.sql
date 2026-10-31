-- A collector's health is only visible when it stops, which is too late.
--
-- `CaptureStats` counts `consecutive_errors` and `sink_failures` and the
-- collector prints them — at exit, and returns them as an exit code. A collector
-- that runs for a month never exits, so those numbers are never read, and the
-- run it opened sits at `status = 'running'` looking identical whether it is
-- capturing a bar a minute or failing every poll (adversarial-reviewer,
-- 2026-09-04, T-f280e8f0).
--
-- A run now carries a heartbeat and its live counters, so the question "is the
-- collector working" is answerable from the database while it is still running,
-- which is when it matters. Nothing here changes an existing row's meaning:
-- both columns are additive, and a run written before this migration has a NULL
-- heartbeat, which reads as "this one never reported" rather than "silent".

ALTER TABLE ingest_runs
    ADD COLUMN heartbeat_at TIMESTAMPTZ,
    ADD COLUMN stats        JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN ingest_runs.heartbeat_at IS
    'When the process last said it was alive. NULL means it has never reported — either it predates the heartbeat or it died before its first one. A `running` row whose heartbeat is older than the collector''s interval is a process that died without closing its run; without this the row is indistinguishable from one that is working.';
COMMENT ON COLUMN ingest_runs.stats IS
    'The live CaptureStats at the last heartbeat: polls, fetched, recorded, withheld_open, already_seen, revised, errors, consecutive_errors, sink_failures. Read by the operations page and by scripts/check_health.py. A snapshot, deliberately not a history — the history is the bars themselves.';

-- The alerting query is "every run still claiming to be running", which is a
-- handful of rows out of a table that grows by one per fetch.
CREATE INDEX ingest_runs_running ON ingest_runs (started_at DESC) WHERE status = 'running';

CREATE VIEW collector_health AS
SELECT
    r.id                            AS run_id,
    s.name                          AS source,
    r.kind,
    r.started_at,
    r.heartbeat_at,
    now() - r.heartbeat_at          AS since_heartbeat,
    r.rows_written,
    coalesce((r.stats ->> 'consecutive_errors')::BIGINT, 0) AS consecutive_errors,
    coalesce((r.stats ->> 'sink_failures')::BIGINT, 0)      AS sink_failures,
    coalesce((r.stats ->> 'dropped')::BIGINT, 0)            AS dropped,
    r.stats
FROM ingest_runs r
JOIN sources s ON s.id = r.source_id
WHERE r.status = 'running'
ORDER BY r.started_at;

COMMENT ON VIEW collector_health IS
    'Every capture or fetch that has not finished, with the counters it last reported. This is the view that answers "is the collector working right now"; `feed_health` answers "has anything arrived", which a wedged process can keep satisfying from bars it wrote yesterday.';
