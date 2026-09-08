"""D1 (M3.9.b): the benchmark-TRI backfill runner — one request per index, whole history.

`indices.ingest_tri` drives one index from nothing to `PUBLISHED`. This is the driver over a set of
them: it resolves the window, skips an index already published for it, leases the host so no second
driver competes for the request budget, commits after each index, and prints what it landed.

It is a *tiny* campaign and that is the whole point of the source. One POST returns an index's
entire published history — 6,764 rows and ~940 KB for NIFTY 50 back to 1999-06-30 — so the three
indices the reference case needs (§8's NIFTY 50 + NIFTY IT + NIFTY CPSE) cost three requests, not
three thousand. D8 is explicit that re-fetching later is the expensive path, so the default window
starts below any plausible index launch and takes everything the endpoint will give.

Three requests is far under the ~200-request threshold AGENTIC_CONTEXT reserves to the owner
(§4), so this needs no sign-off — but it is still a driver run, not a test. It opens sockets, and
the offline suite never reaches it: `run_tri_backfill` takes its `Fetcher`, `L0Store` and
`SyncTracker` by injection so a test drives it with a `RecordedTransport`, and `main` is the only
place that builds the real, networked wiring (B8). The clock is injected (B10) and reaches only the
L0 receipt and the sync row — never a `knowable_date`, which `indices.tri_knowable_date` derives
from each published row's own session date.

Runbook: `ops/runbooks/benchmark-tri.md`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.fetcher import Fetcher, leased_fetcher
from dataplatform.ingest.indices import (
    TRI_METHOD_PUBLISHED,
    TRI_SOURCE_ID,
    SyncTracker,
    TriSeries,
    ingest_tri,
    read_tri_series,
)
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Store

_LOG = get_logger(__name__)

#: The host every request here goes to — leased for the driver's lifetime.
TRI_HOST: Final = "niftyindices.com"


@dataclass(frozen=True, slots=True)
class IndexSpec:
    """One index to backfill: the name to *send*, and the slug the lake files it under.

    `name` is the exchange's own name in CAPS, which is how `getTotalReturnIndexString` wants it
    inbound; it echoes back title-cased and `parse_tri_native` reconciles the two. `slug` is the
    canonical lake identifier, so a change in the site's title casing cannot move a lake path.
    """

    name: str
    slug: str


#: The three indices AGENTIC_CONTEXT §8's ratified reference case needs. `nifty50` is also
#: `backtest.run`'s `_BENCHMARK_TRI_SLUG` — the broad-market benchmark every run is measured
#: against — so it is the one that must exist for the backtest to stop falling back.
DEFAULT_INDEX_SET: Final[tuple[IndexSpec, ...]] = (
    IndexSpec(name="NIFTY 50", slug="nifty50"),
    IndexSpec(name="NIFTY IT", slug="niftyit"),
    IndexSpec(name="NIFTY CPSE", slug="niftycpse"),
)

#: The default window's lower bound: below every NIFTY index's launch, so the endpoint returns
#: whatever depth it actually has rather than whatever we guessed. D8's own probe recorded
#: 2001-04-02 as NIFTY 50's earliest because it *asked* for 2001-04-01; asked from 1990 the same
#: endpoint answers from 1999-06-30. Depth is a measurement, not a documented number.
EARLIEST_REQUESTED: Final = date(1990, 4, 1)


@dataclass(frozen=True, slots=True)
class IndexOutcome:
    """What happened to one index: published with a measured depth, or skipped as already done."""

    spec: IndexSpec
    points: int
    earliest: date | None
    latest: date | None
    skipped: bool

    @property
    def line(self) -> str:
        if self.skipped:
            return f"{self.spec.slug}: skipped — already published for this window"
        return (
            f"{self.spec.slug}: {self.points} points, "
            f"{self.earliest} .. {self.latest} ({self.spec.name})"
        )


def already_published(spec: IndexSpec, start: date, data_root: Path | None) -> bool:
    """Whether L1 already holds a *published* series for this index reaching back to `start`.

    Resume is read off the artefact rather than off the sync row, for the same reason M10.1's
    constituents sweep does: the artefact is what downstream reads, and a sync row that says
    PUBLISHED while the partition is missing is precisely the disagreement a resume check should
    not trust. `method=TRI_METHOD_PUBLISHED` is explicit — a computed-fallback series on disk is
    not a reason to skip fetching the real one.
    """
    series = read_tri_series(spec.slug, date.max, method=TRI_METHOD_PUBLISHED, data_root=data_root)
    return series is not None and series.points[0].as_of <= start


def run_tri_backfill(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SyncTracker,
    indices: Sequence[IndexSpec] = DEFAULT_INDEX_SET,
    start: date = EARLIEST_REQUESTED,
    end: date,
    data_root: Path | None = None,
    register: SourceRegister | None = None,
    commit: Callable[[], None] | None = None,
) -> tuple[IndexOutcome, ...]:
    """Backfill each index's published TRI over `[start, end]`, committing after each.

    Returns one `IndexOutcome` per index in the order given. A failure is *not* swallowed: it is
    recorded on the index's own sync row by `ingest_tri` and then re-raised, because a benchmark
    that half-landed is not a partial success — every excess-return figure downstream would be
    struck against whatever did land. A driver that wanted to park one index and continue would be
    a different decision from a different task.
    """
    outcomes: list[IndexOutcome] = []
    for spec in indices:
        if already_published(spec, start, data_root):
            _LOG.info(
                "tri_backfill.skipped",
                source=TRI_SOURCE_ID,
                index=spec.slug,
                reason="L1 already holds a published series reaching this window's start",
                state="PUBLISHED",
            )
            outcomes.append(
                IndexOutcome(spec=spec, points=0, earliest=None, latest=None, skipped=True)
            )
            continue

        series: TriSeries = ingest_tri(
            fetcher=fetcher,
            l0=l0,
            tracker=tracker,
            index_name=spec.name,
            index_slug=spec.slug,
            start=start,
            end=end,
            data_root=data_root,
            register=register,
        )
        if commit is not None:
            commit()
        outcomes.append(
            IndexOutcome(
                spec=spec,
                points=len(series.points),
                earliest=series.points[0].as_of,
                latest=series.points[-1].as_of,
                skipped=False,
            )
        )
    return tuple(outcomes)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: fetch the configured indices' published TRI and print measured depth.

    Wires the real networked fetcher under a `niftyindices.com` lease, the configured lake and the
    Postgres sync state, and commits after each index so a kill loses at most the index in flight.
    `--end` defaults to today from the system clock, which is a property of *this fetch* — the
    levels carry their own knowable dates, derived from their own sessions.
    """
    parser = argparse.ArgumentParser(prog="tri-backfill", description=__doc__)
    parser.add_argument(
        "--index",
        dest="indices",
        action="append",
        default=None,
        metavar="SLUG",
        help="index slug to backfill, repeatable (default: all of "
        f"{' '.join(s.slug for s in DEFAULT_INDEX_SET)})",
    )
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=EARLIEST_REQUESTED,
        help="earliest date to request (default: 1990-04-01 — below every index's launch)",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=None,
        help="latest date to request, and the sync row's logical date (default: today)",
    )
    args = parser.parse_args(argv)

    known = {spec.slug: spec for spec in DEFAULT_INDEX_SET}
    if args.indices is None:
        indices = DEFAULT_INDEX_SET
    else:
        unknown = [slug for slug in args.indices if slug not in known]
        if unknown:
            print(
                f"unknown index slug(s) {unknown}; known: {sorted(known)}. Add an IndexSpec "
                "rather than passing an unmapped name — the CAPS name sent to the endpoint is "
                "not derivable from the slug.",
                file=sys.stderr,
            )
            return 2
        indices = tuple(known[slug] for slug in args.indices)

    settings: Settings = get_settings()
    clock: Clock = SystemClock()
    register = load_register()
    end = args.end if args.end is not None else clock.now().date()

    with (
        leased_fetcher(
            [TRI_HOST], clock=clock, command="tri-backfill", settings=settings, register=register
        ) as fetcher,
        connection(settings) as conn,
    ):
        l0 = L0Store(clock=clock, data_root=settings.data_root)
        sync = SyncStateStore(conn, clock=clock, calendar=trading_calendar())
        outcomes = run_tri_backfill(
            fetcher=fetcher,
            l0=l0,
            tracker=sync,
            indices=indices,
            start=args.start,
            end=end,
            data_root=settings.data_root,
            register=register,
            commit=conn.commit,
        )

    for outcome in outcomes:
        print(outcome.line)
    published = sum(1 for outcome in outcomes if not outcome.skipped)
    print(
        f"benchmark_tri: {published} of {len(outcomes)} index(es) fetched, "
        f"window {args.start} .. {end}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
