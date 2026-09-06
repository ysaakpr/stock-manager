#!/usr/bin/env bash
# Drive the Integrated Filing forward-sync campaign on a server (bash; Ubuntu has no zsh).
#
#   ops/run_integrated_campaign.sh              # run (resumes from sync_state), logs to ~/campaign
#   ops/run_integrated_campaign.sh --dry-run    # print the page plan only
#
# The window's start is fixed at 2025-03-01 on purpose: the month windows and their checkpoints are
# keyed on it, so moving it would re-plan every page (ops/runbooks/fundamentals_backfill.md). `--to`
# is today, so a rerun picks up new pages. Resumable: PUBLISHED pages and filings are skipped,
# FAILED ones retried. Exit 3 means the run parked on a 403 spike — read the report, do not retry
# blindly. One request budget per host: stop any other driver against nsearchives first.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

CAMPAIGN_DIR="${FUNDAMENTALS_CAMPAIGN_DIR:-$HOME/campaign}"
mkdir -p "$CAMPAIGN_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$CAMPAIGN_DIR/integrated-$STAMP.log"
REPORT="$CAMPAIGN_DIR/integrated-$STAMP.md"

if [ "${1:-}" = "--dry-run" ]; then
  exec uv run python -m dataplatform.ingest.fundamentals_backfill --feed integrated \
      --from 2025-03-01 --to "$(date +%F)" --dry-run
fi

echo "log: $LOG"
echo "report: $REPORT"
nohup uv run python -m dataplatform.ingest.fundamentals_backfill --feed integrated \
    --from 2025-03-01 --to "$(date +%F)" --report "$REPORT" > "$LOG" 2>&1 &
echo "pid: $!"
echo "follow with: tail -f $LOG | grep -v crawl.spacing"
