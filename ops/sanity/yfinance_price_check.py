"""Sanity-check L1 raw closes against Yahoo Finance's unadjusted closes for a sample of names.

The M2 golden suite uses yfinance as its ratified "reference A" (AGENTIC_CONTEXT B2). This script
uses the same source for a broader, cheaper question: do our raw NSE closes agree with an
independent feed's *unadjusted* closes, name by name, session by session, over the last year?
Both should be the exchange's official close, so a disagreement is either a Yahoo data error or a
defect in our bhavcopy → L1 path — and the classification below tells them apart (a Yahoo error is
isolated; ours would be systematic).

Reads the lake read-only; writes only the markdown report. Never imported by product code.

Usage:
    uv run python ops/sanity/yfinance_price_check.py --data-root <lake> --report <md> --names 60
"""

from __future__ import annotations

import argparse
import statistics
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import yfinance as yf

from dataplatform.clock import SystemClock
from dataplatform.config import get_settings
from dataplatform.store.db import connection


def _sample(data_root: Path, names: int) -> tuple[date, list[tuple[str, str]]]:
    con = duckdb.connect()
    prices = f"read_parquet('{data_root}/L1/prices_raw/*/*.parquet', hive_partitioning=1)"
    last = con.execute(f"select max(trade_date) from {prices} where series='EQ'").fetchone()
    assert last is not None
    ranked = con.execute(
        f"select isin from {prices} where series='EQ' and trade_date=$d "
        "order by total_traded_value desc limit $n",
        {"d": last[0], "n": names * 2},
    ).fetchall()
    with connection(get_settings()) as conn:
        rows = conn.execute(
            "select isin, symbol from symbol_history where exchange='NSE' and valid_to is null"
        ).fetchall()
    symbol_of = {str(i): str(s) for i, s in rows}
    chosen = [(str(r[0]), symbol_of[str(r[0])]) for r in ranked if str(r[0]) in symbol_of]
    return last[0], chosen[:names]


def _ours(
    data_root: Path, isins: list[str], start: date, end: date
) -> dict[tuple[str, date], Decimal]:
    con = duckdb.connect()
    prices = f"read_parquet('{data_root}/L1/prices_raw/*/*.parquet', hive_partitioning=1)"
    rows = con.execute(
        f"select isin, trade_date, close from {prices} where series='EQ' "
        "and trade_date between $a and $b and isin in (select unnest($isins))",
        {"a": start, "b": end, "isins": isins},
    ).fetchall()
    return {(str(i), d): Decimal(c) for i, d, c in rows}


def _share(part: int, whole: int) -> str:
    return f"{100 * part / whole:.2f}%" if whole else "n/a"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--names", type=int, default=60)
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args(argv)

    last_session, sample = _sample(args.data_root, args.names)
    start = last_session - timedelta(days=args.days)
    isins = [i for i, _ in sample]
    ours = _ours(args.data_root, isins, start, last_session)
    tickers = [f"{sym}.NS" for _, sym in sample]
    frame = yf.download(
        tickers,
        start=start.isoformat(),
        end=(last_session + timedelta(days=1)).isoformat(),
        auto_adjust=False,
        actions=False,
        group_by="ticker",
        threads=False,
        progress=False,
    )

    exact = within_1pct = mismatch = missing_yahoo = missing_ours = 0
    per_name: dict[str, Counter[str]] = {}
    worst: list[tuple[Decimal, str, date, Decimal, Decimal]] = []
    for isin, sym in sample:
        counter: Counter[str] = Counter()
        ticker = f"{sym}.NS"
        try:
            series = (
                frame[ticker]["Close"].dropna() if len(tickers) > 1 else frame["Close"].dropna()
            )
        except KeyError:
            counter["ticker_absent"] += 1
            per_name[sym] = counter
            continue
        yahoo = {ts.date(): Decimal(str(round(float(v), 2))) for ts, v in series.items()}
        our_dates = {d for (i, d) in ours if i == isin}
        for d in sorted(our_dates | set(yahoo)):
            mine = ours.get((isin, d))
            theirs = yahoo.get(d)
            if mine is None:
                missing_ours += 1
                counter["missing_ours"] += 1
                continue
            if theirs is None:
                missing_yahoo += 1
                counter["missing_yahoo"] += 1
                continue
            rel = abs(mine - theirs) / theirs if theirs else Decimal(0)
            if abs(mine - theirs) <= Decimal("0.05"):
                exact += 1
                counter["exact"] += 1
            elif rel <= Decimal("0.01"):
                within_1pct += 1
                counter["within_1pct"] += 1
            else:
                mismatch += 1
                counter["mismatch"] += 1
                worst.append((rel, sym, d, mine, theirs))
        per_name[sym] = counter

    compared = exact + within_1pct + mismatch
    today = SystemClock().today().isoformat()
    lines = [
        "# Price sanity check — L1 raw closes vs Yahoo Finance unadjusted closes",
        "",
        f"*Generated by `ops/sanity/yfinance_price_check.py` on {today}: "
        f"the {len(sample)} most-liquid names on {last_session.isoformat()}, every session from "
        f"{start.isoformat()}, one batched yfinance download (reference A of the golden suite, "
        f"AGENTIC_CONTEXT B2). Read-only against the lake.*",
        "",
        "| Outcome | Sessions | Share |",
        "| --- | --- | --- |",
        f"| identical to the tick (±₹0.05) | {exact} | {100 * exact / compared:.2f}% |"
        if compared
        else "| n/a | 0 | |",
        f"| within 1% | {within_1pct} | {100 * within_1pct / compared:.2f}% |" if compared else "",
        f"| disagree by more than 1% | {mismatch} | {100 * mismatch / compared:.2f}% |"
        if compared
        else "",
        f"| session in our L1, absent from Yahoo | {missing_yahoo} | — |",
        f"| session in Yahoo, absent from our L1 | {missing_ours} | — |",
        "",
    ]
    bad_names = sorted(
        ((sym, c) for sym, c in per_name.items() if c["mismatch"] or c["ticker_absent"]),
        key=lambda kv: -kv[1]["mismatch"],
    )
    if bad_names:
        lines += [
            "## Names with disagreements",
            "",
            "| Symbol | exact | within 1% | >1% | missing (ours / Yahoo) |",
            "| --- | --- | --- | --- | --- |",
        ]
        for sym, c in bad_names[:30]:
            absent = " (ticker absent on Yahoo)" if c["ticker_absent"] else ""
            lines.append(
                f"| {sym} | {c['exact']} | {c['within_1pct']} | {c['mismatch']} | "
                f"{c['missing_ours']} / {c['missing_yahoo']}{absent} |"
            )
        lines.append("")
    if worst:
        lines += [
            "## Largest disagreements",
            "",
            "| Symbol | Session | Ours | Yahoo | Rel. error |",
            "| --- | --- | --- | --- | --- |",
        ]
        for rel, sym, d, mine, theirs in sorted(worst, reverse=True)[:25]:
            lines.append(f"| {sym} | {d.isoformat()} | {mine} | {theirs} | {float(rel):.2%} |")
        lines.append("")
    if per_name:
        exact_rates = [
            100 * c["exact"] / (c["exact"] + c["within_1pct"] + c["mismatch"])
            for c in per_name.values()
            if (c["exact"] + c["within_1pct"] + c["mismatch"])
        ]
        if exact_rates:
            lines.append(
                f"Per-name identical-to-tick rate: median {statistics.median(exact_rates):.1f}%, "
                f"min {min(exact_rates):.1f}%."
            )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:14]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
