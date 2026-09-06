#!/usr/bin/env bash
# Drive the pre-cutover BSE cash-bhavcopy backfill on a server (bash; Ubuntu has no zsh).
#
#   ops/run_bse_legacy_campaign.sh              # run (resumes from sync_state), logs to ~/campaign
#   ops/run_bse_legacy_campaign.sh --dry-run    # print the session plan only (no socket, no database)
#
# The eight years before `bse_bhavcopy`'s UDiFF era. `--to` is fixed one day before the cutover:
# from 2024-07-08 the zipped file stops being served and `bse_bhavcopy` takes over, so the two sets
# tile the decade without overlapping. `--from` matches the NSE price window's start, because a BSE
# session with no NSE counterpart has nothing to reconcile against.
#
# PRECONDITION: the BSE scrip master must be merged (`dataplatform.ingest.bse_scrip_refresh`).
# Legacy rows carry SC_CODE and no ISIN, so without it every row is unresolved and the campaign
# writes nothing while reporting success. Check: `exchange_listing` holds BSE rows with a
# security_code.
#
# Resumable: PUBLISHED sessions are skipped, FAILED ones retried. Exit 3 means a 403 hard stop —
# read the log, do not retry blindly. One request budget per host: this talks to www.bseindia.com
# only, so it may run beside an NSE campaign; never start a second driver against BSE while it runs.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

FROM="${FROM:-2016-09-01}"
TO=2024-07-07
CAMPAIGN_DIR="${CAMPAIGN_DIR:-$HOME/campaign}"
mkdir -p "$CAMPAIGN_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$CAMPAIGN_DIR/bse-legacy-$STAMP.log"

if [ "${1:-}" = "--dry-run" ]; then
  exec uv run python -m dataplatform.ingest.backfill --source bse_bhavcopy_legacy \
      --from "$FROM" --to "$TO" --dry-run
fi

echo "log: $LOG"
nohup uv run python -m dataplatform.ingest.backfill --source bse_bhavcopy_legacy \
    --from "$FROM" --to "$TO" > "$LOG" 2>&1 &
echo "pid: $!"
echo "follow with: ops/remote.sh logs bse-legacy"
