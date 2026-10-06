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

## The daily paper session (M13.1)

`paper_session` runs at **20:30 IST, Monday to Friday**, after the EOD pipeline above. It decides
one session of the D13-ratified momentum v2 book (`PAPER_RATIFIED_2026_09_06`: 12-1 ranking, top-20
with a top-30 sell band, 200-session regime filter, inverse-vol weights, redeploy next session) in
**paper mode only**: the order path is the backtest's own `ReplayEngine` → `RailGate` (A8) →
`SimBroker`, and nothing on it can build or accept a real broker. Book id
`momentum_v2_paper_2026_09_06`, opening capital ₹10 lakh (the M9 reports' capital), ratified rails.
Code: `backtest/paper_session.py`; ledger: `paper_session` (migration 0012).

What one run does, in order — each step can end the run:

1. **Holiday?** Not a session per `dataplatform/ingest/data/nse_holidays.yaml` → nothing written.
2. **Already decided?** A `COMPLETED` row for the date → no-op. Reruns never trade twice.
3. **Data red?** `nse_bhavcopy` not `PUBLISHED`/quality-green for the date, the status read failing,
   no L1 NSE prices for the date, or — on a rebalance — no published NIFTY 50 level for the date or
   no investable-universe coverage → one `SKIPPED_DATA_RED` journal entry (actor `SYSTEM`), a red
   `paper_session` row, **no order**. A rerun that is still red writes nothing more; a rerun after
   the data heals decides the date normally (the skip stays in the journal — it is append-only).
4. **Rebuild the book** from the `paper_session` rows: every calendar session since the first
   decided one is replayed through `SimBroker` (corporate actions, settlement, fills), the recorded
   orders are placed again, and the rebuilt book must match each session's recorded `book_digest`.
5. **Decide** the session and journal every entry (BUY/SELL, RAIL_BLOCK, or the day's HEARTBEAT),
   each tagged `payload.mode = PAPER`, `payload.paper_book = <book id>`; record the session
   `COMPLETED`. Journal entries and the ledger row commit in one transaction.

**Rebalance timing.** A rebalance is due on the first session of the month *the book decides*:
the first session it ever decides, and thereafter the first green session of each month. A red
first-of-month moves the rebalance to the next green session rather than skipping the month.
Orders staged for a session the book did not decide (red, or the job did not run) **lapse
unfilled** — the paper book never fills on bars the interlock refused. This is a known divergence
from a real broker, which would have filled them; it is the conservative choice.

### Installing and restarting

The job is registered in `dataplatform/scheduler/registry.py`; the running scheduler reads the
registry at start, so after merging, restart the user service (do not restart it mid-run of another
job — check `GET /status/jobs` first):

```bash
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart scheduler
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user status scheduler
make migrate     # once, if 0012_paper_session is not yet applied
```

### Running it by hand

```bash
uv run python -m dataplatform.scheduler run-once paper_session
```

It decides **today** (the clock's date in IST) and is safe to repeat: a decided date is a no-op and
a still-red date writes nothing new. It reads the lake and Postgres only — no network.

### Checking it

```sql
-- the ledger: one row per decided or refused session
SELECT trading_date, outcome, reason, rebalanced, jsonb_array_length(orders) AS orders,
       pending IS NOT NULL AS redeploy_pending
FROM paper_session WHERE book_id = 'momentum_v2_paper_2026_09_06' ORDER BY trading_date DESC;

-- the decisions behind it
SELECT trading_date, decision, isin, sleeve, rationale
FROM decision_journal
WHERE payload->>'paper_book' = 'momentum_v2_paper_2026_09_06'
ORDER BY trading_date DESC, id;
```

`GET /status/jobs` shows the job's last run; a `FAILED` run left nothing behind (the transaction
rolled back) and the next run retries the date.

### When it is red or fails

- **Red every rebalance day: "no level for <date>" from the published `nifty50` series.** The
  regime filter reads the published NIFTY 50 level *for the session itself* and never a stale one
  (`backtest.run._RegimeSource`). `tri_refresh` runs weekly (Saturday 08:00) and only fetches up to
  the session *before* the day it runs, so on its current cadence the session's level is never in
  the lake by 20:30 the same day, and every rebalance attempt is red until it is. The book stays in
  whatever it last held (cash, at inception) and the journal shows one `SKIPPED_DATA_RED` per day
  naming the cause. The fix is a same-evening TRI refresh for the session (a change to
  `tri_refresh`'s cadence and target) — an owner/orchestrator decision, not something this job
  works around.
- **`PaperBookDivergenceError`.** The rebuilt book no longer matches the digest recorded when a
  past session was decided: an input the rebuild reads (a raw bar a fill was priced on, a
  reconciled corporate action) changed since. The job refuses to trade on a history different
  from the journaled one. Do not delete or edit `paper_session` rows; find which input moved
  (the error names the session), and escalate — re-basing a paper book is an owner decision.
- **`CalendarCoverageError` / no session after a date.** The holiday file covers through
  2026-12-31; the job needs the next year's holidays before the last December session. Extend
  `dataplatform/ingest/data/nse_holidays.yaml`.
- **Any other exception.** The run is `FAILED` in `job_run` with nothing written; read the error,
  fix, and `run-once paper_session` (same date: idempotent).

**Never** point this job at a real broker. Real money for this configuration is a separate
ratification (AGENTIC_CONTEXT §3.2) and a separate job; `tests/unit/test_paper_session_paper_only.py`
fails if this one could reach `KiteBroker`.
