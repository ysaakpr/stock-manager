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

`--from-l0` is the same driver with the fetch removed: it re-derives L1 from the payloads already
in this lake's L0 rather than asking the source again. That is the honest way to promote a run into
a second lake (invariant #1 — L0 is the record, L1 is a derivation of it), and it is also the
cheaper one, because a re-fetch would mint fresh receipts and the endpoint's per-request
`RequestNumber` means the same logical history returns as different bytes. It writes no sync row;
`rebuild_tri_from_l0` says why.

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
    parse_l0_tri_filename,
    parse_tri_l0,
    read_tri_series,
    write_tri_l1,
)
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Ref, L0Store

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
    """What happened to one index: published with a measured depth, or skipped as already done.

    `l0_key` names the payload the series came from. A fetch leaves it `None` — the receipt is the
    sync row's — while a `--from-l0` rebuild sets it, because *which stored bytes were re-parsed*
    is the only provenance a rebuild has to report and a run over the wrong payload would otherwise
    look identical to a run over the right one.
    """

    spec: IndexSpec
    points: int
    earliest: date | None
    latest: date | None
    skipped: bool
    l0_key: str | None = None

    @property
    def line(self) -> str:
        if self.skipped:
            return f"{self.spec.slug}: skipped — already published for this window"
        origin = f" from {self.l0_key}" if self.l0_key is not None else ""
        return (
            f"{self.spec.slug}: {self.points} points, "
            f"{self.earliest} .. {self.latest} ({self.spec.name}){origin}"
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


class NoStoredPayloadError(Exception):
    """A `--from-l0` rebuild was asked for an index whose payload is not in this lake's L0."""


def stored_tri_payloads(l0: L0Store, indices: Sequence[IndexSpec]) -> dict[str, tuple[L0Ref, ...]]:
    """Group this lake's stored TRI payloads by index slug, oldest window first.

    Reads `nifty_tri_history` out of L0 and recovers each payload's index from its filename
    (`parse_l0_tri_filename`), because the endpoint is one URL for every index and the name is the
    only place the index was written down. A payload whose slug is not in `indices` is ignored —
    a lake may hold indices this run was not asked about — but a filename that is not an L0 TRI
    name at all raises, rather than being skipped as if it were not there.

    `L0Store.iter_refs` yields in (date, filename) order and that order is preserved here, so a
    caller replaying several windows for one index writes the oldest first and the newest wins
    every date they overlap on.
    """
    wanted = {spec.slug for spec in indices}
    by_slug: dict[str, list[L0Ref]] = {slug: [] for slug in wanted}
    for ref in l0.iter_refs(TRI_SOURCE_ID):
        slug, _start, _end = parse_l0_tri_filename(ref.filename)
        if slug in wanted:
            by_slug[slug].append(ref)
    return {slug: tuple(refs) for slug, refs in by_slug.items()}


def rebuild_tri_from_l0(
    *,
    l0: L0Store,
    indices: Sequence[IndexSpec] = DEFAULT_INDEX_SET,
    data_root: Path | None = None,
) -> tuple[IndexOutcome, ...]:
    """Re-derive each index's published L1 series from the payloads already in this lake's L0.

    What it does: for every stored window of every requested index, re-reads the payload through
    `L0Store.get` (which re-verifies the recorded sha256), re-parses it and rewrites the L1
    partitions. Offline by construction — it takes no `Fetcher`, so it cannot reach the network and
    costs no request against a source whose *re-fetch* is the expensive path (D8).

    What it assumes: L0 holds the payloads. An index with none raises `NoStoredPayloadError` rather
    than returning an empty result, because "the lake has no benchmark" and "the rebuild quietly did
    nothing" must not look the same to a caller.

    What it never does: touch `sync_state`. The sync row records an *ingestion*, and no ingestion is
    happening — L0 is unchanged, and §4.4 closes `PUBLISHED` absolutely (`begin` on a published date
    is an illegal transition, by design, because L0 is immutable). Re-deriving L1 from bytes that
    were already fetched, validated and published is a repair of a derived layer, not a new fetch of
    the source; a row claiming otherwise would be a false receipt. It follows that a lake carried to
    a host whose database does not yet know these payloads gets its L1 back from this path and still
    has no sync history — a true statement about that host, and the runbook says so.
    """
    stored = stored_tri_payloads(l0, indices)
    missing = [spec.slug for spec in indices if not stored[spec.slug]]
    if missing:
        raise NoStoredPayloadError(
            f"no L0 payload for {missing} under {TRI_SOURCE_ID} in {l0.root}. A rebuild re-derives "
            "L1 from stored bytes; it cannot invent them. Copy the payloads in from the lake that "
            "fetched them, or run the fetch (`uv run python -m dataplatform.ingest.tri_backfill`)."
        )

    outcomes: list[IndexOutcome] = []
    for spec in indices:
        series: TriSeries | None = None
        last: L0Ref | None = None
        for ref in stored[spec.slug]:
            series = parse_tri_l0(l0, ref, index_name=spec.name, index_slug=spec.slug)
            write_tri_l1(series, data_root=data_root)
            last = ref
            _LOG.info(
                "tri_backfill.rebuilt_from_l0",
                source=TRI_SOURCE_ID,
                index=spec.slug,
                l0_key=ref.key,
                sha256=ref.sha256,
                points=len(series.points),
                earliest=series.points[0].as_of.isoformat(),
                latest=series.points[-1].as_of.isoformat(),
                state="NORMALIZED",
            )
        assert series is not None and last is not None  # `missing` above ruled the empty case out
        outcomes.append(
            IndexOutcome(
                spec=spec,
                points=len(series.points),
                earliest=series.points[0].as_of,
                latest=series.points[-1].as_of,
                skipped=False,
                l0_key=last.key,
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
    parser.add_argument(
        "--from-l0",
        dest="from_l0",
        action="store_true",
        help="re-derive L1 from the payloads already in L0 instead of fetching: no network, no "
        "request, no sync write. Use it to bring a lake's L1 back after the derived layer was "
        "lost, or to land a run in a second lake without re-fetching (--start/--end are ignored; "
        "the stored windows are whatever was fetched)",
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

    if args.from_l0:
        outcomes = rebuild_tri_from_l0(
            l0=L0Store(clock=clock, data_root=settings.data_root),
            indices=indices,
            data_root=settings.data_root,
        )
        for outcome in outcomes:
            print(outcome.line)
        print(
            f"benchmark_tri: {len(outcomes)} index(es) rebuilt from L0 in {settings.data_root}, "
            "0 requests, sync_state untouched"
        )
        return 0

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
