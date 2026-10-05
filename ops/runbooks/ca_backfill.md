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

## After the backfill: the scheduled refresh

The backfill is a one-shot campaign; keeping the store current is `dataplatform/ingest/ca_refresh.py`,
run by the scheduler:

| Job | When (IST) | What |
|---|---|---|
| `ca_refresh` | Saturday 10:00 | NSE, last 35 days of ex-dates (1 request) + one BSE request per scrip whose NSE action has no BSE twin yet |
| `bse_ca_sweep` | 06:00, first Sunday of the month | the same, plus every BSE scrip that traded in the last year (~6,700 requests) |

Both reconcile under `ACCEPT` — the policy the lake was finalized with (`identity.lineage_rebuild`);
reconcile is whole-set, so a `QUEUE` run here would un-reconcile every single-feed action already
admitted — recompute only the ISINs whose reconciled actions changed, and drain `l2_invalidation`
through `rebuild_invalidated`.

The refresh keys its NSE unit `nse_corp_actions/refresh` on the **refresh date**, not the window
start: the backfill's last chunk (`nse_corp_actions` 2026-09-01) is PUBLISHED, so re-running the
backfill `--from 2026-09-01` resume-skips the window. To catch up by hand, use the refresh:

```bash
uv run python -m dataplatform.ingest.ca_refresh --from 2026-09-01 --skip-l2   # fetch, reconcile, recompute
uv run python -m dataplatform.store.l2_fill --rebuild-invalidated              # rebuild what moved
uv run python -m dataplatform.quality.l2_continuity                            # verify
```

Exit codes: `0` clean; `1` a unit FAILED or the fetch parked on a 403 spike (what landed is still
finalized — re-run after the cause is cleared; PUBLISHED units are skipped); `2` an invalid window
(over twelve months is the backfill's job).
