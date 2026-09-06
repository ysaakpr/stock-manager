"""NSE Integrated Filing (Financials) index → `FilingIndexEntry`s (the post-March-2025 feed).

From the quarter ended 31 March 2025, SEBI's Integrated Filing regime moved listed companies'
results off the `corporates-financial-results` feed (`discovery.py`) and onto
`api/integrated-filing-results`. The old feed kept serving late filings of old quarters by defunct
companies and nothing else — 28 entries for April-June 2025 against 3,865 for the quarter before —
which is how the PIT fundamentals store came to end in February 2025. This module reads the new
feed; the XBRL documents it points at are parsed by the same `parser.py`, whose vocabulary the new
`in-capmkt` taxonomy shares element-for-element for every concept the store keeps.

Three ways the new feed differs from the old, each handled here rather than papered over:

* **Paged, not chunked.** `?type=Integrated Filing- Financials&index=equities&from_date=..
  &to_date=..&page=N&size=M` returns `{"data": [...], "size": M, "page": N-1, "totalCount": T}`;
  `page` is 1-based in the request, 0-based in the response, and a page past the end returns an
  empty `data`. `build_integrated_units` plans a fixed number of pages per calendar month at a
  stated page size; an empty page is a normal, zero-entry unit here, not a parse failure.
* **No ISIN, no period start.** A record carries the symbol, the quarter-end (`qe_Date`, e.g.
  `31-MAR-2025`) and the dissemination time (`creation_Date`; `broadcast_Date` is null on
  revisions). The ISIN is resolved through the D2 identity master *as of the dissemination date*
  (`resolve_isin`, invariant #2) — never read from the document, though the parser cross-checks
  the document's own `ISIN` fact against it. The period start is the calendar quarter's first
  day, because `TypeOfReportingPeriod` is `Quarterly` for every record and the document's `OneD`
  column is that quarter; a fourth-quarter record additionally yields an **Annual** entry for the
  financial year (the document's `FourD` column), which is where the balance-sheet elements the
  M10.5 metrics need (reserves, equity) are stated.
* **Ids are its own.** `seq_Id` is prefixed `IF` so it can never collide with the old feed's
  `seqNumber` (the two ranges overlap); the Annual entry derived from a fourth-quarter record is
  `IF<seq_Id>-FY`. Both are the `filing_id` the store keys restatements apart by.

Records whose symbol the master cannot resolve on the dissemination date are counted and named,
not dropped silently; records whose `xbrl` is the archive's bare `-` placeholder yield an entry
with no URL, exactly as the old feed's do, so the runner counts them as "no document".
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final

from pydantic import ValidationError

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.xbrl.discovery import FilingIndexEntry
from dataplatform.ingest.xbrl.models import Nature
from dataplatform.logging import get_logger

__all__ = [
    "DEFAULT_PAGES_PER_MONTH",
    "DEFAULT_PAGE_SIZE",
    "SOURCE_ID",
    "IntegratedIndex",
    "IntegratedPage",
    "ResolveIsin",
    "build_integrated_pages",
    "parse_integrated_index",
    "quarter_start",
]

_LOG = get_logger(__name__)

#: The Source Register id of the feed (`source_register.yaml`).
SOURCE_ID: Final = "nse_integrated_filing_index"

#: The feed serves up to this many records per page (probed live: 1,000 works, the page reports
#: `totalCount` for the window). One month of results season is ~4,000-5,000 records.
DEFAULT_PAGE_SIZE: Final = 1000
#: Pages planned per calendar month at `DEFAULT_PAGE_SIZE`. Six covers 6,000 records — above the
#: heaviest month probed (May 2025: 3,476; the busiest results months run ~4,500). A page past the
#: end costs one request and yields zero entries; that is the price of a plan the resume path can
#: key without a first-page lookup.
DEFAULT_PAGES_PER_MONTH: Final = 6

_KEY_SYMBOL: Final = "symbol"
_KEY_NAME: Final = "cmName"
_KEY_CONSOLIDATED: Final = "consolidated"
_KEY_AUDITED: Final = "audited"
_KEY_QUARTER_END: Final = "qe_Date"
_KEY_FILING: Final = ("creation_Date", "broadcast_Date")
_KEY_SEQ: Final = "seq_Id"
_KEY_XBRL: Final = "xbrl"
_KEY_TYPE: Final = "type"
_EXPECTED_TYPE: Final = "Integrated Filing- Financials"
_MISSING_XBRL_SUFFIX: Final = "/-"
_ID_PREFIX: Final = "IF"
_ANNUAL_SUFFIX: Final = "-FY"

_FEED_DATE: Final = re.compile(
    r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\s*$"
)
_MONTHS: Final = {
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
_NATURES: Final = {
    "consolidated": Nature.CONSOLIDATED,
    "standalone": Nature.STANDALONE,
    "nonconsolidated": Nature.STANDALONE,
}

#: `(symbol, dissemination date) -> ISIN or None` — the D2 master's `try_resolve`, injected.
ResolveIsin = Callable[[str, date], str | None]


@dataclass(frozen=True, slots=True)
class IntegratedPage:
    """One discovery fetch of the integrated feed: a calendar-month window and a page number."""

    from_date: date
    to_date: date
    page: int
    size: int
    url: str

    @property
    def filename(self) -> str:
        """The L0 filename — window and page, so no two pages of one month collide."""
        return (
            f"integrated-filing-results_{self.from_date:%Y%m%d}_{self.to_date:%Y%m%d}"
            f"_p{self.page:02d}.json"
        )

    @property
    def state_source(self) -> str:
        """`nse_integrated_filing_index/<window end>/p<page>` — 1:1 with the L0 payload."""
        return f"{SOURCE_ID}/{self.to_date.isoformat()}/p{self.page:02d}"

    @property
    def logical_date(self) -> date:
        return self.from_date

    @property
    def label(self) -> str:
        return (
            f"integrated index {self.from_date.isoformat()}..{self.to_date.isoformat()} "
            f"page {self.page}"
        )


@dataclass(frozen=True, slots=True)
class IntegratedIndex:
    """What one page parsed to: the entries, plus what could not become one and why."""

    entries: tuple[FilingIndexEntry, ...]
    unresolved: tuple[tuple[str, date], ...] = field(default_factory=tuple)
    total_count: int | None = None
    records: int = 0


def quarter_start(quarter_end: date) -> date:
    """The first day of the calendar quarter `quarter_end` closes (`2025-03-31` → `2025-01-01`)."""
    month = ((quarter_end.month - 1) // 3) * 3 + 1
    return date(quarter_end.year, month, 1)


def build_integrated_pages(
    from_date: date,
    to_date: date,
    *,
    register: SourceRegister,
    page_size: int = DEFAULT_PAGE_SIZE,
    pages_per_month: int = DEFAULT_PAGES_PER_MONTH,
) -> list[IntegratedPage]:
    """The discovery plan: every calendar month in the window, `pages_per_month` pages each.

    Pure and offline. The month windows are clipped to `[from_date, to_date]`; page numbers are the
    feed's 1-based ones. Raises `ValueError` for an inverted window or a non-positive size/pages.
    """
    if to_date < from_date:
        raise ValueError(f"to_date {to_date} is before from_date {from_date}")
    if page_size <= 0 or pages_per_month <= 0:
        raise ValueError("page_size and pages_per_month must be positive")
    template = _template(register)
    pages: list[IntegratedPage] = []
    cursor = date(from_date.year, from_date.month, 1)
    while cursor <= to_date:
        next_month = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        start = max(cursor, from_date)
        end = min(next_month - timedelta(days=1), to_date)
        for page in range(1, pages_per_month + 1):
            url = (
                template.replace("from_date={DD-MM-YYYY}", f"from_date={start:%d-%m-%Y}")
                .replace("to_date={DD-MM-YYYY}", f"to_date={end:%d-%m-%Y}")
                .replace("page={N}", f"page={page}")
                .replace("size={M}", f"size={page_size}")
            )
            if "{" in url:
                raise ValueError(f"integrated index template left a placeholder unfilled: {url!r}")
            pages.append(
                IntegratedPage(from_date=start, to_date=end, page=page, size=page_size, url=url)
            )
        cursor = next_month
    return pages


def parse_integrated_index(
    payload: bytes, *, filename: str, resolve_isin: ResolveIsin
) -> IntegratedIndex:
    """Parse one page of the integrated feed into entries, resolving each symbol through D2.

    Assumes `payload` is one whole JSON response (`{"data": [...], ...}`). An empty `data` is a
    page past the end and parses to zero entries. Raises `ParseError`, naming the file, for an
    HTML soft-404, a body that is not the feed's envelope, or a record missing a required field.
    A record whose symbol does not resolve on its dissemination date is listed in `unresolved`.
    """
    document = _document(payload, filename=filename)
    records = document.get("data")
    if not isinstance(records, list):
        raise ParseError("integrated index response has no 'data' array", filename=filename)
    total = document.get("totalCount")
    entries: list[FilingIndexEntry] = []
    unresolved: list[tuple[str, date]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ParseError(f"record {index} is not an object", filename=filename)
        kind = record.get(_KEY_TYPE)
        if kind is not None and kind != _EXPECTED_TYPE:
            raise ParseError(
                f"record {index}: type is {kind!r}, expected {_EXPECTED_TYPE!r}", filename=filename
            )
        symbol = _text(record, _KEY_SYMBOL, index=index, filename=filename)
        filing_date = _date(
            _filing_field(record, index=index, filename=filename),
            key="/".join(_KEY_FILING),
            index=index,
            filename=filename,
        )
        isin = resolve_isin(symbol, filing_date)
        if isin is None:
            unresolved.append((symbol, filing_date))
            continue
        quarter_end = _date(
            _text(record, _KEY_QUARTER_END, index=index, filename=filename),
            key=_KEY_QUARTER_END,
            index=index,
            filename=filename,
        )
        seq = _text(record, _KEY_SEQ, index=index, filename=filename)
        common: dict[str, Any] = {
            "isin": isin,
            "symbol": symbol,
            "name": _text(record, _KEY_NAME, index=index, filename=filename),
            "filing_date": filing_date,
            "nature": _nature(record, index=index, filename=filename),
            "audited": _optional_audited(record),
            "xbrl_url": _optional_xbrl_url(record, index=index, filename=filename),
        }
        try:
            entries.append(
                FilingIndexEntry(
                    period_start=quarter_start(quarter_end),
                    period_end=quarter_end,
                    period="Quarterly",
                    seq_number=f"{_ID_PREFIX}{seq}",
                    **common,
                )
            )
            if quarter_end.month == 3:
                # The fourth-quarter document also carries the financial year's column (`FourD`,
                # 1 April → 31 March), which is where the annual balance-sheet elements live.
                entries.append(
                    FilingIndexEntry(
                        period_start=date(quarter_end.year - 1, 4, 1),
                        period_end=quarter_end,
                        period="Annual",
                        seq_number=f"{_ID_PREFIX}{seq}{_ANNUAL_SUFFIX}",
                        **common,
                    )
                )
        except ValidationError as exc:
            raise ParseError(f"record {index}: {exc}", filename=filename) from exc
    ordered = tuple(
        sorted(entries, key=lambda e: (e.filing_date, e.isin, e.nature.value, e.period or ""))
    )
    _LOG.info(
        "xbrl_integrated_index.parsed",
        source=SOURCE_ID,
        filename=filename,
        records=len(records),
        entries=len(ordered),
        unresolved=len(unresolved),
        total_count=total,
        state="VALIDATED",
    )
    return IntegratedIndex(
        entries=ordered,
        unresolved=tuple(unresolved),
        total_count=int(total) if isinstance(total, int) else None,
        records=len(records),
    )


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────


def _template(register: SourceRegister) -> str:
    source = next((entry for entry in register.sources if entry.id == SOURCE_ID), None)
    if source is None:
        raise ValueError(f"no source {SOURCE_ID!r} in the Source Register")
    return source.url_template


def _document(payload: bytes, *, filename: str) -> Mapping[str, Any]:
    if not payload.strip():
        raise ParseError("empty response body", filename=filename)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(f"body is not UTF-8: {exc}", filename=filename) from exc
    if text.lstrip()[:1] == "<":
        raise ParseError(
            "body is markup, not JSON — an HTML error page answered with a 200", filename=filename
        )
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"body is not valid JSON: {exc}", filename=filename) from exc
    if not isinstance(document, dict):
        raise ParseError(
            f"expected the feed's object envelope, got {type(document).__name__}",
            filename=filename,
        )
    return document


def _text(record: Mapping[str, Any], key: str, *, index: int, filename: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ParseError(f"record {index}: missing or blank {key!r}", filename=filename)
    return value.strip()


def _filing_field(record: Mapping[str, Any], *, index: int, filename: str) -> str:
    """The dissemination timestamp: `creation_Date`, else `broadcast_Date` (null on revisions)."""
    for key in _KEY_FILING:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ParseError(
        f"record {index}: none of {_KEY_FILING} states the dissemination date", filename=filename
    )


def _date(value: str, *, key: str, index: int, filename: str) -> date:
    match = _FEED_DATE.match(value)
    if match is None:
        raise ParseError(f"record {index}: {key} {value!r} is not DD-Mon-YYYY", filename=filename)
    day, month_name, year = match.groups()
    month = _MONTHS.get(month_name.upper())
    if month is None:
        raise ParseError(
            f"record {index}: {key} {value!r} names no month we know", filename=filename
        )
    try:
        return date(int(year), month, int(day))
    except ValueError as exc:
        raise ParseError(
            f"record {index}: {key} {value!r} is not a real date", filename=filename
        ) from exc


def _nature(record: Mapping[str, Any], *, index: int, filename: str) -> Nature:
    raw = _text(record, _KEY_CONSOLIDATED, index=index, filename=filename)
    nature = _NATURES.get(re.sub(r"[^a-z]", "", raw.lower()))
    if nature is None:
        raise ParseError(
            f"record {index}: {_KEY_CONSOLIDATED!r} is {raw!r}, expected one of {sorted(_NATURES)}",
            filename=filename,
        )
    return nature


def _optional_audited(record: Mapping[str, Any]) -> bool | None:
    value = record.get(_KEY_AUDITED)
    if not isinstance(value, str):
        return None
    squashed = re.sub(r"[^a-z]", "", value.lower())
    if squashed == "audited":
        return True
    if squashed == "unaudited":
        return False
    return None


def _optional_xbrl_url(record: Mapping[str, Any], *, index: int, filename: str) -> str | None:
    raw = _text(record, _KEY_XBRL, index=index, filename=filename)
    return None if raw.endswith(_MISSING_XBRL_SUFFIX) else raw
