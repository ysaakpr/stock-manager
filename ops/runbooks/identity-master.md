# Runbook — identity master (D2)

The identity master is the only thing in this system allowed to turn a symbol into an ISIN
(invariant #2). Every price row, every corporate action and every holding is keyed on what it
says. Two operator jobs live here: the weekly refresh, and clearing the reconciliation queue.

## Refresh the NSE master

Weekly, and it is now a scheduled job — `identity_refresh`, 07:00 IST on Saturday. The two files
are `EQUITY_L.csv` (today's listings) and `symbolchange.csv` (every rename NSE has published). One
command fetches both into L0 and re-derives the master by reading them back out:

```bash
uv run python -m dataplatform.ingest.identity_refresh
```

It takes the `nsearchives.nseindia.com` lease first, so it refuses to run beside a campaign
(`dataplatform.ingest.lease`), and it re-fetches nothing L0 already holds for the date — a re-run
on the same day is a no-op.

To re-derive without fetching — after a database restore, or to reproduce a past snapshot — read
the payloads straight back out of L0:

```bash
uv run python -m dataplatform.identity.ingest --from-l0 2026-08-08 --dry-run
```

The path form still exists for a file you have in your hand:

```bash
uv run python -m dataplatform.identity.ingest \
  --equity-list  <path>/EQUITY_L.csv \
  --symbol-changes <path>/symbolchange.csv \
  --snapshot-date 2026-08-08 \
  --dry-run                      # drop --dry-run to commit
```

> **Until 2026-09-06 the first paragraph was wrong.** It said "D1 fetches them into L0" and D1
> never had: there was no `data/L0/nse_equity_list/` tree, `symbolchange.csv` had no Source
> Register row, and the master every ISIN join depends on was built from the frozen copies in
> `tests/fixtures/` — outside L0's checksums, outside the backup, and outside invariant #1. The
> `--from-l0` path above is what closes that; the fixtures below are fixtures again.

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
* **An ISIN reissued under the same symbol** (face-value split; M18.1). The ingest splits these
  itself when it has evidence for the switch date. The old ISIN's window closes the day before
  the switch and the new one starts on it. Evidence is an `isin_lineage` edge, else the first
  dated `EQUITY_L_YYYYMMDD.csv` in L0 that shows the new ISIN where the capture immediately
  before it showed the old. Series evidence counts only within one issuer code (`isin[:7]`), and
  a capture without the symbol breaks it. The L0 bhavcopy of that date must also trade the
  symbol as the new ISIN, because the ~19:15 IST capture can list the next session's ISIN a day
  early. A still-queued conflict between two ISINs of one issuer (`INE887D01016` /
  `INE887D01024`) means that evidence was missing or disagreed. Once the switch session is
  confirmed, add the edge (`isin_lineage`, `detected_by = 'MANUAL'`), then re-run
  `identity.ingest --from-l0`. Do not hand-edit windows. Two *different* issuer codes under one
  symbol are another company taking a vacated symbol, not a reissue; treat it as a recycled
  symbol (above). The ingest leaves alone a new-ISIN window that is already stored from the
  listing date. That one-off state is what `repair_reissues` exists for (below).
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

## 2026-10-10: repair the seven reissue windows (M18.1, one-off)

The 07:00 IST `identity_refresh` on Sat 2026-10-10 ran before the reissue split existed and stored
seven new-ISIN windows from the original listing date beside the still-open old ones
(TDPOWERSYS, KIRLPNU, CORDELIA, TCC, TAALTECH, BLSE, BUILDPRO; reconciliation ids 21-27). Nothing
the ingest does will delete those rows. Do this on the server after the M18.1 merge, **before
Mon 2026-10-12 18:30 IST** (`eod_pipeline`'s delivery unit fails every session on TDPOWERSYS until
then) and in any case **before Sat 2026-10-17 07:00 IST**.

**1. Put the fixed code under the scheduler first.** The scheduler reads code at start. If it is
still running pre-fix code, the next `identity_refresh` re-derives the listing-date windows and
re-inserts them, undoing the repair.

```bash
cd /home/ubuntu/stock-manager
git pull --ff-only && uv sync
# Only when no job is running. This must return no rows:
#   SELECT job_name, started_at FROM job_run WHERE state = 'RUNNING';
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart scheduler.service
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user status scheduler.service
```

**2. Repair and re-derive** (from `/home/ubuntu/stock-manager`):

```bash
uv run python -m dataplatform.identity.repair_reissues           # dry run, READ ONLY: read the plan
uv run python -m dataplatform.identity.repair_reissues --apply   # locks both tables, one transaction
uv run python -m dataplatform.identity.ingest --from-l0 2026-10-10   # re-derive; no network
```

* The dry run must show 7 deletes, 7 closes and 7 inserts. Boundaries: `isin_lineage` for
  TDPOWERSYS, KIRLPNU, TCC and CORDELIA; `nse_equity_list_series` for TAALTECH, BLSE and BUILDPRO.
  It must resolve the INGEST rows 21-27 and the RESOLVE rows for the same pairs (29-31 on
  2026-10-10, plus any that delivery or deals add before you run it). It must show **one row left
  open: id 28, RESOLVE BLSE 2026-09-08**. Anything else and it refuses, writing nothing.
* **Row 28 stays open on purpose.** ca_refresh held back BLSE's ₹0.50 dividend (ex-date
  2026-09-08) on this ambiguity. That ex-date has left every future scheduled ca_refresh window,
  and the open row is the only visible record that it was never filed. It stays open until an
  offline re-file from L0 exists. Do not file it with a fetching `ca_refresh` run: that can also
  send BSE requests, and BSE is parked on 403.
* Re-running `--apply` is a no-op ("already applied").
* The re-derive must report `0 windows inserted, 0 closed`. It still exits 1 and prints ten
  `AMBIGUOUS` lines. Those are the BSE scrip-id collisions (ids 1-10), which predate this repair.
  Re-detected conflicts are not re-queued.
* Use `identity.ingest --from-l0`, not `ingest.identity_refresh`: the latter fetches when it is
  run on a day whose files are not in L0 yet.

**3. Check the next scheduled refresh** (Sat 2026-10-17 07:00 IST). It will be `FAILED` while the
ten BSE collisions stand, but it must not have moved a window:

```sql
SELECT state, error FROM job_run WHERE job_name = 'identity_refresh'
 ORDER BY started_at DESC LIMIT 1;
-- error: "identity refresh 2026-10-17: … 0 windows inserted, 0 closed, 10 conflict(s)"
```

A reissue that happened during the week is the exception: it adds one close and one insert,
dated by its evidence, and no conflict. Anything inserted for one of the seven symbols means the
scheduler was not restarted onto the fix. Stop and re-run step 2's dry run.

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
