-- 0018_commons_sheets — the M17 Research Commons: market sheet and universe sheet (M17.1, A10).
--
-- One build per (trading_date, build_digest). The digest is the sha256 of the build's canonical
-- bytes (analyst/commons/sheets.py), so the same lake gives the same digest and the store records
-- it once. A rebuild over a changed lake has a new digest and writes a new build beside the old one.
-- No row is ever updated or deleted: a manager's decision cites the build it read, so that build
-- must stay readable exactly as it was (invariant #12). All three tables carry 0001's
-- reject_mutation guard.
--
-- Numbers are `numeric` with no fixed scale. The builder quantises every value before hashing, and
-- an unconstrained numeric reads back with the same scale, so a stored build re-digests to the
-- digest it was recorded under (CommonsSheets.verify on every read). None of them is a float.
--
-- Numbered 0018, not 0016: 0016-0017 were held for the 2026-10-06 data-widening push and are kept
-- clear even though neither landed.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

CREATE TABLE commons_build (
    trading_date     date        NOT NULL,
    build_digest     text        NOT NULL CHECK (build_digest ~ '^[0-9a-f]{64}$'),
    market_digest    text        NOT NULL CHECK (market_digest ~ '^[0-9a-f]{64}$'),
    universe_digest  text        NOT NULL CHECK (universe_digest ~ '^[0-9a-f]{64}$'),
    sheet_version    text        NOT NULL CHECK (length(trim(sheet_version)) > 0),
    parameters       jsonb       NOT NULL,
    gaps             jsonb       NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(gaps) = 'array'),
    universe_size    integer     NOT NULL CHECK (universe_size >= 0),
    built_at         timestamptz NOT NULL,
    recorded_at      timestamptz NOT NULL,
    PRIMARY KEY (trading_date, build_digest)
);

COMMENT ON TABLE commons_build IS
    'A10 · M17.1 Research Commons. One row per distinct build of a session''s market and universe '
    'sheets, keyed by (trading_date, build_digest). The digest covers both sheets, the gaps and '
    'the parameters, but not built_at, so the same lake builds the same digest. Append-only.';
COMMENT ON COLUMN commons_build.gaps IS
    'Every source the build could not use: [{source, reason}], sorted. The fields it fed are NULL. '
    'Nothing is imputed.';
COMMENT ON COLUMN commons_build.parameters IS
    'The universe screen (pre-registration §2) and the fixed rule parameters the build ran with.';

CREATE TABLE commons_market_sheet (
    trading_date  date  NOT NULL,
    build_digest  text  NOT NULL,
    sheet         jsonb NOT NULL CHECK (jsonb_typeof(sheet) = 'object'),
    PRIMARY KEY (trading_date, build_digest),
    FOREIGN KEY (trading_date, build_digest) REFERENCES commons_build (trading_date, build_digest)
);

COMMENT ON TABLE commons_market_sheet IS
    'A10 · M17.1 Research Commons. The market sheet of one build: index trends, breadth, India VIX, '
    'sector index returns, delivery anomalies, the policy rate. Decimals are JSON strings. '
    'Append-only.';

CREATE TABLE commons_universe_sheet (
    trading_date         date    NOT NULL,
    build_digest         text    NOT NULL,
    isin                 isin    NOT NULL,
    close                numeric NOT NULL CHECK (close > 0),
    return_1w            numeric,
    return_4w            numeric,
    return_13w           numeric,
    return_52w           numeric,
    from_52w_high        numeric,
    volatility_20        numeric,
    median_traded_value  numeric NOT NULL CHECK (median_traded_value >= 0),
    cap_tier             text    CHECK (cap_tier IN ('large', 'mid', 'small')),
    size_rank            integer CHECK (size_rank > 0),
    sector               text,
    asm                  boolean,
    delivery_pct         numeric,
    revenue_ttm_growth   numeric,
    pat_ttm_growth       numeric,
    roe                  numeric,
    pe_ttm               numeric,
    sector_median_pe     numeric,
    pe_vs_sector         numeric,
    filing_date          date,
    announcements_5s     integer CHECK (announcements_5s >= 0),
    PRIMARY KEY (trading_date, build_digest, isin),
    FOREIGN KEY (trading_date, build_digest) REFERENCES commons_build (trading_date, build_digest)
);

COMMENT ON TABLE commons_universe_sheet IS
    'A10 · M17.1 Research Commons. One row per ISIN in a build''s universe (pre-registration §2: '
    'NSE EQ, priced on the session, 20-session median traded value at or above the floor, not in '
    'GSM/ESM). Keyed by ISIN only (invariant #2). A NULL is a value the build could not derive, '
    'never a zero. Append-only.';
COMMENT ON COLUMN commons_universe_sheet.asm IS
    'On the ASM list in force on the session (flagged, never excluded); NULL when the list was '
    'unavailable.';
COMMENT ON COLUMN commons_universe_sheet.cap_tier IS
    'M13 liquidity-rank tier (backtest.cap_tiers, liquidity_rank_tiers/v1): a proxy for the AMFI '
    'tier, never market cap. NULL past rank 500.';

CREATE TRIGGER commons_build_append_only
    BEFORE UPDATE OR DELETE ON commons_build
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER commons_build_no_truncate
    BEFORE TRUNCATE ON commons_build
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
REVOKE UPDATE, DELETE ON commons_build FROM PUBLIC;

CREATE TRIGGER commons_market_sheet_append_only
    BEFORE UPDATE OR DELETE ON commons_market_sheet
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER commons_market_sheet_no_truncate
    BEFORE TRUNCATE ON commons_market_sheet
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
REVOKE UPDATE, DELETE ON commons_market_sheet FROM PUBLIC;

CREATE TRIGGER commons_universe_sheet_append_only
    BEFORE UPDATE OR DELETE ON commons_universe_sheet
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER commons_universe_sheet_no_truncate
    BEFORE TRUNCATE ON commons_universe_sheet
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
REVOKE UPDATE, DELETE ON commons_universe_sheet FROM PUBLIC;
