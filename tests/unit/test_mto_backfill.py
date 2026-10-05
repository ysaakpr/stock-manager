"""The W3 MTO delivery deep-backfill driver: the lake guard, resume, 404-as-evidence, promotion
onto the W1 prices, and the quarantine hazard.

The claims a campaign must not be launched without, each with a test that fails if it broke:

* **The lake root is asserted before request #1.** No `--expect-l0-root`, or a mismatch, and the
  run refuses — a worktree resolves a relative `data_root` to its own checkout.
* **A second run over an acquired range spends zero requests**, counted on the transport.
* **A 404 is evidence** (`NO_MTO_PUBLISHED` in this campaign's own journal), never an error, and a
  run of them hard-stops on a separate counter from real failures.
* **A payload that states another session is never accepted** as this one's.
* **The quarantine partition survives promotion.** It is written whole, so the delivery write must
  hand `write_prices_raw` the bhavcopy's refusals and the delivery rows in one call — asserted by
  reading the partition back — and a row the write would not re-derive stops the session with the
  partition byte-for-byte untouched.

Offline by construction: a `RecordedTransport`, a `tmp_path` lake, an in-memory sync store.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dataplatform.alerts import build_alerter
from dataplatform.clock import FrozenClock
from dataplatform.config import Settings
from dataplatform.identity.master import (
    Exchange,
    IdentityMaster,
    ListingStatus,
    Security,
    SymbolWindow,
)
from dataplatform.ingest import mto_backfill as mb
from dataplatform.ingest.backfill import NSE_DELIVERY
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.fetcher import Fetcher, RecordedResponse, RecordedTransport
from dataplatform.ingest.no_session_journal import NoSessionJournal
from dataplatform.ingest.nse import bhavcopy
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncState
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.paths import Layer, partition_path
from dataplatform.store.schemas import (
    PRICES_RAW_DATASET,
    PRICES_RAW_QUARANTINE_DATASET,
    PRICES_RAW_QUARANTINE_SCHEMA,
    PriceQuarantineReason,
)

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures"
MTO_DIR: Final = FIXTURES / "nse_mto"
BHAV_FIXTURE: Final = FIXTURES / "nse_bhavcopy" / "legacy" / "cm22JUN2011bhav.csv.zip"

CLOCK: Final = FrozenClock(date(2026, 9, 29))
START: Final = date(2011, 6, 22)  # ISIN-era floor; MTO fixture MTO_22062011.DAT
MULTI: Final = date(2011, 7, 1)  # the D+N file
#: Calendar sessions the fixtures do not cover; the transport 404s them.
UNPUBLISHED: Final = (date(2011, 6, 23), date(2011, 6, 24), date(2011, 6, 27))

#: The row the doctored bhavcopy publishes with a placeholder ISIN, so its session has a refusal.
PLACEHOLDER_SYMBOL: Final = "3MINDIA"


# ── wiring ───────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def register() -> SourceRegister:
    return load_register()


def _mto(day: date) -> bytes:
    return (MTO_DIR / f"MTO_{day:%d%m%Y}.DAT").read_bytes()


def _journal(root: Path) -> NoSessionJournal:
    return NoSessionJournal(mb.journal_path_for(root), clock=CLOCK, evidence=mb.EVIDENCE_NO_MTO)


def _transport(
    register: SourceRegister,
    served: dict[date, bytes],
    not_found: tuple[date, ...] = (),
) -> RecordedTransport:
    script: dict[str, RecordedResponse] = {}
    for day, body in served.items():
        script[mb.mto_url(day, register=register)] = RecordedResponse(
            status_code=200, body=body, headers={"content-type": "application/octet-stream"}
        )
    for day in not_found:
        script[mb.mto_url(day, register=register)] = RecordedResponse(
            status_code=404, body=b"<html>Not Found</html>", headers={"content-type": "text/html"}
        )
    return RecordedTransport(cast("Any", script))


def _acquisition(
    tmp_path: Path, transport: RecordedTransport, register: SourceRegister, **kwargs: Any
) -> mb.MtoAcquisition:
    settings = Settings(data_root=tmp_path)
    l0 = L0Store(clock=CLOCK, data_root=tmp_path)
    fetcher = Fetcher(
        transport=transport,
        l0=l0,
        alerter=build_alerter(settings, clock=CLOCK),
        clock=CLOCK,
        register=register,
        settings=settings,
        sleep=lambda _seconds: None,
    )
    return mb.MtoAcquisition(
        fetcher=fetcher, l0=l0, journal=_journal(tmp_path), register=register, **kwargs
    )


def _requests(transport: RecordedTransport) -> int:
    return len(transport.requests)


# ── the lake guard ───────────────────────────────────────────────────────────────────────────


def test_a_run_that_does_not_name_its_lake_is_refused(tmp_path: Path) -> None:
    with pytest.raises(mb.LakeRootMismatchError, match="--expect-l0-root is required"):
        mb.require_l0_root(L0Store(clock=CLOCK, data_root=tmp_path), None)


def test_a_run_whose_lake_resolves_elsewhere_is_refused(tmp_path: Path) -> None:
    l0 = L0Store(clock=CLOCK, data_root=tmp_path / "worktree")
    with pytest.raises(mb.LakeRootMismatchError, match="a worktree is a checkout"):
        mb.require_l0_root(l0, tmp_path / "authoritative" / "L0")


def test_the_declared_lake_is_accepted(tmp_path: Path) -> None:
    l0 = L0Store(clock=CLOCK, data_root=tmp_path)
    assert mb.require_l0_root(l0, l0.root) == l0.root.resolve()


def test_the_cli_refuses_before_any_request_without_the_lake_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2 and no socket: `leased_fetcher` is never reached, so it is replaced by a bomb."""
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(mb, "leased_fetcher", _bomb)
    assert mb.main(["acquire", "--dates", START.isoformat()]) == 2
    assert "--expect-l0-root is required" in capsys.readouterr().err


def _bomb(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("a fetcher was opened before the lake root was checked")


# ── planning ─────────────────────────────────────────────────────────────────────────────────


def test_the_plan_refuses_the_pre_isin_era() -> None:
    with pytest.raises(ValueError, match="ISIN column"):
        mb.plan_sessions(date(2011, 6, 21), START, calendar=trading_calendar())


def test_the_plan_refuses_the_sec_bhavdata_era() -> None:
    with pytest.raises(ValueError, match="sec_bhavdata_full era"):
        mb.plan_sessions(START, mb.MTO_ERA_END, calendar=trading_calendar())


def test_the_plan_is_the_calendars_sessions() -> None:
    plan = mb.plan_sessions(START, date(2011, 7, 1), calendar=trading_calendar())
    assert plan.dates[0] == START and plan.dates[-1] == MULTI
    assert all(day.weekday() < 5 for day in plan.dates)
    assert plan.basis == "calendar+prices_raw"


def test_the_special_saturdays_are_planned_from_the_calendar_itself() -> None:
    """2012-01-07 was a special Saturday session. The calendar now declares it, so delivery for it
    is planned without any price evidence at all."""
    saturday = date(2012, 1, 7)
    plan = mb.plan_sessions(date(2012, 1, 2), date(2012, 1, 9), calendar=trading_calendar())
    assert saturday in plan.dates
    assert date(2012, 1, 8) not in plan.dates
    assert "0 priced session(s) outside the calendar added" in plan.note


def test_a_priced_session_the_calendar_omits_is_planned() -> None:
    """The backstop: a priced date the calendar does not list is planned and named in the note, so
    the calendar can be corrected; a priced date outside the range is not. 2012-01-08 is a Sunday
    the calendar calls WEEKEND, standing in for a session it has yet to learn about."""
    sunday = date(2012, 1, 8)
    calendar = trading_calendar()
    assert sunday not in calendar.expected_data_dates(date(2012, 1, 2), date(2012, 1, 9))
    plan = mb.plan_sessions(
        date(2012, 1, 2),
        date(2012, 1, 9),
        calendar=calendar,
        priced=(sunday, date(2012, 1, 14)),
    )
    assert sunday in plan.dates
    assert date(2012, 1, 14) not in plan.dates
    assert "1 priced session(s) outside the calendar added (2012-01-08)" in plan.note


def test_price_sessions_are_read_off_the_lake(lake: _Lake) -> None:
    assert mb.price_sessions(data_root=lake.root) == frozenset()
    lake.promotion.promote([START])
    assert mb.price_sessions(data_root=lake.root) == {START}


# ── acquisition ──────────────────────────────────────────────────────────────────────────────


def test_acquire_stores_payloads_and_a_rerun_spends_nothing(
    tmp_path: Path, register: SourceRegister
) -> None:
    sessions = [START, UNPUBLISHED[0], MULTI]
    served = {START: _mto(START), MULTI: _mto(MULTI)}
    first = _transport(register, served, not_found=(UNPUBLISHED[0],))
    report = _acquisition(tmp_path, first, register).run(sessions)
    assert report.count(mb.SessionState.FETCHED) == 2
    assert report.count(mb.SessionState.NO_MTO) == 1
    assert report.requests_spent == _requests(first) == 3
    assert not report.hard_stopped

    second = _transport(register, served, not_found=(UNPUBLISHED[0],))
    again = _acquisition(tmp_path, second, register).run(sessions)
    assert _requests(second) == 0
    assert again.count(mb.SessionState.ALREADY_IN_L0) == 2
    assert again.count(mb.SessionState.KNOWN_NO_MTO) == 1


def test_a_404_is_journalled_as_evidence_not_an_error(
    tmp_path: Path, register: SourceRegister
) -> None:
    transport = _transport(register, {}, not_found=(UNPUBLISHED[0],))
    report = _acquisition(tmp_path, transport, register).run([UNPUBLISHED[0]])
    assert report.count(mb.SessionState.FAILED) == 0
    records = _journal(tmp_path).records
    assert [r.trade_date for r in records] == [UNPUBLISHED[0]]
    assert records[0].evidence == mb.EVIDENCE_NO_MTO
    assert records[0].http_status == 404


def test_consecutive_404s_hard_stop_on_their_own_counter(
    tmp_path: Path, register: SourceRegister
) -> None:
    transport = _transport(register, {START: _mto(START)}, not_found=UNPUBLISHED)
    acq = _acquisition(tmp_path, transport, register, no_session_streak_limit=3)
    report = acq.run([*UNPUBLISHED, START])
    assert report.hard_stopped
    assert "consecutive 404s" in (report.stop_reason or "")
    assert report.count(mb.SessionState.FETCHED) == 0, "the run went on past the stop"


def test_a_payload_stating_another_session_is_never_accepted(
    tmp_path: Path, register: SourceRegister
) -> None:
    """The archive serving 2011-06-22's bytes for 2011-06-23 is a stale file, not a session."""
    transport = _transport(register, {UNPUBLISHED[0]: _mto(START)})
    report = _acquisition(tmp_path, transport, register).run([UNPUBLISHED[0]])
    outcome = report.outcomes[0]
    assert outcome.state is mb.SessionState.STALE_PAYLOAD
    assert outcome.error == "file states 2011-06-22"
    assert outcome.state.is_failure


# ── promotion ────────────────────────────────────────────────────────────────────────────────


@dataclass
class _Row:
    state: SyncState
    error: str | None = None


class _FakeSync:
    """In-memory `SyncStateStore` stand-in: only what the promotion driver calls."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, date], _Row] = {}

    def get(self, source: str, logical_date: date) -> _Row | None:
        return self.rows.get((source, logical_date))

    def begin(self, source: str, logical_date: date) -> _Row:
        row = _Row(SyncState.PENDING)
        self.rows[(source, logical_date)] = row
        return row

    def _to(self, source: str, day: date, state: SyncState) -> _Row:
        row = self.rows[(source, day)]
        row.state = state
        return row

    def mark_fetched(self, source: str, day: date, *, checksum: str, l0_path: str) -> _Row:
        return self._to(source, day, SyncState.FETCHED)

    def mark_validated(self, source: str, day: date) -> _Row:
        return self._to(source, day, SyncState.VALIDATED)

    def mark_normalized(self, source: str, day: date) -> _Row:
        return self._to(source, day, SyncState.NORMALIZED)

    def mark_published(self, source: str, day: date) -> _Row:
        return self._to(source, day, SyncState.PUBLISHED)

    def mark_failed(self, source: str, day: date, error: str, *, retryable: bool = True) -> _Row:
        row = self.rows.setdefault((source, day), _Row(SyncState.PENDING))
        row.state, row.error = SyncState.FAILED, error
        return row


def _bhavcopy_with_a_placeholder() -> bytes:
    """The real 2011-06-22 bhavcopy with one row's ISIN replaced by the exchange's `DUMMY`."""
    with zipfile.ZipFile(BHAV_FIXTURE) as source:
        (name,) = source.namelist()
        lines = source.read(name).decode().splitlines(keepends=True)
    doctored = [
        line.replace(line.split(",")[12], "DUMMY")
        if line.startswith(f"{PLACEHOLDER_SYMBOL},EQ,")
        else line
        for line in lines
    ]
    assert doctored != lines
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as target:
        target.writestr(name, "".join(doctored))
    return out.getvalue()


def _master(bhav: bytes) -> IdentityMaster:
    """The session's own EQ `(symbol, isin)` pairs — the bhavcopy states them natively. EQ only,
    as the equity master is: BRITANNIA's debenture series carries a second ISIN on one symbol."""
    rows = bhavcopy.parse(bhav, filename=BHAV_FIXTURE.name, trade_date=START)
    pairs = sorted({(row.symbol, row.isin) for row in rows if row.series == "EQ"})
    return IdentityMaster(
        tuple(
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol=sym,
                valid_from=date(2000, 1, 1),
                valid_to=None,
                isin=isin,
            )
            for sym, isin in pairs
        ),
        securities=tuple(
            Security(
                isin=isin,
                name=sym,
                primary_exchange=Exchange.NSE,
                status=ListingStatus.ACTIVE,
                first_seen_date=date(2000, 1, 1),
            )
            for sym, isin in pairs
        ),
    )


@dataclass
class _Lake:
    root: Path
    l0: L0Store
    sync: _FakeSync
    promotion: mb.MtoPromotion


@pytest.fixture
def lake(tmp_path: Path, register: SourceRegister) -> _Lake:
    """W1's state for 2011-06-22 — bhavcopy in L0 (one placeholder ISIN) — plus its MTO payload."""
    l0 = L0Store(clock=CLOCK, data_root=tmp_path)
    bhav = _bhavcopy_with_a_placeholder()
    l0.put("nse_bhavcopy_legacy", START, BHAV_FIXTURE.name, bhav, content_type="application/zip")
    l0.put(
        "nse_mto",
        START,
        f"MTO_{START:%d%m%Y}.DAT",
        _mto(START),
        content_type="application/octet-stream",
    )
    sync = _FakeSync()
    promotion = mb.MtoPromotion(
        l0=l0,
        sync=cast("Any", sync),
        commit=lambda: None,
        register=register,
        master=_master(bhav),
        data_root=tmp_path,
    )
    return _Lake(root=tmp_path, l0=l0, sync=sync, promotion=promotion)


def _quarantine_path(root: Path) -> Path:
    return partition_path(Layer.L1, PRICES_RAW_QUARANTINE_DATASET, START, data_root=root)


def _quarantine(root: Path) -> list[dict[str, Any]]:
    table = pq.read_table(_quarantine_path(root), schema=PRICES_RAW_QUARANTINE_SCHEMA)
    return cast("list[dict[str, Any]]", table.to_pylist())


def test_promotion_lands_delivery_on_the_w1_prices(lake: _Lake) -> None:
    report = lake.promotion.promote([START])
    (session,) = report.sessions
    assert session.state == "PUBLISHED", session.error
    assert session.delivery_rows == 1473
    assert session.delivery_rows == (
        session.delivery_joined + session.delivery_unresolved + session.delivery_orphaned
    )
    table = pq.read_table(partition_path(Layer.L1, PRICES_RAW_DATASET, START, data_root=lake.root))
    rows = table.to_pylist()
    reliance = next(r for r in rows if (r["symbol"], r["series"]) == ("RELIANCE", "EQ"))
    assert reliance["deliv_qty"] == 2343085
    assert lake.sync.rows[(NSE_DELIVERY, START)].state is SyncState.PUBLISHED


def test_a_published_session_is_not_promoted_twice(lake: _Lake) -> None:
    lake.promotion.promote([START])
    assert lake.promotion.promote([START]).sessions[0].state == "SKIPPED_PUBLISHED"


def test_a_session_without_an_mto_payload_is_reported_not_failed(lake: _Lake) -> None:
    assert lake.promotion.promote([MULTI]).sessions[0].state == "MISSING_IN_L0"


def test_the_bhavcopys_quarantine_rows_survive_the_delivery_write(lake: _Lake) -> None:
    """The hazard. W1 wrote this session's quarantine partition with the placeholder-ISIN row; the
    delivery write rewrites the partition whole, so that row must come back in the same write as
    the delivery rows that could not be placed — not be replaced by them."""
    bhav = bhavcopy.parse_report(
        lake.l0.get(lake.l0.ref_for("nse_bhavcopy_legacy", START, BHAV_FIXTURE.name)),
        filename=BHAV_FIXTURE.name,
        trade_date=START,
    )
    # W1's write for this session, reproduced: prices plus the refusal, no delivery.
    write_prices_raw(
        list(bhav.rows), exchange=Exchange.NSE, unidentified_rows=bhav.refused, data_root=lake.root
    )
    before = _quarantine(lake.root)
    assert [(r["symbol"], r["reason"]) for r in before] == [
        (PLACEHOLDER_SYMBOL, PriceQuarantineReason.ISIN_NOT_PUBLISHED)
    ]

    session = lake.promotion.promote([START]).sessions[0]
    assert session.state == "PUBLISHED", session.error
    after = _quarantine(lake.root)
    placeholder = [r for r in after if r["reason"] == PriceQuarantineReason.ISIN_NOT_PUBLISHED]
    assert [(r["symbol"], r["isin"]) for r in placeholder] == [(PLACEHOLDER_SYMBOL, "DUMMY")]
    delivery_side = [r for r in after if r["reason"] != PriceQuarantineReason.ISIN_NOT_PUBLISHED]
    # Both sets in one partition: the refusal and every delivery row that could not be placed.
    assert len(delivery_side) == session.delivery_unresolved + session.delivery_orphaned
    assert len(delivery_side) >= 1  # the placeholder row's own delivery figure has no price row

    # And a re-promotion (sync reset) re-derives the same partition: nothing accumulates.
    lake.sync.rows.clear()
    lake.promotion.promote([START])
    assert _quarantine(lake.root) == after


def test_another_exchange_s_quarantine_rows_survive_the_promotion(lake: _Lake) -> None:
    """A BSE refusal in the shared partition is carried through the NSE delivery write unchanged.

    This test used to assert the session *failed*, because the quarantine partition was written
    whole from the NSE write alone and the BSE row would have vanished. The writer now replaces
    only the writing exchange's rows (2026-10-05), so the guard has nothing to protect it from.
    """
    foreign = {
        "symbol": "500325",
        "series": "A",
        "trade_date": START,
        "exchange": Exchange.BSE.value,
        "isin": None,
        "deliv_qty": None,
        "deliv_pct": None,
        "reason": PriceQuarantineReason.ISIN_NOT_PUBLISHED,
    }
    path = _quarantine_path(lake.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([foreign], schema=PRICES_RAW_QUARANTINE_SCHEMA), path)

    session = lake.promotion.promote([START]).sessions[0]
    assert session.state == "PUBLISHED", session.error
    survivors = pq.read_table(path, schema=PRICES_RAW_QUARANTINE_SCHEMA).to_pylist()
    assert foreign in survivors


def test_a_relabelled_quarantine_row_is_not_counted_as_surviving(
    lake: _Lake, register: SourceRegister
) -> None:
    """The same symbol under a different reason is a different fact; the guard keys on reason."""
    row = {
        "symbol": PLACEHOLDER_SYMBOL,
        "series": "EQ",
        "trade_date": START,
        "exchange": Exchange.NSE.value,
        "isin": "DUMMY",
        "deliv_qty": None,
        "deliv_pct": None,
        "reason": PriceQuarantineReason.ISIN_COLUMN_ABSENT,
    }
    path = _quarantine_path(lake.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([row], schema=PRICES_RAW_QUARANTINE_SCHEMA), path)
    with pytest.raises(mb.QuarantineClobberError, match="isin_column_absent"):
        mb.guard_quarantine(START, l0=lake.l0, register=register, data_root=lake.root)


# ── the coverage artefact ────────────────────────────────────────────────────────────────────


def test_the_coverage_report_reads_the_lake(lake: _Lake, register: SourceRegister) -> None:
    lake.promotion.promote([START])
    journal = _journal(lake.root)
    journal.record(UNPUBLISHED[0], url="u", http_status=404)
    journal.record(date(2011, 8, 15), url="u", http_status=404)  # a holiday, probed explicitly
    plan = mb.plan_sessions(START, date(2011, 6, 30), calendar=trading_calendar())
    text = mb.coverage_report(
        plan, l0=lake.l0, journal=journal, register=register, data_root=lake.root
    )
    (year,) = [line for line in text.splitlines() if line.startswith("| 2011 ")]
    cells = [c.strip() for c in year.strip("|").split("|")]
    assert cells[1:5] == [str(len(plan)), "1", "1", str(len(plan) - 2)]
    assert int(cells[6]) > 0 and cells[7] == "N x1"
    assert "2011-06-23" in text
    assert "2011-08-15" not in text, "a probe outside the plan's range must not be listed"
