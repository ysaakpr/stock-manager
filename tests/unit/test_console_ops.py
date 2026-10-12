"""M18.1: the console shell and its Operations page — offline, over one in-memory fixture.

One fixture (a stub connection holding `job_run` and `alert_condition` rows, a heartbeat read, a
`BACKUP_ROOT` under tmp_path and a frozen clock) is served to the real app, so the JSON
`/status/jobs` and the HTML `/console` are asked about the *same* rows through the *same*
dependency overrides. The acceptance criteria, one block each:

1. `/console` renders the health banner, every registered job with its state, and the backup panel;
2. a job `/status/jobs` calls FAILING or OVERDUE is shown as such on the page (parity);
3. a credential planted in `job_run.error` (and in an alert title and a heartbeat error) never
   appears in any console response;
4. every console route — enumerated from the router — answers 405 to POST/PUT/PATCH/DELETE;
5. no page references an external URL, and the vendored htmx matches its pinned sha256;
6. every rendered timestamp is labelled IST, and the instant shown is the injected clock's.

The real SQL is exercised against Postgres in `tests/integration/test_console_ops.py`.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.scheduler import Heartbeat
from dataplatform.scheduler.registry import default_registry
from dataplatform.status import console
from dataplatform.status.api import app, clock_source, db_connection, heartbeat_source
from dataplatform.status.api import settings_source as api_settings_source
from dataplatform.status.console import READ_METHODS, router
from dataplatform.status.console.pages import HTMX_FILE, HTMX_PATH
from dataplatform.status.queries import HeartbeatRead

#: The vendored htmx 1.9.12 `dist/htmx.min.js`, byte-identical from unpkg and jsDelivr on
#: 2026-10-12; its sha384 is the SRI value htmx publishes for 1.9.12. Changing the file means
#: changing this pin, deliberately.
HTMX_SHA256: Final = "449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452"

NOW: Final = datetime(2026, 10, 12, 20, 0, tzinfo=IST)  # a Monday
TODAY_0530: Final = NOW.replace(hour=5, minute=30)

#: Every planted secret carries this marker, so "no secret leaked" is one substring check.
PLANT: Final = "PLANTEDzz"
#: A test fake, assembled from fragments so the secret scanner does not read it as a DSN.
_USERINFO: Final = ":".join(("trading", f"{PLANT}pw1"))
PLANTED_ERROR: Final = (
    f"OperationalError: connection to postgresql://{_USERINFO}@db:5432/trading failed; "
    f"retried with password={PLANT}pw2 and token: {PLANT}tok3, header Bearer {PLANT}bearer4abcdef"
)


# ── the fixture ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Run:
    job_name: str
    state: str
    started_at: datetime
    finished_at: datetime | None
    error: str | None = None
    instance: str = "box:101"
    run_id: str = ""


def _runs() -> list[Run]:
    runs = [
        # FAILING: the newest attempt failed, quoting a DSN, a password, a token and a bearer.
        Run("eod_pipeline", "FAILED", NOW.replace(hour=18, minute=30),
            NOW.replace(hour=18, minute=41), PLANTED_ERROR),
        # OVERDUE: weekdays at 19:15; the last success was Friday's.
        Run("daily_snapshot", "SUCCEEDED", NOW - timedelta(days=3, minutes=44),
            NOW - timedelta(days=3, minutes=40)),
        # OK: every 15 min; the 19:45 fire succeeded in 3 s.
        Run("failure_alerts", "SUCCEEDED", NOW.replace(hour=19, minute=45),
            NOW.replace(hour=19, minute=45, second=3)),
        Run("postgres_backup", "SUCCEEDED", TODAY_0530, TODAY_0530 + timedelta(seconds=75)),
        # RUNNING: an attempt in flight, today's due fire already covered.
        Run("l0_backup", "SUCCEEDED", NOW.replace(hour=5, minute=45),
            NOW.replace(hour=5, minute=50)),
        Run("l0_backup", "RUNNING", NOW - timedelta(minutes=2), None),
    ]  # fmt: skip
    # 55 older eod_pipeline attempts, so the drill-down's 50-row cap is observable; the oldest
    # carry the planted secret too, so it is not only the newest row that is masked.
    for day in range(1, 56):
        start = NOW.replace(hour=18, minute=30) - timedelta(days=day)
        failed = day > 50
        runs.append(
            Run("eod_pipeline", "FAILED" if failed else "SUCCEEDED", start,
                start + timedelta(minutes=9), PLANTED_ERROR if failed else None)
        )  # fmt: skip
    return [Run(**{**r.__dict__, "run_id": str(uuid.UUID(int=i + 1))}) for i, r in enumerate(runs)]


ALERTS: Final = [
    # dedup_key, trigger, severity, title, opened_at, last_seen_at
    ("job:eod_pipeline:failed", "job_failed", "critical",
     f"eod_pipeline FAILED: password={PLANT}pw5", NOW - timedelta(hours=1), NOW),
    ("calendar:nse_holidays:coverage_expiry", "calendar_expiry", "warning",
     "holiday calendar ends 2026-12-31", NOW - timedelta(days=2), NOW),
]  # fmt: skip


class _Result:
    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class StubConnection:
    """Answers the four queries the jobs and Ops reads make, from in-memory rows.

    Matched on what each query selects; anything else fails the test loudly, so a new query on the
    page cannot be silently answered with nothing.
    """

    def __init__(self, runs: Sequence[Run], alerts: Sequence[tuple[Any, ...]]) -> None:
        self.runs = list(runs)
        self.alerts = list(alerts)

    def _newest(self) -> dict[str, Run]:
        newest: dict[str, Run] = {}
        for run in self.runs:
            if run.job_name not in newest or run.started_at > newest[run.job_name].started_at:
                newest[run.job_name] = run
        return newest

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Result:
        text = " ".join(sql.split())
        if "max(started_at) AS last_success_at" in text:  # scheduler.health._LAST_RUNS_SQL
            rows = []
            for name, run in sorted(self._newest().items()):
                ok = [
                    r.started_at for r in self.runs if r.job_name == name and r.state == "SUCCEEDED"
                ]
                rows.append((name, run.state, run.started_at, run.error, max(ok, default=None)))
            return _Result(rows)
        if text.startswith("SELECT DISTINCT ON (job_name) job_name, started_at, finished_at"):
            return _Result([(n, r.started_at, r.finished_at) for n, r in self._newest().items()])
        if "FROM job_run WHERE job_name = %s" in text:
            name, limit = params
            mine = sorted(
                (r for r in self.runs if r.job_name == name), key=lambda r: r.started_at,
                reverse=True,
            )[:limit]  # fmt: skip
            return _Result(
                [
                    (r.run_id, r.state, r.instance, r.started_at, r.finished_at, r.error)
                    for r in mine
                ]
            )
        if "FROM alert_condition WHERE resolved_at IS NULL" in text:
            rank = {"critical": 0, "warning": 1, "info": 2}
            return _Result(sorted(self.alerts, key=lambda a: (rank[a[2]], a[4], a[0])))
        raise AssertionError(f"unexpected query against the stub connection: {text}")


@pytest.fixture
def backup_root(tmp_path: Path) -> Path:
    """A BACKUP_ROOT with two finished dumps (sidecars) and an L0 manifest of three entries."""
    root = tmp_path / "backups"
    (root / "postgres").mkdir(parents=True)
    for stamp, size in (("20261011T053000", 900), ("20261012T053000", 2048)):
        dump = root / "postgres" / f"trading-{stamp}.dump"
        dump.write_bytes(b"\0" * size)
        dump.with_suffix(".json").write_text('{"sha256": "' + "ab" * 32 + '"}', encoding="utf-8")
    (root / "l0").mkdir()
    (root / "l0" / "MANIFEST.sha256").write_text(
        "".join(f"{'cd' * 32}  L0/x/{i}.csv\n" for i in range(3)), encoding="utf-8"
    )
    return root


@pytest.fixture
def settings(tmp_path: Path, backup_root: Path) -> Settings:
    return Settings(
        backup_root=backup_root,
        data_root=tmp_path / "lake",
        scheduler_heartbeat_stale_after_seconds=300,
    )


@pytest.fixture
def heartbeat() -> HeartbeatRead:
    return HeartbeatRead(
        reachable=True, beat=Heartbeat(name="main", beat_at=NOW - timedelta(seconds=40))
    )


def _client(
    settings: Settings, conn: StubConnection, beat: HeartbeatRead, now: datetime
) -> TestClient:
    app.dependency_overrides[api_settings_source] = lambda: settings
    app.dependency_overrides[clock_source] = lambda: FrozenClock(now)
    app.dependency_overrides[db_connection] = lambda: conn
    app.dependency_overrides[heartbeat_source] = lambda: beat
    return TestClient(app)


@pytest.fixture
def conn() -> StubConnection:
    return StubConnection(_runs(), ALERTS)


@pytest.fixture
def client(
    settings: Settings, conn: StubConnection, heartbeat: HeartbeatRead
) -> Iterator[TestClient]:
    with _client(settings, conn, heartbeat, NOW) as live:
        yield live
    app.dependency_overrides.clear()


def _job_states(html: str) -> dict[str, str]:
    return dict(re.findall(r'<tr data-job="([^"]+)" data-state="([^"]+)"', html))


def _console_paths() -> list[str]:
    """Every console route's path, with a real registered job for `{name}`."""
    paths = []
    for route in router.routes:
        assert isinstance(route, APIRoute), route
        paths.append(route.path.replace("{name}", "eod_pipeline"))
    return paths


# ── 1. the page renders health, every job, and the backups ─────────────────────────────────


def test_ops_renders_health_every_registered_job_and_the_backup_panel(client: TestClient) -> None:
    for path in ("/console", "/console/ops"):
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/html")
        html = response.text

        assert 'id="health" data-status="OK"' in html
        assert "newest beat 2026-10-12 19:59:20 IST (40.0 s ago, stale after 300 s)" in html

        names = {job.name for job in default_registry()}
        states = _job_states(html)
        assert set(states) == names, "every registered job, and only those, has a row"
        assert states["l0_backup"] == "RUNNING"
        assert states["failure_alerts"] == "OK"
        assert html.count('hx-trigger="every 30s"') == 1

        assert 'id="backups"' in html
        assert "trading-20261012T053000.dump" in html  # the newest of the two
        assert "trading-20261011T053000.dump" not in html
        assert "2.0 KiB" in html
        assert "MANIFEST.sha256" in html and "3 entries" in html
        assert "no restore-drill result is recorded" in html

        assert 'id="alerts"' in html and "2 open" in html
        assert html.index("job:eod_pipeline:failed") < html.index("calendar:nse_holidays")


def test_jobs_are_sorted_failing_then_overdue_first(client: TestClient) -> None:
    states = list(_job_states(client.get("/console").text).values())
    rank = ["FAILING", "OVERDUE", "NEVER_RAN", "RUNNING", "OK"]
    assert states == sorted(states, key=rank.index)
    assert states[0] == "FAILING"


def test_the_htmx_fragment_is_the_same_table(client: TestClient) -> None:
    fragment = client.get("/console/ops/jobs")
    assert fragment.status_code == 200
    assert fragment.text.startswith('<div id="jobs"')
    assert "<html" not in fragment.text
    assert _job_states(fragment.text) == _job_states(client.get("/console").text)


def test_drill_down_lists_the_newest_fifty_runs_with_duration(client: TestClient) -> None:
    response = client.get("/console/ops/jobs/eod_pipeline")
    assert response.status_code == 200
    html = response.text
    assert html.count("<tr data-run=") == 50
    assert "2026-10-12 18:30:00 IST" in html  # the newest attempt is first
    assert "11 min" in html  # its duration, 18:30 → 18:41


def test_drill_down_for_an_unregistered_job_is_404(client: TestClient) -> None:
    response = client.get("/console/ops/jobs/no_such_job")
    assert response.status_code == 404
    assert "no registered job named" in response.text


def test_a_stale_heartbeat_turns_the_banner_red(settings: Settings, conn: StubConnection) -> None:
    stale = HeartbeatRead(
        reachable=True, beat=Heartbeat(name="main", beat_at=NOW - timedelta(hours=2))
    )
    with _client(settings, conn, stale, NOW) as live:
        html = live.get("/console").text
        assert live.get("/health").status_code == 503
    app.dependency_overrides.clear()
    assert 'class="banner bad" id="health" data-status="DEGRADED"' in html
    assert "state-STALE" in html


def test_an_invisible_backup_root_is_said_not_reported_as_no_backups(
    tmp_path: Path, conn: StubConnection, heartbeat: HeartbeatRead
) -> None:
    settings = Settings(backup_root=tmp_path / "not-mounted", data_root=tmp_path)
    with _client(settings, conn, heartbeat, NOW) as live:
        html = live.get("/console").text
    app.dependency_overrides.clear()
    assert "not visible to this process" in html


# ── 2. parity with /status/jobs ────────────────────────────────────────────────────────────


def test_every_job_state_on_the_page_matches_status_jobs(client: TestClient) -> None:
    payload = client.get("/status/jobs").json()
    json_states = {job["name"]: job["state"] for job in payload["jobs"]}
    assert json_states["eod_pipeline"] == "FAILING"
    assert json_states["daily_snapshot"] == "OVERDUE"
    page = _job_states(client.get("/console").text)
    assert page == json_states
    for state in ("FAILING", "OVERDUE"):
        flagged = {name for name, s in json_states.items() if s == state}
        assert flagged and all(page[name] == state for name in flagged)


# ── 3. a planted credential never renders ──────────────────────────────────────────────────


def test_a_planted_credential_never_appears_in_any_console_response(
    settings: Settings, conn: StubConnection
) -> None:
    broken = HeartbeatRead(reachable=False, error=f"could not connect: password={PLANT}pw6")
    for beat in (HeartbeatRead(reachable=True, beat=Heartbeat(name="m", beat_at=NOW)), broken):
        with _client(settings, conn, beat, NOW) as live:
            for path in _console_paths():
                response = live.get(path)
                assert response.status_code == 200, path
                assert PLANT not in response.text, f"{path} leaked a planted credential"
            assert PLANT not in live.get("/status/jobs").text
        app.dependency_overrides.clear()
    # And the masked text is still there to diagnose from, not dropped wholesale.
    with _client(settings, conn, broken, NOW) as live:
        html = live.get("/console/ops/jobs/eod_pipeline").text
    app.dependency_overrides.clear()
    assert "OperationalError: connection to postgresql://***@db:5432/trading failed" in html


# ── 4. read-only: every route refuses a write method ───────────────────────────────────────


def test_every_console_route_is_read_only() -> None:
    assert len(router.routes) >= 8  # /console, /ops, the fragment, drill-down, static, 3 pages
    for route in router.routes:
        assert isinstance(route, APIRoute)
        assert route.path.startswith("/console")
        assert route.methods is not None
        assert set(route.methods) <= set(READ_METHODS), f"{route.path} serves {route.methods}"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_console_route_answers_405_to_a_write(client: TestClient, method: str) -> None:
    for path in _console_paths():
        assert client.request(method, path).status_code == 405, f"{method} {path}"


def test_head_is_answered(client: TestClient) -> None:
    assert client.head("/console").status_code == 200


# ── 5. no external URL; the vendored htmx is the pinned one ────────────────────────────────

_URL_ATTR: Final = re.compile(r'\b(?:src|href|hx-get|hx-post|action|integrity)="([^"]*)"')


def test_no_page_references_an_external_url(client: TestClient) -> None:
    for path in _console_paths():
        response = client.get(path)
        if not response.headers["content-type"].startswith("text/html"):
            continue
        html = response.text
        assert "http://" not in html and "https://" not in html, path
        assert "url(" not in html, path
        for value in _URL_ATTR.findall(html):
            if value.startswith("sha384-"):
                continue
            assert value.startswith("/") and not value.startswith("//"), (path, value)


def test_the_vendored_htmx_matches_its_pin_and_is_what_the_page_loads(client: TestClient) -> None:
    assert hashlib.sha256(HTMX_PATH.read_bytes()).hexdigest() == HTMX_SHA256
    served = client.get(f"/console/static/{HTMX_FILE}")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("text/javascript")
    assert hashlib.sha256(served.content).hexdigest() == HTMX_SHA256
    assert b'version:"1.9.12"' in served.content
    html = client.get("/console").text
    assert f'<script src="/console/static/{HTMX_FILE}" integrity="sha384-' in html
    assert "unpkg" not in html and "jsdelivr" not in html


# ── 6. IST everywhere, from the injected clock ─────────────────────────────────────────────

_STAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?P<tail>.{0,4})")


def test_every_rendered_timestamp_is_labelled_ist(client: TestClient) -> None:
    seen = 0
    for path in _console_paths():
        response = client.get(path)
        if not response.headers["content-type"].startswith("text/html"):
            continue
        for match in _STAMP.finditer(response.text):
            seen += 1
            assert match.group(0).endswith(" IST"), (path, match.group(0))
    assert seen > 50


def test_utc_rows_are_rendered_in_ist(settings: Settings, heartbeat: HeartbeatRead) -> None:
    """A row stored in UTC (as Postgres hands back timestamptz to a UTC session) shows as IST."""
    utc_start = datetime(2026, 10, 12, 13, 0, tzinfo=UTC)  # 18:30 IST
    conn = StubConnection(
        [Run("eod_pipeline", "FAILED", utc_start, utc_start + timedelta(minutes=1), "x",
             run_id=str(uuid.UUID(int=9)))],
        [],
    )  # fmt: skip
    with _client(settings, conn, heartbeat, NOW) as live:
        html = live.get("/console/ops/jobs/eod_pipeline").text
    app.dependency_overrides.clear()
    assert "2026-10-12 18:30:00 IST" in html
    assert "13:00:00" not in html


def test_the_page_answers_at_the_injected_instant(
    settings: Settings, conn: StubConnection, heartbeat: HeartbeatRead
) -> None:
    later = NOW + timedelta(days=1, hours=1)
    with _client(settings, conn, heartbeat, later) as live:
        html = live.get("/console").text
    app.dependency_overrides.clear()
    assert "as of 2026-10-13 21:00:00 IST" in html
    with _client(settings, conn, heartbeat, NOW) as live:
        assert "as of 2026-10-12 20:00:00 IST" in live.get("/console").text
    app.dependency_overrides.clear()


def test_the_console_never_reads_the_wall_clock() -> None:
    package = Path(console.__file__).parent
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for forbidden in ("datetime.now(", "datetime.utcnow(", "time.time(", "date.today("):
            assert forbidden not in text, f"{source.name} calls {forbidden}"
