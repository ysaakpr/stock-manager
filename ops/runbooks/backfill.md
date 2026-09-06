# Runbook — backfill runner (D1, M1.9)

The backfill runner drives a date range of one source set through the whole ingestion pipeline —
`fetch → L0 → parse → L1 → sync_state` — and is safe to kill and restart at any point. It never
re-fetches a session that is already `PUBLISHED`, so "run it again" is always the right first move
when you are unsure where a run got to.

Three source sets exist (`--source`): `nse_bhavcopy` — the NSE cash-market bhavcopy, both format
eras (legacy `cm…bhav.csv.zip` before 2024-07-08, UDiFF after), dispatched by date automatically;
`bse_bhavcopy` — the BSE cash bhavcopy, UDiFF era only (see below); and `nse_delivery`. The price
sets land `prices_raw` partitions in L1 — one partition per date, shared by both exchanges, each
write replacing only its own exchange's rows — and one `sync_state` row per session under the
era-independent set name.

## Before you fetch anything: plan it (`--dry-run`)

`--dry-run` prints the exact request plan — one line per session, its era's register id and the
URL that would be fetched — and the total count. It opens **no socket and no database**, so it is
always safe and needs nothing running.

```bash
uv run python -m dataplatform.ingest.backfill \
  --source nse_bhavcopy --from 2016-01-01 --to 2026-08-07 --dry-run
```

The count is the number of expected trading sessions in the range (weekends and holidays owe no
file and are not planned), taken from the C.2 calendar. If the range leaves the calendar's coverage
the command fails loud with exit code `2` rather than guessing — extend `nse_holidays.yaml` first.

## Sampling (`--limit`) — the B1 default

Under build decision **B1**, an agent may fetch a **~60-session sample spread across all eras**, but
**not** the full 10-year run (that is `NEEDS_GO`, reserved to the human — AGENTIC_CONTEXT §3.3).
`--limit N` samples `N` sessions spaced **evenly** across the whole range, so the sample always
spans both bhavcopy eras and exercises both parsers:

```bash
# ~60 sessions across the decade — the sanctioned unattended sample.
uv run python -m dataplatform.ingest.backfill \
  --source nse_bhavcopy --from 2016-01-01 --to 2026-08-07 --limit 60
```

Preview the exact sample first by adding `--dry-run` to the same command. Do **not** run the full
range unattended without the human's one-word go.

## Resume — how a killed run picks up

Resume is automatic and needs no flag. The checkpoint is the committed `sync_state` row, written
after **every** session, so a kill loses at most the one session in flight:

- a `PUBLISHED` session is skipped and never re-fetched;
- a retryable `FAILED` session is retried from the top on the next run;
- a session that was never reached is fetched normally.

To resume, **re-run the identical command**. It reconciles against `sync_state` and does only the
work that is left. Progress is visible any time at `GET /status/sync?date=` and `GET /status/sources`,
and unexplained gaps at `GET /status/gaps?from=&to=`.

### Graceful stop (Ctrl-C / SIGINT)

One `Ctrl-C` requests a **graceful** stop: the runner finishes (or fails and records) the current
session, commits it, and stops before the next one — no half-written session. A **second** `Ctrl-C`
force-quits. After a graceful stop, just re-run the command to continue.

Exit codes: `0` clean or gracefully stopped · `2` the range could not be planned (calendar
coverage) · `3` the run hard-stopped on a 403 spike (see below).

## 403 hard stop — what to do

A 403 is the exchange refusing this client. The fetcher counts consecutive 403s from a host and, at
`HTTP_FORBIDDEN_STREAK_LIMIT` (default 3), **hard-stops**: it stops talking to that host for the
life of the process, fires a CRITICAL alert, and the backfill exits with code `3`. The tripping
session is recorded `FAILED (retryable=False)`; earlier 403s are `FAILED (retryable=True)`.

**Do not route around it** (AGENTIC_CONTEXT §8). Specifically:

- **no** second user agent, **no** proxy, **no** faster retry, **no** lowered rate limit — the code
  has no switch for any of these on purpose, and adding one is a defect.
- Find out **why** the source started refusing us: check whether NSE changed its access rules or is
  rate-limiting, and whether the request headers still match a real browser (`source_register.yaml`).
- The hard stop is per-process. Only after the cause is understood, restart the runner deliberately
  — re-running the command is safe (already-`PUBLISHED` sessions are skipped), and the fresh process
  clears the in-memory stop.
- If the block is not something you can resolve (the source changed its terms, or it needs a
  credential that does not exist), it is a `NEEDS_GO`/`NEEDS_SECRET`-class matter — escalate rather
  than retry.

## BSE (`bse_bhavcopy`)

BSE's cash bhavcopy comes from `www.bseindia.com` — its own host and its own request budget, so it
may run beside an NSE campaign. Only the UDiFF era (2024-07-08 onward) is wired: it carries ISIN
natively and parses straight to `prices_raw`, tagged `exchange=BSE`, into the same partition as
that date's NSE rows. A pre-cutover `--from` is refused at planning time (exit 2): the legacy files
have no ISIN and their L1 path goes through the scrip master (ops/BACKLOG.md, M3.1). The calendar
is NSE's, so a BSE-only holiday shows up as one `FAILED (retryable)` 404 for D7 to explain.

```bash
# Plan it — 536 sessions from the cutover to 2026-09-05; no socket, no database.
uv run python -m dataplatform.ingest.backfill \
  --source bse_bhavcopy --from 2024-07-08 --to 2026-09-05 --dry-run
```

On the server the campaign is driven by `ops/run_bse_campaign.sh` (fixed `--from`, yesterday's
`--to`, `nohup` with a dated `~/campaign/bse-*.log`; `--dry-run` prints the plan) and followed with
`ops/remote.sh logs bse`. Progress is also at `GET /status/sources` under `bse_bhavcopy`. The full
run was authorised by the owner on 2026-09-06 (HUMAN_DECISIONS.md, D14), in parallel with the NSE
Integrated Filing campaign on the same server.

## Reproducing offline

Every behaviour above is covered offline by `tests/integration/test_backfill.py`, which scripts the
network with the real checked-in fixtures (`tests/fixtures/nse_bhavcopy/{legacy,udiff}/`) and a
scratch Postgres — no exchange is ever contacted. Run it with `uv run pytest
tests/integration/test_backfill.py -q`.
