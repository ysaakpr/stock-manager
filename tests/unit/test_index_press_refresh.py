"""DQ-5 — the scheduled `index_press_refresh` job is bounded and fails loud.

The job reuses the campaign driver, so what must be proved is the bound that separates a weekly
refresh from the owner-gated backfill: it asks only for the last `REFRESH_WINDOW` of releases, at
most `REFRESH_MAX_RELEASES` of them, for the job's own date — and a release that failed (or a
ceiling that was hit) makes the run FAILED instead of a log line. Offline: the fetcher, the
database and the campaign are stand-ins; nothing here opens a socket.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest import index_history_backfill as job_module
from dataplatform.ingest.index_history_backfill import (
    REFRESH_MAX_RELEASES,
    REFRESH_WINDOW,
    CampaignReport,
    run_press_release_refresh,
)
from dataplatform.scheduler.registry import INDEX_PRESS_REFRESH, JobContext
from tests.conftest import SettingsLoader

SATURDAY = datetime(2026, 10, 10, 9, 0, tzinfo=IST)


class _Conn:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def _report(**overrides: Any) -> CampaignReport:
    fields: dict[str, Any] = {
        "as_of": SATURDAY.date(),
        "requests": 9,
        "candidates": 420,
        "fetched": ("ind_prs09102026.pdf",),
        "already_in_l0": (),
        "failed": (),
        "anchors": (),
        "oldest_contiguous": date(2026, 10, 9),
        "budget_exhausted": False,
    }
    fields.update(overrides)
    return CampaignReport(**fields)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"report": _report()}

    @contextmanager
    def fake_lease(hosts: list[str], **kwargs: Any) -> Iterator[object]:
        seen["hosts"] = hosts
        seen["command"] = kwargs["command"]
        yield object()

    @contextmanager
    def fake_connection(settings: Any) -> Iterator[_Conn]:
        seen["conn"] = _Conn()
        yield seen["conn"]

    def fake_campaign(**kwargs: Any) -> CampaignReport:
        seen["kwargs"] = kwargs
        report: CampaignReport = seen["report"]
        return report

    monkeypatch.setattr(job_module, "leased_fetcher", fake_lease)
    monkeypatch.setattr(job_module, "connection", fake_connection)
    monkeypatch.setattr(job_module, "SyncStateStore", lambda *a, **k: object())
    monkeypatch.setattr(job_module, "run_press_release_campaign", fake_campaign)
    return seen


def _context(load_settings: SettingsLoader) -> JobContext:
    return JobContext(
        job_name=INDEX_PRESS_REFRESH.name,
        run_id=uuid4(),
        clock=FrozenClock(SATURDAY),
        settings=load_settings(None),
    )


def test_the_refresh_asks_only_for_a_recent_window_under_a_ceiling(
    wired: dict[str, Any], load_settings: SettingsLoader
) -> None:
    run_press_release_refresh(_context(load_settings))
    kwargs = wired["kwargs"]
    assert kwargs["as_of"] == SATURDAY.date()
    assert kwargs["since"] == SATURDAY.date() - REFRESH_WINDOW
    assert kwargs["max_releases"] == REFRESH_MAX_RELEASES
    assert timedelta(days=180) >= REFRESH_WINDOW, "a wide window is the owner-gated backfill"
    assert REFRESH_MAX_RELEASES < 50
    assert wired["hosts"] == ["niftyindices.com"]
    assert wired["conn"].commits == 1


def test_a_failed_release_fails_the_run(
    wired: dict[str, Any], load_settings: SettingsLoader
) -> None:
    wired["report"] = _report(failed=(("ind_prs09102026.pdf", "FetchHTTPError: 404"),))
    with pytest.raises(RuntimeError, match="1 release"):
        run_press_release_refresh(_context(load_settings))


def test_hitting_the_ceiling_fails_the_run(
    wired: dict[str, Any], load_settings: SettingsLoader
) -> None:
    wired["report"] = _report(budget_exhausted=True, unfetched=("ind_prs01092026.pdf",))
    with pytest.raises(RuntimeError, match="ceiling"):
        run_press_release_refresh(_context(load_settings))


def test_the_job_covers_the_source_and_fires_after_tri_refresh() -> None:
    assert INDEX_PRESS_REFRESH.covers == ("nifty_index_press_releases",)
    assert INDEX_PRESS_REFRESH.sync_sources == ()
    assert INDEX_PRESS_REFRESH.cron == "0 9 * * sat"
