-- 0003_accounts — what an account was, every time it was marked, and what it
-- refused. Plus the record that has to exist before leverage is permitted.
--
-- Three tables, one idea each.
--
--   `leverage_acknowledgements` — the row `risk/leverage.py` looks for. An
--     account may not hold an instrument above 1x without one. It is written
--     once, by a person, and it states the maximum leverage, the liquidation
--     price at it, and what a 20% adverse move costs. The figures are checked
--     against the arithmetic before the row is written, so this cannot be
--     filled in without working them out.
--
--   `account_marks` — the full state at every mark, not a summary. The
--     §29 accounting invariant (equity = cash + marked positions) is a CHECK
--     here rather than a test, so a ledger bug cannot be persisted at all.
--
--   `risk_rejections` — every refusal with its reason, so "the strategy wanted
--     to breach a limit 400 times today" is a query rather than a grep through
--     a transcript. A rejection is a result: the limit worked.
--
-- Every table carries `stage`. The four stages are kept apart (charter §8), and
-- a row that cannot say which stage produced it is a row that will eventually
-- be read as the wrong one. Nothing here grants any stage: `is_live` is False
-- everywhere and every broker adapter raises. The column records; it does not
-- authorise.

CREATE TABLE leverage_acknowledgements (
    id                       BIGSERIAL   PRIMARY KEY,
    account                  TEXT        NOT NULL,
    symbol                   TEXT        NOT NULL,
    max_leverage             NUMERIC     NOT NULL,
    reference_price          NUMERIC     NOT NULL,
    reference_equity         NUMERIC     NOT NULL,
    stated_liquidation_price NUMERIC     NOT NULL,
    stated_loss_at_20pct     NUMERIC     NOT NULL,
    acknowledged_by          TEXT        NOT NULL,
    acknowledged_at          TIMESTAMPTZ NOT NULL,
    notes                    TEXT        NOT NULL DEFAULT '',
    -- Revoked, never deleted. "Who could take 10x on this account in August"
    -- must stay answerable in December.
    revoked_at               TIMESTAMPTZ,
    revoked_reason           TEXT,

    CONSTRAINT ack_leverage_above_1     CHECK (max_leverage > 1),
    CONSTRAINT ack_reference_positive   CHECK (reference_price > 0 AND reference_equity > 0),
    CONSTRAINT ack_has_a_person         CHECK (btrim(acknowledged_by) <> ''),
    CONSTRAINT ack_revocation_is_whole  CHECK (
        (revoked_at IS NULL AND revoked_reason IS NULL)
        OR (revoked_at IS NOT NULL AND btrim(coalesce(revoked_reason, '')) <> '')
    )
);

-- One LIVE acknowledgement per account and symbol; the revoked ones stay.
CREATE UNIQUE INDEX leverage_acknowledgements_one_live
    ON leverage_acknowledgements (account, symbol) WHERE revoked_at IS NULL;

COMMENT ON TABLE  leverage_acknowledgements IS
    'The record that must exist before an account may hold an instrument above 1x. Written once by a person; the stated figures are verified against the arithmetic in risk/leverage.py before the row is written. Absence of a row is a refusal, not a default.';
COMMENT ON COLUMN leverage_acknowledgements.stated_liquidation_price IS
    'Long liquidation price at max_leverage from reference_price. An UPPER BOUND on the room available: a venue liquidates at its maintenance margin, which is lower than initial margin and is not modelled here, so real liquidation arrives sooner.';
COMMENT ON COLUMN leverage_acknowledgements.stated_loss_at_20pct IS
    'Cash lost on reference_equity fully deployed at max_leverage when the market moves 20% adversely. At 5x that is the whole account; at 10x it is twice the account, and the excess is owed.';

CREATE TABLE account_marks (
    id               BIGSERIAL   PRIMARY KEY,
    account          TEXT        NOT NULL,
    cycle            INTEGER     NOT NULL,
    stage            TEXT        NOT NULL,
    at               TIMESTAMPTZ NOT NULL,
    cash             NUMERIC     NOT NULL,
    positions_value  NUMERIC     NOT NULL,
    equity           NUMERIC     NOT NULL,
    realised_pnl     NUMERIC     NOT NULL,
    unrealised_pnl   NUMERIC     NOT NULL,
    fees             NUMERIC     NOT NULL,
    funding          NUMERIC     NOT NULL,
    exposure         NUMERIC     NOT NULL,
    margin_used      NUMERIC     NOT NULL,
    free_cash        NUMERIC     NOT NULL,
    gross_leverage   NUMERIC     NOT NULL,
    peak_equity      NUMERIC     NOT NULL,
    drawdown_pct     NUMERIC     NOT NULL,
    orders           INTEGER     NOT NULL,
    fills            INTEGER     NOT NULL,
    positions        JSONB       NOT NULL,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- §29, enforced by the database rather than only by a test. A ledger that
    -- cannot balance cannot be written down.
    CONSTRAINT marks_equity_balances    CHECK (equity = cash + positions_value),
    -- free_cash is deliberately allowed to go negative — that is the state a
    -- reader most needs to see — so only the reserve itself is bounded.
    CONSTRAINT marks_margin_not_negative CHECK (margin_used >= 0),
    CONSTRAINT marks_drawdown_not_negative CHECK (drawdown_pct >= 0),
    CONSTRAINT marks_exposure_not_negative CHECK (exposure >= 0),
    CONSTRAINT marks_stage_known CHECK (stage IN ('research', 'simulation', 'paper', 'live')),
    CONSTRAINT marks_cycle_positive CHECK (cycle >= 1),
    -- One mark per account, cycle and instant. A re-mark of the same instant is
    -- either a restart replaying or a bug; either way it must not become a
    -- second row that doubles the count in every aggregate below.
    CONSTRAINT marks_once UNIQUE (account, cycle, at)
);

CREATE INDEX account_marks_account_time ON account_marks (account, at DESC);

COMMENT ON TABLE  account_marks IS
    'The full account state at every mark, not a summary. equity = cash + positions_value is a CHECK, so an unbalanced ledger cannot be stored. gross_leverage is exposure/equity: 0 when flat, and the number a leveraged account is actually running at as opposed to the number its instruments permit.';
COMMENT ON COLUMN account_marks.free_cash IS
    'cash - margin_used. NEGATIVE IS MEANINGFUL: the book requires more collateral than the account holds. Reported, never clamped.';
COMMENT ON COLUMN account_marks.stage IS
    'research | simulation | paper | live. Records which stage produced the row; grants nothing. Today every broker adapter raises and no row can legitimately be live.';
COMMENT ON COLUMN account_marks.positions IS
    'symbol -> signed quantity, as JSON. Flat positions are omitted, so an empty object is a flat account.';

CREATE TABLE risk_rejections (
    id          BIGSERIAL   PRIMARY KEY,
    account     TEXT        NOT NULL,
    cycle       INTEGER     NOT NULL,
    stage       TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    reason      TEXT        NOT NULL,
    detail      TEXT        NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT rejections_stage_known CHECK (stage IN ('research', 'simulation', 'paper', 'live')),
    CONSTRAINT rejections_has_a_reason CHECK (btrim(reason) <> '')
);

CREATE INDEX risk_rejections_account_reason ON risk_rejections (account, reason, ts DESC);

COMMENT ON TABLE  risk_rejections IS
    'Every order a limit refused, with the reason. A rejection is a RESULT, not an error: it is the limit working. A strategy that wanted to breach a cap 400 times today is a finding about the strategy, and this is where you count it.';

CREATE VIEW risk_pressure AS
SELECT
    account,
    date_trunc('day', ts)   AS day,
    reason,
    count(*)                AS rejections,
    count(DISTINCT symbol)  AS symbols,
    min(ts)                 AS first_at,
    max(ts)                 AS last_at
FROM risk_rejections
GROUP BY account, date_trunc('day', ts), reason;

COMMENT ON VIEW risk_pressure IS
    'How hard each strategy pushed against each limit, per account per day. A reason that appears every day is a strategy sized for a cap it does not have.';

CREATE VIEW account_latest AS
SELECT DISTINCT ON (account)
    account, cycle, stage, at, equity, cash, exposure, margin_used, free_cash,
    gross_leverage, peak_equity, drawdown_pct, positions
FROM account_marks
ORDER BY account, at DESC;

COMMENT ON VIEW account_latest IS
    'Where each account stands as of its most recent mark. What the operations page leads with.';
