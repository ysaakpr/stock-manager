#!/usr/bin/env bash
# Progress of the M10.4 fundamentals backfill campaign. Read-only; safe to run any time.
#
#   ops/fundamentals_progress.sh          one snapshot
#   ops/fundamentals_progress.sh -w       refresh every 30s
#
# Portable between the macOS laptop and the Ubuntu server on purpose: a campaign that migrates
# between machines needs one progress view, not two that disagree. So this is POSIX-ish bash (no
# bash-4 features — macOS still ships 3.2), colour only when stdout is a terminal, and no zsh
# builtins. Everything heavy happens in Python, which is identical on both.
#
# Reads the same three places the campaign writes: the driver's status log, `sync_state` (the
# checkpoint, which is the authority on what is done), and the L1/L0 lakes. Nothing here infers
# progress from a log tail — a unit counts as done when its checkpoint says PUBLISHED.
#
# The counts, the rate, the ETA and the failure breakdown come from `sync_state` and the lakes, so
# they work with no campaign directory at all. Only the per-segment lines and the in-universe rate
# read the driver's own output; set FUNDAMENTALS_CAMPAIGN_DIR when driving from elsewhere, and the
# rest of the report still works if you do not.

set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="$HOME/.local/bin:$PATH"     # uv installs here on a fresh Linux box

# Where the driver writes its status log and per-segment reports. Checked in order so the same
# script works unchanged on either machine.
if [ -n "${FUNDAMENTALS_CAMPAIGN_DIR:-}" ]; then
  CAMPAIGN="$FUNDAMENTALS_CAMPAIGN_DIR"
else
  CAMPAIGN=""
  for candidate in "$HOME/campaign" "$PWD/ops/campaign" \
      /private/tmp/claude-*/-Users-*-stocks/*/scratchpad/campaign; do
    if [ -d "$candidate" ]; then CAMPAIGN="$candidate"; break; fi
  done
fi

if [ -t 1 ] && command -v tput > /dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  C_HEAD=$(tput setaf 6); C_OK=$(tput setaf 2); C_WARN=$(tput setaf 3); C_OFF=$(tput sgr0)
else
  C_HEAD=""; C_OK=""; C_WARN=""; C_OFF=""
fi

snapshot() {
  printf '%s── M10.4 fundamentals backfill ─────────────────────────────%s  %s\n' \
    "$C_HEAD" "$C_OFF" "$(date '+%F %T')"

  if pgrep -f "campaign/run.sh" > /dev/null 2>&1; then
    printf '  years    %srunning%s\n' "$C_OK" "$C_OFF"
  elif [ -n "$CAMPAIGN" ] && [ -f "$CAMPAIGN/logs/fy2026.done" ]; then
    printf '  years    %scomplete%s\n' "$C_OK" "$C_OFF"
  else
    printf '  years    %snot running%s\n' "$C_WARN" "$C_OFF"
  fi
  pgrep -f "campaign/sweep.sh" > /dev/null 2>&1 && printf '  sweep    %srunning%s\n' "$C_OK" "$C_OFF"
  pgrep -f "campaign/chain.sh" > /dev/null 2>&1 && printf '  chain    armed (sweeps when the years finish)\n'
  [ -n "$CAMPAIGN" ] || printf '  (no campaign directory found; set FUNDAMENTALS_CAMPAIGN_DIR for segment lines)\n'

  FUNDAMENTALS_CAMPAIGN_DIR="$CAMPAIGN" uv run python - <<'PY'
import collections
import os
import pathlib
import re

from dataplatform.config import get_settings
from dataplatform.store.db import connection
from dataplatform.store.pit_fundamentals import PIT_FUNDAMENTALS_DATASET

# Documents-with-an-XBRL per financial year, measured (ops/gates/M10-fundamentals-backfill-live.md
# §3). FY2016/FY2017 are 0 — those years are all placeholder.
WITH_DOC = {
    2016: 0, 2017: 0, 2018: 11_354, 2019: 11_543, 2020: 14_060,
    2021: 14_396, 2022: 15_009, 2023: 16_768, 2024: 18_262, 2025: 44, 2026: 10,
}

# The runner attempts only entries whose ISIN is in the price-window universe, so the raw
# announcement count is the wrong denominator — using it quoted an ETA nearly double the real one.
# The in-universe *rate* is read from the segments that have actually finished and applied to the
# years still to come: measured where it can be, and honest about being an estimate where it cannot.
#
# Only years with a `.done` marker count. A segment stopped part-way still writes a report — a
# graceful Ctrl-C finishes the current unit and the runner reports what it managed — so its
# in-universe figure is a fraction of that year's real one. Averaging it in drags the rate down and
# shrinks the denominator, which is how the same campaign read 63% on one machine and 54% on the
# other. The marker is written only on a clean exit, so it is the honest gate.
campaign = pathlib.Path(os.environ.get("FUNDAMENTALS_CAMPAIGN_DIR") or "")
observed_univ = observed_disc = 0
done_years: set[int] = set()
reports = sorted((campaign / "reports").glob("fy[0-9][0-9][0-9][0-9].md")) if campaign.is_dir() else []
for rpt in reports:
    year = int(rpt.stem[2:])
    if not (campaign / "logs" / f"fy{year}.done").exists():
        continue
    seen = re.search(r"Filings in universe \(fetched\): (\d+)", rpt.read_text())
    if seen and WITH_DOC.get(year):
        observed_univ += int(seen.group(1))
        observed_disc += WITH_DOC[year]
        done_years.add(year)

rate = observed_univ / observed_disc if observed_disc else 0.63  # 0.63 measured on FY2018
total = round(observed_univ + sum(n for y, n in WITH_DOC.items() if y not in done_years) * rate)

settings = get_settings()
with connection(settings) as conn:
    published, failed = conn.execute(
        "SELECT count(*) FILTER (WHERE state = 'PUBLISHED'), "
        "count(*) FILTER (WHERE state = 'FAILED') "
        "FROM sync_state WHERE source LIKE 'nse_xbrl_filing%'"
    ).fetchone()
    chunks = conn.execute(
        "SELECT count(*) FROM sync_state "
        "WHERE source LIKE 'nse_financial_results_index%' AND state = 'PUBLISHED'"
    ).fetchone()[0]
    recent = conn.execute(
        "SELECT count(*) FROM sync_state WHERE source LIKE 'nse_xbrl_filing%' "
        "AND state = 'PUBLISHED' AND updated_at > now() - interval '10 minutes'"
    ).fetchone()[0]
    failures = conn.execute(
        "SELECT last_error FROM sync_state "
        "WHERE source LIKE 'nse_xbrl_filing%' AND state = 'FAILED'"
    ).fetchall()

done = published + failed
pct = 100 * done / total if total else 0.0
filled = int(pct / 2.5)
bar = "#" * filled + "." * (40 - filled)
print(f"  index    {chunks} chunks published")
print(f"  filings  {bar} {pct:5.1f}%   (of ~{total:,} in-universe, {rate:.0%} of documents)")
print(f"           {published:,} published · {failed:,} failed · {done:,}/{total:,} attempted")
if recent:
    per_min = recent / 10
    print(
        f"  rate     {per_min:.1f}/min over the last 10 min → "
        f"~{max(total - done, 0) / per_min / 60:.1f} h remaining"
    )
else:
    print("  rate     idle (nothing published in the last 10 min)")

part = settings.data_root / "L1" / PIT_FUNDAMENTALS_DATASET
partitions = sorted(p.name[5:] for p in part.iterdir() if p.is_dir()) if part.is_dir() else []
span = f", {partitions[0]} → {partitions[-1]}" if partitions else ""
print(f"  L1       {len(partitions)} filing-date partitions{span}")

# Failures by cause, so a class nobody has seen before is visible immediately.
KNOWN = (
    ("no results column covers", "period not in the document (correct refusal)"),
    ("returned 404", "archive 404 (source gap)"),
    ("must name the same company", "symbol not in D2 history"),
    ("no results column:", "no column declares a period"),
    ("ConnectError", "transient network"),
    ("TransportError", "transient network"),
)
grouped: collections.Counter = collections.Counter()
for (error,) in failures:
    text = error or ""
    for needle, label in KNOWN:
        if needle in text:
            grouped[label] += 1
            break
    else:
        grouped["UNKNOWN — needs a look"] += 1
if grouped:
    print("  failures by cause:")
    for label, count in grouped.most_common():
        mark = "  <-- new" if label.startswith("UNKNOWN") else ""
        print(f"    {count:6}  {label}{mark}")
PY

  l0_docs=$(find data/L0/nse_xbrl_filing -name '*.xml' 2>/dev/null | wc -l | tr -d ' ')
  l0_size=$(du -sh data/L0 2>/dev/null | cut -f1)
  printf '  L0       %s documents, %s on disk\n' "$l0_docs" "${l0_size:-?}"

  if [ -n "$CAMPAIGN" ] && [ -f "$CAMPAIGN/status.tsv" ]; then
    printf '  segments:\n'
    awk -F'\t' '$3 ~ /^rc=/ {
        sub(/fundamentals backfill: /, "", $4); printf "    %-18s %s\n", $2, $4
      }' "$CAMPAIGN/status.tsv" | tail -12
  fi
}

if [ "${1:-}" = "-w" ]; then
  while true; do clear; snapshot; sleep 30; done
else
  snapshot
fi
