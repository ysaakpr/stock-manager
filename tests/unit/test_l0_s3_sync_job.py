"""OPS: the weekly `l0_s3_sync` job — its schedule, its failure path, its staleness, its silence.

The script itself is proved against a stub `aws` in `test_l0_s3_sync.py`; this file proves the
job around it. A fake runner stands in for the script where the question is the job's control
flow; the real script against the same stub `aws` and a fake SSE-C key stands in where the
question is what reaches the logs and `job_run.error`. Nothing here touches AWS or the network.
"""

from __future__ import annotations

import base64
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.scheduler import JobRegistry, JobState, SchedulerRunner, build_scheduler
from dataplatform.scheduler.health import JobHealthState, LastRuns, assess
from dataplatform.scheduler.registry import L0_S3_SYNC, default_registry
from dataplatform.store.l0_s3_mirror import (
    SCRIPT,
    L0S3SyncError,
    redact,
    run_l0_s3_sync,
)
from tests.conftest import SettingsLoader
from tests.unit.test_l0_s3_sync import BUCKET, KEY, Rig, _settled
from tests.unit.test_l0_s3_sync import rig as rig

#: Friday 2026-11-06 18:00 IST, the evening the job was registered.
FRIDAY = datetime(2026, 11, 6, 18, 0, tzinfo=IST)


@pytest.fixture
def settings(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Settings:
    """Environment-only settings on a scratch lake; nothing here can reach a real Postgres."""
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/nowhere")
    return load_settings(None)


class FakeScript:
    """Stands in for `ops/l0-s3-sync.sh`: answers each mode with the next scripted exit code."""

    def __init__(self, **codes: list[int]) -> None:
        self.codes = {mode: list(values) for mode, values in codes.items()}
        self.argvs: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.argvs.append(argv)
        self.envs.append(kwargs["env"])
        mode = argv[2]
        code = self.codes[mode].pop(0)
        return subprocess.CompletedProcess(argv, code, stdout=f"l0-s3-sync: {mode} rc={code}\n")

    def modes(self) -> list[str]:
        return [argv[2] for argv in self.argvs]


# ── the schedule ─────────────────────────────────────────────────────────────────────────────


def test_the_job_is_registered_weekly_on_sunday_at_ten_ist() -> None:
    job = default_registry().get("l0_s3_sync")
    assert job is L0_S3_SYNC
    assert job.cron == "0 10 * * sun"
    assert job.max_success_age == timedelta(days=8)
    first = job.trigger(IST).get_next_fire_time(None, FRIDAY)
    assert first == datetime(2026, 11, 8, 10, 0, tzinfo=IST)


def test_the_scheduler_adds_the_job_under_its_cron(settings: Settings) -> None:
    scheduler = build_scheduler(SchedulerRunner(settings=settings))
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert "l0_s3_sync" in jobs
    assert str(jobs["l0_s3_sync"].trigger) == str(L0_S3_SYNC.trigger(IST))


# ── control flow ─────────────────────────────────────────────────────────────────────────────


def test_a_clean_run_is_one_sync_then_one_verify(settings: Settings) -> None:
    fake = FakeScript(sync=[0], verify=[0])
    result = run_l0_s3_sync(settings, sample=50, environ={"PATH": "/bin"}, runner=fake)
    assert fake.argvs == [
        ["bash", str(SCRIPT), "sync"],
        ["bash", str(SCRIPT), "verify", "50"],
    ]
    assert not result.resynced
    assert fake.envs[0]["DATA_ROOT"] == str(settings.data_root)


def test_only_missing_keys_get_one_more_sync_and_verify(settings: Settings) -> None:
    fake = FakeScript(sync=[0, 0], verify=[5, 0])
    assert run_l0_s3_sync(settings, environ={}, runner=fake).resynced
    assert fake.modes() == ["sync", "verify", "sync", "verify"]


def test_only_missing_twice_is_a_failure(settings: Settings) -> None:
    fake = FakeScript(sync=[0, 0], verify=[5, 5])
    with pytest.raises(L0S3SyncError, match="verify exited 5"):
        run_l0_s3_sync(settings, environ={}, runner=fake)


def test_a_content_disagreement_is_never_re_sampled(settings: Settings) -> None:
    """Exit 1 can be a sha256 mismatch; a second random sample could miss it, so no retry."""
    fake = FakeScript(sync=[0], verify=[1])
    with pytest.raises(L0S3SyncError, match="verify exited 1"):
        run_l0_s3_sync(settings, environ={}, runner=fake)
    assert fake.modes() == ["sync", "verify"]


def test_a_failed_sync_stops_before_verify(settings: Settings) -> None:
    fake = FakeScript(sync=[1])
    with pytest.raises(L0S3SyncError, match="sync exited 1"):
        run_l0_s3_sync(settings, environ={}, runner=fake)
    assert fake.modes() == ["sync"]


def test_the_script_gets_an_allowlisted_environment(settings: Settings) -> None:
    fake = FakeScript(sync=[0], verify=[0])
    environ = {
        "HOME": "/home/x",
        "PATH": "/bin",
        "AWS_REGION": "ap-south-1",
        "L0_S3_ENV": "/x/env",
        "SSE_C_KEY_B64": "/x/key",
        # Built at runtime so no credential-shaped literal trips the repo's secret scan.
        "DATABASE_URL": "postgresql://" + "app" + ":" + "x" * 8 + "@db/x",
        "ANTHROPIC_" + "API_KEY": "x" * 8,
    }
    run_l0_s3_sync(settings, environ=environ, runner=fake)
    assert set(fake.envs[0]) == {
        "HOME",
        "PATH",
        "AWS_REGION",
        "L0_S3_ENV",
        "SSE_C_KEY_B64",
        "DATA_ROOT",
    }


@pytest.mark.parametrize(
    ("sync", "verify"), [([1], []), ([2], []), ([0], [1]), ([0], [2]), ([0, 0], [5, 1])]
)
def test_a_failed_sync_or_verify_is_a_failed_job_run(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, sync: list[int], verify: list[int]
) -> None:
    """Through the runner's own outcome mapping: the run is FAILED, with the step in the error."""
    import dataplatform.store.l0_s3_mirror as mirror

    fake = FakeScript(sync=sync, verify=verify)
    monkeypatch.setattr(mirror.subprocess, "run", fake)
    runner = SchedulerRunner(
        JobRegistry([L0_S3_SYNC]), settings=settings, clock=FrozenClock(FRIDAY), instance="t"
    )
    state, error, _ = runner._execute(L0_S3_SYNC, uuid4())
    assert state is JobState.FAILED
    assert error is not None and "L0S3SyncError" in error and "exited" in error


# ── staleness ────────────────────────────────────────────────────────────────────────────────


def _succeeded(at: datetime) -> LastRuns:
    return LastRuns(last_state="SUCCEEDED", last_started_at=at, last_success_at=at)


def test_a_success_older_than_eight_days_is_unhealthy_and_a_recent_one_is_not() -> None:
    """Fails if the age check is inverted, dropped, or measured from the wrong end."""
    sunday = datetime(2026, 11, 15, 10, 0, 5, tzinfo=IST)
    fresh = assess(L0_S3_SYNC, _succeeded(sunday), now=sunday + timedelta(days=7), timezone=IST)
    assert fresh.state is JobHealthState.OK and fresh.healthy

    later = sunday + timedelta(days=8, hours=1)
    stale = assess(L0_S3_SYNC, _succeeded(sunday), now=later, timezone=IST)
    assert stale.state is JobHealthState.OVERDUE and not stale.healthy


def test_the_age_limit_binds_where_the_cron_alone_would_not() -> None:
    """The eight days are the owner's rule, not an echo of the weekly cron.

    On the weekly cron a missed fire is already OVERDUE after 7 days and the 6 h budget, so the
    limit is proved on a monthly copy of the job: nine days after a success that covers the newest
    due fire, the cron alone says OK; the limit says OVERDUE from the instant it was crossed.
    """
    monthly = replace(L0_S3_SYNC, name="l0_s3_sync_monthly", cron="0 10 1 * *")
    first = datetime(2026, 12, 1, 10, 0, 5, tzinfo=IST)
    nine_days = first + timedelta(days=9)

    unlimited = replace(monthly, max_success_age=None)
    assert assess(unlimited, _succeeded(first), now=nine_days, timezone=IST).state is (
        JobHealthState.OK
    )
    stale = assess(monthly, _succeeded(first), now=nine_days, timezone=IST)
    assert stale.state is JobHealthState.OVERDUE and not stale.healthy
    assert stale.due_since == first + timedelta(days=8)
    seven_days = assess(monthly, _succeeded(first), now=first + timedelta(days=7), timezone=IST)
    assert seven_days.state is JobHealthState.OK and seven_days.healthy


def test_a_lock_skip_after_a_stale_success_is_unhealthy() -> None:
    """A by-hand sync holding the lock at 10:00 skips the fire; the old success is still old."""
    old = datetime(2026, 11, 15, 10, 0, tzinfo=IST)
    skipped = datetime(2026, 11, 22, 10, 0, tzinfo=IST)
    runs = LastRuns(last_state="SKIPPED_LOCKED", last_started_at=skipped, last_success_at=old)
    health = assess(L0_S3_SYNC, runs, now=old + timedelta(days=8, minutes=1), timezone=IST)
    assert not health.healthy


def test_a_failed_newest_run_is_unhealthy_even_with_a_fresh_success() -> None:
    success = datetime(2026, 11, 15, 10, 0, tzinfo=IST)
    runs = LastRuns(
        last_state="FAILED",
        last_started_at=success + timedelta(hours=1),
        last_error="L0S3SyncError: l0-s3-sync verify exited 1",
        last_success_at=success,
    )
    health = assess(L0_S3_SYNC, runs, now=success + timedelta(hours=2), timezone=IST)
    assert health.state is JobHealthState.FAILING and not health.healthy


def test_before_its_first_fire_the_job_is_not_yet_due() -> None:
    health = assess(L0_S3_SYNC, LastRuns(), now=FRIDAY + timedelta(hours=1), timezone=IST)
    assert health.state is JobHealthState.NOT_YET_DUE and health.healthy


# ── what reaches the logs ────────────────────────────────────────────────────────────────────


def _key_forms() -> Iterator[str]:
    yield base64.b64encode(KEY).decode()
    yield KEY.hex()
    yield KEY.decode("latin-1")


@pytest.mark.parametrize("fail_download", [False, True])
def test_no_log_line_or_error_carries_the_key_or_the_bucket(
    rig: Rig,
    settings: Settings,
    fail_download: bool,
) -> None:
    """The real script against the stub `aws`, with a fake SSE-C key, through the job function.

    The job hands the script an allowlisted environment, so the stub's own settings travel in a
    wrapper named by `AWS_BIN` (an allowed name) rather than as variables the job would drop.
    """
    assert settings.data_root == rig.l0.parent
    _settled(rig.l0 / "src" / "2024" / "d.csv", b"delta\n")
    wrapper = rig.tmp / "aws-wrapper"
    fail = "export STUB_FAIL_DOWNLOAD=1\n" if fail_download else ""
    stub_env = f"STUB_REMOTE='{rig.env['STUB_REMOTE']}' STUB_LOG='{rig.env['STUB_LOG']}'"
    wrapper.write_text(
        f"#!/bin/sh\nexport {stub_env}\n{fail}exec '{rig.tmp / 'bin' / 'aws'}' \"$@\"\n"
    )
    wrapper.chmod(0o755)
    environ = {**rig.env, "AWS_BIN": str(wrapper)}

    error = ""
    with capture_logs() as entries:
        try:
            run_l0_s3_sync(settings, sample=10, environ=environ)
        except L0S3SyncError as raised:
            error = str(raised)
    assert bool(error) is fail_download
    if fail_download:
        assert "download failures: 4" in error

    logged = "\n".join(repr(entry) for entry in entries)
    assert "l0_s3_sync.step_done" in logged
    argv_log = rig.log.read_text()
    for text in (logged, error):
        assert BUCKET not in text
        for form in _key_forms():
            assert form not in text
    for form in _key_forms():
        assert form not in argv_log


def test_redact_masks_an_s3_url_and_an_account_id() -> None:
    text = f"upload failed: L0/x to s3://{BUCKET}/sm-raw/x An error occurred 123456789012"
    masked = redact(text)
    assert BUCKET not in masked and "123456789012" not in masked
    assert "s3://<bucket>/sm-raw/x" in masked
