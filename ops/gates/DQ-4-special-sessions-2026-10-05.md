# DQ-4 — weekend special sessions: calendar, fetch and evidence (2026-10-05)

Fix 4 of 5 from the 2026-10-05 data-quality audit. The calendar was "weekdays minus holidays, plus
Muhurat", so every non-holiday weekend session classified WEEKEND, no planner asked for it, and the
gap report could not flag it. `nse_holidays.yaml` now declares them under `special_sessions:`
(`DayKind.SPECIAL`, expects data); `expected_data_dates` — and so every planner and the gap
report — includes them.

## Which weekend dates are real sessions

Derived both ways, as the validated-derivation rule requires:

* **Index → archive.** Every weekend date in 2006-2026 carrying a NIFTY 50 TRI value: 16 (after
  setting aside the 20 Muhurat dates). The archive served a self-dated priced file for all 16.
* **Archive → index.** Every weekend date any L0 price source holds is either Muhurat or one of
  those 16. After the fetch below, all seven price sources reconcile against the new calendar with
  **0 missing and 0 unexpected**, both directions:

| source | span | expected = observed |
|---|---|---:|
| nse_bhavcopy_legacy | 2006-01-02..2024-07-05 | 4,588 |
| nse_bhavcopy_udiff | 2024-07-08..2026-09-01 | 535 |
| nse_pr_bundle | 2010-01-04..2026-09-01 | 4,135 |
| nse_mto | 2011-06-22..2019-09-30 | 2,048 |
| nse_sec_bhavdata_full | 2019-10-01..2026-09-01 | 1,718 |
| bse_bhavcopy_legacy | 2016-09-01..2024-07-05 | 1,943 |
| bse_bhavcopy_udiff | 2024-07-08..2026-09-01 | 535 |

Pre-2016 bhavcopies were checked for staleness: each is dated in its own TIMESTAMP column and its
closes differ from the prior session's on > 90% of common symbols (e.g. 2006-06-25, a Sunday:
876 rows, 849/875 closes changed vs 2006-06-23).

## The audit's "36 benchmark_tri dates with no calendar session"

All 36 are real sessions; none is a TRI data error. The count was taken against
`expected_sessions`, which deliberately excludes Muhurat:

* **20 Muhurat dates** (2006-2025) — already `expects_data` before this change; the TRI is right to
  carry them.
* **16 weekend sessions** — the set now declared. Including Sunday 2006-06-25 (NIFTY 50 3625.04),
  which the archive's own bhavcopy proves traded.

(The 9 pre-2006 weekend TRI dates in L1 lie outside calendar coverage and are not classified.)

## Fetch results (L0 only — no L1, L2 or sync_state written)

18 price files, one driver, host leases held, every outcome 200:

| date | nse bhavcopy | nse delivery (sec_bhavdata_full) | bse bhavcopy | pr_bundle |
|---|---|---|---|---|
| 2010-02-06 | in L0 | — (pre-MTO floor) | — (pre-lake) | fetched |
| 2012-01-07 .. 2015-02-28 (7) | in L0 | in L0 (MTO) | — (pre-lake) | fetched ×7 |
| 2006-04-29, 2006-06-25 | in L0 | — | — | — (pre-archive) |
| 2020-02-01 | fetched, 1,886 rows | fetched | fetched (legacy) | fetched |
| 2024-01-20 | fetched, 2,590 rows | fetched | fetched (legacy) | fetched |
| 2024-03-02 | fetched, 2,440 rows | fetched | fetched (legacy) | fetched |
| 2024-05-18 | fetched, 2,512 rows | fetched | fetched (legacy) | fetched |
| 2025-02-01 | fetched (UDiFF), 2,866 rows | fetched | fetched (UDiFF) | fetched |
| 2026-02-01 | fetched (UDiFF), 3,229 rows | fetched | fetched (UDiFF) | fetched |

No file was absent from any archive, so there is no 404 evidence to record. 33 requests in all
(18 + 14 bundles + 1 BSE re-probe below), no 403. The 14 bundle requests ran as 14 one-date
`pr_bundle_campaign acquire` invocations, each with its own rate limiter, so they went out ~1 s
apart rather than at the host's configured spacing. Next time, use one ranged run.

## BSE 2021-12-29

BSE **traded**. The L0 file `EQ291221_CSV.ZIP` (sha256 `e709f214…`) is genuine: its PREVCLOSE
equals 2021-12-28's CLOSE on 3,534/3,534 common scrips, and 2021-12-30's PREVCLOSE equals its
CLOSE on 3,534/3,534. `sync_state` holds `bse_bhavcopy_legacy|2021-12-29|FAILED`, with "row has 27
fields, header has 14": line 1773 is two records run together (`531358 CHOICE INT.` and
`531359 SHRIRAM ASSE`, a missing newline). That is the only malformed line of 3,738. A re-request
on 2026-10-05 got byte-identical content back (same sha256; the L0 put was a no-op), so the defect
is in the exchange's published file, not in our download. The calendar is right. The miss is a
parser-tolerance defect in the BSE legacy reader, which rejects the whole file. That fix is out of
DQ-4's scope and needs its own task.

## Rebuild command (for the single sequenced rebuild after all five fixes)

```
uv run python -m dataplatform.ingest.backfill --source nse_bhavcopy        --from 2020-02-01 --to 2026-02-01
uv run python -m dataplatform.ingest.backfill --source bse_bhavcopy_legacy --from 2020-02-01 --to 2024-05-18
uv run python -m dataplatform.ingest.backfill --source bse_bhavcopy        --from 2025-02-01 --to 2026-02-01
uv run python -m dataplatform.ingest.price_rebuild --from 2020-02-01 --to 2026-02-01
```

PUBLISHED pairs are skipped, and the files are already in L0. Note that `backfill.py` fetches
before it parses, and L0 put is idempotent on identical bytes, so expect one request per special
session per source. An L0-only ingest path such as the price-fetch-restart worker's `l0_acquire`
would avoid them. `price_rebuild.plan_sessions` now uses `expected_data_dates`, so the delivery
rebuild reaches the weekend sessions too.
