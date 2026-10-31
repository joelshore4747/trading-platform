-- 0002_disagreements — what the feeds do not agree about.
--
-- Running several feeds is only worth the cost if the disagreements between
-- them are kept. Two feeds that agree tell you nothing you did not already
-- know from one. Two that disagree have found something, and this is where it
-- goes.
--
-- The distinction this table exists to make, and the reason it is not simply a
-- diff: **two sources quoting the same venue and two sources quoting different
-- venues mean opposite things.**
--
--   Same venue (Polygon and a direct feed both reporting Nasdaq):
--     the prices SHOULD be identical. Any difference is a data error in one of
--     them, and the size of it is the size of the error.
--
--   Different venues (Binance and Coinbase both reporting BTC):
--     the prices SHOULD differ. That difference is the basis, and it is a
--     market fact rather than a fault. Treating it as an error would fill this
--     table with noise and train everyone to ignore it.
--
-- `same_venue` records which kind each row is, taken from `sources.venue` at
-- the time of comparison, so the two are never summed together by accident.
--
-- Volume is recorded and never gated on. Two venues genuinely trade different
-- amounts; even one venue reported by two vendors can differ on volume through
-- trade-condition filtering. A volume difference is context for a price
-- difference, not evidence on its own.

CREATE TABLE reconcile_runs (
    id            BIGSERIAL   PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    market        TEXT        NOT NULL,
    interval      TEXT        NOT NULL,
    instants      BIGINT      NOT NULL DEFAULT 0,
    compared      BIGINT      NOT NULL DEFAULT 0,
    disagreements BIGINT      NOT NULL DEFAULT 0,

    CONSTRAINT reconcile_runs_window_forwards CHECK (window_end >= window_start)
);

COMMENT ON TABLE  reconcile_runs IS
    'One row per comparison pass. `compared` is how many (instant, source pair) comparisons were possible; `disagreements` how many differed. The ratio is the agreement rate, and it only means something with `compared` beside it — 100% agreement over 3 comparisons is not agreement.';
COMMENT ON COLUMN reconcile_runs.instants IS
    'Distinct timestamps that at least two sources both covered. Instants only one source saw cannot be compared and are not evidence of agreement.';

CREATE TABLE source_disagreements (
    id                BIGSERIAL   PRIMARY KEY,
    reconcile_run_id  BIGINT      NOT NULL REFERENCES reconcile_runs (id),
    market            TEXT        NOT NULL,
    symbol            TEXT        NOT NULL,
    interval          TEXT        NOT NULL,
    ts                TIMESTAMPTZ NOT NULL,
    source_a_id       SMALLINT    NOT NULL REFERENCES sources (id),
    source_b_id       SMALLINT    NOT NULL REFERENCES sources (id),
    same_venue        BOOLEAN     NOT NULL,
    close_a           NUMERIC     NOT NULL,
    close_b           NUMERIC     NOT NULL,
    volume_a          NUMERIC     NOT NULL,
    volume_b          NUMERIC     NOT NULL,
    -- The largest relative gap across open, high, low and close. One number to
    -- sort by; the per-field values stay in `bars` for anyone who needs them.
    worst_gap         NUMERIC     NOT NULL,
    worst_field       TEXT        NOT NULL,
    detected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- a < b, so a pair is recorded once rather than once in each direction.
    CONSTRAINT source_disagreements_ordered_pair CHECK (source_a_id < source_b_id),
    CONSTRAINT source_disagreements_gap_positive CHECK (worst_gap > 0),
    CONSTRAINT source_disagreements_once
        UNIQUE (reconcile_run_id, market, symbol, interval, ts, source_a_id, source_b_id)
);

COMMENT ON TABLE  source_disagreements IS
    'Where two sources reported different prices for the same instant. Read with same_venue: false means the two describe different markets and the gap is the basis, true means one of them is wrong.';
COMMENT ON COLUMN source_disagreements.same_venue IS
    'Whether both sources claim the same sources.venue. True: the prices should have been identical and one is a bad print. False: they describe different markets and are expected to differ.';
COMMENT ON COLUMN source_disagreements.worst_gap IS
    'max(|a - b| / a) across open, high, low, close. A fraction, not basis points: 0.0001 is one basis point.';

CREATE INDEX source_disagreements_worst ON source_disagreements (same_venue, worst_gap DESC);
CREATE INDEX source_disagreements_symbol_time ON source_disagreements (symbol, ts DESC);

CREATE VIEW feed_health AS
SELECT
    s.name                                   AS source,
    b.market,
    b.interval,
    count(DISTINCT b.symbol)                 AS symbols,
    count(*)                                 AS bars,
    max(b.ts)                                AS last_bar,
    now() - max(b.ts)                        AS silent_for,
    percentile_disc(0.5) WITHIN GROUP (ORDER BY b.ingested_at - b.ts) AS median_lag
FROM bars b
JOIN sources s ON s.id = b.source_id
WHERE b.ingested_at > now() - interval '1 day'
GROUP BY s.name, b.market, b.interval;

COMMENT ON VIEW feed_health IS
    'Is each feed alive, and how far behind. `silent_for` is the question the operations page leads with; `median_lag` is capture latency over the last day.';
