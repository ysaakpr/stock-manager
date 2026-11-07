"""M14.12 — render the early-exit combination report from the saved runs; replays nothing.

Reads the run directory ``backtest.m12_rerun --arm-set early-exit-combo --universe nifty500``
wrote and renders ``ops/gates/M14.12-early-exit-combo-report.md``. The study is pre-registered in
``ops/studies/preregistration-m14-12-early-exit-combo-2026-11-07.md``: D13, C0 (the paper
candidate, V2 + S+refill) and C0 with M14.5's daily exit, banded (C1) and unbanded (C2).

The tables are M14.10's (:mod:`backtest.m14_9_report`'s machinery). What is new here:

* **Parks and re-entries with the daily exit.** :func:`exit_switches` replays the policy's parked
  state with its own rules — the monthly rebalance, the daily exit (on a non-rebalance session,
  an invested book holding settled shares, TRI below MA200 x (1 - band)), and the V2 trigger (a
  parked book, once per spell) — in ``MomentumV2Policy.decide``'s order, and checks each park and
  re-entry against the fill ledger (a sale, or a purchase, on the following session). "Settled
  shares" is read from the ledger as a purchase filled on an earlier session.
* **Whipsaw.** A re-entry is *reversed* when a park follows it within 21 sessions; a park is
  reversed when a re-entry follows it within 21 sessions.
* **Two comparators.** The decision rule (pre-registration §4) holds an exit arm against D13
  *and* C0, and the scorecard is printed against both.
* **Provenance.** D13's and C0's replay digests are compared with M14.10's runs of the same spec.

Everything below the hand-written marker in an existing report is kept on re-render.

What it never does: replay a run, read a wall clock into the report, or average across windows.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from backtest.m12_rerun import EARLY_EXIT_COMBO_SET, WINDOWS
from backtest.m14_5_report import (
    _WINDOW_ORDER,
    BAR,
    MARKER,
    _floor_label,
    _lakh,
    _pp,
    _pp_rel,
    scorecard,
)
from backtest.m14_9_report import (
    DD_TOLERANCE,
    REVERSAL_SESSIONS,
    EntryFacts,
    EntrySwitches,
    Readings,
    _fires,
    _Lake,
    _load,
    _table,
    collect,
    sector_table,
)
from backtest.policies.momentum_v2 import MomentumV2Parameters
from backtest.run import _first_session_of_each_month
from backtest.sweep import D13_PAPER_BASELINE, HIGH_FLOOR, LOW_FLOOR, Arm

__all__ = [
    "ExitSwitches",
    "conditions",
    "exit_switches",
    "main",
    "render",
    "verdict",
    "whipsaw_table",
]

REPORT = Path("ops/gates/M14.12-early-exit-combo-report.md")
_UNIVERSE = "nifty500"
_D13 = D13_PAPER_BASELINE
_C0: Arm = EARLY_EXIT_COMBO_SET[1]
_EXIT_ARMS: tuple[Arm, ...] = EARLY_EXIT_COMBO_SET[2:]


@dataclass(frozen=True, slots=True)
class ExitSwitches:
    """Parks and re-entries over one run's window, by kind, with ledger checks and whipsaw.

    ``parks`` and ``reentries`` are the sessions each transition was decided (it fills on the
    next session). A risk-off rebalance of a book already parked is not a park; it is counted in
    ``park_sessions`` (every session a park order was issued), as M14.5 counts it.
    """

    parks: tuple[tuple[date, str], ...]
    reentries: tuple[tuple[date, str], ...]
    parks_confirmed: int
    reentries_confirmed: int
    reentries_reversed: int
    parks_reversed: int
    park_sessions: int

    def count(self, kind: str, *, parks: bool) -> int:
        events = self.parks if parks else self.reentries
        return sum(1 for _, k in events if k == kind)


def exit_switches(
    params: MomentumV2Parameters,
    sessions: Sequence[date],
    readings: Readings,
    *,
    bought: set[date],
    sold: set[date],
) -> ExitSwitches:
    """Replay the policy's parked state over ``sessions`` with its own rules (``decide``'s order).

    On a rebalance session: risk-off parks (a new park only if invested), risk-on unparks. Off a
    rebalance: the daily exit first (``regime_daily_exit``; invested, settled shares held, TRI
    below MA200 x (1 - band)), else the early trigger on a parked book. ``bought`` / ``sold`` are
    the ledger's fill dates: they decide "settled shares held" (a purchase filled on an earlier
    session) and confirm each transition (a fill on the following session).
    """
    if params.regime_daily_reentry:
        raise ValueError("the daily re-entry is not an M14.12 arm; exit_switches does not model it")
    rebalances = set(_first_session_of_each_month(sessions))
    first_buy = min(bought, default=None)
    band = params.regime_daily_band
    mode = params.early_entry
    parked = False
    park_sessions = 0
    parks: list[tuple[date, str]] = []
    reentries: list[tuple[date, str]] = []
    for session in sessions:
        if session in rebalances:
            if not readings.regime(session).risk_on:
                park_sessions += 1
                if not parked:
                    parks.append((session, "monthly"))
                parked = True
            else:
                if parked:
                    reentries.append((session, "monthly"))
                parked = False
            continue
        if (
            params.regime_daily_exit
            and not parked
            and first_buy is not None
            and first_buy < session
            and readings.regime(session).breaks_below(band)
        ):
            parks.append((session, "daily"))
            park_sessions += 1
            parked = True
            continue
        if parked and mode is not None and _fires(mode, session, readings):
            reentries.append((session, "early"))
            parked = False
    following = dict(pairwise(sessions))
    index = {s: i for i, s in enumerate(sessions)}

    def followed(first: Sequence[tuple[date, str]], then: Sequence[tuple[date, str]]) -> int:
        return sum(
            1
            for s, _ in first
            if any(0 < index[t] - index[s] <= REVERSAL_SESSIONS for t, _ in then)
        )

    return ExitSwitches(
        parks=tuple(parks),
        reentries=tuple(reentries),
        parks_confirmed=sum(1 for s, _ in parks if following.get(s) in sold),
        reentries_confirmed=sum(1 for s, _ in reentries if following.get(s) in bought),
        reentries_reversed=followed(reentries, parks),
        parks_reversed=followed(parks, reentries),
        park_sessions=park_sessions,
    )


def entry_view(
    sw: ExitSwitches, sessions: Sequence[date], readings: Readings, *, bought: set[date]
) -> EntrySwitches:
    """The M14.10 table's columns (:class:`EntrySwitches`) from the exit-aware replay."""
    early = [s for s, kind in sw.reentries if kind == "early"]
    rebalances = _first_session_of_each_month(sessions)
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
        parks=len(sw.parks),
        monthly_reentries=sw.count("monthly", parks=False),
        early=len(early),
        early_confirmed=sum(1 for s in early if following.get(s) in bought),
        reparked=reparked,
        index_reversed=reversed_,
        park_sessions=sw.park_sessions,
        early_sessions=tuple(early),
    )


@dataclass(frozen=True, slots=True)
class ComboFacts:
    """One saved run: M14.10's facts (with exit-aware switches) plus the whipsaw counts."""

    entry: EntryFacts
    exits: ExitSwitches


def collect_combo(run_dir: Path, lake: _Lake) -> list[ComboFacts]:
    """Every run of the M14.12 arm set in ``run_dir``, with its switches replayed exit-aware."""
    by_label = {arm.label: arm for arm in EARLY_EXIT_COMBO_SET}
    out: list[ComboFacts] = []
    for fact in collect(run_dir, lake, arms=EARLY_EXIT_COMBO_SET):
        f = fact.base
        arm = by_label[f.label]
        assert arm.v2 is not None
        ledger = _load(run_dir / "ledgers" / f"{f.digest}.json")
        bought = {
            date.fromisoformat(t["trade_date"]) for t in ledger["trades"] if t["side"] == "BUY"
        }
        sold = {
            date.fromisoformat(t["trade_date"]) for t in ledger["trades"] if t["side"] == "SELL"
        }
        first, last = WINDOWS[f.window]
        sessions = [s for s in lake.calendar if first <= s <= last]
        readings = Readings(regime=lake.regime, trend=lake.trend, breadth=None)
        exits = exit_switches(arm.v2, sessions, readings, bought=bought, sold=sold)
        view = entry_view(exits, sessions, readings, bought=bought)
        if not arm.v2.regime_daily_exit and view != fact.switches:
            # Without the daily exit the two replays are the same rule: a mismatch is a bug here.
            raise ValueError(f"{f.label} {f.window} {f.floor}: exit replay disagrees with M14.9's")
        base = replace(
            f,
            switches=replace(
                f.switches,
                parks=len(exits.parks),
                reentries=len(exits.reentries),
                parks_confirmed=exits.parks_confirmed,
                reentries_confirmed=exits.reentries_confirmed,
                park_sessions=exits.park_sessions,
            ),
        )
        out.append(ComboFacts(entry=replace(fact, base=base, switches=view), exits=exits))
    return out


# ── tables ───────────────────────────────────────────────────────────────────────────────────────


def whipsaw_table(facts: Sequence[ComboFacts]) -> list[str]:
    """Parks, re-entries and their reversals for every arm and cell, with turnover and charges."""
    order = {arm.label: i for i, arm in enumerate(EARLY_EXIT_COMBO_SET)}
    c0 = {
        (c.entry.base.window, c.entry.base.floor): c.entry.base
        for c in facts
        if c.entry.base.label == _C0.label
    }
    out = [
        "## Parks and whipsaw (generated, pre-registration §5)",
        "",
        "Replayed with the policy's own rules (`backtest.m14_12_report.exit_switches`) over the "
        "published NIFTY 50 TRI, in `decide`'s order, and checked against the fill ledger: in "
        "brackets, the parks a sale and the re-entries a purchase confirm on the following "
        "session. **Re-entries reversed** had a park within the 21 sessions after them; **parks "
        "reversed** had a re-entry within the 21 sessions after them. **Park sessions** counts "
        "every session a park was issued (a risk-off rebalance of a parked book re-issues it).",
        "",
        "| Window | Floor | Strategy | Parks: monthly / daily (confirmed) | Re-entries: monthly "
        "/ V2 early (confirmed) | Re-entries reversed ≤ 21 sessions | Parks reversed ≤ 21 "
        "sessions | Park sessions | Trades | One-way turnover /yr | Charges | Charges vs C0 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    rows = sorted(
        facts,
        key=lambda c: (
            _WINDOW_ORDER.index(c.entry.base.window),
            c.entry.base.floor,
            order[c.entry.base.label],
        ),
    )
    for c in rows:
        f, sw = c.entry.base, c.exits
        ref = c0.get((f.window, f.floor))
        vs_c0 = (
            "—"
            if ref is None or f.label == _C0.label or ref.charges <= 0
            else _pp_rel(f.charges / ref.charges - 1)
        )
        out.append(
            f"| {f.window} | {_floor_label(f.floor)} | {f.label} "
            f"| {sw.count('monthly', parks=True)} / {sw.count('daily', parks=True)} "
            f"({sw.parks_confirmed}) "
            f"| {sw.count('monthly', parks=False)} / {sw.count('early', parks=False)} "
            f"({sw.reentries_confirmed}) | {sw.reentries_reversed} | {sw.parks_reversed} "
            f"| {sw.park_sessions} | {f.trades} | {f.turnover}x | {_lakh(f.charges)} | {vs_c0} |"
        )
    out.append("")
    return out


def provenance_check(facts: Sequence[ComboFacts], m14_10_dir: Path | None) -> list[str]:
    """D13's and C0's replay digests against M14.10's runs of the same spec digest."""
    if m14_10_dir is None:
        return []
    out = [
        "## D13 and C0 against M14.10's runs (generated)",
        "",
        f"Same spec digest, looked up in `{m14_10_dir.name}`; the replay digest covers the "
        "journal, the book and the rails byte for byte.",
        "",
        "| Window | Floor | Strategy | Spec digest | M14.10 replay digest equal? |",
        "| --- | --- | --- | --- | --- |",
    ]
    order = {_D13.label: 0, _C0.label: 1}
    rows = sorted(
        (c.entry.base for c in facts if c.entry.base.label in order),
        key=lambda f: (_WINDOW_ORDER.index(f.window), f.floor, order[f.label]),
    )
    for f in rows:
        summary = m14_10_dir / "runs" / f"{f.digest}.json"
        if summary.is_file():
            equal = "yes" if _load(summary)["replay_digest"] == f.replay_digest else "**no**"
        else:
            equal = "not in M14.10"
        out.append(
            f"| {f.window} | {_floor_label(f.floor)} | {f.label} | `{f.digest[:12]}` | {equal} |"
        )
    out.append("")
    return out


# ── the decision rule (pre-registration §4), evaluated in code ───────────────────────────────────


@dataclass(frozen=True, slots=True)
class Conditions:
    """The three M14.10 conditions for one arm against one comparator over the NIFTY 500 cells."""

    verification: bool
    at_least: int
    cells: int
    drawdown: bool
    worst: Decimal

    @property
    def met(self) -> bool:
        return self.verification and self.at_least >= 6 and self.drawdown

    def failures(self, comparator: str) -> list[str]:
        failed = []
        if not self.verification:
            failed.append(f"verification XIRR/DD below {comparator}'s at a floor")
        if self.at_least < 6:
            failed.append(f"{self.at_least} of {self.cells} cells at or above {comparator}")
        if not self.drawdown:
            failed.append(f"a drawdown {_pp(self.worst)} deeper than {comparator}'s")
        return failed


def conditions(facts: Sequence[EntryFacts], label: str, comparator: str) -> Conditions:
    """``label``'s three conditions against ``comparator`` on the NIFTY 500 cells."""
    cells = [f.base for f in facts if f.base.universe == _UNIVERSE]
    base = {(f.window, f.floor): f for f in cells if f.label == comparator}
    rows = {(f.window, f.floor): f for f in cells if f.label == label}
    pairs = [(rows[k], base[k]) for k in rows if k in base]
    verification = all(
        ("wf-verification", floor) in rows
        and ("wf-verification", floor) in base
        and rows[("wf-verification", floor)].ratio >= base[("wf-verification", floor)].ratio
        for floor in (LOW_FLOOR, HIGH_FLOOR)
    )
    worst = max((f.max_drawdown - b.max_drawdown for f, b in pairs), default=Decimal(0))
    return Conditions(
        verification=verification,
        at_least=sum(1 for f, b in pairs if f.ratio >= b.ratio),
        cells=len(pairs),
        drawdown=bool(pairs) and worst <= DD_TOLERANCE,
        worst=worst,
    )


def decision_table(facts: Sequence[EntryFacts], selected: str) -> list[str]:
    """The pre-registered conditions for each arm against each comparator."""
    out = [
        "## Decision rule (pre-registered M14.12 §4, evaluated in code)",
        "",
        "1. **Walk-forward choice** (NIFTY 500 selection window, XIRR ÷ max DD, ₹1 cr first): "
        f"**{selected or '(none)'}**.",
        "2. An exit arm (C1, C2) is recommended only if it is the walk-forward's choice **and** "
        "meets all three conditions against D13 **and** against C0. If D13 or C0 is chosen, no "
        "change is recommended. Every row below is shown for the record.",
        "",
        "| Arm | Against | Verification XIRR/DD ≥ at both floors | Cells with XIRR/DD ≥ (of 8; "
        "need ≥ 6) | Max DD within 2 pp in every cell | All three |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    rows = [(_C0, _D13)] + [(arm, comp) for arm in _EXIT_ARMS for comp in (_D13, _C0)]
    for arm, comp in rows:
        c = conditions(facts, arm.label, comp.label)
        if not c.cells:
            continue
        name = "D13" if comp is _D13 else "C0"
        out.append(
            f"| {arm.label} | {name} | {'yes' if c.verification else 'no'} "
            f"| {c.at_least} / {c.cells} | {'yes' if c.drawdown else 'no'} (worst {_pp(c.worst)}) "
            f"| {'yes' if c.met else 'no'} |"
        )
    out.append("")
    return out


def verdict(facts: Sequence[EntryFacts], selected: str) -> list[str]:
    """The rule's outcome in words: only the walk-forward's own choice can be recommended."""
    if not selected or selected in (_D13.label, _C0.label):
        return [
            f"3. **Verdict (code):** the walk-forward chose **{selected or '(none)'}**; no change "
            "is recommended. D13 stays as ratified and the M14.11 candidate book is unchanged.",
            "",
        ]
    failed = [
        f"against {name}: {text}"
        for comp, name in ((_D13, "D13"), (_C0, "C0"))
        for text in conditions(facts, selected, comp.label).failures(name)
    ]
    if not failed:
        lines = [
            f"3. **Verdict (code):** {selected} meets all three conditions against D13 and C0: "
            "recommended as a candidate; adoption is the owner's decision."
        ]
    else:
        lines = [
            f"3. **Verdict (code):** the chosen arm, {selected}, is **not recommended** "
            f"({'; '.join(failed)}). No change is recommended."
        ]
    others = [
        arm.label
        for arm in _EXIT_ARMS
        if arm.label != selected
        and all(conditions(facts, arm.label, comp.label).met for comp in (_D13, _C0))
    ]
    if others:
        lines.append(
            "4. Exit arms the walk-forward did **not** choose that meet all six checks: "
            f"{', '.join(others)}. The rule never recommends an unchosen arm."
        )
    return [*lines, ""]


# ── rendering ────────────────────────────────────────────────────────────────────────────────────


def render(
    facts: Sequence[ComboFacts],
    *,
    manifests: Mapping[str, Mapping[str, Any]],
    selected: str,
    provenance: Sequence[str],
    hand_written: str,
) -> str:
    """The whole report: provenance, the tables, scorecards, whipsaw, the decision rule."""
    entries = [c.entry for c in facts]
    out = [
        "# M14.12 — early entry + early exit + sector refill on NIFTY 500",
        "",
        "> **Generated** by `uv run python -m backtest.m14_12_report` from the saved runs listed "
        "below; nothing in the tables is typed by hand. The section after the marker at the end "
        "is hand-written analysis and is labelled as such.",
        "",
        "## What was run",
        "",
        "Pre-registered in `ops/studies/preregistration-m14-12-early-exit-combo-2026-11-07.md` "
        "before any code or run. The universe is the PIT NIFTY 500 screen; windows, floors and "
        "the mandate are M14.10's. The arms:",
        "",
    ]
    for arm in EARLY_EXIT_COMBO_SET:
        out.append(f"* **{arm.label}** — {arm.note}.")
    out += [
        "",
        "**Spell rule (pre-registration §3.2).** A park (a risk-off monthly rebalance, or in "
        "C1/C2 a daily exit) opens a parked spell; a re-entry (a risk-on monthly rebalance, or "
        "V2 firing on a non-rebalance session) ends it, so V2 re-enters at most once per spell "
        "and each park opens a new one. On a non-rebalance session the daily exit is read "
        "before V2, so a session is at most one park or one re-entry. A parked book is never "
        "re-parked daily.",
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
        "Columns as in M14.10: **Parks / monthly re-entries** count every park (monthly and, for "
        "C1/C2, daily) and the risk-on rebalances that unpark; **Early re-entries** are V2's, "
        "with how many the fill ledger confirms in brackets; **Reversed ≤ 1 month** counts V2 "
        "re-entries re-parked at the next rebalance, then those after which the TRI closed below "
        "its MA200 within 21 sessions. **A8 min-holdings refusals** are all sells the 8-name "
        "floor refused; in brackets, the park sessions. The daily parks and the whipsaw are in "
        "their own table below.",
        "",
        "## Results — NIFTY 500 universe",
        "",
    ]
    for window in _WINDOW_ORDER:
        cell = [e for e in entries if e.base.window == window and e.base.universe == _UNIVERSE]
        if not cell:
            continue
        first, last = WINDOWS[window]
        out += [f"### {window} ({first.isoformat()} → {last.isoformat()})", ""]
        for floor in (LOW_FLOOR, HIGH_FLOOR):
            rows = [e for e in cell if e.base.floor == floor]
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
    out += whipsaw_table(facts)
    bases = [e.base for e in entries]
    out += scorecard(bases, EARLY_EXIT_COMBO_SET)
    out += scorecard(bases, EARLY_EXIT_COMBO_SET, baseline=_C0, name="C0")
    out += [
        f"**>25% bar:** {sum(1 for f in bases if f.xirr > BAR)} of {len(bases)} runs clear "
        "a backtested XIRR above 25%; per cell in the last column of each results table.",
        "",
    ]
    out += sector_table(entries, EARLY_EXIT_COMBO_SET)
    out += decision_table(entries, selected)
    out += verdict(entries, selected)
    out += list(provenance)
    out += [MARKER, hand_written.strip("\n"), ""]
    return "\n".join(out)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backtest.m14_12_report")
    parser.add_argument("run_dir", type=Path, help="the early-exit-combo run directory")
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=REPORT)
    parser.add_argument(
        "--m14-10-dir",
        type=Path,
        default=None,
        help="M14.10's run directory, to compare D13's and C0's replay digests with",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Render the report from ``run_dir``; keep any hand-written analysis already in ``--out``."""
    args = _parse_args(argv)
    lake = _Lake(args.data_root)
    try:
        manifest = _load(args.run_dir / "manifest.json")
        results = _load(args.run_dir / "results.json")
        facts = collect_combo(args.run_dir, lake)
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
            provenance=provenance_check(facts, args.m14_10_dir),
            hand_written=hand_written,
        ),
        encoding="utf-8",
    )
    print(f"  {len(facts)} runs rendered to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
