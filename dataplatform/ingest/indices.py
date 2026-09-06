"""Index constituents history + benchmark TRI (§4.1 rows 7 and 8) — M3.9.

Two datasets ride together in this module because they answer the same question from opposite
ends: *what was the market, and how did it do, on a historical date the analyst is reasoning
about.*

**Constituents — the survivorship-bias killer.** niftyindices publishes one CSV per index
(`ind_nifty50list.csv`, the sectoral and thematic lists) that is *always "as of today"* — there is
no historical constituents download anywhere (the register's `pit_notes` for
`nifty_index_constituents` records this after a sweep). So the history has to be *made*: snapshot
the list every month from day one and never overwrite a prior month's membership, and the accrued
snapshots become the point-in-time universe M4 needs. A backtest that screened today's NIFTY 50 over
the last decade would reason about companies *because* they survived into the index — the exact
look-ahead §8.3.6 and invariant #7 exist to forbid. `membership_asof(index, on_date)` answers "who
was in this index then" by returning the snapshot in force on that date, and a date before the first
snapshot returns nothing rather than today's list.

Immutability is enforced, not hoped for. `write_constituents_l1` refuses to overwrite an existing
month's snapshot with different membership (`ImmutableSnapshotError`); re-deriving that month from
the same bytes is a no-op. "Never overwrite a prior month's membership" is thereby a property of the
writer, not a convention a caller must remember.

**Benchmark TRI — the return the strategy is measured against.** The plan wants a total-return index
(price appreciation *plus* reinvested dividends) as the benchmark (§5.2, "NIFTY-TRI + theme proxy").
The direct historical-TRI endpoint (`getTotalReturnIndexString`) is behind an application-level
session gate that our fetcher cannot open without a real browser handshake — the register marks
`nifty_tri_history` **FAILED** with the evidence, and §4.1 anticipated exactly this by naming a
fallback: *"Computed price-index proxy + dividend estimate."* Both paths live here:

* `parse_tri_native` reads a `getTotalReturnIndexString` response, so the moment the session gate is
  solved a real published TRI series ingests unchanged. It is exercised against a format fixture; it
  is not the source of the spot-checked value, because we could not fetch a real one.
* `compute_tri` is §4.1's fallback: it chains a total-return series off the price index and the
  published `Div Yield` column of the daily close-all snapshot (`nifty_index_close_snapshot`, which
  *is* VERIFIED). The series is **seeded to the published closing index value** on its first date,
  its anchor is a published number (acceptance 3's "spot-checked against a published value"), and
  each day adds the price return *plus* a dividend accrual estimated from the yield. Zero dividends
  make it reproduce the price index exactly; a positive yield makes it exceed the price return by
  accrued amount — the property a test asserts so an inverted sign fails loudly. It is explicitly an
  estimate (a constant-yield daily accrual, not a dividend-event ledger), which is why it is tagged
  `computed_price_plus_div` on every point and never presented as the exchange's own TRI.

Conventions inherited from the rest of D1: prices, index values and yields are `Decimal`, never
`float` (a float benchmark would put float error straight into every XIRR-vs-benchmark comparison);
ISIN is the only join key and constituents carry it natively, so nothing here joins on a symbol
(invariant #2); the clock is injected (B10); and the module is offline by construction — it takes
bytes, or an `L0Ref` it reads back through `L0Store`, and the crawl engine is the only thing that
opens a socket.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
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
from dataplatform.ingest.source_register import Source, SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncRecord
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.paths import Layer, l1_partition_path, layer_root, partition_date_of

__all__ = [
    "CLOSE_SNAPSHOT_SOURCE_ID",
    "CONSTITUENTS_DATASET",
    "CONSTITUENTS_SOURCE_ID",
    "DIVIDEND_YEAR_DAYS",
    "TRI_DATASET",
    "TRI_SOURCE_ID",
    "ConstituentRow",
    "ConstituentSnapshot",
    "ImmutableSnapshotError",
    "IndexCloseRow",
    "SyncTracker",
    "TriPoint",
    "TriSeries",
    "close_snapshot_url",
    "compute_tri",
    "constituents_state_source",
    "constituents_url",
    "extend_tri",
    "ingest_constituents",
    "ingest_tri_from_close",
    "l0_close_filename",
    "l0_constituents_filename",
    "membership_asof",
    "parse_close_snapshot",
    "parse_constituents",
    "parse_constituents_l0",
    "parse_tri_native",
    "read_constituents_l1",
    "read_tri_series",
    "tri_url",
    "write_constituents_l1",
    "write_tri_l1",
]

_LOG = get_logger(__name__)

#: Register ids these parsers serve (`source_register.yaml`).
CONSTITUENTS_SOURCE_ID: Final = "nifty_index_constituents"
CLOSE_SNAPSHOT_SOURCE_ID: Final = "nifty_index_close_snapshot"
TRI_SOURCE_ID: Final = "nifty_tri_history"

#: L1 dataset names. Both partition by their as-of date (`date=YYYY-MM-DD`); within a partition each
#: index gets its own file (`<slug>.parquet`), because one date carries many indices and the lake's
#: one-file-per-partition rule (M1.5's determinism check) would otherwise make them collide.
CONSTITUENTS_DATASET: Final = "index_constituents"
TRI_DATASET: Final = "benchmark_tri"

#: Days in a year for the dividend-accrual estimate. 365 (calendar), so the accrual over a real gap
#: between two published closes — a weekend included — is the fraction of the annual yield that
#: actually elapsed. A trading-day count would under-accrue across every weekend and holiday.
DIVIDEND_YEAR_DAYS: Final = Decimal(365)

#: The constituents CSV header, verified on a real fetch (register `parse_check`): five cols, ISIN
#: native. Checked exactly rather than by position so a reordered or renamed column fails loudly
#: instead of being read from the wrong slot.
_CONSTITUENTS_HEADER: Final = ("Company Name", "Industry", "Symbol", "Series", "ISIN Code")

#: The close-all snapshot header (register `parse_check`): 13 cols, price-index only (no TRI — the
#: reason `nifty_tri_history` is still open). Only the four this module needs are read by name.
_CLOSE_INDEX_NAME: Final = "Index Name"
_CLOSE_DATE: Final = "Index Date"
_CLOSE_CLOSING: Final = "Closing Index Value"
_CLOSE_DIV_YIELD: Final = "Div Yield"

#: Month abbreviations spelled out rather than handed to `strptime("%b")`, which reads `LC_TIME`: a
#: host with a non-English locale would otherwise fail to parse a date that is not locale-dependent
#: at all (the same guard shareholding and fii_dii use).
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

#: A price/index value or a rupee amount: strict `Decimal` (no float can be constructed into one),
#: non-negative and finite, so a mis-parsed field cannot become a plausible-looking benchmark value.
IndexValue = Annotated[Decimal, Field(ge=0, strict=True, allow_inf_nan=False)]

#: A dividend yield percentage: strict, finite, and bounded to a real yield. `le=100` turns a
#: mis-scaled field (a fraction where a percentage was meant) into a loud failure, not a silent
#: doubling of the benchmark's dividend leg.
Yield = Annotated[Decimal, Field(ge=0, le=100, strict=True, allow_inf_nan=False)]


class ImmutableSnapshotError(IngestError):
    """An attempt to overwrite a stored monthly snapshot with different membership (§4.1).

    The whole value of constituent history is that a past month's membership is *fixed* once
    recorded — that is what makes a PIT universe trustworthy. Re-deriving the same month from the
    same bytes is fine and is a no-op; changing it is this error, surfaced rather than silently
    written, because a silently mutated snapshot is a survivorship leak that no later test can see.
    """


# ── constituents: the row and the snapshot ─────────────────────────────────────────────────────


class ConstituentRow(BaseModel):
    """One security's membership in one index at one snapshot — ISIN-native, as published.

    What it does: carry the identity of one index constituent (its ISIN join key, plus the symbol,
    series and descriptive fields the source lists).
    What it assumes: the parser already checked the file's structure, so a row that exists is one
    the index list really named.
    What it never does: hold a price, or join on a symbol — ISIN is the key (invariant #2), and the
    constituents file carries it natively so no identity resolution is needed here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(
        pattern=ISIN_PATTERN, description="ISO 6166 identifier — the only join key (invariant #2)"
    )
    symbol: str = Field(min_length=1, description="exchange ticker at the snapshot, as published")
    series: str = Field(min_length=1, description="NSE series (EQ, BE, …), verbatim")
    company_name: str = Field(min_length=1, description="company name as listed, for display")
    industry: str = Field(min_length=1, description="index industry classification, as listed")


class ConstituentSnapshot(BaseModel):
    """One index's membership as of one date — the immutable monthly unit (§4.1).

    What it does: hold every constituent one index-list CSV named, tagged with the index it is for
    and the `as_of` date it was captured.
    What it assumes: each ISIN appears once in a list; a list naming the same company twice is a
    broken file, not two rows.
    What it never does: exist with a duplicate ISIN, or with no rows — validation raises, so a
    snapshot that exists is one every reader and the immutability guard can trust.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    index_slug: str = Field(
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
        description="lake identifier for the index (nifty50, niftyit, …); the URL/file slug",
    )
    index_name: str = Field(min_length=1, description="human label for the index (NIFTY 50, …)")
    as_of: date = Field(description="the date this membership was captured (the snapshot date)")
    rows: tuple[ConstituentRow, ...] = Field(description="one row per constituent, ISIN-sorted")
    source: str = Field(min_length=1, description="Source Register id the payload came from")
    l0_key: str | None = Field(
        default=None, description="`source/date/filename` of the L0 payload this was derived from"
    )

    @model_validator(mode="after")
    def _nonempty_and_unique(self) -> ConstituentSnapshot:
        if not self.rows:
            raise ValueError(
                f"{self.index_slug} {self.as_of.isoformat()}: an index list names at least one "
                "constituent; an empty snapshot is a broken fetch, not a real membership"
            )
        isins = [row.isin for row in self.rows]
        if len(isins) != len(set(isins)):
            duplicate = next(isin for isin in isins if isins.count(isin) > 1)
            raise ValueError(
                f"{self.index_slug} {self.as_of.isoformat()}: {duplicate} listed more than once; "
                "an index names each constituent at most once"
            )
        return self

    @property
    def members(self) -> frozenset[str]:
        """The ISINs in this membership — the set a PIT universe screen intersects against."""
        return frozenset(row.isin for row in self.rows)


# ── constituents: parsing ──────────────────────────────────────────────────────────────────────


def parse_constituents(
    payload: bytes,
    *,
    index_slug: str,
    index_name: str,
    as_of: date,
    filename: str,
    l0_key: str | None = None,
) -> ConstituentSnapshot:
    """Parse one index-list CSV into a dated, immutable membership snapshot.

    Assumes `payload` is one whole `ind_<slug>list.csv` — a five-column CSV whose header is
    exactly `Company Name,Industry,Symbol,Series,ISIN Code` (register `parse_check`). `as_of` is the
    date the list was captured; the file itself carries no date, because niftyindices only ever
    serves "as of today" (register `pit_notes`), so the caller supplies the snapshot date.

    Raises `ParseError`, naming the file, for anything not this format: an HTML soft-404 (the
    site's Angular shell answers a bad path with markup and a 200), an empty body, a wrong header, a
    row with the wrong column count, a malformed ISIN, or a company listed twice.
    """
    text = _decode_csv(payload, filename=filename)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ParseError("no header row", filename=filename) from None
    stripped = tuple(cell.strip() for cell in header)
    if stripped != _CONSTITUENTS_HEADER:
        raise ParseError(
            f"unexpected header {stripped!r}; expected {_CONSTITUENTS_HEADER!r}", filename=filename
        )

    rows: list[ConstituentRow] = []
    for line_no, record in enumerate(reader, start=2):
        if not record or all(not cell.strip() for cell in record):
            continue  # a trailing blank line is not a row
        if len(record) != len(_CONSTITUENTS_HEADER):
            raise ParseError(
                f"row has {len(record)} fields, expected {len(_CONSTITUENTS_HEADER)}: {record!r}",
                filename=filename,
                line=line_no,
            )
        company, industry, symbol, series, isin = (cell.strip() for cell in record)
        try:
            rows.append(
                ConstituentRow(
                    isin=isin,
                    symbol=symbol,
                    series=series,
                    company_name=company,
                    industry=industry,
                )
            )
        except ValidationError as exc:
            raise ParseError(str(exc), filename=filename, line=line_no) from exc

    try:
        snapshot = ConstituentSnapshot(
            index_slug=index_slug,
            index_name=index_name,
            as_of=as_of,
            rows=tuple(sorted(rows, key=lambda row: row.isin)),
            source=CONSTITUENTS_SOURCE_ID,
            l0_key=l0_key,
        )
    except ValidationError as exc:
        raise ParseError(str(exc), filename=filename) from exc

    _LOG.info(
        "indices.constituents_parsed",
        source=CONSTITUENTS_SOURCE_ID,
        index=index_slug,
        as_of=as_of.isoformat(),
        filename=filename,
        rows=len(snapshot.rows),
        state="VALIDATED",
    )
    return snapshot


def parse_constituents_l0(
    store: L0Store, ref: L0Ref, *, index_slug: str, index_name: str, as_of: date
) -> ConstituentSnapshot:
    """Parse the L0 payload a fetch produced, re-verifying its checksum on the way in.

    `Fetcher.fetch` returns an `L0Ref` and never bytes, so this is how a fetched list becomes a
    snapshot. `L0Store.get` re-hashes the payload, making "every L1 value derives from bytes that
    have not changed" true where the derivation happens.
    """
    return parse_constituents(
        store.get(ref),
        index_slug=index_slug,
        index_name=index_name,
        as_of=as_of,
        filename=ref.filename,
        l0_key=ref.key,
    )


def _decode_csv(payload: bytes, *, filename: str) -> str:
    """UTF-8 the body, refusing an empty one and the Angular soft-404 that wears a 200."""
    if not payload.strip():
        raise ParseError("empty response body", filename=filename)
    try:
        text = payload.decode("utf-8-sig")  # tolerate a BOM some exports prepend
    except UnicodeDecodeError as exc:
        raise ParseError(f"body is not UTF-8: {exc}", filename=filename) from exc
    if text.lstrip()[:1] == "<":
        raise ParseError(
            "body is markup, not CSV — the site's Angular shell answered a bad path with HTML and "
            "a 200; it must not become an index membership",
            filename=filename,
        )
    return text


# ── constituents: L1 ───────────────────────────────────────────────────────────────────────────

#: The L1 schema, declared once and enforced on write and read (§4.2). All descriptive; there is no
#: adjusted analogue of a membership list, so invariant #3 has nothing to breach here.
_CONSTITUENTS_SCHEMA: Final = pa.schema(
    [
        pa.field("index_slug", pa.string(), nullable=False),
        pa.field("index_name", pa.string(), nullable=False),
        pa.field("as_of", pa.date32(), nullable=False),
        pa.field("isin", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("series", pa.string(), nullable=False),
        pa.field("company_name", pa.string(), nullable=False),
        pa.field("industry", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=True),
    ]
)


def _constituents_file(index_slug: str) -> str:
    """The per-index filename inside a snapshot-date partition (`<slug>.parquet`)."""
    return f"{index_slug}.parquet"


def write_constituents_l1(snapshot: ConstituentSnapshot, *, data_root: Path | None = None) -> Path:
    """Write one monthly snapshot to L1, immutably, and return the path.

    Layout: `L1/index_constituents/date=<as_of>/<slug>.parquet` — partitioned by the snapshot date,
    one file per index. Membership is ISIN-sorted so a re-derivation from the same bytes is
    byte-identical (M1.5's determinism rule).

    The immutability contract (§4.1, "never overwrite a prior month's membership") is enforced here:
    if a file already exists for this `(index, as_of)`, its membership is compared to the new one.
    Identical membership is a no-op (idempotent re-ingestion); *different* membership raises
    `ImmutableSnapshotError` rather than overwriting, because a silently mutated past snapshot is a
    survivorship leak nothing downstream could detect.
    """
    path = l1_partition_path(
        CONSTITUENTS_DATASET,
        snapshot.as_of,
        filename=_constituents_file(snapshot.index_slug),
        data_root=data_root,
    )
    if path.exists():
        existing = _constituents_of(path)
        if existing.members != snapshot.members:
            raise ImmutableSnapshotError(
                f"{snapshot.index_slug} {snapshot.as_of.isoformat()}: a snapshot already exists "
                f"with {len(existing.members)} members and this one has {len(snapshot.members)}; "
                "a stored month's membership is immutable (§4.1) and will not be overwritten"
            )
        _LOG.info(
            "indices.constituents_l1_unchanged",
            source=snapshot.source,
            index=snapshot.index_slug,
            as_of=snapshot.as_of.isoformat(),
            path=str(path),
            rows=len(snapshot.rows),
            state="NORMALIZED",
        )
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [_constituent_record(snapshot, row) for row in snapshot.rows], schema=_CONSTITUENTS_SCHEMA
    )
    staging = path.with_name(f".{path.name}.partial")
    pq.write_table(table, staging, compression="snappy", version="2.6")
    staging.replace(path)
    _LOG.info(
        "indices.constituents_l1_written",
        source=snapshot.source,
        index=snapshot.index_slug,
        as_of=snapshot.as_of.isoformat(),
        dataset=CONSTITUENTS_DATASET,
        path=str(path),
        rows=len(snapshot.rows),
        state="NORMALIZED",
    )
    return path


def read_constituents_l1(
    index_slug: str, as_of: date, *, data_root: Path | None = None
) -> ConstituentSnapshot:
    """Read one exact `(index, as_of)` snapshot back out of L1.

    Raises `FileNotFoundError` when that month was never captured for that index — an absent
    snapshot is a gap for D7 to explain, not an empty membership.
    """
    path = l1_partition_path(
        CONSTITUENTS_DATASET, as_of, filename=_constituents_file(index_slug), data_root=data_root
    )
    if not path.exists():
        raise FileNotFoundError(
            f"no {index_slug} constituents snapshot for {as_of.isoformat()}: {path}"
        )
    return _constituents_of(path)


def membership_asof(
    index_slug: str, on_date: date, *, data_root: Path | None = None
) -> ConstituentSnapshot | None:
    """The membership of `index_slug` in force on `on_date` — the point-in-time universe read.

    What it does: returns the most recent snapshot whose `as_of` is on or before `on_date`. Monthly
    snapshots accumulate, so "who was in NIFTY 50 in March 2023" is answered by the March-or-earlier
    snapshot that was current then, never by today's list — this is what kills survivorship bias in
    M4's PIT universe (§4.1, invariant #7).
    What it assumes: `on_date` is the decision date in Asia/Kolkata.
    What it never does: return a snapshot captured *after* `on_date` (a future leak), or invent it —
    a date before the first snapshot yields `None`, not today's membership.
    """
    latest: date | None = None
    for snapshot_date in _snapshot_dates(index_slug, data_root=data_root):
        if snapshot_date <= on_date and (latest is None or snapshot_date > latest):
            latest = snapshot_date
    if latest is None:
        return None
    return read_constituents_l1(index_slug, latest, data_root=data_root)


def _snapshot_dates(index_slug: str, *, data_root: Path | None) -> Iterator[date]:
    """The `as_of` dates for which a snapshot of `index_slug` exists in L1."""
    dataset_dir = layer_root(Layer.L1, data_root=data_root) / CONSTITUENTS_DATASET
    if not dataset_dir.is_dir():
        return
    for partition_dir in dataset_dir.iterdir():
        if not partition_dir.is_dir():
            continue
        if not (partition_dir / _constituents_file(index_slug)).exists():
            continue
        try:
            yield partition_date_of(partition_dir)
        except ValueError:
            continue


def _constituents_of(path: Path) -> ConstituentSnapshot:
    """Parse one L1 snapshot file into a `ConstituentSnapshot`, enforcing the schema on read."""
    records = pq.read_table(path, schema=_CONSTITUENTS_SCHEMA).to_pylist()
    if not records:
        raise ParseError("L1 snapshot file has no rows", filename=str(path))
    rows = tuple(
        ConstituentRow(
            isin=str(r["isin"]),
            symbol=str(r["symbol"]),
            series=str(r["series"]),
            company_name=str(r["company_name"]),
            industry=str(r["industry"]),
        )
        for r in records
    )
    head = records[0]
    return ConstituentSnapshot(
        index_slug=str(head["index_slug"]),
        index_name=str(head["index_name"]),
        as_of=head["as_of"],
        rows=tuple(sorted(rows, key=lambda row: row.isin)),
        source=str(head["source"]),
        l0_key=None if head["l0_key"] is None else str(head["l0_key"]),
    )


def _constituent_record(snapshot: ConstituentSnapshot, row: ConstituentRow) -> dict[str, Any]:
    """One row as the dict `pa.Table.from_pylist` writes against `_CONSTITUENTS_SCHEMA`."""
    return {
        "index_slug": snapshot.index_slug,
        "index_name": snapshot.index_name,
        "as_of": snapshot.as_of,
        "isin": row.isin,
        "symbol": row.symbol,
        "series": row.series,
        "company_name": row.company_name,
        "industry": row.industry,
        "source": snapshot.source,
        "l0_key": snapshot.l0_key,
    }


# ── the close-all snapshot (§4.1's TRI fallback input) ─────────────────────────────────────────


class IndexCloseRow(BaseModel):
    """One index's closing values for one session, from the daily close-all snapshot.

    What it does: carry the closing index value and dividend yield for one `(index_name, date)` —
    the two numbers §4.1's computed-TRI fallback needs.
    What it assumes: the parser already checked the header, so a row that exists is one the file
    really published.
    What it never does: hold a `float`, or turn a missing yield into `0` — a yield the file leaves
    blank or `-` is `None` (unknown), never zero (which would read as "this index pays nothing" and
    silently flatten the dividend leg of the benchmark).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    index_name: str = Field(min_length=1, description="index name as published (Nifty 50, …)")
    index_date: date = Field(description="the session this close is for (Asia/Kolkata)")
    close: IndexValue = Field(description="closing index value, unadjusted, as published")
    div_yield: Yield | None = Field(
        default=None, description="dividend yield %, or None when the file does not state it"
    )


def parse_close_snapshot(payload: bytes, *, filename: str) -> tuple[IndexCloseRow, ...]:
    """Parse one `ind_close_all_<DDMMYYYY>.csv` into per-index close rows.

    Assumes `payload` is one whole daily close-all CSV (register `parse_check`: 13 columns, all
    NIFTY indices). Reads the four columns the TRI fallback needs by name — index, date, closing
    value, dividend yield — and is indifferent to the other nine, so a cosmetic column addition does
    not stop ingestion. A `-` or blank dividend yield becomes `None`, not `0`.

    Raises `ParseError`, naming the file, for an HTML soft-404, an empty body, a header missing one
    of the four required columns, or a value that is not a plain decimal.
    """
    text = _decode_csv(payload, filename=filename)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ParseError("no header row", filename=filename)
    fields = {name.strip(): name for name in reader.fieldnames}
    for required in (_CLOSE_INDEX_NAME, _CLOSE_DATE, _CLOSE_CLOSING, _CLOSE_DIV_YIELD):
        if required not in fields:
            raise ParseError(
                f"header is missing {required!r}; present: {', '.join(sorted(fields))}",
                filename=filename,
            )

    rows: list[IndexCloseRow] = []
    for line_no, record in enumerate(reader, start=2):
        name = (record.get(fields[_CLOSE_INDEX_NAME]) or "").strip()
        if not name:
            continue  # a trailing blank line is not a row
        try:
            rows.append(
                IndexCloseRow(
                    index_name=name,
                    index_date=_index_date(
                        (record.get(fields[_CLOSE_DATE]) or "").strip(),
                        index=line_no,
                        filename=filename,
                    ),
                    close=_decimal(
                        (record.get(fields[_CLOSE_CLOSING]) or "").strip(),
                        key=_CLOSE_CLOSING,
                        line=line_no,
                        filename=filename,
                    ),
                    div_yield=_optional_decimal(
                        (record.get(fields[_CLOSE_DIV_YIELD]) or "").strip(),
                        key=_CLOSE_DIV_YIELD,
                        line=line_no,
                        filename=filename,
                    ),
                )
            )
        except ValidationError as exc:
            raise ParseError(str(exc), filename=filename, line=line_no) from exc

    if not rows:
        raise ParseError("no index rows in close-all snapshot", filename=filename)
    _LOG.info(
        "indices.close_snapshot_parsed",
        source=CLOSE_SNAPSHOT_SOURCE_ID,
        filename=filename,
        rows=len(rows),
        state="VALIDATED",
    )
    return tuple(rows)


# ── benchmark TRI: the native published series (ready for when the gate opens) ──────────────────


def parse_tri_native(payload: bytes, *, filename: str, l0_key: str | None = None) -> TriSeries:
    """Parse a `getTotalReturnIndexString` response into a published TRI series.

    The niftyindices historical-TRI endpoint answers with `{"d": "<json-string>"}`, where the inner
    string is a JSON array of `{"Index Name", "HistoricalDate", "TotalReturnsIndex"}` records. This
    reads that shape and preserves each published value as an exact `Decimal`.

    This is the *primary* TRI source and the one we want — but it is behind an application-level
    session gate our fetcher cannot open (register `nifty_tri_history` is FAILED, with evidence).
    The parser is kept ready so that the day the handshake is solved, a published series ingests
    with no code change; until then §4.1's `compute_tri` fallback is what actually produces the
    benchmark. `method` on the result is `published`, distinguishing it from the computed estimate.

    Raises `ParseError`, naming the file, for the HTML the gate returns instead of JSON (a 200
    carrying markup — the failure signal the register warns about), a missing `d` envelope, or a
    value that is not a plain decimal.
    """
    if not payload.strip():
        raise ParseError("empty response body", filename=filename)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(f"body is not UTF-8: {exc}", filename=filename) from exc
    if text.lstrip()[:1] == "<":
        raise ParseError(
            "body is markup, not JSON — the historical-data session gate returned the site's HTML "
            "with a 200 instead of a TRI series; it must not become a benchmark",
            filename=filename,
        )
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"body is not valid JSON: {exc}", filename=filename) from exc
    if not isinstance(envelope, dict) or "d" not in envelope:
        raise ParseError(
            "response has no 'd' envelope; getTotalReturnIndexString wraps its array in {'d': …}",
            filename=filename,
        )
    inner = envelope["d"]
    if isinstance(inner, str):
        try:
            records = json.loads(inner, parse_float=Decimal, parse_int=Decimal)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"'d' is not a JSON string of records: {exc}", filename=filename
            ) from exc
    else:
        records = inner
    if not isinstance(records, list) or not records:
        raise ParseError("TRI payload carries no records", filename=filename)

    index_name = ""
    points: list[TriPoint] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ParseError(f"record {index} is not an object", filename=filename)
        index_name = str(record.get("Index Name", record.get("INDEX_NAME", "")) or "").strip()
        raw_value = record.get("TotalReturnsIndex", record.get("TotalReturnIndex"))
        raw_date = str(record.get("HistoricalDate", "") or "").strip()
        if not index_name or raw_value is None or not raw_date:
            raise ParseError(
                f"record {index} is missing Index Name, HistoricalDate or TotalReturnsIndex",
                filename=filename,
            )
        value = (
            raw_value
            if isinstance(raw_value, Decimal)
            else _decimal(
                str(raw_value).strip(), key="TotalReturnsIndex", line=index, filename=filename
            )
        )
        try:
            points.append(
                TriPoint(
                    index_slug=_slug(index_name),
                    index_name=index_name,
                    as_of=_index_date(raw_date, index=index, filename=filename),
                    tri_value=value,
                    price_close=None,
                    method="published",
                    l0_key=l0_key,
                )
            )
        except ValidationError as exc:
            raise ParseError(f"record {index}: {exc}", filename=filename) from exc

    series = TriSeries(
        index_slug=_slug(index_name),
        index_name=index_name,
        method="published",
        points=tuple(sorted(points, key=lambda p: p.as_of)),
    )
    _LOG.info(
        "indices.tri_native_parsed",
        source=TRI_SOURCE_ID,
        index=series.index_slug,
        filename=filename,
        points=len(series.points),
        state="VALIDATED",
    )
    return series


# ── benchmark TRI: the computed fallback (§4.1) ────────────────────────────────────────────────


class TriPoint(BaseModel):
    """One total-return index value for one index on one date.

    What it does: carry a TRI value, the method that produced it, and — for the computed fallback —
    the price close it was built from, so the estimate is auditable against the price index.
    What it never does: hold a `float`, or hide how it was made — `method` is `published` for the
    exchange's own series and `computed_price_plus_div` for §4.1's estimate, and a consumer can tell
    which it is holding.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    index_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$", description="lake identifier")
    index_name: str = Field(min_length=1, description="human index label")
    as_of: date = Field(description="the session this TRI value is for")
    tri_value: IndexValue = Field(description="total-return index value (price + reinvested divs)")
    price_close: IndexValue | None = Field(
        default=None, description="the price-index close this was computed from; None if published"
    )
    method: str = Field(
        pattern=r"^(published|computed_price_plus_div)$",
        description="how the value was produced — the exchange's series or §4.1's estimate",
    )
    l0_key: str | None = Field(default=None, description="`source/date/filename` of the L0 payload")


class TriSeries(BaseModel):
    """A dated total-return series for one index — the benchmark leg of the return comparison.

    What it does: hold the ordered TRI points for one index and the single method that produced them
    all.
    What it assumes: every point is the same index and method; a mixed series is a construction bug.
    What it never does: exist empty, out of order, or with two values for one date — validation
    raises, so a series that exists is one that can be read as a monotone-in-time benchmark.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    index_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$", description="lake identifier")
    index_name: str = Field(min_length=1, description="human index label")
    method: str = Field(pattern=r"^(published|computed_price_plus_div)$")
    points: tuple[TriPoint, ...] = Field(description="TRI points in ascending date order")

    @model_validator(mode="after")
    def _consistent_and_ordered(self) -> TriSeries:
        if not self.points:
            raise ValueError(f"{self.index_slug}: a TRI series has at least one point")
        dates = [p.as_of for p in self.points]
        if len(dates) != len(set(dates)):
            raise ValueError(f"{self.index_slug}: two TRI values for one date")
        if dates != sorted(dates):
            raise ValueError(f"{self.index_slug}: TRI points are not in ascending date order")
        for point in self.points:
            if point.method != self.method or point.index_slug != self.index_slug:
                raise ValueError(
                    f"{self.index_slug}: a point's method/index does not match the series"
                )
        return self


def compute_tri(
    closes: Sequence[IndexCloseRow], *, index_slug: str, l0_key: str | None = None
) -> TriSeries:
    """Compute §4.1's total-return fallback from a price-index close series with dividend yields.

    What it does: chains a total-return index off the price index. The series is **seeded to the
    published closing index value** on its first date — so its anchor is a real published number
    (acceptance 3) — and each subsequent day multiplies the running value by the price return *plus*
    a dividend accrual estimated from the previous day's yield:

        tri[t] = tri[t-1] * ( close[t] / close[t-1]  +  yield[t-1]/100 * days/365 )

    What it assumes: `closes` are all the same index, one per session; they are sorted by date here.
    A day whose yield is unknown (`None`) contributes no dividend — the price return alone — rather
    than being guessed.
    What it never does: claim to be the exchange's TRI. It is a constant-yield daily estimate, not a
    dividend-event ledger, and every point is stamped `computed_price_plus_div` to say so. With zero
    dividends it reproduces the price index exactly; a positive yield makes it exceed the price
    return by the accrued amount — an inverted sign fails the test that asserts this.

    Raises `IngestError` for an empty series or a non-positive close (a division that cannot yield a
    return).
    """
    if not closes:
        raise IngestError("compute_tri needs at least one close row")
    ordered = sorted(closes, key=lambda row: row.index_date)
    names = {row.index_name for row in ordered}
    if len(names) != 1:
        raise IngestError(f"compute_tri got mixed indices: {sorted(names)}")
    index_name = ordered[0].index_name

    points: list[TriPoint] = []
    # Compound on the *quantized* running value at every step so the batch path here and the
    # daily-incremental `extend_tri` — which only ever has the stored (quantized) prior value —
    # produce byte-identical series. Determinism is the point (M1.5's rule); a full-precision
    # running total would diverge from the stored one by a ULP and break replay.
    running = _quantize(ordered[0].close)
    points.append(
        TriPoint(
            index_slug=index_slug,
            index_name=index_name,
            as_of=ordered[0].index_date,
            tri_value=running,
            price_close=ordered[0].close,
            method="computed_price_plus_div",
            l0_key=l0_key,
        )
    )
    for prev, curr in itertools.pairwise(ordered):
        if prev.close <= 0:
            raise IngestError(
                f"{index_name} {prev.index_date.isoformat()}: close {prev.close} is not positive; "
                "a total return cannot be computed across a zero base"
            )
        running = _quantize(running * _daily_growth(prev, curr))
        points.append(
            TriPoint(
                index_slug=index_slug,
                index_name=index_name,
                as_of=curr.index_date,
                tri_value=running,
                price_close=curr.close,
                method="computed_price_plus_div",
                l0_key=l0_key,
            )
        )

    series = TriSeries(
        index_slug=index_slug,
        index_name=index_name,
        method="computed_price_plus_div",
        points=tuple(points),
    )
    _LOG.info(
        "indices.tri_computed",
        source=CLOSE_SNAPSHOT_SOURCE_ID,
        index=index_slug,
        points=len(series.points),
        seed=str(series.points[0].tri_value),
        state="VALIDATED",
    )
    return series


def extend_tri(prior: TriSeries, close: IndexCloseRow, *, prev_close: IndexCloseRow) -> TriSeries:
    """Append one more day to a computed TRI series — the daily-incremental path.

    A backfill runs `compute_tri` over the accumulated close-all history; the daily loop instead has
    yesterday's series and today's one close row, and chains one step onto it. `prev_close` is the
    session `prior`'s last point was built from, so the price return and the dividend accrual match
    what `compute_tri` would have produced over the same two days (the two paths are equal — a
    property the tests assert).

    Raises `IngestError` if `prior` is not the computed fallback, if the new date is not after the
    last, or if the previous close is non-positive.
    """
    if prior.method != "computed_price_plus_div":
        raise IngestError("extend_tri only extends a computed series; a published one is fetched")
    last = prior.points[-1]
    if close.index_date <= last.as_of:
        raise IngestError(
            f"{prior.index_slug}: new date {close.index_date.isoformat()} is not after the last "
            f"point {last.as_of.isoformat()}"
        )
    if prev_close.close <= 0:
        raise IngestError(f"{prior.index_slug}: previous close {prev_close.close} is not positive")
    value = _quantize(last.tri_value * _daily_growth(prev_close, close))
    return TriSeries(
        index_slug=prior.index_slug,
        index_name=prior.index_name,
        method=prior.method,
        points=(
            *prior.points,
            TriPoint(
                index_slug=prior.index_slug,
                index_name=prior.index_name,
                as_of=close.index_date,
                tri_value=value,
                price_close=close.close,
                method=prior.method,
                l0_key=last.l0_key,
            ),
        ),
    )


# ── benchmark TRI: L1 ──────────────────────────────────────────────────────────────────────────

#: The TRI L1 schema. `decimal128(18, 4)` holds an index value to four places — enough precision
#: that the computed estimate is not rounded away, declared once and enforced on write and read.
_TRI_SCHEMA: Final = pa.schema(
    [
        pa.field("index_slug", pa.string(), nullable=False),
        pa.field("index_name", pa.string(), nullable=False),
        pa.field("as_of", pa.date32(), nullable=False),
        pa.field("tri_value", pa.decimal128(18, 4), nullable=False),
        pa.field("price_close", pa.decimal128(18, 4), nullable=True),
        pa.field("method", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=True),
    ]
)


def _tri_file(index_slug: str) -> str:
    return f"{index_slug}.parquet"


def write_tri_l1(series: TriSeries, *, data_root: Path | None = None) -> tuple[Path, ...]:
    """Write a TRI series to L1, one partition per date, and return the paths written.

    Layout mirrors the rest of the lake: `L1/benchmark_tri/date=<as_of>/<slug>.parquet`, one point
    per partition, so `read_tri_series(index, through)` answers a point-in-time read by choosing
    partitions on or before the date rather than filtering rows — no future TRI value can reach a
    decision dated before it (invariant #7). Each partition is written whole to a temp name then
    renamed over the target, so a re-derivation is byte-identical and a crash mid-write cannot leave
    a half file readable.
    """
    source = TRI_SOURCE_ID if series.method == "published" else CLOSE_SNAPSHOT_SOURCE_ID
    written: list[Path] = []
    for point in series.points:
        path = l1_partition_path(
            TRI_DATASET, point.as_of, filename=_tri_file(series.index_slug), data_root=data_root
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist([_tri_record(point, source)], schema=_TRI_SCHEMA)
        staging = path.with_name(f".{path.name}.partial")
        pq.write_table(table, staging, compression="snappy", version="2.6")
        staging.replace(path)
        written.append(path)
    _LOG.info(
        "indices.tri_l1_written",
        source=source,
        index=series.index_slug,
        dataset=TRI_DATASET,
        method=series.method,
        points=len(written),
        state="NORMALIZED",
    )
    return tuple(written)


def read_tri_series(
    index_slug: str, through: date, *, data_root: Path | None = None
) -> TriSeries | None:
    """Read the computed/published TRI series for one index, up to and including `through`.

    Returns the points whose `as_of` is on or before `through`, in date order — the point-in-time
    benchmark as it was knowable on the decision date. Returns `None` when no partition exists on or
    before that date, rather than an empty series (which the model forbids).
    """
    points: list[TriPoint] = []
    dataset_dir = layer_root(Layer.L1, data_root=data_root) / TRI_DATASET
    if not dataset_dir.is_dir():
        return None
    for partition_dir in dataset_dir.iterdir():
        if not partition_dir.is_dir():
            continue
        path = partition_dir / _tri_file(index_slug)
        if not path.exists():
            continue
        try:
            as_of = partition_date_of(partition_dir)
        except ValueError:
            continue
        if as_of <= through:
            points.append(_tri_point_of(path))
    if not points:
        return None
    points.sort(key=lambda p: p.as_of)
    return TriSeries(
        index_slug=index_slug,
        index_name=points[0].index_name,
        method=points[0].method,
        points=tuple(points),
    )


def ingest_tri_from_close(
    closes: Sequence[IndexCloseRow],
    *,
    index_slug: str,
    data_root: Path | None = None,
    l0_key: str | None = None,
) -> TriSeries:
    """Compute §4.1's TRI fallback from a close series and write it to L1 — the offline ingestion.

    The one call that turns accumulated close-all history into a stored benchmark series: compute,
    then write. Returns the series it stored. This is the path that actually produces the benchmark
    while `nifty_tri_history` stays gated; `parse_tri_native` + `write_tri_l1` is the path for the
    exchange's own series once that gate opens.
    """
    series = compute_tri(closes, index_slug=index_slug, l0_key=l0_key)
    write_tri_l1(series, data_root=data_root)
    return series


def _tri_record(point: TriPoint, source: str) -> dict[str, Any]:
    return {
        "index_slug": point.index_slug,
        "index_name": point.index_name,
        "as_of": point.as_of,
        "tri_value": point.tri_value,
        "price_close": point.price_close,
        "method": point.method,
        "source": source,
        "l0_key": point.l0_key,
    }


def _tri_point_of(path: Path) -> TriPoint:
    record = pq.read_table(path, schema=_TRI_SCHEMA).to_pylist()[0]
    return TriPoint(
        index_slug=str(record["index_slug"]),
        index_name=str(record["index_name"]),
        as_of=record["as_of"],
        tri_value=record["tri_value"],
        price_close=record["price_close"],
        method=str(record["method"]),
        l0_key=None if record["l0_key"] is None else str(record["l0_key"]),
    )


# ── URLs, filenames, and the constituents runner ───────────────────────────────────────────────


class SyncTracker(Protocol):
    """The slice of the §4.4 state machine one snapshot ingestion drives (M1.3).

    A protocol rather than the concrete `SyncStateStore` so a poll can be driven end to end without
    Postgres in an offline unit test (B8) — the store satisfies it structurally.
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


def _source_entry(source_id: str, register: SourceRegister | None) -> Source:
    reg = load_register() if register is None else register
    entry = next((item for item in reg.sources if item.id == source_id), None)
    if entry is None:
        raise IngestError(f"no {source_id!r} entry in the Source Register")
    return entry


def constituents_url(index_slug: str, register: SourceRegister | None = None) -> str:
    """The constituents CSV URL for one index, from the register template (C.1).

    The register holds the verified `ind_{index_slug}list.csv` template; the slug is filled in here
    and nowhere else, so a URL-pattern change is one edit in the register.
    """
    template = _source_entry(CONSTITUENTS_SOURCE_ID, register).url_template
    return template.replace("{index_slug}", index_slug)


def close_snapshot_url(snapshot_date: date, register: SourceRegister | None = None) -> str:
    """The daily close-all CSV URL for a date, from the register template (C.1)."""
    template = _source_entry(CLOSE_SNAPSHOT_SOURCE_ID, register).url_template
    return template.replace("{DDMMYYYY}", f"{snapshot_date:%d%m%Y}")


def tri_url(register: SourceRegister | None = None) -> str:
    """The historical-TRI endpoint, from the register template (C.1).

    Exposed for completeness and for the runner that will use it once the session gate is solved;
    `nifty_tri_history` is FAILED today, so the computed fallback is what runs.
    """
    return _source_entry(TRI_SOURCE_ID, register).url_template


def l0_constituents_filename(index_slug: str, as_of: date) -> str:
    """The L0 filename for one index-list snapshot.

    The source URL carries no date (it is always "as of today"), so the snapshot date goes in the
    name here — otherwise every month's fetch of one index would be handed the same filename and the
    second poll within a month would collide with the first (`L0Store.put`).
    """
    return f"ind_{index_slug}list_{as_of:%Y%m%d}.csv"


def l0_close_filename(snapshot_date: date) -> str:
    """The L0 filename for one close-all snapshot (the URL already dates it, kept explicit)."""
    return f"ind_close_all_{snapshot_date:%d%m%Y}.csv"


def constituents_state_source(index_slug: str) -> str:
    """The sync-state source id for one index list — `nifty_index_constituents/<slug>`.

    The constituents *register* entry is one row (`nifty_index_constituents`, the endpoint the URL
    and crawl policy come from), but every index list publishes "as of today" through it, so a whole
    sweep files many slugs under one logical date. A sync row is keyed `(source, logical_date)`, so
    if every slug used the bare register id they would all collide on the one `(source, as_of)` row
    — the first to `PUBLISHED` would make the next slug's `begin` an illegal transition out of a
    terminal state. Qualifying the sync source with the slug gives each list its own row, so
    `/status/sync` shows per-slug progress and one slug's failure is a `FAILED` row of its own
    (what M10.2's per-slug journaling builds on). The *fetch* still uses the bare register id: the
    URL, headers and 403 watch are all per-endpoint, not per-slug.

    The separator is `/`, which `SyncKey` (D5) splits back out into the `sync_state.unit` column
    added by migration 0008 — so `source` stays the register id the status API and the gap report
    read as an enumeration, and the slug lives where a sub-key belongs. It was `:` until then;
    `SyncKey.parse` still accepts that form so a string persisted before the migration resolves.
    """
    return f"{CONSTITUENTS_SOURCE_ID}/{index_slug}"


def ingest_constituents(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SyncTracker,
    index_slug: str,
    index_name: str,
    as_of: date,
    data_root: Path | None = None,
    register: SourceRegister | None = None,
    state_source: str | None = None,
) -> ConstituentSnapshot:
    """Take one index snapshot from nothing to `PUBLISHED`: fetch → L0 → parse → L1 → sync.

    Drives the §4.4 transitions in order around the fetch and the write, so a partially-ingested
    snapshot is visible as the state it actually reached rather than as an absence. `as_of` is the
    snapshot date — both the logical date the L0 payload files under and the date the membership is
    recorded against.

    `state_source` is the id the *sync row* is keyed under; it defaults to the per-slug
    `constituents_state_source(index_slug)` so a sweep of many lists on one date does not collide on
    one shared sync row (see that helper). The *fetch* always uses the bare register id
    `CONSTITUENTS_SOURCE_ID`, because the URL and crawl policy are per-endpoint.

    A re-run of the same month is safe: `write_constituents_l1` is a no-op when the membership is
    unchanged and raises `ImmutableSnapshotError` if it would differ. Any failure is recorded on the
    sync row — with `retryable` set from what actually went wrong — then re-raised, so the caller
    sees the exception and `/status/sync` sees the state.
    """
    sync_source = state_source or constituents_state_source(index_slug)
    url = constituents_url(index_slug, register)
    tracker.begin(sync_source, as_of)
    try:
        ref = fetcher.fetch(
            CONSTITUENTS_SOURCE_ID, url, as_of, filename=l0_constituents_filename(index_slug, as_of)
        )
        tracker.mark_fetched(sync_source, as_of, checksum=ref.sha256, l0_path=ref.key)

        snapshot = parse_constituents_l0(
            l0, ref, index_slug=index_slug, index_name=index_name, as_of=as_of
        )
        tracker.mark_validated(sync_source, as_of)

        write_constituents_l1(snapshot, data_root=data_root)
        tracker.mark_normalized(sync_source, as_of)

        tracker.mark_published(sync_source, as_of)
    except Exception as exc:
        tracker.mark_failed(
            sync_source, as_of, f"{type(exc).__name__}: {exc}", retryable=_retryable(exc)
        )
        _LOG.error(
            "indices.constituents_ingest_failed",
            source=sync_source,
            index=index_slug,
            as_of=as_of.isoformat(),
            error=f"{type(exc).__name__}: {exc}",
            retryable=_retryable(exc),
            state="FAILED",
        )
        raise

    _LOG.info(
        "indices.constituents_published",
        source=CONSTITUENTS_SOURCE_ID,
        index=index_slug,
        as_of=as_of.isoformat(),
        rows=len(snapshot.rows),
        state="PUBLISHED",
    )
    return snapshot


# ── shared helpers ─────────────────────────────────────────────────────────────────────────────


def _retryable(exc: BaseException) -> bool:
    """Whether repeating this attempt later could ever produce a different outcome (§4.4)."""
    if isinstance(exc, ForbiddenSpikeError):
        return False
    if isinstance(exc, RetryableFetchError):
        return True
    return not isinstance(exc, ParseError | FetchHTTPError | ImmutableSnapshotError)


def _index_date(value: str, *, index: int, filename: str) -> date:
    """`03-Aug-2026` or `03 Aug 2026` → `date(2026, 8, 3)`, locale-independently."""
    cleaned = value.replace("-", " ").split()
    if len(cleaned) != 3:
        raise ParseError(
            f"record {index}: date {value!r} is not DD-Mon-YYYY / DD Mon YYYY", filename=filename
        )
    day_s, month_s, year_s = cleaned
    month = _MONTHS.get(month_s.upper()[:3])
    if month is None:
        raise ParseError(
            f"record {index}: date {value!r} names no month we know", filename=filename
        )
    try:
        return date(int(year_s), month, int(day_s))
    except ValueError as exc:
        raise ParseError(
            f"record {index}: date {value!r} is not a real date", filename=filename
        ) from exc


def _decimal(value: str, *, key: str, line: int, filename: str) -> Decimal:
    """A required decimal, exact and finite, read from the CSV's text."""
    literal = value.replace(",", "").strip()
    if not literal:
        raise ParseError(f"{key!r} is empty", filename=filename, line=line)
    try:
        parsed = Decimal(literal)
    except InvalidOperation as exc:
        raise ParseError(
            f"{key!r} is {value!r}, which is not a plain decimal", filename=filename, line=line
        ) from exc
    if not parsed.is_finite():
        raise ParseError(f"{key!r} is {value!r}", filename=filename, line=line)
    return parsed


def _optional_decimal(value: str, *, key: str, line: int, filename: str) -> Decimal | None:
    """A decimal a source may leave blank or `-` — `None`, not `0` (a real absence, not a zero)."""
    if not value or value == "-":
        return None
    return _decimal(value, key=key, line=line, filename=filename)


def _daily_growth(prev: IndexCloseRow, curr: IndexCloseRow) -> Decimal:
    """One day's total-return growth factor: price return + estimated dividend accrual.

        close[t]/close[t-1]  +  yield[t-1]/100 * days/365

    A day whose previous yield is unknown (`None`) contributes the price return alone rather than
    a guessed dividend — an absent yield is not zero income, it is unknown income, and the estimate
    declines to invent it.
    """
    price_ratio = curr.close / prev.close
    days = Decimal((curr.index_date - prev.index_date).days)
    prev_yield = prev.div_yield if prev.div_yield is not None else Decimal(0)
    div_accrual = (prev_yield / Decimal(100)) * days / DIVIDEND_YEAR_DAYS
    return price_ratio + div_accrual


def _quantize(value: Decimal) -> Decimal:
    """Round a computed TRI value to the four places L1 stores, so write and read round-trip."""
    return value.quantize(Decimal("0.0001"))


def _slug(index_name: str) -> str:
    """A lake identifier from an index name: `Nifty 50 TR` → `nifty50tr`."""
    return "".join(ch.lower() for ch in index_name if ch.isalnum()) or "index"
