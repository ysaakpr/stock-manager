"""M15.4: the scheduler refuses to start while a migration is pending or an applied one was edited,
and starts — with a warning page — on a database ahead of its checkout.

Offline: `schema_status` is stood in for, so what is proved is the decision `main` makes on its
answer. That the answer itself is right against a real database is
`tests/integration/test_scheduler_migration_guard.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from dataplatform.alerts import Severity
from dataplatform.config import Settings
from dataplatform.scheduler import __main__ as cli
from dataplatform.store.migrate import Migration, MigrationDriftError, SchemaStatus

PENDING = Migration(
    version="0099",
    name="not_yet",
    path=Path("dataplatform/store/migrations/0099_not_yet.sql"),
    sql="SELECT 1;",
)
CURRENT = SchemaStatus(pending=(), ahead=())


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records the pages sent and whether the scheduler loop was reached."""
    reached: list[str] = []

    def forever(runner: object) -> int:
        reached.append("run")
        return 0

    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    monkeypatch.setattr(cli, "_run_forever", forever)
    monkeypatch.setattr(
        cli, "_page", lambda settings, verdict: reached.append(f"paged:{verdict.severity}")
    )
    return reached


def _answer(answer: SchemaStatus | Exception) -> Callable[[Settings], SchemaStatus]:
    def status(settings: Settings) -> SchemaStatus:
        if isinstance(answer, Exception):
            raise answer
        return answer

    return status


def _route(
    monkeypatch: pytest.MonkeyPatch,
    answer: SchemaStatus | Exception,
    *,
    sleep: Callable[[float], None] = lambda s: None,
    backoff: tuple[float, ...] = (),
) -> None:
    """Point `main`'s schema check at a fixed answer, with no real sleeping."""
    real = cli.check_schema
    monkeypatch.setattr(
        cli,
        "check_schema",
        lambda settings: real(settings, status=_answer(answer), sleep=sleep, backoff=backoff),
    )


@pytest.mark.parametrize("command", [["run"], ["run-once", "eod_pipeline"]])
def test_a_pending_migration_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
    started: list[str],
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    _route(monkeypatch, SchemaStatus(pending=(PENDING,), ahead=()))
    assert cli.main(command) == cli.EXIT_MIGRATIONS_PENDING != 0
    assert started == ["paged:critical"]  # paged, and neither the loop nor the job was reached
    err = capsys.readouterr().err
    assert "0099_not_yet.sql" in err and "make migrate" in err


def test_a_fully_migrated_database_starts(
    monkeypatch: pytest.MonkeyPatch, started: list[str]
) -> None:
    """The inverse: were the guard inverted, a migrated database would be the one refused."""
    _route(monkeypatch, CURRENT)
    assert cli.main(["run"]) == 0
    assert started == ["run"]


def test_an_edited_applied_migration_refuses_with_its_own_remedy(
    monkeypatch: pytest.MonkeyPatch, started: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _route(monkeypatch, MigrationDriftError("0003_scheduler.sql was applied with checksum abc…"))
    assert cli.main(["run"]) == cli.EXIT_MIGRATIONS_PENDING
    assert started == ["paged:critical"]
    err = capsys.readouterr().err
    assert "new numbered migration" in err and "Run `make migrate`" not in err


def test_a_database_ahead_of_the_checkout_warns_and_starts(
    monkeypatch: pytest.MonkeyPatch, started: list[str]
) -> None:
    """Migrations are additive: an older checkout's tables are all there, so stopping the whole
    scheduler (with a remedy, `make migrate`, that cannot fix it) is the wrong failure."""
    _route(monkeypatch, SchemaStatus(pending=(), ahead=("0015",)))
    assert cli.main(["run"]) == 0
    assert started == ["paged:warning", "run"]


def test_ahead_and_pending_at_once_refuses() -> None:
    answer = SchemaStatus(pending=(PENDING,), ahead=("0015",))
    verdict = cli.check_schema(Settings(), status=_answer(answer), backoff=())
    assert verdict is not None and verdict.refuse and "diverged" in verdict.title


def test_each_verdict_has_a_distinct_title_and_dedup_key() -> None:
    answers: list[SchemaStatus | Exception] = [
        SchemaStatus(pending=(PENDING,), ahead=()),
        SchemaStatus(pending=(), ahead=("0015",)),
        SchemaStatus(pending=(PENDING,), ahead=("0015",)),
        MigrationDriftError("edited"),
    ]
    verdicts = [cli.check_schema(Settings(), status=_answer(a), backoff=()) for a in answers]
    assert all(v is not None for v in verdicts)
    titles = {v.title for v in verdicts if v is not None}
    keys = {v.dedup_key for v in verdicts if v is not None}
    assert len(titles) == len(keys) == 4
    assert [v.severity for v in verdicts if v is not None].count(Severity.WARNING) == 1


def test_a_transient_outage_is_retried_then_checked() -> None:
    calls: list[int] = []
    waits: list[float] = []

    def flaky(settings: Settings) -> SchemaStatus:
        calls.append(1)
        if len(calls) < 3:
            raise psycopg.OperationalError("connection refused")
        return CURRENT

    verdict = cli.check_schema(Settings(), status=flaky, sleep=waits.append, backoff=(5, 10, 20))
    assert verdict is None and waits == [5, 10]


def test_an_outage_that_outlasts_the_backoff_exits_one(
    monkeypatch: pytest.MonkeyPatch, started: list[str]
) -> None:
    waits: list[float] = []
    _route(
        monkeypatch,
        psycopg.OperationalError("connection refused"),
        sleep=waits.append,
        backoff=(1, 2),
    )
    assert cli.main(["run"]) == cli.EXIT_DATABASE_UNREACHABLE == 1
    assert waits == [1, 2] and started == []


def test_the_default_backoff_is_bounded_near_five_minutes() -> None:
    assert 240 <= sum(cli._CONNECT_BACKOFF) <= 330


def test_list_needs_no_database(monkeypatch: pytest.MonkeyPatch, started: list[str]) -> None:
    _route(monkeypatch, AssertionError("list must not consult the database"))
    assert cli.main(["list"]) == 0


def test_systemd_does_not_restart_into_the_same_refusal() -> None:
    unit = (Path(__file__).resolve().parents[2] / "ops/systemd/scheduler.service").read_text()
    assert f"RestartPreventExitStatus={cli.EXIT_MIGRATIONS_PENDING}" in unit
    assert cli.EXIT_DATABASE_UNREACHABLE != cli.EXIT_MIGRATIONS_PENDING
