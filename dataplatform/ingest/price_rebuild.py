"""Re-derive `prices_raw` from immutable L0 — the delivery columns, without a single request.

``uv run python -m dataplatform.ingest.price_rebuild --from 2016-09-01 --to 2026-09-04``

The gap this closes: L1 `prices_raw` carries `deliv_qty` / `deliv_pct`, and the delivery join
happens at *write* time — `write_prices_raw` takes the session's price rows and its delivery rows
together, because a delivery figure is a column on a price row and not a dataset of its own. So a
store whose partitions were written from the bhavcopy alone has those two columns NULL on every row,
and no later patch can fill them: there is no "add delivery to an existing partition" path, by
design. The partition has to be built again from both inputs. On the server that is 4,274,581 NSE
EQ rows with delivery NULL against the laptop's 3,363,340 filled — the same lake, one column apart,
because the server's NSE L1 arrived as a copy of a dump taken before the delivery backfill and the
delivery payloads were rsync'd afterwards.

**Why this is a module and not a `--from-l0` flag on the backfill runner.** `SyncState.PUBLISHED` is
absolutely terminal (`dataplatform.status.sync_state.LEGAL_TRANSITIONS`) and the reason is written
down there: L0 is immutable, so the same `(source, date)` can never yield different bytes, and there
is nothing to re-publish. A rebuild is not another ingestion attempt on those bytes. It is a
re-derivation of a *derived* store from bytes already fetched, validated and published — the same
operation `lineage_rebuild` and the XBRL `--rebuild-from-l0` perform, and like them it writes no
`sync_state` row and drives no lifecycle. Bending the state machine to let a PUBLISHED row go round
again would erase the one guarantee it exists to make.

What it does, per session in the range: ask the *same* `SourceSet` the backfill uses
(`SOURCE_SETS[NSE_DELIVERY]`) for this era's request, read that payload back out of L0, parse it
with that era's parser, and hand the rows to that set's write step, which reads the session's stored
bhavcopy and rebuilds the partition around both. Reusing the source set rather than re-spelling any
of it is deliberate: a rebuild that parsed or wrote differently from the ingest path would produce a
lake that no longer matches its own source of truth, and the 2024-07-08 UDiFF cutover and the
2019-09-30 MTO/sec_bhavdata boundary are exactly the details an independent re-spelling gets wrong.

What it never does: fetch. There is no `Fetcher` here and no network wiring to build. A session
whose payload is not in L0 is counted and named, not fetched — filling that hole is the backfill's
job, and doing it silently here would turn an offline rebuild into an unbudgeted campaign against a
live host. It also never writes an adjusted price, resolves a symbol by name alone, or drops a
delivery row.

Bounded by the identity master, and the report says by how much. A delivery row carries a symbol and
a series, never an ISIN, so it can only be placed through D2 as of its own trade date (invariant
#2). Whatever the master cannot resolve is quarantined and counted — `delivery_unresolved` — so the
run's own output states its coverage rather than leaving it to be discovered in a backtest. Rows the
master resolves to an ISIN that did not trade that session are `delivery_orphaned` and quarantined
too; the four counts reconcile to the rows parsed, which is the "never dropped silently" contract
made checkable.

Idempotent: `write_prices_raw` sorts on a total key, so re-running over the same L0 leaves a
byte-identical partition, and the other exchange's rows in the shared date partition are read back
and written out unchanged.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.identity.master import IdentityMaster, IdentityStore
from dataplatform.ingest.backfill import (
    NSE_DELIVERY,
    SOURCE_SETS,
    SourceSet,
    WriteContext,
)
from dataplatform.ingest.calendar import TradingCalendar, trading_calendar
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import PricesRawWriteReport

_LOG = get_logger(__name__)


@dataclass(slots=True)
class PriceRebuildReport:
    """What one rebuild run did, and what it could not do.

    The delivery counts are summed across every session rebuilt, so `delivery_rows ==
    delivery_joined + delivery_unresolved + delivery_orphaned` holds over the whole run just as it
    does per partition. `no_payload` sessions are the ones whose L0 is missing — a fetch this
    rebuild deliberately does not perform — and `failed` are the ones whose payload is present but
    could not be parsed or written, each with its reason in `failures`.
    """

    requested: int = 0
    rebuilt: int = 0
    no_payload: int = 0
    failed: int = 0
    rows_written: int = 0
    delivery_rows: int = 0
    delivery_joined: int = 0
    delivery_unresolved: int = 0
    delivery_orphaned: int = 0
    missing: list[date] = field(default_factory=list)
    failures: list[tuple[date, str]] = field(default_factory=list)

    @property
    def join_rate(self) -> float:
        """Share of parsed delivery rows that landed on a price row, as a fraction."""
        return self.delivery_joined / self.delivery_rows if self.delivery_rows else 0.0

    def record(self, write_report: PricesRawWriteReport) -> None:
        """Fold one partition's write report into the run totals."""
        self.rebuilt += 1
        self.rows_written += write_report.rows_written
        self.delivery_rows += write_report.delivery_rows
        self.delivery_joined += write_report.delivery_joined
        self.delivery_unresolved += write_report.delivery_unresolved
        self.delivery_orphaned += write_report.delivery_orphaned


class PriceRebuilder:
    """Re-derive one session's `prices_raw` partition at a time, from L0 only.

    Holds the source set, the lake and the write context; `run` walks a session list. Every failure
    is per-session: a date whose payload is unreadable is recorded and the walk continues, because a
    ten-year rebuild that aborts on one bad file has to be restarted from the beginning, and the
    write is idempotent so a re-run costs only the sessions it redoes.
    """

    def __init__(
        self,
        *,
        l0: L0Store,
        register: SourceRegister,
        master: IdentityMaster,
        data_root: Path | None = None,
        source_set: SourceSet[Any] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self._set = source_set if source_set is not None else SOURCE_SETS[NSE_DELIVERY]
        self._l0 = l0
        self._register = register
        self._ctx = WriteContext(l0=l0, data_root=data_root, master=master, register=register)
        self._should_stop = should_stop if should_stop is not None else lambda: False

    def run(self, sessions: Sequence[date]) -> PriceRebuildReport:
        """Rebuild every session in `sessions`, ascending, and report the run."""
        report = PriceRebuildReport(requested=len(sessions))
        total = len(sessions)
        for index, session in enumerate(sorted(sessions), start=1):
            if self._should_stop():
                _LOG.info("price_rebuild.stopped_early", processed=index - 1, total=total)
                break
            self._rebuild_one(session, index=index, total=total, report=report)
        _LOG.info(
            "price_rebuild.done",
            requested=report.requested,
            rebuilt=report.rebuilt,
            no_payload=report.no_payload,
            failed=report.failed,
            delivery_rows=report.delivery_rows,
            delivery_joined=report.delivery_joined,
            delivery_unresolved=report.delivery_unresolved,
            delivery_orphaned=report.delivery_orphaned,
        )
        return report

    def _rebuild_one(
        self, session: date, *, index: int, total: int, report: PriceRebuildReport
    ) -> None:
        """Rebuild one partition, or record why it could not be. Never raises for a data error."""
        request = self._set.build_request(session, self._register)
        if not self._l0.exists(request.fetch_source, session, request.filename):
            report.no_payload += 1
            report.missing.append(session)
            _LOG.info(
                "price_rebuild.no_payload",
                date=session.isoformat(),
                source=request.fetch_source,
                filename=request.filename,
                progress=f"{index}/{total}",
                reason="no L0 payload for this session; the backfill fetches, this does not",
            )
            return
        try:
            ref = self._l0.ref_for(request.fetch_source, session, request.filename)
            rows = self._set.parse(self._l0, ref)
            written = self._set.write(rows, self._ctx)
        except Exception as exc:  # one unreadable file must not end a decade-long walk
            report.failed += 1
            report.failures.append((session, f"{type(exc).__name__}: {exc}"))
            _LOG.error(
                "price_rebuild.session_failed",
                date=session.isoformat(),
                progress=f"{index}/{total}",
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        if not isinstance(written, PricesRawWriteReport):  # pragma: no cover - contract guard
            report.failed += 1
            report.failures.append((session, f"write returned {type(written).__name__}"))
            return
        report.record(written)
        _LOG.info(
            "price_rebuild.session_rebuilt",
            date=session.isoformat(),
            progress=f"{index}/{total}",
            rows=written.rows_written,
            preserved=written.rows_preserved,
            delivery_joined=written.delivery_joined,
            delivery_unresolved=written.delivery_unresolved,
            delivery_orphaned=written.delivery_orphaned,
        )


def plan_sessions(start: date, end: date, *, calendar: TradingCalendar | None = None) -> list[date]:
    """Every trading session in `[start, end]`, ascending — the rebuild's work list.

    Uses the same trading calendar the backfill plans against, so the rebuild asks for exactly the
    dates the ingest path fetched and never invents a session the exchange did not hold.
    """
    cal = calendar if calendar is not None else trading_calendar()
    return list(cal.expected_sessions(start, end))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Offline: reads L0 and Postgres (for the identity master), never the network.

    `--dry-run` reports how many sessions in the range have a delivery payload in L0 and how many do
    not, without opening a database or writing a partition — the cheap "is this rebuild worth
    starting" question.
    """
    parser = argparse.ArgumentParser(prog="price_rebuild", description=__doc__)
    parser.add_argument("--from", dest="from_date", required=True, type=date.fromisoformat)
    parser.add_argument("--to", dest="to_date", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count the sessions whose delivery payload is in L0, and write nothing",
    )
    args = parser.parse_args(argv)
    if args.to_date < args.from_date:
        print(f"error: --to {args.to_date} is before --from {args.from_date}", file=sys.stderr)
        return 2

    register = load_register()
    try:
        sessions = plan_sessions(args.from_date, args.to_date)
    except Exception as exc:  # a calendar that does not cover the range
        print(f"cannot plan rebuild: {exc}", file=sys.stderr)
        return 2
    if not sessions:
        print(f"no trading sessions in [{args.from_date}, {args.to_date}]", file=sys.stderr)
        return 2

    settings = get_settings()
    clock: Clock = SystemClock()
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    source_set = SOURCE_SETS[NSE_DELIVERY]

    if args.dry_run:
        present = sum(
            1
            for session in sessions
            if l0.exists(
                source_set.build_request(session, register).fetch_source,
                session,
                source_set.build_request(session, register).filename,
            )
        )
        print(
            f"{len(sessions)} sessions in range: {present} with a delivery payload in L0, "
            f"{len(sessions) - present} without (those need the backfill, not this)"
        )
        return 0

    return _run(sessions, l0=l0, register=register, settings=settings, clock=clock)


def _run(
    sessions: Sequence[date],
    *,
    l0: L0Store,
    register: SourceRegister,
    settings: Settings,
    clock: Clock,
) -> int:
    """Load the identity master and drive the rebuild. Split from `main` so wiring is in one place.

    The master is loaded once for the whole run rather than per session: it is an as-of structure
    and every read passes the session's own trade date, so one load serves the decade and reloading
    it 2,470 times would only cost time.
    """
    with connection(settings) as conn:
        master = IdentityStore(conn, clock=clock).load_master()
        rebuilder = PriceRebuilder(
            l0=l0, register=register, master=master, data_root=settings.data_root
        )
        report = rebuilder.run(sessions)

    print(
        f"price_rebuild: {report.rebuilt} rebuilt, {report.no_payload} without an L0 payload, "
        f"{report.failed} failed of {report.requested} sessions"
    )
    print(
        f"  delivery rows {report.delivery_rows}: {report.delivery_joined} joined "
        f"({report.join_rate:.1%}), {report.delivery_unresolved} unresolved, "
        f"{report.delivery_orphaned} orphaned (both quarantined, never dropped)"
    )
    if report.missing:
        shown = ", ".join(day.isoformat() for day in report.missing[:5])
        print(f"  first sessions with no L0 payload: {shown}", file=sys.stderr)
    if report.failures:
        print(f"  first failures: {report.failures[:5]}", file=sys.stderr)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
