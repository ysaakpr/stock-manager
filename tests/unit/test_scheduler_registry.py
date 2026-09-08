"""The registry validates a job the moment it exists — offline, no database, no scheduler.

The point of these is *when* they fail. A bad cron expression or a duplicated job name is only
observable in production as a job that silently never fires; validating at construction moves both
into `make check`, and these tests are what keep that validation from being quietly removed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from dataplatform.scheduler import (
    JOB_NAME,
    Job,
    JobContext,
    JobNotRegisteredError,
    JobRegistry,
    default_registry,
)


def _noop(context: JobContext) -> None:
    return None


def _job(name: str = "a_job", cron: str = "30 18 * * mon-fri", **kwargs: object) -> Job:
    fields: dict[str, object] = {"timeout": timedelta(minutes=1), **kwargs}
    return Job(name=name, cron=cron, fn=_noop, **fields)  # type: ignore[arg-type]


def test_a_valid_job_builds_a_trigger_in_the_exchange_timezone() -> None:
    from zoneinfo import ZoneInfo

    trigger = _job().trigger(ZoneInfo("Asia/Kolkata"))
    assert str(trigger.timezone) == "Asia/Kolkata"


@pytest.mark.parametrize("cron", ["not a cron", "30 18 * *", "70 18 * * *", ""])
def test_an_invalid_cron_fails_at_construction(cron: str) -> None:
    """Not at 18:30 on the trading day the schedule was supposed to fire."""
    with pytest.raises(ValueError, match="invalid cron expression"):
        _job(cron=cron)


@pytest.mark.parametrize("name", ["_scheduler", "EodPipeline", "eod-pipeline", "ab", "9lives", ""])
def test_an_illegal_job_name_is_refused(name: str) -> None:
    """`_`-prefixed names especially: the heartbeat row reserves that prefix for itself."""
    with pytest.raises(ValueError, match="must be lower snake_case"):
        _job(name=name)


def test_a_non_positive_timeout_is_refused() -> None:
    with pytest.raises(ValueError, match="positive timeout"):
        _job(timeout=timedelta(0))


def test_registering_the_same_name_twice_is_an_error() -> None:
    """Last-writer-wins would leave two schedules for one name and no way to tell which is live."""
    registry = JobRegistry([_job("duplicated")])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_job("duplicated"))


def test_an_unknown_name_names_the_jobs_that_do_exist() -> None:
    registry = JobRegistry([_job("first"), _job("second")])
    with pytest.raises(JobNotRegisteredError, match="registered jobs: first, second"):
        registry.get("third")


def test_the_registry_keeps_registration_order() -> None:
    registry = JobRegistry([_job("zulu"), _job("alpha")])
    assert registry.names() == ("zulu", "alpha")
    assert len(registry) == 2 and "alpha" in registry and "nope" not in registry


def test_the_default_registry_holds_exactly_the_jobs_production_runs() -> None:
    """The exact set, in registration order — a job may be neither dropped nor added by accident.

    Pinned exactly rather than by `in` on purpose, and it is the *only* assertion here that has to
    change when a job is added: a registry that silently lost the daily EOD job looks identical to
    a healthy one at runtime until 18:30 comes and goes, and a job registered by an unreviewed
    import is exactly what §8.1's explicit registry exists to prevent. Each job's own cadence is
    proved by its own task's tests (`tests/integration/test_scheduler.py` for the EOD pipeline,
    `tests/integration/test_constituents_snapshot_job.py` for the weekly snapshot); what is proved
    here is the membership.
    """
    registry = default_registry()
    assert registry.names() == (
        "eod_pipeline",
        "daily_snapshot",
        "constituents_snapshot",
        "l0_verify",
        "identity_refresh",
    )
    assert registry.get("eod_pipeline").cron == "30 18 * * mon-fri"
    # 19:15, after the 18:30 EOD pipeline: the two share nsearchives.nseindia.com, and a host
    # lease is refused rather than queued, so an overlap would be a skipped snapshot.
    assert registry.get("daily_snapshot").cron == "15 19 * * mon-fri"
    assert registry.get("constituents_snapshot").cron == "0 20 * * sat"
    assert registry.get("l0_verify").cron == "0 3 * * sun"
    assert registry.get("identity_refresh").cron == "0 7 * * sat"


def test_every_default_job_is_valid_and_describes_itself() -> None:
    """The construction-time guarantees, asserted over whatever the registry holds.

    This is the half of the module docstring's promise that survives a new job being added: a bad
    cron or a non-positive timeout in job number three must fail in `make check`, not at the hour
    it was supposed to fire. `Job.__post_init__` already enforces both, so the assertions below can
    only fail if that validation is weakened or bypassed — which is the point of having them.
    """
    from zoneinfo import ZoneInfo

    exchange = ZoneInfo("Asia/Kolkata")
    jobs = [default_registry().get(name) for name in default_registry().names()]
    assert jobs, "a scheduler with no jobs is a misconfiguration, not a valid default"
    for job in jobs:
        assert JOB_NAME.match(job.name), job.name
        assert job.timeout > timedelta(0), job.name
        # Builds in the exchange timezone the scheduler supplies from Settings, never the host's.
        assert str(job.trigger(exchange).timezone) == "Asia/Kolkata", job.name
        # A registered job nobody can identify from `scheduler list` is an operational trap.
        assert job.description.strip(), job.name


def test_the_default_registry_is_a_fresh_object_each_call() -> None:
    """A module-level singleton would let one test's extra job leak into the next one."""
    first = default_registry()
    first.register(_job("extra"))
    assert "extra" not in default_registry()
