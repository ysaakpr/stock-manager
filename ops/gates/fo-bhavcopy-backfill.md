# F&O bhavcopy history backfill (UDiFF era) — `nse_fo_bhavcopy`

**Date:** 2026-10-06 (IST) · **Authority:** owner go 2026-10-06 (chat) · **Status: COMPLETE** for
2024-07-08 → 2026-10-05. History only; the daily-forward F&O job is a separate driver.

## Result

| | |
|---|---|
| Sessions planned (C.2 expected-data dates) | 557 |
| Published | **557** — L1 `fo_contracts` + L2 `fo_aggregates`, one partition each per session |
| 404 / refused / failed | 0 / 0 / 0 |
| Contract rows (L1) | 20,599,837 |
| Per-underlier aggregates (L2) | 118,479 |
| Requests to `nsearchives.nseindia.com` | 557 F&O files + 2 legacy-format probes; no 403, no 5xx |
| Stock underlier-sessions with no ISIN | 332 — only `PEL` (267) and `IDFC` (65), both D2 history gaps (Piramal's demerger rename, IDFC's merger into IDFC First Bank); their aggregates carry `isin = null`, never a guessed ISIN |

Runner: `dataplatform/ingest/fo_backfill.py` (same shape as the M11.2 runner — `sync_state` per
session, commit per session after both partitions are on disk, L0 reuse, 404 closes a session, 403
spike parks with exit 3, host lease, `--stop-before`). Tests: `tests/unit/test_fo_backfill.py`.
`backtest/` reads neither dataset, so the running measurement chain was unaffected.

## A parser defect the first real file exposed

The M3.7 parser had only ever seen its **synthetic** fixture (`tests/fixtures/nse_fo/udiff/
PROVENANCE.md` says so), which wrote the legacy instrument codes `OPTSTK/FUTSTK/OPTIDX/FUTIDX`. The
real UDiFF files write `STO/STF/IDO/IDF` (2024-07-08: 26,431 / 542 / 7,402 / 15 rows). Every real
session was refused. The first five sessions were fetched, refused, and stopped; the parser now maps
both spellings to the same four instruments (any other code still stops), and those five were
re-derived **from L0 with no further request**. Regression test on four rows copied verbatim from the
real 2024-07-08 file. Sanity read of that session's NIFTY aggregate: PCR (OI) 1.184, near-future basis
+55.05 (0.23 %), spot 24,320.55 — plausible against the published market.

No `FUTIVX` (India VIX futures) row appears; the enum member stays and will refuse loudly if a new
code shows up for it.

## The pre-2024-07 legacy format (noted only, nothing registered)

It exists: one GET of
`https://nsearchives.nseindia.com/content/historical/DERIVATIVES/2024/JUL/fo05JUL2024bhav.csv.zip`
answered **200, `application/zip`, 539,677 B** (a preceding HEAD answered 503 — HEAD is not served).
Not stored, not registered, not parsed. The atlas (`ops/studies/evidence/india-acquisition-atlas.md`)
puts its depth at 2006-01-02, ~4,620 requests; that would be its own register row, parser and B1 go.

## State note

The five refused rows had been written `FAILED retryable=false` by the first build (which treated a
parse refusal as final). They were re-opened with one scoped `UPDATE sync_state SET retryable = true`
(this source, FAILED, error text `FinInstrmTp is 'STO'`); the runner now writes refusals retryable.

Log: `~/campaign/fo-backfill-2026-10-06.log`.

```bash
uv run pytest tests/unit/test_fo_backfill.py tests/unit/test_fo.py -q
```
