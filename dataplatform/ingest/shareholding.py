"""NSE quarterly shareholding-pattern filings (§4.1 row 11) → L1 `shareholding`.

Every listed company files a shareholding pattern with the exchange each quarter: how much of it
the promoter and promoter group hold, how much of *that* promoter holding is pledged (encumbered),
and how the rest splits across FII, DII and the wider public. Two of those numbers are load-bearing
for the analyst:

* **The promoter pledge is a break condition, not a footnote.** §5.3's BC3 fires on "promoter
  pledge >50%" — an integrity break that takes a thesis straight to a T0 flag and an immediate T1
  review. A schema that buried pledge inside a notes blob would make BC3 un-evaluatable, so it is a
  first-class, range-checked `Decimal` field here (`promoter_pledge_pct`) with the threshold spelled
  out (`PLEDGE_BREACH_PCT`) and a three-way `bc3_status` the monitor can call directly.
* **Pledge is optional, and its absence is said out loud** (decision D17, 2026-10-06). The live
  master (`master_2026_10` era) carries no pledge at all — it lives in the per-filing XBRL the
  record links to. A row without one has `promoter_pledge_pct = None` and `bc3_status ==
  NOT_APPLICABLE`, never `CLEAR`: "we could not evaluate BC3" and "BC3 holds" are different claims,
  and a boolean predicate that answered `False` for both is exactly the silent skip D17 forbids.
  `quality_findings` turns every not-applicable row into an INFO `quality_flag`, so the gap is on
  `GET /status/quality` and not only in a log line.
* **The filing date is not the quarter end.** §4.1 is explicit ("Filing date ≠ quarter end — store
  both"), and it matters because a quarter that ended 31-Mar is only *knowable* weeks later when the
  filing is broadcast. Storing one and inferring the other would either back-date the knowledge
  (a look-ahead leak — invariant #7) or lose the quarter a number describes. So every row carries
  both `period_end` (the quarter the numbers are about) and `filing_date` (the first date they were
  knowable), parsed from two independent fields in the payload, and a row whose filing does not fall
  strictly after its quarter end is rejected rather than repaired.

The point-in-time contract is enforced where the data is read. L1 is partitioned by `filing_date`,
so `read_pit(on_date)` answers "what did we know on this date" by reading only the partitions whose
filing date is on or before it: a quarter filed after the as-of date is physically not in the
result, which is invariant #7 made structural rather than trusted to a `WHERE` clause a caller might
forget. `tests/unit/test_shareholding.py` asserts a query dated before a filing cannot see it.

Two format eras, told apart by their keys and never mixed in one payload (`FormatEra`):

* `json_v1` — the shape M3.6 was first built against (synthetic fixture): public holding in
  `public_prcnt`, pledge in `pledgeShares_prcnt`, FII/DII splits in `fii_prcnt`/`dii_prcnt`.
* `master_2026_10` — the live master as captured on 2026-10-06: public holding in `public_val`, no
  pledge and no FII/DII split, plus `employeeTrusts` and a `revisedData` marker.

Money-shaped rules that carry over from the rest of D1: percentages are `Decimal`, never `float`
(a float pledge that reads 49.999999 would silently duck a >50 break); a value is read as text and
converted exactly, and `NaN`/`Infinity` are parse failures, not percentages that compare greater
than a hundred. Identity is ISIN (invariant #2): the payload carries it natively, so nothing here
joins on a symbol. The live master leaves `isin` null on the odd record; that record is skipped and
counted (`ShareholdingSnapshot.skipped_no_isin`, and a WARN `quality_flag`), never resolved from
its symbol — the same rule the BSE scrip master follows for a blank ISIN.

Offline by construction: this module takes bytes, or an `L0Ref` it reads back through `L0Store`.
`ingest_snapshot` is the one function that drives a fetch, and it does so through the crawl engine,
which is still the only thing in the platform that opens a socket.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Protocol

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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
from dataplatform.quality.sentinel import QualityFinding, finding_fingerprint
from dataplatform.status.sync_state import SyncRecord
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.paths import Layer, l1_partition_path, layer_root, partition_date_of

__all__ = [
    "BC3_CHECK",
    "NO_ISIN_CHECK",
    "PLEDGE_BREACH_PCT",
    "SHAREHOLDING_DATASET",
    "SOURCE_ID",
    "Bc3Status",
    "FormatEra",
    "ShareholdingRow",
    "ShareholdingSnapshot",
    "SkippedFiling",
    "SyncTracker",
    "ingest_snapshot",
    "l0_filename",
    "merge_l1",
    "parse",
    "parse_l0",
    "quality_findings",
    "read_l1",
    "read_pit",
    "snapshot_url",
    "write_l1",
]

_LOG = get_logger(__name__)

#: The register id this parser serves (`source_register.yaml`, `parser.task: M3.6`).
SOURCE_ID: Final = "nse_shareholding_pattern"

#: The L1 dataset name. Partitioned by `filing_date` (the knowable date), not by quarter end:
#: `data/L1/shareholding/date=YYYY-MM-DD/part.parquet`, where the date is when the filing became
#: knowable. That is what makes `read_pit` a partition prune rather than a row scan.
SHAREHOLDING_DATASET: Final = "shareholding"

#: §5.3 BC3: a promoter pledge above this fraction of promoter holding is an integrity break.
#: A `Decimal`, and the comparison is strict `>`, so exactly 50.00 is not yet a breach — the plan
#: says "pledge >50%", and a float threshold could not represent that boundary exactly.
PLEDGE_BREACH_PCT: Final = Decimal(50)

#: The `quality_flag.check_name` BC3 outcomes are filed under: WARN for a breach, INFO for a row
#: whose pledge the payload did not state (D17's explicit `not_applicable`).
BC3_CHECK: Final = "shareholding_bc3"

#: The `quality_flag.check_name` a record the source published without an ISIN is filed under.
NO_ISIN_CHECK: Final = "shareholding_no_isin"


class Bc3Status(StrEnum):
    """The outcome of §5.3 BC3 on one row. Three values, because "not evaluated" is not "holds"."""

    BREACH = "breach"
    CLEAR = "clear"
    NOT_APPLICABLE = "not_applicable"


class FormatEra(StrEnum):
    """The payload shapes this parser reads; each has a frozen fixture under the same name."""

    JSON_V1 = "json_v1"
    MASTER_2026_10 = "master_2026_10"


#: Month abbreviations as NSE spells them (`31-Mar-2026`). Spelled out rather than handed to
#: `strptime("%b")`, which reads `LC_TIME`: a host with a non-English locale would otherwise fail to
#: parse a date that is not locale-dependent at all (the same guard `fii_dii` uses).
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

#: `31-Mar-2026`, optionally trailed by a `HH:MM:SS` clock the broadcast timestamp carries. The
#: date is all this dataset keeps — §4.1 speaks of a filing *date* — but the clock is tolerated so a
#: `broadcastDate` of `07-May-2026 18:30:00` is not rejected for the time it states.
_FILING_DATE = re.compile(
    r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\s*$"
)

#: A plain decimal literal, optionally signed. Checked before `Decimal()` sees the text because
#: `Decimal` itself accepts `NaN`/`Infinity`, and a mis-framed field that spells one of those must
#: not become a percentage that compares greater than a hundred.
_DECIMAL_LITERAL = re.compile(r"^[+-]?\d+(\.\d+)?$")

#: The payload keys this parser reads, named once so a source rename is one edit. `period_end` and
#: `filing_date` come from two *different* keys on purpose — neither is inferred from the other
#: (acceptance 1). `filing_date` prefers `broadcastDate` and falls back to `cgTimeStamp`; the
#: register's `pit_notes` names both as the first-knowable timestamp.
_KEY_ISIN: Final = "isin"
_KEY_NAME: Final = "name"
_KEY_PERIOD_END: Final = "date"
_KEY_FILING: Final = ("broadcastDate", "cgTimeStamp")
_KEY_PROMOTER: Final = "pr_and_prgrp"
_KEY_PLEDGE: Final = "pledgeShares_prcnt"
_KEY_FII: Final = "fii_prcnt"
_KEY_DII: Final = "dii_prcnt"
_KEY_EMPLOYEE_TRUSTS: Final = "employeeTrusts"
_KEY_REVISED: Final = "revisedData"
_KEY_SYMBOL: Final = "symbol"

#: The public-holding key is what tells the eras apart: each era has exactly one of these.
_KEY_PUBLIC: Final[Mapping[FormatEra, str]] = {
    FormatEra.JSON_V1: "public_prcnt",
    FormatEra.MASTER_2026_10: "public_val",
}

#: `revisedData` as the live master spells it. Anything else fails the parse: a new marker value is
#: a format change to read, not a revision flag to guess at.
_REVISED: Final[Mapping[str, bool]] = {"N": False, "Revised": True}

#: A holding or pledge percentage. Strict (no float can be constructed into one), bounded to a real
#: percentage, and finite. `le=100` is what turns a mis-scaled field — a fraction stated as 0.503
#: where 50.3 was meant, or a basis-point figure — into a loud failure instead of a wrong holding.
Pct = Annotated[Decimal, Field(ge=0, le=100, strict=True, allow_inf_nan=False)]


class ShareholdingRow(BaseModel):
    """One company's shareholding pattern for one quarter, as the exchange published it.

    What it does: carry the promoter/pledge/public split for one `(isin, period_end)`, tagged with
    the `filing_date` on which it first became knowable.
    What it assumes: the parser already checked the payload's structure, so a row that exists is one
    the source really filed, with a filing date strictly after the quarter it reports.
    What it never does: infer one date from the other, hold a `float`, or bury the pledge — the one
    number §5.3 BC3 turns on is a first-class, range-checked field, not a note.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(
        pattern=ISIN_PATTERN, description="ISO 6166 identifier — the only join key (invariant #2)"
    )
    name: str = Field(min_length=1, description="company name as filed, for display only")
    period_end: date = Field(description="the quarter end the holdings are as of (§4.1)")
    filing_date: date = Field(
        description="first date the pattern was knowable; strictly after period_end (§4.1)"
    )
    promoter_holding_pct: Pct = Field(description="promoter + promoter-group holding, % of capital")
    promoter_pledge_pct: Pct | None = Field(
        default=None,
        description="promoter shares pledged/encumbered, % of promoter holding — §5.3 BC3 input; "
        "None when the payload does not state it (BC3 is then not applicable, D17)",
    )
    public_pct: Pct = Field(description="public shareholding, % of capital")
    employee_trusts_pct: Pct | None = Field(
        default=None, description="employee-benefit-trust holding, % of capital; None if not stated"
    )
    revised: bool = Field(
        default=False, description="the exchange marks this filing as a revision of an earlier one"
    )
    fii_pct: Pct | None = Field(
        default=None, description="FII/FPI holding, % of capital; None if not split"
    )
    dii_pct: Pct | None = Field(
        default=None, description="DII holding, % of capital; None if not split"
    )
    source: str = Field(min_length=1, description="Source Register id the payload came from")
    l0_key: str | None = Field(
        default=None, description="`source/date/filename` of the L0 payload this was derived from"
    )

    @property
    def bc3_status(self) -> Bc3Status:
        """§5.3 BC3 on this row: BREACH above 50% pledge, CLEAR at or below, NOT_APPLICABLE without.

        Deliberately not a boolean: a row whose payload stated no pledge has not passed BC3, it was
        never tested, and a caller that only asks "breach?" would read that as "fine" (D17).
        """
        if self.promoter_pledge_pct is None:
            return Bc3Status.NOT_APPLICABLE
        if self.promoter_pledge_pct > PLEDGE_BREACH_PCT:
            return Bc3Status.BREACH
        return Bc3Status.CLEAR

    @model_validator(mode="after")
    def _filing_after_period(self) -> ShareholdingRow:
        """A filing cannot predate — or coincide with — the quarter it reports (§4.1).

        The check that makes "store both, infer neither" enforceable: if the two dates were the
        same object, or the filing were derived from the quarter end, this could not fail. It does
        fail if the two are transposed, which is exactly the mistake a single-date schema invites.
        """
        if self.filing_date <= self.period_end:
            raise ValueError(
                f"{self.isin} {self.period_end.isoformat()}: filing_date "
                f"{self.filing_date.isoformat()} is not after the quarter end; §4.1 requires the "
                "filing date and the quarter end to be distinct, filing after period"
            )
        return self


class SkippedFiling(BaseModel):
    """A record the source published without an ISIN — counted and flagged, never joined.

    `symbol` and `name` are carried for a human reading the flag; nothing joins on them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str | None = Field(default=None, description="exchange symbol, for display only")
    name: str = Field(min_length=1, description="company name as filed, for display only")
    period_end: date
    filing_date: date


class ShareholdingSnapshot(BaseModel):
    """One master payload's worth of filings — every company's latest pattern at one poll.

    What it does: hold the rows one `corporate-share-holdings-master` response yielded, together
    with the L0 payload they came from, so an L1 partition can name its lineage.
    What it assumes: each `(isin, period_end)` appears once in a payload; a company listing the same
    quarter twice is a broken response, not two rows.
    What it never does: exist with a duplicate `(isin, period_end)` — validation raises, so a
    snapshot that exists is one every partition writer can trust.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1, description="Source Register id the payload came from")
    l0_key: str | None = Field(default=None, description="`source/date/filename` of the L0 payload")
    rows: tuple[ShareholdingRow, ...] = Field(description="one row per company/quarter, sorted")
    skipped_no_isin: tuple[SkippedFiling, ...] = Field(
        default=(), description="records the source published with no ISIN; not in `rows`"
    )

    @model_validator(mode="after")
    def _no_duplicate_filing(self) -> ShareholdingSnapshot:
        keys = [(row.isin, row.period_end) for row in self.rows]
        if len(keys) != len(set(keys)):
            duplicate = next(key for key in keys if keys.count(key) > 1)
            raise ValueError(
                f"payload lists {duplicate[0]} for {duplicate[1].isoformat()} more than once; "
                "one master response reports each company's quarter at most once"
            )
        return self

    def breaching(self) -> tuple[ShareholdingRow, ...]:
        """The rows whose promoter pledge crosses BC3 (>50%) — the T0 monitor's shortlist."""
        return tuple(row for row in self.rows if row.bc3_status is Bc3Status.BREACH)

    def bc3_not_applicable(self) -> tuple[ShareholdingRow, ...]:
        """The rows BC3 could not be evaluated on, because the payload stated no pledge (D17)."""
        return tuple(row for row in self.rows if row.bc3_status is Bc3Status.NOT_APPLICABLE)


# ── parsing ──────────────────────────────────────────────────────────────────────────────────


def parse(payload: bytes, *, filename: str, l0_key: str | None = None) -> ShareholdingSnapshot:
    """Parse one `corporate-share-holdings-master` response into a snapshot of filings.

    Assumes `payload` is one whole JSON response — a JSON array of per-company records, all of one
    `FormatEra`. `filename` names the file in errors and logs. Each record's quarter end comes from
    its `date` field and its filing date from `broadcastDate` (or `cgTimeStamp`): the two are read
    independently, so neither can be inferred from the other (acceptance 1). A pledge the record
    does not state is `None` (BC3 not applicable); a pledge it does state is held to the same
    0-100 plain-decimal rule as every other percentage.

    A record whose `isin` is present but null or blank is skipped into `skipped_no_isin` — counted,
    logged, and flagged by `quality_findings` — rather than failing the whole poll for one company
    the exchange did not identify.

    Raises `ParseError`, naming the file, for anything that is not this format: an HTML soft-404, a
    non-array body, a record of neither era or of a different era from its neighbours, a record
    missing a required field (including `isin` itself), a malformed ISIN, a percentage that is not
    a plain decimal or that sits outside 0-100, an unknown `revisedData` marker, a filing that does
    not fall after its quarter, or a company listed twice for one quarter. Never returns a partial
    or repaired snapshot.
    """
    text = _decode(payload, filename=filename)
    records = _records(text, filename=filename)
    era = _era(records, filename=filename)
    rows: list[ShareholdingRow] = []
    skipped: list[SkippedFiling] = []
    for index, record in enumerate(records):
        if _isin_withheld(record, index=index, filename=filename):
            skipped.append(_skipped(record, index=index, filename=filename))
        else:
            rows.append(_row(record, era=era, index=index, l0_key=l0_key, filename=filename))
    try:
        snapshot = ShareholdingSnapshot(
            source=SOURCE_ID,
            l0_key=l0_key,
            rows=tuple(sorted(rows, key=lambda row: (row.isin, row.period_end))),
            skipped_no_isin=tuple(
                sorted(skipped, key=lambda s: (s.filing_date, s.symbol or "", s.name))
            ),
        )
    except ValidationError as exc:
        raise ParseError(str(exc), filename=filename) from exc

    if snapshot.skipped_no_isin:
        _LOG.warning(
            "shareholding.skipped_no_isin",
            source=SOURCE_ID,
            filename=filename,
            skipped=len(snapshot.skipped_no_isin),
            symbols=[s.symbol or s.name for s in snapshot.skipped_no_isin],
            state="VALIDATED",
        )
    _LOG.info(
        "shareholding.parsed",
        source=SOURCE_ID,
        filename=filename,
        era=era.value,
        rows=len(snapshot.rows),
        breaching=len(snapshot.breaching()),
        bc3_not_applicable=len(snapshot.bc3_not_applicable()),
        skipped_no_isin=len(snapshot.skipped_no_isin),
        state="VALIDATED",
    )
    return snapshot


def parse_l0(store: L0Store, ref: L0Ref) -> ShareholdingSnapshot:
    """Parse the L0 payload a fetch produced, re-verifying its checksum on the way in.

    `Fetcher.fetch` returns an `L0Ref` and never bytes, so this is how a fetched response becomes a
    snapshot. `L0Store.get` re-hashes the payload, which makes "every L1 value derives from bytes
    that have not changed" true where the derivation happens.
    """
    return parse(store.get(ref), filename=ref.filename, l0_key=ref.key)


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
            "body is markup, not JSON — an HTML error page answered with a 200; it must not "
            "become a shareholding row",
            filename=filename,
        )
    return text


def _records(text: str, *, filename: str) -> list[Mapping[str, Any]]:
    """The JSON array of company records, with numbers kept out of `float` on the way in."""
    try:
        # `parse_float`/`parse_int` are the money guard: a switch from quoted strings to bare JSON
        # numbers must not silently become binary floating point.
        document = json.loads(text, parse_float=Decimal, parse_int=Decimal)
    except json.JSONDecodeError as exc:
        raise ParseError(f"body is not valid JSON: {exc}", filename=filename) from exc
    if isinstance(document, dict) and "data" in document:
        # Some NSE endpoints wrap the array in `{"data": [...]}`. Accept that shape too, so a
        # cosmetic envelope change does not stop ingestion, but nothing else.
        document = document["data"]
    if not isinstance(document, list):
        raise ParseError(
            f"expected a JSON array of company records, got {type(document).__name__}",
            filename=filename,
        )
    if not document:
        raise ParseError("JSON array is empty; a master response lists filings", filename=filename)
    for index, record in enumerate(document):
        if not isinstance(record, dict):
            raise ParseError(
                f"record {index} is {type(record).__name__}, not an object", filename=filename
            )
    return list(document)


def _era(records: list[Mapping[str, Any]], *, filename: str) -> FormatEra:
    """The one `FormatEra` every record in the payload belongs to.

    Told apart by the public-holding key, which each era spells differently. A record carrying
    both, neither, or a different one from the first record fails the parse: a payload that mixes
    eras is a framing fault, and reading each record by whichever key it happens to have would
    hide a half-migrated source behind rows that look fine.
    """
    eras: list[FormatEra] = []
    for index, record in enumerate(records):
        found = [era for era, key in _KEY_PUBLIC.items() if key in record]
        if len(found) != 1:
            raise ParseError(
                f"record {index}: expected exactly one public-holding key of "
                f"{', '.join(sorted(_KEY_PUBLIC.values()))}; present: {', '.join(sorted(record))}",
                filename=filename,
            )
        eras.append(found[0])
    first = eras[0]
    for index, era in enumerate(eras):
        if era is not first:
            raise ParseError(
                f"record {index} is {era.value} but record 0 is {first.value}; one payload is "
                "one format era",
                filename=filename,
            )
    return first


def _isin_withheld(record: Mapping[str, Any], *, index: int, filename: str) -> bool:
    """Whether the source published this record with a null or blank ISIN.

    The key itself must be there — a record with no `isin` field at all is a different format, not
    a company the exchange did not identify, and fails like any other missing field.
    """
    if _KEY_ISIN not in record:
        raise ParseError(
            f"record {index}: no {_KEY_ISIN!r} field; present: {', '.join(sorted(record))}",
            filename=filename,
        )
    value = record[_KEY_ISIN]
    return value is None or (isinstance(value, str) and not value.strip())


def _skipped(record: Mapping[str, Any], *, index: int, filename: str) -> SkippedFiling:
    """A no-ISIN record, kept only as far as a human needs to chase it."""
    symbol = record.get(_KEY_SYMBOL)
    try:
        return SkippedFiling(
            symbol=symbol.strip() if isinstance(symbol, str) and symbol.strip() else None,
            name=_text(record, _KEY_NAME, index=index, filename=filename),
            period_end=_period_end(record, index=index, filename=filename),
            filing_date=_filing_date(record, index=index, filename=filename),
        )
    except ValidationError as exc:
        raise ParseError(f"record {index}: {exc}", filename=filename) from exc


def _row(
    record: Mapping[str, Any],
    *,
    era: FormatEra,
    index: int,
    l0_key: str | None,
    filename: str,
) -> ShareholdingRow:
    """One feed record → one validated `ShareholdingRow`."""
    try:
        return ShareholdingRow(
            isin=_text(record, _KEY_ISIN, index=index, filename=filename),
            name=_text(record, _KEY_NAME, index=index, filename=filename),
            period_end=_period_end(record, index=index, filename=filename),
            filing_date=_filing_date(record, index=index, filename=filename),
            promoter_holding_pct=_pct(record, _KEY_PROMOTER, index=index, filename=filename),
            promoter_pledge_pct=_pledge(record, era=era, index=index, filename=filename),
            public_pct=_pct(record, _KEY_PUBLIC[era], index=index, filename=filename),
            fii_pct=_optional_pct(record, _KEY_FII, index=index, filename=filename),
            dii_pct=_optional_pct(record, _KEY_DII, index=index, filename=filename),
            employee_trusts_pct=_optional_pct(
                record, _KEY_EMPLOYEE_TRUSTS, index=index, filename=filename
            ),
            revised=_revised(record, index=index, filename=filename),
            source=SOURCE_ID,
            l0_key=l0_key,
        )
    except ValidationError as exc:
        raise ParseError(f"record {index}: {exc}", filename=filename) from exc


def _period_end(record: Mapping[str, Any], *, index: int, filename: str) -> date:
    return _filing_or_period_date(
        _text(record, _KEY_PERIOD_END, index=index, filename=filename),
        key=_KEY_PERIOD_END,
        index=index,
        filename=filename,
    )


def _filing_date(record: Mapping[str, Any], *, index: int, filename: str) -> date:
    return _filing_or_period_date(
        _filing_field(record, index=index, filename=filename),
        key="/".join(_KEY_FILING),
        index=index,
        filename=filename,
    )


def _revised(record: Mapping[str, Any], *, index: int, filename: str) -> bool:
    """`revisedData`: absent (the `json_v1` era) is an original filing; unknown markers fail."""
    value = record.get(_KEY_REVISED)
    if value is None:
        return False
    if not isinstance(value, str) or value.strip() not in _REVISED:
        raise ParseError(
            f"record {index}: {_KEY_REVISED!r} is {value!r}, expected one of "
            f"{', '.join(sorted(_REVISED))}",
            filename=filename,
        )
    return _REVISED[value.strip()]


def _filing_field(record: Mapping[str, Any], *, index: int, filename: str) -> str:
    """The filing timestamp, from `broadcastDate` or its `cgTimeStamp` fallback."""
    for key in _KEY_FILING:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ParseError(
        f"record {index}: no filing timestamp; expected one of {', '.join(_KEY_FILING)}, "
        f"present: {', '.join(sorted(record))}",
        filename=filename,
    )


def _text(record: Mapping[str, Any], key: str, *, index: int, filename: str) -> str:
    """A required string field, present and non-empty."""
    if key not in record:
        raise ParseError(
            f"record {index}: no {key!r} field; present: {', '.join(sorted(record))}",
            filename=filename,
        )
    value = record[key]
    if not isinstance(value, str) or not value.strip():
        raise ParseError(
            f"record {index}: {key!r} is {value!r}, expected a non-empty string", filename=filename
        )
    return value.strip()


def _optional_pct(
    record: Mapping[str, Any], key: str, *, index: int, filename: str
) -> Decimal | None:
    """A percentage that a payload may omit or leave blank — `None`, not `0`.

    A split the filing did not report is unknown, and a zero there would read as "no FII holds any
    of this", which is a different and usually false claim. Absent stays absent.
    """
    value = record.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _pct(record, key, index=index, filename=filename)


def _pledge(
    record: Mapping[str, Any], *, era: FormatEra, index: int, filename: str
) -> Decimal | None:
    """The promoter pledge: `None` only in an era that does not state one (D17).

    Era-aware. `json_v1` is the format that carries `pledgeShares_prcnt`, so there it stays
    required, as M3.6 specified: a v1 record without one — absent or `null` — is a broken record,
    and reading it as "not applicable" would quietly retire BC3 on a format that does state it. In
    `master_2026_10` the pledge lives in the per-filing XBRL, so an absent key or a JSON `null` is
    a record that carries none and BC3 is reported not applicable.

    In either era a *present* value — blank included — is a pledge the source tried to state, so it
    must parse as a 0-100 decimal or fail the parse: a blank or garbled pledge read as "absent"
    would turn a broken field into a quiet `not_applicable` and hide the breach BC3 exists to catch.
    """
    if era is FormatEra.MASTER_2026_10 and record.get(_KEY_PLEDGE) is None:
        return None
    if record.get(_KEY_PLEDGE, "") is None:
        raise ParseError(
            f"record {index}: {_KEY_PLEDGE!r} is null; the {era.value} format states a pledge",
            filename=filename,
        )
    return _pct(record, _KEY_PLEDGE, index=index, filename=filename)


def _pct(record: Mapping[str, Any], key: str, *, index: int, filename: str) -> Decimal:
    """A required percentage, exact and bounded, read from the payload's string or number."""
    if key not in record:
        raise ParseError(
            f"record {index}: no {key!r} field; present: {', '.join(sorted(record))}",
            filename=filename,
        )
    value = record[key]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ParseError(f"record {index}: {key!r} is {value}", filename=filename)
        return value
    if not isinstance(value, str):
        raise ParseError(
            f"record {index}: {key!r} is {type(value).__name__}, expected a decimal string",
            filename=filename,
        )
    literal = value.strip().replace(",", "")
    if not _DECIMAL_LITERAL.match(literal):
        raise ParseError(
            f"record {index}: {key!r} is {value!r}, which is not a plain decimal percentage",
            filename=filename,
        )
    return Decimal(literal)


def _filing_or_period_date(value: str, *, key: str, index: int, filename: str) -> date:
    """`31-Mar-2026` (optionally with a clock) → `date(2026, 3, 31)`, locale-independently."""
    match = _FILING_DATE.match(value)
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


# ── L1 ───────────────────────────────────────────────────────────────────────────────────────

#: The L1 schema, declared once and enforced on write (§4.2, M1.8's rule). `decimal128(6, 2)` holds
#: a percentage to two places (0.00-100.00); a source that started stating a third decimal would
#: fail the write loudly rather than have a holding quietly rounded into L1. `promoter_pledge_pct`
#: is nullable since D17: null is "the filing did not state it", which `bc3_status` reads back as
#: NOT_APPLICABLE — never as a zero pledge.
_L1_SCHEMA: Final = pa.schema(
    [
        pa.field("isin", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("period_end", pa.date32(), nullable=False),
        pa.field("filing_date", pa.date32(), nullable=False),
        pa.field("promoter_holding_pct", pa.decimal128(6, 2), nullable=False),
        pa.field("promoter_pledge_pct", pa.decimal128(6, 2), nullable=True),
        pa.field("public_pct", pa.decimal128(6, 2), nullable=False),
        pa.field("fii_pct", pa.decimal128(6, 2), nullable=True),
        pa.field("dii_pct", pa.decimal128(6, 2), nullable=True),
        pa.field("employee_trusts_pct", pa.decimal128(6, 2), nullable=True),
        pa.field("revised", pa.bool_(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=True),
    ]
)


def write_l1(snapshot: ShareholdingSnapshot, *, data_root: Path | None = None) -> tuple[Path, ...]:
    """Write a snapshot to L1, one partition per `filing_date`, and return the paths written.

    Partitioning by filing date rather than by quarter end is the point-in-time decision: a
    partition holds exactly the filings that became knowable on one date, so `read_pit` can answer
    an as-of question by choosing partitions instead of filtering rows (invariant #7). One master
    poll commonly carries several filing dates, so it writes several partitions.

    Idempotent per `filing_date`: rows within a partition go in `(isin, period_end)` order and the
    file is written whole to a temporary name then renamed over the target, so re-deriving from the
    same payload produces byte-identical partitions and a crash mid-write cannot leave a half file
    readable. Raw as-filed percentages only — this dataset has no adjusted analogue, so invariant #3
    has nothing to breach here.
    """
    by_filing: dict[date, list[ShareholdingRow]] = {}
    for row in snapshot.rows:
        by_filing.setdefault(row.filing_date, []).append(row)

    written: list[Path] = []
    for filing_date in sorted(by_filing):
        rows = sorted(by_filing[filing_date], key=lambda row: (row.isin, row.period_end))
        path = l1_partition_path(SHAREHOLDING_DATASET, filing_date, data_root=data_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist([_to_record(row) for row in rows], schema=_L1_SCHEMA)
        staging = path.with_name(f".{path.name}.partial")
        pq.write_table(table, staging, compression="snappy", version="2.6")
        staging.replace(path)
        written.append(path)
        _LOG.info(
            "shareholding.l1_written",
            source=snapshot.source,
            filing_date=filing_date.isoformat(),
            dataset=SHAREHOLDING_DATASET,
            path=str(path),
            rows=len(rows),
            state="NORMALIZED",
        )
    return tuple(written)


def merge_l1(snapshot: ShareholdingSnapshot, *, data_root: Path | None = None) -> tuple[Path, ...]:
    """Merge a poll into the filing-date partitions it touches, and return the paths written.

    What it does: for every filing date the snapshot carries, rewrites that partition as the union
    of what it already held and what this snapshot says — this snapshot winning for a
    company/quarter both name. The master lists only the current quarter-end's filings, so a
    partition rewritten from one poll's rows alone would drop every filing that has since left the
    list (a revision, or the whole previous quarter).
    What it assumes: snapshots are merged in poll order, so "this snapshot wins" means "the later
    poll wins".
    What it never does: move a row to a different filing date — the partition is the PIT contract.
    """
    by_filing: dict[date, dict[tuple[str, date], ShareholdingRow]] = {}
    for row in snapshot.rows:
        by_filing.setdefault(row.filing_date, {})[(row.isin, row.period_end)] = row
    written: list[Path] = []
    for filing_date, fresh in sorted(by_filing.items()):
        try:
            held = read_l1(filing_date, data_root=data_root)
        except FileNotFoundError:
            held = ()
        merged = {(row.isin, row.period_end): row for row in held}
        merged.update(fresh)
        written += write_l1(
            ShareholdingSnapshot(
                source=snapshot.source,
                l0_key=snapshot.l0_key,
                rows=tuple(sorted(merged.values(), key=lambda row: (row.isin, row.period_end))),
            ),
            data_root=data_root,
        )
    return tuple(written)


def read_l1(filing_date: date, *, data_root: Path | None = None) -> tuple[ShareholdingRow, ...]:
    """Read one filing-date partition back out of L1.

    Raises `FileNotFoundError` when the partition was never written — an absent partition is a gap
    for D7 to explain, not an empty filing date.
    """
    path = l1_partition_path(SHAREHOLDING_DATASET, filing_date, data_root=data_root)
    if not path.exists():
        raise FileNotFoundError(
            f"no {SHAREHOLDING_DATASET} partition for {filing_date.isoformat()}: {path}"
        )
    return _rows_of(path)


def read_pit(on_date: date, *, data_root: Path | None = None) -> tuple[ShareholdingRow, ...]:
    """Every shareholding row knowable on `on_date` — the point-in-time query.

    What it does: reads only the partitions whose `filing_date` is on or before `on_date`, so a
    quarter filed *after* that date is physically absent from the result. This is invariant #7 for
    this dataset: no data with `knowable_date > decision_date` reaches a decision, enforced by the
    partition layout rather than trusted to a caller's `WHERE` clause.
    What it assumes: `on_date` is the decision date in Asia/Kolkata.
    What it never does: return a filing dated after `on_date`, and never invents a partition — an
    as-of date before the earliest filing yields an empty result, not an error.

    Rows are returned in `(isin, period_end, filing_date)` order. A company that restated a quarter
    appears once per filing it made on or before `on_date`; picking the latest of those is a
    D3/quarantine concern (restated data is monitoring-only, §7), not this read's to decide.
    """
    rows: list[ShareholdingRow] = []
    for path in _partitions_through(on_date, data_root=data_root):
        rows.extend(_rows_of(path))
    rows.sort(key=lambda row: (row.isin, row.period_end, row.filing_date))
    return tuple(rows)


def _partitions_through(on_date: date, *, data_root: Path | None) -> Iterator[Path]:
    """The `part.parquet` files whose `filing_date` partition is on or before `on_date`."""
    dataset_dir = layer_root(Layer.L1, data_root=data_root) / SHAREHOLDING_DATASET
    if not dataset_dir.is_dir():
        return
    for partition_dir in sorted(dataset_dir.iterdir()):
        if not partition_dir.is_dir():
            continue
        try:
            filing_date = partition_date_of(partition_dir)
        except ValueError:
            continue
        if filing_date <= on_date:
            path = partition_dir / "part.parquet"
            if path.exists():
                yield path


#: Columns added to `_L1_SCHEMA` after the first partitions could have been written (M13.3), with
#: the value a partition that predates them means: no employee-trust figure, an original filing.
_ADDED_COLUMNS: Final[Mapping[str, Any]] = {"employee_trusts_pct": None, "revised": False}


def _rows_of(path: Path) -> tuple[ShareholdingRow, ...]:
    """Parse one L1 partition file into rows, enforcing the declared schema on read.

    A partition written before M13.3 lacks the columns `_ADDED_COLUMNS` names; they are filled with
    their pre-M13.3 meaning and the table is then cast to `_L1_SCHEMA`, so every other column is
    still checked exactly as before. Any other missing column fails the read.
    """
    table = pq.read_table(path)
    for name, default in _ADDED_COLUMNS.items():
        if name not in table.column_names:
            field = _L1_SCHEMA.field(name)
            table = table.append_column(field, pa.array([default] * table.num_rows, field.type))
    records = table.select(_L1_SCHEMA.names).cast(_L1_SCHEMA).to_pylist()
    return tuple(
        ShareholdingRow(
            isin=str(record["isin"]),
            name=str(record["name"]),
            period_end=record["period_end"],
            filing_date=record["filing_date"],
            promoter_holding_pct=record["promoter_holding_pct"],
            promoter_pledge_pct=record["promoter_pledge_pct"],
            public_pct=record["public_pct"],
            fii_pct=record["fii_pct"],
            dii_pct=record["dii_pct"],
            employee_trusts_pct=record["employee_trusts_pct"],
            revised=bool(record["revised"]),
            source=str(record["source"]),
            l0_key=None if record["l0_key"] is None else str(record["l0_key"]),
        )
        for record in records
    )


def _to_record(row: ShareholdingRow) -> dict[str, Any]:
    """One row as the dict `pa.Table.from_pylist` writes against `_L1_SCHEMA`."""
    return {
        "isin": row.isin,
        "name": row.name,
        "period_end": row.period_end,
        "filing_date": row.filing_date,
        "promoter_holding_pct": row.promoter_holding_pct,
        "promoter_pledge_pct": row.promoter_pledge_pct,
        "public_pct": row.public_pct,
        "fii_pct": row.fii_pct,
        "dii_pct": row.dii_pct,
        "employee_trusts_pct": row.employee_trusts_pct,
        "revised": row.revised,
        "source": row.source,
        "l0_key": row.l0_key,
    }


# ── quality findings: BC3 outcomes and unidentified filings on /status/quality ──────────────


def quality_findings(snapshot: ShareholdingSnapshot) -> tuple[QualityFinding, ...]:
    """The `quality_flag` rows a snapshot owes `GET /status/quality`, deterministically ordered.

    What it does: one finding per row whose BC3 is not CLEAR, and one per skipped no-ISIN record:

    * BC3 BREACH → WARN under `BC3_CHECK`, observed pledge against the 50% threshold. The data is
      fine — the *company* tripped an integrity condition — so it does not gate trading the dataset
      (only ERROR does); the analyst's T0 monitor is what acts on it.
    * BC3 NOT_APPLICABLE → INFO under `BC3_CHECK`, `detail.bc3_status = "not_applicable"`. D17's
      "explicit, never silent": the row is in L1 but its BC3 was never evaluated, and the status
      surface says so per company and filing date.
    * no ISIN → WARN under `NO_ISIN_CHECK`, with the symbol and name for the human who chases it.

    Each finding is dated by the filing date (the date the fact became knowable) and fingerprinted
    by (check, BC3 status, quarter, ISIN or symbol, filing date), so re-deriving the same polls
    raises nothing new, while a breach found after a not_applicable flag is a new finding rather
    than a duplicate of it, and two quarters filed on one date are two findings. An open
    not_applicable flag is not auto-resolved when a later poll evaluates BC3; it stays for a human.
    What it never does: write anything, or emit a finding for a CLEAR row.
    """
    findings: list[QualityFinding] = []
    for row in snapshot.rows:
        status = row.bc3_status
        if status is Bc3Status.CLEAR:
            continue
        detail: dict[str, object] = {
            "bc3_status": status.value,
            "name": row.name,
            "period_end": row.period_end.isoformat(),
            "l0_key": row.l0_key,
        }
        if status is Bc3Status.NOT_APPLICABLE:
            detail["reason"] = (
                "the shareholding payload states no promoter pledge; BC3 was not evaluated (D17)"
            )
        findings.append(
            QualityFinding(
                logical_date=row.filing_date,
                check_name=BC3_CHECK,
                severity="WARN" if status is Bc3Status.BREACH else "INFO",
                isin=row.isin,
                source=SOURCE_ID,
                observed_value=row.promoter_pledge_pct,
                threshold=PLEDGE_BREACH_PCT,
                detail=detail,
                # Status and quarter are in the key: `persist_findings` dedupes on (check_name,
                # fingerprint) whatever the severity, so a key without the status would let an open
                # INFO not_applicable flag swallow a later WARN breach for the same filing.
                fingerprint=finding_fingerprint(
                    f"{BC3_CHECK}/{status.value}/{row.period_end.isoformat()}",
                    row.isin,
                    row.filing_date,
                ),
            )
        )
    for skipped in snapshot.skipped_no_isin:
        label = skipped.symbol or skipped.name
        findings.append(
            QualityFinding(
                logical_date=skipped.filing_date,
                check_name=NO_ISIN_CHECK,
                severity="WARN",
                source=SOURCE_ID,
                detail={
                    "symbol": skipped.symbol,
                    "name": skipped.name,
                    "period_end": skipped.period_end.isoformat(),
                    "l0_key": snapshot.l0_key,
                    "reason": "the source published this filing with no ISIN; it is not in L1",
                },
                fingerprint=finding_fingerprint(
                    f"{NO_ISIN_CHECK}/{label}/{skipped.period_end.isoformat()}",
                    None,
                    skipped.filing_date,
                ),
            )
        )
    findings.sort(key=lambda f: (f.logical_date, f.check_name, f.isin or "", f.fingerprint))
    return tuple(findings)


# ── the snapshot runner ────────────────────────────────────────────────────────────────────────


class SyncTracker(Protocol):
    """The slice of the §4.4 state machine one snapshot ingestion drives (M1.3).

    A protocol rather than the concrete `SyncStateStore` so a poll can be driven end to end without
    Postgres in an offline unit test (B8) — the store satisfies it structurally. It is the whole
    happy path plus `mark_failed`: a runner that could reach `PUBLISHED` without passing through
    `VALIDATED` would be a second state machine.
    """

    def begin(self, source: str, logical_date: date) -> SyncRecord:
        """Start (or restart) an attempt for `(source, date)`, leaving the row `PENDING`."""

    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> SyncRecord:
        """The payload is in L0, with the checksum that makes later corruption detectable."""

    def mark_validated(self, source: str, logical_date: date) -> SyncRecord:
        """The payload parsed and passed its structural checks."""

    def mark_normalized(self, source: str, logical_date: date) -> SyncRecord:
        """The rows are in L1."""

    def mark_published(self, source: str, logical_date: date) -> SyncRecord:
        """Readers may see this poll — the only state the trading interlock accepts."""

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> SyncRecord:
        """Record a specific failure, and whether another attempt could ever help."""


def snapshot_url(register: SourceRegister | None = None) -> str:
    """The endpoint, read from the Source Register rather than repeated here (C.1).

    The register is where a URL is verified and where a change is recorded, so a second copy in code
    is a second thing to keep true. This row's template carries no placeholders — the master serves
    the current filings and has no date parameter.
    """
    reg = load_register() if register is None else register
    source = next((entry for entry in reg.sources if entry.id == SOURCE_ID), None)
    if source is None:
        raise IngestError(f"no {SOURCE_ID!r} entry in the Source Register")
    return source.url_template


def l0_filename(poll_date: date) -> str:
    """The L0 filename for one poll's response.

    The URL carries no date, so L0 would otherwise be handed the same filename every poll and the
    second poll of a month would collide with the first (`L0Store.put`). The poll date goes in the
    name here, which is the only place it can.
    """
    return f"corporate-share-holdings-master_{poll_date:%Y%m%d}.json"


def ingest_snapshot(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SyncTracker,
    poll_date: date,
    data_root: Path | None = None,
    register: SourceRegister | None = None,
) -> ShareholdingSnapshot:
    """Take one poll from nothing to `PUBLISHED`: fetch → L0 → parse → L1 → sync_state.

    What it does: drives the §4.4 transitions in order around the fetch and the writes, so a
    partially-ingested poll is visible as the state it actually reached rather than as an absence.
    `poll_date` is the date we polled the master — the logical date the L0 payload files under — not
    a quarter end; each row carries its own `period_end` and `filing_date` from the payload.
    What it never does: fabricate a state it did not reach. Any failure is recorded on the row —
    with `retryable` set from what actually went wrong — and then re-raised, so the caller sees the
    exception and `/status/sync` sees the state.
    """
    url = snapshot_url(register)
    tracker.begin(SOURCE_ID, poll_date)
    try:
        ref = fetcher.fetch(SOURCE_ID, url, poll_date, filename=l0_filename(poll_date))
        tracker.mark_fetched(SOURCE_ID, poll_date, checksum=ref.sha256, l0_path=ref.key)

        snapshot = parse_l0(l0, ref)
        tracker.mark_validated(SOURCE_ID, poll_date)

        write_l1(snapshot, data_root=data_root)
        tracker.mark_normalized(SOURCE_ID, poll_date)

        tracker.mark_published(SOURCE_ID, poll_date)
    except Exception as exc:
        # Recorded, then re-raised: a failure that is not on the row is a failure the status API
        # cannot see, and one that is only on the row is a failure the caller cannot handle.
        tracker.mark_failed(
            SOURCE_ID, poll_date, f"{type(exc).__name__}: {exc}", retryable=_retryable(exc)
        )
        _LOG.error(
            "shareholding.ingest_failed",
            source=SOURCE_ID,
            poll_date=poll_date.isoformat(),
            error=f"{type(exc).__name__}: {exc}",
            retryable=_retryable(exc),
            state="FAILED",
        )
        raise

    _LOG.info(
        "shareholding.published",
        source=SOURCE_ID,
        poll_date=poll_date.isoformat(),
        rows=len(snapshot.rows),
        state="PUBLISHED",
    )
    return snapshot


def _retryable(exc: BaseException) -> bool:
    """Whether repeating this attempt later could ever produce a different outcome (§4.4).

    A 403 spike is the hard stop — nothing this process does next can help, and re-driving would be
    the "work around the block" AGENTIC_CONTEXT §8 forbids. A transient network error is worth a
    retry; a format change, a 404 or a refusal is not without a human.
    """
    if isinstance(exc, ForbiddenSpikeError):
        return False
    if isinstance(exc, RetryableFetchError):
        return True
    return not isinstance(exc, ParseError | FetchHTTPError)
