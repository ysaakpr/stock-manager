#!/usr/bin/env bash
#
# ops/restore.sh — the restore drill: rebuild a backup into a scratch database and prove it.
#
# What it does, in the order that matters:
#   1. checks the backup against its own SHA256SUMS, so a corrupt archive is caught before it is
#      trusted rather than after it has been restored;
#   2. re-checks every file the L0 manifest recorded against the lake on disk;
#   3. creates a scratch database, `pg_restore`s the dump into it, and compares the exact row count
#      of every table with the counts recorded at dump time;
#   4. drops the scratch database and reports the elapsed recovery time.
#
# What it assumes: the compose stack is up (`make up`); pg_restore runs inside the postgres
# container for the same reason ops/backup.sh dumps there.
#
# What it never does: restore over the live database. This is a *drill* — it proves the backup is
# restorable, and the target is always a scratch name, which the script refuses to let equal the
# database the container serves. A real recovery is a deliberate, human-run pg_restore documented
# in ops/runbooks/backup-restore.md; the point of that asymmetry is that no automated run, and no
# fat-fingered flag, can overwrite production with last night's dump.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT

COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/ops/docker-compose.yml}"
PG_SERVICE="${PG_SERVICE:-postgres}"
DATA_ROOT="${DATA_ROOT:-$REPO_ROOT/data}"
BACKUP_ROOT="${BACKUP_ROOT:-$REPO_ROOT/ops/backups}"

readonly DUMP_FILE="postgres.dump"
readonly COUNTS_FILE="row_counts.tsv"
readonly L0_MANIFEST="l0_manifest.sha256"
readonly SUMS_FILE="SHA256SUMS"

# The same query ops/backup.sh recorded `row_counts.tsv` with. Duplicated deliberately rather than
# sourced from a third file: the two scripts are the two sides of the comparison, and each has to
# be readable on its own by whoever is running a recovery at 2 a.m.
readonly ROW_COUNT_SQL="
SELECT format('%s %s', t.table_name,
              (xpath('/row/c/text()',
                     query_to_xml(format('SELECT count(*) AS c FROM %I.%I',
                                         t.table_schema, t.table_name),
                                  false, true, '')))[1]::text::bigint)
FROM information_schema.tables t
WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
ORDER BY t.table_name;
"

usage() {
    cat <<'USAGE'
usage: bash ops/restore.sh [--scratch] [--backup DIR] [--db NAME] [--keep] [--no-l0]

  --scratch      restore into a scratch database and verify (the default, and the only mode)
  --backup DIR   the backup to restore; default is the newest under ops/backups/
  --db NAME      name of the scratch database; default trading_restore_<label>
  --keep         leave the scratch database behind for inspection
  --no-l0        skip the L0 manifest re-check (for restoring on a host with no lake)
  -h, --help     this text

environment: COMPOSE_FILE, PG_SERVICE, DATA_ROOT, BACKUP_ROOT
USAGE
}

die() {
    printf 'restore.sh: %s\n' "$*" >&2
    exit 1
}

if command -v sha256sum >/dev/null 2>&1; then
    SHA_CMD=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
    SHA_CMD=(shasum -a 256)
else
    die "neither sha256sum nor shasum is on PATH; the backup cannot be verified"
fi

pg() { docker compose -f "$COMPOSE_FILE" exec -T "$PG_SERVICE" sh -c "$1"; }

require_plain_dbname() {
    printf '%s' "$1" | grep -Eq '^[a-z][a-z0-9_]{0,62}$' \
        || die "refusing database name ${1:-<empty>}: expected lower_snake, 1-63 chars"
}

# Drop the scratch database however this script ends, so a failed drill does not leave a
# half-restored database behind to be mistaken for a good one. `--keep` opts out. Scratch files go
# to a temp directory, never into the backup: a backup directory is evidence and stays read-only.
SCRATCH_DB=""
WORK_DIR=""
KEEP=0
cleanup() {
    if [ -n "$SCRATCH_DB" ] && [ "$KEEP" -eq 0 ]; then
        pg "dropdb -U \"\$POSTGRES_USER\" --if-exists --force '$SCRATCH_DB'" >/dev/null 2>&1 || true
    fi
    if [ -n "$WORK_DIR" ]; then
        rm -rf "$WORK_DIR"
    fi
}
trap cleanup EXIT

main() {
    local backup_dir="" scratch_db="" check_l0=1
    while [ $# -gt 0 ]; do
        case "$1" in
            # Accepted and required to be a no-op: the verify command names the mode explicitly,
            # and a future non-scratch mode must be a new flag, never a change of what this means.
            --scratch) shift ;;
            --backup)
                backup_dir="${2:-}"
                [ -n "$backup_dir" ] || die "--backup needs a directory"
                shift 2
                ;;
            --db)
                scratch_db="${2:-}"
                [ -n "$scratch_db" ] || die "--db needs a name"
                shift 2
                ;;
            --keep) KEEP=1; shift ;;
            --no-l0) check_l0=0; shift ;;
            -h|--help) usage; return 0 ;;
            *) usage >&2; die "unknown argument: $1" ;;
        esac
    done

    command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
    [ -f "$COMPOSE_FILE" ] || die "no compose file at $COMPOSE_FILE"
    # A failed probe has two different causes that look identical from the exit code alone: the
    # service really is down, or this user cannot reach the docker daemon at all (e.g. missing
    # from the `docker` group) and every docker command fails the same way regardless of what is
    # running. Capturing docker's own message and keying off it tells the two apart instead of
    # asserting the more common one.
    local probe
    if ! probe="$(pg 'true' 2>&1)"; then
        if printf '%s' "$probe" | grep -qi 'docker.sock\|cannot connect to the docker daemon'; then
            die "cannot reach the docker daemon — this is a docker/permissions problem, not '$PG_SERVICE' being down: $probe"
        fi
        die "the '$PG_SERVICE' service is not running or not reachable — start it with 'make up' first: $probe"
    fi

    local started
    started=$SECONDS
    WORK_DIR="$(mktemp -d)"

    # ── which backup ─────────────────────────────────────────────────────────────────────────
    if [ -z "$backup_dir" ]; then
        # Labels are TZ-fixed `%Y%m%dT%H%M%S`, so newest is greatest lexicographically and no
        # stat(1) portability question arises.
        [ -d "$BACKUP_ROOT" ] || die "no backups in $BACKUP_ROOT — run ops/backup.sh first"
        backup_dir="$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
            | LC_ALL=C sort | tail -1)"
        [ -n "$backup_dir" ] || die "no backups in $BACKUP_ROOT — run ops/backup.sh first"
    fi
    backup_dir="$(cd "$backup_dir" 2>/dev/null && pwd)" || die "no such backup directory"
    local file
    for file in "$DUMP_FILE" "$COUNTS_FILE" "$L0_MANIFEST" "$SUMS_FILE"; do
        [ -f "$backup_dir/$file" ] || die "$backup_dir is not a backup: $file is missing"
    done
    printf 'restore  backup=%s\n' "$backup_dir"

    # ── 1. is the backup itself intact ───────────────────────────────────────────────────────
    ( cd "$backup_dir" && "${SHA_CMD[@]}" -c "$SUMS_FILE" ) \
        || die "$backup_dir fails its own checksums — this backup is not restorable"

    # ── 2. does the L0 manifest still describe the lake ──────────────────────────────────────
    local recorded=0
    recorded="$(wc -l < "$backup_dir/$L0_MANIFEST" | tr -d ' ')"
    if [ "$check_l0" -eq 0 ]; then
        printf 'L0       skipped (--no-l0), %s files were recorded\n' "$recorded"
    elif [ "$recorded" -eq 0 ]; then
        printf 'L0       manifest is empty — the lake held no files when this backup ran\n'
    else
        verify_l0 "$backup_dir/$L0_MANIFEST" "$recorded"
    fi

    # ── 3. restore into a scratch database ───────────────────────────────────────────────────
    local live_db
    live_db="$(pg 'printf %s "$POSTGRES_DB"')"
    if [ -z "$scratch_db" ]; then
        scratch_db="trading_restore_$(basename "$backup_dir" | tr -cd 'a-z0-9_')"
        scratch_db="$(printf '%s' "$scratch_db" | cut -c1-63)"
    fi
    require_plain_dbname "$scratch_db"
    [ "$scratch_db" != "$live_db" ] \
        || die "$scratch_db is the live database; this script only ever restores into a scratch one"
    SCRATCH_DB="$scratch_db"

    printf 'scratch  %s\n' "$scratch_db"
    # client_min_messages, not 2>/dev/null: --if-exists emits a NOTICE for the usual case of the
    # scratch database not being there, and swallowing the whole stream would swallow real errors.
    pg "PGOPTIONS='-c client_min_messages=WARNING' \
        dropdb -U \"\$POSTGRES_USER\" --if-exists --force '$scratch_db'" >/dev/null
    pg "createdb -U \"\$POSTGRES_USER\" -O \"\$POSTGRES_USER\" '$scratch_db'"

    # --exit-on-error, because a restore that reports success having skipped half the objects is
    # the failure mode this whole drill exists to rule out.
    pg "pg_restore -U \"\$POSTGRES_USER\" -d '$scratch_db' --no-owner --no-privileges \
        --exit-on-error" < "$backup_dir/$DUMP_FILE" || die "pg_restore failed"

    # ── 4. do the counts match ───────────────────────────────────────────────────────────────
    local observed="$WORK_DIR/restored_counts"
    printf '%s\n' "$ROW_COUNT_SQL" \
        | pg "psql -U \"\$POSTGRES_USER\" -d '$scratch_db' -qAtX -v ON_ERROR_STOP=1 -f -" \
        > "$observed" || die "counting rows in $scratch_db failed"

    # A whole-file diff, not a per-table lookup: it catches a table the restore never created and
    # a table the dump should not have contained, as well as a count that moved.
    diff -u "$backup_dir/$COUNTS_FILE" "$observed" \
        || die "restored row counts differ from the manifest (- recorded, + restored, above)"

    local tables rows
    tables="$(wc -l < "$observed" | tr -d ' ')"
    rows="$(awk '{ total += $2 } END { print total + 0 }' "$observed")"

    printf 'verified %s tables, %s rows, every count matches row_counts.tsv\n' "$tables" "$rows"
    if [ "$KEEP" -eq 1 ]; then
        printf 'kept     %s (drop it with: make psql then DROP DATABASE %s)\n' \
            "$scratch_db" "$scratch_db"
    fi
    printf 'ok       restore drill passed, recovery time %ss\n' "$((SECONDS - started))"
}

# Re-check every path the manifest recorded. Files added since the backup are expected — the lake
# only grows — so they are counted and reported, not failed on. A recorded file that is missing or
# whose bytes changed is a breach of invariant #1 and fails the drill loudly.
verify_l0() {
    local manifest="$1" recorded="$2"
    [ -d "$DATA_ROOT/L0" ] \
        || die "the manifest records $recorded files but $DATA_ROOT/L0 does not exist"

    local report="$WORK_DIR/l0_check" status=0
    ( cd "$DATA_ROOT" && "${SHA_CMD[@]}" -c "$manifest" ) > "$report" 2>&1 || status=$?
    if [ "$status" -ne 0 ]; then
        printf 'L0       FAILED — files recorded by this backup no longer match the lake:\n'
        grep -v ': OK$' "$report" | head -50 || true
        die "L0 is immutable (invariant #1); a changed or missing payload is an incident, and \
repairing it is reserved to the owner (AGENTIC_CONTEXT §3.10)"
    fi

    local present added
    present="$(find "$DATA_ROOT/L0" -type f | wc -l | tr -d ' ')"
    added=$((present - recorded))
    printf 'L0       %s recorded files re-hashed and unchanged (%s added since)\n' \
        "$recorded" "$added"
}

main "$@"
