"""Index-membership history campaign: fetch the change announcements, then rebuild the history.

Two halves, run in this order and separable on purpose:

1. **Fetch** (`run_press_release_campaign`) — one capture of the press-release listing, one fresh
   constituents CSV per tracked index (the reconciliation anchors), then the candidate release
   PDFs newest first until the request budget is spent. Newest first because the history is
   reconstructed *backward* from today's list: depth is only as good as the contiguous run of
   releases from the present back, and a gap in the middle would cut it anyway. Every byte lands
   in L0 through the crawl engine (`nifty_index_press_releases`, `nifty_index_constituents`); a
   release already in L0 is never re-requested, so a re-run spends only what the last one left.
2. **Build** (`index_history.build_membership_history`) — offline, from L0 and L1 alone.

**The budget is the owner's.** AGENTIC_CONTEXT §3.3 reserves any campaign over ~200 requests to one
source to the human; `--max-releases` defaults under that line with the listing and anchors counted
in, and the report states how far back the budget reached, so going deeper is a decision with its
cost in front of it rather than a default.

`main` is the only place that builds the real networked wiring, under a `niftyindices.com` host
lease so no second driver shares the request budget (B8). Clock injected (B10).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.fetcher import (
    Fetcher,
    FetchError,
    FetchHTTPError,
    ForbiddenSpikeError,
    leased_fetcher,
)
from dataplatform.ingest.index_changes import (
    PRESS_RELEASE_SOURCE_ID,
    TRACKED_INDICES,
    PressRelease,
    candidate_releases,
    fetch_listing,
    fetch_press_release,
    l0_listing_filename,
    parse_press_release_listing,
)
from dataplatform.ingest.indices import (
    CONSTITUENTS_SOURCE_ID,
    SyncTracker,
    constituents_url,
    l0_constituents_filename,
)
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Store

if TYPE_CHECKING:  # imported lazily by the registry to avoid a scheduler→ingest import cycle
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "DEFAULT_MAX_RELEASES",
    "REFRESH_MAX_RELEASES",
    "REFRESH_WINDOW",
    "CampaignReport",
    "release_state_source",
    "run_press_release_campaign",
    "run_press_release_refresh",
]

_LOG = get_logger(__name__)

#: Release PDFs one run may request. With the listing and the seven anchors that is 184 requests to
#: niftyindices.com — under the ~200 line AGENTIC_CONTEXT §3.3 reserves to the owner.
DEFAULT_MAX_RELEASES: Final = 175

#: The host every request of this campaign goes to — the one lease `main` takes.
HOST: Final = "niftyindices.com"

#: The weekly refresh only looks back this far. NSE Indices announces a change days to weeks before
#: it takes effect, so a week's new releases are always inside it — and a window, not "everything
#: missing", is what keeps the scheduled job from turning into the owner-gated backfill of the ~250
#: pre-2018 releases (AGENTIC_CONTEXT §3.3) the first time it runs.
REFRESH_WINDOW: Final = timedelta(days=120)

#: A hard ceiling on release PDFs per refresh run. A normal week brings 0-5; the semi-annual review
#: week a handful more. Hitting the ceiling stops the run short rather than spending past it.
REFRESH_MAX_RELEASES: Final = 25


def release_state_source(release: PressRelease) -> str:
    """The sync-state source of one release — `nifty_index_press_releases/<filename stem>`.

    One register row serves every release, and several releases share an announcement date, so the
    filename is the unit (see `indices.constituents_state_source` for the same reasoning).
    """
    return f"{PRESS_RELEASE_SOURCE_ID}/{release.filename.removesuffix('.pdf')}"


@dataclass(frozen=True, slots=True)
class CampaignReport:
    """What one fetch run did — requests made, releases landed, and how far back it reached."""

    as_of: date
    requests: int
    candidates: int
    fetched: tuple[str, ...]
    already_in_l0: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]
    anchors: tuple[str, ...]
    oldest_contiguous: date | None
    budget_exhausted: bool
    unfetched: tuple[str, ...] = field(default=())


def run_press_release_campaign(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SyncTracker,
    as_of: date,
    max_releases: int = DEFAULT_MAX_RELEASES,
    since: date | None = None,
) -> CampaignReport:
    """Fetch the listing, the anchors and the candidate releases (newest first) into L0.

    What it does: captures the listing for `as_of` (or reuses the capture already in L0), fetches
    each tracked index's constituents CSV for `as_of` as the reconciliation anchor, then walks the
    membership-candidate releases from newest to oldest — skipping those already in L0, stopping
    when `max_releases` new requests have been made or `since` is passed.
    What it assumes: `fetcher` is wired to the networked transport under a host lease (or to a
    recorded one in a test); the caller commits `tracker` between calls if it is transactional.
    What it never does: re-request a payload already in L0, or keep going after a 403 spike — a
    `ForbiddenSpikeError` propagates, because the host has told us to stop.

    A release that fails (404, transport) is recorded `FAILED` on its sync row, reported, and
    counted against `oldest_contiguous` — the depth is the run of releases fetched without a gap.
    """
    requests = 0
    listing_name = l0_listing_filename(as_of)
    if l0.exists(PRESS_RELEASE_SOURCE_ID, as_of, listing_name):
        listing_ref = l0.ref_for(PRESS_RELEASE_SOURCE_ID, as_of, listing_name)
    else:
        listing_ref = fetch_listing(fetcher, as_of=as_of)
        requests += 1
    releases = parse_press_release_listing(l0.get(listing_ref), filename=listing_ref.filename)
    candidates = candidate_releases(releases)

    anchors: list[str] = []
    for slug in TRACKED_INDICES:
        name = l0_constituents_filename(slug, as_of)
        if not l0.exists(CONSTITUENTS_SOURCE_ID, as_of, name):
            fetcher.fetch(CONSTITUENTS_SOURCE_ID, constituents_url(slug), as_of, filename=name)
            requests += 1
        anchors.append(name)

    fetched: list[str] = []
    present: list[str] = []
    failed: list[tuple[str, str]] = []
    unfetched: list[str] = []
    oldest_contiguous: date | None = None
    contiguous = True
    exhausted = False
    for release in candidates:
        if since is not None and release.announced < since:
            break
        if l0.exists(PRESS_RELEASE_SOURCE_ID, release.announced, release.filename):
            present.append(release.filename)
            if contiguous:
                oldest_contiguous = release.announced
            continue
        if len(fetched) + len(failed) >= max_releases:
            exhausted = True
            unfetched.append(release.filename)
            continue
        state_source = release_state_source(release)
        tracker.begin(state_source, release.announced)
        try:
            ref = fetch_press_release(fetcher, release)
        except ForbiddenSpikeError:
            tracker.mark_failed(state_source, release.announced, "403 spike", retryable=True)
            raise
        except (FetchHTTPError, FetchError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
            tracker.mark_failed(state_source, release.announced, detail, retryable=True)
            failed.append((release.filename, detail))
            contiguous = False
            requests += 1
            _LOG.error(
                "index_history.release_fetch_failed",
                source=PRESS_RELEASE_SOURCE_ID,
                filename=release.filename,
                announced=release.announced.isoformat(),
                error=detail,
                state="FAILED",
            )
            continue
        requests += 1
        tracker.mark_fetched(state_source, release.announced, checksum=ref.sha256, l0_path=ref.key)
        fetched.append(release.filename)
        if contiguous:
            oldest_contiguous = release.announced
        _LOG.info(
            "index_history.release_fetched",
            source=PRESS_RELEASE_SOURCE_ID,
            filename=release.filename,
            announced=release.announced.isoformat(),
            size_bytes=ref.size_bytes,
            requests=requests,
            state="FETCHED",
        )

    report = CampaignReport(
        as_of=as_of,
        requests=requests,
        candidates=len(candidates),
        fetched=tuple(fetched),
        already_in_l0=tuple(present),
        failed=tuple(failed),
        anchors=tuple(anchors),
        oldest_contiguous=oldest_contiguous,
        budget_exhausted=exhausted,
        unfetched=tuple(unfetched),
    )
    _LOG.info(
        "index_history.campaign_done",
        source=PRESS_RELEASE_SOURCE_ID,
        as_of=as_of.isoformat(),
        requests=requests,
        fetched=len(fetched),
        already_in_l0=len(present),
        failed=len(failed),
        unfetched=len(unfetched),
        oldest_contiguous=None if oldest_contiguous is None else oldest_contiguous.isoformat(),
    )
    return report


def run_press_release_refresh(context: JobContext) -> None:
    """The scheduler's `index_press_refresh` job body: this week's change releases, into L0.

    What it does: under the `niftyindices.com` lease, runs `run_press_release_campaign` for the
    job's own date (the injected clock, B10) — one listing capture, the seven anchor CSVs, and any
    candidate release announced in the last `REFRESH_WINDOW` that L0 does not already hold, capped
    at `REFRESH_MAX_RELEASES`. A re-run on the same day makes no request. Every release's fetch is a
    `sync_state` row; a failed one makes the run FAILED, so `/status/jobs` shows it.
    What it assumes: the database is migrated and the network reachable.
    What it never does: rebuild L1 (`index_membership_history` is rebuilt from L0 deliberately, as a
    new dated build), reach back past the window, or touch any host but niftyindices.com.
    """
    settings = context.settings
    clock = context.clock
    today = clock.today()
    with (
        leased_fetcher(
            [HOST], clock=clock, command="index_press_refresh", settings=settings
        ) as fetcher,
        connection(settings) as conn,
    ):
        report = run_press_release_campaign(
            fetcher=fetcher,
            l0=L0Store(clock=clock, data_root=settings.data_root),
            tracker=SyncStateStore(conn, clock=clock, calendar=trading_calendar()),
            as_of=today,
            max_releases=REFRESH_MAX_RELEASES,
            since=today - REFRESH_WINDOW,
        )
        conn.commit()
    if report.failed or report.budget_exhausted:
        raise RuntimeError(
            f"index_press_refresh {today}: {len(report.failed)} release(s) failed, "
            f"{len(report.unfetched)} left unfetched at the {REFRESH_MAX_RELEASES}-release ceiling"
        )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: fetch under a `niftyindices.com` lease, then (unless `--fetch-only`) rebuild history."""
    parser = argparse.ArgumentParser(prog="index-history-backfill", description=__doc__)
    parser.add_argument("--as-of", dest="as_of", type=date.fromisoformat, default=None)
    parser.add_argument("--max-releases", type=int, default=DEFAULT_MAX_RELEASES)
    parser.add_argument("--since", type=date.fromisoformat, default=None)
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--build-only", action="store_true", help="no network: rebuild from L0")
    parser.add_argument("--report", type=Path, default=None, help="write the depth report here")
    parser.add_argument(
        "--write-l1",
        type=Path,
        default=None,
        metavar="ROOT",
        help="also write the build's L1 datasets under ROOT/L1 (e.g. a /tmp root to inspect a "
        "build without touching the lake); the lake itself is only ever read",
    )
    args = parser.parse_args(argv)

    settings: Settings = get_settings()
    clock: Clock = SystemClock()
    as_of = args.as_of if args.as_of is not None else clock.now().date()

    if not args.build_only:
        with (
            leased_fetcher([HOST], clock=clock, command="index-history-backfill") as fetcher,
            connection(settings) as conn,
        ):
            l0 = L0Store(clock=clock, data_root=settings.data_root)
            sync = SyncStateStore(conn, clock=clock, calendar=trading_calendar())
            report = run_press_release_campaign(
                fetcher=fetcher,
                l0=l0,
                tracker=sync,
                as_of=as_of,
                max_releases=args.max_releases,
                since=args.since,
            )
            conn.commit()
        print(
            f"press releases {as_of}: {report.requests} requests, {len(report.fetched)} fetched, "
            f"{len(report.already_in_l0)} already in L0, {len(report.failed)} failed, "
            f"{len(report.unfetched)} left for a later (owner-approved) run; contiguous back to "
            f"{report.oldest_contiguous}"
        )
        if args.fetch_only:
            return 0 if not report.failed else 1

    from dataplatform.ingest.index_history import (
        build_membership_history,
        render_history_report,
    )

    l0 = L0Store(clock=clock, data_root=settings.data_root)
    build = build_membership_history(l0=l0, as_of=as_of, data_root=settings.data_root)
    markdown = render_history_report(build)
    if args.write_l1 is not None:
        from dataplatform.ingest.index_history import write_membership_history

        for path in write_membership_history(build, data_root=args.write_l1):
            print(f"wrote {path}", file=sys.stderr)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
