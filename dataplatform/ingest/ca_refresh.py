"""The scheduled corporate-action refresh: new actions in, changed chains recomputed, L2 rebuilt.

`corp_actions_backfill` is a campaign — a ten-year window fetched once and checkpointed by window
start — and nothing ran it again after 2026-09-03, so the store stopped at about 2026-09-01. Two
September 2:1 splits (INE940H01022 on 2026-09-11, INE2FMX01012 on 2026-09-28) then reached L2 only
through the price-implied detector, with no corporate-action record behind them. This module is the
incremental half the backfill never had, run weekly by the scheduler (`ca_refresh`) and monthly in
its BSE-sweep form (`bse_ca_sweep`):

1. **NSE, a trailing window.** One `corporates-corporateActions` request over the last
   `NSE_LOOKBACK_DAYS` of ex-dates, checkpointed under `nse_corp_actions/refresh` keyed on the
   *refresh date*, not the window start. The backfill keys its chunks on their starts, and the last
   one is `2026-09-01` and PUBLISHED, so a backfill invocation from 2026-09-01 would resume-skip the
   very window that holds the missing splits; a refresh key never collides with a chunk, and a
   second refresh on the same day is a no-op.
2. **BSE, the counterpart of what NSE just published.** The BSE feed is per scrip, ~6,700 requests
   for the whole universe, so the weekly run fetches only the scrips of ISINs that have an NSE
   action in the window with no BSE twin yet — so a fresh split is cross-verified the week it lands
   rather than a month later. The monthly sweep fetches every BSE scrip that traded in the last
   `BSE_SWEEP_LOOKBACK_DAYS`, which is what picks up BSE-only listings.
3. **Reconcile, recompute only what moved.** The finalize the backfill runs, under the lake's own
   `SingleSourcePolicy.ACCEPT` (`identity.lineage_rebuild` built the store with it; reconciling the
   whole set under `QUEUE` would un-reconcile every single-feed action it admitted and drop their
   factors). Only ISINs whose reconciled actions changed are recomputed, so only they get an
   `l2_invalidation`.
4. **Drain the queue.** `rebuild_invalidated`, the existing path, rebuilds exactly those ISINs. A
   feed split that lands on a date the implied detector had adjusted replaces it rather than
   doubling it: the detector reads bars already in the recorded chain's terms, so a recorded step is
   no step (`corpactions.implied`).

Offline by construction like the backfill: `refresh_corporate_actions` takes its fetcher, stores and
connection by injection and the L2 drain as a callable; `run_ca_refresh` (the job body) and `main`
build the real wiring. Time is injected (B10).

    uv run python -m dataplatform.ingest.ca_refresh                          # as the weekly job
    uv run python -m dataplatform.ingest.ca_refresh --from 2026-09-01 --skip-l2
    uv run python -m dataplatform.ingest.ca_refresh --bse-sweep              # as the monthly job
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.corpactions.reconcile import SingleSourcePolicy
from dataplatform.identity.lineage import LineageResolver, LineageStore
from dataplatform.identity.master import IdentityMaster, IdentityStore
from dataplatform.ingest.bse import corp_actions as bse_ca
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.corp_actions import build_scrip_index, load_corporate_actions
from dataplatform.ingest.corp_actions_backfill import (
    CaBackfillReport,
    CaBackfillRunner,
    CaFetchUnit,
    FinalizeCounts,
    bse_scrips_for_isins,
    build_bse_units,
    build_nse_units,
    finalize_reconcile_and_recompute,
    isins_in_price_window,
    reconciled_fingerprints,
)
from dataplatform.ingest.fetcher import Fetcher, leased_fetcher
from dataplatform.ingest.nse import corp_actions as nse_ca
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import Connection, connection
from dataplatform.store.l0 import L0Store

if TYPE_CHECKING:
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "BSE_SWEEP_LOOKBACK_DAYS",
    "NSE_LOOKBACK_DAYS",
    "NSE_REFRESH_STATE_SOURCE",
    "REFRESH_POLICY",
    "CaRefreshError",
    "CaRefreshReport",
    "build_bse_refresh_units",
    "build_nse_refresh_unit",
    "main",
    "refresh_corporate_actions",
    "run_bse_ca_sweep",
    "run_ca_refresh",
    "unmatched_nse_isins",
]

_LOG = get_logger(__name__)

#: Ex-dates a weekly refresh re-reads. Five weeks: a missed run, or a late NSE filing for last
#: month's ex-date, is still inside the next window; a re-read action is `ON CONFLICT DO NOTHING`.
NSE_LOOKBACK_DAYS: Final = 35

#: How far back the monthly BSE sweep looks for scrips that traded.
BSE_SWEEP_LOOKBACK_DAYS: Final = 365

#: The refresh's `sync_state` key: the register id with a `refresh` unit, logical date = run date.
NSE_REFRESH_STATE_SOURCE: Final = f"{nse_ca.SOURCE_ID}/refresh"

#: The single-source policy the live store was finalized with (`identity.lineage_rebuild`).
#: Reconciliation runs over the whole stored set, so a refresh under any other policy would
#: re-decide every action the lake already admitted, not just the new ones.
REFRESH_POLICY: Final = SingleSourcePolicy.ACCEPT


class CaRefreshError(RuntimeError):
    """A refresh left units FAILED or parked on a 403 spike; what landed was still finalized."""


@dataclass(slots=True)
class CaRefreshReport:
    """What one refresh did: the two fetch passes, the finalize, and the L2 rebuild."""

    window: tuple[date, date]
    nse: CaBackfillReport
    bse: CaBackfillReport
    bse_scrips: int
    finalize: FinalizeCounts | None = None
    l2_rebuilt: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.failures and not self.nse.parked and not self.bse.parked

    def summary(self) -> str:
        start, end = self.window
        line = (
            f"CA refresh {start.isoformat()}..{end.isoformat()}: NSE "
            f"{self.nse.published} published/{self.nse.skipped_published} already, "
            f"{self.nse.actions_persisted} new actions; BSE {self.bse.published} of "
            f"{self.bse_scrips} scrips, {self.bse.actions_persisted} new actions"
        )
        if self.finalize is not None:
            line += (
                f"; recomputed {self.finalize.isins_recomputed} ISINs, "
                f"{self.finalize.l2_invalidated} L2 invalidations, {self.l2_rebuilt} rebuilt"
            )
        if self.failures:
            line += f"; {len(self.failures)} FAILED: " + "; ".join(
                f"{label}: {message}" for label, message in self.failures[:5]
            )
        return line


def build_nse_refresh_unit(
    from_date: date, to_date: date, *, register: SourceRegister
) -> CaFetchUnit:
    """The one NSE request a refresh makes, keyed on `to_date` under `NSE_REFRESH_STATE_SOURCE`.

    Its filename carries both window ends, so two refreshes in one month (one L0 partition) never
    share a payload unless they asked for the same window — when the stored bytes are the answer.
    Raises `ValueError` for a window the backfill would split into more than one chunk (over twelve
    months): that is a campaign, and two chunks under one refresh key would overwrite each other.
    """
    units = build_nse_units(from_date, to_date, register=register)
    if len(units) != 1:
        raise ValueError(
            f"a refresh window is at most twelve months; {from_date}..{to_date} is "
            f"{len(units)} backfill chunks — run corp_actions_backfill for that"
        )
    return replace(units[0], state_source=NSE_REFRESH_STATE_SOURCE, logical_date=to_date)


def build_bse_refresh_units(
    scrips: Sequence[str], *, as_of: date, register: SourceRegister
) -> list[CaFetchUnit]:
    """The per-scrip BSE requests of a refresh on `as_of`, each keyed and named for that date.

    The backfill's `defaultdata_<scrip>.json` is unique only because it ran once. L0 is partitioned
    by source and *month*, so a second weekly refresh in the same month would find last week's file
    under that name and re-parse it instead of fetching — the date in the filename is what makes
    each refresh's payload its own.
    """
    return [
        replace(unit, filename=f"defaultdata_{unit.scrip_code}_{as_of:%Y%m%d}.json")
        for unit in build_bse_units(scrips, register=register, anchor=as_of)
    ]


def unmatched_nse_isins(conn: Connection, from_date: date, to_date: date) -> set[str]:
    """ISINs with an NSE action ex-dated in the window that the BSE feed has no twin for yet.

    The weekly refresh's BSE plan: an action both feeds already carry needs no BSE request, and an
    NSE-only listing has no BSE scrip and so drops out at `bse_scrips_for_isins`.
    """
    actions = load_corporate_actions(conn)
    bse = {(a.isin, a.ex_date, a.action_type) for a in actions if a.source == bse_ca.SOURCE_ID}
    return {
        a.isin
        for a in actions
        if a.source == nse_ca.SOURCE_ID
        and from_date <= a.ex_date <= to_date
        and (a.isin, a.ex_date, a.action_type) not in bse
    }


def refresh_corporate_actions(
    *,
    from_date: date,
    to_date: date,
    fetcher: Fetcher,
    l0: L0Store,
    sync: SyncStateStore,
    conn: Connection,
    commit: Callable[[], None],
    master: IdentityMaster,
    clock: Clock,
    register: SourceRegister,
    drain_l2: Callable[[], int] | None,
    lineage: LineageResolver | None = None,
    sweep_isins: Iterable[str] | None = None,
) -> CaRefreshReport:
    """Fetch the window's new actions, reconcile, recompute what changed, and rebuild its L2.

    What it does: one NSE request over `from_date..to_date` keyed on `to_date`; then one BSE request
    per scrip of `unmatched_nse_isins` (and of `sweep_isins`, the monthly sweep's universe), keyed
    on `to_date`; then the backfill's finalize under `REFRESH_POLICY`, recomputing only the ISINs
    whose reconciled actions changed; then `drain_l2`, which rebuilds the ISINs that raised an
    `l2_invalidation`. Returns the report; raising is the caller's (`CaRefreshReport.clean`).
    What it assumes: one clock and one transaction across every argument; `commit` checkpoints it.
    What it never does: re-fetch a unit already PUBLISHED for `to_date`, recompute an ISIN whose
    chain did not move, or skip the finalize because a unit failed — what did land is still owed
    its factors. A 403 spike parks the fetch and the finalize still runs over what landed.
    `drain_l2=None` leaves the queue for `l2_fill --rebuild-invalidated`.
    """
    before = reconciled_fingerprints(conn)
    runner = CaBackfillRunner(
        fetcher=fetcher,
        l0=l0,
        sync=sync,
        conn=conn,
        commit=commit,
        master=master,
        scrip_index=build_scrip_index(master),
        clock=clock,
        lineage=lineage,
    )
    nse_report = runner.run([build_nse_refresh_unit(from_date, to_date, register=register)])

    wanted = unmatched_nse_isins(conn, from_date, to_date)
    if sweep_isins is not None:
        wanted |= set(sweep_isins)
    scrips = bse_scrips_for_isins(master, wanted)
    bse_units = build_bse_refresh_units(scrips, as_of=to_date, register=register)
    if nse_report.parked:
        # The NSE host refused us; the BSE host is a different budget, but a park is a human's
        # call and the run says so rather than carrying on half-blind.
        bse_report = CaBackfillReport(requested=len(bse_units))
    else:
        bse_report = runner.run(bse_units)

    report = CaRefreshReport(
        window=(from_date, to_date),
        nse=nse_report,
        bse=bse_report,
        bse_scrips=len(scrips),
        failures=[*nse_report.failures, *bse_report.failures],
    )
    for parked in (nse_report, bse_report):
        if parked.park_detail:
            report.failures.append(("PARKED", parked.park_detail))

    report.finalize = finalize_reconcile_and_recompute(
        conn,
        clock=clock,
        commit=commit,
        single_source_policy=REFRESH_POLICY,
        changed_since=before,
    )
    if drain_l2 is not None:
        report.l2_rebuilt = drain_l2()
    _LOG.info(
        "ca_refresh.done",
        source=nse_ca.SOURCE_ID,
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
        nse_actions=nse_report.actions_persisted,
        bse_scrips=len(scrips),
        bse_actions=bse_report.actions_persisted,
        isins_recomputed=report.finalize.isins_recomputed,
        l2_invalidated=report.finalize.l2_invalidated,
        l2_rebuilt=report.l2_rebuilt,
        failures=len(report.failures),
        state="PUBLISHED" if report.clean else "FAILED",
    )
    return report


def _hosts(register: SourceRegister) -> list[str]:
    """The hosts a refresh talks to, read from the register rows of the two feeds."""
    wanted = {nse_ca.SOURCE_ID, bse_ca.SOURCE_ID}
    return sorted({s.host for s in register.sources if s.id in wanted})


def _run(
    *,
    settings: Settings,
    clock: Clock,
    from_date: date,
    to_date: date,
    command: str,
    sweep: bool,
    rebuild_l2: bool,
) -> CaRefreshReport:
    """The real wiring: leased hosts, the configured lake and store, the lineage-aware L2 drain."""
    from dataplatform.store.l2_fill import drain_invalidated_with

    register = load_register()
    calendar = trading_calendar()
    with (
        leased_fetcher(
            _hosts(register), clock=clock, command=command, settings=settings, register=register
        ) as fetcher,
        connection(settings) as conn,
    ):
        sweep_isins: set[str] | None = None
        if sweep:
            sweep_isins = isins_in_price_window(
                calendar,
                to_date - timedelta(days=BSE_SWEEP_LOOKBACK_DAYS),
                to_date,
                data_root=settings.data_root,
            )

        def drain() -> int:
            rebuilt = drain_invalidated_with(conn, clock=clock, data_root=settings.data_root)
            conn.commit()
            return len(rebuilt)

        report = refresh_corporate_actions(
            from_date=from_date,
            to_date=to_date,
            fetcher=fetcher,
            l0=L0Store(clock=clock, data_root=settings.data_root),
            sync=SyncStateStore(conn, clock=clock, calendar=calendar),
            conn=conn,
            commit=conn.commit,
            master=IdentityStore(conn, clock=clock).load_master(),
            clock=clock,
            register=register,
            drain_l2=drain if rebuild_l2 else None,
            lineage=LineageStore(conn, clock=clock).load(),
            sweep_isins=sweep_isins,
        )
        conn.commit()
    return report


def _job(context: JobContext, *, sweep: bool) -> None:
    today = context.clock.today()
    report = _run(
        settings=context.settings,
        clock=context.clock,
        from_date=today - timedelta(days=NSE_LOOKBACK_DAYS),
        to_date=today,
        command=context.job_name,
        sweep=sweep,
        rebuild_l2=True,
    )
    if not report.clean:
        raise CaRefreshError(report.summary())


def run_ca_refresh(context: JobContext) -> None:
    """The weekly `ca_refresh` job body: the trailing NSE window plus its BSE counterparts.

    Raises `CaRefreshError` when any unit FAILED or the fetch parked, after finalizing and
    rebuilding what did land, so the run is recorded FAILED and `/status/jobs` shows it.
    """
    _job(context, sweep=False)


def run_bse_ca_sweep(context: JobContext) -> None:
    """The monthly `bse_ca_sweep` job body: the weekly refresh plus every BSE scrip that traded."""
    _job(context, sweep=True)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: one refresh over `--from..--to` (default: the weekly job's window ending today).

    Exit 0 on a clean run, 1 when a unit failed or the fetch parked (the report says which).
    """
    clock: Clock = SystemClock()
    today = clock.today()
    parser = argparse.ArgumentParser(prog="ca-refresh", description=__doc__)
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat, default=None)
    parser.add_argument("--to", dest="to_date", type=date.fromisoformat, default=today)
    parser.add_argument(
        "--bse-sweep", action="store_true", help="also fetch every BSE scrip that traded in a year"
    )
    parser.add_argument(
        "--skip-l2",
        action="store_true",
        help="leave the l2_invalidation queue for `l2_fill --rebuild-invalidated`",
    )
    args = parser.parse_args(argv)
    from_date = args.from_date or args.to_date - timedelta(days=NSE_LOOKBACK_DAYS)
    if args.to_date > today:
        parser.error(f"--to {args.to_date} is in the future")
    try:
        report = _run(
            settings=get_settings(),
            clock=clock,
            from_date=from_date,
            to_date=args.to_date,
            command="ca-refresh",
            sweep=args.bse_sweep,
            rebuild_l2=not args.skip_l2,
        )
    except ValueError as error:
        print(f"cannot refresh: {error}", file=sys.stderr)
        return 2
    print(report.summary())
    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
