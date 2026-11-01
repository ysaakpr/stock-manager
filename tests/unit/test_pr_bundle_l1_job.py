"""The scheduled PR-bundle L1 build: what it builds, when it runs, and that a failure is loud.

Until 2026-11-02 nothing ran `pr_bundle_l1` daily, so `pr_ca_broadcasts` — the M17 dossier's
corporate-action notices — stopped at 2026-10-01 while the EOD pipeline kept landing bundles in L0.
`catch_up` builds everything after the newest built partition plus a trailing window, offline.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest.pr_bundle_l1 import (
    CA_BROADCASTS_DATASET,
    CA_BROADCASTS_SCHEMA,
    CATCH_UP_LOOKBACK,
    PR_DATASETS,
    PrBundleL1Error,
    catch_up,
    newest_built_session,
)
from dataplatform.scheduler.registry import (
    EOD_PIPELINE,
    M17_FUND_MANAGERS,
    PAPER_SESSION,
    PR_BUNDLE_L1,
    SHARE_BASIS_GUARD,
    default_registry,
)
from dataplatform.store.l0 import L0Store
from dataplatform.store.paths import l1_partition_path

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_pr_bundle"
SESSION: Final = date(2026, 9, 4)  # the fixture bundle PR040926.zip


def _lake(root: Path, *, built: tuple[date, ...] = ()) -> L0Store:
    """L0 holding the 2026-09-04 bundle; an empty pr_ca_broadcasts partition per `built` date."""
    store = L0Store(clock=FrozenClock(SESSION), data_root=root)
    store.put(
        "nse_pr_bundle", SESSION, "PR040926.zip",
        (FIXTURES / "lowercase" / "PR040926.zip").read_bytes(),
    )  # fmt: skip
    for day in built:
        path = l1_partition_path(CA_BROADCASTS_DATASET, day, data_root=root)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(CA_BROADCASTS_SCHEMA.empty_table(), path)
    return store


def _built(root: Path, day: date = SESSION) -> bool:
    return l1_partition_path(CA_BROADCASTS_DATASET, day, data_root=root).exists()


# ── what a run builds ──────────────────────────────────────────────────────────────────────


def test_a_run_after_an_outage_builds_everything_since_the_newest_partition(tmp_path: Path) -> None:
    # Newest built 2026-08-03, today two months later: far outside the trailing window.
    store = _lake(tmp_path, built=(date(2026, 8, 3),))
    report = catch_up(today=date(2026, 11, 2), l0=store, master=None, data_root=tmp_path)
    assert (report.bundles, report.built, report.failures) == (1, 1, [])
    assert _built(tmp_path)
    assert newest_built_session(data_root=tmp_path) == SESSION


def test_a_bundle_landed_late_inside_the_window_is_built(tmp_path: Path) -> None:
    # A later session is already built; the 09-04 bundle arrived after it (EOD self-heal).
    store = _lake(tmp_path, built=(date(2026, 9, 10),))
    today = SESSION + CATCH_UP_LOOKBACK
    catch_up(today=today, l0=store, master=None, data_root=tmp_path)
    assert _built(tmp_path)


def test_a_built_session_outside_the_window_is_left_alone(tmp_path: Path) -> None:
    store = _lake(tmp_path, built=(date(2026, 9, 30),))
    today = SESSION + CATCH_UP_LOOKBACK + timedelta(days=1)
    report = catch_up(today=today, l0=store, master=None, data_root=tmp_path)
    assert report.bundles == 0 and not _built(tmp_path)


def test_the_broadcasts_carry_the_derived_ex_date_column(tmp_path: Path) -> None:
    catch_up(
        today=date(2026, 11, 2),
        l0=_lake(tmp_path, built=(date(2026, 8, 3),)),
        master=None,
        data_root=tmp_path,
    )
    path = l1_partition_path(CA_BROADCASTS_DATASET, SESSION, data_root=tmp_path)
    assert "ex_date_derived" in pq.read_schema(path).names


def test_an_empty_lake_is_refused_not_backfilled_by_the_nightly_job(tmp_path: Path) -> None:
    with pytest.raises(PrBundleL1Error, match="one-off backfill"):
        catch_up(today=date(2026, 11, 2), l0=_lake(tmp_path), master=None, data_root=tmp_path)
    assert not _built(tmp_path)


def test_an_undated_bundle_fails_the_run_after_the_others_are_written(tmp_path: Path) -> None:
    store = _lake(tmp_path, built=(date(2026, 8, 3),))
    store.put(
        "nse_pr_bundle", SESSION + timedelta(days=1), "PR020118.zip",
        (FIXTURES / "bh_misserved" / "PR020118.zip").read_bytes(),
    )  # fmt: skip
    with pytest.raises(PrBundleL1Error, match=r"1 problem\(s\).*undated"):
        catch_up(today=date(2026, 11, 2), l0=store, master=None, data_root=tmp_path)
    assert _built(tmp_path)  # the good bundle was still built


def test_newest_built_session_reads_every_dataset(tmp_path: Path) -> None:
    assert newest_built_session(data_root=tmp_path) is None
    for i, dataset in enumerate(PR_DATASETS):
        path = l1_partition_path(dataset, date(2026, 10, 1 + i), data_root=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert newest_built_session(data_root=tmp_path) == date(2026, 10, len(PR_DATASETS))


# ── when it runs ───────────────────────────────────────────────────────────────────────────


def _next_fire(job: object, after: datetime) -> datetime:
    fire: datetime = job.trigger(IST).get_next_fire_time(None, after)  # type: ignore[attr-defined]
    return fire


@pytest.mark.parametrize("day", [date(2026, 11, 2) + timedelta(days=i) for i in range(5)])
def test_it_runs_each_weekday_after_the_capture_and_before_the_guard_and_m17(day: date) -> None:
    midnight = datetime(day.year, day.month, day.day, tzinfo=IST)
    fire = _next_fire(PR_BUNDLE_L1, midnight)
    assert fire.date() == day and (fire.hour, fire.minute) == (19, 20)
    eod = _next_fire(EOD_PIPELINE, midnight)
    assert eod + EOD_PIPELINE.timeout <= fire  # after the capture's whole budget
    latest_end = fire + PR_BUNDLE_L1.latest_start + PR_BUNDLE_L1.timeout
    assert latest_end <= _next_fire(SHARE_BASIS_GUARD, midnight)
    assert latest_end < _next_fire(PAPER_SESSION, midnight)
    assert latest_end < _next_fire(M17_FUND_MANAGERS, midnight)


def test_it_does_not_run_at_the_weekend() -> None:
    saturday = datetime(2026, 11, 7, tzinfo=IST)
    assert _next_fire(PR_BUNDLE_L1, saturday).date() == date(2026, 11, 9)


def test_it_is_registered_and_covers_no_fetched_source() -> None:
    assert "pr_bundle_l1" in default_registry()
    assert PR_BUNDLE_L1.covers == () and PR_BUNDLE_L1.sync_sources == ()
