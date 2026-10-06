#!/usr/bin/env bash
# Install (or refresh) the daily-snapshotter user timer. Idempotent; needs no root.
#
# `enable-linger` is the part that makes it survive a reboot: without it ubuntu's user manager
# only exists while ubuntu is logged in, so the timer would silently stop at the next restart —
# which for these sources means losing days of history nobody can buy back.

set -euo pipefail

unit_dir="$HOME/.config/systemd/user"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$unit_dir"
install -m 0644 "$here/daily-snapshot.service" "$unit_dir/daily-snapshot.service"
install -m 0644 "$here/daily-snapshot.timer" "$unit_dir/daily-snapshot.timer"

loginctl enable-linger "$(id -un)"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user daemon-reload
systemctl --user enable --now daily-snapshot.timer

systemctl --user list-timers daily-snapshot.timer --all
