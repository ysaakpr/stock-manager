# Daily capture of the never-fetched VERIFIED sources — 2026-10-06 (ops-daily-capture)

Nine Source Register rows had a parser, a fixture and a live 200, and no byte in L0, because no
scheduled job called them (`ops/gates/macro-news-ingestion-plan-2026-09-07.md` §1). Four jobs now
cover them (`dataplatform/scheduler/registry.py`, driver `dataplatform/ingest/daily_capture.py`).
All nine rows are out of `UNSCHEDULED`; `tests/unit/test_scheduler_coverage.py` pins each job's
`covers` to its body's source tuple, gives the three perishable rows a weekday job and a
one-session lag budget, and fails if a capture job's fire window overlaps another job on a shared
host (a host lease is refused, not queued).

## Jobs (all times IST)

| Job | Cron | Budget | Hosts | Sources |
|---|---|---|---|---|
| `nse_daily_capture` | 20:00 and 23:00 Mon–Fri | 30 min | www.nseindia.com, nsearchives.nseindia.com | `nse_fii_dii_flows`, `nse_bulk_deals`, `nse_block_deals`, `nse_fo_bhavcopy` |
| `shareholding_poll` | 18:05 daily | 15 min | www.nseindia.com | `nse_shareholding_pattern` |
| `announcements_capture` | 00:30 daily | 45 min | www.nseindia.com, api.bseindia.com | `nse_announcements`, `bse_announcements` |
| `news_capture` | 00:15, 06:15, 12:15, 18:15 daily | 15 min | www.rbi.org.in, data.gdeltproject.org | `curated_rss`, `gdelt_v2_event_files` |

None overlaps `eod_pipeline` (18:30–19:15) or `daily_snapshot` (19:15–19:45) on a shared host.
Each host is leased separately inside a job, so a campaign holding one host fails only that host's
sources — each FAILED in `sync_state`, retryable, naming the lease holder — and the job raises at
the end so `/status/jobs` shows the run FAILED.

Rules every capture follows, and why the modules' own `ingest_*` functions are not called:

* **Latest-only payloads are filed under the capture instant** (`fiidiiTradeReact_captured_<ts>`,
  `bulk_captured_<ts>.csv`, `lastupdate_<ts>.txt`, `rbi_press_releases_<ts>.xml`) and published under
  the session the payload states. The old drivers named the file for the session they *asked for*
  (stale bytes under today's name, and a retry that can never succeed) or used an undated name
  (the second day of a month collides with the first).
* **Nothing intraday is published.** A latest-only payload stamped today before 19:30 is kept in L0,
  not published; a morning run owes yesterday's session (`owed_session`).
* **One L1 partition per date is re-derived from every payload of the date**, so NSE and BSE
  announcements, bulk and block deals, and RSS and GDELT never erase each other
  (`news.write_l1_merged` keeps a per-row `l0_key`).

## Per source

| Source | Cadence | Captured today (run-once against the primary lake, 13:44–15:45 IST) | Unrecoverable history |
|---|---|---|---|
| `nse_fii_dii_flows` | nightly 20:00, retry 23:00 | **2026-10-05** — 2 rows (FII, DII), L0 218 B, L1 `fii_dii_flows/date=2026-10-05`, PUBLISHED | Everything before 2026-10-05. The endpoint has no date parameter (M3.4 measured depth = 1 session). NSDL/CDSL monthly FPI series is the only route to the past (FPI-only, unprobed). |
| `nse_bulk_deals` | nightly 20:00, retry 23:00 | 13:44: archive host leased by the macro backfill → 2026-10-05 FAILED(HostBusyError). 15:45 retry, lease free: **2026-10-05** — 180 deals, 76 resolved to ISIN, **104 quarantined** (symbols the D2 master does not know — mostly SME names, not checked one by one), L1 `deals/date=2026-10-05`, PUBLISHED | Every session before 2026-10-05. Rolling current-session file, no archive. |
| `nse_block_deals` | as bulk | 15:45: the live quiet-day file is `NO RECORDS,,,,,,`, not header-only; the parser rejected it (fixed in this PR). The file has no date, and by 15:45 the 14:05 block window of 2026-10-06 had passed, so it cannot be attributed to 10-05: **2026-10-05 stays FAILED**, bytes kept in L0 | Every session before the first capture, including 2026-10-05. |
| `nse_fo_bhavcopy` | nightly 20:00, retry 23:00, current session only | 2026-10-05 was already PUBLISHED by the F&O backfill campaign by 15:45, so 0 requests from this job. No backfill from this job. | None: the archive file is dated and permanent; a missed night is a delay for the backfill to close. |
| `nse_shareholding_pattern` | daily 18:05 | **L0 captured** (2026-10-06, 30,615 B, 32 filings, all for quarter end 30-Sep-2026, broadcast 01..06-Oct). **L1 BLOCKED**: sync FAILED `ParseError: no 'pledgeShares_prcnt' field`. | The June-2026 quarter's list (2,284 filings in the register's 2026-08-08 sample) is gone: the master lists the *current* quarter-end only and rolled on 2026-10-01. The atlas notes a `from_date/to_date` form of this URL; it is not register-verified and was not probed. |
| `nse_announcements` | nightly 00:30, previous day, 7-day self-heal | **2026-09-29 .. 2026-10-05**, 7 days PUBLISHED: 958 / 1,217 / 805 / 116 / 168 / 23 / 480 rows (3,767 in all); L0 15 KB–861 KB per day; L1 `announcements/date=…` | None — date-parameterised; older days are a campaign's call, not this job's. |
| `bse_announcements` | as NSE, paged by `Table1.ROWCNT`, ≤100 pages | **BLOCKED**: `api.bseindia.com` answered **403** for 2026-09-29 and 2026-09-30, the third 403 tripped the fetcher's spike stop; all 7 days FAILED. Not retried, not worked around. | None if the block lifts within a week (the job re-drives the last 7 days); older days would need a separate decision. |
| `curated_rss` | 4×/day | RBI press releases: 10 items, 59,871 B, PUBLISHED as `curated_rss/rbi_press_releases-1344`; L1 `news/date=2026-10-06` | RBI items that rolled off the 10-item feed before today. |
| `gdelt_v2_event_files` | 4×/day, one slot per poll, export file only | slot `20261006081500` — 151 events, 55,883 B, MD5 cross-checked against its manifest, PUBLISHED; same L1 `news` partition | All of GDELT before today — deliberately (no backfill without a separate decision; plan §5). |

L1 written: `fii_dii_flows`, `announcements`, `news`, `deals`. `fo_contracts` for 2026-10-05 came from the backfill campaign, not from this job.
Nothing in `prices_raw` or L2 was touched; the F&O L2 `fo_aggregates` is derived and
is left to the L2 rebuild (`fo_aggregates.rebuild_l2_from_l1`), not written nightly.

## Decisions

* **Shareholding is daily, not weekly or quarterly.** The register said `cadence: quarterly`; the
  first real poll shows the master keeps only the current quarter-end's filings, so the previous
  quarter disappears on the first filing of the next — a weekly poll could miss the last week of
  late filings and revisions forever. Daily costs at most ~2.4 MB/day at the end of a filing season.
* **GDELT: export files only, four slots a day.** The export is the cheapest of the three slot
  files (~55–100 kB vs ~4 MB GKG) and already carries timestamp, link, actors and tone. GDELT is
  evidence-only and was measured near-empty of India-finance content (plan §3), so a six-hourly
  sample of global attention — ~0.3 MB/day, 1/24 of the 96-slot firehose (~30 GB if backfilled) — is
  what it is worth. Each slot is its own `sync_state` unit (`gdelt_v2_event_files/<slot>`).
* **RSS: only feeds that are `active` *and* whose register row is VERIFIED** — today that is RBI
  alone. Business Standard (403 WAF), ET Markets and PIB stay unfetched; no host was added.
* **A quiet deals day is `NO RECORDS,,,,,,`** on the live file, not a header-only one; `deals.parse` now reads both as zero deals.
* **Announcements capture the previous calendar day after midnight**, so each day is complete, and
  weekends are captured (filings happen then too). A BSE page with no rows is a quiet day only when
  the exchange was shut; on a session it is the register's empty-success gotcha and fails.

## Blocked

1. **`bse_announcements` — HTTP 403 from `api.bseindia.com`** (2026-10-06 13:45 IST, three
   consecutive, spike stop). Another agent's BSE campaign was running against `www.bseindia.com` at
   the time; whether the refusal is related is not known and was not tested. The job retries nightly
   (one spike-bounded attempt), which is waiting, not evasion.
2. **`nse_shareholding_pattern` L1 — the parser does not match the live payload.** It was built on a
   synthetic fixture (`fixture.frozen: false`) and requires `pledgeShares_prcnt`, `public_prcnt`,
   `fii_prcnt`, `dii_prcnt`; the live master has `pr_and_prgrp`, `public_val` and no pledge/FII/DII
   (those live in the per-filing XBRL the record links to). Making pledge optional would silently
   disable BC3, so it is a parser decision for its owner (M3.6), not a fix here. L0 is captured daily
   meanwhile and L1 can be derived from it with zero requests once the parser is right.

## Operations

The running scheduler (started by hand, `pgrep -f 'dataplatform.scheduler run'`) loaded its
registry at start; it must be restarted from the merged main to pick up the four jobs. Tonight's
20:00 session is the first perishable one owed after this PR — restart before 20:00, or run
`uv run python -m dataplatform.scheduler run-once nse_daily_capture` after 19:30 by hand.

Driver log of today's run-once calls: `~/campaign/daily-capture-2026-10-06.log`.
