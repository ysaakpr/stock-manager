"""D2: ISIN lineage — the edge an NSE reissue leaves behind, derived from L1 itself.

An Indian equity ISIN is `IN` + `E` + a 4-character issuer code + a 2-digit security type + a
2-digit issue serial + a check digit. A face-value split usually *retires* the ISIN: the issuer
code survives, the serial increments, the check digit changes. GRASIM trades as INE047A0101(3) to
2016-10-06 and INE047A0102(1) from 2016-10-07; IRCTC as INE335Y0101(2) to 2021-10-28 and
INE335Y0102(0) from 2021-10-29.

Nothing else in the platform can see that those are one company. `corporate_actions.isin`
references `security_master`, the retired ISIN was never written there, and so the split is refused
at ingest — no factor, no L2 partition, and a momentum signal that reads a 2:1 split as a genuine
~-50% twelve-month return. This module supplies the missing edge.

**What it does.** Derives one directed edge per reissue from L1 price contiguity alone: within an
issuer code, one equity ISIN's traded span ends and the next one's begins. Corroborates an edge
where a SPLIT or BONUS in the L0 corporate-action payloads falls on the successor's first session,
and records the rest as DERIVED rather than dropping them.

**What it assumes.** L1 `prices_raw` is populated (the derivation is a pure function of it), and
`security_master` holds the *successor* — the survivor is a live security. It does not assume the
predecessor is known; that absence is the whole point.

**What it never does.** It never resolves a symbol — the join key is the ISIN the row carries
(invariant #2). It never invents an edge across issuer codes: a merger or a rename to a different
issuer is not a reissue and is out of scope. And it never treats a lineage as a merge — one
predecessor has exactly one successor, so a demerger cannot be forced through this edge.
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Final

import duckdb

from dataplatform.clock import Clock, SystemClock
from dataplatform.corpactions.parse_terms import classify
from dataplatform.corpactions.taxonomy import ActionType
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection
from dataplatform.store.l2 import open_connection, register_raw_view

__all__ = [
    "CORROBORATING_TYPES",
    "IsinSpan",
    "LineageEdge",
    "LineageResolver",
    "LineageStore",
    "derive_edges",
    "read_corroboration",
    "read_equity_spans",
]

_LOG = get_logger(__name__)

#: The L0 dataset the corroborating actions are read from.
_CA_DATASET: Final = "nse_corp_actions"

#: Only a SPLIT or a BONUS re-bases the share count, and only those explain a reissue. A dividend
#: on the effective date is a coincidence, not an explanation, and must not promote an edge.
CORROBORATING_TYPES: Final[frozenset[ActionType]] = frozenset({ActionType.SPLIT, ActionType.BONUS})

#: `INE` is an equity share. `INF` is a mutual-fund unit: an AMC issues dozens of ETFs under one
#: issuer code, so grouping those by issuer would splice unrelated funds into one "lineage" —
#: measured, it produced 232 spurious overlapping edges before this filter.
_EQUITY_PREFIX: Final = "INE"

#: Characters 8-9 of the ISIN are the security type. `01` is an equity share; `07` and `08` are
#: debentures, which trade under the same NSE symbol as the equity and overlap it in time rather
#: than succeeding it.
_EQUITY_SECURITY_TYPE: Final = "01"

#: The issuer code, `IN` + `E` + four characters. Two ISINs sharing it are the same issuer.
_ISSUER_PREFIX_LEN: Final = 7


@dataclass(frozen=True, slots=True)
class IsinSpan:
    """One ISIN's traded extent in L1: when it first and last carried a price."""

    isin: str
    first_date: date
    last_date: date
    first_symbol: str

    @property
    def issuer(self) -> str:
        """The issuer code these ISINs share across a reissue."""
        return self.isin[:_ISSUER_PREFIX_LEN]


@dataclass(frozen=True, slots=True)
class LineageEdge:
    """One reissue: `predecessor_isin` stopped trading, `successor_isin` took over.

    `gap_sessions` is the number of trading sessions between the two spans — 0 for a seamless
    handover, which is the overwhelming majority. It is kept rather than thresholded away because
    a large gap is the one signal that separates a genuine reissue from an issuer-code reuse.
    """

    predecessor_isin: str
    successor_isin: str
    effective_date: date
    gap_sessions: int
    symbol_at_change: str
    corroborating_action: ActionType | None

    @property
    def confidence(self) -> str:
        """`CORROBORATED` when an action explains the reissue, else `DERIVED`."""
        return "CORROBORATED" if self.corroborating_action is not None else "DERIVED"


def read_equity_spans(
    *, con: duckdb.DuckDBPyConnection | None = None, data_root: Path | None = None
) -> tuple[tuple[IsinSpan, ...], tuple[date, ...]]:
    """Read every equity ISIN's traded span out of L1, with the session calendar to measure gaps.

    Returns `(spans, sessions)`. Spans cover `INE…01…` ISINs only — equity shares — but every
    series, not just `EQ`: the question here is when the *security* existed, and a name that spent
    its last weeks in trade-to-trade (`BE`) before the reissue still traded. Sessions are every
    distinct trade date in L1, which is what turns a calendar gap into a count of missed sessions.
    """
    owns = con is None
    con = open_connection() if con is None else con
    try:
        register_raw_view(con, view="prices_raw", data_root=data_root)
        rows = con.execute(
            "SELECT isin, min(trade_date), max(trade_date), arg_min(symbol, trade_date) "
            "FROM prices_raw "
            "WHERE substr(isin, 1, 3) = $prefix AND substr(isin, 8, 2) = $sec_type "
            "GROUP BY isin ORDER BY isin",
            {"prefix": _EQUITY_PREFIX, "sec_type": _EQUITY_SECURITY_TYPE},
        ).fetchall()
        sessions = con.execute(
            "SELECT DISTINCT trade_date FROM prices_raw ORDER BY trade_date"
        ).fetchall()
    finally:
        if owns:
            con.close()
    spans = tuple(IsinSpan(r[0], r[1], r[2], r[3]) for r in rows)
    _LOG.info("lineage.spans_read", spans=len(spans), sessions=len(sessions))
    return spans, tuple(s[0] for s in sessions)


def read_corroboration(*, data_root: Path | None = None) -> Mapping[tuple[str, date], ActionType]:
    """Map `(isin, ex_date)` to SPLIT or BONUS, read from the L0 corporate-action payloads.

    Reads L0 rather than `corporate_actions` deliberately: the rows that corroborate a reissue are
    filed against the ISIN being retired, so they are exactly the rows the table's foreign key
    refused. L0 is immutable and holds them all (invariant #1).

    Classification goes through `corpactions.parse_terms.classify`, the same tested subject→type
    mapping the ingest path uses; a subject naming two types is ambiguous and is skipped rather
    than guessed at. A row whose ex-date is not a real date is skipped for the same reason.
    """
    root = (Path("data") if data_root is None else data_root) / "L0" / _CA_DATASET
    found: dict[tuple[str, date], ActionType] = {}
    files = sorted(p for p in root.glob("*/*/*.json") if not p.name.endswith(".meta.json"))
    for path in files:
        for record in json.loads(path.read_text()):
            isin, raw_ex = record.get("isin"), record.get("exDate")
            if not isin or not raw_ex:
                continue
            types = [t for t in classify(record.get("subject") or "") if t in CORROBORATING_TYPES]
            if len(types) != 1:
                continue  # unclassifiable, or ambiguous between two types — not evidence
            try:
                ex_date = datetime.strptime(raw_ex, "%d-%b-%Y").date()
            except ValueError:
                continue
            found.setdefault((isin, ex_date), types[0])
    _LOG.info("lineage.corroboration_read", files=len(files), actions=len(found))
    return found


def derive_edges(
    spans: Iterable[IsinSpan],
    sessions: Sequence[date],
    corroboration: Mapping[tuple[str, date], ActionType],
) -> tuple[LineageEdge, ...]:
    """Derive one edge per reissue from spans alone — a pure function, no I/O.

    Within an issuer code, spans are ordered by first trade date and each consecutive pair becomes
    a candidate. A pair is an edge only when the predecessor's span *ends before* the successor's
    begins: two ISINs trading at the same time are not a succession (a company with two live share
    classes, most often), and admitting them would splice concurrent price histories.

    An edge is CORROBORATED when a SPLIT or BONUS is filed against the **predecessor** — the
    retired ISIN is the one the exchange names — on either side of the boundary. NSE dates these
    two ways and both label the same event: measured over the lake, 176 edges carry the action on
    the successor's first session and 115 on the predecessor's last, IRCTC's 2021 split among the
    latter (`exDate` 28-Oct, new ISIN live 29-Oct). Matching only one convention would have left
    the canonical case DERIVED. The window is exactly those two sessions — widening it further
    starts admitting unrelated actions.
    """
    by_issuer: dict[str, list[IsinSpan]] = {}
    for span in spans:
        by_issuer.setdefault(span.issuer, []).append(span)

    edges: list[LineageEdge] = []
    for issuer_spans in by_issuer.values():
        ordered = sorted(issuer_spans, key=lambda s: (s.first_date, s.isin))
        for earlier, later in pairwise(ordered):
            if earlier.last_date >= later.first_date:
                continue  # concurrent, not sequential — not a reissue
            edges.append(
                LineageEdge(
                    predecessor_isin=earlier.isin,
                    successor_isin=later.isin,
                    effective_date=later.first_date,
                    gap_sessions=_sessions_between(sessions, earlier.last_date, later.first_date),
                    symbol_at_change=later.first_symbol,
                    corroborating_action=(
                        corroboration.get((earlier.isin, later.first_date))
                        or corroboration.get((earlier.isin, earlier.last_date))
                    ),
                )
            )
    edges.sort(key=lambda e: (e.effective_date, e.predecessor_isin))
    _LOG.info(
        "lineage.derived",
        edges=len(edges),
        corroborated=sum(1 for e in edges if e.corroborating_action is not None),
        seamless=sum(1 for e in edges if e.gap_sessions == 0),
    )
    return tuple(edges)


def _sessions_between(sessions: Sequence[date], after: date, before: date) -> int:
    """Trading sessions strictly between two dates, from the L1 session calendar."""
    return max(0, bisect_left(sessions, before) - bisect_right(sessions, after))


class LineageStore:
    """Reads and writes `isin_lineage`.

    The derivation is a pure function of L1, so a rebuild *replaces* the derived rows wholesale
    rather than merging: a re-derivation that no longer sees an edge means L1 no longer supports
    it, and leaving the stale row behind would make the table a union of every past belief.
    Rows recorded by hand (`detected_by = 'MANUAL'`) are never touched by a rebuild.
    """

    def __init__(self, conn: Connection, *, clock: Clock | None = None) -> None:
        self._conn = conn
        self._clock: Clock = SystemClock() if clock is None else clock

    def replace_derived(self, edges: Iterable[LineageEdge]) -> int:
        """Replace every `L1_CONTIGUITY` row with `edges`; return the number written.

        An edge whose successor is absent from `security_master` is skipped, not raised on: L1 can
        hold a name the identity master has not ingested yet, and one such name must not cost the
        other 400 edges. The count of skips is logged.
        """
        rows = tuple(edges)
        self._conn.execute("DELETE FROM isin_lineage WHERE detected_by = 'L1_CONTIGUITY'")
        known = {r[0] for r in self._conn.execute("SELECT isin FROM security_master").fetchall()}
        writable = [e for e in rows if e.successor_isin in known]
        skipped = len(rows) - len(writable)
        # One statement over unnested arrays, the same shape `IdentityStore` writes the master
        # with: 400-odd edges as 400 round trips would be the only slow part of a rebuild.
        self._conn.execute(
            """
            INSERT INTO isin_lineage (predecessor_isin, successor_isin, effective_date,
                                      detected_by, confidence, gap_sessions, symbol_at_change,
                                      corroborating_action, computed_at)
            SELECT t.predecessor, t.successor, t.effective, 'L1_CONTIGUITY', t.confidence,
                   t.gap, t.symbol, t.action, %(now)s::timestamptz
              FROM unnest(%(predecessor)s::text[], %(successor)s::text[], %(effective)s::date[],
                          %(confidence)s::text[], %(gap)s::int[], %(symbol)s::text[],
                          %(action)s::text[])
                AS t(predecessor, successor, effective, confidence, gap, symbol, action)
            """,
            {
                "now": self._clock.now(),
                "predecessor": [e.predecessor_isin for e in writable],
                "successor": [e.successor_isin for e in writable],
                "effective": [e.effective_date for e in writable],
                "confidence": [e.confidence for e in writable],
                "gap": [e.gap_sessions for e in writable],
                "symbol": [e.symbol_at_change for e in writable],
                "action": [
                    None if e.corroborating_action is None else e.corroborating_action.value
                    for e in writable
                ],
            },
        )
        _LOG.info(
            "lineage.written",
            written=len(writable),
            skipped_unknown_successor=skipped,
            corroborated=sum(1 for e in writable if e.corroborating_action is not None),
        )
        return len(writable)

    def load(self) -> LineageResolver:
        """Read every edge back as a resolver."""
        rows = self._conn.execute(
            "SELECT predecessor_isin, successor_isin, effective_date FROM isin_lineage"
        ).fetchall()
        return LineageResolver({r[0]: (r[1], r[2]) for r in rows})


class LineageResolver:
    """Walks the lineage: which ISIN a retired one became, and what history a survivor inherits.

    Both directions are needed and neither is one hop. The factor chain resolving a corporate
    action has a retired ISIN and wants the survivor at the far end of the chain; the L2 stitch has
    a survivor and wants every ISIN whose bars belong to it, oldest first.
    """

    def __init__(self, forward: Mapping[str, tuple[str, date]]) -> None:
        self._forward = dict(forward)
        self._backward: dict[str, list[tuple[str, date]]] = {}
        for predecessor, (successor, effective) in self._forward.items():
            self._backward.setdefault(successor, []).append((predecessor, effective))

    def survivor_of(self, isin: str) -> str:
        """The ISIN this one ultimately became — itself when it was never reissued.

        Follows the chain to its end, so a name reissued twice (A→B→C) resolves A straight to C.
        A cycle would mean the derivation produced a contradiction; it is broken rather than
        looped forever, returning the last ISIN before the repeat.
        """
        seen = {isin}
        current = isin
        while (nxt := self._forward.get(current)) is not None:
            if nxt[0] in seen:
                _LOG.warning("lineage.cycle", isin=isin, at=current)
                return current
            current = nxt[0]
            seen.add(current)
        return current

    def chain_to(self, isin: str) -> tuple[str, ...]:
        """Every ISIN whose price history belongs to `isin`, oldest first, ending in `isin`.

        This is what L2 splices: `('INE335Y01012', 'INE335Y01020')` for IRCTC, so the adjusted
        series spans the 2021-10-29 reissue instead of starting there.
        """
        chain: list[str] = []
        frontier = [isin]
        seen = {isin}
        while frontier:
            current = frontier.pop()
            for predecessor, _ in self._backward.get(current, ()):
                if predecessor in seen:
                    continue
                seen.add(predecessor)
                chain.append(predecessor)
                frontier.append(predecessor)
        return (*sorted(chain), isin)

    def effective_date(self, predecessor: str) -> date | None:
        """The session `predecessor`'s successor first traded, or None if it was never reissued."""
        found = self._forward.get(predecessor)
        return None if found is None else found[1]

    def __len__(self) -> int:
        return len(self._forward)
