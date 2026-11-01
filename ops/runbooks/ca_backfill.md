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

### The drain never extends history (M13.8)

L1 reaches back to 2006 while L2 partitions start at 2011-06-22. The drain (`rebuild_invalidated`,
run by both jobs above and by `l2_fill --rebuild-invalidated`) rebuilds a queued ISIN that already
has a partition **from that partition's first date** and drops every L1 bar before it, its lineage
predecessors' included. Each rebuild logs `l2.rebuild_floor` with `history_floor` and `policy`:

- `existing_partition_start`: the partition's first date was kept as the floor.
- `first_time_fill`: there was no partition yet, so the ISIN is built over its whole L1 history,
  exactly as `l2_fill` (no flags, `materialize_missing`) would build it.

Reaching back into older L1 is only ever done on purpose, with `l2_fill --extend --dry-run` first
and then `l2_fill --extend`, followed by `l2_continuity`. A partition whose first date moved
earlier after a Saturday refresh, with no `--extend` run in between, is a regression of this rule.

Exit codes: `0` clean; `1` a unit FAILED or the fetch parked on a 403 spike (what landed is still
finalized — re-run after the cause is cleared; PUBLISHED units are skipped); `2` an invalid window
(over twelve months is the backfill's job).

## The share-basis guard: splits and consolidations the feeds miss (M2.3, 2026-11-01)

Owner ruling 2026-11-01 (HUMAN_DECISIONS, "system should have a way"): a split or consolidation
no feed carries is adjusted automatically when the evidence is sufficient, and otherwise kept away
from every decision and shown on Ops. Nobody is expected to look at the data. The case that forced
it: AvenuesAI's Re 1 -> Rs 10 consolidation (2026-10-13). NSE's bc file stated the terms and the
record date with a **blank ex-date**, NSE's CA API (queried by ex-date) never returned it, BSE is
blocked, and nothing promoted bc lines into factors.

### The derived ex-date rule (`corpactions/ex_date_rule.py`)

`T1_RECORD_DATE`: **ex-date = record date**, only for a record date that is a trading session on
or after **2023-01-27** (every stock settled T+1 from then; every row of the same bundles that
states both dates shows ex = record). Never before 2023-01-27 — T+2 rules varied — and never from
a non-session or a date the calendar does not cover. Where it applies:

| Source | Where the rule is recorded |
|---|---|
| NSE bc file (L1 `pr_ca_broadcasts`) | `ex_date` filled, `ex_date_derived = 'T1_RECORD_DATE'` |
| NSE CA feed (blank `exDate` on a face-value change) | `CorporateAction.ex_date_rule` (log `nse_corp_actions.ex_date_derived`) |
| Curated `manual_actions.yaml` | `ex_date_derived: T1_RECORD_DATE` instead of `ex_date` (never both) |
| The guard's admitted factor | `ex_date_rule` in `<L2>/share_basis/state.json` |

### What the guard does (`quality/share_basis.py`, job `share_basis_guard`)

Runs 19:40 IST weekdays (after the EOD pipeline and snapshot, before the paper session and M17)
and 11:30 IST Saturday (after `ca_refresh`). About 30 s. Each run:

1. **bc lines** stating a face-value change since T+1, resolved to the ISIN traded that session.
   Ex-date stated, or derived by the rule. A line a recorded or curated split/bonus within 7 days
   already covers is left to it.
2. **Reissues** (D2 lineage edges) whose two ISINs' face values differ in the identity master.
3. A factor is **admitted** only when L1's close-to-close step across the event agrees with the
   stated ratio within **0.8-1.25**. One daily band either way. The nearest wrong reading of any
   stated ratio is at least 2x away, so the band cannot admit the wrong one. An admitted factor is
   composed into L2 like a curated row (source `nse_pr_bundle_bc` or `share_basis_reissue`), and
   yields to any feed or curated split/bonus within 7 days. Exactly the partitions whose admitted
   set changed are rebuilt.
4. The **continuity scan** then runs over L2 with the `SHARE_BASIS` class: a step across a
   reissue (any gap), or across a 6-30 day gap at a clean split multiple (`implied.py`'s
   candidates and bands), that nothing explains.
5. **Fail closed.** Every `SHARE_BASIS` step, and every stated ratio the price contradicts, is
   **quarantined**. The ISIN's bars before the step are withheld by
   `dataplatform.query.default_price_quarantine`, the same predicate as D22, in every
   decision-facing reader (QueryService, the backtest readers, the Commons, the paper and M17
   desks). Each is an **ERROR** on `/status/quality` (`l2_continuity` kind `share_basis_step`,
   or `share_basis_guard` for a contradicted ratio) naming the ISIN, the step and the reason.
   The 52 the first full-history sweep raised (steps dated before 2026-11-01) show as ACCEPTED
   (amber) with a remark, by the owner's ruling D33 (2026-11-02, `d33-*` in
   `dataplatform/status/accepted_deviations.yaml`); a step dated on or after 2026-11-01 stays an
   ERROR. Display only: those bars stay quarantined.
6. **Clearing is automatic.** When a feed or curated row later explains the step, the next run
   drops the quarantine and closes the ERROR.

Never adjusted from price alone: a suspension can hide a real move.

```bash
uv run python -m dataplatform.quality.share_basis --dry-run --report /tmp/guard.json  # decide, write nothing
uv run python -m dataplatform.quality.share_basis                                     # what the job runs
cat data/L2/share_basis/state.json                                                    # admitted + quarantined
```

The state file is derived (L0 bc lines + L1 prices + identity tables) and safe to delete. The next
run rewrites it, and until then nothing is admitted or quarantined by the guard. Do not run the
guard by hand between 18:30 and 00:30 IST on a trading day. It may rebuild L2 partitions.

### To fix one by hand anyway

A curated `manual_actions.yaml` row with its bc citation, then `l2_fill --rebuild <survivor>`. The
guard's next run sees it as recorded, so the quarantine and the ERROR clear on their own. A step
that is a genuine move (a relisting after years of suspension) can be an `explained_moves`
`MARKET_MOVE` row; that is also what clears it.
