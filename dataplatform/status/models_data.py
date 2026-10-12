"""M18.2: the wire models behind the console's Data page that `/status/*` did not already have.

The sync board, sources, gaps, quality and quarantine panels render the existing §4.4 models
(`dataplatform.status.models`) unchanged — that is what keeps a panel from disagreeing with its
endpoint. What is new here is the read the JSON surface never served: the archive list (the
endpoint answers one date) and the ISIN lookup over the D2 master, the D3 action and factor
tables and the L2 invalidation queue.

Same conventions as `models.py`: `None` is unknown or absent, never zero; frozen and closed to
undeclared fields; money and factors are `Decimal`, never `float`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

__all__ = [
    "ArchiveListOut",
    "ArchiveSummaryOut",
    "CorporateActionOut",
    "FactorOut",
    "InvalidationOut",
    "IsinLookupOut",
    "LineageEdgeOut",
    "SecurityOut",
    "SymbolCandidateOut",
    "SymbolSearchOut",
    "SymbolWindowOut",
]

_FROZEN = ConfigDict(extra="forbid", frozen=True)


# ── archives ────────────────────────────────────────────────────────────────────────────────


class ArchiveSummaryOut(BaseModel):
    """One published bundle's `archive_bundle` row, without its manifest (that is `/archives`)."""

    model_config = _FROZEN

    logical_date: date
    schema_version: str
    bundle_path: str
    manifest_sha256: str
    file_count: int
    total_bytes: int
    published_at: datetime


class ArchiveListOut(BaseModel):
    """The newest published bundles, and how many exist in all."""

    model_config = _FROZEN

    total: int = Field(description="Every archive_bundle row, counted before the limit")
    bundles: list[ArchiveSummaryOut] = Field(description="Newest logical date first")
    limit: int


# ── ISIN lookup ─────────────────────────────────────────────────────────────────────────────


class SecurityOut(BaseModel):
    """One `security_master` row (D2)."""

    model_config = _FROZEN

    isin: str
    name: str
    primary_exchange: str
    status: str
    face_value_inr: Decimal | None
    first_seen_date: date
    last_seen_date: date | None
    registered_by: str | None = Field(description="'lineage_backfill' for a retired ISIN row")
    updated_at: datetime


class SymbolWindowOut(BaseModel):
    """One `symbol_history` window: what the ISIN traded as, where, and when."""

    model_config = _FROZEN

    exchange: str
    symbol: str
    series: str | None
    valid_from: date
    valid_to: date | None = Field(description="null while current")
    source: str


class LineageEdgeOut(BaseModel):
    """One `isin_lineage` edge on the chain through the looked-up ISIN."""

    model_config = _FROZEN

    predecessor_isin: str
    successor_isin: str
    effective_date: date
    detected_by: str
    confidence: str
    gap_sessions: int
    symbol_at_change: str
    corroborating_action: str | None


class CorporateActionOut(BaseModel):
    """One `corporate_actions` row filed under the ISIN."""

    model_config = _FROZEN

    id: int
    ex_date: date
    action_type: str
    ratio_terms: JsonValue
    dividend_amount_inr: Decimal | None
    record_date: date | None
    knowable_date: date
    source: str
    reconciled: bool
    raw_text: str | None
    filed_against_isin: str | None = Field(
        description="The retired ISIN the exchange filed it against, when that was not this one"
    )


class FactorOut(BaseModel):
    """One link of the ISIN's `adjustment_factors` chain (§4.3)."""

    model_config = _FROZEN

    ex_date: date
    price_factor: Decimal
    qty_factor: Decimal
    cum_price_factor: Decimal
    cum_qty_factor: Decimal
    corporate_action_id: int | None
    structural_break: bool
    computed_at: datetime


class InvalidationOut(BaseModel):
    """One unresolved `l2_invalidation` row: this ISIN's L2 is stale until it drains."""

    model_config = _FROZEN

    id: int
    reason: str
    from_date: date | None = Field(description="null means the whole series")
    requested_at: datetime


class IsinLookupOut(BaseModel):
    """Everything the platform holds about one ISIN's identity and adjustment history.

    `security` is null for an ISIN the master does not hold — a retired predecessor can still
    have lineage, which is exactly when an operator looks it up.
    """

    model_config = _FROZEN

    isin: str
    security: SecurityOut | None
    symbols: list[SymbolWindowOut]
    lineage: list[LineageEdgeOut] = Field(description="The whole linear chain, oldest edge first")
    actions: list[CorporateActionOut] = Field(description="Newest ex-date first")
    factors: list[FactorOut] = Field(description="Newest ex-date first")
    pending_invalidations: list[InvalidationOut]


class SymbolCandidateOut(BaseModel):
    """One ISIN a symbol has been valid for — a candidate to look up, never a join."""

    model_config = _FROZEN

    isin: str
    name: str
    status: str
    exchange: str
    symbol: str
    series: str | None
    valid_from: date
    valid_to: date | None


class SymbolSearchOut(BaseModel):
    """`symbol_history` windows whose symbol matches, case-insensitively, newest first."""

    model_config = _FROZEN

    query: str
    candidates: list[SymbolCandidateOut]
    limit: int
