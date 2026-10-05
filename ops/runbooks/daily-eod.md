# Runbook — daily EOD pipeline (D1, M1.10)

The daily EOD pipeline is the scheduled job that keeps today's data landing. Once a day, after the
NSE close, it takes the latest trading session, drives every daily NSE source down the whole
ingestion pipeline — `fetch → L0 → parse → L1 → sync_state` — self-heals any recent retryable
failure, runs the gap check, publishes the day's archive bundle, and alerts on anything left FAILED.

It is the same ingest code path as the backfill runner (M1.9); the daily job is that runner pointed
at one session plus a short self-heal window. So "run it again" is always safe: a `PUBLISHED`
session is never re-fetched, and the archive is built at most once.

- **Job name:** `eod_pipeline`  ·  **Schedule:** `30 18 * * mon-fri` (18:30 Asia/Kolkata)
- **Daily sources:** `nse_bhavcopy` (cash bhavcopy), `nse_delivery` (`sec_bhavdata_full`) and
  `bse_bhavcopy` (BSE UDiFF), each to `PUBLISHED`; plus the NSE PR bundle (`nse_pr_bundle`) captured
  to L0 only, one session behind. Until 2026-10-05 this list was `nse_bhavcopy` alone.
- **Fired by:** the scheduler process, `ops/systemd/scheduler.service` (below). Nothing else fires
  it — `daily-snapshot.timer` runs only `daily_snapshot`.
- **Lookback (self-heal window):** 7 days.
- **Archive lake / download root:** `DATA_ROOT` (the same lake `/archives` serves from).

## What one run does

1. Picks the **latest trading session** on or before today from the C.2 calendar (a Monday run
   processes Friday; a holiday is stepped over). A date the calendar does not cover fails loud —
   extend `nse_holidays.yaml` rather than let the job guess a session.
2. **Self-heal first.** Every date left `FAILED(retryable)` within the lookback window — and every
   session the calendar expected that has **no row at all** (the trace of a missed run) — is
   attempted before today's session. A non-retryable `FAILED` (e.g. a genuine 404) is left
   alone — re-driving it forever is the hot loop the `retryable` flag exists to prevent.
3. Drives the target session for each daily NSE source to `PUBLISHED`, committing after each one
   (that commit is the checkpoint).
4. Runs the **D7 gap check** over the window and logs its summary. Unexplained gaps raise a WARNING
   alert but do **not** fail the job — filling a genuine multi-year hole is the backfill's job
   (M1.13), not the nightly run's.
5. Publishes the session's **archive bundle** (M1.12) — but only if it has none yet, so a re-run is
   a true no-op.
6. **Alerts** CRITICAL on any source left FAILED, then reports the job FAILED so `run-once` exits
   non-zero and the failure is visible. The FAILED row stays retryable for the next run to heal.

## Keeping it scheduled

The jobs in `dataplatform/scheduler/registry.py` are a schedule only while a process fires them.
On this host that process is the systemd user service:

```bash
ops/systemd/install-scheduler.sh            # install/refresh; disables daily-snapshot.timer
systemctl --user restart scheduler.service  # after every merge to main — code is read at start
systemctl --user status scheduler.service
```

`GET /status/jobs` is the check: every registered job with its newest run, newest success and a
state — `NEVER_RAN` (no `job_run` row: nothing is firing it), `FAILING`, `OVERDUE` (no success
since a fire that should have finished), `RUNNING` or `OK` — plus `unscheduled`, the registry's
ledger of live register sources no job covers and why. `GET /status/sources` marks a scheduled
source `overdue` (and not `healthy`) once it is more sessions behind than its job's budget.

**The 2026-10-05 incident, for the record.** `eod_pipeline` had zero `job_run` rows ever: the only
unit installed was the snapshot timer, so the bhavcopy family stopped at the last manual campaign
(NSE 2026-09-01, BSE and PR 2026-09-04) and `/status/sources` still said `healthy: true`. Both
surfaces above now go red for that state, and `tests/unit/test_scheduler_coverage.py` fails the
gate for a live register source that no job covers and the ledger does not explain.

## Catching up a gap without touching L1

When L1/L2 must not move yet (a rebuild is pending), acquire into L0 only, then derive later:

```bash
# 1. acquire (network; takes the host lease; skips keys L0 already holds; never overwrites)
uv run python -m dataplatform.ingest.l0_acquire --source nse_bhavcopy --source nse_delivery \
    --source bse_bhavcopy --from <first-missed> --to <last-session> \
    --tri nifty50 --tri niftyit --tri niftycpse --end <today>
uv run python -m dataplatform.ingest.pr_bundle_campaign acquire --from <first-missed> --to <last-session>

# 2. derive, when the rebuild is sequenced (zero requests: BackfillRunner reuses stored payloads)
uv run python -m dataplatform.ingest.backfill --source nse_bhavcopy --from <first-missed> --to <last-session>
uv run python -m dataplatform.ingest.backfill --source nse_delivery --from <first-missed> --to <last-session>
uv run python -m dataplatform.ingest.backfill --source bse_bhavcopy --from <first-missed> --to <last-session>
uv run python -m dataplatform.ingest.tri_backfill --from-l0
```

`nse_delivery` must run after `nse_bhavcopy`: it rebuilds each partition from the stored bhavcopy.
The PR bundle has no L1 step (symbol-keyed, no ISIN — `pr_bundle_campaign`'s docstring).

## Running it by hand

The scheduler fires it automatically; to run it now (an operator or an agent):

```bash
uv run python -m dataplatform.scheduler run-once eod_pipeline
```

Exit codes come from the scheduler (`__main__.py`): `0` succeeded, `1` failed or overran its budget,
`2` no such job, `3` another process already holds the job's lock. The singleton advisory lock means
a manual `run-once` and the container's scheduler cannot double-run the session.

This is a **networked** command: it fetches from NSE and writes to `DATA_ROOT`. It is not offline —
the test suite never invokes it (B8); the tests drive `EodPipeline` directly with a recorded
transport.

## Checking a run

- **Was it healthy?** `GET /health` — the heartbeat moved and the last `job_run` for `eod_pipeline`
  is `SUCCEEDED`.
- **Did the session publish?** `GET /status/sync?date=<session>` — every daily NSE source should be
  `PUBLISHED`. `GET /status/sources` shows each source's last success and failure streak.
- **Any gaps?** `GET /status/gaps?from=<window-start>&to=<session>` — the unexplained set should be
  empty once the backfill (M1.13) has run; before that, expect the un-backfilled backlog to show as
  `NEVER_ATTEMPTED`, which is the alert telling you to backfill.
- **The archive:** `GET /archives?date=<session>` returns the manifest; the files download and
  verify. Public redistribution of exchange data is a HUMAN_GATE legal question (§10) — these
  bundles are local/personal download only.

## When a run fails

1. **One source FAILED (retryable).** Nothing to do — the next scheduled run self-heals it. To heal
   now, just re-run `run-once eod_pipeline`; the retryable date comes back to `PENDING` and is
   re-driven. Confirm with `/status/sources`.
2. **A source keeps failing.** Read the `last_error` in `/status/sources`. A parser or URL change
   is a source-register (C.1) fix; a transient upstream 5xx heals on its own next run.
3. **A 403 spike hard-stopped a host.** The fetcher stops talking to that host for the life of the
   process and sends a CRITICAL alert. **Do not work around it** — no user-agent rotation, no proxy,
   no faster retry (AGENTIC_CONTEXT §8). Find out why the source started refusing us, then restart
   the scheduler deliberately.
4. **Calendar coverage error.** `today` is past the holiday file's coverage. Extend
   `dataplatform/ingest/data/nse_holidays.yaml` and re-run.

## Idempotency and safety

Running twice for the same session changes nothing: no re-fetch (the row is `PUBLISHED`), no L1
rewrite, and no second bundle (L0 is immutable, so the bundle would be byte-identical anyway). L0 is
never edited or deleted (invariant #1); every L1 value is re-derivable from it.
