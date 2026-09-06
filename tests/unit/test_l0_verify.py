"""The scheduled L0 integrity sweep (2026-09-06 audit, finding N6).

`L0Store.verify_checksums` could always find damage; nothing ever called it outside the test suite,
so the lake everything is re-derivable from went unswept. These tests pin the job around it, and
each is written so the plausible wrong implementation fails:

* a sweep that finds damage and reports success;
* a sweep that "repairs" L0 — the one thing `L0Store` refuses to do for any reason, including when
  the bytes are wrong (AGENTIC_CONTEXT §3.10), because the corrupt payload is the evidence;
* a WARN where an ERROR belongs, which would let the interlock keep trading on a lake whose raw
  layer no longer matches what was stored (invariant #10 counts only ERROR);
* a rolling window that never becomes a full pass, leaving the old tail — the part nothing reads,
  and therefore the part bit-rot survives in — permanently unswept.

Offline by construction: a `tmp_path` lake, a `FrozenClock`, a recording alerter and the same
`_FakeConn` shape the sentinel suite uses. No Postgres and no socket.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.store.l0 import L0Store
from dataplatform.store.l0_verify import (
    CHECK_NAME,
    DEFAULT_WINDOW_DAYS,
    run_l0_verify,
    sweep_window,
)

SOURCE: Final = "nse_bhavcopy_legacy"
SESSION: Final = date(2026, 8, 7)
NOW: Final = datetime(2026, 8, 20, 3, 0, tzinfo=IST)  # a day that is NOT in the first week


class _RecordingAlerter:
    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str]] = []

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        self.sent.append((severity, title))
        return AlertOutcome.SENT


class _FlagRecorder:
    """The slice of `Connection` `persist_findings` uses: an existence check and an insert."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: Any = ()) -> _FlagRecorder:
        if "SELECT 1 FROM quality_flag" in sql:
            self._last: list[tuple[int, ...]] = []
            return self
        if "INSERT INTO quality_flag" in sql:
            self.rows.append({"severity": params[2], "check_name": params[1], "detail": params[8]})
            self._last = []
            return self
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self) -> tuple[int, ...] | None:
        return None


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """An L0 tree with one real, checksummed payload in it."""
    store = L0Store(clock=FrozenClock(NOW), data_root=tmp_path)
    store.put(SOURCE, SESSION, "cm07AUG2026bhav.csv.zip", b"the bytes the exchange served")
    return tmp_path


def _run(lake: Path, *, clock: FrozenClock, alerter: _RecordingAlerter, conn: _FlagRecorder) -> Any:
    return run_l0_verify(
        conn=conn,  # type: ignore[arg-type]
        settings=Settings(data_root=lake),
        clock=clock,
        store=L0Store(clock=clock, data_root=lake),
        alerter=alerter,
    )


def test_an_intact_lake_sweeps_clean(lake: Path) -> None:
    alerter, conn = _RecordingAlerter(), _FlagRecorder()

    result = _run(lake, clock=FrozenClock(NOW), alerter=alerter, conn=conn)

    assert result.ok
    assert result.report.checked == 1
    assert conn.rows == []
    assert alerter.sent == []


def test_damage_is_found_flagged_at_error_and_alerted(lake: Path) -> None:
    """ERROR, not WARN. Invariant #10 counts open ERROR flags, so this stops trading — correctly.

    "The bytes every derived value came from are not the bytes we stored" is not a warning.
    """
    payload = next((lake / "L0" / SOURCE).rglob("*.zip"))
    payload.chmod(0o644)
    payload.write_bytes(b"something else entirely")
    alerter, conn = _RecordingAlerter(), _FlagRecorder()

    result = _run(lake, clock=FrozenClock(NOW), alerter=alerter, conn=conn)

    assert not result.ok
    assert len(result.report.defects) == 1
    assert [row["severity"] for row in conn.rows] == ["ERROR"]
    assert conn.rows[0]["check_name"] == CHECK_NAME
    assert [severity for severity, _ in alerter.sent] == [Severity.CRITICAL]


def test_the_sweep_never_touches_the_damaged_payload(lake: Path) -> None:
    """The corrupt bytes are the evidence. A sweep that repaired L0 would destroy it.

    `L0Store` refuses to modify or delete a stored payload for any reason (AGENTIC_CONTEXT §3.10);
    this asserts the job inherits that rather than quietly re-fetching over the problem.
    """
    payload = next((lake / "L0" / SOURCE).rglob("*.zip"))
    payload.chmod(0o644)
    payload.write_bytes(b"corrupt")
    before = payload.read_bytes()

    _run(lake, clock=FrozenClock(NOW), alerter=_RecordingAlerter(), conn=_FlagRecorder())

    assert payload.exists()
    assert payload.read_bytes() == before


# ── the window: rolling most weeks, everything once a month ──────────────────────────────────


def test_a_mid_month_run_sweeps_a_trailing_window() -> None:
    start, full = sweep_window(date(2026, 8, 20))

    assert full is False
    assert start == date(2026, 8, 20) - timedelta(days=DEFAULT_WINDOW_DAYS)


def test_the_first_week_of_a_month_sweeps_everything() -> None:
    """The pass that catches bit-rot in the tail — the years nothing has read since they landed."""
    start, full = sweep_window(date(2026, 9, 6))

    assert full is True
    assert start is None


def test_every_month_gets_a_full_pass_whichever_day_the_job_runs() -> None:
    """The job is weekly, so pinning the full pass to the 1st would skip most months.

    Any seven-day window contains exactly one of the job's Sundays, so a first-seven-days rule
    fires once a month whatever weekday the job is on.
    """
    for month in range(1, 13):
        sundays = [
            day
            for day in (date(2026, month, d) for d in range(1, 29))
            if day.weekday() == 6 and sweep_window(day)[1]
        ]
        assert sundays, f"month {month} would get no full pass"


def test_the_window_reaches_further_back_than_the_weekly_cadence() -> None:
    """A payload written just after one sweep must not fall between it and the next."""
    assert DEFAULT_WINDOW_DAYS > 7
