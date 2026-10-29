"""One-off: repair the AvenuesAI ISIN reissue the 2026-10-24 identity refresh stored as ambiguous.

AvenuesAI Ltd (formerly Infibeam Avenues; NSE symbol INFIBEAM, CCAVENUE from 2026-02-03) had its
ISIN reissued from INE483S01020 to INE483S01038 (face value ₹1 → ₹10) while the stock was
suspended. The L0 record of the switch:

* `nse_bhavcopy_udiff` 2026-10-12 trades CCAVENUE as INE483S01020 only — the last old session;
* the bhavcopies of 13, 14, 15 and 16 Oct do not trade the symbol at all, and the `EQUITY_L`
  captures of 13-17 Oct do not list it;
* `nse_bhavcopy_udiff` 2026-10-19 trades CCAVENUE as INE483S01038 only, and that evening's
  `EQUITY_L` lists it under the new ISIN — the first new session.

The series evidence `resolve_reissues` reads (`L0EquityListSeries`) does not bridge a capture that
omits the symbol, by design, and there was no `isin_lineage` edge. So the 07:00 IST
`identity_refresh` on 2026-10-24 found no boundary (`identity.reissue.no_boundary`) and stored the
new ISIN's windows from the listing date beside the still-open old ones: `symbol_history` 11602
(CCAVENUE from 2026-02-03, open) and 11607 (INFIBEAM 2016-04-04 → 2026-02-02, the rename history),
queuing `identity_reconciliation` 72 (INFIBEAM) and 73 (CCAVENUE). Every resolve of CCAVENUE has
raised since.

The runbook's remedy (`ops/runbooks/identity-master.md`) is a MANUAL `isin_lineage` edge, but the
ingest leaves a stored listing-date window alone, so the edge alone changes nothing. This command
does all of it in one transaction:

1. records the MANUAL edge INE483S01020 → INE483S01038 effective 2026-10-19, after re-reading the
   L0 bhavcopies and captures above and refusing if they do not say exactly that;
2. deletes the two listing-date windows on INE483S01038 (the only rows it deletes);
3. re-derives the 2026-10-24 snapshot from L0 through `resolve_reissues` with the edge in place —
   INE483S01020's CCAVENUE window closes 2026-10-18, INE483S01038's opens 2026-10-19 — and
   requires that to be the *only* change the re-derivation makes to the whole master;
4. resolves the open reconciliation rows for this pair (72 and 73, plus any RESOLVE row the same
   defect raised), with the reason recorded.

What it never does: touch an ISIN other than these two, a BSE window (INE483S01020's BSE row is
left as it is), or a store that is neither exactly the broken shape nor exactly the repaired one —
that is refused and nothing is written. A repaired store is a no-op. Dry run (the default) opens a
READ ONLY transaction. It never fetches; every input is read back out of L0.

    uv run python -m dataplatform.identity.repair_avenuesai            # dry run: print the plan
    uv run python -m dataplatform.identity.repair_avenuesai --apply    # one transaction, commits
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final

from dataplatform.clock import IST, Clock, SystemClock
from dataplatform.identity.ingest import (
    NSE_EQUITY_LIST_SOURCE,
    REISSUE_EVIDENCE_LINEAGE,
    ReissueEvidence,
    derive_master,
    equity_list_filename,
    parse_equity_list,
    parse_symbol_changes,
    resolve_reissues,
)
from dataplatform.identity.master import (
    Exchange,
    HistoryPlan,
    IdentityError,
    IdentityStore,
    SymbolWindow,
    detect_conflicts,
    plan_history,
)
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "AVENUESAI",
    "REPAIR_SNAPSHOT_DATE",
    "EvidenceLine",
    "ExpectedConflict",
    "Reissue",
    "RepairCounts",
    "RepairPlan",
    "RepairRefusedError",
    "StaleWindow",
    "apply_repair",
    "check_evidence",
    "main",
    "plan_repair",
]

_log = get_logger(__name__)

_EXCHANGE: Final = Exchange.NSE
_UDIFF_SOURCE: Final = "nse_bhavcopy_udiff"
_EQUITY_LIST: Final = "nse_equity_list"
_SYMBOL_CHANGE: Final = "nse_symbol_change"


@dataclass(frozen=True, slots=True)
class StaleWindow:
    """A listing-date window the broken ingest stored on the new ISIN, which this repair deletes."""

    symbol: str
    valid_from: date
    valid_to: date | None
    source: str
    #: The live `symbol_history.id`; `None` in a test database whose ids are not the live ones.
    row_id: int | None = None


@dataclass(frozen=True, slots=True)
class ExpectedConflict:
    """An INGEST-detected `identity_reconciliation` row this repair resolves."""

    symbol: str
    on_date: date
    #: The live id; `None` in a test database.
    reconciliation_id: int | None = None


@dataclass(frozen=True, slots=True)
class Reissue:
    """The one reissue this repair may touch, and what the store must say about it."""

    old_isin: str
    new_isin: str
    #: The symbol the reissue happened under, and the edge's `symbol_at_change`.
    symbol: str
    #: The last session the old ISIN traded, and the new ISIN's first (the edge's effective date).
    last_old_session: date
    switch: date
    #: Sessions between the two in which the symbol did not trade — the suspension.
    gap_sessions: int
    #: The old ISIN's NSE windows as they stand before the repair, oldest first. The last is the
    #: one the repair closes the day before `switch`; the others are never touched.
    old_windows: tuple[tuple[str, date, date | None], ...]
    stale: tuple[StaleWindow, ...]
    conflicts: tuple[ExpectedConflict, ...]

    @property
    def pair(self) -> list[str]:
        """Both ISINs, sorted — how `identity_reconciliation.isins` stores them."""
        return sorted([self.old_isin, self.new_isin])

    @property
    def symbols(self) -> frozenset[str]:
        """Every NSE symbol either ISIN has carried."""
        return frozenset(s for s, _, _ in self.old_windows) | {w.symbol for w in self.stale}


#: The snapshot whose ingest stored the bad windows. Its L0 files are what the repair re-derives.
REPAIR_SNAPSHOT_DATE: Final = date(2026, 10, 24)

#: AvenuesAI, as investigated on 2026-10-25 (PR #135) and re-checked against L0 on 2026-10-29.
AVENUESAI: Final = Reissue(
    old_isin="INE483S01020",
    new_isin="INE483S01038",
    symbol="CCAVENUE",
    last_old_session=date(2026, 10, 12),
    switch=date(2026, 10, 19),
    gap_sessions=4,
    old_windows=(
        ("INFIBEAM", date(2016, 4, 4), date(2026, 2, 2)),
        ("CCAVENUE", date(2026, 2, 3), None),
    ),
    stale=(
        StaleWindow("INFIBEAM", date(2016, 4, 4), date(2026, 2, 2), _SYMBOL_CHANGE, 11607),
        StaleWindow("CCAVENUE", date(2026, 2, 3), None, _EQUITY_LIST, 11602),
    ),
    conflicts=(
        ExpectedConflict("INFIBEAM", date(2016, 4, 4), 72),
        ExpectedConflict("CCAVENUE", date(2026, 2, 3), 73),
    ),
)


class RepairRefusedError(IdentityError):
    """The store or the L0 evidence is not what the repair was written for. Nothing was written."""


@dataclass(frozen=True, slots=True)
class EvidenceLine:
    """One L0 payload the switch date rests on, and what it shows for the symbol."""

    key: str
    sha256: str
    isins: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _StoredRow:
    id: int
    window: SymbolWindow
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class _OpenConflict:
    id: int
    detected_by: str
    on_date: date
    symbol: str


@dataclass(frozen=True, slots=True)
class RepairPlan:
    """Everything `apply_repair` will do, decided read-only."""

    snapshot_date: date
    reissue: Reissue
    already_applied: bool
    evidence: tuple[EvidenceLine, ...] = ()
    deletes: tuple[_StoredRow, ...] = ()
    history: HistoryPlan = field(default_factory=HistoryPlan)
    resolutions: tuple[tuple[_OpenConflict, str], ...] = ()

    def describe(self) -> list[str]:
        """The plan as lines a human reads before saying `--apply`."""
        r = self.reissue
        head = f"repair_avenuesai {self.snapshot_date}"
        if self.already_applied:
            return [f"{head}: already applied — nothing to do"]
        lines = [f"{head}: plan (one transaction)"]
        lines.append(f"  L0 evidence ({len(self.evidence)} payloads, re-checksummed):")
        for line in self.evidence:
            shown = ",".join(line.isins) if line.isins else f"{r.symbol} absent"
            lines.append(f"    {line.key}  sha256={line.sha256}  -> {shown}")
        lines.append(
            f"  insert 1 isin_lineage edge: {r.old_isin} -> {r.new_isin} effective "
            f"{r.switch.isoformat()} MANUAL/DERIVED gap_sessions={r.gap_sessions} "
            f"symbol_at_change={r.symbol}"
        )
        lines.append(f"  delete {len(self.deletes)} listing-date window(s):")
        for row in self.deletes:
            lines.append(f"    - symbol_history id={row.id} {_window(row.window)}")
        lines.append(f"  close {len(self.history.closes)} old-ISIN window(s):")
        for window in self.history.closes:
            lines.append(f"    ~ {_window(window)}")
        lines.append(f"  insert {len(self.history.inserts)} new-ISIN window(s):")
        for window in self.history.inserts:
            lines.append(f"    + {_window(window)}  [boundary: {REISSUE_EVIDENCE_LINEAGE}]")
        lines.append(f"  resolve {len(self.resolutions)} identity_reconciliation row(s):")
        for conflict, reason in self.resolutions:
            lines.append(
                f"    * id={conflict.id} {conflict.detected_by} {conflict.symbol} "
                f"on {conflict.on_date} {r.pair}"
            )
            lines.append(f"      reason: {reason}")
        return lines


@dataclass(frozen=True, slots=True)
class RepairCounts:
    """What `apply_repair` changed."""

    edges: int
    deleted: int
    inserted: int
    closed: int
    resolved: int


def check_evidence(l0: L0Store, reissue: Reissue) -> tuple[EvidenceLine, ...]:
    """Re-read the switch out of L0; raise `RepairRefusedError` unless it says exactly `reissue`.

    What it does: for every cash bhavcopy and every `EQUITY_L` capture dated from the last old
    session to the switch, collects the ISINs the symbol appears under. The first of each must
    show the old ISIN only, the last the new ISIN only, and every one between must not carry the
    symbol at all; the bhavcopies between must number `gap_sessions`.
    What it assumes: L0 holds one bhavcopy per session and the daily equity-list capture.
    What it never does: open a socket. Every payload is re-hashed by `L0Store.get`.
    """
    from dataplatform.ingest.nse.bhavcopy_udiff import parse_l0 as parse_udiff_l0

    start, end = reissue.last_old_session, reissue.switch
    lines: list[EvidenceLine] = []
    problems: list[str] = []

    sessions = sorted(l0.iter_refs(_UDIFF_SOURCE, start=start, end=end), key=_ref_order)
    session_isins = [
        (
            ref,
            tuple(
                sorted(
                    {
                        row.isin
                        for row in parse_udiff_l0(l0, ref)
                        if row.symbol.strip().upper() == reissue.symbol
                    }
                )
            ),
        )
        for ref in sessions
    ]
    captures = sorted(
        (
            ref
            for ref in l0.iter_refs(NSE_EQUITY_LIST_SOURCE, start=start, end=end)
            if ref.filename == equity_list_filename(ref.logical_date)
        ),
        key=_ref_order,
    )
    capture_isins = [
        (
            ref,
            tuple(
                sorted(
                    row.isin
                    for row in parse_equity_list(l0.get(ref).decode("utf-8"))
                    if row.symbol == reissue.symbol
                )
            ),
        )
        for ref in captures
    ]

    for name, series in (("bhavcopy", session_isins), ("EQUITY_L", capture_isins)):
        if not series:
            problems.append(f"no {name} in L0 for {start}..{end}")
            continue
        first, last = series[0], series[-1]
        if first[0].logical_date != start or first[1] != (reissue.old_isin,):
            problems.append(f"{first[0].key}: {reissue.symbol} shows {first[1]}, not old only")
        if last[0].logical_date != end or last[1] != (reissue.new_isin,):
            problems.append(f"{last[0].key}: {reissue.symbol} shows {last[1]}, not new only")
        problems.extend(
            f"{ref.key}: {reissue.symbol} trades as {isins} inside the suspension"
            for ref, isins in series[1:-1]
            if isins
        )
        lines.extend(EvidenceLine(ref.key, ref.sha256, isins) for ref, isins in series)
    if len(session_isins) - 2 != reissue.gap_sessions:
        problems.append(
            f"{len(session_isins) - 2} bhavcopy session(s) between {start} and {end}, "
            f"expected {reissue.gap_sessions}"
        )
    if problems:
        raise RepairRefusedError("L0 does not evidence the switch:\n  " + "\n  ".join(problems))
    return tuple(lines)


def plan_repair(
    conn: Connection,
    *,
    l0: L0Store,
    equity_list: str,
    symbol_changes: str,
    snapshot_date: date = REPAIR_SNAPSHOT_DATE,
    reissue: Reissue = AVENUESAI,
    evidence: ReissueEvidence | None = None,
) -> RepairPlan:
    """Decide the repair without writing. Raises `RepairRefusedError` on any surprise.

    What it does: checks the switch against L0, classifies the store as *broken* (no edge, the
    stale windows on the new ISIN written by the `snapshot_date` ingest, the INGEST rows open) or
    *repaired* (the MANUAL edge, the old ISIN closed the day before the switch, the new ISIN open
    from it, nothing open), and for a broken store re-derives the snapshot through
    `resolve_reissues` with the edge in place and the stale rows set aside — requiring the result
    to be exactly one close and one insert, on these two ISINs, across the whole master.
    What it assumes: the caller's transaction; `equity_list`/`symbol_changes` are the snapshot
    the stale rows came from.
    What it never does: write, or plan for an ISIN other than the two in `reissue`.
    """
    evidence_lines = check_evidence(l0, reissue)
    r = reissue

    edges = conn.execute(
        "SELECT predecessor_isin, successor_isin, effective_date, detected_by, confidence, "
        "gap_sessions, symbol_at_change, corroborating_action FROM isin_lineage "
        "WHERE predecessor_isin = ANY(%(both)s) OR successor_isin = %(new)s",
        {"both": [r.old_isin, r.new_isin], "new": r.new_isin},
    ).fetchall()
    want_edge = (
        r.old_isin,
        r.new_isin,
        r.switch,
        "MANUAL",
        "DERIVED",
        r.gap_sessions,
        r.symbol,
        None,
    )
    if edges and [tuple(e) for e in edges] != [want_edge]:
        raise RepairRefusedError(f"isin_lineage already holds other edges for the pair: {edges}")
    has_edge = bool(edges)

    rows = _nse_rows(conn, r)
    other = [row for row in rows if row.window.isin not in (r.old_isin, r.new_isin)]
    if other:
        raise RepairRefusedError(
            "another ISIN claims the symbols: " + ", ".join(_window(o.window) for o in other)
        )
    old = [_shape(row.window) for row in rows if row.window.isin == r.old_isin]
    new = [row for row in rows if row.window.isin == r.new_isin]
    conflicts = _open_conflicts(conn, r)

    repaired_old = [*r.old_windows[:-1], (r.symbol, r.old_windows[-1][1], _day_before(r.switch))]
    repaired_new = [(r.symbol, r.switch, None)]
    if (
        has_edge
        and old == repaired_old
        and [_shape(row.window) for row in new] == repaired_new
        and not conflicts
    ):
        _log.info("identity.repair_avenuesai.already_applied", snapshot_date=str(snapshot_date))
        return RepairPlan(snapshot_date=snapshot_date, reissue=r, already_applied=True)

    seen = ", ".join(_window(row.window) for row in rows) or "no NSE windows"
    if has_edge:
        raise RepairRefusedError(f"edge present but the store is not repaired; got {seen}")
    if old != list(r.old_windows):
        raise RepairRefusedError(f"{r.old_isin}: expected {list(r.old_windows)}; got {seen}")
    stale = _match_stale(new, r, snapshot_date=snapshot_date)

    store = IdentityStore(conn)
    gone = {row.window.key for row in stale}
    remaining = tuple(w for w in store.load_windows() if w.key not in gone)
    derived = derive_master(
        parse_equity_list(equity_list),
        parse_symbol_changes(symbol_changes) if symbol_changes.strip() else (),
        snapshot_date=snapshot_date,
    )
    lineage = {(p, s): eff for p, s, eff in store.load_reissues()}
    lineage[(r.old_isin, r.new_isin)] = r.switch
    resolution = resolve_reissues(
        derived.windows,
        remaining,
        listed_isins=frozenset(s.isin for s in derived.securities),
        lineage=lineage,
        snapshot_date=snapshot_date,
        evidence=evidence,
    )
    history = plan_history(remaining, resolution.windows)

    last = r.old_windows[-1]
    want_closes = {(r.old_isin, r.symbol, last[1], _day_before(r.switch))}
    want_inserts = {(r.new_isin, r.symbol, r.switch, None)}
    got_closes = {(w.isin, w.symbol, w.valid_from, w.valid_to) for w in history.closes}
    got_inserts = {(w.isin, w.symbol, w.valid_from, w.valid_to) for w in history.inserts}
    if got_closes != want_closes or got_inserts != want_inserts:
        raise RepairRefusedError(
            "re-derivation does not match the expected boundary — "
            f"closes {sorted(map(str, got_closes ^ want_closes))}, "
            f"inserts {sorted(map(str, got_inserts ^ want_inserts))}"
        )
    boundaries = [
        (b.symbol, b.old_isin, b.new_isin, b.effective, b.evidence) for b in resolution.boundaries
    ]
    if boundaries != [(r.symbol, r.old_isin, r.new_isin, r.switch, REISSUE_EVIDENCE_LINEAGE)]:
        raise RepairRefusedError(f"unexpected reissue boundaries: {boundaries}")
    touched = [f for f in history.refusals if f.stored.isin in (r.old_isin, r.new_isin)]
    if touched:
        raise RepairRefusedError(f"history refusals on the repaired ISINs: {touched}")
    after = [
        c
        for c in detect_conflicts(history.applied_to(remaining), source=_EQUITY_LIST)
        if {r.old_isin, r.new_isin} & set(c.isins) or r.symbols & set(c.symbols)
    ]
    if after:
        raise RepairRefusedError(f"repair would still leave conflicts: {[str(c) for c in after]}")

    resolutions = _resolutions(conflicts, r)
    return RepairPlan(
        snapshot_date=snapshot_date,
        reissue=r,
        already_applied=False,
        evidence=evidence_lines,
        deletes=tuple(stale),
        history=history,
        resolutions=resolutions,
    )


def apply_repair(conn: Connection, plan: RepairPlan, *, clock: Clock) -> RepairCounts:
    """Execute a `plan_repair` result in the caller's transaction. Does not commit.

    Every statement's row count is checked against the plan; a mismatch raises
    `RepairRefusedError` and the caller rolls back, so the repair lands whole or not at all.
    """
    if plan.already_applied:
        return RepairCounts(0, 0, 0, 0, 0)
    r = plan.reissue
    edges = conn.execute(
        "INSERT INTO isin_lineage (predecessor_isin, successor_isin, effective_date, detected_by, "
        "confidence, gap_sessions, symbol_at_change, corroborating_action, computed_at) "
        "VALUES (%s, %s, %s, 'MANUAL', 'DERIVED', %s, %s, NULL, %s) "
        "ON CONFLICT DO NOTHING RETURNING predecessor_isin",
        (r.old_isin, r.new_isin, r.switch, r.gap_sessions, r.symbol, clock.now()),
    ).fetchall()
    deleted = conn.execute(
        "DELETE FROM symbol_history WHERE id = ANY(%s) AND isin = %s AND exchange = %s "
        "RETURNING id",
        ([row.id for row in plan.deletes], r.new_isin, _EXCHANGE.value),
    ).fetchall()
    inserted, closed = IdentityStore(conn, clock=clock).apply_history(plan.history)
    resolved = 0
    for conflict, reason in plan.resolutions:
        resolved += len(
            conn.execute(
                "UPDATE identity_reconciliation SET resolved = true, resolved_at = %s, "
                "resolution = %s WHERE id = %s AND NOT resolved RETURNING id",
                (clock.now(), reason, conflict.id),
            ).fetchall()
        )
    counts = RepairCounts(len(edges), len(deleted), inserted, closed, resolved)
    want = RepairCounts(
        1,
        len(plan.deletes),
        len(plan.history.inserts),
        len(plan.history.closes),
        len(plan.resolutions),
    )
    if counts != want:
        raise RepairRefusedError(f"repair wrote {counts}, planned {want}; roll back")
    _log.info(
        "identity.repair_avenuesai.applied",
        source=_EQUITY_LIST,
        snapshot_date=plan.snapshot_date.isoformat(),
        old_isin=r.old_isin,
        new_isin=r.new_isin,
        switch=r.switch.isoformat(),
        edges=counts.edges,
        deleted=counts.deleted,
        inserted=counts.inserted,
        closed=counts.closed,
        resolved=counts.resolved,
    )
    return counts


def _nse_rows(conn: Connection, r: Reissue) -> list[_StoredRow]:
    """Every NSE window of either ISIN, or under any symbol either has carried."""
    rows = conn.execute(
        "SELECT id, isin, symbol, series, valid_from, valid_to, source, recorded_at "
        "FROM symbol_history WHERE exchange = %s AND (isin = ANY(%s) OR symbol = ANY(%s)) "
        "ORDER BY isin, valid_from, symbol",
        (_EXCHANGE.value, [r.old_isin, r.new_isin], sorted(r.symbols)),
    ).fetchall()
    return [
        _StoredRow(
            id=int(row_id),
            window=SymbolWindow(
                _EXCHANGE,
                str(symbol),
                valid_from,
                valid_to,
                str(isin),
                None if series is None else str(series),
                str(source),
            ),
            recorded_at=recorded_at,
        )
        for row_id, isin, symbol, series, valid_from, valid_to, source, recorded_at in rows
    ]


def _shape(w: SymbolWindow) -> tuple[str, date, date | None]:
    return (w.symbol, w.valid_from, w.valid_to)


def _match_stale(new: Sequence[_StoredRow], r: Reissue, *, snapshot_date: date) -> list[_StoredRow]:
    """The new ISIN's stored windows, which must be exactly `r.stale`, written that morning."""
    by_shape = {(w.symbol, w.valid_from, w.valid_to, w.source): w for w in r.stale}
    matched: list[_StoredRow] = []
    problems: list[str] = []
    for row in new:
        w = row.window
        want = by_shape.pop((w.symbol, w.valid_from, w.valid_to, w.source), None)
        if want is None:
            problems.append(f"unexpected {_window(w)} source={w.source}")
        elif want.row_id is not None and want.row_id != row.id:
            problems.append(f"{_window(w)} is id={row.id}, expected id={want.row_id}")
        elif row.recorded_at.astimezone(IST).date() != snapshot_date:
            problems.append(f"{_window(w)} was recorded {row.recorded_at}, not on {snapshot_date}")
        else:
            matched.append(row)
    problems.extend(
        f"missing {r.new_isin} {s.symbol} from {s.valid_from}" for s in by_shape.values()
    )
    if problems:
        raise RepairRefusedError(f"{r.new_isin}: not the broken shape:\n  " + "\n  ".join(problems))
    return matched


def _open_conflicts(conn: Connection, r: Reissue) -> list[_OpenConflict]:
    """Open reconciliation rows naming either ISIN. Anything but the pair's own is refused."""
    rows = conn.execute(
        "SELECT id, kind, exchange, detected_by, on_date, symbols, isins "
        "FROM identity_reconciliation WHERE NOT resolved AND isins && %s ORDER BY id",
        ([r.old_isin, r.new_isin],),
    ).fetchall()
    out: list[_OpenConflict] = []
    for row_id, kind, exchange, detected_by, on_date, symbols, isins in rows:
        if (
            kind != "SYMBOL_TO_ISIN"
            or exchange != _EXCHANGE.value
            or list(isins) != r.pair
            or len(symbols) != 1
            or symbols[0] not in r.symbols
        ):
            raise RepairRefusedError(
                f"open reconciliation row {row_id} ({kind} {exchange} {symbols} {isins}) is not "
                "this reissue's; refusing"
            )
        out.append(_OpenConflict(int(row_id), str(detected_by), on_date, str(symbols[0])))
    return out


def _resolutions(
    conflicts: Sequence[_OpenConflict], r: Reissue
) -> tuple[tuple[_OpenConflict, str], ...]:
    """The INGEST rows must be exactly `r.conflicts`; RESOLVE rows the same defect raised later
    (a resolver asking about CCAVENUE) are resolved with them."""
    ingest = sorted((c.symbol, c.on_date, c.id) for c in conflicts if c.detected_by == "INGEST")
    want = sorted((e.symbol, e.on_date, e.reconciliation_id) for e in r.conflicts)
    matches = len(ingest) == len(want) and all(
        (s, d) == (ws, wd) and wid in (None, i)
        for (s, d, i), (ws, wd, wid) in zip(ingest, want, strict=True)
    )
    if not matches:
        raise RepairRefusedError(f"expected open INGEST rows {want}, found {ingest}")
    reason = (
        f"repair_avenuesai: ISIN reissued {r.old_isin} -> {r.new_isin} (AvenuesAI; NSE INFIBEAM, "
        f"CCAVENUE from 2026-02-03) across a suspension: last old session "
        f"{r.last_old_session.isoformat()}, {r.gap_sessions} sessions without {r.symbol}, first "
        f"new session {r.switch.isoformat()} (L0 nse_bhavcopy_udiff and nse_equity_list). MANUAL "
        f"isin_lineage edge recorded; the listing-date windows the 2026-10-24 ingest stored on "
        f"{r.new_isin} deleted; {r.old_isin} {r.symbol} closed "
        f"{_day_before(r.switch).isoformat()}; {r.new_isin} {r.symbol} from "
        f"{r.switch.isoformat()}. Owner-approved 2026-10-29."
    )
    return tuple((c, reason) for c in conflicts)


def _day_before(d: date) -> date:
    return d - timedelta(days=1)


def _ref_order(ref: L0Ref) -> date:
    return ref.logical_date


def _window(w: SymbolWindow) -> str:
    end = w.valid_to.isoformat() if w.valid_to else "open"
    return f"{w.exchange.value} {w.symbol} {w.isin} [{w.valid_from.isoformat()}..{end}]"


def main(argv: Sequence[str] | None = None) -> int:
    """Print the plan; with `--apply`, execute it in one transaction and commit."""
    from dataplatform.config import get_settings
    from dataplatform.identity.ingest import L0EquityListSeries, read_snapshot_from_l0
    from dataplatform.logging import configure_logging
    from dataplatform.store.db import connection

    parser = argparse.ArgumentParser(
        prog="identity.repair_avenuesai", description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument(
        "--apply", action="store_true", help="execute and commit (default: dry run, read only)"
    )
    args = parser.parse_args(argv)
    configure_logging()

    clock = SystemClock()
    settings = get_settings()
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    equity_list, changes = read_snapshot_from_l0(REPAIR_SNAPSHOT_DATE, store=l0)
    with connection(settings) as conn:
        if args.apply:
            # Held to commit: no ingest or resolve can change the three tables between the plan
            # being checked and it being written. Reads (the dry run, resolvers) still proceed.
            conn.execute(
                "LOCK TABLE symbol_history, identity_reconciliation, isin_lineage "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
        else:
            conn.execute("SET TRANSACTION READ ONLY")
        try:
            plan = plan_repair(
                conn,
                l0=l0,
                equity_list=equity_list,
                symbol_changes=changes,
                evidence=L0EquityListSeries(l0),
            )
            print("\n".join(plan.describe()))
            if not args.apply:
                conn.rollback()
                print("dry run: nothing written (READ ONLY transaction). Re-run with --apply.")
                return 0
            counts = apply_repair(conn, plan, clock=clock)
        except IdentityError as error:
            conn.rollback()
            print(f"repair_avenuesai refused: {error}", file=sys.stderr)
            return 2
        conn.commit()
    print(
        f"applied: {counts.edges} edge, {counts.deleted} deleted, {counts.inserted} inserted, "
        f"{counts.closed} closed, {counts.resolved} reconciliation row(s) resolved"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
