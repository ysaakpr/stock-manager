-- 0025_thin_trading_label — D7's thinly-traded label: how often each ISIN actually trades.
--
-- Owner ruling (2026-11-01, backlog M13.8): securities that go long stretches without a trade are
-- *marked*, and whether to consider them is the strategy's decision. Written once a session by the
-- `thin_trading_label` job (`dataplatform.quality.thin_trading`), read by the query layer
-- (`dataplatform.query.thin_trading`) and Ops (`/status/thin-trading`).
--
-- A label only. Nothing in the price, factor, quarantine or total-return path reads this table,
-- and it is not part of anything the M17 managers see (that needs a pre-registration amendment).
--
-- One row per (as_of session, ISIN); a rerun of a session replaces that session's rows, earlier
-- sessions are the point-in-time history a backtest reads and are never touched. A session counts
-- as traded when the ISIN traded on any venue it is listed on; capture holes (ours) are counted in
-- capture_holes and never as no-trade sessions. Conventions are 0001_init's: the runner wraps this
-- file in one transaction, so no BEGIN/COMMIT and no IF NOT EXISTS; it runs exactly once.

CREATE TABLE thin_trading_label (
    as_of                 date         NOT NULL,
    isin                  isin         NOT NULL,
    window_start          date         NOT NULL,
    sessions_observed     integer      NOT NULL CHECK (sessions_observed >= 0),
    sessions_traded       integer      NOT NULL CHECK (sessions_traded >= 0),
    capture_holes         integer      NOT NULL CHECK (capture_holes >= 0),
    traded_ratio          numeric(5,4) NOT NULL CHECK (traded_ratio BETWEEN 0 AND 1),
    nse_traded_ratio      numeric(5,4) CHECK (nse_traded_ratio BETWEEN 0 AND 1),
    bse_traded_ratio      numeric(5,4) CHECK (bse_traded_ratio BETWEEN 0 AND 1),
    longest_gap_sessions  integer      NOT NULL CHECK (longest_gap_sessions >= 0),
    longest_gap_days      integer      NOT NULL CHECK (longest_gap_days >= 0),
    longest_gap_from      date,
    longest_gap_to        date,
    current_gap_sessions  integer      NOT NULL CHECK (current_gap_sessions >= 0),
    last_traded           date,
    nse_series            text,
    bse_series            text,
    trade_to_trade        boolean      NOT NULL,
    listing_status        text,
    insufficient_history  boolean      NOT NULL,
    identity_seam         boolean      NOT NULL,
    thin                  boolean      NOT NULL,
    reasons               text[]       NOT NULL,
    computed_at           timestamptz  NOT NULL,
    PRIMARY KEY (as_of, isin),
    CONSTRAINT thin_trading_label_traded_le_observed CHECK (sessions_traded <= sessions_observed),
    CONSTRAINT thin_trading_label_thin_has_reason CHECK (thin = (cardinality(reasons) > 0))
);

CREATE INDEX thin_trading_label_isin ON thin_trading_label (isin, as_of DESC);

COMMENT ON TABLE thin_trading_label IS
    'D7 · Thinly-traded label per (session, ISIN): traded share of the trailing ~250 sessions, the '
    'longest no-trade gap, trade-to-trade and listing status. Information only: no price, factor, '
    'quarantine or total return depends on it. Owner: dataplatform.quality.thin_trading.';
COMMENT ON COLUMN thin_trading_label.capture_holes IS
    'Sessions in the window our capture lacked for a venue the ISIN is listed on (not PUBLISHED in '
    'sync_state, no L1 rows, an L1-refused row, or a PR-bundle trade our bhavcopy missed). Excluded '
    'from the ratio and never part of a no-trade gap.';
COMMENT ON COLUMN thin_trading_label.identity_seam IS
    'The longest gap is mostly sessions another ISIN of the same issuer and security type traded: '
    'a reissue the lineage table lacks an edge for. Ours, so reported and never labelled thin.';
