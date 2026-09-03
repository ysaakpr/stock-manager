"""D3 (M9.1): the resumable corporate-action backfill runner over the L1 price window.

M2.2 built the parse->resolve->persist path for one payload; M2.3 the reconciliation; M2.4 the
factor chain. What none of them own is a *runner* that goes and gets the terms: the store has no
corporate-action rows until something fetches the feeds, reconciles the two exchanges' descriptions
of each action, and drives the M2.4 recompute so ``adjustment_factors`` is populated
(``ops/BACKLOG.md``, M2.2). This module is that runner.

It mirrors two things already in the platform, on purpose:

* **``nse/fii_dii.py:ingest_day``** — one fetch unit is driven ``fetch -> L0 -> parse -> persist``
  through the §4.4 sync-state machine, every failure recorded on the row *and* re-raised so both
  the status API and the caller see it, and the L0 checksum re-verified on the way back in
  (invariant #1).

* **``backfill.py``'s ``SourceSet``/``BackfillRunner`` loop** — resume is read from ``sync_state``
  (a ``PUBLISHED`` unit is never re-fetched), the runner commits after every unit so the checkpoint
  is the committed row, ``--dry-run`` opens no socket, and a **403 spike is a hard stop** the runner
  refuses to route around (AGENTIC_CONTEXT §8) — here it *parks* with an enumerated cause rather
  than lowering the rate or rotating the agent (like M1.13's gated bulk run).

The two feeds are shaped differently and the plan reflects it (Source Register, §4.1 row 5):

* **NSE is per-date-range.** ``corporates-corporateActions?from_date=&to_date=`` returns every
  equity action in a window, so the NSE plan is a handful of date chunks across 2016-09..2026-09.
* **BSE is per-scrip.** ``DefaultData/w?scripcode=`` returns one scrip's actions, so the BSE plan is
  one unit per BSE scrip present in ``prices_raw`` — the scrips are resolved from the ISINs the
  price window actually holds, through the D2 identity master (invariant #2), never a raw symbol.

Neither feed silently drops a row: an unclassifiable purpose string goes to M2.1's manual-entry
queue and an unresolvable identity to the unresolved list, both surfaced in the coverage report,
and a genuine ratio/ex-date disagreement between the exchanges goes to M2.3's reconciliation queue
(``quality_flag``) rather than being guessed at — an unreconciled action never reaches a factor.

Offline by construction (B8): the runner takes its ``Fetcher``, ``L0Store``, ``SyncStateStore`` and
``Connection`` by injection, so a test wires a ``RecordedTransport`` and an in-memory store and
never opens a socket. ``main`` is the only place that builds the real, networked wiring, and the
10-year *execution* over the full universe is a bulk-fetch campaign reserved to the owner by B1 —
this module builds and unit-verifies it; a human gives the go.

Money is ``Decimal`` (inherited from the terms models). Time is injected (B10). The operator
runbook is ``ops/runbooks/ca_backfill.md``.
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.corpactions.recompute import RecomputeResult, recompute_isins
from dataplatform.corpactions.reconcile import (
    persist_reconciliation,
    reconcile,
)
from dataplatform.identity.master import Exchange, IdentityMaster, IdentityStore
from dataplatform.ingest.bse import corp_actions as bse_ca
from dataplatform.ingest.calendar import (
    CalendarCoverageError,
    TradingCalendar,
    trading_calendar,
)
from dataplatform.ingest.corp_actions import (
    CaParseResult,
    build_scrip_index,
    load_corporate_actions,
    write_corporate_actions,
)
from dataplatform.ingest.fetcher import (
    Fetcher,
    ForbiddenSpikeError,
    build_fetcher,
)
from dataplatform.ingest.nse import corp_actions as nse_ca
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import Connection, connection
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import read_prices_raw

__all__ = [
    "BSE_STATE_PREFIX",
    "NSE_STATE_SOURCE",
    "CaBackfillReport",
    "CaBackfillRunner",
    "CaFetchUnit",
    "FinalizeCounts",
    "ParkReason",
    "bse_scrips_for_isins",
    "build_bse_units",
    "build_nse_units",
    "build_plan",
    "finalize_reconcile_and_recompute",
    "isins_in_price_window",
    "main",
    "render_report",
]

_LOG = get_logger(__name__)

#: The `sync_state` source the NSE date-chunk units are tracked under. The register id itself, so a
#: chunk's checkpoint reads the same way the daily-forward CA ingest's would.
NSE_STATE_SOURCE: Final = nse_ca.SOURCE_ID

#: BSE units are per-scrip, and `sync_state` keys on `(source, logical_date)` — a date the per-scrip
#: fetch does not have. So each scrip is tracked under its own state source `bse_corp_actions/N`
#: (a chosen state-source name, exactly as `backfill.py`'s `FetchRequest.state_source` is), and the
#: run-window's start date is the shared logical anchor. This keeps one uniform resume mechanism —
#: "is this unit's `sync_state` row PUBLISHED?" — across both differently-shaped feeds.
BSE_STATE_PREFIX: Final = f"{bse_ca.SOURCE_ID}/"

#: How many months of the window one NSE fetch covers. Twelve keeps the request count small (a
#: ten-year window is ~10 NSE fetches) while staying well inside a single response's size; the feed
#: has no documented page size, so a year at a time is the conservative bound.
DEFAULT_NSE_CHUNK_MONTHS: Final = 12


class ParkReason(StrEnum):
    """Why a run stopped short of its plan and handed control back to a human.

    One member today — the 403 spike — but an enum rather than a bare bool so the park cause is
    *enumerated* (the acceptance criterion) and a second reserved-decision stop later reads the same
    way. A parked run is never a silent skip: the reason and the unit it stopped on are on the
    report, so `main` can print an actionable cause and exit non-zero.
    """

    FORBIDDEN_SPIKE = "FORBIDDEN_SPIKE"
    """The fetcher counted enough consecutive 403s on one host to trip its spike hard stop. The
    process will not talk to that host again; resuming needs a human to clear the block first
    (AGENTIC_CONTEXT §8), not a lower rate or a rotated agent."""


@dataclass(frozen=True, slots=True)
class CaFetchUnit:
    """One dated fetch the CA backfill performs, plus how to parse and checkpoint what comes back.

    A unit is the resume grain: `state_source`/`logical_date` is its `sync_state` key, so a
    `PUBLISHED` unit is skipped on the next run. `exchange` selects the parser (NSE resolves its
    native ISIN through the master, BSE resolves its scrip through the scrip index), and `label` is
    the human-readable name it appears under in logs and the coverage report.
    """

    exchange: Exchange
    state_source: str
    logical_date: date
    fetch_source: str
    url: str
    filename: str
    label: str
    scrip_code: str | None = None


@dataclass(slots=True)
class CaBackfillReport:
    """What one `run` did — the counts an operator and the acceptance test both assert against.

    `requested` is the plan size; `published + skipped_published` are the units now safely landed.
    `queued`/`unresolved` carry M2.1's manual-entry queue and the unresolvable identities forward so
    the coverage report can name them rather than let them vanish. A `park_reason` set means the run
    stopped on a reserved-decision block (`park_detail` enumerates it); `failures` holds the
    per-unit data errors that did *not* stop the run.
    """

    requested: int
    published: int = 0
    skipped_published: int = 0
    failed: int = 0
    actions_persisted: int = 0
    queued: int = 0
    unresolved: int = 0
    touched_isins: set[str] = field(default_factory=set)
    park_reason: ParkReason | None = None
    park_detail: str | None = None
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def parked(self) -> bool:
        """True when the run handed a reserved decision back to a human rather than finishing."""
        return self.park_reason is not None

    @property
    def processed(self) -> int:
        """Units this run actually drove down the pipeline (excludes resume-skips)."""
        return self.published + self.failed


@dataclass(frozen=True, slots=True)
class FinalizeCounts:
    """What the reconcile-and-recompute finalize step changed after the units landed.

    `reconciled`/`queued` are the M2.3 verdicts over the whole stored set; `isins_recomputed` and
    `factor_rows` are the M2.4 rewrite — `factor_rows` being non-zero is the "adjustment_factors is
    non-empty for a ratio-bearing action" acceptance criterion, made a count.
    """

    reconciled: int = 0
    queued: int = 0
    flags_written: int = 0
    isins_recomputed: int = 0
    factor_rows: int = 0
    l2_invalidated: int = 0


# ── planning (pure and offline) ────────────────────────────────────────────────────────────────


def _ca_template(register: SourceRegister, source_id: str) -> str:
    """The verified URL template for a CA source id, read from the register (never hard-coded)."""
    source = next((s for s in register.sources if s.id == source_id), None)
    if source is None:
        raise KeyError(f"source {source_id!r} is not in the source register")
    return source.url_template


def _ddmmyyyy(value: date) -> str:
    """`DD-MM-YYYY`, the format the NSE CA endpoint's `from_date`/`to_date` take."""
    return f"{value:%d-%m-%Y}"


def _chunk_bounds(from_date: date, to_date: date, *, months: int) -> list[tuple[date, date]]:
    """Split `[from_date, to_date]` into inclusive windows of at most `months` months.

    Boundaries are computed in whole months from `from_date` so the same range always yields the
    same chunks (a stable plan is a resumable plan). The last chunk is clamped to `to_date`.
    """
    if from_date > to_date:
        raise ValueError(f"from_date {from_date} is after to_date {to_date}")
    if months < 1:
        raise ValueError(f"chunk months must be >= 1, got {months}")
    bounds: list[tuple[date, date]] = []
    start = from_date
    while start <= to_date:
        total = (start.year * 12 + (start.month - 1)) + months
        year, month = divmod(total, 12)
        # One day before the chunk's month boundary is the inclusive end of the window.
        boundary = date(year, month + 1, 1)
        end = min(boundary - timedelta(days=1), to_date)
        bounds.append((start, end))
        start = boundary
    return bounds


def build_nse_units(
    from_date: date,
    to_date: date,
    *,
    register: SourceRegister,
    chunk_months: int = DEFAULT_NSE_CHUNK_MONTHS,
) -> list[CaFetchUnit]:
    """The NSE date-chunk fetch units for a window — one per `chunk_months` slice.

    The NSE feed is per-date-range, so a whole window's actions come back in a handful of requests.
    Each chunk is checkpointed under `nse_corp_actions` keyed on the chunk's start date.
    """
    template = _ca_template(register, nse_ca.SOURCE_ID)
    units: list[CaFetchUnit] = []
    for start, end in _chunk_bounds(from_date, to_date, months=chunk_months):
        url = template.replace("from_date={DD-MM-YYYY}", f"from_date={_ddmmyyyy(start)}").replace(
            "to_date={DD-MM-YYYY}", f"to_date={_ddmmyyyy(end)}"
        )
        if "{DD-MM-YYYY}" in url:  # both placeholders must have been filled
            raise ValueError(f"NSE CA template left a placeholder unfilled: {url!r}")
        units.append(
            CaFetchUnit(
                exchange=Exchange.NSE,
                state_source=NSE_STATE_SOURCE,
                logical_date=start,
                fetch_source=nse_ca.SOURCE_ID,
                url=url,
                filename=f"corporateActions_{start:%Y%m%d}_{end:%Y%m%d}.json",
                label=f"NSE {start.isoformat()}..{end.isoformat()}",
            )
        )
    return units


def build_bse_units(
    scrips: Sequence[str],
    *,
    register: SourceRegister,
    anchor: date,
) -> list[CaFetchUnit]:
    """The BSE per-scrip fetch units — one per scrip, checkpointed under its own state source.

    `anchor` is the shared logical date the per-scrip `sync_state` rows key on (see
    `BSE_STATE_PREFIX`); the scrip itself lives in the state-source suffix so each scrip resumes
    independently. Scrips are taken in a stable (sorted, de-duplicated) order.
    """
    template = _ca_template(register, bse_ca.SOURCE_ID)
    units: list[CaFetchUnit] = []
    for scrip in sorted(set(scrips)):
        url = template.replace("{SCRIP_CD}", scrip)
        if "{SCRIP_CD}" in url:
            raise ValueError(f"BSE CA template left a placeholder unfilled: {url!r}")
        units.append(
            CaFetchUnit(
                exchange=Exchange.BSE,
                state_source=f"{BSE_STATE_PREFIX}{scrip}",
                logical_date=anchor,
                fetch_source=bse_ca.SOURCE_ID,
                url=url,
                filename=f"defaultdata_{scrip}.json",
                label=f"BSE scrip {scrip}",
                scrip_code=scrip,
            )
        )
    return units


def build_plan(
    from_date: date,
    to_date: date,
    scrips: Sequence[str],
    *,
    register: SourceRegister,
    chunk_months: int = DEFAULT_NSE_CHUNK_MONTHS,
    limit: int | None = None,
) -> list[CaFetchUnit]:
    """The full CA backfill plan for a window: NSE date chunks first, then the BSE scrips.

    Pure and offline — this is what `--dry-run` prints and counts. `limit` truncates the plan (for
    a bounded sample run under B1's verify-then-go discipline); NSE chunks come first so a small
    limit still exercises the date-range feed.
    """
    plan = [
        *build_nse_units(from_date, to_date, register=register, chunk_months=chunk_months),
        *build_bse_units(scrips, register=register, anchor=from_date),
    ]
    if limit is not None:
        if limit <= 0:
            raise ValueError(f"--limit must be positive, got {limit}")
        plan = plan[:limit]
    return plan


def isins_in_price_window(
    calendar: TradingCalendar,
    from_date: date,
    to_date: date,
    *,
    data_root: Path | None = None,
) -> set[str]:
    """Every ISIN with a `prices_raw` row somewhere in the window — the names to backfill CAs for.

    Reads the L1 partitions the calendar says traded; a partition that was never written is a gap
    for D7 to explain, not an error here, so it is skipped. The result is the universe the BSE
    per-scrip plan and the NSE window are meant to cover.
    """
    isins: set[str] = set()
    for day in calendar.expected_data_dates(from_date, to_date):
        try:
            rows = read_prices_raw(day, data_root=data_root)
        except FileNotFoundError:
            continue
        for row in rows:
            isin = row.get("isin")
            if isinstance(isin, str) and isin:
                isins.add(isin)
    return isins


def bse_scrips_for_isins(master: IdentityMaster, isins: Iterable[str]) -> list[str]:
    """The BSE scrip codes those ISINs are listed under, via the D2 master (invariant #2).

    An ISIN with no BSE listing (NSE-only) contributes no scrip and is simply not in the BSE plan —
    its actions still arrive through the NSE date-range feed. Sorted and de-duplicated.
    """
    scrips: set[str] = set()
    for isin in isins:
        listing = master.listing(isin, Exchange.BSE)
        if listing is not None and listing.security_code:
            scrips.add(listing.security_code.strip())
    return sorted(scrips)


# ── the runner ───────────────────────────────────────────────────────────────────────────────


class CaBackfillRunner:
    """Drives CA fetch units through `fetch -> L0 -> parse -> persist -> sync_state`, resumably.

    What it does: for each unit, skips it if `sync_state` already has it `PUBLISHED` (resume),
    otherwise begins the row and advances it one state at a time, persisting the parsed actions and
    committing after every unit so a kill loses at most the unit in flight. Unresolved identities
    and unclassifiable purpose strings are carried onto the report, never dropped. A 403 spike stops
    the whole run and parks it with an enumerated cause.
    What it assumes: its `Fetcher`, `L0Store`, `SyncStateStore` and `Connection` share one clock
    (B10) and one transaction — `commit` is that transaction's checkpoint.
    What it never does: re-fetch a `PUBLISHED` unit, open a socket of its own, weaken the rate limit
    or the 403 hard stop, or reconcile/recompute — that is the finalize step, run once after the
    units have landed so a chain is rebuilt from the whole reconciled set, not a half-landed one.
    """

    def __init__(
        self,
        *,
        fetcher: Fetcher,
        l0: L0Store,
        sync: SyncStateStore,
        conn: Connection,
        commit: Callable[[], None],
        master: IdentityMaster,
        scrip_index: dict[str, str],
        clock: Clock,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self._fetcher = fetcher
        self._l0 = l0
        self._sync = sync
        self._conn = conn
        self._commit = commit
        self._master = master
        self._scrip_index = scrip_index
        self._clock = clock
        self._should_stop = should_stop

    def run(self, units: Sequence[CaFetchUnit]) -> CaBackfillReport:
        """Process every unit in order — resuming, checkpointing, and parking per the class doc.

        Returns a `CaBackfillReport` whichever way the run ends. An individual unit's data failure
        is recorded in `sync_state` and counted so a long run survives one bad payload; only a 403
        spike (a reserved-decision block) ends the whole run, and it parks rather than raising.
        """
        report = CaBackfillReport(requested=len(units))
        total = len(units)
        for index, unit in enumerate(units, start=1):
            if self._should_stop():
                _LOG.warning(
                    "ca_backfill.stopping",
                    processed=report.processed,
                    remaining=total - index + 1,
                    reason="stop requested (SIGINT)",
                )
                report.park_reason = None  # a graceful stop is not a park; just left work
                break
            try:
                self._process(unit, index=index, total=total, report=report)
            except ForbiddenSpikeError as spike:
                self._fail(unit, str(spike), retryable=False, report=report)
                report.park_reason = ParkReason.FORBIDDEN_SPIKE
                report.park_detail = (
                    f"{ParkReason.FORBIDDEN_SPIKE.value}: a 403 spike hard-stopped the fetch at "
                    f"unit {unit.label!r} ({unit.url}). The fetcher has refused this host for the "
                    f"life of the process; resuming requires a human to clear the block, not a "
                    f"lower rate or a rotated agent (AGENTIC_CONTEXT §8). Detail: {spike}"
                )
                _LOG.critical(
                    "ca_backfill.hard_stop",
                    unit=unit.label,
                    processed=report.processed,
                    park_reason=report.park_reason.value,
                    error=str(spike),
                    state="PARKED",
                )
                break
        _LOG.info(
            "ca_backfill.done",
            requested=report.requested,
            published=report.published,
            skipped_published=report.skipped_published,
            failed=report.failed,
            actions_persisted=report.actions_persisted,
            queued=report.queued,
            unresolved=report.unresolved,
            parked=report.parked,
        )
        return report

    def _process(
        self, unit: CaFetchUnit, *, index: int, total: int, report: CaBackfillReport
    ) -> None:
        """Drive one unit, or record why it could not be driven. Never raises for a data error.

        Re-raises only `ForbiddenSpikeError`, which `run` turns into the whole-run park; every other
        failure is caught, filed as a `FAILED` row, committed and counted.
        """
        existing = self._sync.get(unit.state_source, unit.logical_date)
        if existing is not None and existing.state is SyncState.PUBLISHED:
            report.skipped_published += 1
            _LOG.info(
                "ca_backfill.skip_published",
                unit=unit.label,
                progress=f"{index}/{total}",
                state="PUBLISHED",
            )
            return

        _LOG.info(
            "ca_backfill.unit_start",
            unit=unit.label,
            progress=f"{index}/{total}",
            url=unit.url,
        )
        try:
            self._sync.begin(unit.state_source, unit.logical_date)
            ref = self._fetcher.fetch(
                unit.fetch_source, unit.url, unit.logical_date, filename=unit.filename
            )
            self._sync.mark_fetched(
                unit.state_source, unit.logical_date, checksum=ref.sha256, l0_path=ref.key
            )

            result = self._parse(unit, ref_key=ref.key)
            self._sync.mark_validated(unit.state_source, unit.logical_date)

            counts = write_corporate_actions(self._conn, result.actions, clock=self._clock)
            self._sync.mark_normalized(unit.state_source, unit.logical_date)
            self._sync.mark_published(unit.state_source, unit.logical_date)
            self._commit()

            report.published += 1
            report.actions_persisted += counts.inserted
            report.queued += len(result.queued)
            report.unresolved += len(result.unresolved)
            report.touched_isins.update(a.isin for a in result.actions)
            _LOG.info(
                "ca_backfill.unit_published",
                unit=unit.label,
                progress=f"{index}/{total}",
                actions=len(result.actions),
                inserted=counts.inserted,
                queued=len(result.queued),
                unresolved=len(result.unresolved),
                state="PUBLISHED",
            )
        except ForbiddenSpikeError:
            raise
        except Exception as exc:  # fetch/parse/write/DB — recorded, not swallowed; run continues
            self._fail(unit, f"{type(exc).__name__}: {exc}", retryable=True, report=report)

    def _parse(self, unit: CaFetchUnit, *, ref_key: str) -> CaParseResult:
        """Parse the unit's fetched L0 payload with the parser its exchange requires."""
        ref = self._l0.ref_for(unit.fetch_source, unit.logical_date, unit.filename)
        if unit.exchange is Exchange.BSE:
            return bse_ca.parse_l0(self._l0, ref, scrip_index=self._scrip_index, clock=self._clock)
        return nse_ca.parse_l0(self._l0, ref, master=self._master, clock=self._clock)

    def _fail(
        self, unit: CaFetchUnit, message: str, *, retryable: bool, report: CaBackfillReport
    ) -> None:
        """Record one unit's failure in `sync_state`, commit it, and count it."""
        self._rollback()
        try:
            self._sync.begin(unit.state_source, unit.logical_date)
            self._sync.mark_failed(
                unit.state_source, unit.logical_date, message, retryable=retryable
            )
            self._commit()
        except Exception as exc:  # the DB itself is unwell; surface it rather than hide the run
            self._rollback()
            _LOG.error(
                "ca_backfill.fail_record_failed",
                unit=unit.label,
                error=f"{type(exc).__name__}: {exc}",
            )
        report.failed += 1
        report.failures.append((unit.label, message))
        _LOG.warning(
            "ca_backfill.unit_failed",
            unit=unit.label,
            retryable=retryable,
            error=message,
            state="FAILED",
        )

    def _rollback(self) -> None:
        """Best-effort rollback of the shared connection between units."""
        rollback = getattr(self._conn, "rollback", None)
        if callable(rollback):
            rollback()


# ── finalize: reconcile the two feeds, then recompute the factor chain ─────────────────────────


def finalize_reconcile_and_recompute(
    conn: Connection,
    *,
    clock: Clock,
    commit: Callable[[], None] = lambda: None,
) -> FinalizeCounts:
    """Reconcile every stored CA across the two feeds, then recompute each agreed ISIN's chain.

    Run once, after the units have landed: reconciliation pairs NSE against BSE (§4.1 row 5), and a
    disagreement goes to the `quality_flag` queue for a human, never a guessed winner (M2.3). Only
    the reconciled ISINs are handed to M2.4's `recompute_isins`, which rebuilds each one's *whole*
    factor chain from its reconciled actions and flags its L2 stale — so `adjustment_factors` is
    populated exactly for the names with a ratio-bearing, agreed-upon action.

    The caller's `commit` is the durable checkpoint; the reconcile marks and the factor rewrite land
    in one transaction, as every D3 writer intends.
    """
    all_actions = load_corporate_actions(conn)
    result = reconcile(all_actions)
    persist = persist_reconciliation(conn, result, clock=clock)

    reconciled_isins = {action.isin for action in result.reconciled}
    recomputes: tuple[RecomputeResult, ...] = recompute_isins(
        conn, reconciled_isins, clock=clock, reason="M9.1 corporate-action backfill"
    )
    commit()

    counts = FinalizeCounts(
        reconciled=len(result.reconciled),
        queued=len(result.queue),
        flags_written=persist.flags_written,
        isins_recomputed=len(recomputes),
        factor_rows=sum(r.factor_rows_written for r in recomputes),
        l2_invalidated=sum(1 for r in recomputes if r.l2_invalidated),
    )
    _LOG.info(
        "ca_backfill.finalized",
        reconciled=counts.reconciled,
        queued=counts.queued,
        isins_recomputed=counts.isins_recomputed,
        factor_rows=counts.factor_rows,
        l2_invalidated=counts.l2_invalidated,
        state="RECOMPUTED",
    )
    return counts


# ── coverage report ────────────────────────────────────────────────────────────────────────────


def render_report(
    *,
    from_date: date,
    to_date: date,
    universe_size: int,
    report: CaBackfillReport,
    finalize: FinalizeCounts | None,
) -> str:
    """The Markdown coverage report an operator reads after a run (`ops/gates/…`).

    States what the run covered and — the point of a coverage report — what it did *not*: the units
    that failed, the identities that did not resolve, the purpose strings that queued, and whether
    the run parked on a reserved-decision block. `finalize` is `None` when the run parked before the
    reconcile/recompute step could run.
    """
    lines = [
        "# M9.1 — Corporate-action backfill coverage",
        "",
        f"- Window: {from_date.isoformat()} .. {to_date.isoformat()}",
        f"- Universe (ISINs in prices_raw): {universe_size}",
        f"- Units planned: {report.requested}",
        f"- Units published: {report.published}",
        f"- Units resumed (already published): {report.skipped_published}",
        f"- Units failed: {report.failed}",
        f"- Actions persisted (new rows): {report.actions_persisted}",
        f"- Purpose strings queued (manual entry): {report.queued}",
        f"- Identities unresolved: {report.unresolved}",
        f"- ISINs touched: {len(report.touched_isins)}",
    ]
    if report.parked:
        lines += [
            "",
            "## PARKED — reserved decision, run stopped",
            "",
            f"- Reason: `{report.park_reason.value if report.park_reason else 'UNKNOWN'}`",
            f"- Detail: {report.park_detail or '(none recorded)'}",
        ]
    if finalize is not None:
        lines += [
            "",
            "## Reconcile + recompute (M2.3 / M2.4)",
            "",
            f"- Actions reconciled (agreed across feeds): {finalize.reconciled}",
            f"- Disagreements queued (quality_flag): {finalize.queued}",
            f"- Reconciliation flags written: {finalize.flags_written}",
            f"- ISINs recomputed: {finalize.isins_recomputed}",
            f"- adjustment_factors rows written: {finalize.factor_rows}",
            f"- L2 invalidations raised: {finalize.l2_invalidated}",
        ]
    if report.failures:
        lines += ["", "## Failures (recorded, non-fatal)", ""]
        lines += [f"- {label}: {message}" for label, message in report.failures[:50]]
    lines.append("")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def _install_sigint(state: dict[str, bool]) -> None:
    """Flip `state['stop']` on the first SIGINT so the runner stops after the current unit."""

    def handle(_signum: int, _frame: FrameType | None) -> None:
        if state["stop"]:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            return
        state["stop"] = True
        print(
            "\nSIGINT received — finishing the current unit and stopping; "
            "press Ctrl-C again to force-quit.",
            file=sys.stderr,
        )

    signal.signal(signal.SIGINT, handle)


def _print_plan(plan: Sequence[CaFetchUnit]) -> None:
    """Print the dry-run plan: one line per unit, then the count. No socket, no database."""
    for unit in plan:
        print(f"{unit.label}\t{unit.fetch_source}\t{unit.url}")
    print(f"\n{len(plan)} units planned (no fetch performed)")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the corporate-action backfill runner.

    `--dry-run` prints and counts the plan without a socket or a database. Without it, the runner
    fetches: it reads the price window's ISINs from L1, resolves the BSE scrips, drives the plan,
    then reconciles and recomputes. Exit code is 0 on a clean or gracefully stopped run, and 3 when
    the run parked on a 403 spike, so an orchestrator can tell "done" from "needs a human".
    """
    parser = argparse.ArgumentParser(prog="ca-backfill", description=__doc__)
    parser.add_argument("--from", dest="from_date", required=True, type=date.fromisoformat)
    parser.add_argument("--to", dest="to_date", required=True, type=date.fromisoformat)
    parser.add_argument("--dry-run", action="store_true", help="print the plan and count, no fetch")
    parser.add_argument("--limit", type=int, default=None, help="bounded sample of the plan")
    parser.add_argument("--chunk-months", type=int, default=DEFAULT_NSE_CHUNK_MONTHS)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("ops/gates/M9-ca-backfill-report.md"),
        help="where to write the coverage report",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    clock: Clock = SystemClock()
    calendar = trading_calendar()
    register = load_register()

    return _run_live(
        from_date=args.from_date,
        to_date=args.to_date,
        limit=args.limit,
        chunk_months=args.chunk_months,
        dry_run=args.dry_run,
        report_path=args.report,
        settings=settings,
        clock=clock,
        calendar=calendar,
        register=register,
    )


def _run_live(
    *,
    from_date: date,
    to_date: date,
    limit: int | None,
    chunk_months: int,
    dry_run: bool,
    report_path: Path,
    settings: Settings,
    clock: Clock,
    calendar: TradingCalendar,
    register: SourceRegister,
) -> int:
    """Build the real wiring and run the plan; split from `main` so `main` is only arg parsing."""
    with connection(settings) as conn:
        master = IdentityStore(conn, clock=clock).load_master()
        try:
            universe = isins_in_price_window(
                calendar, from_date, to_date, data_root=settings.data_root
            )
        except CalendarCoverageError as exc:
            print(f"cannot plan CA backfill: {exc}", file=sys.stderr)
            return 2
        scrips = bse_scrips_for_isins(master, universe)
        plan = build_plan(
            from_date,
            to_date,
            scrips,
            register=register,
            chunk_months=chunk_months,
            limit=limit,
        )

        if dry_run:
            _print_plan(plan)
            return 0

        stop_state = {"stop": False}
        _install_sigint(stop_state)
        fetcher = build_fetcher(clock=clock, settings=settings, register=register)
        l0 = L0Store(clock=clock, data_root=settings.data_root)
        sync = SyncStateStore(conn, clock=clock, calendar=calendar)
        runner = CaBackfillRunner(
            fetcher=fetcher,
            l0=l0,
            sync=sync,
            conn=conn,
            commit=conn.commit,
            master=master,
            scrip_index=build_scrip_index(master),
            clock=clock,
            should_stop=lambda: stop_state["stop"],
        )
        report = runner.run(plan)

        finalize: FinalizeCounts | None = None
        if not report.parked:
            finalize = finalize_reconcile_and_recompute(conn, clock=clock, commit=conn.commit)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(
            from_date=from_date,
            to_date=to_date,
            universe_size=len(universe),
            report=report,
            finalize=finalize,
        ),
        encoding="utf-8",
    )

    summary = (
        f"CA backfill: {report.published} published, {report.skipped_published} resumed, "
        f"{report.failed} failed of {report.requested} planned; "
        f"{report.actions_persisted} actions, {report.queued} queued, "
        f"{report.unresolved} unresolved"
    )
    if finalize is not None:
        summary += (
            f"; reconciled {finalize.reconciled}, recomputed {finalize.isins_recomputed} ISINs "
            f"→ {finalize.factor_rows} factor rows"
        )
    if report.parked:
        summary += f" — PARKED ({report.park_reason.value if report.park_reason else 'UNKNOWN'})"
    print(summary)
    if report.park_detail:
        print(report.park_detail, file=sys.stderr)
    return 3 if report.parked else 0


if __name__ == "__main__":
    sys.exit(main())
