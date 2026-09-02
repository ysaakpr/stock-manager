"""D1: the resumable, checkpointed backfill runner (M1.9).

One command backfills a date range for a source set, driving each session all the way down the
pipeline the other M1 tasks built — `fetch → L0 → parse → L1 → sync_state` — and it is written so
that a ten-year run can be killed at any point and restarted without losing, re-fetching or
skipping a single session. Four properties make that true, each mapped to an acceptance criterion:

* **`--dry-run` opens no socket.** The request plan is computed from the C.2 calendar alone: the
  expected-data dates in the range, each turned into the exact URL and L0 filename it would fetch.
  It touches neither the network nor Postgres, so it is the safe way to see — and count — what a
  real run would do before committing to it (the B1 "verify + sample, then full go" discipline).

* **Resume is read from `sync_state`, not a sidecar file.** The state machine (M1.3) already knows
  which `(source, date)` pairs are `PUBLISHED`; those are never re-fetched, and a retryable
  `FAILED` one comes back to `PENDING` on the next run. The checkpoint is the committed row, and
  the runner commits after every session, so the durable record of progress is the same table the
  status API and the trading interlock already read.

* **`--limit` samples across the whole range, not just its head.** A naive "first N" of a ten-year
  range never leaves 2016; `sample_dates` spreads the N evenly across the plan so a `--limit 60`
  run spans both bhavcopy eras (legacy and UDiFF) as B1 requires, exercising both parsers.

* **A 403 spike is a hard stop, not something to route around.** The fetcher (M1.2) counts refusals
  and raises `ForbiddenSpikeError` at the limit; the runner catches it, stops the whole run, and
  surfaces it — it never lowers the rate, changes the agent, or retries (AGENTIC_CONTEXT §8).

Offline by construction: the runner takes its `Fetcher`, `L0Store` and `SyncStateStore` by
injection, so a test wires a `RecordedTransport` and a scratch database and never opens a socket
(B8). `main` is the only place that builds the real, networked wiring.

The operator runbook is `ops/runbooks/backfill.md`.
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import FrameType
from typing import Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.identity.master import Exchange
from dataplatform.ingest.calendar import (
    CalendarCoverageError,
    TradingCalendar,
    trading_calendar,
)
from dataplatform.ingest.fetcher import (
    Fetcher,
    ForbiddenSpikeError,
    build_fetcher,
)
from dataplatform.ingest.models import ParseError, PriceRow
from dataplatform.ingest.nse import bhavcopy
from dataplatform.ingest.nse.bhavcopy_legacy import LEGACY_SOURCE_ID
from dataplatform.ingest.nse.bhavcopy_udiff import UDIFF_SOURCE_ID
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.l1 import write_prices_raw

__all__ = [
    "SOURCE_SETS",
    "BackfillReport",
    "BackfillRunner",
    "FetchRequest",
    "SourceSet",
    "build_plan",
    "main",
    "sample_dates",
]

_LOG = get_logger(__name__)

#: English month abbreviations for the legacy URL's `{MON}`, spelled out rather than handed to
#: `strftime("%b")`, whose output depends on `LC_TIME` — the same locale guard the parsers use.
_MON: Final[tuple[str, ...]] = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """One dated fetch the backfill would perform: which source, from where, into what L0 name.

    `state_source` is the `sync_state` source the session is tracked under — deliberately the
    era-independent source-set name (`nse_bhavcopy`), not the register's era id, so the trading
    interlock's `is_green` asks one question across the whole decade rather than two that meet at
    the 2024 cutover. `fetch_source` is the register id whose crawl policy and URL template the
    fetcher resolves (`nse_bhavcopy_legacy` vs `nse_bhavcopy_udiff`).
    """

    trade_date: date
    state_source: str
    fetch_source: str
    url: str
    filename: str


@dataclass(frozen=True, slots=True)
class SourceSet:
    """A named backfill target: how to turn a date into a fetch, and how to land what came back.

    A source set is the unit `--source` selects. It knows three things and nothing else: the
    `sync_state` source name it publishes under, how to build the `FetchRequest` for a given
    session (era dispatch lives here), and how to parse then write the fetched L0 payload. Keeping
    parse and write as two steps lets the runner move the row `FETCHED → VALIDATED → NORMALIZED`
    honestly and attribute a failure to the step that actually broke.
    """

    name: str
    build_request: Callable[[date, SourceRegister], FetchRequest]
    parse: Callable[[L0Store, L0Ref], Sequence[PriceRow]]
    write: Callable[[Sequence[PriceRow], Path | None], object]


# ── nse_bhavcopy source set ────────────────────────────────────────────────────────────────────


def _template_for(register: SourceRegister, source_id: str) -> str:
    """The verified URL template for a register source id, or a loud failure.

    The template is read from the register rather than hard-coded so a URL-pattern change is one
    edit in `source_register.yaml` (C.1), exactly as the other ingest modules read theirs.
    """
    source = next((s for s in register.sources if s.id == source_id), None)
    if source is None:
        raise KeyError(f"source {source_id!r} is not in the source register")
    return source.url_template


def _bhavcopy_request(trade_date: date, register: SourceRegister) -> FetchRequest:
    """Build the cash-bhavcopy fetch for one session, choosing the era's URL and register id.

    Dispatch is `bhavcopy.era_of`, the one place that knows the 2024-07-08 UDiFF cutover, so the
    boundary is resolved identically here and in the parser. The filled URL's last path segment is
    the L0 filename — which is what both eras already name their files by date, so it is unique per
    session by construction.
    """
    if bhavcopy.era_of(trade_date) == "legacy":
        fetch_source = LEGACY_SOURCE_ID
        url = (
            _template_for(register, fetch_source)
            .replace("{YYYY}", f"{trade_date:%Y}")
            .replace("{MON}", _MON[trade_date.month - 1])
            .replace("{DD}", f"{trade_date:%d}")
        )
    else:
        fetch_source = UDIFF_SOURCE_ID
        url = _template_for(register, fetch_source).replace("{YYYYMMDD}", f"{trade_date:%Y%m%d}")
    return FetchRequest(
        trade_date=trade_date,
        state_source=NSE_BHAVCOPY,
        fetch_source=fetch_source,
        url=url,
        filename=url.rsplit("/", 1)[-1],
    )


#: The `sync_state` source name for the NSE cash price core — era-independent on purpose (see
#: `FetchRequest.state_source`). This is the dataset id the daily loop names to `is_green`.
NSE_BHAVCOPY: Final = "nse_bhavcopy"


def _write_bhavcopy(rows: Sequence[PriceRow], data_root: Path | None) -> object:
    """Write one parsed NSE session to its `prices_raw` L1 partition (M1.8).

    No delivery join and no identity master: the bhavcopy carries ISIN natively, so the raw price
    partition stands on its own. The delivery `%` join (M1.6/M1.7) is the daily pipeline's (M1.10)
    concern and a later source set; a backfill of a decade of prices does not block on it.
    `data_root` is the runner's lake root, so L1 lands beside the L0 the payload came from.
    """
    return write_prices_raw(list(rows), exchange=Exchange.NSE, data_root=data_root)


SOURCE_SETS: Final[dict[str, SourceSet]] = {
    NSE_BHAVCOPY: SourceSet(
        name=NSE_BHAVCOPY,
        build_request=_bhavcopy_request,
        parse=lambda store, ref: bhavcopy.parse_l0(store, ref),
        write=_write_bhavcopy,
    ),
}


# ── planning (dry-run and sampling), pure and offline ────────────────────────────────────────


def sample_dates(dates: Sequence[date], limit: int | None) -> list[date]:
    """`limit` dates spread evenly across `dates`, always including the first and the last.

    `--limit` is for sampling, and a sample that is just the first N of a ten-year plan never
    leaves the legacy era. Even spacing is what makes `--limit 60` span both bhavcopy eras (B1):
    the picks are at proportional positions across the range, so the cutover always falls inside
    them. `None` or a limit at least the plan size returns the plan unchanged; a non-positive limit
    is a caller error, not an empty sample.
    """
    if limit is None or limit >= len(dates):
        return list(dates)
    if limit <= 0:
        raise ValueError(f"--limit must be positive, got {limit}")
    if limit == 1:
        return [dates[0]]
    last = len(dates) - 1
    # Proportional positions 0..last inclusive; dedupe defends the degenerate dense case where two
    # positions round to the same index, so the result is strictly increasing dates.
    picked = sorted({round(i * last / (limit - 1)) for i in range(limit)})
    return [dates[i] for i in picked]


def build_plan(
    source_set: SourceSet,
    from_date: date,
    to_date: date,
    *,
    calendar: TradingCalendar,
    register: SourceRegister,
    limit: int | None = None,
) -> list[FetchRequest]:
    """The exact request plan for a range: one `FetchRequest` per session that would be fetched.

    Pure and offline — this is what `--dry-run` prints and counts. The dates are the calendar's
    expected-data dates (sessions and Muhurat days; weekends and holidays owe no file and are not
    requests), sampled by `limit`, each mapped through the source set to its URL and L0 name. It
    opens no socket and no database: an uncovered range fails loud from the calendar rather than
    silently planning fetches for dates nobody can say traded.
    """
    sessions = calendar.expected_data_dates(from_date, to_date)
    chosen = sample_dates(sessions, limit)
    return [source_set.build_request(day, register) for day in chosen]


# ── the runner ───────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class BackfillReport:
    """What one `run` did — the counts an operator and a test both assert against.

    `requested` is how many sessions were in the plan; the rest partition the outcome. `published`
    and `skipped_published` together are the sessions now safely in L1. `hard_stopped` and
    `stopped_early` say *why* a run ended before its plan did: a 403 spike, or a graceful SIGINT.
    """

    source: str
    requested: int
    published: int = 0
    skipped_published: int = 0
    failed: int = 0
    hard_stopped: bool = False
    stopped_early: bool = False
    failures: list[tuple[date, str]] = field(default_factory=list)

    @property
    def processed(self) -> int:
        """Sessions this run actually drove down the pipeline (excludes resume-skips)."""
        return self.published + self.failed


class BackfillRunner:
    """Drives a source set's sessions through `fetch → L0 → parse → L1 → sync_state`, resumably.

    What it does: for each planned session, skips it if `sync_state` already has it `PUBLISHED`
    (resume), otherwise begins the row and advances it one state at a time, committing after every
    session so a kill loses at most the session in flight. A retryable failure is recorded and the
    run moves on; a 403 spike stops the whole run.
    What it assumes: its `Fetcher`, `L0Store` and `SyncStateStore` are wired to the same clock
    (B10), and `commit` durably persists the store's transaction — that commit is the checkpoint.
    What it never does: re-fetch a `PUBLISHED` date, open a socket of its own, or weaken the rate
    limit or the 403 hard stop to make progress.
    """

    def __init__(
        self,
        source_set: SourceSet,
        *,
        fetcher: Fetcher,
        l0: L0Store,
        sync: SyncStateStore,
        commit: Callable[[], None],
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self._set = source_set
        self._fetcher = fetcher
        self._l0 = l0
        self._sync = sync
        self._commit = commit
        self._should_stop = should_stop

    def run(self, requests: Sequence[FetchRequest]) -> BackfillReport:
        """Process every request in order, resuming, checkpointing and stopping per the class doc.

        Returns a `BackfillReport` whichever way the run ends — completed, gracefully stopped, or
        hard-stopped on a 403 spike — so the caller always has the counts. Re-raises nothing from
        an individual session: a session's failure is recorded in `sync_state` and counted, which
        is what lets a decade-long run survive one bad file.
        """
        report = BackfillReport(source=self._set.name, requested=len(requests))
        total = len(requests)
        for index, request in enumerate(requests, start=1):
            if self._should_stop():
                _LOG.warning(
                    "backfill.stopping",
                    source=self._set.name,
                    processed=report.processed,
                    remaining=total - index + 1,
                    reason="stop requested (SIGINT)",
                )
                report.stopped_early = True
                break
            try:
                self._process(request, index=index, total=total, report=report)
            except ForbiddenSpikeError as spike:
                # The hard stop (§4.1/§8): the fetcher already alerted and will refuse this host
                # for the life of the process. Record the session as a non-retryable failure so it
                # is not silently lost, then end the run — never lower the rate or rotate the agent.
                self._fail(request, str(spike), retryable=False, report=report)
                _LOG.critical(
                    "backfill.hard_stop",
                    source=self._set.name,
                    date=request.trade_date.isoformat(),
                    processed=report.processed,
                    error=str(spike),
                    state="HARD_STOPPED",
                )
                report.hard_stopped = True
                break
        _LOG.info(
            "backfill.done",
            source=self._set.name,
            requested=report.requested,
            published=report.published,
            skipped_published=report.skipped_published,
            failed=report.failed,
            hard_stopped=report.hard_stopped,
            stopped_early=report.stopped_early,
        )
        return report

    def _process(
        self, request: FetchRequest, *, index: int, total: int, report: BackfillReport
    ) -> None:
        """Drive one session, or record why it could not be driven. Never raises for a data error.

        Re-raises only `ForbiddenSpikeError`, which `run` treats as the whole-run hard stop; every
        other failure is caught, filed as a retryable `FAILED` row, committed and counted.
        """
        source = request.state_source
        day = request.trade_date

        existing = self._sync.get(source, day)
        if existing is not None and existing.state is SyncState.PUBLISHED:
            report.skipped_published += 1
            _LOG.info(
                "backfill.skip_published",
                source=source,
                date=day.isoformat(),
                progress=f"{index}/{total}",
                state="PUBLISHED",
            )
            return

        _LOG.info(
            "backfill.session_start",
            source=source,
            date=day.isoformat(),
            progress=f"{index}/{total}",
            url=request.url,
        )
        try:
            self._sync.begin(source, day)
            ref = self._fetcher.fetch(
                request.fetch_source, request.url, day, filename=request.filename
            )
            self._sync.mark_fetched(source, day, checksum=ref.sha256, l0_path=ref.key)

            rows = self._set.parse(self._l0, ref)
            self._sync.mark_validated(source, day)

            self._set.write(rows, self._l0.data_root)
            self._sync.mark_normalized(source, day)

            self._sync.mark_published(source, day)
            self._commit()
            report.published += 1
            _LOG.info(
                "backfill.session_published",
                source=source,
                date=day.isoformat(),
                progress=f"{index}/{total}",
                rows=len(rows),
                state="PUBLISHED",
            )
        except ForbiddenSpikeError:
            raise
        except ParseError as exc:
            # A parser that refuses this era or a truncated file: retryable=False would strand a
            # date a fixed parser could later read, so it stays retryable — the failure is loud in
            # sync_state either way (CLAUDE.md "fail loud").
            self._fail(request, f"parse failed: {exc}", retryable=True, report=report)
        except Exception as exc:  # fetch/write/DB — recorded, not swallowed; the run continues
            self._fail(request, f"{type(exc).__name__}: {exc}", retryable=True, report=report)

    def _fail(
        self, request: FetchRequest, message: str, *, retryable: bool, report: BackfillReport
    ) -> None:
        """Record one session's failure in `sync_state`, commit it, and count it.

        Rolls back the aborted session's uncommitted transaction first so the `FAILED` write lands
        on a clean transaction rather than one Postgres has already marked aborted.
        """
        source = request.state_source
        day = request.trade_date
        self._rollback()
        try:
            self._sync.begin(source, day)
            self._sync.mark_failed(source, day, message, retryable=retryable)
            self._commit()
        except Exception as exc:  # the DB itself is unwell; surface it rather than hide the run
            self._rollback()
            _LOG.error(
                "backfill.fail_record_failed",
                source=source,
                date=day.isoformat(),
                error=f"{type(exc).__name__}: {exc}",
            )
        report.failed += 1
        report.failures.append((day, message))
        _LOG.warning(
            "backfill.session_failed",
            source=source,
            date=day.isoformat(),
            retryable=retryable,
            error=message,
            state="FAILED",
        )

    def _rollback(self) -> None:
        """Best-effort rollback of the sync store's connection between sessions."""
        conn = getattr(self._sync, "_conn", None)
        rollback = getattr(conn, "rollback", None)
        if callable(rollback):
            rollback()


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def _install_sigint(state: dict[str, bool]) -> None:
    """Flip `state['stop']` on the first SIGINT so the runner stops after the current session.

    Graceful by design: the handler sets a flag the runner checks between sessions, so an in-flight
    session finishes (or fails) and commits rather than tearing down mid-write. A second SIGINT
    restores the default handler, so an impatient operator can still hard-kill.
    """

    def handle(_signum: int, _frame: FrameType | None) -> None:
        if state["stop"]:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            return
        state["stop"] = True
        print(
            "\nSIGINT received — finishing the current session and stopping; "
            "press Ctrl-C again to force-quit.",
            file=sys.stderr,
        )

    signal.signal(signal.SIGINT, handle)


def _print_plan(plan: Sequence[FetchRequest], *, source: str, sampled: bool) -> None:
    """Print the dry-run plan: one line per request, then the count. No socket, no database."""
    for request in plan:
        print(f"{request.trade_date.isoformat()}\t{request.fetch_source}\t{request.url}")
    label = "sampled requests" if sampled else "requests"
    print(f"\n{source}: {len(plan)} {label} planned (no fetch performed)")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the backfill runner.

    `--dry-run` prints and counts the plan without a socket or a database. Without it, the runner
    fetches: it builds the real networked wiring, installs the graceful-SIGINT handler, and drives
    the plan, committing after each session. Exit code is 0 on a clean or gracefully stopped run,
    and 3 on a 403 hard stop so an orchestrator can tell the two apart.
    """
    parser = argparse.ArgumentParser(prog="backfill", description=__doc__)
    parser.add_argument("--source", required=True, choices=sorted(SOURCE_SETS))
    parser.add_argument("--from", dest="from_date", required=True, type=date.fromisoformat)
    parser.add_argument("--to", dest="to_date", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the exact request plan and count without fetching anything",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="sample this many sessions, spread evenly across the range (spans both eras)",
    )
    args = parser.parse_args(argv)

    source_set = SOURCE_SETS[args.source]
    calendar = trading_calendar()
    register = load_register()

    try:
        plan = build_plan(
            source_set,
            args.from_date,
            args.to_date,
            calendar=calendar,
            register=register,
            limit=args.limit,
        )
    except CalendarCoverageError as exc:
        print(f"cannot plan backfill: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"cannot plan backfill: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        _print_plan(plan, source=args.source, sampled=args.limit is not None)
        return 0

    return _run_live(source_set, plan, calendar=calendar, register=register)


def _run_live(
    source_set: SourceSet,
    plan: Sequence[FetchRequest],
    *,
    calendar: TradingCalendar,
    register: SourceRegister,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> int:
    """Build the real wiring and run the plan against the network and Postgres.

    Split from `main` so the wiring is in one place and `main` stays about argument parsing. The
    connection is *not* autocommit: the runner commits explicitly after each session, which is what
    makes that commit the checkpoint the next run resumes from.
    """
    settings = get_settings() if settings is None else settings
    clock = SystemClock() if clock is None else clock
    stop_state = {"stop": False}
    _install_sigint(stop_state)

    fetcher = build_fetcher(clock=clock, settings=settings, register=register)
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    with connection(settings) as conn:
        sync = SyncStateStore(conn, clock=clock, calendar=calendar)
        runner = BackfillRunner(
            source_set,
            fetcher=fetcher,
            l0=l0,
            sync=sync,
            commit=conn.commit,
            should_stop=lambda: stop_state["stop"],
        )
        report = runner.run(plan)

    print(
        f"{report.source}: {report.published} published, "
        f"{report.skipped_published} already published, {report.failed} failed "
        f"of {report.requested} planned"
        + (" — HARD STOPPED on a 403 spike" if report.hard_stopped else "")
        + (" — stopped early on SIGINT" if report.stopped_early else "")
    )
    if report.failures:
        print(f"  first failures: {report.failures[:5]}", file=sys.stderr)
    return 3 if report.hard_stopped else 0


if __name__ == "__main__":
    sys.exit(main())
