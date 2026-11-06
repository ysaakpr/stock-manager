"""M14.9 — render the early-entry gate report from the saved runs; replays nothing.

Reads the run directories ``backtest.m12_rerun --arm-set early-entry`` wrote and renders
``ops/gates/M14.9-momentum-turn-entry-report.md``: one table per window, floor and universe with
D13 in each, the generated scorecard (``backtest.m14_5_report.scorecard``), the pre-registered
decision rule evaluated in code, and the "this time" readout on the live lake.

**Parks and re-entries** are not journaled in the saved files, so they are re-derived here with
the policy's own trigger predicates (:mod:`backtest.policies.early_entry`) over the published
NIFTY 50 TRI and, for V4, a breadth series struck by the same :class:`~backtest.breadth.
BreadthSeries` the runs read, then checked against the fill ledger: an early re-entry is
*confirmed* when the run bought on the following session. A replay that disagreed with the
policy would show as unconfirmed re-entries rather than pass silently. The breadth series here
spans every window of a (universe, floor) at once, so its pass opens earlier than a single run's;
only a name with fewer than 200 prints in the 400 sessions before a window could differ.

**Reversals.** An early re-entry *reversed within a month* (pre-registration §6) counts two ways:
(a) the first monthly rebalance after it read risk-off and parked the book again; (b) the TRI
closed below its MA200 on any of the 21 sessions after the re-entry decision.

Everything below the hand-written marker in an existing report is kept on re-render.

What it never does: replay a run, read a wall clock into the report, or average across windows.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from backtest.breadth import BreadthSeries
from backtest.m12_rerun import EARLY_ENTRY_SET, SECTOR_ORDER_SET, WINDOWS
from backtest.m14_5_report import (
    _FLOOR_RE,
    _INDEX_RE,
    _WINDOW_ORDER,
    BAR,
    MARKER,
    RunFacts,
    Switches,
    _floor_label,
    _lakh,
    _p,
    _pp,
    _r,
    _window_of,
    scorecard,
)
from backtest.policies.early_entry import (
    BreadthReading,
    EarlyEntry,
    RegimeTrendReading,
    breadth_turn,
    confirmed_cross,
    momentum_turn,
)
from backtest.policies.momentum_v2 import MomentumV2Parameters, RegimeReading
from backtest.run import (
    UniverseParameters,
    _AdjustedCloseSource,
    _first_session_of_each_month,
    _InvestableUniverse,
    _L1Reader,
    _RegimeSource,
)
from backtest.sweep import D13_PAPER_BASELINE, HIGH_FLOOR, LOW_FLOOR, Arm
from dataplatform.query import QueryService

__all__ = [
    "EntryFacts",
    "EntrySwitches",
    "Readings",
    "decision",
    "entry_switches",
    "main",
    "render",
    "this_time",
]

REPORT = Path("ops/gates/M14.9-momentum-turn-entry-report.md")
#: Every arm the report reads: D13, V1-V4 and (pre-registration §8) the two S arms.
ALL_ARMS: tuple[Arm, ...] = (*EARLY_ENTRY_SET, *SECTOR_ORDER_SET[1:])
#: The two families, each with its own walk-forward (pre-registration §5 and §8).
FAMILIES: dict[str, tuple[Arm, ...]] = {
    "early-entry": EARLY_ENTRY_SET,
    "sector-order": SECTOR_ORDER_SET,
}
_ZERO = Decimal("0")
#: Pre-registration §6(b): a re-entry reversed when the TRI closes below MA200 within a month.
REVERSAL_SESSIONS = 21
#: Pre-registration §5: how much deeper than D13's a recommended variant's drawdown may be.
DD_TOLERANCE = Decimal("0.02")
#: The live readout window (pre-registration §6): D13's October and November parks.
THIS_TIME_FROM = date(2026, 10, 1)


@dataclass(frozen=True, slots=True)
class EntrySwitches:
    """Parks and re-entries the policy's own rule produces over a window (one arm, one cell).

    ``parks`` counts entries into the parked state (monthly, risk-off); ``monthly_reentries`` the
    risk-on rebalances that unpark; ``early`` the variant's own re-entries off a rebalance session,
    ``early_confirmed`` those the fill ledger confirms (a purchase on the following session);
    ``reparked`` the early re-entries whose next rebalance parked the book again, and
    ``index_reversed`` those after which the TRI closed below MA200 within 21 sessions.
    ``park_sessions`` counts every session a park was issued (each risk-off rebalance).
    """

    parks: int
    monthly_reentries: int
    early: int
    early_confirmed: int
    reparked: int
    index_reversed: int
    park_sessions: int
    early_sessions: tuple[date, ...] = ()


@dataclass(frozen=True, slots=True)
class Readings:
    """What the triggers read, per session: the regime, its trend, and (V4) breadth."""

    regime: Callable[[date], RegimeReading]
    trend: Callable[[date], RegimeTrendReading]
    breadth: Callable[[date], BreadthReading] | None


def _fires(mode: EarlyEntry, session: date, readings: Readings) -> bool:
    """Whether ``mode``'s trigger fires on ``session`` — the policy's own predicate."""
    if mode is EarlyEntry.BREADTH_TURN:
        if readings.breadth is None:
            raise ValueError("the breadth trigger needs a breadth series")
        return breadth_turn(readings.breadth(session))
    if mode is EarlyEntry.SCALE_IN:
        return readings.regime(session).risk_on
    trend = readings.trend(session)
    return confirmed_cross(trend) if mode is EarlyEntry.CONFIRMED_CROSS else momentum_turn(trend)


def entry_switches(
    params: MomentumV2Parameters,
    sessions: Sequence[date],
    readings: Readings,
    *,
    bought: set[date],
) -> EntrySwitches:
    """Replay the policy's parked state over ``sessions`` with its own rules; count switches.

    Mirrors ``MomentumV2Policy.decide``: a rebalance session parks on risk-off and unparks on
    risk-on (D13); off a rebalance a parked book re-enters when ``params.early_entry`` fires. The
    triggers are read only on parked non-rebalance sessions, as the policy reads them.
    """
    rebalances = _first_session_of_each_month(sessions)
    rebalance_set = set(rebalances)
    mode = params.early_entry
    parked = False
    parks = monthly = park_sessions = 0
    early: list[date] = []
    for session in sessions:
        if session in rebalance_set:
            if not readings.regime(session).risk_on:
                park_sessions += 1
                if not parked:
                    parks += 1
                parked = True
            else:
                if parked:
                    monthly += 1
                parked = False
            continue
        if parked and mode is not None and _fires(mode, session, readings):
            early.append(session)
            parked = False
    following = dict(pairwise(sessions))
    index = {s: i for i, s in enumerate(sessions)}
    reparked = reversed_ = 0
    for session in early:
        later = [r for r in rebalances if r > session]
        if later and not readings.regime(later[0]).risk_on:
            reparked += 1
        after = sessions[index[session] + 1 : index[session] + 1 + REVERSAL_SESSIONS]
        if any(not readings.regime(s).risk_on for s in after):
            reversed_ += 1
    return EntrySwitches(
        parks=parks,
        monthly_reentries=monthly,
        early=len(early),
        early_confirmed=sum(1 for s in early if following.get(s) in bought),
        reparked=reparked,
        index_reversed=reversed_,
        park_sessions=park_sessions,
        early_sessions=tuple(early),
    )


@dataclass(frozen=True, slots=True)
class EntryFacts:
    """One saved run: the M14.5 facts (for the shared scorecard) plus its early-entry switches."""

    base: RunFacts
    switches: EntrySwitches
    #: A8 MAX_SECTOR refusals over the run, and the risk-on rebalances it had (pre-reg §8).
    sector_refusals: int = 0
    risk_on_rebalances: int = 0
    #: The book's cash share at the close of the second session after each risk-on rebalance.
    idle_mean: Decimal = _ZERO
    idle_max: Decimal = _ZERO


def idle_cash_after_rebalances(
    ledger: Mapping[str, Any],
    nav: Mapping[date, Decimal],
    sessions: Sequence[date],
    risk_on_rebalances: Sequence[date],
) -> list[Decimal]:
    """Cash ÷ NAV at the close of the second session after each risk-on rebalance (§8).

    Cash is rebuilt from the fill ledger: the opening deposit (an external flow, negative from the
    investor's side), every sale's net proceeds less every purchase's net cost, dividends and
    interest, each on its own date. The second session after a rebalance R is R+2: R's orders
    fill on R+1 and the next-session redeploy on R+2.
    """
    events: list[tuple[date, Decimal]] = []
    for flow in ledger["external_flows"]:
        events.append((date.fromisoformat(flow["date"]), -Decimal(flow["amount"])))
    for trade in ledger["trades"]:
        amount = Decimal(trade["net_amount"])
        events.append(
            (
                date.fromisoformat(trade["trade_date"]),
                amount if trade["side"] == "SELL" else -amount,
            )
        )
    for item in (*ledger["dividends"], *ledger["interest"]):
        events.append((date.fromisoformat(item["received"]), Decimal(item["amount"])))
    events.sort()
    index = {s: i for i, s in enumerate(sessions)}
    shares: list[Decimal] = []
    for rebalance in risk_on_rebalances:
        at = index[rebalance] + 2
        if at >= len(sessions) or sessions[at] not in nav:
            continue
        close = sessions[at]
        cash = sum((amount for when, amount in events if when <= close), _ZERO)
        if nav[close] > _ZERO:
            shares.append(cash / nav[close])
    return shares


# ── reading the lake ─────────────────────────────────────────────────────────────────────────────


class _Lake:
    """The calendar, the regime source and one breadth series per (universe, floor), on demand."""

    def __init__(self, data_root: Path | None) -> None:
        self._data_root = data_root
        self.reader = _L1Reader(data_root=data_root)
        self.calendar = self.reader.all_sessions()
        self.regime_source = _RegimeSource.published(
            through=self.calendar[-1], ma_days=200, data_root=data_root
        )
        self._service: QueryService | None = None
        self._breadth: dict[tuple[str, Decimal], BreadthSeries] = {}
        self._regime: dict[date, RegimeReading] = {}
        self._trend: dict[date, RegimeTrendReading] = {}

    def regime(self, session: date) -> RegimeReading:
        if session not in self._regime:
            self._regime[session] = self.regime_source.reading(session)
        return self._regime[session]

    def trend(self, session: date) -> RegimeTrendReading:
        if session not in self._trend:
            self._trend[session] = self.regime_source.trend(session)
        return self._trend[session]

    def breadth(self, universe: str, floor: Decimal, first: date) -> BreadthSeries:
        """The breadth series of ``universe`` at ``floor`` from ``first`` to the lake's end."""
        key = (universe, floor)
        if key not in self._breadth:
            if self._service is None:
                self._service = QueryService(data_root=self._data_root)
            closes = _AdjustedCloseSource(self._service, self.reader)
            screen = _InvestableUniverse(
                self.reader,
                UniverseParameters.for_universe(universe, median_turnover_floor=floor),
                data_root=self._data_root,
            )
            start = next(s for s in self.calendar if s >= first)
            self._breadth[key] = BreadthSeries(
                calendar=self.calendar,
                first=start,
                last=self.calendar[-1],
                closes=closes.bulk(self.calendar),
                universe=lambda r: frozenset(screen.constrain(r, self.reader.closes_on(r))),
            )
        return self._breadth[key]

    def readings(self, universe: str, floor: Decimal, first: date) -> Readings:
        series = self.breadth(universe, floor, first)
        return Readings(regime=self.regime, trend=self.trend, breadth=series.reading)

    def close(self) -> None:
        self.reader.close()
        if self._service is not None:
            self._service.close()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def collect(run_dir: Path, lake: _Lake, *, arms: Sequence[Arm] = ALL_ARMS) -> list[EntryFacts]:
    """Every momentum-v2 run in ``run_dir`` whose parameters are one of ``arms``."""
    by_repr = {repr(arm.v2): arm for arm in arms}
    facts: list[EntryFacts] = []
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
        floor = Decimal(floor_match.group(1))
        universe = index_match.group(1) if index_match else "turnover_floor"
        digest = summary["digest"]
        ledger = _load(run_dir / "ledgers" / f"{digest}.json")
        nav = _load(run_dir / "navs" / f"{digest}.json")
        trades = ledger["trades"]
        traded = sum((abs(Decimal(t["net_amount"])) for t in trades), _ZERO)
        points = [Decimal(value) for _, value in nav["points"]]
        mean_nav = sum(points, _ZERO) / Decimal(len(points))
        first, last = WINDOWS[window]
        years = Decimal((last - first).days) / Decimal("365.25")
        sessions = [s for s in lake.calendar if first <= s <= last]
        # One series per (universe, floor) for every window: from the decade's start for the
        # floor-only screen, from the six-year window's for NIFTY 500 (its PIT history opens later).
        earliest = WINDOWS["decade" if universe == "turnover_floor" else "six-year"][0]
        readings = (
            lake.readings(universe, floor, earliest)
            if arm.v2.early_entry is EarlyEntry.BREADTH_TURN
            else Readings(regime=lake.regime, trend=lake.trend, breadth=None)
        )
        bought = {date.fromisoformat(t["trade_date"]) for t in trades if t["side"] == "BUY"}
        sold = {date.fromisoformat(t["trade_date"]) for t in trades if t["side"] == "SELL"}
        flips = entry_switches(arm.v2, sessions, readings, bought=bought)
        following = dict(pairwise(sessions))
        rebalances = _first_session_of_each_month(sessions)
        park_days = [r for r in rebalances if not lake.regime(r).risk_on]
        base = RunFacts(
            window=window,
            universe=universe,
            floor=floor,
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
            floor_refusals=int(summary["rail_blocks"].get("MIN_HOLDINGS", 0)),
            switches=Switches(
                parks=flips.parks,
                reentries=flips.monthly_reentries + flips.early,
                parks_confirmed=sum(1 for d in park_days if following.get(d) in sold),
                reentries_confirmed=flips.early_confirmed,
                park_sessions=flips.park_sessions,
            ),
        )
        risk_on = [r for r in rebalances if lake.regime(r).risk_on]
        idle = idle_cash_after_rebalances(
            ledger,
            {date.fromisoformat(d): Decimal(v) for d, v in nav["points"]},
            sessions,
            risk_on,
        )
        facts.append(
            EntryFacts(
                base=base,
                switches=flips,
                sector_refusals=int(summary["rail_blocks"].get("MAX_SECTOR", 0)),
                risk_on_rebalances=len(risk_on),
                idle_mean=sum(idle, _ZERO) / Decimal(len(idle)) if idle else _ZERO,
                idle_max=max(idle, default=_ZERO),
            )
        )
    return facts


def sector_table(facts: Sequence[EntryFacts]) -> list[str]:
    """How often A8's sector cap bound, and the cash left idle after rebalances (§8)."""
    labels = [arm.label for arm in SECTOR_ORDER_SET]
    rows = [f for f in facts if f.base.label in labels]
    if not rows:
        return []
    out = [
        "## Sector cap and idle cash (generated, pre-registration §8)",
        "",
        "**MAX_SECTOR refusals** are every buy A8's 35% sector rail refused over the run (run "
        "summary). **Idle cash** is the book's cash share at the close of the second session "
        "after each risk-on rebalance (after the rebalance's fills and the next-session "
        "redeploy), rebuilt from the fill ledger; mean and worst over the window.",
        "",
        "| Universe | Window | Floor | Strategy | MAX_SECTOR refusals | per risk-on rebalance "
        "| Idle cash after rebalances: mean | worst | XIRR/DD |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    order = {label: i for i, label in enumerate(labels)}
    rows.sort(
        key=lambda f: (
            f.base.universe != "turnover_floor",
            _WINDOW_ORDER.index(f.base.window),
            f.base.floor,
            order[f.base.label],
        )
    )
    for f in rows:
        per = (
            Decimal(f.sector_refusals) / Decimal(f.risk_on_rebalances)
            if f.risk_on_rebalances
            else _ZERO
        )
        out.append(
            f"| {f.base.universe} | {f.base.window} | {_floor_label(f.base.floor)} "
            f"| {f.base.label} | {f.sector_refusals} | {_r(per)} | {_p(f.idle_mean)} "
            f"| {_p(f.idle_max)} | {_r(f.base.ratio)} |"
        )
    out.append("")
    return out


# ── the decision rule (pre-registration §5), evaluated in code ───────────────────────────────────


def decision(
    facts: Sequence[EntryFacts],
    selected: str,
    *,
    family: Sequence[Arm] = EARLY_ENTRY_SET,
    title: str = "Decision rule (pre-registered §5, evaluated in code)",
) -> list[str]:
    """The pre-registered decision rule for one family over the floor-only cells."""
    floor_only = [f.base for f in facts if f.base.universe == "turnover_floor"]
    d13 = {(f.window, f.floor): f for f in floor_only if f.label == D13_PAPER_BASELINE.label}
    out = [
        f"## {title}",
        "",
        f"1. **Walk-forward choice** (floor-only selection window, XIRR ÷ max DD, ₹1 cr first): "
        f"**{selected or '(none)'}**.",
    ]
    if not selected or selected == D13_PAPER_BASELINE.label:
        out += [
            "2. The walk-forward chose D13, so **no variant is recommended**; D13 stays as "
            "ratified. The conditions below are shown for every variant for the record only.",
            "",
        ]
    else:
        out += [
            "2. The walk-forward chose a variant; it is recommended as a candidate for a separate "
            "paper book only if every condition below holds for it.",
            "",
        ]
    out += [
        "| Variant | Verification XIRR/DD ≥ D13 at both floors | Floor-only cells with XIRR/DD "
        "≥ D13 (of 8; need ≥ 6) | Max DD within 2 pp of D13 in every floor-only cell | All "
        "three |",
        "| --- | --- | --- | --- | --- |",
    ]
    for arm in family[1:]:
        rows = {(f.window, f.floor): f for f in floor_only if f.label == arm.label}
        pairs = [(rows[k], d13[k]) for k in rows if k in d13]
        if not pairs:
            continue
        verification = [
            rows[("wf-verification", floor)].ratio >= d13[("wf-verification", floor)].ratio
            for floor in (LOW_FLOOR, HIGH_FLOOR)
            if ("wf-verification", floor) in rows and ("wf-verification", floor) in d13
        ]
        ver_ok = len(verification) == 2 and all(verification)
        at_least = sum(1 for f, b in pairs if f.ratio >= b.ratio)
        dd_ok = all(f.max_drawdown - b.max_drawdown <= DD_TOLERANCE for f, b in pairs)
        worst = max(f.max_drawdown - b.max_drawdown for f, b in pairs)
        out.append(
            f"| {arm.label} | {'yes' if ver_ok else 'no'} | {at_least} / {len(pairs)} "
            f"| {'yes' if dd_ok else 'no'} (worst {_pp(worst)}) "
            f"| {'yes' if ver_ok and at_least >= 6 and dd_ok else 'no'} |"
        )
    out.append("")
    return out


# ── "this time": the live lake ───────────────────────────────────────────────────────────────────


def this_time(lake: _Lake, breadth_universes: Sequence[tuple[str, Decimal]]) -> list[str]:
    """For each variant, the session it would have re-entered after D13's Oct and Nov parks."""
    sessions = [s for s in lake.calendar if s >= THIS_TIME_FROM]
    rebalances = _first_session_of_each_month(sessions)
    out = [
        "## This time: October to November 2026 on the live lake (generated)",
        "",
        f"Lake through **{lake.calendar[-1].isoformat()}**. Each row replays the rule from D13's "
        "state at the first October session: a risk-off rebalance parks, a risk-on one unparks, "
        "and between them the variant's trigger may re-enter a parked book (the decision is taken "
        "at that session's close and fills on the next session).",
        "",
        "| Session | TRI | MA200 | TRI vs MA200 | Streak >= MA200 | MA50 rising (vs 10 "
        "sessions back) | 20-session return | "
        + " | ".join(f"Breadth {_universe_label(u, f)} (20 back)" for u, f in breadth_universes)
        + " |",
        "| --- | --- | --- | --- | --- | --- | --- | "
        + " | ".join("---" for _ in breadth_universes)
        + " |",
    ]
    series = {key: lake.breadth(key[0], key[1], THIS_TIME_FROM) for key in breadth_universes}
    for s in sessions:
        r = lake.regime(s)
        t = lake.trend(s)
        cells = []
        for key in breadth_universes:
            b = series[key].reading(s)
            cells.append(f"{_p(b.share_above)} ({_p(b.share_above_lagged)})")
        mark = " (rebalance)" if s in rebalances else ""
        ma = r.moving_average.quantize(Decimal("0.01"))
        out.append(
            f"| {s.isoformat()}{mark} | {r.index_level} | {ma} "
            f"| {_pp(r.index_level / r.moving_average - 1).removesuffix('pp')}% "
            f"| {t.sessions_at_or_above} "
            f"| {'yes' if t.short_average > t.short_average_lagged else 'no'} "
            f"({t.short_average.quantize(Decimal('0.1'))} vs "
            f"{t.short_average_lagged.quantize(Decimal('0.1'))}) "
            f"| {_pp(t.index_level / t.level_lagged - 1).removesuffix('pp')}% | "
            + " | ".join(cells)
            + " |"
        )
    out += [
        "",
        "| Variant | Breadth universe | After the 1 Oct park: re-entry decided | Next rebalance "
        "(2 Nov) | After the 2 Nov park: re-entry decided |",
        "| --- | --- | --- | --- | --- |",
    ]
    variants: list[tuple[Arm, tuple[str, Decimal] | None]] = []
    for arm in EARLY_ENTRY_SET:
        assert arm.v2 is not None
        if arm.v2.early_entry is EarlyEntry.BREADTH_TURN:
            variants += [(arm, key) for key in breadth_universes]
        else:
            variants.append((arm, None))
    for arm, chosen in variants:
        assert arm.v2 is not None
        readings = Readings(
            regime=lake.regime,
            trend=lake.trend,
            breadth=series[chosen].reading if chosen is not None else None,
        )
        spells = _spells(arm.v2, sessions, rebalances, readings)
        universe = "—" if chosen is None else _universe_label(*chosen)
        out.append(f"| {arm.label} | {universe} | {' | '.join(spells)} |")
    out.append("")
    return out


def _universe_label(universe: str, floor: Decimal) -> str:
    return f"{universe} ₹{floor / Decimal(10_000_000):g} cr"


def _spells(
    params: MomentumV2Parameters,
    sessions: Sequence[date],
    rebalances: Sequence[date],
    readings: Readings,
) -> list[str]:
    """The first parked spell's re-entry, the next rebalance's verdict, the second spell's."""
    first_rebalance, second = rebalances[0], rebalances[1] if len(rebalances) > 1 else None
    mode = params.early_entry

    def entry(start: date, stop: date | None) -> str:
        if readings.regime(start).risk_on:
            return "not parked (rebalance risk-on)"
        if mode is None:
            return "— (D13 waits for the next rebalance)"
        for s in sessions:
            if s <= start or (stop is not None and s >= stop):
                continue
            if _fires(mode, s, readings):
                following = next((x for x in sessions if x > s), None)
                fill = following.isoformat() if following else "the next session"
                return f"**{s.isoformat()}** (fills {fill})"
        return "not yet" if stop is None else "none before it"

    first_spell = entry(first_rebalance, second)
    if second is None:
        return [first_spell, "—", "—"]
    verdict = "risk-on: invests" if readings.regime(second).risk_on else "risk-off: parks"
    return [first_spell, verdict, entry(second, None)]


# ── rendering ────────────────────────────────────────────────────────────────────────────────────


def _table(rows: list[EntryFacts]) -> list[str]:
    base = next((r for r in rows if r.base.label == D13_PAPER_BASELINE.label), None)
    ranked = sorted(rows, key=lambda r: (-r.base.ratio, r.base.label))
    lines = [
        "| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs "
        "NIFTY 50 TRI | Parks / monthly re-entries | Early re-entries (ledger-confirmed) | "
        "Reversed ≤ 1 month: re-parked next rebalance / TRI < MA200 within 21 sessions | Trades "
        "| One-way turnover /yr | Charges | A8 min-holdings refusals (park sessions) | >25%? |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- "
        "| --- |",
    ]
    for position, row in enumerate(ranked, start=1):
        f, sw = row.base, row.switches
        name = f"**{f.label}**" if row is base else f.label
        delta_x = "—" if base is None or row is base else _pp(f.xirr - base.base.xirr)
        delta_d = (
            "—" if base is None or row is base else _pp(f.max_drawdown - base.base.max_drawdown)
        )
        early = "—" if row is base else f"{sw.early} ({sw.early_confirmed})"
        reversed_ = "—" if row is base else f"{sw.reparked} / {sw.index_reversed}"
        lines.append(
            f"| {position} | {name} | {_p(f.xirr)} | {_p(f.max_drawdown)} | **{_r(f.ratio)}** "
            f"| {delta_x} | {delta_d} | {_p(f.excess)} | {sw.parks} / {sw.monthly_reentries} "
            f"| {early} | {reversed_} | {f.trades} | {f.turnover}x | {_lakh(f.charges)} "
            f"| {f.floor_refusals} ({sw.park_sessions}) | {'yes' if f.xirr > BAR else 'no'} |"
        )
    return lines


def render(
    facts: Sequence[EntryFacts],
    *,
    manifests: Mapping[str, Mapping[str, Any]],
    selected: Mapping[str, str],
    live: Sequence[str],
    hand_written: str,
) -> str:
    """The whole report: provenance, the tables, the scorecard, the decision rule, this time."""
    out = [
        "# M14.9 — momentum v2: early re-entry for a parked book (gate report)",
        "",
        "> **Generated** by `uv run python -m backtest.m14_9_report` from the saved runs listed "
        "below and the live lake; nothing in the tables is typed by hand. The section after the "
        "marker at the end is hand-written analysis and is labelled as such.",
        "",
        "## What was run",
        "",
        "Pre-registered in `ops/studies/preregistration-m14-9-momentum-turn-entry-2026-11-05.md` "
        "before any run. D13 (`PAPER_RATIFIED_2026_09_06`) reads the regime — NIFTY 50 TRI "
        "against its 200-session average — only on the monthly rebalance session. Each variant is "
        "D13 with one switch (`early_entry`) that may re-enter a **parked** book between "
        "rebalances; exits stay D13's monthly rule. Arm S (pre-registration §8) changes only the "
        "order buys reach A8's sector rail, and is listed when its runs are rendered:",
        "",
    ]
    present = {f.base.label for f in facts}
    for arm in ALL_ARMS:
        if arm in EARLY_ENTRY_SET or arm.label in present:
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
        "Columns: **Parks / monthly re-entries** are D13's own switches on rebalance sessions. "
        "**Early re-entries** are the variant's re-entries between rebalances, replayed with the "
        "policy's own trigger predicates (`backtest.m14_9_report.entry_switches`); in brackets, "
        "how many the fill ledger confirms (a purchase on the following session). **Reversed ≤ 1 "
        "month** counts early re-entries whose next rebalance read risk-off and parked the book "
        "again, then those after which the TRI closed below its MA200 within 21 sessions. "
        "**One-way turnover** is (buys + sells) ÷ 2 ÷ mean NAV ÷ years. **Charges** are every "
        "brokerage, STT, stamp, exchange and GST rupee the shared cost model charged (₹10L opening "
        "book). **A8 min-holdings refusals** are all sells A8's 8-name floor refused over the "
        "run; in brackets, the sessions a park was issued (at most 8 refusals each).",
        "",
    ]
    universes = sorted({f.base.universe for f in facts}, key=lambda u: (u != "turnover_floor", u))
    for universe in universes:
        title = "floor-only universe" if universe == "turnover_floor" else f"{universe} universe"
        out += [f"## Results — {title}", ""]
        for window in _WINDOW_ORDER:
            cell = [f for f in facts if f.base.window == window and f.base.universe == universe]
            if not cell:
                continue
            first, last = WINDOWS[window]
            out += [f"### {window} ({first.isoformat()} → {last.isoformat()})", ""]
            for floor in (LOW_FLOOR, HIGH_FLOOR):
                rows = [f for f in cell if f.base.floor == floor]
                if rows:
                    out += [f"**{_floor_label(floor)}**", "", *_table(rows), ""]
        chosen = {fam: selected.get(f"{universe}/{fam}", "") for fam in FAMILIES}
        for fam, pick in chosen.items():
            if pick:
                out += [
                    f"**Walk-forward choice ({title}, {fam} family)**: on the selection window "
                    f"the code (`backtest.verdict.run_walk_forward`, ₹1 cr floor first) chose "
                    f"**{pick}**; its verification-window row is in the table above, read after "
                    "the choice was frozen.",
                    "",
                ]
        if not any(chosen.values()) and any(
            f.base.universe == universe and f.base.window == "wf-verification" for f in facts
        ):
            out += [
                f"**Walk-forward ({title})**: no selection window — the PIT NIFTY 500 membership "
                "history opens 2016-10-24, after the selection window opens. These rows are "
                "attribution only; the walk-forward choice on record is the floor-only one.",
                "",
            ]
    out += scorecard([f.base for f in facts], ALL_ARMS)
    out += decision(facts, selected.get("turnover_floor/early-entry", ""))
    if any(f.base.label in {a.label for a in SECTOR_ORDER_SET[1:]} for f in facts):
        out += sector_table(facts)
        out += decision(
            facts,
            selected.get("turnover_floor/sector-order", ""),
            family=SECTOR_ORDER_SET,
            title="Decision rule for arm S (pre-registered §8, evaluated in code)",
        )
    out += list(live)
    out += [MARKER, hand_written.strip("\n"), ""]
    return "\n".join(out)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backtest.m14_9_report")
    parser.add_argument("run_dirs", nargs="+", type=Path, help="early-entry run directories")
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=REPORT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Render the report from ``run_dirs``; keep any hand-written analysis already in ``--out``."""
    args = _parse_args(argv)
    lake = _Lake(args.data_root)
    try:
        facts: list[EntryFacts] = []
        manifests: dict[str, dict[str, Any]] = {}
        selected: dict[str, str] = {}
        seen: dict[tuple[str, str, Decimal, str], str] = {}
        for run_dir in args.run_dirs:
            manifest = _load(run_dir / "manifest.json")
            manifests[run_dir.name] = manifest
            results = _load(run_dir / "results.json")
            family = next(
                (
                    name
                    for name, arms in FAMILIES.items()
                    if [a.label for a in arms] == manifest["arms"]
                ),
                "early-entry",
            )
            selected[f"{manifest['universe']}/{family}"] = results.get("selected", "")
            for fact in collect(run_dir, lake):
                f = fact.base
                key = (f.universe, f.window, f.floor, f.label)
                if key in seen:
                    # D13 sits in both families' directories (copied, §8): the same run, once.
                    if seen[key] != f.replay_digest:
                        raise ValueError(f"two different runs for {key}")
                    continue
                seen[key] = f.replay_digest
                facts.append(fact)
        live = this_time(
            lake,
            [
                ("nifty500", LOW_FLOOR),  # the paper book's own universe (UniverseParameters())
                ("turnover_floor", LOW_FLOOR),
                ("turnover_floor", HIGH_FLOOR),
            ],
        )
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
            manifests=manifests,
            selected=selected,
            live=live,
            hand_written=hand_written,
        ),
        encoding="utf-8",
    )
    print(f"  {len(facts)} runs rendered to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
