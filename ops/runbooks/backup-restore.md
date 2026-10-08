# Runbook — backup and restore

Two layers. **The nightly one (M15.4)** is two scheduler jobs and one drill command, all in
`dataplatform/store/backup.py`; it is what protects the platform day to day and what the
real-money readiness checklist (item e) rests on. **The by-hand one (M0.7)** is `ops/backup.sh` +
`ops/restore.sh`, kept for an operator who wants a full backup directory with an L0 fingerprint
before something structural; its drills from 2026-08-08 are further down.

## Nightly, scheduled (M15.4)

| Job | IST | What | Budget |
|---|---|---|---|
| `postgres_backup` | 05:30 daily | `pg_dump -Fc --no-owner --no-privileges` → `~/backups/postgres/trading-<stamp>.dump` + `.json` sidecar, then retention | 30 min |
| `l0_backup` | 05:45 daily | extend `~/backups/l0/MANIFEST.sha256` with every new L0 file; rsync L0 to `BACKUP_L0_MIRROR` if set | 1 h |

Why 05:30: every writer of the day has finished — the paper session (21:45), `nse_daily_capture`
(23:00), `announcements_capture` (00:30) and `fundamentals_forward` (02:00, own deadline 04:45) —
and it is clear of the 19:15–21:50 evening jobs and Saturday's 09:00–11:00 refreshes. A dump is an
MVCC snapshot, so a straggling writer is consistent, just not included.

Both are ordinary jobs: a failure is a FAILED `job_run`, shown on `GET /status/jobs` and paged by
`failure_alerts` within 15 minutes, like any other job. By hand, the same code:

```bash
uv run python -m dataplatform.store.backup postgres   # dump now + retention
DATA_ROOT=/home/ubuntu/stock-manager/data uv run python -m dataplatform.store.backup l0
uv run python -m dataplatform.store.backup drill      # restore newest dump into a scratch container
```

Run the `l0` command with `DATA_ROOT` pointing at the real lake when you are in a worktree — a
worktree's own `data/` has no L0 and the command refuses it ("no lake at …").

### Settings (`.env`, all optional)

| Key | Default | Meaning |
|---|---|---|
| `BACKUP_ROOT` | `~/backups` | dumps under `postgres/`, the L0 manifest under `l0/`; must be absolute or `~`-prefixed |
| `BACKUP_KEEP_DAILY` | 14 | newest dump of each of the last 14 days that have one |
| `BACKUP_KEEP_WEEKLY` | 8 | plus the newest of each of the last 8 ISO weeks |
| `BACKUP_PG_CLIENT_IMAGE` | `postgres:16` | image whose `pg_dump`/`pg_restore` run on the host network (the host has no libpq client); empty = tools on `PATH` |
| `BACKUP_L0_MIRROR` | unset | rsync destination for L0 (second disk path or `host:path`) |
| `RESTORE_DRILL_DATABASE_URL` | unset | an empty throwaway database for the drill; unset = a scratch container per drill |

Retention counts *days that have a backup*, not calendar days, so a fortnight of failed runs does not
age every good dump out at once; the newest dump is always kept; nothing not named
`trading-<stamp>.dump` is ever pruned. Expect 14 + up to 8 dumps (~11 MB each on 2026-10-08).

### Credentials

The live DSN is `DATABASE_URL`; its password reaches `pg_dump`/`pg_restore` only as `PGPASSWORD` in
the child's environment, and `docker run -e PGPASSWORD` passes the variable *by name*, so the value
is never in an argv (`ps`), a log line or a sidecar. Logs and sidecars name the target as
`host:port/db`. A failing tool's stderr is redacted before it reaches the job error.
`RESTORE_DRILL_DATABASE_URL` is a `SecretStr`.

### The restore drill

`python -m dataplatform.store.backup drill` takes the newest dump (or `--dump PATH`), checks its
sha256 against the sidecar, then restores it with `pg_restore --exit-on-error` into a **throwaway**
database: `RESTORE_DRILL_DATABASE_URL` if set, otherwise a `postgres:16` container started for the
drill on a random `127.0.0.1` port with a random password, removed afterwards (`--keep` leaves it).
It then compares:

- the restored `schema_migrations` with the ledger recorded at dump time, and reports it against the
  migrations in this checkout;
- the counts of `schema_migrations`, `sync_state`, `job_run`, `decision_journal`, `paper_session`,
  `paper_session_resolution`, `security_master` with the sidecar's. Counts are taken just before
  the dump and every one of these tables only grows, so restored < recorded fails; restored > recorded
  is writes that landed in between.

**It refuses the live database** — exit 2, before any byte is restored — when the target's host
(any loopback spelling), port and database name match `DATABASE_URL`. It never stops, alters or
restarts the live `trading-platform-postgres-1` container. Exit 0 pass, 1 a check failed, 2 refused.

Run it monthly and after anything structural; add a line to the table at the end of this file.

### L0: manifest yes, second copy only with an owner decision

`l0_backup` keeps a cumulative `sha256sum`-format manifest, checkable from the lake root:

```bash
cd /home/ubuntu/stock-manager/data && sha256sum -c --quiet ~/backups/l0/MANIFEST.sha256
```

L0 is write-once, so only files the manifest has not seen are hashed each night (megabytes); the
first run hashed the whole lake. A recorded file that is no longer on disk fails the job — that is an
invariant-#1 incident and repairing it is the owner's (AGENTIC_CONTEXT §3.10). Re-hashing old bytes
is the weekly `l0_verify` sweep's job, not this one.

**There is no second copy of L0 today.** This host has one disk (`/dev/root`, 193 GB, the lake
~10 GB) and no remote target, so `BACKUP_L0_MIRROR` is unset and the job logs
`backup.l0_mirror_unconfigured` every night. Choosing a target — a second EBS volume, an S3 bucket
via a mounted path, or another host reachable by rsync — is an owner decision (a spending decision,
AGENTIC_CONTEXT §3.9). Once chosen, set `BACKUP_L0_MIRROR` and the nightly job starts copying with
`rsync -a --ignore-existing` (never `--delete`). The same is true of the Postgres dumps: they sit on
the same disk as the database, which protects against a bad migration or a dropped table, not against
losing the disk. A nightly copy of `~/backups/postgres/` to the same target closes that too.

### Scheduler start guard

`python -m dataplatform.scheduler run` (and `run-once`) refuse to start — exit 5, `scheduler.refused`
in the log, a CRITICAL alert, the reason on stderr — while any file in
`dataplatform/store/migrations` is not applied, or an applied one was edited. The unit has
`RestartPreventExitStatus=5`, so systemd leaves it `failed` rather than restarting into the same
refusal, and the heartbeat going stale turns `/health` 503 within five minutes. The fix is always:

```bash
make migrate
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart scheduler.service
```

After merging a PR that adds a migration, migrate **before** restarting the scheduler.

## By hand: ops/backup.sh and ops/restore.sh (M0.7)

```bash
make backup     # bash ops/backup.sh
make restore    # bash ops/restore.sh --scratch
```

`ops/backup.sh` writes a Postgres dump plus a checksummed fingerprint of L0 into
`ops/backups/<ts>/` (gitignored, inside the checkout — move anything you want to keep to
`~/backups/`); `ops/restore.sh` proves that dump restores and that every row count survived. §8.1
required the drill to have been executed at least once for the M0 gate — the transcripts below are
that run, on 2026-08-08, pasted verbatim.

### What an M0.7 backup contains

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

#### The gap: nothing leaves this host

**L0 is fingerprinted, not copied, and no backup is uploaded anywhere.** §8.1 calls for
object-storage backup of L0 + Postgres dumps; no target exists yet (a bucket is a spending decision,
AGENTIC_CONTEXT §3.9). Until one does:

- a disk failure loses both the lake and every backup of it;
- what the manifest buys today is *detection* — a restore drill proves the L0 recorded yesterday is
  byte-identical to the L0 on disk now, which is how a silent corruption or a partial `rsync` gets
  caught while the source can still be re-fetched.

Closing it is one task when a target is chosen: upload `ops/backups/<ts>/` and mirror `DATA_ROOT/L0`,
then extend the drill to restore *from the remote copy*. `ops/BACKLOG.md` carries the line.

Nothing prunes `ops/backups/`; delete old directories by hand. (The nightly dumps under
`~/backups/postgres/` are pruned by the job.)

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
DATABASE_URL=postgresql://trading:trading@localhost:5433/trading_drill \
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
able to overwrite production with last night's dump. A genuine recovery is a human at a terminal.
From a nightly dump, step 2 is `uv run python -m dataplatform.store.backup drill --dump
~/backups/postgres/trading-<stamp>.dump` and step 3 reads that file instead; stop the scheduler
(`systemctl --user stop scheduler.service`) alongside the app.

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

## Drill log

The nightly jobs above are the schedule (M15.4). Run the restore drill monthly — `uv run python -m
dataplatform.store.backup drill` — and add a dated line here. M0.7's script drill (`make restore`)
remains valid for an `ops/backups/` directory.

| Date | Backup | Result | Recovery time |
|---|---|---|---|
| 2026-08-08 | `20260808T190102` (live, 17 tables / 3 rows) | pass | 1.5 s |
| 2026-08-08 | `20260808T190155` (seeded, 17 tables / 13 rows, 2 L0 files) | pass | 1.5 s |
| 2026-10-08 | `~/backups/postgres/trading-20261008T085052.dump` (live, 11.3 MB; scratch `postgres:16` container) | pass — 7 key tables match, 14/14 migrations | 3.0 s restore, 5.4 s drill |
