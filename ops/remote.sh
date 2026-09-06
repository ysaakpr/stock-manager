#!/usr/bin/env bash
# The server is the testing engine; the laptop is the coding engine (CLAUDE.md "Development model").
#
#   ops/remote.sh status              # server: commit, dirty files, running drivers, disk
#   ops/remote.sh sync                # push-verified fast-forward of the server checkout to origin
#   ops/remote.sh check               # sync, then `make check` on the server (format, lint, types, tests)
#   ops/remote.sh test [pytest args]  # sync, then `uv run pytest <args>` on the server
#   ops/remote.sh run <command...>    # sync, then any command in the server's repo directory
#   ops/remote.sh shell               # interactive shell in the server's repo directory
#   ops/remote.sh logs [pattern]      # tail the newest ~/campaign log, optionally filtered
#
# Connection details live ONLY in the untracked, gitignored `.remote.env` at the repo root:
#
#   REMOTE_HOST=…      REMOTE_USER=…      REMOTE_KEY=~/.ssh/…      REMOTE_REPO=/home/…/stock-manager
#
# Copy `.remote.env.example` and fill it in. This script never reads the key file — it hands the
# *path* to ssh with `-i` — and nothing in it or in any committed file names the host, the user or
# the key. If `.remote.env` is missing or incomplete the script says which keys are missing and exits
# 2; the caller (a person or an agent) then decides whether to continue on the laptop instead.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".remote.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "remote: no $ENV_FILE at the repo root — the server is not configured on this machine." >&2
  echo "        Copy .remote.env.example to $ENV_FILE and fill in REMOTE_HOST, REMOTE_USER," >&2
  echo "        REMOTE_KEY (a path — never its contents) and REMOTE_REPO. Until then, work runs locally." >&2
  exit 2
fi
# shellcheck disable=SC1090
. "./$ENV_FILE"
missing=""
for key in REMOTE_HOST REMOTE_USER REMOTE_KEY REMOTE_REPO; do
  eval "value=\${$key:-}"
  [ -n "$value" ] || missing="$missing $key"
done
if [ -n "$missing" ]; then
  echo "remote: $ENV_FILE is incomplete — missing:$missing" >&2
  exit 2
fi
# Expand a leading ~ in the key path without ever opening the file.
case "$REMOTE_KEY" in "~/"*) REMOTE_KEY="$HOME/${REMOTE_KEY#\~/}" ;; esac
if [ ! -r "$REMOTE_KEY" ]; then
  echo "remote: key path $REMOTE_KEY is not readable on this machine" >&2
  exit 2
fi

SSH=(ssh -i "$REMOTE_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20
     -o ServerAliveInterval=30 "$REMOTE_USER@$REMOTE_HOST")
remote() {  # run one command line in the server's repo directory, with uv on PATH
  "${SSH[@]}" "cd '$REMOTE_REPO' && export PATH=\"\$HOME/.local/bin:\$PATH\" && $*"
}

cmd_status() {
  remote 'echo "commit:  $(git log --oneline -1)";
          echo "branch:  $(git rev-parse --abbrev-ref HEAD)";
          echo "dirty:   $(git status --short | wc -l) file(s)";
          echo "drivers: $(pgrep -fc "^[^ ]*python[^ ]* -m dataplatform\.ingest" || true) running";
          echo "docker:  $(docker ps --format "{{.Names}} {{.Status}}" 2>/dev/null | tr "\n" ";")";
          echo "disk:    $(df -h . | tail -1 | awk "{print \$4\" free of \"\$2}")"'
}

cmd_sync() {
  # The server pulls; it never receives uncommitted or unpushed work. Refuse to "sync" a HEAD that
  # origin does not have — that is the one way the two machines drift.
  local branch head
  branch="$(git rev-parse --abbrev-ref HEAD)"
  head="$(git rev-parse HEAD)"
  git fetch -q origin "$branch" || true
  if ! git merge-base --is-ancestor "$head" "origin/$branch" 2>/dev/null; then
    echo "remote: local $branch ($(git rev-parse --short HEAD)) is not on origin yet — push first" >&2
    echo "        ($(git rev-list --count "origin/$branch..$branch" 2>/dev/null || echo '?') unpushed commit(s))" >&2
    exit 3
  fi
  if [ -n "$(git status --short)" ]; then
    echo "remote: note — the laptop tree has uncommitted changes; the server will run the pushed HEAD only" >&2
  fi
  # A running campaign driver has its modules imported already, so a pull cannot disturb it — but
  # `uv sync` rewriting the venv under it can. If the lock changed while a driver runs, leave the
  # environment alone and say so; the caller re-syncs once the driver is done.
  remote "old=\$(git rev-parse HEAD); git fetch -q origin && git checkout -q '$branch' \
          && git pull -q --ff-only origin '$branch' || exit \$?;
          drivers=\$(pgrep -fc '^[^ ]*python[^ ]* -m dataplatform\\.ingest' || true);
          if [ \"\$drivers\" -gt 0 ] && ! git diff --quiet \"\$old\" HEAD -- uv.lock pyproject.toml; then
            echo 'remote: uv.lock changed but a driver is running — skipped uv sync; re-run sync after it finishes' >&2;
          else uv sync -q; fi;
          echo \"server at \$(git log --oneline -1)\$( [ \"\$drivers\" -gt 0 ] && echo \" (\$drivers driver(s) running)\" )\""
}

cmd_check() { cmd_sync; remote "make check"; }
cmd_test()  { cmd_sync; remote "uv run pytest $*"; }
cmd_run()   { cmd_sync; remote "$*"; }
cmd_shell() { "${SSH[@]}" -t "cd '$REMOTE_REPO' && export PATH=\"\$HOME/.local/bin:\$PATH\" && exec \$SHELL -l"; }
cmd_logs()  {
  local pattern="${1:-}"
  remote "L=\$(ls -t ~/campaign/*.log 2>/dev/null | head -1); [ -n \"\$L\" ] || { echo 'no campaign logs'; exit 0; };
          echo \"log: \$L\";
          echo \"pages \$(grep -c index_published \$L), published \$(grep -c filing_published \$L), reused \$(grep -c filing_l0_reused \$L), failed \$(grep -c unit_failed \$L)\";
          if [ -n '$pattern' ]; then grep '$pattern' \$L | tail -20; else grep -v crawl.spacing \$L | tail -5 | cut -c1-200; fi"
}

case "${1:-}" in
  status) cmd_status ;;
  sync)   cmd_sync ;;
  check)  cmd_check ;;
  test)   shift; cmd_test "$@" ;;
  run)    shift; cmd_run "$@" ;;
  shell)  cmd_shell ;;
  logs)   shift; cmd_logs "$@" ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
