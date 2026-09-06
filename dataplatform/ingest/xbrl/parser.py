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
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final
from xml.etree import ElementTree as ET

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.xbrl.discovery import FilingIndexEntry
from dataplatform.ingest.xbrl.models import (
    SHAREHOLDERS_EQUITY,
    SHARES_OUTSTANDING,
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

#: The same identifier under BSE's namespace root. The scheme *name* still ends in `NSESymbol` and
#: the value still is one — filers using the BSE-published taxonomy sometimes emit this variant.
#: 362 filings in the captured decade use it, and refusing them was the parser being pedantic about
#: a URL prefix rather than about identity.
_BSE_NSE_SYMBOL_SCHEME: Final = "http://www.bseindia.com/bse-fin/NSESymbol"

#: Every scheme whose identifier is an NSE symbol and may therefore be compared to one.
#: SEBI's own scheme for the same identifier in an Integrated Filing document, and the same
#: identifier under BSE's namespace root in the NBFC variant (`INTEGRATED_FILING_NBFC_INDAS_*`,
#: met on the first live sample of the feed).
_SEBI_SYMBOL_SCHEME: Final = "http://www.sebi.gov.in/in-capmkt/Symbol"
_CAPMKT_BSE_SYMBOL_SCHEME: Final = "http://www.bseindia.com/in-capmkt/Symbol"

_SYMBOL_SCHEMES: Final = frozenset(
    {_NSE_SYMBOL_SCHEME, _BSE_NSE_SYMBOL_SCHEME, _SEBI_SYMBOL_SCHEME, _CAPMKT_BSE_SYMBOL_SCHEME}
)

#: An Integrated Filing document states the company's ISIN as a fact. It is *not* the join key
#: (that stays the index's, resolved through D2 — invariant #2), but a stated ISIN that names a
#: different company than the entry is the DTIL misattribution in a form the document itself can
#: refute, so it is cross-checked when present — **in Integrated Filing documents only**. The older
#: `in-bse-fin` banking documents carry stale ISINs (J&K Bank's states the pre-2020 one), which is
#: why the ISIN was never read from a document before and still is not from those.
_ISIN_ELEMENT: Final = "ISIN"
_INTEGRATED_ENTRY_POINT: Final = "in-capmkt-ent"
#: `INE` plus the four-character issuer code: the part of an Indian ISIN that names the company and
#: survives a split or consolidation (which changes the security suffix and the check digit).
_ISSUER_CODE_LENGTH: Final = 7

#: The other scheme in the wild: a BSE scrip code (e.g. `532209` for J&K Bank). Its identifier is
#: *not* a symbol and must never be compared to one, so a filing using it is cross-checked on its
#: `Symbol` fact instead. Recognised explicitly rather than by "anything that is not a symbol
#: scheme", so a genuinely unknown scheme is still a loud failure.
_BSE_SCRIP_SCHEME: Final = "http://www.bseindia.com/bse-fin/ScripCode"
#: The same scrip-code identifier under SEBI's Integrated Filing namespace root (used by the
#: `_NONINDAS_` documents and by some `_INDAS_` ones); like the BSE one it is not a symbol.
_CAPMKT_SCRIP_SCHEME: Final = "http://www.bseindia.com/in-capmkt/ScripCode"
_SCRIP_SCHEMES: Final = frozenset({_BSE_SCRIP_SCHEME, _CAPMKT_SCRIP_SCHEME})

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

#: What `ReportingQuarter` says, mapped to (months after the financial-year start, months long).
#: The 2018-2022 `…_WEB.xml` generation states no per-column reporting period; for a *sub-annual*
#: filing it names the period here instead, and with the financial year that is a complete
#: statement of what the column covers. `Yearly` is deliberately absent: on those documents the
#: quarter column carries a `Yearly` label from the header block while being zero-filled, and the
#: year's numbers sit in the cumulative column — so `Yearly` on a quarter column says nothing about
#: that column and must not be read as if it did (see `_declared_period`).
_REPORTING_QUARTER_ELEMENT: Final = "ReportingQuarter"
_SUB_ANNUAL_PERIODS: Final[dict[str, tuple[int, int]]] = {
    "first quarter": (0, 3),
    "second quarter": (3, 3),
    "third quarter": (6, 3),
    "fourth quarter": (9, 3),
    "half yearly": (0, 6),
}

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
    # SEBI's Integrated Filing (Financials) taxonomy, in force from the quarter ended March 2025.
    # One entry point serves the `INTEGRATED_FILING_INDAS_*` and `_NONINDAS_*` documents alike, and
    # its vocabulary is the Ind-AS one element-for-element for every concept the store keeps
    # (verified on captured filings: `tests/fixtures/xbrl/integrated/`). A bank's integrated
    # filing has not been met yet; if it names its revenue differently it fails the whitelist and
    # surfaces as a parse failure rather than an empty filing.
    ("in-capmkt-ent", Taxonomy.IND_AS),
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

    if _is_integrated(root):
        _check_stated_isin(facts_by_context, entry=entry, filename=filename)

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
      index states today's. `accepted` is therefore the set of symbols the ISIN could state *on
      the filing date* (from the D2 master, via the caller — in force, just retired, or not yet
      opened); the document must match one of them. Comparing against today's symbol alone
      rejects every renamed company — Sastasundar Ventures files as `SASTASUNDR` under an index
      entry that now says `HEALTHX`. Comparing against every symbol *ever* held is the opposite
      error: Dhunseri Ventures was DTIL until 2010, and a 2024 filing saying DTIL is a different
      company's (Dhunseri Tea & Industries), misattributed by the index.
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
    unknown = schemes - _SYMBOL_SCHEMES - _SCRIP_SCHEMES
    if unknown:
        raise ParseError(
            f"entity identifier scheme is {', '.join(sorted(schemes))!r}, expected one of "
            f"{sorted(_SYMBOL_SCHEMES | _SCRIP_SCHEMES)}; this parser reads the identifier "
            "as an "
            "NSE symbol or a BSE scrip code and takes the ISIN from the announcements index "
            "(invariant #2)",
            filename=filename,
        )

    # Whatever the document states about its own symbol, from either place it can state it. Some
    # `…_WEB.xml` filings declare no `<context>` at all (see `_shapes`) and so carry no
    # `<xbrli:entity>` either; their `Symbol` fact is then the only identity they state, and it is
    # enough. Only a filing that states *neither* is unidentifiable.
    stated: set[str] = set()
    if schemes <= _SYMBOL_SCHEMES:
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
            f"index says this filing is {entry.symbol!r} (ISIN {entry.isin}, symbols accepted on "
            f"{entry.filing_date.isoformat()}: {', '.join(sorted(accepted))}) but the document "
            f"says "
            f"{', '.join(sorted(stated))}; the announcements index and the XBRL must name the "
            "same company",
            filename=filename,
        )
    return sorted(matched)[0]


def _is_integrated(root: ET.Element) -> bool:
    """Whether the document is an Integrated Filing one (its schemaRef names `in-capmkt-ent`)."""
    for element in root.iter():
        if _local(element.tag) != "schemaRef":
            continue
        href = next(
            (value for key, value in element.attrib.items() if _local(key) == "href"), ""
        ).lower()
        return _INTEGRATED_ENTRY_POINT in href
    return False


def _check_stated_isin(
    facts_by_context: dict[str, dict[str, list[str]]], *, entry: FilingIndexEntry, filename: str
) -> None:
    """Refuse a document whose own `ISIN` fact names a different company than the entry.

    Called for Integrated Filing documents only. The comparison is on the **issuer code** — the
    first seven characters of an Indian ISIN (`INE672A` of `INE672A01026`) — not the whole ISIN.
    A stock split or consolidation gives a company a new ISIN under the same issuer code, and the
    first live campaign showed filers keep the old one in their XBRL template for a while (Tata
    Investment, Angel One, E2E within the first hundred filings); refusing those would drop real
    filings for a difference that identifies the same company. A different issuer code is a
    different company — the DTIL misattribution, refused. The index's ISIN stays the join key
    either way; a same-issuer mismatch is logged so the stale-template population stays visible.
    A malformed value is ignored: a filer's typo is not evidence about the company.
    """
    stated = {
        value.strip().upper()
        for facts in facts_by_context.values()
        for value in facts.get(_ISIN_ELEMENT, ())
        if value and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", value.strip().upper())
    }
    if not stated or entry.isin in stated:
        return
    issuer = entry.isin[:_ISSUER_CODE_LENGTH]
    foreign = sorted(isin for isin in stated if isin[:_ISSUER_CODE_LENGTH] != issuer)
    if foreign:
        raise ParseError(
            f"index says this filing is {entry.symbol!r} (ISIN {entry.isin}) but the document "
            f"states ISIN {', '.join(foreign)} — a different issuer; the announcements index and "
            "the XBRL must name the same company",
            filename=filename,
        )
    _LOG.info(
        "xbrl.stated_isin_differs_same_issuer",
        filename=filename,
        entry_isin=entry.isin,
        stated=sorted(stated),
        note="same issuer code: a split changed the ISIN and the template kept the old one",
    )


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
    if financial_year is None:
        return None
    if token == _CUMULATIVE_TOKEN:
        return financial_year

    # Not the cumulative column, so the financial year is not its period — but `ReportingQuarter`
    # may name which part of that year it is, and on the older documents that is the only statement
    # of the column's period there is. `Yearly` is excluded on purpose (see `_SUB_ANNUAL_PERIODS`).
    quarter = _one(facts, _REPORTING_QUARTER_ELEMENT, context_id=context_id, filename=filename)
    if quarter is None:
        return None
    span = _SUB_ANNUAL_PERIODS.get(quarter.strip().lower())
    if span is None:
        return None
    return _period_within(financial_year, span, context_id=context_id, filename=filename)


def _period_within(
    financial_year: tuple[str, str],
    span: tuple[int, int],
    *,
    context_id: str,
    filename: str,
) -> tuple[str, str] | None:
    """The `(start, end)` of a sub-annual span measured from the financial year's start.

    Derived arithmetic, so it is worth being clear about why it cannot mislabel a period: whatever
    this returns still has to *equal the index entry's own period exactly* for the column to be
    selected (`_select_column`). A wrong derivation therefore matches nothing and the filing fails
    loudly, exactly as it did before — it can never be stored under a period nobody asked for.
    """
    fy_start = _iso_date(
        financial_year[0], what=f"context {context_id!r} financial-year start", filename=filename
    )
    offset, length = span
    start = _add_months(fy_start, offset)
    end = _add_months(fy_start, offset + length) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _add_months(day: date, months: int) -> date:
    """`day` shifted by whole months, keeping the day-of-month.

    Financial years start on the 1st, so no clamping is needed for the spans this is used with.
    """
    total = day.month - 1 + months
    return day.replace(year=day.year + total // 12, month=total % 12 + 1)


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
                taxonomy=taxonomy,
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
            taxonomy=taxonomy,
            column=column,
            shapes=shapes,
            facts_by_context=facts_by_context,
            entry=entry,
            source=source,
            l0_key=l0_key,
            filename=filename,
        )
    )
    facts.extend(
        _derived_facts(
            facts,
            taxonomy=taxonomy,
            column=column,
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
    taxonomy: Taxonomy,
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
    ambiguous: set[str] = set()
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
            # Two contexts in one column carrying the same segment name: the filing states two
            # different revenues for one segment and nothing distinguishes them, so neither is
            # usable. Drop that segment rather than failing the filing — its company-level P&L is
            # unaffected and is most of the value, and losing eight good facts because a filer
            # typed a segment name twice is the wrong trade. Loud, counted, never silent.
            _LOG.warning(
                "xbrl.segment_ambiguous",
                filename=filename,
                column=column.context_id,
                segment=name,
                state="DROPPED",
            )
            ambiguous.add(name)
            continue
        seen.add(name)
        facts.append(
            _fact(
                concept=SEGMENT_CONCEPT,
                segment=name,
                raw=raw,
                taxonomy=taxonomy,
                column=column,
                entry=entry,
                source=source,
                l0_key=l0_key,
                filename=filename,
            )
        )
    # A name that turned out to be ambiguous invalidates the fact already collected for it too.
    return [f for f in facts if f.segment not in ambiguous]


#: The widest the two independent share counts may differ before neither is trusted. Three, chosen
#: from the measured distribution rather than picked: over 10,468 (document, column) pairs stating
#: both capital elements, 86.6% agree within 5%, 95.6% fall inside this band, and **every** case
#: outside it is out by a clean power of ten — the smallest real error is 10x, so the band has a
#: 3.3x margin below the nearest thing it must catch.
#:
#: It has to be this loose because the two counts are honestly different measurements. EPS is struck
#: over *weighted-average* shares where paid-up capital is the period-end count, so any mid-period
#: issue, bonus or buyback separates them; an EPS rounded to two decimals is worth ±10% to a company
#: earning ₹0.05 a share; and a consolidated group's bottom line belongs partly to minority holders
#: (see `profit_attributable_to_owners`, which is why that element is preferred below).
_SHARE_COUNT_TOLERANCE: Final = Decimal("3")


def _derived_facts(
    stated: list[FundamentalFact],
    *,
    taxonomy: Taxonomy,
    column: _Column,
    entry: FilingIndexEntry,
    source: str,
    l0_key: str | None,
    filename: str,
) -> list[FundamentalFact]:
    """Share count and shareholders' equity, computed from the stated capital elements.

    These two are the reason a results filing can support a market cap, a P/B and an ROE at all: the
    XBRL states no share count and no equity line, but it states the elements they follow from.

    * `shares_outstanding` = paid-up equity capital / face value per share.
    * `shareholders_equity_excl_revaluation` = paid-up equity capital + reserves excluding
      revaluation. Named for the exclusion because a vendor's "reserves" figure includes any
      revaluation surplus and the two therefore legitimately differ for an asset-heavy company; a
      reader must be able to tell which basis they hold without going back to the filing.

    What it assumes: nothing it does not check. Every input must be stated and usable, and a filing
    missing one yields no derived fact rather than a guess — the same way the whitelist behaves.

    Two checks, both of which fire on real filings in this corpus:

    * **A share count must be corroborated by the filing's own EPS.** `profit / eps_basic` is a
      second, independent count built from concepts already whitelisted, so the filing audits itself
      at no cost — and **~4% of filings fail that audit by a clean power of ten** (KIOCL states a
      paid-up capital 1e6 too large, which would read a ₹600 crore company as ₹62 lakh crore).
      `profit_attributable_to_owners` is preferred as the numerator wherever stated, since that is
      the figure EPS is struck on; using the group bottom line instead falsely refuses every holding
      company, GRASIM among them, whose minorities own half the consolidated profit.

      A filing that fails yields *neither* derived fact, not merely no share count: the suspect
      input is the paid-up capital, which is also a term of the equity sum, and nothing inside the
      document says whether the fault lies there or in the face value. Refusing a possibly-good book
      value is the recoverable error; publishing one wrong by six orders of magnitude is not.

    * **A reserves figure of exactly zero is an unfilled field, not a zero.** 29% of the filings
      that state the element state it as 0.00 — including ones from companies with thousands of
      crores of reserves — because the taxonomy requires the tag and a filer who has not computed it
      enters nothing. Taken at face value it makes equity equal paid-up capital alone, which for
      Schaeffler India would put book value ~100x too low and hand a screen a spectacular fake P/B.
      A *negative* reserve is kept: accumulated losses are real, and so is the negative equity they
      can produce.

    Every refusal is logged with both counts, so the rate is measurable rather than invisible.
    """
    by_concept = {fact.concept: fact.value for fact in stated if fact.segment is None}
    paid_up = by_concept.get("paid_up_equity_capital")
    face_value = by_concept.get("face_value_per_share")
    if paid_up is None or face_value is None or paid_up <= 0 or face_value <= 0:
        return []
    # Rounded to a whole share, because a share count is a count. The division is rarely exact —
    # SPICEMOBI's ₹60.52 crore of ₹3 paid-up equity gives 201,749,666.67 — but the residual is the
    # filing's own rounding (paid-up capital is stated to the nearest thousand rupees), not a
    # fraction of a share that exists. Keeping it would also make the value's scale unbounded, since
    # a repeating decimal has no natural precision to store.
    shares = (paid_up / face_value).quantize(Decimal(1), rounding=ROUND_HALF_UP)

    # The parent's share where the filing states it, the bottom line otherwise. A zero is treated as
    # unstated on both: a standalone filing sometimes tags the attributable element with 0.
    profit = by_concept.get("profit_attributable_to_owners") or by_concept.get("profit_after_tax")
    eps = by_concept.get("eps_basic")
    # A negative implied count means profit and EPS disagree in sign, which is not a share count at
    # all — no corroboration rather than a magnitude to compare against.
    implied = profit / eps if profit and eps else None
    if implied is None or implied <= 0:
        reason = "no usable profit / eps_basic to corroborate against"
    elif not 1 / _SHARE_COUNT_TOLERANCE <= shares / implied <= _SHARE_COUNT_TOLERANCE:
        reason = f"paid_up/face_value is {shares / implied:.3g}x profit/eps_basic"
    else:
        reason = ""
    if reason:
        _LOG.warning(
            "xbrl.derivation_refused",
            filename=filename,
            isin=entry.isin,
            column=column.context_id,
            period_end=column.period_end.isoformat(),
            shares_from_capital=str(shares),
            shares_from_eps=str(implied) if implied is not None else None,
            reason=reason,
            state="REFUSED",
        )
        return []

    facts = [
        _fact(
            concept=SHARES_OUTSTANDING,
            segment=None,
            raw=str(shares),
            taxonomy=taxonomy,
            column=column,
            entry=entry,
            source=source,
            l0_key=l0_key,
            filename=filename,
            derived=True,
        )
    ]
    reserves = by_concept.get("reserves_excl_revaluation")
    if reserves:
        facts.append(
            _fact(
                concept=SHAREHOLDERS_EQUITY,
                segment=None,
                raw=str(paid_up + reserves),
                taxonomy=taxonomy,
                column=column,
                entry=entry,
                source=source,
                l0_key=l0_key,
                filename=filename,
                derived=True,
            )
        )
    return facts


def _fact(
    *,
    concept: str,
    segment: str | None,
    raw: str,
    taxonomy: Taxonomy,
    column: _Column,
    entry: FilingIndexEntry,
    source: str,
    l0_key: str | None,
    filename: str,
    derived: bool = False,
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
            taxonomy=taxonomy,
            filing_id=entry.filing_id,
            concept=concept,
            segment=segment,
            value=value,
            derived=derived,
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
