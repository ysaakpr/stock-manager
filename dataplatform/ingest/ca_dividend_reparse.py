"""Re-derive the same-day dividends the one-dividend key dropped, offline from L0.

Until migration 0020 the store held one dividend per feed per ex-date, so a regular and a special
dividend declared together kept whichever row landed first: BSE's second record and NSE's second
segment were dropped, and NSE's `&`/`+`/`And` lines were stored as one bare dividend with no
amount. The fixed parser (`parse_terms.split_dividends`, component-keyed rows) produces them all,
but the writer is ON CONFLICT DO NOTHING — re-ingesting cannot replace the stale bare row — so
this command does the one-off, in one transaction:

1. re-parses every `nse_corp_actions` and `bse_corp_actions` L0 payload (no request), each with a
   clock frozen at its own `fetched_at`, so a fallback knowable date is the one its first ingest
   gave; where several payloads carry one row, the earliest-fetched wins, as the writer's
   first-write-wins did;
2. selects the *day-groups* — one feed's dividends for one ISIN on one ex-date — the fix changes:
   those the re-parse yields more than one dividend for, and the store already holds one for (the
   old key kept the first; a day-group with nothing stored was never fetched or never resolved,
   which is not this fix's to change). Nothing else is touched;
3. in each, deletes the stored rows the re-parse does not reproduce exactly and inserts the ones it
   adds. A new row takes its stored siblings' knowable date: it came from the same line or
   payload, so it was knowable exactly when they were (invariant #7);
4. runs the CA finalize under the live policy (`ca_refresh.REFRESH_POLICY`) with `changed_since`,
   so only ISINs whose reconciled dividends moved are recomputed and queued for L2.

The L2 drain is left to `l2_fill --rebuild-invalidated`, as after any refresh.

What it never does: fetch, touch a non-dividend row, or touch a day-group the re-parse yields one
dividend for. Dry run (the default) opens a READ ONLY transaction and prints the plan.

    uv run python -m dataplatform.ingest.ca_dividend_reparse            # dry run: print the plan
    uv run python -m dataplatform.ingest.ca_dividend_reparse --apply    # one transaction, commits
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from dataplatform.clock import Clock, FrozenClock, SystemClock
from dataplatform.corpactions.taxonomy import ActionType
from dataplatform.identity.lineage import LineageResolver
from dataplatform.identity.master import IdentityMaster
from dataplatform.ingest.bse import corp_actions as bse_ca
from dataplatform.ingest.corp_actions import (
    CorporateAction,
    build_scrip_index,
    load_corporate_actions,
    write_corporate_actions,
)
from dataplatform.ingest.nse import corp_actions as nse_ca
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection
from dataplatform.store.l0 import L0Store

__all__ = [
    "DayGroup",
    "ReparsePlan",
    "apply_reparse",
    "main",
    "plan_reparse",
    "reparse_dividends",
]

_LOG = get_logger(__name__)

#: The two feeds whose dividends this re-derives.
SOURCES: Final = (nse_ca.SOURCE_ID, bse_ca.SOURCE_ID)

#: One feed's dividends for one ISIN on one ex-date.
DayKey = tuple[str, date, str]


@dataclass(frozen=True, slots=True)
class DayGroup:
    """One day-group the fix changes: what the store holds, and what the re-parse yields."""

    key: DayKey
    stored: tuple[CorporateAction, ...]
    reparsed: tuple[CorporateAction, ...]

    @property
    def deletes(self) -> tuple[CorporateAction, ...]:
        """Stored rows the re-parse does not reproduce — same component and terms — exactly."""
        wanted = {(a.component, a.terms) for a in self.reparsed}
        return tuple(a for a in self.stored if (a.component, a.terms) not in wanted)

    @property
    def inserts(self) -> tuple[CorporateAction, ...]:
        """Re-parsed rows the store does not hold, dated knowable as their stored siblings were."""
        held = {(a.component, a.terms) for a in self.stored}
        knowable = min((a.knowable_date for a in self.stored), default=None)
        return tuple(
            a if knowable is None else a.model_copy(update={"knowable_date": knowable})
            for a in self.reparsed
            if (a.component, a.terms) not in held
        )


@dataclass(frozen=True, slots=True)
class ReparsePlan:
    """Every day-group the re-parse changes, decided read-only."""

    groups: tuple[DayGroup, ...] = ()
    payloads: Mapping[str, int] = field(default_factory=dict)
    #: Day-groups the re-parse yields several dividends for but the store holds none of; left alone.
    unstored: int = 0

    @property
    def deletes(self) -> tuple[CorporateAction, ...]:
        return tuple(a for g in self.groups for a in g.deletes)

    @property
    def inserts(self) -> tuple[CorporateAction, ...]:
        return tuple(a for g in self.groups for a in g.inserts)

    def describe(self) -> list[str]:
        """The plan as lines a human reads before saying `--apply`."""
        changed = [g for g in self.groups if g.deletes or g.inserts]
        by_source: dict[str, int] = defaultdict(int)
        for g in changed:
            by_source[g.key[2]] += 1
        return [
            "ca_dividend_reparse: plan (one transaction)",
            f"  payloads re-parsed: {dict(self.payloads)}",
            f"  day-groups with several dividends: {len(self.groups)}, "
            f"of which the store differs in {len(changed)} ({dict(by_source)})",
            f"  left alone, nothing stored for them: {self.unstored}",
            f"  delete {len(self.deletes)} stored dividend row(s); "
            f"insert {len(self.inserts)} re-parsed one(s)",
            "  then: reconcile + recompute the ISINs whose reconciled dividends moved; "
            "L2 via `l2_fill --rebuild-invalidated`",
        ]


def reparse_dividends(
    l0: L0Store,
    *,
    master: IdentityMaster,
    lineage: LineageResolver,
) -> tuple[dict[DayKey, tuple[CorporateAction, ...]], dict[str, int]]:
    """Every dividend both feeds' L0 payloads yield, by day-group, earliest-fetched payload first.

    Pure over L0 and the identity master: no database write, no request. A row two payloads both
    carry (a yearly NSE chunk and a later refresh window; a scrip's backfill and its refresh) is
    taken from the earlier-fetched one, which is the row the first-write-wins writer kept.
    """
    scrip_index = build_scrip_index(master)
    first: dict[tuple[str, date, str, str], CorporateAction] = {}
    payloads: dict[str, int] = {}
    for source in SOURCES:
        refs = sorted(l0.iter_refs(source), key=lambda ref: (ref.fetched_at, ref.key))
        payloads[source] = len(refs)
        for ref in refs:
            clock = FrozenClock(ref.fetched_at)
            if source == nse_ca.SOURCE_ID:
                result = nse_ca.parse_l0(l0, ref, master=master, clock=clock, lineage=lineage)
            else:
                result = bse_ca.parse_l0(l0, ref, scrip_index=scrip_index, clock=clock)
            for action in result.actions:
                if action.action_type is not ActionType.DIVIDEND:
                    continue
                key = (action.isin, action.ex_date, action.source, action.component)
                first.setdefault(key, action)
    grouped: dict[DayKey, list[CorporateAction]] = defaultdict(list)
    for (isin, ex_date, source, _component), action in sorted(first.items()):
        grouped[(isin, ex_date, source)].append(action)
    return {key: tuple(rows) for key, rows in grouped.items()}, payloads


def plan_reparse(
    stored_actions: Iterable[CorporateAction],
    reparsed: Mapping[DayKey, Sequence[CorporateAction]],
    *,
    payloads: Mapping[str, int] | None = None,
) -> ReparsePlan:
    """The day-groups the re-parse yields several dividends for, beside what the store holds.

    Pure: `stored_actions` is the store as `load_corporate_actions` reads it.
    """
    stored: dict[DayKey, list[CorporateAction]] = defaultdict(list)
    for action in stored_actions:
        if action.action_type is ActionType.DIVIDEND and action.source in SOURCES:
            stored[(action.isin, action.ex_date, action.source)].append(action)
    several = {key: rows for key, rows in sorted(reparsed.items()) if len(rows) > 1}
    groups = tuple(
        DayGroup(key=key, stored=tuple(stored[key]), reparsed=tuple(rows))
        for key, rows in several.items()
        if stored.get(key)
    )
    return ReparsePlan(
        groups=groups,
        payloads=dict(payloads or {}),
        unstored=sum(1 for key in several if not stored.get(key)),
    )


@dataclass(frozen=True, slots=True)
class ReparseCounts:
    """What `apply_reparse` changed in `corporate_actions`."""

    deleted: int
    inserted: int


def apply_reparse(conn: Connection, plan: ReparsePlan, *, clock: Clock) -> ReparseCounts:
    """Execute a `plan_reparse` result in the caller's transaction. Does not commit.

    Each delete names its row by the full natural key; every count is checked against the plan
    and a mismatch raises, so the caller rolls back and the store is left as it was.
    """
    deleted = 0
    for action in plan.deletes:
        deleted += len(
            conn.execute(
                "DELETE FROM corporate_actions WHERE isin = %s AND ex_date = %s "
                "AND action_type = %s AND source = %s AND component = %s RETURNING id",
                (
                    action.isin,
                    action.ex_date,
                    action.action_type.value,
                    action.source,
                    action.component,
                ),
            ).fetchall()
        )
    inserted = write_corporate_actions(conn, plan.inserts, clock=clock).inserted
    counts = ReparseCounts(deleted=deleted, inserted=inserted)
    want = ReparseCounts(deleted=len(plan.deletes), inserted=len(plan.inserts))
    if counts != want:
        raise RuntimeError(f"ca_dividend_reparse wrote {counts}, planned {want}; roll back")
    _LOG.info(
        "ca_dividend_reparse.applied",
        deleted=counts.deleted,
        inserted=counts.inserted,
        groups=len(plan.groups),
        state="NORMALIZED",
    )
    return counts


def _changed_isins(plan: ReparsePlan) -> Iterable[str]:
    return sorted({g.key[0] for g in plan.groups if g.deletes or g.inserts})


def main(argv: Sequence[str] | None = None) -> int:
    """Print the plan; with `--apply`, re-derive, finalize, and commit in one transaction."""
    from dataplatform.config import get_settings
    from dataplatform.identity.lineage import LineageStore
    from dataplatform.identity.master import IdentityStore
    from dataplatform.ingest.ca_refresh import REFRESH_POLICY
    from dataplatform.ingest.corp_actions_backfill import (
        finalize_reconcile_and_recompute,
        reconciled_fingerprints,
    )
    from dataplatform.logging import configure_logging
    from dataplatform.store.db import connection

    parser = argparse.ArgumentParser(
        prog="ingest.ca_dividend_reparse",
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
    with connection(settings) as conn:
        if not args.apply:
            conn.execute("SET TRANSACTION READ ONLY")
        master = IdentityStore(conn, clock=clock).load_master()
        lineage = LineageStore(conn, clock=clock).load()
        reparsed, payloads = reparse_dividends(l0, master=master, lineage=lineage)
        plan = plan_reparse(load_corporate_actions(conn), reparsed, payloads=payloads)
        print("\n".join(plan.describe()))
        print(f"  ISINs whose stored dividends change: {len(list(_changed_isins(plan)))}")
        if not args.apply:
            conn.rollback()
            print("dry run: nothing written (READ ONLY transaction). Re-run with --apply.")
            return 0
        before = reconciled_fingerprints(conn)
        counts = apply_reparse(conn, plan, clock=clock)
        finalize = finalize_reconcile_and_recompute(
            conn,
            clock=clock,
            single_source_policy=REFRESH_POLICY,
            changed_since=before,
        )
        conn.commit()
    print(f"applied: {counts.deleted} deleted, {counts.inserted} inserted; finalize {finalize}")
    print("next: uv run python -m dataplatform.store.l2_fill --rebuild-invalidated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
