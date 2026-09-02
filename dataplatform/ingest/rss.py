"""Curated RSS ingestion (§4.1 row 14, "News / geopolitical") — headlines + links → L1 `news`.

The counterpart to GDELT: where GDELT is a firehose of machine-detected events, this is a small,
**curated** set of feeds we choose (the register's fallback for the news row names it explicitly:
"curated RSS (PIB, business press headlines)"). The set lives in `data/rss_feeds.yaml`, one entry
per feed, and this module fetches, parses and normalizes each into the same `NewsRow` GDELT emits.

**Headlines and links only — the license line (§4.1) is enforced structurally.** An RSS `<item>`
often carries a `<description>` holding the whole article; this parser reads `<title>`, `<link>`
and the item's timestamp and *nothing else*, and `NewsRow` has no body field to put a description
in even if it wanted to. `tests/unit/test_news_ingest.py` proves against the RBI fixture — whose
descriptions are full press releases — that not a byte of body reaches L1.

**A news row must be datable.** RSS timestamps are the item's natural PIT, and this parser
resolves one per item from, in order: the item's `<pubDate>` (RFC 822), its `<dc:date>` (ISO
8601), else the channel's `<lastBuildDate>`/`<pubDate>`. A feed that supplies none of these cannot
produce a PIT-correct row, and the parser raises rather than dating an item from the wall clock —
which is why PIB's `RssMain.aspx` feed, real and curated but carrying no per-item timestamps, is
listed `active: false` in `rss_feeds.yaml` with that reason recorded, not silently backfilled with
a fetch time. An RFC-822 date with no zone (RBI publishes these) is read as Asia/Kolkata, the
exchange zone — never as the host's locale.

Entities and tone are GDELT's to give; RSS states neither, so every RSS row has empty `entities`
and `tone=None`. That is the honest shape, not a gap: a downstream consumer reads the same schema
for both and simply finds fewer fields populated for a headline than for a scored event.

Offline by construction (B8): parsing takes bytes (or an `L0Ref` read back through `L0Store`), and
`ingest_feed` drives its one fetch through the crawl engine.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dataplatform.clock import IST
from dataplatform.ingest.fetcher import (
    Fetcher,
    FetchHTTPError,
    ForbiddenSpikeError,
    RetryableFetchError,
)
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.news import NEWS_DATASET, NewsBatch, NewsRow, SyncTracker, dedupe, write_l1
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "FEEDS_PATH",
    "Feed",
    "FeedSet",
    "active_feeds",
    "ingest_feed",
    "load_feeds",
    "parse_feed",
    "parse_feed_l0",
]

_LOG = get_logger(__name__)

#: The curated feed list, checked in beside this module.
FEEDS_PATH: Final[Path] = Path(__file__).with_name("data") / "rss_feeds.yaml"

#: The Dublin Core namespace, for feeds that date items with `<dc:date>` instead of `<pubDate>`.
_DC_DATE: Final = "{http://purl.org/dc/elements/1.1/}date"


class Feed(BaseModel):
    """One curated RSS feed: what it is, where it lives, and how we fetch it.

    `source` is the Source Register id whose crawl policy (host, headers, robots, spacing) governs
    the fetch — so a feed's host must match its `source` row's host, exactly as the fetcher
    enforces. `active` is whether the feed is ingested now; an inactive feed carries a `reason` so
    "why is this listed but not pulled" is answered in the file, not lost.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, description="stable feed id, stamped as NewsRow.source")
    name: str = Field(min_length=1, description="human name")
    publisher: str = Field(min_length=1, description="who publishes it")
    url: str = Field(min_length=1, description="the feed URL")
    host: str = Field(min_length=1, description="the host, which must match the register source")
    source: str = Field(min_length=1, description="Source Register id whose policy governs fetches")
    active: bool = Field(description="whether this feed is ingested now")
    reason: str | None = Field(default=None, description="why an inactive feed is not ingested")

    @property
    def l0_filename(self) -> str:
        """A stable L0 filename for this feed's payload.

        The feed URL often carries no dated path segment (`pressreleases_rss.xml`), so the date is
        supplied by L0's partitioning and the feed id keeps two feeds on one host from colliding.
        """
        return f"{self.id}.xml"


class FeedSet(BaseModel):
    """The whole `rss_feeds.yaml`: a version and the curated feeds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    feeds: tuple[Feed, ...]


def load_feeds(path: Path = FEEDS_PATH) -> FeedSet:
    """Parse and type-check the curated feed list. Raises on a schema break or a missing file."""
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return FeedSet.model_validate(raw)


def active_feeds(path: Path = FEEDS_PATH) -> tuple[Feed, ...]:
    """The feeds that are ingested now — the daily job's working set."""
    return tuple(feed for feed in load_feeds(path).feeds if feed.active)


def parse_feed(payload: bytes, feed: Feed, *, filename: str) -> tuple[NewsRow, ...]:
    """Parse one feed's RSS 2.0 payload into news rows — headlines and links only.

    Assumes `payload` is one whole feed document. Reads each `<item>`'s `<title>`, `<link>` and
    timestamp; deliberately ignores `<description>` and every other element (the license line is
    headlines + links only). The item timestamp resolves from `<pubDate>` → `<dc:date>` → the
    channel's `<lastBuildDate>`/`<pubDate>`.

    Raises `ParseError`, naming the file, for a body that is not RSS, an item missing its title or
    link, or an item with no resolvable timestamp — the last because a news row that cannot be
    dated is not point-in-time usable, and this parser never dates an item from the wall clock.
    """
    root = _parse_xml(payload, filename=filename)
    channel = root.find("channel")
    if channel is None:
        raise ParseError("not an RSS document: no <channel> element", filename=filename)
    channel_ts = _channel_timestamp(channel)
    rows: list[NewsRow] = []
    for index, item in enumerate(channel.findall("item")):
        rows.append(_item_row(item, feed, channel_ts=channel_ts, index=index, filename=filename))
    deduped = dedupe(rows)
    _LOG.info(
        "rss.parsed",
        source=feed.id,
        filename=filename,
        items=len(rows),
        rows=len(deduped),
        state="VALIDATED",
    )
    return deduped


def parse_feed_l0(store: L0Store, ref: L0Ref, feed: Feed) -> tuple[NewsRow, ...]:
    """Parse the L0 payload a fetch produced, re-verifying its checksum on the way in."""
    return parse_feed(store.get(ref), feed, filename=ref.filename)


def _item_row(
    item: ET.Element, feed: Feed, *, channel_ts: datetime | None, index: int, filename: str
) -> NewsRow:
    """One `<item>` → one `NewsRow`, dated from the source, carrying no body."""
    title = _text(item.find("title"))
    if title is None:
        raise ParseError(f"item {index}: no <title>", filename=filename)
    url = _text(item.find("link"))
    if url is None:
        raise ParseError(f"item {index}: no <link>", filename=filename)
    ts = _item_timestamp(item, channel_ts=channel_ts)
    if ts is None:
        raise ParseError(
            f"item {index} ({title[:60]!r}) has no resolvable timestamp; the feed carries no "
            "<pubDate>, <dc:date> or channel date, so its items cannot be point-in-time dated "
            "(mark the feed inactive with that reason rather than dating from the wall clock)",
            filename=filename,
        )
    try:
        return NewsRow(ts=ts, source=feed.id, title=title, url=url, entities=(), tone=None)
    except ValidationError as exc:
        raise ParseError(f"item {index}: {exc}", filename=filename) from exc


def _item_timestamp(item: ET.Element, *, channel_ts: datetime | None) -> datetime | None:
    """The item's source timestamp: item `<pubDate>` → `<dc:date>` → channel date, else None."""
    pub = _text(item.find("pubDate"))
    if pub is not None:
        parsed = _rfc822(pub)
        if parsed is not None:
            return parsed
    dc = _text(item.find(_DC_DATE))
    if dc is not None:
        parsed = _iso8601(dc)
        if parsed is not None:
            return parsed
    return channel_ts


def _channel_timestamp(channel: ET.Element) -> datetime | None:
    """A channel-level fallback date: `<lastBuildDate>` then `<pubDate>`."""
    for tag in ("lastBuildDate", "pubDate"):
        text = _text(channel.find(tag))
        if text is not None:
            parsed = _rfc822(text)
            if parsed is not None:
                return parsed
    return None


def _rfc822(value: str) -> datetime | None:
    """Parse an RFC-822 date (`Wed, 02 Sep 2026 11:30:00 +0530`), localizing a zone-less one.

    RSS `<pubDate>` is RFC 822. A date with no zone (RBI publishes these) is read as Asia/Kolkata,
    the exchange zone — the one defensible default for an Indian feed, never the host's locale.
    Returns None on an unparseable string so the caller can fall back rather than crash on one bad
    date.
    """
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=IST)
    return parsed


def _iso8601(value: str) -> datetime | None:
    """Parse an ISO-8601 `<dc:date>`, localizing a zone-less one to Asia/Kolkata."""
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=IST)
    return parsed


def _text(element: ET.Element | None) -> str | None:
    """The stripped text of an element, or None when the element is absent or empty."""
    if element is None or element.text is None:
        return None
    text = element.text.strip()
    return text or None


def _parse_xml(payload: bytes, *, filename: str) -> ET.Element:
    """Parse a feed body, refusing an empty one and a non-XML one loudly.

    The XML comes from a small, curated set of trusted publishers (`rss_feeds.yaml`), fetched
    through our own crawl engine and checksummed into L0 before it reaches here, so the stdlib
    parser is used rather than a hardened one — the feeds are chosen, not arbitrary user input. A
    body that will not parse is a `ParseError`, not a crash.
    """
    if not payload.strip():
        raise ParseError("empty response body", filename=filename)
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ParseError(f"body is not parseable XML: {exc}", filename=filename) from exc


def ingest_feed(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SyncTracker,
    feed: Feed,
    logical_date: date,
    data_root: Path | None = None,
) -> NewsBatch:
    """Take one feed from nothing to `PUBLISHED`: fetch → L0 → parse → L1 → sync_state.

    What it does: drives the §4.4 transitions in order around the fetch and the write, under the
    crawl policy of the feed's `source` register row.
    What it assumes: the feed is active and its host matches its `source` row's host (the fetcher
    enforces the latter).
    What it never does: store an article body, or land a row it could not date from the source.

    Any failure is recorded on the sync row with `retryable` set from the cause, then re-raised.
    """
    tracker.begin(feed.source, logical_date)
    try:
        ref = fetcher.fetch(feed.source, feed.url, logical_date, filename=feed.l0_filename)
        tracker.mark_fetched(feed.source, logical_date, checksum=ref.sha256, l0_path=ref.key)

        rows = parse_feed_l0(l0, ref, feed)
        tracker.mark_validated(feed.source, logical_date)

        batch = NewsBatch(logical_date=logical_date, source=feed.id, l0_key=ref.key, rows=rows)
        write_l1(batch, data_root=data_root)
        tracker.mark_normalized(feed.source, logical_date)

        tracker.mark_published(feed.source, logical_date)
    except Exception as exc:
        tracker.mark_failed(
            feed.source, logical_date, f"{type(exc).__name__}: {exc}", retryable=_retryable(exc)
        )
        _LOG.error(
            "rss.ingest_failed",
            source=feed.source,
            feed=feed.id,
            logical_date=logical_date.isoformat(),
            error=f"{type(exc).__name__}: {exc}",
            retryable=_retryable(exc),
            state="FAILED",
        )
        raise

    _LOG.info(
        "rss.published",
        source=feed.source,
        feed=feed.id,
        logical_date=logical_date.isoformat(),
        dataset=NEWS_DATASET,
        rows=len(batch.rows),
        l0_key=batch.l0_key,
        state="PUBLISHED",
    )
    return batch


def _retryable(exc: BaseException) -> bool:
    """Whether repeating this attempt later could ever produce a different outcome."""
    if isinstance(exc, ForbiddenSpikeError):
        return False
    if isinstance(exc, RetryableFetchError):
        return True
    return not isinstance(exc, ParseError | FetchHTTPError)
