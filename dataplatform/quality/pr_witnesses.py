"""D7: gather the PR bundle's same-session move witnesses from L1, and measure what they explain.

Two halves. `read_move_witnesses` is the read the sentinel's caller makes to fill
`SentinelInput.move_witnesses` — `(isin, session)` → witness strings, from the three ISIN-keyed
PR-bundle datasets (`dataplatform.ingest.pr_bundle_l1`):

* `pr_security_marks.corp_ind` → `corp_ind:<code>`
* `pr_band_hits.side`          → `band_hit:H` / `band_hit:L`
* `pr_ca_broadcasts.ex_date`   → `ca_broadcast:<TAG>` on the ex-date session, one per coarse
                                 purpose tag (`pr_bundle.survey.purpose_tags`; `OTHER` if none)

`measure` is the read-only study the 2026-10-06 inventory quotes: every NSE close-to-close move
in `prices_raw` beyond the sentinel's threshold, minus those a `corporate_actions` ex-date
explains (the move rule's own test), run through `UnexplainedMoveRule` and `MoveWitnessRule` —
so the counts are the rules' own verdicts, not a re-implementation. It writes nothing: no
`quality_flag` row is raised, annotated or resolved.

Point in time: every witness is from the session's own bundle (`publication_date == session`
for `Pd`/`bh`); a `Bc` broadcast is counted as a witness only if it was knowable on or before
the session it explains.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb

from dataplatform.ingest.nse.pr_bundle.survey import purpose_tags
from dataplatform.ingest.pr_bundle_l1 import (
    BAND_HITS_DATASET,
    CA_BROADCASTS_DATASET,
    SECURITY_MARKS_DATASET,
)
from dataplatform.quality.rules.move_witness import MoveWitnessRule, best_strength
from dataplatform.quality.rules.unexplained_move import (
    DEFAULT_MOVE_THRESHOLD,
    UnexplainedMoveRule,
)
from dataplatform.quality.sentinel import CloseToCloseMove, SentinelInput
from dataplatform.store.paths import Layer, layer_root

__all__ = ["measure", "read_move_witnesses"]


def _glob(dataset: str, data_root: Path | None) -> str:
    return str(layer_root(Layer.L1, data_root=data_root) / dataset / "date=*" / "*.parquet")


def read_move_witnesses(
    start: date, end: date, *, data_root: Path | None = None
) -> dict[tuple[str, date], tuple[str, ...]]:
    """`(isin, session)` → sorted witness strings for sessions in `[start, end]`.

    What it never does: admit a broadcast knowable after the session it would explain.
    """
    found: dict[tuple[str, date], set[str]] = defaultdict(set)
    con = duckdb.connect()
    try:
        queries = (
            (
                SECURITY_MARKS_DATASET,
                "SELECT isin, session, 'corp_ind:' || corp_ind FROM read_parquet($g) "
                "WHERE corp_ind IS NOT NULL AND session BETWEEN $s AND $e",
            ),
            (
                BAND_HITS_DATASET,
                "SELECT isin, session, 'band_hit:' || side FROM read_parquet($g) "
                "WHERE session BETWEEN $s AND $e",
            ),
            (
                CA_BROADCASTS_DATASET,
                "SELECT DISTINCT isin, ex_date, purpose FROM read_parquet($g) "
                "WHERE ex_date BETWEEN $s AND $e AND knowable_date <= ex_date",
            ),
        )
        for dataset, sql in queries:
            if not any(
                (layer_root(Layer.L1, data_root=data_root) / dataset).glob("date=*/*.parquet")
            ):
                continue  # dataset not built: no witnesses, rather than a DuckDB glob error
            glob = _glob(dataset, data_root)
            for isin, session, value in con.execute(
                sql, {"g": glob, "s": start, "e": end}
            ).fetchall():
                if dataset == CA_BROADCASTS_DATASET:
                    tags = purpose_tags(str(value)) or ("OTHER",)
                    found[(str(isin), session)].update(f"ca_broadcast:{tag}" for tag in tags)
                else:
                    found[(str(isin), session)].add(str(value))
    finally:
        con.close()
    return {key: tuple(sorted(value)) for key, value in found.items()}


def measure(
    *,
    start: date,
    end: date,
    ca_ex_dates: Mapping[str, set[date]],
    data_root: Path | None = None,
    threshold: Decimal = DEFAULT_MOVE_THRESHOLD,
) -> dict[str, object]:
    """Would-be `unexplained_move` flags over NSE `prices_raw`, and how many a witness bears on.

    `ca_ex_dates` is ISIN → ex-dates of the corporate actions the move rule may use. Returns a
    JSON-safe summary: totals, per-year and per-series counts, witness kinds and strengths.
    """
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT isin, trade_date, series, prev_close, close FROM read_parquet($g) "
            "WHERE exchange = 'NSE' AND prev_close > 0 AND close > 0 "
            "AND trade_date BETWEEN $s AND $e "
            "AND abs(close / prev_close - 1) > $t",
            {"g": _glob("prices_raw", data_root), "s": start, "e": end, "t": threshold},
        ).fetchall()
    finally:
        con.close()
    series_of: dict[tuple[str, date], set[str]] = defaultdict(set)
    moves: list[CloseToCloseMove] = []
    ca_explained = 0
    for isin, session, series, prev_close, close in rows:
        if session in ca_ex_dates.get(str(isin), set()):
            ca_explained += 1
            continue
        series_of[(str(isin), session)].add(str(series))
        moves.append(
            CloseToCloseMove(
                isin=str(isin),
                date=session,
                prev_close=Decimal(prev_close),
                close=Decimal(close),
                source="prices_raw",
            )
        )
    witnesses = read_move_witnesses(start, end, data_root=data_root)
    data = SentinelInput(moves=tuple(moves), move_witnesses=witnesses)
    flagged = list(UnexplainedMoveRule(threshold=threshold).evaluate(data))
    annotated = list(MoveWitnessRule(threshold=threshold).evaluate(data))

    by_year: dict[int, Counter[str]] = defaultdict(Counter)
    by_series: Counter[str] = Counter()
    for finding in flagged:
        assert finding.isin is not None
        by_year[finding.logical_date.year]["flagged"] += 1
        by_series.update(series_of[(finding.isin, finding.logical_date)])
    kinds: Counter[str] = Counter()
    strength: Counter[str] = Counter()
    eq_annotated: Counter[str] = Counter()
    for finding in annotated:
        assert finding.isin is not None
        found = [str(w) for w in finding.detail["witnesses"]]  # type: ignore[attr-defined]
        best = best_strength(found)
        strength[best] += 1
        by_year[finding.logical_date.year][f"witness_{best}"] += 1
        if "EQ" in series_of[(finding.isin, finding.logical_date)]:
            eq_annotated[best] += 1
        kinds.update(found)
    return {
        "range": [start.isoformat(), end.isoformat()],
        "threshold": str(threshold),
        "moves_beyond_threshold": len(rows),
        "explained_by_corporate_actions_table": ca_explained,
        "would_be_unexplained_move_flags": len(flagged),
        "flags_eq": by_series["EQ"],
        "flags_with_any_witness": len(annotated),
        "flags_by_best_witness": dict(strength),
        "eq_flags_by_best_witness": dict(eq_annotated),
        "witness_kinds": dict(kinds.most_common()),
        "flags_by_series_top": dict(by_series.most_common(15)),
        "by_year": {str(y): dict(c) for y, c in sorted(by_year.items())},
    }


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m dataplatform.quality.pr_witnesses --data-root … --from … --to … --out …`."""
    from dataplatform.config import get_settings
    from dataplatform.store.db import connection

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--reconciled-only",
        action="store_true",
        help="explain moves only by reconciled corporate actions (the rule's documented input)",
    )
    args = parser.parse_args(argv)

    ex_dates: dict[str, set[date]] = defaultdict(set)
    with connection(get_settings()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT isin, ex_date FROM corporate_actions"
            + (" WHERE reconciled" if args.reconciled_only else "")
        )
        for isin, ex_date in cur.fetchall():
            ex_dates[str(isin)].add(ex_date)
    summary = measure(
        start=args.start, end=args.end, ca_ex_dates=ex_dates, data_root=args.data_root
    )
    summary["corporate_actions_scope"] = "reconciled" if args.reconciled_only else "all"
    args.out.write_text(json.dumps(summary, indent=1, sort_keys=True))
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
