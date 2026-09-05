"""Compare our PIT fundamentals against Screener company pages — the offline half of the check.

Input: the scratch directory `screener_fetch.py` filled (one HTML page per company plus
`sample.json`). Output: a markdown report and nothing else — no store is written (invariant #8: a
restated vendor figure is a *comparison* input here, never a decision input).

What is compared, and how a match is judged:

* **Quarterly Sales / Net Profit** (Screener `#quarters`) against our restatement-collapsed
  `revenue_from_operations` / `profit_after_tax` for the same quarter-end and nature. Screener
  prints whole crores, so a match is within max(₹1 Cr, 0.5%). Banks label sales "Revenue"; both
  labels are read.
* **Quarterly EPS** against our `eps_basic`. Screener restates historical EPS for later bonuses and
  splits (a pre-bonus quarter shows half the filed figure); ours is what the filing said. So EPS is
  reported two ways: exact agreement, and agreement up to a clean corporate-action ratio (2, 0.5,
  5, 0.2, 10, 0.1 …). The second bucket is *expected* disagreement, and it is a measurement of how
  many of our EPS figures a restated vendor has since re-based — which is the whole reason the
  restated store is quarantined from backtests.
* **Annual Sales / Net Profit / EPS** (Screener `#profit-loss`) against our FY facts.
* **Equity Capital** (annual) against our `paid_up_equity_capital`; **Reserves** against
  `reserves_excl_revaluation` (definitional gap: Screener includes revaluation reserves).
* **Face Value** against `face_value_per_share`; **Market Cap**, **Current Price**, **Stock P/E**
  and **Book Value** against our derived metrics at our latest close (a *days-old* close against a
  live price, so these are loose sanity bands, not accounting checks).

Usage:
    uv run python ops/sanity/screener_compare.py --pages <scratch-dir> --data-root <lake> \
        --report ops/gates/fundamentals-screener-sanity.md
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import duckdb

from dataplatform.clock import SystemClock
from dataplatform.ingest.screener import parse_html

CRORE = Decimal("10000000")
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
# Ratios a later bonus/split/consolidation would put between a filed EPS and a re-based one.
_CA_RATIOS = [
    Decimal(x)
    for x in (
        "2",
        "0.5",
        "3",
        "4",
        "5",
        "10",
        "0.25",
        "0.2",
        "0.1",
        "1.5",
        "0.666667",
        "2.5",
        "0.4",
        "0.333333",
    )
]


def _period_end(label: str) -> date | None:
    """'Jun 2023' -> 2023-06-30; anything else (TTM, current) -> None."""
    parts = label.split()
    if len(parts) != 2 or parts[0] not in _MONTHS or not parts[1].isdigit():
        return None
    month, year = _MONTHS[parts[0]], int(parts[1])
    nxt = date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
    return nxt - timedelta(days=1)


def _clean(metric: str) -> str:
    return metric.replace("+", "").strip()


@dataclass
class Tally:
    compared: int = 0
    matched: int = 0
    ca_ratio: int = 0  # EPS only: agreement up to a clean corporate-action ratio
    worst: list[tuple[Decimal, str, str, Decimal, Decimal]] = field(default_factory=list)
    reasons: Counter[str] = field(default_factory=Counter)
    histogram: Counter[str] = field(default_factory=Counter)

    def add(
        self, rel_err: Decimal, ok: bool, symbol: str, period: str, ours: Decimal, theirs: Decimal
    ) -> None:
        self.compared += 1
        self.histogram[_bucket(rel_err)] += 1
        if ok:
            self.matched += 1
        else:
            self.worst.append((rel_err, symbol, period, ours, theirs))
            self.reasons[_classify(ours, theirs)] += 1

    @property
    def rate(self) -> str:
        return f"{100 * self.matched / self.compared:.1f}%" if self.compared else "n/a"


def _bucket(rel: Decimal) -> str:
    if rel <= Decimal("0.005"):
        return "<=0.5%"
    if rel <= Decimal("0.02"):
        return "0.5-2%"
    if rel <= Decimal("0.10"):
        return "2-10%"
    if rel <= Decimal("0.50"):
        return "10-50%"
    return ">50%"


def _rel(ours: Decimal, theirs: Decimal) -> Decimal:
    base = max(abs(theirs), Decimal("1"))
    return abs(ours - theirs) / base


def _classify(ours_cr: Decimal, theirs_cr: Decimal) -> str:
    """Why a money figure disagrees: a clean power-of-ten scale slip, a sign flip, or other."""
    if theirs_cr == 0 or ours_cr == 0:
        return "zero"
    ratio = ours_cr / theirs_cr
    if ratio < 0:
        return "sign"
    for power in range(2, 10):
        for target in (Decimal(10) ** power, Decimal(1) / Decimal(10) ** power):
            if abs(ratio / target - Decimal(1)) <= Decimal("0.03"):
                return f"scale x{'1e' + str(power) if ratio > 1 else '1e-' + str(power)}"
    if Decimal("1.5") <= ratio <= Decimal("5"):
        return "1.5x-5x (gross vs net revenue, excise, restated scope)"
    return "other"


def _money_match(ours_rupees: Decimal, theirs_crore: Decimal) -> tuple[bool, Decimal]:
    ours_cr = ours_rupees / CRORE
    diff = abs(ours_cr - theirs_crore)
    tolerance = max(Decimal("1"), abs(theirs_crore) * Decimal("0.005"))
    return diff <= tolerance, _rel(ours_cr, theirs_crore)


def _eps_match(ours: Decimal, theirs: Decimal) -> tuple[str, Decimal]:
    """'exact' | 'ca_ratio' | 'mismatch', with the relative error to the raw figure."""
    if theirs == 0:
        return ("exact" if ours == 0 else "mismatch"), _rel(ours, theirs)
    if abs(ours - theirs) <= max(Decimal("0.02"), abs(theirs) * Decimal("0.01")):
        return "exact", _rel(ours, theirs)
    for ratio in _CA_RATIOS:
        if abs(ours / theirs - ratio) <= Decimal("0.02") * ratio:
            return "ca_ratio", _rel(ours, theirs)
    return "mismatch", _rel(ours, theirs)


def _load_ours(data_root: Path, isins: list[str]) -> dict[str, list[tuple]]:
    con = duckdb.connect()
    pit = f"read_parquet('{data_root}/L1/pit_fundamentals/*/*.parquet', hive_partitioning=1)"
    rows = con.execute(
        f"""
        with ranked as (
          select isin, period_start, period_end, filing_date, nature, concept, value,
                 row_number() over (partition by isin, period_start, period_end, nature, concept
                                    order by filing_date desc) as rn
          from {pit}
          where segment is null and isin in (select unnest($isins))
        )
        select isin, period_start, period_end, filing_date, nature, concept, value
        from ranked where rn = 1
        """,
        {"isins": isins},
    ).fetchall()
    out: dict[str, list[tuple]] = defaultdict(list)
    for r in rows:
        out[str(r[0])].append(r)
    return out


def _last_close(data_root: Path, isins: list[str]) -> tuple[date, dict[str, Decimal]]:
    con = duckdb.connect()
    prices = f"read_parquet('{data_root}/L1/prices_raw/*/*.parquet', hive_partitioning=1)"
    last = con.execute(f"select max(trade_date) from {prices} where series='EQ'").fetchone()
    assert last is not None
    rows = con.execute(
        f"select isin, close from {prices} where series='EQ' and trade_date=$d "
        "and isin in (select unnest($isins))",
        {"d": last[0], "isins": isins},
    ).fetchall()
    return last[0], {str(i): Decimal(c) for i, c in rows}


def _by_period(
    ours: list[tuple], nature: str, concept: str, *, quarterly: bool
) -> dict[date, Decimal]:
    out: dict[date, Decimal] = {}
    for _isin, ps, pe, _fd, nat, con, val in ours:
        if nat != nature or con != concept or ps is None:
            continue
        days = (pe - ps).days
        is_q = 80 <= days <= 100
        is_fy = 350 <= days <= 380
        if (quarterly and is_q) or (not quarterly and is_fy):
            out[pe] = Decimal(val)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=Path, required=True)
    ap.add_argument("--data-root", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    args = ap.parse_args(argv)

    sample = json.loads((args.pages / "sample.json").read_text(encoding="utf-8"))
    isins = [s["isin"] for s in sample]
    ours_all = _load_ours(args.data_root, isins)
    close_date, closes = _last_close(args.data_root, isins)

    tallies: dict[str, Tally] = defaultdict(Tally)
    eps_buckets: Counter[str] = Counter()
    eps_worst: list[tuple[Decimal, str, str, Decimal, Decimal]] = []
    companies = 0
    parsed_fail: list[str] = []
    no_overlap: list[str] = []
    price_ratios: list[Decimal] = []
    mcap_ratios: list[Decimal] = []
    pe_ratios: list[Decimal] = []
    bv_ratios: list[Decimal] = []
    fv_exact = fv_total = 0
    quarters_overlap: list[int] = []

    for entry in sample:
        sym, isin, consolidated = entry["symbol"], entry["isin"], bool(entry["consolidated"])
        page = args.pages / f"{sym}{'_consolidated' if consolidated else ''}.html"
        if not page.exists():
            continue
        try:
            data = parse_html(
                page.read_text(encoding="utf-8", errors="replace"), filename=page.name, symbol=sym
            )
        except Exception as exc:
            parsed_fail.append(f"{sym}: {type(exc).__name__}: {exc}")
            continue
        companies += 1
        nature = "Consolidated" if consolidated else "Standalone"
        ours = ours_all.get(isin, [])
        by_stmt: dict[str, dict[str, dict[str, Decimal]]] = defaultdict(lambda: defaultdict(dict))
        for d in data:
            by_stmt[d.statement][_clean(d.metric)][d.period] = d.value

        # ── quarterly ──
        q_sales = by_stmt["quarters"].get("Sales") or by_stmt["quarters"].get("Revenue") or {}
        q_np = by_stmt["quarters"].get("Net Profit", {})
        q_eps = by_stmt["quarters"].get("EPS in Rs", {})
        our_rev = _by_period(ours, nature, "revenue_from_operations", quarterly=True)
        our_pat = _by_period(ours, nature, "profit_after_tax", quarterly=True)
        our_eps = _by_period(ours, nature, "eps_basic", quarterly=True)
        overlap = 0
        for label, theirs in q_np.items():
            pe = _period_end(label)
            if pe is None or pe not in our_pat:
                continue
            overlap += 1
            ok, rel = _money_match(our_pat[pe], theirs)
            tallies["Quarterly Net Profit"].add(rel, ok, sym, label, our_pat[pe] / CRORE, theirs)
        for label, theirs in q_sales.items():
            pe = _period_end(label)
            if pe is None or pe not in our_rev:
                continue
            ok, rel = _money_match(our_rev[pe], theirs)
            tallies["Quarterly Sales"].add(rel, ok, sym, label, our_rev[pe] / CRORE, theirs)
        for label, theirs in q_eps.items():
            pe = _period_end(label)
            if pe is None or pe not in our_eps:
                continue
            bucket, rel = _eps_match(our_eps[pe], theirs)
            eps_buckets[bucket] += 1
            if bucket == "mismatch":
                eps_worst.append((rel, sym, label, our_eps[pe], theirs))
        if overlap == 0:
            no_overlap.append(sym)
        quarters_overlap.append(overlap)
        # TTM net profit over the four latest quarters both sides have: earnings apart from price.
        common = sorted(
            pe for label in q_np if (pe := _period_end(label)) is not None and pe in our_pat
        )
        if len(common) >= 4:
            last4 = common[-4:]
            theirs_ttm = sum(q_np[label] for label in q_np if _period_end(label) in last4)
            ours_ttm = sum(our_pat[pe] for pe in last4)
            ok, rel = _money_match(ours_ttm, theirs_ttm)
            tallies["TTM Net Profit (4 latest common quarters)"].add(
                rel, ok, sym, f"to {last4[-1]}", ours_ttm / CRORE, theirs_ttm
            )

        # ── annual ──
        a_sales = by_stmt["profit_loss"].get("Sales") or by_stmt["profit_loss"].get("Revenue") or {}
        a_np = by_stmt["profit_loss"].get("Net Profit", {})
        our_rev_fy = _by_period(ours, nature, "revenue_from_operations", quarterly=False)
        our_pat_fy = _by_period(ours, nature, "profit_after_tax", quarterly=False)
        for label, theirs in a_np.items():
            pe = _period_end(label)
            if pe is None or pe not in our_pat_fy:
                continue
            ok, rel = _money_match(our_pat_fy[pe], theirs)
            tallies["Annual Net Profit"].add(rel, ok, sym, label, our_pat_fy[pe] / CRORE, theirs)
        for label, theirs in a_sales.items():
            pe = _period_end(label)
            if pe is None or pe not in our_rev_fy:
                continue
            ok, rel = _money_match(our_rev_fy[pe], theirs)
            tallies["Annual Sales"].add(rel, ok, sym, label, our_rev_fy[pe] / CRORE, theirs)

        # ── balance sheet (annual) ──
        eq_cap = by_stmt["balance_sheet"].get("Equity Capital", {})
        reserves = by_stmt["balance_sheet"].get("Reserves", {})
        our_paidup = _by_period(ours, nature, "paid_up_equity_capital", quarterly=False)
        our_paidup_q = _by_period(ours, nature, "paid_up_equity_capital", quarterly=True)
        our_res = _by_period(ours, nature, "reserves_excl_revaluation", quarterly=False)
        for label, theirs in eq_cap.items():
            pe = _period_end(label)
            if pe is None:
                continue
            mine = our_paidup.get(pe, our_paidup_q.get(pe))
            if mine is None:
                continue
            ok, rel = _money_match(mine, theirs)
            tallies["Equity Capital (paid-up)"].add(rel, ok, sym, label, mine / CRORE, theirs)
        for label, theirs in reserves.items():
            pe = _period_end(label)
            if pe is None or pe not in our_res:
                continue
            ok, rel = _money_match(our_res[pe], theirs)
            tallies["Reserves (ours excl. revaluation)"].add(
                rel, ok, sym, label, our_res[pe] / CRORE, theirs
            )

        # ── ratios (loose bands: a days-old close vs a live price) ──
        ratios = {k: v.get("current") for k, v in by_stmt["ratios"].items() if "current" in v}
        fv_theirs = ratios.get("Face Value")
        our_fv = [
            Decimal(v)
            for (_i, _ps, _pe, _fd, nat, con, v) in sorted(ours, key=lambda r: (r[3], r[2]))
            if con == "face_value_per_share"
        ]
        if fv_theirs is not None and our_fv:
            fv_total += 1
            fv_exact += our_fv[-1] == fv_theirs
        close = closes.get(isin)
        shares = [
            Decimal(v)
            for (_i, _ps, _pe, _fd, nat, con, v) in sorted(ours, key=lambda r: (r[3], r[2]))
            if con == "shares_outstanding"
        ]
        if close is not None and ratios.get("Current Price"):
            price_ratios.append(close / ratios["Current Price"])
        if close is not None and shares and ratios.get("Market Cap"):
            mcap_ratios.append((close * shares[-1] / CRORE) / ratios["Market Cap"])
        # P/E: our market cap / our TTM PAT (latest four consecutive quarters)
        if close is not None and shares and ratios.get("Stock P/E") and our_pat:
            ends = sorted(our_pat)
            if len(ends) >= 4:
                last4 = ends[-4:]
                if all(85 <= (b - a).days <= 97 for a, b in pairwise(last4)):
                    ttm = sum(our_pat[e] for e in last4)
                    if ttm > 0:
                        pe_ratios.append((close * shares[-1] / ttm) / ratios["Stock P/E"])
        equity = [
            Decimal(v)
            for (_i, _ps, _pe, _fd, nat, con, v) in sorted(ours, key=lambda r: (r[3], r[2]))
            if con == "shareholders_equity_excl_revaluation" and nat == nature
        ]
        if equity and shares and ratios.get("Book Value"):
            bv_ratios.append((equity[-1] / shares[-1]) / ratios["Book Value"])

    def dist(values: list[Decimal]) -> str:
        if not values:
            return "n/a"
        fl = sorted(float(v) for v in values)
        within5 = sum(1 for v in fl if 0.95 <= v <= 1.05)
        within15 = sum(1 for v in fl if 0.85 <= v <= 1.15)
        return (
            f"n={len(fl)} · median {statistics.median(fl):.3f} · "
            f"within 5%: {100 * within5 / len(fl):.0f}% · "
            f"within 15%: {100 * within15 / len(fl):.0f}% · min {fl[0]:.2f} · max {fl[-1]:.2f}"
        )

    lines: list[str] = []
    lines.append("# Fundamentals sanity check — PIT store vs Screener company pages\n")
    n_top = sum(1 for s in sample if s["bucket"] == "top")
    n_random = sum(1 for s in sample if s["bucket"] != "top")
    today = SystemClock().today().isoformat()
    lines.append(
        f"*Generated by `ops/sanity/screener_compare.py` on {today} over "
        f"{companies} companies ({n_top} most-liquid by turnover on {close_date.isoformat()} plus "
        f"{n_random} drawn at random from the rest), one robots-permitted page each at the "
        f"register's 5 s cadence. Pages live in a scratch folder; nothing here wrote to any store "
        f"(invariant #8).*\n"
    )
    lines.append("## What agrees\n")
    lines.append("| Comparison | Pairs compared | Agreement (±max(₹1 Cr, 0.5%)) |")
    lines.append("| --- | --- | --- |")
    for name in (
        "Quarterly Sales",
        "Quarterly Net Profit",
        "TTM Net Profit (4 latest common quarters)",
        "Annual Sales",
        "Annual Net Profit",
        "Equity Capital (paid-up)",
        "Reserves (ours excl. revaluation)",
    ):
        t = tallies[name]
        lines.append(f"| {name} | {t.compared} | {t.rate} |")
    lines.append("")
    lines.append("### Relative-error distribution (all pairs)\n")
    lines.append("| Comparison | <=0.5% | 0.5-2% | 2-10% | 10-50% | >50% |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for name, t in tallies.items():
        h = t.histogram
        cells = " | ".join(str(h[k]) for k in ("<=0.5%", "0.5-2%", "2-10%", "10-50%", ">50%"))
        lines.append(f"| {name} | {cells} |")
    lines.append("")
    lines.append("### Why the disagreements disagree\n")
    lines.append("| Comparison | Disagreements | Breakdown |")
    lines.append("| --- | --- | --- |")
    for name, t in tallies.items():
        if t.reasons:
            breakdown = ", ".join(f"{k}: {v}" for k, v in t.reasons.most_common())
            lines.append(f"| {name} | {sum(t.reasons.values())} | {breakdown} |")
    eps_total = sum(eps_buckets.values())
    if eps_total:
        lines.append("")
        lines.append("### Quarterly EPS — filed figure vs Screener's re-based figure\n")
        lines.append("| Bucket | Pairs | Share |")
        lines.append("| --- | --- | --- |")
        for b, label in (
            ("exact", "identical (±1%)"),
            ("ca_ratio", "differ by a clean bonus/split ratio (Screener re-based history)"),
            ("mismatch", "unexplained"),
        ):
            share = 100 * eps_buckets.get(b, 0) / eps_total
            lines.append(f"| {label} | {eps_buckets.get(b, 0)} | {share:.1f}% |")
    lines.append("")
    lines.append(
        "### Valuation fields (our latest close vs Screener's live figure — loose bands)\n"
    )
    lines.append(f"- **Current Price** ratio ours/theirs: {dist(price_ratios)}")
    lines.append(f"- **Market Cap** ratio ours/theirs: {dist(mcap_ratios)}")
    lines.append(f"- **Stock P/E** ratio ours/theirs: {dist(pe_ratios)}")
    lines.append(f"- **Book Value / share** ratio ours/theirs: {dist(bv_ratios)}")
    lines.append(f"- **Face Value** exact: {fv_exact}/{fv_total}")
    if quarters_overlap:
        lines.append(
            f"- Quarters compared per company: median {statistics.median(quarters_overlap):.0f}, "
            f"min {min(quarters_overlap)}, max {max(quarters_overlap)}; "
            f"companies with no overlapping quarter: {len(no_overlap)}"
        )
    lines.append("")
    lines.append("## Worst disagreements (top 15 per field, by relative error)\n")
    for name, t in tallies.items():
        if not t.worst:
            continue
        lines.append(f"### {name}\n")
        lines.append("| Symbol | Period | Ours (₹ Cr) | Screener (₹ Cr) | Rel. error |")
        lines.append("| --- | --- | --- | --- | --- |")
        for rel, sym, period, ours_v, theirs_v in sorted(t.worst, reverse=True)[:15]:
            lines.append(f"| {sym} | {period} | {ours_v:.2f} | {theirs_v} | {float(rel):.1%} |")
        lines.append("")
    if eps_worst:
        lines.append("### Quarterly EPS — unexplained\n")
        lines.append("| Symbol | Period | Ours (₹) | Screener (₹) | Rel. error |")
        lines.append("| --- | --- | --- | --- | --- |")
        for rel, sym, period, ours_v, theirs_v in sorted(eps_worst, reverse=True)[:25]:
            lines.append(f"| {sym} | {period} | {ours_v} | {theirs_v} | {float(rel):.1%} |")
        lines.append("")
    if no_overlap:
        lines.append(
            f"## Companies with no overlapping quarter\n\n{', '.join(sorted(no_overlap))}\n"
        )
    if parsed_fail:
        lines.append("## Pages the M7.1 parser could not read\n")
        lines.extend(f"- {f}" for f in parsed_fail)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:40]))
    print(f"... report written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
