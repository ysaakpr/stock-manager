#!/usr/bin/env bash
#
# ops/backup.sh — one backup: a Postgres dump plus a checksummed manifest of L0.
#
# What it does: creates `ops/backups/<ts>/` holding a pg_dump custom-format archive of the
# platform database, the exact row count of every public table as of that dump, a sha256 manifest
# of every file in the L0 lake, a metadata document, and a `SHA256SUMS` covering all four.
#
# What it assumes: the compose stack is up (`make up`). `pg_dump` runs *inside* the postgres
# container — the server is 16.x, this host has no libpq client at all, and a client older than
# its server refuses to dump. Credentials come from the container's own environment, so this keeps
# working when the compose defaults are overridden (the same trick as `make psql`).
#
# What it never does: write into an existing backup directory, and never touch L0. The lake is
# immutable (invariant #1) and is *fingerprinted*, not copied: a decade of bhavcopies does not
# belong beside a nightly dump, and until an object-storage target exists there is nowhere off-host
# to put either. That gap is the first thing ops/runbooks/backup-restore.md says.
#
# Restoring and verifying is ops/restore.sh. This script only produces the evidence.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT

# Overridable so the drill, the integration test and a second stack can all run this unedited.
# DATA_ROOT matches the Makefile's default, which is compose's `../data` made absolute.
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/ops/docker-compose.yml}"
PG_SERVICE="${PG_SERVICE:-postgres}"
DATA_ROOT="${DATA_ROOT:-$REPO_ROOT/data}"
# Where `<ts>` directories accumulate. ops/restore.sh reads the same variable, so pointing one
# somewhere else (a mounted disk, a test's tmp_path) moves both halves of the drill together.
BACKUP_ROOT="${BACKUP_ROOT:-$REPO_ROOT/ops/backups}"

# Files inside a backup directory, in the order they are produced. SHA256SUMS covers every one of
# them, so nothing in a backup is unprotected except SHA256SUMS itself.
readonly DUMP_FILE="postgres.dump"
readonly COUNTS_FILE="row_counts.tsv"
readonly L0_MANIFEST="l0_manifest.sha256"
readonly META_FILE="backup.json"
readonly SUMS_FILE="SHA256SUMS"

# Exact per-table counts, one `name count` line per public base table, ordered by name.
# `count(*)` cannot be parameterised over a table name, and `n_live_tup` is an estimate that drifts
# with autovacuum — an estimate would make the restore check meaningless. `query_to_xml` runs a
# real count per table inside one statement. ops/restore.sh runs this same query against the
# restored database; the comparison is only meaningful while both sides count the same way.
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
usage: bash ops/backup.sh [--output DIR] [--db NAME]

  --output DIR   write the backup here instead of $BACKUP_ROOT/<ts>
  --db NAME      dump this database instead of the container's POSTGRES_DB
  -h, --help     this text

environment: COMPOSE_FILE, PG_SERVICE, DATA_ROOT, BACKUP_ROOT,
             BACKUP_TS (fixes the directory name)
USAGE
}

die() {
    printf 'backup.sh: %s\n' "$*" >&2
    exit 1
}

# sha256sum on Linux, shasum on a stock macOS. Identical output format ("<hash>  <path>") and
# identical -c semantics, which is what lets ops/restore.sh check a manifest written on either.
if command -v sha256sum >/dev/null 2>&1; then
    SHA_CMD=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
    SHA_CMD=(shasum -a 256)
else
    die "neither sha256sum nor shasum is on PATH; a backup without checksums is not a backup"
fi

# Run a shell snippet inside the postgres container with stdin and stdout wired straight through,
# so a custom-format archive streams into a host file without a tty mangling it.
pg() { docker compose -f "$COMPOSE_FILE" exec -T "$PG_SERVICE" sh -c "$1"; }

# A database name is interpolated into that snippet, so it must not be able to close the quoting.
# Postgres allows far more than this in a quoted identifier; this platform's databases are
# lower-snake, and rejecting the rest is cheaper than getting the escaping right forever.
require_plain_dbname() {
    printf '%s' "$1" | grep -Eq '^[a-z][a-z0-9_]{0,62}$' \
        || die "refusing database name ${1:-<empty>}: expected lower_snake, 1-63 chars"
}

# Escape a value for backup.json. Only backslash and quote can break the document, and every value
# written there is a path, an identifier or a number.
json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

main() {
    local dest="" source_db=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --output)
                dest="${2:-}"
                [ -n "$dest" ] || die "--output needs a directory"
                shift 2
                ;;
            --db)
                source_db="${2:-}"
                [ -n "$source_db" ] || die "--db needs a name"
                shift 2
                ;;
            -h|--help) usage; return 0 ;;
            *) usage >&2; die "unknown argument: $1" ;;
        esac
    done

    command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
    [ -f "$COMPOSE_FILE" ] || die "no compose file at $COMPOSE_FILE"
    pg 'true' >/dev/null 2>&1 \
        || die "the '$PG_SERVICE' service is not running — start it with 'make up' first"

    # The container is the authority on which database it serves. Asking it beats re-deriving the
    # DSN from .env, which is exactly the drift that ends with a backup of the wrong database.
    local live_db
    live_db="$(pg 'printf %s "$POSTGRES_DB"')"
    [ -n "$live_db" ] || die "the $PG_SERVICE container has no POSTGRES_DB set"
    [ -n "$source_db" ] || source_db="$live_db"
    require_plain_dbname "$source_db"

    local started label
    started=$SECONDS
    # A local-time label, because an operator reading `ops/backups/` is thinking in IST; the
    # unambiguous instant with its offset goes into backup.json. BACKUP_TS fixes it for tests.
    label="${BACKUP_TS:-$(TZ=Asia/Kolkata date +%Y%m%dT%H%M%S)}"
    [ -n "$dest" ] || dest="$BACKUP_ROOT/$label"

    # mkdir without -p on the final component: a second backup in the same second, or a rerun
    # pointed at an existing directory, must fail rather than half-overwrite a good backup.
    mkdir -p "$(dirname "$dest")"
    mkdir "$dest" 2>/dev/null || die "$dest already exists — backups are never overwritten"
    dest="$(cd "$dest" && pwd)"

    printf 'backup   database=%s dest=%s\n' "$source_db" "$dest"

    # ── the dump ─────────────────────────────────────────────────────────────────────────────
    # Custom format: compressed, and pg_restore can read it selectively. --no-owner/--no-privileges
    # so the archive also restores onto a cluster that has never heard of the `trading` role, which
    # is the shape a real recovery takes.
    pg "pg_dump -U \"\$POSTGRES_USER\" -d '$source_db' -Fc --no-owner --no-privileges" \
        > "$dest/$DUMP_FILE" || die "pg_dump failed; $dest is incomplete and must not be trusted"
    [ -s "$dest/$DUMP_FILE" ] || die "pg_dump produced an empty archive"

    # ── row counts: the thing a restore is checked against ───────────────────────────────────
    printf '%s\n' "$ROW_COUNT_SQL" \
        | pg "psql -U \"\$POSTGRES_USER\" -d '$source_db' -qAtX -v ON_ERROR_STOP=1 -f -" \
        > "$dest/$COUNTS_FILE" || die "counting rows failed"
    [ -s "$dest/$COUNTS_FILE" ] \
        || die "no public tables in $source_db — has it been migrated (make migrate)?"

    # ── L0 manifest ──────────────────────────────────────────────────────────────────────────
    # Paths are relative to DATA_ROOT and keep the `L0/` component, so the manifest is checkable
    # with a plain `sha256sum -c` from wherever the lake is mounted. Sidecars are hashed too: a
    # payload whose .meta.json vanished is as broken as a payload whose bytes changed.
    : > "$dest/$L0_MANIFEST"
    local l0_files=0 listing="$dest/.l0-files"
    if [ -d "$DATA_ROOT/L0" ]; then
        ( cd "$DATA_ROOT" && find L0 -type f -print | LC_ALL=C sort ) > "$listing"
        l0_files="$(wc -l < "$listing" | tr -d ' ')"
        if [ "$l0_files" -gt 0 ]; then
            # Batched through xargs rather than one process per file: ten years of daily files
            # across nine sources is ~10^5 payloads, and a fork each would dominate the runtime.
            ( cd "$DATA_ROOT" && tr '\n' '\0' < "$listing" | xargs -0 -n 256 "${SHA_CMD[@]}" ) \
                > "$dest/$L0_MANIFEST"
        fi
        rm -f "$listing"
        local hashed
        hashed="$(wc -l < "$dest/$L0_MANIFEST" | tr -d ' ')"
        [ "$hashed" = "$l0_files" ] \
            || die "hashed $hashed of $l0_files L0 files — the manifest is incomplete"
    fi

    # ── metadata ─────────────────────────────────────────────────────────────────────────────
    local created_at server_version dump_bytes table_count total_rows l0_disk_kib
    created_at="$(TZ=Asia/Kolkata date +%Y-%m-%dT%H:%M:%S%z)"
    server_version="$(pg 'psql -U "$POSTGRES_USER" -d postgres -qAtX -c "SHOW server_version"')"
    dump_bytes="$(wc -c < "$dest/$DUMP_FILE" | tr -d ' ')"
    table_count="$(wc -l < "$dest/$COUNTS_FILE" | tr -d ' ')"
    total_rows="$(awk '{ total += $2 } END { print total + 0 }' "$dest/$COUNTS_FILE")"
    # An if, not `[ -d … ] && …`: under `set -e` a failing test as the last statement of an AND
    # list is a failing command, and an absent lake would abort the backup it was reporting on.
    l0_disk_kib=0
    if [ -d "$DATA_ROOT/L0" ]; then
        l0_disk_kib="$(du -sk "$DATA_ROOT/L0" | awk '{print $1}')"
    fi

    cat > "$dest/$META_FILE" <<JSON
{
  "label": "$(json_escape "$label")",
  "created_at": "$(json_escape "$created_at")",
  "source_database": "$(json_escape "$source_db")",
  "server_version": "$(json_escape "$server_version")",
  "dump_format": "custom",
  "dump_bytes": $dump_bytes,
  "table_count": $table_count,
  "total_rows": $total_rows,
  "data_root": "$(json_escape "$DATA_ROOT")",
  "l0_files": $l0_files,
  "l0_disk_kib": $l0_disk_kib,
  "generator": "ops/backup.sh"
}
JSON

    # ── checksums over everything above ──────────────────────────────────────────────────────
    ( cd "$dest" && "${SHA_CMD[@]}" "$DUMP_FILE" "$COUNTS_FILE" "$L0_MANIFEST" "$META_FILE" ) \
        > "$dest/$SUMS_FILE"

    printf 'dump     %s bytes\n' "$dump_bytes"
    printf 'tables   %s (%s rows total)\n' "$table_count" "$total_rows"
    printf 'L0       %s files fingerprinted (not copied — see ops/runbooks/backup-restore.md)\n' \
        "$l0_files"
    printf 'checksum %s\n' "$dest/$SUMS_FILE"
    printf 'ok       backup complete in %ss\n' "$((SECONDS - started))"
}

main "$@"
