"""M3.4 — NSE FII/DII daily flows.

Two acceptance criteria, and the file is laid out as them:

1. **Daily flows parse and land in L1 with sync_state tracking.** Proved end to end against the
   frozen real payload: a fetch through the real crawl engine (with a recorded transport, never a
   socket) writes L0, parses, writes the L1 partition, and drives §4.4 from `PENDING` to
   `PUBLISHED` in order — with the failure paths driving it to `FAILED` and writing nothing.
2. **The real historical depth is measured and recorded.** Asserted against
   `source_register.yaml`: the row must carry a measurement, it must say one session, and it must
   not claim the ten years the M3 gate asks for from sources that permit it. This one is checked
   here rather than left as prose because "we thought it went back ten years" is exactly the kind
   of claim that survives until a backtest quietly reads two rows.

The money assertions are written so that inverting the logic fails them: a net flow keeps its
sign, `net` must equal `buy - sell`, gross legs may not be negative, and no value may be a float.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import socket
from collections.abc import Mapping
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
    ForbiddenError,
    ForbiddenSpikeError,
    RecordedResponse,
    RecordedTransport,
    ScriptedOutcome,
)
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.fii_dii import (
    FLOWS_DATASET,
    SOURCE_ID,
    FlowCategory,
    FlowDay,
    FlowRow,
    StaleSessionError,
    SyncTracker,
    flows_url,
    ingest_day,
    l0_filename,
    parse,
    read_l1,
    write_l1,
)
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncRecord, SyncState, SyncStateStore
from dataplatform.store.l0 import L0Store
from dataplatform.store.paths import l1_partition_path
from tests.conftest import SettingsLoader

FIXTURE: Final = Path("tests/fixtures/nse_flows/json_v1/fiidiiTradeReact_20260807.json")
SESSION: Final = date(2026, 8, 7)
NOW: Final = datetime(2026, 8, 7, 18, 30, tzinfo=IST)
WARM: Final = "https://www.nseindia.com/"
API: Final = "https://www.nseindia.com/api/fiidiiTradeReact"


#: A synthetic payload builder. Only the checked-in fixture is a real exchange response (B8);
#: everything a single session cannot show — a net seller, a broken net, a missing side — is built
#: here and is clearly not claiming to be an NSE file.
def payload(
    *,
    session: str = "07-Aug-2026",
    fii: Mapping[str, Any] | None = None,
    dii: Mapping[str, Any] | None = None,
    sides: tuple[str, ...] = ("DII", "FII"),
) -> bytes:
    """The feed's shape, with per-side field overrides; `sides` drops a side entirely."""
    records: list[dict[str, Any]] = []
    if "DII" in sides:
        records.append(
            {
                "buyValue": "15679.58",
                "category": "DII",
                "date": session,
                "netValue": "235.56",
                "sellValue": "15444.02",
                **(dii or {}),
            }
        )
    if "FII" in sides:
        records.append(
            {
                "buyValue": "12941.31",
                "category": "FII/FPI",
                "date": session,
                "netValue": "480.24",
                "sellValue": "12461.07",
                **(fii or {}),
            }
        )
    return json.dumps(records).encode("utf-8")


class RecordingTracker:
    """An in-memory §4.4 state machine, so a whole day can be driven without Postgres (B8).

    Delegates every rule to `SyncRecord.transition` — M1.3's pure half — so this is the same state
    machine the stored one applies, not a second copy that could disagree with it. `history` is the
    ordered list of states a `(source, date)` passed through, which is what an assertion about
    "PENDING then FETCHED then VALIDATED…" actually wants to look at.
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
    """`mypy --strict` fails here if M1.3's store ever stops fitting the protocol the runner uses.

    A static assertion rather than a test body: the alternative is a Postgres connection in a unit
    test, and the thing worth proving — that the offline runner drives the *same* interface the
    stored state machine exposes — is a typing question, not a runtime one.
    """
    return store


def _tracker_protocol_is_satisfied_by_the_test_double(tracker: RecordingTracker) -> SyncTracker:
    """And the double in this file fits it too, so the end-to-end tests below are not a fiction."""
    return tracker


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


@pytest.fixture(scope="session")
def fixture_bytes(repo_root: Path) -> bytes:
    """The frozen real response: NSE's own bytes for the 2026-08-07 session."""
    return (repo_root / FIXTURE).read_bytes()


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
        script: Mapping[str, ScriptedOutcome | list[ScriptedOutcome]],
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
def parsed(fixture_bytes: bytes) -> FlowDay:
    return parse(fixture_bytes, filename=FIXTURE.name, l0_key=f"{SOURCE_ID}/2026-08-07/x.json")


def ok(body: bytes) -> RecordedResponse:
    return RecordedResponse(body=body, headers={"content-type": "application/json; charset=utf-8"})


# ── acceptance 1a: the real payload parses ───────────────────────────────────────────────────


def test_the_frozen_fixture_parses_to_both_sides_exactly(parsed: FlowDay) -> None:
    """The numbers, read off the checked-in NSE response by hand."""
    assert parsed.trade_date == SESSION
    assert parsed.source == SOURCE_ID
    assert [row.category for row in parsed.rows] == [FlowCategory.DII, FlowCategory.FII]

    fii = parsed.category(FlowCategory.FII)
    assert fii.buy_value_inr_crore == Decimal("12941.31")
    assert fii.sell_value_inr_crore == Decimal("12461.07")
    assert fii.net_value_inr_crore == Decimal("480.24")

    dii = parsed.category(FlowCategory.DII)
    assert dii.buy_value_inr_crore == Decimal("15679.58")
    assert dii.sell_value_inr_crore == Decimal("15444.02")
    assert dii.net_value_inr_crore == Decimal("235.56")


def test_the_feeds_own_category_wording_survives_into_l1(parsed: FlowDay) -> None:
    """`FII/FPI` normalizes to FII, and the source string is kept so a rewording is visible."""
    assert parsed.category(FlowCategory.FII).raw_category == "FII/FPI"
    assert parsed.category(FlowCategory.DII).raw_category == "DII"


def test_every_amount_is_a_decimal_and_never_a_float(parsed: FlowDay) -> None:
    """CLAUDE.md: a float in a money field is a bug, not a rounding preference.

    `Decimal` and `float` are disjoint, so `mypy --strict` already rejects the float branch
    statically; this is the runtime half — the values a *parser* produced, not ones a test typed.
    """
    for row in parsed.rows:
        for value in (row.buy_value_inr_crore, row.sell_value_inr_crore, row.net_value_inr_crore):
            assert isinstance(value, Decimal)
            assert value == Decimal(str(value))


def test_a_bare_json_number_still_never_becomes_a_float() -> None:
    """If the feed drops the quotes tomorrow, `parse_float=Decimal` keeps money out of binary."""
    raw = json.dumps(
        [
            {
                "buyValue": 1.10,
                "category": "DII",
                "date": "07-Aug-2026",
                "netValue": 0.10,
                "sellValue": 1.00,
            },
            {
                "buyValue": 2.00,
                "category": "FII/FPI",
                "date": "07-Aug-2026",
                "netValue": -1.00,
                "sellValue": 3.00,
            },
        ]
    ).encode()
    day = parse(raw, filename="numbers.json")
    assert day.category(FlowCategory.DII).buy_value_inr_crore == Decimal("1.10")
    assert isinstance(day.category(FlowCategory.FII).net_value_inr_crore, Decimal)


def test_the_rupee_view_is_an_exact_conversion(parsed: FlowDay) -> None:
    """One crore is 10^7 exactly, so no paisa is invented or lost on the way out."""
    fii = parsed.category(FlowCategory.FII)
    assert fii.net_value_inr == Decimal("480.24") * Decimal(10) ** 7
    assert fii.net_value_inr == Decimal("4802400000.00")


def test_a_net_seller_keeps_its_sign() -> None:
    """The assertion that fails if anything ever takes an absolute value of a flow."""
    day = parse(
        payload(fii={"buyValue": "9000.00", "sellValue": "11000.00", "netValue": "-2000.00"}),
        filename="seller.json",
    )
    fii = day.category(FlowCategory.FII)
    assert fii.net_value_inr_crore == Decimal("-2000.00")
    assert fii.net_value_inr < 0


def test_dates_parse_without_depending_on_the_hosts_locale() -> None:
    """`07-Aug-2026` is not a locale-dependent string; a `%b` strptime would make it one."""
    for spelling, expected in (
        ("1-Jan-2016", date(2016, 1, 1)),
        ("31-DEC-2025", date(2025, 12, 31)),
        ("09-sep-2026", date(2026, 9, 9)),
    ):
        assert parse(payload(session=spelling), filename="d.json").trade_date == expected


# ── acceptance 1b: bad input fails loud, and never half-lands ────────────────────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"", "empty response body"),
        (b"   ", "empty response body"),
        (b"<html><body>Access Denied</body></html>", "markup, not JSON"),
        (b"{not json", "not valid JSON"),
        (b'{"category": "FII/FPI"}', "expected a JSON array"),
        (b"[]", "array is empty"),
        (b"[1, 2]", "not an object"),
    ],
)
def test_a_body_that_is_not_this_format_is_a_named_parse_error(body: bytes, expected: str) -> None:
    """Including the soft-404 case: three of nine hosts answer unknown paths with 200 + HTML."""
    with pytest.raises(ParseError, match=expected):
        parse(body, filename="fiidii.json")


def test_a_missing_side_is_not_a_partial_day() -> None:
    with pytest.raises(ParseError, match="no DII row"):
        parse(payload(sides=("FII",)), filename="half.json")


def test_a_duplicated_side_is_rejected() -> None:
    records = json.loads(payload().decode())
    records.append(records[0])
    with pytest.raises(ParseError, match="duplicate category"):
        parse(json.dumps(records).encode(), filename="dupe.json")


def test_two_sessions_in_one_payload_are_rejected() -> None:
    """A response is one session; silently keeping the first would fabricate a day."""
    with pytest.raises(ParseError, match="2 session dates"):
        parse(
            json.dumps(
                [
                    *json.loads(payload().decode()),
                    {
                        "buyValue": "1.00",
                        "category": "DII",
                        "date": "06-Aug-2026",
                        "netValue": "0.00",
                        "sellValue": "1.00",
                    },
                ]
            ).encode(),
            filename="two-days.json",
        )


def test_an_unknown_category_stops_the_day() -> None:
    """A new side is a schema change to look at, never a row to bucket into an existing one."""
    with pytest.raises(ParseError, match="unknown category"):
        parse(payload(fii={"category": "PROP"}), filename="prop.json")


def test_a_net_that_disagrees_with_its_own_gross_legs_is_rejected() -> None:
    """The feed states the arithmetic twice; a mismatch is the feed contradicting itself."""
    with pytest.raises(ParseError, match="disagrees with"):
        parse(payload(fii={"netValue": "999.99"}), filename="bad-net.json")


def test_last_place_rounding_is_tolerated_but_a_scale_error_is_not() -> None:
    """One unit in the last published place is a rounding artefact; ten of them is a defect."""
    parse(payload(fii={"netValue": "480.25"}), filename="rounded.json")
    with pytest.raises(ParseError, match="disagrees with"):
        parse(payload(fii={"netValue": "480.34"}), filename="scaled.json")


def test_a_negative_gross_leg_is_rejected() -> None:
    """Gross purchases are a turnover. A negative one means the fields moved, not that we sold."""
    with pytest.raises(ParseError, match="buy_value_inr_crore"):
        parse(
            payload(fii={"buyValue": "-12941.31", "netValue": "-25402.38"}),
            filename="negative.json",
        )


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1.2e3", "twelve", ""])
def test_a_value_that_is_not_a_plain_decimal_is_rejected(bad: str) -> None:
    """`Decimal('Infinity')` is a number that compares greater than every flow ever published."""
    with pytest.raises(ParseError):
        parse(payload(fii={"buyValue": bad}), filename="bad-value.json")


def test_thousands_separators_are_read_exactly() -> None:
    """Indian financial feeds add them without notice; they must not become a parse failure."""
    day = parse(
        payload(fii={"buyValue": "12,941.31", "sellValue": "12,461.07", "netValue": "480.24"}),
        filename="commas.json",
    )
    assert day.category(FlowCategory.FII).buy_value_inr_crore == Decimal("12941.31")


def test_a_missing_field_names_what_was_there() -> None:
    records = json.loads(payload().decode())
    del records[0]["netValue"]
    with pytest.raises(ParseError, match="no 'netValue' field"):
        parse(json.dumps(records).encode(), filename="short.json")


# ── acceptance 1c: it lands in L1 ────────────────────────────────────────────────────────────


def test_a_session_lands_in_its_l1_partition(parsed: FlowDay, tmp_path: Path) -> None:
    written = write_l1(parsed, data_root=tmp_path)
    assert written == l1_partition_path(FLOWS_DATASET, SESSION, data_root=tmp_path)
    assert written.parent.name == "date=2026-08-07"
    assert written.exists()


def test_the_partition_reads_back_identical(parsed: FlowDay, tmp_path: Path) -> None:
    """The round trip that makes L1 a store rather than a write-only log."""
    write_l1(parsed, data_root=tmp_path)
    back = read_l1(SESSION, data_root=tmp_path)
    assert back.rows == parsed.rows
    assert back.l0_key == parsed.l0_key
    assert back.category(FlowCategory.DII).net_value_inr_crore == Decimal("235.56")


def test_rewriting_a_partition_from_the_same_payload_is_byte_identical(
    parsed: FlowDay, tmp_path: Path
) -> None:
    """Idempotent per (dataset, date): re-deriving a session from L0 must not churn the lake."""
    first = write_l1(parsed, data_root=tmp_path).read_bytes()
    second = write_l1(parsed, data_root=tmp_path).read_bytes()
    assert first == second


def test_the_partition_carries_its_l0_lineage(parsed: FlowDay, tmp_path: Path) -> None:
    """Every L1 row can name the checksummed payload it was derived from (invariant #1)."""
    table = pq.read_table(write_l1(parsed, data_root=tmp_path))
    assert set(table.column("l0_key").to_pylist()) == {parsed.l0_key}
    assert set(table.column("source").to_pylist()) == {SOURCE_ID}


def test_the_l1_schema_is_declared_and_decimal(parsed: FlowDay, tmp_path: Path) -> None:
    """Money is decimal128 on disk too — a float column would put the hazard back in L1."""
    schema = pq.read_schema(write_l1(parsed, data_root=tmp_path))
    assert schema.names == [
        "trade_date",
        "category",
        "raw_category",
        "buy_value_inr_crore",
        "sell_value_inr_crore",
        "net_value_inr_crore",
        "source",
        "l0_key",
    ]
    for name in ("buy_value_inr_crore", "sell_value_inr_crore", "net_value_inr_crore"):
        assert str(schema.field(name).type) == "decimal128(20, 2)"


def test_reading_a_session_that_was_never_written_raises(tmp_path: Path) -> None:
    """An absent partition is a gap for D7 to explain, not an empty trading day."""
    with pytest.raises(FileNotFoundError):
        read_l1(date(2026, 8, 6), data_root=tmp_path)


# ── acceptance 1d: sync_state tracking, end to end ───────────────────────────────────────────


def test_a_day_goes_from_pending_to_published_in_order(
    build: Any, tracker: RecordingTracker, fixture_bytes: bytes, tmp_path: Path
) -> None:
    """The §4.4 happy path, driven through the real crawl engine over a recorded transport."""
    fetcher, l0, transport = build({WARM: ok(b"<html>"), API: ok(fixture_bytes)})

    day = ingest_day(
        fetcher=fetcher, l0=l0, tracker=tracker, trade_date=SESSION, data_root=tmp_path
    )

    assert tracker.history == [
        SyncState.PENDING,
        SyncState.FETCHED,
        SyncState.VALIDATED,
        SyncState.NORMALIZED,
        SyncState.PUBLISHED,
    ]
    row = tracker.rows[(SOURCE_ID, SESSION)]
    assert row.state is SyncState.PUBLISHED
    assert row.checksum == "1d16ad6b79af9d612f5c80e68fb06d74f95febca474a8d7c86c4285f52664ce0"
    assert row.l0_path == f"{SOURCE_ID}/2026-08-07/{l0_filename(SESSION)}"
    assert day.category(FlowCategory.FII).net_value_inr_crore == Decimal("480.24")
    assert [request.url for request in transport.requests] == [WARM, API]


def test_the_fetched_bytes_are_in_l0_before_anything_is_parsed(
    build: Any, tracker: RecordingTracker, fixture_bytes: bytes, tmp_path: Path
) -> None:
    """Invariant #1 at the point it is created: L1 is derived from a checksummed payload."""
    fetcher, l0, _ = build({WARM: ok(b"<html>"), API: ok(fixture_bytes)})
    ingest_day(fetcher=fetcher, l0=l0, tracker=tracker, trade_date=SESSION, data_root=tmp_path)

    stored = [path for path in (tmp_path / "L0").rglob("*") if path.is_file()]
    assert sorted(path.name for path in stored) == [
        l0_filename(SESSION),
        f"{l0_filename(SESSION)}.meta.json",
    ]
    assert (tmp_path / "L0" / SOURCE_ID / "2026" / "08" / l0_filename(SESSION)).read_bytes() == (
        fixture_bytes
    )


def test_the_l0_filename_carries_the_date_the_url_does_not(build: Any) -> None:
    """The URL has no date, so two sessions in one month would otherwise be one L0 key."""
    assert l0_filename(date(2026, 8, 7)) != l0_filename(date(2026, 8, 6))
    assert l0_filename(date(2026, 8, 7)) == "fiidiiTradeReact_20260807.json"


def test_a_stale_session_fails_the_day_retryably_and_writes_no_l1(
    build: Any, tracker: RecordingTracker, tmp_path: Path
) -> None:
    """The rolling-feed hazard: NSE publishes late, so an early run gets yesterday's numbers.

    Filing them under today would fabricate a row that no later fetch could ever correct — this
    endpoint has no history to re-derive from. The day fails and is retried instead.
    """
    fetcher, l0, _ = build({WARM: ok(b"<html>"), API: ok(payload(session="06-Aug-2026"))})

    with pytest.raises(StaleSessionError):
        ingest_day(fetcher=fetcher, l0=l0, tracker=tracker, trade_date=SESSION, data_root=tmp_path)

    row = tracker.rows[(SOURCE_ID, SESSION)]
    assert row.state is SyncState.FAILED
    assert row.retryable is True
    assert row.last_error is not None
    assert "2026-08-06" in row.last_error
    assert not l1_partition_path(FLOWS_DATASET, SESSION, data_root=tmp_path).exists()
    assert tracker.history[-1] is SyncState.FAILED
    assert SyncState.VALIDATED not in tracker.history


def test_a_soft_404_fails_the_day_without_a_retry(
    build: Any, tracker: RecordingTracker, tmp_path: Path
) -> None:
    """A format failure is a dead end: re-driving it is how a backfill becomes a hot loop."""
    fetcher, l0, _ = build({WARM: ok(b"<html>"), API: ok(b"<html>Access Denied</html>")})

    with pytest.raises(ParseError):
        ingest_day(fetcher=fetcher, l0=l0, tracker=tracker, trade_date=SESSION, data_root=tmp_path)

    row = tracker.rows[(SOURCE_ID, SESSION)]
    assert row.state is SyncState.FAILED
    assert row.retryable is False
    assert not l1_partition_path(FLOWS_DATASET, SESSION, data_root=tmp_path).exists()


def test_a_403_on_the_session_warm_up_does_not_stop_the_fetch(
    build: Any, tracker: RecordingTracker, fixture_bytes: bytes, tmp_path: Path
) -> None:
    """NSE answers its own homepage with 403 and sets the usable cookie anyway.

    Observed live on 2026-08-08 and recorded in `ops/gates/source-verification.md` §5.7: the
    warm-up 403 is followed by a 200 on the API. Treating the handshake's refusal as a refusal of
    us would stop every cookie source in the register from ever fetching — while the hard stop
    stays armed on the request that matters, which the next test proves.
    """
    fetcher, l0, transport = build(
        {WARM: RecordedResponse(status_code=403, body=b"<html>"), API: ok(fixture_bytes)}
    )

    ingest_day(fetcher=fetcher, l0=l0, tracker=tracker, trade_date=SESSION, data_root=tmp_path)

    assert [request.url for request in transport.requests] == [WARM, API]
    assert tracker.rows[(SOURCE_ID, SESSION)].state is SyncState.PUBLISHED
    assert not fetcher.is_stopped("www.nseindia.com")


def test_a_403_on_the_data_request_still_hard_stops(
    build: Any, tracker: RecordingTracker, settings: Settings, tmp_path: Path
) -> None:
    """The handshake exemption is exactly one status on exactly one request, and no wider."""
    fetcher, l0, _ = build(
        {WARM: RecordedResponse(status_code=403), API: RecordedResponse(status_code=403)}
    )

    for offset in range(settings.http_forbidden_streak_limit):
        with pytest.raises((ForbiddenError, ForbiddenSpikeError)):
            ingest_day(
                fetcher=fetcher,
                l0=l0,
                tracker=tracker,
                trade_date=SESSION - timedelta(days=offset),
                data_root=tmp_path,
            )

    assert fetcher.is_stopped("www.nseindia.com")
    assert tracker.rows[(SOURCE_ID, SESSION)].retryable is False


def test_the_sync_tracker_protocol_matches_the_real_store() -> None:
    """The runner drives M1.3's interface, not a shape of its own.

    `mypy --strict` proves assignability through the two module-level functions above; this checks
    the signatures line up too, so a keyword rename in `SyncStateStore` is a failure here rather
    than a `TypeError` on the first real EOD run.
    """
    for name, member in inspect.getmembers(SyncTracker, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        stored = getattr(SyncStateStore, name)
        assert inspect.signature(stored) == inspect.signature(member), name


# ── acceptance 2: the measured historical depth is recorded ──────────────────────────────────


def test_the_register_records_a_measured_depth_for_this_source(register: SourceRegister) -> None:
    """M3 gate box 2 asks for ten years "where the source permits". This one does not permit."""
    source = next(entry for entry in register.sources if entry.id == SOURCE_ID)
    depth = source.history
    assert depth is not None, "M3.4 must record the depth it measured, not leave it unstated"
    assert depth.measured_by == "M3.4"
    assert depth.sessions_available == 1
    assert depth.earliest_available == depth.latest_available == SESSION
    assert depth.accrues_forward is True
    assert depth.method.strip(), "a depth with no method is a claim, not a measurement"


def test_the_register_does_not_claim_ten_years_it_does_not_have(
    register: SourceRegister,
) -> None:
    """The failure this criterion exists to prevent: a depth nobody checked, believed downstream."""
    source = next(entry for entry in register.sources if entry.id == SOURCE_ID)
    assert source.history is not None
    assert source.history.earliest_available is not None
    span_days = (SESSION - source.history.earliest_available).days
    assert span_days < 3650
    assert "NOT achievable" in source.history.note


def test_the_fallback_is_named_and_marked_as_a_fallback(register: SourceRegister) -> None:
    """NSDL/CDSL are FPI-only and custodian-confirmed: a fallback for one leg, not a substitute."""
    source = next(entry for entry in register.sources if entry.id == SOURCE_ID)
    assert source.fallback
    assert source.history is not None
    assert "NSDL" in source.history.note
    assert "substitute" in source.history.note


def test_the_frozen_fixture_is_the_payload_the_register_verified(
    register: SourceRegister, repo_root: Path, fixture_bytes: bytes
) -> None:
    """B8's fixture, and the evidence that it is the real response and not a hand-written one."""
    source = next(entry for entry in register.sources if entry.id == SOURCE_ID)
    assert source.fixture.frozen is True
    assert source.fixture.path is not None
    assert (repo_root / source.fixture.path).is_file()
    assert hashlib.sha256(fixture_bytes).hexdigest() == source.sample_sha256
    assert len(fixture_bytes) == source.sample_bytes


def test_the_url_comes_from_the_register(register: SourceRegister) -> None:
    """One place holds the endpoint. A second copy in code is a second thing to keep true."""
    source = next(entry for entry in register.sources if entry.id == SOURCE_ID)
    assert flows_url(register) == source.url_template
    assert "{" not in source.url_template, "a date parameter would change the measured depth"


# ── the model refuses to be half a day ───────────────────────────────────────────────────────


def _row(category: FlowCategory, **kwargs: Any) -> FlowRow:
    values: dict[str, Any] = {
        "trade_date": SESSION,
        "category": category,
        "raw_category": category.value,
        "buy_value_inr_crore": Decimal("100.00"),
        "sell_value_inr_crore": Decimal("60.00"),
        "net_value_inr_crore": Decimal("40.00"),
    }
    return FlowRow(**{**values, **kwargs})


def test_a_flow_day_cannot_be_constructed_with_one_side() -> None:
    with pytest.raises(ValueError, match="no FII row"):
        FlowDay(trade_date=SESSION, source=SOURCE_ID, rows=(_row(FlowCategory.DII),))


def test_a_flow_day_cannot_mix_dates() -> None:
    with pytest.raises(ValueError, match="row dated"):
        FlowDay(
            trade_date=SESSION,
            source=SOURCE_ID,
            rows=(
                _row(FlowCategory.DII),
                _row(FlowCategory.FII, trade_date=date(2026, 8, 6)),
            ),
        )


def test_rows_are_frozen(parsed: FlowDay) -> None:
    """A row a parser vouched for cannot be edited afterwards."""
    with pytest.raises(ValueError, match="frozen"):
        parsed.rows[0].buy_value_inr_crore = Decimal("1")
