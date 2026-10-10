"""M18.1: repair the seven NSE ISIN reissues the 2026-10-10 identity refresh stored as ambiguous.

Before `resolve_reissues` existed, the 07:00 IST `identity_refresh` on 2026-10-10 met seven
securities whose ISIN had been reissued after a face-value split while NSE kept the symbol. For
each it inserted a window for the new ISIN starting on the company's *original* listing date and
left the old ISIN's window open, so both claim the symbol over the same dates and every resolve
raises `AmbiguousSymbolError` (reconciliation ids 21-27).

The fixed ingest will not touch that state on its own: `plan_history` never deletes a window, and
`resolve_reissues` deliberately leaves a stored listing-date window alone rather than patch around
it. This command is the one-off that does, in one transaction:

1. deletes the seven new-ISIN windows inserted at 07:00 (the only rows it deletes);
2. re-derives them from the same day's L0 snapshot through the fixed `ingest_snapshot` logic —
   the new ISIN starts on its evidenced switch date (`isin_lineage`, else the L0 equity-list
   series), the old ISIN's window closes the day before;
3. marks the matching open `identity_reconciliation` rows resolved, with the reason recorded.

What it never does: run against a store that is not exactly the expected broken state. Every
window, every boundary and every reconciliation id is checked against `EXPECTED_2026_10_10` before
a write. Anything else is refused and nothing is written. A store that is already repaired is a
no-op. Dry run (the default) opens a READ ONLY transaction, so it cannot write. It never fetches;
the snapshot and the series are read back out of L0.

    uv run python -m dataplatform.identity.repair_reissues            # dry run: print the plan
    uv run python -m dataplatform.identity.repair_reissues --apply    # one transaction, commits
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.identity.ingest import (
    ReissueBoundary,
    ReissueEvidence,
    derive_master,
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

__all__ = [
    "EXPECTED_2026_10_10",
    "REPAIR_SNAPSHOT_DATE",
    "ExpectedReissue",
    "RepairCounts",
    "RepairPlan",
    "RepairRefusedError",
    "apply_repair",
    "main",
    "plan_repair",
]

_log = get_logger(__name__)

_SOURCE: Final = "nse_equity_list"


@dataclass(frozen=True, slots=True)
class ExpectedReissue:
    """One reissue the repair is allowed to touch, and what the store must say about it."""

    symbol: str
    old_isin: str
    new_isin: str
    switch: date
    #: The INGEST-detected `identity_reconciliation` row this repair resolves; `None` in a test
    #: database whose ids are not the live ones.
    reconciliation_id: int | None = None


#: The snapshot whose ingest stored the bad windows. Its L0 files are what the repair re-derives.
REPAIR_SNAPSHOT_DATE: Final = date(2026, 10, 10)

#: The seven, as investigated on 2026-10-10. The first four switch dates are `isin_lineage` edges;
#: the last three are the first L0 `EQUITY_L` capture showing the new ISIN.
EXPECTED_2026_10_10: Final[tuple[ExpectedReissue, ...]] = (
    ExpectedReissue("TDPOWERSYS", "INE419M01027", "INE419M01035", date(2026, 8, 24), 21),
    ExpectedReissue("KIRLPNU", "INE811A01020", "INE811A01038", date(2026, 8, 18), 22),
    ExpectedReissue("BLSE", "INE0NLT01010", "INE0NLT01028", date(2026, 10, 6), 23),
    ExpectedReissue("BUILDPRO", "INE24OJ01011", "INE24OJ01029", date(2026, 10, 8), 24),
    ExpectedReissue("TCC", "INE887D01016", "INE887D01024", date(2026, 9, 4), 25),
    ExpectedReissue("TAALTECH", "INE524T01011", "INE524T01029", date(2026, 9, 22), 26),
    ExpectedReissue("CORDELIA", "INE0LZF01013", "INE0LZF01039", date(2026, 8, 25), 27),
)


class RepairRefusedError(IdentityError):
    """The store is not in the state the repair was written for. Nothing was written."""


@dataclass(frozen=True, slots=True)
class _StoredRow:
    id: int
    window: SymbolWindow


@dataclass(frozen=True, slots=True)
class _OpenConflict:
    id: int
    detected_by: str
    on_date: date
    symbol: str
    isins: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepairPlan:
    """Everything `apply_repair` will do, decided read-only."""

    snapshot_date: date
    already_applied: bool
    deletes: tuple[_StoredRow, ...] = ()
    history: HistoryPlan = field(default_factory=HistoryPlan)
    boundaries: tuple[ReissueBoundary, ...] = ()
    resolutions: tuple[tuple[_OpenConflict, str], ...] = ()

    def describe(self) -> list[str]:
        """The plan as lines a human reads before saying `--apply`."""
        if self.already_applied:
            return [f"repair_reissues {self.snapshot_date}: already applied — nothing to do"]
        lines = [f"repair_reissues {self.snapshot_date}: plan (one transaction)"]
        lines.append(f"  delete {len(self.deletes)} listing-date window(s):")
        for row in self.deletes:
            lines.append(f"    - symbol_history id={row.id} {_window(row.window)}")
        lines.append(f"  close {len(self.history.closes)} old-ISIN window(s):")
        for window in self.history.closes:
            lines.append(f"    ~ {_window(window)}")
        lines.append(f"  insert {len(self.history.inserts)} new-ISIN window(s):")
        by_new = {b.new_isin: b for b in self.boundaries}
        for window in self.history.inserts:
            evidence = by_new[window.isin].evidence if window.isin in by_new else "?"
            lines.append(f"    + {_window(window)}  [boundary: {evidence}]")
        lines.append(f"  resolve {len(self.resolutions)} identity_reconciliation row(s):")
        for conflict, reason in self.resolutions:
            lines.append(
                f"    * id={conflict.id} {conflict.detected_by} {conflict.symbol} "
                f"on {conflict.on_date} {list(conflict.isins)}"
            )
            lines.append(f"      reason: {reason}")
        return lines


@dataclass(frozen=True, slots=True)
class RepairCounts:
    """What `apply_repair` changed."""

    deleted: int
    inserted: int
    closed: int
    resolved: int


def plan_repair(
    conn: Connection,
    *,
    equity_list: str,
    symbol_changes: str,
    snapshot_date: date = REPAIR_SNAPSHOT_DATE,
    evidence: ReissueEvidence | None = None,
    expected: Sequence[ExpectedReissue] = EXPECTED_2026_10_10,
) -> RepairPlan:
    """Decide the repair without writing. Raises `RepairRefusedError` on any surprise.

    What it does: classifies each expected symbol's NSE windows as *broken* (old and new ISIN both
    open from the same start, the new one from the equity list) or *repaired* (old closed the day
    before the switch, new open from it), re-derives the snapshot through `resolve_reissues` with
    the broken rows set aside, and requires the result to be exactly the expected boundaries.
    What it assumes: the caller's transaction; `equity_list`/`symbol_changes` are the snapshot
    the bad rows came from.
    What it never does: write, or plan for a symbol not in `expected`.
    """
    symbols = [e.symbol for e in expected]
    rows = conn.execute(
        "SELECT id, isin, symbol, series, valid_from, valid_to, source FROM symbol_history "
        "WHERE exchange = %s AND symbol = ANY(%s) ORDER BY symbol, valid_from, isin",
        (Exchange.NSE.value, symbols),
    ).fetchall()
    by_symbol: dict[str, list[_StoredRow]] = {}
    for row_id, isin, symbol, series, valid_from, valid_to, source in rows:
        by_symbol.setdefault(str(symbol), []).append(
            _StoredRow(
                id=int(row_id),
                window=SymbolWindow(
                    Exchange.NSE,
                    str(symbol),
                    valid_from,
                    valid_to,
                    str(isin),
                    None if series is None else str(series),
                    str(source),
                ),
            )
        )

    broken: list[_StoredRow] = []
    repaired = 0
    problems: list[str] = []
    for e in expected:
        state = _classify(e, by_symbol.get(e.symbol, []))
        if isinstance(state, _StoredRow):
            broken.append(state)
        elif state == "repaired":
            repaired += 1
        else:
            problems.append(state)
    if problems:
        raise RepairRefusedError("store differs from expected:\n  " + "\n  ".join(problems))
    if repaired == len(expected):
        _log.info("identity.repair_reissues.already_applied", snapshot_date=str(snapshot_date))
        return RepairPlan(snapshot_date=snapshot_date, already_applied=True)
    if repaired:
        raise RepairRefusedError(
            f"store is half repaired ({repaired} of {len(expected)} symbols); refusing to guess"
        )

    store = IdentityStore(conn)
    stale = {row.window.key for row in broken}
    remaining = tuple(w for w in store.load_windows() if w.key not in stale)
    derived = derive_master(
        parse_equity_list(equity_list),
        parse_symbol_changes(symbol_changes) if symbol_changes.strip() else (),
        snapshot_date=snapshot_date,
    )
    resolution = resolve_reissues(
        derived.windows,
        remaining,
        listed_isins=frozenset(s.isin for s in derived.securities),
        lineage={(old, new): effective for old, new, effective in store.load_reissues()},
        snapshot_date=snapshot_date,
        evidence=evidence,
    )
    history = plan_history(remaining, resolution.windows)

    want_closes = {(e.old_isin, e.symbol, e.switch - timedelta(days=1)) for e in expected}
    want_inserts = {(e.new_isin, e.symbol, e.switch) for e in expected}
    got_closes = {(w.isin, w.symbol, w.valid_to) for w in history.closes}
    got_inserts = {(w.isin, w.symbol, w.valid_from) for w in history.inserts}
    open_inserts = all(w.valid_to is None for w in history.inserts)
    if got_closes != want_closes or got_inserts != want_inserts or not open_inserts:
        raise RepairRefusedError(
            "re-derivation does not match the expected boundaries — "
            f"closes {sorted(map(str, got_closes ^ want_closes))}, "
            f"inserts {sorted(map(str, got_inserts ^ want_inserts))}"
        )
    touched = [r for r in history.refusals if r.stored.symbol in symbols]
    if touched:
        raise RepairRefusedError(f"history refusals on repaired symbols: {touched}")
    after = [
        c
        for c in detect_conflicts(history.applied_to(remaining), source=_SOURCE)
        if c.exchange is Exchange.NSE and set(c.symbols) & set(symbols)
    ]
    if after:
        raise RepairRefusedError(f"repair would still leave conflicts: {[str(c) for c in after]}")

    resolutions = _resolutions(conn, expected, resolution.boundaries)
    return RepairPlan(
        snapshot_date=snapshot_date,
        already_applied=False,
        deletes=tuple(broken),
        history=history,
        boundaries=resolution.boundaries,
        resolutions=resolutions,
    )


def apply_repair(conn: Connection, plan: RepairPlan, *, clock: Clock) -> RepairCounts:
    """Execute a `plan_repair` result in the caller's transaction. Does not commit.

    Every statement's row count is checked against the plan; a mismatch raises
    `RepairRefusedError` and the caller rolls back, so the repair lands whole or not at all.
    """
    if plan.already_applied:
        return RepairCounts(0, 0, 0, 0)
    deleted = conn.execute(
        "DELETE FROM symbol_history WHERE id = ANY(%s) AND valid_to IS NULL "
        "AND exchange = %s RETURNING id",
        ([row.id for row in plan.deletes], Exchange.NSE.value),
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
    counts = RepairCounts(len(deleted), inserted, closed, resolved)
    want = RepairCounts(
        len(plan.deletes),
        len(plan.history.inserts),
        len(plan.history.closes),
        len(plan.resolutions),
    )
    if counts != want:
        raise RepairRefusedError(f"repair wrote {counts}, planned {want}; roll back")
    _log.info(
        "identity.repair_reissues.applied",
        source=_SOURCE,
        snapshot_date=plan.snapshot_date.isoformat(),
        deleted=counts.deleted,
        inserted=counts.inserted,
        closed=counts.closed,
        resolved=counts.resolved,
    )
    return counts


def _classify(e: ExpectedReissue, rows: Sequence[_StoredRow]) -> _StoredRow | str:
    """The stale row to delete when broken, `"repaired"`, or a description of the surprise."""
    old = [r for r in rows if r.window.isin == e.old_isin]
    new = [r for r in rows if r.window.isin == e.new_isin]
    other = [r for r in rows if r.window.isin not in (e.old_isin, e.new_isin)]
    seen = ", ".join(_window(r.window) for r in rows) or "no NSE windows"
    if other or len(old) != 1 or len(new) != 1:
        return f"{e.symbol}: expected one window each for {e.old_isin}/{e.new_isin}; got {seen}"
    o, n = old[0].window, new[0].window
    if (
        o.valid_to is None
        and n.valid_to is None
        and n.valid_from == o.valid_from < e.switch
        and n.source == _SOURCE
    ):
        return new[0]
    if (
        o.valid_to == e.switch - timedelta(days=1)
        and n.valid_from == e.switch
        and n.valid_to is None
    ):
        return "repaired"
    return f"{e.symbol}: neither the broken nor the repaired shape; got {seen}"


def _resolutions(
    conn: Connection,
    expected: Sequence[ExpectedReissue],
    boundaries: Sequence[ReissueBoundary],
) -> tuple[tuple[_OpenConflict, str], ...]:
    """Open SYMBOL_TO_ISIN rows naming exactly one expected (symbol, {old, new}), with reasons.

    The INGEST row must exist (and carry the expected id when one is given). RESOLVE rows the
    same defect raised later — ca_refresh asking about a held-back action — are resolved too.
    """
    by_symbol = {b.symbol: b for b in boundaries}
    out: list[tuple[_OpenConflict, str]] = []
    for e in expected:
        rows = conn.execute(
            "SELECT id, detected_by, on_date FROM identity_reconciliation "
            "WHERE NOT resolved AND kind = 'SYMBOL_TO_ISIN' AND exchange = %s "
            "AND symbols = %s AND isins = %s ORDER BY id",
            (Exchange.NSE.value, [e.symbol], sorted([e.old_isin, e.new_isin])),
        ).fetchall()
        conflicts = [
            _OpenConflict(int(i), str(d), on, e.symbol, tuple(sorted([e.old_isin, e.new_isin])))
            for i, d, on in rows
        ]
        ingest_ids = [c.id for c in conflicts if c.detected_by == "INGEST"]
        if len(ingest_ids) != 1 or (
            e.reconciliation_id is not None and ingest_ids != [e.reconciliation_id]
        ):
            raise RepairRefusedError(
                f"{e.symbol}: expected one open INGEST reconciliation row "
                f"{e.reconciliation_id or ''}, found {ingest_ids}"
            )
        boundary = by_symbol[e.symbol]
        reason = (
            f"M18.1 repair_reissues: ISIN reissued under unchanged symbol {e.symbol} "
            f"({e.old_isin} -> {e.new_isin}, switch {boundary.effective.isoformat()}, evidence "
            f"{boundary.evidence}). Listing-date window for {e.new_isin} deleted and re-derived "
            f"from the switch; {e.old_isin} closed "
            f"{(boundary.effective - timedelta(days=1)).isoformat()}."
        )
        out.extend((c, reason) for c in conflicts)
    return tuple(out)


def _window(w: SymbolWindow) -> str:
    end = w.valid_to.isoformat() if w.valid_to else "open"
    return f"{w.exchange.value} {w.symbol} {w.isin} [{w.valid_from.isoformat()}..{end}]"


def main(argv: Sequence[str] | None = None) -> int:
    """Print the plan; with `--apply`, execute it in one transaction and commit."""
    from dataplatform.identity.ingest import L0EquityListSeries, read_snapshot_from_l0
    from dataplatform.logging import configure_logging
    from dataplatform.store.db import connection
    from dataplatform.store.l0 import L0Store

    parser = argparse.ArgumentParser(
        prog="identity.repair_reissues", description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument(
        "--apply", action="store_true", help="execute and commit (default: dry run, read only)"
    )
    args = parser.parse_args(argv)
    configure_logging()

    clock = SystemClock()
    l0 = L0Store(clock=clock)
    equity_list, changes = read_snapshot_from_l0(REPAIR_SNAPSHOT_DATE, store=l0)
    with connection() as conn:
        if not args.apply:
            conn.execute("SET TRANSACTION READ ONLY")
        try:
            plan = plan_repair(
                conn,
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
            print(f"repair_reissues refused: {error}", file=sys.stderr)
            return 2
        conn.commit()
    print(
        f"applied: {counts.deleted} deleted, {counts.inserted} inserted, {counts.closed} closed, "
        f"{counts.resolved} reconciliation row(s) resolved"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
