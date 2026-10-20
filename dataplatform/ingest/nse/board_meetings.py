"""NSE board-meeting intimations (M17.16, pre-registration Amendment 4) → L1 `board_meetings`.

`www.nseindia.com/api/corporate-board-meetings` is the feed behind NSE's own "Board Meetings"
page: one record per intimation a listed company filed, naming the meeting date (`bm_date`), the
purpose (`bm_purpose`, a short fixed vocabulary such as "Financial Results/Dividend"), the
company's own one-line statement (`bm_desc`), the ISIN (`sm_isin`, native) and the exchange's
dissemination time (`bm_timestamp`). The `from_date` / `to_date` parameters filter on the
*meeting* date, not the filing date, so one request answers "every meeting scheduled in this
window that anyone has intimated so far".

Why a source of its own: results board-meeting intimations do not reach the corporate-announcements
feed at all — 0 of 8,743 NSE announcements in L1 for 2026 were one (the 2026-10-16 buy review), so
S5's `results_board_meeting` keyword category never fired, and the M17 managers spent 36 % of their
web queries finding results dates by hand.

Three facts shape this module:

* **The dissemination time is the fact's PIT, and the capture cut-off is the knowable date.**
  `bm_timestamp` is the instant NSE published the intimation (IST, to the second). The nightly
  capture fires at :data:`KNOWABLE_CUTOFF` (20:00 IST), so an intimation disseminated after that
  on day D cannot have been in D's capture: its knowable date is D + 1. Partitioning L1 by that
  knowable date makes "known by the session's evening" a choice of partitions (as
  `shareholding.read_pit` does), and makes a live run and a later replay of the same session read
  the same rows.
* **The feed is a current-state list, so captures merge.** Each capture re-serves every intimation
  for the meetings in its window, past and future. A partition is rewritten as the union of what it
  held and what the capture says, keyed on (ISIN, meeting date, purpose, dissemination time) — a
  later capture never erases a row an earlier one landed. A meeting the company later rescheduled
  stays listed under its first date as well as its new one; the reader takes the earliest
  scheduled date after the session, which is the date the company last *did* state for the
  nearest meeting only when it did not move it (documented limitation, Amendment 4 (a)).
* **"Results" is read from the company's words.** About half of results intimations carry the
  generic purpose "Board Meeting Intimation" and say "financial results" only in `bm_desc`. A row is
  a results meeting when :data:`RESULTS_PATTERN` matches its purpose or its description. Measured on
  the July 2026 payload (1,811 rows): every row whose text says "results" says "financial results".

Identity is ISIN (invariant #2); `symbol` is display only. Offline by construction (B8): the parser
takes bytes or an `L0Ref`; only `dataplatform.ingest.fetcher` opens a socket.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, field_validator

from dataplatform.clock import IST
from dataplatform.ingest.models import ISIN_PATTERN, ParseError
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.paths import Layer, l1_partition_path, layer_root, partition_date_of

__all__ = [
    "BOARD_MEETINGS_DATASET",
    "CAPTURE_DAYS_AFTER",
    "CAPTURE_DAYS_BEFORE",
    "KNOWABLE_CUTOFF",
    "RESULTS_PATTERN",
    "SOURCE_ID",
    "BoardMeetingRow",
    "board_meetings_url",
    "knowable_date_of",
    "l0_filename",
    "merge_l1",
    "parse",
    "parse_l0",
    "read_l1",
    "read_pit",
]

_LOG = get_logger(__name__)

#: The register id this parser serves (`source_register.yaml`, `parser.task: M17.16`).
SOURCE_ID: Final = "nse_board_meetings"

#: `data/L1/board_meetings/date=<knowable date>/part.parquet`.
BOARD_MEETINGS_DATASET: Final = "board_meetings"

#: The nightly capture's fire time (`nse_daily_capture`, 20:00 IST). An intimation disseminated at
#: or after it on day D is knowable on D + 1 (module docstring).
KNOWABLE_CUTOFF: Final = time(20, 0)

#: The nightly capture's meeting-date window around the owed session: a week back (late
#: intimations of meetings just held) and 90 days ahead (the longest observed notice was 65 days).
CAPTURE_DAYS_BEFORE: Final = 7
CAPTURE_DAYS_AFTER: Final = 90

#: A results meeting: "financial result(s)" in the purpose or the company's description.
RESULTS_PATTERN: Final = re.compile(r"financial\s+results?", re.IGNORECASE)

_MONTHS: Final[Mapping[str, int]] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}  # fmt: skip
_DATE: Final = re.compile(r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s*$")
_TIMESTAMP: Final = re.compile(r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{2}):(\d{2}):(\d{2})\s*$")
_MAX_TEXT: Final = 2000


class BoardMeetingRow(BaseModel):
    """One board-meeting intimation, as NSE disseminated it.

    What it does: carries the meeting date and purpose a company stated, when NSE published that
    statement (`disseminated_at`), the knowable date that implies (`knowable_date`), and whether it
    is a results meeting (`is_results`).
    What it assumes: the parser resolved every field from the payload; nothing is defaulted.
    What it never does: hold a time from the ingest clock, or join on `symbol`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN)
    symbol: str | None = None
    meeting_date: date
    purpose: str = Field(min_length=1)
    description: str | None = None
    disseminated_at: datetime
    knowable_date: date
    is_results: bool
    source: str = SOURCE_ID
    l0_key: str | None = None

    @field_validator("disseminated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dissemination time must be tz-aware (IST) to be point-in-time usable")
        return value

    @property
    def key(self) -> tuple[str, date, str, datetime]:
        """The identity a merge de-duplicates on."""
        return (self.isin, self.meeting_date, self.purpose, self.disseminated_at)


def knowable_date_of(disseminated_at: datetime) -> date:
    """The date an intimation is knowable on: its IST date, or the next day from 20:00 IST on."""
    local = disseminated_at.astimezone(IST)
    return local.date() + timedelta(days=1) if local.time() >= KNOWABLE_CUTOFF else local.date()


def board_meetings_url(start: date, end: date, register: SourceRegister | None = None) -> str:
    """The register's URL for meetings dated ``start``..``end`` (both inclusive)."""
    if end < start:
        raise ValueError(f"meeting window ends {end} before it starts {start}")
    reg = load_register() if register is None else register
    entry = next((row for row in reg.sources if row.id == SOURCE_ID), None)
    if entry is None:
        raise LookupError(f"no {SOURCE_ID!r} entry in the Source Register")
    template = entry.url_template
    template = template.replace("{DD-MM-YYYY}", f"{start:%d-%m-%Y}", 1)
    return template.replace("{DD-MM-YYYY}", f"{end:%d-%m-%Y}", 1)


def l0_filename(owed: date, start: date, end: date) -> str:
    """The L0 name of one capture: the session it was owed for and the meeting window it asked."""
    return f"board_meetings_{owed:%Y%m%d}_{start:%Y%m%d}-{end:%Y%m%d}.json"


def _date(text: object, *, filename: str, field: str) -> date:
    match = _DATE.match(text) if isinstance(text, str) else None
    month = _MONTHS.get(match.group(2).upper()) if match else None
    if match is None or month is None:
        raise ParseError(f"{field} {text!r} is not DD-Mon-YYYY", filename=filename)
    return date(int(match.group(3)), month, int(match.group(1)))


def _timestamp(text: object, *, filename: str) -> datetime:
    match = _TIMESTAMP.match(text) if isinstance(text, str) else None
    month = _MONTHS.get(match.group(2).upper()) if match else None
    if match is None or month is None:
        raise ParseError(
            f"bm_timestamp {text!r} is not DD-Mon-YYYY HH:MM:SS; a row the exchange did not date "
            "is not point-in-time usable",
            filename=filename,
        )
    day, _, year, hh, mm, ss = match.groups()
    return datetime(int(year), month, int(day), int(hh), int(mm), int(ss), tzinfo=IST)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:_MAX_TEXT] or None


def parse(
    payload: bytes, *, filename: str, l0_key: str | None = None
) -> tuple[BoardMeetingRow, ...]:
    """Parse one payload (a JSON array of intimations) into rows, sorted and de-duplicated.

    Raises :class:`ParseError` for a body that is not a JSON array of objects, or a record missing
    its ISIN, meeting date, purpose or dissemination time. An empty array is a legal answer (no
    meeting intimated in the window).
    """
    try:
        document: Any = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseError(f"body is not valid JSON: {exc}", filename=filename) from exc
    if not isinstance(document, list):
        raise ParseError(
            f"expected a JSON array of intimations, got {type(document).__name__}",
            filename=filename,
        )
    rows: dict[tuple[str, date, str, datetime], BoardMeetingRow] = {}
    for i, record in enumerate(document):
        if not isinstance(record, dict):
            raise ParseError(f"record {i} is not an object", filename=filename)
        isin = record.get("sm_isin")
        purpose = _text(record.get("bm_purpose"))
        if not isinstance(isin, str) or not re.fullmatch(ISIN_PATTERN, isin.strip()):
            raise ParseError(f"record {i} has no valid sm_isin ({isin!r})", filename=filename)
        if purpose is None:
            raise ParseError(f"record {i} ({isin}) has no bm_purpose", filename=filename)
        when = _timestamp(record.get("bm_timestamp"), filename=filename)
        description = _text(record.get("bm_desc"))
        row = BoardMeetingRow(
            isin=isin.strip(),
            symbol=_text(record.get("bm_symbol")),
            meeting_date=_date(record.get("bm_date"), filename=filename, field="bm_date"),
            purpose=purpose,
            description=description,
            disseminated_at=when,
            knowable_date=knowable_date_of(when),
            is_results=bool(RESULTS_PATTERN.search(f"{purpose} {description or ''}")),
            l0_key=l0_key,
        )
        rows.setdefault(row.key, row)
    return tuple(sorted(rows.values(), key=lambda r: (r.knowable_date, *r.key)))


def parse_l0(store: L0Store, ref: L0Ref) -> tuple[BoardMeetingRow, ...]:
    """Parse a payload read back (checksum re-verified) from L0."""
    return parse(store.get(ref), filename=ref.filename, l0_key=ref.key)


# ── L1 ────────────────────────────────────────────────────────────────────────────────────────

_L1_SCHEMA: Final = pa.schema(
    [
        pa.field("isin", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=True),
        pa.field("meeting_date", pa.date32(), nullable=False),
        pa.field("purpose", pa.string(), nullable=False),
        pa.field("description", pa.string(), nullable=True),
        pa.field("disseminated_at", pa.timestamp("s", tz="Asia/Kolkata"), nullable=False),
        pa.field("knowable_date", pa.date32(), nullable=False),
        pa.field("is_results", pa.bool_(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=True),
    ]
)


def _write_partition(day: date, rows: Sequence[BoardMeetingRow], data_root: Path | None) -> Path:
    path = l1_partition_path(BOARD_MEETINGS_DATASET, day, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda r: r.key)
    table = pa.Table.from_pylist([r.model_dump() for r in ordered], schema=_L1_SCHEMA)
    staging = path.with_name(f".{path.name}.partial")
    pq.write_table(table, staging, compression="snappy", version="2.6")
    staging.replace(path)
    return path


def _rows_of(path: Path) -> tuple[BoardMeetingRow, ...]:
    table = pq.read_table(path).cast(_L1_SCHEMA)
    return tuple(BoardMeetingRow(**record) for record in table.to_pylist())


def read_l1(knowable: date, *, data_root: Path | None = None) -> tuple[BoardMeetingRow, ...]:
    """One knowable-date partition; ``FileNotFoundError`` when it was never written."""
    path = l1_partition_path(BOARD_MEETINGS_DATASET, knowable, data_root=data_root)
    if not path.exists():
        raise FileNotFoundError(f"no {BOARD_MEETINGS_DATASET} partition for {knowable}: {path}")
    return _rows_of(path)


def merge_l1(rows: Iterable[BoardMeetingRow], *, data_root: Path | None = None) -> int:
    """Merge rows into their knowable-date partitions; return how many rows were new.

    What it does: for each knowable date the rows carry, rewrites that partition as the union of
    what it held and the new rows, keyed on :attr:`BoardMeetingRow.key` (the held row wins, so its
    original `l0_key` stays). Partitions with nothing new are not rewritten.
    What it never does: drop a held row, or move a row to another knowable date.
    """
    by_day: dict[date, dict[tuple[str, date, str, datetime], BoardMeetingRow]] = {}
    for row in rows:
        by_day.setdefault(row.knowable_date, {}).setdefault(row.key, row)
    added = 0
    for day, fresh in sorted(by_day.items()):
        try:
            held = {r.key: r for r in read_l1(day, data_root=data_root)}
        except FileNotFoundError:
            held = {}
        new = {k: r for k, r in fresh.items() if k not in held}
        if not new:
            continue
        added += len(new)
        path = _write_partition(day, [*held.values(), *new.values()], data_root)
        _LOG.info(
            "board_meetings.l1_merged",
            source=SOURCE_ID,
            knowable_date=day.isoformat(),
            rows=len(held) + len(new),
            added=len(new),
            path=str(path),
            state="NORMALIZED",
        )
    return added


def read_pit(
    through: date, *, start: date | None = None, data_root: Path | None = None
) -> tuple[BoardMeetingRow, ...]:
    """Every intimation knowable on or before ``through`` (and on or after ``start``, if given).

    The partition layout is the PIT guard: a partition dated after ``through`` is never opened.
    Raises ``FileNotFoundError`` when the dataset has no partition at all (never captured), so a
    reader can tell "no source" from "no meeting".
    """
    root = layer_root(Layer.L1, data_root=data_root) / BOARD_MEETINGS_DATASET
    parts = sorted(root.glob("date=*/*.parquet")) if root.exists() else []
    if not parts:
        raise FileNotFoundError(f"no {BOARD_MEETINGS_DATASET} partition under {root}")
    out: list[BoardMeetingRow] = []
    for path in parts:
        day = partition_date_of(path)
        if day > through or (start is not None and day < start):
            continue
        out.extend(_rows_of(path))
    return tuple(sorted(out, key=lambda r: (r.knowable_date, *r.key)))
