"""Parse one NSE results filing (XBRL) into point-in-time `FundamentalFact`s (M7.3).

NSE serves each results filing as an XBRL document against a BSE-published `in-bse-fin` taxonomy.
The shape is not the "one document, one period" instance a reader might expect from generic XBRL —
it is a **transcription of the published results table, column by column**, and getting that wrong
is what made the first cut of this parser unable to read a single real filing. Four properties of
the real format drive every decision here; each was confirmed against the captured filings under
`tests/fixtures/xbrl/filings/` (B8) rather than inferred from the taxonomy.

* **A context is a column, and the column declares its own period.** A quarterly filing carries two
  duration contexts with *no* dimension — conventionally `OneD` (the quarter) and `FourD` (the
  cumulative year-to-date) — and both usually state the *same* `xbrli:period`, the quarter's dates.
  The `xbrli:period` is therefore useless for telling them apart. What does tell them apart is that
  each column reports `DateOfStartOfReportingPeriod` / `DateOfEndOfReportingPeriod` *as facts inside
  itself*: `OneD` says 01-Oct→31-Dec, `FourD` says 01-Apr→31-Dec. Those in-context facts are the
  authoritative period, and `_columns` reads them.
* **The announcements index chooses the column.** One document serves more than one index entry: a
  December-year-end company's document is linked by both a Quarterly entry (01-Oct→31-Dec) and an
  Annual entry (01-Jan→31-Dec), and each names the column it means through its `fromDate`/`toDate`.
  So `parse` takes the `FilingIndexEntry` and selects the column whose declared period equals the
  entry's `(period_start, period_end)` exactly. Matching on `period_end` alone would admit the
  cumulative column of the same filing — a nine-month revenue stored as a quarter.
* **The document does not carry a usable ISIN; the index does.** Every context identifies the entity
  by *NSE symbol* (`scheme="http://www.nseindia.com/NSESymbol"`, e.g. `VSTTILLERS`). The Ind-AS
  filings have no ISIN element at all, and where the banking taxonomy does have one it can be stale
  — a captured HDFCBANK filing says `INE040A01034` where the index says `INE040A01018`. So the ISIN
  is taken from the index entry (D2's join key, invariant #2) and the *symbol* is what gets
  cross-checked against the document. The document's own ISIN element is never read.
* **A segment is an explicit dimension member in `xbrli:scenario`, and it is column-scoped.** The
  segment breakdown is `SegmentRevenue` under `in-bse-fin:ReportableSegmentsAxis`, whose members
  embed the column (`OneReportableSegmentRevenue01Member` vs `FourReportableSegment...`); the
  segment's human-readable name is a sibling `DescriptionOfReportableSegment` fact in the same
  context. Not `xbrli:segment`, not a typed member, not `SegmentName` — those were the fabricated
  fixtures' invention, and no real filing has them.

Two further decisions worth stating because they are load-bearing:

* **`filing_date` is never read from the document.** It is the exchange dissemination date, which
  lives only in the announcements index; the document knows the period it reports, not when the
  market learned it. The two therefore come from two genuinely independent places, which is what
  makes `(period_end, filing_date)` a real PIT pair rather than one field dressed as two
  (acceptance 1, invariant #7).
* **Values are absolute rupees.** `LevelOfRoundingUsedInFinancialStatements` says `Lakhs`/`Crores`/
  `Millions`, but that describes the *published statement*, not the XBRL: a captured Reliance
  filing tags 2438650000000 for a quarter its statement prints as 243,865 crore. Nothing here
  scales a value, and a filing that did mean lakhs would be wrong by 1e5 — hence the check that the
  fixtures' headline figures match the filings' own published numbers (`tests/unit/test_xbrl.py`).

Matching is by element *local-name* within the xbrli / xbrldi / in-bse-fin families rather than a
pinned namespace URI, so a taxonomy revision (a new date in the namespace) does not silently stop
the parser finding facts it should — a format change must fail loudly, not return nothing. Money is
`Decimal` throughout: values are read as text and converted exactly, and `NaN`/`Infinity` or
anything that is not a plain decimal literal is a `ParseError`, never a number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final
from xml.etree import ElementTree as ET

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.xbrl.discovery import FilingIndexEntry
from dataplatform.ingest.xbrl.models import (
    Filing,
    FundamentalFact,
    Nature,
    Taxonomy,
    concepts_for,
)
from dataplatform.logging import get_logger

__all__ = [
    "SEGMENT_CONCEPT",
    "SOURCE_ID",
    "parse",
]

_LOG = get_logger(__name__)

#: The register id the point-in-time filings path serves (`source_register.yaml`, parser M7.3).
SOURCE_ID: Final = "nse_xbrl_filing"

#: The concept key a per-segment revenue fact carries. One element (`SegmentRevenue`) split by a
#: context dimension, so it is not in the per-taxonomy concept maps (which are name → key).
SEGMENT_CONCEPT: Final = "segment_revenue"

#: The entity-identifier scheme most real NSE filings use: the identifier is an NSE *symbol*, not
#: an ISIN. Asserting the scheme is how a future switch to an ISIN scheme fails loudly here instead
#: of silently comparing a symbol against an ISIN.
_NSE_SYMBOL_SCHEME: Final = "http://www.nseindia.com/NSESymbol"

#: The other scheme in the wild: a BSE scrip code (e.g. `532209` for J&K Bank). Its identifier is
#: *not* a symbol and must never be compared to one, so a filing using it is cross-checked on its
#: `Symbol` fact instead. Recognised explicitly rather than by "anything that is not NSESymbol", so
#: a third scheme is still a loud failure.
_BSE_SCRIP_SCHEME: Final = "http://www.bseindia.com/bse-fin/ScripCode"

#: Elements that populate filing-level fields rather than becoming facts.
_NATURE_ELEMENT: Final = "NatureOfReportStandaloneConsolidated"
_AUDITED_ELEMENT: Final = "WhetherResultsAreAuditedOrUnaudited"
_PERIOD_START_ELEMENT: Final = "DateOfStartOfReportingPeriod"
_PERIOD_END_ELEMENT: Final = "DateOfEndOfReportingPeriod"
#: The document's financial year. Part of the header block, which the 2018-2022 `…_WEB.xml`
#: generation attaches to the `OneD` context by convention — it describes the *document*, not that
#: context, so it is read document-wide and never as a column's own period.
_FY_START_ELEMENT: Final = "DateOfStartOfFinancialYear"
_FY_END_ELEMENT: Final = "DateOfEndOfFinancialYear"

#: The column token of the *cumulative* column — the year-to-date, and on an annual filing the year.
#: Every filing in the captured corpus that declares its columns' periods agrees on this (`FourD`
#: carries the financial-year range and `OneD` the discrete quarter), which is what licenses using
#: it for the filings that declare no per-column period at all: those state the financial year
#: once, in the header, and it is `Four`'s period. `One`'s is not derivable — a quarter's start
#: appears nowhere in such a document — so an undeclared `One` is skipped rather than guessed.
_CUMULATIVE_TOKEN: Final = "Four"
#: `NameOfTheCompany` in the Ind-AS taxonomy; the banking one calls the same thing `NameOfBank`.
_NAME_ELEMENTS: Final = ("NameOfTheCompany", "NameOfBank")
#: The document's own copy of the NSE symbol, cross-checked alongside the entity identifier.
_SYMBOL_ELEMENT: Final = "Symbol"

#: The segment breakdown: `SegmentRevenue` under `ReportableSegmentsAxis`, named by a sibling
#: `DescriptionOfReportableSegment` fact in the same context.
_SEGMENT_AXIS: Final = "ReportableSegmentsAxis"
_SEGMENT_NAME_ELEMENT: Final = "DescriptionOfReportableSegment"
_SEGMENT_REVENUE_ELEMENT: Final = "SegmentRevenue"

#: How each taxonomy family announces itself in the `link:schemaRef` entry point, matched on the
#: stem so the version date in the filename is not part of the identity (`Taxonomy`). Order matters
#: only in that `other_than_banks_` and `banking_` must be distinguished before the Ind-AS stems;
#: they share no substring, so the tuple is simply the observed set. Every entry point met across a
#: decade of the real index is here — an unrecognised one is a hard failure by design.
_ENTRY_POINTS: Final[tuple[tuple[str, Taxonomy], ...]] = (
    ("banking_entry_point", Taxonomy.BANKING),
    ("other_than_banks_entry_point", Taxonomy.NON_IND_AS),
    ("ind-as_entry_point", Taxonomy.IND_AS),
    ("in-bse-fin-", Taxonomy.IND_AS),
)

#: The ordinal word a context id leads with — the results table's column, and the only reliable link
#: between a dimensioned context (`OneReportableSegmentRevenue01D`) and the undimensioned column
#: context that declares that column's period (`OneD`).
_COLUMN_TOKEN = re.compile(
    r"^(One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|Eleven|Twelve)(?=[A-Z]|$)"
)

#: A plain decimal literal, optionally signed. Checked before `Decimal()` sees the text because
#: `Decimal` itself accepts `NaN`/`Infinity`, and a mis-framed field spelling one of those must not
#: become a value that compares greater than everything.
_DECIMAL_LITERAL = re.compile(r"^[+-]?\d+(\.\d+)?$")
#: An ISO date as XBRL states it (`2026-06-30`).
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class _ContextShape:
    """What one `<context>` is structurally: its column token, whether it is a duration, its axes.

    Resolved once for every context in the document, because both halves of the parse need it and
    for opposite reasons: `_columns` wants the contexts with *no* dimension (the table's columns),
    and `_segment_facts` wants exactly those carrying the reportable-segments axis.
    """

    context_id: str
    token: str
    is_duration: bool
    axes: frozenset[str]

    @property
    def is_column(self) -> bool:
        """Whether this context is one of the results table's columns: a duration, no dimension."""
        return self.is_duration and not self.axes


@dataclass(frozen=True, slots=True)
class _Column:
    """One column of the published results table, as the document transcribes it.

    `token` is the ordinal the context ids share (`One`, `Four`); `context_id` is the undimensioned
    duration context that anchors the column and carries its company-level facts; `period` is what
    that context *declared* through its own `DateOf…ReportingPeriod` facts, which is the period the
    column is really about — not its `xbrli:period`, which is unreliable.
    """

    token: str
    context_id: str
    period_start: date
    period_end: date
    nature: Nature
    audited: bool | None


def parse(
    payload: bytes,
    *,
    entry: FilingIndexEntry,
    known_symbols: frozenset[str] | None = None,
    source: str = SOURCE_ID,
    l0_key: str | None = None,
    filename: str,
) -> Filing:
    """Parse one XBRL results filing into the `Filing` the given index entry names.

    `entry` is not a convenience bundle — it is what makes the parse well-defined. It supplies the
    ISIN (invariant #2: the document has no usable one), the `filing_date` (first-knowable, which
    the document does not carry), the `filing_id` that keys this filing apart from a later
    restatement, and the `(period_start, period_end)` that **selects which column** of the results
    table to read. One document commonly holds two columns — a quarter and a cumulative period —
    and only the entry says which one this filing is.

    Cross-checks rather than trusts: the document's own symbol must name the same company as the
    entry, and the selected column's `NatureOfReportStandaloneConsolidated` must equal
    `entry.nature`. A disagreement is a `ParseError`, because it means the index and the document
    are describing different filings and neither side is safe to prefer.

    `known_symbols` is every symbol the entry's ISIN has ever traded under, from the D2 master.
    Pass it whenever the master is at hand — companies get renamed, and a filing states the symbol
    it had *when filed*, not the one the index reports today. Omitted, the check falls back to
    `entry.symbol` alone, which is right for a fresh filing and wrong for a renamed company; the
    parser stays offline either way, so resolving the history is the caller's job.

    Raises `ParseError`, naming the file, for anything that is not this format: a body that is not
    well-formed XML, a root that is not `xbrl`, an unrecognised taxonomy entry point, an entity
    identified by something other than an NSE symbol, no column matching the entry's period, an
    ambiguous match, a value that is not a plain decimal, or a filing whose selected column reports
    none of the whitelisted concepts. Never returns a partial or repaired filing.
    """
    if entry.period_start is None:
        raise ParseError(
            f"index entry {entry.seq_number} has no period start, so the results column it means "
            "cannot be identified; a filing document holds several periods",
            filename=filename,
        )

    root = _root(payload, filename=filename)
    taxonomy = _taxonomy(root, filename=filename)
    shapes = _shapes(root, filename=filename)
    facts_by_context = _facts_by_context(root)

    accepted = frozenset({entry.symbol}) if known_symbols is None else known_symbols
    symbol = _check_symbol(
        root,
        entry=entry,
        facts_by_context=facts_by_context,
        accepted=accepted,
        filename=filename,
    )

    column = _select_column(
        _columns(shapes, facts_by_context=facts_by_context, filename=filename),
        entry=entry,
        filename=filename,
    )
    if column.nature is not entry.nature:
        raise ParseError(
            f"index says this filing is {entry.nature.value} but column {column.context_id!r} of "
            f"the document reports {column.nature.value}; the announcements index and the XBRL "
            "must describe the same filing",
            filename=filename,
        )

    facts = _facts(
        taxonomy=taxonomy,
        column=column,
        shapes=shapes,
        facts_by_context=facts_by_context,
        entry=entry,
        source=source,
        l0_key=l0_key,
        filename=filename,
    )

    try:
        filing = Filing(
            isin=entry.isin,
            symbol=symbol,
            taxonomy=taxonomy,
            name=_company_name(facts_by_context, entry=entry),
            period_start=column.period_start,
            period_end=column.period_end,
            filing_date=entry.filing_date,
            nature=column.nature,
            filing_id=entry.filing_id,
            audited=column.audited if column.audited is not None else entry.audited,
            source=source,
            l0_key=l0_key,
            facts=facts,
        )
    except ValueError as exc:
        raise ParseError(str(exc), filename=filename) from exc

    _LOG.info(
        "xbrl.parsed",
        source=source,
        filename=filename,
        isin=filing.isin,
        symbol=filing.symbol,
        taxonomy=filing.taxonomy.value,
        column=column.context_id,
        period_start=filing.period_start.isoformat() if filing.period_start else None,
        period_end=filing.period_end.isoformat(),
        filing_date=filing.filing_date.isoformat(),
        nature=filing.nature.value,
        facts=len(filing.facts),
        segments=len(filing.segments()),
        state="VALIDATED",
    )
    return filing


# ── XML plumbing ───────────────────────────────────────────────────────────────────────────────


def _root(payload: bytes, *, filename: str) -> ET.Element:
    """The document root, confirmed to be an XBRL instance."""
    if not payload.strip():
        raise ParseError("empty response body", filename=filename)
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ParseError(f"body is not well-formed XML: {exc}", filename=filename) from exc
    if _local(root.tag) != "xbrl":
        raise ParseError(
            f"root element is {_local(root.tag)!r}, not an XBRL <xbrl> instance", filename=filename
        )
    return root


def _local(tag: str) -> str:
    """The local-name of a possibly-namespaced tag (`{uri}RevenueFromOperations` → the name)."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _qname_local(qname: str) -> str:
    """The local part of a prefixed QName (`in-bse-fin:ReportableSegmentsAxis` → the axis name).

    Distinct from `_local`: ElementTree expands namespaces in *tags* to `{uri}name`, but leaves a
    QName appearing in an *attribute value* or element text — which is how XBRL writes a dimension
    and its member — with its source prefix intact. Stripping the wrong one of the two is silent:
    the comparison simply never matches and the filing looks segment-free.
    """
    return qname.rsplit(":", 1)[-1] if ":" in qname else _local(qname)


def _taxonomy(root: ET.Element, *, filename: str) -> Taxonomy:
    """Which in-bse-fin vocabulary this filing speaks, from its `link:schemaRef` entry point.

    Resolved from the document rather than taken from the index's `indAs` field so the vocabulary a
    fact is read with is the one the fact was written against. An unrecognised entry point is a hard
    failure: guessing a vocabulary would find no concepts and report an empty filing as success.
    """
    for element in root.iter():
        if _local(element.tag) != "schemaRef":
            continue
        href = next(
            (value for key, value in element.attrib.items() if _local(key) == "href"), ""
        ).lower()
        for marker, taxonomy in _ENTRY_POINTS:
            if marker in href:
                return taxonomy
        raise ParseError(
            f"schemaRef entry point {href!r} is not a results taxonomy this parser knows "
            f"({', '.join(marker for marker, _ in _ENTRY_POINTS)})",
            filename=filename,
        )
    raise ParseError(
        "no <link:schemaRef>; a results filing declares its taxonomy entry point", filename=filename
    )


def _facts_by_context(root: ET.Element) -> dict[str, dict[str, list[str]]]:
    """Every fact's text, grouped `context id → element local-name → values in document order`.

    One pass, because the column model needs to look facts up by context repeatedly (a column's
    declared period, its nature, then its concepts) and re-walking the tree per lookup is what made
    the previous cut's `_find_local`/`_first_fact_element` helpers quietly O(n) per field. Values
    are lists: a well-formed filing reports a concept once per context, and keeping duplicates
    visible lets `_one` refuse them loudly instead of silently taking the first.
    """
    grouped: dict[str, dict[str, list[str]]] = {}
    for element in root.iter():
        context_ref = element.get("contextRef")
        if context_ref is None:
            continue
        text = (element.text or "").strip()
        grouped.setdefault(context_ref, {}).setdefault(_local(element.tag), []).append(text)
    return grouped


def _one(facts: dict[str, list[str]], name: str, *, context_id: str, filename: str) -> str | None:
    """The single value of `name` in one context: None if absent, `ParseError` if reported twice."""
    values = [value for value in facts.get(name, ()) if value]
    if not values:
        return None
    if len(values) > 1:
        raise ParseError(
            f"context {context_id!r} reports {name} {len(values)} times ({', '.join(values)}); a "
            "column reports each concept once",
            filename=filename,
        )
    return values[0]


# ── identity ───────────────────────────────────────────────────────────────────────────────────


def _check_symbol(
    root: ET.Element,
    *,
    entry: FilingIndexEntry,
    facts_by_context: dict[str, dict[str, list[str]]],
    accepted: frozenset[str],
    filename: str,
) -> str:
    """Assert the document is about the company the index entry names, and return its symbol.

    The symbol is the only identity a filing states usefully, so it is what gets cross-checked and
    the ISIN is left to the index — which is what keeps invariant #2's join key coming from D2
    rather than from a filer's typing. Two real-world facts shape how the check is done:

    * **The identifier is not always a symbol.** Most filings use `scheme=…/NSESymbol`, but some
      (notably bank returns) identify the entity by BSE scrip code, which cannot be compared to a
      symbol at all. Those are checked on the document's own `Symbol` fact. A scheme that is
      neither is a hard failure: guessing what an unknown identifier means is how a filing gets
      stored under the wrong company.
    * **Companies are renamed.** A filing from 2023 states the symbol it had in 2023, while the
      index states today's. `accepted` is therefore the set of symbols the ISIN has *ever* traded
      under (from the D2 master, via the caller); the document must match one of them. Comparing
      against today's symbol alone rejects every renamed company — Sastasundar Ventures files as
      `SASTASUNDR` under an index entry that now says `HEALTHX`.
    """
    identifiers: set[str] = set()
    schemes: set[str] = set()
    for element in root.iter():
        if _local(element.tag) != "identifier":
            continue
        identifiers.add((element.text or "").strip())
        schemes.add((element.get("scheme") or "").strip())
    if len(identifiers) > 1:
        raise ParseError(
            f"filing names more than one entity ({', '.join(sorted(identifiers))}); one results "
            "filing is about one company",
            filename=filename,
        )
    unknown = schemes - {_NSE_SYMBOL_SCHEME, _BSE_SCRIP_SCHEME}
    if unknown:
        raise ParseError(
            f"entity identifier scheme is {', '.join(sorted(schemes))!r}, expected one of "
            f"{[_NSE_SYMBOL_SCHEME, _BSE_SCRIP_SCHEME]}; this parser reads the identifier as an "
            "NSE symbol or a BSE scrip code and takes the ISIN from the announcements index "
            "(invariant #2)",
            filename=filename,
        )

    # Whatever the document states about its own symbol, from either place it can state it. Some
    # `…_WEB.xml` filings declare no `<context>` at all (see `_shapes`) and so carry no
    # `<xbrli:entity>` either; their `Symbol` fact is then the only identity they state, and it is
    # enough. Only a filing that states *neither* is unidentifiable.
    stated: set[str] = set()
    if schemes == {_NSE_SYMBOL_SCHEME}:
        stated |= identifiers
    for facts in facts_by_context.values():
        stated |= {value for value in facts.get(_SYMBOL_ELEMENT, ()) if value}
    if not stated:
        described = (
            f"only as {next(iter(identifiers))!r} under scheme {next(iter(schemes))!r}"
            if identifiers
            else "by no <xbrli:identifier> at all"
        )
        raise ParseError(
            f"filing identifies its entity {described} and states no <{_SYMBOL_ELEMENT}>, so it "
            f"cannot be checked against the index entry for {entry.symbol!r}",
            filename=filename,
        )

    matched = {
        symbol
        for symbol in stated
        if any(_same_symbol(symbol, candidate) for candidate in accepted)
    }
    if not matched:
        raise ParseError(
            f"index says this filing is {entry.symbol!r} (ISIN {entry.isin}, symbols ever used: "
            f"{', '.join(sorted(accepted))}) but the document says "
            f"{', '.join(sorted(stated))}; the announcements index and the XBRL must name the "
            "same company",
            filename=filename,
        )
    return sorted(matched)[0]


def _same_symbol(left: str, right: str) -> bool:
    """Whether two NSE symbols are the same, ignoring case and surrounding blanks."""
    return left.strip().upper() == right.strip().upper()


def _company_name(
    facts_by_context: dict[str, dict[str, list[str]]], *, entry: FilingIndexEntry
) -> str:
    """The company name as the document filed it, falling back to the index's.

    Display only, never an identity: the two spellings routinely differ in punctuation, and neither
    is a join key. A filing that omits it is not a broken filing, so this does not fail.
    """
    for facts in facts_by_context.values():
        for element in _NAME_ELEMENTS:
            for value in facts.get(element, ()):
                if value:
                    return value
    return entry.name


# ── columns ────────────────────────────────────────────────────────────────────────────────────


def _shapes(root: ET.Element, *, filename: str) -> dict[str, _ContextShape]:
    """Every `<context>` in the document, resolved to its structural shape, keyed by id.

    One pass over the contexts. Dimension members live in `xbrli:scenario` in every real filing
    (not `xbrli:segment`, which is what the fabricated fixtures used and what generic XBRL examples
    show), so both containers are read and the axis is taken from the member's `dimension`
    attribute — which is the durable part either way.

    A document may declare *no* context for a ref its facts use: the 2018-2022 `…_WEB.xml`
    generation routinely declares only the dimensioned contexts and leaves `OneD`/`FourD` — the
    table's own columns — undeclared, and some declare no context at all. That is malformed XBRL,
    but it is what NSE served for four years, and it costs nothing to read: a column's period,
    nature and audited flag are all facts *inside* the column, never attributes of the `<context>`
    element. So a missing declaration is not an error here; it simply means "no dimension", which
    `_column_shape` supplies. What a `<context>` is needed for is the opposite question — proving a
    ref *is* dimensioned — and a ref nobody declared cannot be a segment (`_segment_facts`).
    """
    shapes: dict[str, _ContextShape] = {}
    for element in root.iter():
        if _local(element.tag) != "context":
            continue
        context_id = element.get("id")
        if not context_id:
            raise ParseError("a <context> has no id attribute", filename=filename)
        axes: set[str] = set()
        is_duration = False
        for child in element.iter():
            local = _local(child.tag)
            if local in ("explicitMember", "typedMember"):
                axes.add(_qname_local(child.get("dimension") or ""))
            elif local == "startDate" and (child.text or "").strip():
                is_duration = True
        token = _COLUMN_TOKEN.match(context_id)
        shapes[context_id] = _ContextShape(
            context_id=context_id,
            token=token.group(1) if token else context_id,
            is_duration=is_duration,
            axes=frozenset(axes),
        )
    return shapes


def _column_shape(context_id: str) -> _ContextShape:
    """The shape of a context no `<context>` element declared: undimensioned, by construction.

    Its token still comes from the id (`OneD` → `One`), which is what links the column to the
    dimensioned contexts hanging off it.
    """
    token = _COLUMN_TOKEN.match(context_id)
    return _ContextShape(
        context_id=context_id,
        token=token.group(1) if token else context_id,
        is_duration=True,
        axes=frozenset(),
    )


def _columns(
    shapes: dict[str, _ContextShape],
    *,
    facts_by_context: dict[str, dict[str, list[str]]],
    filename: str,
) -> tuple[_Column, ...]:
    """Every column of the results table this document transcribes.

    A column is identified by **what it reports, not by what declared it**: a context carrying its
    own `DateOfStartOfReportingPeriod`/`DateOfEndOfReportingPeriod` and
    `NatureOfReportStandaloneConsolidated`, with no dimension member. Iterating the *facts* rather
    than the `<context>` elements is what lets the 2018-2022 `…_WEB.xml` generation parse at all —
    those documents leave the column contexts undeclared (see `_shapes`), so a walk over declared
    contexts finds every expense-line breakdown and not one column.

    The period comes from those in-context facts and never from `xbrli:period`: real filings put
    the quarter's dates on the cumulative column's `xbrli:period` too, so the attribute cannot tell
    the two apart. A context that reports no period or no nature is a breakdown or a spare, not a
    column, and is skipped.
    """
    financial_year = _document_financial_year(facts_by_context, filename=filename)
    columns: list[_Column] = []
    for context_id in facts_by_context:
        shape = shapes.get(context_id) or _column_shape(context_id)
        if shape.axes:
            continue
        facts = facts_by_context.get(context_id, {})
        period = _declared_period(
            facts,
            context_id=context_id,
            token=shape.token,
            financial_year=financial_year,
            filename=filename,
        )
        if period is None:
            # A context that says nothing about the period it covers is not one of the table's
            # columns — it is a spare, or the old format's unlabelled second column. Either way
            # there is no honest way to say which period its numbers belong to, so it is skipped
            # rather than guessed at from its position or from how many facts it happens to carry.
            continue
        start, end = period
        nature = _nature(facts, context_id=context_id, filename=filename)
        if nature is None:
            continue
        columns.append(
            _Column(
                token=shape.token,
                context_id=context_id,
                period_start=_iso_date(
                    start, what=f"context {context_id!r} {_PERIOD_START_ELEMENT}", filename=filename
                ),
                period_end=_iso_date(
                    end, what=f"context {context_id!r} {_PERIOD_END_ELEMENT}", filename=filename
                ),
                nature=nature,
                audited=_audited(facts, context_id=context_id, filename=filename),
            )
        )
    if not columns:
        raise ParseError(
            "no results column: no undimensioned context declares both a period "
            f"({_PERIOD_START_ELEMENT}/{_PERIOD_END_ELEMENT}, or the document's "
            f"{_FY_START_ELEMENT}/{_FY_END_ELEMENT} for the {_CUMULATIVE_TOKEN} column) and "
            f"{_NATURE_ELEMENT}",
            filename=filename,
        )
    return tuple(columns)


def _document_financial_year(
    facts_by_context: dict[str, dict[str, list[str]]], *, filename: str
) -> tuple[str, str] | None:
    """The financial year the document declares, from wherever in it that is declared.

    Read document-wide on purpose. These elements are part of the header block, which filers pin
    to the `OneD` context — but they describe the filing, not that column, and reading them as
    `OneD`'s own period puts the year's dates on the quarter's numbers. On the captured old-era
    annual filings that mistake is not subtle: `OneD` is zero-filled and `FourD` holds the year, so
    it stores a company's revenue as zero.
    """
    starts = {
        v for facts in facts_by_context.values() for v in facts.get(_FY_START_ELEMENT, ()) if v
    }
    ends = {v for facts in facts_by_context.values() for v in facts.get(_FY_END_ELEMENT, ()) if v}
    if len(starts) != 1 or len(ends) != 1:
        # None stated, or two filings' worth of header in one document — neither is a year we can
        # attribute a column to.
        return None
    return next(iter(starts)), next(iter(ends))


def _declared_period(
    facts: dict[str, list[str]],
    *,
    context_id: str,
    token: str,
    financial_year: tuple[str, str] | None,
    filename: str,
) -> tuple[str, str] | None:
    """The period a column covers, as `(start, end)` strings — or None if nothing states one.

    Two sources, in order:

    * `DateOfStartOfReportingPeriod` / `DateOfEndOfReportingPeriod` *inside the column*. The
      authoritative answer, and the only one every filing since roughly 2022 needs.
    * For the cumulative column alone, the document's financial year. The 2018-2022 `…_WEB.xml`
      generation declares no per-column periods; it states the financial year once in its header,
      and that is the cumulative column's period. Restricted to `Four` because that is the only
      column whose period the financial year *is* — a discrete quarter's start is stated nowhere in
      those documents, and a prior-year cumulative column would need a year this fact does not
      describe.

    A column matching neither is skipped, which is what keeps the choice honest: these documents
    routinely carry two undimensioned columns with the full concept set, and the one that reports
    nothing about its period gets no period invented for it.
    """
    start = _one(facts, _PERIOD_START_ELEMENT, context_id=context_id, filename=filename)
    end = _one(facts, _PERIOD_END_ELEMENT, context_id=context_id, filename=filename)
    if start is not None and end is not None:
        return start, end
    if token == _CUMULATIVE_TOKEN and financial_year is not None:
        return financial_year
    return None


def _select_column(
    columns: tuple[_Column, ...], *, entry: FilingIndexEntry, filename: str
) -> _Column:
    """The one column whose declared period is exactly the period the index entry names.

    Both ends must match. `period_end` alone is not enough and is the subtle way this goes wrong: a
    Q3 filing's cumulative column ends on the same day as its quarter column, so an end-only match
    admits a nine-month revenue as a quarter's. No match and an ambiguous match are both hard
    failures — the alternative is storing a number for a period nobody asked for.
    """
    matches = [
        column
        for column in columns
        if column.period_start == entry.period_start and column.period_end == entry.period_end
    ]
    if len(matches) == 1:
        return matches[0]
    offered = ", ".join(
        f"{column.context_id}={column.period_start.isoformat()}→{column.period_end.isoformat()}"
        f"/{column.nature.value}"
        for column in columns
    )
    wanted = (
        f"{entry.period_start.isoformat() if entry.period_start else '?'}"
        f"→{entry.period_end.isoformat()}"
    )
    if not matches:
        raise ParseError(
            f"no results column covers {wanted} as the index entry ({entry.seq_number}) says; the "
            f"document offers {offered}",
            filename=filename,
        )
    raise ParseError(
        f"{len(matches)} results columns claim {wanted} ("
        f"{', '.join(column.context_id for column in matches)}); the period does not identify one "
        f"column, so which numbers the entry means is undecidable. Document offers {offered}",
        filename=filename,
    )


def _nature(facts: dict[str, list[str]], *, context_id: str, filename: str) -> Nature | None:
    """`Standalone` / `Consolidated` from a column's nature fact; None when it states none."""
    raw = _one(facts, _NATURE_ELEMENT, context_id=context_id, filename=filename)
    if raw is None:
        return None
    for nature in Nature:
        if raw.lower() == nature.value.lower():
            return nature
    raise ParseError(
        f"context {context_id!r} {_NATURE_ELEMENT} is {raw!r}, expected one of "
        f"{[nature.value for nature in Nature]}",
        filename=filename,
    )


def _audited(facts: dict[str, list[str]], *, context_id: str, filename: str) -> bool | None:
    """True/False from a column's audited fact; None when it did not state it.

    Per column, not per document: a December-year-end company files one document whose annual column
    is audited and whose fourth-quarter column is not.
    """
    raw = _one(facts, _AUDITED_ELEMENT, context_id=context_id, filename=filename)
    if raw is None:
        return None
    value = raw.lower().replace("-", "")
    if value == "audited":
        return True
    if value == "unaudited":
        return False
    return None


# ── facts ──────────────────────────────────────────────────────────────────────────────────────


def _facts(
    *,
    taxonomy: Taxonomy,
    column: _Column,
    shapes: dict[str, _ContextShape],
    facts_by_context: dict[str, dict[str, list[str]]],
    entry: FilingIndexEntry,
    source: str,
    l0_key: str | None,
    filename: str,
) -> tuple[FundamentalFact, ...]:
    """The selected column's whitelisted company facts, plus its per-segment revenue.

    Everything comes from the *selected* column: company concepts from its anchor context, segment
    revenue from the dimensioned contexts sharing its column token. The other column's contexts are
    not filtered out after the fact — they are never visited, which is why a cumulative figure
    cannot leak in as a quarter's.

    A column that reports none of the whitelisted concepts is a `ParseError`: it means the taxonomy
    was resolved wrongly, or this is not a results filing, and either way an empty `Filing` would
    record "we looked and there was nothing" as a successful ingest.
    """
    concepts = concepts_for(taxonomy)
    company = facts_by_context.get(column.context_id, {})
    facts: list[FundamentalFact] = []

    for element, concept in concepts.items():
        raw = _one(company, element, context_id=column.context_id, filename=filename)
        if raw is None:
            continue
        facts.append(
            _fact(
                concept=concept,
                segment=None,
                raw=raw,
                column=column,
                entry=entry,
                source=source,
                l0_key=l0_key,
                filename=filename,
            )
        )
    if not facts:
        raise ParseError(
            f"column {column.context_id!r} reports none of the {taxonomy.value} concepts "
            f"({', '.join(sorted(concepts))}); either this is not a results filing or its taxonomy "
            "was resolved wrongly",
            filename=filename,
        )

    facts.extend(
        _segment_facts(
            column=column,
            shapes=shapes,
            facts_by_context=facts_by_context,
            entry=entry,
            source=source,
            l0_key=l0_key,
            filename=filename,
        )
    )
    facts.sort(key=lambda fact: (fact.concept, fact.segment or ""))
    return tuple(facts)


def _segment_facts(
    *,
    column: _Column,
    shapes: dict[str, _ContextShape],
    facts_by_context: dict[str, dict[str, list[str]]],
    entry: FilingIndexEntry,
    source: str,
    l0_key: str | None,
    filename: str,
) -> list[FundamentalFact]:
    """Per-segment revenue for the selected column — §5.3 BC1's inputs.

    A segment fact must satisfy two conditions, and both matter:

    * **Its context carries the reportable-segments axis.** The column's own anchor context reports
      `SegmentRevenue` too, but there it is the *cross-segment total* — for a captured GRASIM
      filing, 351,546.8 against a 347,928.5 `RevenueFromOperations` and a 3,618.3
      `InterSegmentRevenue`, i.e. the gross figure before elimination. Taking it as a segment datum
      would file the whole company's revenue under a segment named by whatever came to hand.
    * **Its context shares the column's ordinal token.** A multi-segment filing states each segment
      twice — once for the quarter, once for the cumulative period — and their `xbrli:period`s are
      identical, so the token is the only thing that separates them.

    A filing may disclose no segments (a single-segment company), which is not an error. Two
    contexts naming the same segment in one column *is*: it would silently halve or double a
    segment's revenue depending on which won.
    """
    facts: list[FundamentalFact] = []
    seen: set[str] = set()
    for context_id, context_facts in facts_by_context.items():
        shape = shapes.get(context_id)
        if shape is None or shape.token != column.token or _SEGMENT_AXIS not in shape.axes:
            continue
        raw = _one(
            context_facts, _SEGMENT_REVENUE_ELEMENT, context_id=context_id, filename=filename
        )
        if raw is None:
            continue
        name = _one(context_facts, _SEGMENT_NAME_ELEMENT, context_id=context_id, filename=filename)
        if name is None:
            raise ParseError(
                f"context {context_id!r} reports {_SEGMENT_REVENUE_ELEMENT} on "
                f"{_SEGMENT_AXIS} with no {_SEGMENT_NAME_ELEMENT}; a segment figure without its "
                "segment cannot be compared across quarters",
                filename=filename,
            )
        if name in seen:
            raise ParseError(
                f"column {column.context_id!r} reports segment {name!r} more than once",
                filename=filename,
            )
        seen.add(name)
        facts.append(
            _fact(
                concept=SEGMENT_CONCEPT,
                segment=name,
                raw=raw,
                column=column,
                entry=entry,
                source=source,
                l0_key=l0_key,
                filename=filename,
            )
        )
    return facts


def _fact(
    *,
    concept: str,
    segment: str | None,
    raw: str,
    column: _Column,
    entry: FilingIndexEntry,
    source: str,
    l0_key: str | None,
    filename: str,
) -> FundamentalFact:
    """One validated `FundamentalFact`, tagged with the column's period and the entry's identity."""
    value = _value(raw, concept=concept, segment=segment, filename=filename)
    try:
        return FundamentalFact(
            isin=entry.isin,
            period_start=column.period_start,
            period_end=column.period_end,
            filing_date=entry.filing_date,
            nature=column.nature,
            filing_id=entry.filing_id,
            concept=concept,
            segment=segment,
            value=value,
            source=source,
            l0_key=l0_key,
        )
    except ValueError as exc:
        raise ParseError(f"{concept}/{segment}: {exc}", filename=filename) from exc


def _value(text: str, *, concept: str, segment: str | None, filename: str) -> Decimal:
    """A fact's reported value as an exact `Decimal`, refusing anything that is not one."""
    literal = text.strip()
    if not literal:
        raise ParseError(f"{concept}/{segment} has no value", filename=filename)
    if not _DECIMAL_LITERAL.match(literal):
        raise ParseError(
            f"{concept}/{segment} value {literal!r} is not a plain decimal", filename=filename
        )
    try:
        return Decimal(literal)
    except InvalidOperation as exc:  # pragma: no cover - the regex already excludes this
        raise ParseError(
            f"{concept}/{segment} value {literal!r}: {exc}", filename=filename
        ) from exc


def _iso_date(text: str, *, what: str, filename: str) -> date:
    """`2026-06-30` → `date(2026, 6, 30)`, refusing anything that is not an ISO date."""
    if not _ISO_DATE.match(text):
        raise ParseError(f"{what} {text!r} is not an ISO date (YYYY-MM-DD)", filename=filename)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ParseError(f"{what} {text!r} is not a real date", filename=filename) from exc
