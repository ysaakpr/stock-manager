"""D5 (M18.4): the wire models of the decision-record reads — journal, cases, evidence bundles.

Kept apart from `models.py` so the three M18 pages built in parallel do not collide in one file;
the conventions are that file's: frozen, closed to undeclared fields, and `None` means unknown or
absent, never zero. Every free-text field here has already been masked by the query layer
(`queries_journal`), and the console masks it again on the way out (invariant #13).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DECISION_KINDS",
    "JOURNAL_PAGE_SIZE",
    "NO_OP_DECISIONS",
    "CaseLineOut",
    "CasesOut",
    "EvidenceBundleOut",
    "EvidenceLineOut",
    "EvidenceListOut",
    "JournalEntryOut",
    "JournalFacetsOut",
    "JournalPageOut",
    "JournalQuery",
]

#: `decision_journal.decision`'s CHECK constraint as of migration 0007, in its own order. Mirrored
#: (not imported) from `analyst.journal.Decision`: System 1 never imports System 2.
DECISION_KINDS: Final = (
    "HOLD",
    "BUY",
    "SELL",
    "ESCALATE",
    "HEARTBEAT",
    "SKIPPED_DATA_RED",
    "AUTH_REQUIRED",
    "DEFERRED",
    "RAIL_BLOCK",
    "POLICY_PROPOSAL",
)

#: The kinds that record "checked, nothing done" (invariant #9): listed like every other entry and
#: marked as no-ops, never filtered out by default.
NO_OP_DECISIONS: Final = frozenset({"HOLD", "HEARTBEAT", "SKIPPED_DATA_RED", "AUTH_REQUIRED"})

#: Entries per journal page.
JOURNAL_PAGE_SIZE: Final = 50


class JournalQuery(BaseModel):
    """One journal search: every filter optional, `before` the keyset cursor (an entry id)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date_from: date | None = None
    date_to: date | None = None
    case_id: str | None = None
    book: str | None = Field(default=None, description="An M17 stream: payload.book")
    decision: str | None = None
    before: int | None = Field(default=None, description="Only entries with id < before")
    limit: int = Field(default=JOURNAL_PAGE_SIZE, ge=1, le=500)


class JournalEntryOut(BaseModel):
    """One `decision_journal` row, its free text masked and its payload pretty-printed, masked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    ts: datetime
    trading_date: date
    case_id: str | None
    book: str | None = Field(description="payload.book — the M17 stream, if any")
    actor: str
    decision: str
    no_op: bool
    isin: str | None
    sleeve: str | None
    evidence_ref: str | None
    rationale: str | None
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_inr: Decimal | None
    orders_ref: str | None
    payload: str = Field(description="Canonical (sorted, indented) JSON, masked and bounded")
    break_conditions: str = Field(description="As `payload`")
    recorded_at: datetime


class JournalPageOut(BaseModel):
    """One page of a search, newest (highest id) first; `next_before` is the cursor to the next."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    query: JournalQuery
    entries: list[JournalEntryOut]
    next_before: int | None = Field(description="null on the last page")


class JournalFacetsOut(BaseModel):
    """What the search form offers: every case, every M17 stream, every decision kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cases: list[str]
    books: list[str]
    kinds: list[str]


class CaseLineOut(BaseModel):
    """One `case_` row with its lifecycle stage and how much of the journal is about it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    title: str
    state: str
    funding_mode: str
    theme: str | None
    created_at: datetime
    updated_at: datetime
    entries: int
    last_trading_date: date | None


class CasesOut(BaseModel):
    """Every case, by id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    cases: list[CaseLineOut]


class EvidenceLineOut(BaseModel):
    """One stored A9 evidence bundle, and the journal entries that reference it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    bytes: int
    written_at: datetime
    entries: int
    last_referenced_at: datetime | None


class EvidenceListOut(BaseModel):
    """The evidence store under `<data_root>/evidence`, newest file first, at most `limit`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    root: str
    root_visible: bool
    total: int
    limit: int
    bundles: list[EvidenceLineOut]


class EvidenceBundleOut(BaseModel):
    """One bundle opened: its bytes re-hashed against its name, its content masked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    ref: str
    bytes: int
    written_at: datetime
    verified: bool = Field(description="sha256 of the stored bytes equals the reference")
    content: str = Field(description="Pretty-printed JSON (or the raw text), masked and bounded")
    referenced_by: list[int] = Field(description="Journal entry ids citing it, newest first")
