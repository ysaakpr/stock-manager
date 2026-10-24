"""Capture-only sources: FETCHED is their success on /status/sources — and nowhere else.

`nifty_index_press_releases` lands each release in L0 and stops at FETCHED; nothing on its path
publishes an L1 dataset. `/status/sources` called it NO_SUCCESS with every row "stuck". The
register now says so (`final_state: FETCHED`) and the source status honours it. Proved both ways:
a source that publishes keeps calling FETCHED stuck, and the interlock still requires PUBLISHED.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Final

from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest.source_register import capture_only_sources, load
from dataplatform.status.sync_state import (
    SourceStatus,
    SyncRecord,
    SyncState,
    SyncStateStore,
    evaluate_green,
)

PRESS: Final = "nifty_index_press_releases"
DAY: Final = date(2026, 10, 12)
AT: Final = datetime(2026, 10, 12, 9, 5, tzinfo=IST)


def _status(final_state: SyncState) -> SourceStatus:
    return SourceStatus(
        source=PRESS,
        last_success_date=DAY,
        last_success_at=AT,
        latest_date=DAY,
        lag_days=0,
        lag_sessions=0,
        failure_streak=0,
        last_failure_date=None,
        last_error=None,
        last_failure_retryable=None,
        counts={SyncState.FETCHED: 343},
        final_state=final_state,
    )


def test_the_register_names_the_press_releases_capture_only() -> None:
    assert capture_only_sources() == frozenset({PRESS})
    by_id = {source.id: source for source in load().sources}
    assert by_id[PRESS].final_state == "FETCHED"
    assert by_id["nse_bhavcopy_udiff"].final_state == "PUBLISHED"


def test_fetched_rows_are_success_only_where_fetched_is_final() -> None:
    assert _status(SyncState.FETCHED).healthy
    assert not _status(SyncState.PUBLISHED).healthy  # the same rows on a publishing source: stuck


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Conn:
    """`_SOURCE_STATUS_SQL`'s rows: 343 FETCHED, never PUBLISHED, newest FETCHED on DAY."""

    def execute(self, _sql: str, _params: object = None) -> _Rows:
        def row(source: str) -> tuple[Any, ...]:
            return (
                source, None, None, DAY, 0, 343, 0, 0, 0, 0, 0, 0, None, None, None, DAY, AT,
            )  # fmt: skip

        return _Rows([row(PRESS), row("nse_fii_dii_flows")])


def test_the_store_dates_a_capture_only_success_from_its_newest_fetched_row() -> None:
    store = SyncStateStore(
        _Conn(),  # type: ignore[arg-type]
        clock=FrozenClock(AT),
        declined={},
        capture_only={PRESS},
    )
    lines = {line.source: line for line in store.source_statuses()}
    assert lines[PRESS].last_success_date == DAY and lines[PRESS].last_success_at == AT
    assert lines[PRESS].final_state is SyncState.FETCHED and lines[PRESS].healthy
    other = lines["nse_fii_dii_flows"]
    assert other.last_success_date is None and not other.healthy


def test_the_interlock_still_requires_published() -> None:
    fetched = SyncRecord(
        source=PRESS,
        logical_date=DAY,
        state=SyncState.FETCHED,
        attempts=1,
        retryable=True,
        updated_at=AT,
    )
    green = evaluate_green(DAY, (PRESS,), {PRESS: fetched}, day_kind=None, open_error_flags=0)
    assert not green.green
