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

## Resume and stop

- **Resume:** a `PUBLISHED` unit is never re-fetched — rerun the same command and it continues.
  Published index chunks are re-parsed from L0 (no socket) so ingest still sees their entries.
- **Graceful stop:** first `Ctrl-C` finishes the current unit and stops; second forces quit.
- **403 spike (hard stop):** the run **parks** with `ParkReason.FORBIDDEN_SPIKE`, records the
  tripping unit `FAILED` (non-retryable), and exits 3. Do **not** lower the rate, rotate the agent,
  or proxy around it (AGENTIC_CONTEXT §8) — understand why the host refused, then restart deliberately.

## Exit codes

`0` clean or gracefully stopped · `2` cannot plan (bad range) · `3` parked on a 403 spike.

## Coverage report

Written to `ops/gates/M10-fundamentals-backfill-report.md`: index chunks published, filings
discovered vs in-universe vs published/failed, facts written, ISINs covered, entries skipped as out
of universe, and any park cause. Read it to see what a run covered and what it left.
