"""The canonical shape of one normalized news item, shared by the GDELT and RSS parsers (§4.1
row 14, "News / geopolitical"), and the L1 store it lands in.

Two very different sources converge on one row here, for the same reason the two bhavcopy eras
converge on `PriceRow` (see `models.py`): a consumer downstream of D1 — the M6.2 break-condition
matcher, the T2 monitor — must not carry a "was this GDELT or RSS?" branch. So GDELT events and
RSS headlines both emit `NewsRow`, and the fields a given source cannot supply are `None`/empty
rather than a second schema:

* **GDELT** carries no headline (an event is actors + an action, not an article title), so its
  `title` is `None`; it *does* carry named actors (`entities`) and an average `tone`.
* **RSS** carries a headline and a link but no sentiment and no entity extraction, so its
  `entities` is empty and its `tone` is `None`.

What every row must have is a **source timestamp** (`ts`) and a **link** (`url`): those are the
two things that make a news item point-in-time-usable and traceable. A source that cannot state
when an item was published cannot produce a PIT-correct row from it, so `ts` is required and
tz-aware — a naive datetime is a bug (invariant #7 lives or dies on knowing *when* a fact was
knowable), never an assumption about the host's locale.

**What this module never stores: an article body.** The register's §4.1 license note is
"headlines + links only" for RSS, and the structural guarantee of that is right here — `NewsRow`
has no `body`/`description`/`content` field, so an RSS parser physically cannot smuggle a full
article into L1 even by accident. `tests/unit/test_news_ingest.py` asserts the absence.

`tone` is a `Decimal`, reconstructed exactly from the source string and stored as its canonical
string in L1, so it never round-trips through binary floating point (the same discipline the money
fields keep, applied to a signed score that is not money but is still worth not corrupting).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, Protocol

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, field_validator

from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncRecord
from dataplatform.store.paths import l1_partition_path

__all__ = [
    "NEWS_DATASET",
    "NewsBatch",
    "NewsRow",
    "SyncTracker",
    "Tone",
    "dedupe",
    "read_l1",
    "write_l1",
]

_LOG = get_logger(__name__)

#: The L1 dataset name — `data/L1/news/date=YYYY-MM-DD/part.parquet` (§4.2). One partition per
#: ingest logical date; the rows inside carry their own source `ts`, which may predate it.
NEWS_DATASET: Final = "news"

#: A sentiment score, signed and unbounded. `strict` keeps a float from being constructed into
#: one, `allow_inf_nan=False` keeps a mis-parsed field that spells `NaN`/`Infinity` from becoming
#: a score that compares against every other. It is not money, but corrupting it silently is the
#: same class of bug the money guard exists to stop, so it gets the same guard.
Tone = Annotated[Decimal, Field(strict=True, allow_inf_nan=False)]


class NewsRow(BaseModel):
    """One normalized news item — the canonical D1 output for the News/geopolitical dataset.

    What it does: carry the point-in-time facts a news source states about one item, in the types
    the rest of the platform may compute with: when it was published (`ts`), who published a
    pointer to it (`source`), the headline if there is one (`title`), the link (`url`), the named
    entities the source tagged (`entities`), and a sentiment score if the source scored it
    (`tone`).
    What it assumes: the parser that built it has already resolved the source timestamp and
    checked the link — a `NewsRow` that exists is an item the source really published, at a time
    the source really stated.
    What it never does: hold an article body. There is no field for one; the register permits
    headlines and links only for RSS, and this schema enforces that by construction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime = Field(description="source publication timestamp, tz-aware — the item's PIT")
    source: str = Field(min_length=1, description="feed/source id: 'gdelt' or a curated RSS id")
    title: str | None = Field(
        default=None, description="headline as published; None for GDELT events (no headline)"
    )
    url: str = Field(min_length=1, description="link to the article or source document")
    entities: tuple[str, ...] = Field(
        default=(), description="named actors/orgs the source tagged; () when it tags none"
    )
    tone: Tone | None = Field(
        default=None, description="signed sentiment score; None when the source states none"
    )

    @field_validator("ts")
    @classmethod
    def _timestamp_is_aware(cls, value: datetime) -> datetime:
        """A news timestamp with no timezone is not a point in time — reject it loudly.

        PIT correctness (invariant #7) rests on comparing a fact's knowable instant to a decision
        instant; a naive datetime silently adopts whatever zone the reader assumes, which is
        exactly how a fact leaks into a decision made before it existed. A parser localizes the
        source's stated time itself and hands over an aware object.
        """
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "news timestamp is naive; a source time must be tz-aware to be point-in-time "
                "usable (the parser localizes it — an offset is never assumed here)"
            )
        return value

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str | None) -> str | None:
        """A present title is a real one. An empty headline is a parse failure, not a headline."""
        if value is not None and not value.strip():
            raise ValueError("title is present but blank; omit it (None) rather than store empty")
        return value


class NewsBatch(BaseModel):
    """One ingest's worth of rows — the unit an L1 partition is written from.

    What it does: hold the rows a single fetch produced together with the logical date they file
    under and the L0 payload they were derived from, so an L1 partition can name its lineage.
    What it assumes: every row belongs to this ingest's `logical_date` partition; the rows' own
    `ts` may be earlier (a feed lists items published over the preceding hours).
    What it never does: exist without a source. An empty batch is legal — a 15-minute GDELT slot
    or an RSS poll can genuinely carry nothing new — and writes an empty, well-formed partition.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_date: date = Field(description="the ingest date this batch's partition files under")
    source: str = Field(min_length=1, description="Source Register id the payload came from")
    l0_key: str | None = Field(
        default=None, description="`source/date/filename` of the L0 payload this was derived from"
    )
    rows: tuple[NewsRow, ...] = Field(default=(), description="the normalized items, in feed order")


class SyncTracker(Protocol):
    """The slice of the §4.4 state machine (M1.3) one ingest drives.

    A structural protocol rather than the concrete `SyncStateStore`, so an ingest can be driven
    end to end offline (B8) without Postgres — the store satisfies it, and a test double satisfies
    it too. It is the whole happy path plus `mark_failed`: a runner that could reach `PUBLISHED`
    without passing `VALIDATED` would be a second state machine.
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


#: The L1 schema, declared once and enforced on write (§4.2, M1.8's rule). `ts` is stored as a
#: UTC instant — the source's zone is folded into the instant, which is the fact PIT cares about;
#: `tone` is stored as its canonical decimal string, lossless and never a float; `entities` is a
#: list column so a row's actors survive the round trip.
_L1_SCHEMA: Final = pa.schema(
    [
        pa.field("ts", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("title", pa.string(), nullable=True),
        pa.field("url", pa.string(), nullable=False),
        pa.field(
            "entities", pa.list_(pa.field("item", pa.string(), nullable=False)), nullable=False
        ),
        pa.field("tone", pa.string(), nullable=True),
        pa.field("logical_date", pa.date32(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=True),
    ]
)


def write_l1(batch: NewsBatch, *, data_root: Path | None = None) -> Path:
    """Write one ingest's rows to its L1 partition and return the file's path.

    Idempotent per `(dataset, logical_date)`: the file is written whole to a temporary name and
    renamed over the target, so re-deriving the same batch from L0 produces the same bytes and a
    crash mid-write cannot leave a half partition readable. An empty batch writes an empty,
    schema-correct partition rather than nothing, so "ingested, nothing new" is distinguishable
    from "never ingested".

    No adjusted or derived values here — a news row is what the source stated — and the batch's
    `l0_key` rides on every row so each is traceable to the payload it came from.
    """
    path = l1_partition_path(NEWS_DATASET, batch.logical_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [
            {
                "ts": row.ts,
                "source": row.source,
                "title": row.title,
                "url": row.url,
                "entities": list(row.entities),
                "tone": None if row.tone is None else str(row.tone),
                "logical_date": batch.logical_date,
                "l0_key": batch.l0_key,
            }
            for row in batch.rows
        ],
        schema=_L1_SCHEMA,
    )
    staging = path.with_name(f".{path.name}.partial")
    pq.write_table(table, staging, compression="snappy", version="2.6")
    staging.replace(path)
    _LOG.info(
        "news.l1_written",
        source=batch.source,
        logical_date=batch.logical_date.isoformat(),
        dataset=NEWS_DATASET,
        path=str(path),
        rows=len(batch.rows),
        l0_key=batch.l0_key,
        state="NORMALIZED",
    )
    return path


def read_l1(logical_date: date, *, data_root: Path | None = None) -> NewsBatch:
    """Read one ingest's partition back out of L1.

    The round trip `write_l1` is verified against. Raises `FileNotFoundError` when the partition
    was never written — an absent partition is a gap for D7 to explain, not an empty batch (which
    is a partition that exists and holds zero rows).
    """
    path = l1_partition_path(NEWS_DATASET, logical_date, data_root=data_root)
    if not path.exists():
        raise FileNotFoundError(
            f"no {NEWS_DATASET} partition for {logical_date.isoformat()}: {path}"
        )
    records = pq.read_table(path, schema=_L1_SCHEMA).to_pylist()
    source = str(records[0]["source"]) if records else NEWS_DATASET
    l0_key = records[0]["l0_key"] if records else None
    return NewsBatch(
        logical_date=logical_date,
        source=source,
        l0_key=l0_key,
        rows=tuple(
            NewsRow(
                ts=record["ts"],
                source=record["source"],
                title=record["title"],
                url=record["url"],
                entities=tuple(record["entities"]),
                tone=None if record["tone"] is None else Decimal(record["tone"]),
            )
            for record in records
        ),
    )


def dedupe(rows: Iterable[NewsRow]) -> tuple[NewsRow, ...]:
    """Drop exact repeats while keeping first-seen order.

    A GDELT slot lists the same article under several events, and an RSS poll overlaps the
    previous one; the identity of a news item for this purpose is its `(source, url, ts)`. Kept a
    plain helper rather than folded into the parsers so both share one notion of "same item".
    """
    seen: set[tuple[str, str, datetime]] = set()
    out: list[NewsRow] = []
    for row in rows:
        key = (row.source, row.url, row.ts)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return tuple(out)
