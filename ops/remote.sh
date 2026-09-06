#!/usr/bin/env bash
# The laptop codes and tests; the server fetches and rebuilds, uninterrupted (CLAUDE.md "Development model").
#
#   ops/remote.sh status              # server: commit, dirty files, running drivers, disk
#   ops/remote.sh sync                # push-verified fast-forward of the server checkout to origin
#   ops/remote.sh check               # quiet server only: sync, then `make check` there
#   ops/remote.sh test [pytest args]  # quiet server only: sync, then `uv run pytest <args>` there
#   ops/remote.sh run <command...>    # sync, then any command in the server's repo directory
#   ops/remote.sh shell               # interactive shell in the server's repo directory
#   ops/remote.sh logs [family] [pattern]  # tail the newest ~/campaign log — of one family
#                                     # (integrated, bse, rebuild-legacy…) if named — optionally filtered
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

refuse_if_driver_running() {
  # The server fetches and rebuilds uninterrupted; the gate runs on the laptop. A test run beside a
  # campaign driver competes with it for the CPU and the Postgres, so check/test are for a quiet
  # server only. Exit 4 so a caller can tell this from a sync or a test failure.
  local n
  n=$(remote 'pgrep -fc "^[^ ]*python[^ ]* -m dataplatform\.ingest" || true')
  if [ "${n:-0}" -gt 0 ]; then
    echo "remote: $n driver(s) running on the server — it fetches and rebuilds uninterrupted." >&2
    echo "        Run the gate here (make check); use check/test only on a quiet server." >&2
    exit 4
  fi
}

cmd_check() { refuse_if_driver_running; cmd_sync; remote "make check"; }
cmd_test()  { refuse_if_driver_running; cmd_sync; remote "uv run pytest $*"; }
cmd_run()   { cmd_sync; remote "$*"; }
cmd_shell() { "${SSH[@]}" -t "cd '$REMOTE_REPO' && export PATH=\"\$HOME/.local/bin:\$PATH\" && exec \$SHELL -l"; }
cmd_logs()  {
  # `logs [family] [pattern]`: a first argument that is the prefix of some ~/campaign/<family>-*.log
  # selects the newest log of that family (integrated, bse, rebuild-legacy…); otherwise it is the
  # grep pattern and the newest log of any family is used. The counters are per runner: the
  # fundamentals runner logs pages/filings, the price backfill runner logs sessions.
  local a="${1:-}" b="${2:-}"
  remote "family=''; pattern='$a';
          if [ -n '$a' ] && ls ~/campaign/'$a'-*.log >/dev/null 2>&1; then family='$a'; pattern='$b'; fi;
          if [ -n \"\$family\" ]; then L=\$(ls -t ~/campaign/\"\$family\"-*.log | head -1);
          else L=\$(ls -t ~/campaign/*.log 2>/dev/null | head -1); fi;
          [ -n \"\$L\" ] || { echo 'no campaign logs'; exit 0; };
          echo \"log: \$L\";
          case \"\$L\" in
            */bse-*|*/nse-*)
              echo \"published \$(grep -c backfill.session_published \$L), already \$(grep -c backfill.skip_published \$L), failed \$(grep -c backfill.session_failed \$L), hard_stop \$(grep -c backfill.hard_stop \$L)\";;
            *)
              echo \"pages \$(grep -c index_published \$L), published \$(grep -c filing_published \$L), reused \$(grep -c filing_l0_reused \$L), failed \$(grep -c unit_failed \$L)\";;
          esac;
          if [ -n \"\$pattern\" ]; then grep \"\$pattern\" \$L | tail -20; else grep -v crawl.spacing \$L | tail -5 | cut -c1-200; fi"
}

case "${1:-}" in
  status) cmd_status ;;
  sync)   cmd_sync ;;
  check)  cmd_check ;;
  test)   shift; cmd_test "$@" ;;
  run)    shift; cmd_run "$@" ;;
  shell)  cmd_shell ;;
  logs)   shift; cmd_logs "$@" ;;
  *) sed -n '2,13p' "$0"; exit 1 ;;
esac
