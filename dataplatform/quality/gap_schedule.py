"""When a `(source, date)` pair is owed: the gap report's `DueSchedule`, read off the job registry.

The gap report (`quality.gaps`) owed every trading day the moment it began, so today's
announcements — landed at 00:30 tomorrow — were "NEVER_ATTEMPTED" all day, and the news and TRI
sources, which file one row per sub-unit, were "NEVER_ATTEMPTED" every day because no whole-source
row exists. This module answers the two questions the report needs to stop saying that, and
nothing else:

* `due_at` — the instant by which the scheduled fire that covers *that* date, plus its budget and
  a small grace, has passed. The covering fire is the first fire of a covering job at or after
  midnight of `date + Job.logical_date_lag_days`; with several covering jobs, the earliest. It is
  never the job's *next* fire: a job that missed its run always has a next fire ahead, and keying
  on it would call the missed day "not yet due" forever.
* `expected_units` — for a unit-keyed source whose sub-units configuration lists, each sub-unit
  the day owes and its own due instant:

  ==================  ========================================================================
  source              sub-units of day D
  ==================  ========================================================================
  curated_rss         `<feed>-<HHMM>` for every active feed filed under it (`rss_feeds.yaml`)
                      times every `news_capture` fire on D (the poll's IST HHMM)
  nifty_tri_history   every slug of `tri_backfill.EVENING_INDEX_SET`, due after the first
                      `tri_evening` fire on D
  ==================  ========================================================================

  `gdelt_v2_event_files` also files one row per sub-unit, but its unit is the GDELT slot the
  manifest names at poll time, which no configuration lists, so it is not here and its days are
  judged as before. Neither set is dated: a feed or index added later is expected on earlier days
  too, which can only make such a day unexplained, never hide one.

What it never does: read the database, or the wall clock — `now` is the caller's injected instant.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from dataplatform.quality.gaps import ExpectedUnit
from dataplatform.scheduler.registry import Job, JobRegistry, default_registry

__all__ = ["GRACE", "RegistrySchedule"]

#: Slack after a covering fire's budget before its date counts as missed: a fire starts a few
#: seconds late and a commit lands after the run is recorded. Small on purpose.
GRACE: Final = timedelta(minutes=15)

#: (source, the job whose fires define its sub-units).
_NEWS_JOB: Final = "news_capture"
_TRI_JOB: Final = "tri_evening"


def _rss_units(job: Job, fire: datetime) -> tuple[str, ...]:
    from dataplatform.ingest import rss

    return tuple(
        f"{feed.id}-{fire:%H%M}" for feed in rss.active_feeds() if feed.source == "curated_rss"
    )


def _tri_units(job: Job, fire: datetime) -> tuple[str, ...]:
    from dataplatform.ingest.tri_backfill import EVENING_INDEX_SET

    return tuple(spec.slug for spec in EVENING_INDEX_SET)


#: source → (job, every fire or only the first, the units one fire owes).
_UNIT_SOURCES: Final[Mapping[str, tuple[str, bool, Callable[[Job, datetime], tuple[str, ...]]]]] = {
    "curated_rss": (_NEWS_JOB, True, _rss_units),
    "nifty_tri_history": (_TRI_JOB, False, _tri_units),
}


@dataclass(frozen=True)
class RegistrySchedule:
    """`quality.gaps.DueSchedule` from a job registry, in the exchange's time zone."""

    timezone: ZoneInfo
    registry: JobRegistry = field(default_factory=default_registry)

    def _covering(self, source: str) -> list[Job]:
        return [job for job in self.registry if source in job.sync_sources or source in job.covers]

    def _fires_on(self, job: Job, day: date) -> Iterator[datetime]:
        trigger = job.trigger(self.timezone)
        start = datetime.combine(day, time.min, tzinfo=self.timezone)
        end = start + timedelta(days=1)
        previous: datetime | None = None
        cursor = start
        while True:
            fire = trigger.get_next_fire_time(previous, cursor)
            if fire is None or fire >= end:
                return
            yield fire
            previous, cursor = fire, fire + timedelta(seconds=1)

    def _deadline(self, job: Job, fire: datetime) -> datetime:
        return fire + job.timeout + GRACE

    def due_at(self, source: str, logical_date: date) -> datetime | None:
        """The earliest covering job's first fire at or after midnight of D + its lag, + budget."""
        deadlines = []
        for job in self._covering(source):
            anchor = datetime.combine(
                logical_date + timedelta(days=job.logical_date_lag_days),
                time.min,
                tzinfo=self.timezone,
            )
            fire = job.trigger(self.timezone).get_next_fire_time(None, anchor)
            if fire is not None:
                deadlines.append(self._deadline(job, fire))
        return min(deadlines, default=None)

    def expected_units(self, source: str, logical_date: date) -> tuple[ExpectedUnit, ...] | None:
        """The sub-units day D of a configured unit-keyed source owes, or None for any other."""
        spec = _UNIT_SOURCES.get(source)
        if spec is None:
            return None
        job_name, every_fire, units = spec
        job = self.registry.get(job_name)
        fires = list(self._fires_on(job, logical_date))
        if not every_fire:
            fires = fires[:1]
        return tuple(
            ExpectedUnit(unit=unit, due_at=self._deadline(job, fire))
            for fire in fires
            for unit in units(job, fire)
        )
