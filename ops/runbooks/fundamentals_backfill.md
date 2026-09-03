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

There is **no scheduled job for this yet** — the scheduler runs `eod_pipeline` and
`constituents_snapshot` only. Until one exists, a forward sync is this command run over a recent
window, and there is one trap in doing that:

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

`0` clean or gracefully stopped · `2` cannot plan (bad range) · `3` parked on a 403 spike.

## Coverage report

Written to `ops/gates/M10-fundamentals-backfill-report.md`: index chunks published, filings
discovered vs in-universe vs published/failed, facts written, ISINs covered, entries skipped as out
of universe, and any park cause. Read it to see what a run covered and what it left.
