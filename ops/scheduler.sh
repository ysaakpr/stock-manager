#!/usr/bin/env bash
# The scheduler process's entry point under systemd (§8.1): `python -m dataplatform.scheduler run`.
#
# This is the process whose absence was the 2026-10-05 audit's root cause. `eod_pipeline` was
# registered at M1.10 and fired by nothing: the only unit ever installed was the daily-snapshot
# timer, which runs one job, so the bhavcopy family stopped at the last manual campaign and stayed
# stopped for three weeks. One long-running process fires every registered job from the registry's
# own crons — no second copy of the schedule to drift — and beats the heartbeat `/health` reads.
#
# Environment (set by the unit): SCHEDULER_REPO (default: this script's repo), DATA_ROOT (absolute,
# the authoritative lake), SNAPSHOT_EXPECT_LAKE_ROOT (derived; `daily_snapshot` refuses to fetch if
# it disagrees with DATA_ROOT). Exit 4: no usable `uv`.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="${SCHEDULER_REPO:-$here}"

: "${DATA_ROOT:=/home/ubuntu/stock-manager/data}"
export DATA_ROOT
export SNAPSHOT_EXPECT_LAKE_ROOT="${SNAPSHOT_EXPECT_LAKE_ROOT:-$DATA_ROOT/L0}"

# `uv` lives in ~/.local/bin, which is not on a systemd user unit's PATH (see daily-snapshot.sh).
uv_bin="${UV_BIN:-}"
if [[ -z "$uv_bin" ]]; then
    uv_bin="$(command -v uv || true)"
fi
if [[ -z "$uv_bin" && -x "$HOME/.local/bin/uv" ]]; then
    uv_bin="$HOME/.local/bin/uv"
fi
if [[ ! -x "$uv_bin" ]]; then
    echo "scheduler: no executable uv found (tried \$UV_BIN, PATH, ~/.local/bin/uv)." >&2
    exit 4
fi

cd "$repo"
echo "scheduler: repo=$repo DATA_ROOT=$DATA_ROOT uv=$uv_bin"
exec "$uv_bin" run python -m dataplatform.scheduler run
