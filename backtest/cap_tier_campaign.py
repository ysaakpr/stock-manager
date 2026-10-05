"""X2: the cap-tier study — three liquidity-tier arms against the current strategies (2026-10-05).

Owner request (2026-10-05): run multi-cap, focused-midcap and focused-smallcap versions of the swing
composite and measure them against the strategies the repo already has, over the whole history.
The tiers are **liquidity-rank tiers that proxy AMFI cap tiers** (``backtest.cap_tiers``; the
proxy's validation is ``ops/studies/cap-tier-size-measure-2026-10-05.md``).

``python -m backtest.cap_tier_campaign run --out DIR --data-root ROOT --workers N``
    Replays the three cap-tier arms and the four comparison arms (Swing composite (M10.7), M10.7 +
    regime gate, Momentum v2, Naive momentum) on the continuous ``full`` window
    (``backtest/windows.yaml``, 2012-07-04 → 2026-08-31) and on each fold's test window
    (``backtest/folds.yaml``), at both the ₹1 crore and ₹10 crore floors, with the store's
    corporate actions in force (book and signal) and cash interest on — the fold campaign's own
    contexts. One unit per (window, floor), longest first, at most two at a time. Resumable: every
    run persists its summary, fill ledger and NAV under ``DIR`` keyed by its digest.

``python -m backtest.cap_tier_campaign render --out DIR --data-root ROOT``
    Replays nothing. Reads every run back and writes ``DIR/reports/cap-tiers.md``: per window x
    floor, pre-tax XIRR, after-tax XIRR on realised gains (resident, 30 % slab, no surcharge, tax
    paid at FY end), max drawdown, return / drawdown, worst calendar year, longest drawdown in days
    to recover, trades, total costs, rail blocks by rail, end cash share and holdings stuck in names
    that stopped printing; the NIFTY 50, Midcap 150 and Smallcap 250 TRIs on the same windows; and
    for the smallcap arm the top-5 names' share of profit and the 2018-01 → 2020-03 small-cap crash.

What this module never does: tune a parameter (every arm is fixed in ``backtest.sweep``), write
under the lake, read a wall clock into a result, or value a name that stopped printing at anything
but its last printed close (the book's own rule; the report says so).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from multiprocessing import get_context
from pathlib import Path
from typing import Final

from backtest.book_actions import (
    BookActionCalendar,
    BookActionSource,
    UnmodelledAction,
    corporate_actions_in_force,
    load_store_book_actions,
)
from backtest.campaign import MAX_WORKERS, UnitOutcome, _git_commit
from backtest.cash_interest import accrue_cash_interest, load_repo_rate_schedule
from backtest.fold_campaign import PROFILE
from backtest.folds import load_folds
from backtest.nav import nav_file, read_nav
from backtest.run import _L1Reader
from backtest.run_ledger import (
    RunSummary,
    _replayed_quantities,
    load_run,
    persist_run_ledgers,
    refuse_lake_location,
)
from backtest.sweep import (
    ARMS,
    CAP_TIER_ARMS,
    FOCUSED_SMALLCAP,
    HIGH_FLOOR,
    LOW_FLOOR,
    Arm,
    l1_grandfathering,
    run_digests,
    run_sweep,
)
from backtest.tax import (
    GrandfatheringPrices,
    ReissueEvent,
    RunLedger,
    TaxError,
    compute_after_tax,
    load_tax_schedule,
)
from backtest.windows import Window, load_windows
from dataplatform.ingest.indices import TRI_METHOD_PUBLISHED, read_tri_series
from dataplatform.logging import get_logger
from execution.broker import Side

__all__ = [
    "BENCHMARK_SLUGS",
    "COMPARISON_LABELS",
    "CRASH_WINDOW",
    "CapTierPlan",
    "PathFigures",
    "annualised_growth",
    "cap_tier_plan",
    "longest_drawdown",
    "main",
    "max_drawdown",
    "name_contributions",
    "path_figures",
    "worst_calendar_year",
]

_LOG = get_logger(__name__)

#: The current strategies the cap-tier arms are measured against, by their sweep labels.
COMPARISON_LABELS: Final[tuple[str, ...]] = (
    "Swing composite (M10.7)",
    "M10.7 + regime gate",
    "Momentum v2, all on (M9.5)",
    "Naive momentum (M4.10)",
)
#: The benchmarks every window is reported against: slug → label.
BENCHMARK_SLUGS: Final[tuple[tuple[str, str], ...]] = (
    ("nifty50", "NIFTY 50 TRI"),
    ("niftymidcap150", "NIFTY Midcap 150 TRI"),
    ("niftysmallcap250", "NIFTY Smallcap 250 TRI"),
)
#: The 2018-2020 small-cap drawdown, fixed before any run: the NIFTY Smallcap 250 topped in January
#: 2018 and bottomed in March 2020. Measured from the last NAV of 2017 to the last of 2020-03-31.
CRASH_WINDOW: Final = (date(2017, 12, 31), date(2020, 3, 31))
_FLOORS: Final = (LOW_FLOOR, HIGH_FLOOR)
_FULL = "full"
_ZERO = Decimal(0)
#: Significant digits for the annualising power; far past the report's two places, and fixed so the
#: figure is the same whatever context the caller runs in.
_CAGR_PRECISION: Final = 28
_ONE = Decimal(1)


class CapTierCampaignError(RuntimeError):
    """The campaign cannot run or render as asked — fails loud."""


@dataclass(frozen=True, slots=True)
class CapTierPlan:
    """What the campaign runs: arms x windows x floors, and where it keeps the runs."""

    out_dir: Path
    arms: tuple[Arm, ...]
    windows: tuple[Window, ...]
    data_root: Path | None
    book_actions: bool = True

    @property
    def units(self) -> tuple[tuple[int, Decimal], ...]:
        """``(window index, floor)``, longest window first so two workers finish close together."""
        order = sorted(
            range(len(self.windows)),
            key=lambda i: (-(self.windows[i].end - self.windows[i].start).days, i),
        )
        return tuple((i, floor) for i in order for floor in _FLOORS)


def cap_tier_plan(
    out_dir: Path, *, data_root: Path | None, book_actions: bool = True
) -> CapTierPlan:
    """The full window plus every fold's test window, the cap-tier arms and the comparison arms."""
    by_label = {arm.label: arm for arm in ARMS}
    comparison = tuple(by_label[label] for label in COMPARISON_LABELS)
    full = load_windows().named(_FULL)
    tests = tuple(
        Window(f"{fold.name}-test", fold.test.start, fold.test.end) for fold in load_folds().folds
    )
    return CapTierPlan(
        out_dir=out_dir,
        arms=(*CAP_TIER_ARMS, *comparison),
        windows=(Window(_FULL, full.start, full.end), *tests),
        data_root=data_root,
        book_actions=book_actions,
    )


# ── running ──────────────────────────────────────────────────────────────────────────────────────


def _contexts(stack: ExitStack, actions: BookActionSource | None) -> None:
    """The fold campaign's contexts: corporate actions in the book and the signal, cash interest."""
    stack.enter_context(corporate_actions_in_force(actions, apply_to_book=True))
    stack.enter_context(accrue_cash_interest(load_repo_rate_schedule()))


def _actions(plan: CapTierPlan) -> BookActionCalendar | None:
    return load_store_book_actions() if plan.book_actions else None


def run_unit(plan: CapTierPlan, window_index: int, floor: Decimal) -> UnitOutcome:
    """Run (or resume) every arm on one window at one floor. Module-level for spawn."""
    window = plan.windows[window_index]
    with ExitStack() as stack:
        _contexts(stack, _actions(plan))
        stack.enter_context(persist_run_ledgers(plan.out_dir))
        result = run_sweep(
            start=window.start,
            end=window.end,
            arms=plan.arms,
            floors=(floor,),
            data_root=plan.data_root,
        )
    failed = sum(1 for row in result.rows if not row.ok)
    for row in result.rows:
        if not row.ok:
            _LOG.error(
                "cap_tiers.arm_failed", arm=row.arm.label, window=window.name, error=row.error
            )
    name = f"{window.name}@{floor}"
    _LOG.info("cap_tiers.unit_done", unit=name, failed=failed, resumed=result.resumed)
    return UnitOutcome(name, len(result.rows) - result.resumed - failed, result.resumed, failed)


def run_units(plan: CapTierPlan, *, workers: int) -> list[UnitOutcome]:
    if not 1 <= workers <= MAX_WORKERS:
        raise CapTierCampaignError(f"workers must be 1..{MAX_WORKERS} on this box, got {workers}")
    if workers == 1:
        return [run_unit(plan, index, floor) for index, floor in plan.units]
    with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
        futures = [pool.submit(run_unit, plan, index, floor) for index, floor in plan.units]
        return [future.result() for future in futures]


def _digests(
    plan: CapTierPlan, actions: BookActionSource | None
) -> dict[tuple[str, str, Decimal], str]:
    """``(arm, window, floor) -> digest`` under the same contexts the runs were made in."""
    out: dict[tuple[str, str, Decimal], str] = {}
    with ExitStack() as stack:
        _contexts(stack, actions)
        for window in plan.windows:
            digests = run_digests(
                start=window.start, end=window.end, arms=plan.arms, floors=_FLOORS
            )
            for (label, floor), digest in digests.items():
                out[(label, window.name, floor)] = digest
    return out


# ── path figures ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PathFigures:
    """What a dated value path says on its own: growth, drawdown, worst year, longest drawdown."""

    cagr: Decimal
    max_drawdown: Decimal
    worst_year: tuple[int, Decimal] | None
    #: Calendar days from a peak to the first close back at or above it; the longest such spell.
    longest_drawdown_days: int
    #: False when the longest spell had not recovered by the path's end (it is then a lower bound).
    recovered: bool


def max_drawdown(points: Sequence[tuple[date, Decimal]]) -> Decimal:
    """The largest peak-to-trough fall, as a positive fraction."""
    peak, worst = _ZERO, _ZERO
    for _, value in points:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def worst_calendar_year(points: Sequence[tuple[date, Decimal]]) -> tuple[int, Decimal] | None:
    """The calendar year with the lowest return, each year from the previous year's last value.

    The first year runs from the path's first value; a partial year is a year like any other here,
    and the report labels the window's span so a reader can see which years are partial.
    """
    if len(points) < 2:
        return None
    last: dict[int, Decimal] = {}
    for when, value in points:
        last[when.year] = value
    years = sorted(last)
    base = points[0][1]
    out: list[tuple[int, Decimal]] = []
    for year in years:
        if base > 0:
            out.append((year, last[year] / base - 1))
        base = last[year]
    return min(out, key=lambda item: (item[1], item[0])) if out else None


def longest_drawdown(points: Sequence[tuple[date, Decimal]]) -> tuple[int, bool]:
    """The longest underwater spell — peak to the first value back at or above it — in calendar
    days, and whether it recovered. A spell still open at the end counts to the end, unrecovered."""
    if not points:
        return 0, True
    peak_value, peak_date = points[0][1], points[0][0]
    longest, underwater = 0, False
    for when, value in points[1:]:
        if value >= peak_value:
            if underwater:
                longest = max(longest, (when - peak_date).days)
            peak_value, peak_date, underwater = value, when, False
        else:
            underwater = True
    tail = (points[-1][0] - peak_date).days
    if underwater and tail > longest:
        return tail, False
    return longest, True


def annualised_growth(growth: Decimal, days: int) -> Decimal:
    """The compound annual rate that turns 1 into ``growth`` over ``days`` calendar days.

    ``growth ** (365 / days) - 1``, ACT/365F like ``backtest.xirr``, in ``Decimal`` end to end: a
    return is held to the money rule (CLAUDE.md), and a float round-trip here would also make the
    figure depend on binary rounding rather than on the path. The power runs in a local context of
    :data:`_CAGR_PRECISION` digits so the result never depends on the caller's ambient context.
    Assumes ``growth >= 0`` and ``days > 0``; raises ``CapTierCampaignError`` otherwise rather than
    returning a rate for a span that has none.
    """
    if days <= 0 or growth < 0:
        raise CapTierCampaignError(f"no annual rate for growth {growth} over {days} days")
    with localcontext() as ctx:
        ctx.prec = _CAGR_PRECISION
        return growth ** (Decimal(365) / Decimal(days)) - 1


def path_figures(points: Sequence[tuple[date, Decimal]]) -> PathFigures:
    if len(points) < 2 or points[0][1] <= 0:
        raise CapTierCampaignError("a path needs two or more points and a positive start")
    days = (points[-1][0] - points[0][0]).days
    growth = points[-1][1] / points[0][1]
    cagr = annualised_growth(growth, days) if days > 0 else _ZERO
    longest, recovered = longest_drawdown(points)
    return PathFigures(
        cagr=cagr,
        max_drawdown=max_drawdown(points),
        worst_year=worst_calendar_year(points),
        longest_drawdown_days=longest,
        recovered=recovered,
    )


# ── ledger figures ───────────────────────────────────────────────────────────────────────────────


def _canonical(ledger: RunLedger) -> dict[str, str]:
    """Each ISIN to the last ISIN its reissue chain carried it to — one name, one key."""
    parent: dict[str, str] = {}
    for event in ledger.corporate_events:
        if isinstance(event, ReissueEvent):
            parent[event.from_isin] = event.isin

    def root(isin: str) -> str:
        seen = set()
        while isin in parent and isin not in seen:
            seen.add(isin)
            isin = parent[isin]
        return isin

    names = {t.isin for t in ledger.trades} | set(parent) | set(parent.values())
    return {isin: root(isin) for isin in names}


def name_contributions(ledger: RunLedger) -> dict[str, Decimal]:
    """Each name's profit in rupees: sells + dividends + terminal value - buys, net of charges.

    A reissued ISIN is folded into its survivor, so a split that changed the ISIN is one name.
    Cash interest is no name's and is left out.
    """
    key = _canonical(ledger)
    out: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for trade in ledger.trades:
        sign = -1 if trade.side is Side.BUY else 1
        out[key.get(trade.isin, trade.isin)] += sign * trade.net_amount
    for dividend in ledger.dividends:
        out[key.get(dividend.isin, dividend.isin)] += dividend.amount
    for isin, quantity in _replayed_quantities(ledger).items():
        price = ledger.terminal_prices.get(isin)
        if price is None:
            raise CapTierCampaignError(f"{isin} held at the end with no terminal price")
        out[key.get(isin, isin)] += price * quantity
    return dict(out)


@dataclass(frozen=True, slots=True)
class RunRow:
    arm: str
    summary: RunSummary
    path: PathFigures
    after_tax: Decimal | None
    after_tax_error: str | None
    trades: int
    end_cash_share: Decimal
    stuck: tuple[tuple[str, date, Decimal, bool], ...]  # isin, last print, value, merger in store
    contributions: Mapping[str, Decimal]
    crash: tuple[Decimal, Decimal] | None  # return, max drawdown inside CRASH_WINDOW


def _crash(points: Sequence[tuple[date, Decimal]]) -> tuple[Decimal, Decimal] | None:
    start, end = CRASH_WINDOW
    inside = [p for p in points if start < p[0] <= end]
    before = [p for p in points if p[0] <= start]
    if not inside or not before:
        return None
    span = [before[-1], *inside]
    return span[-1][1] / span[0][1] - 1, max_drawdown(span)


def _row(
    out_dir: Path,
    arm: str,
    digest: str,
    *,
    fmv: GrandfatheringPrices,
    last_print: Mapping[str, date],
    mergers: frozenset[str],
) -> RunRow:
    loaded = load_run(out_dir, digest)
    if loaded is None:
        raise CapTierCampaignError(f"{arm}: run {digest[:12]} is not on disk — run it first")
    summary, ledger = loaded
    nav = read_nav(nav_file(out_dir, digest), digest=digest).points
    try:
        taxed = compute_after_tax(ledger, PROFILE, schedule=load_tax_schedule(), fmv=fmv)
        after_tax, error = taxed.after_tax_xirr_realised, taxed.realised_xirr_error
    except TaxError as failure:
        after_tax, error = None, str(failure)
    held = _replayed_quantities(ledger)
    values = {isin: ledger.terminal_prices[isin] * qty for isin, qty in held.items()}
    cash = ledger.terminal_nav - sum(values.values(), _ZERO)
    stuck = tuple(
        (isin, last_print[isin], values[isin], isin in mergers)
        for isin in sorted(held)
        if last_print.get(isin, ledger.terminal_date) < ledger.terminal_date
    )
    return RunRow(
        arm=arm,
        summary=summary,
        path=path_figures(nav),
        after_tax=after_tax,
        after_tax_error=error,
        trades=len(ledger.trades),
        end_cash_share=cash / ledger.terminal_nav if ledger.terminal_nav else _ZERO,
        stuck=stuck,
        contributions=name_contributions(ledger),
        crash=_crash(nav),
    )


# ── the report ───────────────────────────────────────────────────────────────────────────────────


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _lakh(value: Decimal) -> str:
    return f"₹{value / Decimal(100_000):,.2f} L"


def _floor(floor: Decimal) -> str:
    label = f"₹{floor / Decimal(10_000_000):.0f} cr/day"
    return f"{label} (cost model unvalidated for small-cap impact)" if floor == LOW_FLOOR else label


def _blocks(summary: RunSummary) -> str:
    if summary.rail_blocks is None:
        return "not recorded"
    return ", ".join(f"{k} {v}" for k, v in sorted(summary.rail_blocks.items())) or "none"


def _year(worst: tuple[int, Decimal] | None) -> str:
    return "n/a" if worst is None else f"{worst[0]}: {worst[1]:.2%}"


def _days(path: PathFigures) -> str:
    return f"{path.longest_drawdown_days}" + ("" if path.recovered else " (not recovered)")


def _benchmark_path(
    slug: str, window: Window, data_root: Path | None
) -> list[tuple[date, Decimal]] | None:
    series = read_tri_series(slug, window.end, method=TRI_METHOD_PUBLISHED, data_root=data_root)
    if series is None:
        return None
    return [(p.as_of, p.tri_value) for p in series.points if window.start <= p.as_of <= window.end]


def render(plan: CapTierPlan, *, commit: str) -> str:
    actions = _actions(plan)
    digests = _digests(plan, actions)
    mergers = frozenset(
        a.isin
        for a in (actions.between(None, date.max) if actions is not None else ())
        if isinstance(a, UnmodelledAction) and a.action_type == "MERGER"
    )
    reader = _L1Reader(data_root=plan.data_root)
    try:
        last_print = {
            w.isin: w.delisted_on - timedelta(days=1) if w.delisted_on is not None else date.max
            for w in reader.listing_windows()
        }
    finally:
        reader.close()
    _service, fmv = l1_grandfathering(plan.data_root)
    lines = [
        "# Cap-tier strategies vs the current strategies (X2, 2026-10-05)",
        "",
        f"- Runs: `{plan.out_dir}`, made at commit `{commit}`, lake `{plan.data_root}`.",
        "- Tiers are **liquidity-rank tiers that proxy AMFI cap tiers** (126-session median "
        "close x quantity, ranks 1-100 / 101-250 / 251-500): "
        "`ops/studies/cap-tier-size-measure-2026-10-05.md`.",
        "- Investor: resident individual, 30 % slab, no surcharge, tax paid at FY end; cash "
        "interest on; corporate actions in the book and the signal. Opening cash ₹10 lakh.",
        "- After-tax XIRR is on realised gains (holdings at the end untaxed).",
        "- Benchmarks: published TRIs. Midcap 150 / Smallcap 250 launched 2016-04-01; earlier "
        "levels are NSE's back-computed series (base 1000 on 2005-04-01).",
        "- Money in lakh (₹1 L = ₹100,000).",
        "- **₹1 cr floor: the cost model is unvalidated for small-cap impact cost** — every ₹1 cr "
        "number carries that caveat.",
        "- A name that stops printing is valued at its last printed close until the end (never "
        "written down, never credited a merger consideration): see 'stuck' below.",
        "",
    ]
    rows: dict[tuple[str, Decimal, str], RunRow] = {}
    for window in plan.windows:
        bench = {slug: _benchmark_path(slug, window, plan.data_root) for slug, _ in BENCHMARK_SLUGS}
        for floor in _FLOORS:
            lines += [
                f"## {window.name} ({window.start} → {window.end}), floor {_floor(floor)}",
                "",
                "| strategy | XIRR pre-tax | XIRR after-tax (realised) | max DD | return/DD "
                "| worst calendar year | longest DD (days) | trades | total costs "
                "| rail blocks | end cash | stuck at end |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|",
            ]
            for arm in plan.arms:
                digest = digests[(arm.label, window.name, floor)]
                row = _row(
                    plan.out_dir,
                    arm.label,
                    digest,
                    fmv=fmv,
                    last_print=last_print,
                    mergers=mergers,
                )
                rows[(window.name, floor, arm.label)] = row
                s = row.summary
                ratio = s.xirr / s.max_drawdown if s.max_drawdown else None
                lines.append(
                    f"| {arm.label} | {_pct(s.xirr)} | "
                    f"{_pct(row.after_tax) if row.after_tax is not None else row.after_tax_error} "
                    f"| {_pct(s.max_drawdown)} | {'n/a' if ratio is None else f'{ratio:.2f}'} "
                    f"| {_year(row.path.worst_year)} | {_days(row.path)} | {row.trades} "
                    f"| {_lakh(s.total_charges)} | {_blocks(s)} | {_pct(row.end_cash_share)} "
                    f"| {len(row.stuck)} ({sum(1 for x in row.stuck if x[3])} merger) |"
                )
            for slug, label in BENCHMARK_SLUGS:
                points = bench[slug]
                if not points or len(points) < 2:
                    lines.append(f"| {label} | not in lake | | | | | | | | | | |")
                    continue
                p = path_figures(points)
                ratio = p.cagr / p.max_drawdown if p.max_drawdown else None
                lines.append(
                    f"| {label} | {_pct(p.cagr)} | — | {_pct(p.max_drawdown)} | "
                    f"{'n/a' if ratio is None else f'{ratio:.2f}'} | {_year(p.worst_year)} "
                    f"| {_days(p)} | — | — | — | — | — |"
                )
            lines.append("")
    lines += _smallcap_section(rows, plan)
    return "\n".join(lines) + "\n"


def _smallcap_section(
    rows: Mapping[tuple[str, Decimal, str], RunRow], plan: CapTierPlan
) -> list[str]:
    lines = [
        "## Focused smallcap: concentration, the 2018-2020 crash, names that stopped printing",
        "",
    ]
    full = plan.windows[0]
    bench = _benchmark_path("niftysmallcap250", full, plan.data_root)
    bench_crash = _crash(bench) if bench else None
    lines.append(
        f"Crash window {CRASH_WINDOW[0]} → {CRASH_WINDOW[1]} (fixed before any run). "
        f"NIFTY Smallcap 250 TRI: return {_pct(bench_crash[0]) if bench_crash else 'n/a'}, "
        f"max DD {_pct(bench_crash[1]) if bench_crash else 'n/a'}."
    )
    lines.append("")
    lines += [
        "| floor | strategy | crash return | crash max DD | top-5 names' share of profit "
        "| top-5 names |",
        "|---|---|---|---|---|---|",
    ]
    for floor in _FLOORS:
        for arm in plan.arms:
            row = rows[(full.name, floor, arm.label)]
            gains = sorted(row.contributions.items(), key=lambda kv: (-kv[1], kv[0]))
            total = sum(row.contributions.values(), _ZERO)
            top = gains[:5]
            share = sum((v for _, v in top), _ZERO) / total if total > 0 else None
            crash = row.crash
            lines.append(
                f"| {_floor(floor).split(' (')[0]} | {arm.label} | "
                f"{_pct(crash[0]) if crash else 'n/a'} | {_pct(crash[1]) if crash else 'n/a'} | "
                f"{_pct(share)} of {_lakh(total)} | "
                + ", ".join(f"{isin} {_lakh(v)}" for isin, v in top)
                + " |"
            )
    lines += [
        "",
        "**Holdings stuck in names that stopped printing (full window, at the end):**",
        "",
    ]
    for floor in _FLOORS:
        for label in (
            FOCUSED_SMALLCAP,
            *(a.label for a in plan.arms if a.label != FOCUSED_SMALLCAP),
        ):
            row = rows[(full.name, floor, label)]
            if not row.stuck:
                continue
            stuck_value = sum((value for _, _, value, _ in row.stuck), _ZERO)
            share = stuck_value / row.summary.final_nav if row.summary.final_nav else _ZERO
            detail = "; ".join(
                f"{isin} last print {last}, valued {_lakh(value)}"
                + (" (MERGER in store)" if merger else "")
                for isin, last, value, merger in row.stuck
            )
            lines.append(
                f"- {_floor(floor).split(' (')[0]}, {label}: {len(row.stuck)} names, "
                f"{_lakh(stuck_value)} = {share:.2%} of the final NAV — {detail}"
            )
    lines.append("")
    return lines


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cap-tier-campaign", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("run", "render"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)
    data_root = args.data_root.resolve()
    out_dir = refuse_lake_location(args.out.resolve(), data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = cap_tier_plan(out_dir, data_root=data_root)
    commit = _git_commit()
    if args.command == "run":
        manifest = out_dir / "manifest.json"
        document = {
            "commit": commit,
            "data_root": str(data_root),
            "windows": [[w.name, w.start.isoformat(), w.end.isoformat()] for w in plan.windows],
            "arms": [a.label for a in plan.arms],
            "floors": [str(f) for f in _FLOORS],
        }
        if manifest.is_file():
            existing = json.loads(manifest.read_text(encoding="utf-8"))
            if existing != document:
                raise CapTierCampaignError(
                    f"{manifest} records a different campaign; use a new --out"
                )
        else:
            manifest.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        outcomes = run_units(plan, workers=args.workers)
        for outcome in outcomes:
            print(outcome)
        return 1 if any(o.failed for o in outcomes) else 0
    report = render(plan, commit=commit)
    path = out_dir / "reports" / "cap-tiers.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
