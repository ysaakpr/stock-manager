-- 0020_ca_dividend_component — let one feed record more than one dividend on one ex-date.
--
-- `corporate_actions_unique` was (isin, ex_date, action_type, source): one dividend per feed per
-- day. A company that declares a regular and a special dividend together has two, and both feeds
-- say so — BSE as two records (`Final Dividend - Rs. - 1.2500`, `Special Dividend - Rs. - 5.0000`),
-- NSE as one line (`Final Dividend- Rs 1.25 Per Share/ Special Dividend- Rs 5 Per Share`, or
-- joined by `&`, `+`, `And`). The writer is ON CONFLICT DO NOTHING, so whichever came first was
-- kept and the other silently dropped — and the two feeds, each having dropped a different one,
-- then disagreed: most of the 183 open dividend RATIO_MISMATCH flags are that, not a real
-- disagreement. The dropped special dividends are also missing from every total-return series.
--
-- `component` tells a feed's same-day dividends apart: the dividend's kind (`FINAL`, `SPECIAL`,
-- `INTERIM`, `UNSPECIFIED`), and `KIND#n` for a further one of the same kind in one published line
-- (a REIT's taxable and exempt dividends, both unspecified). '' for every other action type, so
-- their key is unchanged. The kind, not the amount: a feed that revises a dividend's amount must
-- still meet the row it revises, exactly as before (`ingest.corp_actions.dividend_component`).
--
-- Existing dividend rows are backfilled with their stored kind — the same value the code derives
-- — so a re-ingest of an unchanged payload still writes nothing, and the rows the old key dropped
-- are the only ones a re-parse from L0 adds.
--
-- Conventions are 0001_init's: the runner wraps this file in one transaction, so no BEGIN/COMMIT
-- and no IF NOT EXISTS; it runs exactly once.

ALTER TABLE corporate_actions
    ADD COLUMN component text NOT NULL DEFAULT '';

UPDATE corporate_actions
   SET component = COALESCE(ratio_terms->>'dividend_kind', '')
 WHERE action_type = 'DIVIDEND';

ALTER TABLE corporate_actions
    DROP CONSTRAINT corporate_actions_unique;

ALTER TABLE corporate_actions
    ADD CONSTRAINT corporate_actions_unique
    UNIQUE (isin, ex_date, action_type, source, component);

COMMENT ON COLUMN corporate_actions.component IS
    'D3 · Which of one feed''s same-day dividends this row is: the dividend kind (FINAL, SPECIAL, '
    'INTERIM, UNSPECIFIED), KIND#n for a further dividend of the same kind in one published line, '
    'and the empty string for every other action type. Part of corporate_actions_unique, so a '
    'regular and a special dividend declared on one ex-date are two rows from each feed, and '
    'reconciliation pairs them component by component.';
