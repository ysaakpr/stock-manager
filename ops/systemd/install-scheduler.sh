#!/usr/bin/env bash
# Install (or refresh) the scheduler user service and retire the single-job snapshot timer.
# Idempotent; needs no root. Run from the authoritative checkout after the change is on main.
#
# `enable-linger` keeps ubuntu's user manager — and so the scheduler — running without a login and
# across reboots (the same reason `install.sh` has it).

set -euo pipefail

unit_dir="$HOME/.config/systemd/user"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$unit_dir"
install -m 0644 "$here/scheduler.service" "$unit_dir/scheduler.service"

loginctl enable-linger "$(id -un)"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user daemon-reload
# The scheduler fires daily_snapshot itself; two schedulers for one job is how drift starts.
systemctl --user disable --now daily-snapshot.timer 2>/dev/null || true
systemctl --user enable --now scheduler.service

systemctl --user status scheduler.service --no-pager || true
