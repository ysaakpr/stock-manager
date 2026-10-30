-- 0021_websearch_usage — request counts for the M17 web research tools (analyst.websearch).
--
-- Two counters, both reserved before a request is sent (analyst/websearch/ledger.py):
--
--   websearch_vendor_month   units spent per vendor per calendar month (IST). A month is its first
--                            day, so the 1st of a new month is a new row and every budget restarts
--                            at zero without anything being reset. `used` is in the vendor's own
--                            billing unit (credits); `direct` is our own fetch, counted, uncapped.
--   websearch_session_usage  requests per M17 session per capability, against the session cap.
--
-- These are counters, not records: a reservation is one conditional UPDATE (`used + n <= budget`),
-- so they are updated in place and carry no reject_mutation trigger. Nothing in them is evidence;
-- the evidence of a search or fetch is its content-addressed Commons snapshot. No key, query or
-- URL is stored here.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

CREATE TABLE websearch_vendor_month (
    vendor      text        NOT NULL CHECK (vendor IN ('brave', 'tavily', 'firecrawl', 'direct')),
    month       date        NOT NULL CHECK (extract(day FROM month) = 1),
    used        integer     NOT NULL DEFAULT 0 CHECK (used >= 0),
    updated_at  timestamptz NOT NULL,
    PRIMARY KEY (vendor, month)
);

COMMENT ON TABLE websearch_vendor_month IS
    'A10 · M17 web research: units spent per vendor per IST calendar month, reserved before '
    'each request (analyst.websearch.ledger). Budgets are configuration (WEBSEARCH_BUDGET_*), '
    'not stored here.';

CREATE TABLE websearch_session_usage (
    session     date        NOT NULL,
    capability  text        NOT NULL CHECK (capability IN ('search', 'fetch')),
    used        integer     NOT NULL DEFAULT 0 CHECK (used >= 0),
    updated_at  timestamptz NOT NULL,
    PRIMARY KEY (session, capability)
);

COMMENT ON TABLE websearch_session_usage IS
    'A10 · M17 web research: requests sent per M17 session per capability, against '
    'WEBSEARCH_SESSION_MAX_SEARCHES / _FETCHES. Cache hits are never counted.';
