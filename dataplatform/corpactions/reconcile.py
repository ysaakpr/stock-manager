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
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dataplatform.clock import Clock
from dataplatform.corpactions.factors import PRICE_EVENT_TYPES
from dataplatform.corpactions.manual_actions import MANUAL_SOURCE, default_manual_actions
from dataplatform.corpactions.parse_terms import (
    names_non_equity_bonus,
    names_several_dividends,
    states_dividend_amount,
)
from dataplatform.corpactions.taxonomy import (
    TERMS_ADAPTER,
    ActionType,
    DividendKind,
    DividendTerms,
    Terms,
    UnquantifiedTerms,
    describe,
)
from dataplatform.ingest.models import ISIN_PATTERN, IngestError
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

if TYPE_CHECKING:
    from dataplatform.ingest.corp_actions import CorporateAction

__all__ = [
    "CA_RECONCILIATION_CHECK",
    "DEFAULT_EX_DATE_MISMATCH_WINDOW_DAYS",
    "DEFAULT_EX_DATE_TOLERANCE_DAYS",
    "PersistCounts",
    "QualityFlagRecord",
    "ReconcileError",
    "ReconciledAction",
    "ReconciliationConflict",
    "ReconciliationReason",
    "ReconciliationResult",
    "SingleSourcePolicy",
    "collapse_reconciled_rows",
    "eligible_for_factor_chain",
    "load_reconciled_actions",
    "load_unreconciled_actions",
    "persist_reconciliation",
    "reconcile",
]

_LOG = get_logger(__name__)

#: How far two ex-dates may sit apart and still be treated as *the same event* rather than two
#: different actions. NSE and BSE almost always agree on the ex-date to the day; the window exists
#: only to absorb the occasional off-by-one, and a difference wider than this is itself reported
#: (`EX_DATE_MISMATCH`) rather than silently bridged.
DEFAULT_EX_DATE_TOLERANCE_DAYS: Final = 2

#: How far apart two *leftovers* — one row per feed of one `(ISIN, type)`, neither matched within
#: the tolerance above — may sit and still be queued as one event the feeds dated differently
#: (`EX_DATE_MISMATCH`). Further apart they are two different events, each single-source.
#:
#: Why 30. The feeds agree on the ex-date to the day in 13,255 of the store's 13,256 cross-feed
#: pairs (2026-10-23), and the one exception is a day out; a real misdating is a revised record
#: date — a fresh LODR Reg. 42 intimation days to weeks after the first — not a different year.
#: The upper bound is the shortest gap at which one ISIN repeats a type in earnest: quarterly
#: interim dividends sit ~90 days apart, so 30 never pairs two of them. Unbounded, the window paired
#: any lone leftover with any other: all 11 open EX_DATE_MISMATCH flags were pairs 2 to 25 years
#: apart (BHAGYANGR's 2026 demerger against its 2017 spin-off), and each one unreconciled both
#: actions — including the older one ACCEPT had already admitted.
DEFAULT_EX_DATE_MISMATCH_WINDOW_DAYS: Final = 30

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
    """Both exchanges announced an action of this type for this ISIN, on ex-dates too far apart
    to be confidently the same event yet close enough (`DEFAULT_EX_DATE_MISMATCH_WINDOW_DAYS`)
    that they may be."""

    SINGLE_SOURCE = "SINGLE_SOURCE"
    """Only one exchange published this action. Often benign (a single-listed security), but never
    silently assumed to be: an unconfirmed action is still a known-unknown to the factor chain."""


class SingleSourcePolicy(StrEnum):
    """What `reconcile()` does with an action only one feed published.

    `QUEUE` (the default, and the two-exchange invariant) never lets a single feed's word reach the
    factor chain: a single-source action becomes a `SINGLE_SOURCE` conflict a human confirms once.

    `ACCEPT` admits it to the factor chain marked `cross_verified=False` — for a store that carries
    only one exchange's feed (so *every* action is single-source and `QUEUE` would yield no factors
    at all), or a genuinely single-listed security. It is never silent: the resulting
    `ReconciledAction` records the single source, `cross_verified` is `False`, and the note says so,
    so a factor built on one feed is always distinguishable from one two feeds agreed on. Ratio and
    ex-date *disagreements* are unaffected by this policy — a contradiction between two feeds is
    always queued, never accepted, whatever the policy.
    """

    QUEUE = "QUEUE"
    ACCEPT = "ACCEPT"


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
        "component": action.component,
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
    """One corporate action cleared for the factor chain — the only kind it may consume.

    Almost always this is an action **both exchanges agree on**: `cross_verified` is `True` and
    `sources` holds **both** raw `CorporateAction`s, so a reconciled row is never separated from the
    two feed rows that were found to agree. It can only be minted by `reconcile()`, which is what
    makes "only reconciled actions reach a factor" a type-level fact and not a convention a caller
    has to remember.

    The one exception is a single-source action admitted under `SingleSourcePolicy.ACCEPT`:
    `cross_verified` is `False`, `sources` holds the **one** feed row, and `reconciliation_note`
    says the action was not cross-verified. This keeps a factor built on one feed always
    distinguishable from one two feeds agreed on — the policy relaxes *whether* a single feed is
    trusted, never *whether the fact that it is single-sourced* is recorded.

    What it never does: choose between disagreeing feeds. If two feeds contradicted each other there
    is no `ReconciledAction` — there is a `ReconciliationConflict` in the queue instead, regardless
    of policy.

    Canonical fields when two rows differ only within tolerance: `ex_date` is the earlier of the two
    (deterministic and source-agnostic), and `knowable_date` is the *later* — the action is only
    known to be reconciled once both feeds have arrived, so a decision may not see the reconciled
    verdict before then (invariant #7). Any within-tolerance ex-date difference is recorded in
    `reconciliation_note`; nothing is lost.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    isin: str = Field(pattern=ISIN_PATTERN)
    ex_date: date
    action_type: ActionType
    terms: Terms
    knowable_date: date
    record_date: date | None = None
    reconciled: Literal[True] = True
    cross_verified: bool = True
    reconciliation_note: str | None = None
    sources: tuple[CorporateAction, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _sources_match_verification(self) -> ReconciledAction:
        """A cross-verified action carries both feeds; a single-source one carries exactly one."""
        if self.cross_verified and len(self.sources) < 2:
            raise ValueError("a cross-verified action must carry both feed rows")
        if not self.cross_verified and len(self.sources) != 1:
            raise ValueError("a single-source action must carry exactly one feed row")
        return self

    @property
    def source_ids(self) -> tuple[str, ...]:
        """The feed ids behind this action, sorted — one if single-source, two if cross-verified."""
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

        A record cut from a line naming several dividends adds its component: the regular and the
        special dividend of `Dividend - Rs 8 & Special Dividend Rs 5` share every other part, and
        without it their two disagreements would be one flag. A one-dividend line adds nothing, so
        the flags raised before components existed keep their fingerprints.
        """
        parts = [self.isin, self.action_type.value, self.reason.value]
        for action in sorted(
            self.records, key=lambda a: (a.source, a.ex_date, a.raw_text, a.component)
        ):
            parts.extend((action.source, action.ex_date.isoformat(), action.raw_text))
            if action.component and names_several_dividends(action.raw_text):
                parts.append(action.component)
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


def _quantified_fill(left: Terms, right: Terms) -> Terms | None:
    """The one side's terms when the *other* stated none, else `None`.

    Not every disagreement is a contradiction. BSE writes 155 splits as bare
    `Sub Division of Equity shares` and 3 bonuses as prose with no ratio at all — the numbers are
    simply not in that feed — while NSE states them (`FV SPLIT FROM RS.10/- TO RS.2/-`). Treating
    that as a `RATIO_MISMATCH` sends a perfectly well-evidenced action to a human whose only
    possible move is to copy the number across from the other feed already in front of them.

    So silence yields to a statement, and only to a statement. Silence takes two shapes here.

    **The whole terms.** Exactly one side is `UnquantifiedTerms` — the other's numbers stand.

    **One field.** Both sides are `DividendTerms` naming the same money, and exactly one gives the
    dividend's *kind* while the other says `UNSPECIFIED` — which the enum documents as "the feed
    said only 'dividend'". BSE writes `Final Dividend - Rs. - 2.5000` and NSE writes
    `Dividend - Rs 2.50 Per Share`: the same 2.50, and one feed happens to know it is the final
    one. 3,982 of the 4,195 open ratio mismatches were this, against 213 where the feeds really do
    name different money.

    Two feeds that both state terms and *disagree* are still a mismatch, and two that both say
    nothing still have nothing to fill from — a filled action is always backed by one feed's
    explicit statement, never by an average or an inference.
    """
    left_silent = isinstance(left, UnquantifiedTerms)
    right_silent = isinstance(right, UnquantifiedTerms)
    if left_silent != right_silent:
        return right if left_silent else left
    if left_silent:  # both silent — nothing to copy across
        return None
    if isinstance(left, DividendTerms) and isinstance(right, DividendTerms):
        return _dividend_kind_fill(left, right)
    return None


def _dividend_kind_fill(left: DividendTerms, right: DividendTerms) -> DividendTerms | None:
    """The side that named the dividend's kind, when the other said `UNSPECIFIED`.

    The money must match exactly first — this fills in *which* dividend it is, never how much.
    """
    if left.amount_inr != right.amount_inr:
        return None
    if left.percent_of_face_value != right.percent_of_face_value:
        return None
    left_silent = left.dividend_kind is DividendKind.UNSPECIFIED
    right_silent = right.dividend_kind is DividendKind.UNSPECIFIED
    if left_silent == right_silent:
        return None
    return right if left_silent else left


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


def _rows_agree(left: CorporateAction, right: CorporateAction) -> bool:
    return _terms_agree(left.terms, right.terms)


def _rows_fill(left: CorporateAction, right: CorporateAction) -> bool:
    return _row_fill(left, right) is not None


def _row_fill(left: CorporateAction, right: CorporateAction) -> Terms | None:
    """`_quantified_fill` on two rows' terms, else `_named_kind_fill` on the rows themselves."""
    filled = _quantified_fill(left.terms, right.terms)
    return filled if filled is not None else _named_kind_fill(left, right)


def _names_kind(text: str, kind: DividendKind) -> bool:
    return re.search(rf"\b{kind.value.lower()}\b", text.lower()) is not None


def _named_kind_fill(left: CorporateAction, right: CorporateAction) -> DividendTerms | None:
    """The narrower kind, when one feed's single dividend phrase names both feeds' kinds.

    NSE writes `Special Interim Dividend Rs 12/- Per Share` and BSE `Interim Dividend - Rs. -
    12.0000`. The taxonomy holds one kind per dividend, so the parser keeps SPECIAL and the pair
    read as INTERIM against SPECIAL — two RATIO_MISMATCH flags (INE029A01011 2017-03-24,
    INE259A01022 2018-05-29) on the same money, the same day, and words both feeds wrote. NSE's
    phrase *says* interim; nothing disagrees, the feeds only stated it at different widths.

    So, as in `_dividend_kind_fill`: the money must match exactly and be stated; then the kinds
    differ, neither is UNSPECIFIED, and one row's text — a line naming one dividend, not a list —
    names the other row's kind as well as its own. The other row's terms stand: its kind is the
    one both feeds wrote. Feeds naming different kinds outright (NSE `Special Dividend`, BSE
    `Interim Dividend`) are left to `_name_only` (rule C), which reconciles them only as the one
    dividend on that day in each feed.
    """
    lt, rt = left.terms, right.terms
    if not (isinstance(lt, DividendTerms) and isinstance(rt, DividendTerms)):
        return None
    if lt.amount_inr is None and lt.percent_of_face_value is None:
        return None
    if (lt.amount_inr, lt.percent_of_face_value) != (rt.amount_inr, rt.percent_of_face_value):
        return None
    kinds = (lt.dividend_kind, rt.dividend_kind)
    if kinds[0] is kinds[1] or DividendKind.UNSPECIFIED in kinds:
        return None
    for broad, narrow in ((left.raw_text, rt), (right.raw_text, lt)):
        if not names_several_dividends(broad) and _names_kind(broad, narrow.dividend_kind):
            return narrow
    return None


# ── one event's dividends stated differently: the three owner-approved rules (2026-11-01) ──────
#
# Two exchanges disagreeing is flagged, never silently resolved. These three resolve a
# disagreement only when the amounts are *consistent*, and only where the day leaves no choice
# of partner: one dividend in each feed for A and C, one record against all of the other feed's
# for B. A pair the by-date fallback forms later is never judged by them, so a busy day whose rows
# could be paired several ways stays a RATIO_MISMATCH for a human.


@dataclass(frozen=True, slots=True)
class _Resolution:
    """One event the feeds were found to state consistently: every row behind it, and what counts.

    `rows` are all the feed rows (and any restored record, `MANUAL_SOURCE`) the event consumed —
    two for a pair, one plus its parts for rule B. `terms` is what the factor chain and the total
    return book, once; `carrier` the row whose statement that is, so the folded event points at
    real evidence. `note` says which rule matched and what each side said.
    """

    rows: tuple[CorporateAction, ...]
    terms: Terms
    carrier: CorporateAction
    note: str | None = None


def _pair_resolution(left: CorporateAction, right: CorporateAction) -> _Resolution | None:
    """Two rows that agree, or that one feed's silence fills (`_row_fill`); else `None`."""
    if _terms_agree(left.terms, right.terms):
        return _Resolution(rows=(left, right), terms=left.terms, carrier=left)
    filled = _row_fill(left, right)
    if filled is None:
        return None
    # `_row_fill` returns one side's own terms object; that side stated them.
    return _Resolution(
        rows=(left, right), terms=filled, carrier=left if filled == left.terms else right
    )


def _states_money(terms: DividendTerms) -> bool:
    return terms.amount_inr is not None or terms.percent_of_face_value is not None


def _kind_said(action: CorporateAction) -> str:
    terms = action.terms
    kind = terms.dividend_kind.value if isinstance(terms, DividendTerms) else "?"
    return f"{action.source} said {kind}"


def _silent_amount_fill(left: CorporateAction, right: CorporateAction) -> _Resolution | None:
    """Rule A, silence yields: one feed's dividend states no amount, the other's states one.

    NSE writes a blank `Dividend - Rs  Per Share`, a bare `Interim Dividend`, or a typo the parser
    rightly refuses (`Re 0..30`, `Rs 0 .70`, `Re 0.0`, `Rs Per 0.50 Share`); BSE once wrote a bare
    `Final Dividend` beside NSE's `Dividend - Rs 14 Per Share` (ACC, 2020-03-27). Nothing there
    contradicts the other feed: one record states the money and the other says nothing usable
    about it, so the stated amount stands — the same rule `_quantified_fill` applies to a split
    one feed left unquantified.

    "Says nothing" is checked on the text, not only on the parsed terms: a line the parser left
    bare because it read *two* amounts in it (`parse_terms` queues those as conflicting) states
    money, and stays a mismatch. The amount is never read out of the silent line's typo — it comes
    from the other exchange's record, whose source and text the note names. Kinds may differ (a
    label, as rule C has it); both are recorded.
    """
    lt, rt = left.terms, right.terms
    if not (isinstance(lt, DividendTerms) and isinstance(rt, DividendTerms)):
        return None

    def silent(action: CorporateAction, terms: DividendTerms) -> bool:
        return not _states_money(terms) and not states_dividend_amount(action.raw_text)

    if silent(left, lt) and _states_money(rt):
        quiet, loud = left, right
    elif silent(right, rt) and _states_money(lt):
        quiet, loud = right, left
    else:
        return None
    note = (
        f"silence yields: amount taken from {loud.source} ({loud.raw_text.strip()!r}); "
        f"{quiet.source} stated none ({quiet.raw_text.strip()!r}); "
        f"{_kind_said(left)}, {_kind_said(right)}"
    )
    return _Resolution(rows=(left, right), terms=loud.terms, carrier=loud, note=note)


def _name_only(left: CorporateAction, right: CorporateAction) -> _Resolution | None:
    """Rule C, name only: the same stated money, the feeds' kind labels differ.

    Ingersoll-Rand's ₹202 of 2018-05-24 is a `Special Dividend` to NSE and an `Interim Dividend`
    to BSE; Swaraj's ₹8.25 is NSE's interim and BSE's final. The money is the event — the total
    return reinvests an amount, not a label — so the pair reconciles on the amount, and the note
    records which kind each side said. The kind kept is the first feed's by name (BSE before NSE),
    a deterministic choice rather than a judgement: nothing downstream reads the kind of a
    reconciled dividend.

    What it never does: reconcile different money, or a stated amount against a silent one (that
    is rule A, with its own text check).
    """
    lt, rt = left.terms, right.terms
    if not (isinstance(lt, DividendTerms) and isinstance(rt, DividendTerms)):
        return None
    if not _states_money(lt):
        return None
    if (lt.amount_inr, lt.percent_of_face_value) != (rt.amount_inr, rt.percent_of_face_value):
        return None
    if lt.dividend_kind is rt.dividend_kind:
        return None  # identical terms agree; nothing for this rule to do
    note = (
        f"name only: the same money, kinds differ ({_kind_said(left)}, {_kind_said(right)}); "
        f"reconciled on the amount, {left.source}'s kind kept"
    )
    return _Resolution(rows=(left, right), terms=lt, carrier=left, note=note)


def _parts_add_up(whole: CorporateAction, parts: Sequence[CorporateAction]) -> _Resolution | None:
    """Rule B, parts add up: one feed itemises what the other files as one record.

    NSE's `Dividend - Rs 10/- Per Share + Special Dividend - Rs 7.5/- Per Share` is two rows (₹10
    and ₹7.5) beside BSE's one `Final Dividend - Rs. - 17.5000`; Gujarat Industries Power's NSE
    `Interim Dividend - Rs 3.50 Per Share` is BSE's Interim 2 plus Special 1.50. Every amount must
    be a stated rupee figure, and the parts must sum to the whole *exactly* in `Decimal`, or it is
    no rule at all and stays a mismatch.

    Which side is booked: the single record, never the parts beside it. The money must be counted
    once, and of the two representations the single one is also the exact one for the total
    return: one dividend D against the cum-dividend close P reinvests at (P - D) / P, while the
    parts booked as separate dividends on one day compound to (P - d1)(P - d2) / P², over-stating
    the factor by d1·d2 / P². The parts stay on the reconciled action as its evidence, and the
    note lists them.
    """
    wt = whole.terms
    if not isinstance(wt, DividendTerms) or wt.amount_inr is None:
        return None
    amounts: list[Decimal] = []
    for part in parts:
        pt = part.terms
        if not isinstance(pt, DividendTerms) or pt.amount_inr is None:
            return None
        amounts.append(pt.amount_inr)
    if sum(amounts, Decimal(0)) != wt.amount_inr:
        return None
    itemised = " + ".join(
        f"{p.terms.dividend_kind.value} {p.terms.amount_inr}"
        for p in parts
        if isinstance(p.terms, DividendTerms)
    )
    feeds = ", ".join(sorted({p.source for p in parts}))
    note = (
        f"parts add up: {feeds} itemised {itemised} = {whole.source} "
        f"{wt.dividend_kind.value} {wt.amount_inr} in one record; the single record is booked, "
        "its parts are evidence and are not booked beside it"
    )
    return _Resolution(rows=(whole, *parts), terms=wt, carrier=whole, note=note)


def _consistent_day_rules(
    pending_left: list[CorporateAction],
    pending_right: list[CorporateAction],
    tolerance: timedelta,
) -> list[_Resolution]:
    """Rules A, B and C over the dividends the exact passes left, removing what they consume.

    A and C apply only to a lone dividend in each feed within `tolerance` of each other — the day
    offers no other partner, so pairing them is no choice. B applies only to one record alone in
    its feed against every remaining row of the other feed near it (two or more). Anything else
    is left for the by-date fallback, whose pairs are judged only by exact agreement or a fill.
    """

    def near(action: CorporateAction, rows: Sequence[CorporateAction]) -> list[CorporateAction]:
        return [r for r in rows if abs(r.ex_date - action.ex_date) <= tolerance]

    def alone(action: CorporateAction, rows: Sequence[CorporateAction]) -> bool:
        return all(r is action for r in rows) and len(rows) == 1

    out: list[_Resolution] = []
    for left in list(pending_left):
        (right, *more) = near(left, pending_right) or (None,)
        if right is None or more or not alone(left, near(right, pending_left)):
            continue
        res = _silent_amount_fill(left, right) or _name_only(left, right)
        if res is not None:
            out.append(res)
            pending_left.remove(left)
            pending_right.remove(right)
    for side, other in ((pending_left, pending_right), (pending_right, pending_left)):
        for whole in list(side):
            if whole not in side:
                continue
            parts = near(whole, other)
            if len(parts) < 2 or not all(alone(whole, near(p, side)) for p in parts):
                continue
            res = _parts_add_up(whole, parts)
            if res is not None:
                out.append(res)
                side.remove(whole)
                for part in parts:
                    other.remove(part)
    return out


def _match(
    left: Sequence[CorporateAction],
    right: Sequence[CorporateAction],
    tolerance: timedelta,
) -> tuple[
    list[_Resolution],
    list[tuple[CorporateAction, CorporateAction]],
    list[CorporateAction],
    list[CorporateAction],
]:
    """Pair one `(ISIN, type)`'s rows across the feeds: dividends by component, others by date.

    Returns the events resolved (rows the feeds state consistently), the pairs that disagree, and
    the two lists of leftovers.

    A feed can carry several dividends on one ex-date — a regular and a special declared together
    — and nearest-date matching alone pairs them by row order, so NSE's ₹8 regular met BSE's ₹5
    special and both read as a ratio mismatch. Dividends are therefore paired in four passes,
    each within `tolerance`: rows whose terms agree; then rows one feed's silence fills
    (`_quantified_fill`: the same money, one feed naming the kind); then the consistent-day rules
    A, B and C (`_consistent_day_rules`); then whatever is left, by date, exactly as before — a
    pair formed there that disagrees is still a `RATIO_MISMATCH`. Every other type has at most one
    row per feed and date, and keeps `_match_by_ex_date` unchanged.
    """
    resolved: list[_Resolution] = []
    disputed: list[tuple[CorporateAction, CorporateAction]] = []

    def judge(pairs: Iterable[tuple[CorporateAction, CorporateAction]]) -> None:
        for a, b in pairs:
            res = _pair_resolution(a, b)
            if res is None:
                disputed.append((a, b))
            else:
                resolved.append(res)

    if not any(action.action_type is ActionType.DIVIDEND for action in (*left, *right)):
        pairs, unmatched_left, unmatched_right = _match_by_ex_date(left, right, tolerance)
        judge(pairs)
        return resolved, disputed, unmatched_left, unmatched_right
    pending_left = sorted(left, key=lambda a: (a.ex_date, a.component))
    pending_right = sorted(right, key=lambda a: (a.ex_date, a.component))
    exact: list[tuple[CorporateAction, CorporateAction]] = []
    for accepts in (_rows_agree, _rows_fill):
        for action in list(pending_left):
            candidates = [
                other
                for other in pending_right
                if abs(other.ex_date - action.ex_date) <= tolerance and accepts(action, other)
            ]
            if not candidates:
                continue
            best = min(
                candidates, key=lambda o: (abs(o.ex_date - action.ex_date), o.ex_date, o.component)
            )
            exact.append((action, best))
            pending_left.remove(action)
            pending_right.remove(best)
    judge(exact)
    resolved.extend(_consistent_day_rules(pending_left, pending_right, tolerance))
    rest, unmatched_left, unmatched_right = _match_by_ex_date(
        pending_left, pending_right, tolerance
    )
    judge(rest)
    return resolved, disputed, unmatched_left, unmatched_right


def _reconcile_resolution(res: _Resolution) -> ReconciledAction:
    """Fold one resolved event's rows into one `ReconciledAction` (see the class docstring).

    `res.terms` are what the feeds were found to state; when one feed stated the numbers and the
    other did not (`_quantified_fill`), or a rule A/B/C matched, the note records which feed
    supplied them, so a factor built on a cross-filled action is never indistinguishable from one
    both feeds spelled out.
    """
    rows = res.rows
    first, second = rows[0], rows[1]
    notes: list[str] = []
    if len({a.ex_date for a in rows}) > 1:
        dated = ", ".join(f"{a.source}={a.ex_date.isoformat()}" for a in rows)
        notes.append(f"ex-dates differed within tolerance: {dated}; earliest used")
    if not isinstance(res.terms, UnquantifiedTerms):
        # Keyed on which feed was *silent*, not on which object won: the pair is ordered by source
        # name, so the feed that stated the numbers is as often `left` as `right`, and comparing
        # against `left.terms` silently skipped the note for half of them.
        silent = next((a for a in (first, second) if isinstance(a.terms, UnquantifiedTerms)), None)
        if silent is not None:
            stated = second if silent is first else first
            notes.append(
                f"terms taken from {stated.source} ({stated.raw_text.strip()!r}); "
                f"{silent.source} stated none"
            )
    if res.note is not None:
        notes.append(res.note)
    return ReconciledAction(
        isin=first.isin,
        ex_date=min(a.ex_date for a in rows),
        action_type=first.action_type,
        terms=res.terms,
        knowable_date=max(a.knowable_date for a in rows),
        record_date=next((a.record_date for a in rows if a.record_date is not None), None),
        reconciliation_note="; ".join(notes) if notes else None,
        sources=tuple(sorted(rows, key=lambda a: (a.source, a.component))),
    )


def _feed(action: CorporateAction) -> str:
    """The feed a row speaks for: its source, or for a restored record the feed it restores.

    A restored record (`manual_actions`, `MANUAL_SOURCE`) is a feed's own published record the
    store's key could not hold — Patanjali's second NSE Interim of 2026-08-21. It is transcribed
    from that feed's L0 object, whose key starts with the feed's id, so it stands on that feed's
    side of the pairing and never makes a third source.
    """
    if action.source != MANUAL_SOURCE:
        return action.source
    if not action.l0_key or "/" not in action.l0_key:
        raise ReconcileError(
            f"{action.isin} {action.action_type.value} on {action.ex_date.isoformat()}: a "
            "restored record must name the feed L0 object it was transcribed from"
        )
    return action.l0_key.split("/", 1)[0]


def _by_feed(rows: Iterable[CorporateAction]) -> dict[str, list[CorporateAction]]:
    """One `(ISIN, type)` group's rows by the feed they speak for.

    A restored record joins only beside a row its feed did store for that ex-date — the sibling it
    was dropped next to. Without one there is no event for it to complete (a store that has not
    ingested that day, a test store), so it is left out rather than queued as a lone record.
    """
    rows = list(rows)
    stored = {(_feed(r), r.ex_date) for r in rows if r.source != MANUAL_SOURCE}
    by_source: dict[str, list[CorporateAction]] = defaultdict(list)
    for row in rows:
        if row.source == MANUAL_SOURCE and (_feed(row), row.ex_date) not in stored:
            continue
        by_source[_feed(row)].append(row)
    return by_source


def _acceptable_alone(action: CorporateAction) -> bool:
    """Whether `SingleSourcePolicy.ACCEPT` may admit this one-feed action to the factor chain.

    A price event whose one feed never stated a ratio cannot become a factor, and ACCEPT must not
    pretend otherwise: `_event_factors` raises on it, and because that raise happens inside the
    recompute it takes every *other* ISIN's factors down with it. Such an action is queued instead —
    a human (or a later feed) supplies the number. A dividend is different: an unquantified one is a
    known-unknown the chain already tolerates, so only the share-basis events are held back.
    """
    return not (
        action.action_type in PRICE_EVENT_TYPES and isinstance(action.terms, UnquantifiedTerms)
    )


def _accept_single_source(action: CorporateAction) -> ReconciledAction:
    """Admit a single-feed action to the factor chain under `SingleSourcePolicy.ACCEPT`.

    `cross_verified=False` and the note name the one feed, so the action is trusted for adjustment
    but never mistaken for one two feeds agreed on. Its `knowable_date` is the feed's own — there is
    no second feed to wait for, unlike a cross-verified pair.
    """
    return ReconciledAction(
        isin=action.isin,
        ex_date=action.ex_date,
        action_type=action.action_type,
        terms=action.terms,
        knowable_date=action.knowable_date,
        record_date=action.record_date,
        cross_verified=False,
        reconciliation_note=(
            f"single-source ({action.source}): accepted under SingleSourcePolicy.ACCEPT, "
            "not cross-verified against a second feed"
        ),
        sources=(action,),
    )


def reconcile(
    actions: Iterable[CorporateAction],
    *,
    ex_date_tolerance_days: int = DEFAULT_EX_DATE_TOLERANCE_DAYS,
    ex_date_mismatch_window_days: int = DEFAULT_EX_DATE_MISMATCH_WINDOW_DAYS,
    single_source_policy: SingleSourcePolicy = SingleSourcePolicy.QUEUE,
) -> ReconciliationResult:
    """Reconcile a set of corporate actions across exchanges into agreements and a queue.

    What it does: groups the actions by `(ISIN, action_type)`, and within each group pairs the two
    feeds by ex-date. A pair whose terms agree becomes a `ReconciledAction`; a pair whose terms
    differ becomes a `RATIO_MISMATCH`; a lone unpaired action on each side, no more than
    `ex_date_mismatch_window_days` apart, becomes one `EX_DATE_MISMATCH`, and several such on both
    sides are each queued as `SINGLE_SOURCE` whatever the policy; an action with no counterpart at
    all — including one with no other-feed leftover within that window, however many lie further
    away — is single-source, accepted or queued as `SINGLE_SOURCE` per `single_source_policy`.

    What it assumes: at most two distinct sources per `(ISIN, type)` — the platform has exactly two
    corporate-action feeds. Three or more is a malformed input, not a data disagreement, and raises
    `ReconcileError` rather than silently reconciling a chosen pair.

    What it never does: pick a winner between disagreeing feeds, read a clock, or touch a database.
    """
    tolerance = timedelta(days=ex_date_tolerance_days)
    mismatch_window = timedelta(days=ex_date_mismatch_window_days)
    grouped: dict[tuple[str, ActionType], list[CorporateAction]] = defaultdict(list)
    reconciled: list[ReconciledAction] = []
    queue: list[ReconciliationConflict] = []
    for action in actions:
        if action.action_type in PRICE_EVENT_TYPES and names_non_equity_bonus(action.raw_text):
            # A row parsed before the parser knew better: TVS Holdings' `Bonus Ncrps 1:116` stored
            # as an equity bonus. It is a distribution of preference shares, so it is never paired
            # and never admitted — whatever the policy — and a re-parse files it as the scheme of
            # arrangement it is (`ingest.ca_reparse_non_equity`).
            queue.append(
                ReconciliationConflict(
                    reason=ReconciliationReason.SINGLE_SOURCE,
                    isin=action.isin,
                    action_type=action.action_type,
                    records=(action,),
                )
            )
            continue
        grouped[(action.isin, action.action_type)].append(action)

    def file_single_source(action: CorporateAction, isin: str, action_type: ActionType) -> None:
        """File one action no second feed published: accept it or queue it, per the policy."""
        if (
            single_source_policy is SingleSourcePolicy.ACCEPT
            and _acceptable_alone(action)
            and action.source != MANUAL_SOURCE
        ):
            # Owner-ratified: trust the one feed, but stamp cross_verified=False so the factor it
            # feeds is never mistaken for a two-feed agreement.
            reconciled.append(_accept_single_source(action))
        else:
            queue.append(
                ReconciliationConflict(
                    reason=ReconciliationReason.SINGLE_SOURCE,
                    isin=isin,
                    action_type=action_type,
                    records=(action,),
                )
            )

    for (isin, action_type), group in grouped.items():
        by_source = _by_feed(group)

        if len(by_source) > 2:
            raise ReconcileError(
                f"{isin} {action_type.value}: {len(by_source)} sources "
                f"({', '.join(sorted(by_source))}); reconciliation is defined for two exchanges"
            )

        if len(by_source) <= 1:
            for action in (a for rows in by_source.values() for a in rows):
                file_single_source(action, isin, action_type)
            continue

        left_source, right_source = sorted(by_source)
        resolved, disputed, unmatched_left, unmatched_right = _match(
            by_source[left_source], by_source[right_source], tolerance
        )

        paired_days: list[date] = []
        disputed_days: list[date] = []
        for res in resolved:
            paired_days.extend(action.ex_date for action in res.rows)
            reconciled.append(_reconcile_resolution(res))
        for left, right in disputed:
            paired_days.extend((left.ex_date, right.ex_date))
            disputed_days.extend((left.ex_date, right.ex_date))
            queue.append(
                ReconciliationConflict(
                    reason=ReconciliationReason.RATIO_MISMATCH,
                    isin=isin,
                    action_type=action_type,
                    records=tuple(sorted((left, right), key=lambda a: a.source)),
                )
            )

        # A same-day extra: one feed itemized a further dividend on a day the two feeds already
        # paired (NSE's `Dividend Rs 10 + Special Dividend Rs 7.5` beside BSE's one record). It is
        # not a candidate misdating of anything — the day is accounted for — so it never joins the
        # leftovers below, where one extra beside an old one-feed dividend would make "leftovers
        # on both sides" and queue every old dividend that ACCEPT had admitted. It is single-source
        # under the policy, unless a pair that day disagreed: then it is queued with that dispute.
        def same_day(action: CorporateAction, days: Sequence[date]) -> bool:
            return any(abs(action.ex_date - day) <= tolerance for day in days)

        extras = (
            [a for a in (*unmatched_left, *unmatched_right) if same_day(a, paired_days)]
            if action_type is ActionType.DIVIDEND
            else []
        )
        unmatched_left = [a for a in unmatched_left if a not in extras]
        unmatched_right = [a for a in unmatched_right if a not in extras]
        for action in extras:
            if same_day(action, disputed_days):
                queue.append(
                    ReconciliationConflict(
                        reason=ReconciliationReason.SINGLE_SOURCE,
                        isin=isin,
                        action_type=action_type,
                        records=(action,),
                    )
                )
            else:
                file_single_source(action, isin, action_type)

        if unmatched_left and unmatched_right:
            # Leftovers on both sides. Only one with an other-feed row inside the mismatch window
            # can be one event the feeds dated differently; the rest are events one feed alone
            # published, single-source under the policy whatever sits on the other side years
            # away. One bound for the lone pair (#133) and for several leftovers on both sides,
            # which until 2026-11-01 queued all of them unbounded: one new NSE dividend beside a
            # dozen old BSE ones — 967 rows in 83 groups, none with a counterpart within the
            # window — pulled every old dividend ACCEPT had admitted out of the books and never
            # booked the new one.
            # "Other-feed row" counts the paired rows too, not just the leftovers: NSE re-files a
            # dividend whose ex-date moved as a second row (INE617A01013's ₹5 on 2023-09-27 and
            # 2023-10-19, BSE only on 10-19), and admitting the stale one beside the pair that
            # already carries that ₹5 would reinvest it twice.
            def near_other_feed(action: CorporateAction, others: Sequence[CorporateAction]) -> bool:
                return any(abs(action.ex_date - o.ex_date) <= mismatch_window for o in others)

            left_rows, right_rows = by_source[left_source], by_source[right_source]
            far = [a for a in unmatched_left if not near_other_feed(a, right_rows)] + [
                a for a in unmatched_right if not near_other_feed(a, left_rows)
            ]
            for action in far:
                file_single_source(action, isin, action_type)
            unmatched_left = [a for a in unmatched_left if a not in far]
            unmatched_right = [a for a in unmatched_right if a not in far]

            if (
                len(unmatched_left) == 1
                and len(unmatched_right) == 1
                and abs(unmatched_left[0].ex_date - unmatched_right[0].ex_date) <= mismatch_window
            ):
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
                # What is left is near something on the other side: several candidates for one
                # misdating, or a row beside a day the feeds already paired. Which is which is a
                # guess, so it is queued whatever the policy.
                for action in (*unmatched_left, *unmatched_right):
                    queue.append(
                        ReconciliationConflict(
                            reason=ReconciliationReason.SINGLE_SOURCE,
                            isin=isin,
                            action_type=action_type,
                            records=(action,),
                        )
                    )
        else:
            # Leftovers on one side only: events the other feed never published at all, single-
            # source in exactly the sense the policy governs. Until 2026-09-07 this branch queued
            # them regardless of policy, so ACCEPT applied to a name only when *every* action of
            # that type was one feed's — UNOMINDA's 2:1 bonus of 2018 (BSE-only, NSE's line was a
            # compound string the parser refused) stayed unadjusted because NSE had published the
            # 2016 and 2022 bonuses. Same event, same evidence, different verdict by accident.
            # A leftover with nothing on the other side within the mismatch window was filed the
            # same way above: no misdating spans that, so it is an event one feed alone published.
            # Pairing a lone one anyway (until 2026-10-23) unreconciled both, so a new action
            # retroactively pulled an old accepted one — BHAGYANGR's 2017 spin-off — out of the
            # factor chain and the books.
            for action in (*unmatched_left, *unmatched_right):
                file_single_source(action, isin, action_type)

    _LOG.info(
        "ca.reconciled",
        reconciled=len(reconciled),
        queued=len(queue),
        state="RECONCILED",
    )
    return ReconciliationResult(reconciled=tuple(reconciled), queue=tuple(queue))


def collapse_reconciled_rows(
    rows: Iterable[CorporateAction],
    *,
    ex_date_tolerance_days: int = DEFAULT_EX_DATE_TOLERANCE_DAYS,
    restored: Iterable[CorporateAction] = (),
) -> tuple[CorporateAction, ...]:
    """One `CorporateAction` per reconciled *event*, however many feeds published it.

    `corporate_actions` keeps one row per `(isin, ex_date, action_type, source)`, so an event both
    exchanges reported is two rows, both marked reconciled. Every consumer downstream of the
    reconciled set reasons per event: `build_factor_chain` multiplies the factors of all actions
    sharing an ex-date, `total_return_series` reinvests every dividend it is handed. Handed both
    rows, they count the event twice — a 10→2 split the feeds agreed on became `0.2 x 0.2`, and a
    ₹5 dividend both reported was reinvested as ₹10. The laptop, holding one feed, never showed it;
    the first store with both (the server, 2026-09-07) squared 548 split/bonus events on 412 ISINs.

    Pairs the rows exactly as `reconcile` paired them — same `(ISIN, type)` grouping, same
    `_match` (dividends component by component) within the same tolerance — and folds each pair
    into one action with the reconciliation's canonical fields: the earlier ex-date, the later
    knowable date, the first stated record and announcement dates, and the terms the feeds agreed
    on or the one feed that stated them (`_quantified_fill`). The row carrying those terms
    carries the event — its source, raw text and L0 key — so a factor built on it still points at
    real evidence. A row with no counterpart passes through untouched.

    An event the feeds stated as parts and a whole (rule B) folds to the whole alone, so its
    money is counted once. `restored` are the curated records of a feed's own the store's key
    could not hold (`ManualActions.restored_dividends`); they join their feed's side exactly as
    `reconcile` saw them, and one that ends up in no reconciled event is dropped — a restored
    record only ever completes an event, it is never booked on its own.

    What it never does: pick between two reconciled rows that *contradict*. Two rows marked
    reconciled whose terms neither agree nor fill can only mean the marks are stale relative to the
    rules, and that raises `ReconcileError` rather than building a factor on a guess. Three sources
    for one event is a malformed store, and raises for the same reason `reconcile` does.
    """
    tolerance = timedelta(days=ex_date_tolerance_days)
    grouped: dict[tuple[str, ActionType], list[CorporateAction]] = defaultdict(list)
    for row in (*rows, *restored):
        grouped[(row.isin, row.action_type)].append(row)

    def booked(actions: Iterable[CorporateAction]) -> list[CorporateAction]:
        return [a for a in actions if a.source != MANUAL_SOURCE]

    folded: list[CorporateAction] = []
    for (isin, action_type), group in grouped.items():
        by_source = _by_feed(group)
        if len(by_source) > 2:
            raise ReconcileError(
                f"{isin} {action_type.value}: {len(by_source)} sources "
                f"({', '.join(sorted(by_source))}); reconciliation is defined for two exchanges"
            )
        if len(by_source) <= 1:
            folded.extend(booked(group))
            continue
        left_source, right_source = sorted(by_source)
        resolved, disputed, unmatched_left, unmatched_right = _match(
            by_source[left_source], by_source[right_source], tolerance
        )
        for left, right in disputed:
            if MANUAL_SOURCE in (left.source, right.source):
                # A restored record beside an event it does not complete: it is not booked, and
                # the feed row it met is left to stand as it was marked.
                folded.extend(booked((left, right)))
                continue
            raise ReconcileError(
                f"{left.isin} {left.action_type.value} on {left.ex_date.isoformat()}: rows from "
                f"{left.source} ({describe(left.action_type, left.terms)!r}) and {right.source} "
                f"({describe(right.action_type, right.terms)!r}) are both marked reconciled but "
                "state different terms; the reconciliation marks are stale — re-run the pass "
                "rather than build a factor on a guess"
            )
        folded.extend(_fold(res) for res in resolved)
        folded.extend(booked(unmatched_left))
        folded.extend(booked(unmatched_right))

    folded.sort(key=lambda a: (a.isin, a.ex_date, a.action_type.value, a.source, a.component))
    return tuple(folded)


def _fold(res: _Resolution) -> CorporateAction:
    """One resolved event's reconciled rows, as the one action the chain may count once."""
    rows = res.rows
    return res.carrier.model_copy(
        update={
            "ex_date": min(a.ex_date for a in rows),
            "knowable_date": max(a.knowable_date for a in rows),
            "record_date": next((a.record_date for a in rows if a.record_date is not None), None),
            "announcement_date": next(
                (a.announcement_date for a in rows if a.announcement_date is not None), None
            ),
            "filed_against_isin": next(
                (a.filed_against_isin for a in rows if a.filed_against_isin), None
            ),
            "terms": res.terms,
        }
    )


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
    "WHERE isin = %s AND ex_date = %s AND action_type = %s AND source = %s AND component = %s"
)

_MARK_UNRECONCILED_SQL: Final = (
    "UPDATE corporate_actions SET reconciled = false "
    "WHERE isin = %s AND ex_date = %s AND action_type = %s AND source = %s AND component = %s"
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

#: Close the flags this pass has superseded. Scoped to the ISINs the pass actually looked at —
#: a reconciliation over one ISIN must never resolve another's open disagreement — and to the
#: fingerprints it did *not* raise, so a disagreement that still stands keeps its flag and its
#: original `raised_at`.
_RESOLVE_SUPERSEDED_SQL: Final = (
    "UPDATE quality_flag SET resolved = true, resolved_at = %(now)s, resolution = %(resolution)s "
    "WHERE check_name = %(check_name)s AND NOT resolved "
    "  AND isin = ANY(%(isins)s::text[]) "
    "  AND NOT (detail->>'fingerprint' = ANY(%(fingerprints)s::text[]))"
)

_LOAD_RECONCILED_SQL: Final = (
    "SELECT isin, ex_date, action_type, ratio_terms, record_date, announcement_date, "
    "knowable_date, source, source_ref, raw_text, l0_key, component FROM corporate_actions "
    "WHERE reconciled = true"
)

_LOAD_UNRECONCILED_SQL: Final = (
    "SELECT isin, ex_date, action_type, ratio_terms, record_date, announcement_date, "
    "knowable_date, source, source_ref, raw_text, l0_key, component FROM corporate_actions "
    "WHERE reconciled = false"
)


class PersistCounts(BaseModel):
    """What `persist_reconciliation` changed. `flags_skipped` are disagreements already queued."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rows_marked_reconciled: int = 0
    flags_written: int = 0
    flags_skipped: int = 0
    #: Open flags this pass closed because the disagreement they recorded no longer exists.
    flags_resolved: int = 0


def persist_reconciliation(
    conn: Connection,
    result: ReconciliationResult,
    *,
    clock: Clock,
) -> PersistCounts:
    """Write a reconciliation pass to the store: mark rows, and queue the disagreements.

    For each `ReconciledAction`, sets `reconciled = true` on **both** of its source rows (matched
    by the table's natural key `(isin, ex_date, action_type, source, component)`) with the
    reconciliation note. For each `ReconciliationConflict`, forces the involved rows back to
    `reconciled = false` — an unreconciled action must never be left marked reconciled from a prior
    run — and writes one `quality_flag`, unless an open flag with the same fingerprint already
    exists (idempotent).

    Does not commit or roll back: the caller owns the transaction, exactly as M2.2's writer does.
    """
    rows_marked = 0
    for reconciled in result.reconciled:
        for action in reconciled.sources:
            if action.source == MANUAL_SOURCE:
                continue  # a restored record lives in manual_actions.yaml, not in the table
            conn.execute(
                _MARK_RECONCILED_SQL,
                (
                    reconciled.reconciliation_note,
                    action.isin,
                    action.ex_date,
                    action.action_type.value,
                    action.source,
                    action.component,
                ),
            )
            rows_marked += 1

    written = 0
    skipped = 0
    now = clock.now()
    for conflict in result.queue:
        for action in conflict.records:
            if action.source == MANUAL_SOURCE:
                continue
            conn.execute(
                _MARK_UNRECONCILED_SQL,
                (
                    action.isin,
                    action.ex_date,
                    action.action_type.value,
                    action.source,
                    action.component,
                ),
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

    # Anything still open for an ISIN this pass examined, that this pass did not raise, has been
    # answered — by a parser fix, a second feed arriving, or a correction upstream. Without this
    # the queue only ever grows: a fingerprint that stops being raised is simply never mentioned
    # again, so `/status/quality` reported 8,495 RATIO_MISMATCH flags when 109 were live and the
    # other 8,386 had been fixed an hour earlier. A queue that cannot go down is not a queue.
    examined = sorted(
        {action.isin for reconciled in result.reconciled for action in reconciled.sources}
        | {action.isin for conflict in result.queue for action in conflict.records}
    )
    resolved = 0
    if examined:
        cursor = conn.execute(
            _RESOLVE_SUPERSEDED_SQL,
            {
                "now": now,
                "resolution": "superseded: not raised by a later reconciliation of this ISIN",
                "check_name": CA_RECONCILIATION_CHECK,
                "isins": examined,
                "fingerprints": [c.as_quality_flag(clock=clock).fingerprint for c in result.queue],
            },
        )
        resolved = cursor.rowcount

    counts = PersistCounts(
        rows_marked_reconciled=rows_marked,
        flags_written=written,
        flags_skipped=skipped,
        flags_resolved=resolved,
    )
    _LOG.info(
        "ca.reconcile.persisted",
        rows_marked_reconciled=counts.rows_marked_reconciled,
        flags_written=counts.flags_written,
        flags_skipped=counts.flags_skipped,
        flags_resolved=counts.flags_resolved,
    )
    return counts


def load_reconciled_actions(
    conn: Connection,
    *,
    isin: str | None = None,
    ex_after: date | None = None,
    ex_through: date | None = None,
) -> tuple[CorporateAction, ...]:
    """The factor chain's single door: corporate actions marked `reconciled = true`, one per event.

    This is where invariant "an unreconciled action never feeds a factor" is enforced at the
    database boundary — M2.4 reads its actions through here, never off the raw table, so a row the
    feeds disagreed on (or that only one feed published) is physically absent from the factor
    chain's input until a human resolves it. Terms come back through the same discriminated adapter
    that wrote them, so an illegal `(type, terms)` pair fails here rather than in a factor.

    The rows come back *collapsed* (`collapse_reconciled_rows`): an event both feeds published is
    two reconciled rows in the table and exactly one action here, because everything behind this
    door counts per event and would otherwise count it twice. The curated records restoring a
    feed's dropped dividend (`ManualActions.restored_dividends`) join the collapse exactly as they
    joined the reconciliation, so the event they complete folds the same way here.

    `ex_after` / `ex_through` bound the ex-date (``ex_after < ex_date <= ex_through``) for a reader
    that needs only a recent window across every ISIN — the L2 overlay (`store.l2_overlay`) —
    rather than one ISIN's whole history. The rows inside the window collapse exactly as before.
    """
    sql = _LOAD_RECONCILED_SQL
    params: list[object] = []
    if isin is not None:
        sql += " AND isin = %s"
        params.append(isin)
    if ex_after is not None:
        sql += " AND ex_date > %s"
        params.append(ex_after)
    if ex_through is not None:
        sql += " AND ex_date <= %s"
        params.append(ex_through)
    sql += " ORDER BY isin, ex_date, action_type, source, component"
    rows = conn.execute(sql, tuple(params)).fetchall()
    restored = default_manual_actions().restored_dividends(
        isin=isin, ex_after=ex_after, ex_through=ex_through
    )
    return collapse_reconciled_rows((_action_from_row(row) for row in rows), restored=restored)


def load_unreconciled_actions(
    conn: Connection,
    *,
    ex_after: date,
    ex_through: date,
    action_types: Iterable[ActionType],
) -> tuple[CorporateAction, ...]:
    """The rows of ``action_types`` ex in ``(ex_after, ex_through]`` that are *not* reconciled.

    What it does: reads the rows `load_reconciled_actions` keeps out of the factor chain, so a
    reader that must not treat "no factor" as "no event" can see them: the L2 overlay flags an
    ISIN whose unreconciled split is ex inside its window rather than showing the raw step as a
    return. One row per source, uncollapsed — a caller only asks whether an event exists.

    What it never does: hand a row to a factor. Nothing built from these is an adjustment.
    """
    types = sorted(t.value for t in action_types)
    rows = conn.execute(
        _LOAD_UNRECONCILED_SQL
        + " AND ex_date > %s AND ex_date <= %s AND action_type = ANY(%s)"
        + " ORDER BY isin, ex_date, action_type, source, component",
        (ex_after, ex_through, types),
    ).fetchall()
    return tuple(_action_from_row(row) for row in rows)


def _action_from_row(row: Sequence[Any]) -> CorporateAction:
    """A `CorporateAction` from one `_LOAD_RECONCILED_SQL`-shaped row."""
    from dataplatform.ingest.corp_actions import CorporateAction

    return CorporateAction(
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
        component=str(row[11]),
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
