# Runbook — backup and restore

Two scripts and one drill. `ops/backup.sh` writes a Postgres dump plus a checksummed fingerprint of
L0; `ops/restore.sh` proves that dump restores and that every row count survived. §8.1 requires the
drill to have been executed at least once for the M0 gate — the transcripts below are that run, on
2026-08-08, pasted verbatim.

```bash
make backup     # bash ops/backup.sh
make restore    # bash ops/restore.sh --scratch
```

## What a backup contains

`ops/backups/<ts>/`, where `<ts>` is `%Y%m%dT%H%M%S` in IST:

| File | What it is |
|---|---|
| `postgres.dump` | `pg_dump -Fc --no-owner --no-privileges` of the platform database |
| `row_counts.tsv` | exact `count(*)` per public table at dump time — what the restore is checked against |
| `l0_manifest.sha256` | sha256 of every file in L0, payloads and `.meta.json` sidecars alike, relative to `DATA_ROOT` |
| `backup.json` | label, instant with offset, source database, server version, sizes, file counts |
| `SHA256SUMS` | checksums of the four files above |

Both scripts run `pg_dump`/`pg_restore`/`psql` **inside the postgres container**. That is not a
stylistic choice: the server is 16.x, this host has no libpq client at all, and a client older than
its server refuses to dump. Credentials come from the container's own environment, so overriding the
compose defaults does not break either script.

Environment both scripts honour: `COMPOSE_FILE`, `PG_SERVICE`, `DATA_ROOT`, `BACKUP_ROOT`.

### The gap: nothing leaves this host

**L0 is fingerprinted, not copied, and no backup is uploaded anywhere.** §8.1 calls for
object-storage backup of L0 + Postgres dumps; no target exists yet (a bucket is a spending decision,
AGENTIC_CONTEXT §3.9). Until one does:

- a disk failure loses both the lake and every backup of it;
- what the manifest buys today is *detection* — a restore drill proves the L0 recorded yesterday is
  byte-identical to the L0 on disk now, which is how a silent corruption or a partial `rsync` gets
  caught while the source can still be re-fetched.

Closing it is one task when a target is chosen: upload `ops/backups/<ts>/` and mirror `DATA_ROOT/L0`,
then extend the drill to restore *from the remote copy*. `ops/BACKLOG.md` carries the line.

Nothing prunes old backups either. `ops/backups/` is gitignored; delete old directories by hand.

## Drill 1 — the nightly path, executed 2026-08-08

Preconditions, unchanged from `ops/README.md`:

```
$ docker compose -f ops/docker-compose.yml ps --format 'table {{.Service}}\t{{.Status}}'
SERVICE    STATUS
app        Up 40 minutes (healthy)
postgres   Up 41 minutes (healthy)

$ uv run python -m dataplatform.store.migrate
applied 0002_status_surface.sql
applied 0003_scheduler.sql
```

Backup:

```
$ time bash ops/backup.sh
backup   database=trading dest=/Users/vysh/Documents/work/stocks/ops/backups/20260808T190102
dump     71216 bytes
tables   17 (3 rows total)
L0       0 files fingerprinted (not copied — see ops/runbooks/backup-restore.md)
checksum /Users/vysh/Documents/work/stocks/ops/backups/20260808T190102/SHA256SUMS
ok       backup complete in 0s
bash ops/backup.sh  0.25s user 0.21s system 42% cpu 1.072 total
```

What it wrote:

```
$ cat ops/backups/20260808T190102/backup.json
{
  "label": "20260808T190102",
  "created_at": "2026-08-08T19:01:02+0530",
  "source_database": "trading",
  "server_version": "16.14 (Debian 16.14-1.pgdg13+1)",
  "dump_format": "custom",
  "dump_bytes": 71216,
  "table_count": 17,
  "total_rows": 3,
  "data_root": "/Users/vysh/Documents/work/stocks/data",
  "l0_files": 0,
  "l0_disk_kib": 0,
  "generator": "ops/backup.sh"
}

$ cat ops/backups/20260808T190102/SHA256SUMS
ae99d8bd2f81476c44c8f0cf82aafd62a3587fb1dea3d49ffe401d5dc9e27813  postgres.dump
2a93da43ac9407350c50a2bcc1624f9be71cacdc29cc5c774cb585b1aa296787  row_counts.tsv
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  l0_manifest.sha256
0993660a0c38354282d7250cbc0d9a0e46d2333786733d0700aae586b6bf62da  backup.json
```

Restore drill (no argument: it picks the newest backup):

```
$ time bash ops/restore.sh --scratch
restore  backup=/Users/vysh/Documents/work/stocks/ops/backups/20260808T190102
postgres.dump: OK
row_counts.tsv: OK
l0_manifest.sha256: OK
backup.json: OK
L0       manifest is empty — the lake held no files when this backup ran
scratch  trading_restore_20260808190102
verified 17 tables, 3 rows, every count matches row_counts.tsv
ok       restore drill passed, recovery time 1s
bash ops/restore.sh --scratch  0.33s user 0.25s system 38% cpu 1.500 total
```

**Recovery time observed: 1.5 s wall clock** for a 71 kB archive over 17 tables — checksum
verification, database creation, `pg_restore`, re-count and drop, end to end. Read it as the fixed
overhead, not as a projection: today the database holds 3 rows and L0 is empty, because M1's backfill
has not run. The manifest re-hash is the part that will grow with the lake (a full ten-year L0 is
~10⁵ files), and `du` on `DATA_ROOT/L0` in `backup.json` is the number to watch. Re-run this drill
after the backfill and record the new figure here.

Also note `schema_migrations 3` in `row_counts.tsv` below — that, not the empty tables, is what makes
the count comparison non-vacuous in drill 1. Drill 2 exists because "all zeros equal all zeros" is a
weak thing to hang a gate on.

## Drill 2 — with real rows and a populated lake, executed 2026-08-08

Same two scripts, pointed at a seeded database and a scratch lake, so the counts and the manifest are
both non-trivial. Reproducible as written:

```bash
export DRILL=/tmp/m0.7-drill
mkdir -p "$DRILL/lake/L0/nse_bhavcopy/2026/08"
printf 'SYMBOL,SERIES,CLOSE\nRELIANCE,EQ,1500.25\n' \
  > "$DRILL/lake/L0/nse_bhavcopy/2026/08/cm07AUG2026bhav.csv"
printf '{"source": "nse_bhavcopy"}\n' \
  > "$DRILL/lake/L0/nse_bhavcopy/2026/08/cm07AUG2026bhav.csv.meta.json"

docker compose -f ops/docker-compose.yml exec -T postgres \
  sh -c 'createdb -U "$POSTGRES_USER" trading_drill'
POSTGRES_HOST=localhost POSTGRES_PORT=5433 POSTGRES_USER=trading \
POSTGRES_PASSWORD="$POSTGRES_PASSWORD" POSTGRES_DB=trading_drill \
  uv run python -m dataplatform.store.migrate
# then 3 security_master, 2 case_ and 5 decision_journal rows via psql
```

```
$ DATA_ROOT=$DRILL/lake BACKUP_ROOT=$DRILL/backups bash ops/backup.sh --db trading_drill
backup   database=trading_drill dest=/tmp/m0.7-drill/backups/20260808T190155
dump     70928 bytes
tables   17 (13 rows total)
L0       2 files fingerprinted (not copied — see ops/runbooks/backup-restore.md)
checksum /tmp/m0.7-drill/backups/20260808T190155/SHA256SUMS
ok       backup complete in 1s

$ cat $DRILL/backups/*/row_counts.tsv
adjustment_factors 0
archive_bundle 0
case_ 2
corporate_actions 0
decision_journal 5
exchange_listing 0
job_run 0
order_ 0
policy_set 0
quality_flag 0
scheduler_heartbeat 0
schema_migrations 3
security_master 3
symbol_history 0
sync_state 0
thesis 0
token_usage 0

$ cat $DRILL/backups/*/l0_manifest.sha256
15e81dde85add61ba5ad2a07225ef467c8bd6ada37cf18d9403494a07069b631  L0/nse_bhavcopy/2026/08/cm07AUG2026bhav.csv
2f668fa1731f8408ee5908351e59c09151995e5cdfa641842fb1c0d870ce16b8  L0/nse_bhavcopy/2026/08/cm07AUG2026bhav.csv.meta.json

$ time DATA_ROOT=$DRILL/lake BACKUP_ROOT=$DRILL/backups \
       bash ops/restore.sh --scratch --db trading_drill_restore
restore  backup=/tmp/m0.7-drill/backups/20260808T190155
postgres.dump: OK
row_counts.tsv: OK
l0_manifest.sha256: OK
backup.json: OK
L0       2 recorded files re-hashed and unchanged (0 added since)
scratch  trading_drill_restore
verified 17 tables, 13 rows, every count matches row_counts.tsv
ok       restore drill passed, recovery time 1s
...  0.33s user 0.23s system 37% cpu 1.513 total
```

13 seeded rows in, 13 out, per table.

## Drill 3 — the failure paths, executed 2026-08-08

A check that has never been seen to fail is not a check. Both of these were run against the drill-2
backup, after resealing `SHA256SUMS` so the *first* gate would not fire and mask the second.

Wrong row count:

```
$ sed -i "" "s/^decision_journal 5$/decision_journal 6/" $DRILL/tampered/row_counts.tsv
$ (cd $DRILL/tampered && shasum -a 256 postgres.dump row_counts.tsv l0_manifest.sha256 backup.json > SHA256SUMS)
$ DATA_ROOT=$DRILL/lake bash ops/restore.sh --scratch --backup $DRILL/tampered --db trading_drill_restore
restore  backup=/tmp/m0.7-drill/tampered
postgres.dump: OK
row_counts.tsv: OK
l0_manifest.sha256: OK
backup.json: OK
L0       2 recorded files re-hashed and unchanged (0 added since)
scratch  trading_drill_restore
--- /tmp/m0.7-drill/tampered/row_counts.tsv	2026-08-08 19:02:05
+++ /var/folders/.../restored_counts	2026-08-08 19:02:06
@@ -2,7 +2,7 @@
 archive_bundle 0
 case_ 2
 corporate_actions 0
-decision_journal 6
+decision_journal 5
 exchange_listing 0
 job_run 0
 order_ 0
restore.sh: restored row counts differ from the manifest (- recorded, + restored, above)
exit=1
```

An L0 payload edited after it was fingerprinted:

```
$ printf 'SYMBOL,SERIES,CLOSE\nRELIANCE,EQ,9999.99\n' \
    > $DRILL/lake/L0/nse_bhavcopy/2026/08/cm07AUG2026bhav.csv
$ DATA_ROOT=$DRILL/lake bash ops/restore.sh --scratch --backup $DRILL/backups/<ts> --db trading_drill_restore
restore  backup=/tmp/m0.7-drill/backups/20260808T190155
postgres.dump: OK
row_counts.tsv: OK
l0_manifest.sha256: OK
backup.json: OK
L0       FAILED — files recorded by this backup no longer match the lake:
sha256sum: WARNING: 1 computed checksum did NOT match
L0/nse_bhavcopy/2026/08/cm07AUG2026bhav.csv: FAILED
restore.sh: L0 is immutable (invariant #1); a changed or missing payload is an incident, and repairing it is reserved to the owner (AGENTIC_CONTEXT §3.10)
exit=1
```

A corrupt `postgres.dump` fails earlier still, at the `SHA256SUMS` gate, before anything is restored.
`tests/integration/test_backup_restore.py` runs all three failure paths plus the happy path on every
`make check`.

## Real recovery — not what restore.sh does

`ops/restore.sh` **only ever restores into a scratch database** and refuses a target named the same as
the live one. That asymmetry is deliberate: the drill runs unattended, and no unattended run should be
able to overwrite production with last night's dump. A genuine recovery is a human at a terminal:

```bash
# 1. stop the app so nothing writes while the database is being replaced
docker compose -f ops/docker-compose.yml stop app

# 2. prove the backup first — never restore an archive you have not verified
bash ops/restore.sh --scratch --backup ops/backups/<ts>

# 3. restore into a fresh database beside the live one
docker compose -f ops/docker-compose.yml exec -T postgres \
  sh -c 'createdb -U "$POSTGRES_USER" trading_recovered'
docker compose -f ops/docker-compose.yml exec -T postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d trading_recovered --no-owner --no-privileges --exit-on-error' \
  < ops/backups/<ts>/postgres.dump

# 4. look at it before you commit to it
docker compose -f ops/docker-compose.yml exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d trading_recovered -qAtX -c "SELECT count(*) FROM decision_journal"'

# 5. swap: rename the old database aside, rename the new one into place, restart the app
#    (ALTER DATABASE ... RENAME TO needs no other session connected)
docker compose -f ops/docker-compose.yml start app
```

Steps 3 and 4 were executed as part of this drill and are known to work as written:

```
$ pg_restore ... -d trading_recovered ... < <backup>/postgres.dump
exit=0
$ psql -d trading_recovered -c "SELECT count(*) FROM decision_journal"
5
```

Step 5 is written out rather than scripted on purpose — renaming the live database is the one
irreversible move in this procedure.

L0 needs no recovery step: it is on the host filesystem, outside every container, and the whole point
of `l0_manifest.sha256` is to tell you whether it is still intact. If it is not, the missing payloads
are re-fetchable from the sources (that is what makes L0 recoverable at all) — and deleting or
rewriting what is left of it is reserved to the owner, AGENTIC_CONTEXT §3.10, with no exception for
"it looked corrupt".

## Scheduling

Not scheduled yet. Once M0.6's scheduler owns the daily pipeline, the backup belongs at the end of
it, after ingestion and quality have run. Until then, run `make backup` by hand before anything
structural — a migration, a compose change, a `docker compose down -v`.

The monthly restore drill is a continuous track in EXECUTION_PLAN §11 with no gate: run `make
restore` once a month and add a dated line to the table below.

| Date | Backup | Result | Recovery time |
|---|---|---|---|
| 2026-08-08 | `20260808T190102` (live, 17 tables / 3 rows) | pass | 1.5 s |
| 2026-08-08 | `20260808T190155` (seeded, 17 tables / 13 rows, 2 L0 files) | pass | 1.5 s |
