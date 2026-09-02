"""NSE quarterly shareholding-pattern filings (§4.1 row 11) → L1 `shareholding`.

Every listed company files a shareholding pattern with the exchange each quarter: how much of it
the promoter and promoter group hold, how much of *that* promoter holding is pledged (encumbered),
and how the rest splits across FII, DII and the wider public. Two of those numbers are load-bearing
for the analyst:

* **The promoter pledge is a break condition, not a footnote.** §5.3's BC3 fires on "promoter
  pledge >50%" — an integrity break that takes a thesis straight to a T0 flag and an immediate T1
  review. A schema that buried pledge inside a notes blob would make BC3 un-evaluatable, so it is a
  first-class, range-checked `Decimal` field here (`promoter_pledge_pct`) with the threshold spelled
  out (`PLEDGE_BREACH_PCT`) and a `breaches_bc3` predicate the monitor can call directly.
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

Money-shaped rules that carry over from the rest of D1: percentages are `Decimal`, never `float`
(a float pledge that reads 49.999999 would silently duck a >50 break); a value is read as text and
converted exactly, and `NaN`/`Infinity` are parse failures, not percentages that compare greater
than a hundred. Identity is ISIN (invariant #2): the payload carries it natively, so nothing here
joins on a symbol.

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
from dataplatform.status.sync_state import SyncRecord
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.paths import Layer, l1_partition_path, layer_root, partition_date_of

__all__ = [
    "PLEDGE_BREACH_PCT",
    "SHAREHOLDING_DATASET",
    "SOURCE_ID",
    "ShareholdingRow",
    "ShareholdingSnapshot",
    "SyncTracker",
    "ingest_snapshot",
    "l0_filename",
    "parse",
    "parse_l0",
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
_KEY_PUBLIC: Final = "public_prcnt"
_KEY_FII: Final = "fii_prcnt"
_KEY_DII: Final = "dii_prcnt"

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
    promoter_pledge_pct: Pct = Field(
        description="promoter shares pledged/encumbered, % of promoter holding — §5.3 BC3 input"
    )
    public_pct: Pct = Field(description="public shareholding, % of capital")
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
    def breaches_bc3(self) -> bool:
        """Whether the promoter pledge crosses §5.3's BC3 integrity threshold (>50%)."""
        return self.promoter_pledge_pct > PLEDGE_BREACH_PCT

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
        return tuple(row for row in self.rows if row.breaches_bc3)


# ── parsing ──────────────────────────────────────────────────────────────────────────────────


def parse(payload: bytes, *, filename: str, l0_key: str | None = None) -> ShareholdingSnapshot:
    """Parse one `corporate-share-holdings-master` response into a snapshot of filings.

    Assumes `payload` is one whole JSON response — a JSON array of per-company records. `filename`
    names the file in errors and logs. Each record's quarter end comes from its `date` field and its
    filing date from `broadcastDate` (or `cgTimeStamp`): the two are read independently, so neither
    can be inferred from the other (acceptance 1).

    Raises `ParseError`, naming the file, for anything that is not this format: an HTML soft-404, a
    non-array body, a record missing a required field, a percentage that is not a plain decimal or
    that sits outside 0-100, a filing that does not fall after its quarter, or a company listed
    twice for one quarter. Never returns a partial or repaired snapshot.
    """
    text = _decode(payload, filename=filename)
    records = _records(text, filename=filename)
    rows = tuple(
        _row(record, index=index, l0_key=l0_key, filename=filename)
        for index, record in enumerate(records)
    )
    try:
        snapshot = ShareholdingSnapshot(
            source=SOURCE_ID,
            l0_key=l0_key,
            rows=tuple(sorted(rows, key=lambda row: (row.isin, row.period_end))),
        )
    except ValidationError as exc:
        raise ParseError(str(exc), filename=filename) from exc

    _LOG.info(
        "shareholding.parsed",
        source=SOURCE_ID,
        filename=filename,
        rows=len(snapshot.rows),
        breaching=len(snapshot.breaching()),
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


def _row(
    record: Mapping[str, Any], *, index: int, l0_key: str | None, filename: str
) -> ShareholdingRow:
    """One feed record → one validated `ShareholdingRow`."""
    try:
        return ShareholdingRow(
            isin=_text(record, _KEY_ISIN, index=index, filename=filename),
            name=_text(record, _KEY_NAME, index=index, filename=filename),
            period_end=_filing_or_period_date(
                _text(record, _KEY_PERIOD_END, index=index, filename=filename),
                key=_KEY_PERIOD_END,
                index=index,
                filename=filename,
            ),
            filing_date=_filing_or_period_date(
                _filing_field(record, index=index, filename=filename),
                key="/".join(_KEY_FILING),
                index=index,
                filename=filename,
            ),
            promoter_holding_pct=_pct(record, _KEY_PROMOTER, index=index, filename=filename),
            promoter_pledge_pct=_pct(record, _KEY_PLEDGE, index=index, filename=filename),
            public_pct=_pct(record, _KEY_PUBLIC, index=index, filename=filename),
            fii_pct=_optional_pct(record, _KEY_FII, index=index, filename=filename),
            dii_pct=_optional_pct(record, _KEY_DII, index=index, filename=filename),
            source=SOURCE_ID,
            l0_key=l0_key,
        )
    except ValidationError as exc:
        raise ParseError(f"record {index}: {exc}", filename=filename) from exc


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
#: fail the write loudly rather than have a holding quietly rounded into L1.
_L1_SCHEMA: Final = pa.schema(
    [
        pa.field("isin", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("period_end", pa.date32(), nullable=False),
        pa.field("filing_date", pa.date32(), nullable=False),
        pa.field("promoter_holding_pct", pa.decimal128(6, 2), nullable=False),
        pa.field("promoter_pledge_pct", pa.decimal128(6, 2), nullable=False),
        pa.field("public_pct", pa.decimal128(6, 2), nullable=False),
        pa.field("fii_pct", pa.decimal128(6, 2), nullable=True),
        pa.field("dii_pct", pa.decimal128(6, 2), nullable=True),
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


def _rows_of(path: Path) -> tuple[ShareholdingRow, ...]:
    """Parse one L1 partition file into rows, enforcing the declared schema on read."""
    records = pq.read_table(path, schema=_L1_SCHEMA).to_pylist()
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
        "source": row.source,
        "l0_key": row.l0_key,
    }


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
