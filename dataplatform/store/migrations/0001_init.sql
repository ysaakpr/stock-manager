-- 0001_init — Postgres schema v1 (EXECUTION_PLAN.md §4.2 sketch, task M0.4).
--
-- What Postgres holds: the masters (D2), the corporate-action and factor tables (D3), sync and
-- quality state (D1/D7), and the analyst's own record — cases, policies, theses, the decision
-- journal, orders and token spend (A1/A4/A9, X1/X3). Prices themselves do NOT live here: L1 is
-- Parquet partitioned by dataset/date (§4.2), and Postgres is the small, transactional,
-- relationally-joined part of the system.
--
-- Conventions this file fixes for every later migration:
--   * ISIN is the only join key (invariant #2). Every instrument-scoped table keys on the `isin`
--     domain; no table stores a symbol as its identifier. Symbols live in symbol_history, which
--     exists precisely so that a symbol can be resolved to an ISIN as of a date and then thrown
--     away.
--   * Money is the `money_inr` domain — NUMERIC, never float or double precision (CLAUDE.md).
--     A binary float cannot represent ₹0.05 exactly, and a cost model that is wrong in the
--     seventh decimal is wrong in the P&L after ten years of daily compounding.
--   * Timestamps are `timestamptz`, trading dates are `date`. A trading date is a calendar date
--     in Asia/Kolkata and is never derived from a timestamp by the database.
--   * Enumerations are text + CHECK rather than PostgreSQL enums: a later migration adds a state
--     with an ordinary ALTER TABLE instead of ALTER TYPE's transaction restrictions (boring
--     tech, decision #13).
--   * Every table carries a COMMENT naming the plan module that owns it, so a schema dump alone
--     answers "who is allowed to write this".
--
-- The runner (dataplatform/store/migrate.py) wraps this file in one transaction, so there is no
-- BEGIN/COMMIT here and no IF NOT EXISTS: this file runs exactly once against a database, and
-- either all of it lands or none of it does.

-- ── domains ─────────────────────────────────────────────────────────────────────────────────

CREATE DOMAIN isin AS text
    CHECK (VALUE ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$');
COMMENT ON DOMAIN isin IS
    'D2 · ISO 6166 identifier — the only join key in this system (invariant #2). The CHECK is '
    'shape-only: it rejects a symbol pasted into an ISIN column, which is the failure it exists '
    'to catch, and does not verify the check digit.';

CREATE DOMAIN money_inr AS numeric(20, 6);
COMMENT ON DOMAIN money_inr IS
    'X1 · A rupee amount. NUMERIC by construction so that no money column in this schema can '
    'ever be a binary float; 6 decimal places because Indian transaction charges (SEBI turnover '
    'fee, exchange txn charges) are quoted in fractions of a paisa.';

CREATE DOMAIN factor AS numeric(30, 15)
    CHECK (VALUE > 0);
COMMENT ON DOMAIN factor IS
    'D3 · A multiplicative adjustment factor (§4.3). Exact decimal, wide scale: factors are '
    'chained across every corporate action in an instrument''s life, so rounding compounds.';

CREATE DOMAIN percent AS numeric(9, 6)
    CHECK (VALUE >= 0 AND VALUE <= 100);
COMMENT ON DOMAIN percent IS
    'A8 · A percentage in 0..100, used by the rails and the rotation dial. NUMERIC because a '
    'rail comparison that is off by a float epsilon is a rail that did not hold.';

-- ── append-only enforcement (invariant #12) ─────────────────────────────────────────────────

CREATE FUNCTION reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table %.% is append-only: % is not permitted',
        TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'feature_not_supported',
              HINT = 'append a superseding row and leave the existing one in place '
                     '(AGENTIC_CONTEXT.md invariant #12)';
END;
$$;
COMMENT ON FUNCTION reject_mutation() IS
    'A9 · Raises on any UPDATE, DELETE or TRUNCATE of an append-only table. Attached as a '
    'STATEMENT-level trigger rather than a row-level one so that a statement matching zero rows '
    'fails too — "DELETE FROM decision_journal WHERE false" must not look like it succeeded.';

-- ── D2 · identity master ────────────────────────────────────────────────────────────────────

CREATE TABLE security_master (
    isin              isin        PRIMARY KEY,
    name              text        NOT NULL,
    primary_exchange  text        NOT NULL CHECK (primary_exchange IN ('NSE', 'BSE')),
    status            text        NOT NULL CHECK (status IN ('ACTIVE', 'SUSPENDED', 'DELISTED')),
    face_value_inr    money_inr,
    first_seen_date   date        NOT NULL,
    last_seen_date    date,
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,
    CONSTRAINT security_master_seen_order CHECK (last_seen_date IS NULL
                                                 OR last_seen_date >= first_seen_date)
);
COMMENT ON TABLE security_master IS
    'D2 · One row per instrument, keyed by ISIN. The spine every other instrument-scoped table '
    'points at. Delisted securities are kept with status DELISTED and never removed — a '
    'point-in-time universe (§4.5) is unbuildable, and a backtest is survivorship-biased, the '
    'moment a dead security can vanish from here.';
COMMENT ON COLUMN security_master.status IS
    'Current listing status. Historical status transitions live in exchange_listing.';

CREATE TABLE symbol_history (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    isin        isin        NOT NULL REFERENCES security_master (isin),
    exchange    text        NOT NULL CHECK (exchange IN ('NSE', 'BSE')),
    symbol      text        NOT NULL,
    series      text,
    valid_from  date        NOT NULL,
    valid_to    date,
    source      text        NOT NULL,
    recorded_at timestamptz NOT NULL,
    CONSTRAINT symbol_history_unique UNIQUE (isin, exchange, symbol, valid_from),
    CONSTRAINT symbol_history_validity CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
COMMENT ON TABLE symbol_history IS
    'D2 · Every (exchange, symbol) an ISIN has ever traded under, with the window it was valid '
    'for. NULL valid_to means current. This is how a raw bhavcopy symbol becomes an ISIN as of a '
    'date; nothing downstream is permitted to join on the symbol itself (invariant #2). Rows are '
    'appended on a rename, never overwritten, because yesterday''s file still says the old name.';
CREATE INDEX symbol_history_lookup ON symbol_history (exchange, symbol, valid_from DESC);
CREATE INDEX symbol_history_isin ON symbol_history (isin);

CREATE TABLE exchange_listing (
    isin           isin        NOT NULL REFERENCES security_master (isin),
    exchange       text        NOT NULL CHECK (exchange IN ('NSE', 'BSE')),
    security_code  text,
    series         text,
    lot_size       integer     CHECK (lot_size IS NULL OR lot_size > 0),
    face_value_inr money_inr,
    listing_date   date,
    delisting_date date,
    status         text        NOT NULL CHECK (status IN ('ACTIVE', 'SUSPENDED', 'DELISTED')),
    recorded_at    timestamptz NOT NULL,
    PRIMARY KEY (isin, exchange)
);
COMMENT ON TABLE exchange_listing IS
    'D2 · Per-exchange listing facts for an ISIN — the BSE scrip code, the NSE series, lot size, '
    'listing and delisting dates. One ISIN is commonly listed on both exchanges; the price tables '
    'carry the exchange alongside the ISIN for exactly this reason.';
COMMENT ON COLUMN exchange_listing.security_code IS
    'The exchange''s own identifier (BSE scrip code, NSE token). Stored so a raw file can be '
    'traced back, never used as a join key.';

-- ── D3 · corporate actions and the adjustment engine ────────────────────────────────────────

CREATE TABLE corporate_actions (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    isin                isin        NOT NULL REFERENCES security_master (isin),
    ex_date             date        NOT NULL,
    action_type         text        NOT NULL CHECK (action_type IN (
                                        'DIVIDEND', 'BONUS', 'SPLIT', 'CONSOLIDATION', 'RIGHTS',
                                        'MERGER', 'DEMERGER', 'FACE_VALUE_CHANGE', 'OTHER')),
    ratio_terms         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    dividend_amount_inr money_inr,
    record_date         date,
    announcement_date   date,
    knowable_date       date        NOT NULL,
    source              text        NOT NULL,
    source_ref          text,
    reconciled          boolean     NOT NULL DEFAULT false,
    reconciliation_note text,
    recorded_at         timestamptz NOT NULL,
    CONSTRAINT corporate_actions_unique UNIQUE (isin, ex_date, action_type, source)
);
COMMENT ON TABLE corporate_actions IS
    'D3 · One row per (ISIN, ex-date, action, source). NSE and BSE describe the same action '
    'differently (§4.1), so both are ingested and `reconciled` records whether they agree; an '
    'unreconciled action is a D7 quality flag, not something the adjustment engine may trust.';
COMMENT ON COLUMN corporate_actions.ratio_terms IS
    'The action''s terms as structured JSON — {"new": 1, "old": 1} for a 1:1 bonus, {"from": 10, '
    '"to": 2} for a 1:5 split, an entity/share map for a demerger. Free-form because a merger '
    'and a dividend have nothing in common but an ex-date; the engine parses per action_type.';
COMMENT ON COLUMN corporate_actions.knowable_date IS
    'First date this row''s content was knowable to us (invariant #7). Corporate actions apply '
    'retroactively to prices but must NOT apply retroactively to decisions: a backtest as of D '
    'may only see rows with knowable_date <= D, even though ex_date may be earlier.';
CREATE INDEX corporate_actions_isin_ex_date ON corporate_actions (isin, ex_date);
CREATE INDEX corporate_actions_unreconciled ON corporate_actions (ex_date)
    WHERE NOT reconciled;

CREATE TABLE adjustment_factors (
    isin                isin        NOT NULL REFERENCES security_master (isin),
    ex_date             date        NOT NULL,
    price_factor        factor      NOT NULL,
    qty_factor          factor      NOT NULL,
    cum_price_factor    factor      NOT NULL,
    cum_qty_factor      factor      NOT NULL,
    corporate_action_id bigint      REFERENCES corporate_actions (id),
    structural_break    boolean     NOT NULL DEFAULT false,
    computed_at         timestamptz NOT NULL,
    PRIMARY KEY (isin, ex_date)
);
COMMENT ON TABLE adjustment_factors IS
    'D3 · The factor chain per ISIN (§4.3). Adjusted prices are raw x cum_price_factor, computed '
    'on read or materialized into L2 — never stored in L1 (invariant #3). A newly ingested '
    'action triggers a full recompute of this chain for that ISIN and invalidates its L2.';
COMMENT ON COLUMN adjustment_factors.structural_break IS
    'True for mergers and demergers, where the ex-date price gap is a structural event and not a '
    'return (§4.3 rule 3). A return series that treats one of these as a 40% drop is a bug the '
    'golden CA suite exists to catch.';

-- ── D1/D4 · ingestion state machine ─────────────────────────────────────────────────────────

CREATE TABLE sync_state (
    source           text        NOT NULL,
    logical_date     date        NOT NULL,
    state            text        NOT NULL CHECK (state IN (
                                     'PENDING', 'FETCHED', 'VALIDATED', 'NORMALIZED',
                                     'PUBLISHED', 'FAILED', 'GAP')),
    attempts         integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    retryable        boolean     NOT NULL DEFAULT true,
    last_error       text,
    checksum         text,
    l0_path          text,
    first_attempt_at timestamptz,
    updated_at       timestamptz NOT NULL,
    PRIMARY KEY (source, logical_date)
);
COMMENT ON TABLE sync_state IS
    'D1 · The §4.4 state machine, one row per (source, date): PENDING -> FETCHED -> VALIDATED -> '
    'NORMALIZED -> PUBLISHED, with FAILED(retryable, attempts) and GAP (expected: holiday or '
    'weekend) as terminal branches. The status API reads this table and the daily loop reads the '
    'status API before it trades (invariant #10).';
COMMENT ON COLUMN sync_state.logical_date IS
    '§4.2 calls this column `date`; it is `logical_date` here to match '
    'dataplatform.store.paths.l0_dir(source, logical_date), which builds the L0 directory for '
    'the same (source, date) pair. It is the date the payload is ABOUT, not the date it was '
    'fetched — a bhavcopy fetched late on a Monday for Friday has Friday''s logical_date.';
COMMENT ON COLUMN sync_state.checksum IS
    'SHA-256 of the L0 payload this row was derived from. L0 is immutable (invariant #1), so a '
    'checksum that no longer matches the file on disk is a corruption alert, never a reason to '
    'rewrite L0.';
CREATE INDEX sync_state_by_date ON sync_state (logical_date, state);
CREATE INDEX sync_state_unfinished ON sync_state (source, logical_date)
    WHERE state <> 'PUBLISHED';

-- ── D7 · data quality ───────────────────────────────────────────────────────────────────────

CREATE TABLE quality_flag (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    logical_date   date        NOT NULL,
    check_name     text        NOT NULL,
    severity       text        NOT NULL CHECK (severity IN ('INFO', 'WARN', 'ERROR')),
    isin           isin,
    source         text,
    detail         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    observed_value numeric,
    threshold      numeric,
    resolved       boolean     NOT NULL DEFAULT false,
    resolved_at    timestamptz,
    resolution     text,
    raised_at      timestamptz NOT NULL,
    CONSTRAINT quality_flag_resolution CHECK (resolved = (resolved_at IS NOT NULL))
);
COMMENT ON TABLE quality_flag IS
    'D7 · Sentinel findings — price anomalies, CA mismatches between exchanges, volume outliers, '
    'missing constituents. Surfaced by GET /status/quality. `isin` is deliberately NOT a foreign '
    'key and may be NULL: a flag about an unknown identifier, or about a whole market date, is '
    'exactly the kind of thing that must still be recordable.';
CREATE INDEX quality_flag_open ON quality_flag (logical_date, severity)
    WHERE NOT resolved;

-- ── A1 · cases and ratified policy ──────────────────────────────────────────────────────────

CREATE TABLE case_ (
    case_id             text        PRIMARY KEY,
    title               text        NOT NULL,
    state               text        NOT NULL CHECK (state IN (
                                        'DRAFT', 'INTERVIEW', 'PROPOSAL', 'RATIFIED', 'FUNDED',
                                        'ACTIVE', 'SUSPENDED', 'CLOSED')),
    funding_mode        text        NOT NULL DEFAULT 'PAPER'
                                    CHECK (funding_mode IN ('PAPER', 'REAL')),
    theme               text,
    horizon_years       integer     CHECK (horizon_years IS NULL OR horizon_years > 0),
    benchmark_primary   text,
    benchmark_secondary text,
    sip_amount_inr      money_inr   CHECK (sip_amount_inr IS NULL OR sip_amount_inr > 0),
    sip_day_of_month    integer     CHECK (sip_day_of_month IS NULL
                                           OR sip_day_of_month BETWEEN 1 AND 28),
    config              jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL,
    updated_at          timestamptz NOT NULL
);
COMMENT ON TABLE case_ IS
    'A1 · One investment case and its §5.1 lifecycle state. Trailing underscore because CASE is '
    'a reserved word. funding_mode PAPER is the default and moving to REAL is decision #8''s '
    'graduation — a human action, never an agent''s (AGENTIC_CONTEXT §3.6); it selects which '
    'Broker is injected and nothing else (invariant #5).';
COMMENT ON COLUMN case_.sip_day_of_month IS
    'Capped at 28 so the instalment date exists in February. Month-end SIPs, if ever wanted, get '
    'their own nullable rule rather than a day that silently skips a month.';

CREATE TABLE policy_set (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id             text        NOT NULL REFERENCES case_ (case_id),
    version             integer     NOT NULL CHECK (version >= 1),
    supersedes_version  integer     CHECK (supersedes_version IS NULL OR supersedes_version >= 1),
    policy              jsonb       NOT NULL,
    rotation_dial_pct   percent     NOT NULL,
    max_position_pct    percent     NOT NULL,
    max_sector_pct      percent     NOT NULL,
    min_holdings        integer     NOT NULL CHECK (min_holdings >= 0),
    drawdown_review_pct percent     NOT NULL,
    ratified_by         text        NOT NULL,
    ratified_at         timestamptz NOT NULL,
    ratification_kind   text        NOT NULL CHECK (ratification_kind IN ('HUMAN', 'FIXTURE')),
    recorded_at         timestamptz NOT NULL,
    CONSTRAINT policy_set_version_unique UNIQUE (case_id, version)
);
COMMENT ON TABLE policy_set IS
    'A1 · The ratified policy set of §5.2 — capital plan, rotation dial, rails, exit menu, cash '
    'policy, monitoring cadence — versioned per case. APPEND-ONLY: a policy change is a new '
    'version requiring a fresh ratification (decisions #4/#5/#9), never an edit, because the '
    'question "which rails were in force when that order was placed" must stay answerable years '
    'later. The rail columns are promoted out of `policy` so A8 reads scalars, not JSON.';
COMMENT ON COLUMN policy_set.ratification_kind IS
    'HUMAN or FIXTURE. FIXTURE is the B9 reference case, valid for paper and tests only; the '
    'real-money path requires a HUMAN row. Keeping the distinction in the schema means the two '
    'can never be confused by a query that forgot to check.';
CREATE INDEX policy_set_current ON policy_set (case_id, version DESC);

CREATE TRIGGER policy_set_append_only
    BEFORE UPDATE OR DELETE ON policy_set
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER policy_set_no_truncate
    BEFORE TRUNCATE ON policy_set
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
-- Belt and braces: the triggers are the real guard, because the application connects as the
-- table owner and owner rights are not subject to grants. These REVOKEs only close the door for
-- any future read-only or reporting role that gets created later.
REVOKE UPDATE, DELETE ON policy_set FROM PUBLIC;

-- ── A4 · theses ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE thesis (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id           text        NOT NULL REFERENCES case_ (case_id),
    isin              isin        NOT NULL REFERENCES security_master (isin),
    version           integer     NOT NULL CHECK (version >= 1),
    sleeve            text        NOT NULL CHECK (sleeve IN ('CORE', 'TACTICAL')),
    driver            text        NOT NULL,
    theme_purity      numeric(5, 4) CHECK (theme_purity IS NULL
                                           OR theme_purity BETWEEN 0 AND 1),
    expected_evidence jsonb       NOT NULL DEFAULT '[]'::jsonb,
    break_conditions  jsonb       NOT NULL DEFAULT '[]'::jsonb,
    status            text        NOT NULL CHECK (status IN (
                                      'DRAFT', 'RATIFIED', 'SUPERSEDED', 'BROKEN')),
    ratified_by       text,
    ratified_at       timestamptz,
    recorded_at       timestamptz NOT NULL,
    CONSTRAINT thesis_version_unique UNIQUE (case_id, isin, version),
    CONSTRAINT thesis_ratified_together CHECK ((ratified_by IS NULL) = (ratified_at IS NULL)),
    CONSTRAINT thesis_ratified_has_ratifier CHECK (status <> 'RATIFIED' OR ratified_by IS NOT NULL)
);
COMMENT ON TABLE thesis IS
    'A4 · The §5.3 thesis object: driver, theme purity, expected evidence and the break '
    'conditions T0/T1 evaluate against. Every CORE holding carries a RATIFIED thesis before its '
    'first buy; an edit is a new version requiring re-ratification, which is why the unique key '
    'includes version and old rows keep status SUPERSEDED. Tactical-sleeve positions carry a '
    'journaled rationale instead and need no row here — that is the point of the sleeve (§5.5).';
CREATE INDEX thesis_case_current ON thesis (case_id, isin, version DESC);

-- ── A9 · the decision journal ───────────────────────────────────────────────────────────────

CREATE TABLE decision_journal (
    id                         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                         timestamptz NOT NULL,
    trading_date               date        NOT NULL,
    case_id                    text        REFERENCES case_ (case_id),
    actor                      text        NOT NULL CHECK (actor IN (
                                               'T0', 'T1', 'T2', 'RAILS', 'EXEC', 'USER',
                                               'SYSTEM')),
    decision                   text        NOT NULL CHECK (decision IN (
                                               'HOLD', 'BUY', 'SELL', 'ESCALATE', 'HEARTBEAT',
                                               'SKIPPED_DATA_RED', 'RAIL_BLOCK',
                                               'POLICY_PROPOSAL')),
    isin                       isin,
    sleeve                     text        CHECK (sleeve IN ('CORE', 'TACTICAL', 'CASH')),
    evidence_snapshot_ref      text,
    break_conditions_evaluated jsonb       NOT NULL DEFAULT '[]'::jsonb,
    rationale                  text,
    model                      text,
    tokens_in                  integer     CHECK (tokens_in IS NULL OR tokens_in >= 0),
    tokens_out                 integer     CHECK (tokens_out IS NULL OR tokens_out >= 0),
    cost_inr                   money_inr   CHECK (cost_inr IS NULL OR cost_inr >= 0),
    orders_ref                 text,
    payload                    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    recorded_at                timestamptz NOT NULL
);
COMMENT ON TABLE decision_journal IS
    'A9 · The product (§0): every decision, including the no-ops. A day on which nothing happened '
    'still writes a HEARTBEAT row with the evidence considered (invariant #9), and a day whose '
    'data was not green writes SKIPPED_DATA_RED and no orders (invariant #10). APPEND-ONLY '
    '(invariant #12) — a journal that can be edited after the fact is not evidence, and the '
    'evidence pack of §5.7 is built entirely from these rows.';
COMMENT ON COLUMN decision_journal.isin IS
    'Not a foreign key: a journal row must be writable even when the decision was about an '
    'instrument the identity master has not ingested yet, and a journal entry can never be '
    'deleted to satisfy a constraint. Referential integrity here would be a way for D2 to block '
    'A9 from recording what happened.';
COMMENT ON COLUMN decision_journal.evidence_snapshot_ref IS
    'Content-addressed reference to the bundle actually shown to the model — prices, filings, '
    'news items. Replay (§8.3.3) reconstructs a decision from this, so it is the bundle that was '
    'shown, not the bundle that could have been assembled later.';
CREATE INDEX decision_journal_by_date ON decision_journal (trading_date DESC, id DESC);
CREATE INDEX decision_journal_by_case ON decision_journal (case_id, ts DESC);

CREATE TRIGGER decision_journal_append_only
    BEFORE UPDATE OR DELETE ON decision_journal
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER decision_journal_no_truncate
    BEFORE TRUNCATE ON decision_journal
    FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation();
REVOKE UPDATE, DELETE ON decision_journal FROM PUBLIC;

-- ── X1 · orders ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE order_ (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_uid           text        NOT NULL UNIQUE,
    case_id             text        NOT NULL REFERENCES case_ (case_id),
    isin                isin        NOT NULL REFERENCES security_master (isin),
    exchange            text        NOT NULL CHECK (exchange IN ('NSE', 'BSE')),
    sleeve              text        NOT NULL CHECK (sleeve IN ('CORE', 'TACTICAL', 'CASH')),
    side                text        NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type          text        NOT NULL CHECK (order_type IN ('MARKET', 'LIMIT')),
    quantity            integer     NOT NULL CHECK (quantity > 0),
    limit_price_inr     money_inr   CHECK (limit_price_inr IS NULL OR limit_price_inr > 0),
    state               text        NOT NULL CHECK (state IN (
                                        'STAGED', 'RAIL_BLOCKED', 'SENT', 'PARTIAL', 'EXECUTED',
                                        'CANCELLED', 'REJECTED')),
    broker              text        NOT NULL CHECK (broker IN ('SIM', 'KITE')),
    broker_order_id     text,
    staged_at           timestamptz NOT NULL,
    staged_for_date     date        NOT NULL,
    executed_at         timestamptz,
    filled_quantity     integer     NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    avg_fill_price_inr  money_inr   CHECK (avg_fill_price_inr IS NULL OR avg_fill_price_inr > 0),
    gross_value_inr     money_inr,
    costs_inr           money_inr   CHECK (costs_inr IS NULL OR costs_inr >= 0),
    net_value_inr       money_inr,
    cost_breakdown      jsonb,
    decision_journal_id bigint      REFERENCES decision_journal (id),
    rejection_reason    text,
    updated_at          timestamptz NOT NULL,
    CONSTRAINT order_limit_has_price CHECK (order_type <> 'LIMIT' OR limit_price_inr IS NOT NULL),
    CONSTRAINT order_fill_within_quantity CHECK (filled_quantity <= quantity),
    CONSTRAINT order_executed_has_fill CHECK (state <> 'EXECUTED' OR (executed_at IS NOT NULL
                                                                     AND filled_quantity > 0))
);
COMMENT ON TABLE order_ IS
    'X1 · Staged and executed orders. Trailing underscore because ORDER is a reserved word. '
    'Decisions stage orders EOD and the next session executes them (§6), so both lifecycle '
    'halves live in one row: state carries it from STAGED to EXECUTED. `broker` records which '
    'Broker implementation was injected — the only difference between paper and real (invariant '
    '#5). RAIL_BLOCKED is a first-class state because a blocked order is a journaled event, not '
    'an order that never existed (invariant #6).';
COMMENT ON COLUMN order_.order_uid IS
    'Caller-generated idempotency key. Unique so that a retried placement after an ambiguous '
    'broker response cannot become two real orders.';
COMMENT ON COLUMN order_.cost_breakdown IS
    'Per-component costs from the one shared cost model (invariant #4) — brokerage, STT, '
    'exchange txn charges, SEBI fee, stamp duty, GST, DP charge. Stored as the model emitted it '
    'so a reconciliation break against the broker ledger can be attributed to a component.';
CREATE INDEX order_open ON order_ (staged_for_date, state)
    WHERE state IN ('STAGED', 'SENT', 'PARTIAL');
CREATE INDEX order_by_case ON order_ (case_id, staged_at DESC);
CREATE INDEX order_by_isin ON order_ (isin, staged_for_date DESC);

-- ── X3 · metering ───────────────────────────────────────────────────────────────────────────

CREATE TABLE token_usage (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                  timestamptz NOT NULL,
    provider            text        NOT NULL,
    model               text        NOT NULL,
    purpose             text        NOT NULL,
    case_id             text        REFERENCES case_ (case_id),
    decision_journal_id bigint      REFERENCES decision_journal (id),
    tokens_in           bigint      NOT NULL CHECK (tokens_in >= 0),
    tokens_out          bigint      NOT NULL CHECK (tokens_out >= 0),
    cached_tokens       bigint      NOT NULL DEFAULT 0 CHECK (cached_tokens >= 0),
    cost_inr            money_inr   NOT NULL CHECK (cost_inr >= 0),
    cost_usd            numeric(20, 6) CHECK (cost_usd IS NULL OR cost_usd >= 0),
    recorded_at         timestamptz NOT NULL
);
COMMENT ON TABLE token_usage IS
    'X3 · One row per model call: who spent what, on which decision. The tiered monitoring design '
    '(§5.4) only works if T1/T2 escalation cost is visible, and the evidence pack reports token '
    'burn alongside returns. cost_inr is the settled figure; cost_usd is what the provider '
    'billed, kept because the FX rate applied is otherwise unrecoverable.';
CREATE INDEX token_usage_by_day ON token_usage (ts DESC);
CREATE INDEX token_usage_by_case ON token_usage (case_id, ts DESC);
