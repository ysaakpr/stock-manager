# M9.1 — Corporate-action backfill over the price window

Status: **runner built and verified offline; the 10-year live execution is B1-gated.**

This report is written at build time. The `--dry-run`/live runner
(`dataplatform/ingest/corp_actions_backfill.py`) overwrites it with real coverage counts when the
bulk fetch is run — see the runbook (`ops/runbooks/ca_backfill.md`). The runner's own
`render_report` emits the same shape this file describes.

## What was built

A resumable, checkpointed corporate-action backfill runner that lands real split/bonus/dividend
terms so the M2.4 adjustment engine has something to work with (the gap noted in `ops/BACKLOG.md`,
M2.2: nothing wrote CA rows).

- **`fetch -> L0 -> parse -> persist`** per fetch unit, driven through the §4.4 sync-state machine,
  mirroring `nse/fii_dii.py:ingest_day` and `backfill.py`'s `SourceSet`/`BackfillRunner` loop.
- **Per-date NSE, per-scrip BSE.** The NSE `corporates-corporateActions` feed takes a
  `from_date`/`to_date` range, so the NSE plan is a handful of date chunks across the window; the
  BSE `DefaultData/w` feed takes a `scripcode`, so the BSE plan is one unit per BSE scrip present in
  `prices_raw`, resolved from ISIN through the D2 identity master (invariant #2).
- **Reconcile + recompute finalize.** After the units land, `finalize_reconcile_and_recompute`
  reconciles the two exchanges' descriptions of each action (M2.3) and runs the M2.4
  `recompute_isins` for each agreed ISIN, so `adjustment_factors` is populated for every name with a
  ratio-bearing, agreed-upon action, and its L2 is flagged stale.
- **Resumable / checkpointed.** Resume reads `sync_state`: a `PUBLISHED` unit is never re-fetched,
  and the runner commits after every unit, so a kill loses at most the unit in flight.
- **A 403 spike parks, it does not hammer.** The fetcher's spike hard stop ends the whole run,
  which is recorded as a non-retryable `FAILED` row and surfaced on the report with an **enumerated
  cause** (`ParkReason.FORBIDDEN_SPIKE`) and the unit it stopped on — never a silent skip, never a
  lowered rate or a rotated agent (AGENTIC_CONTEXT §8, like M1.13's gated bulk run).
- **No validator weakened.** An unclassifiable purpose string goes to M2.1's manual-entry queue and
  an unresolvable identity to the unresolved list; a genuine ratio/ex-date disagreement goes to
  M2.3's reconciliation queue (`quality_flag`). None reach a factor, and none are dropped.

## Offline verification (the acceptance criteria)

`uv run pytest tests/unit/test_ca_backfill.py -q` — all pass, no socket, no Postgres:

1. `corporate_actions` holds real split/bonus/dividend rows keyed by ISIN, from both feeds, and the
   recompute leaves `adjustment_factors` non-empty for the ratio-bearing names (the split and the
   bonus each produce a factor row) — `test_backfill_lands_reconciles_and_recomputes`.
2. The runner is resumable: a second run re-fetches nothing and publishes nothing —
   `test_resume_skips_published_units`.
3. A 403 spike parks with an enumerated cause and leaves later units untouched —
   `test_403_spike_parks_with_enumerated_cause`.
4. Nothing is dropped (queued / unresolved surfaced) and the plan is pure and offline —
   `test_unresolved_and_queued_are_surfaced`, `test_plan_fills_both_url_shapes`,
   `test_dry_run_is_offline`.

## Why the live 10-year run is not executed here

Backfilling CA terms for every name in `prices_raw` over 2016-09..2026-09 is a bulk-fetch campaign
(the BSE leg alone is one request per scrip, thousands of them). B1 (AGENTIC_CONTEXT §2/§3.3)
reserves any campaign over ~200 requests to one source to the owner, exactly as the NSE price
backfill (M1.13) is `NEEDS_GO`. The runner is built and unit-verified; a human gives the go, then
`ca-backfill --from 2016-09-01 --to 2026-09-30` fills this report with the real coverage counts.
