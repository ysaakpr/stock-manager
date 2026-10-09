-- 0019_commons_shortlist_digests — the M17 Commons shortlist and filing digests (M17.2, A10).
--
-- The shortlist: one per (trading_date, shortlist_digest), citing the 0018 sheets build it ranked.
-- The digest is the sha256 of the shortlist's canonical bytes (analyst/commons/shortlist.py), so
-- the same lake gives the same digest and the store records it once. Its entries are one row per
-- position, keyed by ISIN only (invariant #2).
--
-- The digests: one per filing id, ever. A filing is digested once and every manager reads that one
-- digest (pre-registration §1, §4 step 2). The body is the model's factual output; it has no field
-- for a recommendation, a rating or a price view, and the writer refuses one before it gets here.
-- Each digest carries its model, provider, prompt digest and token usage (decision #12). The runs
-- table records which filings each session's build covered and which failed.
--
-- No row is ever updated or deleted (invariant #12). All four tables carry 0001's reject_mutation
-- guard. Numbers are `numeric` with no fixed scale, as in 0018, so a stored shortlist re-digests to
-- the digest it was recorded under. None of them is a float.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

CREATE TABLE commons_shortlist_build (
    trading_date       date        NOT NULL,
    shortlist_digest   text        NOT NULL CHECK (shortlist_digest ~ '^[0-9a-f]{64}$'),
    build_digest       text        NOT NULL,
    shortlist_version  text        NOT NULL CHECK (length(trim(shortlist_version)) > 0),
    rule_hash          text        NOT NULL CHECK (rule_hash ~ '^[0-9a-f]{64}$'),
    universe_size      integer     NOT NULL CHECK (universe_size >= 0),
    coverage           jsonb       NOT NULL CHECK (jsonb_typeof(coverage) = 'object'),
    gaps               jsonb       NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(gaps) = 'array'),
    shortlist_size     integer     NOT NULL CHECK (shortlist_size >= 0),
    built_at           timestamptz NOT NULL,
    recorded_at        timestamptz NOT NULL,
    PRIMARY KEY (trading_date, shortlist_digest),
    FOREIGN KEY (trading_date, build_digest) REFERENCES commons_build (trading_date, build_digest)
);

COMMENT ON TABLE commons_shortlist_build IS
    'A10 · M17.2 Research Commons. One row per distinct shortlist of a session: the frozen rule '
    '(pre-registration §3, rule_hash) applied to the cited sheets build. The digest covers the '
    'entries, coverage and gaps, not built_at. Append-only.';
COMMENT ON COLUMN commons_shortlist_build.coverage IS
    'Per factor, the universe names where it was defined. The rest took the mean rank (1/2).';

CREATE TABLE commons_shortlist (
    trading_date               date    NOT NULL,
    shortlist_digest           text    NOT NULL,
    position                   integer NOT NULL CHECK (position >= 1),
    isin                       isin    NOT NULL,
    momentum_12_1              numeric,
    relative_strength_20       numeric,
    earnings_surprise          numeric,
    log_liquidity              numeric,
    rank_momentum_12_1         numeric NOT NULL CHECK (rank_momentum_12_1 BETWEEN 0 AND 1),
    rank_relative_strength_20  numeric NOT NULL CHECK (rank_relative_strength_20 BETWEEN 0 AND 1),
    rank_earnings_surprise     numeric NOT NULL CHECK (rank_earnings_surprise BETWEEN 0 AND 1),
    rank_log_liquidity         numeric NOT NULL CHECK (rank_log_liquidity BETWEEN 0 AND 1),
    composite                  numeric NOT NULL CHECK (composite BETWEEN 0 AND 1),
    PRIMARY KEY (trading_date, shortlist_digest, position),
    UNIQUE (trading_date, shortlist_digest, isin),
    FOREIGN KEY (trading_date, shortlist_digest)
        REFERENCES commons_shortlist_build (trading_date, shortlist_digest)
);

COMMENT ON TABLE commons_shortlist IS
    'A10 · M17.2 Research Commons. One row per shortlisted name, by position: the four raw factors '
    '(NULL = undefined, never zero), their percentile ranks and the equal-weight composite. Keyed by '
    'ISIN only (invariant #2). Append-only.';

CREATE TABLE commons_filing_digest (
    filing_id           text        PRIMARY KEY CHECK (length(trim(filing_id)) > 0),
    kind                text        NOT NULL CHECK (kind IN ('ANNOUNCEMENT', 'RESULTS')),
    isin                isin        NOT NULL,
    knowable_date       date        NOT NULL,
    trading_date        date        NOT NULL CHECK (trading_date >= knowable_date),
    digest_version      text        NOT NULL CHECK (length(trim(digest_version)) > 0),
    input_digest        text        NOT NULL CHECK (input_digest ~ '^[0-9a-f]{64}$'),
    input_truncated     boolean     NOT NULL,
    prompt_digest       text        NOT NULL CHECK (prompt_digest ~ '^[0-9a-f]{64}$'),
    provider            text        NOT NULL CHECK (length(trim(provider)) > 0),
    model               text        NOT NULL CHECK (length(trim(model)) > 0),
    body                jsonb       NOT NULL CHECK (jsonb_typeof(body) = 'object'),
    input_tokens        integer     NOT NULL CHECK (input_tokens >= 0),
    output_tokens       integer     NOT NULL CHECK (output_tokens >= 0),
    cache_write_tokens  integer     NOT NULL CHECK (cache_write_tokens >= 0),
    cache_read_tokens   integer     NOT NULL CHECK (cache_read_tokens >= 0),
    digested_at         timestamptz NOT NULL,
    recorded_at         timestamptz NOT NULL,
    -- The body's keys are exactly the factual schema's: no recommendation, rating or price field.
    CHECK (body ?& ARRAY['headline', 'disclosed', 'period', 'figures']),
    CHECK (body - ARRAY['headline', 'disclosed', 'period', 'figures'] = '{}'::jsonb)
);

CREATE INDEX commons_filing_digest_by_isin ON commons_filing_digest (isin, knowable_date);

COMMENT ON TABLE commons_filing_digest IS
    'A10 · M17.2 Research Commons. One factual digest per filing id (an NSE announcement or a '
    'results filing), made once by one model call and read by every manager. trading_date is the '
    'build that made it. Token usage is the provider''s count. Append-only.';

CREATE TABLE commons_digest_run (
    trading_date  date        NOT NULL,
    run_digest    text        NOT NULL CHECK (run_digest ~ '^[0-9a-f]{64}$'),
    since_date    date        NOT NULL CHECK (since_date <= trading_date),
    filing_ids    jsonb       NOT NULL CHECK (jsonb_typeof(filing_ids) = 'array'),
    digested      jsonb       NOT NULL CHECK (jsonb_typeof(digested) = 'array'),
    cached        jsonb       NOT NULL CHECK (jsonb_typeof(cached) = 'array'),
    failures      jsonb       NOT NULL CHECK (jsonb_typeof(failures) = 'array'),
    gaps          jsonb       NOT NULL CHECK (jsonb_typeof(gaps) = 'array'),
    recorded_at   timestamptz NOT NULL,
    PRIMARY KEY (trading_date, run_digest)
);

COMMENT ON TABLE commons_digest_run IS
    'A10 · M17.2 Research Commons. One row per distinct digest run of a session: the filings in '
    'its window (since_date, trading_date], which were new, which were cache hits, and which '
    'failed and why. Append-only.';

CREATE TRIGGER commons_shortlist_build_append_only
    BEFORE UPDATE OR DELETE ON commons_shortlist_build
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER commons_shortlist_build_no_truncate
    BEFORE TRUNCATE ON commons_shortlist_build
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
REVOKE UPDATE, DELETE ON commons_shortlist_build FROM PUBLIC;

CREATE TRIGGER commons_shortlist_append_only
    BEFORE UPDATE OR DELETE ON commons_shortlist
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER commons_shortlist_no_truncate
    BEFORE TRUNCATE ON commons_shortlist
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
REVOKE UPDATE, DELETE ON commons_shortlist FROM PUBLIC;

CREATE TRIGGER commons_filing_digest_append_only
    BEFORE UPDATE OR DELETE ON commons_filing_digest
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER commons_filing_digest_no_truncate
    BEFORE TRUNCATE ON commons_filing_digest
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
REVOKE UPDATE, DELETE ON commons_filing_digest FROM PUBLIC;

CREATE TRIGGER commons_digest_run_append_only
    BEFORE UPDATE OR DELETE ON commons_digest_run
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER commons_digest_run_no_truncate
    BEFORE TRUNCATE ON commons_digest_run
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
REVOKE UPDATE, DELETE ON commons_digest_run FROM PUBLIC;
