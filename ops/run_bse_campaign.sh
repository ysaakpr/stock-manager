#!/usr/bin/env bash
# Drive the BSE cash-bhavcopy backfill campaign on a server (bash; Ubuntu has no zsh).
#
#   ops/run_bse_campaign.sh              # run (resumes from sync_state), logs to ~/campaign/bse-*.log
#   ops/run_bse_campaign.sh --dry-run    # print the session plan only (no socket, no database)
#
# `--from` is fixed at 2024-07-08, the BSE UDiFF cutover: the legacy era before it carries no ISIN
# and the `bse_bhavcopy` source set refuses it on purpose (its L1 path goes through the scrip
# master; ops/BACKLOG.md, M3.1). `--to` is yesterday, so a rerun picks up new sessions without
# planning today's file before BSE has published it. Resumable: PUBLISHED sessions are skipped,
# FAILED ones retried (ops/runbooks/backfill.md). Exit 3 means a 403 hard stop — read the log, do
# not retry blindly. One request budget per host: this talks to www.bseindia.com only, so it may
# run beside the NSE campaigns; never start a second driver against BSE while it runs.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

FROM=2024-07-08
TO="$(date -d yesterday +%F 2>/dev/null || date -v-1d +%F)"
CAMPAIGN_DIR="${CAMPAIGN_DIR:-$HOME/campaign}"
mkdir -p "$CAMPAIGN_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$CAMPAIGN_DIR/bse-$STAMP.log"

if [ "${1:-}" = "--dry-run" ]; then
  exec uv run python -m dataplatform.ingest.backfill --source bse_bhavcopy \
      --from "$FROM" --to "$TO" --dry-run
fi

echo "log: $LOG"
nohup uv run python -m dataplatform.ingest.backfill --source bse_bhavcopy \
    --from "$FROM" --to "$TO" > "$LOG" 2>&1 &
echo "pid: $!"
echo "follow with: ops/remote.sh logs bse   (or: tail -f $LOG | grep -v crawl.spacing)"
