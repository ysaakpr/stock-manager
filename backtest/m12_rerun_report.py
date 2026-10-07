"""M12.R — render the re-run's gate report: today's tables, the old ones beside them, and why.

Reads what :mod:`backtest.m12_rerun` wrote (``results.json`` per run directory) and the 2026-09-07
gate reports as they stand on main, and writes one markdown report. The old reports are read, never
edited: they are the historical record, and the comparison is only honest if they stay as struck.

**Old figures are parsed from the old reports' own tables**, because no run of that day persisted a
ledger. Three sources, each with its own arm set: the M12.2 sweep reports (decade and six-year, 23
arms, both floors), the M12.3 verdict (the walk-forward, ₹1 crore floor only — the only floor it
printed) and the duration-grid report (13 arms, all four windows, both floors). A rank is only
compared within the arm set it was struck in: the new rank beside an old one is re-ranked over that
report's arms alone, so a row does not "move" because arms were added around it.

**Attribution is a ladder of measured runs, not a story.** Where a run exists for it, a row's move
is split into: the engine as of the 2026-09-28 after-tax campaign on the pre-#75 lake (rails, book
corporate actions, ₹5,000 minimum order, PIT NIFTY 500 universe, the L2 seam fix), then today's
lake and engine with idle cash earning nothing (#74/#75, data to October), then cash interest. A
window without those runs says so instead of guessing.

What this module never does: replay a run, read a wall clock, or average across windows.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

__all__ = [
    "BAR",
    "Ladder",
    "OldFigure",
    "Row",
    "WindowResult",
    "load_ladder_campaign",
    "load_results",
    "main",
    "parse_duration_report",
    "parse_sweep_report",
    "parse_verdict_report",
    "render",
]

BAR = Decimal("0.25")
#: Marks a section written by hand, so a reader can tell analysis from rendered run output.
WRITTEN = "*Written analysis — not generated from run outputs.*"
#: A move smaller than this, in XIRR, with an unchanged rank, is not listed in the diff table.
MOVE = Decimal("0.005")

LOW = "10000000"
HIGH = "100000000"
FLOOR_LABEL = {LOW: "₹1 crore/day", HIGH: "₹10 crore/day"}

WINDOW_ORDER = ("decade", "six-year", "wf-selection", "wf-verification")
WINDOW_TITLE = {
    "decade": "Decade",
    "six-year": "Six-year",
    "wf-selection": "Walk-forward selection",
    "wf-verification": "Walk-forward verification",
    "long": "Long window (supplementary)",
}

D13 = "Momentum v2, D13 paper config"
V2_ALL_ON = "Momentum v2, all on (M9.5)"

SWEEP_SET = "M12.2 sweep"
DURATION_SET = "duration grid"


# ── the new results ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Row:
    """One arm on one floor of one window, as ``backtest.m12_rerun`` persisted it."""

    label: str
    family: str
    ok: bool
    error: str | None
    xirr: Decimal
    max_drawdown: Decimal
    ratio: Decimal
    round_trips: int
    median_hold_days: int
    cost: Decimal
    excess: Decimal


@dataclass(slots=True)
class WindowResult:
    """One window's sweep: its span, benchmark and the ranked rows per floor."""

    name: str
    start: str
    terminal: str
    sessions: int
    benchmark: str
    benchmark_xirr: Decimal
    floors: dict[str, list[Row]] = field(default_factory=dict)

    def ranked(self, floor: str, labels: Iterable[str] | None = None) -> list[Row]:
        """Rows best ratio first (the driver's order), optionally only those in ``labels``."""
        rows = self.floors.get(floor, [])
        if labels is None:
            return list(rows)
        wanted = set(labels)
        return [row for row in rows if row.label in wanted]

    def rank(self, floor: str, label: str, labels: Iterable[str] | None = None) -> int | None:
        for position, row in enumerate(self.ranked(floor, labels), start=1):
            if row.label == label:
                return position if row.ok else None
        return None

    def row(self, floor: str, label: str) -> Row | None:
        return next((r for r in self.floors.get(floor, []) if r.label == label), None)


def load_results(path: Path) -> tuple[dict[str, WindowResult], str]:
    """``results.json`` → windows by name, and the walk-forward's frozen choice."""
    doc: Any = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, WindowResult] = {}
    for name, raw in doc["windows"].items():
        window = WindowResult(
            name=name,
            start=raw["start"],
            terminal=raw["terminal"],
            sessions=int(raw["sessions"]),
            benchmark=raw["benchmark"],
            benchmark_xirr=Decimal(raw["benchmark_xirr"]),
        )
        for floor, rows in raw["floors"].items():
            window.floors[floor] = [
                Row(
                    label=r["label"],
                    family=r["family"],
                    ok=bool(r["ok"]),
                    error=r["error"],
                    xirr=Decimal(r["xirr"]),
                    max_drawdown=Decimal(r["max_drawdown"]),
                    ratio=Decimal(r["ratio"]),
                    round_trips=int(r["round_trips"]),
                    median_hold_days=int(r["median_hold_days"]),
                    cost=Decimal(r["cost"]),
                    excess=Decimal(r["excess"]),
                )
                for r in rows
            ]
        out[name] = window
    return out, str(doc.get("selected", ""))


# ── the old reports ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OldFigure:
    """One arm's figures in one of the 2026-09-07 reports. ``None`` where that report has none."""

    source: str
    arm_set: str
    window: str
    floor: str
    label: str
    rank: int | None
    xirr: Decimal | None
    max_drawdown: Decimal | None
    ratio: Decimal | None


_PCT = re.compile(r"^(-?[0-9.]+)%$")


def _pct(cell: str) -> Decimal | None:
    match = _PCT.match(cell.strip())
    return Decimal(match.group(1)) / 100 if match else None


def _ratio(cell: str) -> Decimal | None:
    text = cell.strip().strip("*")
    try:
        return Decimal(text)
    except ArithmeticError:
        return None


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _ranked_tables(text: str) -> Iterable[tuple[str, str, list[list[str]]]]:
    """Every ``Ranked — <floor>`` table, with the ``## `` heading it sits under."""
    section = ""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("## "):
            section = line[3:]
        # The sweep reports head their tables "## Ranked", the duration report "### Ranked".
        if line.startswith(("## Ranked — ", "### Ranked — ")):
            floor = LOW if "₹1 crore" in line else HIGH
            rows: list[list[str]] = []
            j = i + 1
            while j < len(lines) and not lines[j].startswith("| #"):
                j += 1
            j += 2  # header and separator
            while j < len(lines) and lines[j].startswith("|"):
                rows.append(_cells(lines[j]))
                j += 1
            yield section, floor, rows
            i = j
            continue
        i += 1


def parse_sweep_report(path: Path, window: str) -> list[OldFigure]:
    """The M12.2 sweep report's ranked tables (columns: #, Strategy, Family, XIRR, Max DD, …)."""
    out: list[OldFigure] = []
    for _section, floor, rows in _ranked_tables(path.read_text(encoding="utf-8")):
        for cells in rows:
            out.append(
                OldFigure(
                    source=path.name,
                    arm_set=SWEEP_SET,
                    window=window,
                    floor=floor,
                    label=cells[1],
                    rank=int(cells[0]) if cells[0].isdigit() else None,
                    xirr=_pct(cells[3]),
                    max_drawdown=_pct(cells[4]),
                    ratio=_ratio(cells[5]),
                )
            )
    return out


_DURATION_WINDOWS = {
    "Decade": "decade",
    "Six-year": "six-year",
    "Walk-forward selection": "wf-selection",
    "Walk-forward verification": "wf-verification",
}


def parse_duration_report(path: Path) -> list[OldFigure]:
    """The duration report's ranked tables (#, Strategy, Duration, XIRR, Max DD, XIRR/DD, …)."""
    out: list[OldFigure] = []
    for section, floor, rows in _ranked_tables(path.read_text(encoding="utf-8")):
        title = section.removeprefix("Window — ").split(":")[0]
        window = _DURATION_WINDOWS.get(title)
        if window is None:
            continue
        for cells in rows:
            out.append(
                OldFigure(
                    source=path.name,
                    arm_set=DURATION_SET,
                    window=window,
                    floor=floor,
                    label=cells[1],
                    rank=int(cells[0]) if cells[0].isdigit() else None,
                    xirr=_pct(cells[3]),
                    max_drawdown=_pct(cells[4]),
                    ratio=_ratio(cells[5]),
                )
            )
    return out


def parse_verdict_report(path: Path) -> list[OldFigure]:
    """The M12.3 verdict's selection-against-verification table (₹1 crore floor only)."""
    out: list[OldFigure] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("| Strategy | Selection"))
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = _cells(line)
        label = cells[0].removesuffix(" ←").strip()
        out.append(
            OldFigure(
                source=path.name,
                arm_set=SWEEP_SET,
                window="wf-selection",
                floor=LOW,
                label=label,
                rank=int(cells[1]),
                xirr=None,
                max_drawdown=None,
                ratio=_ratio(cells[2]),
            )
        )
        out.append(
            OldFigure(
                source=path.name,
                arm_set=SWEEP_SET,
                window="wf-verification",
                floor=LOW,
                label=label,
                rank=int(cells[3]),
                xirr=_pct(cells[5]),
                max_drawdown=None,
                ratio=_ratio(cells[4]),
            )
        )
    return out


# ── the attribution ladder ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Ladder:
    """XIRR by (window, floor, label) for each measured rung between the old report and today."""

    #: The 2026-09-28 after-tax campaign: engine ce49e0f on the pre-#75 lake, no cash interest.
    reference: dict[tuple[str, str, str], Decimal] = field(default_factory=dict)
    reference_name: str = ""
    #: A run of today's engine on today's lake with one switch changed, splitting the move since
    #: the reference in two: ``middle_label`` names middle minus reference, ``last_label`` names
    #: today's figure minus middle.
    middle: dict[tuple[str, str, str], Decimal] = field(default_factory=dict)
    middle_label: str = ""
    last_label: str = ""


#: Today's engine with idle cash at 0 %: the last rung is cash interest alone. Only a run that
#: changes one switch and keeps the floor-only screen may be a middle rung — a run on another
#: universe would step out of the ladder and back, two offsetting rungs that name nothing real.
NO_INTEREST_RUNGS = ("lake #74/#75, engine since", "cash interest")


_FIELD = re.compile(r"(\w+)=((?:Decimal\('[^']*'\))|(?:<[^>]*>)|[^,()]+)")


def _fields(repr_text: str) -> dict[str, str]:
    """``Params(a=1, b=Decimal('2'))`` → ``{"a": "1", "b": "Decimal('2')"}``."""
    return {k: v.strip() for k, v in _FIELD.findall(repr_text)}


def load_ladder_campaign(
    campaign: Path, arms: Mapping[str, tuple[str, str]], windows: Mapping[str, tuple[str, str]]
) -> dict[tuple[str, str, str], Decimal]:
    """XIRR per (window, floor, label) from a persisted campaign directory's run summaries.

    ``arms`` maps label → (runner, parameters repr) as today's code renders it; a persisted run
    matches an arm when its runner agrees and every parameter it recorded has today's value (the
    parameter classes have since grown fields, which an older run cannot have recorded).
    ``windows`` maps window name → (start, end) as the campaign recorded them.
    """
    wanted = {span: name for name, span in windows.items()}
    out: dict[tuple[str, str, str], Decimal] = {}
    for path in sorted((campaign / "runs").glob("*.json")):
        doc: Any = json.loads(path.read_text(encoding="utf-8"))
        spec = doc["spec"]
        window = wanted.get((spec["start"], spec["end"]))
        if window is None:
            continue
        floor_match = re.search(r"median_turnover_floor=Decimal\('(\d+)'\)", spec["universe"])
        if floor_match is None:
            continue
        old = _fields(spec["parameters"])
        for label, (runner, parameters) in arms.items():
            if runner != spec["runner"]:
                continue
            new = _fields(parameters)
            if all(new.get(k) == v for k, v in old.items()):
                out[(window, floor_match.group(1), label)] = Decimal(doc["xirr"])
    return out


# ── rendering ──────────────────────────────────────────────────────────────────────────────────


def _p(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _pp(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:+.2f}"


def _r(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _rupees(value: Decimal) -> str:
    return f"₹{value:,.0f}"


def _mark(label: str) -> str:
    return f"**{label}** ◆" if label == D13 else label


def _table(window: WindowResult, floor: str) -> list[str]:
    lines = [
        "| # | Strategy | Family | XIRR | Max DD | **XIRR/DD** | Round trips | Median hold | Cost "
        "| Excess |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for position, row in enumerate(window.ranked(floor), start=1):
        if not row.ok:
            lines.append(
                f"| — | {_mark(row.label)} | {row.family} | **failed** | — | — | — | — | — | "
                f"{row.error} |"
            )
            continue
        dd = _p(row.max_drawdown) if row.max_drawdown > 0 else "— (no drawdown sampled)"
        lines.append(
            f"| {position} | {_mark(row.label)} | {row.family} | {_p(row.xirr)} | {dd} | "
            f"**{_r(row.ratio)}** | {row.round_trips} | {row.median_hold_days}d | "
            f"{_rupees(row.cost)} | {_p(row.excess)} |"
        )
    return lines


def _best(window: WindowResult, floor: str) -> tuple[Row | None, Row | None]:
    ok = [r for r in window.ranked(floor) if r.ok]
    by_ratio = ok[0] if ok else None
    by_xirr = max(ok, key=lambda r: r.xirr) if ok else None
    return by_ratio, by_xirr


def _spearman(a: Sequence[str], b: Sequence[str]) -> Decimal | None:
    common = [x for x in a if x in set(b)]
    n = len(common)
    if n < 3:
        return None
    ra = {x: i for i, x in enumerate([x for x in a if x in common])}
    rb = {x: i for i, x in enumerate([x for x in b if x in common])}
    d2 = sum((ra[x] - rb[x]) ** 2 for x in common)
    return Decimal(1) - Decimal(6 * d2) / Decimal(n * (n * n - 1))


@dataclass(frozen=True, slots=True)
class _Move:
    old: OldFigure
    new: Row
    new_rank: int | None
    delta: Decimal | None
    reference: Decimal | None
    middle: Decimal | None
    labels: tuple[str, str]


def _moves(
    windows: Mapping[str, WindowResult], old: Sequence[OldFigure], ladder: Ladder
) -> list[_Move]:
    sets: dict[tuple[str, str, str], list[str]] = {}
    for fig in old:
        sets.setdefault((fig.source, fig.window, fig.floor), []).append(fig.label)
    out: list[_Move] = []
    for fig in old:
        window = windows.get(fig.window)
        row = window.row(fig.floor, fig.label) if window else None
        if window is None or row is None:
            continue
        new_rank = window.rank(fig.floor, fig.label, sets[(fig.source, fig.window, fig.floor)])
        delta = row.xirr - fig.xirr if fig.xirr is not None and row.ok else None
        moved = (delta is not None and abs(delta) > MOVE) or new_rank != fig.rank
        if not moved:
            continue
        key = (fig.window, fig.floor, fig.label)
        out.append(
            _Move(
                fig,
                row,
                new_rank,
                delta,
                ladder.reference.get(key),
                ladder.middle.get(key),
                (ladder.middle_label, ladder.last_label),
            )
        )
    return out


def _cause(move: _Move) -> str:
    """The rung that carries most of the move, from measured runs only."""
    if move.delta is None or move.old.xirr is None:
        return "rank only (the old report printed no XIRR for this window)"
    if move.reference is None:
        return "engine + lake, not separable on this window (no intermediate run)"
    parts = {"engine to 2026-09-28": move.reference - move.old.xirr}
    if move.middle is None:
        parts["everything since 2026-09-28"] = move.new.xirr - move.reference
    else:
        parts[move.labels[0]] = move.middle - move.reference
        parts[move.labels[1]] = move.new.xirr - move.middle
    top = max(parts, key=lambda k: abs(parts[k]))
    detail = ", ".join(f"{k} {_pp(v)}" for k, v in parts.items())
    return f"mostly {top} ({detail} pp)"


def render(
    windows: Mapping[str, WindowResult],
    selected: str,
    old: Sequence[OldFigure],
    ladder: Ladder,
    *,
    facts: Sequence[str],
    long_window: WindowResult | None = None,
    notes: str = "",
    universe_check: Mapping[str, WindowResult] | None = None,
) -> str:
    """The whole gate report as markdown; ``notes`` (the written verdict) follows the headline."""
    floors = (LOW, HIGH)
    out: list[str] = []
    add = out.append

    add("# M12.R — the M12 strategy review re-run on today's lake and engine (2026-10-07)")
    add("")
    add(
        "*Generated by `python -m backtest.m12_rerun_report` from the runs `python -m "
        "backtest.m12_rerun` persisted. The 2026-09-07 reports it compares against "
        "(`M12-strategy-sweep-decade.md`, `M12-strategy-sweep-sixyear.md`, "
        "`M12-strategy-verdict.md`, `M12-swing-duration-window-report.md`) are read, never edited. "
        "Ranked on XIRR / max drawdown (owner decision, 2026-09-07). No figure is averaged across "
        "windows. ◆ marks the momentum v2 configuration paper trading runs (D13).*"
    )
    add("")
    add("## How this run was made")
    add("")
    add(WRITTEN)
    add("")
    out.extend(f"- {line}" for line in facts)
    add("")

    # ── headline ──
    add("## Headline, per window and floor")
    add("")
    add(
        "| Window | Floor | Best XIRR/DD | its XIRR / DD | Highest XIRR | D13 paper config: rank, "
        "XIRR, DD, XIRR/DD | Benchmark |"
    )
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for name in WINDOW_ORDER:
        window = windows.get(name)
        if window is None:
            continue
        for floor in floors:
            by_ratio, by_xirr = _best(window, floor)
            d13 = window.row(floor, D13)
            n = len(window.ranked(floor))
            d13_cell = (
                f"{window.rank(floor, D13)} of {n}, {_p(d13.xirr)}, {_p(d13.max_drawdown)}, "
                f"{_r(d13.ratio)}"
                if d13 is not None and d13.ok
                else "failed"
            )
            add(
                f"| {WINDOW_TITLE[name]} | {FLOOR_LABEL[floor]} | "
                f"{by_ratio.label if by_ratio else '—'} "
                f"({_r(by_ratio.ratio if by_ratio else None)})"
                f" | {_p(by_ratio.xirr if by_ratio else None)} / "
                f"{_p(by_ratio.max_drawdown if by_ratio else None)} | "
                f"{by_xirr.label if by_xirr else '—'} ({_p(by_xirr.xirr if by_xirr else None)}) | "
                f"{d13_cell} | {_p(window.benchmark_xirr)} |"
            )
    add("")

    if notes:
        out.extend([notes.rstrip(), ""])

    # ── walk-forward ──
    sel, ver = windows.get("wf-selection"), windows.get("wf-verification")
    if sel is not None and ver is not None:
        add("## Walk-forward: chosen on 2016-09..2021-08, verified on 2021-09..2026-08")
        add("")
        add(
            "The choice on record is made by `backtest.verdict.run_walk_forward` on the selection "
            "window's ₹1 crore ranking and frozen before the verification sweep runs, as in "
            "2026-09-07. The same rule applied to the ₹10 crore ranking is shown beside it, "
            "because that is the floor a real book plans against. Each is shown over every arm "
            "of this re-run and over each old report's own arm set, so the old choice and the "
            "new one compare like for like."
        )
        add("")
        add(
            "| Arm set | Chosen on | Choice | Selection XIRR / DD (ratio) | Verification, same "
            "floor: rank, XIRR / DD (ratio) | Verification, other floor: rank, XIRR | Old choice "
            "(2026-09-07, ₹1 crore) |"
        )
        add("| --- | --- | --- | --- | --- | --- | --- |")
        sweep_labels = sorted({f.label for f in old if f.arm_set == SWEEP_SET})
        duration_labels = sorted({f.label for f in old if f.arm_set == DURATION_SET})
        olds = {
            SWEEP_SET: "M10.7 + regime gate",
            DURATION_SET: "M10.7 @ monthly / 126-session hold",
        }
        for floor in floors:
            other = HIGH if floor == LOW else LOW
            for set_name, labels in (
                ("every arm of this re-run", None),
                (SWEEP_SET, sweep_labels),
                (DURATION_SET, duration_labels),
            ):
                ranked = [r for r in sel.ranked(floor, labels) if r.ok]
                if not ranked:
                    continue
                choice = ranked[0]
                if floor == LOW and labels is None and selected and choice.label != selected:
                    raise ValueError(
                        f"frozen choice {selected!r} is not the top row {choice.label!r}"
                    )
                vrow = ver.row(floor, choice.label)
                orow = ver.row(other, choice.label)
                n = len(ver.ranked(floor, labels))
                old_choice = (
                    olds.get(set_name, "—") if floor == LOW else "— (chose on ₹1 crore only)"
                )
                add(
                    f"| {set_name} | {FLOOR_LABEL[floor]} | **{choice.label}** | "
                    f"{_p(choice.xirr)} / {_p(choice.max_drawdown)} ({_r(choice.ratio)}) | "
                    f"**{ver.rank(floor, choice.label, labels)} of {n}**, "
                    f"{_p(vrow.xirr if vrow else None)} / "
                    f"{_p(vrow.max_drawdown if vrow else None)} "
                    f"({_r(vrow.ratio if vrow else None)}) | "
                    f"{ver.rank(other, choice.label, labels)} of {n}, "
                    f"{_p(orow.xirr if orow else None)} | {old_choice} |"
                )
        add("")
        for floor in floors:
            rho = _spearman(
                [r.label for r in sel.ranked(floor) if r.ok],
                [r.label for r in ver.ranked(floor) if r.ok],
            )
            add(
                f"- Spearman rank correlation, selection against verification, "
                f"{FLOOR_LABEL[floor]}: **{_r(rho)}**"
            )
        add("")
        add("### Selection rank against verification rank — every arm, ₹1 crore/day")
        add("")
        add(
            "| Strategy | Selection rank | Selection XIRR | Selection XIRR/DD | Verification rank "
            "| Verification XIRR | Verification XIRR/DD |"
        )
        add("| --- | --- | --- | --- | --- | --- | --- |")
        for row in sel.ranked(LOW):
            v = ver.row(LOW, row.label)
            mark = " ←" if row.label == selected else ""
            add(
                f"| {_mark(row.label)}{mark} | {sel.rank(LOW, row.label) or '—'} | "
                f"{_p(row.xirr)} | "
                f"{_r(row.ratio)} | {ver.rank(LOW, row.label) or '—'} | "
                f"{_p(v.xirr if v else None)} | {_r(v.ratio if v else None)} |"
            )
        add("")

    # ── the bar ──
    add("## The 25 % bar")
    add("")
    add(
        "*Only the walk-forward verification window is out-of-sample. The decade, the six-year "
        "window and the selection window are in-sample by construction and overlap one another.*"
    )
    add("")
    for name in WINDOW_ORDER:
        window = windows.get(name)
        if window is None:
            continue
        scope = "**out-of-sample**" if name == "wf-verification" else "in-sample"
        for floor in floors:
            clear = [r for r in window.ranked(floor) if r.ok and r.xirr > BAR]
            if clear:
                names = ", ".join(
                    f"{r.label} ({_p(r.xirr)}, DD {_p(r.max_drawdown)})" for r in clear
                )
                add(f"- {WINDOW_TITLE[name]} ({scope}), {FLOOR_LABEL[floor]}: {names}")
            else:
                _, top = _best(window, floor)
                add(
                    f"- {WINDOW_TITLE[name]} ({scope}), {FLOOR_LABEL[floor]}: none — best "
                    f"{_p(top.xirr if top else None)} ({top.label if top else '—'})"
                )
    add("")

    # ── full tables ──
    for name in WINDOW_ORDER:
        window = windows.get(name)
        if window is None:
            continue
        add(f"## {WINDOW_TITLE[name]}: {window.start} → {window.terminal}")
        add("")
        add(
            f"- {window.sessions} sessions; benchmark **{_p(window.benchmark_xirr)}** "
            f"({window.benchmark}) — one arm's money-weighted figure; each row's Excess is struck "
            "on its own cashflows"
        )
        add("")
        for floor in floors:
            add(f"### Ranked — {FLOOR_LABEL[floor]} liquidity floor")
            add("")
            out.extend(_table(window, floor))
            add("")

    # ── diff ──
    moves = _moves(windows, old, ladder)
    add("## What moved since 2026-09-07")
    add("")
    add(
        f"Every arm whose XIRR moved by more than {MOVE * 100:.1f} pp, or whose rank changed, "
        "against the report it was printed in. *Old rank* is as printed; *new rank* is re-ranked "
        "over that report's own arm set, so adding arms cannot move a row. The cause column is "
        "measured where intermediate runs exist (decade and six-year), as rungs that sum to the "
        "move. **engine to 2026-09-28** is "
        f"`{ladder.reference_name or 'the reference campaign'}` minus the old figure: the engine "
        "as of that campaign, on the pre-#75 lake with idle cash at 0 %. It is the *combined* "
        "engine change — corporate actions in the book, the A8 rails, the ₹5,000 minimum order "
        "and the L2 seam fix — and no run separates one from another. That campaign is "
        "**floor-only in effect**: its specs name `index_slug='nifty500'`, but at its commit the "
        "screen read constituent snapshots through `membership_asof`, which answers `None` "
        "before the first snapshot (2026-09-08, after every window ends), and the screen is "
        "then a no-op; the PIT membership history (`f006a9b`) is not its ancestor. So the old "
        "report, the reference campaign and today's run all screen the same floor-only universe. "
        + (
            f"Where a third run exists, **{ladder.middle_label}** is that run minus the reference "
            f"and **{ladder.last_label}** is today's figure minus that run; elsewhere "
            if ladder.middle
            else ""
        )
        + "**everything since 2026-09-28** is today's figure minus the reference: lake #74/#75, "
        "the engine since (among it the regime gate reading the published NIFTY 50, `e6e862f`) "
        "and idle cash earning repo - 0.50 %, together. "
        + "The walk-forward windows have no intermediate run and say so."
    )
    add("")
    add(
        "| Window | Floor | Report | Strategy | Old XIRR | New XIRR | Δ pp | Old rank | New rank "
        "| Cause |"
    )
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for move in sorted(
        moves,
        key=lambda m: (
            WINDOW_ORDER.index(m.old.window),
            m.old.floor != LOW,
            m.old.arm_set,
            m.old.rank or 99,
        ),
    ):
        add(
            f"| {WINDOW_TITLE[move.old.window]} | {FLOOR_LABEL[move.old.floor]} | "
            f"{move.old.arm_set} | {move.old.label} | {_p(move.old.xirr)} | {_p(move.new.xirr)} | "
            f"{_pp(move.delta)} | {move.old.rank or '—'} | {move.new_rank or '—'} | "
            f"{_cause(move)} |"
        )
    add("")
    if universe_check:
        add("## Universe check: the PIT NIFTY 500 screen the paper book trades")
        add("")
        add(
            "Every table above screens the floor-only universe (see *How this run was made*). "
            "The paper book screens the PIT NIFTY 500, whose membership history opens 2016-10-24, "
            "so the headline arms were also run on it over the two windows it covers — today's "
            "engine and lake, the same switches, only the universe changed. Ranks are within "
            "these arms only. It is a separate measurement of the universe, not a rung of the "
            "attribution in *What moved*: every run there screens the floor-only universe."
        )
        add("")
        add(
            "| Window | Floor | Strategy | Floor-only XIRR / DD (ratio) | NIFTY 500 XIRR / DD "
            "(ratio) | NIFTY 500 rank | Δ XIRR pp (floor-only minus NIFTY 500) |"
        )
        add("| --- | --- | --- | --- | --- | --- | --- |")
        for name, check in universe_check.items():
            base = windows.get(name)
            for floor in floors:
                for position, row in enumerate(check.ranked(floor), start=1):
                    wide = base.row(floor, row.label) if base else None
                    diff = wide.xirr - row.xirr if wide and wide.ok and row.ok else None
                    wide_cell = (
                        f"{_p(wide.xirr)} / {_p(wide.max_drawdown)} ({_r(wide.ratio)})"
                        if wide
                        else "—"
                    )
                    add(
                        f"| {WINDOW_TITLE[name]} | {FLOOR_LABEL[floor]} | {_mark(row.label)} | "
                        f"{wide_cell} | {_p(row.xirr)} / {_p(row.max_drawdown)} "
                        f"({_r(row.ratio)}) | {position} of {len(check.ranked(floor))} | "
                        f"{_pp(diff)} |"
                    )
        add("")
    if long_window is not None:
        add(f"## Supplementary: {long_window.start} → {long_window.terminal}")
        add("")
        add(
            "*Not part of the mandate's three windows and not used for any choice. L2 reaches "
            "2006 since #74/#75; this window is the longest the lookbacks allow.*"
        )
        add("")
        for floor in floors:
            add(f"### Ranked — {FLOOR_LABEL[floor]} liquidity floor")
            add("")
            out.extend(_table(long_window, floor))
            add("")
    return "\n".join(out) + "\n"


def _arm_specs() -> dict[str, tuple[str, str]]:
    from backtest.m12_rerun import RERUN_ARMS

    specs: dict[str, tuple[str, str]] = {}
    for arm in RERUN_ARMS:
        if arm.band_hit_avoidance or arm.cap_tiers is not None:
            continue  # their difference is outside the parameters, so an old run cannot show it
        if arm.swing is not None:
            specs[arm.label] = ("swing_composite", repr(arm.swing))
        elif arm.naive is not None:
            specs[arm.label] = ("naive_momentum", repr(arm.naive))
        else:
            specs[arm.label] = ("momentum_v2", repr(arm.v2))
    return specs


def _xirrs(path: Path | None) -> dict[tuple[str, str, str], Decimal]:
    if path is None:
        return {}
    windows, _ = load_results(path)
    return {
        (name, floor, row.label): row.xirr
        for name, window in windows.items()
        for floor, rows in window.floors.items()
        for row in rows
        if row.ok
    }


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m backtest.m12_rerun_report``: render the gate report from persisted results."""
    parser = argparse.ArgumentParser(prog="python -m backtest.m12_rerun_report")
    parser.add_argument("--results", type=Path, required=True, help="the re-run's results.json")
    parser.add_argument("--no-interest", type=Path, default=None, help="results.json, 0%% cash")
    parser.add_argument(
        "--nifty500", type=Path, default=None, help="results.json for the universe check"
    )
    parser.add_argument("--long", type=Path, default=None, help="results.json with the long window")
    parser.add_argument("--reference-campaign", type=Path, default=None)
    parser.add_argument("--fact", action="append", default=[], help="a line for 'How this was run'")
    parser.add_argument("--gates", type=Path, default=Path("ops/gates"))
    parser.add_argument("--notes", type=Path, default=None, help="markdown: the written verdict")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    windows, selected = load_results(args.results)
    old = [
        *parse_sweep_report(args.gates / "M12-strategy-sweep-decade.md", "decade"),
        *parse_sweep_report(args.gates / "M12-strategy-sweep-sixyear.md", "six-year"),
        *parse_verdict_report(args.gates / "M12-strategy-verdict.md"),
        *parse_duration_report(args.gates / "M12-swing-duration-window-report.md"),
    ]
    ladder = Ladder()
    if args.no_interest is not None:
        ladder.middle = _xirrs(args.no_interest)
        ladder.middle_label, ladder.last_label = NO_INTEREST_RUNGS
    universe_check: dict[str, WindowResult] = {}
    if args.nifty500 is not None:
        # The universe check only — never a rung of the attribution ladder (see NO_INTEREST_RUNGS).
        universe_check, _ = load_results(args.nifty500)
    if args.reference_campaign is not None:
        ladder.reference = load_ladder_campaign(
            args.reference_campaign,
            _arm_specs(),
            {"decade": ("2016-09-02", "2026-08-31"), "six-year": ("2019-07-01", "2026-08-31")},
        )
        ladder.reference_name = args.reference_campaign.name
    long_window = None
    if args.long is not None:
        long_windows, _ = load_results(args.long)
        long_window = long_windows.get("long")
    notes = args.notes.read_text(encoding="utf-8") if args.notes is not None else ""
    text = render(
        windows,
        selected,
        old,
        ladder,
        facts=args.fact,
        long_window=long_window,
        notes=notes,
        universe_check=universe_check,
    )
    args.out.write_text(text, encoding="utf-8")
    print(f"  report written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
