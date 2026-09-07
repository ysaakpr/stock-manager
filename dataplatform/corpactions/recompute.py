"""D3 (M2.4): the retroactive recompute seam — rewrite an ISIN's factor chain, invalidate its L2.

§4.3 rule 2: *"a new corporate action triggers retroactive recompute of the full factor chain for
that ISIN + invalidation of L2."* This module is that trigger's body. Everything in
``dataplatform.corpactions.factors`` is pure — raw prices and terms in, factors and series out; this
is the thin database seam that persists a rebuilt chain and records that the derived L2 is stale.

The shape mirrors M2.2/M2.3's writers exactly, and for the same reason: the caller owns the
transaction, so a corporate-action ingest and the recompute it triggers commit together, and a
recompute never half-applies. Two properties make the rewrite safe:

* **Full rewrite, per ISIN.** ``recompute_isin`` deletes *every* ``adjustment_factors`` row for the
  ISIN and re-inserts the freshly built chain. A factor chain is not incrementally patchable — a
  backfilled 2019 split changes the cumulative factor of every earlier row — so "rewrite the ISIN's
  full adjusted history" (the task's acceptance) is a delete-and-rebuild, not an upsert. It is
  read back through ``load_reconciled_actions``, so an unreconciled action is physically absent from
  the input (invariant enforced by M2.3), and a reconciliation that has *unreconciled* an action
  since the last run correctly drops its factor.

* **L2 is invalidated, never silently left stale.** The recompute cannot rebuild the derived L2
  Parquet (that is M2.5's materializer), so it writes one open row to ``l2_invalidation`` naming the
  ISIN. Bad or stale L2 must never become a decision (invariant #10); an unresolved invalidation is
  a visible instruction to rebuild. Idempotent: a second recompute of an ISIN whose L2 is already
  flagged does not stack a duplicate open row.

Time is injected (B10): ``computed_at`` and ``requested_at`` come from the ``Clock``, never a wall
clock. Nothing here fetches. Money and factors are ``Decimal``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

from dataplatform.clock import Clock
from dataplatform.corpactions.factors import (
    PRICE_EVENT_TYPES,
    STRUCTURAL_BREAK_TYPES,
    FactorError,
    build_chain_for_isin,
)
from dataplatform.corpactions.reconcile import load_reconciled_actions
from dataplatform.corpactions.taxonomy import ActionType
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

if TYPE_CHECKING:
    # Annotation-only (see factors.py) — kept off the runtime import graph.
    from dataplatform.ingest.corp_actions import CorporateAction

__all__ = [
    "RecomputeResult",
    "recompute_for_actions",
    "recompute_isin",
    "recompute_isins",
]

_LOG = get_logger(__name__)

#: The action types whose arrival makes an ISIN's L2 stale: everything that moves the price basis or
#: is a structural break (the price-adjusted series) plus dividends (total-return). A name
#: change or a buyback price alters no series, so it alone does not invalidate L2.
_SERIES_AFFECTING: Final[frozenset[ActionType]] = (
    PRICE_EVENT_TYPES | STRUCTURAL_BREAK_TYPES | frozenset({ActionType.DIVIDEND})
)

_DELETE_FACTORS_SQL: Final = "DELETE FROM adjustment_factors WHERE isin = %s"

_INSERT_FACTOR_SQL: Final = (
    "INSERT INTO adjustment_factors "
    "(isin, ex_date, price_factor, qty_factor, cum_price_factor, cum_qty_factor, "
    " corporate_action_id, structural_break, computed_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

_OPEN_INVALIDATION_SQL: Final = (
    "SELECT 1 FROM l2_invalidation WHERE isin = %s AND NOT resolved LIMIT 1"
)

_INSERT_INVALIDATION_SQL: Final = (
    "INSERT INTO l2_invalidation (isin, reason, from_date, requested_at) VALUES (%s, %s, %s, %s)"
)


class RecomputeResult(BaseModel):
    """What one ISIN's recompute changed — the counts a caller (and the acceptance test) checks.

    ``factor_rows_written`` is the size of the rebuilt chain (0 for an ISIN with only dividends).
    ``l2_invalidated`` is true when a new open ``l2_invalidation`` row was written; ``False`` with
    ``invalidation_skipped`` true means the ISIN's L2 was already flagged stale (idempotent), and
    ``False`` with neither means nothing series-affecting exists to invalidate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    isin: str
    factor_rows_written: int = 0
    l2_invalidated: bool = False
    invalidation_skipped: bool = False


def recompute_isin(
    conn: Connection,
    isin: str,
    *,
    clock: Clock,
    reason: str = "corporate action recompute",
) -> RecomputeResult:
    """Rebuild one ISIN's full factor chain and flag its L2 stale (§4.3 rule 2).

    What it does: reads the ISIN's reconciled actions through ``load_reconciled_actions`` (never the
    raw table — an unreconciled action must not feed a factor, M2.3), builds the full chain, deletes
    the ISIN's existing ``adjustment_factors`` rows and inserts the rebuilt chain, then records an
    open ``l2_invalidation`` row unless one already stands. Returns the counts.

    What it assumes: the caller owns the transaction and commits it — as with M2.2's writer, so a CA
    ingest and its recompute share one commit. The clock is injected.

    What it never does: patch the chain in place (a new action re-scales every earlier row, so the
    rewrite is whole), reach an unreconciled action, or rebuild the L2 Parquet itself (that is M2.5,
    driven by the invalidation this writes).
    """
    actions = load_reconciled_actions(conn, isin=isin)
    chain = build_chain_for_isin(isin, actions)
    now = clock.now()

    conn.execute(_DELETE_FACTORS_SQL, (isin,))
    for row in chain.rows:
        conn.execute(
            _INSERT_FACTOR_SQL,
            (
                row.isin,
                row.ex_date,
                row.price_factor,
                row.qty_factor,
                row.cum_price_factor,
                row.cum_qty_factor,
                None,  # corporate_action_id: the chain is per ex-date, not per source row
                row.structural_break,
                now,
            ),
        )

    l2_invalidated = False
    invalidation_skipped = False
    from_date = _earliest_series_ex_date(actions)
    if from_date is not None:
        exists = conn.execute(_OPEN_INVALIDATION_SQL, (isin,)).fetchone()
        if exists is None:
            conn.execute(_INSERT_INVALIDATION_SQL, (isin, reason, from_date, now))
            l2_invalidated = True
        else:
            invalidation_skipped = True

    result = RecomputeResult(
        isin=isin,
        factor_rows_written=len(chain.rows),
        l2_invalidated=l2_invalidated,
        invalidation_skipped=invalidation_skipped,
    )
    _LOG.info(
        "ca.factors_recomputed",
        isin=isin,
        factor_rows=result.factor_rows_written,
        l2_invalidated=result.l2_invalidated,
        invalidation_skipped=result.invalidation_skipped,
        state="RECOMPUTED",
    )
    return result


def recompute_isins(
    conn: Connection,
    isins: Iterable[str],
    *,
    clock: Clock,
    reason: str = "corporate action recompute",
) -> tuple[RecomputeResult, ...]:
    """Recompute several ISINs, deduplicating the input, in a stable (sorted) order.

    One ISIN's `FactorError` does not stop the others. It used to: a single unquantifiable split
    raised out of this generator, and because the whole finalize is one transaction it rolled back
    every other ISIN's rebuilt chain too — 19,034 reconciled actions and thousands of good factor
    rows discarded over one bad action. The failure is still loud (a warning naming the ISIN and
    the reason, and the ISIN is absent from the returned results), but it is now *this* ISIN's
    failure rather than the batch's.

    Only `FactorError` is caught, and deliberately so: it means "these actions cannot produce a
    chain", which is a per-ISIN data fact. A database error is not, and must still abort the
    transaction rather than leave half a recompute committed.
    """
    results: list[RecomputeResult] = []
    for isin in sorted(set(isins)):
        try:
            results.append(recompute_isin(conn, isin, clock=clock, reason=reason))
        except FactorError as exc:
            _LOG.warning(
                "ca.factors_unbuildable",
                isin=isin,
                reason=str(exc)[:200],
                state="SKIPPED",
            )
    return tuple(results)


def recompute_for_actions(
    conn: Connection,
    actions: Iterable[CorporateAction],
    *,
    clock: Clock,
    reason: str = "corporate action recompute",
) -> tuple[RecomputeResult, ...]:
    """Recompute exactly the ISINs touched by a batch of newly ingested actions.

    The convenience the daily/backfill CA writer calls after landing a batch: it extracts the
    distinct ISINs the batch touched and recomputes each one's full chain. Each ISIN is still
    rebuilt from *all* its reconciled actions, not just the new ones — a chain is only correct
    as a whole.
    """
    return recompute_isins(conn, (a.isin for a in actions), clock=clock, reason=reason)


def _earliest_series_ex_date(actions: Iterable[CorporateAction]) -> date | None:
    """The earliest ex-date among the ISIN's series-affecting actions, or ``None`` if none exist.

    ``None`` means nothing to invalidate — the ISIN has no split, bonus, break or dividend
    (e.g. only a name change), so its L2 series are unchanged and no rebuild is owed. Otherwise the
    returned date is the ``from_date`` hint on the invalidation row (back-adjustment re-scales all
    earlier history, so a materializer may treat it as "from here back").
    """
    dates = [a.ex_date for a in actions if a.action_type in _SERIES_AFFECTING]
    return min(dates) if dates else None
