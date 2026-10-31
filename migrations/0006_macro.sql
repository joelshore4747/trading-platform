-- 0006_macro — government and macro series, stored by VINTAGE.
--
-- The whole reason this table has four key columns instead of three.
--
-- **Macro data is revised after publication.** GDP for a quarter is published
-- once, revised a month later, revised again, and revised years afterwards in an
-- annual benchmark. Storing only the current value means a backtest reads a
-- number nobody had at the time — lookahead, in precisely the form this
-- repository exists to prevent, and the more insidious form because it looks
-- like ordinary data rather than like a bug.
--
-- So every value is `(series, obs_date, vintage, value)`:
--
--   obs_date   the period the number describes (January 2026)
--   vintage    the day that number became the published figure
--   value      what it said on that day
--
-- One `obs_date` therefore has many rows, and a backtest standing at time T
-- reads the row with the greatest vintage that is available at T. Not the
-- greatest vintage — the greatest AVAILABLE one.
--
-- **Availability is not the vintage date.** A figure with vintage 2026-02-01 was
-- released at some hour of that day, usually 08:30 New York. A backtest at
-- 00:00 UTC on 2026-02-01 that reads it has a working day of hindsight.
-- `released_at` holds the exact moment when the source tells us; when it does
-- not, `available_from` is the END of the vintage day, which is the conservative
-- reading and is stated rather than assumed.
--
-- **Nothing is ever updated here.** A revision is a new row with a later
-- vintage. `UPDATE macro_observations SET value = ...` would destroy the record
-- of what was believed, which is the only thing this table is for.

CREATE TABLE macro_series (
    id             SMALLSERIAL PRIMARY KEY,
    code           TEXT        NOT NULL UNIQUE,
    provider       TEXT        NOT NULL,
    title          TEXT        NOT NULL DEFAULT '',
    units          TEXT        NOT NULL DEFAULT '',
    frequency      TEXT        NOT NULL DEFAULT '',
    -- What the provider says about revisions. Recorded because "is this series
    -- revised" decides whether a single-vintage backtest is merely imprecise or
    -- outright wrong.
    revision_policy TEXT       NOT NULL DEFAULT '',
    -- When new values arrive. A strategy that reads a monthly series daily is
    -- reading the same number thirty times.
    release_schedule TEXT      NOT NULL DEFAULT '',
    -- Where it came from, in enough detail to fetch it again.
    source_url     TEXT        NOT NULL DEFAULT '',
    added_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT macro_series_provider_known
        CHECK (provider IN ('fred', 'sec', 'treasury', 'bls', 'derived')),
    CONSTRAINT macro_series_code_shape CHECK (btrim(code) = code AND code <> '')
);

COMMENT ON TABLE  macro_series IS
    'One row per series. `revision_policy` and `release_schedule` are recorded because they decide whether a backtest that ignores vintages is imprecise or wrong.';

CREATE TABLE macro_observations (
    series_id      SMALLINT    NOT NULL REFERENCES macro_series (id),
    -- The period the number describes.
    obs_date       DATE        NOT NULL,
    -- The day this value became the published figure. A revision is a NEW ROW
    -- with a later vintage, never an update.
    vintage        DATE        NOT NULL,
    value          NUMERIC,
    -- The exact publication moment, when the provider states one. NULL means we
    -- only know the day, and `available_from` is then the end of it.
    released_at    TIMESTAMPTZ,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ingest_run_id  BIGINT      REFERENCES ingest_runs (id),

    PRIMARY KEY (series_id, obs_date, vintage)
);

CREATE INDEX macro_observations_series_vintage
    ON macro_observations (series_id, vintage, obs_date);

COMMENT ON TABLE  macro_observations IS
    'Every value ever published for every period. One obs_date has many rows — one per revision. A backtest reads the greatest vintage AVAILABLE at its timestamp, which is not the same as the greatest vintage.';
COMMENT ON COLUMN macro_observations.value IS
    'NULL is a real answer: the provider published no figure for that period. It is stored rather than skipped, because "no value" and "we never fetched it" must not look alike.';
COMMENT ON COLUMN macro_observations.released_at IS
    'The exact publication moment when the provider gives one. NULL means only the day is known, and the value is then treated as available from the END of the vintage day — the conservative reading, stated rather than assumed.';

-- Availability, computed once, so no caller has to remember the rule.
CREATE VIEW macro_available AS
SELECT
    s.code,
    s.provider,
    o.obs_date,
    o.vintage,
    o.value,
    o.released_at,
    coalesce(o.released_at, (o.vintage + 1)::timestamptz) AS available_from,
    o.series_id
FROM macro_observations o
JOIN macro_series s ON s.id = o.series_id;

COMMENT ON VIEW macro_available IS
    'Observations with the moment each became knowable. `available_from` is released_at when the provider states one, and otherwise midnight at the END of the vintage day. Read this, never macro_observations directly, unless you are writing.';

CREATE VIEW macro_revisions AS
SELECT
    s.code,
    o.obs_date,
    count(*)                       AS vintages,
    min(o.vintage)                 AS first_published,
    max(o.vintage)                 AS last_revised,
    min(o.value)                   AS lowest,
    max(o.value)                   AS highest,
    max(o.value) - min(o.value)    AS spread
FROM macro_observations o
JOIN macro_series s ON s.id = o.series_id
GROUP BY s.code, o.obs_date
HAVING count(*) > 1;

COMMENT ON VIEW macro_revisions IS
    'Periods that were revised, and by how much. `spread` is the size of the lookahead a single-vintage backtest would have granted itself on that period.';
