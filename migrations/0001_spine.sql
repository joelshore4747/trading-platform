-- 0001_spine — sources, ingest runs, and bars.
--
-- The three tables everything else hangs off. The shape is decided by two
-- rules that are not negotiable here.
--
-- ONE: a bar is identified by its SOURCE as well as its symbol and time.
-- Two venues quoting BTC at 09:31 are two facts, not one fact observed twice,
-- and the moment they are merged into a single row the disagreement between
-- them is gone and cannot be recovered. Disagreement is the reason for having
-- more than one source at all. So the primary key on `bars` includes
-- `source_id`, and nothing in this schema ever collapses two sources into one.
--
-- TWO: every row can be traced back to the fetch that produced it. `bars`
-- points at an `ingest_run`, which points at a `source` and records when it
-- ran, what it asked for, and how it ended. A number nobody can trace is a
-- number nobody should act on.
--
-- Prices are NUMERIC, never double precision. The rest of this repository
-- keeps money in Decimal for the same reason: binary floating point cannot
-- represent 0.1, and a cent lost per row becomes a figure that argues with
-- itself. NUMERIC round-trips to Decimal exactly.
--
-- Every table and column carries a COMMENT. They are not decoration: an agent
-- (or a person) can read the whole data model out of the database itself with
-- a query against pg_description, without being handed a document that may
-- have drifted from the schema.

CREATE TABLE sources (
    id           SMALLSERIAL PRIMARY KEY,
    name         TEXT        NOT NULL UNIQUE,
    kind         TEXT        NOT NULL,
    venue        TEXT,
    description  TEXT        NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT sources_kind_known CHECK (kind IN ('venue', 'vendor', 'government', 'derived')),
    CONSTRAINT sources_name_shape CHECK (name ~ '^[a-z0-9][a-z0-9._-]{0,62}$')
);

COMMENT ON TABLE  sources IS
    'Where data came from. One row per distinct origin: a venue''s API, a vendor archive, a government series. Referenced by every fact in the database.';
COMMENT ON COLUMN sources.name IS
    'Stable identifier used in code and queries, e.g. alpaca-crypto, binance-um, fred. Lower case, never renamed once data references it.';
COMMENT ON COLUMN sources.kind IS
    'venue = a trading venue''s own feed; vendor = a redistributor or archive; government = a public statistical series; derived = computed here from other sources.';
COMMENT ON COLUMN sources.venue IS
    'The exchange this data describes, when that differs from who supplied it. A vendor redistributing Binance prices has venue = binance.';

CREATE TABLE ingest_runs (
    id            BIGSERIAL   PRIMARY KEY,
    source_id     SMALLINT    NOT NULL REFERENCES sources (id),
    kind          TEXT        NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT        NOT NULL DEFAULT 'running',
    params        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    rows_written  BIGINT      NOT NULL DEFAULT 0,
    error         TEXT        NOT NULL DEFAULT '',

    CONSTRAINT ingest_runs_status_known CHECK (status IN ('running', 'ok', 'failed')),
    CONSTRAINT ingest_runs_finished_when_done CHECK (
        (status = 'running' AND finished_at IS NULL)
        OR (status <> 'running' AND finished_at IS NOT NULL)
    ),
    CONSTRAINT ingest_runs_failed_says_why CHECK (status <> 'failed' OR error <> ''),
    CONSTRAINT ingest_runs_not_backwards CHECK (finished_at IS NULL OR finished_at >= started_at)
);

COMMENT ON TABLE  ingest_runs IS
    'One row per fetch or capture session. Opened before any row is written and closed when it ends, so a run that died mid-way is visible as status=running with no finished_at rather than as a silent short read.';
COMMENT ON COLUMN ingest_runs.kind IS
    'What the run was doing, e.g. backfill, live-capture, import-archive. Free text on purpose: new kinds should not need a migration.';
COMMENT ON COLUMN ingest_runs.params IS
    'Exactly what was asked for: symbols, interval, window, feed tier. Enough to repeat the run.';
COMMENT ON COLUMN ingest_runs.status IS
    'running until the run ends. ok on clean completion. failed with a non-empty error otherwise. A run left running is a crash, and is meant to look like one.';

CREATE INDEX ingest_runs_source_started ON ingest_runs (source_id, started_at DESC);
CREATE INDEX ingest_runs_unfinished ON ingest_runs (started_at DESC) WHERE status = 'running';

CREATE TABLE bars (
    source_id      SMALLINT    NOT NULL REFERENCES sources (id),
    market         TEXT        NOT NULL,
    symbol         TEXT        NOT NULL,
    interval       TEXT        NOT NULL,
    ts             TIMESTAMPTZ NOT NULL,
    open           NUMERIC     NOT NULL,
    high           NUMERIC     NOT NULL,
    low            NUMERIC     NOT NULL,
    close          NUMERIC     NOT NULL,
    volume         NUMERIC     NOT NULL,
    ingest_run_id  BIGINT      NOT NULL REFERENCES ingest_runs (id),
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (source_id, market, symbol, interval, ts),

    CONSTRAINT bars_high_is_highest CHECK (high >= open AND high >= close AND high >= low),
    CONSTRAINT bars_low_is_lowest   CHECK (low  <= open AND low  <= close),
    CONSTRAINT bars_prices_positive CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0),
    CONSTRAINT bars_volume_not_negative CHECK (volume >= 0),
    CONSTRAINT bars_symbol_shape CHECK (symbol <> '' AND symbol = btrim(symbol))
);

COMMENT ON TABLE  bars IS
    'OHLCV, one row per source per symbol per interval per instant. The primary key includes source_id deliberately: the same bar from two feeds is two rows, and comparing them is how a bad print is found. Nothing here merges sources.';
COMMENT ON COLUMN bars.ts IS
    'The bar''s CLOSE time, timezone-aware. The whole engine assumes close-stamped bars; a feed that stamps its bars at the open must be shifted before it reaches this table, or every signal fires one bar early.';
COMMENT ON COLUMN bars.market IS
    'Instrument family: spot, um (USD-margined perpetual), cm (coin-margined), equity, etf, option. Matches data.cache.MARKETS.';
COMMENT ON COLUMN bars.interval IS
    'Bar length in the vocabulary of data.intervals, e.g. 1m, 5m, 1h, 1d.';
COMMENT ON COLUMN bars.ingested_at IS
    'When this row was written here, which is not when the bar happened. The gap between ts and ingested_at is the capture latency.';

-- Reading by symbol across sources is the query the reconciler runs and the one
-- a chart needs; the primary key leads with source_id and cannot serve it.
CREATE INDEX bars_symbol_time ON bars (market, symbol, interval, ts DESC);

CREATE VIEW source_coverage AS
SELECT
    s.name       AS source,
    b.market,
    b.symbol,
    b.interval,
    count(*)     AS bars,
    min(b.ts)    AS first_bar,
    max(b.ts)    AS last_bar,
    max(b.ingested_at) AS last_written
FROM bars b
JOIN sources s ON s.id = b.source_id
GROUP BY s.name, b.market, b.symbol, b.interval;

COMMENT ON VIEW source_coverage IS
    'What each source has actually delivered. The first question to ask of this database, and the one the operations page leads with.';
