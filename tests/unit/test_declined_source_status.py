"""A source DECLINED on policy grounds (D12, via D19) is never red — and nothing else is hidden.

`screener_company_fundamentals` is declined in the Source Register: never scheduled, never fetched.
So it can never be PUBLISHED, and counting it would hold the trading interlock red forever or put a
permanent red line on `/status/sources` that operators learn to ignore. These tests pin both
halves of the rule:

* a declined source is set aside from the green verdict (invariant #10) and reported `declined`,
  never failing or overdue, on `/status/sources` and `/status/jobs`;
* **the exclusion is not inverted** — a non-declined source that failed, is missing or is stale
  still turns the date red and the source unhealthy, with or without a declined source beside it.

Offline: the store runs against a stub connection that answers the per-source aggregate query.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from analyst.monitor.interlock import CORE_DATASETS, StatusApiGate
from dataplatform.clock import IST, Clock, FrozenClock
from dataplatform.ingest.calendar import DayKind
from dataplatform.ingest.source_register import Declined, load
from dataplatform.status.api import app, clock_source, sync_store
from dataplatform.status.models import SourceStatusOut, SyncStatusOut
from dataplatform.status.sync_state import (
    SourceStatus,
    SyncRecord,
    SyncState,
    SyncStateStore,
    evaluate_green,
)

SCREENER: Final = "screener_company_fundamentals"
SESSION: Final = date(2026, 8, 7)
AT: Final = datetime(2026, 8, 7, 18, 30, tzinfo=IST)
NOW: Final = datetime(2026, 8, 10, 20, 0, tzinfo=IST)


@pytest.fixture(scope="module")
def declined() -> dict[str, Declined]:
    """The checked-in register's declined set — the real decision, not a test stand-in."""
    found = load().declined()
    assert SCREENER in found
    return found


def _published(source: str) -> SyncRecord:
    return SyncRecord(source=source, logical_date=SESSION, state=SyncState.PUBLISHED, updated_at=AT)


def _failed(source: str) -> SyncRecord:
    return SyncRecord(
        source=source,
        logical_date=SESSION,
        state=SyncState.FAILED,
        updated_at=AT,
        last_error="HTTP 503",
    )


# ── the interlock: evaluate_green ─────────────────────────────────────────────────────────────


def test_a_declined_dataset_does_not_make_the_date_red(declined: dict[str, Declined]) -> None:
    status = evaluate_green(
        SESSION,
        ["nse_eod", SCREENER],
        {"nse_eod": _published("nse_eod")},
        day_kind=DayKind.SESSION,
        declined=declined.keys(),
    )
    assert status.green, status.reason
    assert status.declined == (SCREENER,)
    assert status.datasets == ("nse_eod",)
    assert status.missing == ()


def test_a_failing_non_declined_dataset_is_still_red_beside_a_declined_one(
    declined: dict[str, Declined],
) -> None:
    """Inverted check: the decline must not hide a real failure next to it."""
    status = evaluate_green(
        SESSION,
        ["nse_eod", "bse_eod", SCREENER],
        {"nse_eod": _published("nse_eod"), "bse_eod": _failed("bse_eod")},
        day_kind=DayKind.SESSION,
        declined=declined.keys(),
    )
    assert not status.green
    assert status.not_published == (("bse_eod", SyncState.FAILED),)


def test_a_missing_non_declined_dataset_is_still_red(declined: dict[str, Declined]) -> None:
    status = evaluate_green(
        SESSION, ["nse_eod", SCREENER], {}, day_kind=DayKind.SESSION, declined=declined.keys()
    )
    assert not status.green
    assert status.missing == ("nse_eod",)


def test_without_the_decline_the_same_source_would_be_red() -> None:
    """The exemption comes from the declined set and nothing else: drop it and the source counts."""
    status = evaluate_green(
        SESSION, ["nse_eod", SCREENER], {"nse_eod": _published("nse_eod")}, day_kind=DayKind.SESSION
    )
    assert not status.green
    assert status.missing == (SCREENER,)


def test_asking_only_about_declined_datasets_is_the_vacuous_green_bug(
    declined: dict[str, Declined],
) -> None:
    with pytest.raises(ValueError, match="DECLINED"):
        evaluate_green(SESSION, [SCREENER], {}, day_kind=DayKind.SESSION, declined=declined.keys())


def test_asking_about_a_declined_dataset_is_logged_as_a_warning(
    declined: dict[str, Declined],
) -> None:
    with capture_logs() as entries:
        evaluate_green(
            SESSION,
            ["nse_eod", SCREENER],
            {"nse_eod": _published("nse_eod")},
            day_kind=DayKind.SESSION,
            declined=declined.keys(),
        )
    warnings = [e for e in entries if e["event"] == "sync_state.declined_dataset_requested"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["declined"] == [SCREENER]


def test_no_warning_when_nothing_declined_is_requested(declined: dict[str, Declined]) -> None:
    with capture_logs() as entries:
        evaluate_green(
            SESSION,
            ["nse_eod"],
            {"nse_eod": _published("nse_eod")},
            day_kind=DayKind.SESSION,
            declined=declined.keys(),
        )
    assert not [e for e in entries if e["event"] == "sync_state.declined_dataset_requested"]


def test_the_trading_interlock_depends_on_no_declined_source(
    declined: dict[str, Declined],
) -> None:
    """The decline exemption must never be what lets the real interlock go green."""
    assert not set(CORE_DATASETS) & declined.keys()
    assert not set(StatusApiGate().datasets) & declined.keys()


def test_the_green_payload_names_what_it_set_aside(declined: dict[str, Declined]) -> None:
    status = evaluate_green(
        SESSION,
        ["nse_eod", SCREENER],
        {"nse_eod": _published("nse_eod")},
        day_kind=DayKind.SESSION,
        declined=declined.keys(),
    )
    out = SyncStatusOut.of(SESSION, (), day_kind="SESSION", expects_data=True, green=status)
    assert out.declined == [SCREENER]
    assert out.green is True


# ── /status/sources: a declined line is never red ────────────────────────────────────────────


def _line(source: str, *, streak: int, record: Declined | None, budget: int | None) -> SourceStatus:
    return SourceStatus(
        source=source,
        last_success_date=None,
        last_success_at=None,
        latest_date=SESSION,
        lag_days=None,
        lag_sessions=None,
        failure_streak=streak,
        last_failure_date=SESSION,
        last_error="HTTP 404",
        last_failure_retryable=False,
        counts={SyncState.FAILED: streak},
        max_lag_sessions=budget,
        declined=record,
    )


def test_a_declined_source_with_old_failures_is_neither_failing_nor_overdue(
    declined: dict[str, Declined],
) -> None:
    line = _line(SCREENER, streak=3, record=declined[SCREENER], budget=1)
    assert line.healthy
    assert not line.overdue
    out = SourceStatusOut.of(line)
    assert out.healthy and not out.overdue
    assert out.declined is not None and "D19" in out.declined.decision
    assert out.failure_streak == 3  # history is still reported truthfully


def test_the_same_line_without_the_decline_is_red() -> None:
    """Inverted check: identical facts, no decline record → failing and overdue."""
    line = _line(SCREENER, streak=3, record=None, budget=1)
    assert not line.healthy
    assert line.overdue


# ── the store and the API, end to end over a stub connection ─────────────────────────────────


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _StubConnection:
    """Answers `_SOURCE_STATUS_SQL` with fixed rows: one failing live source, one declined one."""

    def execute(self, _sql: str, _params: object = None) -> _Result:
        def row(source: str) -> tuple[Any, ...]:
            # source, last_success_date, last_success_at, latest_date, pending, fetched, validated,
            # normalized, published, failed, gap, streak, last_failure date/error/retryable
            return (source, None, None, SESSION, 0, 0, 0, 0, 0, 2, 0, 2, SESSION, "boom", False)

        return _Result([row("bse_eod"), row(SCREENER)])


def _store(declined: dict[str, Declined] | None = None) -> SyncStateStore:
    return SyncStateStore(_StubConnection(), clock=FrozenClock(NOW), declined=declined)  # type: ignore[arg-type]


def test_the_store_marks_the_declined_line_and_leaves_the_failing_one_red() -> None:
    lines = {line.source: line for line in _store().source_statuses({"bse_eod": 1})}
    assert lines[SCREENER].declined is not None
    assert lines[SCREENER].healthy
    assert lines["bse_eod"].declined is None
    assert not lines["bse_eod"].healthy


def test_an_injected_empty_declined_set_reports_screener_as_failing() -> None:
    """Inverted check at the store: the exemption is the register's decision, not the source id."""
    lines = {line.source: line for line in _store({}).source_statuses()}
    assert not lines[SCREENER].healthy


@pytest.fixture
def client() -> Iterator[TestClient]:
    def frozen() -> Clock:
        return FrozenClock(NOW)

    app.dependency_overrides[sync_store] = _store
    app.dependency_overrides[clock_source] = frozen
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_status_sources_reports_declined_as_its_own_state(client: TestClient) -> None:
    body = client.get("/status/sources").json()
    lines = {line["source"]: line for line in body["sources"]}
    assert lines[SCREENER]["healthy"] is True
    assert lines[SCREENER]["overdue"] is False
    assert lines[SCREENER]["declined"]["robots_rule"] == "/user/*"
    assert lines["bse_eod"]["healthy"] is False
    assert lines["bse_eod"]["declined"] is None
    assert set(body["declined"]) == {SCREENER}
    assert "D12" in body["declined"][SCREENER]["decision"]
    assert body["declined"][SCREENER]["reason"].strip()
