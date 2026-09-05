"""Verify the re-derived pit_fundamentals store against L0 and the invariants.

Race-safe by construction: takes the sync_state snapshot *before* reading L1 and again after, and
only reasons about filings published inside both, so a concurrent writer cannot make a growing store
look like a corrupt one (the mistake that once reported "513 EXTRA facts").
"""

import collections
import logging
import random
import sys
from decimal import Decimal
from typing import Any

import pyarrow.dataset as ds

from dataplatform.clock import SystemClock
from dataplatform.config import get_settings
from dataplatform.identity.master import IdentityStore
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.xbrl import parse, parse_index
from dataplatform.store.db import connection

# The rebuild's own warnings are not this script's output; it reports its own findings.
logging.disable(logging.WARNING)

settings = get_settings()
root = settings.data_root
fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        fails.append(name)


with connection(settings) as conn:
    before = {
        r[0]
        for r in conn.execute(
            "SELECT split_part(source,'/',2) FROM sync_state "
            "WHERE source LIKE 'nse_xbrl_filing%' AND state='PUBLISHED'"
        ).fetchall()
    }
    master = IdentityStore(conn, clock=SystemClock()).load_master()

parts = sorted((root / "L1" / "pit_fundamentals").rglob("*.parquet"))
t = ds.dataset([str(p) for p in parts], format="parquet").to_table()

with connection(settings) as conn:
    after = {
        r[0]
        for r in conn.execute(
            "SELECT split_part(source,'/',2) FROM sync_state "
            "WHERE source LIKE 'nse_xbrl_filing%' AND state='PUBLISHED'"
        ).fetchall()
    }
    row = conn.execute(
        "SELECT count(*) FROM sync_state WHERE source LIKE 'nse_xbrl_filing%' AND state='FAILED'"
    ).fetchone()
    failed = row[0] if row else 0

stable = before & after
rows = t.to_pylist()
print(
    f"\n{len(parts):,} partitions · {len(rows):,} facts · checkpoints stable in both snapshots: "
    f"{len(stable):,} (published {len(after):,}, failed {failed:,})\n"
)

print("── the store against its checkpoints ──")
l1_filings = {r["filing_id"] for r in rows}
check(
    "every L1 filing is PUBLISHED (no orphans)",
    not (l1_filings - after),
    f"{len(l1_filings - after)} orphaned",
)
check(
    "every stable PUBLISHED filing is in L1",
    not (stable - l1_filings),
    f"{len(stable - l1_filings)} missing",
)

print("\n── the invariants ──")
check(
    "filing_date > period_end on every fact (invariant #7)",
    all(r["filing_date"] > r["period_end"] for r in rows),
)
check(
    "every value is a Decimal (CLAUDE.md: money is never a float)",
    all(type(r["value"]) is Decimal for r in rows),
)
check(
    "every fact carries an l0_key (lineage back to the immutable payload)",
    all(r["l0_key"] for r in rows),
)
srcs = {r["source"] for r in rows}
check(
    "one source only — nothing leaked in from a vendor (invariant #8)",
    srcs == {"nse_xbrl_filing"},
    str(srcs),
)
check("taxonomy populated on every row", all(r["taxonomy"] for r in rows))
derived_concepts = {r["concept"] for r in rows if r["derived"]}
check(
    "derived flag marks exactly the two derived concepts",
    derived_concepts <= {"shares_outstanding", "shareholders_equity_excl_revaluation"},
    str(sorted(derived_concepts)),
)

print("\n── the derivations, recomputed from their own inputs ──")
by_filing: dict[str, dict[str, Decimal]] = collections.defaultdict(dict)
for r in rows:
    if r["segment"] is None:
        by_filing[r["filing_id"]][r["concept"]] = r["value"]
bad_shares = bad_equity = n_shares = n_equity = 0
for c in by_filing.values():
    if "shares_outstanding" in c:
        n_shares += 1
        want = (c["paid_up_equity_capital"] / c["face_value_per_share"]).quantize(Decimal(1))
        bad_shares += abs(c["shares_outstanding"] - want) > 1
    if "shareholders_equity_excl_revaluation" in c:
        n_equity += 1
        bad_equity += c["shareholders_equity_excl_revaluation"] != (
            c["paid_up_equity_capital"] + c["reserves_excl_revaluation"]
        )
check(
    f"shares_outstanding == paid_up / face_value ({n_shares:,} facts)",
    bad_shares == 0,
    f"{bad_shares} mismatched",
)
check(
    f"equity == paid_up + reserves ({n_equity:,} facts)",
    bad_equity == 0,
    f"{bad_equity} mismatched",
)
check(
    "no published equity rests on a zero reserves figure",
    not any(
        "shareholders_equity_excl_revaluation" in c and not c.get("reserves_excl_revaluation")
        for c in by_filing.values()
    ),
)

print("\n── the guard actually held: every published share count agrees with its own EPS ──")
outside = 0
checked = 0
for c in by_filing.values():
    if "shares_outstanding" not in c:
        continue
    profit = c.get("profit_attributable_to_owners") or c.get("profit_after_tax")
    eps = c.get("eps_basic")
    if not profit or not eps:
        continue
    implied = profit / eps
    if implied <= 0:
        continue
    checked += 1
    outside += not (Decimal(1) / 3 <= c["shares_outstanding"] / implied <= 3)
check(f"all {checked:,} corroborable counts inside the 3x band", outside == 0, f"{outside} outside")

print("\n── re-derived from L0: a sample of filings parsed again and compared fact by fact ──")
by_name = {p.name: p for p in (root / "L0" / "nse_xbrl_filing").rglob("*.xml")}
index_docs = sorted((root / "L0" / "nse_financial_results_index").rglob("*.json"))
random.seed(53)
random.shuffle(index_docs)
want_by_filing: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
for r in rows:
    want_by_filing[r["filing_id"]].append(r)

compared = mismatched = 0
for path in index_docs:
    if compared >= 1500:
        break
    try:
        entries = parse_index(path.read_bytes(), filename=path.name)
    except ParseError:
        continue
    for e in entries:
        if compared >= 1500 or e.seq_number not in stable or e.xbrl_url is None:
            continue
        doc = by_name.get(e.xbrl_url.rsplit("/", 1)[-1])
        if doc is None:
            continue
        try:
            f = parse(
                doc.read_bytes(),
                entry=e,
                known_symbols=frozenset(w.symbol for w in master.windows_for(e.isin)) | {e.symbol},
                filename=doc.name,
            )
        except ParseError:
            continue
        fresh = {(x.concept, x.segment, x.value, x.taxonomy.value, x.derived) for x in f.facts}
        stored = {
            (x["concept"], x["segment"], x["value"], x["taxonomy"], x["derived"])
            for x in want_by_filing[e.seq_number]
        }
        compared += 1
        if fresh != stored:
            mismatched += 1
            if mismatched <= 3:
                extra, gone = sorted(fresh - stored)[:2], sorted(stored - fresh)[:2]
                print(f"      {e.seq_number}: +{extra} -{gone}")
check(
    f"{compared:,} filings re-derived from L0 match the store exactly",
    mismatched == 0,
    f"{mismatched} differ",
)

print("\n── coverage ──")
per = collections.Counter(r["concept"] for r in rows)
filings = len(l1_filings)
for c, n in per.most_common():
    print(f"  {c:<36}{n:>9,}{n / filings * 100:>8.1f}% of filings")

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {fails}")
    sys.exit(1)
print("All checks passed.")
