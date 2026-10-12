# Operator console (M18)

One web view of the running platform, served by the status API itself at `/console` — no new
service, no new port, read-only. It reads through `dataplatform.status.queries`, the same
functions the JSON endpoints use, so a page never disagrees with `/status/*`.

## Reaching it

The status API listens on loopback only (M15.6, `ops/runbooks/loopback-only-ports.md`), so the
console is reached through an ssh tunnel to port 8000:

```bash
ssh -L 8000:127.0.0.1:8000 <the server>      # host and user from your own ssh config
# then open http://127.0.0.1:8000/console in a local browser
```

Nothing on the page is loaded from outside the process: htmx 1.9.12 is vendored at
`dataplatform/status/console/static/` (sha256 pinned in `tests/unit/test_console_ops.py`, SRI on
the script tag), so the page works with no outbound network. Every time shown is Asia/Kolkata,
labelled `IST`, as of the injected clock.

The running `app` container serves whatever code it was built from — after a merge, the console
appears only once that container is rebuilt and restarted (an owner step).

## The Operations page (`/console`, `/console/ops`)

| Panel | What it shows | Where it comes from |
|---|---|---|
| Health banner | OK/DEGRADED, database reachable, scheduler heartbeat state, age and threshold | `assess_health` — the same verdict as `GET /health` |
| Jobs | every registered job, worst first (FAILING, OVERDUE, NEVER_RAN, RUNNING, OK): state, cron, last run, its duration, last success, due since, next fire, last error (masked). Refreshes every 30 s | `read_job_health` — the same call `GET /status/jobs` makes |
| Job drill-down (`/console/ops/jobs/<name>`) | the newest 50 `job_run` rows: start, finish, state, duration, instance, error (masked, whole) | `read_job_runs` |
| Alerts | open `alert_condition` rows (critical first), the alert channel (`ALERT_PROVIDER`; `log` means nothing pages), and the `failure_alerts` job's state and last tick | `read_alerts` |
| Backups | for `postgres_backup` and `l0_backup`: the job's state, last run and last success, the newest dump (or the L0 manifest) under `BACKUP_ROOT` with size, IST stamp and age | `read_backups` (`store.backup.list_backups`) |

What it does not show, and why:

- **A restore-drill verdict.** The drill prints its report and records none, so the panel says so
  (BACKLOG M18.1). Run `uv run python -m dataplatform.store.backup drill` for a fresh one.
- **Backup files, when served from the container.** The `app` container does not mount
  `BACKUP_ROOT`; the panel says "not visible to this process" rather than "no backups". The job
  lines still come from `job_run` and are authoritative for whether the jobs ran.
- **A secret.** Every free-text field (`job_run.error`, alert titles, the database error) goes
  through `dataplatform.redaction.mask_and_truncate` before it renders, even when the writer
  already masked it.

Data, Desk and Journal are in the navigation and say "not built yet" until M18.2–M18.4.

## Read-only

Every console route answers GET and HEAD only; POST, PUT, PATCH and DELETE are 405. The unit
suite enumerates the router's routes to prove it, so a new route is covered without being listed.
