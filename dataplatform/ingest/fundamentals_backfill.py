"""D1 (M10.4): the resumable, checkpointed fundamentals backfill runner (XBRL → PIT store).

M7.3 built the two halves of the true point-in-time fundamentals path — `discovery` (the
`corporates-financial-results` index → `FilingIndexEntry`s, each carrying the first-knowable
`filing_date` and the absolute URL of one filing's XBRL document) and `parser` (one XBRL document →
a `Filing` of `FundamentalFact`s) — and `store.pit_fundamentals` (partitioned by `filing_date`,
restatements are new records, quarantined from backtests per invariant #8). What none of them owns
is the *runner* that goes and gets the filings: the PIT store holds no data until something fetches
the results index across the price window, then fetches each filing's XBRL, resolves it to a real
ISIN through the D2 master, and writes it into the store tagged with the date it first became
knowable. This module is that runner.

It is a **two-phase** campaign, because the two sources are shaped differently (Source Register
§4.1, the "Fundamentals (point-in-time)" rows):

* **Discovery is per-date-range.** `corporates-financial-results?period=&from_date=&to_date=`
  returns every company's results filings in a window, so one index fetch per quarter (and per
  annual period) yields the whole universe's filing coordinates. The discovery plan is therefore a
  few dozen fetches across the ~10-year price window — cheap.
* **Ingest is per-filing.** Each discovered `FilingIndexEntry` names one XBRL document, fetched
  individually. This is the bulk half: thousands of per-filing fetches over the window, which is
  exactly why the whole campaign is `NEEDS_GO` (B1) like the M1.13 price backfill.

Four properties, each mapped to an acceptance criterion, mirror `backfill.py` and
`corp_actions_backfill.py` deliberately so the platform has one resume/checkpoint/park shape:

* **Resume is read from `sync_state`, never a sidecar.** An index chunk is checkpointed under
  `nse_financial_results_index` keyed on its `(period, chunk-start)`; a filing under its own state
  source `nse_xbrl_filing/<filing_id>` keyed on the filing date. A `PUBLISHED` unit is never
  re-fetched; the runner commits after every unit, so the committed row *is* the checkpoint.
* **The store keys every fact by ISIN + filing_date.** `write_pit` lands a filing in its
  `filing_date` partition; a restatement is a later filing with a later date (or a distinct
  `filing_id`), so it is a new record and never an overwrite (invariant #8). The runner adds no
  overwrite path — it only ever calls `write_pit`.
* **Identity resolves through the D2 master (invariant #2).** The universe of names to backfill is
  the ISINs actually present in `prices_raw` over the window, intersected with the master's known
  securities; a filing whose ISIN is not in that universe is skipped and surfaced on the coverage
  report, never joined on a raw symbol and never silently dropped.
* **A 403 spike is a hard stop that *parks*.** The fetcher (M1.2) counts refusals and raises
  `ForbiddenSpikeError`; the runner catches it, records the tripping unit `FAILED` (non-retryable),
  and ends the whole run with an enumerated `ParkReason` — it never lowers the rate, rotates the
  agent, or routes around the block (AGENTIC_CONTEXT §8).

Offline by construction (B8): the runner takes its `Fetcher`, `L0Store` and `SyncStateStore` by
injection, so a test wires a `RecordedTransport` and a scratch lake and never opens a socket. `main`
is the only place that builds the real, networked wiring, and the full campaign over the whole
window is the bulk-fetch execution reserved to the owner by B1 — this module builds and
unit-verifies it; a human gives the go.

Money is `Decimal` (inherited from the XBRL models). Time is injected (B10). The operator runbook is
`ops/runbooks/fundamentals_backfill.md`.
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
from dataplatform.identity.master import IdentityMaster, IdentityStore
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
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.ingest.xbrl import discovery, parser
from dataplatform.ingest.xbrl.discovery import FilingIndexEntry
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import read_prices_raw
from dataplatform.store.pit_fundamentals import write_pit

__all__ = [
    "DEFAULT_CHUNK_MONTHS",
    "FILING_STATE_PREFIX",
    "INDEX_STATE_SOURCE",
    "FilingUnit",
    "FundamentalsBackfillReport",
    "FundamentalsBackfillRunner",
    "IndexUnit",
    "ParkReason",
    "Period",
    "build_index_units",
    "isins_in_price_window",
    "main",
    "render_report",
    "resolve_universe",
]

_LOG = get_logger(__name__)

#: The `sync_state` source the discovery index chunks are checkpointed under — the register id
#: itself, so a chunk's checkpoint reads the same way the daily-forward filings ingest's would.
INDEX_STATE_SOURCE: Final = discovery.SOURCE_ID

#: Per-filing ingest units are keyed on the filing date, but many filings share one date, so each
#: filing is tracked under its own state source `nse_xbrl_filing/<filing_id>` (exactly as the BSE
#: per-scrip CA units are `bse_corp_actions/<scrip>`). This keeps one uniform resume mechanism —
#: "is this unit's `sync_state` row PUBLISHED?" — across the per-range and per-filing feeds.
FILING_STATE_PREFIX: Final = f"{parser.SOURCE_ID}/"

#: How many months of the window one discovery fetch covers. Three months = one quarter, which
#: matches the filing cadence and keeps a single index response well inside a sane size; the feed
#: has no documented page size, so a quarter at a time is the conservative bound.
DEFAULT_CHUNK_MONTHS: Final = 3


class Period(StrEnum):
    """The `period` selector the results-index endpoint takes.

    Quarterly and Annual are separate query values that return different filing sets (a company
    files quarterly results and, separately, its audited annual results), so the discovery plan
    fetches both across the window — omitting one would silently lose half the fundamentals.
    """

    QUARTERLY = "Quarterly"
    ANNUAL = "Annual"


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
class IndexUnit:
    """One discovery fetch: a `(period, date-range)` slice of the results index.

    `state_source`/`logical_date` is its `sync_state` key (a `PUBLISHED` chunk is skipped on the
    next run). Fetching it and parsing the payload yields the `FilingIndexEntry`s whose XBRL the
    ingest phase then fetches.
    """

    period: Period
    from_date: date
    to_date: date
    url: str
    filename: str

    @property
    def state_source(self) -> str:
        return INDEX_STATE_SOURCE

    @property
    def logical_date(self) -> date:
        return self.from_date

    @property
    def label(self) -> str:
        return f"index {self.period.value} {self.from_date.isoformat()}..{self.to_date.isoformat()}"


@dataclass(frozen=True, slots=True)
class FilingUnit:
    """One per-filing ingest fetch: the coordinates to fetch, parse and write one XBRL document.

    Built from a discovered `FilingIndexEntry` — carries the `filing_date` (first-knowable, from the
    index, never the document) and the `filing_id` that keys a restatement apart, so the parse and
    the `write_pit` land the fact tagged exactly as invariant #7/#8 require. Its `sync_state` key is
    `nse_xbrl_filing/<filing_id>` on the filing date.
    """

    entry: FilingIndexEntry
    url: str
    filename: str

    @property
    def state_source(self) -> str:
        return f"{FILING_STATE_PREFIX}{self.entry.filing_id}"

    @property
    def logical_date(self) -> date:
        return self.entry.filing_date

    @property
    def label(self) -> str:
        return (
            f"filing {self.entry.isin} {self.entry.period_end.isoformat()} "
            f"{self.entry.nature.value} ({self.entry.filing_id})"
        )


@dataclass(slots=True)
class FundamentalsBackfillReport:
    """What one `run` did — the counts an operator and the acceptance test both assert against.

    `index_requested` is the discovery plan size; the ingest counts partition the per-filing
    outcome. `filings_discovered` is every entry the index returned; `filings_in_universe` the
    subset whose ISIN is a price-window name resolved through the master (the ones actually
    fetched). `skipped_out_of_universe` and `unresolved_isins` are the two ways an entry is *not*
    ingested — surfaced on the report, never silently dropped. A `park_reason` set means the run
    stopped on a reserved-decision block (`park_detail` enumerates it).
    """

    index_requested: int
    index_published: int = 0
    index_skipped_published: int = 0
    index_failed: int = 0
    filings_discovered: int = 0
    filings_in_universe: int = 0
    filings_published: int = 0
    filings_skipped_published: int = 0
    filings_failed: int = 0
    facts_written: int = 0
    skipped_out_of_universe: int = 0
    covered_isins: set[str] = field(default_factory=set)
    unresolved_isins: set[str] = field(default_factory=set)
    park_reason: ParkReason | None = None
    park_detail: str | None = None
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def parked(self) -> bool:
        """True when the run handed a reserved decision back to a human rather than finishing."""
        return self.park_reason is not None

    @property
    def processed(self) -> int:
        """Units this run actually drove down a pipeline (excludes resume-skips)."""
        return (
            self.index_published + self.index_failed + self.filings_published + self.filings_failed
        )


# ── planning (pure and offline) ──────────────────────────────────────────────────────────────


def _index_template(register: SourceRegister) -> str:
    """The verified discovery URL template, read from the register (never hard-coded)."""
    source = next((s for s in register.sources if s.id == discovery.SOURCE_ID), None)
    if source is None:
        raise KeyError(f"source {discovery.SOURCE_ID!r} is not in the source register")
    return source.url_template


def _ddmmyyyy(value: date) -> str:
    """`DD-MM-YYYY`, the format the results-index endpoint's `from_date`/`to_date` take."""
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
        boundary = date(year, month + 1, 1)
        end = min(boundary - timedelta(days=1), to_date)
        bounds.append((start, end))
        start = boundary
    return bounds


def build_index_units(
    from_date: date,
    to_date: date,
    *,
    register: SourceRegister,
    chunk_months: int = DEFAULT_CHUNK_MONTHS,
    periods: Sequence[Period] = (Period.QUARTERLY, Period.ANNUAL),
    limit: int | None = None,
) -> list[IndexUnit]:
    """The discovery plan for a window: one index fetch per `(period, chunk)`.

    Pure and offline — this is what `--dry-run` prints and counts. Both periods are planned across
    the same date chunks (Quarterly and Annual return different filing sets). `limit` truncates the
    plan for a bounded sample run under B1's verify-then-go discipline.
    """
    template = _index_template(register)
    units: list[IndexUnit] = []
    for period in periods:
        for start, end in _chunk_bounds(from_date, to_date, months=chunk_months):
            url = (
                template.replace("period={Quarterly|Annual}", f"period={period.value}")
                .replace("from_date={DD-MM-YYYY}", f"from_date={_ddmmyyyy(start)}")
                .replace("to_date={DD-MM-YYYY}", f"to_date={_ddmmyyyy(end)}")
            )
            if "{" in url:  # every placeholder must have been filled
                raise ValueError(f"index template left a placeholder unfilled: {url!r}")
            units.append(
                IndexUnit(
                    period=period,
                    from_date=start,
                    to_date=end,
                    url=url,
                    filename=(
                        f"corporates-financial-results_{period.value}_"
                        f"{start:%Y%m%d}_{end:%Y%m%d}.json"
                    ),
                )
            )
    if limit is not None:
        if limit <= 0:
            raise ValueError(f"--limit must be positive, got {limit}")
        units = units[:limit]
    return units


def isins_in_price_window(
    calendar: TradingCalendar,
    from_date: date,
    to_date: date,
    *,
    data_root: Path | None = None,
) -> set[str]:
    """Every ISIN with a `prices_raw` row somewhere in the window — the names to backfill.

    Reads the L1 partitions the calendar says traded; a partition that was never written is a gap
    for D7 to explain, not an error here, so it is skipped. The result is the universe the ingest
    phase's filings are filtered against (invariant #2 — a name we have prices for).
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


def resolve_universe(master: IdentityMaster, price_isins: Iterable[str]) -> set[str]:
    """The price-window ISINs that are known securities in the D2 master (invariant #2).

    Intersecting with the master is the identity resolution the spec asks for: the target universe
    is names we both have prices for *and* can name a security_master row for, so a filing is only
    ever joined on a resolved ISIN, never a raw symbol. An ISIN in `prices_raw` but absent from the
    master is a D2 gap, not a backfill target, and is simply not in the universe.
    """
    known = master.securities
    return {isin for isin in price_isins if isin in known}


# ── the runner ───────────────────────────────────────────────────────────────────────────────


class FundamentalsBackfillRunner:
    """Drives discovery then per-filing ingest through `fetch → L0 → parse → write_pit`, resumably.

    What it does: for each index chunk, skips it if `sync_state` has it `PUBLISHED` (resume),
    otherwise fetches and parses it into `FilingIndexEntry`s; each entry whose ISIN is in the
    resolved universe becomes a filing unit driven the same way — skip-if-published, else fetch the
    XBRL, parse it into a `Filing`, `write_pit`, checkpoint. Commits after every unit so a kill
    loses at most the unit in flight. Entries out of universe or unresolved are counted and carried
    onto the report, never dropped. A 403 spike stops the whole run and parks it.
    What it assumes: its `Fetcher`, `L0Store` and `SyncStateStore` share one clock (B10), and
    `commit` durably persists the transaction — that commit is the checkpoint.
    What it never does: re-fetch a `PUBLISHED` unit, open a socket of its own, overwrite a stored
    fact (only `write_pit`, which is restatement-safe), or weaken the rate limit or the 403 hard
    stop to make progress.
    """

    def __init__(
        self,
        *,
        fetcher: Fetcher,
        l0: L0Store,
        sync: SyncStateStore,
        universe: set[str],
        commit: Callable[[], None],
        should_stop: Callable[[], bool] = lambda: False,
        data_root: Path | None = None,
        max_filings: int | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._l0 = l0
        self._sync = sync
        self._universe = universe
        self._commit = commit
        self._should_stop = should_stop
        self._data_root = data_root
        #: A bounded-sample cap on how many in-universe filings the ingest phase *attempts* (B1's
        #: "verify + sample, then full go"): `None` runs the whole discovered set. It caps attempts,
        #: not the discovery phase, so the coverage report still reflects the true universe size.
        self._max_filings = max_filings
        self._filings_attempted = 0

    def run(self, index_units: Sequence[IndexUnit]) -> FundamentalsBackfillReport:
        """Process the discovery plan, then ingest each chunk's in-universe filings, resumably.

        Returns a `FundamentalsBackfillReport` whichever way the run ends. An individual unit's data
        failure is recorded in `sync_state` and counted so a long run survives one bad payload; only
        a 403 spike (a reserved-decision block) ends the whole run — it parks, and does not raise.
        """
        report = FundamentalsBackfillReport(index_requested=len(index_units))
        try:
            for index, unit in enumerate(index_units, start=1):
                if self._should_stop():
                    _LOG.warning(
                        "fundamentals_backfill.stopping",
                        processed=report.processed,
                        reason="stop requested (SIGINT)",
                    )
                    break
                entries = self._process_index(
                    unit, index=index, total=len(index_units), report=report
                )
                self._ingest_entries(entries, report=report)
        except _ParkedError as parked:
            report.park_reason = parked.reason
            report.park_detail = parked.detail
        _LOG.info(
            "fundamentals_backfill.done",
            index_requested=report.index_requested,
            index_published=report.index_published,
            filings_discovered=report.filings_discovered,
            filings_in_universe=report.filings_in_universe,
            filings_published=report.filings_published,
            filings_failed=report.filings_failed,
            facts_written=report.facts_written,
            covered_isins=len(report.covered_isins),
            parked=report.parked,
        )
        return report

    def _process_index(
        self,
        unit: IndexUnit,
        *,
        index: int,
        total: int,
        report: FundamentalsBackfillReport,
    ) -> tuple[FilingIndexEntry, ...]:
        """Fetch and parse one index chunk into its entries; resume-skip if already published.

        A published chunk is re-parsed from its L0 payload (no socket) so the ingest phase still has
        its entries to work through — resume must re-discover what a prior run discovered, not lose
        the filings a checkpointed chunk pointed at. Raises `_ParkedError` on a 403 spike.
        """
        existing = self._sync.get(unit.state_source, unit.logical_date)
        if existing is not None and existing.state is SyncState.PUBLISHED:
            report.index_skipped_published += 1
            _LOG.info(
                "fundamentals_backfill.index_skip_published",
                unit=unit.label,
                progress=f"{index}/{total}",
                state="PUBLISHED",
            )
            try:
                ref = self._l0.ref_for(INDEX_STATE_SOURCE, unit.logical_date, unit.filename)
                return discovery.parse_index_l0(self._l0, ref)
            except (FileNotFoundError, ParseError) as exc:
                # The checkpoint says published but the payload is gone/unreadable; surface it and
                # move on rather than crash a resume of a decade-long run.
                _LOG.warning(
                    "fundamentals_backfill.index_reparse_failed",
                    unit=unit.label,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return ()

        _LOG.info(
            "fundamentals_backfill.index_start",
            unit=unit.label,
            progress=f"{index}/{total}",
            url=unit.url,
        )
        try:
            self._sync.begin(unit.state_source, unit.logical_date)
            ref = self._fetcher.fetch(
                discovery.SOURCE_ID, unit.url, unit.logical_date, filename=unit.filename
            )
            self._sync.mark_fetched(
                unit.state_source, unit.logical_date, checksum=ref.sha256, l0_path=ref.key
            )
            entries = discovery.parse_index_l0(self._l0, ref)
            self._sync.mark_validated(unit.state_source, unit.logical_date)
            self._sync.mark_normalized(unit.state_source, unit.logical_date)
            self._sync.mark_published(unit.state_source, unit.logical_date)
            self._commit()
            report.index_published += 1
            report.filings_discovered += len(entries)
            _LOG.info(
                "fundamentals_backfill.index_published",
                unit=unit.label,
                progress=f"{index}/{total}",
                entries=len(entries),
                state="PUBLISHED",
            )
            return entries
        except ForbiddenSpikeError as spike:
            self._park_on_spike(unit.label, unit.state_source, unit.logical_date, spike)
        except ParseError as exc:
            self._fail(
                unit.state_source,
                unit.logical_date,
                unit.label,
                f"index parse failed: {exc}",
                retryable=True,
                report=report,
                index=True,
            )
        except Exception as exc:  # fetch/DB — recorded, not swallowed; the run continues
            self._fail(
                unit.state_source,
                unit.logical_date,
                unit.label,
                f"{type(exc).__name__}: {exc}",
                retryable=True,
                report=report,
                index=True,
            )
        return ()

    def _ingest_entries(
        self, entries: Sequence[FilingIndexEntry], *, report: FundamentalsBackfillReport
    ) -> None:
        """Ingest every in-universe entry discovery found; a spike raises `_ParkedError`."""
        for entry in entries:
            if self._should_stop():
                break
            if entry.isin not in self._universe:
                report.skipped_out_of_universe += 1
                report.unresolved_isins.add(entry.isin)
                continue
            report.filings_in_universe += 1
            if self._max_filings is not None and self._filings_attempted >= self._max_filings:
                # Bounded sample reached: stop *attempting* fetches, but keep counting the universe
                # so the coverage report still names how much the full run has left to do.
                continue
            self._filings_attempted += 1
            unit = FilingUnit(
                entry=entry,
                url=entry.xbrl_url,
                filename=entry.xbrl_url.rsplit("/", 1)[-1],
            )
            self._process_filing(unit, report=report)

    def _process_filing(self, unit: FilingUnit, *, report: FundamentalsBackfillReport) -> None:
        """Drive one filing `fetch → L0 → parse → write_pit`, or record why it could not be driven.

        Skip-if-published (resume). On success `write_pit` lands the filing in its `filing_date`
        partition (restatement-safe — a new record, never an overwrite). A 403 spike raises
        `_ParkedError`; every other failure is caught, filed `FAILED`, committed and counted.
        """
        existing = self._sync.get(unit.state_source, unit.logical_date)
        if existing is not None and existing.state is SyncState.PUBLISHED:
            report.filings_skipped_published += 1
            report.covered_isins.add(unit.entry.isin)
            _LOG.info(
                "fundamentals_backfill.filing_skip_published",
                unit=unit.label,
                state="PUBLISHED",
            )
            return

        _LOG.info("fundamentals_backfill.filing_start", unit=unit.label, url=unit.url)
        try:
            self._sync.begin(unit.state_source, unit.logical_date)
            ref = self._fetcher.fetch(
                parser.SOURCE_ID, unit.url, unit.logical_date, filename=unit.filename
            )
            self._sync.mark_fetched(
                unit.state_source, unit.logical_date, checksum=ref.sha256, l0_path=ref.key
            )
            filing = parser.parse(
                self._l0.get(ref),
                filing_date=unit.entry.filing_date,
                filing_id=unit.entry.filing_id,
                l0_key=ref.key,
                filename=unit.filename,
                isin=unit.entry.isin,
            )
            self._sync.mark_validated(unit.state_source, unit.logical_date)
            write_pit(filing, data_root=self._data_root)
            self._sync.mark_normalized(unit.state_source, unit.logical_date)
            self._sync.mark_published(unit.state_source, unit.logical_date)
            self._commit()
            report.filings_published += 1
            report.facts_written += len(filing.facts)
            report.covered_isins.add(filing.isin)
            _LOG.info(
                "fundamentals_backfill.filing_published",
                unit=unit.label,
                facts=len(filing.facts),
                state="PUBLISHED",
            )
        except ForbiddenSpikeError as spike:
            self._park_on_spike(unit.label, unit.state_source, unit.logical_date, spike)
        except ParseError as exc:
            self._fail(
                unit.state_source,
                unit.logical_date,
                unit.label,
                f"parse failed: {exc}",
                retryable=True,
                report=report,
                index=False,
            )
        except Exception as exc:  # fetch/write/DB — recorded, not swallowed; the run continues
            self._fail(
                unit.state_source,
                unit.logical_date,
                unit.label,
                f"{type(exc).__name__}: {exc}",
                retryable=True,
                report=report,
                index=False,
            )

    def _park_on_spike(
        self, label: str, state_source: str, logical_date: date, spike: ForbiddenSpikeError
    ) -> None:
        """Record the tripping unit `FAILED` (non-retryable), then raise `_ParkedError` for `run`.

        The fetcher has already alerted and will refuse this host for the life of the process; the
        run ends. Never lower the rate or rotate the agent (AGENTIC_CONTEXT §8).
        """
        self._rollback()
        try:
            self._sync.begin(state_source, logical_date)
            self._sync.mark_failed(state_source, logical_date, str(spike), retryable=False)
            self._commit()
        except Exception:  # the DB itself is unwell; the park still takes priority
            self._rollback()
        detail = (
            f"{ParkReason.FORBIDDEN_SPIKE.value}: a 403 spike hard-stopped the fetch at {label!r}. "
            f"The fetcher has refused this host for the life of the process; resuming requires a "
            f"human to clear the block, not a lower rate or a rotated agent (AGENTIC_CONTEXT §8). "
            f"Detail: {spike}"
        )
        _LOG.critical(
            "fundamentals_backfill.hard_stop",
            unit=label,
            park_reason=ParkReason.FORBIDDEN_SPIKE.value,
            error=str(spike),
            state="PARKED",
        )
        raise _ParkedError(ParkReason.FORBIDDEN_SPIKE, detail)

    def _fail(
        self,
        state_source: str,
        logical_date: date,
        label: str,
        message: str,
        *,
        retryable: bool,
        report: FundamentalsBackfillReport,
        index: bool,
    ) -> None:
        """Record one unit's failure in `sync_state`, commit it, and count it."""
        self._rollback()
        try:
            self._sync.begin(state_source, logical_date)
            self._sync.mark_failed(state_source, logical_date, message, retryable=retryable)
            self._commit()
        except Exception as exc:  # the DB itself is unwell; surface it rather than hide the run
            self._rollback()
            _LOG.error(
                "fundamentals_backfill.fail_record_failed",
                unit=label,
                error=f"{type(exc).__name__}: {exc}",
            )
        if index:
            report.index_failed += 1
        else:
            report.filings_failed += 1
        report.failures.append((label, message))
        _LOG.warning(
            "fundamentals_backfill.unit_failed",
            unit=label,
            retryable=retryable,
            error=message,
            state="FAILED",
        )

    def _rollback(self) -> None:
        """Best-effort rollback of the sync store's connection between units."""
        conn = getattr(self._sync, "_conn", None)
        rollback = getattr(conn, "rollback", None)
        if callable(rollback):
            rollback()


class _ParkedError(Exception):
    """Internal signal that a reserved-decision block ended the run; `run` catches it."""

    def __init__(self, reason: ParkReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# ── coverage report ────────────────────────────────────────────────────────────────────────────


def render_report(
    *,
    from_date: date,
    to_date: date,
    universe_size: int,
    report: FundamentalsBackfillReport,
) -> str:
    """The Markdown coverage report an operator reads after a run (`ops/gates/…`).

    States what the run covered and — the point of a coverage report — what it did *not*: the units
    that failed, the entries skipped as out of the price-window universe, the ISINs that did not
    resolve, and whether the run parked on a reserved-decision block.
    """
    lines = [
        "# M10.4 — Fundamentals backfill coverage",
        "",
        f"- Window: {from_date.isoformat()} .. {to_date.isoformat()}",
        f"- Universe (price-window ISINs resolved through D2): {universe_size}",
        f"- Index chunks planned: {report.index_requested}",
        f"- Index chunks published: {report.index_published}",
        f"- Index chunks resumed (already published): {report.index_skipped_published}",
        f"- Index chunks failed: {report.index_failed}",
        f"- Filings discovered (all entries): {report.filings_discovered}",
        f"- Filings in universe (fetched): {report.filings_in_universe}",
        f"- Filings published: {report.filings_published}",
        f"- Filings resumed (already published): {report.filings_skipped_published}",
        f"- Filings failed: {report.filings_failed}",
        f"- Facts written: {report.facts_written}",
        f"- ISINs covered: {len(report.covered_isins)}",
        f"- Entries skipped (ISIN not in universe): {report.skipped_out_of_universe}",
        f"- Distinct unresolved/out-of-universe ISINs: {len(report.unresolved_isins)}",
    ]
    if report.parked:
        lines += [
            "",
            "## PARKED — reserved decision, run stopped",
            "",
            f"- Reason: `{report.park_reason.value if report.park_reason else 'UNKNOWN'}`",
            f"- Detail: {report.park_detail or '(none recorded)'}",
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


def _print_plan(plan: Sequence[IndexUnit]) -> None:
    """Print the dry-run discovery plan: one line per index chunk, then the count. No socket."""
    for unit in plan:
        print(f"{unit.label}\t{discovery.SOURCE_ID}\t{unit.url}")
    print(
        f"\n{len(plan)} index chunks planned (no fetch performed); "
        "the per-filing ingest count is only known once the index is fetched"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the fundamentals backfill runner.

    `--dry-run` prints and counts the discovery plan without a socket or a database. Without it, the
    runner fetches: it reads the price-window ISINs from L1, resolves them through the D2 master,
    drives the discovery plan, and ingests each chunk's in-universe filings. Exit code is 0 on a
    clean or gracefully stopped run, and 3 when the run parked on a 403 spike, so an orchestrator
    can tell "done" from "needs a human".
    """
    ap = argparse.ArgumentParser(prog="fundamentals-backfill", description=__doc__)
    ap.add_argument("--from", dest="from_date", required=True, type=date.fromisoformat)
    ap.add_argument("--to", dest="to_date", required=True, type=date.fromisoformat)
    ap.add_argument("--dry-run", action="store_true", help="print the plan and count, no fetch")
    ap.add_argument("--limit", type=int, default=None, help="bounded sample of the index plan")
    ap.add_argument(
        "--max-filings",
        type=int,
        default=None,
        help="cap the per-filing ingest at this many attempts (B1 verify+sample of the bulk half)",
    )
    ap.add_argument("--chunk-months", type=int, default=DEFAULT_CHUNK_MONTHS)
    ap.add_argument(
        "--report",
        type=Path,
        default=Path("ops/gates/M10-fundamentals-backfill-report.md"),
        help="where to write the coverage report",
    )
    args = ap.parse_args(argv)

    settings = get_settings()
    clock: Clock = SystemClock()
    calendar = trading_calendar()
    register = load_register()

    return _run_live(
        from_date=args.from_date,
        to_date=args.to_date,
        limit=args.limit,
        max_filings=args.max_filings,
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
    max_filings: int | None,
    chunk_months: int,
    dry_run: bool,
    report_path: Path,
    settings: Settings,
    clock: Clock,
    calendar: TradingCalendar,
    register: SourceRegister,
) -> int:
    """Build the real wiring and run the plan; split from `main` so `main` is only arg parsing."""
    try:
        plan = build_index_units(
            from_date, to_date, register=register, chunk_months=chunk_months, limit=limit
        )
    except ValueError as exc:
        print(f"cannot plan fundamentals backfill: {exc}", file=sys.stderr)
        return 2

    if dry_run:
        _print_plan(plan)
        return 0

    with connection(settings) as conn:
        master = IdentityStore(conn, clock=clock).load_master()
        try:
            price_isins = isins_in_price_window(
                calendar, from_date, to_date, data_root=settings.data_root
            )
        except CalendarCoverageError as exc:
            print(f"cannot plan fundamentals backfill: {exc}", file=sys.stderr)
            return 2
        universe = resolve_universe(master, price_isins)

        stop_state = {"stop": False}
        _install_sigint(stop_state)
        fetcher = build_fetcher(clock=clock, settings=settings, register=register)
        l0 = L0Store(clock=clock, data_root=settings.data_root)
        sync = SyncStateStore(conn, clock=clock, calendar=calendar)
        runner = FundamentalsBackfillRunner(
            fetcher=fetcher,
            l0=l0,
            sync=sync,
            universe=universe,
            commit=conn.commit,
            should_stop=lambda: stop_state["stop"],
            data_root=settings.data_root,
            max_filings=max_filings,
        )
        report = runner.run(plan)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(
            from_date=from_date, to_date=to_date, universe_size=len(universe), report=report
        ),
        encoding="utf-8",
    )

    summary = (
        f"fundamentals backfill: {report.index_published} index chunks, "
        f"{report.filings_published} filings published "
        f"({report.filings_skipped_published} resumed, {report.filings_failed} failed), "
        f"{report.facts_written} facts over {len(report.covered_isins)} ISINs"
    )
    if report.parked:
        summary += f" — PARKED ({report.park_reason.value if report.park_reason else 'UNKNOWN'})"
    print(summary)
    if report.park_detail:
        print(report.park_detail, file=sys.stderr)
    return 3 if report.parked else 0


if __name__ == "__main__":
    sys.exit(main())
