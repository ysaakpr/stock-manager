# M10.4: what the fundamentals store is still owed (2026-10-06)

**Authority:** owner go 2026-10-06 (chat). **Status: RUN, 2026-10-06 20:51 → 20:57 IST. The
owed integrated-feed fetch is done (§ "Result" at the end).** It was held back during the day because
the PIT write would have landed while the backtest chain at `a4a4003` (`cap_tier_campaign`, which
reads `pit_fundamentals`) was still running. A store that changes under a running measurement makes
that measurement unreproducible. Below, the "lake as it stands" and "what is owed" sections are the
state *before* the run.

## The lake as it stands (`sync_state`, primary lake)

| Source | State | Rows | Span |
|---|---|---|---|
| `nse_financial_results_index` | PUBLISHED | 90 chunks | 2016-04-01 → 2026-07-01 |
| `nse_financial_results_index` | FAILED (retryable) | 1 | `Annual/2026-06-30`: empty JSON array. The results feed stopped taking new periods after Dec-2024, so this is expected. |
| `nse_integrated_filing_index` | PUBLISHED | 114 pages | 2025-03 → the `2026-09-06` partial month |
| `nse_xbrl_filing` | PUBLISHED | 102,650 | filing dates 2018-05-21 → 2026-09-05 |
| `nse_xbrl_filing` | FAILED (retryable) | 2,956 | see below |

## What is owed

1. **The integrated feed from 2026-09-06 on.** The last sweep ended mid-September (its last chunk is
   keyed `2026-09-06`), so the rest of September and October 2026 have never been discovered. That
   is 12 index pages on `www.nseindia.com` (6 for September, 6 for 2026-10-01..05), plus one XBRL
   document from `nsearchives` for each new in-universe filing. The busy filing season for the
   September quarter starts mid-October, so expect tens to a few hundred documents, not thousands.
   Filings already PUBLISHED are skipped, and documents already in L0 are re-parsed without a
   request.
2. **Nothing in the results feed.** It is complete up to the point where the feed stopped.
3. **The 2,956 failed filings mostly need no fetch.** By cause:
   * 1,197 + 302 + 236 + 72 entries are "the index entry's period is not a column in the document".
     These are correct refusals: the parser will not store a yearly document as a quarter.
   * About 400 are identity mismatches (the document's symbol or ISIN disagrees with the index, or
     the filing names more than one entity). These are D2 or filer issues, and the bytes are in L0.
   * **710 `MissingPayloadError` and 51 HTTP 404.** The archive answered 404 for these documents
     during the original campaign. The earlier gate already classed them as a source gap
     (`M10-fundamentals-backfill-live.md` §6b). Asking for all of them again would cost about 760
     requests for documents the archive said it does not have. That is not owed. If anyone wants
     the question reopened, a 10-document sample should answer it.

## The command (run once the chain is done, outside 18:00–20:30 IST)

```bash
DATA_ROOT=/home/ubuntu/stock-manager/data nohup uv run python -m dataplatform.ingest.fundamentals_backfill \
  --feed integrated --from 2026-09-01 --to 2026-10-05 \
  --report ~/campaign/fundamentals-integrated-2026-10-06.md \
  > ~/campaign/fundamentals-integrated-2026-10-06.log 2>&1 &
```

Planned with `--dry-run`: 12 index chunks. The runner now holds the leases for both
`www.nseindia.com` and `nsearchives.nseindia.com` while it fetches. If either lease is taken (for
example by a daily job on the site host), it exits 4 and names the holder. It has no
`--stop-before`, so send SIGINT before 18:00 IST if it is still running. It stops after the current
unit and resumes per filing.

## Result (2026-10-06, all times IST)

**Preconditions, checked just before the start:**

- No `fold_campaign` or `cap_tier_campaign` process was running.
- No other backfill or campaign driver was running, and no host lease was held.
- No `job_run` row was RUNNING.
- The run was outside 18:00–20:30. It started after the 20:00 `nse_daily_capture` had SUCCEEDED and
  after M11.2's archive run (20:30–20:43) had released `nsearchives`, so the two campaigns never
  overlapped.

It ran the command above unchanged, from the main checkout at `22b616a`, **20:51:01 → 20:57:16**.

| | |
|---|---|
| Index chunks | **12 / 12 published**, 0 failed. The new units are keyed `2026-09-30/p01..06` and `2026-10-05/p01..06`, so they do not collide with the earlier `2026-09-06/pNN`. |
| Filings discovered (all in universe) | 246 |
| Filings published | **198**. Another 48 were already PUBLISHED (the 09-01..09-05 overlap). 59 payloads were reused from L0. |
| Filings failed | **0** |
| Facts written | **2,468** over **119** ISINs |
| Share counts refused (EPS contradicts paid-up capital) | 17 |
| Symbols D2 could not resolve | 90 records, 36 distinct symbols (listed in the report: recent listings and renames, D2 territory) |
| Requests | 151 (12 index pages + 139 XBRL documents). Not parked. |
| 403 | One, on the `www.nseindia.com/` homepage warm-up handshake (`fetch.handshake_forbidden`, the known cookie-warm behaviour). Every API and archive request after it answered 200. |

**Store after the run:**

- `nse_xbrl_filing` PUBLISHED: 102,650 → **102,848**, latest filing date **2026-10-05**.
- The 2,956 FAILED rows are unchanged, as planned (see "What is owed" §3).
- `nse_integrated_filing_index`: 126 pages PUBLISHED.
- `pit_fundamentals`: 1,352,030 facts over 2,291 ISINs, filing dates up to 2026-10-05.

**Logs:** `~/campaign/fundamentals-integrated-2026-10-06.log`; coverage report
`~/campaign/fundamentals-integrated-2026-10-06.md`.

**Next owed:** the September-quarter filing season, which starts mid-October. Re-run the same
command with `--from 2026-10-01 --to <date>`. Published units cost no request.

