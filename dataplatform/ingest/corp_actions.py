"""D3 ingestion: the one normalized corporate-action row both exchange parsers emit and persist.

NSE publishes a corporate action as a JSON record keyed on ISIN; BSE publishes the same universe
of events keyed on a scrip code, with different field names and different date spellings (§4.1
row 5, "the two exchanges describe the same action differently — reconciliation needed"). The
reconciliation itself is M2.3's job. What M2.2 owns is turning either feed into *one* shape, so
that M2.3 compares two `CorporateAction`s rather than two feed dialects, and the factor chain
(M2.4) reads one model rather than an exchange branch.

That shape is `CorporateAction`. It carries the identity every downstream consumer needs — ISIN
(resolved through the D2 identity master, never a raw symbol; invariant #2), the ex-date, the
normalized action type and its structured terms (the M2.1 taxonomy), and the first date the action
was knowable to us (invariant #7) — plus the two things ingestion must never throw away: the
exchange's purpose string **verbatim** (`raw_text`), and the L0 key of the checksummed payload the
row was derived from (`l0_key`, invariant #1).

Two deliberate non-features:

* **No adjustment factor, no adjusted price.** This is the raw, normalized *event*; the factor
  chain is M2.4 and lives in `dataplatform.corpactions.factors`. A `CorporateAction` never knows
  what a split does to a 2019 close.
* **No reconciliation.** `reconciled` stays false on every row this module writes. An unreconciled
  action is a known-unknown the factor chain must not trust (M2.3), and pretending otherwise here
  would be the silent guess the whole D3 design refuses to make.

Money is `Decimal`. Dates are `datetime.date`. `recorded_at` comes from an injected `Clock`
(B10) — this module never reads a wall clock. Nothing here fetches: a parser takes bytes, or an
`L0Ref` it reads back through `L0Store`, so no row can exist that was not derived from L0.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dataplatform.clock import Clock
from dataplatform.corpactions.parse_terms import ManualQueueEntry
from dataplatform.corpactions.taxonomy import (
    TERMS_ADAPTER,
    TERMS_BY_ACTION,
    ActionType,
    DividendTerms,
    ParsedAction,
    Terms,
    describe,
)
from dataplatform.identity.master import Exchange, IdentityMaster
from dataplatform.ingest.models import ISIN_PATTERN, IngestError
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

__all__ = [
    "CaParseResult",
    "CorporateAction",
    "CorporateActionResolutionError",
    "IngestCounts",
    "UnresolvedIdentity",
    "build_scrip_index",
    "load_corporate_actions",
    "month_from_name",
    "write_corporate_actions",
]

_LOG = get_logger(__name__)

#: English month abbreviations, spelled out rather than handed to `strptime("%b")`, which reads
#: `LC_TIME`. Both exchanges date their records with a three-letter month at least some of the
#: time, and a parser whose output depends on the host locale is not reproducible (matches the
#: convention already set by the bhavcopy and FII/DII parsers).
_MONTHS: Final[Mapping[str, int]] = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def month_from_name(name: str) -> int | None:
    """Month number for a three-letter English abbreviation, case-insensitively, or `None`.

    Shared by both exchange parsers so the locale-independence guarantee lives in one place.
    """
    return _MONTHS.get(name.strip().upper()[:3])


class CorporateActionResolutionError(IngestError):
    """A feed row could not be attached to an ISIN through the D2 identity master.

    Raised, never swallowed: a corporate action whose security we cannot name is not a row to file
    under a guessed ISIN (that silently rewrites the wrong instrument's history) and not one to
    drop (that loses a real event). It stops the row and names what could not be resolved, so the
    caller records it and a human fixes the identity data behind it (invariant #2).
    """


class CorporateAction(BaseModel):
    """One corporate action, normalized — the single shape both exchange feeds produce.

    What it does: carry the identity, timing, type and structured terms of one action, plus the
    verbatim source text and the L0 lineage that make it auditable and re-derivable.
    What it assumes: `isin` was resolved through the D2 identity master by the parser that built
    this (both feeds route through it — NSE validates its native ISIN, BSE resolves its scrip
    code), so the ISIN here is a real join key and not a symbol in disguise.
    What it never does: hold an adjustment factor, an adjusted price, or a reconciliation verdict.
    Those are M2.4 and M2.3; this is the raw event.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(
        pattern=ISIN_PATTERN,
        description="ISO 6166 identifier resolved via D2 — the only legitimate join key",
    )
    ex_date: date = Field(description="the ex-date the action takes effect from (Asia/Kolkata)")
    action_type: ActionType = Field(description="normalized type from the M2.1 taxonomy")
    terms: Terms = Field(
        description="structured terms; `UnquantifiedTerms` when the text stated none"
    )
    source: str = Field(min_length=1, description="Source Register id, e.g. 'nse_corp_actions'")
    raw_text: str = Field(description="the exchange's purpose string, exactly as published")
    knowable_date: date = Field(
        description="first date this action was knowable to us (invariant #7): NSE's broadcast "
        "date, or the ingest date for a feed that publishes none"
    )
    record_date: date | None = Field(
        default=None, description="the record/book-closure date, when the feed states one"
    )
    announcement_date: date | None = Field(
        default=None, description="when the exchange announced/broadcast the action, when stated"
    )
    source_ref: str | None = Field(
        default=None, description="the source's own identifier for the row (NSE symbol, BSE scrip)"
    )
    l0_key: str | None = Field(
        default=None, description="`source/date/filename` of the L0 payload this was derived from"
    )

    @model_validator(mode="after")
    def _terms_are_legal_for_type(self) -> CorporateAction:
        """The same rule `ParsedAction` enforces: a bonus cannot carry face values.

        Re-checked here rather than trusted from the parser, because a `CorporateAction` read back
        out of the database has not been through `ParsedAction` and an illegal `(type, terms)` pair
        in stored jsonb must fail on load, not reach the factor chain.
        """
        allowed = TERMS_BY_ACTION[self.action_type]
        if not isinstance(self.terms, allowed):
            names = ", ".join(model.__name__ for model in allowed)
            raise ValueError(f"{self.action_type} takes {names}, not {type(self.terms).__name__}")
        return self

    @classmethod
    def from_parsed(
        cls,
        parsed: ParsedAction,
        *,
        isin: str,
        source: str,
        ex_date: date,
        knowable_date: date,
        record_date: date | None = None,
        announcement_date: date | None = None,
        source_ref: str | None = None,
        l0_key: str | None = None,
    ) -> CorporateAction:
        """Build a row from a normalizer `ParsedAction` plus the feed row's identity and dates.

        The seam between M2.1 and M2.2: the normalizer establishes *what the action is* from the
        purpose string; the parser establishes *whose it is and when* from the surrounding feed
        row. `raw_text` and the type/terms come from the parsed action so they cannot drift from
        what the normalizer actually saw.
        """
        return cls(
            isin=isin,
            ex_date=ex_date,
            action_type=parsed.action_type,
            terms=parsed.terms,
            source=source,
            raw_text=parsed.raw_text,
            knowable_date=knowable_date,
            record_date=record_date,
            announcement_date=announcement_date,
            source_ref=source_ref,
            l0_key=l0_key,
        )

    @property
    def dividend_amount_inr(self) -> Decimal | None:
        """The cash dividend per share in rupees, when the terms state one as an amount.

        Populates the dedicated `dividend_amount_inr` column so a dividend query need not unpack
        jsonb. `None` for a percentage-of-face-value dividend (the rupee figure needs a face value
        this row does not carry) and for every non-dividend action.
        """
        if isinstance(self.terms, DividendTerms):
            return self.terms.amount_inr
        return None

    def ratio_terms_json(self) -> dict[str, object]:
        """The terms as the discriminated jsonb blob stored in `corporate_actions.ratio_terms`."""
        return cast("dict[str, object]", TERMS_ADAPTER.dump_python(self.terms, mode="json"))

    def describe(self) -> str:
        """This action as one human-readable sentence (the M2.1 renderer)."""
        return describe(self.action_type, self.terms)


@dataclass(frozen=True, slots=True)
class UnresolvedIdentity:
    """A feed row that parsed to a real action but could not be attached to an ISIN via D2.

    Kept rather than dropped, and kept separately from the manual-entry queue: this is an identity
    gap (a scrip code the master has never seen, a native ISIN unknown to it, a symbol resolving to
    a different security), not an unparseable purpose string. A human fixes it in the identity data
    (D2), not by hand-entering CA terms.
    """

    source: str
    source_ref: str
    ex_date: date
    raw_text: str
    reason: str


@dataclass(frozen=True, slots=True)
class CaParseResult:
    """Everything one feed payload produced: the rows to persist, and the two kinds of leftover.

    Both exchange parsers return this shape. `actions` is what `write_corporate_actions` persists;
    `queued` and `unresolved` are the two ways a row is deliberately *not* silently accepted — an
    unparseable purpose string (M2.1's manual-entry queue) and an unresolvable identity. Neither is
    an error that stops the feed: a single bad row is recorded and the rest of the file lands.
    """

    actions: tuple[CorporateAction, ...] = ()
    queued: tuple[ManualQueueEntry, ...] = ()
    unresolved: tuple[UnresolvedIdentity, ...] = ()

    @property
    def is_clean(self) -> bool:
        """True when every row in the payload became a persistable action."""
        return not self.queued and not self.unresolved


@dataclass(frozen=True, slots=True)
class IngestCounts:
    """What a write actually changed. `inserted + skipped` is the number of rows offered."""

    inserted: int = 0
    skipped: int = 0

    @property
    def offered(self) -> int:
        return self.inserted + self.skipped


def build_scrip_index(master: IdentityMaster, exchange: Exchange = Exchange.BSE) -> dict[str, str]:
    """A `{security_code: isin}` map for one exchange, built from the D2 identity master.

    The BSE corporate-actions feed keys on a scrip code, not an ISIN (§4.1); this is the only
    legitimate scrip→ISIN path, and it goes through the master's own listing facts rather than any
    private table (invariant #2). A scrip code that two ISINs both claim is dropped from the index
    and named in the log — an ambiguous scrip is exactly the kind of identity defect that must not
    resolve to a silent pick — so a feed row bearing it lands in `unresolved` rather than under a
    guessed ISIN.
    """
    index: dict[str, str] = {}
    ambiguous: set[str] = set()
    for isin in master.securities:
        listing = master.listing(isin, exchange)
        if listing is None or listing.security_code is None:
            continue
        code = listing.security_code.strip()
        if not code:
            continue
        existing = index.get(code)
        if existing is not None and existing != isin:
            ambiguous.add(code)
            continue
        index[code] = isin
    for code in ambiguous:
        index.pop(code, None)
        _LOG.warning(
            "ca.scrip.ambiguous",
            exchange=exchange.value,
            security_code=code,
            detail="scrip code maps to more than one ISIN; rows bearing it will not resolve",
        )
    return index


def write_corporate_actions(
    conn: Connection,
    actions: Sequence[CorporateAction],
    *,
    clock: Clock,
) -> IngestCounts:
    """Persist normalized corporate actions; return how many rows were new.

    Idempotent per `(isin, ex_date, action_type, source)` — the table's unique key. A row that
    already exists is left exactly as it was (`ON CONFLICT DO NOTHING`), so re-ingesting a feed
    that has not changed writes nothing and does not even restamp `recorded_at`. That is what lets
    a daily-forward run and a backfill overlap safely, and what makes the round-trip test's
    "re-ingest changes nothing" assertion hold.

    Does not commit or roll back — the caller owns the transaction, so an identity refresh and the
    CA rows that resolve against it can share one. `reconciled` is left false on every row: whether
    NSE and BSE agree is M2.3's verdict, not this writer's.
    """
    if not actions:
        return IngestCounts()

    recorded_at = clock.now()
    inserted = 0
    for action in actions:
        row = conn.execute(
            "INSERT INTO corporate_actions "
            "(isin, ex_date, action_type, ratio_terms, dividend_amount_inr, record_date, "
            " announcement_date, knowable_date, source, source_ref, raw_text, l0_key, recorded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (isin, ex_date, action_type, source) DO NOTHING RETURNING id",
            (
                action.isin,
                action.ex_date,
                action.action_type.value,
                json.dumps(action.ratio_terms_json()),
                action.dividend_amount_inr,
                action.record_date,
                action.announcement_date,
                action.knowable_date,
                action.source,
                action.source_ref,
                action.raw_text,
                action.l0_key,
                recorded_at,
            ),
        ).fetchone()
        if row is not None:
            inserted += 1

    counts = IngestCounts(inserted=inserted, skipped=len(actions) - inserted)
    _LOG.info(
        "ca.persisted",
        offered=counts.offered,
        inserted=counts.inserted,
        skipped=counts.skipped,
        state="NORMALIZED",
    )
    return counts


def load_corporate_actions(
    conn: Connection, *, isin: str | None = None, source: str | None = None
) -> tuple[CorporateAction, ...]:
    """Read normalized corporate actions back out of the store, newest ex-date last.

    The round trip `write_corporate_actions` is verified against: the jsonb terms come back through
    the same discriminated adapter that wrote them, so a `(type, terms)` pair that survived the
    write but would be illegal on read fails here rather than in the factor chain.
    """
    sql = (
        "SELECT isin, ex_date, action_type, ratio_terms, record_date, announcement_date, "
        "knowable_date, source, source_ref, raw_text, l0_key FROM corporate_actions"
    )
    clauses: list[str] = []
    params: list[object] = []
    if isin is not None:
        clauses.append("isin = %s")
        params.append(isin)
    if source is not None:
        clauses.append("source = %s")
        params.append(source)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY isin, ex_date, action_type, source"

    rows = conn.execute(sql, tuple(params)).fetchall()
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
