"""D7: BSE's own ex-event marker (`TDCLOINDI`) as an independent witness to `corporate_actions`.

``uv run python -m dataplatform.quality.ex_marker_witness --from 2016-09-01 --to 2024-07-05``

The BSE legacy bhavcopy prints a marker on the price row of an ex-date — `XD` ex-dividend, `XB`
ex-bonus, `SS` sub-division, `CS` consolidation, `XR` ex-rights, `SA` scheme of arrangement — and
the platform's corporate-action feed is built from different publications altogether (the BSE/NSE
corporate-action APIs). Two sources stating the same ex-date is corroboration; one stating it and
the other not is a lead worth reading. This module measures both directions:

* **marker → action**: of the markers in `price_session_attributes`, how many have a stored action
  of the matching type on the same ISIN (or the action's `filed_against_isin`) on the same date
  (`exact`), within `near_days` calendar days (`near`), only of another type (`type_mismatch`), or
  none at all (`unmatched`);
* **action → marker**: of the stored actions of a marked type whose ISIN has a BSE price row on the
  ex-date, how many carry the matching marker on that row (`witnessed`).

What it never does: write a factor, a corporate action or a `quality_flag`. A disagreement here is
reported, not acted on — the marker is a witness, not an authority, and letting it move an
adjustment factor would make a label on a price row into a corporate-action source without any of
the review that source gets.
"""

from __future__ import annotations

import argparse
import json
import sys
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
        rows = (
            [self.marker_to_action[marker]]
            if marker is not None
            else list(self.marker_to_action.values())
        )
        total = sum(sum(c.values()) for c in rows)
        return sum(c["exact"] for c in rows) / total if total else 0.0

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
        }


def compare(
    markers: Iterable[MarkerRecord],
    actions: Iterable[ActionRecord],
    *,
    traded: Iterable[tuple[str, date]] = (),
    near_days: int = 3,
) -> WitnessReport:
    """Score markers against actions and actions against markers. Pure; reads nothing."""
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
    for action in action_list:
        kinds = {m for m, types in MARKER_ACTIONS.items() if action.action_type in types}
        if not kinds:
            continue
        isins = {action.isin, action.filed_against_isin} - {None}
        if not any((isin, action.ex_date) in traded_set for isin in isins if isin):
            report.action_to_marker[action.action_type]["no_bse_row_that_day"] += 1
            continue
        seen = set().union(*(marked.get((isin, action.ex_date), set()) for isin in isins if isin))
        outcome = "witnessed" if seen & kinds else ("other_marker" if seen else "no_marker")
        report.action_to_marker[action.action_type][outcome] += 1
    return report


def _bse_rows_on(
    days: Sequence[tuple[str, date]], *, data_root: Path | None
) -> set[tuple[str, date]]:
    """Which `(isin, date)` pairs among `days` have a BSE `prices_raw` row."""
    root = layer_root(Layer.L1, data_root=data_root) / PRICES_RAW_DATASET
    wanted_dates = sorted({d for _, d in days})
    files = [root / f"date={d.isoformat()}" / "part.parquet" for d in wanted_dates]
    files = [f for f in files if f.is_file()]
    if not files:
        return set()
    con = duckdb.connect()
    con.execute("CREATE TEMP TABLE want(isin VARCHAR, d DATE)")
    con.executemany("INSERT INTO want VALUES (?, ?)", list(days))
    rows = con.execute(
        "SELECT DISTINCT p.isin, p.trade_date FROM read_parquet($files) p "
        "JOIN want w ON p.isin = w.isin AND p.trade_date = w.d WHERE p.exchange = 'BSE'",
        {"files": [str(f) for f in files]},
    ).fetchall()
    return {(str(r[0]), r[1]) for r in rows}


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
    probe = sorted(
        {(isin, a.ex_date) for a in in_window for isin in (a.isin, a.filed_against_isin) if isin}
    )
    traded = _bse_rows_on(probe, data_root=args.data_root)
    report = compare(markers, actions, traded=traded)
    # action → marker is scored only for actions inside the window the markers cover
    report.action_to_marker = compare(markers, in_window, traded=traded).action_to_marker
    payload = {"window": [args.start.isoformat(), args.end.isoformat()], "markers": len(markers)}
    payload.update(report.as_json())
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.report is not None:
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
