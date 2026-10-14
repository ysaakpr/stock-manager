"""Research part 3: the tables for the stock-level 30-DMA system, from saved runs; replays nothing.

Reads a ``backtest.sma30_run`` directory (summaries, fill ledgers, NAV paths) and, for the D13
comparison rows, the part-two ``regime-length`` directory. Buy-and-hold is the published NIFTY 50
TRI over the same window (its XIRR is each run's own ``benchmark_xirr``; its max drawdown is read
off the TRI by :mod:`backtest.ma_timing`).

Columns beyond M14.5's: **avg names held** and **% sessions in cash** are reconstructed from the
fill ledger (fills plus the ledger's split/bonus/reissue events), session by session over the NAV
path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from backtest.sma30_run import ARMS, WINDOWS
from backtest.sweep import D13_PAPER_BASELINE, HIGH_FLOOR, LOW_FLOOR

__all__ = ["Part3Run", "collect", "render"]

_ZERO: Final = Decimal("0")
_FLOOR_RE = re.compile(r"median_turnover_floor=Decimal\('(\d+)'\)")
_ORDER: Final = ("decade", "six-year", "wf-selection", "wf-verification")
BAR: Final = Decimal("0.25")


@dataclass(frozen=True, slots=True)
class Part3Run:
    label: str
    window: str
    floor: Decimal
    xirr: Decimal
    max_drawdown: Decimal
    excess: Decimal
    benchmark_xirr: Decimal
    trades: int
    turnover: Decimal
    charges: Decimal
    refusals: Mapping[str, int]
    avg_names: Decimal
    cash_share: Decimal
    terminal: date
    held_at_end: tuple[str, ...]

    @property
    def ratio(self) -> Decimal:
        return self.xirr / self.max_drawdown if self.max_drawdown > _ZERO else _ZERO


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _names_path(ledger: Mapping[str, Any], sessions: Sequence[date]) -> list[int]:
    """Names held at each session's close, from the fills and the ledger's share events."""
    events: dict[date, list[Mapping[str, Any]]] = {}
    for e in ledger["corporate_events"]:
        events.setdefault(date.fromisoformat(e["ex_date"]), []).append(e)
    fills: dict[date, list[Mapping[str, Any]]] = {}
    for t in ledger["trades"]:
        fills.setdefault(date.fromisoformat(t["trade_date"]), []).append(t)
    qty: dict[str, Decimal] = {}
    out: list[int] = []
    pending = sorted(set(events) | set(fills))
    i = 0
    for s in sessions:
        while i < len(pending) and pending[i] <= s:
            d = pending[i]
            for e in events.get(d, []):
                isin = e["isin"]
                if e["kind"] == "reissue":
                    qty[isin] = qty.get(isin, _ZERO) + qty.pop(e["from_isin"], _ZERO)
                elif e.get("resulting_quantity") is not None:
                    qty[isin] = Decimal(e["resulting_quantity"])
                elif e["kind"] == "split" and isin in qty:
                    qty[isin] = qty[isin] * Decimal(e["numerator"]) / Decimal(e["denominator"])
            for t in fills.get(d, []):
                q = Decimal(t["quantity"])
                qty[t["isin"]] = qty.get(t["isin"], _ZERO) + (q if t["side"] == "BUY" else -q)
            i += 1
        out.append(sum(1 for v in qty.values() if v > 0))
    return out


def _window_of(start: str, end: str) -> str | None:
    for name, (first, last) in WINDOWS.items():
        if first.isoformat() == start and last.isoformat() == end:
            return name
    return None


def collect(
    run_dir: Path,
    labels: Mapping[str, str],
    *,
    runner: str,
    window_of: Callable[..., Any] = _window_of,
) -> list[Part3Run]:
    """Every run in ``run_dir`` whose parameters' repr is a key of ``labels``."""
    out: list[Part3Run] = []
    for f in sorted((run_dir / "runs").glob("*.json")):
        s = _load(f)
        spec = s["spec"]
        label = labels.get(spec.get("parameters", ""))
        if spec.get("runner") != runner or label is None:
            continue
        window = window_of(spec["start"], spec["end"]) or f"{spec['start']}..{spec['end']}"
        floor_m = _FLOOR_RE.search(spec["universe"])
        if floor_m is None:
            continue
        ledger = _load(run_dir / "ledgers" / f"{s['digest']}.json")
        nav = _load(run_dir / "navs" / f"{s['digest']}.json")
        points = [(date.fromisoformat(d), Decimal(v)) for d, v in nav["points"]]
        sessions = [d for d, _ in points]
        names = _names_path(ledger, sessions)
        mean_nav = sum((v for _, v in points), _ZERO) / Decimal(len(points))
        years = Decimal((sessions[-1] - sessions[0]).days) / Decimal("365.25")
        traded = sum((abs(Decimal(t["net_amount"])) for t in ledger["trades"]), _ZERO)
        out.append(
            Part3Run(
                label=label,
                window=window,
                floor=Decimal(floor_m.group(1)),
                xirr=Decimal(s["xirr"]),
                max_drawdown=Decimal(s["max_drawdown"]),
                excess=Decimal(s["excess"]),
                benchmark_xirr=Decimal(s["benchmark_xirr"]),
                trades=len(ledger["trades"]),
                turnover=(traded / 2 / mean_nav / years).quantize(Decimal("0.01")),
                charges=Decimal(s["total_charges"]),
                refusals={k: int(v) for k, v in s["rail_blocks"].items()},
                avg_names=(Decimal(sum(names)) / Decimal(len(names))).quantize(Decimal("0.1")),
                cash_share=Decimal(sum(1 for n in names if n == 0)) / Decimal(len(names)),
                terminal=date.fromisoformat(s["terminal"]),
                held_at_end=tuple(sorted(ledger["terminal_prices"])),
            )
        )
    return out


def _p(v: Decimal) -> str:
    return f"{(v * 100).quantize(Decimal('0.01'))}%"


def _r(v: Decimal) -> str:
    return str(v.quantize(Decimal("0.01")))


def _lakh(v: Decimal) -> str:
    return f"₹{(v / Decimal('100000')).quantize(Decimal('0.01'))}L"


def _floor_label(floor: Decimal) -> str:
    return "₹1 cr/day floor" if floor == LOW_FLOOR else "₹10 cr/day floor"


def _refusals(r: Part3Run) -> str:
    total = sum(r.refusals.values())
    if not total:
        return "0"
    parts = ", ".join(f"{k} {v}" for k, v in sorted(r.refusals.items()))
    return f"{total} ({parts})"


def render(
    runs: Sequence[Part3Run],
    d13: Sequence[Part3Run],
    bh_drawdown: Callable[[str], Decimal],
    symbols: Mapping[str, str],
) -> list[str]:
    """The Part 3 section: rules, one table per (window, floor), the walk-forward, today."""
    out = [
        "## Part 3 — stock-level 30-DMA system (the article's per-stock rules)",
        "",
        "The owner's follow-up pasted the article's actual system, which is per stock, not index "
        "timing. Each vague term is pinned to one rule fixed before any window was run "
        "(`backtest/policies/sma30_stock.py`):",
        "",
        '* **Trend ("sustainably above")**: the close above SMA30 on each of the last 5 sessions '
        "and SMA30 above its value 5 sessions earlier.",
        "* **Pullback**: the session low within 2% of SMA30 (`low <= SMA30 x 1.02`) without "
        "closing more than 1% below it.",
        "* **Bounce (pullback entry)**: trend true two sessions ago, pullback yesterday, and today "
        "closes above yesterday and above SMA30. Signal at today's close, bought at the next "
        "session's open (SimBroker), never at the signal close.",
        "* **Golden cross**: EMA10 crossed above SMA30 in the last 5 sessions and is still above. "
        "**Death cross**: EMA10 below SMA30 — the exit for every arm.",
        "* **Protective exit (P3c)**: a close more than 3% below SMA30.",
        "* **Regime gate**: no new buys while NIFTY 50 TRI is below its 200-session SMA (D13's "
        "index and length; the 30-session gate of part one on one arm). Held names leave on "
        "their own exits — the swing composite's gate semantics, not D13's park.",
        "* **Portfolio**: ₹10 L; at most 20 names; each new name bought at 1/20 of account value "
        "(equal weight) or, vol-scaled, at 1/20 x (session median 63-session vol / own vol), "
        "capped at 10%; never beyond settled cash or A8's per-order ceiling; no top-ups or trims. "
        "When more names signal than slots are free, they are taken in 12-1 momentum order "
        "(ties by ISIN). Signals checked every session.",
        "* **Prices**: every SMA/EMA/low on the swing composite's seam-consistent adjusted price "
        "(L2 over raw L1, store split factors before the seam), so a split cannot fake a death "
        "cross (`tests/unit/test_sma30_stock.py`); fills and sizing on raw L1.",
        "* **Universe and rails**: the floor-only PIT investable screen at ₹1 cr and ₹10 cr "
        "median daily turnover (as every M12/M14 arm); every order through `RailGate` (A8) and "
        "`SimBroker` with the shared cost model; book corporate actions and cash interest on. "
        "That set is every NSE EQ-series listing that clears the floor, so it includes "
        "exchange-traded fund units (`INF…` ISINs: gold, silver, index ETFs) exactly as D13's "
        "does; they are not filtered out here either.",
        "",
        "**Overlap check.** No existing arm implements these rules. The M10.7 swing composite is "
        "a cross-sectional ranker (its only MA input, `ma_proximity` = close / 50-session mean, "
        "is a ranking leg, never a condition; its exits are rank band, re-underwrite and a 25% "
        "trailing stop); the M12 'Mean proximity (50d)' and 'Breakout' arms rank on that leg; "
        "no arm or screen in the repo computes an EMA. The M17 Commons screen S3 'pullback in a "
        "leader' (top-quintile 6-1 momentum, above SMA200, 4-12% off a recent 20-session high, "
        "within 3% of SMA20 or SMA50) is a screen evaluated only as forward returns "
        "(`analyst/commons/base_rates.py`), not a backtestable policy, and its pullback is to "
        "SMA20/50, not SMA30. So the rules were built new, but on the swing composite's own "
        "adjusted-price SQL, PIT universe, runner wiring and rails — nothing re-implemented.",
        "",
        "Arms:",
        "",
    ]
    for arm in ARMS:
        out.append(f"* **{arm.label}** — {arm.note}.")
    out += [
        "",
        "Columns: XIRR / max DD / **XIRR/DD** (ranked on it) / excess over NIFTY 50 TRI; trades "
        "(fills); one-way turnover = (buys + sells) ÷ 2 ÷ mean NAV ÷ years; charges from the "
        "shared cost model; A8 refusals by rail (every refused order, any session — a refused "
        "exit is re-staged each session while its rule holds, so one stuck exit counts many "
        "times); avg names held and % sessions holding nothing, from the fill ledger. **D13** "
        "(part two's run, same window and floor) and **buy-and-hold NIFTY 50 TRI** are the "
        "comparison rows.",
        "",
    ]
    for window in _ORDER:
        rows_w = [r for r in runs if r.window == window]
        if not rows_w:
            continue
        first, last = WINDOWS[window]
        out += [f"### {window} ({first.isoformat()} → {last.isoformat()})", ""]
        for floor in (LOW_FLOOR, HIGH_FLOOR):
            rows = sorted(
                [r for r in rows_w if r.floor == floor]
                + [r for r in d13 if r.window == window and r.floor == floor],
                key=lambda r: (-r.ratio, r.label),
            )
            if not rows:
                continue
            bh_x = rows[0].benchmark_xirr
            bh_dd = bh_drawdown(window)
            out += [
                f"**{_floor_label(floor)}** — buy-and-hold NIFTY 50 TRI: XIRR {_p(bh_x)}, max DD "
                f"{_p(bh_dd)}, XIRR/DD **{_r(bh_x / bh_dd)}**",
                "",
                "| # | Strategy | XIRR | Max DD | **XIRR/DD** | Excess vs NIFTY 50 TRI | Trades "
                "| Turnover /yr | Charges | A8 refusals | Avg names | % sessions in cash "
                "| >25%? |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
            for i, r in enumerate(rows, start=1):
                name = f"**{r.label}**" if r.label == D13_PAPER_BASELINE.label else r.label
                out.append(
                    f"| {i} | {name} | {_p(r.xirr)} | {_p(r.max_drawdown)} | **{_r(r.ratio)}** "
                    f"| {_p(r.excess)} | {r.trades} | {r.turnover}x | {_lakh(r.charges)} "
                    f"| {_refusals(r)} | {r.avg_names} | {_p(r.cash_share)} "
                    f"| {'yes' if r.xirr > BAR else 'no'} |"
                )
            out.append("")
    sel = [r for r in runs if r.window == "wf-selection" and r.floor == LOW_FLOOR]
    if sel:
        chosen = max(sel, key=lambda r: (r.ratio, r.label))
        ver = [r for r in runs if r.window == "wf-verification" and r.label == chosen.label]
        ver_d13 = [r for r in d13 if r.window == "wf-verification"]
        out += [
            "### Walk-forward",
            "",
            f"Chosen in code on the selection window (₹1 cr floor, best XIRR/DD among the P3 "
            f"arms): **{chosen.label}** ({_r(chosen.ratio)}). Its verification rows:",
            "",
            "| Floor | Chosen arm XIRR / DD / ratio | D13 XIRR / DD / ratio |",
            "| --- | --- | --- |",
        ]
        for floor in (LOW_FLOOR, HIGH_FLOOR):
            v = next((r for r in ver if r.floor == floor), None)
            d = next((r for r in ver_d13 if r.floor == floor), None)

            def cell(x: Part3Run | None) -> str:
                if x is None:
                    return "—"
                return f"{_p(x.xirr)} / {_p(x.max_drawdown)} / **{_r(x.ratio)}**"

            out.append(f"| {_floor_label(floor)} | {cell(v)} | {cell(d)} |")
        out.append("")
    today = [r for r in runs if r.window not in WINDOWS]
    if today:
        out += [
            "### What each arm would hold on the lake's last session",
            "",
            "From each arm replayed 2026-01-01 → the lake's last session (an event-driven book "
            "turns over within weeks, so the opening date barely matters by October). Names are "
            "the ISINs marked at the terminal session, shown by NSE symbol. L2 (the adjusted "
            "prices the rules read) ends 2026-10-09 in this lake, so the last signals are struck "
            "on the 2026-10-09 close; 10-12 and 10-13 carry no new signal for any name. A gated "
            "book left holding 8 names in a risk-off market is A8's minimum-holdings floor "
            "refusing the exits that would take it below 8 (the refusals column).",
            "",
            "| Arm | Floor | Terminal | Names | Holdings |",
            "| --- | --- | --- | --- | --- |",
        ]
        order = {a.label: i for i, a in enumerate(ARMS)}
        for r in sorted(today, key=lambda r: (order.get(r.label, 99), r.floor)):
            names = ", ".join(symbols.get(i, i) for i in r.held_at_end) or "— (all cash)"
            out.append(
                f"| {r.label} | {_floor_label(r.floor).removesuffix(' floor')} "
                f"| {r.terminal.isoformat()} | {len(r.held_at_end)} | {names} |"
            )
        out.append("")
    return out


def arm_labels() -> dict[str, str]:
    """``repr(parameters) -> label`` for every part-3 arm."""
    return {repr(a.params): a.label for a in ARMS}
