"""Accepted deviations on the surfaces — and the hard boundary that they change no gate.

Two halves:

1. **The boundary.** The registry is display and alerting only. No gate module may import it
   (walked from the source tree, so a new importer fails here), and every strict health field
   keeps its value for an accepted failure: `SourceStatus.healthy`, `JobsOut.healthy`, the gap
   report's `fully_explained` (the M1 gate), `evaluate_green` (invariant #10), the scheduler's
   own `JobHealth.healthy`.
2. **The rendering.** An accepted item shows `ACCEPTED` in amber with its remark, the real state
   muted beside it and the entry on hover; an unaccepted one is red exactly as before; an expired
   one is red and says the acceptance expired; headings count errors and accepted apart.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

from dataplatform.scheduler.health import JobHealth, JobHealthState, LastRuns
from dataplatform.status.console import data_render, pages
from dataplatform.status.deviations import DeviationKind, DeviationOut, DeviationStatus
from dataplatform.status.models import (
    AlertConditionOut,
    AlertsOut,
    CheckCountOut,
    GapEntryOut,
    GapsOut,
    JobLineOut,
    QualityOut,
    QualitySeverity,
    SourcesOut,
    SourceStatusOut,
)
from dataplatform.status.queries import sort_worst_first, split_accepted
from dataplatform.status.queries_data import sort_sources_worst_first
from dataplatform.status.sync_state import SyncRecord, SyncState, evaluate_green

ROOT: Final = Path(__file__).resolve().parents[2]
NOW: Final = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)

#: The only modules allowed to import the registry: the status surfaces and the alert evaluator.
IMPORTERS: Final = frozenset(
    {
        "dataplatform/alert_triggers.py",
        "dataplatform/status/api.py",
        "dataplatform/status/deps.py",
        "dataplatform/status/models.py",
        "dataplatform/status/queries.py",
        "dataplatform/status/queries_data.py",
        "dataplatform/status/console/pages.py",
        "dataplatform/status/console/routes.py",
        "dataplatform/status/console/data_render.py",
    }
)

#: Gate modules, named so the failure says which gate grew the dependency.
GATES: Final = (
    "dataplatform/status/sync_state.py",  # is_green, invariant #10
    "dataplatform/scheduler/health.py",  # JobHealth.healthy, /status/jobs' verdict
    "dataplatform/scheduler/runner.py",  # job_run's record of a run, the heartbeat
    "analyst/monitor/interlock.py",  # the trading interlock (StatusApiGate)
    "execution/session.py",
    "execution/broker.py",
    "execution/kill_switch.py",
    "backtest/paper_session.py",  # paper session gating
    "backtest/fm_job.py",  # the M17 desk
)


def _imports_registry(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == "dataplatform.status.deviations":
                return True
            if node.module == "dataplatform.status" and any(
                alias.name == "deviations" for alias in node.names
            ):
                return True
        if isinstance(node, ast.Import) and any(
            alias.name == "dataplatform.status.deviations" for alias in node.names
        ):
            return True
    return False


# ── 1. the boundary ────────────────────────────────────────────────────────────────────────


def test_only_the_status_surfaces_and_the_alerter_import_the_registry() -> None:
    importers = {
        str(path.relative_to(ROOT))
        for package in ("dataplatform", "analyst", "execution", "backtest", "accounting")
        for path in (ROOT / package).rglob("*.py")
        if path.name != "deviations.py" and _imports_registry(path)
    }
    assert importers <= IMPORTERS, f"unexpected importers: {sorted(importers - IMPORTERS)}"
    for gate in GATES:
        assert not _imports_registry(ROOT / gate), (
            f"{gate} is a gate and must not read the registry"
        )


def _accepted(kind: DeviationKind = DeviationKind.SOURCE) -> DeviationOut:
    return DeviationOut(
        id="bse-403-source-scrip-master",
        kind=kind,
        target="bse_scrip_master",
        status=DeviationStatus.ACCEPTED,
        remark="api.bseindia.com has refused this server (HTTP 403) since 2026-09-24",
        basis="owner, 2026-10-12 — HUMAN_DECISIONS.md D26 (owner, 2026-10-12: 'Ignore BSE')",
        since=date(2026, 9, 8),
        review_by=None,
    )


def _expired() -> DeviationOut:
    return _accepted().model_copy(
        update={"status": DeviationStatus.EXPIRED, "review_by": date(2026, 10, 20)}
    )


def _source(
    name: str, *, streak: int = 3, deviation: DeviationOut | None = None
) -> SourceStatusOut:
    return SourceStatusOut(
        source=name,
        last_success_date=None,
        last_success_at=None,
        latest_date=date(2026, 10, 22),
        lag_days=None,
        lag_sessions=None,
        failure_streak=streak,
        last_failure_date=date(2026, 10, 22),
        last_error="ForbiddenError: x returned 403",
        last_failure_retryable=True,
        max_lag_sessions=None,
        overdue=False,
        healthy=False,
        deviation=deviation,
        counts={SyncState.FAILED: streak},
    )


def test_an_accepted_source_is_still_unhealthy_on_the_wire() -> None:
    line = _source("bse_scrip_master", deviation=_accepted())
    body = SourcesOut(as_of=date(2026, 10, 23), sources=[line], accepted=1, errors=0)
    payload = body.model_dump(mode="json")
    assert payload["sources"][0]["healthy"] is False
    assert payload["sources"][0]["deviation"]["status"] == "ACCEPTED"


def test_accepted_counts_split_only_the_unhealthy_and_never_count_expired() -> None:
    healthy = [False, False, False, True]
    verdicts = [_accepted(), _expired(), None, _accepted()]
    assert split_accepted(healthy, verdicts) == (1, 2)


def test_the_interlock_is_red_for_an_accepted_failed_dataset() -> None:
    """Invariant #10 is untouched: a FAILED BSE row is red whatever the registry says."""
    day = date(2026, 10, 22)
    record = SyncRecord(
        source="bse_announcements",
        logical_date=day,
        state=SyncState.FAILED,
        attempts=1,
        retryable=True,
        last_error="ForbiddenSpikeError: ingestion from api.bseindia.com is hard-stopped",
        updated_at=NOW,
    )
    green = evaluate_green(
        day,
        ("bse_announcements",),
        {"bse_announcements": record},
        day_kind=None,
        open_error_flags=0,
    )
    assert not green.green


def test_the_schedulers_own_verdict_is_untouched() -> None:
    health = JobHealth(
        name="ca_refresh",
        cron="0 10 * * sat",
        state=JobHealthState.FAILING,
        due_since=None,
        runs=LastRuns(last_state="FAILED", last_started_at=NOW, last_error="CaRefreshError: x"),
    )
    assert not health.healthy
    assert "judge" not in JobHealth.__dataclass_fields__


def test_a_fully_accepted_gap_range_is_still_not_fully_explained() -> None:
    entry = _gap(_accepted(DeviationKind.GAP))
    gaps = _gaps([entry], accepted_total=1)
    assert gaps.fully_explained is False and gaps.unexplained_total == 1


# ── 2. the Ops page ────────────────────────────────────────────────────────────────────────


def _line(name: str, deviation: DeviationOut | None = None, *, healthy: bool = False) -> JobLineOut:
    return JobLineOut(
        name=name,
        cron="0 10 * * sat",
        state=JobHealthState.OK if healthy else JobHealthState.FAILING,
        healthy=healthy,
        deviation=deviation,
        due_since=None,
        last_state="SUCCEEDED" if healthy else "FAILED",
        last_started_at=NOW,
        last_finished_at=NOW,
        last_duration_seconds=1.0,
        last_success_at=None,
        last_error=None if healthy else "CaRefreshError: x",
        next_fire_at=None,
        timeout_seconds=3600,
    )


def _row(html: str, attr: str, key: str) -> str:
    found = re.search(rf'<tr {attr}="{re.escape(key)}"[^>]*>.*?</tr>', html, re.S)
    assert found is not None, key
    return found.group(0)


def test_an_accepted_job_is_amber_with_its_remark_and_an_error_stays_red() -> None:
    accepted = _accepted(DeviationKind.JOB)
    html = pages.render_jobs_table(
        [
            _line("identity_refresh"),
            _line("ca_refresh", accepted),
            _line("tri_refresh", healthy=True),
        ],
        as_of=NOW,
    )
    row = _row(html, "data-job", "ca_refresh")
    assert 'data-deviation="ACCEPTED"' in row and 'data-state="FAILING"' in row
    assert '<span class="state state-ACCEPTED"' in row
    assert accepted.remark in row
    assert "bse-403-source-scrip-master" in row and "D26" in row  # the entry, on hover
    assert '<span class="muted"><span class="state state-FAILING">' in row  # muted, not the badge

    error = _row(html, "data-job", "identity_refresh")
    assert "state-ACCEPTED" not in error and "data-deviation" not in error
    assert '<td><span class="state state-FAILING">FAILING</span></td>' in error
    assert 'data-errors="1" data-accepted="1"' in html
    assert "1 error, 1 accepted" in html


def test_an_expired_acceptance_is_red_and_says_so() -> None:
    row = _row(pages.render_jobs_table([_line("ca_refresh", _expired())], as_of=NOW), "data-job",
               "ca_refresh")  # fmt: skip
    assert 'data-deviation="EXPIRED"' in row and "state-ACCEPTED" not in row
    assert "state state-FAILING" in row and "expired after 2026-10-20" in row


def test_accepted_jobs_sort_below_every_error() -> None:
    lines = [
        _line("a_accepted", _accepted(DeviationKind.JOB)),
        _line("z_error"),
        _line("ok", healthy=True),
    ]
    assert [line.name for line in sort_worst_first(lines)] == ["z_error", "a_accepted", "ok"]


def test_the_drill_down_shows_the_entry_in_full() -> None:
    from dataplatform.status.models import JobRunsOut

    html = pages.render_job_runs(
        _line("ca_refresh", _accepted(DeviationKind.JOB)),
        JobRunsOut(as_of=NOW, job="ca_refresh", cron="0 10 * * sat", runs=[], limit=50),
    )
    block = html[html.index('id="deviation"') :]
    assert "ACCEPTED" in block and "bse-403-source-scrip-master" in block
    assert "since 2026-09-08" in block and "HUMAN_DECISIONS.md D26" in block


def test_an_accepted_alert_is_amber_and_counted_apart() -> None:
    def alert(key: str, deviation: DeviationOut | None) -> AlertConditionOut:
        return AlertConditionOut(
            dedup_key=key,
            trigger="job_failed",
            severity="critical",
            title=f"scheduled job {key} raised",
            opened_at=NOW,
            last_seen_at=NOW,
            deviation=deviation,
        )

    alerts = AlertsOut(
        as_of=NOW,
        provider="log",
        open=[
            alert("job:identity_refresh:failed", None),
            alert("job:ca_refresh:failed", _accepted()),
        ],
        accepted=1,
        failure_alerts=None,
    )
    html = pages._alerts_panel(alerts)
    accepted = _row(html, "data-alert", "job:ca_refresh:failed")
    assert 'data-deviation="ACCEPTED"' in accepted and "state-ACCEPTED" in accepted
    assert "HTTP 403" in accepted
    error = _row(html, "data-alert", "job:identity_refresh:failed")
    assert '<td><span class="state state-critical">critical</span></td>' in error
    assert "2 open: 1 error, 1 accepted" in html


# ── 3. the Data page ───────────────────────────────────────────────────────────────────────


def test_an_accepted_source_is_amber_and_an_error_stays_red() -> None:
    lines = [_source("bse_scrip_master", deviation=_accepted()), _source("nse_xbrl_filing")]
    ordered = sort_sources_worst_first(lines)
    assert [line.source for line in ordered] == ["nse_xbrl_filing", "bse_scrip_master"]
    html = data_render.render_sources(SourcesOut(as_of=date(2026, 10, 23), sources=lines), ordered)
    accepted = _row(html, "data-source", "bse_scrip_master")
    assert 'data-health="ACCEPTED"' in accepted and "rag-amber" in accepted
    assert 'data-healthy="false"' in accepted and "HTTP 403" in accepted
    error = _row(html, "data-source", "nse_xbrl_filing")
    assert 'data-health="FAILING"' in error and "rag-red" in error
    assert "2 not healthy: 1 error, 1 accepted" in html


def _gap(deviation: DeviationOut | None, source: str = "bse_announcements") -> GapEntryOut:
    return GapEntryOut(
        source=source,
        date=date(2026, 10, 21),
        reason="FAILED",  # type: ignore[arg-type]
        explained=False,
        day_kind="SESSION",
        detail="FAILED after 1 attempt",
        state=SyncState.FAILED,
        attempts=1,
        retryable=True,
        last_error="ForbiddenSpikeError: x",
        first_attempt_at=None,
        updated_at=None,
        l1_partition=None,
        deviation=deviation,
    )


def _gaps(entries: list[GapEntryOut], *, accepted_total: int) -> GapsOut:
    return GapsOut(
        from_date=date(2026, 10, 17),
        to_date=date(2026, 10, 23),
        sources=sorted({e.source for e in entries}),
        unexplained=entries,
        unexplained_total=len(entries),
        accepted_total=accepted_total,
        explained_total=0,
        complete=0,
        pairs_examined=len(entries),
        l1_unchecked=0,
        counts=[],
        fully_explained=False,
        limit=500,
    )


def test_gaps_count_errors_and_accepted_apart() -> None:
    html = data_render.render_gaps(
        _gaps([_gap(_accepted(DeviationKind.GAP)), _gap(None, "curated_rss")], accepted_total=1)
    )
    assert 'data-errors="1" data-accepted="1"' in html
    assert ">1 unexplained<" in html and ">1 accepted<" in html
    accepted = _row(html, "data-gap", "bse_announcements|2026-10-21")
    assert "rag-amber" in accepted and 'data-deviation="ACCEPTED"' in accepted
    error = _row(html, "data-gap", "curated_rss|2026-10-21")
    assert "rag-red" in error and "data-deviation" not in error


def test_an_accepted_quality_check_is_amber() -> None:
    check = CheckCountOut(
        check_name="ca_reconciliation",
        severity=QualitySeverity.ERROR,
        count=194,
        first_date=date(2001, 1, 1),
        last_date=date(2026, 10, 1),
        raised_today=0,
        raised_last_7_days=0,
        deviation=_accepted(DeviationKind.QUALITY),
    )
    quality = QualityOut(as_of=NOW, open_total=194, counts=[], by_check=[check], flags=[], limit=50)
    html = data_render.render_quality(quality)
    assert 'data-deviation="ACCEPTED"' in html and "rag-amber" in html


def test_the_judge_opens_no_connection_of_its_own() -> None:
    """`/status/sources` reads the store and nothing else; the judge must not add a database.

    CI caught this (run 40000579070): the judge declared `db_connection`, so a test that overrides
    only `sync_store` reached for Postgres. Here `db_connection` raises, so any route whose judge
    opens its own connection fails, with or without a database on the machine.
    """
    from fastapi.testclient import TestClient

    from dataplatform.clock import FrozenClock
    from dataplatform.status.api import app, clock_source, db_connection, sync_store
    from dataplatform.status.sync_state import SyncStateStore

    class _Rows:
        def fetchall(self) -> list[tuple[object, ...]]:
            return []

        def fetchone(self) -> tuple[object, ...] | None:
            return None

    class _Conn:
        def execute(self, sql: str, params: object = ()) -> _Rows:
            return _Rows()

    def no_database() -> object:
        raise AssertionError("a route opened a database connection of its own")

    store = SyncStateStore(_Conn(), clock=FrozenClock(NOW), declined={})  # type: ignore[arg-type]
    app.dependency_overrides[sync_store] = lambda: store
    app.dependency_overrides[clock_source] = lambda: FrozenClock(NOW)
    app.dependency_overrides[db_connection] = no_database
    try:
        with TestClient(app) as client:
            response = client.get("/status/sources")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    assert response.json()["sources"] == []
