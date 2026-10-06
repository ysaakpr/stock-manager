# BSE deep backfill 2006 → 2016-09-01, and this week's failed BSE syncs — 2026-10-06

Branch `feat/bse-deep-backfill`. Authority: owner go 2026-10-06 (chat) — *"do each item which can be
done, avoid the blocked"* — the B1 go for the bulk BSE legacy campaign. All times IST.

## 1. This week's FAILED BSE sync rows — causes and outcome

| `sync_state` rows | Cause | Class | Outcome |
|---|---|---|---|
| `bse_scrip_master` 2026-09-08 … 09-23, 11 rows, `expected a non-empty JSON array` | `daily_snapshot` sent the register's `url_template` verbatim, so the request carried the literal `status={Active\|Suspended\|Delisted}`. BSE answered **200 with `[]`** (2-byte L0 payloads, `ListofScripData_Active_202609dd.json`). The request was broken, not the payload. | **code defect** | **Fixed.** `SnapshotSpec.url_tokens` fills the choice (`Active`) from the same `STATUS_TOKEN` `bse_scrip_refresh` uses; any brace left in a snapshot URL is refused before a request. Tests fail on the old code. |
| `bse_scrip_master` 2026-09-24 … 10-05, 7 rows, `ForbiddenError … 403` | `api.bseindia.com` refuses this host with 403. | **source refusal — BLOCKED** | Not worked around. One re-probe at 13:30 with the *fixed* URL (`…&status=Active`, register headers) → **403** again (`sync_state` 2026-10-06 FAILED). |
| `bse_corp_actions` 2026-10-06, units 500083/500117/500183 | Same host, same 403 (the 2026-10-05 23:42 `ca-refresh` hard-stopped after 3). | **source refusal — BLOCKED** | Left FAILED. Retry is a later run once the host answers; nothing here varies UA, headers or rate. |
| `bse_corp_actions` 2016-09-01 unit 517380 (since 2026-09-07) | BSE prints `Bonus issue 0:0` for IGARASHI (ex 2018-09-27). `RatioTerms(0, 0)` raised a pydantic `ValidationError` out of `parse_purpose` and failed the whole scrip. | **parse defect** | **Fixed + re-driven.** Zero bonus/exchange ratios go to the manual queue as `TERMS_CONFLICTING` (as zero-price rights already did). Re-driven at 15:58 from its L0 payload with an empty-script transport (zero requests): 1 published, 18 actions, 1 queued. |
| `bse_scrip_master` GAP 2026-09-14 and 2026-10-02 | Ganesh Chaturthi and Gandhi Jayanti — `nse_holidays.yaml` HOLIDAY; every snapshot source has GAP on both. | **correct** | No change. GAP is the designed state for a closed day. |

Since 2026-09-24 every `api.bseindia.com` endpoint (`ListofScripData`, `DefaultData`) answers 403;
`www.bseindia.com` (bhavcopy) is unaffected. The scrip-master history for 2026-09-08 … 10-05 is lost —
a snapshot-only source cannot be backfilled — and the next capture is the first one the scheduler makes
after the refusal lifts *and* this fix is deployed.

**Side effect, pending:** the 517380 re-drive's standard `finalize_reconcile_and_recompute`
(policy ACCEPT, as `ca_refresh` uses) recomputed 3,600 ISINs and opened **3,452 `l2_invalidation`
rows**. I did **not** drain them: L2 is rebuilt from `prices_raw`, and `prices_raw` 2006-01-02 …
2011-06-21 currently holds foreign NSE partitions (§3) that should not be propagated into L2 before
their owner decides. Drain once §3 is settled:
`uv run python -m dataplatform.store.l2_fill --rebuild-invalidated`.

## 2. BSE legacy bhavcopy 2006 → 2016-08-31 into L0

**Probe (10 requests, into a scratch L0, not the lake).** 2016-08-31, 2014-06-02, 2011-01-03,
2008-01-02, 2007-01-02, 2006-07-03, 2006-04-03 → real zips. 2006-01-02, 2006-01-03 and 2010-01-26
(Republic Day) → **HTTP 200, `text/html`, 14,287 bytes — BSE's Angular shell, never a 404.**
Every file across 2006-2016 has the same 14-column header as 2016-2024, so this is **not a new parser
era**. Frozen fixtures `tests/fixtures/bse_bhavcopy/legacy-2006/` (`EQ030406_CSV.ZIP` and the shell).

**Calendar.** The plan is `nse_holidays.yaml` expected-data dates (coverage 2006-01-01 →). BSE
shared the sessions 2016-2024 exactly (1,943 of 1,943), and 2006-2016 has no BSE file on any NSE
holiday that was checked; the 65 absences below are all on NSE sessions.

**Campaign.** One ranged, resumable run, lease on `www.bseindia.com`:
`uv run python -m dataplatform.ingest.l0_acquire --source bse_bhavcopy_legacy --from 2006-01-02 --to 2016-08-31`
13:37 → 15:31, log `~/campaign/bse-deep-backfill-2026-10-06.log`. Zero 403s, two read timeouts
(retried by the fetcher).

| | Sessions |
|---|---|
| Owed by the calendar 2006-01-02 … 2016-08-31 | 2,645 |
| **Fetched, real zip** | **2,580** |
| `SOFT_404` (200 + HTML shell, kept in L0 as evidence) | 65 |
| Failed / 403 | 0 |

**Span reached: 2006-03-01** is BSE's earliest legacy file (2006-01-02 … 02-28 are all shells).
The 65 absences: all 39 NSE sessions 2006-01-02 … 2006-02-28; 2006-03-28; 2006-04-18 … 04-24 (5);
2008-06-25, 2008-11-14; 17 in 2009 (02-16, 03-17, 03-25, 05-15, 07-07, 07-30, 07-31, 08-20, 08-24,
08-26, 08-28, 09-01, 09-03, 09-08, 09-11, 10-14, 11-30); 2010-01-28. Cause: BSE serves no file for
those NSE sessions (archive gaps — the exchange traded; the file is simply not published).

## 3. Rows identified vs quarantined, and L1 promotion

Resolution is `build_scrip_index` over the current identity master (the 2026-09-06 scrip master),
the same resolver the 2016-2024 legacy era used.

| Span | Sessions with a file | Rows | Resolved to ISIN | `scrip_unresolved` | `prev_close_absent` |
|---|---|---|---|---|---|
| 2006-03-01 … 2011-06-21 | 1,292 | 3,597,091 | 3,524,441 (98.0%) | 72,650 | 0 |
| 2011-06-22 … 2016-08-31 | 1,288 | 3,830,184 | 3,663,292 (95.6%) | 166,874 | 18 |

Unresolved by BSE group (whole span): F (debt) 146,035 · B 27,405 · S 21,671 · A 16,351 · E (gold
ETF) 14,240 · B1 3,819 · T 3,818 · TS 3,511 · Z 944 · others < 300. Mostly non-equity the master
excludes by construction; the equity residue is delisted scrips whose master ISIN is `NA`.

Code changes this needed (all tested, each test fails on the old code):

* **Unresolved rows were dropped.** `_write_bse_legacy` logged 20 codes and discarded the rest
  (RAW_DATA_CATALOG B1 defect (b)); they now land in `prices_raw_quarantine` as `scrip_unresolved`.
* Three archive shapes failed whole sessions (15 sessions): a blank `PREVCLOSE` on a first session
  (10, Jan 2012 → line quarantined `prev_close_absent`); a blank `SC_NAME` (2011-05-05, 2013-10-31 →
  scrip code stands in for the label); a stray second archive member (`.dbf`, nested zip, `.url` —
  2011-06-02, 2011-10-13, 2014-03-26 → the member named `EQ{DDMMYY}.CSV` is read).
* `l0_acquire` reports BSE's shell as `SOFT_404`, never `FETCHED`; the parser names it as a
  soft-404 rather than "unexpected header".

**Promotion.** The backtest chain (`chain-a4a4003`) was gone by 15:45 (its last step, the cap-tier
render, exited with `CapTierCampaignError`), so promotion ran at 15:46:

* **2011-06-22 … 2016-08-31: done.** `uv run python -m dataplatform.ingest.backfill --source
  bse_bhavcopy_legacy --from 2011-06-22 --to 2016-08-31` → 1,275 published, 13 failed; re-run after
  the parser fixes → 13 published. **1,288 / 1,288 PUBLISHED**, 3,663,292 BSE rows in `prices_raw`,
  166,892 BSE quarantine rows. Zero requests (every payload reused from L0).
* **2006-01-02 … 2011-06-21: DONE 2026-10-06 18:50–18:54 IST** (see the update below). *Original note:* **PENDING — blocked by foreign partitions.** Every `prices_raw` partition
  in this span (1,357 of them) was rewritten today between 13:49 and 13:50 by another process with
  NSE rows whose `total_trades` is **all-null** — a schema the canonical writer refuses
  (`PRICES_RAW_SCHEMA.total_trades` is non-nullable). The BSE write preserves other exchanges' rows
  and re-validates them, so it fails loud (`ArrowInvalid: Column 'total_trades' is declared
  non-nullable but contains nulls`) and writes nothing. The first attempt (15:46) left **682
  retryable FAILED rows** for `bse_bhavcopy_legacy` 2006-01-02 … 2008-09-24 (636 of that error, 46
  soft-404 dates); I stopped it there. No partition was modified (mtimes still 13:49-13:50, no
  `.partial` files). This looks like the shared-scratchpad `lake/` whose `L0` is a symlink into the
  primary lake (coordinator warning) — not created by this task. Once the owner has decided what
  those NSE partitions should be, run:

  `uv run python -m dataplatform.ingest.backfill --source bse_bhavcopy_legacy --from 2006-01-02 --to 2011-06-21`

  Expected: 1,292 published, **65 FAILED with the soft-404 ParseError** (BSE published no file;
  `mark_gap` refuses a calendar session, so FAILED-with-cause is the honest state), zero requests.

  **Update 2026-10-06 (D14).** The NSE partitions were re-derived first (`l1-widen-2026-10-06.md`
  §7, 18:48–18:50 IST, with nullable `total_trades` merged in #61). The command above then ran
  18:50:48 → 18:53:59 IST:

  * **1,292 published, 65 FAILED**: the soft-404 ParseError, exactly the 65 dates in §2.
  * **0 fetched.** All 1,357 payloads were reused from L0.
  * 3,524,441 BSE rows in `prices_raw`, 72,650 `scrip_unresolved` in the quarantine. Both match
    the table above.
  * The 682 retryable FAILED rows are closed: 636 are now PUBLISHED, and the 46 soft-404 dates
    among them stay FAILED with that cause.
  * Log: `~/campaign/bse-promote-2006-2011-2026-10-06.log`.

  **The §1 `l2_invalidation` side effect is still pending, deliberately.** The 3,452 queued ISINs
  were **not** drained. A drain now builds each partition over its full L1 history, which includes
  2006–2011. That would write 1,126 of them with pre-2011 history (corrected from an earlier
  1,506; see `l1-widen-2026-10-06.md` §7 for the count). Five of the seven uncurated pre-2011
  steps would reach L2 with them, turning `l2_continuity` red. Drain only after M13.4 curates them
  or M13.8 (in flight) stops the drain from extending history. Note that the weekly `ca_refresh`
  and the monthly `bse_ca_sweep` both drain the queue themselves (`l1-widen-2026-10-06.md` §7).

