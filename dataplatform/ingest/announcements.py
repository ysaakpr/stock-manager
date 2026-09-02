"""Corporate announcements, NSE + BSE (§4.1 row 13) → L1 `announcements`.

Every listed company files disclosures with the exchange throughout the day — board meetings,
results intimations, business divestments, auditor changes, pledge disclosures. §5.4's T0 tier
matches these against the keyword sets a ratified thesis defines (§5.3 BC2/BC3: "exit/divestment
of robotics business line", "auditor resignation / fraud investigation"), so an announcement that
lands here is not a footnote — it is a break-condition input. That shapes this module three ways:

* **The source timestamp is the fact, and it is preserved exactly.** Announcements are natural PIT
  (§4.1): the exchange dissemination time is the first instant the disclosure was knowable to us,
  and re-stamping a row with the ingest wall clock would back- or forward-date a fact the whole
  monitoring tier keys on (invariant #7). So `ts` comes only from the source's own dissemination
  field (NSE `exchdisstime`, BSE `DT_TM`) — never from a `Clock` — and is required and tz-aware. A
  row the source could not date is not point-in-time usable, and this parser refuses it rather than
  dating it from now.

* **Two exchange dialects converge on one row.** As the two bhavcopy eras converge on `PriceRow`
  and GDELT/RSS on `NewsRow`, NSE and BSE announcements both emit `AnnouncementRow`, so the T0
  matcher carries no "was this NSE or BSE?" branch. NSE keys natively on ISIN; BSE keys on a scrip
  code and must be resolved scrip→ISIN through the D2 identity master (invariant #2 — nothing joins
  on a raw exchange identifier), so `parse_bse` takes a `scrip_index` and a scrip it cannot resolve
  lands in `unresolved` rather than under a guessed ISIN.

* **Body and attachment are kept, deliberately.** Unlike the news row (headlines + links only, a
  license constraint), an exchange disclosure's substance is the announcement text and its
  attachment — the keyword match runs over subject + body, so dropping the body would blind BC2/BC3.
  There is no license note on this §4.1 row that forbids it.

Identity is ISIN (invariant #2); `symbol` is carried for display only and never joined on. Offline
by construction (B8): every parser takes bytes, or an `L0Ref` read back through `L0Store`; the crawl
engine (`dataplatform.ingest.fetcher`) is the only thing in the platform that opens a socket.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, Protocol

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dataplatform.clock import IST
from dataplatform.ingest.fetcher import (
    Fetcher,
    FetchHTTPError,
    ForbiddenSpikeError,
    RetryableFetchError,
)
from dataplatform.ingest.models import ISIN_PATTERN, IngestError, ParseError
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncRecord
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.paths import Layer, l1_partition_path, layer_root, partition_date_of

__all__ = [
    "ANNOUNCEMENTS_DATASET",
    "BSE_SOURCE_ID",
    "NSE_SOURCE_ID",
    "AnnParseResult",
    "AnnouncementBatch",
    "AnnouncementRow",
    "SyncTracker",
    "UnresolvedAnnouncement",
    "dedupe",
    "ingest_bse",
    "ingest_nse",
    "iter_l1",
    "parse_bse",
    "parse_bse_l0",
    "parse_nse",
    "parse_nse_l0",
    "read_l1",
    "write_l1",
]

_LOG = get_logger(__name__)

#: The register ids these parsers serve (`source_register.yaml`, `parser.task: M3.8`).
NSE_SOURCE_ID: Final = "nse_announcements"
BSE_SOURCE_ID: Final = "bse_announcements"

#: The L1 dataset name — `data/L1/announcements/date=YYYY-MM-DD/part.parquet` (§4.2). One partition
#: per ingest logical date; the rows inside carry their own source `ts`, which an intraday poll may
#: date earlier than the partition.
ANNOUNCEMENTS_DATASET: Final = "announcements"

#: Month abbreviations as the exchanges spell them (`02-Aug-2026`). Spelled out rather than handed
#: to `strptime("%b")`, which reads `LC_TIME`: a host with a non-English locale would otherwise fail
#: to parse a date that is not locale-dependent at all (as `fii_dii`/`shareholding` also guard).
_MONTHS: Final[Mapping[str, int]] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}  # fmt: skip

#: NSE payload keys (`source_register.yaml` parse_check). `exchdisstime` is the dissemination time
#: and the preferred `ts`; `an_dt` is the fallback. Both are the *source's* timestamps — neither is
#: the ingest clock, which never touches `ts` (invariant #7).
_NSE_ISIN: Final = "sm_isin"
_NSE_SYMBOL: Final = "symbol"
_NSE_SUBJECT: Final = "desc"
_NSE_BODY: Final = "attchmntText"
_NSE_ATTACH: Final = "attchmntFile"
_NSE_TS: Final = ("exchdisstime", "an_dt")
_NSE_SEQ: Final = "seq_id"

#: BSE payload keys. The records live under `Table`; `DT_TM` is the dissemination time (preferred
#: `ts`), `NEWS_DT` the fallback. BSE keys on `SCRIP_CD`, resolved scrip→ISIN via the D2 master.
_BSE_TABLE: Final = "Table"
_BSE_SCRIP: Final = "SCRIP_CD"
_BSE_SUBJECT: Final = "NEWSSUB"
_BSE_BODY: Final = "HEADLINE"
_BSE_CATEGORY: Final = "CATEGORYNAME"
_BSE_ATTACH: Final = "ATTACHMENTNAME"
_BSE_TS: Final = ("DT_TM", "NEWS_DT")
_BSE_NEWSID: Final = "NEWSID"


class AnnouncementRow(BaseModel):
    """One corporate announcement, as an exchange disseminated it — the canonical D1 output row.

    What it does: carry the point-in-time facts one disclosure states — when the exchange
    disseminated it (`ts`, the natural PIT), whose disclosure it is (`isin`), what it is about
    (`category`/`subject`) and its substance (`body`, `attachment_ref`), plus the exchange's own id
    for the disclosure (`source_ref`) so a re-poll can be de-duplicated against a prior one.
    What it assumes: the parser that built it already resolved the source timestamp and the ISIN —
    a row that exists is a disclosure the exchange really made, at a time it really stated.
    What it never does: hold a timestamp derived from the ingest clock (that would defeat the PIT
    the T0 monitor keys on), or join on `symbol` — `symbol` is display-only (invariant #2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime = Field(
        description="exchange dissemination timestamp, tz-aware — the disclosure's PIT"
    )
    source: str = Field(
        min_length=1, description="Source Register id: nse_announcements / bse_announcements"
    )
    isin: str = Field(
        pattern=ISIN_PATTERN, description="ISO 6166 identifier — the only join key (invariant #2)"
    )
    symbol: str | None = Field(
        default=None, description="exchange ticker as published; display only, never a join key"
    )
    category: str | None = Field(
        default=None, description="announcement category as the exchange classified it, if any"
    )
    subject: str = Field(
        min_length=1, description="the announcement subject/headline, as published"
    )
    body: str | None = Field(
        default=None, description="the announcement text; None when the disclosure is a header only"
    )
    attachment_ref: str | None = Field(
        default=None,
        description="link/name of the attached document, if the disclosure carries one",
    )
    source_ref: str | None = Field(
        default=None, description="the exchange's own id for this disclosure (seq_id/NEWSID)"
    )
    l0_key: str | None = Field(
        default=None, description="`source/date/filename` of the L0 payload this was derived from"
    )

    @field_validator("ts")
    @classmethod
    def _timestamp_is_aware(cls, value: datetime) -> datetime:
        """A dissemination time with no zone is not a point in time — reject it loudly.

        PIT correctness (invariant #7) rests on comparing a fact's knowable instant to a decision
        instant; a naive datetime silently adopts whatever zone the reader assumes, which is exactly
        how a fact leaks into a decision made before it existed. The parser localizes the source's
        stated time (zone-less exchange strings are read as Asia/Kolkata) and hands over an aware
        object — it never stands in the ingest clock for a missing source time.
        """
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "announcement timestamp is naive; the exchange dissemination time must be tz-aware "
                "to be point-in-time usable (the parser localizes it — the ingest clock is never a "
                "substitute for a missing source time)"
            )
        return value

    @property
    def searchable_text(self) -> str:
        """Subject, category and body joined — the text the keyword index matches against.

        Everything the exchange said in words, so a break-condition term (§5.3) can hit whichever
        field carried it. Kept a property rather than a stored column: it is derivable from the row,
        and storing it would be a second thing to keep true.
        """
        parts = [self.subject]
        if self.category:
            parts.append(self.category)
        if self.body:
            parts.append(self.body)
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class UnresolvedAnnouncement:
    """A feed record that parsed but could not be attached to an ISIN via D2.

    Kept rather than dropped: this is an identity gap (a BSE scrip the master has never seen, or
    an NSE row with a missing/blank native ISIN), not an unparseable record. A human fixes it in the
    identity data (D2), not by hand — so it is surfaced apart from the rows that landed, exactly
    as the corporate-actions parsers surface theirs (`CaParseResult.unresolved`).
    """

    source: str
    source_ref: str
    raw_identifier: str
    subject: str
    reason: str


@dataclass(frozen=True, slots=True)
class AnnParseResult:
    """Everything one feed payload produced: the rows to persist, and the identity leftovers.

    Both exchange parsers return this shape. `rows` is what `write_l1` persists; `unresolved` is the
    deliberately-not-silently-dropped remainder — a single unresolvable record is named,
    never fatal to the rest of the file.
    """

    rows: tuple[AnnouncementRow, ...] = ()
    unresolved: tuple[UnresolvedAnnouncement, ...] = ()

    @property
    def is_clean(self) -> bool:
        """True when every record in the payload resolved to a persistable row."""
        return not self.unresolved


class AnnouncementBatch(BaseModel):
    """One ingest's worth of rows — the unit an L1 partition is written from.

    What it does: hold the rows a single poll produced together with the logical date they file
    under and the L0 payload they came from, so an L1 partition can name its lineage.
    What it assumes: every row belongs to this poll's `logical_date` partition; the rows' own `ts`
    may be earlier (an EOD poll lists disclosures disseminated over the day).
    What it never does: exist without a source. An empty batch is legal for a genuinely quiet poll
    and writes an empty, well-formed partition, so "polled, nothing new" is distinguishable from
    "never polled".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_date: date = Field(description="the poll date this batch's partition files under")
    source: str = Field(min_length=1, description="Source Register id the payload came from")
    l0_key: str | None = Field(
        default=None, description="`source/date/filename` of the L0 payload this was derived from"
    )
    rows: tuple[AnnouncementRow, ...] = Field(default=(), description="the normalized disclosures")


# ── parsing ──────────────────────────────────────────────────────────────────────────────────


def parse_nse(payload: bytes, *, filename: str, l0_key: str | None = None) -> AnnParseResult:
    """Parse one NSE `corporate-announcements` response into announcement rows.

    Assumes `payload` is one whole JSON response — a JSON array of per-disclosure records keyed
    natively on ISIN (`sm_isin`). The subject is `desc`, the body `attchmntText`, the attachment
    `attchmntFile`; `ts` is the exchange dissemination time (`exchdisstime`, falling back to
    `an_dt`) and comes only from the source (invariant #7).

    Raises `ParseError`, naming the file, for anything that is not this format: an HTML soft-404, a
    non-array body, a record missing its subject, or a record with no resolvable source timestamp.
    A record whose native ISIN is missing or malformed is not fatal — it lands in `unresolved`,
    because a blank identity is an identity gap to fix in D2, not a reason to drop the whole poll.
    """
    records = _json_records(payload, filename=filename)
    rows: list[AnnouncementRow] = []
    unresolved: list[UnresolvedAnnouncement] = []
    for index, record in enumerate(records):
        subject = _text(record, _NSE_SUBJECT, index=index, filename=filename)
        ts = _source_timestamp(record, _NSE_TS, index=index, filename=filename)
        source_ref = _optional_text(record, _NSE_SEQ)
        isin = _optional_text(record, _NSE_ISIN)
        if isin is None:
            unresolved.append(
                UnresolvedAnnouncement(
                    source=NSE_SOURCE_ID,
                    source_ref=source_ref or f"row {index}",
                    raw_identifier=_optional_text(record, _NSE_SYMBOL) or "",
                    subject=subject,
                    reason=f"record {index}: native {_NSE_ISIN!r} is missing or blank",
                )
            )
            continue
        try:
            rows.append(
                AnnouncementRow(
                    ts=ts,
                    source=NSE_SOURCE_ID,
                    isin=isin,
                    symbol=_optional_text(record, _NSE_SYMBOL),
                    category=None,
                    subject=subject,
                    body=_optional_text(record, _NSE_BODY),
                    attachment_ref=_optional_text(record, _NSE_ATTACH),
                    source_ref=source_ref,
                    l0_key=l0_key,
                )
            )
        except ValidationError as exc:
            # A malformed native ISIN (fails the ISIN pattern) is an identity gap, not a format
            # break: keep the rest of the poll and surface this one for a human to fix in D2.
            unresolved.append(
                UnresolvedAnnouncement(
                    source=NSE_SOURCE_ID,
                    source_ref=source_ref or f"row {index}",
                    raw_identifier=isin,
                    subject=subject,
                    reason=f"record {index}: {exc}",
                )
            )
    result = AnnParseResult(rows=dedupe(rows), unresolved=tuple(unresolved))
    _LOG.info(
        "announcements.parsed",
        source=NSE_SOURCE_ID,
        filename=filename,
        rows=len(result.rows),
        unresolved=len(result.unresolved),
        state="VALIDATED",
    )
    return result


def parse_bse(
    payload: bytes,
    *,
    scrip_index: Mapping[str, str],
    filename: str,
    l0_key: str | None = None,
) -> AnnParseResult:
    """Parse one BSE `AnnSubCategoryGetData` response into announcement rows.

    Assumes `payload` is one whole JSON response — an object whose `Table` key holds the disclosure
    records. BSE keys on `SCRIP_CD`, so every record is resolved scrip→ISIN through `scrip_index`
    (built from the D2 master by `dataplatform.ingest.corp_actions.build_scrip_index`); a scrip the
    index does not carry lands in `unresolved` rather than under a guessed ISIN (invariant #2). The
    subject is `NEWSSUB`, the body `HEADLINE`, the category `CATEGORYNAME`; `ts` is `DT_TM`
    (falling back to `NEWS_DT`) and comes only from the source (invariant #7).

    Raises `ParseError` for a body that is not this format — including the documented empty-success
    (`{}`) that the range endpoint returns, which arrives as a 200 and must not be trusted by status
    code alone. A record missing its subject or with no resolvable timestamp is a `ParseError`; a
    record whose scrip does not resolve is `unresolved`, not fatal.
    """
    records = _bse_table(payload, filename=filename)
    rows: list[AnnouncementRow] = []
    unresolved: list[UnresolvedAnnouncement] = []
    for index, record in enumerate(records):
        subject = _text(record, _BSE_SUBJECT, index=index, filename=filename)
        ts = _source_timestamp(record, _BSE_TS, index=index, filename=filename)
        source_ref = _optional_text(record, _BSE_NEWSID)
        scrip = _optional_text(record, _BSE_SCRIP)
        if scrip is None:
            unresolved.append(
                UnresolvedAnnouncement(
                    source=BSE_SOURCE_ID,
                    source_ref=source_ref or f"row {index}",
                    raw_identifier="",
                    subject=subject,
                    reason=f"record {index}: {_BSE_SCRIP!r} is missing or blank",
                )
            )
            continue
        isin = scrip_index.get(scrip)
        if isin is None:
            unresolved.append(
                UnresolvedAnnouncement(
                    source=BSE_SOURCE_ID,
                    source_ref=source_ref or f"row {index}",
                    raw_identifier=scrip,
                    subject=subject,
                    reason=f"record {index}: scrip {scrip!r} is not in the D2 identity master",
                )
            )
            continue
        try:
            rows.append(
                AnnouncementRow(
                    ts=ts,
                    source=BSE_SOURCE_ID,
                    isin=isin,
                    symbol=None,
                    category=_optional_text(record, _BSE_CATEGORY),
                    subject=subject,
                    body=_optional_text(record, _BSE_BODY),
                    attachment_ref=_optional_text(record, _BSE_ATTACH),
                    source_ref=source_ref,
                    l0_key=l0_key,
                )
            )
        except ValidationError as exc:
            raise ParseError(f"record {index}: {exc}", filename=filename) from exc
    result = AnnParseResult(rows=dedupe(rows), unresolved=tuple(unresolved))
    _LOG.info(
        "announcements.parsed",
        source=BSE_SOURCE_ID,
        filename=filename,
        rows=len(result.rows),
        unresolved=len(result.unresolved),
        state="VALIDATED",
    )
    return result


def parse_nse_l0(store: L0Store, ref: L0Ref) -> AnnParseResult:
    """Parse the NSE L0 payload a fetch produced, re-verifying its checksum on the way in."""
    return parse_nse(store.get(ref), filename=ref.filename, l0_key=ref.key)


def parse_bse_l0(store: L0Store, ref: L0Ref, *, scrip_index: Mapping[str, str]) -> AnnParseResult:
    """Parse the BSE L0 payload a fetch produced, re-verifying its checksum on the way in."""
    return parse_bse(store.get(ref), scrip_index=scrip_index, filename=ref.filename, l0_key=ref.key)


def dedupe(rows: list[AnnouncementRow]) -> tuple[AnnouncementRow, ...]:
    """Drop exact repeats while keeping first-seen order.

    An intraday poll overlaps the previous one, so the same disclosure arrives more than once. Its
    identity for this purpose is the exchange's own id where it has one (`source, source_ref`), else
    the content tuple `(source, isin, ts, subject)` — so a feed that omits an id still de-duplicates
    on what the disclosure actually is.
    """
    seen: set[tuple[str, ...]] = set()
    out: list[AnnouncementRow] = []
    for row in rows:
        key = (
            (row.source, "id", row.source_ref)
            if row.source_ref is not None
            else (row.source, row.isin, row.ts.isoformat(), row.subject)
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return tuple(out)


def _json_records(payload: bytes, *, filename: str) -> list[Mapping[str, Any]]:
    """The JSON array of NSE records, refusing an empty body and an HTML page wearing a 200."""
    text = _decode(payload, filename=filename)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"body is not valid JSON: {exc}", filename=filename) from exc
    if isinstance(document, dict) and "data" in document:
        document = document["data"]
    if not isinstance(document, list):
        raise ParseError(
            f"expected a JSON array of announcement records, got {type(document).__name__}",
            filename=filename,
        )
    return _as_records(document, filename=filename)


def _bse_table(payload: bytes, *, filename: str) -> list[Mapping[str, Any]]:
    """The BSE `Table` array of records, refusing the documented empty-success `{}` (200)."""
    text = _decode(payload, filename=filename)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"body is not valid JSON: {exc}", filename=filename) from exc
    if not isinstance(document, dict):
        raise ParseError(
            f"expected a JSON object with a {_BSE_TABLE!r} array, got {type(document).__name__}",
            filename=filename,
        )
    table = document.get(_BSE_TABLE)
    if not isinstance(table, list) or not table:
        # The range endpoint answers a bad window with a 200 carrying `{}` (register gotcha): an
        # empty success, not an error. A caller trusting the status code would file nothing and call
        # it done; we fail loud so a missed poll is visible in /status/sync, not silent.
        raise ParseError(
            f"no {_BSE_TABLE!r} records; BSE returns a 200 with an empty body for a bad window "
            "— a poll that reaches here on a trading day is a failure, not an empty success",
            filename=filename,
        )
    return _as_records(table, filename=filename)


def _as_records(document: list[Any], *, filename: str) -> list[Mapping[str, Any]]:
    """Confirm every element is an object before the field readers assume so."""
    for index, record in enumerate(document):
        if not isinstance(record, dict):
            raise ParseError(
                f"record {index} is {type(record).__name__}, not an object", filename=filename
            )
    return list(document)


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
            "body is markup, not JSON — an HTML error page answered with a 200; it must not become "
            "an announcement row",
            filename=filename,
        )
    return text


def _text(record: Mapping[str, Any], key: str, *, index: int, filename: str) -> str:
    """A required string field, present and non-empty."""
    value = _optional_text(record, key)
    if value is None:
        raise ParseError(
            f"record {index}: no non-empty {key!r} field; present: {', '.join(sorted(record))}",
            filename=filename,
        )
    return value


def _optional_text(record: Mapping[str, Any], key: str) -> str | None:
    """A string field a record may omit or leave blank — `None`, not `""`."""
    value = record.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _source_timestamp(
    record: Mapping[str, Any], keys: tuple[str, ...], *, index: int, filename: str
) -> datetime:
    """The disclosure's source timestamp, from the first of `keys` that parses.

    The dissemination time is the only legitimate `ts` (invariant #7): a disclosure this parser
    cannot date from the source is not point-in-time usable, so it raises rather than dating it from
    the ingest clock. A zone-less exchange string is localized to Asia/Kolkata, the exchange zone —
    never the host's locale.
    """
    tried: list[str] = []
    for key in keys:
        raw = _optional_text(record, key)
        if raw is None:
            continue
        tried.append(f"{key}={raw!r}")
        parsed = _parse_datetime(raw)
        if parsed is not None:
            return parsed
    detail = f"tried {', '.join(tried)}" if tried else f"none of {', '.join(keys)} present"
    raise ParseError(
        f"record {index}: no resolvable source dissemination timestamp ({detail}); an announcement "
        "that cannot be dated from the source is not point-in-time usable",
        filename=filename,
    )


def _parse_datetime(value: str) -> datetime | None:
    """`02-Aug-2026 15:30:45` or an ISO-8601 string → an aware datetime; None if neither.

    Two shapes cover both exchanges: BSE's ISO `DT_TM`/`NEWS_DT` (`2026-08-07T15:30:45`, optionally
    fractional) and NSE's `DD-Mon-YYYY HH:MM:SS` `exchdisstime`. Both are localized to Asia/Kolkata
    when zone-less; a value that is neither shape returns None so the caller can try the next key.
    """
    text = value.strip()
    iso = _try_iso(text)
    if iso is not None:
        return iso
    return _try_dmy(text)


def _try_iso(text: str) -> datetime | None:
    """Parse an ISO-8601 datetime, localizing a zone-less one to Asia/Kolkata."""
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=IST)
    return parsed


def _try_dmy(text: str) -> datetime | None:
    """Parse `DD-Mon-YYYY[ HH:MM[:SS]]` locale-independently, localizing to Asia/Kolkata.

    A missing clock is read as midnight — a disclosure dated to the day but not the second still has
    a defensible knowable instant, and the exchange's own time takes precedence when it is there.
    """
    parts = text.split(" ", 1)
    date_part = parts[0]
    fields = date_part.split("-")
    if len(fields) != 3:
        return None
    day_s, mon_s, year_s = fields
    month = _MONTHS.get(mon_s.upper())
    if month is None or not (day_s.isdigit() and year_s.isdigit()):
        return None
    hour = minute = second = 0
    if len(parts) == 2 and parts[1].strip():
        clock = parts[1].strip().split(":")
        if not all(field.isdigit() for field in clock) or len(clock) not in (2, 3):
            return None
        hour = int(clock[0])
        minute = int(clock[1])
        second = int(clock[2]) if len(clock) == 3 else 0
    try:
        return datetime(int(year_s), month, int(day_s), hour, minute, second, tzinfo=IST)
    except ValueError:
        return None


# ── L1 ───────────────────────────────────────────────────────────────────────────────────────

#: The L1 schema, declared once and enforced on write (§4.2, M1.8's rule). `ts` is stored as a UTC
#: instant — the source's zone is folded into the instant, which is the fact PIT cares about — so a
#: round trip returns the same instant it was disseminated at, never re-stamped.
_L1_SCHEMA: Final = pa.schema(
    [
        pa.field("ts", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("isin", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=True),
        pa.field("category", pa.string(), nullable=True),
        pa.field("subject", pa.string(), nullable=False),
        pa.field("body", pa.string(), nullable=True),
        pa.field("attachment_ref", pa.string(), nullable=True),
        pa.field("source_ref", pa.string(), nullable=True),
        pa.field("logical_date", pa.date32(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=True),
    ]
)


def write_l1(batch: AnnouncementBatch, *, data_root: Path | None = None) -> Path:
    """Write one poll's rows to its L1 partition and return the file's path.

    Idempotent per `(dataset, logical_date)`: rows go in `(ts, isin, source_ref)` order and the file
    is written whole to a temporary name then renamed over the target, so re-deriving the same batch
    from L0 produces byte-identical output and a crash mid-write cannot leave a half partition
    readable. An empty batch writes an empty, schema-correct partition. No derived or re-stamped
    values — every row's `ts` is the source's own, and the batch's `l0_key` rides on each row.
    """
    rows = sorted(batch.rows, key=lambda row: (row.ts, row.isin, row.source_ref or ""))
    path = l1_partition_path(ANNOUNCEMENTS_DATASET, batch.logical_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [_to_record(row, logical_date=batch.logical_date) for row in rows], schema=_L1_SCHEMA
    )
    staging = path.with_name(f".{path.name}.partial")
    pq.write_table(table, staging, compression="snappy", version="2.6")
    staging.replace(path)
    _LOG.info(
        "announcements.l1_written",
        source=batch.source,
        logical_date=batch.logical_date.isoformat(),
        dataset=ANNOUNCEMENTS_DATASET,
        path=str(path),
        rows=len(rows),
        l0_key=batch.l0_key,
        state="NORMALIZED",
    )
    return path


def read_l1(logical_date: date, *, data_root: Path | None = None) -> AnnouncementBatch:
    """Read one poll's partition back out of L1.

    Raises `FileNotFoundError` when the partition was never written — an absent partition is a gap
    for D7 to explain, not an empty poll (which is a partition that exists and holds zero rows).
    """
    path = l1_partition_path(ANNOUNCEMENTS_DATASET, logical_date, data_root=data_root)
    if not path.exists():
        raise FileNotFoundError(
            f"no {ANNOUNCEMENTS_DATASET} partition for {logical_date.isoformat()}: {path}"
        )
    rows = _rows_of(path)
    source = rows[0].source if rows else ANNOUNCEMENTS_DATASET
    l0_key = rows[0].l0_key if rows else None
    return AnnouncementBatch(logical_date=logical_date, source=source, l0_key=l0_key, rows=rows)


def iter_l1(
    *, start: date | None = None, end: date | None = None, data_root: Path | None = None
) -> Iterator[AnnouncementRow]:
    """Yield every announcement row across the partitions in an inclusive logical-date range.

    The read the index is built from. Partitions are chosen by their `logical_date` (the poll
    date); a row's own `ts` may fall outside `[start, end]` by a poll's worth, and filtering to the
    caller's true window is the search layer's job against `ts`, not this coarse partition prune.
    Ordered by partition date so an index build is reproducible.
    """
    if start is not None and end is not None and start > end:
        raise ValueError(f"empty date range: start {start.isoformat()} > end {end.isoformat()}")
    dataset_dir = layer_root(Layer.L1, data_root=data_root) / ANNOUNCEMENTS_DATASET
    if not dataset_dir.is_dir():
        return
    for partition_dir in sorted(dataset_dir.iterdir()):
        if not partition_dir.is_dir():
            continue
        try:
            logical_date = partition_date_of(partition_dir)
        except ValueError:
            continue
        if (start is not None and logical_date < start) or (end is not None and logical_date > end):
            continue
        path = partition_dir / "part.parquet"
        if path.exists():
            yield from _rows_of(path)


def _rows_of(path: Path) -> tuple[AnnouncementRow, ...]:
    """Parse one L1 partition file into rows, enforcing the declared schema on read."""
    records = pq.read_table(path, schema=_L1_SCHEMA).to_pylist()
    return tuple(
        AnnouncementRow(
            ts=record["ts"],
            source=str(record["source"]),
            isin=str(record["isin"]),
            symbol=_none_or_str(record["symbol"]),
            category=_none_or_str(record["category"]),
            subject=str(record["subject"]),
            body=_none_or_str(record["body"]),
            attachment_ref=_none_or_str(record["attachment_ref"]),
            source_ref=_none_or_str(record["source_ref"]),
            l0_key=_none_or_str(record["l0_key"]),
        )
        for record in records
    )


def _to_record(row: AnnouncementRow, *, logical_date: date) -> dict[str, Any]:
    """One row as the dict `pa.Table.from_pylist` writes against `_L1_SCHEMA`."""
    return {
        "ts": row.ts,
        "source": row.source,
        "isin": row.isin,
        "symbol": row.symbol,
        "category": row.category,
        "subject": row.subject,
        "body": row.body,
        "attachment_ref": row.attachment_ref,
        "source_ref": row.source_ref,
        "logical_date": logical_date,
        "l0_key": row.l0_key,
    }


def _none_or_str(value: Any) -> str | None:
    """A nullable string column read back as `None` or `str`, never a numpy scalar."""
    return None if value is None else str(value)


# ── the poll runners ───────────────────────────────────────────────────────────────────────────


class SyncTracker(Protocol):
    """The slice of the §4.4 state machine (M1.3) one announcement poll drives.

    A structural protocol rather than the concrete `SyncStateStore`, so a poll can be driven end to
    end offline (B8) without Postgres — the store satisfies it, and a test double satisfies it too.
    It is the whole happy path plus `mark_failed`: a runner that could reach `PUBLISHED` without
    passing `VALIDATED` would be a second state machine.
    """

    def begin(self, source: str, logical_date: date) -> SyncRecord: ...

    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> SyncRecord: ...

    def mark_validated(self, source: str, logical_date: date) -> SyncRecord: ...

    def mark_normalized(self, source: str, logical_date: date) -> SyncRecord: ...

    def mark_published(self, source: str, logical_date: date) -> SyncRecord: ...

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> SyncRecord: ...


def _source_url(source_id: str, register: SourceRegister | None) -> str:
    """The endpoint template, read from the Source Register rather than repeated here (C.1)."""
    reg = load_register() if register is None else register
    source = next((entry for entry in reg.sources if entry.id == source_id), None)
    if source is None:
        raise IngestError(f"no {source_id!r} entry in the Source Register")
    return source.url_template


def _l0_filename(source_id: str, poll_date: date) -> str:
    """A stable, dated L0 filename for one poll's response.

    The announcement endpoints carry no dated path segment, so L0 would otherwise be handed the same
    filename every poll and the second poll of a month would collide with the first (`L0Store.put`).
    The poll date goes in the name here, the only place it can.
    """
    return f"{source_id}_{poll_date:%Y%m%d}.json"


def ingest_nse(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SyncTracker,
    poll_date: date,
    url: str | None = None,
    data_root: Path | None = None,
    register: SourceRegister | None = None,
) -> AnnParseResult:
    """Take one NSE announcement poll from nothing to `PUBLISHED`: fetch → L0 → parse → L1 → sync.

    `poll_date` is the logical date the L0 payload and the L1 partition file under; each row carries
    its own source `ts`. `url` overrides the register template for a windowed backfill; by default
    the register's verified template is used. Any failure is recorded on the sync row with
    `retryable` set from the cause, then re-raised, so the caller sees the exception and the status
    API sees the state. Unresolved rows are logged but do not fail the poll — they are an identity
    gap for D2, not a format break.
    """
    return _ingest(
        source_id=NSE_SOURCE_ID,
        parse=lambda ref: parse_nse_l0(l0, ref),
        fetcher=fetcher,
        tracker=tracker,
        poll_date=poll_date,
        url=url,
        data_root=data_root,
        register=register,
    )


def ingest_bse(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SyncTracker,
    scrip_index: Mapping[str, str],
    poll_date: date,
    url: str | None = None,
    data_root: Path | None = None,
    register: SourceRegister | None = None,
) -> AnnParseResult:
    """Take one BSE announcement poll from nothing to `PUBLISHED`, resolving scrip→ISIN via D2.

    As `ingest_nse`, but the BSE feed keys on `SCRIP_CD`, so `scrip_index` (from
    `corp_actions.build_scrip_index`) is required and a scrip it lacks lands in the result's
    `unresolved` rather than under a guess (invariant #2).
    """
    return _ingest(
        source_id=BSE_SOURCE_ID,
        parse=lambda ref: parse_bse_l0(l0, ref, scrip_index=scrip_index),
        fetcher=fetcher,
        tracker=tracker,
        poll_date=poll_date,
        url=url,
        data_root=data_root,
        register=register,
    )


def _ingest(
    *,
    source_id: str,
    parse: Any,
    fetcher: Fetcher,
    tracker: SyncTracker,
    poll_date: date,
    url: str | None,
    data_root: Path | None,
    register: SourceRegister | None,
) -> AnnParseResult:
    """The §4.4 drive shared by both exchanges — one fetch, one parse, one partition write."""
    endpoint = _source_url(source_id, register) if url is None else url
    tracker.begin(source_id, poll_date)
    try:
        ref = fetcher.fetch(
            source_id, endpoint, poll_date, filename=_l0_filename(source_id, poll_date)
        )
        tracker.mark_fetched(source_id, poll_date, checksum=ref.sha256, l0_path=ref.key)

        result: AnnParseResult = parse(ref)
        tracker.mark_validated(source_id, poll_date)

        batch = AnnouncementBatch(
            logical_date=poll_date, source=source_id, l0_key=ref.key, rows=result.rows
        )
        write_l1(batch, data_root=data_root)
        tracker.mark_normalized(source_id, poll_date)

        tracker.mark_published(source_id, poll_date)
    except Exception as exc:
        tracker.mark_failed(
            source_id, poll_date, f"{type(exc).__name__}: {exc}", retryable=_retryable(exc)
        )
        _LOG.error(
            "announcements.ingest_failed",
            source=source_id,
            poll_date=poll_date.isoformat(),
            error=f"{type(exc).__name__}: {exc}",
            retryable=_retryable(exc),
            state="FAILED",
        )
        raise

    _LOG.info(
        "announcements.published",
        source=source_id,
        poll_date=poll_date.isoformat(),
        rows=len(result.rows),
        unresolved=len(result.unresolved),
        l0_key=ref.key,
        state="PUBLISHED",
    )
    return result


def _retryable(exc: BaseException) -> bool:
    """Whether repeating this attempt later could ever produce a different outcome (§4.4)."""
    if isinstance(exc, ForbiddenSpikeError):
        return False
    if isinstance(exc, RetryableFetchError):
        return True
    return not isinstance(exc, ParseError | FetchHTTPError)
