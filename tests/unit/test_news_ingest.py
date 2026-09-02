"""M6.1 — GDELT 2.0 + curated RSS/PIB ingestion.

The three acceptance criteria, and the file is laid out as them:

1. **GDELT and RSS rows land with source timestamps preserved and L0 lineage.** Proved end to end
   against frozen real payloads: a fetch through the real crawl engine (recorded transport, never a
   socket) writes L0, parses, writes the L1 `news` partition, and drives §4.4 from `PENDING` to
   `PUBLISHED` — with each row carrying the exact instant the source stated and the `l0_key` of the
   payload it came from. GDELT dates from `DATEADDED` (UTC); RBI dates from `<pubDate>` (zone-less
   → Asia/Kolkata). The timestamp assertions fail if the localisation is dropped or inverted.

2. **No full article bodies are stored for RSS sources.** Proved two ways: structurally, `NewsRow`
   has no body/description/content field at all; and against data, the RBI fixture's `<item>`s
   carry full HTML press releases in `<description>`, yet no stored title or url contains markup —
   the description never crosses into L1.

3. **Both source register rows flip to VERIFIED with evidence.** Asserted against the checked-in
   `source_register.yaml`: `gdelt_v2_event_files` and `curated_rss` are both VERIFIED and both
   carry a real successful fetch (status, bytes, checksum, parse note).

Offline by construction (B8): an autouse fixture makes any socket in this module an error.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest import gdelt, rss
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
    ScriptedOutcome,
)
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.news import NewsBatch, NewsRow, read_l1, write_l1
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncRecord, SyncState
from dataplatform.store.l0 import L0Store
from tests.conftest import SettingsLoader

_UTC: Final = ZoneInfo("UTC")
NOW: Final = datetime(2026, 9, 2, 20, 0, tzinfo=IST)
LOGICAL_DATE: Final = date(2026, 9, 2)

GDELT_MANIFEST: Final = "gdelt/v2/lastupdate.txt"
GDELT_EXPORT: Final = "gdelt/v2/20260902074500.export.CSV.zip"
RBI_FEED: Final = "rss/rbi/2026-09-02/pressreleases_rss.xml"
PIB_FEED: Final = "rss/pib/2026-09-02/RssMain.xml"


# ── a recording sync tracker (M1.3's pure half, no Postgres) ──────────────────────────────────


class RecordingTracker:
    """Drives the real `SyncRecord.transition`, so it is the same state machine the store applies.

    `history` is the ordered list of states a `(source, date)` passed through — what an assertion
    about "PENDING then FETCHED then …" wants to look at.
    """

    def __init__(self, clock: FrozenClock) -> None:
        self._clock = clock
        self.rows: dict[tuple[str, date], SyncRecord] = {}
        self.history: list[SyncState] = []

    def begin(self, source: str, logical_date: date) -> SyncRecord:
        existing = self.rows.get((source, logical_date))
        now = self._clock.now()
        record = (
            SyncRecord(
                source=source,
                logical_date=logical_date,
                state=SyncState.PENDING,
                updated_at=now,
                attempts=1,
                first_attempt_at=now,
            )
            if existing is None
            else existing.transition(SyncState.PENDING, at=now)
        )
        return self._store(record)

    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> SyncRecord:
        return self._advance(
            source, logical_date, SyncState.FETCHED, checksum=checksum, l0_path=l0_path
        )

    def mark_validated(self, source: str, logical_date: date) -> SyncRecord:
        return self._advance(source, logical_date, SyncState.VALIDATED)

    def mark_normalized(self, source: str, logical_date: date) -> SyncRecord:
        return self._advance(source, logical_date, SyncState.NORMALIZED)

    def mark_published(self, source: str, logical_date: date) -> SyncRecord:
        return self._advance(source, logical_date, SyncState.PUBLISHED)

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> SyncRecord:
        return self._advance(
            source, logical_date, SyncState.FAILED, error=error, retryable=retryable
        )

    def _advance(
        self, source: str, logical_date: date, to_state: SyncState, **kwargs: Any
    ) -> SyncRecord:
        current = self.rows[(source, logical_date)]
        return self._store(current.transition(to_state, at=self._clock.now(), **kwargs))

    def _store(self, record: SyncRecord) -> SyncRecord:
        self.rows[record.key] = record
        self.history.append(record.state)
        return record


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket in this module is a bug: every response is scripted (B8)."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ingestion tests must never touch the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture(scope="session")
def register() -> SourceRegister:
    """The real, checked-in Source Register (C.1). Read for its shape, never for its hosts."""
    return load_register()


@pytest.fixture
def settings(load_settings: SettingsLoader) -> Settings:
    """Defaults only — the developer's `.env` must not decide what these tests observe."""
    return load_settings(None)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def tracker(clock: FrozenClock) -> RecordingTracker:
    return RecordingTracker(clock)


class SpyAlerter:
    """Records alerts instead of delivering them, and satisfies the C.3 `Alerter` protocol."""

    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str, str, str]] = []

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        self.sent.append((severity, title, body, dedup_key))
        return AlertOutcome.SENT


@pytest.fixture
def fixtures(repo_root: Path) -> Callable[[str], bytes]:
    def _read(rel: str) -> bytes:
        return (repo_root / "tests" / "fixtures" / rel).read_bytes()

    return _read


@pytest.fixture
def build(clock: FrozenClock, settings: Settings, register: SourceRegister, tmp_path: Any) -> Any:
    """Build a real `Fetcher` over a recorded transport and a real L0 store under `tmp_path`."""

    def _build(
        script: dict[str, ScriptedOutcome | list[ScriptedOutcome]],
    ) -> tuple[Fetcher, L0Store, RecordedTransport]:
        transport = RecordedTransport(script)
        l0 = L0Store(clock=clock, data_root=tmp_path)
        fetcher = Fetcher(
            transport=transport,
            l0=l0,
            alerter=SpyAlerter(),
            clock=clock,
            register=register,
            settings=settings,
            sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
        )
        return fetcher, l0, transport

    return _build


# ── acceptance 1a: GDELT rows land with source timestamps and L0 lineage ──────────────────────


def test_gdelt_export_parses_with_source_timestamp_tone_and_entities(fixtures: Any) -> None:
    rows = gdelt.parse_export(fixtures(GDELT_EXPORT), filename="20260902074500.export.CSV.zip")
    assert rows, "the frozen slot has events"

    # A known event from the first line of the frozen export.
    known = next(
        r for r in rows if r.url.startswith("http://www.philippinetimes.com/news/279280999")
    )
    assert known.ts == datetime(2026, 9, 2, 7, 45, 0, tzinfo=_UTC)  # DATEADDED, UTC — preserved
    assert known.source == "gdelt"
    assert known.title is None  # a GDELT event has no headline, by design
    assert known.entities == ("UNIVERSITY",)  # Actor1Name; Actor2Name was empty
    assert known.tone == Decimal("1.47874306839187")  # AvgTone, exact, not a float
    assert isinstance(known.tone, Decimal)

    # Every row is datable and linked; none carries a body.
    for row in rows:
        assert row.ts.tzinfo is not None
        assert row.url
        assert not hasattr(row, "body")


def test_gdelt_end_to_end_writes_l1_with_lineage(
    build: Any, tracker: RecordingTracker, fixtures: Any, register: SourceRegister, tmp_path: Any
) -> None:
    manifest = fixtures(GDELT_MANIFEST)
    export = fixtures(GDELT_EXPORT)
    manifest_url = next(s for s in register.sources if s.id == gdelt.SOURCE_ID).url_template
    export_url = gdelt.export_entry(
        gdelt.parse_manifest(manifest, filename="lastupdate.txt"), filename="lastupdate.txt"
    ).url

    fetcher, l0, _ = build(
        {
            manifest_url: RecordedResponse(body=manifest, headers={"content-type": "text/plain"}),
            export_url: RecordedResponse(body=export, headers={"content-type": "application/zip"}),
        }
    )
    batch = gdelt.ingest_slice(
        fetcher=fetcher, l0=l0, tracker=tracker, logical_date=LOGICAL_DATE, data_root=tmp_path
    )

    assert batch.rows
    assert batch.l0_key and batch.l0_key.endswith("20260902074500.export.CSV.zip")
    assert tracker.history == [
        SyncState.PENDING,
        SyncState.FETCHED,
        SyncState.VALIDATED,
        SyncState.NORMALIZED,
        SyncState.PUBLISHED,
    ]

    # The partition round-trips, preserving the source instant and the L0 lineage on every row.
    reread = read_l1(LOGICAL_DATE, data_root=tmp_path)
    assert len(reread.rows) == len(batch.rows)
    assert reread.l0_key == batch.l0_key
    known = next(
        r for r in reread.rows if r.url.startswith("http://www.philippinetimes.com/news/279280999")
    )
    assert known.ts == datetime(2026, 9, 2, 7, 45, 0, tzinfo=_UTC)
    assert known.tone == Decimal("1.47874306839187")


def test_gdelt_md5_mismatch_fails_loud_and_records_failed(
    build: Any, tracker: RecordingTracker, fixtures: Any, register: SourceRegister, tmp_path: Any
) -> None:
    manifest = fixtures(GDELT_MANIFEST)
    manifest_url = next(s for s in register.sources if s.id == gdelt.SOURCE_ID).url_template
    export_url = gdelt.export_entry(
        gdelt.parse_manifest(manifest, filename="lastupdate.txt"), filename="lastupdate.txt"
    ).url

    fetcher, l0, _ = build(
        {
            manifest_url: RecordedResponse(body=manifest, headers={"content-type": "text/plain"}),
            # Bytes that will not match the manifest's MD5.
            export_url: RecordedResponse(body=b"PK\x03\x04 not the real export"),
        }
    )
    with pytest.raises(ParseError, match="MD5"):
        gdelt.ingest_slice(
            fetcher=fetcher, l0=l0, tracker=tracker, logical_date=LOGICAL_DATE, data_root=tmp_path
        )
    assert tracker.rows[(gdelt.SOURCE_ID, LOGICAL_DATE)].state is SyncState.FAILED


# ── acceptance 1b + 2: RSS rows land with source timestamps; no bodies stored ─────────────────


def _rbi_feed() -> rss.Feed:
    return next(f for f in rss.load_feeds().feeds if f.id == "rbi_press_releases")


def test_rss_rbi_parses_with_source_timestamp_and_no_body(fixtures: Any) -> None:
    rows = rss.parse_feed(fixtures(RBI_FEED), _rbi_feed(), filename="pressreleases_rss.xml")
    assert rows

    first = next(
        r
        for r in rows
        if r.url == "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=63499"
    )
    # <pubDate> "Wed, 02 Sep 2026 11:30:00" is zone-less → read as Asia/Kolkata, preserved exactly.
    assert first.ts == datetime(2026, 9, 2, 11, 30, 0, tzinfo=IST)
    assert first.source == "rbi_press_releases"
    assert first.title and first.title.startswith("Conference for Registrars")
    assert first.entities == ()  # RSS states no entities
    assert first.tone is None  # RSS states no tone

    # No body crosses into a row: the RBI <description> is full HTML, yet nothing stored is markup.
    for row in rows:
        assert "<" not in (row.title or "")
        assert "<" not in row.url


def test_newsrow_has_no_body_field() -> None:
    fields = set(NewsRow.model_fields)
    assert fields == {"ts", "source", "title", "url", "entities", "tone"}
    for forbidden in ("body", "description", "content", "summary"):
        assert forbidden not in fields


def test_rss_without_timestamp_fails_loud_rather_than_fabricating(fixtures: Any) -> None:
    # PIB's RssMain feed carries title + link only — no per-item or channel date. A news row that
    # cannot be dated must not be dated from the wall clock.
    pib = rss.Feed(
        id="pib_press_releases",
        name="PIB",
        publisher="Press Information Bureau",
        url="https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
        host="pib.gov.in",
        source="curated_rss_pib",
        active=False,
        reason="no timestamps",
    )
    with pytest.raises(ParseError, match="no resolvable timestamp"):
        rss.parse_feed(fixtures(PIB_FEED), pib, filename="RssMain.xml")


def test_rss_end_to_end_writes_l1_with_lineage(
    build: Any, tracker: RecordingTracker, fixtures: Any, tmp_path: Any
) -> None:
    feed = _rbi_feed()
    fetcher, l0, _transport = build(
        {feed.url: RecordedResponse(body=fixtures(RBI_FEED), headers={"content-type": "text/xml"})}
    )
    batch = rss.ingest_feed(
        fetcher=fetcher,
        l0=l0,
        tracker=tracker,
        feed=feed,
        logical_date=LOGICAL_DATE,
        data_root=tmp_path,
    )

    assert batch.rows
    assert batch.l0_key and batch.l0_key.endswith("rbi_press_releases.xml")
    assert tracker.history[-1] is SyncState.PUBLISHED

    reread = read_l1(LOGICAL_DATE, data_root=tmp_path)
    assert {r.source for r in reread.rows} == {"rbi_press_releases"}
    assert reread.l0_key == batch.l0_key  # the partition carries the L0 lineage
    assert all(r.tone is None and r.entities == () for r in reread.rows)
    # The exact source instant survives the L0→L1 round trip.
    first = next(
        r
        for r in reread.rows
        if r.url == "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=63499"
    )
    assert first.ts == datetime(2026, 9, 2, 11, 30, 0, tzinfo=IST)


# ── acceptance 3: both source register rows are VERIFIED with evidence ────────────────────────


@pytest.mark.parametrize("source_id", ["gdelt_v2_event_files", "curated_rss"])
def test_source_register_rows_are_verified_with_evidence(
    register: SourceRegister, source_id: str
) -> None:
    source = next(s for s in register.sources if s.id == source_id)
    assert source.status.value == "VERIFIED"
    assert source.fetch_succeeded, "VERIFIED requires a real, parsed, successful fetch"
    assert source.content_type
    assert source.sample_sha256
    assert source.parse_check
    assert source.fixture.frozen is True


def test_curated_feed_set_is_consistent() -> None:
    feeds = rss.load_feeds().feeds
    ids = [f.id for f in feeds]
    assert len(ids) == len(set(ids)), "feed ids are unique"
    # The active feed's register source id exists and matches its host.
    register = load_register()
    reg_by_id = {s.id: s for s in register.sources}
    for feed in feeds:
        if feed.active:
            assert feed.source in reg_by_id, f"active feed {feed.id} needs a register row"
            assert reg_by_id[feed.source].host == feed.host
    # PIB is present but inactive (no timestamps), with a recorded reason.
    pib = next(f for f in feeds if f.id == "pib_press_releases")
    assert pib.active is False
    assert pib.reason


# ── L1 io round trip on an empty batch (a real slot can carry nothing new) ────────────────────


def test_empty_batch_writes_a_wellformed_partition(tmp_path: Any) -> None:
    batch = NewsBatch(logical_date=LOGICAL_DATE, source="gdelt", l0_key=None, rows=())
    write_l1(batch, data_root=tmp_path)
    reread = read_l1(LOGICAL_DATE, data_root=tmp_path)
    assert reread.rows == ()
