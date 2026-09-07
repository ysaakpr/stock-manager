"""Adjusted-price sanity check — L2 ``adj_close`` vs Yahoo Finance's split-adjusted close.

`yfinance_price_check.py` compares L1 *raw* closes with Yahoo for the trailing year; it says whether
the bhavcopy landed intact. This script asks the question that check cannot: **did the factor chain
adjust the right events by the right amount?** Yahoo's ``Close`` (``auto_adjust=False``) is
split- and bonus-adjusted but not dividend-adjusted — the same semantics as our ``adj_close`` — so
over a decade the ratio ``ours / yahoo`` should sit at 1.0 for every session. Where it does not, it
steps: a *ratio shift* is a session on which one side applied an adjustment the other did not, and
its size names the event (x0.5 = a 1:1 bonus one side missed, x0.2 = a 5:1 split, ...).

What it does: takes the most-liquid NSE EQ names on the lake's last session plus any symbols named
on the command line, reads their L2 partitions, downloads Yahoo's history in one batched call, and
reports per name the agreement rate, the persistent ratio shifts and the one-day blips (a shift that
reverses on the next session is a bad Yahoo print, not an adjustment). Read-only against the lake;
the report goes wherever ``--report`` says.

What it assumes: the NSE ticker is ``<symbol>.NS`` and Yahoo carries history under the *current*
symbol across ISIN reissues — which is exactly what makes this a test of the D2 lineage stitch.

What it never does: write to any store, treat Yahoo as authoritative (it has its own glitches — see
the blips), or adjust for demergers and rights the way Yahoo does. A demerger is a structural break
here by design (EXECUTION_PLAN §4.3), so a persistent shift on a demerger ex-date is a semantic
difference, not a defect; the report says which shifts coincide with a DEMERGER or RIGHTS action so
a reader can tell the two apart.

    uv run python ops/sanity/l2_yahoo_check.py --data-root data --report ops/gates/l2-vs-yahoo.md \
        --names 60 --extra UNOMINDA,HINDPETRO,IRCTC
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import warnings
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import yfinance as yf

from dataplatform.config import get_settings
from dataplatform.store.db import connection

#: Ratio sizes that name an event, with the tolerance a decimal factor rounds inside.
_EVENT_SIZES: tuple[tuple[float, str], ...] = (
    (2.0, "x2 (1:1 bonus or 2:1 split)"),
    (0.5, "x1/2 (1:1 bonus or 2:1 split)"),
    (3.0, "x3 (2:1 bonus)"),
    (1 / 3, "x1/3 (2:1 bonus)"),
    (1.5, "x1.5 (1:2 bonus)"),
    (1 / 1.5, "x1/1.5 (1:2 bonus)"),
    (1.25, "x1.25 (1:4 bonus)"),
    (0.8, "x1/1.25 (1:4 bonus)"),
    (5.0, "x5 (5:1 split)"),
    (0.2, "x1/5 (5:1 split)"),
    (10.0, "x10 (10:1 split)"),
    (0.1, "x1/10 (10:1 split)"),
    (4.0, "x4"),
    (0.25, "x1/4"),
    (2.5, "x2.5"),
    (0.4, "x1/2.5"),
)


def _label(size: float) -> str:
    for k, name in _EVENT_SIZES:
        if abs(size / k - 1) < 0.03:
            return name
    return f"x{size:.3f} (other)"


def _sample(
    prices: pa.Table, names: int, extra: list[str]
) -> tuple[list[str], dict[str, str], dt.date, list[str]]:
    """The ISINs to compare: the `names` most liquid on the last session, then `extra` symbols."""
    nse_eq = prices.filter(
        pc.and_(pc.equal(prices.column("exchange"), "NSE"), pc.equal(prices.column("series"), "EQ"))
    )
    last = pc.max(nse_eq.column("trade_date")).as_py()
    rows = nse_eq.filter(pc.equal(nse_eq.column("trade_date"), last)).sort_by(
        [("total_traded_value", "descending")]
    )
    isins = rows.column("isin").to_pylist()
    symbols = rows.column("symbol").to_pylist()
    sym_by_isin = dict(zip(isins, symbols, strict=True))
    isin_by_sym = dict(zip(symbols, isins, strict=True))
    top = [i for i in isins if not i.startswith("INF")][:names]
    picked = list(dict.fromkeys(top + [isin_by_sym[s] for s in extra if s in isin_by_sym]))
    absent = [s for s in extra if s not in isin_by_sym]
    return picked, sym_by_isin, last, absent


def _shifts(ratio: pd.Series) -> list[tuple[dt.date, float, float, float, bool]]:
    """Sessions on which ours/yahoo moved by more than 5 %, flagging one-session reversals."""
    step = ratio / ratio.shift(1)
    idx = list(ratio.index)
    out: list[tuple[dt.date, float, float, float, bool]] = []
    for pos, (day, size) in enumerate(step.items()):
        if pd.isna(size) or 0.95 <= size <= 1.05:
            continue
        before = float(ratio.iloc[pos - 1])
        after = float(ratio.iloc[pos])
        # A blip is a bad print on one side: the ratio leaves and returns within a session. Both
        # legs are flagged — the departure (tomorrow returns to `before`) and the return (today
        # lands where the day before yesterday was).
        departs = pos + 1 < len(idx) and abs(float(ratio.iloc[pos + 1]) / before - 1) < 0.01
        returns = pos >= 2 and abs(after / float(ratio.iloc[pos - 2]) - 1) < 0.01
        out.append((day.date(), before, after, float(size), departs or returns))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-root", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--names", type=int, default=60)
    ap.add_argument("--since", type=dt.date.fromisoformat, default=dt.date(2016, 9, 1))
    ap.add_argument("--extra", type=str, default="", help="comma-separated NSE symbols to add")
    ap.add_argument("--tolerance", type=float, default=0.005)
    args = ap.parse_args(argv)
    warnings.filterwarnings("ignore")
    extra = [s.strip() for s in args.extra.split(",") if s.strip()]

    prices = ds.dataset(
        args.data_root / "L1" / "prices_raw", format="parquet", partitioning="hive"
    ).to_table(columns=["isin", "exchange", "symbol", "series", "trade_date", "total_traded_value"])
    sample, sym_by_isin, last, absent = _sample(prices, args.names, extra)
    l2 = ds.dataset(
        args.data_root / "L2" / "prices_adjusted", format="parquet", partitioning="hive"
    ).to_table(columns=["isin", "trade_date", "adj_close"])
    l2 = l2.filter(pc.is_in(l2.column("isin"), pa.array(sample)))

    # Which shifts sit on a demerger/rights ex-date — a semantic difference, not a defect.
    structural: dict[str, set[dt.date]] = collections.defaultdict(set)
    with connection(get_settings()) as conn:
        for isin, ex_date in conn.execute(
            "SELECT isin, ex_date FROM corporate_actions WHERE action_type IN "
            "('DEMERGER', 'RIGHTS', 'SCHEME_OF_ARRANGEMENT') AND isin = ANY(%s)",
            (sample,),
        ):
            structural[isin].add(ex_date)

    tickers = [sym_by_isin[i] + ".NS" for i in sample]
    frame = yf.download(
        tickers,
        start=args.since.isoformat(),
        end=(last + dt.timedelta(days=1)).isoformat(),
        auto_adjust=False,
        group_by="ticker",
        threads=True,
        progress=False,
    )

    rows: list[dict[str, Any]] = []
    totals = collections.Counter[str]()
    persistent: list[tuple[str, dt.date, float, float, str, str]] = []
    blips: list[tuple[str, dt.date]] = []
    for isin in sample:
        sym = sym_by_isin[isin]
        part = l2.filter(pc.equal(l2.column("isin"), isin))
        ours = pd.Series(
            [float(x) for x in part.column("adj_close").to_pylist()],
            index=pd.to_datetime(part.column("trade_date").to_pylist()),
            dtype=float,
        ).sort_index()
        row: dict[str, Any] = {"symbol": sym, "isin": isin, "l2_sessions": len(ours)}
        if ours.empty:
            totals["no_l2"] += 1
            row.update(compared=0, note="no L2 partition", shifts="")
            rows.append(row)
            continue
        try:
            yahoo = frame[sym + ".NS"]["Close"].dropna()
        except KeyError:
            yahoo = pd.Series(dtype=float)
        if yahoo.empty:
            totals["no_yahoo"] += 1
            row.update(compared=0, note="no Yahoo history", shifts="")
            rows.append(row)
            continue
        yahoo.index = pd.to_datetime(yahoo.index).tz_localize(None).normalize()
        joined = pd.concat([ours.rename("ours"), yahoo.rename("yahoo")], axis=1, join="inner")
        joined = joined[(joined.ours > 0) & (joined.yahoo > 0)]
        ratio = joined.ours / joined.yahoo
        agree = int((abs(ratio - 1) <= args.tolerance).sum())
        totals["compared"] += len(joined)
        totals["agree"] += agree
        shifts = _shifts(ratio)
        described: list[str] = []
        for day, before, after, size, blip in shifts:
            if blip:
                blips.append((sym, day))
                described.append(f"{day}: one-day Yahoo blip")
                continue
            kind = (
                "structural (demerger/rights ex-date)" if day in structural[isin] else _label(size)
            )
            persistent.append((sym, day, before, after, kind, isin))
            described.append(f"{day}: {before:.4f}->{after:.4f} {kind}")
        clean = (
            not any(not b for *_, b in shifts) and abs(float(ratio.median()) - 1) <= args.tolerance
        )
        totals["clean"] += int(clean)
        row.update(
            compared=len(joined),
            note=f"{agree / len(joined):.1%} within {args.tolerance:.1%}; "
            f"median ours/yahoo {float(ratio.median()):.4f}",
            shifts="; ".join(described),
        )
        rows.append(row)

    compared = totals["compared"] or 1
    lines = [
        "# Adjusted-price sanity check — L2 `adj_close` vs Yahoo Finance split-adjusted closes",
        "",
        f"*Generated by `ops/sanity/l2_yahoo_check.py` on {dt.date.today()}: the {args.names} "
        f"most-liquid NSE names on {last} plus {len(sample) - min(args.names, len(sample))} "
        f"named on the command line, every common session since {args.since}, one batched "
        "yfinance download with `auto_adjust=False` (Yahoo's Close is split- and bonus-adjusted, "
        "not dividend-adjusted — the semantics of our `adj_close`). Read-only against the lake.*",
        "",
        "| Outcome | Value |",
        "| --- | --- |",
        f"| names compared | {sum(1 for r in rows if r['compared'])} |",
        f"| sessions compared | {totals['compared']} |",
        f"| within {args.tolerance:.1%} | {totals['agree']} ({totals['agree'] / compared:.2%}) |",
        f"| names fully consistent (median ratio 1, no persistent shift) | {totals['clean']} |",
        f"| names with no L2 partition | {totals['no_l2']} |",
        f"| names with no Yahoo history | {totals['no_yahoo']} |",
        f"| persistent ratio shifts | {len(persistent)} across "
        f"{len({p[0] for p in persistent})} names |",
        f"| one-day Yahoo blips (ignored) | {len(blips)} |",
    ]
    if absent:
        lines += ["", f"Not trading as NSE EQ on {last}, skipped: {', '.join(absent)}."]
    lines += [
        "",
        "## Persistent shifts — one side adjusted an event the other did not",
        "",
        "| Symbol | Session | ours/yahoo before | after | reading |",
        "| --- | --- | --- | --- | --- |",
    ]
    for sym, day, before, after, kind, _ in sorted(persistent, key=lambda p: (p[0], p[1])):
        lines.append(f"| {sym} | {day} | {before:.4f} | {after:.4f} | {kind} |")
    lines += [
        "",
        "A ratio *above* 1 before the session means our history is higher than Yahoo's: an event "
        "Yahoo adjusted and we did not (or a demerger we keep as a structural break). A ratio "
        "*below* 1 means we adjusted something Yahoo did not.",
        "",
        "## Every name",
        "",
        "| Symbol | ISIN | L2 sessions | compared | agreement | shifts |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            f"| {r['symbol']} | {r['isin']} | {r['l2_sessions']} | {r['compared']} | "
            f"{r['note']} | {r['shifts']} |"
        )
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:16]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
