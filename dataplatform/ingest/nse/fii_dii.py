"""NSE FII/DII cash-market flows (§4.1 row 9) — `/api/fiidiiTradeReact` → L1 `fii_dii_flows`.

Two numbers per session per side: what foreign and domestic institutions bought and sold in the
NSE cash segment, in ₹ crore. The agent's rotation and T2 review read them as a regime signal
(§5.4), which is the whole reason they are worth a dataset of their own.

**This source has no history, and that is the important fact about it.** The endpoint carries no
date parameter and serves only the latest session; M3.4 measured that directly on 2026-08-08 by
requesting it with a `?date=` for an earlier session and getting a byte-identical payload back
(same sha256), and by probing the archive host for a dated cash-segment equivalent, which 404s.
So the M3 gate's "flows queryable 10 years back" is **not** achievable from here: depth is one
session, and history accrues forward from the first daily capture. The measurement is recorded in
`source_register.yaml` under this row's `history:` block rather than asserted here, and the
NSDL/CDSL monthly series stays the noted fallback for anything that needs the past (it is
FPI-only — no DII — so it is a fallback, not a substitute). A missed day is a permanent hole:
nothing can re-fetch it, which is why `ingest_day` refuses to file one session's numbers under
another session's date.

Shape decisions worth their reason:

* **Money is `Decimal`, in the unit the exchange published.** The feed states ₹ crore to two
  decimals as JSON *strings*; `json.loads(parse_float=Decimal)` means a numeric field cannot
  become a float on the way in either. L1 holds the crore figures exactly as published, and
  `FlowRow.buy_value_inr` converts to rupees exactly (times 10^7) for a caller that wants them.
* **`category` is normalized, `raw_category` is kept.** The feed says `FII/FPI`; consumers want a
  stable `FII`. Keeping the source string means a wording change is visible in L1 rather than
  silently absorbed by the alias table.
* **Both sides must be present.** A payload with FII and no DII is a broken session, not half a
  dataset, and it fails loud (`ParseError`) rather than landing a partial day in L1.
* **`net = buy - sell` is checked.** It has held exactly on every observed payload; a breach
  beyond one unit in the last published place is a defect in the feed and stops the day.
* **No ISIN, and none is missing.** These are market aggregates, not per-security rows, so
  invariant #2 has nothing to bind here — there is no symbol to accidentally join on.

Offline by construction: this module takes bytes (or an `L0Ref` it reads back through `L0Store`).
`ingest_day` is the one function that drives a fetch, and it does so through the crawl engine,
which is still the only thing in the platform that opens a socket.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
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
from dataplatform.ingest.models import IngestError, ParseError
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncRecord
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.paths import l1_partition_path

__all__ = [
    "CATEGORY_ALIASES",
    "FLOWS_DATASET",
    "NET_TOLERANCE_INR_CRORE",
    "RUPEES_PER_CRORE",
    "SOURCE_ID",
    "FlowCategory",
    "FlowDay",
    "FlowRow",
    "StaleSessionError",
    "SyncTracker",
    "flows_url",
    "ingest_day",
    "l0_filename",
    "parse",
    "parse_l0",
    "read_l1",
    "write_l1",
]

_LOG = get_logger(__name__)

#: The register id this parser serves (`source_register.yaml`, `parser.task: M3.4`).
SOURCE_ID: Final = "nse_fii_dii_flows"

#: The L1 dataset name — `data/L1/fii_dii_flows/date=YYYY-MM-DD/part.parquet` (§4.2).
FLOWS_DATASET: Final = "fii_dii_flows"

#: Exact, by definition: one crore is 10⁷. `Decimal` so the conversion cannot lose a paisa.
RUPEES_PER_CRORE: Final = Decimal(10) ** 7

#: How far `netValue` may sit from `buyValue - sellValue` before the day is rejected. Exactly one
#: unit in the last published place: the feed states two decimals, so a genuine rounding artefact
#: cannot exceed this, and anything that does is the feed contradicting itself.
NET_TOLERANCE_INR_CRORE: Final = Decimal("0.01")

#: Month abbreviations as the feed spells them (`07-Aug-2026`). Spelled out rather than handed to
#: `strptime("%b")`, which reads `LC_TIME`: a host with a non-English locale would otherwise fail
#: to parse a date that is not locale-dependent at all.
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

_SESSION_DATE = re.compile(r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s*$")

#: A plain decimal literal, optionally signed, with optional thousands separators. Checked before
#: `Decimal()` sees the text because `Decimal` itself accepts `NaN` and `Infinity`, and a
#: mis-framed field that spells one of those must not become a flow that compares greater than
#: every other flow.
_DECIMAL_LITERAL = re.compile(r"^[+-]?\d+(\.\d+)?$")


class FlowCategory(StrEnum):
    """The two sides this dataset reports. Normalized, so consumers never match on feed wording."""

    FII = "FII"
    """Foreign institutional / portfolio investors — the feed's `FII/FPI`."""

    DII = "DII"
    """Domestic institutional investors."""


#: Feed spellings → normalized category. Only `FII/FPI` and `DII` have actually been observed;
#: the other two are the obvious near-spellings and are accepted so a cosmetic change on the feed
#: does not stop ingestion. Anything else is a `ParseError` — a new category is a schema change
#: and must be looked at, not silently bucketed.
CATEGORY_ALIASES: Final[Mapping[str, FlowCategory]] = {
    "FII/FPI": FlowCategory.FII,
    "FII": FlowCategory.FII,
    "FPI": FlowCategory.FII,
    "DII": FlowCategory.DII,
}

#: A gross traded value in ₹ crore. Strict, so a float cannot be constructed into one; `ge=0`
#: because a gross purchase or sale is a turnover, and a negative one is a parse failure rather
#: than a small number.
GrossCrore = Annotated[Decimal, Field(ge=0, strict=True, allow_inf_nan=False)]

#: A net flow in ₹ crore. Signed — institutions sell — and otherwise as strict as the gross side.
NetCrore = Annotated[Decimal, Field(strict=True, allow_inf_nan=False)]


class StaleSessionError(IngestError):
    """The endpoint served a different session than the one being ingested.

    The distinguishing failure of a rolling "latest" source: NSE publishes this feed some minutes
    after the close, so a run that starts too early gets *yesterday's* numbers with today's
    request. Filing those under today would be a fabricated row that no later fetch could correct
    (this endpoint has no history to re-derive from), so the day fails and is retried instead.
    """

    def __init__(self, *, expected: date, served: date) -> None:
        self.expected = expected
        self.served = served
        super().__init__(
            f"{SOURCE_ID}: asked for {expected.isoformat()} but the endpoint served "
            f"{served.isoformat()}; this feed has no date parameter, so the session it publishes "
            "is whatever it publishes — retry after the exchange updates it, never rewrite the date"
        )


class FlowRow(BaseModel):
    """One side's institutional flow for one session, exactly as the exchange published it.

    What it does: carry the buy, sell and net turnover of FII or DII for a session, in ₹ crore.
    What it assumes: the parser already checked that the payload is a whole, self-consistent day.
    What it never does: hold a derived aggregate, a currency conversion, or a value the feed did
    not state. `buy_value_inr` and friends are exact 10^7 views, computed on read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date = Field(description="the exchange session this row is about (Asia/Kolkata)")
    category: FlowCategory = Field(description="normalized side: FII or DII")
    raw_category: str = Field(
        min_length=1, description="the feed's own wording, kept so a change is visible in L1"
    )
    buy_value_inr_crore: GrossCrore = Field(description="gross purchases in ₹ crore (buyValue)")
    sell_value_inr_crore: GrossCrore = Field(description="gross sales in ₹ crore (sellValue)")
    net_value_inr_crore: NetCrore = Field(description="net, signed, in ₹ crore (netValue)")

    @property
    def buy_value_inr(self) -> Decimal:
        """Gross purchases in rupees. Exact — one crore is 10⁷, so nothing is rounded."""
        return self.buy_value_inr_crore * RUPEES_PER_CRORE

    @property
    def sell_value_inr(self) -> Decimal:
        """Gross sales in rupees, exact."""
        return self.sell_value_inr_crore * RUPEES_PER_CRORE

    @property
    def net_value_inr(self) -> Decimal:
        """Net flow in rupees, exact and signed. Negative means the side was a net seller."""
        return self.net_value_inr_crore * RUPEES_PER_CRORE

    @model_validator(mode="after")
    def _net_matches_gross(self) -> FlowRow:
        """`net` must be `buy - sell` to the last published digit.

        The one arithmetic the feed states twice, so it is the one check that can catch a
        transposed or mis-scaled field. A row whose net disagrees with its own gross legs is not a
        row to fix silently — it is a broken publication.
        """
        implied = self.buy_value_inr_crore - self.sell_value_inr_crore
        if abs(self.net_value_inr_crore - implied) > NET_TOLERANCE_INR_CRORE:
            raise ValueError(
                f"{self.category.value} {self.trade_date.isoformat()}: netValue "
                f"{self.net_value_inr_crore} disagrees with buyValue - sellValue = {implied} "
                f"by more than {NET_TOLERANCE_INR_CRORE} crore"
            )
        return self


class FlowDay(BaseModel):
    """One session's complete FII/DII picture — both sides, or it is not a day.

    What it does: hold the rows of a single session together with the L0 payload they came from,
    so an L1 partition can name its lineage.
    What it assumes: both institutional sides are reported for every session the exchange trades.
    What it never does: exist half-populated. A missing side, a duplicate side or two dates in one
    payload all raise here, which means a `FlowDay` that exists is a day that can be written.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date = Field(description="the exchange session all rows are about")
    source: str = Field(min_length=1, description="Source Register id the payload came from")
    l0_key: str | None = Field(
        default=None, description="`source/date/filename` of the L0 payload this was derived from"
    )
    rows: tuple[FlowRow, ...] = Field(description="one row per category, category-ordered")

    @model_validator(mode="after")
    def _complete_and_consistent(self) -> FlowDay:
        """Both categories, exactly once each, all on this day's date."""
        seen = [row.category for row in self.rows]
        missing = sorted(set(FlowCategory) - set(seen))
        if missing:
            raise ValueError(
                f"{self.trade_date.isoformat()}: no {', '.join(c.value for c in missing)} row; "
                "a session reports both institutional sides or it is not a complete day"
            )
        if len(seen) != len(set(seen)):
            raise ValueError(f"{self.trade_date.isoformat()}: duplicate category in {seen}")
        wrong = [row for row in self.rows if row.trade_date != self.trade_date]
        if wrong:
            raise ValueError(
                f"{self.trade_date.isoformat()}: row dated "
                f"{wrong[0].trade_date.isoformat()} in this day's payload"
            )
        return self

    def category(self, category: FlowCategory) -> FlowRow:
        """The row for one side. Raises `KeyError` if absent, which validation makes impossible."""
        for row in self.rows:
            if row.category is category:
                return row
        raise KeyError(f"no {category.value} row for {self.trade_date.isoformat()}")


# ── parsing ──────────────────────────────────────────────────────────────────────────────────


def parse(payload: bytes, *, filename: str, l0_key: str | None = None) -> FlowDay:
    """Parse one `fiidiiTradeReact` response into a complete session.

    Assumes `payload` is one whole response — a 215-byte JSON array, so holding it whole is free
    and a truncation is detectable rather than streamed past. `filename` names the file in errors
    and logs; the session date comes from the payload's own `date` field and never from the
    filename, because this endpoint's filename is one we chose and the payload's date is the fact.

    Raises `ParseError`, naming the file, for anything that is not this format: an HTML soft-404
    (three of the platform's nine hosts answer unknown paths with 200 + markup), a non-array body,
    a missing or unknown field, a value that is not a plain decimal, a category nobody has seen, a
    payload carrying two dates, or a day missing one of its two sides.

    Never returns a partial day, and never repairs one.
    """
    text = _decode(payload, filename=filename)
    records = _records(text, filename=filename)
    rows = tuple(
        _row(record, index=index, filename=filename) for index, record in enumerate(records)
    )
    dates = {row.trade_date for row in rows}
    if len(dates) != 1:
        raise ParseError(
            f"payload carries {len(dates)} session dates "
            f"({', '.join(sorted(d.isoformat() for d in dates))}); one response is one session",
            filename=filename,
        )
    try:
        day = FlowDay(
            trade_date=next(iter(dates)),
            source=SOURCE_ID,
            l0_key=l0_key,
            rows=tuple(sorted(rows, key=lambda row: row.category.value)),
        )
    except ValidationError as exc:
        raise ParseError(str(exc), filename=filename) from exc

    _LOG.info(
        "fii_dii.parsed",
        source=SOURCE_ID,
        trade_date=day.trade_date.isoformat(),
        filename=filename,
        rows=len(day.rows),
        state="VALIDATED",
    )
    return day


def parse_l0(store: L0Store, ref: L0Ref) -> FlowDay:
    """Parse the L0 payload a fetch produced, re-verifying its checksum on the way in.

    The pipeline's entry point: `Fetcher.fetch` returns an `L0Ref` and never bytes, so this is how
    a fetched response becomes a session. `L0Store.get` re-hashes the payload, which makes "every
    L1 value derives from bytes that have not changed" true where the derivation happens.
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
            "body is markup, not JSON — an HTML error page answered with a 200 "
            "(ops/gates/source-verification.md §5.1); it must not become a flows row",
            filename=filename,
        )
    return text


def _records(text: str, *, filename: str) -> list[Mapping[str, Any]]:
    """The JSON array of category records, with numbers kept out of `float` on the way in."""
    try:
        # `parse_float`/`parse_int` are the money guard: today the feed quotes strings, and a
        # switch to bare JSON numbers must not silently become binary floating point.
        document = json.loads(text, parse_float=Decimal, parse_int=Decimal)
    except json.JSONDecodeError as exc:
        raise ParseError(f"body is not valid JSON: {exc}", filename=filename) from exc
    if not isinstance(document, list):
        raise ParseError(
            f"expected a JSON array of category records, got {type(document).__name__}",
            filename=filename,
        )
    if not document:
        raise ParseError("JSON array is empty; a session reports FII and DII", filename=filename)
    for index, record in enumerate(document):
        if not isinstance(record, dict):
            raise ParseError(
                f"record {index} is {type(record).__name__}, not an object", filename=filename
            )
    return list(document)


def _row(record: Mapping[str, Any], *, index: int, filename: str) -> FlowRow:
    """One feed record → one validated `FlowRow`."""
    raw_category = _text(record, "category", index=index, filename=filename)
    category = CATEGORY_ALIASES.get(raw_category.strip().upper())
    if category is None:
        raise ParseError(
            f"record {index}: unknown category {raw_category!r}; known: "
            f"{', '.join(sorted(CATEGORY_ALIASES))}. A new category is a schema change",
            filename=filename,
        )
    try:
        return FlowRow(
            trade_date=_session_date(
                _text(record, "date", index=index, filename=filename),
                index=index,
                filename=filename,
            ),
            category=category,
            raw_category=raw_category,
            buy_value_inr_crore=_amount(record, "buyValue", index=index, filename=filename),
            sell_value_inr_crore=_amount(record, "sellValue", index=index, filename=filename),
            net_value_inr_crore=_amount(record, "netValue", index=index, filename=filename),
        )
    except ValidationError as exc:
        raise ParseError(f"record {index}: {exc}", filename=filename) from exc


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
            f"record {index}: {key!r} is {value!r}, expected a non-empty string",
            filename=filename,
        )
    return value.strip()


def _amount(record: Mapping[str, Any], key: str, *, index: int, filename: str) -> Decimal:
    """A ₹ crore amount, exact.

    Accepts the string the feed quotes today and the bare JSON number it might quote tomorrow —
    which `json.loads(parse_float=Decimal)` has already kept out of `float`. Thousands separators
    are tolerated because Indian financial feeds add them without notice; anything else, including
    `NaN` and `Infinity`, is a parse failure.
    """
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
            f"record {index}: {key!r} is {value!r}, which is not a plain decimal amount",
            filename=filename,
        )
    try:
        return Decimal(literal)
    except InvalidOperation as exc:  # pragma: no cover - the regex already excludes these
        raise ParseError(f"record {index}: {key!r} is {value!r}", filename=filename) from exc


def _session_date(value: str, *, index: int, filename: str) -> date:
    """`07-Aug-2026` → `date(2026, 8, 7)`, locale-independently."""
    match = _SESSION_DATE.match(value)
    if match is None:
        raise ParseError(f"record {index}: date {value!r} is not DD-Mon-YYYY", filename=filename)
    day, month_name, year = match.groups()
    month = _MONTHS.get(month_name.upper())
    if month is None:
        raise ParseError(
            f"record {index}: date {value!r} names no month we know", filename=filename
        )
    try:
        return date(int(year), month, int(day))
    except ValueError as exc:
        raise ParseError(
            f"record {index}: date {value!r} is not a real date", filename=filename
        ) from exc


# ── L1 ───────────────────────────────────────────────────────────────────────────────────────

#: The L1 schema, declared once and enforced on write (§4.2, M1.8's rule). `decimal128(20, 2)`
#: mirrors what the exchange publishes: a feed that started stating a third decimal would fail the
#: write loudly rather than have a money value quietly rounded into L1.
_L1_SCHEMA: Final = pa.schema(
    [
        pa.field("trade_date", pa.date32(), nullable=False),
        pa.field("category", pa.string(), nullable=False),
        pa.field("raw_category", pa.string(), nullable=False),
        pa.field("buy_value_inr_crore", pa.decimal128(20, 2), nullable=False),
        pa.field("sell_value_inr_crore", pa.decimal128(20, 2), nullable=False),
        pa.field("net_value_inr_crore", pa.decimal128(20, 2), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=True),
    ]
)


def write_l1(day: FlowDay, *, data_root: Path | None = None) -> Path:
    """Write one session to its L1 partition and return the file's path.

    Idempotent per `(dataset, date)`: rows go in category order and the file is written whole to a
    temporary name and then renamed over the target, so re-deriving a session from L0 produces the
    same bytes and a crash mid-write cannot leave a half partition readable.

    Raw values only — this dataset has no adjusted analogue, so invariant #3 has nothing to breach
    here, and the write carries the `l0_key` so every row can name the payload it came from.
    """
    path = l1_partition_path(FLOWS_DATASET, day.trade_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [
            {
                "trade_date": row.trade_date,
                "category": row.category.value,
                "raw_category": row.raw_category,
                "buy_value_inr_crore": row.buy_value_inr_crore,
                "sell_value_inr_crore": row.sell_value_inr_crore,
                "net_value_inr_crore": row.net_value_inr_crore,
                "source": day.source,
                "l0_key": day.l0_key,
            }
            for row in day.rows
        ],
        schema=_L1_SCHEMA,
    )
    staging = path.with_name(f".{path.name}.partial")
    pq.write_table(table, staging, compression="snappy", version="2.6")
    staging.replace(path)
    _LOG.info(
        "fii_dii.l1_written",
        source=day.source,
        trade_date=day.trade_date.isoformat(),
        dataset=FLOWS_DATASET,
        path=str(path),
        rows=len(day.rows),
        l0_key=day.l0_key,
        state="NORMALIZED",
    )
    return path


def read_l1(trade_date: date, *, data_root: Path | None = None) -> FlowDay:
    """Read one session back out of L1.

    The round trip `write_l1` is verified against, and the read path a query-layer flows join will
    use until D4 generalizes it. Raises `FileNotFoundError` when the partition was never written —
    an absent partition is a gap for D7 to explain, not an empty day.
    """
    path = l1_partition_path(FLOWS_DATASET, trade_date, data_root=data_root)
    if not path.exists():
        raise FileNotFoundError(
            f"no {FLOWS_DATASET} partition for {trade_date.isoformat()}: {path}"
        )
    records = pq.read_table(path, schema=_L1_SCHEMA).to_pylist()
    return FlowDay(
        trade_date=trade_date,
        source=str(records[0]["source"]),
        l0_key=records[0]["l0_key"],
        rows=tuple(
            FlowRow(
                trade_date=record["trade_date"],
                category=FlowCategory(record["category"]),
                raw_category=record["raw_category"],
                buy_value_inr_crore=record["buy_value_inr_crore"],
                sell_value_inr_crore=record["sell_value_inr_crore"],
                net_value_inr_crore=record["net_value_inr_crore"],
            )
            for record in records
        ),
    )


# ── the day runner ───────────────────────────────────────────────────────────────────────────


class SyncTracker(Protocol):
    """The slice of the §4.4 state machine one day's ingestion drives (M1.3).

    A protocol rather than the concrete `SyncStateStore` so a session can be driven end to end
    without Postgres in an offline unit test (B8) — the store satisfies it structurally, which
    `tests/unit/test_fii_dii.py` asserts statically. It is deliberately the *whole* happy path
    plus `mark_failed`: a runner that could reach `PUBLISHED` without passing through `VALIDATED`
    would be a second state machine.
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
        """Readers may see this date — the only state the trading interlock accepts."""

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> SyncRecord:
        """Record a specific failure, and whether another attempt could ever help."""


def flows_url(register: SourceRegister | None = None) -> str:
    """The endpoint, read from the Source Register rather than repeated here.

    The register is where a URL is verified and where a change is recorded (C.1), so a second copy
    in code is a second thing to keep true. This row's template carries no placeholders — the feed
    has no date parameter, which is the whole reason its history is one session deep.
    """
    reg = load_register() if register is None else register
    source = next((entry for entry in reg.sources if entry.id == SOURCE_ID), None)
    if source is None:
        raise IngestError(f"no {SOURCE_ID!r} entry in the Source Register")
    if "{" in source.url_template:
        raise IngestError(
            f"{SOURCE_ID}: url_template {source.url_template!r} now carries a placeholder; "
            "if this feed has gained a date parameter, re-measure its history depth before using it"
        )
    return source.url_template


def l0_filename(trade_date: date) -> str:
    """The L0 filename for one session's response.

    The URL carries no date, so L0 would otherwise be handed the same filename every day and the
    second session of a month would collide with the first (`L0Store.put`). The date goes in the
    name here, which is the only place it can.
    """
    return f"fiidiiTradeReact_{trade_date:%Y%m%d}.json"


def ingest_day(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SyncTracker,
    trade_date: date,
    data_root: Path | None = None,
    register: SourceRegister | None = None,
) -> FlowDay:
    """Take one session from nothing to `PUBLISHED`: fetch → L0 → parse → L1 → sync_state.

    What it does: drives the §4.4 transitions in order around the fetch and the write, so a
    partially-ingested day is visible as the state it actually reached rather than as an absence.
    What it assumes: `trade_date` is a session the exchange traded — the caller (C.2's calendar,
    and M1.10's daily job) decides that; a closed day is a `GAP`, which is the state machine's
    business and not this function's.
    What it never does: file a payload under a date the payload does not claim. The endpoint
    serves whatever session it currently has, so the served date is checked against the requested
    one and a mismatch is a retryable failure (`StaleSessionError`), never a rewritten date.

    Any failure is recorded on the row — with `retryable` set from what actually went wrong — and
    then re-raised, so the caller sees the exception and `/status/sync` sees the state.
    """
    url = flows_url(register)
    tracker.begin(SOURCE_ID, trade_date)
    try:
        ref = fetcher.fetch(SOURCE_ID, url, trade_date, filename=l0_filename(trade_date))
        tracker.mark_fetched(SOURCE_ID, trade_date, checksum=ref.sha256, l0_path=ref.key)

        day = parse_l0(l0, ref)
        if day.trade_date != trade_date:
            raise StaleSessionError(expected=trade_date, served=day.trade_date)
        tracker.mark_validated(SOURCE_ID, trade_date)

        write_l1(day, data_root=data_root)
        tracker.mark_normalized(SOURCE_ID, trade_date)

        tracker.mark_published(SOURCE_ID, trade_date)
    except Exception as exc:
        # Recorded, then re-raised: a failure that is not on the row is a failure the status API
        # cannot see, and one that is only on the row is a failure the caller cannot handle.
        tracker.mark_failed(
            SOURCE_ID, trade_date, f"{type(exc).__name__}: {exc}", retryable=_retryable(exc)
        )
        _LOG.error(
            "fii_dii.ingest_failed",
            source=SOURCE_ID,
            trade_date=trade_date.isoformat(),
            error=f"{type(exc).__name__}: {exc}",
            retryable=_retryable(exc),
            state="FAILED",
        )
        raise

    _LOG.info(
        "fii_dii.published",
        source=SOURCE_ID,
        trade_date=trade_date.isoformat(),
        l0_key=day.l0_key,
        state="PUBLISHED",
    )
    return day


def _retryable(exc: BaseException) -> bool:
    """Whether repeating this attempt later could ever produce a different outcome.

    The distinction §4.4 rests on. A stale session or an exhausted retry budget is "not yet"; a
    format change, a 404 or a refusal is "not without a human", and marking those retryable is how
    a backfill turns into a hot loop against a source that is telling it to stop.
    """
    if isinstance(exc, ForbiddenSpikeError):
        # The hard stop. Nothing this process does next can help, and re-driving the date would
        # be the exact "work around the block" AGENTIC_CONTEXT §8 forbids.
        return False
    if isinstance(exc, StaleSessionError | RetryableFetchError):
        return True
    return not isinstance(exc, ParseError | FetchHTTPError)
