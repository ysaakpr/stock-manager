# Data lake status: availability and quality as of 2026-11-06

**As of:** Fri 2026-11-06, 15:08–15:45 IST. The last session in the lake is **Thu 2026-11-05**. Today's
EOD pipeline (18:30 IST) had not run yet.
**Scope:** every dataset in `data/L0` (by source), `data/L1` and `data/L2`, the Postgres stores
(corporate actions, factors, quality flags, `sync_state`), the status API and the L0 S3 mirror.
**Method:** read-only throughout. Sources were the status API on localhost:8000, parquet footers and
narrow DuckDB scans on one thread, L0 `.meta.json` sidecars, and `SELECT`s in a read-only Postgres
session. No fetch, no job trigger, no DB or lake write. Every number was recounted today; none is
copied from an earlier report. The raw numbers are saved as `lake_status.json` with this run (not
committed).

---

## 1. Verdict

**The lake is complete enough for every current consumer. One consumer, the M17 fund managers, gets
materially thinner dossiers than the lake could give it.** All fetch campaigns that were planned
have finished. Every scheduled job is healthy, and 2026-11-05 is green for NSE prices, delivery and
BSE prices.

**The NIFTY 500 point-in-time (PIT) membership needs no fetch and no publish.** M14.1's extended
build was published on **Sat 2026-10-10 09:49 IST** as build `date=2026-10-10`, which
`read_membership_history` reads.
- Its NIFTY 500 coverage starts on **2015-03-28**, not 2016-10-24.
- It returns 500 members on 2016-09-01.
- On 2026-11-05 it matches the constituents snapshot exactly, for all seven tracked indices.

The 2016-10-24 figure in the M14.5 report, in PR #168's pre-registration and in older notes was
true before 2026-10-10 and is now out of date (§3).

| Consumer | State | One-line reason |
|---|---|---|
| EOD platform (System 1) | **OK** | 26/26 jobs OK; every source healthy except 3 BSE sources paused under D29 (accepted); 53/53 open ERROR flags accepted (D27, D33) |
| Momentum v2 paper book | **OK, with caveats** | Its NIFTY 500 PIT universe is current. L2 is 4 sessions behind L1, so CONCORDBIO's 1:1 bonus (ex 2026-11-04) reads as a raw −47% until Saturday's rebuild. 3 REITs in NIFTY 500 have no L2 series. |
| Backtests | **OK, inside the stated windows** | Prices, L2, TRI and NIFTY 500 PIT cover the M12 windows (2016-09-01 → 2026-08-31) on both universes. Fundamentals before 2025 are thin (§5). |
| M17 fund managers | **Caveat** | Board meetings and announcements are keyed to stale ISINs for **129 of the NIFTY 500**, so their results dates and announcement lists are empty. TTM growth is null for **51 of the top 100**. |
| Console | **OK** | It serves the same status API; `/console` answers 200. |

**What is missing, in priority order** (details and the owner decision each needs are in §6):

1. **Stale ISINs in NSE's corporate-filing feeds.**
   - The board-meeting, announcement and shareholding feeds state a company's *original* ISIN, for
     example HDFCBANK `INE040A01018` (pre-2011) where it now trades as `INE040A01034`.
   - The lake stores that ISIN verbatim, and the M17 readers join on the current one, so **129
     NIFTY 500 names** get no results dates and no announcements.
   - In each of the three feeds, about 22% of ISINs do not trade on NSE today: 539 of 2,429 board
     meetings, 536 of 2,430 announcements, 554 of 2,479 shareholding.
   - The fix needs no fetch: re-key through the D2 ISIN lineage, which already holds these links.
2. **Pre-2025 fundamentals are missing for names whose ISIN changed.**
   - NSE's old results index (2016–2024 filings) uses the same stale ISINs.
   - The backfill skipped those filings as out of universe, so they were never fetched.
   - **108 NIFTY 500 names that listed before 2024 have PIT quarters only from 2025**, including
     HDFCBANK and ASIANPAINT.
   - Recovering them needs a targeted fetch.
3. **XBRL parse and fetch failures not yet accepted: 1,213** of 2,640 FAILED rows.
   - 710 are `MissingPayloadError`, which needs a re-fetch.
   - 503 need a ruling.
4. **The L0 S3 mirror is 4 weeks stale.**
   - It was last synced and verified on 2026-10-08 16:33 IST.
   - 4,734 L0 payload files written since then exist only on this box (plus the local sha256
     manifest).
   - Nothing schedules the sync.
5. **L2 is 4 sessions behind L1**, which is normal between Saturday rebuilds. The CONCORDBIO bonus
   in the lag window heals at the `ca_refresh` of Sat 2026-11-07 10:00 IST.
6. **Unscheduled or stale secondary series:**
   - NIFTY Midcap 150 and Smallcap 250 TRI stop at 2026-10-01 (opt-in, no job).
   - `price_session_attributes` stops at 2026-10-05 (one-shot).
   - The membership-history rebuild is manual. It is current today: none of the three releases since
     2026-10-10 touches a tracked index.
7. **BSE (D26, D29):**
   - BSE corporate actions end at ex-date 2026-09-30.
   - BSE announcements and the scrip master stopped on 2026-09-24.
   - Known and accepted. It affects BSE-only names only.
8. **243 small bookkeeping gaps outside XBRL are unexplained**: archive defects, a site outage and
   unit-keyed rows (§4.2). None of them hides missing price data.

---

## 2. Availability inventory

Sessions are counted against the checked-in NSE calendar through 2026-11-05. "Missing" means
calendar sessions with no partition between the dataset's own first and last date. All times are
IST. Job schedules come from `dataplatform/scheduler/registry.py`; cron times are IST.

### 2.1 L1 (normalised, ISIN-keyed)

| Dataset | First | Last | Rows | ISINs | Last written | Refresh job (schedule) | Missing sessions |
|---|---|---|---|---|---|---|---|
| `prices_raw`, NSE | 2006-01-02 | 2026-11-05 | 9,242,090 | 8,443 | 11-05 18:30 | `eod_pipeline` (18:30 Mon–Fri) | **0** of 5,131 |
| `prices_raw`, BSE | 2006-03-01 | 2026-11-05 | 16,034,616 | 12,204 | 11-05 18:30 | `eod_pipeline` | 26 (2006: 6, 2008: 2, 2009: 17, 2010: 1; BSE served its HTML soft-404) |
| delivery (`prices_raw.deliv_qty`, NSE) | 2011-06-22 | 2026-11-05 | in `prices_raw` | — | 11-05 18:30 | `eod_pipeline` (`nse_delivery`) | 0 of 3,782. One special session absent: 2021-11-04 Muhurat, whose file states 11-03 |
| `prices_raw_quarantine` | 2006-01-02 | 2021-02-16 | 521,252 | 6 | 10-06 18:53 | written by the price writers when needed | n/a (event table) |
| `price_session_attributes` | 2016-09-01 | **2026-10-05** | 6,960,737 | 12,898 | 10-06 19:05 | **none** (one-shot CLI) | 146 in 2016–2019 (source eras), plus everything after 10-05 |
| `benchmark_tri`, NIFTY 50 | 1999-06-30 | 2026-11-05 | 6,804 | — | 11-05 19:50 | `tri_evening` (19:50, 20:50, 21:30 Mon–Fri), `tri_refresh` (Sat 08:00) | 0 since 2006 |
| `benchmark_tri`, NIFTY 500 | 1995-01-01 | 2026-11-05 | 7,892 | — | 11-05 19:50 | same | 0 since 2006 |
| `benchmark_tri`, NIFTY IT, NIFTY CPSE | 1999 / 2009 | 2026-11-05 | 6,805 / 4,422 | — | 11-05 19:50 | same | 0 |
| `benchmark_tri`, Midcap 150, Smallcap 250 | 2005-04-01 | **2026-10-01** | 5,334 each | — | — | **none** (opt-in index set) | 0 up to 10-01, then 23 sessions stale |
| `index_constituents` (22 indices) | 2026-09-08 | 2026-11-05 | 53,188 | 535 | 11-05 19:16 | `daily_snapshot` (19:15 Mon–Fri), `constituents_snapshot` (Sat 20:00) | 0 of 40 |
| `index_membership_history` (7 indices) | build 2026-10-10 | — | 9,905 (all builds) | see §3 | 10-10 09:49 | **none**: `index_press_refresh` (Sat 09:00) lands releases in L0 only; the L1 build is manual | n/a |
| `index_change_events` | build 2026-10-10 | — | 13,052 | 1,051 | 10-10 09:49 | none (built with the history) | n/a |
| `pr_security_marks` | 2010-01-04 | 2026-11-04 | 8,198,101 | 8,427 | 11-05 19:20 | `pr_bundle_l1` (19:20 Mon–Fri) from L0 fetched by `eod_pipeline` | 4 (2011-08-19, 2013-01-10, 2013-06-04, 2018-01-02: archive defects, documented in `pr-bundle-inventory-2026-10-06.md`) |
| `pr_ca_broadcasts` | 2010-01-04 | 2026-11-04 | 956,206 | 4,946 | 11-05 19:20 | `pr_bundle_l1` | same 4 |
| `pr_band_hits` / `pr_index_eod` | 2010-01-04 | 2026-11-04 | 838,076 / 229,543 | 5,512 / — | 11-05 19:20 | `pr_bundle_l1` | same 4 |
| `pr_bundle_quarantine` | 2010-01-04 | 2026-11-04 | 835,682 | — | 11-05 19:20 | `pr_bundle_l1` | 5 (the 4 above plus 2022-01-10) |
| `fo_contracts` (F&O) | 2024-07-08 | 2026-11-05 | 21,417,932 | 289 underlyings | 11-05 20:00 | `nse_daily_capture` (20:00, 23:00 Mon–Fri) | 0 of 575 |
| `deals` (bulk 2,143, block 42) | 2026-10-05 | 2026-11-05 | 2,185 | 239 | 11-05 20:00 | `nse_daily_capture` | 0 of 23 (forward-only: the endpoint serves the latest session only) |
| `fii_dii_flows` | 2026-10-05 | 2026-11-05 | 46 | — | 11-05 20:00 | `nse_daily_capture` | 0 of 23 (forward-only) |
| `board_meetings` | 2026-03-02 (knowable) | 2026-11-05 | 17,276 | 2,429 (**539 not trading on NSE**) | 11-05 20:00 | `nse_daily_capture` | n/a (meetings run to 2026-12-05) |
| `announcements` (NSE) | 2026-09-29 | 2026-11-05 | 21,307 | 2,430 (**536 not trading**) | 11-06 00:30 | `announcements_capture` (00:30 daily) | n/a (forward-only) |
| `shareholding` | filed 2026-10-01 | 2026-11-04 | 2,502 | 2,479 (**554 not trading**) | 11-05 18:05 | `shareholding_poll` (18:05 daily) | n/a (current quarter only, by the source's design) |
| `pit_fundamentals` (XBRL) | filed 2018-05-21 | filed 2026-11-05 | 1,394,861 | 2,362 | 11-06 02:20 | `fundamentals_forward` (02:00 daily) | n/a; 106,291 filings; period ends 2017-03-31 → 2026-09-30 |
| `macro_series` | 2012-10-01 | 2026-11-05 | 1,017,305 | — | 11-05 20:35 | `index_close_evening` (20:35, 21:10, 21:45 Mon–Fri), `fbil_reference_rates` (16:00 Mon–Fri), `macro_release_capture` (Sun 10:00) | see note |
| `news` | 2026-10-06 | 2026-11-06 | 27,989 | — | 11-06 12:15 | `news_capture` (00:15, 06:15, 12:15, 18:15) | n/a (forward-only) |

`macro_series` by source (series / rows / last period):
- index valuations: 611 / 1,007,394 / 2026-11-05
- FBIL: 7 / 8,457 / 2026-10-29
- India VIX: 4 / 152, **from 2026-09-10 only**
- WPI: 5 / 213 / 2026-09
- GST: 2 / 60 / 2026-09
- RBI rates: 7 / 49
- World Bank: 8 / 980 / 2025

### 2.2 L2 (derived) and Postgres stores

| Dataset | First | Last | Rows | ISINs | Last written | Refresh | Behind L1 |
|---|---|---|---|---|---|---|---|
| `prices_adjusted` | 2006-01-02 | **2026-10-30** | 7,474,994 | 3,549 | 11-01 21:49 | `ca_refresh` drain (Sat 10:00), `bse_ca_sweep` (1st Sun 06:00) | **4 sessions** (11-02 to 11-05). 1,933 ISINs end on 10-30. 11 NSE EQ/BE ISINs whose first bar is on or after 10-28 have no series yet. |
| `fo_aggregates` | 2024-07-08 | 2026-11-05 | 123,332 | 276 | 11-05 20:00 | `nse_daily_capture` | 0 |
| `share_basis/state.json` | — | — | 52 quarantine windows on 47 ISINs; 13 admitted stated splits | — | 11-05 19:40 | `share_basis_guard` (19:40 Mon–Fri, Sat 11:30) | current |
| `corporate_actions` (PG) | ex 2000-06-30 | ex **2026-10-30** (NSE), **2026-09-30** (BSE) | 50,447 (NSE 14,597, BSE 35,850); 166 unreconciled | 3,645 | 11-01 18:19 | `ca_refresh` (Sat 10:00); BSE twin paused (D29) | weekly |
| `adjustment_factors` (PG) | ex 2000-08-02 | ex 2026-10-23 | 2,819 (422 structural breaks) | 1,839 | 11-01 21:49 | with the L2 rebuild | weekly |
| `thin_trading_label` (PG) | — | as of 2026-11-05 | 18,620 | 6,210 labelled | 11-05 20:15 | `thin_trading_label` (20:15 Mon–Fri) | current |
| `security_master` / `isin_lineage` (PG) | — | — | 8,715 / 640 links | — | Sat 10-31 | `identity_refresh` (Sat 07:00) | weekly |

The `l2_invalidation` queue is empty: 24,717 requests, all resolved, the last on 2026-11-01 21:49.

### 2.3 L0 (immutable fetched files), by source

| Source | First logical date | Last | Payload files | Last written | Job |
|---|---|---|---|---|---|
| `nse_bhavcopy_legacy` / `nse_bhavcopy_udiff` | 2006-01-02 / 2024-07-08 | 2024-07-05 / 2026-11-05 | 4,588 / 579 | 10-05 / 11-05 18:30 | `eod_pipeline` |
| `nse_sec_bhavdata_full` / `nse_mto` (delivery) | 2019-10 / 2011-06-22 | 2026-11-05 / 2019-09-30 | 1,762 / 2,048 | 11-05 18:30 / 09-29 | `eod_pipeline` / unscheduled (superseded) |
| `bse_bhavcopy_legacy` / `bse_bhavcopy_udiff` | 2006-01-02 / 2024-07-08 | 2024-07-05 / 2026-11-05 | 4,588 / 579 | 10-06 / 11-05 18:30 | `eod_pipeline` |
| `nse_pr_bundle` | 2010-01-04 | 2026-11-04 | 4,178 | 11-05 18:30 | `eod_pipeline` |
| `nse_fo_bhavcopy` | 2024-07-08 | 2026-11-05 | 579 | 11-05 20:00 | `nse_daily_capture` |
| `nse_xbrl_filing` | 2018-05-21 | 2026-11-05 | 84,959 | 11-06 02:20 | `fundamentals_forward` |
| `nse_integrated_filing_index` / `nse_financial_results_index` | 2025-03-01 / 2016-04-01 | 2026-11-05 / 2026-07-01 | 288 / 92 | 11-06 02:20 / 09-05 | `fundamentals_forward` / unscheduled (feed retired after Dec-2024 quarters) |
| `nse_corp_actions` / `bse_corp_actions` | 2016-09-01 / 2016-09-01 | 2026-11-01 / **2026-09-06** | 17 / 6,689 | 11-01 / 09-07 | `ca_refresh` / `bse_ca_sweep` (BSE paused, D29) |
| `nifty_index_press_releases` | 1998-09-02 | 2026-10-31 (listing) | 428 | 10-31 09:00 | `index_press_refresh` |
| `nifty_index_constituents` | 2026-09-03 | 2026-11-05 | 906 | 11-05 19:16 | `daily_snapshot`, `constituents_snapshot` |
| `nifty_tri_history` | 2026-09-08 | 2026-11-05 | 102 | 11-05 19:50 | `tri_evening`, `tri_refresh` |
| `nse_index_close_snapshot` | 2012-10-01 | 2026-11-05 | 3,478 | 11-05 20:35 | `index_close_evening` |
| `nifty_india_vix_history` | 2026-10-09 | 2026-11-05 | 23 | 11-05 20:35 | `index_close_evening` |
| `nse_board_meetings` | 2026-10-19 | 2026-11-05 | 23 | 11-05 20:00 | `nse_daily_capture` |
| `nse_bulk_deals` / `nse_block_deals` / `nse_fii_dii_flows` | 2026-10-06 | 2026-11-05 | 24 / 23 / 24 | 11-05 20:00 | `nse_daily_capture` |
| `nse_announcements` / `nse_announcement_attachment` | 2014-12-20 / 2014-12-15 | 2026-11-05 / 2026-07-07 | 69 / 25 | 11-06 00:30 / 10-06 | `announcements_capture` / unscheduled (on demand) |
| `nse_shareholding_pattern` | 2026-10-06 | 2026-11-05 | 31 | 11-05 18:05 | `shareholding_poll` |
| `nse_asm_list`, `nse_esm_list`, `nse_gsm_list`, `nse_price_bands`, `nse_industry_classification` | 2026-09-08 | 2026-11-05 | 40 each | 11-05 19:15 | `daily_snapshot` |
| `nse_equity_list`, `nse_symbol_changes` | 2026-09-08 | 2026-11-05 | 44 each | 11-05 19:15 | `daily_snapshot`, `identity_refresh` |
| `bse_scrip_master` | 2026-09-06 | **2026-09-23** | 14 | 09-23 | `daily_snapshot` (paused, D29) |
| `curated_rss` / `gdelt_v2_event_files` | 2026-10-06 | 2026-11-06 | 123 / 245 | 11-06 12:15 | `news_capture` |
| `fbil_reference_rates` | 2019-07-09 | 2026-11-05 | 31 | 11-05 16:00 | `fbil_reference_rates` |
| `rbi_current_rates`, `oea_wpi_monthly_index`, `gstn_tax_collection`, `worldbank_indicator_api` | 2026-10-06 | 2026-11-01 | 7 / 12 / 7 / 56 | 11-01 10:00 | `macro_release_capture` |

L0 holds 11 GB in 233,770 files including sidecars. The local sha256 manifest (`l0_backup`, 05:45
daily, last run 11-06 05:45) has 233,758 lines.

### 2.4 L0 S3 mirror (state only)

| | |
|---|---|
| Last `ops/l0-s3-sync.sh sync` + `verify` | **2026-10-08 16:32–16:33 IST**: 224,302 local = 224,302 remote, 0 size mismatches, 0 of 200 sampled sha256 mismatches, VERIFIED |
| Since then | 4,734 L0 payload files (about 9.5k with sidecars) written after 10-08 are **not mirrored** |
| Schedule | none. No cron or job runs the sync, and `l0_backup`'s `BACKUP_L0_MIRROR` is unset, so `l0_backup` extends the local manifest but writes no second copy |
| Key | the SSE-C key stays on this host only (not read for this report) |

---

## 3. Index membership, especially NIFTY 500

**What the lake holds.** There are three write-once builds of `index_membership_history`. Readers
(`read_membership_history`, `members_asof`, and the backtest engine's `_InvestableUniverse`) take
the **newest** one.

| Build | NIFTY 50 / Next 50 / 100 / 200 | NIFTY 500 | Midcap 150 | Smallcap 250 |
|---|---|---|---|---|
| `date=2026-10-05` (DQ-5) | from 2021-10-22 | from 2021-10-22 | from 2021-10-22 | from 2021-10-22 |
| `date=2026-10-06` (DQ-5.1) | from 2016-04-01 (NIFTY 50) / 2016-10-24 | from **2016-10-24** | from 2016-10-24 | from 2016-09-30 |
| **`date=2026-10-10` (M14.1, read today)** | **from 2014-03-28** | **from 2015-03-28** | **from 2016-08-12** | **from 2016-04-22** |

Newest build, per index:

| Index | Intervals | ISINs ever | Members on 2016-09-01 / 2016-10-24 / 2026-11-05 | Agrees with 2026-11-05 snapshot |
|---|---|---|---|---|
| NIFTY 500 | 1,309 | 1,074 | 500 / 500 / 500 | yes, 500 = 500 |
| NIFTY 50 | 108 | 105 | 51 / 51 / 50 | yes |
| NIFTY Next 50 | 216 | 165 | 50 / 50 / 50 | yes |
| NIFTY 100 / 200 | 268 / 544 | 221 / 450 | 101, 201 in 2016; 100, 200 today | yes |
| Midcap 150 | 489 | 393 | 150 / 150 / 150 | yes |
| Smallcap 250 | 986 | 761 | 250 / 250 / 250 | yes |

Some indices read one name over size in the early years of the history:
- NIFTY 50 reads 51 names on 2016-09-01 and 2016-10-24, and 50 by 2018-01-01.
- NIFTY 100 and NIFTY 200 read 101 and 201 from 2016 through at least 2018-01-01, and 100 and 200
  by 2021-01-01.

This was not investigated further. NIFTY 500 reads exactly 500 on every probe date.

**Does the M14.1 build exist, and was it published?** Yes, on both counts.
- TASK_GRAPH `M14.1` gives the result as "NIFTY 500 from 2015-03-28".
- `ops/gates/pit-membership-detached-2026-10-07.md` §5 named the post-merge publish:
  `index_history_backfill --build-only --as-of 2026-10-10 --write-l1`, after Saturday's
  `index_press_refresh`.
- `M16-strategy-exploration-report.md` records that "Saturday's `index_press_refresh` (2026-10-10
  09:49 IST) published a build covering from 2015-03-28". M16 then ran NIFTY 500 on all four
  windows from 2016-09-01, and the engine answered on all 120 rebalance dates.
- The lake confirms it: `date=2026-10-10/nifty500.parquet` was written at 09:49 IST, and its
  `coverage_start` is 2015-03-28.
- `members_asof('nifty500', 2016-09-01)` returns 500 names. A date before 2015-03-28 returns `None`,
  so the engine refuses it.

`ops/gates/M14.5-regime-reentry-report.md` (written 2026-10-08) said "M14.1 verified the build but
did not write the lake". That was true for two days.

**Is a fetch needed?** No. **Is a publish needed?** No.
- After 2026-10-10, L0 received three NSE Indices releases: `ind_prs12102026`, `ind_prs23102026_1`
  and `ind_prs28102026`.
- They cover Microcap 250, Total Market, Smallcap 500, India Internet, Nifty50/500 Shariah, and
  Sugar & Ethanol. **None is a tracked index.**
- So the 2026-10-10 build is still current. Its open memberships equal the 2026-11-05 constituents
  file for all seven indices, with no difference in either direction.

**What would gate a future rebuild.** Nothing schedules one. `index_press_refresh` lands releases in
L0 and never rebuilds L1. The next change to a tracked index will need the same manual,
request-free `--build-only --as-of <date> --write-l1` run that M14.1 used. That is an agent task
under existing authority, not a new owner decision. The next semi-annual review is announced around
February, effective late March.

**What this unblocks.** A NIFTY 500 walk-forward whose selection window opens on 2016-09-01 runs
today. PR #168's pre-registration (§1, §3) assumes "the PIT NIFTY 500 membership history opens
2016-10-24", so it treats NIFTY 500 as attribution-only on the six-year and verification windows.
The premise is out of date.

**Owner decision:** whether to amend the pre-registration, before any further result is read, to add
NIFTY 500 to the selection and decade units. No data work is needed either way.

---

## 4. Quality

### 4.1 Status API at 15:08–15:23 IST

| Surface | Result |
|---|---|
| `/health` | OK; DB reachable; scheduler heartbeat 23 s old |
| `/status/jobs` | 26/26 OK; 0 errors, 0 accepted. There were 18 FAILED runs in the last 14 days, and every job's latest run succeeded: `announcements_capture` 6 (BSE leg), `eod_pipeline` 4 (`nse_delivery` late, 10-26..10-29), `nse_daily_capture` 3, `macro_release_capture` 2, and `identity_refresh`, `ca_refresh` and `constituents_snapshot` 1 each (10-24) |
| `/status/sources` | 32 sources. **29 healthy**, **3 unhealthy-and-accepted**: `bse_announcements` (streak 30), `bse_corp_actions` (12) and `bse_scrip_master` (35), each **paused under D29** with deviation `bse-403-source-*` (D26). 0 unaccepted errors. Declined: `screener_company_fundamentals` (D12/D19) |
| `/status/sync?date=2026-11-05` | 544 rows: 542 PUBLISHED, 2 FAILED (two XBRL filings of 11-05, amber under D28). `nse_bhavcopy`, `nse_delivery` and `bse_bhavcopy` are **green** |
| `/status/quality` | 2,796 open flags: **53 ERROR** (all 53 accepted), 241 WARN, 2,502 INFO (table below) |
| `/status/quarantine` | 0 rows in the last 90 days. 521,252 rows in total, all dated ≤ 2021-02-16 |
| `/status/thin-trading` | as of 2026-11-05: 6,210 labelled, 1,088 thin, **0 of them NIFTY 500 members** |
| `/status/gaps` | 1,456 unaccepted and 1,493 accepted, counted from each source's own era start (§4.2) |

**Open quality flags**

| Check | Severity | Open | Dates | Covered by |
|---|---|---|---|---|
| `l2_continuity` (`share_basis_step`) | ERROR | 47 | 2011-08-25 → 2026-08-17 | **D33** `d33-share-basis-history-continuity` (47/47) |
| `share_basis_guard` (3 `reissue_face_value_disagrees`, 2 `stated_terms_disagree`) | ERROR | 5 | 2021-08-24 → 2025-11-21 | **D33** `d33-share-basis-history-guard` (5/5) |
| `ca_reconciliation` | ERROR | 1 | 2017-09-06 | **D27** `d27-ca-reconciliation-backlog` |
| `ca_reconciliation` | WARN | 164 | 2001-10-03 → 2026-10-16 | warning; no acceptance needed |
| `shareholding_no_isin` / `shareholding_bad_isin` | WARN | 72 / 2 | 2026-10-05 → 10-21 | warning |
| `l2_continuity` (`unsourced_action_step`) | WARN | 3 | 2006-02-13 → 2008-09-08 | warning (D22 family) |
| `shareholding_bc3` | INFO | 2,502 | 2026-10-01 → 11-04 | information |

### 4.2 Gaps: what `/status/gaps` cannot yet explain

Each source was counted from its own first `sync_state` date to 2026-11-06. ("Unexplained" in the
API includes the accepted rows; the table splits them.)

| Source | Unaccepted | Accepted | What it is |
|---|---|---|---|
| `nse_xbrl_filing` | **1,213** | 1,427 (D28 2, D31 28, D32 24, D34 8, D35 67, D36 13, D37 69, D38 4, D39 1,212) | §4.3 |
| `nifty_tri_history` | 75 | 0 | Rows for the `nifty500` unit, 2026-09-08..10-08, before that unit existed. The TRI series itself has 0 missing sessions. Bookkeeping |
| `bse_bhavcopy_legacy` | 66 | 0 | 65 sessions in 2006–2010 where BSE served its HTML soft-404 (L0 present, L1 absent), plus 1 era-boundary artefact (2024-07-08) |
| `nifty_index_constituents` | 41 | 0 | 22 FAILED on Sun 10-25 (niftyindices.com 404/503, healed next run). 19 `niftyprivatebank` files 09-03..10-05 that were markup under the old file name |
| `gdelt_v2_event_files` | 24 | 0 | 23 day-level "never attempted" rows, although the source keys rows by 15-minute export and `news` has every day. 1 FAILED 404 on 10-20, past D28's 7-day window |
| `bse_scrip_master` | 11 | 24 (D26) | 2026-09-08..09-23 soft block (empty array; L0 present, L1 absent). Only the `source` kind accepts it, not the `gap` kind |
| `nse_index_close_snapshot` | 12 | 0 | The archive returns 404 for 12 dates, 2013-10-09..2016-06-20 |
| `nse_integrated_filing_index` | 6 | 0 | 5xx on 2026-10-10; later windows published |
| `curated_rss` | 5 | 0 | `rbi_press_releases` slot, 10-06..10-21, never attempted |
| `nse_block_deals`, `nse_delivery`, `nse_financial_results_index` | 1 each | 0 | 10-05, before capture began; the 2021-11-04 Muhurat file; the 2026-04-01 empty array of the retired feed |
| `bse_announcements`, `bse_corp_actions` | 0 | 30, 12 (D26/D29) | the BSE 403 |

**Price data has no unexplained gap.** NSE `prices_raw` misses 0 of 5,131 sessions, and delivery
misses 0 of 3,782.

### 4.3 XBRL fundamentals: the 2,640 FAILED filings, recounted

- **1,427 are accepted**, by D28 (still retrying), D31, D32, D34, D35, D36, D37, D38 and D39.
- **1,213 are not.**

The classes below are assigned by error shape, matched to the taxonomy of the 2026-11-02 triage.

| Class | Count now | 2026-11-02 triage | Shape | What would clear it |
|---|---|---|---|---|
| `MissingPayloadError` | **710** (400 Standalone, 310 Consolidated) | 710 | The 2026-09-06 rebuild-from-L0 found no L0 payload. Filings span 2018–2025 (2018: 83, 2019: 292, 2020: 78, 2021: 73, 2022: 43, 2023: 81, 2024: 57, 2025: 3) | a re-fetch of the documents (owner-gated), or acceptance |
| A: Q4 labelled as half-year, failed the strict check | **166** | 287 (121 since relabelled) | IF entry Jan–Mar, the document offers an Oct→Mar half and an Apr→Mar year | owner: keep refused, or accept |
| M: old half-year | **117** | 117 | a Jul–Sep entry offered Apr→Sep (pre-2024) | owner |
| K/L not covered by D39 (no sibling published) | **104** (K 91, L 13) | 104 | stale period or annual-only document, pre-2021/2024 | owner |
| F2: index symbol ≠ document symbol | **89** | 89 | SEPC/SHRIRAMEPC 34, ZFCVINDIA/WABCOINDIA 21, PATANJALI/RUCHI 13, KEEPLEARN/DALALSTCOM 10, RBA/BURGERKING 5, ESSENTIA/INTEGRA 5, MOTHERSON/'' 1 | owner: accept as renames through D2, or keep refused |
| G: document ISIN of a different issuer | **13** | 14 (INDBANK since accepted) | | keep refused (owner ruling of 11-02) |
| C: date typo, including ICICIPRULI `-FY` | **7** | 6 + 1 | | owner |
| H: entity-identifier scheme | **6** | 6 | | owner |
| other: multi-entity filing (PANACHE) | 1 | — | | owner |
| E, F1 | **0** | 182, 23 | cleared by the identity re-queue (PR #163) | — |

By filing year, the unaccepted rows are 2018: 220, 2019: 326, 2020: 137, 2021: 131, 2022: 59, 2023: 87,
2024: 57, 2025: 127 and 2026: 69. The current feed is healthy: `nse_xbrl_filing` has failure streak
0, and only 2 filings of 11-05 are failing, both amber under D28.

### 4.4 Price quarantine

There are 521,252 quarantined rows, all from 2021-02-16 or earlier.
- 239,524 are BSE `scrip_unresolved` (2006-03..2016-08).
- 281,528 are pre-ISIN NSE rows the E1 resolver did not admit (`isin_column_absent:*`, 2006–2011).
- The rest: 171 `symbol_unresolved`, 18 `prev_close_absent`, 6 `no_matching_price` and 5
  `isin_not_published`.

The only step-change alerts are 2019-06-17 and 06-18: 64 and 65 `symbol_unresolved` rows against a
median of 1. Nothing was quarantined in the last 90 days.

### 4.5 Share-basis guard

State written 2026-11-05 19:40 IST. 52 quarantine windows on 47 ISINs: 47 `share_basis_step`, 3
`reissue_face_value_disagrees` and 2 `stated_terms_disagree`. Bars before the step are withheld from
decisions. 13 stated splits were admitted (`BC_STATED_TERMS`).

Three windows touch today's NIFTY 500:
- **PIRAMALFIN**: step 2025-11-21, two windows. Its bars before that date are withheld, so its
  12-month momentum is incomplete until about 2026-11-21.
- **HEXT**: 2025-02-19.
- **PATANJALI**: 2020-02-07.

All the windows' flags are displayed as accepted under D33. A flag dated 2026-11-01 or later would
show red; there are none.

### 4.6 Thin-trading label (information only, M13.8)

As of 2026-11-05, over a 250-session window:

| Measure | Count |
|---|---|
| Labelled | 6,210 |
| Thin | 1,088 (ratio < 0.80: 1,078; no-trade gap ≥ 20 sessions: 501) |
| Thin and NSE-listed | 296 |
| Trade-to-trade | 1,106, of which 473 are thin |
| Thin, by listing status | ACTIVE 605, SUSPENDED 234, DELISTED 19, UNKNOWN 230 |
| Insufficient history | 52 |
| Identity seams | 73 |
| Capture holes | 0 |
| **NIFTY 500 members labelled thin** | **0** |

---

## 5. Consumer readiness matrix

| Consumer | OK | Caveat | Blocked |
|---|---|---|---|
| **EOD platform** | All 26 jobs OK. 11-05 green for NSE prices, delivery and BSE prices. Every ERROR accepted. | 3 BSE sources paused (D29). XBRL history: 1,213 unaccepted rows are visible on `/status/gaps`. | — |
| **Backtests: `turnover_floor` universe** | Raw prices from 2006 (NSE ISIN era from 2011-06-22; pre-ISIN 2006–2011 promoted with quarantines). L2 from 2006. NIFTY 50/500 TRI with no missing session since 2006. All M12 windows (2016-09-01 → 2026-08-31, six-year 2019-07-01, verification 2021-09-01) are covered. | Midcap 150 / Smallcap 250 TRI end 2026-10-01; harmless for windows ending 2026-08-31. | — |
| **Backtests: `nifty500` universe** | PIT membership **from 2015-03-28**: decade, walk-forward selection (2016-09-01), six-year and verification all run. | Any window opening before 2015-03-28 is refused, by design. Other indices: 50/Next 50/100/200 from 2014-03-28; Midcap 150 from 2016-08-12; Smallcap 250 from 2016-04-22. | — |
| **Backtests: fundamentals arms** | PIT store from 2018-05, 106,291 filings, 2,362 ISINs | Quarterly coverage is about 1,500–1,574 ISINs per quarter before 2025-03, against 2,005–2,288 after. **108 NIFTY 500 names listed before 2024 have no PIT quarter before 2025** (stale-ISIN skip, §6.2). 1,213 unaccepted XBRL failures. Results feeding a fundamentals arm before 2025 are biased toward the names that kept their ISIN. | — |
| **Backtests: other inputs** | Delivery from 2011-06-22. F&O from 2024-07-08. CA broadcasts from 2010. | Deals, flows, announcements, news, shareholding and board meetings are forward-only (2026-03 to 2026-10). No backtest can use them. | — |
| **Momentum v2 paper book** (`momentum_v2_paper_2026_09_06`) | The 11-05 session COMPLETED, recon CLEAN, kill switch not tripped. Its NIFTY 500 PIT universe equals today's list. 0 members thin. NIFTY 50 TRI regime input current to 11-05. | L2 ends 10-30 and the reader lays it over raw closes, so **CONCORDBIO's 1:1 bonus (ex 11-04, raw 1,227.90 → 655.50) reads as a raw step until the Sat 11-07 rebuild**. 3 REIT members (EMBASSY, BIRET, BAGMANE) have L1 bars but no L2 series. Whether the signal then prices them raw or drops them was not checked. PIRAMALFIN's pre-2025-11-21 bars are withheld (D33). | — |
| **M17 dossiers** | The run of 11-05 completed. Prices, delivery (0 nulls), F&O block for the 217 NIFTY 500 names with F&O, CA broadcasts and deals are keyed correctly. | **Board-meeting fields** (`next_results_date`, `last_results_date` and the results-day fields that hang off them) are empty for **134 of 500** NIFTY 500 names: 129 under an older ISIN plus 5 with no rows. **Announcements of the last 20 sessions** are empty for **129** for the same reason. The F&O block is `None` for the 283 names without F&O, by design. Universe sheet on 11-05 (1,619 names): `revenue_ttm_growth` null 829 (top 100: 51), `pat_ttm_growth` 910 (55), `roe` 460 (22), `pe_ttm` 440 (22), `pe_vs_sector` 966, `sector` 877 (8), `return_52w` 232 (16, mostly recent listings and demergers). Earnings surprise null for 11 of 40 shortlisted on 11-05. The Sep-2026 quarter has been filed for 410 of 500 so far; the results season runs to 11-14. | — |
| **Console** | `/console` answers 200 and reads the same status API. Shows the D26–D39 acceptances as amber. | It shows only what the API knows, so it does not flag the stale-ISIN gap (§6.1). | — |

---

## 6. Open gaps and the owner decision each needs

Nothing here creates a new D-number. Where an existing decision covers a gap, it is cited.

1. **Re-key the stale-ISIN feeds** (board meetings, announcements, shareholding).
   - **Evidence:**
     - NSE states `sm_isin` and the announcement ISIN as the company's original ISIN, and the lake
       stores it verbatim.
     - For 129 NIFTY 500 names the only rows are under an older ISIN. Examples:
       HDFCBANK `INE040A01018` → `INE040A01034`; ASIANPAINT `INE021A01018` → `INE021A01026`.
     - `isin_lineage` already holds the links, for example `INE040A01018 → INE040A01026`
       (2011-07-15) and `INE021A01018 → INE021A01026` (2013-07-31).
     - `results_dates()` in `analyst/commons/volume_quality.py` matches the exact ISIN, so those
       dossier fields are `None`.
   - **Work:** resolve these feeds through the D2 lineage at L1 write or at read. No fetch.
   - **Owner decision:**
     - This changes what the M17 managers see in the middle of the forward test.
     - Approved work is being held until after S0 (Fri 11-13).
     - Fix it before S0, after S0, or as a pre-registered amendment?
2. **Pre-2025 fundamentals for names whose ISIN changed.**
   - **Evidence:**
     - The old results index names HDFCBANK's Sep-2024 filing under `INE040A01018`.
     - That ISIN has no `prices_raw` row in the backfill window, so the filing fell outside the
       backfill universe.
     - The filing has no `sync_state` row and no L0 payload.
     - 108 NIFTY 500 names listed before 2024 have PIT quarters only from 2025. This is also why
       TTM growth is null for about half the top 100.
   - **Work:** map index ISINs through the lineage, then fetch the skipped XBRL documents. Size it
     first with a request-free dry run.
   - **Owner decision:** authorise that targeted fetch. Bulk fetch is reserved to the owner (B1).
3. **The 710 `MissingPayloadError` filings** (2018–2025, from the 2026-09-06 rebuild).
   - **Owner decision:** authorise a one-time re-fetch, like the D34 re-check, or accept them.
4. **The other 503 unaccepted XBRL failures:** A 166, M 117, K/L 104, F2 89, G 13, C 7, H 6,
   other 1.
   - **Owner decision:** rule on each class. Accept it, as D35–D39 did for others, or keep it red.
     F2 (company renames such as RUCHI → PATANJALI and WABCOINDIA → ZFCVINDIA) is the class most
     likely to hold real, recoverable results.
5. **The L0 S3 mirror is stale since 2026-10-08** (4,734 payload files not mirrored, no schedule).
   - **Owner decision:**
     - Re-run `ops/l0-s3-sync.sh sync` and `verify`; it writes to AWS under the SSE-C key.
     - Then either schedule it, or set `BACKUP_L0_MIRROR` so the nightly `l0_backup` keeps a
       second copy.
6. **PR #168's pre-registration premise is out of date.** NIFTY 500 PIT membership opens on
   2015-03-28 (§3).
   - **Owner decision:** amend it to run NIFTY 500 on the selection and decade units, or keep
     NIFTY 500 attribution-only as registered. No data work either way.
7. **REITs in NIFTY 500 have no L2 series** (EMBASSY, BIRET, BAGMANE).
   - **Owner decision:** carry REIT/InvIT series in L2, or exclude them explicitly from the
     momentum universe. Today they have no adjusted close, and how each reader treats them was not
     checked.
8. **Unscheduled secondary series.** These are agent tasks under existing authority unless the owner
   objects.
   - Midcap 150 / Smallcap 250 TRI: stale since 2026-10-01. Add them to `tri_evening` or leave them
     opt-in.
   - `price_session_attributes`: stale since 2026-10-05, one-shot.
   - The index-membership rebuild after a tracked-index release: manual.
9. **Bookkeeping gaps (243 outside XBRL, §4.2).**
   - The real losses are 65 BSE soft-404 sessions (2006–2010), 12 index-close archive 404s
     (2013–2016) and 1 GDELT 404 (10-20).
   - The rest are unit-keying or era artefacts.
   - **Owner decision:** accept the real losses as permanent in the D31/D34 manner. Fixing the
     artefacts in the gap checker needs no decision.
10. **BSE stays paused under D26 and D29.**
    - BSE-only names have no corporate actions after ex-date 2026-09-30, and no BSE announcements or
      scrip master since 2026-09-24.
    - No decision is needed until the 403 lifts.
11. **L2 lag, CONCORDBIO bonus.** Self-heals at `ca_refresh`, Sat 2026-11-07 10:00 IST. Watch
    `/status/quality` for a new `l2_continuity` flag after the drain. No decision needed.

---

### Provenance

- Status API: `/health`, `/status/jobs`, `/status/sources`, `/status/sync?date=2026-11-05`,
  `/status/quality`, `/status/quarantine` (2000-01-01..2026-11-06), `/status/thin-trading`, and
  `/status/gaps` per source from each source's first `sync_state` date, with `nse_xbrl_filing` in
  15 date slices so that every row was listed.
- Lake: parquet footers for row counts. Single-thread DuckDB, `nice -n 19`, for ISIN counts,
  per-session presence and the NIFTY 500 joins. L0 `.meta.json` sidecars for logical dates.
- Postgres: one read-only session (`default_transaction_read_only=on`), `SELECT` only.
- Calendar: `dataplatform/ingest/data/nse_holidays.yaml` through 2026-11-05.
- Not done: no request to any data host, no job trigger, no write to the lake or the database. The
  S3 state is taken from the last sync/verify log; S3 was not contacted and the key was not read.
