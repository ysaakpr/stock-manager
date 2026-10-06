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
    whose last NSE EQ print is before the window's end (each flagged when an unconverted merger —
    one with no sourced terms — explains it); the NIFTY 50, Midcap 150 and Smallcap 250 TRIs on the
    same windows; and for the smallcap arm the top-5 names' share of profit and the 2018-01 →
    2020-03 small-cap crash.
    Per window x floor it also splits each arm's idle cash by cause, from the saved ledger and NAV
    (``backtest.idle_cash``): mean cash share of NAV, waiting proceeds, ceiling-bound and other
    leftover, and buys at the per-order ceiling. It flags any arm whose longest buy-free span runs
    past ``EMPTY_TIER_DECISIONS`` decision sessions, with its XIRR from its first buy as an
    informational figure beside the headline.

``--universe {nifty500,turnover_floor}`` (both verbs) names the investable universe: ``nifty500``
    (point-in-time NIFTY 500 membership, the default — it refuses any window before the membership
    history's 2016-10-24 start, which the ``full`` window and early fold tests are) or
    ``turnover_floor`` (every NSE EQ name above the floor, no index screen). It is in every run's
    spec, the manifest and the report header; a directory started on one is never resumed on the
    other.

``render --match-saved-runs`` finds each arm's run on disk by its strategy specification with the
    store- and lake-derived fields (:data:`STORE_SPEC_FIELDS`) set aside, so runs made against an
    earlier store can still be rendered once corporate actions or the index history have moved
    their digests. Each arm must match exactly one saved run; the report names the store the runs
    recorded.

What this module never does: tune a parameter (every arm is fixed in ``backtest.sweep``), write
under the lake, read a wall clock into a result, or value a name that stopped printing at anything
but its last printed close (the book's own rule; the report says so).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
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
from backtest.fold_campaign import PROFILE, universe_line
from backtest.folds import load_folds
from backtest.idle_cash import (
    EMPTY_TIER_DECISIONS,
    BuyFreeSpan,
    IdleCash,
    idle_cash,
    longest_buy_free_span,
    xirr_from_first_buy,
)
from backtest.nav import nav_file, read_nav
from backtest.rails import ratified_backtest_rail_policy
from backtest.run import (
    DEFAULT_UNIVERSE,
    UNIVERSE_CHOICES,
    UniverseParameters,
    _first_session_of_each_month,
    _L1Reader,
)
from backtest.run_ledger import (
    RunSummary,
    SchemeCashCredit,
    _actions_identity,
    _replayed_quantities,
    load_run,
    persist_run_ledgers,
    refuse_lake_location,
    summary_path,
    unrecorded_scheme_cash,
)
from backtest.sweep import (
    _DEFAULT_OPENING_CASH,
    ARMS,
    CAP_TIER_ARMS,
    FOCUSED_SMALLCAP,
    HIGH_FLOOR,
    LOW_FLOOR,
    Arm,
    _arm_spec,
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
from dataplatform.corpactions import MergerTerms, load_merger_terms
from dataplatform.ingest.indices import TRI_METHOD_PUBLISHED, read_tri_series
from dataplatform.logging import get_logger
from execution.broker import Side

__all__ = [
    "BENCHMARK_SLUGS",
    "COMPARISON_LABELS",
    "CRASH_WINDOW",
    "MERGER_IN_STORE",
    "MERGER_TERMS_UNSOURCED",
    "RECORDED_STORE_FIELDS",
    "STORE_SPEC_FIELDS",
    "CapTierPlan",
    "PathFigures",
    "StuckHolding",
    "annualised_growth",
    "cap_tier_plan",
    "decision_sessions",
    "longest_drawdown",
    "main",
    "max_drawdown",
    "merger_flags",
    "name_contributions",
    "path_figures",
    "recorded_stores",
    "saved_run_digests",
    "stuck_holdings",
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
#: A held name's merger is a store ``MERGER`` row the curated terms do not cover: no surviving
#: ISIN, so the book leaves the holding where it is (``backtest.book_actions``).
MERGER_IN_STORE: Final = "merger in store, unconverted"
#: A held name's merger is a scheme the curated table lists as unsourced (``MERGER:unsourced``): the
#: scheme is known, no source states its terms, so the book leaves the holding where it is.
MERGER_TERMS_UNSOURCED: Final = "merger, terms unsourced"
#: The specification fields that record the store and lake a run was made against rather than
#: the strategy it ran: corporate actions in the book, the signal's split factors, the index
#: membership history. ``render --match-saved-runs`` sets them aside to find a run on disk.
STORE_SPEC_FIELDS: Final = frozenset({"book_actions", "signal_split_factors", "index_membership"})
#: The store identities a report names for the runs it renders, in the order it names them.
RECORDED_STORE_FIELDS: Final = ("book_actions", "signal_split_factors")


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
    #: The investable universe every run screens (``backtest.run.UNIVERSE_CHOICES``).
    universe: str = DEFAULT_UNIVERSE

    @property
    def units(self) -> tuple[tuple[int, Decimal], ...]:
        """``(window index, floor)``, longest window first so two workers finish close together."""
        order = sorted(
            range(len(self.windows)),
            key=lambda i: (-(self.windows[i].end - self.windows[i].start).days, i),
        )
        return tuple((i, floor) for i in order for floor in _FLOORS)


def cap_tier_plan(
    out_dir: Path,
    *,
    data_root: Path | None,
    book_actions: bool = True,
    universe: str = DEFAULT_UNIVERSE,
) -> CapTierPlan:
    """The full window plus every fold's test window, the cap-tier arms and the comparison arms."""
    if universe not in UNIVERSE_CHOICES:
        raise CapTierCampaignError(
            f"unknown universe {universe!r}; one of: {', '.join(UNIVERSE_CHOICES)}"
        )
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
        universe=universe,
    )


# ── running ──────────────────────────────────────────────────────────────────────────────────────


def _contexts(stack: ExitStack, actions: BookActionSource | None) -> None:
    """The fold campaign's contexts: corporate actions in the book and the signal, cash interest."""
    stack.enter_context(corporate_actions_in_force(actions, apply_to_book=True))
    stack.enter_context(accrue_cash_interest(load_repo_rate_schedule()))


def _actions(plan: CapTierPlan) -> BookActionCalendar | None:
    # The plan's lake, not the configured default: from a git worktree the default is the
    # worktree's own (absent) data/, and the store's listing-window read then binds to nothing.
    return load_store_book_actions(data_root=plan.data_root) if plan.book_actions else None


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
            universe_name=plan.universe,
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
                start=window.start,
                end=window.end,
                arms=plan.arms,
                floors=_FLOORS,
                universe_name=plan.universe,
            )
            for (label, floor), digest in digests.items():
                out[(label, window.name, floor)] = digest
    return out


def _strategy_identity(spec: Mapping[str, str]) -> str:
    kept = {k: v for k, v in spec.items() if k not in STORE_SPEC_FIELDS}
    return json.dumps(kept, sort_keys=True, separators=(",", ":"))


def saved_run_digests(
    plan: CapTierPlan, actions: BookActionSource | None
) -> tuple[dict[tuple[str, str, Decimal], str], tuple[str, ...]]:
    """``(arm, window, floor) -> digest`` of the runs on disk, matched by strategy specification.

    Each arm's specification is built as :func:`_digests` builds it, then compared with every saved
    run's with :data:`STORE_SPEC_FIELDS` set aside. Fails loud unless each arm matches exactly one
    saved run. Also returns the distinct ``book_actions`` identities the matched runs recorded, for
    the report to name. Never replays and never writes; a digest it returns is one already on disk.
    """
    saved: dict[str, list[str]] = defaultdict(list)
    for path in sorted((plan.out_dir / "runs").glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        saved[_strategy_identity(document["spec"])].append(document["digest"])
    out: dict[tuple[str, str, Decimal], str] = {}
    stores: set[str] = set()
    with ExitStack() as stack:
        _contexts(stack, actions)
        for window in plan.windows:
            for floor in _FLOORS:
                universe = UniverseParameters.for_universe(
                    plan.universe, median_turnover_floor=floor
                )
                for arm in plan.arms:
                    spec = _arm_spec(
                        arm,
                        start=window.start,
                        end=window.end,
                        universe=universe,
                        opening_cash=_DEFAULT_OPENING_CASH,
                        adjusted=True,
                    )
                    found = saved.get(_strategy_identity(spec), [])
                    if len(found) != 1:
                        raise CapTierCampaignError(
                            f"{arm.label}, {window.name}, floor {floor}: {len(found)} saved runs "
                            f"match its strategy specification under {plan.out_dir}, not one"
                        )
                    out[(arm.label, window.name, floor)] = found[0]
    for digest in out.values():
        loaded = load_run(plan.out_dir, digest)
        if loaded is None:
            raise CapTierCampaignError(f"run {digest[:12]} has a summary but no ledger on disk")
        stores.add(loaded[0].spec.get("book_actions", "none"))
    return out, tuple(sorted(stores))


def recorded_stores(out_dir: Path, digests: Sequence[str]) -> dict[str, Counter[str]]:
    """Per :data:`RECORDED_STORE_FIELDS` field, how many of the runs ``digests`` recorded each
    identity — read from the saved summaries, never from today's store."""
    out: dict[str, Counter[str]] = {field: Counter() for field in RECORDED_STORE_FIELDS}
    for digest in digests:
        spec = json.loads(summary_path(out_dir, digest).read_text(encoding="utf-8"))["spec"]
        for field in RECORDED_STORE_FIELDS:
            out[field][str(spec.get(field, "none"))] += 1
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


def name_contributions(
    ledger: RunLedger, scheme_cash: Sequence[SchemeCashCredit] = ()
) -> dict[str, Decimal]:
    """Each name's profit in rupees: sells + dividends + scheme cash + terminal value - buys, net
    of charges.

    A reissued ISIN is folded into its survivor, so a split that changed the ISIN is one name.
    ``scheme_cash`` is a swap's cash leg the ledger does not record
    (``backtest.run_ledger.unrecorded_scheme_cash``); it is the old name's, folded the same way.
    Cash interest is no name's and is left out.
    """
    key = _canonical(ledger)
    out: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for credit in scheme_cash:
        out[key.get(credit.isin, credit.isin)] += credit.amount
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
class StuckHolding:
    """A name held at a run's end whose last NSE EQ print in the window is before that end."""

    isin: str
    last_print: date
    #: Its last printed close x the shares held: the book's terminal mark, never written down.
    value: Decimal
    #: :data:`MERGER_IN_STORE`, :data:`MERGER_TERMS_UNSOURCED`, or ``None`` when no unconverted
    #: merger is on record for the name.
    merger: str | None


def merger_flags(actions: BookActionSource | None) -> dict[str, str]:
    """Each ISIN with a merger the book leaves unconverted, to the flag the report prints for it.

    Two kinds, both unconverted: a store ``MERGER`` row the curated terms do not cover
    (:data:`MERGER_IN_STORE`), and a scheme the curated table lists as unsourced
    (``MERGER:unsourced``, :data:`MERGER_TERMS_UNSOURCED`), which wins when both are on record. A
    merger with sourced terms converts the holding (PR #41) and so leaves nothing to flag. Reads
    the whole calendar: a name stuck at a window's end may merge after it.
    """
    flags: dict[str, str] = {}
    for action in actions.between(None, date.max) if actions is not None else ():
        if not isinstance(action, UnmodelledAction):
            continue
        if action.action_type == "MERGER:unsourced":
            flags[action.isin] = MERGER_TERMS_UNSOURCED
        elif action.action_type == "MERGER":
            flags.setdefault(action.isin, MERGER_IN_STORE)
    return flags


def stuck_holdings(
    values: Mapping[str, Decimal],
    *,
    last_prints: Mapping[str, date],
    terminal_date: date,
    mergers: Mapping[str, str],
) -> tuple[StuckHolding, ...]:
    """The holdings in ``values`` whose last print is before ``terminal_date``, by ISIN.

    ``last_prints`` must be bounded by the run's window (:meth:`backtest.run._L1Reader.last_prints`
    at the window's end): a lake-wide last print hides a name that stopped inside the window and
    printed again after it. A held name with no print at all fails loud — it cannot have been
    bought — rather than being passed as live.
    """
    out: list[StuckHolding] = []
    for isin in sorted(values):
        last = last_prints.get(isin)
        if last is None:
            raise CapTierCampaignError(f"{isin} held at {terminal_date} with no print before it")
        if last < terminal_date:
            out.append(StuckHolding(isin, last, values[isin], mergers.get(isin)))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class RunRow:
    arm: str
    summary: RunSummary
    path: PathFigures
    after_tax: Decimal | None
    after_tax_error: str | None
    trades: int
    end_cash_share: Decimal
    stuck: tuple[StuckHolding, ...]
    contributions: Mapping[str, Decimal]
    crash: tuple[Decimal, Decimal] | None  # return, max drawdown inside CRASH_WINDOW
    idle: IdleCash
    buy_free: BuyFreeSpan | None
    from_first_buy: tuple[date, Decimal] | None  # first populated session, XIRR from it


def decision_sessions(arm: Arm, sessions: Sequence[date]) -> tuple[date, ...]:
    """The sessions ``arm``'s policy decides on in a window's ``sessions``, as its runner sets them.

    A swing arm decides every ``rebalance_interval_sessions``-th session counted from the first
    (``backtest.run._L1SwingData``); the momentum baselines on the first session of each month.
    Momentum v2's next-session redeploy is not a decision of its own here: its buys credit the
    month's decision, which is all the buy-free span needs.
    """
    if arm.swing is not None:
        return tuple(sessions[:: arm.swing.rebalance_interval_sessions])
    return tuple(_first_session_of_each_month(sessions))


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
    arm_spec: Arm,
    digest: str,
    *,
    fmv: GrandfatheringPrices,
    last_prints: Mapping[str, date],
    mergers: Mapping[str, str],
    terms: MergerTerms,
) -> RunRow:
    """One run's row, from its saved summary, ledger and NAV.

    Decision sessions are the saved NAV's dates (the runner samples every session of the window).
    Cash is rebuilt from the ledger plus the scheme cash it does not record, and must land on the
    run's own terminal cash — terminal NAV less the holdings at their terminal marks — to the
    paisa, or the row is refused. Reads no store; ``last_prints`` and ``fmv`` are the lake's.
    """
    arm = arm_spec.label
    loaded = load_run(out_dir, digest)
    if loaded is None:
        raise CapTierCampaignError(f"{arm}: run {digest[:12]} is not on disk — run it first")
    summary, ledger = loaded
    policy = ratified_backtest_rail_policy()
    if summary.spec.get("rail_policy") != policy.digest():
        raise CapTierCampaignError(
            f"{arm}: run {digest[:12]} ran under rail policy {summary.spec.get('rail_policy')}, "
            f"not {policy.label}; its per-order ceiling cannot be measured against the ratified one"
        )
    nav = read_nav(nav_file(out_dir, digest), digest=digest).points
    try:
        taxed = compute_after_tax(ledger, PROFILE, schedule=load_tax_schedule(), fmv=fmv)
        after_tax, error = taxed.after_tax_xirr_realised, taxed.realised_xirr_error
    except TaxError as failure:
        after_tax, error = None, str(failure)
    held = _replayed_quantities(ledger)
    values = {isin: ledger.terminal_prices[isin] * qty for isin, qty in held.items()}
    cash = ledger.terminal_nav - sum(values.values(), _ZERO)
    stuck = stuck_holdings(
        values, last_prints=last_prints, terminal_date=ledger.terminal_date, mergers=mergers
    )
    scheme_cash = unrecorded_scheme_cash(ledger, terms)
    idle = idle_cash(
        ledger, nav, rails=policy.rails, credits=[(c.received, c.amount) for c in scheme_cash]
    )
    if idle.end_cash != cash:
        raise CapTierCampaignError(
            f"{arm}: run {digest[:12]}: cash rebuilt from the saved ledger ends at "
            f"{idle.end_cash}, but its terminal NAV less holdings is {cash} — the ledger does not "
            "account for all of the run's cash, so no cash figure from it is printed"
        )
    decisions = decision_sessions(arm_spec, [when for when, _ in nav])
    return RunRow(
        arm=arm,
        summary=summary,
        path=path_figures(nav),
        after_tax=after_tax,
        after_tax_error=error,
        trades=len(ledger.trades),
        end_cash_share=cash / ledger.terminal_nav if ledger.terminal_nav else _ZERO,
        stuck=stuck,
        contributions=name_contributions(ledger, scheme_cash),
        crash=_crash(nav),
        idle=idle,
        buy_free=longest_buy_free_span(
            decisions, [t.trade_date for t in ledger.trades if t.side is Side.BUY]
        ),
        from_first_buy=xirr_from_first_buy(ledger, nav),
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


def _header(
    plan: CapTierPlan,
    *,
    commit: str,
    stores: Sequence[str] | None,
    rendered_at: str | None = None,
    recorded: Mapping[str, Mapping[str, int]] | None = None,
    today_store: str | None = None,
    flags_evaluated: bool = True,
) -> list[str]:
    """The report's opening: provenance, investor, and how each column is measured.

    ``commit`` is the commit the runs were made at, ``rendered_at`` the one rendering them when it
    differs. ``stores`` is the corporate-action identities the runs recorded when they were found
    by :func:`saved_run_digests`, ``None`` when they were found by digest; ``recorded`` the count
    of runs per identity (:func:`recorded_stores`) and ``today_store`` today's. When the merger
    flags could not be read at the runs' own store (``flags_evaluated`` false) the header says so.
    """
    rendered = f" (rendered at `{rendered_at}`)" if rendered_at and rendered_at != commit else ""
    return [
        "# Cap-tier strategies vs the current strategies (X2, 2026-10-05)",
        "",
        f"- Runs: `{plan.out_dir}`, made at commit `{commit}`{rendered}, lake `{plan.data_root}`.",
        f"- {universe_line(plan.universe)}.",
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
        "- A name that stops printing is valued at its last printed close until the end, never "
        "written down. A merger or cash exit with sourced terms "
        "(`dataplatform.corpactions.merger_terms`, PR #41) converts the holding into the "
        "survivor's shares or the exit cash on its date; a merger with no sourced terms is not "
        "converted. *Stuck at end*: held at the run's end with the last NSE EQ print before the "
        "window's end, each flagged '" + MERGER_IN_STORE + "' or '" + MERGER_TERMS_UNSOURCED + "' "
        "when such a merger is on record; listed in the last section.",
        "- **Idle cash** (each window's second table) is rebuilt from the saved ledger and NAV "
        "(`backtest.idle_cash`). It is the mean over NAV sessions of cash / NAV, split into "
        "*waiting proceeds* (sale cash since the last buy session; a buy never spends its own "
        "session's sale proceeds), *ceiling leftover* (the rest, after a buy session where at "
        "least one buy hit the per-order ceiling min(₹1.2 L, 15 % of NAV)) and *other leftover*. "
        "The three add up to the mean cash. *Buys at ceiling*: buys within one share of that "
        "ceiling.",
        f"- **Empty-tier flag**: an arm with no buy on more than {EMPTY_TIER_DECISIONS} "
        "consecutive decision sessions (one trading year at the swing cadence; longer than any "
        "regime-gate stand-aside in these runs). The ledger cannot see candidates, so buys stand "
        "in for them. *XIRR from first buy* is **informational only**: measured from the "
        "decision session before the first buy. It does not replace the pre-tax XIRR headline, "
        "because the investor's money was in the run from the window's start.",
        "- **Every strategy figure comes from the saved run** (summary, fill ledger, NAV): XIRR, "
        "drawdowns, worst year, trades, costs, rail blocks, end cash, idle cash, decision "
        "sessions (the NAV's dates), contributions and the crash. Two columns need the lake and "
        "read today's L1 raw prices, which no corporate action changes: the last NSE EQ print "
        "behind *stuck at end*, and the 31-01-2018 FMV behind after-tax XIRR. Benchmarks are "
        "read from the lake's published TRIs.",
        "- A share swap's cash leg (Cairn India's four ₹10 Vedanta preference shares per share, "
        "2017-04-27) is cash the book credited but the saved ledger does not record. It is "
        "rebuilt from the ledger's own swap and the curated terms "
        "(`backtest.run_ledger.unrecorded_scheme_cash`), and every run's rebuilt cash must land "
        "on its saved terminal cash to the paisa. It counts in idle cash and in the old name's "
        "profit; it is not taxed (a known gap, `backtest.book_actions`).",
        *(
            _store_lines(stores, recorded, today_store, flags_evaluated)
            if stores is not None
            else []
        ),
        *(
            []
            if flags_evaluated
            else [
                "- **Merger flags not evaluated**: the store the flags are read from has moved "
                "since the runs (today "
                f"`{today_store}`), and the runs' store cannot be rebuilt, so *stuck at end* "
                "counts the holdings without saying which an unconverted merger explains."
            ]
        ),
        "",
    ]


def _store_lines(
    stores: Sequence[str],
    recorded: Mapping[str, Mapping[str, int]] | None,
    today_store: str | None,
    flags_evaluated: bool,
) -> list[str]:
    """The header lines for runs found by :func:`saved_run_digests`: how, and at which stores."""
    set_aside = ", ".join(sorted(STORE_SPEC_FIELDS))
    lines = [
        "- **Saved runs matched by strategy specification with store fields set aside** ("
        f"{set_aside}): each arm x window x floor matched exactly one run on disk. The runs "
        "recorded corporate actions " + "; ".join(f"`{s}`" for s in stores) + "."
    ]
    for field, counts in (recorded or {}).items():
        if len(counts) > 1:
            lines.append(
                f"- The saved runs span {len(counts)} `{field}` identities: "
                + "; ".join(f"`{identity}` ({n} runs)" for identity, n in sorted(counts.items()))
                + "."
            )
    if today_store is not None:
        same = "the same as" if flags_evaluated else "not"
        lines.append(f"- Today's store is `{today_store}`, {same} the runs'.")
    return lines


def render(
    plan: CapTierPlan,
    *,
    commit: str,
    match_saved_runs: bool = False,
    rendered_at: str | None = None,
) -> str:
    """The report over the runs on disk. Replays nothing and writes nothing.

    Every strategy figure is the saved run's (:func:`_row`). Today's store is read for two things
    only: to find the runs by digest (or, with ``match_saved_runs``, to build each arm's strategy
    specification) and for the merger flags — which are printed only when the runs recorded
    today's store identity, since a moved store would flag holdings the runs' store did not.
    """
    actions = _actions(plan)
    stores: tuple[str, ...] | None = None
    if match_saved_runs:
        digests, stores = saved_run_digests(plan, actions)
    else:
        digests = _digests(plan, actions)
    recorded = recorded_stores(plan.out_dir, sorted(set(digests.values())))
    today_store = _actions_identity(actions)
    flags_evaluated = set(recorded["book_actions"]) == {today_store}
    mergers = merger_flags(actions) if flags_evaluated else {}
    reader = _L1Reader(data_root=plan.data_root)
    try:
        last_prints = {w.name: reader.last_prints(w.end) for w in plan.windows}
    finally:
        reader.close()
    _service, fmv = l1_grandfathering(plan.data_root)
    terms = load_merger_terms()
    lines = _header(
        plan,
        commit=commit,
        stores=stores,
        rendered_at=rendered_at,
        recorded=recorded,
        today_store=today_store,
        flags_evaluated=flags_evaluated,
    )
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
                    arm,
                    digest,
                    fmv=fmv,
                    last_prints=last_prints[window.name],
                    mergers=mergers,
                    terms=terms,
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
                    f"| {_stuck_cell(row.stuck, flags_evaluated)} |"
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
            lines += _idle_cash_table([rows[(window.name, floor, a.label)] for a in plan.arms])
    lines += _empty_tier_section(rows, plan)
    lines += _smallcap_section(rows, plan)
    lines += _stuck_section(rows, plan)
    return "\n".join(lines) + "\n"


def _stuck_cell(stuck: Sequence[StuckHolding], flags_evaluated: bool = True) -> str:
    if not flags_evaluated:
        return f"{len(stuck)} (merger flags not evaluated)"
    in_store = sum(1 for x in stuck if x.merger == MERGER_IN_STORE)
    unsourced = sum(1 for x in stuck if x.merger == MERGER_TERMS_UNSOURCED)
    return f"{len(stuck)} ({in_store} merger in store, {unsourced} merger terms unsourced)"


def _span(span: BuyFreeSpan | None) -> str:
    if span is None:
        return "0"
    end = f"next buy {span.next_buy}" if span.next_buy else "no buy to the end"
    flag = " **EMPTY TIER**" if span.flagged else ""
    return f"{span.decisions} from {span.start} ({end}){flag}"


def _first_buy(row: RunRow) -> str:
    if row.from_first_buy is None:
        return "n/a"
    start, rate = row.from_first_buy
    return f"{_pct(rate)} from {start}"


def _idle_cash_table(rows: Sequence[RunRow]) -> list[str]:
    lines = [
        "Idle cash by cause (mean over NAV sessions, share of NAV):",
        "",
        "| strategy | mean cash | waiting proceeds | ceiling leftover | other leftover "
        "| waiting share of cash | buys at ceiling | longest buy-free span (decision sessions) "
        "| XIRR from first buy (info only) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        idle = row.idle
        lines.append(
            f"| {row.arm} | {_pct(idle.mean_cash_share)} | {_pct(idle.mean_waiting_share)} "
            f"| {_pct(idle.mean_ceiling_share)} | {_pct(idle.mean_other_share)} "
            f"| {_pct(idle.waiting_share_of_cash)} | {idle.ceiling_buys} of {idle.buys} "
            f"| {_span(row.buy_free)} | {_first_buy(row)} |"
        )
    lines.append("")
    return lines


def _empty_tier_section(
    rows: Mapping[tuple[str, Decimal, str], RunRow], plan: CapTierPlan
) -> list[str]:
    lines = [
        f"## Empty tiers: no buy on more than {EMPTY_TIER_DECISIONS} consecutive decision sessions",
        "",
    ]
    flagged = [
        (window, floor, arm.label, row, span)
        for window in plan.windows
        for floor in _FLOORS
        for arm in plan.arms
        if (span := (row := rows[(window.name, floor, arm.label)]).buy_free) is not None
        and span.flagged
    ]
    if not flagged:
        return [*lines, "None.", ""]
    for window, floor, label, row, span in flagged:
        lines.append(
            f"- {window.name}, {_floor(floor).split(' (')[0]}, {label}: no buy on {span.decisions} "
            f"decision sessions from {span.start}"
            + (f" to the first buy on {span.next_buy}" if span.next_buy else " to the end")
            + f". Mean cash {_pct(row.idle.mean_cash_share)} of NAV. Headline pre-tax XIRR "
            f"{_pct(row.summary.xirr)}; XIRR from first buy (informational, not the headline) "
            f"{_first_buy(row)}."
        )
    lines.append("")
    return lines


def _smallcap_section(
    rows: Mapping[tuple[str, Decimal, str], RunRow], plan: CapTierPlan
) -> list[str]:
    lines = [
        "## Focused smallcap: concentration and the 2018-2020 crash",
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
    lines.append("")
    return lines


def _stuck_section(rows: Mapping[tuple[str, Decimal, str], RunRow], plan: CapTierPlan) -> list[str]:
    lines = [
        "## Holdings stuck in names that stopped printing before the window's end",
        "",
        "Every window, at its end: held, with the last NSE EQ print before the window's last "
        "session, valued at that print's close.",
        "",
    ]
    for window in plan.windows:
        for floor in _FLOORS:
            for label in (
                FOCUSED_SMALLCAP,
                *(a.label for a in plan.arms if a.label != FOCUSED_SMALLCAP),
            ):
                row = rows[(window.name, floor, label)]
                if not row.stuck:
                    continue
                stuck_value = sum((x.value for x in row.stuck), _ZERO)
                share = stuck_value / row.summary.final_nav if row.summary.final_nav else _ZERO
                detail = "; ".join(
                    f"{x.isin} last print {x.last_print}, valued {_lakh(x.value)}"
                    + (f" ({x.merger})" if x.merger else "")
                    for x in row.stuck
                )
                lines.append(
                    f"- {window.name}, {_floor(floor).split(' (')[0]}, {label}: "
                    f"{len(row.stuck)} names, {_lakh(stuck_value)} = {share:.2%} of the final "
                    f"NAV — {detail}"
                )
    if len(lines) == 5:
        lines.append("None.")
    lines.append("")
    return lines


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cap-tier-campaign", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("run", "render"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--universe",
        choices=UNIVERSE_CHOICES,
        default=DEFAULT_UNIVERSE,
        help="investable universe: nifty500 (point-in-time NIFTY 500 membership, the default) or "
        "turnover_floor (every NSE EQ name above the floor, no index screen)",
    )
    parser.add_argument(
        "--match-saved-runs",
        action="store_true",
        help="render: find runs by strategy spec, store-derived fields set aside",
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        default=None,
        help="render: write cap-tiers.md under this directory instead of OUT/reports",
    )
    args = parser.parse_args(argv)
    data_root = args.data_root.resolve()
    out_dir = refuse_lake_location(args.out.resolve(), data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = cap_tier_plan(out_dir, data_root=data_root, universe=args.universe)
    commit = _git_commit()
    if args.command == "run":
        manifest = out_dir / "manifest.json"
        document = {
            "commit": commit,
            "data_root": str(data_root),
            "windows": [[w.name, w.start.isoformat(), w.end.isoformat()] for w in plan.windows],
            "arms": [a.label for a in plan.arms],
            "floors": [str(f) for f in _FLOORS],
            "universe": plan.universe,
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
    manifest = out_dir / "manifest.json"
    run_commit = (
        str(json.loads(manifest.read_text(encoding="utf-8"))["commit"])
        if manifest.is_file()
        else commit
    )
    report = render(
        plan, commit=run_commit, match_saved_runs=args.match_saved_runs, rendered_at=commit
    )
    report_dir = (
        out_dir / "reports"
        if args.report_out is None
        else refuse_lake_location(args.report_out.resolve(), data_root)
    )
    path = report_dir / "cap-tiers.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
