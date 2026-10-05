"""Record `nse_delivery` in `sync_state` for sessions `price_rebuild` derived into L1.

``uv run python -m dataplatform.ingest.delivery_reconcile --from 2016-09-02 --to 2026-09-01``

The gap this closes: `price_rebuild` re-derives `prices_raw` — delivery columns included — from L0
and, by design, writes no `sync_state` row (its module docstring says why). So a session whose
delivery reached L1 only through a rebuild has the data and no record of it, and the D7 gap report
calls it `NEVER_ATTEMPTED`. On the server that was every session from 2016-09-02 to 2026-09-01:
2,477 entries, each one a real-looking miss for data that was sitting in L1.

**Why a reconcile and not a flag on the rebuild.** The rebuild's refusal to touch `sync_state` is
about rows that already exist — a PUBLISHED row is closed absolutely and a rebuild must not drive it
round again. A session with *no row at all* is a different case: nothing was ever recorded, so
recording the first lifecycle is not a re-publication, it is the bookkeeping the backfill would have
done had it been the path that wrote the partition. And it has to be checkable after the fact: the
rebuild has already run on the server, and re-running a 3,787-session rewrite of the lake to get
its side effect on Postgres would be the wrong trade. This reads the lake and writes only Postgres.

Every state it records is one it has evidence for, checked per session, the same edges the backfill
runner walks (`BackfillRunner._process`):

* **FETCHED** — the era's payload (the same `SourceSet` request the backfill and the rebuild use) is
  in L0. Its checksum and key are recorded, exactly as `mark_fetched` records them on a fetch.
* **VALIDATED** — that payload parses with its era's parser. The parser re-verifies the checksum and
  refuses a file stating another session, so a parse here is a real validation, not a formality.
* **NORMALIZED / PUBLISHED** — the session's `prices_raw` partition carries delivery on at least one
  NSE row. A partition written from the bhavcopy alone has delivery NULL on every row, and that is
  precisely the case this must not paper over.

What it never does:

* **Touch an existing row.** PUBLISHED is terminal; a FAILED row is the backfill's to retry; an
  in-flight row belongs to whoever is driving it. Only absent rows are written.
* **Mark a session without an L0 payload.** No payload is no evidence; the row stays absent and the
  gap report keeps saying `NEVER_ATTEMPTED`, which is the truth — the backfill owes it a fetch.
* **Mark a session PUBLISHED when L1 lacks its delivery.** That session needs `price_rebuild`, and
  the run names it rather than recording data that is not there.
* **Hide a payload that does not parse.** That is a real failure of a real payload, and it is
  recorded as one — FETCHED with its L0 key, then FAILED with the parser's own message, retryable,
  as the backfill records a `ParseError`. 2021-11-04 is the live example: NSE served 2021-11-03's
  file at that date's URL, the parser refuses it, and the gap report then explains it as
  `L0_PRESENT_L1_ABSENT` — a parser/source question at zero request cost, not a re-fetch.
* **Fetch.** There is no `Fetcher` here.

`--dry-run` runs every check and writes nothing; it is how to see the effect before applying it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import pyarrow.compute as pc
import pyarrow.parquet as pq

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import get_settings
from dataplatform.identity.master import Exchange
from dataplatform.ingest.backfill import NSE_DELIVERY, SOURCE_SETS, SourceSet
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.price_rebuild import plan_sessions
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import PRICES_RAW_DATASET
from dataplatform.store.paths import l1_partition_path

__all__ = [
    "DeliveryReconciler",
    "Outcome",
    "ReconcileReport",
    "delivery_rows_in_l1",
    "main",
]

_LOG = get_logger(__name__)


class Outcome(StrEnum):
    """What the reconcile found for one session, and therefore what it wrote."""

    HAS_ROW = "HAS_ROW"
    """A `sync_state` row already exists. Left exactly as it is."""

    PUBLISHED = "PUBLISHED"
    """Payload in L0, parsed, delivery in L1 — the full lifecycle is recorded."""

    PARSE_FAILED = "PARSE_FAILED"
    """Payload in L0 but it does not parse. Recorded FETCHED then FAILED, with the parser's reason."""

    NOT_DERIVED = "NOT_DERIVED"
    """Payload parses but L1 carries no delivery for the session. Nothing written: run the rebuild."""

    NO_PAYLOAD = "NO_PAYLOAD"
    """No payload in L0. Nothing written: the backfill owes this session a fetch."""


class SyncWriter(Protocol):
    """The slice of `SyncStateStore` the reconcile drives — the backfill runner's own calls."""

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


@dataclass(slots=True)
class ReconcileReport:
    """Per-outcome session lists for one run. Every requested session lands in exactly one."""

    requested: int = 0
    applied: bool = False
    sessions: dict[Outcome, list[date]] = field(
        default_factory=lambda: {outcome: [] for outcome in Outcome}
    )
    failures: list[tuple[date, str]] = field(default_factory=list)

    def count(self, outcome: Outcome) -> int:
        """How many sessions ended in `outcome`."""
        return len(self.sessions[outcome])


def delivery_rows_in_l1(session: date, *, data_root: Path | None = None) -> int:
    """NSE rows in the session's `prices_raw` partition that carry a delivery quantity.

    Zero for an absent partition. Reads two columns, not the partition: this is a presence check,
    and a decade of it should cost seconds. BSE rows are excluded because the partition is shared
    and BSE never carries NSE delivery — counting them could only ever add noise.
    """
    path = l1_partition_path(PRICES_RAW_DATASET, session, data_root=data_root)
    if not path.exists():
        return 0
    table = pq.read_table(path, columns=["exchange", "deliv_qty"])
    nse = table.filter(pc.equal(table["exchange"], Exchange.NSE.value))
    return nse.num_rows - nse["deliv_qty"].null_count


class DeliveryReconciler:
    """Walk sessions and record the `nse_delivery` lifecycle each one has evidence for.

    `commit` is called after each session's writes, so an interrupted run keeps what it recorded
    and a re-run skips those sessions as `HAS_ROW`. With `apply=False` nothing is written and
    `commit` is never called — every check still runs, so a dry run reports the same outcomes.
    """

    def __init__(
        self,
        *,
        l0: L0Store,
        register: SourceRegister,
        sync: SyncWriter,
        commit: Callable[[], None],
        apply: bool,
        data_root: Path | None = None,
        source_set: SourceSet[Any] | None = None,
    ) -> None:
        self._set = source_set if source_set is not None else SOURCE_SETS[NSE_DELIVERY]
        self._l0 = l0
        self._register = register
        self._sync = sync
        self._commit = commit
        self._apply = apply
        self._data_root = data_root

    def run(self, sessions: Sequence[date]) -> ReconcileReport:
        """Reconcile every session, ascending, and report what each one was."""
        report = ReconcileReport(requested=len(sessions), applied=self._apply)
        for session in sorted(sessions):
            outcome = self._one(session, report)
            report.sessions[outcome].append(session)
        _LOG.info(
            "delivery_reconcile.done",
            source=self._set.name,
            applied=self._apply,
            requested=report.requested,
            **{outcome.value.lower(): report.count(outcome) for outcome in Outcome},
        )
        return report

    def _one(self, session: date, report: ReconcileReport) -> Outcome:
        source = self._set.name
        if self._sync.get(source, session) is not None:
            return Outcome.HAS_ROW

        request = self._set.build_request(session, self._register)
        if not self._l0.exists(request.fetch_source, session, request.filename):
            _LOG.info(
                "delivery_reconcile.no_payload",
                source=source,
                date=session.isoformat(),
                fetch_source=request.fetch_source,
                filename=request.filename,
                state="ABSENT",
            )
            return Outcome.NO_PAYLOAD

        ref = self._l0.ref_for(request.fetch_source, session, request.filename)
        try:
            rows = self._set.parse(self._l0, ref)
            if not rows:
                raise ParseError(f"{request.filename}: parsed to zero delivery rows")
        except Exception as exc:  # recorded as the backfill records it, never swallowed
            message = (
                f"parse failed: {exc}"
                if isinstance(exc, ParseError)
                else f"{type(exc).__name__}: {exc}"
            ) + " (recorded by delivery_reconcile from the stored L0 payload)"
            report.failures.append((session, message))
            _LOG.warning(
                "delivery_reconcile.parse_failed",
                source=source,
                date=session.isoformat(),
                l0_key=ref.key,
                error=message,
                state="FAILED",
                applied=self._apply,
            )
            if self._apply:
                self._sync.begin(source, session)
                self._sync.mark_fetched(source, session, checksum=ref.sha256, l0_path=ref.key)
                self._sync.mark_failed(source, session, message, retryable=True)
                self._commit()
            return Outcome.PARSE_FAILED

        in_l1 = delivery_rows_in_l1(session, data_root=self._data_root)
        if in_l1 == 0:
            _LOG.warning(
                "delivery_reconcile.not_derived",
                source=source,
                date=session.isoformat(),
                parsed=len(rows),
                state="ABSENT",
                reason="payload parses but prices_raw carries no NSE delivery; run price_rebuild",
            )
            return Outcome.NOT_DERIVED

        if self._apply:
            self._sync.begin(source, session)
            self._sync.mark_fetched(source, session, checksum=ref.sha256, l0_path=ref.key)
            self._sync.mark_validated(source, session)
            self._sync.mark_normalized(source, session)
            self._sync.mark_published(source, session)
            self._commit()
        _LOG.info(
            "delivery_reconcile.published",
            source=source,
            date=session.isoformat(),
            parsed=len(rows),
            delivery_in_l1=in_l1,
            state="PUBLISHED",
            applied=self._apply,
        )
        return Outcome.PUBLISHED


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Reads L0, L1 and `sync_state`; writes `sync_state` only, and only with
    `--apply`. Never the network, never the lake.

    Exit 0 when every session ended PUBLISHED or HAS_ROW; 1 when this run left any NO_PAYLOAD,
    NOT_DERIVED or PARSE_FAILED, so a caller cannot read a partial reconcile as a clean one. A row
    recorded FAILED by an earlier run is HAS_ROW here — the gap report is where it stays visible.
    """
    parser = argparse.ArgumentParser(prog="delivery_reconcile", description=__doc__)
    parser.add_argument("--from", dest="from_date", required=True, type=date.fromisoformat)
    parser.add_argument("--to", dest="to_date", required=True, type=date.fromisoformat)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="run every check, write nothing")
    mode.add_argument("--apply", action="store_true", help="write the sync_state rows")
    args = parser.parse_args(argv)
    if args.to_date < args.from_date:
        print(f"error: --to {args.to_date} is before --from {args.from_date}", file=sys.stderr)
        return 2

    try:
        sessions = plan_sessions(args.from_date, args.to_date)
    except Exception as exc:  # a calendar that does not cover the range
        print(f"cannot plan reconcile: {exc}", file=sys.stderr)
        return 2

    settings = get_settings()
    clock: Clock = SystemClock()
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    with connection(settings) as conn:
        if not args.apply:
            conn.execute("SET TRANSACTION READ ONLY")
        reconciler = DeliveryReconciler(
            l0=l0,
            register=load_register(),
            sync=SyncStateStore(conn, clock=clock),
            commit=conn.commit,
            apply=args.apply,
            data_root=settings.data_root,
        )
        report = reconciler.run(sessions)
        if not args.apply:
            conn.rollback()

    verb = "recorded" if args.apply else "would record (dry run, nothing written)"
    print(f"delivery_reconcile: {report.requested} sessions in range; {verb}:")
    for outcome in Outcome:
        days = report.sessions[outcome]
        shown = ", ".join(day.isoformat() for day in days[:5])
        more = f" (+{len(days) - 5} more)" if len(days) > 5 else ""
        print(f"  {outcome.value:<13} {len(days):>5}" + (f"  {shown}{more}" if days else ""))
    for day, message in report.failures[:5]:
        print(f"  {day.isoformat()}: {message}", file=sys.stderr)
    unfinished = (Outcome.NO_PAYLOAD, Outcome.NOT_DERIVED, Outcome.PARSE_FAILED)
    return 1 if any(report.count(outcome) for outcome in unfinished) else 0


if __name__ == "__main__":
    sys.exit(main())
