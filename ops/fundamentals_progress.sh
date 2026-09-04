#!/bin/zsh
# Progress of the M10.4 fundamentals backfill campaign. Read-only; safe to run any time.
#
#   ops/fundamentals_progress.sh          one snapshot
#   ops/fundamentals_progress.sh -w       refresh every 30s
#
# Reads the same three places the campaign writes: the driver's status log, `sync_state` (the
# checkpoint, which is the authority on what is done), and the L1/L0 lakes. Nothing here infers
# progress from a log tail — a unit counts as done when its checkpoint says PUBLISHED.
#
# The counts, the rate, the ETA and the failure breakdown come from `sync_state` and the lakes, so
# they work with no campaign directory at all. Only the per-segment lines and the in-universe rate
# read the driver's own output; point FUNDAMENTALS_CAMPAIGN_DIR at it if you are driving the
# campaign from somewhere else, and the rest of the report still works if you do not.

set -u
cd "$(dirname "$0")/.." || exit 1

CAMPAIGN="${FUNDAMENTALS_CAMPAIGN_DIR:-/private/tmp/claude-504/-Users-vysh-Documents-work-stocks/ccb0f9c4-fac0-4237-b9ae-4cf175ea7016/scratchpad/campaign}"

snapshot() {
  print -P "%F{cyan}── M10.4 fundamentals backfill ─────────────────────────────────%f  $(date '+%F %T')"

  # ── processes ────────────────────────────────────────────────────────────────
  if pgrep -f "campaign/run.sh" > /dev/null; then
    print -P "  years    %F{green}running%f"
  elif [ -f "$CAMPAIGN/logs/fy2026.done" ]; then
    print -P "  years    %F{green}complete%f"
  else
    print -P "  years    %F{yellow}not running%f"
  fi
  pgrep -f "campaign/sweep.sh" > /dev/null && print -P "  sweep    %F{green}running%f"
  pgrep -f "campaign/chain.sh" > /dev/null && print -P "  chain    armed (sweeps when the years finish)"

  # ── the checkpoint: the authority on what is actually done ───────────────────
  FUNDAMENTALS_CAMPAIGN_DIR="$CAMPAIGN" uv run python - <<'PY'
import os
import pathlib
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
# announcement count is the wrong denominator — it made the first version of this script quote an
# ETA nearly double the real one. The in-universe *rate* is read from the segments that have
# actually finished and applied to the years still to come, so the estimate is measured where it
# can be and honest about being an estimate where it cannot.
import re
CAMPAIGN = pathlib.Path(os.environ.get("FUNDAMENTALS_CAMPAIGN_DIR", ""))
observed_univ = observed_disc = 0
done_years: set[int] = set()
for rpt in sorted((CAMPAIGN / "reports").glob("fy[0-9][0-9][0-9][0-9].md")) if CAMPAIGN.is_dir() else []:
    text = rpt.read_text()
    u = re.search(r"Filings in universe \(fetched\): (\d+)", text)
    d = re.search(r"Filings discovered \(all entries\): (\d+)", text)
    year = int(rpt.stem[2:])
    if u and d and WITH_DOC.get(year):
        observed_univ += int(u.group(1))
        observed_disc += WITH_DOC[year]
        done_years.add(year)

rate = observed_univ / observed_disc if observed_disc else 0.63  # 0.63 measured on FY2018
TOTAL = round(observed_univ + sum(n for y, n in WITH_DOC.items() if y not in done_years) * rate)
s = get_settings()
with connection(s) as c:
    pub, fail, attempts = c.execute("""
        SELECT count(*) FILTER (WHERE state = 'PUBLISHED'),
               count(*) FILTER (WHERE state = 'FAILED'),
               coalesce(sum(attempts), 0)
        FROM sync_state WHERE source LIKE 'nse_xbrl_filing%'
    """).fetchone()
    idx = c.execute("""SELECT count(*) FROM sync_state
        WHERE source LIKE 'nse_financial_results_index%' AND state = 'PUBLISHED'""").fetchone()[0]
    recent = c.execute("""SELECT count(*) FROM sync_state
        WHERE source LIKE 'nse_xbrl_filing%' AND state = 'PUBLISHED'
          AND updated_at > now() - interval '10 minutes'""").fetchone()[0]

done = pub + fail
pct = 100 * done / TOTAL
bar = "█" * int(pct / 2.5) + "·" * (40 - int(pct / 2.5))
print(f"  index    {idx} chunks published")
print(f"  filings  {bar} {pct:5.1f}%   (of ~{TOTAL:,} in-universe, {rate:.0%} of documents)")
print(f"           {pub:,} published · {fail:,} failed · {done:,}/{TOTAL:,} attempted")
if recent:
    rate = recent / 10
    left = max(TOTAL - done, 0)
    print(f"  rate     {rate:.1f}/min over the last 10 min → ~{left / rate / 60:.1f} h remaining")
else:
    print("  rate     idle (nothing published in the last 10 min)")

# ── what has actually landed ─────────────────────────────────────────────────
part = s.data_root / "L1" / PIT_FUNDAMENTALS_DATASET
partitions = sorted(p.name[5:] for p in part.iterdir() if p.is_dir()) if part.is_dir() else []
print(f"  L1       {len(partitions)} filing-date partitions"
      + (f", {partitions[0]} → {partitions[-1]}" if partitions else ""))
PY

  local docs size
  docs=$(find data/L0/nse_xbrl_filing -name '*.xml' 2>/dev/null | wc -l | tr -d ' ')
  size=$(du -sh data/L0 2>/dev/null | cut -f1)
  print "  L0       ${docs} documents, ${size:-?} on disk"

  # ── failures by cause, so a new class is visible immediately ────────────────
  uv run python - <<'PY'
import collections
from dataplatform.config import get_settings
from dataplatform.store.db import connection

KNOWN = (
    ("no results column covers", "period not in the document (correct refusal)"),
    ("returned 404", "archive 404 (source gap)"),
    ("must name the same company", "symbol not in D2 history"),
    ("no results column:", "no column declares a period"),
    ("ConnectError", "transient network"),
    ("TransportError", "transient network"),
)
with connection(get_settings()) as c:
    rows = c.execute("""SELECT last_error FROM sync_state
        WHERE source LIKE 'nse_xbrl_filing%' AND state = 'FAILED'""").fetchall()
g: collections.Counter[str] = collections.Counter()
for (e,) in rows:
    e = e or ""
    for needle, label in KNOWN:
        if needle in e:
            g[label] += 1
            break
    else:
        g["UNKNOWN — needs a look"] += 1
if g:
    print("  failures by cause:")
    for k, v in g.most_common():
        mark = "  <-- new" if k.startswith("UNKNOWN") else ""
        print(f"    {v:6}  {k}{mark}")
PY

  print "  segments:"
  [ -f "$CAMPAIGN/status.tsv" ] && \
    awk -F'\t' '$3 ~ /^rc=/ {gsub(/fundamentals backfill: /,"",$4); printf "    %-16s %s\n", $2, $4}' \
      "$CAMPAIGN/status.tsv" | tail -12
}

if [ "${1:-}" = "-w" ]; then
  while true; do clear; snapshot; sleep 30; done
else
  snapshot
fi
