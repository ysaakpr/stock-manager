"""D3 (M2.3): reconcile NSE's and BSE's descriptions of the same corporate action.

Both exchanges publish the same universe of corporate actions, and §4.1 row 5 states the problem
plainly: *"the two exchanges describe the same action differently — reconciliation needed."* NSE
says `FV SPLIT FROM RS.10/- TO RS.2/-`; BSE says `Stock Split From Rs.10/- to Rs.2/-`; M2.1
normalizes both to the same `FaceValueTerms(10 → 2)` and M2.2 lands them as two
`CorporateAction` rows, one per source. This module decides, for each real-world action, whether
the two rows *agree*.

The output is deliberately two-sided, and the asymmetry is the whole point:

* **Agreement → one `ReconciledAction`.** Same ISIN, same type, ex-dates within tolerance, and
  byte-for-byte equal structured terms. Both raw `CorporateAction`s are carried on it, so the
  reconciled row is never a claim divorced from its evidence — a human (or a later re-parse) can
  always see the two strings that were found to agree.

* **Disagreement → the reconciliation queue.** A different ratio, an ex-date too far apart to be
  the same event, or an action only one exchange published: each becomes a
  `ReconciliationConflict` holding the raw records *side by side*, and each is written to the D7
  `quality_flag` table so it surfaces on `GET /status/quality` (§4.4) for a human to resolve.

The invariant this exists to protect is the last one in the task spec: **an unreconciled action
is a known-unknown, and a known-unknown may not feed the factor chain.** Reconciliation never
guesses a winner between two disagreeing feeds — guessing is exactly how one ISIN's ten years of
adjusted history gets silently rewritten (risk register row 1). So the factor chain (M2.4) reads
its actions through `load_reconciled_actions` (which returns only `reconciled = true` rows) and
consumes `ReconciledAction`, a type this module alone can mint; `eligible_for_factor_chain` is the
same gate over an in-memory result. A raw `CorporateAction` whose sources disagreed can reach the
queue and a human, but it cannot reach a factor.

Two halves, as elsewhere in D3. `reconcile()` and everything it returns are pure: they take
`CorporateAction`s and return a `ReconciliationResult`, with no database, no network and no clock,
so the matching rules are tested offline and deterministically. `persist_reconciliation` and
`load_reconciled_actions` are the thin database seam — they mark rows, write flags, and read the
reconciled subset back — and take the clock injected (B10), never a wall clock.

Money is `Decimal` (inherited from the terms models). Dates are `datetime.date`. Nothing here
reads `datetime.now()`.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from dataplatform.clock import Clock
from dataplatform.corpactions.taxonomy import (
    TERMS_ADAPTER,
    ActionType,
    Terms,
    describe,
)
from dataplatform.ingest.models import ISIN_PATTERN, IngestError
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

if TYPE_CHECKING:
    from dataplatform.ingest.corp_actions import CorporateAction

__all__ = [
    "CA_RECONCILIATION_CHECK",
    "DEFAULT_EX_DATE_TOLERANCE_DAYS",
    "PersistCounts",
    "QualityFlagRecord",
    "ReconcileError",
    "ReconciledAction",
    "ReconciliationConflict",
    "ReconciliationReason",
    "ReconciliationResult",
    "eligible_for_factor_chain",
    "load_reconciled_actions",
    "persist_reconciliation",
    "reconcile",
]

_LOG = get_logger(__name__)

#: How far two ex-dates may sit apart and still be treated as *the same event* rather than two
#: different actions. NSE and BSE almost always agree on the ex-date to the day; the window exists
#: only to absorb the occasional off-by-one, and a difference wider than this is itself reported
#: (`EX_DATE_MISMATCH`) rather than silently bridged.
DEFAULT_EX_DATE_TOLERANCE_DAYS: Final = 2

#: The `quality_flag.check_name` every reconciliation disagreement is filed under. One name so a
#: human filtering `/status/quality` sees the whole reconciliation queue with one predicate.
CA_RECONCILIATION_CHECK: Final = "ca_reconciliation"


class ReconcileError(IngestError):
    """A reconciliation input the engine cannot process — a defect, not a data disagreement.

    Distinct from a `ReconciliationConflict`: a conflict is the *expected* outcome of two feeds
    describing an action differently and belongs in the queue. This is raised only when the input
    itself is malformed — most concretely, more than two distinct sources for one `(ISIN, type)`,
    which the two-exchange design has no meaning for and must not quietly pick two of.
    """


class ReconciliationReason(StrEnum):
    """Why a pair of feed rows did not reconcile. Each names a *different* human fix.

    Kept separate rather than collapsed into one "mismatch" because the resolution differs: a
    ratio disagreement is a parse or a feed error to adjudicate, an ex-date disagreement is a
    calendar question, and a single-source action is usually a listing fact (the security trades
    on one exchange) that a human confirms once.
    """

    RATIO_MISMATCH = "RATIO_MISMATCH"
    """Same ISIN, type and ex-date, but the two exchanges state different terms. The dangerous
    one: picking a side would rewrite the ISIN's adjusted history the wrong way."""

    EX_DATE_MISMATCH = "EX_DATE_MISMATCH"
    """Both exchanges announced an action of this type for this ISIN, but on ex-dates too far
    apart to be confidently the same event."""

    SINGLE_SOURCE = "SINGLE_SOURCE"
    """Only one exchange published this action. Often benign (a single-listed security), but never
    silently assumed to be: an unconfirmed action is still a known-unknown to the factor chain."""


#: The severity each reason carries on `/status/quality`. A contradiction between the feeds is an
#: ERROR (a human must adjudicate before the factor chain can trust the action); a single-source
#: action is a WARN (frequently a listing fact, not a defect). Both block the factor chain equally
#: — severity is the operator-attention signal, not the enforcement, which is `reconciled = false`.
_SEVERITY: Final[dict[ReconciliationReason, str]] = {
    ReconciliationReason.RATIO_MISMATCH: "ERROR",
    ReconciliationReason.EX_DATE_MISMATCH: "ERROR",
    ReconciliationReason.SINGLE_SOURCE: "WARN",
}


def _record_view(action: CorporateAction) -> dict[str, object]:
    """One `CorporateAction` as the JSON-safe blob the queue shows a human.

    Carries the source, ex-date, type, structured terms (through the same discriminated adapter
    the store uses, so Decimals serialize identically), the verbatim purpose string, and the
    rendered sentence — everything needed to judge the disagreement without re-opening the feed.
    """
    return {
        "source": action.source,
        "isin": action.isin,
        "ex_date": action.ex_date.isoformat(),
        "action_type": action.action_type.value,
        "terms": TERMS_ADAPTER.dump_python(action.terms, mode="json"),
        "raw_text": action.raw_text,
        "described": describe(action.action_type, action.terms),
        "knowable_date": action.knowable_date.isoformat(),
        "l0_key": action.l0_key,
    }


class QualityFlagRecord(BaseModel):
    """A D7 `quality_flag` row a reconciliation disagreement becomes — the queue, persisted.

    This is the exact shape `persist_reconciliation` inserts and `GET /status/quality` reads back:
    `detail` carries the two raw records side by side (`records`), the reason, and a `fingerprint`
    that makes the write idempotent — re-running reconciliation over an unchanged disagreement
    must not stack duplicate flags. `source` is set to one involved feed rather than left NULL on
    purpose: a NULL-source ERROR flag counts market-wide in the trading interlock
    (`SyncStateStore.open_error_flags`), and a disagreement about one ISIN must not halt the whole
    market — the scoping lives here, while both sources remain visible in `detail`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_date: date = Field(description="The action's ex-date — the date the flag is about.")
    check_name: str = Field(description="Always CA_RECONCILIATION_CHECK.")
    severity: Literal["ERROR", "WARN"]
    isin: str = Field(pattern=ISIN_PATTERN)
    source: str | None = Field(description="One involved feed, so the flag scopes to it, not all.")
    detail: dict[str, object] = Field(description="reason, action_type, fingerprint, both records")
    fingerprint: str = Field(description="Stable id of this disagreement; dedupes re-runs.")


class ReconciledAction(BaseModel):
    """One corporate action both exchanges agree on — the only kind the factor chain may consume.

    What it holds: the canonical identity and terms of the agreed action, plus **both** raw
    `CorporateAction`s in `sources`, so a reconciled row is never separated from the two feed rows
    that were found to agree. `reconciled` is `Literal[True]` and this type can only be minted by
    `reconcile()`, which is what makes "only reconciled actions reach a factor" a type-level fact
    and not a convention a caller has to remember.

    What it never does: choose between disagreeing feeds. If the two rows did not agree, there is
    no `ReconciledAction` — there is a `ReconciliationConflict` in the queue instead.

    Canonical fields when the two rows differ only within tolerance: `ex_date` is the earlier of
    the two (deterministic and source-agnostic), and `knowable_date` is the *later* — the action
    is only known to be reconciled once both feeds have arrived, so a decision may not see the
    reconciled verdict before then (invariant #7). Any within-tolerance ex-date difference is
    recorded in `reconciliation_note`; nothing is lost.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    isin: str = Field(pattern=ISIN_PATTERN)
    ex_date: date
    action_type: ActionType
    terms: Terms
    knowable_date: date
    record_date: date | None = None
    reconciled: Literal[True] = True
    reconciliation_note: str | None = None
    sources: tuple[CorporateAction, ...] = Field(min_length=2)

    @property
    def source_ids(self) -> tuple[str, ...]:
        """The feed ids that agreed, sorted — e.g. `('bse_corp_actions', 'nse_corp_actions')`."""
        return tuple(sorted(action.source for action in self.sources))

    def describe(self) -> str:
        """The agreed action as one human-readable sentence (the M2.1 renderer)."""
        return describe(self.action_type, self.terms)


class ReconciliationConflict(BaseModel):
    """A disagreement between the feeds — one entry in the reconciliation queue.

    Carries the reason and the raw records **side by side** (`records`): two for a ratio or
    ex-date disagreement, one for a single-source action. It never carries a resolution — that is
    the human's, made on `/status/quality`; this is only the evidence, kept whole.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: ReconciliationReason
    isin: str = Field(pattern=ISIN_PATTERN)
    action_type: ActionType
    records: tuple[CorporateAction, ...] = Field(min_length=1)

    @property
    def severity(self) -> Literal["ERROR", "WARN"]:
        """How loudly `/status/quality` reports it (see `_SEVERITY`)."""
        sev = _SEVERITY[self.reason]
        # narrow for the type checker: _SEVERITY only ever holds these two literals.
        return "ERROR" if sev == "ERROR" else "WARN"

    def fingerprint(self) -> str:
        """A stable identifier for *this specific disagreement*, so re-runs do not duplicate it.

        Built from the ISIN, type, reason and each record's (source, ex-date, raw text) — the
        things that make two runs the same disagreement. It deliberately excludes anything a
        re-parse might improve, so a genuinely changed disagreement gets a new flag while an
        unchanged one is recognised and skipped.
        """
        parts = [self.isin, self.action_type.value, self.reason.value]
        for action in sorted(self.records, key=lambda a: (a.source, a.ex_date, a.raw_text)):
            parts.extend((action.source, action.ex_date.isoformat(), action.raw_text))
        digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
        return digest[:32]

    def as_quality_flag(self, *, clock: Clock) -> QualityFlagRecord:
        """Project this disagreement onto the D7 `quality_flag` row `/status/quality` serves.

        `logical_date` is the earliest ex-date among the records; `source` is one involved feed
        (so the flag scopes to it in the trading interlock rather than halting the whole market),
        while both feeds live in `detail.records`. The clock is injected — this stamps nothing
        from the wall clock.
        """
        fingerprint = self.fingerprint()
        logical_date = min(action.ex_date for action in self.records)
        primary_source = min(action.source for action in self.records)
        detail: dict[str, object] = {
            "reason": self.reason.value,
            "action_type": self.action_type.value,
            "fingerprint": fingerprint,
            "records": [_record_view(action) for action in self.records],
        }
        return QualityFlagRecord(
            logical_date=logical_date,
            check_name=CA_RECONCILIATION_CHECK,
            severity=self.severity,
            isin=self.isin,
            source=primary_source,
            detail=detail,
            fingerprint=fingerprint,
        )


class ReconciliationResult(BaseModel):
    """Everything one reconciliation pass produced: the agreed actions and the queue.

    `reconciled` is what may feed the factor chain (via `eligible_for_factor_chain`); `queue` is
    what a human must resolve. The two are exhaustive over the input — every action given to
    `reconcile` ends up represented in exactly one of them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reconciled: tuple[ReconciledAction, ...] = ()
    queue: tuple[ReconciliationConflict, ...] = ()

    @property
    def is_clean(self) -> bool:
        """True when no disagreement was found — every action reconciled."""
        return not self.queue


def _terms_agree(left: Terms, right: Terms) -> bool:
    """Whether two structured terms state the *same* action.

    Value equality on the frozen terms models: same type and numerically equal fields
    (`Decimal("1") == Decimal("1.0")`, so a feed writing `1` and one writing `1.0` agree). This is
    intentionally exact rather than economically clever — deciding that a 10→2 face-value split and
    a 100→20 one are "equivalent" is the factor chain's arithmetic (M2.4), and at reconciliation
    time an unexplained difference in the stated numbers is precisely what a human should see.
    """
    return type(left) is type(right) and left == right


def _match_by_ex_date(
    left: Sequence[CorporateAction],
    right: Sequence[CorporateAction],
    tolerance: timedelta,
) -> tuple[
    list[tuple[CorporateAction, CorporateAction]], list[CorporateAction], list[CorporateAction]
]:
    """Pair actions of one `(ISIN, type)` across two feeds by ex-date proximity.

    Greedy nearest-date matching: each left action claims the closest still-unclaimed right action
    within `tolerance`. Handles the common one-vs-one case exactly, and the several-per-year case
    (multiple dividends) correctly, since same-event rows share an ex-date. Returns the pairs and
    the two lists of leftovers the caller then classifies.
    """
    remaining = sorted(right, key=lambda a: a.ex_date)
    claimed: set[int] = set()
    pairs: list[tuple[CorporateAction, CorporateAction]] = []
    unmatched_left: list[CorporateAction] = []

    for action in sorted(left, key=lambda a: a.ex_date):
        best_index: int | None = None
        best_gap: timedelta | None = None
        for index, candidate in enumerate(remaining):
            if index in claimed:
                continue
            gap = abs(candidate.ex_date - action.ex_date)
            if gap <= tolerance and (best_gap is None or gap < best_gap):
                best_index, best_gap = index, gap
        if best_index is None:
            unmatched_left.append(action)
        else:
            claimed.add(best_index)
            pairs.append((action, remaining[best_index]))

    unmatched_right = [c for i, c in enumerate(remaining) if i not in claimed]
    return pairs, unmatched_left, unmatched_right


def _reconcile_pair(left: CorporateAction, right: CorporateAction) -> ReconciledAction:
    """Fold two agreeing feed rows into one `ReconciledAction` (see the class docstring)."""
    note: str | None = None
    if left.ex_date != right.ex_date:
        note = (
            f"ex-dates differed within tolerance: {left.source}={left.ex_date.isoformat()}, "
            f"{right.source}={right.ex_date.isoformat()}; earliest used"
        )
    return ReconciledAction(
        isin=left.isin,
        ex_date=min(left.ex_date, right.ex_date),
        action_type=left.action_type,
        terms=left.terms,
        knowable_date=max(left.knowable_date, right.knowable_date),
        record_date=left.record_date if left.record_date is not None else right.record_date,
        reconciliation_note=note,
        sources=tuple(sorted((left, right), key=lambda a: a.source)),
    )


def reconcile(
    actions: Iterable[CorporateAction],
    *,
    ex_date_tolerance_days: int = DEFAULT_EX_DATE_TOLERANCE_DAYS,
) -> ReconciliationResult:
    """Reconcile a set of corporate actions across exchanges into agreements and a queue.

    What it does: groups the actions by `(ISIN, action_type)`, and within each group pairs the two
    feeds by ex-date. A pair whose terms agree becomes a `ReconciledAction`; a pair whose terms
    differ becomes a `RATIO_MISMATCH`; an action with a same-type counterpart it could not be
    paired to becomes an `EX_DATE_MISMATCH`; an action with no counterpart at all becomes a
    `SINGLE_SOURCE`.

    What it assumes: at most two distinct sources per `(ISIN, type)` — the platform has exactly two
    corporate-action feeds. Three or more is a malformed input, not a data disagreement, and raises
    `ReconcileError` rather than silently reconciling a chosen pair.

    What it never does: pick a winner between disagreeing feeds, read a clock, or touch a database.
    """
    tolerance = timedelta(days=ex_date_tolerance_days)
    grouped: dict[tuple[str, ActionType], list[CorporateAction]] = defaultdict(list)
    for action in actions:
        grouped[(action.isin, action.action_type)].append(action)

    reconciled: list[ReconciledAction] = []
    queue: list[ReconciliationConflict] = []

    for (isin, action_type), group in grouped.items():
        by_source: dict[str, list[CorporateAction]] = defaultdict(list)
        for action in group:
            by_source[action.source].append(action)

        if len(by_source) > 2:
            raise ReconcileError(
                f"{isin} {action_type.value}: {len(by_source)} sources "
                f"({', '.join(sorted(by_source))}); reconciliation is defined for two exchanges"
            )

        if len(by_source) == 1:
            for action in group:
                queue.append(
                    ReconciliationConflict(
                        reason=ReconciliationReason.SINGLE_SOURCE,
                        isin=isin,
                        action_type=action_type,
                        records=(action,),
                    )
                )
            continue

        left_source, right_source = sorted(by_source)
        pairs, unmatched_left, unmatched_right = _match_by_ex_date(
            by_source[left_source], by_source[right_source], tolerance
        )

        for left, right in pairs:
            if _terms_agree(left.terms, right.terms):
                reconciled.append(_reconcile_pair(left, right))
            else:
                queue.append(
                    ReconciliationConflict(
                        reason=ReconciliationReason.RATIO_MISMATCH,
                        isin=isin,
                        action_type=action_type,
                        records=tuple(sorted((left, right), key=lambda a: a.source)),
                    )
                )

        if len(unmatched_left) == 1 and len(unmatched_right) == 1:
            queue.append(
                ReconciliationConflict(
                    reason=ReconciliationReason.EX_DATE_MISMATCH,
                    isin=isin,
                    action_type=action_type,
                    records=tuple(
                        sorted((unmatched_left[0], unmatched_right[0]), key=lambda a: a.source)
                    ),
                )
            )
        else:
            for action in (*unmatched_left, *unmatched_right):
                queue.append(
                    ReconciliationConflict(
                        reason=ReconciliationReason.SINGLE_SOURCE,
                        isin=isin,
                        action_type=action_type,
                        records=(action,),
                    )
                )

    _LOG.info(
        "ca.reconciled",
        reconciled=len(reconciled),
        queued=len(queue),
        state="RECONCILED",
    )
    return ReconciliationResult(reconciled=tuple(reconciled), queue=tuple(queue))


def eligible_for_factor_chain(result: ReconciliationResult) -> tuple[ReconciledAction, ...]:
    """The only actions M2.4 may build factors from: the reconciled ones, in-memory.

    The in-memory twin of `load_reconciled_actions`. It exists so the gate — "an unreconciled
    action never reaches a factor" — is one named function a caller uses rather than a `.reconciled`
    field access every consumer has to remember to prefer over `.queue`. Whatever is in the queue
    is, by construction, absent from what this returns.
    """
    return result.reconciled


# ── database seam ─────────────────────────────────────────────────────────────────────────────
#
# Everything above is pure. Below is the thin persistence: mark the store's rows reconciled,
# write disagreements to the quality queue, and read the reconciled subset back for the factor
# chain. The caller owns the transaction (as in M2.2's writer), so a CA ingest and its
# reconciliation can share one commit.

_MARK_RECONCILED_SQL: Final = (
    "UPDATE corporate_actions SET reconciled = true, reconciliation_note = %s "
    "WHERE isin = %s AND ex_date = %s AND action_type = %s AND source = %s"
)

_MARK_UNRECONCILED_SQL: Final = (
    "UPDATE corporate_actions SET reconciled = false "
    "WHERE isin = %s AND ex_date = %s AND action_type = %s AND source = %s"
)

_FLAG_EXISTS_SQL: Final = (
    "SELECT 1 FROM quality_flag "
    "WHERE check_name = %s AND NOT resolved AND detail->>'fingerprint' = %s LIMIT 1"
)

_INSERT_FLAG_SQL: Final = (
    "INSERT INTO quality_flag "
    "(logical_date, check_name, severity, isin, source, detail, raised_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)

_LOAD_RECONCILED_SQL: Final = (
    "SELECT isin, ex_date, action_type, ratio_terms, record_date, announcement_date, "
    "knowable_date, source, source_ref, raw_text, l0_key FROM corporate_actions "
    "WHERE reconciled = true"
)


class PersistCounts(BaseModel):
    """What `persist_reconciliation` changed. `flags_skipped` are disagreements already queued."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rows_marked_reconciled: int = 0
    flags_written: int = 0
    flags_skipped: int = 0


def persist_reconciliation(
    conn: Connection,
    result: ReconciliationResult,
    *,
    clock: Clock,
) -> PersistCounts:
    """Write a reconciliation pass to the store: mark rows, and queue the disagreements.

    For each `ReconciledAction`, sets `reconciled = true` on **both** of its source rows (matched
    by the table's natural key `(isin, ex_date, action_type, source)`) with the reconciliation
    note. For each `ReconciliationConflict`, forces the involved rows back to `reconciled = false`
    — an unreconciled action must never be left marked reconciled from a prior run — and writes one
    `quality_flag`, unless an open flag with the same fingerprint already exists (idempotent).

    Does not commit or roll back: the caller owns the transaction, exactly as M2.2's writer does.
    """
    rows_marked = 0
    for reconciled in result.reconciled:
        for action in reconciled.sources:
            conn.execute(
                _MARK_RECONCILED_SQL,
                (
                    reconciled.reconciliation_note,
                    action.isin,
                    action.ex_date,
                    action.action_type.value,
                    action.source,
                ),
            )
            rows_marked += 1

    written = 0
    skipped = 0
    now = clock.now()
    for conflict in result.queue:
        for action in conflict.records:
            conn.execute(
                _MARK_UNRECONCILED_SQL,
                (action.isin, action.ex_date, action.action_type.value, action.source),
            )
        flag = conflict.as_quality_flag(clock=clock)
        exists = conn.execute(_FLAG_EXISTS_SQL, (flag.check_name, flag.fingerprint)).fetchone()
        if exists is not None:
            skipped += 1
            continue
        conn.execute(
            _INSERT_FLAG_SQL,
            (
                flag.logical_date,
                flag.check_name,
                flag.severity,
                flag.isin,
                flag.source,
                json.dumps(flag.detail),
                now,
            ),
        )
        written += 1

    counts = PersistCounts(
        rows_marked_reconciled=rows_marked, flags_written=written, flags_skipped=skipped
    )
    _LOG.info(
        "ca.reconcile.persisted",
        rows_marked_reconciled=counts.rows_marked_reconciled,
        flags_written=counts.flags_written,
        flags_skipped=counts.flags_skipped,
    )
    return counts


def load_reconciled_actions(
    conn: Connection, *, isin: str | None = None
) -> tuple[CorporateAction, ...]:
    """The factor chain's single door: corporate actions marked `reconciled = true`, only.

    This is where invariant "an unreconciled action never feeds a factor" is enforced at the
    database boundary — M2.4 reads its actions through here, never off the raw table, so a row the
    feeds disagreed on (or that only one feed published) is physically absent from the factor
    chain's input until a human resolves it. Terms come back through the same discriminated adapter
    that wrote them, so an illegal `(type, terms)` pair fails here rather than in a factor.
    """
    sql = _LOAD_RECONCILED_SQL
    params: tuple[object, ...] = ()
    if isin is not None:
        sql += " AND isin = %s"
        params = (isin,)
    sql += " ORDER BY isin, ex_date, action_type, source"

    from dataplatform.ingest.corp_actions import CorporateAction

    rows = conn.execute(sql, params).fetchall()
    return tuple(
        CorporateAction(
            isin=str(row[0]),
            ex_date=row[1],
            action_type=ActionType(row[2]),
            terms=TERMS_ADAPTER.validate_python(row[3]),
            record_date=row[4],
            announcement_date=row[5],
            knowable_date=row[6],
            source=str(row[7]),
            source_ref=None if row[8] is None else str(row[8]),
            raw_text=str(row[9]),
            l0_key=None if row[10] is None else str(row[10]),
        )
        for row in rows
    )


def _bind_corporate_action() -> bool:
    """Resolve the `CorporateAction` forward reference the models above carry.

    `CorporateAction` lives in `dataplatform.ingest.corp_actions`, which imports this package's
    taxonomy/parse_terms back — importing it at module top makes the two a runtime cycle whenever
    `dataplatform.ingest.corp_actions` is imported *first* (M3.8's announcement tests do exactly
    that). So it is kept off the runtime import graph (annotation-only, above) and bound here once,
    into this module's globals, so the pydantic models resolve their forward reference.

    Returns True once the binding succeeds. When this package was pulled in *by* corp_actions and
    that module is still mid-initialization, the import raises `ImportError`; corp_actions calls
    `bind_corporate_action()` from the tail of its own module to complete the binding once it has
    finished defining the class. The models are only ever validated after corp_actions is loaded,
    so no validation can observe the unbound state.
    """
    if "CorporateAction" in globals():
        return True
    try:
        from dataplatform.ingest.corp_actions import CorporateAction
    except ImportError:
        return False
    globals()["CorporateAction"] = CorporateAction
    for _model in (ReconciledAction, ReconciliationConflict, ReconciliationResult):
        _model.model_rebuild(force=True)
    return True


#: Public re-entry point for corp_actions to finish the binding (see `_bind_corporate_action`).
bind_corporate_action = _bind_corporate_action

_bind_corporate_action()
