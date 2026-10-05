"""Step 0 of the cap-tier study: what size measure can be point-in-time across 2012-2026?

Measures, off the authoritative lake and nothing else (no fetch):

1. **PIT shares-outstanding coverage per year, 2011-2026**, for the liquid universe (NSE EQ
   equities whose 365-day median traded value reaches Rs 1 crore on the first session of July):
   the share with a dated share count from each source on that session — the PR bundle's ``mcap``
   Issue Size (from 2024-02-01), ``ffix`` ISSUE_CAP (index members, 2010-01-04..2013-04-30), and
   XBRL ``shares_outstanding`` filed on or before the session for a period ending within the
   preceding 400 days.
2. **The proxy against real market cap where real market cap exists**: on the first session of
   each month, the proxy size (``backtest.cap_tiers``: 126-session median close x quantity) and
   real market cap (``mcap``: Market Cap; ``ffix``: ISSUE_CAP x close for CNX 500 members) over the
   names both cover. Reports the Spearman rank correlation and, tier by tier, the share of a
   real-mcap tier's names the proxy places in the same tier.

``ffix`` covers CNX 500 members only, so its ranks are *within CNX 500* for both measures — a
check on ordering, not on the rank-500 boundary.

Run: ``DATA_ROOT=/home/ubuntu/stock-manager/data uv run python ops/studies/cap_tier_size_measure.py
--out <file.md>``. Read-only: opens L0 payloads through ``L0Store.get`` (re-hashed) and L1 parquet.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from backtest.cap_tiers import (
    LiquidityRankTiers,
    assign_tiers,
    is_ranked_equity,
)
from backtest.run import _L1Reader
from dataplatform.clock import FrozenClock
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle.bundle import PR_BUNDLE_SOURCE_ID, MemberKind, PrBundle
from dataplatform.ingest.nse.pr_bundle.ffix import parse_ffix_bundle
from dataplatform.ingest.nse.pr_bundle.mcap import parse_mcap_bundle
from dataplatform.store.l0 import L0Store
from dataplatform.store.l2 import open_connection
from dataplatform.store.paths import l1_partition_path

_FLOOR = Decimal("10000000")
_TIERS = ("large", "mid", "small")
FFIX_RATIOS: list[Decimal] = []


def _listing(con, session: date, root: Path) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """That session's NSE ``symbol -> ISIN`` for the EQ series (the PR bundle is symbol-keyed)."""
    path = l1_partition_path("prices_raw", session, data_root=root)
    if not path.exists():
        return {}
    rows = con.execute(
        "SELECT symbol, isin FROM read_parquet($p) WHERE exchange='NSE' AND series='EQ'",
        {"p": str(path)},
    ).fetchall()
    out: dict[str, str] = {}
    dup: set[str] = set()
    for symbol, isin in rows:
        if symbol in out and out[symbol] != isin:
            dup.add(symbol)
        out[symbol] = isin
    for symbol in dup:
        del out[symbol]
    return out


def _bundle(store: L0Store, session: date) -> PrBundle | None:
    refs = list(store.iter_refs(PR_BUNDLE_SOURCE_ID, start=session, end=session))
    for ref in refs:
        try:
            return PrBundle(store.get(ref), filename=ref.filename)
        except ParseError:
            continue
    return None


def real_mcap(
    store: L0Store,
    con,
    session: date,
    root: Path,  # type: ignore[no-untyped-def]
) -> tuple[str, dict[str, Decimal]] | None:
    """(source, ISIN -> full market cap in rupees) on ``session``, or None if no source has it."""
    bundle = _bundle(store, session)
    if bundle is None:
        return None
    listing = _listing(con, session, root)
    with bundle:
        if bundle.has(MemberKind.MCAP):
            parsed = parse_mcap_bundle(bundle)
            out = {}
            for row in parsed.rows:
                isin = listing.get(row.symbol) if row.series == "EQ" else None
                if isin and is_ranked_equity(isin) and row.market_cap > 0:
                    out[isin] = row.market_cap
            return "mcap", out
        if bundle.has(MemberKind.FFIX):
            parsed_f = parse_ffix_bundle(bundle)
            out = {}
            for frow in parsed_f.constituents("CNX 500"):
                isin = listing.get(frow.symbol) if frow.series == "EQ" else None
                if isin and is_ranked_equity(isin) and frow.issue_cap > 0:
                    out[isin] = frow.issue_cap * frow.close_price
                    # ISSUE_CAP is the full share count iff FF_MKT_CAP = ISSUE_CAP x IWF x close.
                    full_ff = frow.issue_cap * frow.investible_factor * frow.close_price
                    if full_ff > 0:
                        FFIX_RATIOS.append(frow.ff_market_cap / full_ff)
            return "ffix", out
    return None


def _ranks(values: Mapping[str, Decimal]) -> dict[str, int]:
    ordered = sorted(values, key=lambda isin: (-values[isin], isin))
    return {isin: i for i, isin in enumerate(ordered, start=1)}


def spearman(a: Mapping[str, Decimal], b: Mapping[str, Decimal]) -> Decimal:
    common = sorted(set(a) & set(b))
    ra = _ranks({k: a[k] for k in common})
    rb = _ranks({k: b[k] for k in common})
    n = Decimal(len(common))
    d2 = sum(Decimal((ra[k] - rb[k]) ** 2) for k in common)
    return 1 - 6 * d2 / (n * (n * n - 1))


def _first_sessions(calendar: Sequence[date], months: Iterable[tuple[int, int]]) -> list[date]:
    out = []
    for year, month in months:
        hit = next((s for s in calendar if (s.year, s.month) == (year, month)), None)
        if hit is not None:
            out.append(hit)
    return out


def _xbrl_shares(con, root: Path) -> dict[str, list[tuple[date, date]]]:  # type: ignore[no-untyped-def]
    rows = con.execute(
        "SELECT isin, filing_date, period_end FROM read_parquet($g, hive_partitioning=false) "
        "WHERE concept = 'shares_outstanding' AND value > 0",
        {"g": str(root / "L1/pit_fundamentals/*/*.parquet")},
    ).fetchall()
    out: dict[str, list[tuple[date, date]]] = defaultdict(list)
    for isin, filed, period_end in rows:
        out[str(isin)].append((filed, period_end))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path(os.environ.get("DATA_ROOT", "")).resolve()
    if root != Path("/home/ubuntu/stock-manager/data"):
        print(f"DATA_ROOT must be the primary lake, got {root!r}", file=sys.stderr)
        return 2
    reader = _L1Reader(data_root=root)
    calendar = reader.all_sessions()
    con = open_connection()
    store = L0Store(clock=FrozenClock(calendar[-1]), data_root=root)
    lines: list[str] = []

    # ── 1. coverage ─────────────────────────────────────────────────────────────────────────
    xbrl = _xbrl_shares(con, root)
    july = _first_sessions(calendar, ((y, 7) for y in range(2011, 2027)))
    july = [s for s in july if s >= date(2011, 7, 1)]
    lines += [
        "## 1. PIT shares-outstanding coverage of the liquid universe",
        "",
        "Liquid universe: NSE EQ equities (INE, valid check digit) printing on the session with a "
        "365-day median traded value >= Rs 1 crore. 2026 uses the first session of July 2026.",
        "",
        "| session | liquid names | mcap Issue Size | ffix ISSUE_CAP (any index) "
        "| XBRL shares (filed, period <= 400d old) | any source |",
        "|---|---|---|---|---|---|",
    ]
    for session in july:
        medians = reader.median_turnover_over(session - timedelta(days=365), session)
        printed = set(reader.closes_on(session))
        liquid = {
            i for i, m in medians.items() if m >= _FLOOR and i in printed and is_ranked_equity(i)
        }
        mcap_cov: set[str] = set()
        ffix_cov: set[str] = set()
        bundle = _bundle(store, session)
        if bundle is not None:
            listing = _listing(con, session, root)
            with bundle:
                if bundle.has(MemberKind.MCAP):
                    mcap_cov = {
                        listing[r.symbol]
                        for r in parse_mcap_bundle(bundle).rows
                        if r.series == "EQ" and r.symbol in listing and r.issue_size > 0
                    }
                if bundle.has(MemberKind.FFIX):
                    ffix_cov = {
                        listing[r.symbol]
                        for r in parse_ffix_bundle(bundle).rows
                        if r.series == "EQ" and r.symbol in listing
                    }
        xbrl_cov = {
            isin
            for isin in liquid
            if any(
                filed <= session and (session - period_end).days <= 400
                for filed, period_end in xbrl.get(isin, ())
            )
        }
        n = len(liquid)

        def pct(cov: set[str], n: int = n, liquid: set[str] = liquid) -> str:
            k = len(cov & liquid)
            return f"{k} ({k / n:.0%})" if n else "—"

        lines.append(
            f"| {session} | {n} | {pct(mcap_cov)} | {pct(ffix_cov)} | {pct(xbrl_cov)} | "
            f"{pct(mcap_cov | ffix_cov | xbrl_cov)} |"
        )

    # ── 2. proxy vs real market cap ─────────────────────────────────────────────────────────
    months = [(y, m) for y in range(2011, 2014) for m in range(1, 13)] + [
        (y, m) for y in range(2024, 2027) for m in range(1, 13)
    ]
    sessions = [
        s
        for s in _first_sessions(calendar, months)
        if date(2011, 2, 1) <= s <= date(2013, 4, 30) or date(2024, 2, 1) <= s <= date(2026, 8, 31)
    ]
    proxy = LiquidityRankTiers(calendar, data_root=root)
    sizes = proxy.size_measure(sessions)
    lines += [
        "",
        "## 2. The proxy against real market cap",
        "",
        "Population: names both measures cover on the session. `mcap` ranks are over all such "
        "NSE EQ equities, so the tier boundaries are the strategy's own; `ffix` ranks are within "
        "CNX 500 only (both measures re-ranked on that subset).",
        "",
        "| session | source | names | Spearman "
        "| large same tier | mid same tier | small same tier |",
        "|---|---|---|---|---|---|---|",
    ]
    agg: dict[str, list[Decimal]] = defaultdict(list)
    agree: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    precision: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    order = {name: k for k, name in enumerate(_TIERS)}
    for session in sessions:
        real = real_mcap(store, con, session, root)
        if real is None:
            lines.append(f"| {session} | — | 0 | — | — | — | — |")
            continue
        source, mcap = real
        common = set(mcap) & set(sizes.get(session, {}))
        if len(common) < 50:
            lines.append(f"| {session} | {source} | {len(common)} | — | — | — | — |")
            continue
        p = {i: sizes[session][i] for i in common}
        m = {i: mcap[i] for i in common}
        rho = spearman(p, m)
        agg[source].append(rho)
        pt: dict[str, str] = {t.isin: t.tier.value for t in assign_tiers(p, as_of=session)}
        mt = {t.isin: t.tier.value for t in assign_tiers(m, as_of=session)}
        cells = []
        for tier in _TIERS:
            names = [i for i, t in mt.items() if t == tier]
            same = sum(1 for i in names if pt.get(i) == tier)
            agree[(source, tier)][0] += same
            agree[(source, tier)][1] += len(names)
            cells.append(f"{same}/{len(names)}" if names else "—")
            picked = [i for i, t in pt.items() if t == tier]
            precision[(source, tier)][0] += sum(1 for i in picked if mt.get(i) == tier)
            precision[(source, tier)][1] += sum(
                1 for i in picked if abs(order.get(mt.get(i, ""), 3) - order[tier]) <= 1
            )
            precision[(source, tier)][2] += len(picked)
        lines.append(
            f"| {session} | {source} | {len(common)} | {rho:.3f} | " + " | ".join(cells) + " |"
        )
    lines += ["", "**Pooled:**", ""]
    for source, rhos in sorted(agg.items()):
        mean = sum(rhos) / len(rhos)
        tiers = ", ".join(
            f"{tier} {agree[(source, tier)][0] / agree[(source, tier)][1]:.1%}"
            for tier in _TIERS
            if agree[(source, tier)][1]
        )
        held = ", ".join(
            f"{tier} {precision[(source, tier)][0] / precision[(source, tier)][2]:.1%} same / "
            f"{precision[(source, tier)][1] / precision[(source, tier)][2]:.1%} within one tier"
            for tier in _TIERS
            if precision[(source, tier)][2]
        )
        lines.append(
            f"- `{source}`: {len(rhos)} sessions, mean Spearman {mean:.3f} "
            f"(min {min(rhos):.3f}, max {max(rhos):.3f}); share of each real tier the proxy puts "
            f"in the same tier: {tiers}; of each proxy tier's names, the share whose real tier is "
            f"the same / within one tier (past rank 500 counts as a fourth tier): {held}"
        )
    if FFIX_RATIOS:
        ordered = sorted(FFIX_RATIOS)
        lines.append(
            f"- `ffix` ISSUE_CAP check: FF_MKT_CAP / (ISSUE_CAP x INVESTIBLE_FACTOR x close) "
            f"median {ordered[len(ordered) // 2]:.4f}, p1 {ordered[len(ordered) // 100]:.4f}, "
            f"p99 {ordered[-len(ordered) // 100]:.4f} over {len(ordered)} rows"
        )
    proxy.close()
    con.close()
    reader.close()
    args.out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
