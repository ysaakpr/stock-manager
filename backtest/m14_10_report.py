"""M14.10 — render the NIFTY 500 walk-forward report from the saved runs; replays nothing.

Reads the run directory ``backtest.m12_rerun --arm-set nifty500-wf --universe nifty500`` wrote
and renders ``ops/gates/M14.10-nifty500-walk-forward-report.md``. The study is pre-registered in
``ops/studies/preregistration-m14-10-nifty500-walk-forward-2026-11-06.md``. Every piece is M14.9's
machinery (:mod:`backtest.m14_9_report`): the same per-run facts, switch replay, table, scorecard,
sector-cap and idle-cash table, and decision rule, here applied to the NIFTY 500 cells.

Everything below the hand-written marker in an existing report is kept on re-render.

What it never does: replay a run, read a wall clock into the report, or average across windows.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from backtest.m12_rerun import NIFTY500_WF_SET, WINDOWS
from backtest.m14_5_report import _WINDOW_ORDER, MARKER, _floor_label, scorecard
from backtest.m14_9_report import (
    EntryFacts,
    _Lake,
    _load,
    _table,
    collect,
    decision,
    sector_table,
)
from backtest.sweep import D13_PAPER_BASELINE, HIGH_FLOOR, LOW_FLOOR

__all__ = ["main", "render"]

REPORT = Path("ops/gates/M14.10-nifty500-walk-forward-report.md")
_UNIVERSE = "nifty500"


def render(
    facts: Sequence[EntryFacts],
    *,
    manifests: Mapping[str, Mapping[str, Any]],
    selected: str,
    hand_written: str,
) -> str:
    """The whole report: provenance, the tables, the scorecard, the decision rule."""
    out = [
        "# M14.10 — momentum v2 on NIFTY 500: walk-forward of early entry and the sector refill",
        "",
        "> **Generated** by `uv run python -m backtest.m14_10_report` from the saved runs listed "
        "below; nothing in the tables is typed by hand. The section after the marker at the end "
        "is hand-written analysis and is labelled as such.",
        "",
        "## What was run",
        "",
        "Pre-registered in "
        "`ops/studies/preregistration-m14-10-nifty500-walk-forward-2026-11-06.md` before any "
        "run. The universe is the PIT NIFTY 500 screen (the paper book's own), whose membership "
        "history covers from 2015-03-28 (M14.1 build of 2026-10-10). Each arm is M14.9's "
        "exactly, plus one pre-specified combination:",
        "",
    ]
    for arm in NIFTY500_WF_SET:
        out.append(f"* **{arm.label}** — {arm.note}.")
    out += [
        "",
        "Mandate (owner, 2026-09-07): rank on XIRR ÷ max drawdown; both liquidity floors (₹1 cr "
        "and ₹10 cr median daily turnover); the decade, the six-year window and the walk-forward "
        "(choose on 2016-09..2021-08, verify on 2021-09..2026-08), never averaged; bar: "
        "backtested XIRR > 25%. Book corporate actions and interest on idle cash on; every order "
        "through A8's ratified rails.",
        "",
        "| Run directory | Universe | Commit | Lake last session | Units |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, manifest in manifests.items():
        out.append(
            f"| `{name}` | {manifest['universe']} | `{manifest['commit'][:10]}` | "
            f"{manifest['lake_last_session']} | {', '.join(manifest['units'])} |"
        )
    out += [
        "",
        "Columns as in M14.9: **Parks / monthly re-entries** are D13's own switches; **Early "
        "re-entries** are the V1/V2 triggers' re-entries between rebalances, replayed with the "
        "policy's own predicates, with how many the fill ledger confirms in brackets; **Reversed "
        "≤ 1 month** counts those re-parked at the next rebalance, then those after which the TRI "
        "closed below its MA200 within 21 sessions. **A8 min-holdings refusals** are all sells the "
        "8-name floor refused; in brackets, the park sessions.",
        "",
        "## Results — NIFTY 500 universe",
        "",
    ]
    for window in _WINDOW_ORDER:
        cell = [f for f in facts if f.base.window == window and f.base.universe == _UNIVERSE]
        if not cell:
            continue
        first, last = WINDOWS[window]
        out += [f"### {window} ({first.isoformat()} → {last.isoformat()})", ""]
        for floor in (LOW_FLOOR, HIGH_FLOOR):
            rows = [f for f in cell if f.base.floor == floor]
            if rows:
                out += [f"**{_floor_label(floor)}**", "", *_table(rows), ""]
    if selected:
        out += [
            "**Walk-forward choice (NIFTY 500)**: on the selection window the code "
            "(`backtest.verdict.run_walk_forward`, ₹1 cr floor first) chose "
            f"**{selected}**; its verification-window row is in the table above, read after the "
            "choice was frozen.",
            "",
        ]
    out += scorecard([f.base for f in facts], NIFTY500_WF_SET)
    sector_arms = [
        arm
        for arm in NIFTY500_WF_SET
        if arm.label == D13_PAPER_BASELINE.label
        or (arm.v2 is not None and arm.v2.rank_ordered_buys)
    ]
    out += sector_table(facts, sector_arms)
    out += decision(
        facts,
        selected,
        family=NIFTY500_WF_SET,
        title="Decision rule (pre-registered M14.10 §4, evaluated in code)",
        universe=_UNIVERSE,
    )
    out += [MARKER, hand_written.strip("\n"), ""]
    return "\n".join(out)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backtest.m14_10_report")
    parser.add_argument("run_dir", type=Path, help="the nifty500-wf run directory")
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=REPORT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Render the report from ``run_dir``; keep any hand-written analysis already in ``--out``."""
    args = _parse_args(argv)
    lake = _Lake(args.data_root)
    try:
        manifest = _load(args.run_dir / "manifest.json")
        results = _load(args.run_dir / "results.json")
        facts = collect(args.run_dir, lake, arms=NIFTY500_WF_SET)
    finally:
        lake.close()
    hand_written = ""
    if args.out.is_file():
        existing = args.out.read_text(encoding="utf-8")
        if MARKER in existing:
            hand_written = existing.split(MARKER, 1)[1]
    args.out.write_text(
        render(
            facts,
            manifests={args.run_dir.name: manifest},
            selected=results.get("selected", ""),
            hand_written=hand_written,
        ),
        encoding="utf-8",
    )
    print(f"  {len(facts)} runs rendered to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
