"""M14.6 — per-stock daily entry variants of momentum v2: the signal panel and the forward check.

Two jobs, both research-only (nothing here is read by the paper job):

* ``panel`` — cut the daily momentum-v2 candidate sets (:mod:`backtest.signal_panel`) for every
  session of a span, both liquidity floors, with the exact ``_L1MomentumV2Data._compute`` every
  backtest uses. Two worker processes split the span at a month boundary; each month file is
  written atomically, so a killed build restarted with the same command redoes only what is
  missing. The campaign itself runs through ``backtest.m12_rerun --arm-set per-stock
  --signal-panel <dir>``, so it inherits that driver's resumable run directory and manifest.
* ``forward`` — replay each M14.6 arm from 2026-09-01 to the lake's last session (the paper book's
  live span) on both floors and write what each would hold, so the report can say what each
  variant would own today.

What it never does: write under the lake, fetch anything, or touch ``PAPER_RATIFIED_2026_09_06``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from decimal import Decimal
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from backtest.campaign import MAX_WORKERS, CampaignError
from backtest.run import (
    UNIVERSE_TURNOVER_FLOOR,
    UniverseParameters,
    _AdjustedCloseSource,
    _InvestableUniverse,
    _L1MomentumV2Data,
    _L1Reader,
    _RegimeSource,
)
from backtest.run_ledger import refuse_lake_location
from backtest.signal_panel import (
    SignalPanel,
    panel_key,
    signal_panel_in_force,
    write_month,
    write_panel_manifest,
)
from backtest.sweep import HIGH_FLOOR, LOW_FLOOR
from dataplatform.logging import get_logger
from dataplatform.query.service import QueryService

__all__ = ["build_panel", "forward_holdings", "main"]

_LOG = get_logger(__name__)

#: The paper book's live span opens here (M13.1's first paper session month).
FORWARD_START = date(2026, 9, 1)


def _subdir(universe: str, floor: Decimal) -> str:
    return f"{universe}-{floor}"


def _months(sessions: Sequence[date]) -> list[str]:
    return sorted({s.strftime("%Y-%m") for s in sessions})


def _build_chunk(
    out: str,
    months: list[str],
    floors: list[str],
    universe_name: str,
    data_root: str | None,
) -> int:
    """Compute and write ``months`` of every floor's series; return the sessions written.

    Module-level so a spawned worker can import it. One reader and one adjusted-close source serve
    every floor, so the per-session close cross-sections are read once for both.
    """
    root = Path(out)
    lake = Path(data_root) if data_root else None
    reader = _L1Reader(data_root=lake)
    service = QueryService(data_root=lake)
    written = 0
    try:
        calendar = reader.all_sessions()
        sessions = [s for s in calendar if s.strftime("%Y-%m") in set(months)]
        todo = [
            m
            for m in months
            if not all(
                (root / _subdir(universe_name, Decimal(f)) / f"{m}.json").is_file() for f in floors
            )
        ]
        if not todo:
            return 0
        closes = _AdjustedCloseSource(service, reader)
        regime = _RegimeSource.published(through=sessions[-1], ma_days=200, data_root=lake)
        sources = {
            floor: _L1MomentumV2Data(
                reader,
                (sessions[0],),
                regime,
                signal_closes=closes,
                universe_filter=_InvestableUniverse(
                    reader,
                    UniverseParameters.for_universe(
                        universe_name, median_turnover_floor=Decimal(floor)
                    ),
                    data_root=lake,
                ),
                lookback_sessions=calendar,
            )
            for floor in floors
        }
        for month in todo:
            month_sessions = [s for s in sessions if s.strftime("%Y-%m") == month]
            for floor, source in sources.items():
                body = {s: source.records_on(s) for s in month_sessions}
                write_month(root, _subdir(universe_name, Decimal(floor)), month, body)
            written += len(month_sessions)
            _LOG.info("m14_6.panel_month", month=month, sessions=len(month_sessions))
    finally:
        service.close()
        reader.close()
    return written


def build_panel(
    out: Path,
    *,
    start: date,
    end: date,
    floors: Sequence[Decimal] = (LOW_FLOOR, HIGH_FLOOR),
    universe_name: str = UNIVERSE_TURNOVER_FLOOR,
    data_root: Path | None,
    workers: int = 2,
    commit: str = "",
) -> SignalPanel:
    """Build (or finish) the panel for every session in ``[start, end]``; return it opened."""
    if not 1 <= workers <= MAX_WORKERS:
        raise CampaignError(f"workers must be 1..{MAX_WORKERS} on this box, got {workers}")
    out = refuse_lake_location(out, data_root)
    reader = _L1Reader(data_root=data_root)
    try:
        calendar = reader.all_sessions()
        label = reader.data_root_label
    finally:
        reader.close()
    sessions = [s for s in calendar if start <= s <= end]
    if not sessions:
        raise CampaignError(f"no sessions in {start.isoformat()}..{end.isoformat()}")
    months = _months(sessions)
    floor_text = [str(f) for f in floors]
    # Interleaved month split: both workers walk forward, each warming its own look-back cache.
    half = (len(months) + 1) // 2
    chunks = [months[:half], months[half:]] if workers == 2 else [months]
    root = str(data_root) if data_root else None
    if len(chunks) == 1:
        _build_chunk(str(out), chunks[0], floor_text, universe_name, root)
    else:
        with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
            futures = [
                pool.submit(_build_chunk, str(out), chunk, floor_text, universe_name, root)
                for chunk in chunks
                if chunk
            ]
            for future in futures:
                future.result()
    keys = {
        panel_key(
            repr(UniverseParameters.for_universe(universe_name, median_turnover_floor=floor)),
            adjusted=True,
            l1_isins_only=False,
            data_root=label,
        ): _subdir(universe_name, floor)
        for floor in floors
    }
    write_panel_manifest(
        out,
        {
            "first": sessions[0].isoformat(),
            "last": sessions[-1].isoformat(),
            "keys": keys,
            "lake_last_session": calendar[-1].isoformat(),
            "commit": commit,
            "universe": universe_name,
            "floors": floor_text,
            "data_root": label,
        },
    )
    return SignalPanel(out)


def forward_holdings(
    *,
    start: date,
    end: date,
    data_root: Path | None,
    panel: SignalPanel | None,
    floors: Sequence[Decimal] = (LOW_FLOOR, HIGH_FLOOR),
) -> dict[str, Any]:
    """Replay every M14.6 arm over ``[start, end]`` on both floors; what each holds at the end.

    Reported per arm and floor: the terminal session, the settled and in-settlement names with
    their share counts, the buys and sells decided on the terminal session (they fill on the
    session after it, the reserved fill-headroom session when ``end`` is the lake's last), cash,
    NAV and whether the regime filter read risk-on on the terminal session.
    """
    from analyst.journal.models import Decision
    from backtest.book_actions import corporate_actions_in_force, load_store_book_actions
    from backtest.cash_interest import accrue_cash_interest, load_repo_rate_schedule
    from backtest.m12_rerun import ARM_SETS
    from backtest.run import run_momentum_v2

    arms = ARM_SETS["per-stock"]
    regime = _RegimeSource.published(through=end, ma_days=200, data_root=data_root)
    out: dict[str, Any] = {"start": start.isoformat(), "end": end.isoformat(), "arms": {}}
    with (
        corporate_actions_in_force(
            load_store_book_actions(data_root=data_root), apply_to_book=True
        ),
        accrue_cash_interest(load_repo_rate_schedule()),
        signal_panel_in_force(panel),
    ):
        for arm in arms:
            assert arm.v2 is not None
            for floor in floors:
                run = run_momentum_v2(
                    start=start,
                    end=end,
                    v2_parameters=arm.v2,
                    data_root=data_root,
                    adjusted=True,
                    universe=UniverseParameters.for_universe(
                        UNIVERSE_TURNOVER_FLOOR, median_turnover_floor=floor
                    ),
                )
                terminal = run.terminal
                book = run.book
                last = [e for e in run.result.journal if e.trading_date == terminal]
                reading = regime.reading(terminal)
                out["arms"].setdefault(arm.label, {})[str(floor)] = {
                    "terminal": terminal.isoformat(),
                    "holdings": {h["isin"]: h["quantity"] for h in book.holdings},
                    "positions": {p["isin"]: p["quantity"] for p in book.positions},
                    "buys_decided_on_terminal": sorted(
                        e.isin for e in last if e.decision is Decision.BUY and e.isin
                    ),
                    "sells_decided_on_terminal": sorted(
                        e.isin for e in last if e.decision is Decision.SELL and e.isin
                    ),
                    "cash": str(book.cash),
                    "final_nav": str(run.final_nav),
                    "xirr": str(run.comparison.portfolio_xirr),
                    "regime_level": str(reading.index_level),
                    "regime_ma": str(reading.moving_average),
                    "risk_on": reading.risk_on,
                }
                _LOG.info(
                    "m14_6.forward_arm",
                    arm=arm.label,
                    floor=str(floor),
                    names=len(book.holdings) + len(book.positions),
                )
    return out


def _git_commit() -> str:
    from backtest.campaign import _git_commit as commit

    return commit()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backtest.m14_6")
    sub = parser.add_subparsers(dest="command", required=True)
    panel = sub.add_parser("panel", help="cut the daily signal panel")
    panel.add_argument("--out", type=Path, required=True)
    panel.add_argument("--data-root", type=Path, default=None)
    panel.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    panel.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    panel.add_argument("--workers", type=int, default=2)
    forward = sub.add_parser("forward", help="what each arm holds when run forward")
    forward.add_argument("--out", type=Path, required=True, help="JSON file to write")
    forward.add_argument("--data-root", type=Path, default=None)
    forward.add_argument("--signal-panel", type=Path, default=None)
    forward.add_argument("--from", dest="start", type=date.fromisoformat, default=FORWARD_START)
    forward.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m backtest.m14_6``. Returns a process exit code."""
    args = _parse_args(argv)
    try:
        if args.command == "panel":
            built = build_panel(
                args.out,
                start=args.start,
                end=args.end,
                data_root=args.data_root,
                workers=args.workers,
                commit=_git_commit(),
            )
            print(f"  panel {built.root}: {built.identity()['first']}..{built.identity()['last']}")
            return 0
        panel = SignalPanel(args.signal_panel) if args.signal_panel else None
        result = forward_holdings(
            start=args.start, end=args.end, data_root=args.data_root, panel=panel
        )
        refuse_lake_location(args.out.parent, args.data_root)
        args.out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"  forward holdings written to {args.out}")
        return 0
    except CampaignError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
