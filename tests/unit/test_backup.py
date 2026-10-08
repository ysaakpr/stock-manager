"""M15.4: retention, credentials kept out of argv and logs, the live-target refusal, the L0 step.

Offline: the client tools are a fake runner and the live database's counts are a stand-in, so what
is proved here is what this module hands to `pg_dump`/`rsync` and what it decides — the real dump
and restore are the gate note's drill (ops/gates/M15.4-backup-restore-2026-10-08.md).
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.store import backup
from dataplatform.store.backup import (
    BackupError,
    DsnParts,
    LiveTargetError,
    PgClient,
    check_restored,
    parse_dsn,
    run_l0_backup,
    run_postgres_backup,
    run_restore_drill,
    same_database,
    select_retention,
)

PASSWORD = "pw-Zx81-must-never-leak"
LIVE_URL = f"postgresql://trading:{PASSWORD}@localhost:5433/trading"
NOW = datetime(2026, 10, 8, 5, 30, tzinfo=IST)


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": LIVE_URL,
        "backup_root": tmp_path / "backups",
        "data_root": tmp_path / "data",
    }
    values.update(overrides)
    return Settings(**values)


class FakeRunner:
    """Records every argv and env; writes `payload` to a stdout file handle like pg_dump would."""

    def __init__(self, *, returncode: int = 0, stderr: bytes = b"", payload: bytes = b"PGDMP"):
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.returncode = returncode
        self.stderr = stderr
        self.payload = payload

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(argv), dict(kwargs.get("env") or {})))
        stdout = kwargs.get("stdout")
        if stdout is not None and hasattr(stdout, "write") and self.returncode == 0:
            stdout.write(self.payload)
        return subprocess.CompletedProcess(argv, self.returncode, b"", self.stderr)


@pytest.fixture
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backup,
        "_source_state",
        lambda live: ({"decision_journal": 5, "sync_state": 7}, ("0001", "0002"), "16.14"),
    )


# ── retention ─────────────────────────────────────────────────────────────────────────────────


def _nightly(days: int, start: datetime = NOW) -> list[datetime]:
    return [start - timedelta(days=offset) for offset in range(days)]


def test_retention_keeps_fourteen_dailies_and_eight_weeklies() -> None:
    stamps = _nightly(120)
    plan = select_retention(stamps, keep_daily=14, keep_weekly=8)
    daily = set(_nightly(14))
    assert daily <= set(plan.keep)
    weeks = {stamp.isocalendar()[:2] for stamp in plan.keep}
    # 14 dailies (Thu 8 Oct back to Fri 25 Sep) already hold the newest of ISO weeks 41-39, so the
    # 8 weeklies add the newest of weeks 38-34: five more, and eight distinct weeks in all.
    assert len(weeks) == 8 and len(plan.keep) == 14 + 5
    assert set(plan.keep) | set(plan.prune) == set(stamps)
    assert not set(plan.keep) & set(plan.prune)
    # Every weekly survivor beyond the dailies is the newest backup of its ISO week.
    for stamp in set(plan.keep) - daily:
        same_week = [s for s in stamps if s.isocalendar()[:2] == stamp.isocalendar()[:2]]
        assert stamp == max(same_week)


def test_retention_prunes_the_oldest_never_the_newest() -> None:
    """Fails if the selection is inverted: the newest must survive, the oldest must not."""
    stamps = _nightly(120)
    plan = select_retention(stamps, keep_daily=14, keep_weekly=8)
    assert max(stamps) in plan.keep
    assert min(stamps) in plan.prune
    assert plan.keep[0] == max(stamps)


def test_two_backups_on_one_day_keep_only_the_newer() -> None:
    late, early = NOW, NOW - timedelta(hours=3)
    plan = select_retention([early, late], keep_daily=14, keep_weekly=0)
    assert plan.keep == (late,) and plan.prune == (early,)


def test_retention_counts_days_that_have_a_backup_not_calendar_days() -> None:
    """A month of failed runs must not age every good backup out at once."""
    stamps = _nightly(20, start=NOW - timedelta(days=40))
    plan = select_retention(stamps, keep_daily=14, keep_weekly=0)
    assert len(plan.keep) == 14


def test_retention_refuses_a_policy_that_keeps_nothing() -> None:
    with pytest.raises(ValueError):
        select_retention([NOW], keep_daily=0, keep_weekly=8)


def test_the_job_prunes_only_its_own_dumps(tmp_path: Path, no_database: None) -> None:
    settings = _settings(tmp_path, backup_keep_daily=2, backup_keep_weekly=0)
    directory = tmp_path / "backups" / "postgres"
    directory.mkdir(parents=True)
    for offset in (3, 2, 1):
        stamp = (NOW - timedelta(days=offset)).strftime("%Y%m%dT%H%M%S")
        (directory / f"trading-{stamp}.dump").write_bytes(b"old")
        (directory / f"trading-{stamp}.json").write_text("{}")
    stranger = directory / "hand-made.dump"
    stranger.write_bytes(b"not ours")

    run_postgres_backup(settings, clock=FrozenClock(NOW), runner=FakeRunner())

    dumps = sorted(path.name for path in directory.glob("trading-*.dump"))
    assert dumps == [
        f"trading-{(NOW - timedelta(days=1)).strftime('%Y%m%dT%H%M%S')}.dump",
        f"trading-{NOW.strftime('%Y%m%dT%H%M%S')}.dump",
    ]
    assert stranger.exists()
    assert sorted(p.stem for p in directory.glob("trading-*.json")) == [Path(d).stem for d in dumps]


# ── credentials never in argv, log or sidecar ─────────────────────────────────────────────────


def test_the_dump_passes_the_password_by_environment_only(
    tmp_path: Path, no_database: None
) -> None:
    runner = FakeRunner()
    with capture_logs() as logs:
        record = run_postgres_backup(_settings(tmp_path), clock=FrozenClock(NOW), runner=runner)

    ((argv, env),) = runner.calls
    assert argv[:3] == ["docker", "run", "--rm"] and "pg_dump" in argv
    assert not any(PASSWORD in part for part in argv), argv
    assert not any("postgresql://" in part for part in argv), argv
    assert "PGPASSWORD" in argv and env["PGPASSWORD"] == PASSWORD
    assert env["PGHOST"] == "localhost" and env["PGPORT"] == "5433"
    assert PASSWORD not in json.dumps(logs, default=str)
    sidecar = record.path.with_suffix(".json").read_text()
    assert PASSWORD not in sidecar and json.loads(sidecar)["source"] == "localhost:5433/trading"
    assert record.path.read_bytes() == b"PGDMP"


def test_a_failed_dump_redacts_the_password_and_leaves_no_partial(
    tmp_path: Path, no_database: None
) -> None:
    runner = FakeRunner(returncode=1, stderr=f"auth failed for password {PASSWORD}".encode())
    with pytest.raises(BackupError) as caught:
        run_postgres_backup(_settings(tmp_path), clock=FrozenClock(NOW), runner=runner)
    assert PASSWORD not in str(caught.value) and "***" in str(caught.value)
    assert list((tmp_path / "backups" / "postgres").iterdir()) == []


def test_an_unparseable_dsn_is_not_echoed() -> None:
    with pytest.raises(BackupError) as caught:
        parse_dsn(SecretStr(f"host=x password={PASSWORD} ==="))
    assert PASSWORD not in str(caught.value)


def test_dsn_parts_never_show_the_password() -> None:
    parts = parse_dsn(LIVE_URL)
    assert PASSWORD not in repr(parts) and PASSWORD not in parts.redacted
    assert parts.password.get_secret_value() == PASSWORD


def test_the_client_names_variables_without_values() -> None:
    argv = PgClient("postgres:16").argv("pg_restore", "-d", "x")
    flags = [argv[i + 1] for i, part in enumerate(argv) if part == "-e"]
    assert flags == ["PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSWORD"]
    assert PgClient("").argv("pg_dump", "-Fc") == ["pg_dump", "-Fc"]


# ── the live-target refusal ───────────────────────────────────────────────────────────────────


def _parts(host: str, port: int, dbname: str) -> DsnParts:
    return DsnParts(host=host, port=port, user="u", dbname=dbname, password=SecretStr("p"))


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "LOCALHOST"])
def test_every_loopback_spelling_of_the_live_database_is_the_live_database(host: str) -> None:
    assert same_database(_parts(host, 5433, "trading"), _parts("localhost", 5433, "trading"))


@pytest.mark.parametrize(
    "other",
    [
        ("localhost", 5499, "trading"),
        ("localhost", 5433, "trading_drill"),
        ("db2", 5433, "trading"),
    ],
)
def test_a_different_port_database_or_host_is_not_live(other: tuple[str, int, str]) -> None:
    """The inverse of the refusal: were it inverted or blanket, these would read as live."""
    assert not same_database(_parts(*other), _parts("localhost", 5433, "trading"))


def _sealed_dump(tmp_path: Path) -> Path:
    directory = tmp_path / "backups" / "postgres"
    directory.mkdir(parents=True)
    dump = directory / "trading-20261008T053000.dump"
    dump.write_bytes(b"PGDMP")
    dump.with_suffix(".json").write_text(
        json.dumps({"sha256": backup._sha256(dump), "counts": {}, "migrations": []})
    )
    return dump


def test_the_drill_refuses_the_live_database_before_restoring_anything(tmp_path: Path) -> None:
    _sealed_dump(tmp_path)
    runner = FakeRunner()
    with pytest.raises(LiveTargetError):
        run_restore_drill(_settings(tmp_path), target=parse_dsn(LIVE_URL), runner=runner)
    assert runner.calls == []


def test_the_drill_refuses_a_configured_target_that_is_the_live_database(tmp_path: Path) -> None:
    _sealed_dump(tmp_path)
    runner = FakeRunner()
    live_again = LIVE_URL.replace("localhost", "127.0.0.1")
    settings = _settings(tmp_path, restore_drill_database_url=SecretStr(live_again))
    with pytest.raises(LiveTargetError):
        run_restore_drill(settings, runner=runner)
    assert runner.calls == []


def test_the_drill_refuses_a_dump_that_fails_its_checksum(tmp_path: Path) -> None:
    dump = _sealed_dump(tmp_path)
    dump.write_bytes(b"PGDMP-corrupted")
    with pytest.raises(BackupError, match="sha256"):
        run_restore_drill(_settings(tmp_path), target=_parts("localhost", 5499, "drill"))


# ── the drill's verdict ───────────────────────────────────────────────────────────────────────


def test_lost_rows_fail_the_drill_and_later_writes_do_not() -> None:
    recorded = {"decision_journal": 10, "sync_state": 50, "paper_session": 3}
    assert (
        check_restored(
            recorded=recorded,
            restored={"decision_journal": 10, "sync_state": 52, "paper_session": 3},
            recorded_migrations=["0001"],
            restored_migrations=["0001"],
        )
        == ()
    )
    problems = check_restored(
        recorded=recorded,
        restored={"decision_journal": 9, "sync_state": 50},
        recorded_migrations=["0001", "0002"],
        restored_migrations=["0001"],
    )
    assert len(problems) == 3
    assert any("decision_journal" in p for p in problems)
    assert any("paper_session" in p and "missing" in p for p in problems)
    assert any("migration ledger" in p for p in problems)


# ── L0 ────────────────────────────────────────────────────────────────────────────────────────


def _lake(tmp_path: Path, *names: str) -> None:
    for name in names:
        path = tmp_path / "data" / "L0" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)


def test_the_l0_manifest_hashes_only_new_files(tmp_path: Path) -> None:
    _lake(tmp_path, "nse/2026/a.csv", "nse/2026/a.csv.meta.json")
    settings = _settings(tmp_path)
    first = run_l0_backup(settings)
    assert (first.files, first.hashed, first.mirrored_to) == (2, 2, None)
    _lake(tmp_path, "nse/2026/b.csv")
    second = run_l0_backup(settings)
    assert (second.files, second.hashed) == (3, 1)
    lines = second.manifest.read_text().splitlines()
    assert [line.split("  ")[1] for line in lines] == [
        "L0/nse/2026/a.csv",
        "L0/nse/2026/a.csv.meta.json",
        "L0/nse/2026/b.csv",
    ]
    assert lines[2].split("  ")[0] == backup._sha256(tmp_path / "data" / "L0/nse/2026/b.csv")


def test_a_recorded_l0_file_that_vanished_fails_the_job(tmp_path: Path) -> None:
    _lake(tmp_path, "nse/a.csv", "nse/b.csv")
    settings = _settings(tmp_path)
    run_l0_backup(settings)
    (tmp_path / "data" / "L0" / "nse" / "b.csv").unlink()
    with pytest.raises(BackupError, match="immutable"):
        run_l0_backup(settings)


def test_no_lake_is_a_failure_not_an_empty_manifest(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="no lake"):
        run_l0_backup(_settings(tmp_path))


def test_no_mirror_means_no_rsync(tmp_path: Path) -> None:
    _lake(tmp_path, "nse/a.csv")
    runner = FakeRunner()
    with capture_logs() as logs:
        run_l0_backup(_settings(tmp_path), runner=runner)
    assert runner.calls == []
    assert any(event["event"] == "backup.l0_mirror_unconfigured" for event in logs)


def test_a_mirror_is_copied_onto_and_never_deleted_from(tmp_path: Path) -> None:
    _lake(tmp_path, "nse/a.csv")
    runner = FakeRunner()
    result = run_l0_backup(_settings(tmp_path, backup_l0_mirror="/mnt/second/lake"), runner=runner)
    ((argv, _),) = runner.calls
    assert argv[0] == "rsync" and "--ignore-existing" in argv
    assert not any(part.startswith("--delete") for part in argv)
    assert argv[-2:] == [f"{tmp_path / 'data' / 'L0'}/", "/mnt/second/lake/L0/"]
    assert result.mirrored_to == "/mnt/second/lake"


def test_a_failed_mirror_fails_the_job(tmp_path: Path) -> None:
    _lake(tmp_path, "nse/a.csv")
    with pytest.raises(BackupError, match="rsync"):
        run_l0_backup(
            _settings(tmp_path, backup_l0_mirror="/mnt/x"), runner=FakeRunner(returncode=23)
        )


# ── settings ──────────────────────────────────────────────────────────────────────────────────


def test_a_relative_backup_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="backup_root"):
        _settings(tmp_path, backup_root=Path("backups"))


def test_the_drill_target_is_a_secret(tmp_path: Path) -> None:
    drill_password = "drill-pw-Qq42"
    settings = _settings(
        tmp_path, restore_drill_database_url=f"postgresql://u:{drill_password}@h/d"
    )
    assert isinstance(settings.restore_drill_database_url, SecretStr)
    assert drill_password not in repr(settings)
