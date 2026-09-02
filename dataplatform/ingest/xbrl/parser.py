"""Parse one NSE/BSE results filing (XBRL) into point-in-time `FundamentalFact`s (M7.3).

NSE serves each results filing as an XBRL document against the BSE-published `in-bse-fin`
taxonomy (§4.1; the register's `parse_check` records "root xbrli:xbrl with the in-bse-fin
2020-03-31 taxonomy namespaces"). XBRL states a *fact* — a number — against a *context* that fixes
whose number it is (the entity), what period it covers, and, for a segment breakdown, which segment.
This parser reads exactly that: it walks the contexts once, then attaches each whitelisted fact to
the period and segment its context declares.

Three parsing decisions carry the acceptance criteria:

* **The period comes from the context, the filing date does not.** The XBRL knows the period it
  reports (its `endDate`), but the *first-knowable* date is the exchange dissemination timestamp,
  which lives in the announcements index that pointed here — never invented from the document. So
  `filing_date` is injected by the caller (from `discovery.FilingIndexEntry`), and `period_end` is
  read from the document; the two come from two independent places, which is what makes them
  genuinely independent rather than one dressed as the other (acceptance 1, invariant #7).
* **Standalone vs consolidated is the document's `NatureOfReportStandaloneConsolidated`.** Each
  filing declares one nature, and it becomes part of every fact's identity, so a later query for the
  consolidated revenue cannot accidentally read the standalone number (acceptance 2).
* **A segment fact is a `SegmentRevenue` whose context carries a segment dimension.** The segment
  name is read from a typed dimension member (`<in-bse-fin:SegmentName>`), giving a stable,
  human-readable key the segment-decline break condition (§5.3 BC1, built in M7.4) compares across
  quarters (acceptance 2).

Matching is by element *local-name* within the xbrli / xbrldi / in-bse-fin families rather than a
pinned namespace URI, so a future taxonomy revision (a new date in the namespace) does not silently
stop the parser from finding facts it should — a format change must fail loudly at the schema, not
by returning nothing.

Money is `Decimal` throughout: a value is read as text and converted exactly, and `NaN`/`Infinity`
or anything that is not a plain decimal literal is a `ParseError`, never a number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final
from xml.etree import ElementTree as ET

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.xbrl.models import CONCEPTS, Filing, FundamentalFact, Nature
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
#: context dimension, so it is not in `CONCEPTS` (which maps element name → key one-to-one).
SEGMENT_CONCEPT: Final = "segment_revenue"

#: The in-bse-fin element whose value is `Standalone` / `Consolidated` for the whole filing.
_NATURE_ELEMENT: Final = "NatureOfReportStandaloneConsolidated"
#: in-bse-fin elements that populate filing-level fields rather than becoming facts.
_NAME_ELEMENT: Final = "NameOfTheCompany"
_AUDITED_ELEMENT: Final = "WhetherResultsAreAuditedOrUnaudited"
_PERIOD_START_ELEMENT: Final = "DateOfStartOfReportingPeriod"
_PERIOD_END_ELEMENT: Final = "DateOfEndOfReportingPeriod"
#: The typed-dimension child element carrying a segment's name.
_SEGMENT_NAME_ELEMENT: Final = "SegmentName"
#: The per-segment revenue element.
_SEGMENT_REVENUE_ELEMENT: Final = "SegmentRevenue"

#: A plain decimal literal, optionally signed. Checked before `Decimal()` sees the text because
#: `Decimal` itself accepts `NaN`/`Infinity`, and a mis-framed field spelling one of those must not
#: become a value that compares greater than everything.
_DECIMAL_LITERAL = re.compile(r"^[+-]?\d+(\.\d+)?$")
#: An ISO date as XBRL states it (`2026-06-30`).
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class _Context:
    """One resolved XBRL context: whose fact, what period, which segment (if any)."""

    isin: str
    period_start: date | None
    period_end: date
    segment: str | None


def parse(
    payload: bytes,
    *,
    filing_date: date,
    filing_id: str,
    source: str = SOURCE_ID,
    l0_key: str | None = None,
    filename: str,
    isin: str | None = None,
) -> Filing:
    """Parse one XBRL results filing into a `Filing` of point-in-time facts.

    `filing_date` is the first-knowable date, supplied by the caller from the announcements index
    (never read from the document — the document does not authoritatively carry it). `filing_id`
    distinguishes this filing from a later restatement of the same period; pass the index seq number
    or the XBRL filename stem. `isin`, when given, is cross-checked against the entity identifier in
    the document and a mismatch is a `ParseError` — the index and the document must agree on whose
    filing this is.

    Raises `ParseError`, naming the file, for anything that is not this format: a body that is not
    well-formed XML, a root that is not `xbrl`, a missing nature or period, a value that is not a
    plain decimal, a fact referencing an undeclared context, or a filing with no company facts.
    Never returns a partial or repaired filing.
    """
    root = _root(payload, filename=filename)
    contexts = _contexts(root, filename=filename)
    entity_isin = _entity_isin(contexts, filename=filename)
    if isin is not None and isin != entity_isin:
        raise ParseError(
            f"index says this filing is {isin} but the document's entity identifier is "
            f"{entity_isin}; the announcements index and the XBRL must name the same company",
            filename=filename,
        )

    nature = _nature(root, filename=filename)
    name = _text_element(root, _NAME_ELEMENT, filename=filename)
    audited = _audited(root)
    period_start, period_end = _reporting_period(root, filename=filename)

    facts = _facts(
        root,
        contexts=contexts,
        isin=entity_isin,
        period_start=period_start,
        period_end=period_end,
        filing_date=filing_date,
        nature=nature,
        filing_id=filing_id,
        source=source,
        l0_key=l0_key,
        filename=filename,
    )

    try:
        filing = Filing(
            isin=entity_isin,
            name=name,
            period_start=period_start,
            period_end=period_end,
            filing_date=filing_date,
            nature=nature,
            filing_id=filing_id,
            audited=audited,
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


def _contexts(root: ET.Element, *, filename: str) -> dict[str, _Context]:
    """Every `<context>` in the document, resolved to entity/period/segment, keyed by its id."""
    contexts: dict[str, _Context] = {}
    for element in root.iter():
        if _local(element.tag) != "context":
            continue
        context_id = element.get("id")
        if not context_id:
            raise ParseError("a <context> has no id attribute", filename=filename)
        contexts[context_id] = _context(element, context_id=context_id, filename=filename)
    if not contexts:
        raise ParseError("no <context> elements; not a results filing", filename=filename)
    return contexts


def _context(element: ET.Element, *, context_id: str, filename: str) -> _Context:
    """Resolve one `<context>` into its entity ISIN, period and optional segment."""
    identifier = _find_local(element, "identifier")
    if identifier is None or not (identifier.text and identifier.text.strip()):
        raise ParseError(f"context {context_id!r} has no entity identifier", filename=filename)
    isin = identifier.text.strip()

    period = _find_local(element, "period")
    if period is None:
        raise ParseError(f"context {context_id!r} has no <period>", filename=filename)
    # `or` is unsafe on ElementTree elements — an element with no children is falsy — so choose
    # explicitly with `is None` rather than letting a real, childless <endDate> read as absent.
    end_element = _find_local(period, "endDate")
    if end_element is None:
        end_element = _find_local(period, "instant")
    if end_element is None or not (end_element.text and end_element.text.strip()):
        raise ParseError(
            f"context {context_id!r} has no period end (endDate or instant)", filename=filename
        )
    period_end = _iso_date(
        end_element.text.strip(), what=f"context {context_id!r} period end", filename=filename
    )
    start_element = _find_local(period, "startDate")
    period_start = (
        _iso_date(
            start_element.text.strip(),
            what=f"context {context_id!r} period start",
            filename=filename,
        )
        if start_element is not None and start_element.text and start_element.text.strip()
        else None
    )

    return _Context(
        isin=isin,
        period_start=period_start,
        period_end=period_end,
        segment=_segment_name(element, context_id=context_id, filename=filename),
    )


def _segment_name(element: ET.Element, *, context_id: str, filename: str) -> str | None:
    """The segment name a context declares via a dimension member, or None at company level.

    Prefers a typed member carrying `<in-bse-fin:SegmentName>` (a readable, stable key); falls back
    to an explicit member, deriving the name from its QName local part. A context with no segment
    dimension is a company-level context.
    """
    segment = _find_local(element, "segment")
    if segment is None:
        return None
    typed = _find_local(segment, "typedMember")
    if typed is not None:
        name_element = _find_local(typed, _SEGMENT_NAME_ELEMENT)
        if name_element is not None and name_element.text and name_element.text.strip():
            return name_element.text.strip()
        raise ParseError(
            f"context {context_id!r} has a typed segment member with no <{_SEGMENT_NAME_ELEMENT}>",
            filename=filename,
        )
    explicit = _find_local(segment, "explicitMember")
    if explicit is not None and explicit.text and explicit.text.strip():
        member = _local(explicit.text.strip())
        return member.removesuffix("Member").removesuffix("Segment") or member
    raise ParseError(
        f"context {context_id!r} has a <segment> with no recognizable member", filename=filename
    )


def _find_local(element: ET.Element, local_name: str) -> ET.Element | None:
    """The first descendant with the given local-name, namespace-agnostically."""
    for child in element.iter():
        if child is not element and _local(child.tag) == local_name:
            return child
    return None


# ── filing-level fields ──────────────────────────────────────────────────────────────────────


def _entity_isin(contexts: dict[str, _Context], *, filename: str) -> str:
    """The single entity ISIN every context must share — a filing is about one company."""
    isins = {context.isin for context in contexts.values()}
    if len(isins) != 1:
        raise ParseError(
            f"filing names more than one entity identifier ({', '.join(sorted(isins))}); one "
            "results filing is about one company",
            filename=filename,
        )
    return next(iter(isins))


def _nature(root: ET.Element, *, filename: str) -> Nature:
    """`Standalone` / `Consolidated` from the document's nature element, case-tolerantly."""
    raw = _text_element(root, _NATURE_ELEMENT, filename=filename)
    for nature in Nature:
        if raw.strip().lower() == nature.value.lower():
            return nature
    raise ParseError(
        f"{_NATURE_ELEMENT} is {raw!r}, expected one of {[n.value for n in Nature]}",
        filename=filename,
    )


def _audited(root: ET.Element) -> bool | None:
    """True/False from the audited element; None when the filing did not state it."""
    element = _first_fact_element(root, _AUDITED_ELEMENT)
    if element is None or not (element.text and element.text.strip()):
        return None
    value = element.text.strip().lower()
    if value == "audited":
        return True
    if value == "unaudited":
        return False
    return None


def _reporting_period(root: ET.Element, *, filename: str) -> tuple[date | None, date]:
    """The filing's reporting period from its explicit start/end elements.

    These are the authoritative period the numbers are about (`period_end`), read from the
    document's own date-valued elements rather than guessed from any one context — a filing carries
    comparative-period contexts too, and only these elements say which period is *this* filing's.
    """
    end_text = _text_element(root, _PERIOD_END_ELEMENT, filename=filename)
    period_end = _iso_date(end_text, what=_PERIOD_END_ELEMENT, filename=filename)
    start_element = _first_fact_element(root, _PERIOD_START_ELEMENT)
    period_start = (
        _iso_date(start_element.text.strip(), what=_PERIOD_START_ELEMENT, filename=filename)
        if start_element is not None and start_element.text and start_element.text.strip()
        else None
    )
    return period_start, period_end


# ── facts ──────────────────────────────────────────────────────────────────────────────────────


def _facts(
    root: ET.Element,
    *,
    contexts: dict[str, _Context],
    isin: str,
    period_start: date | None,
    period_end: date,
    filing_date: date,
    nature: Nature,
    filing_id: str,
    source: str,
    l0_key: str | None,
    filename: str,
) -> tuple[FundamentalFact, ...]:
    """Every whitelisted company and segment fact whose context is this filing's reporting period.

    A fact is kept only when its context's period end matches the filing's `period_end`: that is how
    a comparative prior-year figure sharing the document is left out, so the store holds this
    filing's period and no other. A fact referencing a context the document never declared is a
    `ParseError`, not a dropped row.
    """
    facts: list[FundamentalFact] = []
    seen: set[tuple[str, str | None]] = set()
    for element in root.iter():
        local = _local(element.tag)
        context_ref = element.get("contextRef")
        if context_ref is None:
            continue
        is_segment = local == _SEGMENT_REVENUE_ELEMENT
        concept = SEGMENT_CONCEPT if is_segment else CONCEPTS.get(local)
        if concept is None:
            continue

        if context_ref not in contexts:
            raise ParseError(
                f"fact {local!r} references undeclared context {context_ref!r}", filename=filename
            )
        context = contexts[context_ref]
        if context.period_end != period_end:
            # A comparative-period figure (prior quarter/year) sharing the document — not this
            # filing's period. Left out on purpose so the store holds one period per filing.
            continue
        if is_segment and context.segment is None:
            raise ParseError(
                f"{_SEGMENT_REVENUE_ELEMENT} in context {context_ref!r} has no segment dimension",
                filename=filename,
            )
        if not is_segment and context.segment is not None:
            # A company-level concept reported under a segment context is malformed; skip rather
            # than mislabel it as company-wide.
            continue

        key = (concept, context.segment)
        if key in seen:
            raise ParseError(
                f"filing reports {local!r} for segment {context.segment!r} more than once",
                filename=filename,
            )
        seen.add(key)

        value = _value(element.text, concept=concept, segment=context.segment, filename=filename)
        try:
            facts.append(
                FundamentalFact(
                    isin=isin,
                    period_start=period_start,
                    period_end=period_end,
                    filing_date=filing_date,
                    nature=nature,
                    filing_id=filing_id,
                    concept=concept,
                    segment=context.segment,
                    value=value,
                    source=source,
                    l0_key=l0_key,
                )
            )
        except ValueError as exc:
            raise ParseError(f"{concept}/{context.segment}: {exc}", filename=filename) from exc

    facts.sort(key=lambda fact: (fact.concept, fact.segment or ""))
    return tuple(facts)


def _value(text: str | None, *, concept: str, segment: str | None, filename: str) -> Decimal:
    """A fact's reported value as an exact `Decimal`, refusing anything that is not one."""
    if text is None or not text.strip():
        raise ParseError(f"{concept}/{segment} has no value", filename=filename)
    literal = text.strip()
    if not _DECIMAL_LITERAL.match(literal):
        raise ParseError(
            f"{concept}/{segment} value {text.strip()!r} is not a plain decimal", filename=filename
        )
    try:
        return Decimal(literal)
    except InvalidOperation as exc:  # pragma: no cover - the regex already excludes this
        raise ParseError(
            f"{concept}/{segment} value {text.strip()!r}: {exc}", filename=filename
        ) from exc


def _text_element(root: ET.Element, local_name: str, *, filename: str) -> str:
    """A required, non-empty text fact by local-name."""
    element = _first_fact_element(root, local_name)
    if element is None or not (element.text and element.text.strip()):
        raise ParseError(f"no {local_name} element with a value", filename=filename)
    return element.text.strip()


def _first_fact_element(root: ET.Element, local_name: str) -> ET.Element | None:
    """The first element with the given local-name anywhere in the document."""
    for element in root.iter():
        if _local(element.tag) == local_name:
            return element
    return None


def _iso_date(text: str, *, what: str, filename: str) -> date:
    """`2026-06-30` → `date(2026, 6, 30)`, refusing anything that is not an ISO date."""
    if not _ISO_DATE.match(text):
        raise ParseError(f"{what} {text!r} is not an ISO date (YYYY-MM-DD)", filename=filename)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ParseError(f"{what} {text!r} is not a real date", filename=filename) from exc
