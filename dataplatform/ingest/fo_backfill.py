"""D1: the F&O bhavcopy history backfill — one session at a time over the UDiFF era (M3.7 data).

M3.7 built the parser (`ingest.nse.fo_bhavcopy`: one `BhavCopy_NSE_FO_…_F_0000.csv.zip` → contract
rows) and the stores (`store.fo_aggregates`: raw contracts in L1 `fo_contracts`, per-underlier
sentiment aggregates in L2 `fo_aggregates`), but nothing ever fetched the history: the register
calls the source `daily` and the scheduler ledger says "backfill-only". This is the history half.
The daily-forward job is a separate driver and deliberately not this one.

It is the same shape as `macro.backfill` and `fundamentals_backfill`, so the vocabulary is one:

* **Resume from `sync_state`.** One unit per session under `nse_fo_bhavcopy`. `PUBLISHED` is
  skipped; a 404 closes its session (`retryable=False`) and is reported, never re-asked; a
  retryable failure is tried again next run. A payload already in L0 is re-parsed, not re-fetched.
* **Commit per session**, after L1 and L2 are both on disk.
* **A 403 spike parks** with `ParkReason.FORBIDDEN_SPIKE` and exit 3 (AGENTIC_CONTEXT §8).
* **`--stop-before HH:MM` IST** steps out of the exchange's evening window between sessions.

The era starts at the UDiFF cutover (2024-07-08). The older `fo<DD><MON><YYYY>bhav.csv.zip`
format is a different parser and a different register row; this runner refuses to plan before
the era rather than ask for a file its parser cannot read.

Sentiment context only, never traded (EXECUTION_PLAN §4.1 row 12): nothing here imports
`execution`. Offline by construction (B8): fetcher, L0, sync store and identity master are injected.
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Final, Protocol

from dataplatform.clock import IST, Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.identity.master import IdentityMaster, IdentityStore
from dataplatform.ingest.calendar import TradingCalendar, trading_calendar
from dataplatform.ingest.fetcher import (
    Fetcher,
    FetchError,
    FetchHTTPError,
    ForbiddenError,
    ForbiddenSpikeError,
    leased_fetcher,
)
from dataplatform.ingest.lease import HostBusyError
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.fo_bhavcopy import FO_ERA_START, FO_SOURCE_ID, parse
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.fo_aggregates import UnderlyingKind, build_aggregates, write_l1, write_l2
from dataplatform.store.l0 import L0Error, L0Ref, L0Store

__all__ = [
    "FoBackfillReport",
    "FoBackfillRunner",
    "FoSessionUnit",
    "ParkReason",
    "build_plan",
    "main",
]

_LOG = get_logger(__name__)

HOST: Final = "nsearchives.nseindia.com"


class ParkReason(StrEnum):
    """Why a run stopped short and handed control to a human (an enumerated cause)."""

    FORBIDDEN_SPIKE = "FORBIDDEN_SPIKE"


@dataclass(frozen=True, slots=True)
class FoSessionUnit:
    session: date
    url: str
    filename: str

    @property
    def label(self) -> str:
        return f"fo bhavcopy {self.session.isoformat()}"


@dataclass(slots=True)
class FoBackfillReport:
    planned: int
    published: int = 0
    resumed: int = 0
    closed: int = 0
    not_published: int = 0
    refused: int = 0
    failed: int = 0
    l0_reused: int = 0
    requests: int = 0
    contracts_written: int = 0
    aggregates_written: int = 0
    unresolved_underlyings: int = 0
    stopped_early: str | None = None
    park_reason: ParkReason | None = None
    park_detail: str | None = None
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def parked(self) -> bool:
        return self.park_reason is not None


class SyncStore(Protocol):
    def get(self, source: str, logical_date: date) -> object | None: ...
    def begin(self, source: str, logical_date: date) -> object: ...
    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> object: ...
    def mark_validated(self, source: str, logical_date: date) -> object: ...
    def mark_normalized(self, source: str, logical_date: date) -> object: ...
    def mark_published(self, source: str, logical_date: date) -> object: ...
    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> object: ...


def build_plan(
    from_date: date, to_date: date, *, calendar: TradingCalendar, register: SourceRegister
) -> list[FoSessionUnit]:
    """One unit per expected data date in the UDiFF era, ascending. Pure: no socket, no DB.

    Raises `ValueError` on an inverted window or one starting before the UDiFF cutover.
    """
    if from_date > to_date:
        raise ValueError(f"from {from_date} is after to {to_date}")
    if from_date < FO_ERA_START:
        raise ValueError(
            f"from {from_date} precedes the UDiFF F&O era ({FO_ERA_START}); the legacy format is "
            "another parser and another register row"
        )
    source = next((s for s in register.sources if s.id == FO_SOURCE_ID), None)
    if source is None:
        raise ValueError(f"source {FO_SOURCE_ID!r} is not in the source register")
    units = []
    for session in calendar.expected_data_dates(from_date, to_date):
        url = source.url_template.replace("{YYYYMMDD}", f"{session:%Y%m%d}")
        units.append(FoSessionUnit(session, url, url.rsplit("/", 1)[-1]))
    return units


class FoBackfillRunner:
    """Drive a plan of F&O sessions to `PUBLISHED`, one committed session at a time.

    What it does: per open session, take the payload from L0 or fetch it, parse it, write the L1
    contract partition and the L2 aggregate partition, then commit the row `PUBLISHED`.
    What it assumes: the caller holds the archive host's lease; `master` is the D2 identity master
    (a stock underlier's ISIN resolves through it, never by a raw symbol join).
    What it never does: re-request a `PUBLISHED` or 404-closed session, or retry a 403 differently.
    """

    def __init__(
        self,
        *,
        fetcher: Fetcher | None,
        l0: L0Store,
        sync: SyncStore,
        commit: Callable[[], None],
        rollback: Callable[[], None] = lambda: None,
        master: IdentityMaster | None = None,
        should_stop: Callable[[], str | None] = lambda: None,
        data_root: Path | None = None,
        max_sessions: int | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._l0 = l0
        self._sync = sync
        self._commit = commit
        self._rollback = rollback
        self._master = master
        self._should_stop = should_stop
        self._data_root = data_root
        self._max_sessions = max_sessions

    def run(self, plan: Sequence[FoSessionUnit]) -> FoBackfillReport:
        report = FoBackfillReport(planned=len(plan))
        attempted = 0
        for unit in plan:
            row = self._sync.get(FO_SOURCE_ID, unit.session)
            state = getattr(row, "state", None)
            if state is SyncState.PUBLISHED:
                report.resumed += 1
                continue
            if state is SyncState.FAILED and getattr(row, "retryable", True) is False:
                report.closed += 1
                continue
            stop = self._should_stop()
            if stop is not None:
                report.stopped_early = stop
                break
            if self._max_sessions is not None and attempted >= self._max_sessions:
                report.stopped_early = f"--max-sessions {self._max_sessions} reached"
                break
            attempted += 1
            try:
                self._process(unit, report)
            except _ParkedError as parked:
                report.park_reason, report.park_detail = parked.reason, parked.detail
                break
        _LOG.info(
            "fo_backfill.run_done",
            source=FO_SOURCE_ID,
            planned=report.planned,
            published=report.published,
            resumed=report.resumed,
            requests=report.requests,
            not_published=report.not_published,
            failed=report.failed,
            refused=report.refused,
            state="PARKED" if report.parked else "DONE",
        )
        return report

    def _process(self, unit: FoSessionUnit, report: FoBackfillReport) -> None:
        try:
            ref = self._ref_for(unit, report)
        except ForbiddenSpikeError as spike:
            self._park(unit, spike)
        except ForbiddenError as exc:
            self._fail(unit, f"403: {exc}", retryable=True, report=report)
            return
        except FetchHTTPError as exc:
            if exc.status_code == 404:
                self._fail(
                    unit,
                    f"404: the archive does not hold {unit.filename}",
                    retryable=False,
                    report=report,
                    counter="not_published",
                )
            else:
                self._fail(unit, f"HTTP {exc.status_code}: {exc}", retryable=True, report=report)
            return
        except FetchError as exc:
            self._fail(unit, f"{type(exc).__name__}: {exc}", retryable=True, report=report)
            return

        try:
            rows = parse(self._l0.get(ref), filename=unit.filename)
            sessions = {row.trade_date for row in rows}
            if sessions != {unit.session}:
                raise ParseError(
                    f"file reports {sorted(sessions)}, requested {unit.session}",
                    filename=unit.filename,
                )
            aggregates = build_aggregates(rows, master=self._master)
        except (ParseError, L0Error, ValueError) as exc:
            # Retryable: the bytes are in L0, so a re-run after a parser fix re-derives the
            # session without a request. Only a 404 closes a session.
            self._fail(unit, str(exc), retryable=True, report=report, counter="refused")
            return

        try:
            self._sync.begin(FO_SOURCE_ID, unit.session)
            self._sync.mark_fetched(
                FO_SOURCE_ID, unit.session, checksum=ref.sha256, l0_path=ref.key
            )
            self._sync.mark_validated(FO_SOURCE_ID, unit.session)
            write_l1(rows, data_root=self._data_root)
            write_l2(aggregates, data_root=self._data_root)
            self._sync.mark_normalized(FO_SOURCE_ID, unit.session)
            self._sync.mark_published(FO_SOURCE_ID, unit.session)
            self._commit()
        except Exception:
            self._rollback()
            raise
        unresolved = sum(
            1 for a in aggregates if a.underlying_kind is UnderlyingKind.STOCK and a.isin is None
        )
        report.published += 1
        report.contracts_written += len(rows)
        report.aggregates_written += len(aggregates)
        report.unresolved_underlyings += unresolved
        _LOG.info(
            "fo_backfill.session_published",
            source=FO_SOURCE_ID,
            date=unit.session.isoformat(),
            contracts=len(rows),
            underlyings=len(aggregates),
            unresolved=unresolved,
            state="PUBLISHED",
        )

    def _ref_for(self, unit: FoSessionUnit, report: FoBackfillReport) -> L0Ref:
        if self._l0.exists(FO_SOURCE_ID, unit.session, unit.filename):
            report.l0_reused += 1
            return self._l0.ref_for(FO_SOURCE_ID, unit.session, unit.filename)
        if self._fetcher is None:
            raise FetchError(f"{unit.filename} is not in L0 and this run may not fetch")
        report.requests += 1
        return self._fetcher.fetch(FO_SOURCE_ID, unit.url, unit.session, filename=unit.filename)

    def _fail(
        self,
        unit: FoSessionUnit,
        message: str,
        *,
        retryable: bool,
        report: FoBackfillReport,
        counter: str = "failed",
    ) -> None:
        """Record the session FAILED, commit, and count it under `counter`."""
        self._rollback()
        self._sync.begin(FO_SOURCE_ID, unit.session)
        self._sync.mark_failed(FO_SOURCE_ID, unit.session, message, retryable=retryable)
        self._commit()
        setattr(report, counter, getattr(report, counter) + 1)
        report.failures.append((unit.label, message))
        _LOG.warning(
            "fo_backfill.session_failed",
            source=FO_SOURCE_ID,
            date=unit.session.isoformat(),
            retryable=retryable,
            error=message,
            state="FAILED",
        )

    def _park(self, unit: FoSessionUnit, spike: ForbiddenSpikeError) -> None:
        self._rollback()
        try:
            self._sync.begin(FO_SOURCE_ID, unit.session)
            self._sync.mark_failed(FO_SOURCE_ID, unit.session, str(spike), retryable=False)
            self._commit()
        except Exception:  # the DB itself is unwell; the park still takes priority
            self._rollback()
        detail = (
            f"{ParkReason.FORBIDDEN_SPIKE.value}: a 403 spike hard-stopped the fetch at "
            f"{unit.label!r}; resuming needs a human to clear the block, not a lower rate or a "
            f"rotated agent (AGENTIC_CONTEXT §8). Detail: {spike}"
        )
        _LOG.critical(
            "fo_backfill.hard_stop",
            source=FO_SOURCE_ID,
            date=unit.session.isoformat(),
            state="PARKED",
        )
        raise _ParkedError(ParkReason.FORBIDDEN_SPIKE, detail)


class _ParkedError(Exception):
    def __init__(self, reason: ParkReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def _deadline(clock: Clock, hhmm: str | None) -> Callable[[], str | None]:
    if hhmm is None:
        return lambda: None
    hour, minute = (int(part) for part in hhmm.split(":"))
    start = clock.now().astimezone(IST)
    cutoff = datetime.combine(start.date(), time(hour, minute), tzinfo=IST)
    if cutoff <= start:
        raise ValueError(f"--stop-before {hhmm} IST has already passed today ({start:%H:%M})")
    return lambda: (
        f"--stop-before {hhmm} IST reached" if clock.now().astimezone(IST) >= cutoff else None
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 on a clean or stopped run, 2 on a planning error, 3 when parked, 4 when the host
    lease is held by another driver."""
    ap = argparse.ArgumentParser(prog="fo-backfill", description=__doc__)
    ap.add_argument("--from", dest="from_date", type=date.fromisoformat, default=FO_ERA_START)
    ap.add_argument("--to", dest="to_date", type=date.fromisoformat, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-sessions", type=int, default=None)
    ap.add_argument("--stop-before", default=None, help="stop cleanly at HH:MM IST today")
    args = ap.parse_args(argv)

    settings = get_settings()
    clock: Clock = SystemClock()
    calendar = trading_calendar()
    register = load_register()
    try:
        plan = build_plan(args.from_date, args.to_date, calendar=calendar, register=register)
        external_stop = _deadline(clock, args.stop_before)
    except ValueError as exc:
        print(f"cannot plan F&O backfill: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        for unit in plan:
            print(f"{unit.label}\t{unit.url}")
        print(f"\n{len(plan)} sessions planned (no fetch performed)")
        return 0
    try:
        return _run_live(
            args,
            plan,
            settings=settings,
            clock=clock,
            calendar=calendar,
            register=register,
            external_stop=external_stop,
        )
    except HostBusyError as busy:
        print(f"F&O backfill refused to start: {busy}", file=sys.stderr)
        return 4


def _run_live(
    args: argparse.Namespace,
    plan: Sequence[FoSessionUnit],
    *,
    settings: Settings,
    clock: Clock,
    calendar: TradingCalendar,
    register: SourceRegister,
    external_stop: Callable[[], str | None],
) -> int:
    stop_state = {"stop": False}

    def handle(_signum: int, _frame: FrameType | None) -> None:
        stop_state["stop"] = True

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)

    with (
        connection(settings) as conn,
        leased_fetcher(
            [HOST],
            clock=clock,
            command="dataplatform.ingest.fo_backfill",
            settings=settings,
            register=register,
        ) as fetcher,
    ):
        master = IdentityStore(conn, clock=clock).load_master()
        runner = FoBackfillRunner(
            fetcher=fetcher,
            l0=L0Store(clock=clock, data_root=settings.data_root),
            sync=SyncStateStore(conn, clock=clock, calendar=calendar),
            commit=conn.commit,
            rollback=conn.rollback,
            master=master,
            should_stop=lambda: "stop signal received" if stop_state["stop"] else external_stop(),
            data_root=settings.data_root,
            max_sessions=args.max_sessions,
        )
        report = runner.run(plan)

    print(
        f"F&O backfill: {report.published} sessions published, {report.resumed} resumed, "
        f"{report.closed} closed, {report.requests} requests ({report.l0_reused} L0 reused), "
        f"{report.not_published} not published (404), {report.refused} refused, "
        f"{report.failed} failed; {report.contracts_written} contracts, "
        f"{report.aggregates_written} aggregates, {report.unresolved_underlyings} stock "
        f"underliers unresolved"
        + (f" — stopped early ({report.stopped_early})" if report.stopped_early else "")
        + (" — PARKED (FORBIDDEN_SPIKE)" if report.parked else "")
    )
    for label, message in report.failures[:100]:
        print(f"  {label}: {message}")
    if report.park_detail:
        print(report.park_detail, file=sys.stderr)
    return 3 if report.parked else 0


if __name__ == "__main__":
    sys.exit(main())
