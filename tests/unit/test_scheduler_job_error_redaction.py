"""`job_run.error` is masked, then bounded, where the runner records it (invariant #13).

A job's exception can quote anything — a DSN, a CLI's stderr, a token in a URL — and `job_run.error`
is read back by the status API and by `failure_alerts`. These tests drive the runner's outcome
mapping (`_execute`, which opens no connection) and show the alert body built from the recorded
error is the one it would have built from the raw text: `failure_alerts` behaves the same.

Credential-shaped values are built at runtime, so no literal here trips the repo's secret scan.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from dataplatform.alert_triggers import FinishedAttempt, failed_job_conditions
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.scheduler import Job, JobContext, JobRegistry, JobState, SchedulerRunner
from dataplatform.scheduler.runner import JOB_ERROR_CHARS
from tests.conftest import SettingsLoader

NOW = datetime(2026, 10, 9, 22, 0, tzinfo=IST)


@pytest.fixture
def settings(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Settings:
    """Environment-only settings on a scratch lake; nothing here can reach a real Postgres."""
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/nowhere")
    return load_settings(None)


def _secret(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _failing(settings: Settings, message: str) -> tuple[JobState, str | None]:
    def boom(_: JobContext) -> None:
        raise RuntimeError(message)

    job = Job(name="probe_job", cron="0 22 * * *", fn=boom, timeout=timedelta(minutes=5))
    runner = SchedulerRunner(
        JobRegistry([job]), settings=settings, clock=FrozenClock(NOW), instance="test"
    )
    state, error, _ = runner._execute(job, uuid4())
    return state, error


def test_a_failed_jobs_error_is_masked_before_it_is_recorded(settings: Settings) -> None:
    """Fails before the fix: `job_run.error` was `f"{type}: {error}"`, verbatim and unbounded."""
    secret = _secret("job-error")
    state, error = _failing(settings, f"could not connect to postgresql://app:{secret}@db/x")
    assert state is JobState.FAILED
    assert error == "RuntimeError: could not connect to postgresql://***@db/x"


def test_a_failed_jobs_error_is_bounded_after_masking(settings: Settings) -> None:
    secret = _secret("job-error-long")
    state, error = _failing(settings, "x" * (JOB_ERROR_CHARS * 2) + f" token={secret}")
    assert state is JobState.FAILED and error is not None
    assert len(error) == JOB_ERROR_CHARS and error.endswith("…")
    assert secret not in error


@pytest.mark.parametrize(
    "message",
    [
        "HostBusyError: nsearchives.nseindia.com is leased by delivery",
        "could not connect to postgresql://app:{secret}@db/x",
        "the Claude CLI failed: Authorization: Bearer {secret}\n" + "y" * 3000,
    ],
)
def test_failure_alerts_builds_the_same_page_from_the_recorded_error(
    settings: Settings, message: str
) -> None:
    raw = message.format(secret=_secret("alert-parity"))
    _, recorded = _failing(settings, raw)

    def page(error: str | None) -> str:
        (condition,) = failed_job_conditions([FinishedAttempt("probe_job", "FAILED", NOW, error)])
        return condition.body

    assert page(recorded) == page(f"RuntimeError: {raw}")
