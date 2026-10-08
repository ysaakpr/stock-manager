"""M14.5 — render the regime-timing gate report from the saved runs; replays nothing.

Reads the run directories ``backtest.m12_rerun --arm-set regime-daily`` wrote (``results.json``,
``manifest.json`` and each run's summary, fill ledger and NAV path) and renders
``ops/gates/M14.5-regime-reentry-report.md``: one table per window, liquidity floor and universe,
D13 in every one, each variant's change against D13, its regime switches and what its trading
cost.

**Regime switches** are not journaled in the saved files, so they are re-derived here from the
published NIFTY 50 TRI with the policy's own triggers (:meth:`RegimeReading.risk_on`,
:meth:`~RegimeReading.clears_above`, :meth:`~RegimeReading.breaks_below`) and the same rebalance
calendar, then checked against the fill ledger: a park is *confirmed* when the run sold on the
next session, a re-entry when it bought. A model that disagreed with the replay would show as
unconfirmed switches rather than pass silently.

Everything below the hand-written marker in an existing report is kept on re-render, so the
analysis can be written once the tables exist and survive a re-render.

What it never does: replay a run, read a wall clock into the report, or average across windows.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from backtest.m12_rerun import REGIME_DAILY_SET, WINDOWS
from backtest.policies.momentum_v2 import MomentumV2Parameters, RegimeReading
from backtest.run import _first_session_of_each_month, _L1Reader, _RegimeSource
from backtest.sweep import D13_PAPER_BASELINE, HIGH_FLOOR, LOW_FLOOR

__all__ = ["RunFacts", "Switches", "collect", "main", "render", "switches"]

REPORT = Path("ops/gates/M14.5-regime-reentry-report.md")
MARKER = "<!-- hand-written analysis below: kept verbatim on re-render -->"
BAR = Decimal("0.25")
_ZERO = Decimal("0")
_CRORE = Decimal("10000000")
_FLOOR_RE = re.compile(r"median_turnover_floor=Decimal\('(\d+)'\)")
_INDEX_RE = re.compile(r"index_slug='([a-z0-9]+)'")
_WINDOW_ORDER = ("decade", "six-year", "wf-selection", "wf-verification")


@dataclass(frozen=True, slots=True)
class Switches:
    """Parks and re-entries the policy's triggers produce over a window, and how many the ledger
    confirms (a sale, respectively a purchase, filled on the following session)."""

    parks: int
    reentries: int
    parks_confirmed: int
    reentries_confirmed: int


@dataclass(frozen=True, slots=True)
class RunFacts:
    """One saved run, as the report needs it."""

    window: str
    universe: str
    floor: Decimal
    label: str
    digest: str
    replay_digest: str
    xirr: Decimal
    max_drawdown: Decimal
    excess: Decimal
    charges: Decimal
    final_nav: Decimal
    trades: int
    traded_value: Decimal
    turnover: Decimal
    switches: Switches

    @property
    def ratio(self) -> Decimal:
        return self.xirr / self.max_drawdown if self.max_drawdown > _ZERO else _ZERO


def switches(
    params: MomentumV2Parameters,
    sessions: Sequence[date],
    reading: Mapping[date, RegimeReading],
    *,
    sold: set[date],
    bought: set[date],
) -> Switches:
    """Replay the policy's parked state over ``sessions`` with its own triggers; count switches.

    Mirrors ``MomentumV2Policy.decide``: on a rebalance session the unbanded rule parks or clears
    the park; between rebalances a parked book re-enters on ``clears_above(band)`` (re-entry on)
    and an invested one parks on ``breaks_below(band)`` (exit on). The book counts as invested once
    it has first come off a park or bought at a risk-on rebalance.
    """
    rebalances = set(_first_session_of_each_month(sessions))
    band = params.regime_daily_band
    parked = invested = False
    park_days: list[date] = []
    entry_days: list[date] = []
    for session in sessions:
        r = reading[session]
        if session in rebalances:
            if not r.risk_on:
                if not parked:
                    park_days.append(session)
                parked = True
            else:
                if parked:
                    entry_days.append(session)
                parked, invested = False, True
            continue
        if parked:
            if params.regime_daily_reentry and r.clears_above(band):
                entry_days.append(session)
                parked, invested = False, True
        elif params.regime_daily_exit and invested and r.breaks_below(band):
            park_days.append(session)
            parked = True
    following = dict(pairwise(sessions))
    return Switches(
        parks=len(park_days),
        reentries=len(entry_days),
        parks_confirmed=sum(1 for d in park_days if following.get(d) in sold),
        reentries_confirmed=sum(1 for d in entry_days if following.get(d) in bought),
    )


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _window_of(start: str, end: str) -> str:
    for name, (first, last) in WINDOWS.items():
        if first.isoformat() == start and last.isoformat() == end:
            return name
    raise ValueError(f"no mandate window is {start}..{end}")


def collect(run_dir: Path, *, data_root: Path | None) -> list[RunFacts]:
    """Every momentum-v2 run in ``run_dir`` whose parameters are an arm of the regime-daily set."""
    by_repr = {repr(arm.v2): arm for arm in REGIME_DAILY_SET}
    reader = _L1Reader(data_root=data_root)
    try:
        calendar = reader.all_sessions()
    finally:
        reader.close()
    regime = _RegimeSource.published(
        through=calendar[-1],
        ma_days=D13_PAPER_BASELINE.v2.regime_ma_days if D13_PAPER_BASELINE.v2 else 200,
        data_root=data_root,
    )
    readings: dict[date, RegimeReading] = {}
    facts: list[RunFacts] = []
    for summary_file in sorted((run_dir / "runs").glob("*.json")):
        summary = _load(summary_file)
        spec = summary["spec"]
        arm = by_repr.get(spec.get("parameters", ""))
        if spec.get("runner") != "momentum_v2" or arm is None or arm.v2 is None:
            continue
        window = _window_of(spec["start"], spec["end"])
        floor_match = _FLOOR_RE.search(spec["universe"])
        index_match = _INDEX_RE.search(spec["universe"])
        if floor_match is None:
            raise ValueError(f"{summary_file.name}: no turnover floor in {spec['universe']}")
        digest = summary["digest"]
        ledger = _load(run_dir / "ledgers" / f"{digest}.json")
        nav = _load(run_dir / "navs" / f"{digest}.json")
        trades = ledger["trades"]
        traded = sum((abs(Decimal(t["net_amount"])) for t in trades), _ZERO)
        points = [Decimal(value) for _, value in nav["points"]]
        mean_nav = sum(points, _ZERO) / Decimal(len(points))
        first, last = WINDOWS[window]
        years = Decimal((last - first).days) / Decimal("365.25")
        sessions = [s for s in calendar if first <= s <= last]
        for s in sessions:
            if s not in readings:
                readings[s] = regime.reading(s)
        facts.append(
            RunFacts(
                window=window,
                universe=index_match.group(1) if index_match else "turnover_floor",
                floor=Decimal(floor_match.group(1)),
                label=arm.label,
                digest=digest,
                replay_digest=summary["replay_digest"],
                xirr=Decimal(summary["xirr"]),
                max_drawdown=Decimal(summary["max_drawdown"]),
                excess=Decimal(summary["excess"]),
                charges=Decimal(summary["total_charges"]),
                final_nav=Decimal(summary["final_nav"]),
                trades=len(trades),
                traded_value=traded,
                turnover=(traded / 2 / mean_nav / years).quantize(Decimal("0.01")),
                switches=switches(
                    arm.v2,
                    sessions,
                    readings,
                    sold={
                        date.fromisoformat(t["trade_date"]) for t in trades if t["side"] == "SELL"
                    },
                    bought={
                        date.fromisoformat(t["trade_date"]) for t in trades if t["side"] == "BUY"
                    },
                ),
            )
        )
    return facts


# ── rendering ────────────────────────────────────────────────────────────────────────────────────


def _p(value: Decimal) -> str:
    return f"{(value * 100).quantize(Decimal('0.01'))}%"


def _pp(value: Decimal) -> str:
    return f"{'+' if value >= 0 else ''}{(value * 100).quantize(Decimal('0.01'))}pp"


def _r(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _lakh(value: Decimal) -> str:
    return f"₹{(value / Decimal('100000')).quantize(Decimal('0.01'))}L"


def _crore(value: Decimal) -> str:
    return f"₹{(value / _CRORE).quantize(Decimal('0.01'))}cr"


def _floor_label(floor: Decimal) -> str:
    return f"₹{(floor / _CRORE).normalize()} cr/day floor"


def _table(rows: list[RunFacts]) -> list[str]:
    base = next((r for r in rows if r.label == D13_PAPER_BASELINE.label), None)
    ranked = sorted(rows, key=lambda r: (-r.ratio, r.label))
    lines = [
        "| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs "
        "NIFTY 50 TRI | Parks / re-entries (ledger-confirmed) | Trades | One-way turnover /yr "
        "| Charges | >25%? |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for position, row in enumerate(ranked, start=1):
        sw = row.switches
        name = f"**{row.label}**" if row is base else row.label
        delta_x = "—" if base is None or row is base else _pp(row.xirr - base.xirr)
        delta_d = "—" if base is None or row is base else _pp(row.max_drawdown - base.max_drawdown)
        lines.append(
            f"| {position} | {name} | {_p(row.xirr)} | {_p(row.max_drawdown)} "
            f"| **{_r(row.ratio)}** | {delta_x} | {delta_d} | {_p(row.excess)} "
            f"| {sw.parks} / {sw.reentries} ({sw.parks_confirmed} / {sw.reentries_confirmed}) "
            f"| {row.trades} | {row.turnover}x "
            f"| {_lakh(row.charges)} | {'yes' if row.xirr > BAR else 'no'} |"
        )
    return lines


def render(
    facts: Sequence[RunFacts],
    *,
    manifests: Mapping[str, Mapping[str, Any]],
    selected: Mapping[str, str],
    hand_written: str,
) -> str:
    """The whole report: provenance, one table per (universe, window, floor), the walk-forward."""
    out = [
        "# M14.5 — momentum v2: daily regime re-entry / exit (gate report)",
        "",
        "> **Generated** by `uv run python -m backtest.m14_5_report` from the saved runs listed "
        "below; nothing in the tables is typed by hand. The section after the marker at the end "
        "is hand-written analysis and is labelled as such.",
        "",
        "## What was run",
        "",
        "D13 (`PAPER_RATIFIED_2026_09_06`) reads the regime — NIFTY 50 TRI against its "
        "200-session average — only on the monthly rebalance session. The variants (M14.5 "
        "switches on D13's own parameters, nothing else changed):",
        "",
    ]
    for arm in REGIME_DAILY_SET:
        out.append(f"* **{arm.label}** — {arm.note}.")
    out += [
        "",
        "Mandate (owner, 2026-09-07): rank on XIRR ÷ max drawdown; both liquidity floors "
        "(₹1 cr and ₹10 cr median daily turnover); the decade, the six-year window and the "
        "walk-forward (choose on 2016-09..2021-08, verify on 2021-09..2026-08), never averaged; "
        "bar: backtested XIRR > 25%. Book corporate actions and interest on idle cash on; every "
        "order through A8's ratified rails.",
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
        "Columns: **Parks / re-entries** are regime switches — the policy's own triggers replayed "
        "over the published TRI and calendar (`backtest.m14_5_report.switches`); in brackets, how "
        "many the fill ledger confirms (a sale, resp. a purchase, on the following session). An "
        "unconfirmed park is one with nothing to sell; a D13 park after the first is usually "
        "that, since A8's minimum-holdings floor (8 names) refuses the last sells of a park and "
        "the kept names then sit through the risk-off spell. **One-way turnover** is "
        "(buys + sells) ÷ 2 ÷ mean NAV ÷ years. **Charges** are every brokerage, STT, stamp, "
        "exchange and GST rupee the shared cost model charged (₹10L opening book).",
        "",
    ]
    universes = sorted({f.universe for f in facts}, key=lambda u: (u != "turnover_floor", u))
    for universe in universes:
        title = "floor-only universe" if universe == "turnover_floor" else f"{universe} universe"
        out += [f"## Results — {title}", ""]
        windows = [
            w for w in _WINDOW_ORDER if any(f.window == w and f.universe == universe for f in facts)
        ]
        for window in windows:
            first, last = WINDOWS[window]
            out += [f"### {window} ({first.isoformat()} → {last.isoformat()})", ""]
            for floor in (LOW_FLOOR, HIGH_FLOOR):
                rows = [
                    f
                    for f in facts
                    if f.universe == universe and f.window == window and f.floor == floor
                ]
                if not rows:
                    continue
                out += [f"**{_floor_label(floor)}**", "", *_table(rows), ""]
        if selected.get(universe):
            out += [
                f"**Walk-forward choice ({title})**: on the selection window the code "
                f"(`backtest.verdict.run_walk_forward`, ₹1 cr floor first) chose "
                f"**{selected[universe]}**; its verification-window row is in the table above, "
                "read after the choice was frozen.",
                "",
            ]
    out += [MARKER, hand_written.strip("\n"), ""]
    return "\n".join(out)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backtest.m14_5_report")
    parser.add_argument("run_dirs", nargs="+", type=Path, help="regime-daily run directories")
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=REPORT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Render the report from ``run_dirs``; keep any hand-written analysis already in ``--out``."""
    args = _parse_args(argv)
    facts: list[RunFacts] = []
    manifests: dict[str, dict[str, Any]] = {}
    selected: dict[str, str] = {}
    for run_dir in args.run_dirs:
        manifest = _load(run_dir / "manifest.json")
        manifests[run_dir.name] = manifest
        results = _load(run_dir / "results.json")
        selected[manifest["universe"]] = results.get("selected", "")
        facts.extend(collect(run_dir, data_root=args.data_root))
    hand_written = ""
    if args.out.is_file():
        existing = args.out.read_text(encoding="utf-8")
        if MARKER in existing:
            hand_written = existing.split(MARKER, 1)[1]
    args.out.write_text(
        render(facts, manifests=manifests, selected=selected, hand_written=hand_written),
        encoding="utf-8",
    )
    print(f"  {len(facts)} runs rendered to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
