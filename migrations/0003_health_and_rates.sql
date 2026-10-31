-- 0004 — two corrections an adversarial review of the collector found, both of
-- which made a number say the opposite of what it meant.
--
-- **A feed that has been silent for a day disappeared from `feed_health`.** The
-- view filtered on `ingested_at > now() - interval '1 day'`, so a source with no
-- rows in the last day had no row in the view whose entire stated purpose is
-- "is each feed alive". The one question it exists to answer was the one case it
-- could not answer. `silent_for` is now computed over all of a source's rows;
-- only `median_lag` and `bars_last_day` are windowed, which is what the window
-- was actually for.
--
-- **The agreement rate summed the two kinds of disagreement.** `reconcile.py`
-- says same-venue gaps (an error) and cross-venue gaps (the basis) must never be
-- summed, and then the headline rate subtracted both from the same total. Three
-- sources across two venues reported "agreement 33.3333%" when nothing was wrong
-- at all. The same-venue counts are stored separately so the error rate can be
-- read on its own.

ALTER TABLE reconcile_runs
    ADD COLUMN same_venue_compared      BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN same_venue_disagreements BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN reconcile_runs.same_venue_compared IS
    'Comparisons between two sources claiming the SAME venue. These should agree exactly, so same_venue_disagreements / same_venue_compared is the error rate — the number that means something. The all-in `compared` includes cross-venue pairs, which are expected to differ.';
COMMENT ON COLUMN reconcile_runs.same_venue_disagreements IS
    'How many of those differed. Every one is a bad print in one of the two sources.';

DROP VIEW feed_health;

CREATE VIEW feed_health AS
SELECT
    s.name                                   AS source,
    b.market,
    b.interval,
    count(DISTINCT b.symbol)                 AS symbols,
    count(*)                                 AS bars,
    max(b.ts)                                AS last_bar,
    now() - max(b.ts)                        AS silent_for,
    count(*) FILTER (WHERE b.ingested_at > now() - interval '1 day') AS bars_last_day,
    percentile_disc(0.5) WITHIN GROUP (ORDER BY b.ingested_at - b.ts)
        FILTER (WHERE b.ingested_at > now() - interval '1 day')      AS median_lag
FROM bars b
JOIN sources s ON s.id = b.source_id
GROUP BY s.name, b.market, b.interval;

COMMENT ON VIEW feed_health IS
    'Is each feed alive, and how far behind. `silent_for` is the question the operations page leads with and is computed over ALL of a source''s rows, so a source that stopped a week ago still appears — it is the one that matters most. `bars_last_day` and `median_lag` are windowed to the last day.';
