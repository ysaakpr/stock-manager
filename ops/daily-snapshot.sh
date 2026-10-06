#!/usr/bin/env bash
# The daily snapshotter's entry point for an unattended schedule (OPS).
#
# Wraps `python -m dataplatform.scheduler run-once daily_snapshot` so the job goes through this
# repo's own `SchedulerRunner`: the Postgres advisory lock (two of these cannot run at once), the
# `job_run` row, and the `scheduler_heartbeat` upsert `/health` reads. A cron line or a systemd
# unit calling uv directly would get none of that.
#
# Two environment variables matter and both are set by the unit:
#
#   SNAPSHOT_REPO  the repo to run from. Defaults to this script's own repo, so running it by
#                  hand from a worktree does the right thing.
#   DATA_ROOT      the authoritative lake. Absolute, always. `SNAPSHOT_EXPECT_LAKE_ROOT` is
#                  derived from it and the job refuses to fetch if the two disagree — the guard
#                  against the failure that has already happened twice, a worker capturing into
#                  its own worktree's data/L0.
#
# Exit codes are `run-once`'s, so systemd's status is the job's: 0 succeeded, 1 failed or overran
# its budget, 2 the job is not registered in this checkout, 3 another process holds the lock. 4 is
# this script's own: no usable `uv`.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="${SNAPSHOT_REPO:-$here}"

: "${DATA_ROOT:=/home/ubuntu/stock-manager/data}"
export DATA_ROOT
export SNAPSHOT_EXPECT_LAKE_ROOT="${SNAPSHOT_EXPECT_LAKE_ROOT:-$DATA_ROOT/L0}"

# `uv` lives in ~/.local/bin, which is NOT on a systemd user unit's PATH
# (/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:...). Resolving it here rather than
# pinning PATH in the unit keeps the knowledge in the one place that needs it, and fails with a
# sentence instead of "127: uv: not found" in a log nobody is reading.
uv_bin="${UV_BIN:-}"
if [[ -z "$uv_bin" ]]; then
    uv_bin="$(command -v uv || true)"
fi
if [[ -z "$uv_bin" && -x "$HOME/.local/bin/uv" ]]; then
    uv_bin="$HOME/.local/bin/uv"
fi
if [[ ! -x "$uv_bin" ]]; then
    echo "daily-snapshot: no executable uv found (tried \$UV_BIN, PATH, ~/.local/bin/uv)." >&2
    echo "The host python is never used here (CLAUDE.md); set UV_BIN in the unit." >&2
    exit 4
fi

log_dir="${SNAPSHOT_LOG_DIR:-$HOME/snapshots}"
mkdir -p "$log_dir"
log="$log_dir/daily-snapshot-$(date +%Y-%m-%d).log"

{
    echo "── $(date -Is) ─────────────────────────────────────────────────────────────────"
    echo "repo=$repo"
    echo "DATA_ROOT=$DATA_ROOT"
    echo "SNAPSHOT_EXPECT_LAKE_ROOT=$SNAPSHOT_EXPECT_LAKE_ROOT"
    echo "uv=$uv_bin"
} >>"$log"

cd "$repo"
set +e
"$uv_bin" run python -m dataplatform.scheduler run-once daily_snapshot >>"$log" 2>&1
status=$?
set -e
echo "exit=$status  $(date -Is)" >>"$log"
exit "$status"
