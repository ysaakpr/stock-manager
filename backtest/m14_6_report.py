"""M14.6 — render the per-stock entry gate report from the saved runs; replays nothing.

Reads the run directory ``backtest.m12_rerun --arm-set per-stock --signal-panel <dir>`` wrote
(``results.json``, ``manifest.json`` and each run's summary, fill ledger and NAV path), the
forward-holdings file ``backtest.m14_6 forward`` wrote and the panel check ``backtest.m14_6
verify`` wrote, and renders ``ops/gates/M14.6-per-stock-entry-report.md``: one table per window
and liquidity floor with D13 in every one, a headline XIRR/DD matrix, the book-exposure answer
(names held, sessions with an empty book, cash share of NAV) and what each variant would hold on
the paper book's live span.

**Book exposure** is not journaled in the saved files, so it is rebuilt here from the fill ledger:
shares per ISIN from every fill and every corporate event the run applied (split, bonus, reissue),
cash from the opening deposit, every fill's net amount, dividends and interest. The rebuild is
checked at the terminal session against the run's own terminal NAV and terminal holdings; a run
whose rebuild disagrees reports its exposure columns as unchecked rather than pass silently.

Everything below the hand-written marker in an existing report is kept on re-render.

What it never does: replay a run, read a wall clock into the report, or average across windows.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from backtest.m12_rerun import PER_STOCK_SET, WINDOWS
from backtest.policies.momentum_v2 import RegimeReading
from backtest.run import _L1Reader, _RegimeSource
from backtest.sweep import D13_PAPER_BASELINE, HIGH_FLOOR, LOW_FLOOR

__all__ = ["Exposure", "RunFacts", "collect", "exposure", "main", "render"]

REPORT = Path("ops/gates/M14.6-per-stock-entry-report.md")
MARKER = "<!-- hand-written analysis below: kept verbatim on re-render -->"
BAR = Decimal("0.25")
_ZERO = Decimal("0")
_CRORE = Decimal("10000000")
_FLOOR_RE = re.compile(r"median_turnover_floor=Decimal\('(\d+)'\)")
_WINDOW_ORDER = ("decade", "six-year", "wf-selection", "wf-verification")
_SHORT = {
    D13_PAPER_BASELINE.label: "V0",
    **{arm.label: arm.label.split(" ", 1)[0] for arm in PER_STOCK_SET[1:]},
}
#: The rebuild carries no cash event the ledger omits (a few thousand rupees on some runs, e.g. a
#: scheme credit), so it is held to 0.5% of the terminal NAV rather than to the rupee.
_NAV_TOLERANCE = Decimal("0.005")


@dataclass(frozen=True, slots=True)
class Exposure:
    """How invested a run's book was, session by session, rebuilt from its ledger."""

    mean_names: Decimal
    empty_share: Decimal  # share of sessions ending with no name held
    thin_share: Decimal  # share of sessions ending with at most 8 names (A8's floor)
    mean_cash_share: Decimal  # mean of cash / NAV at each session's close
    trading_sessions_per_year: Decimal  # sessions with at least one fill
    checked: bool  # the rebuild matched the run's terminal NAV and holdings


@dataclass(frozen=True, slots=True)
class RunFacts:
    """One saved run, as the report needs it."""

    window: str
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
    turnover: Decimal
    rail_blocks: Mapping[str, int]
    exposure: Exposure

    @property
    def ratio(self) -> Decimal:
        return self.xirr / self.max_drawdown if self.max_drawdown > _ZERO else _ZERO

    @property
    def short(self) -> str:
        return _SHORT.get(self.label, self.label)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _window_of(start: str, end: str) -> str:
    for name, (first, last) in WINDOWS.items():
        if first.isoformat() == start and last.isoformat() == end:
            return name
    raise ValueError(f"no mandate window is {start}..{end}")


def exposure(ledger: Mapping[str, Any], nav: Mapping[str, Any], years: Decimal) -> Exposure:
    """Rebuild names held and cash per session from ``ledger``; check it at the terminal."""
    by_day: dict[date, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for event in ledger["corporate_events"]:
        by_day[date.fromisoformat(event["ex_date"])].append((0, event))
    for trade in ledger["trades"]:
        by_day[date.fromisoformat(trade["trade_date"])].append((1, trade))
    cash_by_day: dict[date, Decimal] = defaultdict(lambda: _ZERO)
    for flow in ledger["external_flows"]:
        cash_by_day[date.fromisoformat(flow["date"])] -= Decimal(flow["amount"])
    for item in (*ledger["dividends"], *ledger["interest"]):
        cash_by_day[date.fromisoformat(item["received"])] += Decimal(item["amount"])
    shares: dict[str, int] = {}
    cash = _ZERO
    points = [(date.fromisoformat(day), Decimal(value)) for day, value in nav["points"]]
    days = sorted(set(by_day) | set(cash_by_day))
    cursor = 0
    names: list[int] = []
    cash_shares: list[Decimal] = []
    for session, value in points:
        while cursor < len(days) and days[cursor] <= session:
            day = days[cursor]
            cash += cash_by_day.get(day, _ZERO)
            for _, item in sorted(by_day.get(day, ()), key=lambda pair: pair[0]):
                _apply(shares, item)
                if "side" in item:
                    amount = Decimal(item["net_amount"])
                    cash += -amount if item["side"] == "BUY" else amount
            cursor += 1
        names.append(sum(1 for q in shares.values() if q > 0))
        cash_shares.append(cash / value if value > _ZERO else _ZERO)
    terminal_prices = {isin: Decimal(p) for isin, p in ledger["terminal_prices"].items()}
    held = {isin: q for isin, q in shares.items() if q > 0}
    marked = sum((Decimal(q) * terminal_prices.get(isin, _ZERO) for isin, q in held.items()), _ZERO)
    terminal_nav = Decimal(ledger["terminal_nav"])
    checked = (
        set(held) <= set(terminal_prices)
        and terminal_nav > _ZERO
        and abs(cash + marked - terminal_nav) / terminal_nav <= _NAV_TOLERANCE
    )
    count = Decimal(len(points)) if points else Decimal(1)
    trading = len({t["trade_date"] for t in ledger["trades"]})
    return Exposure(
        mean_names=(Decimal(sum(names)) / count).quantize(Decimal("0.1")),
        empty_share=Decimal(sum(1 for n in names if n == 0)) / count,
        thin_share=Decimal(sum(1 for n in names if n <= 8)) / count,
        mean_cash_share=sum(cash_shares, _ZERO) / count,
        trading_sessions_per_year=(Decimal(trading) / years).quantize(Decimal("0.1")),
        checked=checked,
    )


def _apply(shares: dict[str, int], item: Mapping[str, Any]) -> None:
    if "side" in item:
        quantity = int(item["quantity"])
        sign = 1 if item["side"] == "BUY" else -1
        shares[item["isin"]] = shares.get(item["isin"], 0) + sign * quantity
        return
    kind = item["kind"]
    if kind in ("split", "bonus"):
        shares[item["isin"]] = int(item["resulting_quantity"])
    elif kind == "reissue":
        moved = shares.pop(item["from_isin"], 0)
        shares[item["isin"]] = shares.get(item["isin"], 0) + moved
    elif "resulting_quantity" in item:
        shares[item["isin"]] = int(item["resulting_quantity"])
    else:
        raise ValueError(f"corporate event kind {kind!r} is not modelled by the rebuild")


def collect(run_dir: Path) -> list[RunFacts]:
    """Every momentum-v2 run in ``run_dir`` whose parameters are an arm of the per-stock set."""
    by_repr = {repr(arm.v2): arm for arm in PER_STOCK_SET}
    facts: list[RunFacts] = []
    for summary_file in sorted((run_dir / "runs").glob("*.json")):
        summary = _load(summary_file)
        spec = summary["spec"]
        arm = by_repr.get(spec.get("parameters", ""))
        if spec.get("runner") != "momentum_v2" or arm is None:
            continue
        window = _window_of(spec["start"], spec["end"])
        floor_match = _FLOOR_RE.search(spec["universe"])
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
        blocks = _rail_blocks(summary["rail_blocks"])
        facts.append(
            RunFacts(
                window=window,
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
                turnover=(traded / 2 / mean_nav / years).quantize(Decimal("0.01")),
                rail_blocks=blocks,
                exposure=exposure(ledger, nav, years),
            )
        )
    return facts


def _rail_blocks(text: str | Mapping[str, Any]) -> dict[str, int]:
    if isinstance(text, Mapping):
        return {str(k): int(v) for k, v in text.items()}
    return {k: int(v) for k, v in re.findall(r"'([A-Z_]+)': (\d+)", text)}


# ── regime context ───────────────────────────────────────────────────────────────────────────────


def regime_off_share(data_root: Path | None) -> dict[str, Decimal]:
    """Per mandate window: the share of sessions the NIFTY 50 TRI sat below its 200-session MA."""
    reader = _L1Reader(data_root=data_root)
    try:
        calendar = reader.all_sessions()
    finally:
        reader.close()
    last = max(end for _, end in WINDOWS.values())
    regime = _RegimeSource.published(through=last, ma_days=200, data_root=data_root)
    out: dict[str, Decimal] = {}
    for name in _WINDOW_ORDER:
        first, end = WINDOWS[name]
        sessions = [s for s in calendar if first <= s <= end]
        readings: list[RegimeReading] = [regime.reading(s) for s in sessions]
        out[name] = Decimal(sum(1 for r in readings if not r.risk_on)) / Decimal(len(readings))
    return out


# ── rendering ────────────────────────────────────────────────────────────────────────────────────


def _p(value: Decimal) -> str:
    return f"{(value * 100).quantize(Decimal('0.01'))}%"


def _p1(value: Decimal) -> str:
    return f"{(value * 100).quantize(Decimal('0.1'))}%"


def _pp(value: Decimal) -> str:
    return f"{'+' if value >= 0 else ''}{(value * 100).quantize(Decimal('0.01'))}pp"


def _r(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _lakh(value: Decimal) -> str:
    return f"₹{(value / Decimal('100000')).quantize(Decimal('0.01'))}L"


def _floor_label(floor: Decimal) -> str:
    crore = floor / _CRORE
    shown = crore.to_integral_value() if crore == crore.to_integral_value() else crore
    return f"₹{shown} cr/day floor"


def _floor_short(floor: Decimal) -> str:
    return _floor_label(floor).removesuffix("/day floor")


def _exposure_cells(e: Exposure) -> str:
    mark = "" if e.checked else " (unchecked)"
    return (
        f"{e.mean_names}{mark} | {_p1(e.empty_share)} | {_p1(e.thin_share)} "
        f"| {_p1(e.mean_cash_share)} | {e.trading_sessions_per_year}"
    )


def _table(rows: list[RunFacts]) -> list[str]:
    base = next((r for r in rows if r.label == D13_PAPER_BASELINE.label), None)
    ranked = sorted(rows, key=lambda r: (-r.ratio, r.label))
    lines = [
        "| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs "
        "NIFTY 50 TRI | Trades | One-way turnover /yr | Charges | Mean names held | Sessions "
        "with no name | Sessions with ≤8 names | Mean cash / NAV | Trading sessions /yr "
        "| A8 refusals: min-holdings / sector / other | >25%? |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- "
        "| --- | --- | --- |",
    ]
    for position, row in enumerate(ranked, start=1):
        name = f"**{row.label}**" if row is base else row.label
        delta_x = "—" if base is None or row is base else _pp(row.xirr - base.xirr)
        delta_d = "—" if base is None or row is base else _pp(row.max_drawdown - base.max_drawdown)
        floor_blocks = row.rail_blocks.get("MIN_HOLDINGS", 0)
        sector_blocks = row.rail_blocks.get("MAX_SECTOR", 0)
        other = sum(row.rail_blocks.values()) - floor_blocks - sector_blocks
        lines.append(
            f"| {position} | {name} | {_p(row.xirr)} | {_p(row.max_drawdown)} "
            f"| **{_r(row.ratio)}** | {delta_x} | {delta_d} | {_p(row.excess)} "
            f"| {row.trades} | {row.turnover}x | {_lakh(row.charges)} "
            f"| {_exposure_cells(row.exposure)} | {floor_blocks} / {sector_blocks} / {other} "
            f"| {'yes' if row.xirr > BAR else 'no'} |"
        )
    return lines


def headline(facts: Sequence[RunFacts]) -> list[str]:
    """XIRR, max DD and XIRR/DD per variant x (window, floor) — the matrix the analysis reads."""
    cells = [(w, f) for w in _WINDOW_ORDER for f in (LOW_FLOOR, HIGH_FLOOR)]
    index = {(r.label, r.window, r.floor): r for r in facts}
    header = " | ".join(f"{w} {_floor_short(f)}" for w, f in cells)
    out = [
        "## Headline: XIRR / max DD / **XIRR÷DD** (generated)",
        "",
        "Each cell is one run; nothing is averaged. Bold marks the best XIRR÷DD in its column.",
        "",
        f"| Variant | {header} |",
        "| --- | " + " | ".join("---" for _ in cells) + " |",
    ]
    best = {
        cell: max(
            (r.ratio for r in facts if (r.window, r.floor) == cell),
            default=_ZERO,
        )
        for cell in cells
    }
    for arm in PER_STOCK_SET:
        row = [_variant_name(arm.label)]
        for cell in cells:
            run = index.get((arm.label, *cell))
            if run is None:
                row.append("—")
                continue
            ratio = _r(run.ratio)
            ratio = f"**{ratio}**" if run.ratio == best[cell] else ratio
            row.append(f"{_p1(run.xirr)} / {_p1(run.max_drawdown)} / {ratio}")
        out.append("| " + " | ".join(row) + " |")
    out.append("")
    return out


def _variant_name(label: str) -> str:
    """``V0 D13`` for the baseline, the arm's own label (which starts with its code) otherwise."""
    return "V0 D13" if label == D13_PAPER_BASELINE.label else label


def exposure_table(facts: Sequence[RunFacts]) -> list[str]:
    """The owner's question, per variant: how many names, how often an empty book, how much cash."""
    out = [
        "## Book exposure: names held and time in cash (generated)",
        "",
        "Rebuilt from each run's fill ledger and corporate events, checked against its terminal "
        "NAV (`backtest.m14_6_report.exposure`). Decade window, per floor; the other windows' "
        "figures are in the per-window tables.",
        "",
        "| Variant | Floor | Mean names held | Sessions with no name | Sessions with ≤8 names "
        "| Mean cash / NAV | Trading sessions /yr |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in PER_STOCK_SET:
        for floor in (LOW_FLOOR, HIGH_FLOOR):
            run = next(
                (r for r in facts if (r.label, r.window, r.floor) == (arm.label, "decade", floor)),
                None,
            )
            if run is None:
                continue
            out.append(
                f"| {_variant_name(arm.label)} "
                f"| {_floor_short(floor)} | {_exposure_cells(run.exposure)} |"
            )
    out.append("")
    return out


def scorecard(facts: Sequence[RunFacts]) -> list[str]:
    """Each variant against D13, cell by cell (window x floor); comparisons exact."""
    base = {(f.window, f.floor): f for f in facts if f.label == D13_PAPER_BASELINE.label}
    out = [
        "## Scorecard against D13 (generated)",
        "",
        "Counted in code (`backtest.m14_6_report.scorecard`) over every (window, floor) cell that "
        "has both the variant and D13. A variant beats D13 *consistently* only if its XIRR/DD is "
        "higher in every cell.",
        "",
        "| Strategy | Cells | XIRR/DD better / tie / worse than D13 | Cells where XIRR/DD is "
        "worse | Max DD worse / tie / better | XIRR > 25% | One-way turnover /yr "
        "| Charges vs D13 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in PER_STOCK_SET:
        rows = sorted(
            (f for f in facts if f.label == arm.label and (f.window, f.floor) in base),
            key=lambda f: (_WINDOW_ORDER.index(f.window), f.floor),
        )
        if not rows:
            continue
        cleared = sum(1 for f in rows if f.xirr > BAR)
        turnover = f"{min(f.turnover for f in rows)}x to {max(f.turnover for f in rows)}x"
        if arm.label == D13_PAPER_BASELINE.label:
            out.append(
                f"| **{arm.label}** | {len(rows)} | — | — | — | {cleared} / {len(rows)} "
                f"| {turnover} | — |"
            )
            continue
        pairs = [(f, base[(f.window, f.floor)]) for f in rows]
        better = sum(1 for f, b in pairs if f.ratio > b.ratio)
        tie = sum(1 for f, b in pairs if f.ratio == b.ratio)
        worse = [f"{f.window} {_floor_short(f.floor)}" for f, b in pairs if f.ratio < b.ratio]
        dd_worse = sum(1 for f, b in pairs if f.max_drawdown > b.max_drawdown)
        dd_tie = sum(1 for f, b in pairs if f.max_drawdown == b.max_drawdown)
        rel = [f.charges / b.charges - 1 for f, b in pairs if b.charges > _ZERO]
        charges = f"{_rel(min(rel))} to {_rel(max(rel))}" if rel else "—"
        worse_text = "—" if not worse else ("all" if len(worse) == len(pairs) else "; ".join(worse))
        out.append(
            f"| {arm.label} | {len(pairs)} | {better} / {tie} / {len(worse)} | {worse_text} "
            f"| {dd_worse} / {dd_tie} / {len(pairs) - dd_worse - dd_tie} | {cleared} / {len(rows)} "
            f"| {turnover} | {charges} |"
        )
    out.append("")
    return out


def _rel(value: Decimal) -> str:
    return f"{'+' if value >= 0 else ''}{(value * 100).quantize(Decimal('1'))}%"


def forward_section(forward: Mapping[str, Any] | None) -> list[str]:
    """What each variant would hold after the 2026-10-13 fills, run forward from 2026-09-01."""
    if not forward:
        return []
    out = [
        f"## Run forward from {forward['start']} — what each variant would hold (generated)",
        "",
        "Each arm replayed from the paper book's first live month to the lake's last session, "
        f"{forward['end']}. That session is the fill-headroom session (T+1), so the replay "
        "decides through the session before it; *held after the next fills* is the terminal "
        "book plus the buys and less the sells decided on the terminal session. Same rails, "
        "broker and cost model as every backtest. ISINs; the NIFTY 50 TRI regime reading is on "
        "the terminal session.",
        "",
        "| Variant | Floor | Terminal | Regime (TRI vs 200d MA) | Held at terminal close "
        "| Decided on terminal: buys / sells | Held after the next fills | Cash / NAV |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    names_after: dict[tuple[str, str], list[str]] = {}
    for arm in PER_STOCK_SET:
        per_floor = forward["arms"].get(arm.label, {})
        for floor in (str(LOW_FLOOR), str(HIGH_FLOOR)):
            cell = per_floor.get(floor)
            if cell is None:
                continue
            held = set(cell["holdings"]) | set(cell["positions"])
            after = sorted(
                (held | set(cell["buys_decided_on_terminal"]))
                - set(cell["sells_decided_on_terminal"])
            )
            names_after[(arm.label, floor)] = after
            level, ma = Decimal(cell["regime_level"]), Decimal(cell["regime_ma"])
            gap = _p(level / ma - 1)
            cash_share = Decimal(cell["cash"]) / Decimal(cell["final_nav"])
            out.append(
                f"| {_SHORT[arm.label]} | {_floor_short(Decimal(floor))} | {cell['terminal']} "
                f"| {'risk-on' if cell['risk_on'] else 'risk-off'} ({gap}) | {len(held)} "
                f"| {len(cell['buys_decided_on_terminal'])} / "
                f"{len(cell['sells_decided_on_terminal'])} | {len(after)} | {_p1(cash_share)} |"
            )
    out.append("")
    for (label, floor), after in names_after.items():
        if after:
            out.append(
                f"* **{_SHORT[label]}, {_floor_short(Decimal(floor))}** would hold: "
                + ", ".join(f"`{isin}`" for isin in after)
            )
    out.append("")
    return out


def panel_check_section(check: Mapping[str, Any] | None) -> list[str]:
    if not check:
        return []
    verdict = "identical" if check["mismatches"] == 0 else f"{check['mismatches']} MISMATCHED"
    return [
        "## Signal panel check (generated)",
        "",
        f"The daily candidate sets were served from the panel at `{check['panel']}`. "
        f"`backtest.m14_6 verify` recomputed {check['sessions']} sessions on each of "
        f"{check['keys']} floors live with `_L1MomentumV2Data._compute` "
        f"({check['first']} … {check['last']}, "
        f"evenly spaced, every month-first session among them): **{verdict}** "
        "(ISIN, both momentum figures, price and volatility, exact Decimal equality).",
        "",
    ]


def render(
    facts: Sequence[RunFacts],
    *,
    manifest: Mapping[str, Any],
    run_dir_name: str,
    selected: str,
    regime_off: Mapping[str, Decimal],
    forward: Mapping[str, Any] | None,
    panel_check: Mapping[str, Any] | None,
    hand_written: str,
) -> str:
    """The whole report."""
    out = [
        "# M14.6 — momentum v2: per-stock daily entry and review (research gate report)",
        "",
        "> **Generated** by `uv run python -m backtest.m14_6_report` from the saved runs listed "
        "below; nothing in the tables is typed by hand. The section after the marker at the end "
        "is hand-written analysis and is labelled as such.",
        "",
        "## What was run",
        "",
        "D13 (`PAPER_RATIFIED_2026_09_06`) rebalances the whole book on the first session of each "
        "month and reads the regime (NIFTY 50 TRI against its 200-session average) only then. "
        "The variants, each D13's own parameters plus M14.6 switches "
        "(`backtest/policies/momentum_v2.py`), research configurations only:",
        "",
    ]
    for arm in PER_STOCK_SET:
        out.append(f"* **{arm.label}** — {arm.note}.")
    out += [
        "",
        "Mandate (owner, 2026-09-07): rank on XIRR ÷ max drawdown; both liquidity floors "
        "(₹1 cr and ₹10 cr median daily turnover); the decade, the six-year window and the "
        "walk-forward (choose on 2016-09..2021-08, verify on 2021-09..2026-08), never averaged; "
        "bar: backtested XIRR > 25%. Floor-only universe (as M14.5: the PIT NIFTY 500 history "
        "opens 2016-10-24). Book corporate actions and interest on idle cash on; every order "
        "through A8's ratified rails and the one SimBroker/cost-model path.",
        "",
        "| Run directory | Commit | Lake last session | Units | Signal panel |",
        "| --- | --- | --- | --- | --- |",
        f"| `{run_dir_name}` | `{manifest['commit'][:10]}` | {manifest['lake_last_session']} "
        f"| {', '.join(manifest['units'])} "
        f"| {manifest.get('signal_panel', {}).get('first', '—')} … "
        f"{manifest.get('signal_panel', {}).get('last', '—')} |",
        "",
        "Share of sessions the regime read **risk-off** (TRI below its 200d MA), per window: "
        + "; ".join(f"{w} {_p1(v)}" for w, v in regime_off.items())
        + ".",
        "",
        "Columns: **One-way turnover** is (buys + sells) ÷ 2 ÷ mean NAV ÷ years. **Charges** are "
        "every rupee the shared cost model charged (₹10L opening book). **Mean names held**, "
        "**sessions with no name**, **sessions with ≤8 names** (A8's minimum-holdings floor) and "
        "**mean cash / NAV** are rebuilt from the fill ledger, session by session; *(unchecked)* "
        "marks a run whose rebuild missed its terminal NAV by more than 0.5%. **Trading "
        "sessions /yr** counts sessions with at least one fill — how staggered the trading is. "
        "**A8 refusals** are every order the rails refused over the run, by rail.",
        "",
    ]
    out += panel_check_section(panel_check)
    out += headline(facts)
    out += scorecard(facts)
    out += exposure_table(facts)
    out += forward_section(forward)
    out += ["## Results by window (generated)", ""]
    for window in _WINDOW_ORDER:
        if not any(f.window == window for f in facts):
            continue
        first, last = WINDOWS[window]
        out += [f"### {window} ({first.isoformat()} → {last.isoformat()})", ""]
        for floor in (LOW_FLOOR, HIGH_FLOOR):
            rows = [f for f in facts if f.window == window and f.floor == floor]
            if rows:
                out += [f"**{_floor_label(floor)}**", "", *_table(rows), ""]
    if selected:
        out += [
            f"**Walk-forward choice**: on the selection window the code "
            f"(`backtest.verdict.run_walk_forward`, ₹1 cr floor first) chose **{selected}**; its "
            "verification-window row is in the table above, read after the choice was frozen.",
            "",
        ]
    out += [MARKER, hand_written.strip("\n"), ""]
    return "\n".join(out)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backtest.m14_6_report")
    parser.add_argument("run_dir", type=Path, help="the per-stock run directory")
    parser.add_argument("--forward", type=Path, default=None, help="backtest.m14_6 forward JSON")
    parser.add_argument("--panel-check", type=Path, default=None, help="m14_6 verify JSON")
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=REPORT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Render the report; keep any hand-written analysis already in ``--out``."""
    args = _parse_args(argv)
    manifest = _load(args.run_dir / "manifest.json")
    results = _load(args.run_dir / "results.json")
    facts = collect(args.run_dir)
    hand_written = ""
    if args.out.is_file():
        existing = args.out.read_text(encoding="utf-8")
        if MARKER in existing:
            hand_written = existing.split(MARKER, 1)[1]
    args.out.write_text(
        render(
            facts,
            manifest=manifest,
            run_dir_name=args.run_dir.name,
            selected=results.get("selected", ""),
            regime_off=regime_off_share(args.data_root),
            forward=_load(args.forward) if args.forward else None,
            panel_check=_load(args.panel_check) if args.panel_check else None,
            hand_written=hand_written,
        ),
        encoding="utf-8",
    )
    print(f"  {len(facts)} runs rendered to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
