"""Research: render ``ops/gates/MA-timing-30d-report.md`` — index MA timing and D13's regime length.

Part one replays the index-timing family of :mod:`backtest.ma_timing` over the published TRIs
(cheap: a few seconds a rule) and renders every table from those replays. Part two reads the saved
D13 regime-length runs (``backtest.m12_rerun --arm-set regime-length``) through the M14.5 renderer's
own collector, so its rows are built exactly as M14.5's were; it replays nothing.

``--today-runs`` (optional) names a directory of runs struck through the lake's last session for
the "what would it hold today" lines; :func:`run_today` makes it (``--make-today-runs``).

Everything below the hand-written marker in an existing report is kept on re-render.

What it never does: write under the lake, fetch, read a wall clock into the report, average a figure
across windows, or tune a band or cadence into the headline.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from backtest.cash_interest import (
    RepoRateSchedule,
    accrue_cash_interest,
    load_repo_rate_schedule,
)
from backtest.m12_rerun import REGIME_LENGTH_SET, WINDOWS
from backtest.ma_timing import (
    COST_CARD_FROM,
    ETF_ISIN,
    SLIPPAGE,
    Cadence,
    CostMode,
    IndexSeries,
    Metrics,
    SimResult,
    TimingRule,
    after_tax,
    load_index,
    metrics,
    simulate,
    states,
)
from backtest.policies.momentum_v2 import RegimeReading
from execution.costs import CostModel, load_rate_card

__all__ = ["MARKER", "REPORT", "Row", "Window", "main", "render_index_part", "run_today"]

REPORT: Final = Path("ops/gates/MA-timing-30d-report.md")
MARKER: Final = "<!-- hand-written analysis below: kept verbatim on re-render -->"

HEADLINE: Final = {"nifty50": "NIFTY 50 TRI", "nifty500": "NIFTY 500 TRI"}
SECONDARY: Final = {
    "niftymidcap150": "NIFTY Midcap 150 TRI",
    "niftysmallcap250": "NIFTY Smallcap 250 TRI",
}
NS: Final = (10, 20, 30, 50, 100, 150, 200)
THESIS_N: Final = 30
BANDS: Final = (Decimal("0"), Decimal("0.01"), Decimal("0.02"))
_ACCOUNT_STATE: Final = "MH"  # as backtest.run: the account's state for pre-2020 stamp duty
_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_EPISODES: Final = (
    ("2000-03 dot-com", date(2000, 1, 1), date(2003, 6, 30)),
    ("2008 GFC", date(2007, 10, 1), date(2009, 12, 31)),
    ("2020 Covid", date(2020, 1, 1), date(2020, 12, 31)),
    ("2024-26", date(2024, 9, 1), date(2026, 12, 31)),
)


# ── windows ───────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Window:
    """One evaluation window. ``taxed`` when every model (cost card, repo, tax) covers it."""

    name: str
    start: date
    end: date
    note: str

    @property
    def taxed(self) -> bool:
        return self.start >= COST_CARD_FROM


def windows_for(series: IndexSeries) -> list[Window]:
    """The mandate's windows for one index, clipped to its own history.

    ``full`` opens on the first session every N's decision exists (the 200-session SMA's first
    decision trades the session after it), so all N and buy-and-hold start the same day.
    """
    sessions = series.sessions
    first = sessions[max(NS)]
    last = sessions[-1]
    out = [
        Window("full", first, last, "whole published history (pre-2006 costs and cash on proxies)")
    ]
    out.append(
        Window("2006→", COST_CARD_FROM, last, "span every model covers (after-tax available)")
    )
    for name in ("decade", "six-year", "wf-selection", "wf-verification"):
        start, end = WINDOWS[name]
        if start >= first:
            out.append(Window(name, start, min(end, last), "owner mandate window"))
    mid = sessions[(sessions.index(first) + len(sessions)) // 2]
    out.append(Window("full-1st-half", first, mid, "long walk-forward: selection"))
    out.append(Window("full-2nd-half", mid, last, "long walk-forward: verification"))
    return out


# ── rows ──────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Row:
    """One strategy on one window, under every cost reading."""

    label: str
    rule: TimingRule | None
    card: Metrics
    gross: Metrics
    ex_stt: Metrics
    after_tax: Decimal | None
    run: SimResult
    worst_year: tuple[int, Decimal, int] | None
    excess: Decimal


class _Engine:
    """Caches the series, the decisions and the buy-and-hold runs; every replay goes through it."""

    def __init__(self, *, data_root: Path | None, through: date) -> None:
        self.data_root = data_root
        self.through = through
        self.cost = CostModel(load_rate_card(), account_state=_ACCOUNT_STATE)
        self.repo: RepoRateSchedule = load_repo_rate_schedule()
        self._series: dict[str, IndexSeries] = {}
        self._decided: dict[tuple[str, TimingRule], Any] = {}
        self._rows: dict[tuple[str, str, TimingRule | None], Row] = {}

    def series(self, slug: str) -> IndexSeries:
        if slug not in self._series:
            self._series[slug] = load_index(slug, through=self.through, data_root=self.data_root)
        return self._series[slug]

    def decided(self, slug: str, rule: TimingRule) -> Any:
        key = (slug, rule)
        if key not in self._decided:
            self._decided[key] = states(self.series(slug).points, rule)
        return self._decided[key]

    def _sim(self, slug: str, w: Window, rule: TimingRule | None, mode: CostMode) -> SimResult:
        return simulate(
            self.series(slug),
            start=w.start,
            end=w.end,
            rule=rule,
            cost_model=self.cost,
            repo=self.repo,
            costs=mode,
            decided=None if rule is None else self.decided(slug, rule),
        )

    def row(self, slug: str, w: Window, rule: TimingRule | None) -> Row:
        key = (slug, w.name, rule)
        if key in self._rows:
            return self._rows[key]
        card = self._sim(slug, w, rule, CostMode.CARD)
        gross = self._sim(slug, w, rule, CostMode.FRICTIONLESS)
        ex_stt = self._sim(slug, w, rule, CostMode.CARD_EX_STT)
        taxed = after_tax(card).after_tax_xirr_liquidated if w.taxed else None
        m = metrics(card)
        if rule is None:
            worst, excess = None, _ZERO
        else:
            bh = self.row(slug, w, None)
            worst = _worst_year(card, bh.run)
            excess = m.cagr - bh.card.cagr
        row = Row(
            label="buy-and-hold" if rule is None else rule.label,
            rule=rule,
            card=m,
            gross=metrics(gross),
            ex_stt=metrics(ex_stt),
            after_tax=taxed,
            run=card,
            worst_year=worst,
            excess=excess,
        )
        self._rows[key] = row
        return row


def _year_returns(run: SimResult) -> dict[int, Decimal]:
    out: dict[int, Decimal] = {}
    base = run.nav[0][1]
    year = run.nav[0][0].year
    last = base
    for d, v in run.nav:
        if d.year != year:
            out[year] = last / base - _ONE
            base, year = last, d.year
        last = v
    out[year] = last / base - _ONE
    return out


def _year_switches(run: SimResult) -> dict[int, int]:
    out: dict[int, int] = {}
    for t in run.trades[1:]:
        out[t.session.year] = out.get(t.session.year, 0) + 1
    return out


def _worst_year(run: SimResult, bh: SimResult) -> tuple[int, Decimal, int] | None:
    """The calendar year the rule trailed buy-and-hold most: (year, excess, switches that year)."""
    mine, theirs = _year_returns(run), _year_returns(bh)
    years = sorted(set(mine) & set(theirs))
    if not years:
        return None
    sw = _year_switches(run)
    y = min(years, key=lambda k: (mine[k] - theirs[k], k))
    return y, mine[y] - theirs[y], sw.get(y, 0)


# ── formatting ────────────────────────────────────────────────────────────────────────────────────


def _p(v: Decimal | None) -> str:
    return "—" if v is None else f"{(v * 100).quantize(Decimal('0.01'))}%"


def _pp(v: Decimal) -> str:
    return f"{'+' if v >= 0 else ''}{(v * 100).quantize(Decimal('0.01'))}pp"


def _r(v: Decimal) -> str:
    return str(v.quantize(Decimal("0.01")))


def _lakh(v: Decimal) -> str:
    return f"₹{(v / Decimal('100000')).quantize(Decimal('0.01'))}L"


def _worst(row: Row) -> str:
    if row.worst_year is None:
        return "—"
    y, ex, sw = row.worst_year
    return f"{y} ({_pp(ex)}, {sw} sw)"


def _window_head(w: Window) -> str:
    return f"{w.name} ({w.start.isoformat()} → {w.end.isoformat()})"


# ── the index part ────────────────────────────────────────────────────────────────────────────────


def _headline(engine: _Engine) -> list[str]:
    out = [
        "## Headline — N=30 against buy-and-hold",
        "",
        "Daily check, no band, signal at close t, switch at close t+1. **After costs** is the "
        "shared cost model's delivery card plus slippage (the reported figure); **ex-STT** leaves "
        "the card's STT line out (the lower bound on what an ETF switch costs); **after tax** is "
        "the after-cost run taxed (FIFO lots, STCG/LTCG, slab tax on cash interest) with the "
        "terminal holding deemed sold — only on windows every model covers. CAGR/DD is after "
        "costs.",
        "",
    ]
    for slug, title in HEADLINE.items():
        out += [f"### {title}", ""]
        out += [
            "| Window | Strategy | CAGR before costs | **CAGR after costs** | CAGR ex-STT "
            "| CAGR after tax | Max DD | **CAGR/DD** | % invested | Switches/yr "
            "| Excess vs B&H (after costs) |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for w in windows_for(engine.series(slug)):
            for rule in (None, TimingRule(THESIS_N)):
                r = engine.row(slug, w, rule)
                name = "Buy-and-hold" if rule is None else "**N=30**"
                out.append(
                    f"| {w.name} | {name} | {_p(r.gross.cagr)} | **{_p(r.card.cagr)}** "
                    f"| {_p(r.ex_stt.cagr)} | {_p(r.after_tax)} | {_p(r.card.max_drawdown)} "
                    f"| **{_r(r.card.ratio)}** | {_p(r.card.invested_share)} "
                    f"| {_r(r.card.switches_per_year)} | {'—' if rule is None else _pp(r.excess)} |"
                )
        out.append("")
    return out


def _grid(
    engine: _Engine, slugs: Mapping[str, str], window_names: Sequence[str] | None
) -> list[str]:
    out: list[str] = []
    for slug, title in slugs.items():
        out += [f"### {title}", ""]
        for w in windows_for(engine.series(slug)):
            if window_names is not None and w.name not in window_names:
                continue
            rows = [engine.row(slug, w, None), *(engine.row(slug, w, TimingRule(n)) for n in NS)]
            ranked = sorted(rows, key=lambda r: (-r.card.ratio, r.label))
            out += [
                f"**{_window_head(w)}** — {w.note}",
                "",
                "| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** "
                "| Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H "
                "| Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- "
                "| --- | --- |",
            ]
            for i, r in enumerate(ranked, start=1):
                name = f"**{r.label}**" if r.rule == TimingRule(THESIS_N) else r.label
                out.append(
                    f"| {i} | {name} | {_p(r.card.cagr)} | {_p(r.gross.cagr)} "
                    f"| {_p(r.card.max_drawdown)} | **{_r(r.card.ratio)}** "
                    f"| {_p(r.card.volatility)} | {_r(r.card.sharpe)} "
                    f"| {_p(r.card.invested_share)} | {_r(r.card.switches_per_year)} "
                    f"| {_worst(r)} | {'—' if r.rule is None else _pp(r.excess)} "
                    f"| {_p(r.after_tax)} | {_lakh(r.run.charges)} ({_lakh(r.run.stt)}) "
                    f"| {_lakh(r.run.slippage_cost)} |"
                )
            out.append("")
    return out


def _thesis_rank(engine: _Engine) -> list[str]:
    """Where N=30 places among the seven N on each window, by CAGR/DD and by CAGR (after costs)."""
    out = [
        "## Is 30 special? Rank of N=30 among the seven N (generated)",
        "",
        "Rank 1 = best of 7 (buy-and-hold excluded). After costs.",
        "",
        "| Index | Window | Rank by CAGR/DD | Rank by CAGR | Best N by CAGR/DD | Best N by CAGR "
        "| Does any N beat B&H on CAGR? | On CAGR/DD? |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for slug, title in {**HEADLINE, **SECONDARY}.items():
        for w in windows_for(engine.series(slug)):
            bh = engine.row(slug, w, None)
            rows = [engine.row(slug, w, TimingRule(n)) for n in NS]
            by_ratio = sorted(rows, key=lambda r: -r.card.ratio)
            by_cagr = sorted(rows, key=lambda r: -r.card.cagr)
            thesis = next(r for r in rows if r.rule == TimingRule(THESIS_N))
            beat_c = [r.label for r in rows if r.card.cagr > bh.card.cagr]
            beat_r = [r.label for r in rows if r.card.ratio > bh.card.ratio]
            out.append(
                f"| {title} | {w.name} | {by_ratio.index(thesis) + 1} "
                f"| {by_cagr.index(thesis) + 1} "
                f"| {by_ratio[0].label} | {by_cagr[0].label} "
                f"| {', '.join(beat_c) if beat_c else 'none'} "
                f"| {', '.join(beat_r) if beat_r else 'none'} |"
            )
    out.append("")
    return out


def _robustness(engine: _Engine) -> list[str]:
    out = [
        "## Robustness knobs — reported separately, never tuned into the headline",
        "",
        "N=30 and, for reference, N=200 with a ±1% / ±2% hysteresis band (in above SMAx(1+b), out "
        "below SMAx(1-b)) and with the signal read weekly (last session of the week) or monthly "
        "(last session of the month). After costs.",
        "",
    ]
    for slug, title in HEADLINE.items():
        out += [f"### {title}", ""]
        for w in windows_for(engine.series(slug)):
            if w.name not in ("full", "2006→", "decade", "six-year"):
                continue
            bh = engine.row(slug, w, None)
            out += [
                f"**{_window_head(w)}** — buy-and-hold: CAGR {_p(bh.card.cagr)}, max DD "
                f"{_p(bh.card.max_drawdown)}, CAGR/DD {_r(bh.card.ratio)}"
                f"{'' if bh.after_tax is None else f', after tax {_p(bh.after_tax)}'}",
                "",
                "| Rule | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | % invested "
                "| Switches/yr | CAGR after tax |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
            rows: list[Row] = []
            for n in (THESIS_N, 200):
                for cadence in Cadence:
                    for band in BANDS:
                        r = engine.row(slug, w, TimingRule(n, band, cadence))
                        rows.append(r)
                        out.append(
                            f"| {r.label} | {_p(r.card.cagr)} | {_p(r.gross.cagr)} "
                            f"| {_p(r.card.max_drawdown)} | **{_r(r.card.ratio)}** "
                            f"| {_p(r.card.invested_share)} | {_r(r.card.switches_per_year)} "
                            f"| {_p(r.after_tax)} |"
                        )
            beat_cost = sum(1 for r in rows if r.card.cagr > bh.card.cagr)
            beat_ratio = sum(1 for r in rows if r.card.ratio > bh.card.ratio)
            line = (
                f"Of these {len(rows)} rules, **{beat_cost}** beat buy-and-hold on CAGR after "
                f"costs and **{beat_ratio}** on CAGR/DD"
            )
            if bh.after_tax is not None:
                beat_tax = sum(
                    1 for r in rows if r.after_tax is not None and r.after_tax > bh.after_tax
                )
                line += f"; **{beat_tax}** on CAGR after tax"
            out += ["", line + ".", ""]
    return out


def _walk_forward(engine: _Engine) -> list[str]:
    out = [
        "## Walk-forward — choose N on the first half, verify on the second (generated)",
        "",
        "The choice is the N (daily, no band) with the best after-cost CAGR/DD on the selection "
        "window, made in code before the verification window is read.",
        "",
        "| Index | Split | Chosen N (selection CAGR/DD) | Verification: chosen N CAGR / DD / ratio "
        "| Verification: N=30 | Verification: buy-and-hold |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for slug, title in {**HEADLINE, **SECONDARY}.items():
        ws = {w.name: w for w in windows_for(engine.series(slug))}
        for sel, ver in (("wf-selection", "wf-verification"), ("full-1st-half", "full-2nd-half")):
            if sel not in ws or ver not in ws:
                continue
            rows = [engine.row(slug, ws[sel], TimingRule(n)) for n in NS]
            chosen = max(
                rows, key=lambda r: (r.card.ratio, -NS.index(r.rule.ma_days) if r.rule else 0)
            )
            assert chosen.rule is not None
            v = engine.row(slug, ws[ver], chosen.rule)
            t = engine.row(slug, ws[ver], TimingRule(THESIS_N))
            b = engine.row(slug, ws[ver], None)

            def cell(r: Row) -> str:
                return f"{_p(r.card.cagr)} / {_p(r.card.max_drawdown)} / **{_r(r.card.ratio)}**"

            out.append(
                f"| {title} | {ws[sel].start.isoformat()}..{ws[sel].end.isoformat()} → "
                f"{ws[ver].start.isoformat()}..{ws[ver].end.isoformat()} "
                f"| {chosen.label} ({_r(chosen.card.ratio)}) | {cell(v)} | {cell(t)} | {cell(b)} |"
            )
    out.append("")
    return out


def _per_year(engine: _Engine) -> list[str]:
    out = [
        "## Calendar years — N=30 against buy-and-hold (full history, after costs)",
        "",
    ]
    for slug, title in HEADLINE.items():
        w = windows_for(engine.series(slug))[0]
        bh = engine.row(slug, w, None)
        r = engine.row(slug, w, TimingRule(THESIS_N))
        gross = simulate(
            engine.series(slug),
            start=w.start,
            end=w.end,
            rule=TimingRule(THESIS_N),
            cost_model=engine.cost,
            repo=engine.repo,
            costs=CostMode.FRICTIONLESS,
            decided=engine.decided(slug, TimingRule(THESIS_N)),
        )
        yb, yr, yg = _year_returns(bh.run), _year_returns(r.run), _year_returns(gross)
        sw = _year_switches(r.run)
        inv: dict[int, list[bool]] = {}
        for (d, _), held in zip(r.run.nav[1:], r.run.invested, strict=True):
            inv.setdefault(d.year, []).append(held)
        out += [
            f"### {title} ({w.start.isoformat()} → {w.end.isoformat()}; first and last years "
            "partial)",
            "",
            "| Year | Buy-and-hold | N=30 after costs | N=30 before costs | Excess (after costs) "
            "| Switches | % invested |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        wins = 0
        for y in sorted(yb):
            ex = yr[y] - yb[y]
            wins += ex > 0
            flags = inv.get(y, [])
            share = Decimal(sum(flags)) / Decimal(len(flags)) if flags else _ZERO
            out.append(
                f"| {y} | {_p(yb[y])} | {_p(yr[y])} | {_p(yg[y])} | {_pp(ex)} | {sw.get(y, 0)} "
                f"| {_p(share)} |"
            )
        out += [
            "",
            f"N=30 beat buy-and-hold (after costs) in **{wins} of {len(yb)}** calendar years.",
            "",
        ]
    return out


def _episodes(engine: _Engine) -> list[str]:
    out = [
        "## Drawdown episodes — when did N=30 get out and back in",
        "",
        "Peak and trough are the index's own (largest peak-to-trough fall inside the search "
        "range). "
        "N=30's path here is before costs (TRI when in, cash at repo-50bp when out), positions "
        "lagged one session as everywhere else. **Out** is N=30's first exit on or after the "
        "peak; **back in for good** is its last entry before the index regained the peak (or the "
        "range end).",
        "",
        "| Index | Episode | Index peak → trough (fall) | Recovered peak | N=30 out (index vs peak "
        "then) | Round trips peak→recovery | N=30 back in for good (index vs trough then) "
        "| N=30 peak→trough vs index | N=30 peak→recovery vs index |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for slug, title in HEADLINE.items():
        series = engine.series(slug)
        pts = series.points
        sessions = series.sessions
        decided = engine.decided(slug, TimingRule(THESIS_N))
        for name, lo, hi in _EPISODES:
            idx = [i for i, s in enumerate(sessions) if lo <= s <= hi]
            if len(idx) < 30:
                continue
            peak_i = best_peak = idx[0]
            worst = _ZERO
            trough_i = idx[0]
            for i in idx:
                if pts[i].level > pts[best_peak].level:
                    best_peak = i
                fall = _ONE - pts[i].level / pts[best_peak].level
                if fall > worst:
                    worst, peak_i, trough_i = fall, best_peak, i
            rec_i = next(
                (i for i in range(trough_i, len(pts)) if pts[i].level >= pts[peak_i].level), None
            )
            end_i = rec_i if rec_i is not None else len(pts) - 1
            held = [bool(decided[sessions[i - 1]][0]) for i in range(peak_i + 1, end_i + 1)]
            changes = [
                (sessions[i], held[k])
                for k, i in enumerate(range(peak_i + 1, end_i + 1))
                if k == 0 or held[k] != held[k - 1]
            ]
            out_day = next((d for d, h in changes if not h), None)
            entries = [d for d, h in changes if h]
            back = entries[-1] if entries and (not held or held[-1]) else None
            round_trips = sum(1 for d, h in changes[1:] if h)
            # Gross equity of N=30 from the peak.
            eq = _ONE
            path = [eq]
            for k, i in enumerate(range(peak_i + 1, end_i + 1)):
                if held[k]:
                    eq *= pts[i].level / pts[i - 1].level
                else:
                    days = Decimal((sessions[i] - sessions[i - 1]).days)
                    rate = engine.repo.earning_rate(max(sessions[i - 1], engine.repo.coverage_from))
                    eq *= _ONE + rate * days / Decimal("365")
                path.append(eq)
            n_trough = path[trough_i - peak_i]
            idx_rec = pts[end_i].level / pts[peak_i].level - _ONE

            peak_level, trough_level = pts[peak_i].level, pts[trough_i].level
            if out_day is None:
                out_txt = "never"
            else:
                lvl = pts[sessions.index(out_day)].level
                out_txt = f"{out_day.isoformat()} ({_pp(lvl / peak_level - _ONE)})"
            if back is None:
                back_txt = "still out" if held and not held[-1] else "—"
            else:
                lvl = pts[sessions.index(back)].level
                back_txt = f"{back.isoformat()} ({_pp(lvl / trough_level - _ONE)})"

            out.append(
                f"| {title} | {name} | {sessions[peak_i].isoformat()} → "
                f"{sessions[trough_i].isoformat()} ({_p(worst)}) "
                f"| {sessions[rec_i].isoformat() if rec_i is not None else 'not yet'} "
                f"| {out_txt} | {round_trips} | {back_txt} "
                f"| {_pp(n_trough - _ONE)} vs {_pp(-worst)} "
                f"| {_pp(path[-1] - _ONE)} vs {_pp(idx_rec)} |"
            )
    out.append("")
    return out


def _today(engine: _Engine) -> list[str]:
    out = [
        "## Today — what each rule says on the lake's last session",
        "",
        "The decision struck at the last session's close (traded at the next session's close). "
        "**Since** is the session whose close last flipped the decision.",
        "",
        "| Index | Last session | Close | N | SMA | Close vs SMA | Decision | Since |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for slug, title in {**HEADLINE, **SECONDARY}.items():
        series = engine.series(slug)
        last = series.points[-1]
        for n in NS:
            decided = engine.decided(slug, TimingRule(n))
            state, sma = decided[last.session]
            since = last.session
            for s in reversed(series.sessions):
                if decided[s][0] != state:
                    break
                since = s
            assert sma is not None
            out.append(
                f"| {title} | {last.session.isoformat()} | {_r(last.level)} | {n} | {_r(sma)} "
                f"| {_pp(last.level / sma - _ONE)} | {'**invested**' if state else 'cash'} "
                f"| {since.isoformat()} |"
            )
    out.append("")
    return out


def _data_notes(engine: _Engine) -> list[str]:
    out = [
        "## Data checks (generated)",
        "",
        "| Index | Points | First | Last | `index_name` spellings (sessions) | Max |daily move| |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for slug, title in {**HEADLINE, **SECONDARY}.items():
        s = engine.series(slug)
        spell = "; ".join(
            f"'{n}' x{len(d)}" + (f" ({', '.join(x.isoformat() for x in d)})" if len(d) < 5 else "")
            for n, d in sorted(s.spellings.items(), key=lambda kv: -len(kv[1]))
        )
        moves = [
            abs(b.level / a.level - _ONE) for a, b in zip(s.points, s.points[1:], strict=False)
        ]
        out.append(
            f"| {title} | {len(s.points)} | {s.points[0].session} | {s.points[-1].session} "
            f"| {spell} | {_p(max(moves))} |"
        )
    out += [
        "",
        "Each index is one parquet file per session: the case-variant `index_name` on two "
        "sessions is a label on the only point for that session, not a second value, so there is "
        "nothing to disagree; `load_index` normalises case and would refuse a name that differed "
        "otherwise. Every point's `knowable_date` equals its session (a TRI close is knowable at "
        "that close).",
        "",
    ]
    return out


def render_index_part(engine: _Engine) -> list[str]:
    """Every index-timing table, in report order."""
    out = [
        "## What was run",
        "",
        "**Rule (the reading implemented — the article's own rule is not known here):** hold the "
        "index's total-return index when its close is at or above its N-session simple moving "
        "average of closes, otherwise hold cash. The decision is struck at the close of session t "
        "using only closes knowable by t (t's own included) and is traded at the close of the "
        "**next** session t+1 — never the same close. N=30 is the thesis; N=10, 20, 50, 100, 150 "
        "and 200 are context so 30 is not read alone. No band, daily check unless a table says "
        "otherwise. Buy-and-hold of the same TRI is the benchmark, bought at the window's first "
        "close.",
        "",
        "**Money:** ₹10 L opening book; each switch trades the whole book in a liquid index ETF "
        f"(cost-model key {ETF_ISIN}, NIFTYBEES; one unit = TRI ÷ 100) at the close ± "
        f"{(SLIPPAGE * 100).normalize()}% slippage a side, charged by the shared cost model "
        "(`execution.costs`, invariant #4) at the rate card in force on the trade date. The card "
        "is delivery equity and charges delivery STT (0.1% a leg since 2012) because it has no "
        "ETF instrument type; an equity ETF's exchange STT is far lower, so the ex-STT column "
        "brackets the true cost from below. Before 2006-01-01 (where the card starts) a switch is "
        "priced on the 2006-01-01 card — a stated proxy. Buy-and-hold pays the same entry costs.",
        "",
        "**Cash:** while out of the market the book earns RBI repo - 50 bp, Actual/365, credited "
        "monthly (`backtest.cash_interest`, the repo's convention). The lake has FBIL *FX* "
        "reference rates only — no FBIL overnight MIBOR / TREPS series — so the repo schedule is "
        "the cash rate. It starts 2006-01-01; before that the 2006-01-01 rate (6.25% - 0.5%) is "
        "carried back as a proxy. Indian short rates were mostly higher than that in 1995-2001, "
        "so the proxy, if anything, understates what timing earned in cash in those years.",
        "",
        "**Tax:** `backtest.tax` for a resident individual, 30% slab, no surcharge, tax paid at FY "
        "end; STCG/LTCG by FIFO lot on the ETF (Sec 111A/112A, Sec 55(2)(ac) grandfathering at the "
        "31-01-2018 close), interest at slab. Reported as the liquidated after-tax CAGR (open "
        "holding deemed sold at the end, so buy-and-hold's deferred LTCG is charged too) and only "
        "on windows that open on or after 2006-01-01.",
        "",
        "**Not modelled:** ETF expense ratio and tracking error (both strategies hold the ETF; "
        "timing holds it less), the TRI's own dividend timing vs an ETF's, intraday execution.",
        "",
    ]
    out += _data_notes(engine)
    out += _headline(engine)
    out += _thesis_rank(engine)
    out += ["## Every N, every window — NIFTY 50 and NIFTY 500 (ranked by CAGR/DD after costs)", ""]
    out += _grid(engine, HEADLINE, None)
    out += _walk_forward(engine)
    out += _robustness(engine)
    out += _per_year(engine)
    out += _episodes(engine)
    out += ["## Secondary — Midcap 150 and Smallcap 250 (ranked by CAGR/DD after costs)", ""]
    out += _grid(engine, SECONDARY, ("full", "2006→", "decade", "six-year"))
    out += _today(engine)
    return out


# ── the D13 part ──────────────────────────────────────────────────────────────────────────────────


def _gate_readings(engine: _Engine) -> list[str]:
    """NIFTY 50 TRI against its 30/50/200-session average on recent monthly rebalance sessions."""
    from backtest.run import _first_session_of_each_month, _RegimeSource

    series = engine.series("nifty50")
    last = series.points[-1].session
    sessions = [s for s in series.sessions if s >= date(2026, 5, 1)]
    rebalances = set(_first_session_of_each_month(series.sessions))
    sources = {
        n: _RegimeSource.published(through=last, ma_days=n, data_root=engine.data_root)
        for n in (30, 50, 200)
    }
    out = [
        "### The regime gate on recent rebalance sessions",
        "",
        "D13 reads the gate only on the monthly rebalance session (the first session of the "
        "month): risk-on is NIFTY 50 TRI ≥ its N-session average (`RegimeReading.risk_on`). "
        "The last row is the lake's last session, for reference (not a rebalance).",
        "",
        "| Session | TRI | 200-MA | 200 gate | 50-MA | 50 gate | 30-MA | 30 gate |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in [*(x for x in sessions if x in rebalances), last]:
        cells: list[str] = []
        level: Decimal | None = None
        for n in (200, 50, 30):
            r: RegimeReading = sources[n].reading(s)
            level = r.index_level
            cells += [_r(r.moving_average), "risk-on" if r.risk_on else "**risk-off**"]
        assert level is not None
        tag = " (last session)" if s == last else ""
        out.append(f"| {s.isoformat()}{tag} | {_r(level)} | {' | '.join(cells)} |")
    out.append("")
    return out


def _today_holdings(today_dir: Path, *, data_root: Path | None, through: date) -> list[str]:
    out = [
        "### What each would hold on the lake's last session",
        "",
        "From replays of each arm struck from 2016-09-01 through the lake's last session "
        "(floor-only universe, same switches as the tables), the ISINs held at the terminal "
        "session and the cash share of NAV.",
        "",
        "| Arm | Floor | Terminal | Names held | Cash share of NAV | Holdings (symbol) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    by_repr = {repr(a.v2): a for a in REGIME_LENGTH_SET}
    held_isins: set[str] = set()
    for f in (today_dir / "ledgers").glob("*.json"):
        held_isins |= set(json.loads(f.read_text(encoding="utf-8"))["terminal_prices"])
    symbols = symbols_for(held_isins, data_root=data_root, through=through)
    for f in sorted((today_dir / "runs").glob("*.json")):
        summary = json.loads(f.read_text(encoding="utf-8"))
        arm = by_repr.get(summary["spec"].get("parameters", ""))
        if arm is None:
            continue
        ledger = json.loads(
            (today_dir / "ledgers" / f"{summary['digest']}.json").read_text("utf-8")
        )
        qty: dict[str, int] = {}
        for t in ledger["trades"]:
            q = int(t["quantity"])
            qty[t["isin"]] = qty.get(t["isin"], 0) + (q if t["side"] == "BUY" else -q)
        prices = {k: Decimal(v) for k, v in ledger["terminal_prices"].items()}
        nav = Decimal(ledger["terminal_nav"])
        held = sorted(prices)
        # Quantities from trades alone miss corporate-action share changes; the value is a share
        # estimate only, the names are exact (the terminal mark lists every ISIN held).
        invested = sum((prices[i] * max(qty.get(i, 0), 0) for i in held), _ZERO)
        cash_share = _ONE - invested / nav if nav > _ZERO else _ZERO
        floor = summary["spec"]["universe"]
        floor_txt = "₹10 cr" if "100000000" in floor else "₹1 cr"
        names = ", ".join(symbols.get(i, i) for i in held) if held else "— (all cash)"
        out.append(
            f"| {arm.label} | {floor_txt} | {summary['terminal']} | {len(held)} "
            f"| ~{_p(max(cash_share, _ZERO))} | {names} |"
        )
    out.append("")
    return out


def symbols_for(isins: set[str], *, data_root: Path | None, through: date) -> dict[str, str]:
    """ISIN -> NSE symbol off the ``through`` session's raw prices — a display label only."""
    import pyarrow.parquet as pq

    from dataplatform.store.paths import Layer, layer_root

    part = layer_root(Layer.L1, data_root=data_root) / "prices_raw" / f"date={through.isoformat()}"
    if not part.is_dir() or not isins:
        return {}
    table = pq.read_table(part, columns=["isin", "exchange", "symbol"]).to_pylist()
    return {r["isin"]: r["symbol"] for r in table if r["exchange"] == "NSE" and r["isin"] in isins}


def render_d13_part(engine: _Engine, run_dirs: Sequence[Path], today_dir: Path | None) -> list[str]:
    """The D13 regime-length tables, via the M14.5 renderer's collector and table."""
    from backtest.m14_5_report import _WINDOW_ORDER, _floor_label, _table, collect, scorecard
    from backtest.sweep import HIGH_FLOOR, LOW_FLOOR

    out = [
        "## Part two — D13's regime gate: 200 vs 50 vs 30 sessions",
        "",
        "D13 (`PAPER_RATIFIED_2026_09_06`) parks the momentum book in cash when NIFTY 50 TRI is "
        "below its 200-session average on the monthly rebalance session. The parameter already "
        "exists (`MomentumV2Parameters.regime_ma_days`, default 200 = D13); the arms set only it:",
        "",
    ]
    for arm in REGIME_LENGTH_SET:
        out.append(f"* **{arm.label}** — {arm.note}.")
    out += [
        "",
        "Same harness as `ops/gates/M14.5-regime-reentry-report.md` (`backtest.m12_rerun`, book "
        "corporate actions and cash interest on, every order through A8's ratified rails), same "
        "mandate: XIRR ÷ max DD; ₹1 cr and ₹10 cr median-turnover floors; decade, six-year, "
        "walk-forward; never averaged; bar XIRR > 25%. Columns as in M14.5.",
        "",
    ]
    facts = []
    manifests: dict[str, Any] = {}
    selected: dict[str, str] = {}
    for d in run_dirs:
        manifests[d.name] = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        results = json.loads((d / "results.json").read_text(encoding="utf-8"))
        selected[manifests[d.name]["universe"]] = results.get("selected", "")
        facts.extend(collect(d, data_root=engine.data_root, arms=REGIME_LENGTH_SET))
    out += [
        "| Run directory | Universe | Commit | Lake last session | Units |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, m in manifests.items():
        out.append(
            f"| `{name}` | {m['universe']} | `{m['commit'][:10]}` | {m['lake_last_session']} "
            f"| {', '.join(m['units'])} |"
        )
    out.append("")
    universes = sorted({f.universe for f in facts}, key=lambda u: (u != "turnover_floor", u))
    for universe in universes:
        title = "floor-only universe" if universe == "turnover_floor" else f"{universe} universe"
        out += [f"### Results — {title}", ""]
        for window in _WINDOW_ORDER:
            rows_w = [f for f in facts if f.universe == universe and f.window == window]
            if not rows_w:
                continue
            first, last = WINDOWS[window]
            out += [f"#### {window} ({first.isoformat()} → {last.isoformat()})", ""]
            for floor in (LOW_FLOOR, HIGH_FLOOR):
                rows = [f for f in rows_w if f.floor == floor]
                if rows:
                    out += [f"**{_floor_label(floor)}**", "", *_table(rows), ""]
        if selected.get(universe):
            out += [
                f"**Walk-forward choice ({title})**: on the selection window the code "
                f"(`backtest.verdict.run_walk_forward`, ₹1 cr floor first) chose "
                f"**{selected[universe]}**; its verification row is above, read after the choice "
                "was frozen.",
                "",
            ]
    out += [
        line.replace("## Scorecard", "### Scorecard")
        for line in scorecard(facts, REGIME_LENGTH_SET)
    ]
    out += _gate_readings(engine)
    if today_dir is not None:
        out += _today_holdings(today_dir, data_root=engine.data_root, through=engine.through)
    return out


# ── the today runs ────────────────────────────────────────────────────────────────────────────────


def run_today(out_dir: Path, *, data_root: Path | None, through: date) -> None:
    """Replay the regime-length arms 2016-09-01 → ``through`` on both floors, persisting each run.

    Resumable (``backtest.run_ledger``).
    """
    from backtest.book_actions import corporate_actions_in_force, load_store_book_actions
    from backtest.run_ledger import persist_run_ledgers, refuse_lake_location
    from backtest.sweep import HIGH_FLOOR, LOW_FLOOR, run_sweep

    out_dir = refuse_lake_location(out_dir, data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (
        corporate_actions_in_force(
            load_store_book_actions(data_root=data_root), apply_to_book=True
        ),
        accrue_cash_interest(load_repo_rate_schedule()),
        persist_run_ledgers(out_dir),
    ):
        run_sweep(
            start=date(2016, 9, 1),
            end=through,
            arms=REGIME_LENGTH_SET,
            floors=(LOW_FLOOR, HIGH_FLOOR),
            data_root=data_root,
            universe_name="turnover_floor",
        )


def _part3(engine: _Engine, run_dir: Path, regime_dirs: Sequence[Path]) -> list[str]:
    from backtest import sma30_report
    from backtest.sweep import D13_PAPER_BASELINE

    runs = sma30_report.collect(run_dir, sma30_report.arm_labels(), runner="sma30_stock")
    d13: list[sma30_report.Part3Run] = []
    labels = {repr(D13_PAPER_BASELINE.v2): D13_PAPER_BASELINE.label}
    for d in regime_dirs[:1]:  # the floor-only directory
        d13 += sma30_report.collect(d, labels, runner="momentum_v2")

    def bh_drawdown(window: str) -> Decimal:
        start, end = WINDOWS[window]
        return engine.row("nifty50", Window(window, start, end, ""), None).card.max_drawdown

    held = {i for r in runs for i in r.held_at_end}
    symbols = symbols_for(held, data_root=engine.data_root, through=engine.through)
    return sma30_report.render(runs, d13, bh_drawdown, symbols)


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backtest.ma_timing_report")
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--through", type=date.fromisoformat, required=True)
    parser.add_argument("--regime-runs", type=Path, nargs="*", default=[])
    parser.add_argument("--today-runs", type=Path, default=None)
    parser.add_argument(
        "--make-today-runs", action="store_true", help="replay the today runs into --today-runs"
    )
    parser.add_argument("--part3-runs", type=Path, default=None, help="a backtest.sma30_run dir")
    parser.add_argument("--out", type=Path, default=REPORT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Render the report (or, with ``--make-today-runs``, only replay the today runs)."""
    args = _parse_args(argv)
    if args.make_today_runs:
        if args.today_runs is None:
            raise SystemExit("--make-today-runs needs --today-runs")
        run_today(args.today_runs, data_root=args.data_root, through=args.through)
        return 0
    engine = _Engine(data_root=args.data_root, through=args.through)
    lines = [
        "# MA timing — the 30-day moving-average thesis on NSE TRIs, and D13's regime length",
        "",
        "> **Generated** by `uv run python -m backtest.ma_timing_report` from the published TRIs "
        "in the lake and the saved D13 runs listed in part two; nothing in the tables is typed by "
        "hand. The section after the marker at the end is hand-written analysis and is labelled "
        "as such. Research only: nothing here changes the paper strategy.",
        "",
        f"Lake read through **{args.through.isoformat()}** (read-only; nothing fetched).",
        "",
    ]
    lines += render_index_part(engine)
    if args.regime_runs:
        lines += render_d13_part(engine, args.regime_runs, args.today_runs)
    if args.part3_runs is not None:
        lines += _part3(engine, args.part3_runs, args.regime_runs)
    hand = ""
    if args.out.is_file():
        existing = args.out.read_text(encoding="utf-8")
        if MARKER in existing:
            hand = existing.split(MARKER, 1)[1]
    lines += [MARKER, hand.strip("\n"), ""]
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"  report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
