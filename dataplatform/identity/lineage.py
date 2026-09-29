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
`security_master` holds the *survivor* at the end of each chain — a live security. It does not
assume the predecessor is known; that absence is the whole point. Nor does it assume the middle of
a chain is known: an ISIN issued by one reissue and retired by the next (BAJFINANCE's INE296A01024,
2016-09-09 to 2025-06-13) is in no current snapshot, and `LineageStore.replace_derived` registers
it as a DELISTED master row from its own L1 EQ rows rather than drop the edge that names it.

**What it never does.** It never resolves a symbol — the join key is the ISIN the row carries
(invariant #2). It never invents an edge across issuer codes: a merger or a rename to a different
issuer is not a reissue and is out of scope. And it never treats a lineage as a merge — one
predecessor has exactly one successor, so a demerger cannot be forced through this edge.
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Final

import duckdb

from dataplatform.clock import Clock, SystemClock
from dataplatform.corpactions.parse_terms import classify, split_compound
from dataplatform.corpactions.taxonomy import ActionType
from dataplatform.identity.master import Exchange, IdentityStore, ListingStatus, Security
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection
from dataplatform.store.l2 import open_connection, register_raw_view

__all__ = [
    "CORROBORATING_TYPES",
    "LINEAGE_BACKFILL",
    "EqPresence",
    "IsinSpan",
    "LineageEdge",
    "LineageResolver",
    "LineageStore",
    "LineageWriteReport",
    "RegistrationPlan",
    "SkipReason",
    "corroborating_type",
    "derive_edges",
    "plan_registrations",
    "read_corroboration",
    "read_eq_presence",
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

#: The exchange whose L1 rows are span evidence. Only the NSE bhavcopy prints the ISIN as of each
#: session; BSE legacy rows carry a scrip-master ISIN assigned today (see `read_equity_spans`).
_SPAN_EXCHANGE: Final = "NSE"

#: The `security_master.registered_by` marker (0011) on a row this module inferred from L1.
LINEAGE_BACKFILL: Final = "lineage_backfill"


class SkipReason(StrEnum):
    """Why a derived edge was not written — each one a different fix, so each is counted apart."""

    #: The successor is the end of its chain and no snapshot lists it. The survivor of a live
    #: company is in the current snapshot, so this is an identity-refresh gap, not a lineage one,
    #: and registering it from L1 would have to guess whether it is ACTIVE, SUSPENDED or DELISTED.
    TERMINAL_SUCCESSOR_NOT_IN_MASTER = "terminal_successor_not_in_master"
    #: The successor is a retired middle of a chain, but has no NSE EQ bar in L1 to register it
    #: from. An ISIN with no evidence is never invented.
    NO_L1_EQ_HISTORY = "no_l1_eq_history"
    #: The successor is a retired middle, but the chain past it never reaches a security the
    #: master knows or could register — there is no survivor for it to hand its history to.
    CHAIN_SURVIVOR_NOT_IN_MASTER = "chain_survivor_not_in_master"


@dataclass(frozen=True, slots=True)
class EqPresence:
    """One ISIN's NSE EQ extent in L1 — the evidence a retired intermediate is registered from."""

    isin: str
    first_date: date
    last_date: date
    last_symbol: str


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
    distinct NSE trade date in L1, which turns a calendar gap into a count of missed sessions.

    NSE rows only, and not because BSE is uninteresting: a span is evidence of when the *source*
    printed the ISIN, and only the NSE bhavcopy prints it as of each session. The BSE legacy
    bhavcopy (before the 2024-07 UDiFF cutover) carries no ISIN; its rows reach L1 labelled with
    the ISIN the scrip master holds today — for a reissued security, the successor — so on a lake
    holding both exchanges the successor appears to trade from the original listing, overlaps the
    predecessor, and the derivation drops the reissue as concurrent. Measured on the server on
    2026-09-07: IRCTC's INE335Y01020 on BSE from 2019-10-14, two years before NSE issued it.
    """
    owns = con is None
    con = open_connection() if con is None else con
    try:
        register_raw_view(con, view="prices_raw", data_root=data_root)
        rows = con.execute(
            "SELECT isin, min(trade_date), max(trade_date), arg_min(symbol, trade_date) "
            "FROM prices_raw "
            "WHERE exchange = $exchange "
            "AND substr(isin, 1, 3) = $prefix AND substr(isin, 8, 2) = $sec_type "
            "GROUP BY isin ORDER BY isin",
            {
                "exchange": _SPAN_EXCHANGE,
                "prefix": _EQUITY_PREFIX,
                "sec_type": _EQUITY_SECURITY_TYPE,
            },
        ).fetchall()
        sessions = con.execute(
            "SELECT DISTINCT trade_date FROM prices_raw WHERE exchange = $exchange "
            "ORDER BY trade_date",
            {"exchange": _SPAN_EXCHANGE},
        ).fetchall()
    finally:
        if owns:
            con.close()
    spans = tuple(IsinSpan(r[0], r[1], r[2], r[3]) for r in rows)
    _LOG.info("lineage.spans_read", spans=len(spans), sessions=len(sessions))
    return spans, tuple(s[0] for s in sessions)


def read_eq_presence(
    isins: Iterable[str],
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    data_root: Path | None = None,
) -> Mapping[str, EqPresence]:
    """Each named ISIN's NSE `EQ` extent and last symbol in L1; an ISIN with no such row is absent.

    `EQ` rather than every series, unlike `read_equity_spans`: a span only has to show the ISIN
    existed, but a row registered from this is a security the L2 materializer will build bars
    for, and it builds from `EQ` alone. NSE rows only, for `read_equity_spans`'s reason — a BSE
    legacy row carries the ISIN today's scrip master assigns, not the one of its session. Keyed by
    the ISIN each row carries; no symbol is ever resolved here (invariant #2).
    """
    wanted = sorted(set(isins))
    if not wanted:
        return {}
    owns = con is None
    con = open_connection() if con is None else con
    try:
        register_raw_view(con, view="prices_raw", data_root=data_root)
        rows = con.execute(
            "SELECT isin, min(trade_date), max(trade_date), arg_max(symbol, trade_date) "
            "FROM prices_raw "
            "WHERE exchange = $exchange AND series = 'EQ' AND list_contains($isins, isin) "
            "GROUP BY isin ORDER BY isin",
            {"exchange": _SPAN_EXCHANGE, "isins": wanted},
        ).fetchall()
    finally:
        if owns:
            con.close()
    _LOG.info("lineage.eq_presence_read", asked=len(wanted), present=len(rows))
    return {r[0]: EqPresence(r[0], r[1], r[2], r[3]) for r in rows}


def corroborating_type(subject: str) -> ActionType | None:
    """The reissue-making event one feed line names — SPLIT or BONUS — or None.

    Each segment of a compound line (`split_compound`) is classified on its own: `Bonus 1:5/Face
    Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share` names a bonus *and*
    a split, and the split is what reissued the ISIN, so SPLIT wins when both are present. A single
    segment that names two types is ambiguous and is not evidence; a line naming neither is None.
    Until 2026-09-07 the whole line went through `classify` at once, so every compound line was
    skipped and its edge recorded as DERIVED (BEARDSELL's 2017 reissue among them).
    """
    named: set[ActionType] = set()
    for segment in split_compound(subject):
        types = [t for t in classify(segment) if t in CORROBORATING_TYPES]
        if len(types) == 1:
            named.add(types[0])
    if ActionType.SPLIT in named:
        return ActionType.SPLIT
    if ActionType.BONUS in named:
        return ActionType.BONUS
    return None


def read_corroboration(*, data_root: Path | None = None) -> Mapping[tuple[str, date], ActionType]:
    """Map `(isin, ex_date)` to SPLIT or BONUS, read from the L0 corporate-action payloads.

    Reads L0 rather than `corporate_actions` deliberately: the rows that corroborate a reissue are
    filed against the ISIN being retired, so they are exactly the rows the table's foreign key
    refused. L0 is immutable and holds them all (invariant #1).

    Classification is `corroborating_type`: the same tested subject→type mapping the ingest path
    uses, one segment of a compound line at a time. A row whose ex-date is not a real date is
    skipped rather than guessed at.
    """
    root = (Path("data") if data_root is None else data_root) / "L0" / _CA_DATASET
    found: dict[tuple[str, date], ActionType] = {}
    files = sorted(p for p in root.glob("*/*/*.json") if not p.name.endswith(".meta.json"))
    for path in files:
        for record in json.loads(path.read_text()):
            isin, raw_ex = record.get("isin"), record.get("exDate")
            if not isin or not raw_ex:
                continue
            action = corroborating_type(record.get("subject") or "")
            if action is None:
                continue  # unclassifiable, or ambiguous within one segment — not evidence
            try:
                ex_date = datetime.strptime(raw_ex, "%d-%b-%Y").date()
            except ValueError:
                continue
            found.setdefault((isin, ex_date), action)
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


@dataclass(frozen=True, slots=True)
class RegistrationPlan:
    """Which unknown successors to register, which edges to write, and which stay skipped."""

    register: tuple[Security, ...]
    writable: tuple[LineageEdge, ...]
    skipped: tuple[tuple[LineageEdge, SkipReason], ...]


def plan_registrations(
    edges: Iterable[LineageEdge],
    known: Container[str],
    presence: Mapping[str, EqPresence],
) -> RegistrationPlan:
    """Decide, without I/O, which unknown successors to register and which edges that unblocks.

    An unknown successor is registered only when it is a chain's *retired middle*: it is itself
    the predecessor of a derived edge (so a reissue retired it — DELISTED is a fact, not a guess),
    it has NSE EQ bars in L1 to be registered from, and the chain past it reaches a security the
    master knows, through intermediates that qualify the same way. A chain end the master does not
    know is not registered (`TERMINAL_SUCCESSOR_NOT_IN_MASTER`), and nothing without L1 evidence
    ever is (`NO_L1_EQ_HISTORY`). Every edge whose successor is known or registered is writable;
    every other edge is returned with its reason, so the count of skips is never a guess.
    """
    rows = tuple(edges)
    forward = {e.predecessor_isin: e.successor_isin for e in rows}
    verdict: dict[str, SkipReason | None] = {}

    def resolve(isin: str, trail: frozenset[str]) -> SkipReason | None:
        """None when `isin` is known or registrable, else why not. Memoised; cycle-safe."""
        if isin in known:
            return None
        if isin in verdict:
            return verdict[isin]
        nxt = forward.get(isin)
        reason: SkipReason | None
        if nxt is None:
            reason = SkipReason.TERMINAL_SUCCESSOR_NOT_IN_MASTER
        elif isin not in presence:
            reason = SkipReason.NO_L1_EQ_HISTORY
        elif nxt in trail or resolve(nxt, trail | {isin}) is not None:
            reason = SkipReason.CHAIN_SURVIVOR_NOT_IN_MASTER
        else:
            reason = None
        verdict[isin] = reason
        return reason

    writable: list[LineageEdge] = []
    skipped: list[tuple[LineageEdge, SkipReason]] = []
    register: dict[str, Security] = {}
    for edge in rows:
        reason = resolve(edge.successor_isin, frozenset())
        if reason is not None:
            skipped.append((edge, reason))
            continue
        writable.append(edge)
        if edge.successor_isin not in known and edge.successor_isin not in register:
            seen = presence[edge.successor_isin]
            register[edge.successor_isin] = Security(
                isin=seen.isin,
                name=seen.last_symbol,
                primary_exchange=Exchange.NSE,
                status=ListingStatus.DELISTED,
                first_seen_date=seen.first_date,
                last_seen_date=seen.last_date,
            )
    return RegistrationPlan(
        register=tuple(register[i] for i in sorted(register)),
        writable=tuple(writable),
        skipped=tuple(skipped),
    )


@dataclass(frozen=True, slots=True)
class LineageWriteReport:
    """What one `replace_derived` did: every derived edge is written or skipped with a reason."""

    derived: int
    written: int
    registered_intermediates: tuple[str, ...]
    skipped: tuple[tuple[LineageEdge, SkipReason], ...]

    @property
    def still_skipped(self) -> int:
        """Edges not written."""
        return len(self.skipped)

    def skipped_by_reason(self) -> dict[str, int]:
        """Skip counts per `SkipReason` value, every reason present (zero when none)."""
        counts = {r.value: 0 for r in SkipReason}
        for _, reason in self.skipped:
            counts[reason.value] += 1
        return counts


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

    def replace_derived(
        self,
        edges: Iterable[LineageEdge],
        *,
        presence: Mapping[str, EqPresence] | None = None,
    ) -> LineageWriteReport:
        """Replace every `L1_CONTIGUITY` row with `edges`; report what was written and skipped.

        A successor absent from `security_master` that is a chain's retired middle is first
        registered there as DELISTED, `registered_by = 'lineage_backfill'`, from its L1 EQ extent
        in `presence` (`plan_registrations` decides which). Without that, A→B→C with B in no
        snapshot drops A→B, and the survivor's history starts at B. Pass no `presence` and nothing
        is registered. Any other edge whose successor is unknown is skipped, not raised on — one
        unseen name must not cost the other 500 edges — and returned with its reason. Registered
        rows are insert-only and outlive the rebuild (a master row is never removed, §4.5), so a
        re-run finds them known and registers nothing twice.
        """
        rows = tuple(edges)
        self._conn.execute("DELETE FROM isin_lineage WHERE detected_by = 'L1_CONTIGUITY'")
        known = {r[0] for r in self._conn.execute("SELECT isin FROM security_master").fetchall()}
        plan = plan_registrations(rows, known, {} if presence is None else presence)
        IdentityStore(self._conn, clock=self._clock).register_inferred(
            plan.register, registered_by=LINEAGE_BACKFILL
        )
        writable = plan.writable
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
        report = LineageWriteReport(
            derived=len(rows),
            written=len(writable),
            registered_intermediates=tuple(s.isin for s in plan.register),
            skipped=plan.skipped,
        )
        _LOG.info(
            "lineage.written",
            derived=report.derived,
            written=report.written,
            registered_intermediates=len(report.registered_intermediates),
            still_skipped=report.still_skipped,
            corroborated=sum(1 for e in writable if e.corroborating_action is not None),
            **{f"skipped_{k}": v for k, v in report.skipped_by_reason().items()},
        )
        return report

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

    def survivors(self) -> frozenset[str]:
        """Every ISIN that inherited history from at least one predecessor — the stitch's keys."""
        return frozenset(self._backward)

    def __len__(self) -> int:
        return len(self._forward)
