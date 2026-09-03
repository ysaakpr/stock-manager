"""Discover results filings from the NSE announcements index (M7.3, §4.1 point-in-time row).

The point-in-time fundamentals path has two halves. This is the first: the
`corporates-financial-results` index, a JSON feed listing every results filing with the one thing
the XBRL document cannot authoritatively carry — the exchange dissemination timestamp, which is the
*first date the market could have known* the numbers (invariant #7). Each entry also carries the
absolute URL of the filing's XBRL document, which is how the second half (`parser`) is reached: the
register is explicit that these URLs are never guessed, only taken from the index (`pit_notes`).

Why the filing date lives here and not in the parser: a quarter that ended 30-Jun is filed weeks
later, and the *knowable* date is when the exchange broadcast it, not the board-meeting date or the
period end. The feed states it as `broadCastDate` (with `exchdisstime` as a fallback); the parser is
handed that date and never derives one from the document, so `(period_end, filing_date)` come from
two genuinely independent places (acceptance 1).

Three fields of an entry are not merely descriptive — the parser cannot read the document without
them, because one XBRL document holds several periods and several companies' worth of ambiguity:

* **`isin` is the join key.** Real filings identify the entity by NSE *symbol* and carry no usable
  ISIN of their own, so the ISIN a fact is stored under comes from here (invariant #2, via D2) and
  `symbol` is what the parser cross-checks against the document.
* **`(period_start, period_end)` selects the results column.** A document transcribes the published
  results table column by column — a quarter *and* a cumulative period, both with the same
  `xbrli:period` — and one document is linked by more than one entry (a December-year-end company's
  is linked by both its Quarterly and its Annual entry). The entry's `fromDate`/`toDate` is the
  only thing that says which column it means.
* **`nature` is cross-checked, not merely carried.** The feed and the document spell the same
  distinction differently (`Non-Consolidated` here, `Standalone` there — `_nature` maps them), and
  the selected column's own declaration must agree with this entry or the two are describing
  different filings.

Offline by construction: this module takes bytes (or an `L0Ref` read back through `L0Store`) and
never fetches. Money is not in scope here — the index carries dates and URLs, not values.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dataplatform.ingest.models import ISIN_PATTERN, ParseError
from dataplatform.ingest.xbrl.models import Nature
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "SOURCE_ID",
    "FilingIndexEntry",
    "parse_index",
    "parse_index_l0",
]

_LOG = get_logger(__name__)

#: The register id the discovery half serves (`source_register.yaml`, parser M7.3).
SOURCE_ID: Final = "nse_financial_results_index"

#: Feed keys, named once so a source rename is one edit. `filing_date` prefers `broadCastDate` and
#: falls back to `exchdisstime`; both are named in the register's `pit_notes` as first-knowable.
_KEY_ISIN: Final = "isin"
_KEY_SYMBOL: Final = "symbol"
_KEY_NAME: Final = "companyName"
_KEY_FILING: Final = ("broadCastDate", "exchdisstime", "filingDate")
_KEY_FROM: Final = "fromDate"
_KEY_TO: Final = "toDate"
_KEY_CONSOLIDATED: Final = "consolidated"
_KEY_AUDITED: Final = "audited"
_KEY_PERIOD: Final = "period"
_KEY_XBRL: Final = "xbrl"
_KEY_SEQ: Final = "seqNumber"

#: `30-Jul-2026`, optionally trailed by a clock the broadcast timestamp carries. Spelled out rather
#: than handed to `strptime("%b")`, which reads `LC_TIME`: a non-English host would otherwise fail
#: to parse a date that is not locale-dependent (the guard the NSE parsers share).
_FEED_DATE = re.compile(r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\s*$")
#: How the feed's `consolidated` field spells each `Nature`, compared after `_squash`. The feed's
#: `Non-Consolidated` is the document's `Standalone`; both spellings map to one stored value.
_NATURE_SYNONYMS: Final[Mapping[str, Nature]] = {
    "nonconsolidated": Nature.STANDALONE,
    "standalone": Nature.STANDALONE,
    "consolidated": Nature.CONSOLIDATED,
}

#: A `xbrl` field with no document behind it. The feed states the URL of the filings archive with a
#: bare `-` where a record has no XBRL attachment, which is a real, common state (18 of a captured
#: 3,816 records) rather than a malformed record — so the entry parses and reports itself
#: non-actionable instead of failing the whole index response.
_MISSING_XBRL_SUFFIX: Final = "/-"

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


class FilingIndexEntry(BaseModel):
    """One results-filing announcement — the coordinates needed to fetch and tag its XBRL.

    What it does: carry the filing's ISIN, the reporting period, the first-knowable `filing_date`,
    the declared nature, and the absolute `xbrl_url` of the document, plus a `seq_number` that keys
    one filing apart from a later restatement of the same period.
    What it assumes: the feed states the filing date and the XBRL URL; an entry missing either is
    not actionable and is rejected by the parser rather than yielded half-formed.
    What it never does: derive the filing date from the period, or construct the XBRL URL by
    guessing (the register forbids it) — the URL is taken verbatim from the feed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN, description="ISO 6166 identifier (invariant #2)")
    symbol: str = Field(min_length=1, description="exchange ticker, for logs and display only")
    name: str = Field(min_length=1, description="company name as filed")
    period_start: date | None = Field(default=None, description="reporting period start (fromDate)")
    period_end: date = Field(description="reporting period end (toDate) — never the knowable date")
    filing_date: date = Field(description="exchange dissemination date — the first-knowable date")
    nature: Nature = Field(description="Standalone/Consolidated as the feed declares it")
    audited: bool | None = Field(default=None, description="whether the results are audited")
    period: str | None = Field(default=None, description="Quarterly/Annual, as stated by the feed")
    xbrl_url: str | None = Field(
        default=None,
        description="absolute URL of the filing's XBRL document; None when the feed has none",
    )
    seq_number: str = Field(min_length=1, description="feed sequence id; keys restatements apart")

    @property
    def filing_id(self) -> str:
        """The stable id the parser and store use to keep restatements distinct."""
        return self.seq_number

    @property
    def is_actionable(self) -> bool:
        """Whether this entry can actually be ingested — i.e. it names an XBRL document.

        Announcements with no XBRL attachment are real and routine, and the register forbids
        constructing the URL by guessing. An ingest runner asks this and counts what it skipped,
        rather than the index parser dropping such records silently (CLAUDE.md: fail loud).
        """
        return self.xbrl_url is not None


def parse_index(payload: bytes, *, filename: str) -> tuple[FilingIndexEntry, ...]:
    """Parse one `corporates-financial-results` response into filing-index entries.

    Assumes `payload` is one whole JSON response — an array of per-filing records, optionally
    wrapped in `{"data": [...]}`. Raises `ParseError`, naming the file, for an HTML soft-404, a
    non-array body, or a record missing a required field / a bad date. Records are returned sorted
    by `(filing_date, isin, nature)` so a re-parse of the same payload is deterministic.
    """
    records = _records(_decode(payload, filename=filename), filename=filename)
    entries = tuple(
        sorted(
            (
                _entry(record, index=index, filename=filename)
                for index, record in enumerate(records)
            ),
            key=lambda entry: (entry.filing_date, entry.isin, entry.nature.value),
        )
    )
    _LOG.info(
        "xbrl_index.parsed",
        source=SOURCE_ID,
        filename=filename,
        entries=len(entries),
        state="VALIDATED",
    )
    return entries


def parse_index_l0(store: L0Store, ref: L0Ref) -> tuple[FilingIndexEntry, ...]:
    """Parse the L0 index payload a fetch produced, re-verifying its checksum on the way in."""
    return parse_index(store.get(ref), filename=ref.filename)


def _decode(payload: bytes, *, filename: str) -> str:
    """UTF-8 the body, refusing an empty one and an HTML page wearing a 200."""
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
    return text


def _records(text: str, *, filename: str) -> list[Mapping[str, Any]]:
    """The JSON array of filing records, unwrapping a `{"data": [...]}` envelope if present."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"body is not valid JSON: {exc}", filename=filename) from exc
    if isinstance(document, dict) and "data" in document:
        document = document["data"]
    if not isinstance(document, list):
        raise ParseError(
            f"expected a JSON array of filing records, got {type(document).__name__}",
            filename=filename,
        )
    if not document:
        raise ParseError("JSON array is empty; an index response lists filings", filename=filename)
    for index, record in enumerate(document):
        if not isinstance(record, dict):
            raise ParseError(
                f"record {index} is {type(record).__name__}, not an object", filename=filename
            )
    return list(document)


def _entry(record: Mapping[str, Any], *, index: int, filename: str) -> FilingIndexEntry:
    """One feed record → one validated `FilingIndexEntry`."""
    try:
        return FilingIndexEntry(
            isin=_text(record, _KEY_ISIN, index=index, filename=filename),
            symbol=_text(record, _KEY_SYMBOL, index=index, filename=filename),
            name=_text(record, _KEY_NAME, index=index, filename=filename),
            period_start=_optional_date(record, _KEY_FROM, index=index, filename=filename),
            period_end=_date(
                _text(record, _KEY_TO, index=index, filename=filename),
                key=_KEY_TO,
                index=index,
                filename=filename,
            ),
            filing_date=_date(
                _filing_field(record, index=index, filename=filename),
                key="/".join(_KEY_FILING),
                index=index,
                filename=filename,
            ),
            nature=_nature(record, index=index, filename=filename),
            audited=_optional_audited(record),
            period=_optional_text(record, _KEY_PERIOD),
            xbrl_url=_optional_xbrl_url(record, index=index, filename=filename),
            seq_number=_text(record, _KEY_SEQ, index=index, filename=filename),
        )
    except ValidationError as exc:
        raise ParseError(f"record {index}: {exc}", filename=filename) from exc


def _nature(record: Mapping[str, Any], *, index: int, filename: str) -> Nature:
    """`Standalone`/`Consolidated` from the feed's `consolidated` field.

    The feed and the XBRL document spell this distinction differently: the feed says
    `Non-Consolidated` where the document says `Standalone` (across a captured 3,816-record index
    the only two values are `Non-Consolidated` and `Consolidated` — `Standalone` never appears). The
    document's spelling is the stored one (`Nature`), so the feed's synonym is mapped here, and the
    document's own spelling is accepted too rather than being a surprise if the feed ever adopts it.
    """
    raw = _text(record, _KEY_CONSOLIDATED, index=index, filename=filename)
    nature = _NATURE_SYNONYMS.get(_squash(raw))
    if nature is not None:
        return nature
    raise ParseError(
        f"record {index}: {_KEY_CONSOLIDATED!r} is {raw!r}, expected one of "
        f"{sorted(_NATURE_SYNONYMS)}",
        filename=filename,
    )


def _filing_field(record: Mapping[str, Any], *, index: int, filename: str) -> str:
    """The filing timestamp, from `broadCastDate` / `exchdisstime` / `filingDate` in order."""
    for key in _KEY_FILING:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ParseError(
        f"record {index}: no filing timestamp; expected one of {', '.join(_KEY_FILING)}",
        filename=filename,
    )


def _optional_xbrl_url(record: Mapping[str, Any], *, index: int, filename: str) -> str | None:
    """The filing's XBRL URL, or None when the feed's placeholder says there is no document.

    Taken verbatim from the feed — never constructed (the register forbids guessing these). The
    field is always present as a string; a record with no attachment spells it as the archive path
    ending in a bare `-`, which is not a URL that can be fetched and so becomes None.
    """
    raw = _text(record, _KEY_XBRL, index=index, filename=filename)
    return None if raw.endswith(_MISSING_XBRL_SUFFIX) else raw


def _squash(value: str) -> str:
    """Fold a feed label to a comparable key: lowercase, no hyphens, no surrounding blanks.

    The feed and the XBRL document disagree on hyphens and case for the same words (`Un-Audited` vs
    `Unaudited`, `Non-Consolidated` vs `Standalone`), and comparing raw strings is how the first cut
    of this parser rejected every real record.
    """
    return value.strip().lower().replace("-", "").replace(" ", "")


def _text(record: Mapping[str, Any], key: str, *, index: int, filename: str) -> str:
    """A required, non-empty string field."""
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ParseError(
            f"record {index}: {key!r} is {value!r}, expected a non-empty string", filename=filename
        )
    return value.strip()


def _optional_text(record: Mapping[str, Any], key: str) -> str | None:
    """A string field a record may omit or leave blank — None, not empty string."""
    value = record.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_audited(record: Mapping[str, Any]) -> bool | None:
    """True/False from the audited field; None when absent or unrecognized.

    The feed hyphenates (`Un-Audited`) where the XBRL document does not (`Unaudited`), so the
    hyphen is squashed before comparing — matching on the un-squashed spelling read every one of a
    captured index's 3,598 unaudited entries as "did not say".
    """
    value = record.get(_KEY_AUDITED)
    if not isinstance(value, str):
        return None
    squashed = _squash(value)
    if squashed == "audited":
        return True
    if squashed == "unaudited":
        return False
    return None


def _optional_date(
    record: Mapping[str, Any], key: str, *, index: int, filename: str
) -> date | None:
    """A date field the feed may omit — None, parsed strictly when present."""
    value = record.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _date(
        _text(record, key, index=index, filename=filename), key=key, index=index, filename=filename
    )


def _date(value: str, *, key: str, index: int, filename: str) -> date:
    """`30-Jul-2026` (optionally with a clock) → `date(2026, 7, 30)`, locale-independently."""
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
