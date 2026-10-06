"""M11.2 acceptance: the index-valuation backfill runner, end to end and offline.

The network is a `RecordedTransport` scripted with the real captured close-all files
(`tests/fixtures/nifty_index_close/`, both naming eras), the lake is a temp directory, and
`sync_state` is an in-memory stand-in speaking the transitions the runner drives. No socket opens.

Acceptance criteria, one test each:
  1. resumable and checkpointed — a re-run redoes no published session
     (`test_rerun_redoes_no_published_session`), and a 404 closes its session to retries;
  2. coverage reported per session, unmapped names listed separately
     (`test_coverage_is_per_session_and_unmapped_names_are_listed_separately`);
  3. a 403 spike parks with an enumerated cause and a non-zero exit
     (`test_403_spike_parks_with_enumerated_cause`).
"""

from __future__ import annotations

import socket
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from dataplatform.alerts import build_alerter
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.fetcher import Fetcher, RecordedResponse, RecordedTransport
from dataplatform.ingest.macro import backfill as mb
from dataplatform.ingest.macro import load_index_aliases
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncState
from dataplatform.store.l0 import L0Store
from dataplatform.store.macro_series import read_l1, read_pit

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "nifty_index_close"
FILES: Final = {
    date(2012, 10, 1): FIXTURES / "cnx_era" / "ind_close_all_01102012.csv",
    date(2015, 11, 6): FIXTURES / "cnx_era" / "ind_close_all_06112015.csv",
    date(2015, 11, 10): FIXTURES / "nifty_era" / "ind_close_all_10112015.csv",
    date(2026, 9, 1): FIXTURES / "nifty_era" / "ind_close_all_01092026.csv",
}
#: A real session the archive is scripted to answer 404 for — the "not published" path.
MISSING: Final = date(2015, 11, 9)
CLOCK: Final = FrozenClock(datetime(2026, 10, 6, 14, 0, tzinfo=IST))


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; macro-backfill tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class _Row:
    def __init__(self, state: SyncState, *, retryable: bool = True, error: str = "") -> None:
        self.state = state
        self.retryable = retryable
        self.last_error = error


class _FakeSync:
    """In-memory `sync_state` keyed by `(source, logical_date)`, modelling what the runner calls."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, date], _Row] = {}

    def get(self, source: str, logical_date: date) -> _Row | None:
        return self.rows.get((source, logical_date))

    def begin(self, source: str, logical_date: date) -> _Row:
        prior = self.rows.get((source, logical_date))
        assert prior is None or prior.state is not SyncState.PUBLISHED, "re-began a published row"
        assert prior is None or prior.retryable, "re-began a non-retryable failure"
        row = _Row(SyncState.PENDING)
        self.rows[(source, logical_date)] = row
        return row

    def _to(self, source: str, logical_date: date, state: SyncState) -> _Row:
        row = self.rows[(source, logical_date)]
        row.state = state
        return row

    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> _Row:
        return self._to(source, logical_date, SyncState.FETCHED)

    def mark_validated(self, source: str, logical_date: date) -> _Row:
        return self._to(source, logical_date, SyncState.VALIDATED)

    def mark_normalized(self, source: str, logical_date: date) -> _Row:
        return self._to(source, logical_date, SyncState.NORMALIZED)

    def mark_published(self, source: str, logical_date: date) -> _Row:
        return self._to(source, logical_date, SyncState.PUBLISHED)

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> _Row:
        row = self.rows.setdefault((source, logical_date), _Row(SyncState.PENDING))
        row.state, row.retryable, row.last_error = SyncState.FAILED, retryable, error
        return row


def _plan(*sessions: date) -> list[mb.SessionUnit]:
    plan = mb.build_plan(
        date(2012, 10, 1), date(2026, 9, 1), calendar=trading_calendar(), register=load_register()
    )
    wanted = set(sessions)
    return [unit for unit in plan if unit.session in wanted]


def _transport(plan: list[mb.SessionUnit], **overrides: Any) -> RecordedTransport:
    script: dict[str, Any] = {}
    for unit in plan:
        if unit.session in FILES:
            script[unit.url] = RecordedResponse(body=FILES[unit.session].read_bytes())
        else:
            script[unit.url] = RecordedResponse(status_code=404, body=b"<html>not found</html>")
    script.update(overrides)
    return RecordedTransport(script)


def _runner(
    transport: RecordedTransport, tmp_path: Path, sync: _FakeSync, **kwargs: Any
) -> mb.MacroBackfillRunner:
    settings = Settings(data_root=tmp_path)
    fetcher = Fetcher(
        transport=transport,
        l0=L0Store(clock=CLOCK, data_root=tmp_path),
        alerter=build_alerter(settings, clock=CLOCK),
        clock=CLOCK,
        register=load_register(),
        settings=settings,
        sleep=lambda _seconds: None,
    )
    return mb.MacroBackfillRunner(
        fetcher=fetcher,
        l0=L0Store(clock=CLOCK, data_root=tmp_path),
        sync=sync,
        commit=lambda: None,
        data_root=tmp_path,
        **kwargs,
    )


def test_plan_is_every_expected_data_date_on_the_archive_host() -> None:
    plan = mb.build_plan(
        date(2015, 11, 2), date(2015, 11, 13), calendar=trading_calendar(), register=load_register()
    )
    sessions = [unit.session for unit in plan]
    assert date(2015, 11, 7) not in sessions  # a Saturday
    assert sessions == trading_calendar().expected_data_dates(date(2015, 11, 2), date(2015, 11, 13))
    assert plan[0].url == (
        "https://nsearchives.nseindia.com/content/indices/ind_close_all_02112015.csv"
    )
    with pytest.raises(ValueError, match="archive epoch"):
        mb.build_plan(
            date(2012, 9, 1),
            date(2012, 10, 5),
            calendar=trading_calendar(),
            register=load_register(),
        )


def test_backfill_lands_valuation_facts_under_one_series_across_the_rename(
    tmp_path: Path,
) -> None:
    plan = _plan(*FILES)
    report = _runner(_transport(plan), tmp_path, _FakeSync()).run(plan)
    assert report.published == 4 and report.requests == 4 and not report.parked

    pe_2012 = {f.series_id: f for f in read_l1(date(2012, 10, 1), data_root=tmp_path)}
    # "S&P CNX Nifty" in 2012 lands under today's name.
    assert pe_2012["IN.NSE.NIFTY_50.PE"].value == Decimal("19.22")
    assert pe_2012["IN.NSE.NIFTY_50.PE"].source == mb.SOURCE_ID
    # A fact released after the as-of date is physically absent.
    knowable = read_pit(date(2015, 11, 6), data_root=tmp_path)
    assert {f.release_date for f in knowable} == {date(2012, 10, 1), date(2015, 11, 6)}


def test_rerun_redoes_no_published_session(tmp_path: Path) -> None:
    plan = _plan(*FILES, MISSING)
    sync = _FakeSync()
    first = _runner(_transport(plan), tmp_path, sync).run(plan)
    assert first.published == 4 and first.not_published == 1 and first.requests == 5

    # The second run's transport answers nothing: any request would be an UnrecordedRequestError.
    second = _runner(RecordedTransport({}), tmp_path, sync).run(plan)
    assert second.requests == 0 and second.published == 0
    assert second.resumed == 4 and second.closed == 1  # the 404 is closed, never re-asked
    assert sync.get(mb.SOURCE_ID, MISSING).retryable is False  # type: ignore[union-attr]


def test_a_payload_already_in_l0_is_reparsed_not_refetched(tmp_path: Path) -> None:
    plan = _plan(date(2026, 9, 1))
    L0Store(clock=CLOCK, data_root=tmp_path).put(
        mb.SOURCE_ID, date(2026, 9, 1), plan[0].filename, FILES[date(2026, 9, 1)].read_bytes()
    )
    report = _runner(RecordedTransport({}), tmp_path, _FakeSync()).run(plan)
    assert report.published == 1 and report.requests == 0 and report.l0_reused == 1


def test_max_sessions_caps_attempts_and_the_rest_resume_later(tmp_path: Path) -> None:
    plan = _plan(*FILES)
    sync = _FakeSync()
    capped = _runner(_transport(plan), tmp_path, sync, max_sessions=2).run(plan)
    assert capped.published == 2 and capped.stopped_early
    rest = _runner(_transport(plan), tmp_path, sync).run(plan)
    assert rest.resumed == 2 and rest.published == 2 and rest.requests == 2


def test_a_stop_signal_ends_the_run_between_sessions(tmp_path: Path) -> None:
    plan = _plan(*FILES)
    calls = {"n": 0}

    def stop_after_one() -> str | None:
        calls["n"] += 1
        return "deadline" if calls["n"] > 1 else None

    report = _runner(_transport(plan), tmp_path, _FakeSync(), should_stop=stop_after_one).run(plan)
    assert report.published == 1 and report.stopped_early == "deadline"


def test_a_file_dated_to_another_session_is_refused(tmp_path: Path) -> None:
    plan = _plan(date(2015, 11, 6))
    wrong = RecordedResponse(body=FILES[date(2015, 11, 10)].read_bytes())
    sync = _FakeSync()
    report = _runner(_transport(plan, **{plan[0].url: wrong}), tmp_path, sync).run(plan)
    assert report.refused == 1 and report.published == 0
    assert not (tmp_path / "L1" / "macro_series").exists()


def test_403_spike_parks_with_enumerated_cause(tmp_path: Path) -> None:
    plan = _plan(*FILES, MISSING)
    forbidden = {unit.url: RecordedResponse(status_code=403, body=b"no") for unit in plan}
    sync = _FakeSync()
    report = _runner(RecordedTransport(forbidden), tmp_path, sync).run(plan)
    assert report.parked and report.park_reason is mb.ParkReason.FORBIDDEN_SPIKE
    assert "FORBIDDEN_SPIKE" in (report.park_detail or "")
    # The sessions after the park were never touched.
    assert sync.get(mb.SOURCE_ID, plan[-1].session) is None


def test_main_exits_non_zero_when_parked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def parked_run(*_a: Any, **_k: Any) -> int:
        return 3

    monkeypatch.setattr(mb, "_run_live", parked_run)
    assert mb.main(["--from", "2026-09-01", "--to", "2026-09-01"]) == 3
    assert mb.main(["--from", "2026-09-01", "--to", "2026-09-01", "--dry-run"]) == 0
    assert mb.main(["--from", "2011-01-03", "--to", "2026-09-01", "--dry-run"]) == 2


def test_coverage_is_per_session_and_unmapped_names_are_listed_separately(tmp_path: Path) -> None:
    plan = _plan(*FILES, MISSING)
    sync = _FakeSync()
    l0 = L0Store(clock=CLOCK, data_root=tmp_path)
    report = _runner(_transport(plan), tmp_path, sync).run(plan)
    table = load_index_aliases()
    coverage = mb.survey(plan, l0=l0, sync=sync, table=table)

    assert [line.session for line in coverage] == [unit.session for unit in plan]
    by_date = {line.session: line for line in coverage}
    assert by_date[MISSING].outcome is mb.Outcome.NOT_PUBLISHED
    assert by_date[date(2012, 10, 1)].indices == 30

    names = {u.name: u for u in mb.unmapped_names(coverage)}
    # A deliberately unmapped pre-2015 name, seen on both CNX-era files and never after.
    assert names["CNX Midcap"].last_seen == date(2015, 11, 6)
    assert "S&P CNX Nifty" not in names and "Nifty 50" not in names  # known to the table

    text = mb.render_report(
        from_date=date(2012, 10, 1), to_date=date(2026, 9, 1), report=report, coverage=coverage
    )
    assert "| CNX Midcap |" in text and "NOT_PUBLISHED" in text
    csv_path = tmp_path / "cov.csv"
    mb.write_coverage_csv(coverage, csv_path)
    assert len(csv_path.read_text().splitlines()) == len(plan) + 1


def test_a_name_published_twice_with_different_values_is_withheld_not_guessed() -> None:
    """The real 2013-02-08 file lists `CNX Alpha Index` twice; the second row is High Beta's."""
    from dataplatform.ingest.macro import parse_index_valuation

    header = FILES[date(2012, 10, 1)].read_text().splitlines()[0]
    body = "\n".join(
        [
            header,
            "CNX Low Volatility,08-02-2013,-,-,-,4563.87,-31.06,-0.68,1,1,19.87,3.48,1.46",
            "CNX Alpha Index,08-02-2013,-,-,-,4713.18,-36.65,-0.77,1,1,23.07,3.6,0.65",
            "CNX Alpha Index,08-02-2013,-,-,-,1572.9,-21.94,-1.38,1,1,22.39,0.96,1.18",
        ]
    ).encode()
    release = parse_index_valuation(body, filename="ind_close_all_08022013.csv")
    assert release.withheld == ("IN.NSE.CNX_ALPHA_INDEX",)
    assert {f.series_id.rsplit(".", 1)[0] for f in release.facts} == {"IN.NSE.CNX_LOW_VOLATILITY"}
