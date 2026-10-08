"""M15.4: the scheduler refuses to start while a migration is not applied.

Offline: `pending_migrations` is stood in for, so what is proved is the decision `main` makes on
its answer. That the answer itself is right against a real database is
`tests/integration/test_scheduler_migration_guard.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dataplatform.scheduler import __main__ as cli
from dataplatform.store.migrate import Migration, MigrationError

PENDING = Migration(
    version="0099",
    name="not_yet",
    path=Path("dataplatform/store/migrations/0099_not_yet.sql"),
    sql="SELECT 1;",
)


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records whether the scheduler loop (or a job) was reached instead of starting either."""
    reached: list[str] = []

    def forever(runner: object) -> int:
        reached.append("run")
        return 0

    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    monkeypatch.setattr(cli, "_run_forever", forever)
    monkeypatch.setattr(cli, "_page", lambda settings, reason: reached.append("paged"))
    return reached


@pytest.mark.parametrize("command", [["run"], ["run-once", "eod_pipeline"]])
def test_a_pending_migration_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
    started: list[str],
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    monkeypatch.setattr(cli, "pending_migrations", lambda settings: [PENDING])
    assert cli.main(command) == cli.EXIT_MIGRATIONS_PENDING != 0
    assert started == ["paged"]  # paged, and neither the loop nor the job was reached
    err = capsys.readouterr().err
    assert "0099_not_yet.sql" in err and "make migrate" in err


def test_a_fully_migrated_database_starts(
    monkeypatch: pytest.MonkeyPatch, started: list[str]
) -> None:
    """The inverse: were the guard inverted, a migrated database would be the one refused."""
    monkeypatch.setattr(cli, "pending_migrations", lambda settings: [])
    assert cli.main(["run"]) == 0
    assert started == ["run"]


def test_a_drifted_ledger_also_refuses(monkeypatch: pytest.MonkeyPatch, started: list[str]) -> None:
    def drifted(settings: object) -> list[Migration]:
        raise MigrationError("0003_scheduler.sql was applied with checksum abc… but now …")

    monkeypatch.setattr(cli, "pending_migrations", drifted)
    assert cli.main(["run"]) == cli.EXIT_MIGRATIONS_PENDING
    assert started == ["paged"]


def test_list_needs_no_database(monkeypatch: pytest.MonkeyPatch, started: list[str]) -> None:
    def unreachable(settings: object) -> list[Migration]:
        raise AssertionError("list must not consult the database")

    monkeypatch.setattr(cli, "pending_migrations", unreachable)
    assert cli.main(["list"]) == 0


def test_systemd_does_not_restart_into_the_same_refusal() -> None:
    unit = (Path(__file__).resolve().parents[2] / "ops/systemd/scheduler.service").read_text()
    assert f"RestartPreventExitStatus={cli.EXIT_MIGRATIONS_PENDING}" in unit
