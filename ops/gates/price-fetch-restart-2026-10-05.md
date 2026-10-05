# Price fetch restart — 2026-10-05 (DQ audit, fix 1)

## Root cause

| Layer | Evidence | Fix |
|---|---|---|
| **No process fired `eod_pipeline`.** | `job_run` grouped by job: `daily_snapshot SUCCEEDED 20 (2026-09-08..2026-10-02)` and nothing else — `eod_pipeline` has zero rows, ever. The only systemd unit on the host is `daily-snapshot.timer` (`run-once daily_snapshot`); the compose `app` service runs only the status API (and is crash-looping on a stale image's migration check). `daily-snapshot.service`'s own comment: "`eod_pipeline`, which has never run once". | `ops/systemd/scheduler.service` + `install-scheduler.sh` run `python -m dataplatform.scheduler run` (every registered job), `Restart=always`. |
| **`eod_pipeline` covered one of four sources.** | `eod.DAILY_NSE_SOURCES == (nse_bhavcopy,)`. Delivery, BSE and the PR bundle had no daily path; their L0 ended where the last manual campaign ended (NSE 09-01, BSE/PR 09-04). | Tuple is now `nse_bhavcopy, nse_delivery, bse_bhavcopy`; the job also captures `nse_pr_bundle` to L0, one session behind. |
| **Self-heal skipped missed runs.** | `_failed_retryable` re-drove `FAILED(retryable)` rows only. A day the scheduler never ran has *no* row, so it was stepped over. | `_owed` also drives expected sessions in the lookback with no row. |
| **TRI resume checked the start only.** | `tri_backfill.already_published` returned true whenever the series reached back to the window start, so nifty50/niftyit/niftycpse (published once, to 2026-09-07) were skipped by every later run. midcap150/smallcap250 were current only because they were first fetched today. | `already_published(..., through=)` also requires the last session before `end`; weekly `tri_refresh` job. |
| **Nothing went red.** | `/status/sources` `healthy` = success ∧ no failure streak ∧ nothing in flight — lag ignored, so a source 21 sessions behind read `healthy: true`. `/health` reads only the heartbeat. The 09-06 gap report said `NEVER_RAN`; nothing alerted on it. | `/status/jobs` (NEVER_RAN / FAILING / OVERDUE / RUNNING / OK per registered job, plus the `UNSCHEDULED` ledger); `/status/sources` marks a scheduled source `overdue` past its job's lag budget. |
| **CA / filings refresh.** | `corp_actions_backfill`, `fundamentals_backfill`: one-off campaign drivers, never scheduled. | **Not scheduled in this fix** — refresh writes `corporate_actions` and recomputes adjustment factors (L2-facing), which must wait for the sequenced rebuild. Recorded with reasons in `registry.UNSCHEDULED` and served on `/status/jobs`. |

Regression guard: `tests/unit/test_scheduler_coverage.py` fails the gate for a live Source Register
row that no job `covers` and `UNSCHEDULED` does not explain, for a job's declared coverage drifting
from what its body fetches, for a never-fired job not reading NEVER_RAN, and for a stale scheduled
source reading healthy.

## L0 backfill (no L1/L2 written)

Driver log: `~/campaign/dq-price-fetch-restart-2026-10-05.log` (10:35–10:39 UTC, host leases held,
3 s spacing, 81 HTTP responses all 200, 0 non-2xx).

| Source | Window fetched | Fetched | Already in L0 | Missing | Expected sessions missing, whole era → 2026-10-01 |
|---|---|---|---|---|---|
| `nse_bhavcopy_udiff` | 2026-09-02..10-01 | 21 | 0 | 0 | 0 of 554 (from 2024-07-08) |
| `nse_sec_bhavdata_full` | 2026-09-02..10-01 | 21 | 0 | 0 | 0 of 1,734 (from 2019-09-30) |
| `bse_bhavcopy_udiff` | 2026-09-02..10-01 | 18 | 3 | 0 | 0 of 554 (from 2024-07-08) |
| `nse_pr_bundle` | 2026-09-02..10-01 | 18 | 3 | 0 | 0 of 4,142 (from 2010-01-04) |
| TRI `nifty50` / `niftyit` / `niftycpse` | whole history, end 2026-10-05 | 3 | 0 | 0 | series now end 2026-10-01; no 09-02..10-01 session absent |

2026-10-05 itself is today's session; its files are tonight's first scheduled run.

## Deriving L1 later — the commands for the sequenced rebuild

Zero requests: `BackfillRunner` now reuses a payload L0 already holds.

```bash
uv run python -m dataplatform.ingest.backfill --source nse_bhavcopy --from 2026-09-02 --to 2026-10-01
uv run python -m dataplatform.ingest.backfill --source nse_delivery --from 2026-09-02 --to 2026-10-01
uv run python -m dataplatform.ingest.backfill --source bse_bhavcopy --from 2026-09-02 --to 2026-10-01
uv run python -m dataplatform.ingest.tri_backfill --from-l0
```

`nse_delivery` after `nse_bhavcopy` (it rebuilds each partition from the stored bhavcopy). The PR
bundle has no L1 step. Then L2 per the rebuild's own runbook. Afterwards, enable the scheduler from
the merged main: `ops/systemd/install-scheduler.sh`.
