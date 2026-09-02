"""M3.6 — NSE quarterly shareholding pattern, dual-dated.

The file is laid out as the three acceptance criteria, because each is a property that a plausible
wrong implementation would quietly violate:

1. **Every row carries `(period_end, filing_date)` and neither is inferred from the other.** Proved
   by parsing a filing whose two dates are weeks apart and by rejecting a record whose filing does
   not fall after its quarter — a single-date schema could not tell those apart.
2. **Promoter pledge % is a queryable, first-class field.** Proved by reading it back as an exact
   `Decimal` and by the BC3 predicate (§5.3, pledge >50%) selecting exactly the breaching company —
   a float pledge or a pledge buried in a notes blob would fail one of these.
3. **A PIT query dated before a filing cannot see it.** Proved end to end against L1: a row filed on
   12-May is invisible to `read_pit(11-May)` and visible to `read_pit(12-May)` — invariant #7 made
   structural by partitioning on the filing (knowable) date, not the quarter end.

The money assertions are written so inverting the logic fails them: a percentage stays a `Decimal`,
never a `float`; a value outside 0-100 is a parse error; and the BC3 threshold is a strict `>`, so
exactly 50.00 is not yet a break.
"""

from __future__ import annotations

import json
import socket
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pyarrow.parquet as pq
import pytest

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
    ScriptedOutcome,
)
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.shareholding import (
    PLEDGE_BREACH_PCT,
    SHAREHOLDING_DATASET,
    SOURCE_ID,
    ShareholdingRow,
    ShareholdingSnapshot,
    SyncTracker,
    ingest_snapshot,
    l0_filename,
    parse,
    read_l1,
    read_pit,
    snapshot_url,
    write_l1,
)
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncRecord, SyncState, SyncStateStore
from dataplatform.store.l0 import L0Store
from dataplatform.store.paths import l1_partition_path
from tests.conftest import SettingsLoader

FIXTURE: Final = Path(
    "tests/fixtures/nse_shareholding/json_v1/corporate-share-holdings-master_20260807.json"
)
POLL: Final = date(2026, 8, 7)
NOW: Final = datetime(2026, 8, 7, 19, 0, tzinfo=IST)
WARM: Final = "https://www.nseindia.com/"
API: Final = "https://www.nseindia.com/api/corporate-share-holdings-master?index=equities"

#: ISINs in the fixture, for readable assertions.
RELIANCE: Final = "INE002A01018"
TCS: Final = "INE467B01029"
PLEDGED: Final = "INE134E01011"  # promoter pledge 62.50% — the BC3 breach
HDFC: Final = "INE040A01034"
INFOSYS: Final = "INE009A01021"


# ── a §4.4 test double, so a whole poll runs without Postgres (B8) ──────────────────────────────


class RecordingTracker:
    """An in-memory §4.4 state machine that delegates every rule to `SyncRecord.transition`.

    The same pure state machine M1.3's store applies, so the end-to-end tests are not a fiction.
    `history` is the ordered list of states a `(source, date)` passed through.
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


def _tracker_protocol_is_satisfied_by_the_real_store(store: SyncStateStore) -> SyncTracker:
    """`mypy --strict` fails here if M1.3's store ever stops fitting the runner's protocol."""
    return store


def _tracker_protocol_is_satisfied_by_the_double(tracker: RecordingTracker) -> SyncTracker:
    """And the double in this file fits it too, so the end-to-end tests are not a fiction."""
    return tracker


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket in this module is a bug: every response is scripted (B8)."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; shareholding tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture(scope="session")
def fixture_bytes(repo_root: Path) -> bytes:
    """The representative master payload for the 2026-08-07 poll."""
    return (repo_root / FIXTURE).read_bytes()


@pytest.fixture
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
def build(clock: FrozenClock, settings: Settings, register: SourceRegister, tmp_path: Path) -> Any:
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


@pytest.fixture
def parsed(fixture_bytes: bytes) -> ShareholdingSnapshot:
    return parse(fixture_bytes, filename=FIXTURE.name, l0_key=f"{SOURCE_ID}/2026-08-07/x.json")


def ok(body: bytes) -> RecordedResponse:
    return RecordedResponse(body=body, headers={"content-type": "application/json; charset=utf-8"})


def _row(parsed: ShareholdingSnapshot, isin: str) -> ShareholdingRow:
    return next(row for row in parsed.rows if row.isin == isin)


def _record(**overrides: Any) -> bytes:
    """One well-formed record, mutated per test to exercise one failure at a time."""
    base: dict[str, Any] = {
        "isin": RELIANCE,
        "name": "Reliance Industries Limited",
        "date": "31-Mar-2026",
        "broadcastDate": "07-May-2026 18:30:00",
        "cgTimeStamp": "07-May-2026 18:30:00",
        "pr_and_prgrp": "50.30",
        "pledgeShares_prcnt": "0.00",
        "public_prcnt": "49.70",
        "fii_prcnt": "22.10",
        "dii_prcnt": "15.40",
        "industry": "Refineries",
    }
    base.update(overrides)
    for key, value in list(base.items()):
        if value is None:
            del base[key]
    return json.dumps([base]).encode("utf-8")


# ── acceptance 1: both dates, neither inferred from the other ────────────────────────────────


def test_the_fixture_parses_every_company(parsed: ShareholdingSnapshot) -> None:
    assert {row.isin for row in parsed.rows} == {RELIANCE, TCS, PLEDGED, HDFC, INFOSYS}


def test_every_row_carries_both_dates(parsed: ShareholdingSnapshot) -> None:
    """Both a quarter end and a filing date, on every row."""
    for row in parsed.rows:
        assert isinstance(row.period_end, date)
        assert isinstance(row.filing_date, date)


def test_the_two_dates_are_independent_not_one_derived_from_the_other(
    parsed: ShareholdingSnapshot,
) -> None:
    """The whole point of §4.1's rule: a quarter that ended 31-Mar is filed weeks later.

    Reliance and TCS share a quarter end (31-Mar-2026) but were filed on different dates
    (07-May and 18-Apr). No arithmetic on `period_end` could yield both filing dates, so the two
    fields cannot be the same value in two costumes.
    """
    reliance = _row(parsed, RELIANCE)
    tcs = _row(parsed, TCS)
    assert reliance.period_end == tcs.period_end == date(2026, 3, 31)
    assert reliance.filing_date == date(2026, 5, 7)
    assert tcs.filing_date == date(2026, 4, 18)
    assert reliance.filing_date != reliance.period_end


def test_a_filing_that_does_not_fall_after_its_quarter_is_rejected() -> None:
    """A filing dated on or before its quarter end is a transposition, not a filing (§4.1)."""
    with pytest.raises(ParseError, match="not after the quarter end"):
        parse(
            _record(date="31-Mar-2026", broadcastDate="31-Mar-2026 10:00:00"),
            filename="x.json",
        )
    with pytest.raises(ParseError, match="not after the quarter end"):
        parse(
            _record(date="31-Mar-2026", broadcastDate="15-Mar-2026 10:00:00"),
            filename="x.json",
        )


def test_the_filing_date_falls_back_to_cg_timestamp_when_broadcast_is_absent() -> None:
    """`pit_notes` names both fields as the first-knowable timestamp; either populates it."""
    snapshot = parse(
        _record(broadcastDate=None, cgTimeStamp="09-May-2026 12:00:00"), filename="x.json"
    )
    assert snapshot.rows[0].filing_date == date(2026, 5, 9)


def test_a_record_with_no_filing_timestamp_is_a_named_error() -> None:
    with pytest.raises(ParseError, match="no filing timestamp"):
        parse(_record(broadcastDate=None, cgTimeStamp=None), filename="x.json")


# ── acceptance 2: promoter pledge % is a first-class, queryable field ─────────────────────────


def test_promoter_pledge_is_extracted_as_a_decimal(parsed: ShareholdingSnapshot) -> None:
    pledged = _row(parsed, PLEDGED)
    assert pledged.promoter_pledge_pct == Decimal("62.50")
    assert isinstance(pledged.promoter_pledge_pct, Decimal)


def test_every_percentage_is_a_decimal_and_never_a_float(parsed: ShareholdingSnapshot) -> None:
    for row in parsed.rows:
        for value in (
            row.promoter_holding_pct,
            row.promoter_pledge_pct,
            row.public_pct,
            row.fii_pct,
            row.dii_pct,
        ):
            assert value is None or isinstance(value, Decimal)
            assert not isinstance(value, float)


def test_bc3_flags_exactly_the_company_over_the_pledge_threshold(
    parsed: ShareholdingSnapshot,
) -> None:
    """§5.3 BC3: promoter pledge >50% is an integrity break. Only the pledged company qualifies."""
    assert {row.isin for row in parsed.breaching()} == {PLEDGED}
    assert _row(parsed, PLEDGED).breaches_bc3 is True
    assert _row(parsed, RELIANCE).breaches_bc3 is False


def test_the_bc3_threshold_is_a_strict_boundary() -> None:
    """ "pledge >50%" — exactly 50.00 is not yet a break, and one paisa over it is."""
    assert Decimal("50") == PLEDGE_BREACH_PCT
    at = parse(_record(pledgeShares_prcnt="50.00"), filename="x.json").rows[0]
    over = parse(_record(pledgeShares_prcnt="50.01"), filename="x.json").rows[0]
    assert at.breaches_bc3 is False
    assert over.breaches_bc3 is True


def test_a_pledge_outside_0_to_100_is_rejected() -> None:
    with pytest.raises(ParseError):
        parse(_record(pledgeShares_prcnt="120.00"), filename="x.json")
    with pytest.raises(ParseError):
        parse(_record(pledgeShares_prcnt="-1.00"), filename="x.json")


def test_a_pledge_that_is_not_a_plain_decimal_is_rejected() -> None:
    for bad in ("NaN", "Infinity", "n/a", ""):
        with pytest.raises(ParseError):
            parse(_record(pledgeShares_prcnt=bad), filename="x.json")


def test_an_unreported_split_is_none_not_zero() -> None:
    """A split the filing did not state is unknown; a zero would assert no FII holds any of it."""
    snapshot = parse(_record(fii_prcnt=None, dii_prcnt=""), filename="x.json")
    assert snapshot.rows[0].fii_pct is None
    assert snapshot.rows[0].dii_pct is None


# ── acceptance 3: a PIT query before filing_date cannot see the row ───────────────────────────


def test_a_pit_query_before_a_filing_cannot_see_it(
    parsed: ShareholdingSnapshot, tmp_path: Path
) -> None:
    """The core PIT guarantee (invariant #7). The pledged company filed on 12-May-2026.

    A decision dated 11-May must not see it — the break condition it carries did not exist yet.
    """
    write_l1(parsed, data_root=tmp_path)

    before = read_pit(date(2026, 5, 11), data_root=tmp_path)
    on = read_pit(date(2026, 5, 12), data_root=tmp_path)

    assert PLEDGED not in {row.isin for row in before}
    assert PLEDGED in {row.isin for row in on}


def test_pit_visibility_grows_monotonically_with_the_as_of_date(
    parsed: ShareholdingSnapshot, tmp_path: Path
) -> None:
    """As the as-of date advances past each filing date, exactly those rows become knowable."""
    write_l1(parsed, data_root=tmp_path)

    # Nothing filed before 18-Apr (the earliest filing in the fixture).
    assert read_pit(date(2026, 4, 17), data_root=tmp_path) == ()
    # 18-Apr: TCS. 21-Apr: + Infosys. 07-May: + Reliance. 12-May: + HDFC and the pledged co.
    assert {r.isin for r in read_pit(date(2026, 4, 18), data_root=tmp_path)} == {TCS}
    assert {r.isin for r in read_pit(date(2026, 4, 21), data_root=tmp_path)} == {TCS, INFOSYS}
    assert {r.isin for r in read_pit(date(2026, 5, 7), data_root=tmp_path)} == {
        TCS,
        INFOSYS,
        RELIANCE,
    }
    assert {r.isin for r in read_pit(date(2026, 5, 12), data_root=tmp_path)} == {
        RELIANCE,
        TCS,
        PLEDGED,
        HDFC,
        INFOSYS,
    }


def test_the_bc3_break_is_invisible_until_its_filing_is_knowable(
    parsed: ShareholdingSnapshot, tmp_path: Path
) -> None:
    """A monitor scanning as of 11-May sees no breach; as of 12-May it sees exactly one."""
    write_l1(parsed, data_root=tmp_path)

    before = [row for row in read_pit(date(2026, 5, 11), data_root=tmp_path) if row.breaches_bc3]
    on = [row for row in read_pit(date(2026, 5, 12), data_root=tmp_path) if row.breaches_bc3]

    assert before == []
    assert [row.isin for row in on] == [PLEDGED]


# ── L1 round trip and partition discipline ───────────────────────────────────────────────────


def test_l1_partitions_by_filing_date_not_quarter_end(
    parsed: ShareholdingSnapshot, tmp_path: Path
) -> None:
    """The partition key is the knowable date — what makes `read_pit` a partition prune."""
    written = write_l1(parsed, data_root=tmp_path)
    # Four distinct filing dates in the fixture → four partitions.
    assert {path.parent.name for path in written} == {
        "date=2026-04-18",
        "date=2026-04-21",
        "date=2026-05-07",
        "date=2026-05-12",
    }
    # Quarter end 31-Mar is NOT a partition — it is data inside the rows.
    assert not (tmp_path / "L1" / SHAREHOLDING_DATASET / "date=2026-03-31").exists()


def test_a_partition_reads_back_identically(parsed: ShareholdingSnapshot, tmp_path: Path) -> None:
    write_l1(parsed, data_root=tmp_path)
    back = read_l1(date(2026, 5, 12), data_root=tmp_path)
    assert {row.isin for row in back} == {HDFC, PLEDGED}
    assert _row_by(back, PLEDGED).promoter_pledge_pct == Decimal("62.50")
    assert _row_by(back, PLEDGED).period_end == date(2026, 3, 31)
    assert _row_by(back, PLEDGED).filing_date == date(2026, 5, 12)


def test_rewriting_a_partition_from_the_same_payload_is_byte_identical(
    parsed: ShareholdingSnapshot, tmp_path: Path
) -> None:
    """Idempotent per (dataset, filing_date): the M1.5 determinism rule (§4.2)."""
    first = {p: p.read_bytes() for p in write_l1(parsed, data_root=tmp_path)}
    second = {p: p.read_bytes() for p in write_l1(parsed, data_root=tmp_path)}
    assert first == second


def test_the_partition_carries_its_l0_lineage(parsed: ShareholdingSnapshot, tmp_path: Path) -> None:
    write_l1(parsed, data_root=tmp_path)
    table = pq.read_table(
        l1_partition_path(SHAREHOLDING_DATASET, date(2026, 5, 12), data_root=tmp_path)
    )
    assert set(table.column("l0_key").to_pylist()) == {f"{SOURCE_ID}/2026-08-07/x.json"}


def test_the_l1_schema_is_declared_and_decimal(
    parsed: ShareholdingSnapshot, tmp_path: Path
) -> None:
    write_l1(parsed, data_root=tmp_path)
    schema = pq.read_schema(
        l1_partition_path(SHAREHOLDING_DATASET, date(2026, 5, 7), data_root=tmp_path)
    )
    for name in ("promoter_holding_pct", "promoter_pledge_pct", "public_pct"):
        assert str(schema.field(name).type) == "decimal128(6, 2)"


def test_reading_a_partition_that_was_never_written_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_l1(date(2026, 5, 12), data_root=tmp_path)


def test_read_pit_on_an_empty_lake_is_empty_not_an_error(tmp_path: Path) -> None:
    assert read_pit(date(2026, 5, 12), data_root=tmp_path) == ()


def _row_by(rows: tuple[ShareholdingRow, ...], isin: str) -> ShareholdingRow:
    return next(row for row in rows if row.isin == isin)


# ── structural failures parse loudly ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"", "empty response body"),
        (b"   ", "empty response body"),
        (b"<html>Access Denied</html>", "markup, not JSON"),
        (b"{not json", "not valid JSON"),
        (b"{}", "expected a JSON array"),
        (b"[]", "JSON array is empty"),
        (b"[1, 2]", "not an object"),
    ],
)
def test_a_body_that_is_not_this_format_is_a_named_parse_error(body: bytes, expected: str) -> None:
    with pytest.raises(ParseError, match=expected):
        parse(body, filename="master.json")


def test_a_missing_required_field_names_what_was_there() -> None:
    with pytest.raises(ParseError, match="no 'pr_and_prgrp' field"):
        parse(_record(pr_and_prgrp=None), filename="x.json")


def test_a_company_listed_twice_for_one_quarter_is_rejected() -> None:
    body = json.dumps(
        [
            json.loads(_record().decode())[0],
            json.loads(_record().decode())[0],
        ]
    ).encode("utf-8")
    with pytest.raises(ParseError, match="more than once"):
        parse(body, filename="x.json")


def test_the_data_envelope_shape_is_accepted(fixture_bytes: bytes) -> None:
    """An NSE {"data": [...]} envelope is accepted; a cosmetic wrapper must not stop ingestion."""
    wrapped = json.dumps({"data": json.loads(fixture_bytes.decode())}).encode("utf-8")
    assert len(parse(wrapped, filename="x.json").rows) == 5


# ── end to end: a poll goes to PUBLISHED, checksummed in L0 first ─────────────────────────────


def test_a_poll_goes_from_pending_to_published_in_order(
    build: Any, tracker: RecordingTracker, fixture_bytes: bytes, tmp_path: Path
) -> None:
    """The §4.4 happy path, driven through the real crawl engine over a recorded transport."""
    fetcher, l0, transport = build({WARM: ok(b"<html>"), API: ok(fixture_bytes)})

    snapshot = ingest_snapshot(
        fetcher=fetcher, l0=l0, tracker=tracker, poll_date=POLL, data_root=tmp_path
    )

    assert tracker.history == [
        SyncState.PENDING,
        SyncState.FETCHED,
        SyncState.VALIDATED,
        SyncState.NORMALIZED,
        SyncState.PUBLISHED,
    ]
    assert tracker.rows[(SOURCE_ID, POLL)].state is SyncState.PUBLISHED
    assert len(snapshot.rows) == 5
    assert [request.url for request in transport.requests] == [WARM, API]


def test_the_fetched_bytes_are_in_l0_before_anything_is_parsed(
    build: Any, tracker: RecordingTracker, fixture_bytes: bytes, tmp_path: Path
) -> None:
    """Invariant #1 at the point it is created: L1 is derived from a checksummed payload."""
    fetcher, l0, _ = build({WARM: ok(b"<html>"), API: ok(fixture_bytes)})
    ingest_snapshot(fetcher=fetcher, l0=l0, tracker=tracker, poll_date=POLL, data_root=tmp_path)

    stored = tmp_path / "L0" / SOURCE_ID / "2026" / "08" / l0_filename(POLL)
    assert stored.read_bytes() == fixture_bytes


def test_the_l0_filename_carries_the_poll_date_the_url_does_not() -> None:
    """The URL has no date, so two polls in one month would otherwise be one L0 key."""
    assert l0_filename(date(2026, 8, 7)) != l0_filename(date(2026, 8, 6))
    assert l0_filename(date(2026, 8, 7)) == "corporate-share-holdings-master_20260807.json"


def test_a_soft_404_fails_the_poll_without_a_retry(
    build: Any, tracker: RecordingTracker, tmp_path: Path
) -> None:
    """A format failure is a dead end: re-driving it is how a backfill becomes a hot loop."""
    fetcher, l0, _ = build({WARM: ok(b"<html>"), API: ok(b"<html>Access Denied</html>")})

    with pytest.raises(ParseError):
        ingest_snapshot(fetcher=fetcher, l0=l0, tracker=tracker, poll_date=POLL, data_root=tmp_path)

    row = tracker.rows[(SOURCE_ID, POLL)]
    assert row.state is SyncState.FAILED
    assert row.retryable is False
    assert SyncState.VALIDATED not in tracker.history


def test_the_snapshot_url_comes_from_the_register(register: SourceRegister) -> None:
    """C.1: the endpoint is read from the register, not repeated in code."""
    assert snapshot_url(register) == API
