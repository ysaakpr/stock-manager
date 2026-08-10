"""M0.7 acceptance: a backup is complete and checksummed, and a restore is actually verified.

Everything here drives `ops/backup.sh` and `ops/restore.sh` as an operator does — as scripts, from
the repo root — because that is the artefact the M0 gate accepts, and a Python reimplementation of
what they do would test the reimplementation.

The source database is a scratch one created and migrated per session, never the developer's
`trading` database: the counts asserted below are only meaningful against rows this file put
there. The lake the manifest is taken over is a synthetic `DATA_ROOT` under `tmp_path`, so nothing
here can write into the real L0 — which is immutable and, once written, not something a test is
allowed to clean up (AGENTIC_CONTEXT §3.10).

Needs the docker postgres (`make up`); skips loudly if it is unreachable.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from pathlib import Path

import psycopg
import pytest
from pydantic import SecretStr

from dataplatform.clock import IST
from dataplatform.config import Settings
from dataplatform.store.db import connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Dumped by the tests below. Dropped and recreated per session, so its counts are exactly ours.
#: The pid suffix is not decoration: this repo is built by several agents at once against one
#: Postgres, and `DROP DATABASE … WITH (FORCE)` from a second run of this module would otherwise
#: terminate the first one's pg_restore mid-flight. Unique per process, so runs cannot collide.
SOURCE_DB = f"trading_m0_7_source_{os.getpid()}"

#: Where the restore lands. Named, so a run that died leaves an obvious thing to drop.
SCRATCH_DB = f"trading_m0_7_restore_{os.getpid()}"

#: Rows this file seeds, per table. Chosen to be distinct so a restore that mixed two tables up
#: could not pass, and non-zero so "counts match" cannot pass by everything being empty.
SEEDED = {"security_master": 3, "case_": 2, "decision_journal": 5}

#: Files written into the synthetic lake: two payloads and their sidecars, in the real L0 layout.
LAKE_FILES = {
    "L0/nse_bhavcopy/2026/08/cm07AUG2026bhav.csv": b"SYMBOL,SERIES,CLOSE\nRELIANCE,EQ,1500.25\n",
    "L0/nse_bhavcopy/2026/08/cm07AUG2026bhav.csv.meta.json": b'{"source": "nse_bhavcopy"}\n',
    "L0/bse_bhavcopy/2026/08/EQ_ISINCODE_070826.zip": b"PK\x03\x04not-really-a-zip",
    "L0/bse_bhavcopy/2026/08/EQ_ISINCODE_070826.zip.meta.json": b'{"source": "bse_bhavcopy"}\n',
}

#: Files a backup directory must contain. `SHA256SUMS` covers the other four.
BACKUP_FILES = ("postgres.dump", "row_counts.tsv", "l0_manifest.sha256", "backup.json")

SEEDED_AT = datetime(2026, 8, 8, 18, 30, tzinfo=IST)


def _settings_for(dbname: str) -> Settings:
    """Settings for the configured server with a different database selected."""
    dsn = with_dbname(Settings().database_url.get_secret_value(), dbname)
    return Settings(database_url=SecretStr(dsn))


def _run(
    script: str, *args: str, env: Mapping[str, str], expect_ok: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run one ops script from the repo root and return the finished process.

    Output is captured and, on an unexpected outcome, put in the assertion message: a drill that
    fails in CI has to say why without anyone re-running it by hand.
    """
    done = subprocess.run(
        ["bash", f"ops/{script}", *args],
        cwd=REPO_ROOT,
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if expect_ok:
        assert done.returncode == 0, f"ops/{script} failed:\n{done.stdout}\n{done.stderr}"
    else:
        assert done.returncode != 0, f"ops/{script} should have failed:\n{done.stdout}"
    return done


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reseal(backup: Path) -> None:
    """Rewrite SHA256SUMS to match the current contents of a (tampered) backup copy.

    The tamper tests are about the *count* check and the *L0* check, so they hand the restore a
    backup that passes its own checksums — otherwise the first gate would fire and prove nothing
    about the second.
    """
    lines = [f"{_sha256(backup / name)}  {name}\n" for name in BACKUP_FILES]
    (backup / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")


@pytest.fixture(scope="session")
def docker_postgres() -> None:
    """Skip the module unless the compose postgres is up and docker is usable."""
    if shutil.which("docker") is None:  # pragma: no cover - environment, not logic
        pytest.skip("docker is not on PATH")
    try:
        connect(_settings_for("postgres"), autocommit=True).close()
    except psycopg.OperationalError as error:  # pragma: no cover - environment, not logic
        pytest.skip(f"postgres is not reachable — run `make up` first: {error}")


@pytest.fixture(scope="session")
def source_db(docker_postgres: None) -> Iterator[str]:
    """A migrated database holding exactly `SEEDED` rows, dropped at the end of the session."""
    admin = _settings_for("postgres")
    conn = connect(admin, autocommit=True)
    try:
        for dbname in (SOURCE_DB, SCRATCH_DB):
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SOURCE_DB}"')
    finally:
        conn.close()

    settings = _settings_for(SOURCE_DB)
    migrate(settings)
    with connection(settings) as live:
        for index in range(SEEDED["security_master"]):
            live.execute(
                "INSERT INTO security_master (isin, name, primary_exchange, status, "
                "first_seen_date, created_at, updated_at) VALUES (%s, %s, 'NSE', 'ACTIVE', "
                "%s, %s, %s)",
                (f"INE00000000{index}", f"Fixture {index}", date(2026, 8, 7), SEEDED_AT, SEEDED_AT),
            )
        for index in range(SEEDED["case_"]):
            live.execute(
                "INSERT INTO case_ (case_id, title, state, created_at, updated_at) "
                "VALUES (%s, %s, 'DRAFT', %s, %s)",
                (f"CASE_M0_7_{index}", f"M0.7 fixture {index}", SEEDED_AT, SEEDED_AT),
            )
        for index in range(SEEDED["decision_journal"]):
            live.execute(
                "INSERT INTO decision_journal (ts, trading_date, actor, decision, rationale, "
                "recorded_at) VALUES (%s, %s, 'T0', 'HEARTBEAT', %s, %s)",
                (SEEDED_AT, date(2026, 8, 7), f"nothing happened {index}", SEEDED_AT),
            )
        live.commit()

    yield SOURCE_DB

    conn = connect(admin, autocommit=True)
    try:
        for dbname in (SOURCE_DB, SCRATCH_DB):
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture(scope="session")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A synthetic DATA_ROOT with a populated L0, standing in for the real lake."""
    root = tmp_path_factory.mktemp("lake")
    for relative, payload in LAKE_FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return root


@pytest.fixture(scope="session")
def script_env(lake: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Environment for the ops scripts: the synthetic lake, and backups outside the repo."""
    return {
        **os.environ,
        "DATA_ROOT": str(lake),
        "BACKUP_ROOT": str(tmp_path_factory.mktemp("backups")),
    }


@pytest.fixture(scope="session")
def backup(
    source_db: str, script_env: dict[str, str], tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """One backup of the seeded database, made once and reused by every assertion below."""
    dest = tmp_path_factory.mktemp("backup") / "run"
    _run("backup.sh", "--db", source_db, "--output", str(dest), env=script_env)
    return dest


@pytest.fixture
def tampered(backup: Path, tmp_path: Path) -> Path:
    """A writable copy of the backup, for the tests that break one thing in it."""
    copy = tmp_path / "tampered"
    shutil.copytree(backup, copy)
    return copy


# ── acceptance 1: backup.sh produces a dump + checksummed manifest ───────────────────────────


def test_backup_writes_every_file_and_its_checksums(backup: Path) -> None:
    for name in (*BACKUP_FILES, "SHA256SUMS"):
        assert (backup / name).is_file(), f"{name} is missing from the backup"
    assert (backup / "postgres.dump").stat().st_size > 0

    recorded = {
        name: digest
        for digest, name in (
            line.split("  ", 1)
            for line in (backup / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        )
    }
    assert set(recorded) == set(BACKUP_FILES), "SHA256SUMS must cover every file in the backup"
    for name, digest in recorded.items():
        assert _sha256(backup / name) == digest, f"{name} does not match its recorded checksum"


def test_backup_is_a_custom_format_archive(backup: Path) -> None:
    """`PGDMP` is pg_dump's custom-format magic; a plain-SQL dump would not restore selectively."""
    assert (backup / "postgres.dump").read_bytes()[:5] == b"PGDMP"


def test_row_counts_record_what_was_seeded(backup: Path) -> None:
    counts = {
        name: int(value)
        for name, value in (
            line.split() for line in (backup / "row_counts.tsv").read_text().splitlines()
        )
    }
    assert counts["schema_migrations"] > 0
    for table, expected in SEEDED.items():
        assert counts[table] == expected, f"{table} recorded {counts[table]}, seeded {expected}"


def test_l0_manifest_fingerprints_every_file_in_the_lake(backup: Path, lake: Path) -> None:
    manifest = {
        relative: digest
        for digest, relative in (
            line.split("  ", 1) for line in (backup / "l0_manifest.sha256").read_text().splitlines()
        )
    }
    assert set(manifest) == set(LAKE_FILES), "the manifest must cover payloads and sidecars alike"
    for relative, digest in manifest.items():
        assert digest == _sha256(lake / relative)


def test_backup_refuses_to_overwrite_an_existing_directory(
    source_db: str, backup: Path, script_env: dict[str, str]
) -> None:
    """A backup that can be half-overwritten is not evidence of anything."""
    done = _run(
        "backup.sh", "--db", source_db, "--output", str(backup), env=script_env, expect_ok=False
    )
    assert "never overwritten" in done.stderr


# ── acceptance 2: restore.sh restores into a scratch DB and verifies counts match ────────────


def test_restore_drill_passes_and_reports_the_counts(
    backup: Path, script_env: dict[str, str]
) -> None:
    done = _run(
        "restore.sh", "--scratch", "--backup", str(backup), "--db", SCRATCH_DB, env=script_env
    )
    assert "every count matches row_counts.tsv" in done.stdout
    assert "restore drill passed" in done.stdout
    assert "re-hashed and unchanged" in done.stdout


def test_restore_leaves_no_scratch_database_behind(
    backup: Path, script_env: dict[str, str]
) -> None:
    """The drill is a drill: it proves the dump restores and then tidies up after itself."""
    _run("restore.sh", "--scratch", "--backup", str(backup), "--db", SCRATCH_DB, env=script_env)
    with connection(_settings_for("postgres"), autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_database WHERE datname = %s", (SCRATCH_DB,)
        ).fetchone()
    assert row is not None and row[0] == 0


def test_restore_finds_the_newest_backup_when_none_is_named(
    source_db: str, script_env: dict[str, str]
) -> None:
    """The nightly path takes no arguments, so the default discovery is part of the contract."""
    for label in ("20260101T000000", "20260808T235959"):
        _run("backup.sh", "--db", source_db, env={**script_env, "BACKUP_TS": label})
    done = _run("restore.sh", "--scratch", "--db", SCRATCH_DB, env=script_env)
    assert "backup=" in done.stdout and "20260808T235959" in done.stdout


def test_a_wrong_row_count_fails_the_drill(tampered: Path, script_env: dict[str, str]) -> None:
    """The count check is the whole point of the drill — it has to be able to fail."""
    counts = tampered / "row_counts.tsv"
    counts.write_text(
        counts.read_text().replace(f"case_ {SEEDED['case_']}", f"case_ {SEEDED['case_'] + 1}"),
        encoding="utf-8",
    )
    _reseal(tampered)
    done = _run(
        "restore.sh",
        "--scratch",
        "--backup",
        str(tampered),
        "--db",
        SCRATCH_DB,
        env=script_env,
        expect_ok=False,
    )
    assert "row counts differ" in done.stderr
    assert f"-case_ {SEEDED['case_'] + 1}" in done.stdout, "the diff must name the table"


def test_a_corrupt_dump_is_caught_before_it_is_restored(
    tampered: Path, script_env: dict[str, str]
) -> None:
    dump = tampered / "postgres.dump"
    dump.write_bytes(dump.read_bytes() + b"corruption")
    done = _run(
        "restore.sh",
        "--scratch",
        "--backup",
        str(tampered),
        "--db",
        SCRATCH_DB,
        env=script_env,
        expect_ok=False,
    )
    assert "fails its own checksums" in done.stderr


def test_a_changed_l0_payload_fails_the_drill(
    backup: Path, script_env: dict[str, str], tmp_path: Path
) -> None:
    """Invariant #1: a payload that changed after it was fingerprinted is an incident."""
    lake = tmp_path / "damaged-lake"
    for relative, payload in LAKE_FILES.items():
        target = lake / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    changed = next(name for name in LAKE_FILES if name.endswith(".csv"))
    (lake / changed).write_bytes(b"SYMBOL,SERIES,CLOSE\nRELIANCE,EQ,9999.99\n")

    done = _run(
        "restore.sh",
        "--scratch",
        "--backup",
        str(backup),
        "--db",
        SCRATCH_DB,
        env={**script_env, "DATA_ROOT": str(lake)},
        expect_ok=False,
    )
    assert "invariant #1" in done.stderr
    assert changed in done.stdout


def test_restore_refuses_to_target_the_live_database(
    backup: Path, script_env: dict[str, str]
) -> None:
    """The one thing a drill must never do is overwrite the database it is drilling for."""
    live = Settings().database_url.get_secret_value().rsplit("/", 1)[-1].split("?")[0]
    done = _run(
        "restore.sh",
        "--scratch",
        "--backup",
        str(backup),
        "--db",
        live,
        env=script_env,
        expect_ok=False,
    )
    assert "only ever restores into a scratch one" in done.stderr
