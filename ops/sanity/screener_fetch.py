"""Fetch a sample of Screener company pages into a *scratch* directory for a sanity comparison.

This is the network half of the fundamentals sanity check (ops/sanity/screener_compare.py is the
offline half). It exists to answer one question — do the numbers we derived from NSE XBRL filings
agree with an independent publisher's statement of the same accounting facts? — and nothing else.

What it does: picks a sample of ISINs that have PIT fundamentals, resolves each to its current NSE
symbol through the D2 identity master (invariant #2: the symbol is looked up, never trusted as a
key), chooses the standalone or consolidated page to match the nature of our latest filing, and
fetches one page per company through the M7.1 crawler's URL policy (`ScreenerCrawler.guard`, so a
robots-disallowed path is impossible by construction), spaced at the register's 5 s cadence, with a
hard stop on a 403 streak.

What it never does: write to the L0 lake, to `pit_fundamentals`, or to any store the decision path
can read. Pages land in the scratch directory passed on the command line and nowhere else —
invariant #8 keeps restated data out of decisions, and "comparison input in a scratch folder" is
the whole reach of this script. It also never retries with a different UA or routes around a block.

Usage:
    uv run python ops/sanity/screener_fetch.py --out <scratch-dir> --top 250 --random 50
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import duckdb
import httpx

from dataplatform.clock import SystemClock
from dataplatform.config import get_settings
from dataplatform.ingest.screener import ScreenerCrawler
from dataplatform.store.db import connection

SPACING_SECONDS = (
    5.0  # source_register.yaml, screener robots_notes: "per-company cadence only, 5 s"
)
FORBIDDEN_STREAK_LIMIT = 3


def _sample(data_root: Path, top: int, rand: int, seed: int) -> list[dict[str, object]]:
    con = duckdb.connect()
    pit = f"read_parquet('{data_root}/L1/pit_fundamentals/*/*.parquet', hive_partitioning=1)"
    prices = f"read_parquet('{data_root}/L1/prices_raw/*/*.parquet', hive_partitioning=1)"
    # Latest nature per ISIN: the nature of the most recent filing (consolidated wins a tie).
    natures = con.execute(
        f"""
        with latest as (
          select isin, max(filing_date) fd from {pit} group by isin
        )
        select p.isin, max(case when p.nature='Consolidated' then 1 else 0 end) as consolidated
        from {pit} p join latest l on p.isin=l.isin and p.filing_date=l.fd
        group by p.isin
        """
    ).fetchall()
    nature_of = {isin: bool(c) for isin, c in natures}
    last_session = con.execute(f"select max(trade_date) from {prices} where series='EQ'").fetchone()
    assert last_session is not None
    turnover = con.execute(
        f"select isin, total_traded_value from {prices} "
        f"where series='EQ' and trade_date = $d order by total_traded_value desc",
        {"d": last_session[0]},
    ).fetchall()
    ranked = [str(isin) for isin, _ in turnover if str(isin) in nature_of]
    head = ranked[:top]
    tail_pool = ranked[top:]
    rng = random.Random(seed)
    tail = rng.sample(tail_pool, min(rand, len(tail_pool)))
    chosen = head + tail
    with connection(get_settings()) as conn:
        rows = conn.execute(
            "select isin, symbol from symbol_history where exchange='NSE' and valid_to is null"
        ).fetchall()
    symbol_of = {str(isin): str(sym) for isin, sym in rows}
    out = []
    for isin in chosen:
        sym = symbol_of.get(isin)
        if sym is None:
            continue
        out.append(
            {
                "isin": isin,
                "symbol": sym,
                "consolidated": nature_of[isin],
                "bucket": "top" if isin in head else "random",
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument("--top", type=int, default=250)
    ap.add_argument("--random", type=int, default=50)
    ap.add_argument("--seed", type=int, default=20260906)
    args = ap.parse_args(argv)
    settings = get_settings()
    data_root = args.data_root or settings.data_root
    args.out.mkdir(parents=True, exist_ok=True)
    sample = _sample(data_root, args.top, args.random, args.seed)
    (args.out / "sample.json").write_text(json.dumps(sample, indent=1), encoding="utf-8")
    crawler = ScreenerCrawler.from_register()
    headers = {"User-Agent": settings.http_user_agent, "Accept": "text/html"}
    log = (args.out / "fetch_log.jsonl").open("a", encoding="utf-8")
    forbidden_streak = 0
    done = 0
    with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as client:
        for entry in sample:
            sym = str(entry["symbol"])
            consolidated = bool(entry["consolidated"])
            target = args.out / f"{sym}{'_consolidated' if consolidated else ''}.html"
            if target.exists():
                continue
            try:
                url = crawler.company_url(sym, consolidated=consolidated)
                crawler.guard(url)  # refuses anything robots disallows, before a socket exists
            except Exception as exc:
                log.write(json.dumps({"symbol": sym, "error": f"policy: {exc}"}) + "\n")
                continue
            started = time.monotonic()
            try:
                resp = client.get(url)
                status = resp.status_code
            except httpx.HTTPError as exc:
                log.write(json.dumps({"symbol": sym, "url": url, "error": str(exc)}) + "\n")
                time.sleep(SPACING_SECONDS)
                continue
            if status == 403:
                forbidden_streak += 1
                if forbidden_streak >= FORBIDDEN_STREAK_LIMIT:
                    log.write(json.dumps({"halt": "403 streak", "at": sym}) + "\n")
                    print("HALT: 403 streak — stopping, not evading", file=sys.stderr)
                    return 3
            else:
                forbidden_streak = 0
            if status == 200:
                target.write_bytes(resp.content)
                done += 1
            log.write(
                json.dumps(
                    {
                        "symbol": sym,
                        "url": url,
                        "status": status,
                        "bytes": len(resp.content),
                        "fetched_on": SystemClock().today().isoformat(),
                    }
                )
                + "\n"
            )
            log.flush()
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, SPACING_SECONDS - elapsed))
    print(f"fetched {done} pages into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
