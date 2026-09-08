"""The daily snapshotter (OPS) — the job whose failure mode is silence.

This job runs unattended forever against sources that have no past, so the tests are laid out as
the four ways it could quietly stop being worth anything:

1. **It captures into the wrong lake.** The L0 root is asserted before the first request, and a
   mismatch fetches nothing at all — which has already happened twice for real.
2. **It runs twice and either breaks or pays twice.** A second run of the same day makes zero
   requests, raises nothing, and re-reports what the lake already holds.
3. **It files a stale payload as today's.** NSE answers 200 with a previous session's file; every
   surveillance payload stamps its own date, and a mismatch is a distinct outcome with a FAILED
   sync row and a CRITICAL alert — never a published date.
4. **One broken source takes the others down.** A 404, a soft 404 and a truncated file each park
   their own source, alert, and leave the sweep running.

Offline (B8): every response is a checked-in fixture or a scripted status, and a socket is a test
bug. The frozen payloads are the ones the 2026-09-08 probe served
(`ops/gates/daily-snapshotter-2026-09-08.md`).
"""

from __future__ import annotations

import json
import socket
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, Protocol

import pytest

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.calendar import DayKind, trading_calendar
from dataplatform.ingest.daily_snapshot import (
    DEFAULT_SNAPSHOT_SET,
    DailySnapshotError,
    LakeRootMismatchError,
    SnapshotContentError,
    SnapshotSpec,
    SnapshotStatus,
    SnapshotTracker,
    run_daily_snapshot,
)
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
    ScriptedOutcome,
)
from dataplatform.ingest.source_register import Status
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncRecord, SyncState, SyncStateStore
from dataplatform.store.l0 import L0Store
from tests.conftest import SettingsLoader

FIXTURES: Final = Path("tests/fixtures/nse_market_structure/2026-09-08")

#: The capture date the frozen payloads stamp themselves with. Every one of them says 08-Sep-2026,
#: so replaying this era under any other date is exactly the stale-200 case.
AS_OF: Final = date(2026, 9, 8)
NOW: Final = datetime(2026, 9, 8, 19, 15, tzinfo=IST)

#: The five market-structure specs — the sources this task registered. `nse_equity_list`,
#: `nse_symbol_changes` and `bse_scrip_master` are exercised by their own suites and are dropped
#: here so this module needs no second set of frozen payloads to say anything about the sweep.
SPECS: Final[tuple[SnapshotSpec, ...]] = tuple(
    spec
    for spec in DEFAULT_SNAPSHOT_SET
    if spec.source_id
    in {
        "nse_industry_classification",
        "nse_price_bands",
        "nse_asm_list",
        "nse_gsm_list",
        "nse_esm_list",
    }
)

#: Which frozen file each spec's URL serves.
PAYLOADS: Final[dict[str, str]] = {
    "nse_industry_classification": "ind_niftytotalmarket_list.csv",
    "nse_price_bands": "sec_list.csv",
    "nse_asm_list": "reportASM.json",
    "nse_gsm_list": "reportGSM.json",
    "nse_esm_list": "reportESM.json",
}


def _url(source_id: str) -> str:
    entry = next(row for row in load_register().sources if row.id == source_id)
    return entry.url_template


def _body(source_id: str) -> bytes:
    return (FIXTURES / PAYLOADS[source_id]).read_bytes()


def _script(
    overrides: dict[str, ScriptedOutcome] | None = None,
) -> dict[str, ScriptedOutcome | list[ScriptedOutcome]]:
    """The happy-path transport script for every spec, with per-source overrides.

    The NSE warm-up URL is scripted too: three of the five rows carry `needs_session_cookie`, and
    the fetcher visits the site once per process before touching a JSON endpoint.
    """
    script: dict[str, ScriptedOutcome | list[ScriptedOutcome]] = {
        "https://www.nseindia.com/": RecordedResponse(status_code=403, body=b"")
    }
    for spec in SPECS:
        script[_url(spec.source_id)] = RecordedResponse(body=_body(spec.source_id))
    for source_id, outcome in (overrides or {}).items():
        script[_url(source_id)] = outcome
    return script


# ── a §4.4 test double, so a whole sweep runs without Postgres (B8) ─────────────────────────────


class RecordingTracker:
    """An in-memory §4.4 state machine delegating every rule to `SyncRecord.transition`.

    Delegating rather than reimplementing matters here: the sweep's idempotence depends on
    `PUBLISHED` having no outgoing edge, so a double that allowed `PUBLISHED → PENDING` would make
    the second-run test pass against code that raises in production.
    """

    def __init__(self, clock: FrozenClock) -> None:
        self._clock = clock
        self.rows: dict[tuple[str, date], SyncRecord] = {}
        self.history: list[tuple[str, SyncState]] = []

    def get(self, source: str, logical_date: date) -> SyncRecord | None:
        return self.rows.get((source, logical_date))

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

    def mark_gap(self, source: str, logical_date: date) -> SyncRecord:
        existing = self.rows.get((source, logical_date))
        now = self._clock.now()
        record = (
            SyncRecord(
                source=source, logical_date=logical_date, state=SyncState.GAP, updated_at=now
            )
            if existing is None
            else existing.transition(SyncState.GAP, at=now)
        )
        return self._store(record)

    def _advance(
        self, source: str, logical_date: date, to_state: SyncState, **kwargs: Any
    ) -> SyncRecord:
        current = self.rows[(source, logical_date)]
        return self._store(current.transition(to_state, at=self._clock.now(), **kwargs))

    def _store(self, record: SyncRecord) -> SyncRecord:
        self.rows[record.key] = record
        self.history.append((record.source, record.state))
        return record


def _the_real_store_satisfies_the_protocol(store: SyncStateStore) -> SnapshotTracker:
    """`mypy --strict` fails here if `SyncStateStore` ever stops fitting the sweep's protocol."""
    tracker: SnapshotTracker = store
    return tracker


class SpyAlerter:
    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str, str, str]] = []

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        self.sent.append((severity, title, body, dedup_key))
        return AlertOutcome.SENT


class Build(Protocol):
    def __call__(
        self, script: dict[str, ScriptedOutcome | list[ScriptedOutcome]]
    ) -> tuple[Fetcher, L0Store, RecordedTransport]: ...


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket in this module is a bug: every response is scripted or a checked-in file (B8)."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; the snapshotter tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def settings(load_settings: SettingsLoader) -> Settings:
    return load_settings(None)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def tracker(clock: FrozenClock) -> RecordingTracker:
    return RecordingTracker(clock)


@pytest.fixture
def alerter() -> SpyAlerter:
    return SpyAlerter()


@pytest.fixture
def build(clock: FrozenClock, settings: Settings, tmp_path: Path) -> Build:
    """A real `Fetcher` over a recorded transport and a real `L0Store` under `tmp_path`.

    The transport comes back too, so a test can assert exactly which URLs were and were not
    requested — the evidence for the idempotence claim, which is otherwise unfalsifiable.
    """
    register = load_register()

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
            sleep=lambda _seconds: None,
        )
        return fetcher, l0, transport

    return _build


def _sweep(
    build: Build,
    tracker: RecordingTracker,
    alerter: SpyAlerter,
    *,
    script: dict[str, ScriptedOutcome | list[ScriptedOutcome]] | None = None,
    as_of: date = AS_OF,
    l0: L0Store | None = None,
    expect_lake_root: Path | None = None,
    specs: tuple[SnapshotSpec, ...] = SPECS,
) -> Any:
    fetcher, store, transport = build(script if script is not None else _script())
    report = run_daily_snapshot(
        fetcher=fetcher,
        l0=store if l0 is None else l0,
        tracker=tracker,
        alerter=alerter,
        as_of=as_of,
        calendar=trading_calendar(),
        specs=specs,
        expect_lake_root=expect_lake_root,
    )
    return report, store, transport


# ── 1. the lake is absolute ──────────────────────────────────────────────────────────────────


def test_a_wrong_lake_root_fetches_nothing_at_all(
    build: Build, tracker: RecordingTracker, alerter: SpyAlerter, tmp_path: Path
) -> None:
    """The guard has to fire *before* the first request, not after the first payload.

    This has gone wrong twice for real: a worker fetched into its own git worktree's `data/L0` and
    the payloads had to be moved across afterwards. A snapshot in a disposable directory is worse
    than no snapshot because it looks done — so a mismatch must cost zero requests and write zero
    sync rows, leaving the day genuinely uncaptured and visibly so.
    """
    fetcher, store, transport = build(_script())

    with pytest.raises(LakeRootMismatchError, match="worse than no snapshot"):
        run_daily_snapshot(
            fetcher=fetcher,
            l0=store,
            tracker=tracker,
            alerter=alerter,
            as_of=AS_OF,
            calendar=trading_calendar(),
            specs=SPECS,
            expect_lake_root=tmp_path / "somewhere" / "else",
        )

    assert transport.requests == []
    assert tracker.rows == {}


def test_the_declared_root_is_compared_resolved_not_as_written(
    build: Build, tracker: RecordingTracker, alerter: SpyAlerter, tmp_path: Path
) -> None:
    """`…/L0/../L0` is the same directory. A string compare would refuse a correct invocation.

    The assertion exists to catch the wrong *lake*, not the wrong spelling of the right one — and a
    guard that fires on a symlink or a `..` in a systemd unit's path gets switched off, which
    leaves the real failure uncovered.
    """
    report, _store, _transport = _sweep(
        build,
        tracker,
        alerter,
        expect_lake_root=tmp_path / "L0" / ".." / "L0",
    )

    assert report.lake_root == (tmp_path / "L0").resolve()
    assert len(report.captured) == len(SPECS)


# ── 2. the same day twice ────────────────────────────────────────────────────────────────────


def test_a_second_run_the_same_day_spends_no_requests_and_raises_nothing(
    build: Build, tracker: RecordingTracker, alerter: SpyAlerter, tmp_path: Path
) -> None:
    """The acceptance the schedule depends on: idempotence per (source, date).

    A scheduler double-fire, a manual re-run and a retry wrapper all land here. L0 is immutable, so
    a second fetch of one key either wastes a request or raises `L0ImmutabilityError` the moment
    the source's bytes have drifted — which for a snapshot endpoint is *most* days.
    """
    first, store, first_transport = _sweep(build, tracker, alerter)
    assert first.requests_spent == len(SPECS)
    assert len(first_transport.requests) > 0

    second, _store, second_transport = _sweep(build, tracker, alerter, l0=store)

    assert second_transport.requests == []
    assert second.requests_spent == 0
    assert {o.status for o in second.outcomes} == {SnapshotStatus.REUSED}
    assert second.degraded == ()
    # The rows are untouched: no `begin()` on a PUBLISHED row, which would raise.
    assert {row.state for row in tracker.rows.values()} == {SyncState.PUBLISHED}


def test_a_reused_payload_is_still_read_back_out_of_the_lake(
    build: Build, tracker: RecordingTracker, alerter: SpyAlerter
) -> None:
    """The zero-request path re-reads L0 rather than trusting the row, so the report is evidence.

    `L0Store.get` re-hashes on the way out, so a reused snapshot whose bytes have rotted is visible
    in the second run rather than silently reported from a database row that outlived them.
    """
    first, store, _t = _sweep(build, tracker, alerter)
    second, _store, _transport = _sweep(build, tracker, alerter, l0=store)

    by_source = {o.source_id: o for o in second.outcomes}
    for outcome in first.outcomes:
        assert by_source[outcome.source_id].rows == outcome.rows
        assert by_source[outcome.source_id].ref is not None


# ── 3. HTTP 200 is not evidence of a fresh payload ───────────────────────────────────────────


@pytest.mark.parametrize(
    "source_id", ["nse_asm_list", "nse_gsm_list", "nse_esm_list"], ids=["asm", "gsm", "esm"]
)
def test_a_payload_dating_itself_yesterday_is_never_published(
    build: Build, tracker: RecordingTracker, alerter: SpyAlerter, source_id: str
) -> None:
    """NSE's real failure: 200 carrying the previous session's file.

    `sec_bhavdata_full` does exactly this on a market holiday, so it is not hypothetical. The
    payload's own stamp is the evidence, and the outcome must be a *distinct* state: the bytes stay
    in L0 (they are the record of what the source served today) and the date stays unpublished, so
    nothing downstream can read a 08-Sep list as a 09-Sep classification.
    """
    # Replay the 08-Sep fixtures on the next session. Nothing else changes.
    next_session = date(2026, 9, 9)

    report, _store, _transport = _sweep(build, tracker, alerter, as_of=next_session)

    stale = {o.source_id: o for o in report.degraded}
    assert stale[source_id].status is SnapshotStatus.STALE
    assert stale[source_id].content_date == AS_OF
    assert tracker.rows[(source_id, next_session)].state is SyncState.FAILED
    # Kept, not discarded: L0 records what the source served on the day it served it.
    assert stale[source_id].ref is not None
    # CRITICAL, not WARNING: a stale list published as today's would be silently wrong data,
    # which is worse than a source that is visibly absent.
    alerted = {key: severity for severity, _title, _body, key in alerter.sent}
    assert alerted[f"snapshot:{source_id}:{next_session.isoformat()}:STALE"] is Severity.CRITICAL


def test_the_two_undated_csvs_are_not_treated_as_stale_on_another_day(
    build: Build, tracker: RecordingTracker, alerter: SpyAlerter
) -> None:
    """A payload that makes no date claim must not be *assumed* fresh or *assumed* stale.

    `sec_list.csv` and the classification file carry no date anywhere — not in the name, not in the
    header, not in a row. Inventing a staleness verdict for them would make the guard meaningless
    where it does apply; their only guard is structural, and that is recorded honestly rather than
    dressed up as a freshness check.
    """
    report, _store, _transport = _sweep(build, tracker, alerter, as_of=date(2026, 9, 9))

    undated = {
        o.source_id: o
        for o in report.outcomes
        if o.source_id in {"nse_price_bands", "nse_industry_classification"}
    }
    assert {o.status for o in undated.values()} == {SnapshotStatus.CAPTURED}
    assert {o.content_date for o in undated.values()} == {None}


def test_a_stamp_that_varies_within_one_payload_is_refused_not_averaged() -> None:
    """The staleness check rests on one stamp per file. If that stops holding, say so loudly.

    Measured on the 2026-09-08 probe: one distinct `gsmTime` date across all 75 rows, because the
    stamp is the *file's* timestamp and not each name's entry date. If NSE ever changes what the
    field means, picking a winner (the max, the mode) would keep the check running while quietly
    making its verdicts wrong — so it raises instead, and the source parks until re-derived.
    """
    spec = next(s for s in SPECS if s.source_id == "nse_gsm_list")
    rows = json.loads(_body("nse_gsm_list").decode())
    rows[0]["gsmTime"] = "07-Sep-2026 08:07:02"

    with pytest.raises(SnapshotContentError, match="distinct 'gsmTime' values"):
        spec.inspect(json.dumps(rows).encode())


def test_an_unreadable_stamp_is_a_break_not_a_missing_check() -> None:
    """A stamp we cannot parse must not silently become "this source has no staleness check"."""
    spec = next(s for s in SPECS if s.source_id == "nse_esm_list")
    rows = json.loads(_body("nse_esm_list").decode())
    for row in rows:
        row["esmTime"] = "2026-09-08"  # ISO, not NSE's DD-Mon-YYYY

    with pytest.raises(SnapshotContentError, match="is not '%d-%b-%Y'"):
        spec.inspect(json.dumps(rows).encode())


# ── 4. one broken source must not take the others down ───────────────────────────────────────


def test_a_404_parks_one_source_and_the_sweep_captures_the_rest(
    build: Build, tracker: RecordingTracker, alerter: SpyAlerter
) -> None:
    """Four sources capturing history beats five sources debated."""
    report, _store, _transport = _sweep(
        build,
        tracker,
        alerter,
        script=_script({"nse_price_bands": RecordedResponse(status_code=404, body=b"gone")}),
    )

    assert len(report.captured) == len(SPECS) - 1
    parked = report.degraded
    assert [o.source_id for o in parked] == ["nse_price_bands"]
    assert parked[0].status is SnapshotStatus.FAILED
    assert tracker.rows[("nse_price_bands", AS_OF)].state is SyncState.FAILED
    assert len(alerter.sent) == 1


def test_a_soft_404_is_malformed_not_captured(
    build: Build, tracker: RecordingTracker, alerter: SpyAlerter
) -> None:
    """HTTP 200 carrying an HTML shell — the failure the probe actually found.

    `niftyindices.com/IndexConstituent/ind_niftytotalmarketlist.csv` answered 200 with 78 KB of the
    site's Angular page for a CSV URL. Status code, byte count and content length all look healthy;
    only the header says otherwise, which is why the header is compared verbatim.
    """
    shell = b'  <!DOCTYPE html> <html> <head> <meta charset="utf-8" />' + b" " * 4000

    report, _store, _transport = _sweep(
        build,
        tracker,
        alerter,
        script=_script(
            {
                "nse_industry_classification": RecordedResponse(
                    body=shell, headers={"content-type": "text/html; charset=utf-8"}
                )
            }
        ),
    )

    bad = {o.source_id: o for o in report.degraded}
    assert bad["nse_industry_classification"].status is SnapshotStatus.MALFORMED
    assert "not the file the register describes" in bad["nse_industry_classification"].detail
    # Kept in L0 even so: it is the evidence of what the source served.
    assert bad["nse_industry_classification"].ref is not None
    assert len(report.captured) == len(SPECS) - 1


def test_a_header_only_csv_is_refused_as_truncated(
    build: Build, tracker: RecordingTracker, alerter: SpyAlerter
) -> None:
    """The other 200-shaped failure: the right file with its rows gone.

    The header check alone would pass this, and a reader would then see "nobody has a price band
    today" — which is not a fact the source ever asserted.
    """
    header = b"Symbol,Series,Security Name,Band,Remarks\n"

    report, _store, _transport = _sweep(
        build, tracker, alerter, script=_script({"nse_price_bands": RecordedResponse(body=header)})
    )

    bad = {o.source_id: o for o in report.degraded}
    assert bad["nse_price_bands"].status is SnapshotStatus.MALFORMED
    assert "under a floor of" in bad["nse_price_bands"].detail


def test_every_source_failing_raises_so_the_run_is_recorded_failed(
    build: Build, tracker: RecordingTracker, alerter: SpyAlerter
) -> None:
    """A total outage must not be a green run with five alerts nobody is looking at.

    `run_once` records the run FAILED when the job raises, and FAILED is what makes tomorrow's run
    a self-heal rather than a repeat.
    """
    script = _script({spec.source_id: RecordedResponse(status_code=404) for spec in SPECS})

    with pytest.raises(DailySnapshotError, match="landed no healthy source"):
        _sweep(build, tracker, alerter, script=script)


# ── the closed-day path ──────────────────────────────────────────────────────────────────────


def test_a_closed_day_files_a_gap_per_source_and_makes_no_request(
    build: Build, tracker: RecordingTracker, alerter: SpyAlerter
) -> None:
    """A weekend is not a missed day, and the difference has to be recorded, not inferred.

    Filing `GAP` is what keeps a *missed* trading day visible in the D7 gap report: an absent row
    on a session is `NEVER_ATTEMPTED`, while an absent row on a Sunday would be indistinguishable
    from it if nothing was ever written.
    """
    sunday = date(2026, 9, 13)

    report, _store, transport = _sweep(build, tracker, alerter, as_of=sunday)

    assert report.closed
    assert report.day_kind is DayKind.WEEKEND
    assert transport.requests == []
    assert report.requests_spent == 0
    assert {row.state for row in tracker.rows.values()} == {SyncState.GAP}
    assert alerter.sent == []
    assert "nothing was owed" in report.summary()


def test_ganesh_chaturthi_is_a_closed_day_too(
    build: Build, tracker: RecordingTracker, alerter: SpyAlerter
) -> None:
    """A declared holiday, not just a weekend — the day NSE serves the previous session at 200."""
    report, _store, transport = _sweep(build, tracker, alerter, as_of=date(2026, 9, 14))

    assert report.closed
    assert report.day_kind is DayKind.HOLIDAY
    assert transport.requests == []


# ── the set itself ───────────────────────────────────────────────────────────────────────────


def test_every_spec_names_a_registered_source_and_takes_its_url_from_the_register() -> None:
    """A spec that named its own URL would be a second endpoint the day one of them changed."""
    by_id = {entry.id: entry for entry in load_register().sources}

    for spec in DEFAULT_SNAPSHOT_SET:
        assert spec.source_id in by_id, f"{spec.source_id} is not in the source register"
        assert by_id[spec.source_id].status is Status.VERIFIED, (
            f"{spec.source_id} is captured daily but its register status is "
            f"{by_id[spec.source_id].status}"
        )


def test_every_snapshot_filename_carries_the_capture_date() -> None:
    """L0 partitions by *month*, so an undated filename is one key for the whole month.

    `L0Store.put` raises `L0ImmutabilityError` on the second capture under one key once the bytes
    have drifted — which for a snapshot endpoint is most days. This is the bug that had been sitting
    in the identity spine's filenames, undiscovered because the job had never run.
    """
    first, second = date(2026, 9, 8), date(2026, 9, 15)

    for spec in DEFAULT_SNAPSHOT_SET:
        early, late = spec.filename(first), spec.filename(second)
        assert early != late, f"{spec.source_id}: {early} is the same key on both dates"
        assert "20260908" in early, f"{spec.source_id}: {early} does not name its capture date"


def test_the_snapshot_set_covers_every_source_the_deadline_names() -> None:
    """The five snapshot-only families the data study (wave W8) says have no past.

    Pinned as a set rather than a count: a source silently dropped from the sweep is a source whose
    history stops accruing, and nothing at runtime would say so.
    """
    captured = {spec.source_id for spec in DEFAULT_SNAPSHOT_SET}

    assert captured == {
        # industry / sector classification — NSE's own, and BSE's second opinion
        "nse_industry_classification",
        "bse_scrip_master",
        # price bands
        "nse_price_bands",
        # ASM / GSM / ESM surveillance
        "nse_asm_list",
        "nse_gsm_list",
        "nse_esm_list",
        # the identity spine
        "nse_equity_list",
        "nse_symbol_changes",
    }
