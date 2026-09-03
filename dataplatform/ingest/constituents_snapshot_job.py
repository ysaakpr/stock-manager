"""Forward sector-history accumulation — the weekly constituents snapshot job (M10.2).

niftyindices publishes each index's constituents *"as of today"* only: there is no download of who
was in NIFTY BANK or NIFTY IT in a past year. A static-today membership map applied backward is
therefore survivorship-biased — it silently assumes today's members were always the members — which
is exactly the caveat M4's PIT universe (invariant #7) exists to defeat. The only honest cure is to
*start capturing now*: snapshot the lists with a capture date, week after week, so real
point-in-time sector history accumulates going forward. This module is that mechanism.

M10.1 already built the sweep — `run_constituents_ingest` drives every configured `IndexSpec` down
`fetch → L0 → parse → L1 → sync` for one `as_of`, idempotent per `(slug, as_of)`, parking (and
journaling) one slug's failure without aborting the rest. What M10.1 did *not* have is the two
things that make it accrue history unattended, and those are this task:

* **A weekly schedule.** `constituents_snapshot` is registered in the M0.6 job registry on a weekly
  cron; each firing appends that week's dated membership record for every configured slug. History
  is not manufactured — the job only ever writes *this* week's list, dated now — it accrues.

* **Per-*week* idempotency, and alerting on failure.** The snapshot is stamped `week_anchor(today)`
  — the ISO week's Sunday — so every run inside one week resolves to the same `as_of`. M10.1's
  per-`(slug, as_of)` resume check then turns any second run of the same week (a manual re-run, a
  scheduler double-fire) into a true no-op: nothing is fetched, nothing is written. A missed week
  leaves its anchor with no partition — a *visible* gap D7 can see, never a silently back-filled
  one. And a slug that fails is not only journaled (M10.1 writes its FAILED `sync_state` row and the
  structlog event) but **alerted**, one alert per parked slug, while the sweep goes on.

Why the week's *end* (Sunday) and not its start (Monday) as the anchor: the job captures on its
scheduled day (a Saturday) and stamps the capture this anchor. Anchoring to the Sunday keeps the
stamp on-or-after the capture, so `membership_asof` can never return a membership captured *after*
the decision date it is queried for — the no-future-data invariant (#7), which is the whole point of
this survivorship-bias-killer task. A Monday anchor would label a Saturday capture five days in its
own past and leak that week's list into earlier-in-the-week decisions.

Offline by construction (B8): `run_weekly_snapshot` is the injectable core a test drives with a
`RecordedTransport`, a scratch database and a `tmp_path` lake; `run_constituents_snapshot` is the
thin scheduler entry point that builds the real networked wiring, exactly as `run_eod_pipeline`
(M1.10) builds its own. The clock is injected (B10); joins remain on ISIN (#2), inherited from the
snapshot the sweep writes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from dataplatform.alerts import Alerter, AlertOutcome, Severity, build_alerter
from dataplatform.clock import Clock
from dataplatform.config import Settings
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.constituents_ingest import (
    DEFAULT_INDEX_SET,
    CoverageReport,
    IndexSpec,
    SlugOutcome,
    run_constituents_ingest,
)
from dataplatform.ingest.fetcher import Fetcher, build_fetcher
from dataplatform.ingest.indices import CONSTITUENTS_SOURCE_ID, SyncTracker
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Store

if TYPE_CHECKING:  # imported lazily by the registry to avoid a scheduler→ingest import cycle
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "ConstituentsSnapshotError",
    "run_constituents_snapshot",
    "run_weekly_snapshot",
    "week_anchor",
]

_LOG = get_logger(__name__)


class ConstituentsSnapshotError(RuntimeError):
    """The weekly snapshot job produced no queryable snapshot at all — a total outage.

    Raised only when *every* configured slug failed, so nothing landed for the week. A single slug
    parking is not this: that slug is alerted and left as a visible gap, and the sweep goes on
    (acceptance 3). This exception exists so the scheduler records the run FAILED and the next
    week's run is the self-heal — a week with a partial map is a green run with alerts, but a week
    with no map at all is a failure the operator must see.
    """


def week_anchor(on: date) -> date:
    """The canonical snapshot date for the ISO week containing `on` — that week's Sunday.

    Why anchor at all: it is what makes the job idempotent per *week* rather than per *day*. Every
    run inside one ISO week resolves to the same anchor, so M10.1's per-`(slug, as_of)` resume check
    turns any second run of the same week — a manual re-run, a scheduler double-fire — into a true
    no-op. A missed week leaves its anchor with no partition: a visible gap, never a silent fill.

    Why the week's *end* (Sunday) and not its start: the job captures on its scheduled day (a
    Saturday) and stamps it this anchor. Anchoring to the Sunday keeps the stamp on-or-after the
    capture, so `membership_asof` can never hand back a membership captured *after* the decision
    date it is queried for — the no-future-data invariant (#7). A Monday anchor would label a
    Saturday capture five days in its own past.
    """
    iso = on.isocalendar()
    return date.fromisocalendar(iso.year, iso.week, 7)


def run_weekly_snapshot(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SyncTracker,
    alerter: Alerter,
    as_of: date,
    commit: Callable[[], None] = lambda: None,
    specs: tuple[IndexSpec, ...] = DEFAULT_INDEX_SET,
    data_root: Path | None = None,
    register: SourceRegister | None = None,
) -> CoverageReport:
    """One weekly snapshot sweep, offline-testable: append a dated snapshot per slug for `as_of`.

    The injectable core of the scheduled job. For each configured slug it drives M10.1's
    single-index `run_constituents_ingest` for the week anchor `as_of`, committing after each so a
    kill loses at most the slug in flight; a slug already present for this `(slug, as_of)` is
    skipped without a fetch (the resume path that makes a same-week re-run a no-op), and a slug that
    fails is parked with its enumerated cause and *alerted* — one alert per parked slug — while the
    sweep continues (acceptance 3). Returns the `CoverageReport`; it raises for no per-slug failure.

    `as_of` is expected to be a `week_anchor` value; the function does not impose that, so a caller
    can snapshot an explicit date, but the scheduled job always passes the anchor.
    """
    outcomes: list[SlugOutcome] = []
    for spec in specs:
        single = run_constituents_ingest(
            fetcher=fetcher,
            l0=l0,
            tracker=tracker,
            as_of=as_of,
            specs=(spec,),
            data_root=data_root,
            register=register,
        )
        outcomes.extend(single.outcomes)
        commit()

    report = CoverageReport(as_of=as_of, outcomes=tuple(outcomes))
    alerts_sent = _emit_snapshot_alerts(report, alerter)
    commit()
    _LOG.info(
        "constituents.weekly_snapshot_done",
        source=CONSTITUENTS_SOURCE_ID,
        week=as_of.isoformat(),
        published=len(report.published),
        skipped=len(report.skipped),
        parked=len(report.parked),
        alerts_sent=alerts_sent,
    )
    return report


def _emit_snapshot_alerts(report: CoverageReport, alerter: Alerter) -> int:
    """Alert once per parked slug; return how many were actually sent (not deduplicated away).

    The dedup key is stable per `(slug, week, cause)` (§8.1 alerting): a slug that stays gated for
    weeks is one piece of news per week, not one per re-run of the same week. A parked slug is
    already journaled — M10.1 wrote its FAILED `sync_state` row and its structlog event — so this
    adds the *alert* half of acceptance 3 on top of the journal M10.1 already writes.
    """
    sent = 0
    for outcome in report.parked:
        result = alerter.send(
            Severity.WARNING,
            f"Constituents snapshot: {outcome.spec.slug} parked for week "
            f"{report.as_of.isoformat()}",
            f"The weekly constituents snapshot could not ingest {outcome.spec.name} "
            f"({outcome.spec.slug}) for the week of {report.as_of.isoformat()}: "
            f"{outcome.cause} — {outcome.detail}. The other slugs were unaffected; this week is a "
            f"visible gap for {outcome.spec.slug} until a later run lands it.",
            f"constituents:{outcome.spec.slug}:{report.as_of.isoformat()}:{outcome.cause}",
        )
        sent += int(result == AlertOutcome.SENT)
    return sent


def run_constituents_snapshot(context: JobContext) -> None:
    """The scheduler's `constituents_snapshot` job body — build real wiring and snapshot the week.

    What it does: from the job's injected clock and settings (B10), computes the current ISO week's
    anchor, builds the networked fetcher, the L0 store, the configured alerter and a database
    connection, and runs `run_weekly_snapshot` over the default index set — appending a dated
    membership record per slug so real point-in-time sector history accrues going forward. Commits
    per slug so the run is a checkpoint.
    What it assumes: the database is migrated and reachable and the network is up — the real wiring
    is built here, exactly as `run_eod_pipeline` builds its own (M1.10).
    What it never does: manufacture past history (it only ever writes *this* week's list, dated
    now), silently fill a missed week, or abort the sweep on one slug's failure. It raises
    `ConstituentsSnapshotError` only when the whole sweep landed nothing, so a total outage is
    recorded FAILED and the next week self-heals; a partial map is a green run with per-slug alerts.
    """
    settings: Settings = context.settings
    clock: Clock = context.clock
    register = load_register()
    as_of = week_anchor(clock.now().date())

    fetcher = build_fetcher(clock=clock, settings=settings, register=register)
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    alerter = build_alerter(settings, clock=clock)
    calendar = trading_calendar()

    with connection(settings) as conn:
        sync = SyncStateStore(conn, clock=clock, calendar=calendar)
        report = run_weekly_snapshot(
            fetcher=fetcher,
            l0=l0,
            tracker=sync,
            alerter=alerter,
            as_of=as_of,
            commit=conn.commit,
            data_root=settings.data_root,
            register=register,
        )

    if not report.covered:
        raise ConstituentsSnapshotError(
            f"weekly constituents snapshot for week {as_of.isoformat()} landed no slug at all "
            f"({len(report.parked)} parked); recorded FAILED for the next run to self-heal. "
            "See the per-slug alerts and /status/sync."
        )
