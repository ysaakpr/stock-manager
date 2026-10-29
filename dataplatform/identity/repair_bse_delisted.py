"""Remove the current BSE symbol windows the 2026-09-06 merge opened for delisted scrips.

Before `derive_master` skipped them, the one BSE scrip-master merge (2026-09-06, 20:41 IST) opened
a current `symbol_history` window for every scrip, delisted ones included: every ISIN that snapshot
lists only as delisted got an open BSE window from 2026-09-06 claiming its last ticker was still
its own. BSE has handed ten of those tickers to a live ISIN, so each resolves two ways from that
date (`identity_reconciliation` ids 1-10), and every Saturday the NSE `identity_refresh` — which
judges conflicts over the whole master — has failed on them.

The fixed merge does not undo that on its own: `plan_history` never deletes a row, and each window
starts on the very snapshot that says its scrip was delisted, so no close can make it true (a
window closed on 2026-09-06 still claims 2026-09-06). This command is the one-off that does, in one
transaction:

1. re-derives the 2026-09-06 snapshot from L0 — all three statuses, no request — through the fixed
   `derive_master`, and requires every other stored BSE window to be exactly what that derives;
2. deletes the stored BSE windows of the ISINs that snapshot lists only as delisted: written by
   that merge (source, `valid_from` and `recorded_at` all 2026-09-06), still open — no other row;
3. requires the master left behind to hold no BSE conflict and no conflict it did not already
   hold, and marks the ten open `identity_reconciliation` rows resolved with the reason recorded.

The delisted scrips keep their `security_master` and `exchange_listing` rows — the `SC_CODE → ISIN`
fact the pre-2024 BSE bhavcopy resolves through. Only the false "current ticker" claim goes.

What it never does: run against a store that is not exactly the expected broken state. Every
deleted row, every remaining BSE window and every reconciliation id is checked before a write;
anything else is refused and nothing is written. A store that is already repaired is a no-op. Dry
run (the default) opens a READ ONLY transaction, so it cannot write. It never fetches.

    uv run python -m dataplatform.identity.repair_bse_delisted            # dry run: print the plan
    uv run python -m dataplatform.identity.repair_bse_delisted --apply    # one transaction, commits
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from dataplatform.clock import IST, Clock, SystemClock
from dataplatform.identity.master import (
    ConflictKind,
    Exchange,
    IdentityConflict,
    IdentityError,
    IdentityStore,
    SymbolWindow,
    detect_conflicts,
    plan_history,
)
from dataplatform.ingest.bse.scrip_master import (
    BSE_SCRIP_MASTER_SOURCE,
    delisted_isins,
    derive_master,
    parse_scrip_master,
)
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

__all__ = [
    "EXPECTED_2026_09_06",
    "REPAIR_SNAPSHOT_DATE",
    "ExpectedConflict",
    "RepairCounts",
    "RepairPlan",
    "RepairRefusedError",
    "apply_repair",
    "main",
    "plan_repair",
]

_log = get_logger(__name__)

#: The snapshot whose merge stored the windows. Its L0 files are what the repair re-derives.
REPAIR_SNAPSHOT_DATE: Final = date(2026, 9, 6)


@dataclass(frozen=True, slots=True)
class ExpectedConflict:
    """One reused ticker the repair resolves: the delisted ISIN it is taken from, the live one."""

    symbol: str
    delisted_isin: str
    listed_isin: str
    #: The open `identity_reconciliation` row; `None` in a test database whose ids are not live.
    reconciliation_id: int | None = None


#: The ten, as raised at 2026-09-06 20:41 IST and investigated on 2026-10-23 against that day's L0
#: snapshot: each ticker is Active under the second ISIN and Delisted under the first.
EXPECTED_2026_09_06: Final[tuple[ExpectedConflict, ...]] = (
    ExpectedConflict("ALPHA", "INE506B01016", "INF174KA1IA5", 1),
    ExpectedConflict("BAYERCROP", "INE556A01013", "INE462A01022", 2),
    ExpectedConflict("BOSTON", "INE122C01010", "INE109B01019", 3),
    ExpectedConflict("CMSINFO", "INE915A01011", "INE925R01014", 4),
    ExpectedConflict("IDEA", "INE311B01011", "INE669E01016", 5),
    ExpectedConflict("MILLENNIUM", "INE258D01028", "INE570N01025", 6),
    ExpectedConflict("ONIDA", "INE328B01015", "INE831A01028", 7),
    ExpectedConflict("SAWACA", "INE185B01019", "INE248B01023", 8),
    ExpectedConflict("SUMMITSEC", "INE852A01016", "INE519C01017", 9),
    ExpectedConflict("SUPHA", "INE647C01016", "INE549S01036", 10),
)


class RepairRefusedError(IdentityError):
    """The store is not in the state the repair was written for. Nothing was written."""


@dataclass(frozen=True, slots=True)
class _StoredRow:
    id: int
    window: SymbolWindow
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class _OpenConflict:
    id: int
    symbols: tuple[str, ...]
    isins: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepairPlan:
    """Everything `apply_repair` will do, decided read-only."""

    snapshot_date: date
    already_applied: bool
    deletes: tuple[_StoredRow, ...] = ()
    resolutions: tuple[tuple[_OpenConflict, str], ...] = ()
    kept_bse_windows: int = 0
    conflicts_before: int = 0
    conflicts_after: int = 0

    def describe(self) -> list[str]:
        """The plan as lines a human reads before saying `--apply`."""
        if self.already_applied:
            return [f"repair_bse_delisted {self.snapshot_date}: already applied — nothing to do"]
        resolving = {isin for conflict, _ in self.resolutions for isin in conflict.isins}
        colliding = [row for row in self.deletes if row.window.isin in resolving]
        lines = [f"repair_bse_delisted {self.snapshot_date}: plan (one transaction)"]
        lines.append(
            f"  delete {len(self.deletes)} open BSE window(s) of delisted-only ISINs "
            f"({len(colliding)} on a reused ticker, {len(self.deletes) - len(colliding)} not):"
        )
        for row in colliding:
            lines.append(f"    - symbol_history id={row.id} {_window(row.window)}")
        lines.append(
            f"  keep {self.kept_bse_windows} BSE window(s), each exactly what the fixed derive "
            "produces; insert 0, close 0; security_master and exchange_listing untouched"
        )
        lines.append(f"  resolve {len(self.resolutions)} identity_reconciliation row(s):")
        for conflict, _reason in self.resolutions:
            lines.append(f"    * id={conflict.id} {list(conflict.symbols)} {list(conflict.isins)}")
        lines.append(
            f"  master-wide conflicts: {self.conflicts_before} before, {self.conflicts_after} after"
        )
        return lines


@dataclass(frozen=True, slots=True)
class RepairCounts:
    """What `apply_repair` changed."""

    deleted: int
    resolved: int


def plan_repair(
    conn: Connection,
    *,
    scrip_master_json: str,
    snapshot_date: date = REPAIR_SNAPSHOT_DATE,
    expected: Sequence[ExpectedConflict] = EXPECTED_2026_09_06,
) -> RepairPlan:
    """Decide the repair without writing. Raises `RepairRefusedError` on any surprise.

    What it does: re-derives `scrip_master_json` (the three statuses of `snapshot_date`, as one
    array) through the fixed `derive_master`; selects the stored BSE windows of the ISINs it lists
    only as delisted; requires each to be the shape that merge wrote, every other stored BSE window
    to be exactly what the fixed derive produces, and the master without them to hold no BSE
    conflict and nothing new; and pairs each expected conflict with its open reconciliation row.
    What it assumes: the caller's transaction; `scrip_master_json` is the snapshot the rows came
    from (`bse_scrip_refresh.read_snapshot_from_l0`).
    What it never does: write, or plan to delete a row that is not a delisted-only ISIN's.
    """
    scrips, _skipped = parse_scrip_master(scrip_master_json)
    derived = derive_master(scrips, snapshot_date=snapshot_date)
    gone = delisted_isins(scrips)
    for e in expected:
        if e.delisted_isin not in gone or e.listed_isin in gone:
            raise RepairRefusedError(
                f"{e.symbol}: the snapshot does not list {e.delisted_isin} as delisted-only and "
                f"{e.listed_isin} as listed"
            )

    rows = _bse_rows(conn)
    stale = [row for row in rows if row.window.isin in gone]
    kept = [row for row in rows if row.window.isin not in gone]

    problems = [
        f"{_window(row.window)} (source {row.window.source}, recorded {row.recorded_at})"
        for row in stale
        if not _written_by_the_merge(row, snapshot_date)
    ]
    if problems:
        raise RepairRefusedError(
            "a delisted-only ISIN's BSE window is not the shape the "
            f"{snapshot_date.isoformat()} merge wrote:\n  " + "\n  ".join(problems)
        )
    kept_windows = tuple(row.window for row in kept)
    plan = plan_history(kept_windows, derived.windows)
    if (
        not plan.is_empty
        or plan.refusals
        or {w.key for w in kept_windows} != {w.key for w in derived.windows}
    ):
        raise RepairRefusedError(
            "the store's other BSE windows are not exactly what the fixed derive produces "
            f"({len(plan.inserts)} to insert, {len(plan.closes)} to close, "
            f"{len(plan.refusals)} refused, {len(kept_windows)} stored vs "
            f"{len(derived.windows)} derived); refusing to guess"
        )

    if not stale:
        if _open_bse_conflicts(conn):
            raise RepairRefusedError(
                "no delisted window is left, yet BSE conflicts are still open; refusing to guess"
            )
        _log.info("identity.repair_bse_delisted.already_applied", snapshot_date=str(snapshot_date))
        return RepairPlan(snapshot_date=snapshot_date, already_applied=True)

    stale_keys = {row.window.key for row in stale}
    everything = IdentityStore(conn).load_windows()
    after = [w for w in everything if w.key not in stale_keys]
    before_conflicts = detect_conflicts(everything, source=BSE_SCRIP_MASTER_SOURCE)
    after_conflicts = detect_conflicts(after, source=BSE_SCRIP_MASTER_SOURCE)
    left_on_bse = [str(c) for c in after_conflicts if c.exchange is Exchange.BSE]
    if left_on_bse:
        raise RepairRefusedError(f"repair would still leave BSE conflicts: {left_on_bse}")
    new = set(map(_identity, after_conflicts)) - set(map(_identity, before_conflicts))
    if new:
        raise RepairRefusedError(f"repair would raise conflicts the store does not hold: {new}")
    removed = {
        (c.symbols, c.isins)
        for c in before_conflicts
        if c.exchange is Exchange.BSE and c.kind is ConflictKind.SYMBOL_TO_ISIN
    }
    want = {((e.symbol,), tuple(sorted((e.delisted_isin, e.listed_isin)))) for e in expected}
    if removed != want:
        raise RepairRefusedError(
            f"the BSE conflicts in the store are not the expected ones: "
            f"unexpected {sorted(removed - want)}, missing {sorted(want - removed)}"
        )

    resolutions = _resolutions(conn, expected, deleted=len(stale))
    return RepairPlan(
        snapshot_date=snapshot_date,
        already_applied=False,
        deletes=tuple(sorted(stale, key=lambda row: row.window.sort_key)),
        resolutions=resolutions,
        kept_bse_windows=len(kept),
        conflicts_before=len(before_conflicts),
        conflicts_after=len(after_conflicts),
    )


def apply_repair(conn: Connection, plan: RepairPlan, *, clock: Clock) -> RepairCounts:
    """Execute a `plan_repair` result in the caller's transaction. Does not commit.

    Every statement's row count is checked against the plan; a mismatch raises
    `RepairRefusedError` and the caller rolls back, so the repair lands whole or not at all.
    """
    if plan.already_applied:
        return RepairCounts(0, 0)
    deleted = conn.execute(
        "DELETE FROM symbol_history WHERE id = ANY(%s) AND exchange = %s AND source = %s "
        "AND valid_from = %s AND valid_to IS NULL RETURNING id",
        (
            [row.id for row in plan.deletes],
            Exchange.BSE.value,
            BSE_SCRIP_MASTER_SOURCE,
            plan.snapshot_date,
        ),
    ).fetchall()
    resolved = 0
    for conflict, reason in plan.resolutions:
        resolved += len(
            conn.execute(
                "UPDATE identity_reconciliation SET resolved = true, resolved_at = %s, "
                "resolution = %s WHERE id = %s AND NOT resolved RETURNING id",
                (clock.now(), reason, conflict.id),
            ).fetchall()
        )
    counts = RepairCounts(len(deleted), resolved)
    want = RepairCounts(len(plan.deletes), len(plan.resolutions))
    if counts != want:
        raise RepairRefusedError(f"repair wrote {counts}, planned {want}; roll back")
    _log.info(
        "identity.repair_bse_delisted.applied",
        source=BSE_SCRIP_MASTER_SOURCE,
        snapshot_date=plan.snapshot_date.isoformat(),
        deleted=counts.deleted,
        resolved=counts.resolved,
    )
    return counts


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def _bse_rows(conn: Connection) -> list[_StoredRow]:
    rows = conn.execute(
        "SELECT id, isin, symbol, series, valid_from, valid_to, source, recorded_at "
        "FROM symbol_history WHERE exchange = %s ORDER BY symbol, valid_from, isin",
        (Exchange.BSE.value,),
    ).fetchall()
    return [
        _StoredRow(
            id=int(row_id),
            window=SymbolWindow(
                Exchange.BSE,
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


def _written_by_the_merge(row: _StoredRow, snapshot_date: date) -> bool:
    """Opened by the `snapshot_date` merge: its source, its date, still open, written that day."""
    w = row.window
    return (
        w.source == BSE_SCRIP_MASTER_SOURCE
        and w.valid_from == snapshot_date
        and w.valid_to is None
        and row.recorded_at.astimezone(IST).date() == snapshot_date
    )


def _identity(conflict: IdentityConflict) -> tuple[object, ...]:
    """A conflict as the queue's UNIQUE key sees it — not the note or who detected it."""
    return (conflict.kind, conflict.exchange, conflict.on_date, conflict.symbols, conflict.isins)


def _open_bse_conflicts(conn: Connection) -> list[_OpenConflict]:
    rows = conn.execute(
        "SELECT id, symbols, isins FROM identity_reconciliation "
        "WHERE NOT resolved AND exchange = %s AND source = %s ORDER BY id",
        (Exchange.BSE.value, BSE_SCRIP_MASTER_SOURCE),
    ).fetchall()
    return [
        _OpenConflict(int(row_id), tuple(symbols), tuple(sorted(isins)))
        for row_id, symbols, isins in rows
    ]


def _resolutions(
    conn: Connection, expected: Iterable[ExpectedConflict], *, deleted: int
) -> tuple[tuple[_OpenConflict, str], ...]:
    """Each expected conflict's one open row (with the expected id when one is given), and why.

    Every open BSE row from this source must be one of the expected: a row the repair cannot name
    is a different defect, and it is refused rather than left to look resolved alongside these.
    """
    open_rows = _open_bse_conflicts(conn)
    out: list[tuple[_OpenConflict, str]] = []
    claimed: set[int] = set()
    for e in expected:
        isins = tuple(sorted((e.delisted_isin, e.listed_isin)))
        matches = [c for c in open_rows if c.symbols == (e.symbol,) and c.isins == isins]
        if len(matches) != 1 or (
            e.reconciliation_id is not None and matches[0].id != e.reconciliation_id
        ):
            raise RepairRefusedError(
                f"{e.symbol}: expected one open reconciliation row "
                f"{e.reconciliation_id or ''}, found {[c.id for c in matches]}"
            )
        claimed.add(matches[0].id)
        out.append(
            (
                matches[0],
                f"repair_bse_delisted: BSE ticker {e.symbol} is {e.listed_isin}'s; "
                f"{e.delisted_isin} is delisted, and the {REPAIR_SNAPSHOT_DATE.isoformat()} "
                "scrip-master merge opened a current window for it anyway. That window and the "
                f"{deleted - 1} other delisted-only ISINs' windows deleted; their listings and "
                "scrip codes kept.",
            )
        )
    unexpected = [c.id for c in open_rows if c.id not in claimed]
    if unexpected:
        raise RepairRefusedError(
            f"open BSE reconciliation rows the repair cannot name: {unexpected}"
        )
    return tuple(out)


def _window(w: SymbolWindow) -> str:
    end = w.valid_to.isoformat() if w.valid_to else "open"
    return f"{w.exchange.value} {w.symbol} {w.isin} [{w.valid_from.isoformat()}..{end}]"


def main(argv: Sequence[str] | None = None) -> int:
    """Print the plan; with `--apply`, execute it in one transaction and commit."""
    from dataplatform.config import get_settings
    from dataplatform.ingest.bse_scrip_refresh import read_snapshot_from_l0
    from dataplatform.logging import configure_logging
    from dataplatform.store.db import connection
    from dataplatform.store.l0 import L0Store

    parser = argparse.ArgumentParser(
        prog="identity.repair_bse_delisted",
        description=__doc__.splitlines()[0] if __doc__ else None,
    )
    parser.add_argument(
        "--apply", action="store_true", help="execute and commit (default: dry run, read only)"
    )
    args = parser.parse_args(argv)
    configure_logging()

    clock = SystemClock()
    settings = get_settings()
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    snapshot = read_snapshot_from_l0(l0, REPAIR_SNAPSHOT_DATE)
    with connection(settings) as conn:
        if args.apply:
            # Held to commit: no ingest or resolve can change either table between the plan
            # being checked and it being written. Reads (the dry run, resolvers) still proceed.
            conn.execute(
                "LOCK TABLE symbol_history, identity_reconciliation IN SHARE ROW EXCLUSIVE MODE"
            )
        else:
            conn.execute("SET TRANSACTION READ ONLY")
        try:
            plan = plan_repair(conn, scrip_master_json=snapshot)
            print("\n".join(plan.describe()))
            if not args.apply:
                conn.rollback()
                print("dry run: nothing written (READ ONLY transaction). Re-run with --apply.")
                return 0
            counts = apply_repair(conn, plan, clock=clock)
        except IdentityError as error:
            conn.rollback()
            print(f"repair_bse_delisted refused: {error}", file=sys.stderr)
            return 2
        conn.commit()
    print(f"applied: {counts.deleted} deleted, {counts.resolved} reconciliation row(s) resolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
