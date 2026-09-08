# Runbook — the daily snapshotter

**This is the only job in the platform with a deadline.** Everything else can be backfilled; these
sources cannot. `sec_list.csv`, `/api/reportASM`, `/api/reportGSM`, `/api/reportESM`,
`ind_niftytotalmarket_list.csv`, `EQUITY_L.csv`, `symbolchange.csv`, the BSE list-of-scrips and the
niftyindices constituent lists each serve exactly one snapshot — the current one. No date
parameter, no archive host, no historical form. **Every day this does not run is a day of history
that no later money or effort can buy back.**

If you read one line of this file: *check the timer is enabled and the last run succeeded.*

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user list-timers daily-snapshot.timer --all      # next fire, last fire
systemctl --user status daily-snapshot.service               # last outcome
tail -40 ~/snapshots/daily-snapshot-$(date +%F).log          # what it did
```

## What it captures, and why each one matters

| Register id | Payload | Why it cannot wait |
|---|---|---|
| `nse_industry_classification` | `ind_niftytotalmarket_list.csv` | **The load-bearing one.** A thematic fund's universe rule *is* a point-in-time classification. The only other sector source in this repo is a 42-ISIN fixture CSV applied backward — classification bias in one file |
| `bse_scrip_master` | list-of-scrips, `INDUSTRY` + `ISIN_NUMBER` | BSE's own taxonomy over every active scrip: a genuine second opinion, and far wider than the broad-market 750 |
| `nse_price_bands` | `sec_list.csv` | Buying at a locked upper circuit is the most flattering fiction a backtest can tell, and only the day's operative band exposes it |
| `nse_asm_list` / `nse_gsm_list` / `nse_esm_list` | ASM / GSM / ESM | Tradability. A GSM stage is the difference between a name a fund could exit and one it could not |
| `nse_equity_list` / `nse_symbol_changes` | `EQUITY_L.csv`, `symbolchange.csv` | The identity spine. **A delisted company vanishes from `EQUITY_L.csv` the day it dies** — the accumulated series is the only record it was ever listed |
| `nifty_index_constituents` | 17 index lists | Survivorship bias. Today's members applied backward assumes they always were the members |

~25 requests per day at policy spacing, four hosts. Far under the ~200-request line
`AGENTIC_CONTEXT §3.3` reserves to the owner, so it needs no sign-off — only that it keeps running.

## The schedule

A **systemd user timer**, `19:15 Asia/Kolkata, Mon..Fri`. Units are checked in at
`ops/systemd/` and installed with `ops/systemd/install.sh`.

Why a timer and not this repo's own scheduler: `python -m dataplatform.scheduler run` would start
*every* registered job, including `eod_pipeline`, which has never run once. Turning on the whole
scheduler is a much larger change than starting the snapshotter, and it is the owner's decision.
The timer still calls `SchedulerRunner.run_once`, so the Postgres advisory lock, the `job_run` row
and the `scheduler_heartbeat` upsert `/health` reads are all exactly as they would be under the
scheduler process — nothing about the job's operability depends on which of the two started it.

Why 19:15 IST: after the 15:30 close, after NSE publishes the next session's bands and surveillance
lists, and 45 minutes after the 18:30 EOD pipeline. The two share `nsearchives.nseindia.com`, and a
host lease is **refused rather than queued**, so an overlap would be a skipped snapshot.

Why it survives a reboot: `loginctl enable-linger ubuntu` (done by `install.sh`) starts ubuntu's
user manager at boot with no login, and `Persistent=true` runs a fire the box was down for as soon
as it comes back. A late capture is worth far more than a skipped one.

Weekdays only. The job files `GAP` for a closed day and spends no requests, so a weekend fire would
be harmless — but a declared holiday is exactly when NSE answers 200 with the previous session's
file, and not asking beats asking and discarding.

## Reading an outcome

`sync_state` carries one row per `(source, capture date)`. `job_run` carries one row per run.

```bash
docker exec trading-platform-postgres-1 psql -U trading -d trading -c "
  select job_name, state, started_at, finished_at, error
  from job_run where job_name = 'daily_snapshot' order by started_at desc limit 5"

docker exec trading-platform-postgres-1 psql -U trading -d trading -c "
  select source, state, last_error
  from sync_state where logical_date = current_date order by source"
```

| Outcome | State in `sync_state` | What to do |
|---|---|---|
| `CAPTURED` | `PUBLISHED` | Nothing |
| `REUSED` | `PUBLISHED` | Nothing — the lake already held the date; no request was made |
| `STALE` | `FAILED` (retryable) | **Act today.** The source answered 200 with another session's file. The bytes are in L0 as the record of what it served, but the date is not published. Re-run after NSE updates: `systemctl --user start daily-snapshot.service` |
| `MALFORMED` | `FAILED` (retryable) | The payload is not the shape the register records — usually a soft 404 (200 + an HTML shell). Read the payload in L0, then fix the register row or the inspector. Do **not** relax the inspector to make it pass |
| `FAILED` | `FAILED` (retryable) | A 4xx, an exhausted retry budget or a 403 hard stop. Check `~/snapshots/*.log` for which |
| whole run raises | `job_run.state = FAILED` | Nothing landed at all. Tomorrow's run self-heals, but today is lost unless you re-run it today |

Every degraded source also raises an alert, one per `(source, date, status)` — `STALE` at
**CRITICAL** (silently wrong data is worse than visibly absent data), the rest at WARNING.

## Running it by hand

Idempotent per `(source, date)`: a source already `PUBLISHED` for the date is re-reported from the
bytes in the lake with **zero requests**. So a manual run beside the timer is safe.

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user start daily-snapshot.service       # through systemd, exactly as the timer does

# or directly, which is what the unit runs:
DATA_ROOT=/home/ubuntu/stock-manager/data ops/daily-snapshot.sh
```

**Never run it with `DATA_ROOT` unset.** `Settings.data_root` defaults to `data` anchored at the
repo root, so from a git worktree it resolves to *that worktree's* `data/L0`. This has already gone
wrong twice and the payloads had to be transferred across afterwards. The guard is
`SNAPSHOT_EXPECT_LAKE_ROOT`: the job resolves its L0 root, compares it, and refuses to make a
single request if they disagree. When it fires, **fix the invocation, never the assertion.**

## Backfilling a missed day

You cannot. That is the entire point of this runbook. There is no URL that serves an earlier
snapshot of any of these sources. A missed day is a permanent hole, visible as a
`NEVER_ATTEMPTED` entry in the D7 gap report for the five `era.start: 2026-09-08` sources, and
invisible for the rest.

The one thing that *is* recoverable: a day the box was down but came back the same day. The timer's
`Persistent=true` catches that on its own.

## Pre-merge caveat (delete this section when `ops/daily-snapshotter` merges)

The checked-in unit points at `/home/ubuntu/stock-manager`, which is where this should run from.
The job does not exist there until this branch merges, so a temporary drop-in
(`ops/systemd/pre-merge-drop-in.conf`, installed as
`~/.config/systemd/user/daily-snapshot.service.d/10-pre-merge.conf`) points the schedule at the
branch's worktree meanwhile — because a day spent waiting for a merge is a day of history gone.
`DATA_ROOT` still names the authoritative lake either way, and the assertion still proves it.

**On merge:** delete the drop-in, `systemctl --user daemon-reload`, and run the service once by
hand to prove the merged path. Until then, do not delete `/home/ubuntu/wt/daily-snapshotter`.
