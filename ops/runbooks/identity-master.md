# Runbook — identity master (D2)

The identity master is the only thing in this system allowed to turn a symbol into an ISIN
(invariant #2). Every price row, every corporate action and every holding is keyed on what it
says. Two operator jobs live here: the weekly refresh, and clearing the reconciliation queue.

## Refresh the NSE master

Weekly. The two files are `EQUITY_L.csv` (today's listings) and `symbolchange.csv` (every rename
NSE has published). D1 fetches them into L0; this command reads files off disk and never opens a
socket.

```bash
uv run python -m dataplatform.identity.ingest \
  --equity-list  <path>/EQUITY_L.csv \
  --symbol-changes <path>/symbolchange.csv \
  --snapshot-date 2026-08-08 \
  --dry-run                      # drop --dry-run to commit
```

`--snapshot-date` is the date the snapshot *describes*, not the date you ran it. Omit it and the
injected clock's today is used, which is right for a same-day refresh and wrong for a re-run of
last week's file.

Expect, for a snapshot with nothing new in it: `0 changed, 0 windows inserted, 0 closed`. The
ingest is idempotent — re-running it does not restamp a single row — so a re-run is always safe
and is the first thing to try if you are unsure whether the last one landed.

Exit codes: `0` clean · `1` ingested, but the run was not clean (see below) · `2` the files did
not parse and nothing was written.

**Frozen copies** of both files live in `tests/fixtures/nse_equity_list/2026-08-08/` with their
provenance. Use them to reproduce a parse failure offline.

## Exit 1 — the run was not clean

Two things can make a run unclean, and they are printed to stderr.

### `AMBIGUOUS: …` — a symbol resolves to two ISINs

A row is now in `identity_reconciliation` and **nothing downstream will resolve that symbol on
those dates** — `IdentityMaster.resolve` raises rather than picking. That is deliberate: a wrong
ISIN silently merges two companies' histories and nothing downstream could detect it.

```sql
SELECT id, kind, exchange, on_date, symbols, isins, detected_by, source, detail
FROM identity_reconciliation WHERE NOT resolved ORDER BY on_date;
```

To resolve one you have to decide which claim is wrong, which means looking at the source files:

* **A recycled symbol with a bad date.** Most common. The old company's window should have closed
  before the new one opened; NSE's rename date is wrong, or the rename is missing from
  `symbolchange.csv` entirely. Fix the window by hand (below).
* **A genuine dual claim.** Two live securities with the same symbol on one exchange does not
  happen; if you are looking at one, the ISIN in one of the source rows is wrong. Check the ISIN
  against the exchange's own page before touching anything.

Correct the window, then mark the queue row resolved with what you decided:

```sql
-- close the older company's window the day before the newer one opens
UPDATE symbol_history SET valid_to = DATE '2009-12-31'
 WHERE isin = 'INE222B01012' AND exchange = 'NSE' AND symbol = 'ACME' AND valid_to IS NULL;

UPDATE identity_reconciliation
   SET resolved = true, resolved_at = now(), resolution = 'NSE rename date wrong; …'
 WHERE id = 42;
```

`resolution` is free text and is the only record of why. Write the reasoning, not "fixed".

The next ingest re-detects anything still ambiguous, so a wrong fix comes back rather than
sticking. A queue row is *not* re-created for a defect already recorded — the table's UNIQUE
constraint deduplicates it — so an untouched row and a re-detected one look the same; the
resolved ones are the audit trail.

### `refused: …` — the source disagrees with stored history

A window already closed in `symbol_history` is one the source now dates differently. The store
keeps what it has: a closed window is never moved or reopened, because a past date's meaning
would change under everything that has already resolved against it. Nothing is broken and the
rest of the ingest landed; decide whether the stored window or the new file is right, and if it
is the file, correct the row by hand as above.

## Health

* Every stored window names the file it came from: `symbol_history.source` is `nse_equity_list`
  for a current symbol and `nse_symbol_change` for a historical one.
* `IdentityIngestReport.clamped` lists securities whose oldest window could not be back-dated to
  their listing date, because NSE's `DATE OF LISTING` is the current entity's and post-dates the
  rename chain. Those symbols resolve to *unknown* before the clamp date rather than to a
  guessed ISIN. 52 of 2,886 windows in the 2026-08-08 snapshot; accumulating weekly snapshots is
  what closes them.
* `security_master` rows are never deleted, including delisted securities. A universe that can
  lose a dead security is survivorship-biased (§4.5). If you are tempted to clean one up, do not.
