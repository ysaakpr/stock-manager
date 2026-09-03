# Runbook — corporate-action backfill (M9.1)

Fills `corporate_actions` with real split/bonus/dividend/rights/merger terms for the names in the
L1 price window, reconciles NSE against BSE, and recomputes `adjustment_factors` (M2.4).

Entry point: `dataplatform/ingest/corp_actions_backfill.py` (`python -m` or the wired CLI).

## Preconditions

- Postgres up and migrated (`make up`, `make migrate`) — the runner reads the D2 identity master and
  writes `corporate_actions` / `adjustment_factors`.
- L1 `prices_raw` populated for the window (M1 backfill), since the universe of names to backfill CAs
  for is read from there.
- **The full 10-year run is B1-gated.** It is a bulk-fetch campaign (one request per BSE scrip);
  get the owner's go before running it live, exactly as for the NSE price backfill (M1.13).

## Dry run — plan only, no socket, no writes

(Reads the DB read-only to size the universe from `prices_raw`; opens no socket and writes nothing.)

```bash
uv run python -m dataplatform.ingest.corp_actions_backfill \
  --from 2016-09-01 --to 2026-09-30 --dry-run
```

Prints one line per fetch unit (NSE date chunks first, then BSE per-scrip) and the total count.
Use it to size the campaign before committing to it.

## Live run

```bash
uv run python -m dataplatform.ingest.corp_actions_backfill \
  --from 2016-09-01 --to 2026-09-30 \
  --report ops/gates/M9-ca-backfill-report.md
```

Options: `--limit N` runs a bounded sample of the plan (NSE chunks first); `--chunk-months N` sets
the NSE date-range chunk size (default 12).

It drives each unit `fetch -> L0 -> parse -> persist`, committing after every unit, then reconciles
and recomputes, then writes the coverage report. Exit code:

- **0** — completed (or gracefully stopped on `Ctrl-C`, which finishes the current unit first).
- **3** — **parked on a 403 spike.** The fetcher has refused the host for the life of the process.
  Do **not** lower the rate or rotate the agent (AGENTIC_CONTEXT §8). The report's `PARKED` section
  and stderr name the host and unit. Wait for the block to clear, then re-run — resume skips every
  already-`PUBLISHED` unit, so it continues from where it stopped.

## Resume

Resume is automatic and is read from `sync_state`: a `PUBLISHED` unit is never re-fetched, and a
retryable `FAILED` one is retried. Just re-run the same command; the committed rows are the
checkpoint.

## What lands where, and what does not

- Agreed actions (both feeds concur) → `corporate_actions` (`reconciled = true`) and, for
  ratio-bearing ones, `adjustment_factors` + an open `l2_invalidation` (rebuild L2 with M2.5).
- Disagreements → `quality_flag` (`GET /status/quality`) for a human; they never reach a factor.
- Unclassifiable purpose strings → the M2.1 manual-entry queue (surfaced in the report count).
- Unresolvable identities (a scrip/ISIN the master does not know) → the report's unresolved count;
  fix the identity data (D2), not the CA terms.
