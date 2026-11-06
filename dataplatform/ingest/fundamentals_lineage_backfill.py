"""DQ-ISIN Phase 2: fetch the pre-2025 results filings the backfill skipped over a stale ISIN.

NSE's old results index (`nse_financial_results_index`, filings up to Dec-2024) lists a company
under its *original* ISIN, as the corporate-filing feeds do (`identity.filing_isin`). The M10.4
backfill's universe is the ISINs that traded in the price window, so HDFCBANK's filings under
`INE040A01018` were skipped as out of universe and never fetched. 108 NIFTY 500 names that listed
before 2024 have PIT quarters only from 2025 (`ops/gates/data-lake-status-2026-11-06.md` gap 2);
for 91 of them the stored index pages name the filings, under an ISIN the lineage resolves.

**What it does.** A one-shot scheduler job (`fundamentals_lineage_backfill`). It reads the old
index's entries from the pages already in L0 (no index request), keeps each pre-2025 entry whose
stated ISIN the lineage walks to one of the checked-in names (`fundamentals_lineage_backfill.yaml`),
re-keys it to the ISIN live on its *filing date*, and drives it through the campaign's own runner
under the NSE host leases and the fetcher's polite rate: XBRL into L0, parse, the PIT store,
`sync_state` per filing. A `PUBLISHED` filing is never fetched again, so a second run resumes.

**Point in time.** A filing is keyed to the ISIN the lineage says was live on its filing date,
through explained edges only (`FilingIsinResolver`). HDFCBANK's 2018 filings land on
`INE040A01026` and its 2024 filings on `INE040A01034`, the same keying the forward feed gives.

**What it never does.** Fetch a name outside the list, follow an edge the resolver refuses, request
an index page, run on any day but `RUN_DAYS`, or start a filing after `DEADLINE` (IST).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

from dataplatform.clock import Clock
from dataplatform.config import Settings
from dataplatform.identity.filing_isin import (
    UNMAPPED,
    FilingIsinResolver,
    FilingIsinStatus,
    load_filing_resolver,
)
from dataplatform.identity.master import IdentityStore
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.daily_snapshot import LakeRootMismatchError
from dataplatform.ingest.fetcher import Transport, leased_fetcher
from dataplatform.ingest.fundamentals_backfill import (
    LEASED_HOSTS,
    FundamentalsBackfillReport,
    FundamentalsBackfillRunner,
    bse_codes_of,
    isins_near_of,
    symbols_accepted_on,
)
from dataplatform.ingest.fundamentals_relabel import recorder as relabel_recorder
from dataplatform.ingest.models import ISIN_PATTERN, ParseError
from dataplatform.ingest.source_register import load as load_register
from dataplatform.ingest.xbrl import discovery
from dataplatform.ingest.xbrl.discovery import FilingIndexEntry
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import Connection, connection
from dataplatform.store.l0 import L0Error, L0Store

if TYPE_CHECKING:  # a runtime import would cycle through the registry
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "COMMAND",
    "DEADLINE",
    "NAMES_PATH",
    "OLD_INDEX_BEFORE",
    "RUN_DAYS",
    "LineageBackfillError",
    "Selection",
    "load_names",
    "run_fundamentals_lineage_backfill",
    "run_fundamentals_lineage_backfill_job",
    "select_entries",
    "stored_index_entries",
]

_LOG = get_logger(__name__)

#: The lease command and the job name.
COMMAND: Final = "fundamentals_lineage_backfill"

#: The checked-in list of names this job may fetch for, by current ISIN.
NAMES_PATH: Final = Path(__file__).with_name("fundamentals_lineage_backfill.yaml")

#: The only days the job fetches (coordinator, 2026-11-06): Monday after the DQ-ISIN deploy, and
#: Wednesday to resume. Tuesday 2026-11-10 is an NSE holiday and is skipped. On any other day the
#: job logs and returns without taking a lease.
RUN_DAYS: Final[frozenset[date]] = frozenset({date(2026, 11, 9), date(2026, 11, 11)})

#: No filing is started at or after this IST time, so the run is off the NSE hosts by 18:00 —
#: before `shareholding_poll` (18:05) and long before any L1/L2 write curfew (18:30).
DEADLINE: Final = time(17, 45)

#: The old index's last period ends Dec-2024; every later filing is the forward feed's.
OLD_INDEX_BEFORE: Final = date(2025, 1, 1)


class LineageBackfillError(RuntimeError):
    """The run parked on a 403 spike, or stopped at the deadline with filings still owed."""


class _Name(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN)
    symbol: str = Field(min_length=1)


class _NamesFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1, le=1)
    names: tuple[_Name, ...] = Field(min_length=1)


def load_names(path: Path = NAMES_PATH) -> Mapping[str, str]:
    """Current ISIN → symbol for every listed name. Raises on a malformed or duplicated entry."""
    document = _NamesFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    names: dict[str, str] = {}
    for name in document.names:
        if name.isin in names:
            raise ValueError(f"{path.name}: {name.isin} is listed twice")
        names[name.isin] = name.symbol
    return names


def stored_index_entries(l0: L0Store) -> tuple[tuple[FilingIndexEntry, ...], int]:
    """Every entry on the old index's pages already in L0, one per filing; and pages unreadable.

    No request: the pages are read back (checksum re-verified) from L0. One filing appears on
    several pages when a chunk was captured more than once; it is kept once, by
    `(filing_id, filing_date)`, the key its `sync_state` row has.
    """
    entries: dict[tuple[str, date], FilingIndexEntry] = {}
    unreadable = 0
    for ref in l0.iter_refs(discovery.SOURCE_ID):
        try:
            parsed = discovery.parse_index_l0(l0, ref)
        except (ParseError, L0Error) as exc:
            unreadable += 1
            _LOG.warning(
                "lineage_backfill.index_page_unreadable",
                source=discovery.SOURCE_ID,
                l0_key=ref.key,
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        for entry in parsed:
            entries.setdefault((entry.filing_id, entry.filing_date), entry)
    ordered = sorted(entries.values(), key=lambda e: (e.filing_date, e.filing_id))
    return tuple(ordered), unreadable


@dataclass(frozen=True, slots=True)
class Selection:
    """Which stored entries this job drives, re-keyed, and why every other one is not."""

    entries: tuple[FilingIndexEntry, ...]
    #: Entries on a stale ISIN whose company is not on the list: never fetched.
    refused_outside_list: int
    #: Stale ISINs the lineage walks to a name not on the list.
    outside_isins: frozenset[str]
    #: Every other skip, by reason (`current_isin`, `after_old_index`, a resolver status).
    skipped: Mapping[str, int] = field(default_factory=dict)

    @property
    def names(self) -> frozenset[str]:
        """The ISINs the runner's universe is made of: each entry's re-keyed ISIN."""
        return frozenset(e.isin for e in self.entries)


def select_entries(
    entries: Iterable[FilingIndexEntry],
    resolver: FilingIsinResolver,
    names: Mapping[str, str],
    *,
    as_of: date,
    before: date = OLD_INDEX_BEFORE,
) -> Selection:
    """The entries to drive: stale-ISIN filings of a listed name, re-keyed to their filing date.

    What it does: for each entry filed before ``before``, walks its stated ISIN to ``as_of``
    (today). An entry whose ISIN is still current was in the M10.4 universe and is not this job's.
    One the walk refuses, or that lands on a name not in ``names``, is counted and not driven.
    The rest are re-keyed to the ISIN live on their own filing date (the PIT key).
    What it never does: map an ISIN the resolver will not walk, or admit an unlisted name.
    """
    chosen: list[FilingIndexEntry] = []
    skipped: Counter[str] = Counter()
    outside: set[str] = set()
    refused = 0
    for entry in entries:
        if entry.filing_date >= before:
            skipped["after_old_index"] += 1
            continue
        now = resolver.resolve(entry.isin, as_of)
        if now.status is not FilingIsinStatus.REKEYED:
            # AS_FILED: still the live ISIN, so the M10.4 universe already had it. Anything else
            # is a walk the resolver refused (or one not yet effective), and is never guessed.
            reason = "current_isin" if now.status is FilingIsinStatus.AS_FILED else now.status
            skipped[str(reason)] += 1
            continue
        if now.isin not in names:
            refused += 1
            outside.add(entry.isin)
            continue
        then = resolver.resolve(entry.isin, entry.filing_date)
        if then.status in UNMAPPED:
            skipped[then.status.value] += 1
            continue
        chosen.append(entry.model_copy(update={"isin": then.isin}))
    return Selection(
        entries=tuple(chosen),
        refused_outside_list=refused,
        outside_isins=frozenset(outside),
        skipped=dict(sorted(skipped.items())),
    )


def _deadline(start: datetime) -> datetime:
    return datetime.combine(start.date(), DEADLINE, start.tzinfo)


def run_fundamentals_lineage_backfill(
    settings: Settings,
    clock: Clock,
    *,
    transport: Transport | None = None,
    names_path: Path = NAMES_PATH,
    resolver_of: Callable[[Connection], FilingIsinResolver] = load_filing_resolver,
) -> FundamentalsBackfillReport | None:
    """One run: select from L0, then fetch each selected filing under the NSE leases.

    Returns `None`, having taken no lease, on a day not in `RUN_DAYS` or after `DEADLINE`.
    Raises `LineageBackfillError` when a 403 spike parked the run or the deadline stopped it with
    filings still owed; per-filing failures are `FAILED` in `sync_state` and counted, as on every
    fundamentals run. `transport` is a test seam: `None` is the network.
    """
    started = clock.now()
    today = started.date()
    deadline = _deadline(started)
    if today not in RUN_DAYS or started >= deadline:
        _LOG.info(
            "lineage_backfill.not_a_run_window",
            today=today.isoformat(),
            run_days=sorted(d.isoformat() for d in RUN_DAYS),
            deadline=DEADLINE.isoformat(),
            state="SKIPPED",
        )
        return None

    names = load_names(names_path)
    calendar = trading_calendar()
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    expected = settings.snapshot_expect_lake_root
    if expected is not None and l0.root.resolve() != expected.resolve():
        raise LakeRootMismatchError(
            f"L0 resolved to {l0.root.resolve()} but the operator declared {expected.resolve()}; "
            "nothing was fetched. Fix the invocation (DATA_ROOT), not this assertion."
        )
    entries, unreadable = stored_index_entries(l0)
    stopped: list[datetime] = []

    def should_stop() -> bool:
        if clock.now() < deadline:
            return False
        if not stopped:
            stopped.append(clock.now())
            _LOG.warning(
                "lineage_backfill.deadline", deadline=deadline.isoformat(), state="STOPPING"
            )
        return True

    with (
        leased_fetcher(
            LEASED_HOSTS,
            clock=clock,
            command=COMMAND,
            settings=settings,
            register=load_register(),
            l0=l0,
            transport=transport,
        ) as fetcher,
        connection(settings) as conn,
    ):
        resolver = resolver_of(conn)
        selection = select_entries(entries, resolver, names, as_of=today)
        master = IdentityStore(conn, clock=clock).load_master()
        _LOG.info(
            "lineage_backfill.selected",
            source=discovery.SOURCE_ID,
            index_entries=len(entries),
            index_pages_unreadable=unreadable,
            selected=len(selection.entries),
            names_listed=len(names),
            refused_outside_list=selection.refused_outside_list,
            skipped=dict(selection.skipped),
            deadline=deadline.isoformat(),
            state="RUNNING",
        )
        runner = FundamentalsBackfillRunner(
            fetcher=fetcher,
            l0=l0,
            sync=SyncStateStore(conn, clock=clock, calendar=calendar),
            universe=set(selection.names),
            commit=conn.commit,
            should_stop=should_stop,
            data_root=settings.data_root,
            symbol_history=lambda isin, on: symbols_accepted_on(
                master.windows_for(isin),
                on,
                isin=isin,
                owner_on=lambda symbol: master.try_resolve(symbol, on),
            ),
            bse_codes=lambda isin: bse_codes_of(master, isin),
            isins_near=isins_near_of(master),
            record_relabel=relabel_recorder(conn, clock),
            clock=clock,
        )
        report = runner.ingest(selection.entries)

    state = "PARKED" if report.parked else "STOPPED" if stopped else "PUBLISHED"
    _LOG.info(
        "lineage_backfill.done",
        source="nse_xbrl_filing",
        selected=len(selection.entries),
        filings_published=report.filings_published,
        filings_skipped_published=report.filings_skipped_published,
        filings_failed=report.filings_failed,
        filings_refetched=report.filings_refetched,
        skipped_no_document=report.skipped_no_document,
        facts_written=report.facts_written,
        covered_isins=len(report.covered_isins),
        stopped_at_deadline=bool(stopped),
        state=state,
    )
    if report.parked:
        raise LineageBackfillError(f"parked — {report.park_detail}")
    if stopped:
        raise LineageBackfillError(
            f"stopped at the {deadline:%H:%M} deadline with filings still owed; "
            f"{report.filings_published} published this run. The next run day resumes: a "
            "PUBLISHED filing is never fetched again"
        )
    return report


def run_fundamentals_lineage_backfill_job(context: JobContext) -> None:
    """Scheduler body for `fundamentals_lineage_backfill`."""
    run_fundamentals_lineage_backfill(context.settings, context.clock)
