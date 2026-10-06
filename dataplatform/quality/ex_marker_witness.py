"""D7: BSE's own ex-event marker (`TDCLOINDI`) as an independent witness to `corporate_actions`.

``uv run python -m dataplatform.quality.ex_marker_witness --from 2016-09-01 --to 2024-07-05``

The BSE legacy bhavcopy prints a marker on the price row of an ex-date — `XD` ex-dividend, `XB`
ex-bonus, `SS` sub-division, `CS` consolidation, `XR` ex-rights, `SA` scheme of arrangement — and
the platform's corporate-action feed is built from different publications altogether (the BSE/NSE
corporate-action APIs). Two sources stating the same ex-date is corroboration; one stating it and
the other not is a lead worth reading. This module measures both directions:

* **marker → action**: of the markers in `price_session_attributes`, how many have a stored action
  of the matching type on the same ISIN (or the action's `filed_against_isin`) on the same date
  (`exact`), on the scrip's first BSE session after an ex-date that fell in a trading gap
  (`first_session`), within `near_days` calendar days (`near`), only of another type
  (`type_mismatch`), or none at all (`unmatched`);
* **action → marker**: of the stored actions of a marked type whose ISIN has a BSE price row on the
  ex-date — or, when it had none, on its first BSE session after it — how many carry the matching
  marker on that row (`witnessed`).

**Why `first_session`.** BSE prints the marker on the row where the new basis first trades, and a
consolidation suspends the scrip until the consolidated shares are credited. BSE's
corporate-action feed dates the event at the ex-date, the last day of the old basis' book, so for
VEERHEALTH the stored action is 2016-11-29, the last old-basis bar 2016-11-28, and the `CS` row
2017-01-13. No bar exists in between, so the two dates name the same boundary. A same-day
comparison scored all 34 `CS` markers of 2016-09..2024-07 `unmatched` on that alone. A match is
`first_session` only when the ISIN has *no* BSE row from the ex-date up to the marker, which needs
the scrip's sessions (`sessions=`), and only within `MAX_SUSPENSION_DAYS`.

What it never does: write a factor, a corporate action or a `quality_flag`. A disagreement here is
reported, not acted on — the marker is a witness, not an authority, and letting it move an
adjustment factor would make a label on a price row into a corporate-action source without any of
the review that source gets.
"""

from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Final

import duckdb

from dataplatform.config import get_settings
from dataplatform.ingest.session_attributes import read_session_attributes
from dataplatform.store.db import connection
from dataplatform.store.paths import Layer, layer_root
from dataplatform.store.schemas import PRICES_RAW_DATASET

__all__ = [
    "MARKER_ACTIONS",
    "MAX_SUSPENSION_DAYS",
    "ActionRecord",
    "MarkerRecord",
    "WitnessReport",
    "compare",
    "main",
]

#: Which stored `action_type`s each marker corroborates. `SS`/`CS` both change face value, which
#: the store files as `SPLIT` (a consolidation is a split with ratio < 1) or `FACE_VALUE_CHANGE`.
MARKER_ACTIONS: Final[Mapping[str, frozenset[str]]] = {
    "XD": frozenset({"DIVIDEND"}),
    "XB": frozenset({"BONUS"}),
    "SS": frozenset({"SPLIT", "FACE_VALUE_CHANGE"}),
    "CS": frozenset({"SPLIT", "FACE_VALUE_CHANGE"}),
    "XR": frozenset({"RIGHTS"}),
    "SA": frozenset({"SCHEME_OF_ARRANGEMENT", "DEMERGER", "MERGER"}),
}

#: The longest trading gap after an ex-date whose first session still counts as the ex-date's
#: own session. On the 2016-09..2024-07 `CS` markers the first session came 3-65 days after
#: the stored ex-date, so this leaves room; it stops a scrip that resumes years later from
#: matching an old action.
MAX_SUSPENSION_DAYS: Final = 120


@dataclass(frozen=True, slots=True)
class MarkerRecord:
    """One marked BSE price row: the ISIN it resolved to, its session and the marker."""

    isin: str
    trade_date: date
    marker: str


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """One stored corporate action, with the ISIN it was filed against when that differs."""

    isin: str
    ex_date: date
    action_type: str
    filed_against_isin: str | None = None


@dataclass(slots=True)
class WitnessReport:
    """Agreement counts per marker, both directions."""

    marker_to_action: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    action_to_marker: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))

    def agreement(self, marker: str | None = None) -> float:
        """Share of markers (one kind, or all) with a same-day matching action — `exact` rate."""
        return self._rate(marker, ("exact",))

    def matched(self, marker: str | None = None) -> float:
        """Share of markers matched on the action's own BSE session: `exact` + `first_session`."""
        return self._rate(marker, ("exact", "first_session"))

    def _rate(self, marker: str | None, outcomes: tuple[str, ...]) -> float:
        rows = (
            [self.marker_to_action[marker]]
            if marker is not None
            else list(self.marker_to_action.values())
        )
        total = sum(sum(c.values()) for c in rows)
        return sum(c[o] for c in rows for o in outcomes) / total if total else 0.0

    def as_json(self) -> dict[str, object]:
        """The report a gate note quotes."""

        def rate(counter: Counter[str], key: str) -> float:
            total = sum(counter.values())
            return counter[key] / total if total else 0.0

        return {
            "marker_to_action": {
                m: {
                    **dict(c),
                    "exact_rate": rate(c, "exact"),
                    "matched_rate": (
                        (c["exact"] + c["first_session"]) / sum(c.values())
                        if sum(c.values())
                        else 0.0
                    ),
                    "exact_or_near_rate": (
                        (c["exact"] + c["near"]) / sum(c.values()) if sum(c.values()) else 0.0
                    ),
                }
                for m, c in sorted(self.marker_to_action.items())
            },
            "action_to_marker": {
                t: {**dict(c), "witnessed_rate": rate(c, "witnessed")}
                for t, c in sorted(self.action_to_marker.items())
            },
            "overall_exact_rate": self.agreement(),
            "overall_matched_rate": self.matched(),
        }


def compare(
    markers: Iterable[MarkerRecord],
    actions: Iterable[ActionRecord],
    *,
    traded: Iterable[tuple[str, date]] = (),
    sessions: Mapping[str, Sequence[date]] | None = None,
    near_days: int = 3,
) -> WitnessReport:
    """Score markers against actions and actions against markers. Pure; reads nothing.

    `sessions` maps an ISIN to its BSE trading sessions. With it, a marker on the first session
    after an ex-date the scrip did not trade (a suspension) is `first_session`, and an action with
    no BSE row on its ex-date is probed on that first session; its sessions also count as
    `traded`. Without it, only same-day rows are compared, which is the behaviour that scored
    every consolidation unmatched.
    """
    first_after = _FirstSession(sessions or {})
    by_isin: dict[str, list[ActionRecord]] = defaultdict(list)
    action_list = list(actions)
    for action in action_list:
        by_isin[action.isin].append(action)
        if action.filed_against_isin and action.filed_against_isin != action.isin:
            by_isin[action.filed_against_isin].append(action)

    report = WitnessReport()
    marked: dict[tuple[str, date], set[str]] = defaultdict(set)
    for record in markers:
        marked[(record.isin, record.trade_date)].add(record.marker)
        wanted = MARKER_ACTIONS.get(record.marker)
        candidates = by_isin.get(record.isin, [])
        if wanted is None:
            report.marker_to_action[record.marker]["unknown_marker"] += 1
            continue
        same_day = [a for a in candidates if a.ex_date == record.trade_date]
        if any(a.action_type in wanted for a in same_day):
            outcome = "exact"
        elif any(
            a.action_type in wanted
            and a.ex_date < record.trade_date
            and first_after(record.isin, a.ex_date) == record.trade_date
            for a in candidates
        ):
            outcome = "first_session"
        elif any(
            a.action_type in wanted and abs((a.ex_date - record.trade_date).days) <= near_days
            for a in candidates
        ):
            outcome = "near"
        elif same_day:
            outcome = "type_mismatch"
        else:
            outcome = "unmatched"
        report.marker_to_action[record.marker][outcome] += 1

    traded_set = set(traded)
    traded_set.update((isin, d) for isin, days in (sessions or {}).items() for d in days)
    for action in action_list:
        kinds = {m for m, types in MARKER_ACTIONS.items() if action.action_type in types}
        if not kinds:
            continue
        isins = sorted({i for i in (action.isin, action.filed_against_isin) if i})
        probes = [(isin, action.ex_date) for isin in isins if (isin, action.ex_date) in traded_set]
        if not probes:
            later = ((isin, first_after(isin, action.ex_date)) for isin in isins)
            probes = [(isin, day) for isin, day in later if day is not None]
        if not probes:
            report.action_to_marker[action.action_type]["no_bse_row_that_day"] += 1
            continue
        seen = set().union(*(marked.get(probe, set()) for probe in probes))
        outcome = "witnessed" if seen & kinds else ("other_marker" if seen else "no_marker")
        report.action_to_marker[action.action_type][outcome] += 1
    return report


class _FirstSession:
    """`(isin, day)` → the ISIN's first BSE session on or after `day`, within the suspension cap."""

    def __init__(self, sessions: Mapping[str, Sequence[date]]) -> None:
        self._sessions = {isin: sorted(days) for isin, days in sessions.items()}

    def __call__(self, isin: str, day: date) -> date | None:
        days = self._sessions.get(isin, ())
        i = bisect_left(days, day)
        if i == len(days) or (days[i] - day).days > MAX_SUSPENSION_DAYS:
            return None
        return days[i]


def _bse_sessions(
    isins: Iterable[str], start: date, end: date, *, data_root: Path | None
) -> dict[str, list[date]]:
    """Each of `isins`' BSE `prices_raw` sessions in `[start, end]`, ascending."""
    root = layer_root(Layer.L1, data_root=data_root) / PRICES_RAW_DATASET
    files = sorted(
        str(f)
        for f in root.glob("date=*/part.parquet")
        if start.isoformat() <= f.parent.name.removeprefix("date=") <= end.isoformat()
    )
    wanted = sorted(set(isins))
    if not files or not wanted:
        return {}
    con = duckdb.connect()
    con.execute("CREATE TEMP TABLE want(isin VARCHAR)")
    con.executemany("INSERT INTO want VALUES (?)", [(i,) for i in wanted])
    rows = con.execute(
        "SELECT DISTINCT p.isin, p.trade_date FROM read_parquet($files) p "
        "JOIN want w ON p.isin = w.isin WHERE p.exchange = 'BSE' ORDER BY 1, 2",
        {"files": files},
    ).fetchall()
    out: dict[str, list[date]] = defaultdict(list)
    for isin, day in rows:
        out[str(isin)].append(day)
    return dict(out)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: read the markers from L1 and the actions from Postgres (read-only); print the report."""
    parser = argparse.ArgumentParser(prog="ex_marker_witness", description=__doc__)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=date(2016, 9, 1))
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=date(2024, 7, 5))
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    markers = [
        MarkerRecord(isin=str(r["isin"]), trade_date=r["trade_date"], marker=str(r["ex_marker"]))  # type: ignore[arg-type]
        for r in read_session_attributes(args.start, args.end, data_root=args.data_root)
        if r["exchange"] == "BSE" and r["ex_marker"]
    ]
    with connection(get_settings()) as conn:
        cur = conn.execute(
            "SELECT isin, ex_date, action_type, filed_against_isin FROM corporate_actions "
            "WHERE ex_date BETWEEN %s AND %s",
            (args.start - timedelta(days=7), args.end + timedelta(days=7)),
        )
        actions = [
            ActionRecord(str(r[0]), r[1], str(r[2]), str(r[3]) if r[3] else None)
            for r in cur.fetchall()
        ]
        conn.rollback()
    in_window = [a for a in actions if args.start <= a.ex_date <= args.end]
    isins = {m.isin for m in markers} | {
        isin for a in actions for isin in (a.isin, a.filed_against_isin) if isin
    }
    sessions = _bse_sessions(
        isins,
        args.start - timedelta(days=MAX_SUSPENSION_DAYS),
        args.end + timedelta(days=MAX_SUSPENSION_DAYS),
        data_root=args.data_root,
    )
    report = compare(markers, actions, sessions=sessions)
    # action → marker is scored only for actions inside the window the markers cover
    report.action_to_marker = compare(markers, in_window, sessions=sessions).action_to_marker
    payload = {"window": [args.start.isoformat(), args.end.isoformat()], "markers": len(markers)}
    payload.update(report.as_json())
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.report is not None:
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
