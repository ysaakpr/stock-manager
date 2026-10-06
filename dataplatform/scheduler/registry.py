"""The job registry — the complete list of what the scheduler is allowed to run.

A `Job` is a name, a cron expression, a function, and a time budget. Nothing else: the runner owns
locking, logging and recording, so a job body is only the work itself and can be tested without a
scheduler anywhere near it.

Two properties are deliberate. First, **the registry is explicit** — a job exists because it is
constructed here and handed to a `JobRegistry`, never because a decorator ran on an import that
happened to be reached. A scheduler whose contents depend on import order is a scheduler nobody can
audit. Second, **a job is validated at construction**: the name shape and the cron expression are
checked the moment the object exists, so a typo in a schedule fails at import — in `make check` —
rather than at 18:30 on a trading day when the job silently never fires.

Jobs take a `JobContext` rather than no arguments, because a job needs the clock and the settings
and must not go and get them itself (B10, invariant #11).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from dataplatform.clock import Clock
from dataplatform.config import Settings
from dataplatform.logging import get_logger

__all__ = [
    "ANNOUNCEMENTS_CAPTURE",
    "BSE_CA_SWEEP",
    "CA_REFRESH",
    "CONSTITUENTS_SNAPSHOT",
    "DAILY_SNAPSHOT",
    "EOD_PIPELINE",
    "FBIL_REFERENCE_RATES",
    "INDEX_PRESS_REFRESH",
    "JOB_NAME",
    "MACRO_RELEASE_CAPTURE",
    "NEWS_CAPTURE",
    "NSE_DAILY_CAPTURE",
    "PAPER_SESSION",
    "SHAREHOLDING_POLL",
    "TRI_REFRESH",
    "UNSCHEDULED",
    "Job",
    "JobContext",
    "JobFn",
    "JobNotRegisteredError",
    "JobRegistry",
    "announcements_capture",
    "bse_ca_sweep",
    "ca_refresh",
    "constituents_snapshot",
    "daily_snapshot",
    "default_registry",
    "eod_pipeline",
    "fbil_reference_rates",
    "lag_budgets",
    "macro_release_capture",
    "news_capture",
    "nse_daily_capture",
    "paper_session",
    "shareholding_poll",
    "tri_refresh",
]

#: Job names are lower snake_case and never start with an underscore, which is what keeps them
#: disjoint from the scheduler's own internal ids (`runner.TICK_JOB_ID`).
JOB_NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

log = get_logger(__name__)


class JobNotRegisteredError(LookupError):
    """A job was asked for by a name the registry does not know."""


@dataclass(frozen=True, slots=True)
class JobContext:
    """Everything a job function is handed, and the only way it learns any of it.

    A job that wants the current date reads `context.clock`, and a job that wants the database
    reads `context.settings` — neither is fetched from module state, so a replay or a test can run
    the same function against a frozen clock and a scratch database without patching anything.
    """

    job_name: str
    run_id: UUID
    clock: Clock
    settings: Settings


#: What the scheduler calls. A job reports failure by raising; the runner catches it, records the
#: run FAILED and keeps the scheduler alive, so no job needs its own try/except to be safe.
JobFn = Callable[[JobContext], None]


@dataclass(frozen=True, slots=True)
class Job:
    """One scheduled unit of work, validated at construction.

    What it does: binds a callable to a cron schedule under a stable name, with a duration budget
    the runner reports against.
    What it assumes: `fn` is safe to run concurrently with *other* jobs — the runner's advisory
    lock only serialises a job against itself.
    What it never does: run anything. Constructing a `Job` has no side effect beyond validation.

    `covers` names the Source Register rows this job keeps current, and is what
    `test_scheduler_registry` holds against the register: a live source that no job covers and
    `UNSCHEDULED` does not explain fails the gate. `sync_sources` names the `sync_state` sources
    whose lag this job answers for, and `max_lag_sessions` how many sessions behind one may fall
    before `/status/sources` stops calling it healthy — the 2026-10-05 audit found the bhavcopy
    family 21 sessions behind and reported `healthy: true`, because health looked at failures and
    never at lag.
    """

    name: str
    cron: str
    fn: JobFn
    timeout: timedelta
    description: str = ""
    covers: tuple[str, ...] = ()
    sync_sources: tuple[str, ...] = ()
    max_lag_sessions: int = 1

    def __post_init__(self) -> None:
        if not JOB_NAME.match(self.name):
            raise ValueError(
                f"job name {self.name!r} must be lower snake_case, 3-64 chars, and must not "
                "start with an underscore (that prefix is reserved for the scheduler's own "
                "internal jobs, such as the heartbeat tick)"
            )
        if self.timeout <= timedelta(0):
            raise ValueError(f"job {self.name!r} needs a positive timeout, got {self.timeout!r}")
        if self.max_lag_sessions < 0:
            raise ValueError(f"job {self.name!r} needs a non-negative max_lag_sessions")
        self.trigger()  # validate the cron now, not on the morning it was supposed to fire

    def trigger(self, timezone: ZoneInfo | None = None) -> Any:
        """This job's cron expression as an APScheduler trigger, in `timezone`.

        Raises `ValueError` naming the job when the expression is not a valid 5-field crontab —
        the whole reason this is called from `__post_init__`.
        """
        try:
            return CronTrigger.from_crontab(self.cron, timezone=timezone)
        except ValueError as error:
            raise ValueError(
                f"job {self.name!r} has an invalid cron expression {self.cron!r}: {error}"
            ) from error


class JobRegistry:
    """The set of jobs one scheduler process may run, keyed by name.

    What it does: holds jobs, rejects a duplicate name, and fails loud on an unknown one.
    What it assumes: it is built once at startup and not mutated afterwards.
    What it never does: create a job implicitly. A name that is not in here cannot be run, which
    is the property that makes `run-once` safe to expose to an agent.
    """

    __slots__ = ("_jobs",)

    def __init__(self, jobs: Iterable[Job] = ()) -> None:
        self._jobs: dict[str, Job] = {}
        for job in jobs:
            self.register(job)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({list(self.names())!r})"

    def __iter__(self) -> Iterator[Job]:
        """Jobs in registration order — the order the scheduler adds them in."""
        return iter(self._jobs.values())

    def __len__(self) -> int:
        return len(self._jobs)

    def __contains__(self, name: object) -> bool:
        return name in self._jobs

    def register(self, job: Job) -> Job:
        """Add `job`; raise if a job of that name is already registered.

        Silently replacing would mean two schedules for one name and no way to tell which one is
        live, so a collision is an error rather than a last-writer-wins.
        """
        if job.name in self._jobs:
            raise ValueError(f"job {job.name!r} is already registered")
        self._jobs[job.name] = job
        return job

    def get(self, name: str) -> Job:
        """The job called `name`, or `JobNotRegisteredError` listing what is registered."""
        try:
            return self._jobs[name]
        except KeyError:
            known = ", ".join(self.names()) or "none"
            raise JobNotRegisteredError(
                f"no job named {name!r}; registered jobs: {known}"
            ) from None

    def names(self) -> tuple[str, ...]:
        """Registered job names, in registration order."""
        return tuple(self._jobs)


# ── the registered jobs ─────────────────────────────────────────────────────────────────────


def eod_pipeline(context: JobContext) -> None:
    """The daily end-of-day pipeline (M1.10): the latest session, fetched to PUBLISHED, archived.

    What it does: drives every daily NSE source for the latest trading session down
    `fetch → L0 → parse → L1 → sync_state`, self-heals any FAILED(retryable) date in the lookback
    window first, runs the D7 gap check, publishes the day's archive bundle, and alerts on any
    source left FAILED. It raises `EodPipelineError` when the target session did not publish, so the
    run is recorded FAILED and the next run self-heals it.
    What it assumes: the injected clock and settings are the run's (B10), the database is migrated,
    and the network is reachable — the real wiring is built inside `run_eod_pipeline`.
    What it never does: decide the time for itself, or report a green run on a day its session never
    landed. The import is deferred so this module (loaded by the scheduler) does not pull in the
    whole ingest stack at import time, and so `dataplatform.ingest.eod` can name `JobContext`
    without an import cycle.
    """
    from dataplatform.ingest.eod import run_eod_pipeline

    run_eod_pipeline(context)


#: The one job M0.6 registers (§8.1: one daily EOD pipeline). 18:30 IST on weekdays — after the
#: 15:30 close and after NSE publishes the day's bhavcopy, with the timezone supplied by the
#: scheduler from `Settings`, never assumed to be the host's.
EOD_PIPELINE = Job(
    name="eod_pipeline",
    cron="30 18 * * mon-fri",
    fn=eod_pipeline,
    timeout=timedelta(minutes=45),
    description="Daily EOD ingest → validate → normalize → publish → archive (M1.10)",
    # `eod.DAILY_NSE_SOURCES`' current-era register ids, plus the PR bundle it captures to L0.
    covers=("nse_bhavcopy_udiff", "nse_sec_bhavdata_full", "bse_bhavcopy_udiff", "nse_pr_bundle"),
    sync_sources=("nse_bhavcopy", "nse_delivery", "bse_bhavcopy"),
)


def constituents_snapshot(context: JobContext) -> None:
    """The weekly index-constituents snapshot (M10.2): the survivorship-bias killer.

    What it does: snapshots the broad and sectoral niftyindices constituent lists with this week's
    capture date, appending a dated membership record per slug so real point-in-time sector history
    accumulates going forward — niftyindices publishes "as of today" only, so a static-today map
    applied backward is survivorship-biased, and this is the mechanism that builds true history
    over time. Idempotent per (slug, week): a re-run of the same week is a no-op. One slug's fetch
    failure is journaled, alerted and skipped; it never aborts the others.
    What it assumes: the injected clock and settings are the run's (B10), the database is migrated,
    and the network is reachable — the real wiring is built inside `run_constituents_snapshot`.
    What it never does: manufacture past history, or silently fill a missed week (a gap stays a
    gap). The import is deferred so this module (loaded by the scheduler) does not pull in the
    ingest stack at import time, and so `constituents_ingest` can name `JobContext` without a cycle.
    """
    from dataplatform.ingest.constituents_snapshot_job import run_constituents_snapshot

    run_constituents_snapshot(context)


#: The weekly constituents snapshot (M10.2). 20:00 IST every Saturday — the market is closed and the
#: published lists are stable for the week, and the Saturday capture is stamped `as_of` that ISO
#: week's Sunday (`week_anchor`) so a second run of the same week is a true no-op. The timezone is
#: supplied by the scheduler from `Settings`, never the host's.
CONSTITUENTS_SNAPSHOT = Job(
    name="constituents_snapshot",
    cron="0 20 * * sat",
    fn=constituents_snapshot,
    timeout=timedelta(minutes=30),
    description="Weekly dated snapshot of index constituents — forward sector history (M10.2)",
    covers=("nifty_index_constituents",),
    sync_sources=("nifty_index_constituents",),
    max_lag_sessions=6,
)


def daily_snapshot(context: JobContext) -> None:
    """The daily market-structure snapshot (OPS): the only job here with a real deadline.

    What it does: captures every snapshot-only source into L0 under today's date — the NSE and BSE
    industry classifications, the price bands, the ASM/GSM/ESM surveillance lists, `EQUITY_L.csv`,
    `symbolchange.csv` and the index constituent lists. None of these has a past: each endpoint
    serves only its current snapshot, so every day this does not run is a day of history destroyed
    that no later effort can recover. Idempotent per (source, date) — a second run the same day
    makes zero requests. A source that fails, returns a malformed body, or answers 200 with another
    session's file is journaled to `sync_state`, alerted, and left behind while the sweep goes on.
    What it assumes: the injected clock and settings are the run's (B10), the database is migrated,
    and the network is reachable — the real wiring is built inside `run_daily_snapshot_job`.
    What it never does: fetch into a lake the operator did not declare
    (`snapshot_expect_lake_root`, asserted before the first request), capture on a day the exchange
    was shut (those are filed `GAP`), or file a payload whose own date is not today's as today's
    data. The import is deferred for the same reason the others are.
    """
    from dataplatform.ingest.daily_snapshot import run_daily_snapshot_job

    run_daily_snapshot_job(context)


_SNAPSHOT_SOURCES: tuple[str, ...] = (
    "nse_industry_classification",
    "bse_scrip_master",
    "nse_price_bands",
    "nse_asm_list",
    "nse_gsm_list",
    "nse_esm_list",
    "nse_equity_list",
    "nse_symbol_changes",
)

#: The daily snapshot (OPS). 19:15 IST Monday to Friday — after the 15:30 close and after the
#: surveillance lists and price bands for the next session are published, and comfortably clear of
#: the 18:30 EOD pipeline so the two are not competing for the same host budget. Trading days only:
#: the sweep files `GAP` for a closed day, so a *missed* day stays distinguishable from a day
#: nothing was owed on. The timezone comes from `Settings`, never the host's.
DAILY_SNAPSHOT = Job(
    name="daily_snapshot",
    cron="15 19 * * mon-fri",
    fn=daily_snapshot,
    timeout=timedelta(minutes=30),
    description="Daily capture of every snapshot-only source — the deadline job (OPS)",
    # `daily_snapshot.DEFAULT_SNAPSHOT_SET`, whose sync_state source is the register id itself.
    covers=_SNAPSHOT_SOURCES,
    sync_sources=_SNAPSHOT_SOURCES,
)


#: What the four capture jobs keep current — kept in step with `daily_capture`'s own tuples by
#: `tests/unit/test_scheduler_coverage.py`, and spelled out here so this module does not import the
#: ingest stack at load time.
_NSE_DAILY_CAPTURE_SOURCES: tuple[str, ...] = (
    "nse_fii_dii_flows",
    "nse_bulk_deals",
    "nse_block_deals",
    "nse_fo_bhavcopy",
)
_SHAREHOLDING_SOURCES: tuple[str, ...] = ("nse_shareholding_pattern",)
_ANNOUNCEMENT_SOURCES: tuple[str, ...] = ("nse_announcements", "bse_announcements")
_NEWS_SOURCES: tuple[str, ...] = ("curated_rss", "gdelt_v2_event_files")


def nse_daily_capture(context: JobContext) -> None:
    """The nightly capture of the perishable NSE end-of-day sources (ops-daily-capture).

    What it does: lands tonight's FII/DII flows, bulk deals and block deals — endpoints that serve
    the latest session only, so a night this does not run is history destroyed — and the current
    session's F&O bhavcopy, each into L0 and L1 with a `sync_state` row per (source, session).
    Each host is leased on its own, so a campaign holding the archive host costs the deals and F&O
    for the night and never the flows. See `daily_capture.run_nse_daily_capture`.
    What it assumes: the injected clock and settings are the run's (B10).
    What it never does: backfill F&O (the historical campaign owns every earlier session), publish
    an intraday copy, or file one session's payload under another session's date. The import is
    deferred for the same reason the others are.
    """
    from dataplatform.ingest.daily_capture import run_nse_daily_capture_job

    run_nse_daily_capture_job(context)


#: The nightly NSE capture. 20:00 and 23:00 IST Monday to Friday — after `daily_snapshot` (19:15,
#: 30-minute budget) has released `www.nseindia.com` and the archive host, and after the evening
#: publication of flows and deals (`daily_capture.CAPTURE_CUTOFF`, 19:30). The 23:00 fire is the
#: same-night retry for an endpoint that published late: it makes no request for a source already
#: PUBLISHED, and it is the last chance, because by the next evening the latest-only endpoints
#: have rolled. The timezone comes from `Settings`, never the host's.
NSE_DAILY_CAPTURE = Job(
    name="nse_daily_capture",
    cron="0 20,23 * * mon-fri",
    fn=nse_daily_capture,
    timeout=timedelta(minutes=30),
    description="Nightly FII/DII flows, bulk/block deals, current-session F&O → L0 + L1",
    covers=_NSE_DAILY_CAPTURE_SOURCES,
    sync_sources=_NSE_DAILY_CAPTURE_SOURCES,
)


def shareholding_poll(context: JobContext) -> None:
    """The daily poll of NSE's shareholding master (ops-daily-capture).

    What it does: one request for the master into L0, then the filing-date L1 partitions merged
    with what it carries. The endpoint has no date parameter and lists the filings of the *current*
    quarter-end only — measured 2026-10-06: 32 records, every one for 30-Sep-2026, broadcast
    01..06-Oct; the register's 2026-08-08 sample held 2,284 for the June quarter — so the previous
    quarter's list is gone the day the next quarter's first filing lands. See
    `daily_capture.run_shareholding_poll`.
    What it assumes: the injected clock and settings are the run's (B10).
    What it never does: re-date a filing or drop one an earlier poll landed.
    """
    from dataplatform.ingest.daily_capture import run_shareholding_poll_job

    run_shareholding_poll_job(context)


#: The daily shareholding poll. 18:05 IST every day — the half hour before `eod_pipeline` (18:30)
#: when nothing else holds `www.nseindia.com`, inside the campaign quiet window so no campaign can
#: hold it either. Daily because the quarter rolls over without notice: a weekly poll could miss
#: the late filings and revisions of a quarter's last week, which no later poll can recover. The
#: payload peaks near 2.4 MB at the end of a filing season. Two sessions of lag budget: a missed day
#: is recovered by the next one while the quarter lasts, so one miss is late, not lost.
SHAREHOLDING_POLL = Job(
    name="shareholding_poll",
    cron="5 18 * * *",
    fn=shareholding_poll,
    timeout=timedelta(minutes=15),
    description="Daily NSE shareholding-master poll → L0, merged into filing-date L1 partitions",
    covers=_SHAREHOLDING_SOURCES,
    sync_sources=_SHAREHOLDING_SOURCES,
    max_lag_sessions=2,
)


def announcements_capture(context: JobContext) -> None:
    """The nightly NSE + BSE corporate-announcement capture (ops-daily-capture).

    What it does: lands the previous calendar day — complete, since it runs after midnight — from
    both exchanges into one L1 partition, BSE paged by its own row count and resolved scrip→ISIN
    through the D2 master; and re-drives any day of the last week not yet PUBLISHED. See
    `daily_capture.run_announcements_capture`.
    What it assumes: the injected clock and settings are the run's (B10).
    What it never does: backfill past its seven-day window (the merger-terms campaign fetches
    per-symbol history on demand), or let one exchange's write erase the other's rows.
    """
    from dataplatform.ingest.daily_capture import run_announcements_capture_job

    run_announcements_capture_job(context)


#: The nightly announcement capture. 00:30 IST every day — announcements are filed on weekends
#: too, the day before is complete, and no other job holds `www.nseindia.com` or `api.bseindia.com`
#: then (the monthly BSE sweep starts at 06:00 on its Sunday). Two sessions of lag budget: the
#: logical date is a calendar day, so between midnight and this fire yesterday is owed but not due.
ANNOUNCEMENTS_CAPTURE = Job(
    name="announcements_capture",
    cron="30 0 * * *",
    fn=announcements_capture,
    timeout=timedelta(minutes=45),
    description="Nightly NSE + BSE announcements for the previous day → L0 + L1 (7-day self-heal)",
    covers=_ANNOUNCEMENT_SOURCES,
    sync_sources=_ANNOUNCEMENT_SOURCES,
    max_lag_sessions=2,
)


def news_capture(context: JobContext) -> None:
    """The six-hourly news poll: the ratified curated RSS feeds and one GDELT export slot.

    What it does: each active feed whose register row is VERIFIED, once, and the GDELT slot the
    manifest names, into L0; then today's L1 `news` partition re-derived from every news payload of
    the day. See `daily_capture.run_news_capture`.
    What it assumes: the injected clock and settings are the run's (B10).
    What it never does: backfill GDELT, add a feed the register has not verified, or fetch the
    mentions/GKG files.
    """
    from dataplatform.ingest.daily_capture import run_news_capture_job

    run_news_capture_job(context)


#: The news poll. 00:15, 06:15, 12:15 and 18:15 IST — no NSE or BSE host is touched, so it does
#: not compete with any exchange job. Four GDELT slots a day, export files only (~75-100 kB each):
#: GDELT is evidence-only and measured near-empty of India-finance content, so a six-hourly sample
#: of global attention is what it is worth, at about 1/24 of the 96-slot firehose. Four RBI polls
#: cover its ten-item feed with room to spare.
NEWS_CAPTURE = Job(
    name="news_capture",
    cron="15 0,6,12,18 * * *",
    fn=news_capture,
    timeout=timedelta(minutes=15),
    description="Six-hourly curated RSS + one GDELT export slot → L0 + L1 news",
    covers=_NEWS_SOURCES,
    sync_sources=_NEWS_SOURCES,
)


def l0_verify(context: JobContext) -> None:
    """The weekly L0 integrity sweep (2026-09-06 audit, finding N6).

    What it does: re-hashes stored payloads against their sidecars and raises an ERROR
    `quality_flag` plus a CRITICAL alert for every one that does not match. Rolling over a trailing
    window most weeks, and over the whole lake in the first week of each month — L0 is write-once,
    so what changes is what was written since, but bit-rot in the old tail is exactly what nothing
    else would ever read.
    What it assumes: the injected clock and settings are the run's (B10) and the database is
    migrated.
    What it never does: repair. `L0Store` refuses to modify a stored payload for any reason,
    including corruption (AGENTIC_CONTEXT §3.10); a defect is a human's call, and re-fetching over
    it would destroy the evidence. The import is deferred for the same reason the others are.
    """
    from dataplatform.store.db import connection
    from dataplatform.store.l0_verify import run_l0_verify

    with connection(context.settings) as conn:
        result = run_l0_verify(conn=conn, settings=context.settings, clock=context.clock)
        conn.commit()
    if not result.ok:
        raise RuntimeError(result.summary())


#: The weekly L0 sweep. 03:00 IST on Sunday — no market, no campaign window, and the quietest hour
#: for a pass that reads gigabytes off disk. The timezone comes from `Settings`, never the host's.
L0_VERIFY = Job(
    name="l0_verify",
    cron="0 3 * * sun",
    fn=l0_verify,
    timeout=timedelta(hours=2),
    description="Weekly L0 checksum sweep; full pass in the first week of each month",
)


def identity_refresh(context: JobContext) -> None:
    """The weekly identity-master refresh (2026-09-06 audit, finding N4).

    What it does: fetches `EQUITY_L.csv` and `symbolchange.csv` into L0 under the host lease, then
    re-derives `security_master`, `symbol_history` and `exchange_listing` by reading both payloads
    back out of the lake. `ops/runbooks/identity-master.md` has said "weekly" since M1.7; nothing
    did it weekly, and until this job the two files had never been fetched at all — the production
    master was built from `tests/fixtures/`, outside L0's checksums and outside the backup.
    What it assumes: the injected clock and settings are the run's (B10). A re-run on the same day
    re-fetches nothing, because L0 already holds that date's payloads.
    What it never does: overwrite a snapshot. Every week's copy is kept, which is the only way the
    accumulated series can ever reconstruct the symbol history one snapshot cannot show
    (`pit_notes` on both register rows).
    """
    from dataplatform.ingest.identity_refresh import refresh_identity
    from dataplatform.store.db import connection

    with connection(context.settings) as conn:
        report = refresh_identity(conn=conn, clock=context.clock, settings=context.settings)
        conn.commit()
    if not report.ingest.is_clean:
        raise RuntimeError(report.summary())


#: The weekly identity refresh. 07:00 IST on Saturday — after the week's last session, before the
#: constituents snapshot at 20:00, and well clear of any weekday campaign window. The timezone
#: comes from `Settings`, never the host's.
IDENTITY_REFRESH = Job(
    name="identity_refresh",
    cron="0 7 * * sat",
    fn=identity_refresh,
    timeout=timedelta(minutes=15),
    description="Weekly NSE identity-master refresh: fetch to L0, re-derive from L0 (M1.7)",
    covers=("nse_equity_list", "nse_symbol_changes"),
)


def tri_refresh(context: JobContext) -> None:
    """The weekly benchmark-TRI refresh (2026-10-05 audit): every default index brought current.

    What it does: one whole-history POST per index whose published L1 series no longer reaches the
    last session before today, and nothing for an index already current — see
    `tri_backfill.run_tri_refresh`. Until this job the TRI was a one-shot campaign whose resume
    check looked only at the series' *start*, so NIFTY 50, IT and CPSE froze at the day they were
    first fetched.
    What it assumes: the injected clock and settings are the run's (B10).
    What it never does: touch a host other than niftyindices.com. The import is deferred for the
    same reason the others are.
    """
    from dataplatform.ingest.tri_backfill import run_tri_refresh

    run_tri_refresh(context)


#: The weekly TRI refresh. 08:00 IST on Saturday — the week's last level has been disseminated,
#: and one ~1 MB payload per index per week keeps L0 growth honest for a series a backtest reads at
#: weekly-or-coarser resolution. Weekly rather than daily is why its lag budget is six sessions.
TRI_REFRESH = Job(
    name="tri_refresh",
    cron="0 8 * * sat",
    fn=tri_refresh,
    timeout=timedelta(minutes=15),
    description="Weekly benchmark TRI refresh for the default index set (M3.9.b)",
    covers=("nifty_tri_history",),
    sync_sources=("nifty_tri_history",),
    max_lag_sessions=6,
)


def index_press_refresh(context: JobContext) -> None:
    """The weekly index-change announcement capture (DQ-5): new releases into L0, nothing else.

    What it does: fetches the niftyindices.com press-release listing, the seven tracked indices'
    anchor CSVs and every candidate change release of the last 120 days not yet in L0 — see
    `index_history_backfill.run_press_release_refresh`. NSE Indices publishes its semi-annual
    reviews and ad-hoc replacements there days to weeks before they take effect, so a weekly pass
    misses nothing and leaves every release in L0 before its change is effective.
    What it assumes: the injected clock and settings are the run's (B10).
    What it never does: rebuild the membership history in L1, or backfill pre-window releases (the
    owner-gated campaign). The import is deferred for the same reason the others are.
    """
    from dataplatform.ingest.index_history_backfill import run_press_release_refresh

    run_press_release_refresh(context)


#: The weekly announcement capture. 09:00 IST on Saturday — after `tri_refresh` (08:00, 15-minute
#: budget) on the same host, because a host lease is refused rather than queued, and well before the
#: 20:00 constituents snapshot there. No `sync_sources`: each release is its own sync row dated by
#: its announcement, so a session-lag budget would measure nothing.
INDEX_PRESS_REFRESH = Job(
    name="index_press_refresh",
    cron="0 9 * * sat",
    fn=index_press_refresh,
    timeout=timedelta(minutes=20),
    description="Weekly capture of NSE Indices change announcements into L0 (DQ-5)",
    covers=("nifty_index_press_releases",),
)


def ca_refresh(context: JobContext) -> None:
    """The weekly corporate-action refresh (DQ): new actions in, changed chains and L2 rebuilt.

    What it does: one NSE request over the last five weeks of ex-dates, one BSE request per scrip
    whose NSE action has no BSE twin yet, then the backfill's reconcile under the lake's own ACCEPT
    policy, a recompute of only the ISINs whose reconciled actions moved, and a drain of the
    `l2_invalidation` queue that recompute raised — see `ca_refresh.refresh_corporate_actions`.
    Until this job the CA store was a one-shot campaign that stopped at 2026-09-01, and two
    September 2:1 splits reached L2 only through the price-implied detector.
    What it assumes: the injected clock and settings are the run's (B10).
    What it never does: recompute an ISIN whose chain did not change, or re-run a refresh already
    PUBLISHED for today. The import is deferred for the same reason the others are.
    """
    from dataplatform.ingest.ca_refresh import run_ca_refresh

    run_ca_refresh(context)


#: The weekly CA refresh. 10:00 IST on Saturday — the week's ex-dates are all in, and it follows
#: `identity_refresh` (07:00) so a name listed this week resolves. Weekly is enough for a factor
#: chain: the implied-split detector already keeps an unrecorded split out of L2 in the meantime,
#: and this job replaces that inference with the published record. Six sessions of lag budget.
CA_REFRESH = Job(
    name="ca_refresh",
    cron="0 10 * * sat",
    fn=ca_refresh,
    timeout=timedelta(hours=1),
    description="Weekly NSE CA refresh + BSE counterparts → recompute changed chains → rebuild L2",
    covers=("nse_corp_actions",),
    sync_sources=("nse_corp_actions",),
    max_lag_sessions=6,
)


def bse_ca_sweep(context: JobContext) -> None:
    """The monthly BSE corporate-action sweep: every BSE scrip that traded in the last year.

    What it does: `ca_refresh`'s run, plus one request per BSE scrip with a price in the trailing
    year — which is what reaches a BSE-only listing, whose actions the NSE feed never carries. The
    finalize, the changed-only recompute and the L2 drain are the weekly job's.
    What it assumes: the injected clock and settings are the run's (B10).
    What it never does: re-fetch a scrip already PUBLISHED for today. The import is deferred for the
    same reason the others are.
    """
    from dataplatform.ingest.ca_refresh import run_bse_ca_sweep

    run_bse_ca_sweep(context)


#: The monthly BSE sweep. 06:00 IST on the first Sunday of the month (APScheduler ANDs the day and
#: weekday fields): ~6,700 per-scrip requests at the host's spacing is most of a day's budget, so it
#: runs where no session, no EOD pipeline and no snapshot competes for the BSE host, after the
#: 03:00 L0 sweep. The lag budget spans the longest gap between first Sundays (35 days).
BSE_CA_SWEEP = Job(
    name="bse_ca_sweep",
    cron="0 6 1-7 * sun",
    fn=bse_ca_sweep,
    timeout=timedelta(hours=10),
    description="Monthly per-scrip BSE CA sweep of every traded scrip → recompute → rebuild L2",
    covers=("bse_corp_actions",),
    sync_sources=("bse_corp_actions",),
    max_lag_sessions=27,
)


def fbil_reference_rates(context: JobContext) -> None:
    """The daily FBIL reference-rate capture (macro-probes): USD/INR and the other INR benchmarks.

    What it does: one request for FBIL's archive over the trailing three weeks — wide enough for
    the public site's few-session lag — into L0, then one `macro_series` release per publication
    date (`macro.capture.run_fbil_capture`). A window already in L0 costs no request.
    What it assumes: the injected clock and settings are the run's (B10).
    What it never does: date a rate by the fetch; the benchmark's own `displayTime` dates it. The
    import is deferred for the same reason the others are.
    """
    from dataplatform.ingest.macro.capture import run_fbil_capture

    run_fbil_capture(context)


#: 16:00 IST on weekdays — after FBIL's 13:00 publication; no NSE host, so clear of every campaign
#: window by construction.
FBIL_REFERENCE_RATES = Job(
    name="fbil_reference_rates",
    cron="0 16 * * mon-fri",
    fn=fbil_reference_rates,
    timeout=timedelta(minutes=10),
    description="Daily FBIL INR reference rates (trailing 3 weeks) → macro_series (macro-probes)",
    covers=("fbil_reference_rates",),
)


def macro_release_capture(context: JobContext) -> None:
    """The weekly macro forward capture (macro-probes, Tier B of the 2026-09-07 macro plan).

    What it does: World Bank indicators (dated by the envelope's `lastupdated`), the RBI's "Current
    Rates" panel, the OEA's WPI file, GSTN's collection workbook and the trailing month of India
    VIX spot, each under its own host lease, each written to `macro_series` — current-vintage
    tables only where a value is new or revised (`macro.capture.run_macro_release_capture`). One
    step failing is logged and the others still run; the job then raises naming every failure.
    What it assumes: the injected clock and settings are the run's (B10).
    What it never does: back-date a current-vintage figure: the Tier B series are knowable from
    the capture on, which is the whole point of starting them now. The import is deferred.
    """
    from dataplatform.ingest.macro.capture import run_macro_release_capture

    run_macro_release_capture(context)


#: 10:00 IST on Sunday — no session, after the 03:00 L0 sweep, and on a day no niftyindices.com job
#: holds that host's lease (the Saturday jobs do). No `sync_sources`: Tier B releases are monthly or
#: irregular, so a session-lag budget would measure nothing.
MACRO_RELEASE_CAPTURE = Job(
    name="macro_release_capture",
    cron="0 10 * * sun",
    fn=macro_release_capture,
    timeout=timedelta(minutes=20),
    description="Weekly macro forward capture: World Bank, RBI rates, WPI, GST, India VIX",
    covers=(
        "worldbank_indicator_api",
        "rbi_current_rates",
        "oea_wpi_monthly_index",
        "gstn_tax_collection",
        "nifty_india_vix_history",
    ),
)


def paper_session(context: JobContext) -> None:
    """The daily paper-trading session (M13.1): one session of the D13-ratified momentum v2 book.

    What it does: decides today's session of the paper book through the same replay-engine →
    rails → `SimBroker` path its backtests ran on, journals every decision including the no-ops,
    and records the session in `paper_session` — or, when the data is red, journals
    `SKIPPED_DATA_RED` and places nothing. Idempotent per trading date; a holiday is a no-op.
    What it assumes: the injected clock and settings are the run's (B10), the database is migrated
    through 0012, and the owed session's EOD pipeline has run — the interlock checks it published.
    Off unless `Settings.paper_session_enabled`: the ratified regime filter has no same-evening
    source for the session's published NIFTY 50 TRI yet (ops/runbooks/daily-eod.md).
    What it never does: touch a real broker — the session builds a `SimBroker` and nothing else,
    and `execution.kite_broker` is not imported on this path. The import is deferred like the
    others', so loading the registry does not pull in the backtest stack.
    """
    from backtest.paper_session import run_paper_session_job

    run_paper_session_job(context)


#: The paper session (M13.1). 20:30 IST Monday to Friday — after the 18:30 EOD pipeline's 45-minute
#: budget has run out, so the session's prices have published or the interlock says why not. It
#: reads the lake and Postgres only and fetches nothing, so it holds no host lease. Holidays are
#: skipped inside the job against the holiday calendar. Registered but a no-op until
#: PAPER_SESSION_ENABLED is set (see `paper_session`).
PAPER_SESSION = Job(
    name="paper_session",
    cron="30 20 * * mon-fri",
    fn=paper_session,
    timeout=timedelta(minutes=30),
    description=(
        "Daily paper-trading session of the D13-ratified momentum v2 book (M13.1); "
        "a no-op until PAPER_SESSION_ENABLED=true"
    ),
)


#: Every live Source Register row that no registered job keeps current, and why. The 2026-10-05
#: audit's root cause was not one broken job but sources that were simply never scheduled — the
#: register said `cadence: daily` and nothing ran them. A source belongs here only with a reason a
#: reviewer can check; `test_scheduler_registry` fails for a live row in neither place, and
#: `/status/jobs` serves this ledger so the gap is visible where operators look.
UNSCHEDULED: dict[str, str] = {
    "nse_mto": (
        "Superseded from 2019-09-30 by nse_sec_bhavdata_full; the delivery source set fetches MTO "
        "only for older sessions, so there is nothing new to take daily."
    ),
    "nse_financial_results_index": (
        "fundamentals_backfill campaign (B1 NEEDS_GO: thousands of per-filing requests); no "
        "incremental daily job yet."
    ),
    "nse_integrated_filing_index": "Same as nse_financial_results_index.",
    "nse_xbrl_filing": "Same as nse_financial_results_index.",
    "nifty_index_close_snapshot": (
        "Input to the computed TRI fallback only; the published TRI is live (tri_refresh)."
    ),
    "nse_index_close_snapshot": (
        "History via the M11.2 valuation backfill campaign; the daily valuation job is not wired "
        "yet (no consumer in the decision path)."
    ),
    "nse_announcement_attachment": (
        "Per-filing documents fetched on demand by the merger-terms campaign (M3.8); no job yet."
    ),
    "gdelt_doc_api": "Register status FAILED; nothing to schedule until it verifies.",
    "alfred_series_vintage": "Register status FAILED; nothing to schedule until it verifies.",
    "mospi_api": (
        "Register status FAILED (TLS needs unsafe legacy renegotiation); not worked around."
    ),
    "rbi_dbie": "Register status FAILED (certificate hostname mismatch); not worked around.",
    "screener_company_fundamentals": "Register status BLOCKED_CREDENTIAL.",
}


def lag_budgets(registry: JobRegistry) -> dict[str, int]:
    """`sync_state` source → the sessions it may fall behind, from every job that answers for it.

    Two jobs answering for one source is legal (a weekly and a daily refresh, say); the tighter
    budget wins, because the source is owed by whichever is due sooner.
    """
    budgets: dict[str, int] = {}
    for job in registry:
        for source in job.sync_sources:
            budgets[source] = min(budgets.get(source, job.max_lag_sessions), job.max_lag_sessions)
    return budgets


def default_registry() -> JobRegistry:
    """The registry a production scheduler process runs.

    A fresh object each call rather than a module-level singleton: two processes in one test, or a
    test that registers an extra job, must not be able to mutate what the next one sees.
    """
    return JobRegistry(
        [
            EOD_PIPELINE,
            DAILY_SNAPSHOT,
            CONSTITUENTS_SNAPSHOT,
            L0_VERIFY,
            IDENTITY_REFRESH,
            TRI_REFRESH,
            INDEX_PRESS_REFRESH,
            CA_REFRESH,
            BSE_CA_SWEEP,
            FBIL_REFERENCE_RATES,
            MACRO_RELEASE_CAPTURE,
            NSE_DAILY_CAPTURE,
            SHAREHOLDING_POLL,
            ANNOUNCEMENTS_CAPTURE,
            NEWS_CAPTURE,
            PAPER_SESSION,
        ]
    )
