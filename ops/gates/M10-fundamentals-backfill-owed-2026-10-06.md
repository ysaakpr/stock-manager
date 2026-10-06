# M10.4: what the fundamentals store is still owed (2026-10-06)

**Authority:** owner go 2026-10-06 (chat). **Status: NOT RUN today.** This note works out what is owed
and gives the exact command. The fetch was held back because the PIT write would land while the
backtest chain at `a4a4003` (`cap_tier_campaign`, which reads `pit_fundamentals`) was still running.
A store that changes under a running measurement makes that measurement unreproducible.

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
