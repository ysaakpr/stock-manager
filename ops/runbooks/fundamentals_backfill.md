# Runbook — fundamentals backfill (M10.4)

Fills the true point-in-time fundamentals store (`pit_fundamentals`, M7.3) by fetching the NSE
results index across the price window and each entry's XBRL filing, resolving to ISIN through the D2
master, and writing every fact tagged with the `filing_date` it first became knowable.

This is a **B1 bulk-fetch campaign** (thousands of per-filing fetches) — `NEEDS_GO`. Do not launch the
full-window run without the owner's go, exactly as for the M1.13 price backfill.

## Commands

```bash
# See the discovery plan without touching the network (index chunks only; the per-filing count is
# only known once the index is fetched).
uv run python -m dataplatform.ingest.fundamentals_backfill \
    --from 2016-09-01 --to 2026-09-01 --dry-run

# A bounded verify+sample run (B1): recent filing-date window, capped per-filing.
uv run python -m dataplatform.ingest.fundamentals_backfill \
    --from 2026-07-01 --to 2026-08-15 --max-filings 50

# The full-window run (owner go required).
uv run python -m dataplatform.ingest.fundamentals_backfill \
    --from 2016-09-01 --to 2026-09-01
```

Flags: `--limit` samples the index-chunk plan; `--max-filings` caps how many in-universe filings the
ingest phase attempts (the bulk half's sample control); `--chunk-months` sizes each index fetch's
filing-date window (default 3 — one quarter); `--report` sets the coverage-report path.

## What it does, in order

1. **Plan** — one index fetch per `(period, filing-date chunk)` for Quarterly and Annual. The index
   endpoint filters by **filing date** (dissemination), not reporting period, which is the correct
   PIT axis.
2. **Universe** — the ISINs present in `prices_raw` over the window, intersected with the D2 master's
   known securities (invariant #2). Filings whose ISIN is out of this set are skipped and counted.
3. **Discovery** — fetch each index chunk → `FilingIndexEntry`s, checkpointed under
   `nse_financial_results_index` in `sync_state`.
4. **Ingest** — for each in-universe entry: fetch the XBRL, parse it, `write_pit`. Checkpointed under
   `nse_xbrl_filing/<filing_id>`. `write_pit` lands each filing in its `filing_date` partition; a
   restatement is a new record, never an overwrite (invariant #8).

## Watching a run

```bash
ops/fundamentals_progress.sh        # one snapshot
ops/fundamentals_progress.sh -w     # refresh every 30s
```

Reports index chunks, filings published/failed against the in-universe total, the publish rate over
the last ten minutes with an ETA, the L1 partition span, L0 size, and **failures grouped by cause**
— anything it cannot classify is printed as `UNKNOWN — needs a look`, which is the line to watch:
every other class is a known and understood outcome.

Progress comes from `sync_state`, not from tailing a log, because the checkpoint is the authority on
what is done. The denominator is the *in-universe* count, not the raw announcement count — the
runner only attempts entries whose ISIN is in the price-window universe (about 63% of documents),
and using the announcement total instead nearly doubles the quoted ETA.

## Restarting after a failure

**Rerun the same command.** Nothing is redone: the checkpoint is committed per unit, and the only
state that is skipped is `PUBLISHED`, so a run never restarts from the beginning.

| What happened | State left behind | Next run |
|---|---|---|
| Unit published | `PUBLISHED` | Skipped, no request |
| Unit failed (404, parse error, DB error) | `FAILED` + the reason in `last_error`, `attempts` incremented | **Retried** |
| Process crashed or was killed mid-unit | `PENDING`/`FETCHED`/`VALIDATED` — whatever it reached | **Retried** |
| 403 spike parked the run | tripping unit `FAILED`, exit 3 | **Retried** (see the caution below) |

`retryable` is recorded for the report, **not** read back as a gate — the same convention as the
price (M1.13) and corporate-action (M9) backfills. That is deliberate: a parse failure whose parser
has since been fixed, or a host that has stopped refusing, should heal by running the command again
rather than by hand-editing `sync_state`. The cost is that a permanently broken unit is re-attempted
every run; it stays visible in the coverage report's failure list, and `attempts` shows how often.

`--max-filings` counts *attempts*, not resume-skips, so a bounded restart makes real progress:
`--max-filings 25` over a window with 22 already published attempts 25 **new** units, not 3.

- **Graceful stop:** first `Ctrl-C` finishes the current unit and stops; second forces quit.
- **403 spike (hard stop):** the run **parks** with `ParkReason.FORBIDDEN_SPIKE`, records the
  tripping unit `FAILED` (non-retryable), and exits 3. Do **not** lower the rate, rotate the agent,
  or proxy around it (AGENTIC_CONTEXT §8) — understand why the host refused, then restart deliberately.

## Picking up new filings

**The scheduler does this now** — `fundamentals_forward` (M14.3), nightly at 02:00 IST; see
[The scheduled forward job](#the-scheduled-forward-job-m143) below. Run a forward sync by hand
only to recover from something the job cannot (a hole older than its retry lookback, a parser
fix), and never while the job runs: both lease the same two hosts, so the second one to start is
refused. A manual forward sync is this command run over a recent window, and there is one trap in
doing that:

> **A published index chunk is re-parsed from its frozen L0 payload, not re-fetched.** So rerunning
> the *identical* window will never discover a filing broadcast since that chunk was first fetched.

The chunk's checkpoint key is `nse_financial_results_index/<period>/<chunk end>`, which is one-to-one
with its L0 filename — so **moving `--to` forward creates a new chunk and re-fetches the index**,
which is what you want:

```bash
uv run python -m dataplatform.ingest.fundamentals_backfill --from 2026-07-01 --to $(date +%F)
```

The per-filing checkpoint is keyed on the feed's `seqNumber`, which is stable across fetches, so
everything already ingested is skipped and only genuinely new filings are fetched. A verified run of
exactly this shape re-read two index chunks (2 requests), discovered 9 entries, and fetched **zero**
documents because all 8 in-universe filings were already published.

Keep `--from` fixed and `--chunk-months` consistent between runs. Changing either changes the chunk
boundaries, and a chunk whose range changed is correctly treated as not-yet-fetched — harmless, but
it re-fetches index chunks you already have (~88 requests for the full window).

## Exit codes

`0` clean or gracefully stopped · `2` cannot plan (bad range) · `3` parked on a 403 spike · `4`
another driver holds one of the hosts' leases (the scheduled job, for instance).

## Coverage report

Written to `ops/gates/M10-fundamentals-backfill-report.md`: index chunks published, filings
discovered vs in-universe vs published/failed, facts written, ISINs covered, entries skipped as out
of universe, and any park cause. Read it to see what a run covered and what it left.

## The feed moved: Integrated Filing (Financials) from the quarter ended March 2025

`corporates-financial-results` stopped receiving new periods after the quarter ended December 2024
(it still lists defunct companies filing old quarters — 28 entries for April-June 2025 against
3,865 for the quarter before). Results from the March-2025 quarter on are published only through
SEBI's Integrated Filing regime, `api/integrated-filing-results`, which is paged (`page`, `size`
up to 1000), carries no ISIN and no period start, and points at `INTEGRATED_FILING_INDAS_*` /
`_NONINDAS_*` documents in SEBI's `in-capmkt` taxonomy. `--feed integrated` reads it:

```bash
uv run python -m dataplatform.ingest.fundamentals_backfill --feed integrated \
    --from 2025-03-01 --to $(date +%F) --dry-run          # 6 pages per calendar month
uv run python -m dataplatform.ingest.fundamentals_backfill --feed integrated \
    --from 2025-03-01 --to 2025-04-30 --max-filings 20    # B1 verify + sample
uv run python -m dataplatform.ingest.fundamentals_backfill --feed integrated \
    --from 2025-03-01 --to $(date +%F) --report ops/reports/fundamentals-integrated-<date>.md
```

Each page is its own resumable unit (`nse_integrated_filing_index/<month end>/p<NN>`); a page past
the end of a month is a normal zero-entry unit. The symbol on each record is resolved through D2 as
of its dissemination date (`creation_Date`), a record that does not resolve is counted and named on
the report, and the document's own `ISIN` fact is cross-checked against the resolved one. A
fourth-quarter record yields two entries — the quarter and the financial year — because the
document carries both columns and the annual one is where the balance-sheet elements are. Ids are
prefixed `IF` so they never collide with the old feed's. Documents land in the same L0 source and
the same PIT store as before; the filing-date partition is the first-knowable date either way.

Keep `--from` fixed at `2025-03-01` between runs so the month windows (and their checkpoints) stay
identical; move `--to` forward to pick up new pages. About 26,600 records existed on 2026-09-06,
so the first full run is a B1 campaign of roughly a day at the 2.5 s spacing.

## The scheduled forward job (M14.3)

`fundamentals_forward` in `dataplatform/scheduler/registry.py`, body
`dataplatform/ingest/fundamentals_forward.py`. It is the integrated-feed campaign above, run by the
scheduler over a window `sync_state` names, so the store keeps up through results season without
anyone picking dates.

- **When:** 02:00 IST every day (results are disseminated on weekends too), 3-hour budget. That is
  the one long gap in which no other job leases `www.nseindia.com` or `nsearchives.nseindia.com`:
  `announcements_capture` (00:30, 45 min) has finished, and the run is over before the
  first-Sunday `bse_ca_sweep` (06:00), the Saturday `identity_refresh` (07:00) and `ca_refresh`
  (10:00) — and nowhere near the 18:00-20:30 evening window (`shareholding_poll` 18:05,
  `eod_pipeline` 18:30, `daily_snapshot` 19:15, `nse_daily_capture` 20:00 / 23:00).
  `tests/unit/test_scheduler_coverage.py` fails the gate if the slot ever overlaps one of them.
- **Window:** from the day after the **watermark** to **yesterday**. The watermark is the end of
  the contiguous run of integrated-index windows whose six pages are all `PUBLISHED`, starting at
  `2025-03-01`; a window with a failed or never-attempted page does not count, and a hole holds the
  watermark at the hole. Yesterday, not today: the feed filters on the dissemination date, and a
  window ending today would be marked done at 02:00 with the day's filings still to come.
- **Bounded:** at most 3 new days per run (peak season: 840 filings on 2026-05-29, ~2.5 s each). A
  longer outage is caught up 3 days a night; `/status/sources` shows the index overdue meanwhile.
- **Retries:** a retryable `FAILED` filing from the last 7 days pulls the window's start back over
  its date. The document is usually already in L0, so a retry costs index pages, not a re-download.
  After 7 days a still-failing filing is left `FAILED` on `/status/sources` for a human.
- **Universe:** ISINs with a `prices_raw` row in the 92 days to yesterday, resolved through D2 —
  not the window's own days, which on a weekend have no price partition at all.
- **Idempotent:** nothing owed means no lease, no request, a `fundamentals_forward.nothing_owed`
  log line and a SUCCEEDED run. A `PUBLISHED` page or filing is never re-fetched.
- **No watermark** (an empty store) fails the run: starting the store is the B1 campaign's job.

**Where a failure shows.** The job raises — so `job_run` is FAILED, `GET /status/jobs` reports it
`FAILING` and `failure_alerts` pages it once — when:

- **another driver holds either NSE host** (`HostBusyError: … pid … running '<command>'` in
  `job_run.error`). The lease is refused, never queued, and never broken while its holder lives.
  The watermark has not moved, so the next night owes the same days plus one. If a campaign is
  going to hold an NSE host across 02:00, expect this failure and let the next run catch up;
- **an index page failed** (`FundamentalsForwardError: … index page(s) failed`) — the window is not
  done and the next run re-plans it under a new key, re-fetching the live index;
- **a 403 spike parked the run** (`… parked — FORBIDDEN_SPIKE …`) — AGENTIC_CONTEXT §8: find out
  why the host refused before anything else; do not lower the rate or rotate the agent.

A single filing's failure does **not** fail the run: it is `FAILED` on its own
`nse_xbrl_filing/<filing_id>` row, which `/status/sources` counts, and the retry lookback re-plans
it. The run's `fundamentals_forward.done` log line carries the counts, including the integrated
records whose symbol D2 did not resolve (`unresolved_symbols`) — a listing newer than the Saturday
`identity_refresh`. Those are not retried automatically; after the next identity refresh, re-run
the affected window by hand (above) to pick them up.

```bash
# Run it now (an operator or an agent); exit 1 on FAILED, 3 if the scheduler is already running it.
uv run python -m dataplatform.scheduler run-once fundamentals_forward
# What did the last runs do?
curl -s localhost:8000/status/jobs | jq '.jobs[] | select(.name == "fundamentals_forward")'
```

After a merge that changes the job, restart the scheduler — it reads the registry only at start:

```bash
cd /home/ubuntu/stock-manager            # the scheduler's checkout, on the merged main
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart scheduler
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user status scheduler
```

## Retrying refusals after a parser fix

A parse refusal is recorded `FAILED` with its reason, and the document's bytes are already in L0
(the fetch succeeded; the parse did not). So when a parser change makes a refusal class parse —
the scheme spellings and the mistyped stated ISINs of the 2026-09-06 campaign, for example — the
recovery costs **no requests**: rerun the same window with the same feed. `PUBLISHED` units are
skipped, `FAILED` ones are retried, and `_ref_for` finds each document in L0 instead of fetching.
Do it on the machine that holds the L0 (the server, under the development model), after the
running driver has exited — never beside it, because both would write the same `filing_date`
partitions. Read the new report's failure section afterwards: what is still `FAILED` is a class the
change did not cover, and it should be named in `ops/BACKLOG.md` before the next season.
