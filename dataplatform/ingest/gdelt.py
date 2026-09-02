"""GDELT 2.0 event ingestion (§4.1 row 14, "News / geopolitical") — the free/open global news
feed → L1 `news`.

GDELT publishes a new batch every 15 minutes. The batch is discovered through one small manifest,
`lastupdate.txt`, which names the three files of the slot (export, mentions, GKG) with their sizes
and MD5s. This parser takes the **export** file: one row per detected event, carrying the actors
involved, an average tone, the source article URL and the instant GDELT added it
(`DATEADDED`, UTC). That is the cheapest of the three files (~75 kB zipped vs ~4 MB for the GKG)
and it already carries the four things the news dataset needs from GDELT: a source timestamp, a
link, named entities, and a tone. It does **not** carry a headline — a GDELT event is actors plus
an action, not an article title — so `NewsRow.title` is `None` for every GDELT row, by design and
not by omission.

Why the manifest matters beyond discovery: it states the export file's MD5, and this module
cross-checks it against the bytes L0 actually stored (`ops/gates/source-verification.md` §6, the
register's `parse_check` note). A slot whose export does not match its own manifest is a corrupt
download, not news, and it fails loud rather than landing garbage in L1.

Timestamps are natural PIT (invariant #7): `DATEADDED` is `YYYYMMDDHHMMSS` in UTC, and the slot
timestamp is in the filename. No clock is consulted to date a row — the source dates it.

HTTPS caveat (register note): `data.gdeltproject.org` is a CNAME to Google Cloud Storage and
presents a certificate for that name, so `https://` fails validation; plain `http://` is the
published access path and the payload's own MD5 is the integrity control. The URLs in the manifest
are therefore `http://`, and this parser fetches them exactly as the manifest names them.

Offline by construction (B8): parsing takes bytes (or an `L0Ref` read back through `L0Store`).
`ingest_slice` is the one function that drives a fetch, and it does so through the crawl engine,
still the only socket on the ingestion path.
"""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from dataplatform.ingest.fetcher import (
    Fetcher,
    FetchHTTPError,
    ForbiddenSpikeError,
    RetryableFetchError,
)
from dataplatform.ingest.models import IngestError, ParseError
from dataplatform.ingest.news import NEWS_DATASET, NewsBatch, NewsRow, SyncTracker, dedupe, write_l1
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "EXPORT_SUFFIX",
    "GDELT_SOURCE",
    "SOURCE_ID",
    "ManifestEntry",
    "ingest_slice",
    "parse_export",
    "parse_export_l0",
    "parse_manifest",
    "slot_from_export_url",
]

_LOG = get_logger(__name__)

#: The register id this parser fetches under (`source_register.yaml`, `parser.task: M6.1`).
SOURCE_ID: Final = "gdelt_v2_event_files"

#: The `source` stamped on every GDELT-derived `NewsRow`. One value, so a consumer never has to
#: know which of GDELT's three files a row came from — only that it is GDELT.
GDELT_SOURCE: Final = "gdelt"

#: The manifest line whose file this parser reads. `.export.CSV.zip` is the event table.
EXPORT_SUFFIX: Final = ".export.CSV.zip"

#: GDELT 2.0 export column positions (0-indexed), confirmed against a live slot on 2026-09-02
#: (`ops/gates/source-verification.md` §6). The export row has 61 tab-separated columns.
_ACTOR1_NAME: Final = 6
_ACTOR2_NAME: Final = 16
_AVG_TONE: Final = 34
_DATE_ADDED: Final = 59
_SOURCE_URL: Final = 60
_MIN_COLUMNS: Final = 61

#: `DATEADDED` and the slot filename are both UTC (GDELT states this in its codebook).
_UTC: Final = ZoneInfo("UTC")


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One line of `lastupdate.txt`: a file's byte size, its MD5, and its URL."""

    size_bytes: int
    md5: str
    url: str


def parse_manifest(payload: bytes, *, filename: str) -> tuple[ManifestEntry, ...]:
    """Parse `lastupdate.txt` into its three file entries.

    Each line is `size md5 url`, whitespace-separated. Raises `ParseError`, naming the file, for
    anything that is not that shape — an empty body, an HTML soft-404, a line with the wrong field
    count, or a size that is not an integer. The returned tuple is in manifest order.
    """
    text = _decode(payload, filename=filename)
    entries: list[ManifestEntry] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 3:
            raise ParseError(
                f"manifest line has {len(parts)} fields, expected 3 (size md5 url)",
                filename=filename,
                line=lineno,
            )
        size_text, md5, url = parts
        try:
            size = int(size_text)
        except ValueError as exc:
            raise ParseError(
                f"manifest size {size_text!r} is not an integer", filename=filename, line=lineno
            ) from exc
        entries.append(ManifestEntry(size_bytes=size, md5=md5.lower(), url=url))
    if not entries:
        raise ParseError("manifest carries no file entries", filename=filename)
    return tuple(entries)


def export_entry(entries: tuple[ManifestEntry, ...], *, filename: str) -> ManifestEntry:
    """The manifest entry for the export file — the one this parser reads.

    Raises `ParseError` if the slot names no export file, which would mean the manifest format
    changed and the whole discovery assumption needs re-checking rather than a silent skip.
    """
    for entry in entries:
        if entry.url.endswith(EXPORT_SUFFIX):
            return entry
    raise ParseError(
        f"manifest names no {EXPORT_SUFFIX} file; got "
        f"{', '.join(e.url.rsplit('/', 1)[-1] for e in entries)}",
        filename=filename,
    )


def slot_from_export_url(url: str) -> datetime:
    """The 15-minute slot instant from an export URL like `.../20260902074500.export.CSV.zip`.

    UTC, because GDELT slot timestamps are UTC. Raises `IngestError` if the name does not start
    with a 14-digit timestamp — the parser will not invent a slot time for a file it cannot date.
    """
    name = url.rsplit("/", 1)[-1]
    stamp = name.split(".", 1)[0]
    if len(stamp) != 14 or not stamp.isdigit():
        raise IngestError(
            f"cannot read a slot timestamp from GDELT filename {name!r}; expected a 14-digit "
            "YYYYMMDDHHMMSS prefix"
        )
    try:
        return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=_UTC)
    except ValueError as exc:
        raise IngestError(f"GDELT filename {name!r} carries an impossible timestamp") from exc


def parse_export(payload: bytes, *, filename: str) -> tuple[NewsRow, ...]:
    """Parse a GDELT 2.0 `.export.CSV.zip` payload into news rows.

    Assumes `payload` is the zip exactly as served (the whole slot file). Each event row becomes a
    `NewsRow` with the event's actors as entities, its `AvgTone` as tone, `DATEADDED` as the
    source timestamp, and `SOURCEURL` as the link. An event with no source URL is not a news link
    and is skipped — GDELT emits events detected from non-web material — rather than fabricating a
    row without a target. Exact duplicates (the same article under several events) collapse to one.

    Raises `ParseError`, naming the file, for anything that is not this format: a body that is not
    a zip, a zip with no member, a row with too few columns, or a `DATEADDED`/`AvgTone` that will
    not parse. A malformed row stops the slot rather than landing a corrupt event.
    """
    csv_bytes = _unzip_single_member(payload, filename=filename)
    text = _decode(csv_bytes, filename=filename)
    rows: list[NewsRow] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        row = _event_row(raw, lineno=lineno, filename=filename)
        if row is not None:
            rows.append(row)
    deduped = dedupe(rows)
    _LOG.info(
        "gdelt.parsed",
        source=GDELT_SOURCE,
        filename=filename,
        events=len(rows),
        rows=len(deduped),
        state="VALIDATED",
    )
    return deduped


def parse_export_l0(store: L0Store, ref: L0Ref) -> tuple[NewsRow, ...]:
    """Parse the L0 export payload a fetch produced, re-verifying its checksum on the way in.

    `L0Store.get` re-hashes the payload, which makes "every L1 value derives from bytes that have
    not changed" true where the derivation happens.
    """
    return parse_export(store.get(ref), filename=ref.filename)


def _event_row(line: str, *, lineno: int, filename: str) -> NewsRow | None:
    """One export line → one `NewsRow`, or `None` when the event has no source URL."""
    fields = line.split("\t")
    if len(fields) < _MIN_COLUMNS:
        raise ParseError(
            f"export row has {len(fields)} columns, expected at least {_MIN_COLUMNS}",
            filename=filename,
            line=lineno,
        )
    url = fields[_SOURCE_URL].strip()
    if not url:
        return None
    ts = _date_added(fields[_DATE_ADDED], lineno=lineno, filename=filename)
    tone = _tone(fields[_AVG_TONE], lineno=lineno, filename=filename)
    entities = tuple(
        dict.fromkeys(
            name for name in (fields[_ACTOR1_NAME].strip(), fields[_ACTOR2_NAME].strip()) if name
        )
    )
    try:
        return NewsRow(
            ts=ts, source=GDELT_SOURCE, title=None, url=url, entities=entities, tone=tone
        )
    except ValidationError as exc:
        raise ParseError(f"event row: {exc}", filename=filename, line=lineno) from exc


def _date_added(value: str, *, lineno: int, filename: str) -> datetime:
    """`20260902074500` → `datetime(..., tzinfo=UTC)`. GDELT states `DATEADDED` is UTC."""
    stamp = value.strip()
    if len(stamp) != 14 or not stamp.isdigit():
        raise ParseError(
            f"DATEADDED {value!r} is not a 14-digit UTC timestamp", filename=filename, line=lineno
        )
    try:
        return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=_UTC)
    except ValueError as exc:
        raise ParseError(
            f"DATEADDED {value!r} is not a real timestamp", filename=filename, line=lineno
        ) from exc


def _tone(value: str, *, lineno: int, filename: str) -> Decimal:
    """`AvgTone` as an exact `Decimal`, never a float. Rejects NaN/Infinity."""
    text = value.strip()
    try:
        tone = Decimal(text)
    except InvalidOperation as exc:
        raise ParseError(
            f"AvgTone {value!r} is not a decimal", filename=filename, line=lineno
        ) from exc
    if not tone.is_finite():
        raise ParseError(f"AvgTone {value!r} is not finite", filename=filename, line=lineno)
    return tone


def _unzip_single_member(payload: bytes, *, filename: str) -> bytes:
    """The bytes of the zip's single member — GDELT slot files hold exactly one CSV."""
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            names = archive.namelist()
            if len(names) != 1:
                raise ParseError(
                    f"export zip holds {len(names)} members, expected 1: {names}", filename=filename
                )
            return archive.read(names[0])
    except zipfile.BadZipFile as exc:
        raise ParseError(f"body is not a valid zip: {exc}", filename=filename) from exc


def _decode(payload: bytes, *, filename: str) -> str:
    """Decode a text payload, refusing an empty body and an HTML page wearing a 200.

    GDELT export cells are Latin-1-ish (article titles from many locales); `utf-8` with
    `replace` keeps a single odd byte from failing a whole slot, while still rejecting a body that
    is clearly not the feed (markup) or is empty.
    """
    if not payload.strip():
        raise ParseError("empty response body", filename=filename)
    text = payload.decode("utf-8", errors="replace")
    if text.lstrip()[:1] == "<":
        raise ParseError(
            "body is markup, not the GDELT feed — an HTML error page answered with a 200",
            filename=filename,
        )
    return text


def _manifest_url(register: SourceRegister | None = None) -> str:
    """The discovery manifest URL, read from the Source Register rather than repeated here."""
    reg = load_register() if register is None else register
    source = next((entry for entry in reg.sources if entry.id == SOURCE_ID), None)
    if source is None:
        raise IngestError(f"no {SOURCE_ID!r} entry in the Source Register")
    return source.url_template


def ingest_slice(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SyncTracker,
    logical_date: date,
    register: SourceRegister | None = None,
    data_root: Path | None = None,
) -> NewsBatch:
    """Take one GDELT slot from nothing to `PUBLISHED`: manifest → export → L0 → parse → L1.

    What it does: fetches the discovery manifest, resolves the export file it names, fetches that
    into L0, cross-checks the stored bytes against the manifest's MD5, parses the events, writes
    the L1 partition, and drives the §4.4 transitions in order around the fetch and the write.
    What it assumes: the manifest names one slot (GDELT's "latest"); `logical_date` is the ingest
    date the caller wants this slot filed under.
    What it never does: land a slot whose export bytes disagree with the manifest's own MD5, or
    file a row it could not date from the source.

    Any failure is recorded on the sync row — with `retryable` set from what actually went wrong —
    and then re-raised, so the caller sees the exception and `/status/sync` sees the state.
    """
    manifest_url = _manifest_url(register)
    tracker.begin(SOURCE_ID, logical_date)
    try:
        manifest_ref = fetcher.fetch(SOURCE_ID, manifest_url, logical_date)
        entries = parse_manifest(l0.get(manifest_ref), filename=manifest_ref.filename)
        entry = export_entry(entries, filename=manifest_ref.filename)

        export_ref = fetcher.fetch(SOURCE_ID, entry.url, logical_date)
        _verify_md5(l0, export_ref, expected=entry.md5)
        tracker.mark_fetched(
            SOURCE_ID, logical_date, checksum=export_ref.sha256, l0_path=export_ref.key
        )

        rows = parse_export_l0(l0, export_ref)
        tracker.mark_validated(SOURCE_ID, logical_date)

        batch = NewsBatch(
            logical_date=logical_date, source=GDELT_SOURCE, l0_key=export_ref.key, rows=rows
        )
        write_l1(batch, data_root=data_root)
        tracker.mark_normalized(SOURCE_ID, logical_date)

        tracker.mark_published(SOURCE_ID, logical_date)
    except Exception as exc:
        tracker.mark_failed(
            SOURCE_ID, logical_date, f"{type(exc).__name__}: {exc}", retryable=_retryable(exc)
        )
        _LOG.error(
            "gdelt.ingest_failed",
            source=SOURCE_ID,
            logical_date=logical_date.isoformat(),
            error=f"{type(exc).__name__}: {exc}",
            retryable=_retryable(exc),
            state="FAILED",
        )
        raise

    _LOG.info(
        "gdelt.published",
        source=SOURCE_ID,
        logical_date=logical_date.isoformat(),
        dataset=NEWS_DATASET,
        rows=len(batch.rows),
        l0_key=batch.l0_key,
        state="PUBLISHED",
    )
    return batch


def _verify_md5(l0: L0Store, ref: L0Ref, *, expected: str) -> None:
    """Cross-check the stored export bytes against the manifest's MD5 (register §6 note).

    GDELT's own integrity control. A mismatch is a corrupt or truncated download, not news, and it
    is a `ParseError` (non-retryable format failure) rather than a row that reaches L1.
    """
    digest = hashlib.md5(l0.get(ref)).hexdigest()
    if digest != expected.lower():
        raise ParseError(
            f"export MD5 {digest} does not match the manifest's {expected.lower()}; the download "
            "is corrupt or truncated and must not become news rows",
            filename=ref.filename,
        )


def _retryable(exc: BaseException) -> bool:
    """Whether repeating this attempt later could ever produce a different outcome."""
    if isinstance(exc, ForbiddenSpikeError):
        return False
    if isinstance(exc, RetryableFetchError):
        return True
    return not isinstance(exc, ParseError | FetchHTTPError)
