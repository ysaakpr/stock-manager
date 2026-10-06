"""`delivery_reconcile`: record `nse_delivery` for sessions the rebuild derived, and nothing else.

The situation under test is the server's after `price_rebuild`: delivery is in `prices_raw`, the
payload is in L0, and `sync_state` has no row, so the gap report calls a delivered session
`NEVER_ATTEMPTED`. Each test builds the lake the real way — the genuine 2026-08-07 bhavcopy and
`sec_bhavdata_full`, through the same source sets the backfill uses — and asserts which rows the
reconcile writes.

The fake store applies the real `SyncRecord.transition` rules, so a reconcile that skipped a state
or drove an illegal edge fails here exactly as it would against Postgres. No network, no database.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from dataplatform.clock import FrozenClock
from dataplatform.identity.master import IdentityMaster
from dataplatform.ingest.backfill import NSE_BHAVCOPY, NSE_DELIVERY, SOURCE_SETS, WriteContext
from dataplatform.ingest.calendar import DayKind
from dataplatform.ingest.delivery_reconcile import (
    DeliveryReconciler,
    Outcome,
    delivery_rows_in_l1,
)
from dataplatform.ingest.nse import bhavcopy
from dataplatform.ingest.price_rebuild import PriceRebuilder
from dataplatform.ingest.source_register import load as load_register
from dataplatform.quality.gaps import GapReason, classify_pair, expectations_from_register
from dataplatform.status.sync_state import SyncRecord, SyncState
from dataplatform.store.l0 import L0Store
from tests.unit.test_delivery_backfill import BHAVCOPY_FIXTURE, DELIVERY_FIXTURE, SESSION
from tests.unit.test_delivery_backfill import master as master_fixture  # noqa: F401

#: The session before the fixture's, used to file the fixture's payload under the wrong date — the
#: shape of 2021-11-04, where NSE served 2021-11-03's file at that date's URL.
WRONG_DATE = date(2026, 8, 6)


class _Store:
    """In-memory `SyncStateStore`: real transition rules, no Postgres."""

    def __init__(self) -> None:
        self.clock = FrozenClock(SESSION)
        self.rows: dict[tuple[str, date], SyncRecord] = {}
        self.writes = 0

    def get(self, source: str, logical_date: date) -> SyncRecord | None:
        return self.rows.get((source, logical_date))

    def _put(self, record: SyncRecord) -> SyncRecord:
        self.rows[record.key] = record
        self.writes += 1
        return record

    def begin(self, source: str, logical_date: date) -> SyncRecord:
        existing = self.get(source, logical_date)
        now = self.clock.now()
        if existing is None:
            return self._put(
                SyncRecord(
                    source, logical_date, SyncState.PENDING, now, attempts=1, first_attempt_at=now
                )
            )
        return self._put(existing.transition(SyncState.PENDING, at=now))

    def _advance(self, source: str, day: date, to: SyncState, **kw: object) -> SyncRecord:
        return self._put(self.rows[(source, day)].transition(to, at=self.clock.now(), **kw))  # type: ignore[arg-type]

    def mark_fetched(
        self, source: str, day: date, *, checksum: str, l0_path: str | None = None
    ) -> SyncRecord:
        return self._advance(source, day, SyncState.FETCHED, checksum=checksum, l0_path=l0_path)

    def mark_validated(self, source: str, day: date) -> SyncRecord:
        return self._advance(source, day, SyncState.VALIDATED)

    def mark_normalized(self, source: str, day: date) -> SyncRecord:
        return self._advance(source, day, SyncState.NORMALIZED)

    def mark_published(self, source: str, day: date) -> SyncRecord:
        return self._advance(source, day, SyncState.PUBLISHED)

    def mark_failed(
        self, source: str, day: date, error: str, *, retryable: bool = True
    ) -> SyncRecord:
        return self._advance(source, day, SyncState.FAILED, error=error, retryable=retryable)


@pytest.fixture
def master(master_fixture: IdentityMaster) -> IdentityMaster:  # noqa: F811
    return master_fixture


def _lake(root: Path, *, with_delivery: bool) -> L0Store:
    store = L0Store(clock=FrozenClock(SESSION), data_root=root)
    store.put(
        "nse_bhavcopy_udiff",
        SESSION,
        BHAVCOPY_FIXTURE.name,
        BHAVCOPY_FIXTURE.read_bytes(),
        content_type="application/zip",
    )
    if with_delivery:
        store.put(
            "nse_sec_bhavdata_full",
            SESSION,
            DELIVERY_FIXTURE.name,
            DELIVERY_FIXTURE.read_bytes(),
            content_type="text/csv",
        )
    return store


def _write_prices_only(lake: L0Store, master: IdentityMaster) -> None:
    """The partition as a price-only backfill left it: delivery NULL on every row."""
    ref = lake.ref_for("nse_bhavcopy_udiff", SESSION, BHAVCOPY_FIXTURE.name)
    ctx = WriteContext(l0=lake, data_root=lake.data_root, master=master, register=load_register())
    SOURCE_SETS[NSE_BHAVCOPY].write(bhavcopy.parse_l0_report(lake, ref), ctx)


def _rebuilt(lake: L0Store, master: IdentityMaster) -> L0Store:
    """The server's state: prices written, then delivery re-derived by `price_rebuild`."""
    _write_prices_only(lake, master)
    report = PriceRebuilder(
        l0=lake, register=load_register(), master=master, data_root=lake.data_root
    ).run([SESSION])
    assert report.rebuilt == 1
    return lake


def _reconcile(
    lake: L0Store,
    store: _Store,
    sessions: list[date],
    *,
    apply: bool = True,
    data_root: Path | None = None,
) -> tuple[Outcome, ...]:
    commits: list[None] = []
    report = DeliveryReconciler(
        l0=lake,
        register=load_register(),
        sync=store,
        commit=lambda: commits.append(None),
        apply=apply,
        data_root=lake.data_root if data_root is None else data_root,
    ).run(sessions)
    if not apply:
        assert commits == []
    outcomes = {day: outcome for outcome, days in report.sessions.items() for day in days}
    assert sorted(outcomes) == sorted(sessions)  # every session lands in exactly one outcome
    return tuple(outcomes[day] for day in sessions)


def test_a_rebuilt_session_is_recorded_published_with_its_l0_evidence(
    tmp_path: Path, master: IdentityMaster
) -> None:
    lake = _rebuilt(_lake(tmp_path, with_delivery=True), master)
    store = _Store()

    assert _reconcile(lake, store, [SESSION]) == (Outcome.PUBLISHED,)

    record = store.rows[(NSE_DELIVERY, SESSION)]
    assert record.state is SyncState.PUBLISHED
    assert record.attempts == 1
    ref = lake.ref_for("nse_sec_bhavdata_full", SESSION, DELIVERY_FIXTURE.name)
    assert record.checksum == ref.sha256
    assert record.l0_path == ref.key
    # ...and the gap report stops calling it a miss.
    expectation = expectations_from_register()[NSE_DELIVERY]
    assert classify_pair(expectation, SESSION, DayKind.SESSION, None) is not None
    assert classify_pair(expectation, SESSION, DayKind.SESSION, record) is None


def test_a_session_without_its_l0_payload_is_never_marked_published(
    tmp_path: Path, master: IdentityMaster
) -> None:
    """No payload is no evidence — even when L1 carries delivery for the session.

    The L1 here is fully derived (delivery joined) and only the L0 delivery payload is missing, so
    the one thing standing between this session and a PUBLISHED row is the L0 check. A reconcile
    that trusted L1 alone would publish a session whose source bytes nobody holds.
    """
    derived = _rebuilt(_lake(tmp_path / "derived", with_delivery=True), master)
    assert delivery_rows_in_l1(SESSION, data_root=derived.data_root) > 0
    bare = _lake(tmp_path / "bare", with_delivery=False)
    store = _Store()

    outcomes = _reconcile(bare, store, [SESSION], data_root=derived.data_root)

    assert outcomes == (Outcome.NO_PAYLOAD,)
    assert store.rows == {}
    assert store.writes == 0


def test_a_payload_whose_delivery_never_reached_l1_is_left_for_the_rebuild(
    tmp_path: Path, master: IdentityMaster
) -> None:
    """Payload present and parseable, partition written from the bhavcopy alone: not PUBLISHED."""
    lake = _lake(tmp_path, with_delivery=True)
    _write_prices_only(lake, master)
    assert delivery_rows_in_l1(SESSION, data_root=lake.data_root) == 0
    store = _Store()

    assert _reconcile(lake, store, [SESSION]) == (Outcome.NOT_DERIVED,)
    assert store.rows == {}


def test_a_payload_stating_another_session_is_recorded_failed_and_explained(
    tmp_path: Path, master: IdentityMaster
) -> None:
    """The 2021-11-04 shape: the archive served the previous session's file at this date's URL.

    It must end FAILED with the parser's reason and its L0 key, so the gap report classifies it as
    `L0_PRESENT_L1_ABSENT` with that reason — never PUBLISHED, and never silently absent.
    """
    lake = _rebuilt(_lake(tmp_path, with_delivery=True), master)
    lake.put(
        "nse_sec_bhavdata_full",
        WRONG_DATE,
        "sec_bhavdata_full_06082026.csv",
        DELIVERY_FIXTURE.read_bytes(),  # states 2026-08-07
        content_type="text/csv",
    )
    store = _Store()

    assert _reconcile(lake, store, [WRONG_DATE, SESSION]) == (
        Outcome.PARSE_FAILED,
        Outcome.PUBLISHED,
    )

    record = store.rows[(NSE_DELIVERY, WRONG_DATE)]
    assert record.state is SyncState.FAILED
    assert record.retryable
    assert record.last_error is not None and "2026-08-07" in record.last_error
    assert record.l0_path is not None and record.l0_path.endswith("sec_bhavdata_full_06082026.csv")
    entry = classify_pair(
        expectations_from_register()[NSE_DELIVERY],
        WRONG_DATE,
        DayKind.SESSION,
        record,
        l0_present=True,
    )
    assert entry is not None and entry.reason is GapReason.L0_PRESENT_L1_ABSENT
    assert not entry.reason.explained  # still owed an answer — just the right one


@pytest.mark.parametrize("state", [SyncState.PUBLISHED, SyncState.FAILED, SyncState.FETCHED])
def test_an_existing_row_is_never_touched(
    tmp_path: Path, master: IdentityMaster, state: SyncState
) -> None:
    lake = _rebuilt(_lake(tmp_path, with_delivery=True), master)
    store = _Store()
    existing = SyncRecord(
        NSE_DELIVERY,
        SESSION,
        state,
        FrozenClock(SESSION).now(),
        attempts=3,
        last_error="an earlier attempt's own reason" if state is SyncState.FAILED else None,
    )
    store.rows[existing.key] = existing

    assert _reconcile(lake, store, [SESSION]) == (Outcome.HAS_ROW,)
    assert store.rows[existing.key] is existing
    assert store.writes == 0


def test_a_dry_run_reports_the_same_outcomes_and_writes_nothing(
    tmp_path: Path, master: IdentityMaster
) -> None:
    lake = _rebuilt(_lake(tmp_path, with_delivery=True), master)
    lake.put(
        "nse_sec_bhavdata_full",
        WRONG_DATE,
        "sec_bhavdata_full_06082026.csv",
        DELIVERY_FIXTURE.read_bytes(),
        content_type="text/csv",
    )
    store = _Store()

    dry = _reconcile(lake, store, [WRONG_DATE, SESSION], apply=False)

    assert dry == (Outcome.PARSE_FAILED, Outcome.PUBLISHED)
    assert store.writes == 0
    assert _reconcile(lake, store, [WRONG_DATE, SESSION]) == dry


def test_a_second_run_is_a_no_op(tmp_path: Path, master: IdentityMaster) -> None:
    lake = _rebuilt(_lake(tmp_path, with_delivery=True), master)
    store = _Store()
    _reconcile(lake, store, [SESSION])
    writes = store.writes

    assert _reconcile(lake, store, [SESSION]) == (Outcome.HAS_ROW,)
    assert store.writes == writes
