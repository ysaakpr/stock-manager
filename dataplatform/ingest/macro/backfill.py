"""D1 (M11.2): the resumable, checkpointed index-valuation backfill (close-all snapshot → macro).

M11.1 built the parser (`index_valuation.parse_index_valuation`: one `ind_close_all_<DDMMYYYY>.csv`
→ a `MacroRelease` of closing level, P/E, P/B and dividend yield per NIFTY index, every name
resolved through the evidence-backed alias table) and the store (`store.macro_series`, partitioned
by `release_date`). What neither owns is the runner that walks the archive one session at a time
from the measured epoch (2012-10-01) to now. This is that runner, and it is deliberately the same
shape as `fundamentals_backfill.py` so the platform keeps one resume/checkpoint/park vocabulary:

* **Resume is read from `sync_state`, never a sidecar.** Each session is one unit under
  `nse_index_close_snapshot` on its own logical date. A `PUBLISHED` row is skipped; a
  non-retryable `FAILED` row (a 404 — the archive never published that session) is skipped and
  reported as a gap, not hammered again; a retryable failure is tried again on the next run.
* **Commit per unit.** One session's facts are written to its `release_date` partition, then the
  row is driven to `PUBLISHED` and committed. A kill loses at most the session in flight, and a
  payload already in L0 is re-parsed rather than re-fetched, so a crash between fetch and publish
  costs no request.
* **A 403 spike parks.** The fetcher counts refusals and raises `ForbiddenSpikeError`; the runner
  records the tripping session `FAILED` (non-retryable), ends the run with the enumerated
  `ParkReason.FORBIDDEN_SPIKE`, and `main` exits 3. It never lowers the rate, rotates the agent or
  routes around the block (AGENTIC_CONTEXT §8).
* **A deadline stops it cleanly.** `--stop-before HH:MM` (IST) ends the run after the session in
  flight once the injected clock reaches the deadline, so a campaign can step out of the
  exchange's evening window (the scheduler's EOD jobs need this host's lease) and resume after it.

The coverage report is built from **L0, not from what this run happened to do**: every planned
session is surveyed — its payload, its index count, its fact count, and every published index name
the alias table has never seen — so a resumed run reports the whole window, not just its tail. The
unknown names are listed separately with the first and last session each appeared on; a name that
stops appearing is either retired or renamed, and that list is the input to widening the name
history (never a guess made here).

Offline by construction (B8): the runner takes its `Fetcher`, `L0Store` and sync store by injection.
`main` is the only place that builds the networked wiring, and it takes the host lease so a second
driver against the archive host refuses to start. The full campaign is a B1 bulk fetch reserved to
the owner; this module builds and unit-verifies it.
"""

from __future__ import annotations

import argparse
import csv
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
from dataplatform.ingest.calendar import TradingCalendar, trading_calendar
from dataplatform.ingest.fetcher import (
    Fetcher,
    FetchError,
    FetchHTTPError,
    ForbiddenError,
    ForbiddenSpikeError,
    RetryableFetchError,
    leased_fetcher,
)
from dataplatform.ingest.lease import HostBusyError
from dataplatform.ingest.macro.index_valuation import (
    IndexAliasTable,
    load_index_aliases,
    parse_index_valuation,
    published_index_names,
)
from dataplatform.ingest.macro.models import MacroRelease
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Error, L0Ref, L0Store
from dataplatform.store.macro_series import write_release

__all__ = [
    "ARCHIVE_EPOCH",
    "SOURCE_ID",
    "MacroBackfillReport",
    "MacroBackfillRunner",
    "Outcome",
    "ParkReason",
    "RederiveReport",
    "SessionCoverage",
    "SessionUnit",
    "UnmappedName",
    "build_plan",
    "main",
    "rederive",
    "render_report",
    "survey",
    "survey_l0",
    "unmapped_names",
    "write_coverage_csv",
]

_LOG = get_logger(__name__)

#: The register row this runner fetches under — the NSE archive host's copy of the close-all file.
SOURCE_ID: Final = "nse_index_close_snapshot"

#: The measured archive epoch (M11.1: 2012-07-01 answers 404, 2012-10-01 answers 200).
ARCHIVE_EPOCH: Final = date(2012, 10, 1)


#: How a fetch-side failure's `last_error` begins (`_process`); anything else is a refusal of bytes
#: already in L0 — a parse or store rejection, which a re-run re-derives without a request.
_FETCH_FAILURE_PREFIXES: Final = ("403", "HTTP ", "TransportError", "ServerError", "FetchError")


class ParkReason(StrEnum):
    """Why a run stopped short of its plan and handed control to a human (an enumerated cause)."""

    FORBIDDEN_SPIKE = "FORBIDDEN_SPIKE"
    """The fetcher tripped its 403 hard stop on the archive host. Resuming needs a human to
    understand and clear the refusal, not a lower rate or a rotated agent (AGENTIC_CONTEXT §8)."""


class Outcome(StrEnum):
    """What one planned session ended as — the per-session coverage vocabulary."""

    PUBLISHED = "PUBLISHED"
    """Payload in L0, facts in `macro_series`, row `PUBLISHED`."""
    NOT_PUBLISHED = "NOT_PUBLISHED"
    """The archive answered 404 for a date the calendar calls a session: a gap at the source,
    closed to retries (repeating a 404 is a hot loop, not a fix)."""
    FAILED = "FAILED"
    """A failure that may clear: transport, 5xx, a single 403. Retried on the next run."""
    REFUSED = "REFUSED"
    """The payload is in L0 but was refused — markup wearing a 200, a malformed row, or a file
    dated to a different session than the one requested. Left retryable: the bytes are in L0, so a
    re-run (after a parser fix) re-derives the session without a request."""
    PENDING = "PENDING"
    """Not reached yet (a capped or stopped run, or a session after the park)."""


@dataclass(frozen=True, slots=True)
class SessionUnit:
    """One planned fetch: the close-all file for one session."""

    session: date
    url: str
    filename: str

    @property
    def label(self) -> str:
        return f"close-all {self.session.isoformat()}"


@dataclass(frozen=True, slots=True)
class SessionCoverage:
    """One session's line on the coverage report."""

    session: date
    outcome: Outcome
    indices: int = 0
    facts: int = 0
    unmapped: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True, slots=True)
class UnmappedName:
    """A published index name the alias table has no evidence about, and where it appeared."""

    name: str
    first_seen: date
    last_seen: date
    sessions: int


@dataclass(slots=True)
class MacroBackfillReport:
    """What one `run` did. `requests` counts sessions that went to the network (not L0 reuses)."""

    planned: int
    published: int = 0
    resumed: int = 0
    closed: int = 0
    not_published: int = 0
    refused: int = 0
    failed: int = 0
    l0_reused: int = 0
    requests: int = 0
    facts_written: int = 0
    stopped_early: str | None = None
    park_reason: ParkReason | None = None
    park_detail: str | None = None
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def parked(self) -> bool:
        return self.park_reason is not None


class SyncStore(Protocol):
    """The slice of `SyncStateStore` the runner drives (a test passes an in-memory stand-in)."""

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


# ── planning (pure and offline) ──────────────────────────────────────────────────────────────


def build_plan(
    from_date: date,
    to_date: date,
    *,
    calendar: TradingCalendar,
    register: SourceRegister,
) -> list[SessionUnit]:
    """One unit per date the C.2 calendar expects data for, ascending. No socket, no database.

    Uses `expected_data_dates` (sessions, Muhurat and declared weekend sessions), because the
    close-all file is published for every one of them. Raises `ValueError` on an inverted window
    or one starting before the measured archive epoch — asking for a 2011 file is asking for a 404.
    """
    if from_date > to_date:
        raise ValueError(f"from {from_date} is after to {to_date}")
    if from_date < ARCHIVE_EPOCH:
        raise ValueError(
            f"from {from_date} precedes the measured archive epoch {ARCHIVE_EPOCH}; the archive "
            "answers 404 before it (M11.1)"
        )
    source = next((s for s in register.sources if s.id == SOURCE_ID), None)
    if source is None:
        raise ValueError(f"source {SOURCE_ID!r} is not in the source register")
    units = []
    for session in calendar.expected_data_dates(from_date, to_date):
        filename = f"ind_close_all_{session:%d%m%Y}.csv"
        url = source.url_template.replace("{DDMMYYYY}", f"{session:%d%m%Y}")
        units.append(SessionUnit(session=session, url=url, filename=filename))
    return units


# ── the runner ───────────────────────────────────────────────────────────────────────────────


class MacroBackfillRunner:
    """Drive a plan of sessions to `PUBLISHED`, one committed session at a time.

    What it does: for each session not already closed in `sync_state`, take the payload from L0 if
    it is there and fetch it if not, parse it, write its facts to `macro_series`, and commit the
    row `PUBLISHED`.
    What it assumes: the caller holds the archive host's lease, and `commit` commits the sync
    store's connection.
    What it never does: re-request a session that is `PUBLISHED` or closed by a 404, retry a 403
    differently, or write a fact dated to a session other than the one requested.

    `pending_from` is the same-evening job's (M17.10): a 404 for a session on or after it is "not
    published yet", left retryable, rather than a gap at the source closed for good — the evening
    job asks for D on D's evening, possibly before the archive has it.
    """

    def __init__(
        self,
        *,
        fetcher: Fetcher | None,
        l0: L0Store,
        sync: SyncStore,
        commit: Callable[[], None],
        rollback: Callable[[], None] = lambda: None,
        should_stop: Callable[[], str | None] = lambda: None,
        data_root: Path | None = None,
        table: IndexAliasTable | None = None,
        max_sessions: int | None = None,
        pending_from: date | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._l0 = l0
        self._sync = sync
        self._commit = commit
        self._rollback = rollback
        self._should_stop = should_stop
        self._data_root = data_root
        self._table = load_index_aliases() if table is None else table
        self._max_sessions = max_sessions
        self._pending_from = pending_from

    def run(self, plan: Sequence[SessionUnit]) -> MacroBackfillReport:
        """Run the plan; returns the report. Never raises for one session's failure."""
        report = MacroBackfillReport(planned=len(plan))
        attempted = 0
        for unit in plan:
            if self._closed(unit, report):
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
                report.park_reason = parked.reason
                report.park_detail = parked.detail
                break
        _LOG.info(
            "macro_backfill.run_done",
            source=SOURCE_ID,
            planned=report.planned,
            published=report.published,
            resumed=report.resumed,
            requests=report.requests,
            not_published=report.not_published,
            failed=report.failed,
            refused=report.refused,
            parked=report.park_reason.value if report.park_reason else None,
            state="DONE" if report.park_reason is None else "PARKED",
        )
        return report

    def _closed(self, unit: SessionUnit, report: MacroBackfillReport) -> bool:
        """True when `sync_state` says this session is finished: published, or closed by a 404."""
        row = self._sync.get(SOURCE_ID, unit.session)
        state = getattr(row, "state", None)
        if state is SyncState.PUBLISHED:
            report.resumed += 1
            return True
        if state is SyncState.FAILED and getattr(row, "retryable", True) is False:
            report.closed += 1
            return True
        return False

    def _process(self, unit: SessionUnit, report: MacroBackfillReport) -> None:
        try:
            ref = self._ref_for(unit, report)
        except ForbiddenSpikeError as spike:
            self._park(unit, spike)
        except ForbiddenError as exc:  # one 403 is not yet a spike; never retried differently
            self._fail(unit, f"403: {exc}", retryable=True, report=report, outcome=Outcome.FAILED)
            return
        except FetchHTTPError as exc:
            if exc.status_code == 404 and self._not_yet_due(unit):
                # The same-evening job asks for D before it may be up: a 404 then is "not yet",
                # and closing D to retries would lose the session for good (M17.10).
                self._fail(
                    unit,
                    f"HTTP 404: {unit.filename} not yet published (asked the evening of the "
                    "session; retried by the next fire)",
                    retryable=True,
                    report=report,
                    outcome=Outcome.FAILED,
                )
            elif exc.status_code == 404:
                self._fail(
                    unit,
                    f"404: the archive does not hold {unit.filename}",
                    retryable=False,
                    report=report,
                    outcome=Outcome.NOT_PUBLISHED,
                )
            else:
                self._fail(
                    unit,
                    f"HTTP {exc.status_code}: {exc}",
                    retryable=True,
                    report=report,
                    outcome=Outcome.FAILED,
                )
            return
        except (RetryableFetchError, FetchError) as exc:
            self._fail(
                unit,
                f"{type(exc).__name__}: {exc}",
                retryable=True,
                report=report,
                outcome=Outcome.FAILED,
            )
            return

        try:
            payload = self._l0.get(ref)
            release = parse_index_valuation(
                payload, filename=unit.filename, table=self._table, l0_key=ref.key, source=SOURCE_ID
            )
            if release.release_date != unit.session:
                raise ParseError(
                    f"file reports session {release.release_date}, requested {unit.session}; a "
                    "file dated to another session would land its facts in the wrong partition",
                    filename=unit.filename,
                )
        except (ParseError, L0Error) as exc:
            self._fail(unit, str(exc), retryable=True, report=report, outcome=Outcome.REFUSED)
            return

        try:
            self._sync.begin(SOURCE_ID, unit.session)
            self._sync.mark_fetched(SOURCE_ID, unit.session, checksum=ref.sha256, l0_path=ref.key)
            self._sync.mark_validated(SOURCE_ID, unit.session)
            write_release(release, data_root=self._data_root)
            self._sync.mark_normalized(SOURCE_ID, unit.session)
            self._sync.mark_published(SOURCE_ID, unit.session)
            self._commit()
        except ValueError as exc:
            # The store refused the release (a value conflicting with what the partition already
            # holds). One session's refusal must reach the report, not end a 3,000-session run.
            self._fail(
                unit,
                f"store refused: {exc}",
                retryable=True,
                report=report,
                outcome=Outcome.REFUSED,
            )
            return
        except Exception:
            self._rollback()
            raise
        report.published += 1
        report.facts_written += len(release.facts)
        _LOG.info(
            "macro_backfill.session_published",
            source=SOURCE_ID,
            date=unit.session.isoformat(),
            facts=len(release.facts),
            l0_key=ref.key,
            state="PUBLISHED",
        )

    def _not_yet_due(self, unit: SessionUnit) -> bool:
        """Whether a 404 for `unit` may only mean the file is not up yet (`pending_from`)."""
        return self._pending_from is not None and unit.session >= self._pending_from

    def _ref_for(self, unit: SessionUnit, report: MacroBackfillReport) -> L0Ref:
        """The session's payload from L0 if it is already there, else fetched (one request)."""
        if self._l0.exists(SOURCE_ID, unit.session, unit.filename):
            report.l0_reused += 1
            return self._l0.ref_for(SOURCE_ID, unit.session, unit.filename)
        if self._fetcher is None:
            raise FetchError(f"{unit.filename} is not in L0 and this run may not fetch")
        report.requests += 1
        return self._fetcher.fetch(SOURCE_ID, unit.url, unit.session, filename=unit.filename)

    def _fail(
        self,
        unit: SessionUnit,
        message: str,
        *,
        retryable: bool,
        report: MacroBackfillReport,
        outcome: Outcome,
    ) -> None:
        self._rollback()
        self._sync.begin(SOURCE_ID, unit.session)
        self._sync.mark_failed(SOURCE_ID, unit.session, message, retryable=retryable)
        self._commit()
        if outcome is Outcome.NOT_PUBLISHED:
            report.not_published += 1
        elif outcome is Outcome.REFUSED:
            report.refused += 1
        else:
            report.failed += 1
        report.failures.append((unit.label, message))
        _LOG.warning(
            "macro_backfill.session_failed",
            source=SOURCE_ID,
            date=unit.session.isoformat(),
            outcome=outcome.value,
            retryable=retryable,
            error=message,
            state="FAILED",
        )

    def _park(self, unit: SessionUnit, spike: ForbiddenSpikeError) -> None:
        self._rollback()
        try:
            self._sync.begin(SOURCE_ID, unit.session)
            self._sync.mark_failed(SOURCE_ID, unit.session, str(spike), retryable=False)
            self._commit()
        except Exception:  # the DB itself is unwell; the park still takes priority
            self._rollback()
        detail = (
            f"{ParkReason.FORBIDDEN_SPIKE.value}: a 403 spike hard-stopped the fetch at "
            f"{unit.label!r}. The fetcher has refused this host for the life of the process; "
            "resuming requires a human to clear the block, not a lower rate or a rotated agent "
            f"(AGENTIC_CONTEXT §8). Detail: {spike}"
        )
        _LOG.critical(
            "macro_backfill.hard_stop",
            source=SOURCE_ID,
            date=unit.session.isoformat(),
            park_reason=ParkReason.FORBIDDEN_SPIKE.value,
            state="PARKED",
        )
        raise _ParkedError(ParkReason.FORBIDDEN_SPIKE, detail)


class _ParkedError(Exception):
    def __init__(self, reason: ParkReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# ── coverage (from L0, so a resumed run reports the whole window) ──────────────────────────────


def survey(
    plan: Sequence[SessionUnit],
    *,
    l0: L0Store,
    sync: SyncStore,
    table: IndexAliasTable,
) -> list[SessionCoverage]:
    """One coverage line per planned session, read from L0 and `sync_state` — never the network.

    A published session is re-parsed from its L0 payload (local, cheap) so its index count, fact
    count and unknown names come from the bytes, whichever run fetched them.
    """
    lines: list[SessionCoverage] = []
    for unit in plan:
        row = sync.get(SOURCE_ID, unit.session)
        state = getattr(row, "state", None)
        error = str(getattr(row, "last_error", "") or "")
        if state is SyncState.PUBLISHED:
            try:
                ref = l0.ref_for(SOURCE_ID, unit.session, unit.filename)
                payload = l0.get(ref)
                names = published_index_names(payload, filename=unit.filename)
                release = parse_index_valuation(
                    payload, filename=unit.filename, table=table, source=SOURCE_ID
                )
            except (L0Error, ParseError, FileNotFoundError) as exc:
                lines.append(
                    SessionCoverage(unit.session, Outcome.REFUSED, detail=f"re-read failed: {exc}")
                )
                continue
            lines.append(
                SessionCoverage(
                    unit.session,
                    Outcome.PUBLISHED,
                    indices=len(names),
                    facts=len(release.facts),
                    unmapped=tuple(dict.fromkeys(n for n in names if not table.knows(n))),
                    detail=(
                        f"withheld, published twice with different values: "
                        f"{', '.join(release.withheld)}"
                        if release.withheld
                        else ""
                    ),
                )
            )
        elif state is SyncState.FAILED:
            if error.startswith("404"):
                outcome = Outcome.NOT_PUBLISHED
            elif error.startswith(_FETCH_FAILURE_PREFIXES) or "spike" in error.lower():
                outcome = Outcome.FAILED
            else:
                outcome = Outcome.REFUSED
            lines.append(SessionCoverage(unit.session, outcome, detail=error))
        else:
            lines.append(SessionCoverage(unit.session, Outcome.PENDING))
    return lines


def _from_l0(
    unit: SessionUnit, *, l0: L0Store, table: IndexAliasTable
) -> SessionCoverage | tuple[MacroRelease, bytes]:
    """A session's release (and payload) re-derived from L0, or the coverage line saying why not.

    The same refusal rule the runner applies: a payload dated to another session is not this
    session's, and is refused rather than written to the wrong partition.
    """
    if not l0.exists(SOURCE_ID, unit.session, unit.filename):
        return SessionCoverage(unit.session, Outcome.PENDING, detail="not in L0")
    try:
        ref = l0.ref_for(SOURCE_ID, unit.session, unit.filename)
        payload = l0.get(ref)
        release = parse_index_valuation(
            payload, filename=unit.filename, table=table, l0_key=ref.key, source=SOURCE_ID
        )
    except (L0Error, ParseError) as exc:
        return SessionCoverage(unit.session, Outcome.REFUSED, detail=str(exc))
    if release.release_date != unit.session:
        return SessionCoverage(
            unit.session,
            Outcome.REFUSED,
            detail=f"file reports session {release.release_date}, requested {unit.session}",
        )
    return release, payload


def survey_l0(
    plan: Sequence[SessionUnit], *, l0: L0Store, table: IndexAliasTable
) -> list[SessionCoverage]:
    """Coverage read from L0 alone — no `sync_state`, no network, no write.

    What it does: re-parse every planned session's stored payload with `table` and report its
    index count, fact count and the names `table` does not know; a session with no payload is
    PENDING, a payload refused by the parser or dated to another session is REFUSED.
    What it is for: measuring an alias table against the archive's whole history — the
    unknown-names count before and after widening it — without touching the lake or the database.
    """
    lines: list[SessionCoverage] = []
    for unit in plan:
        derived = _from_l0(unit, l0=l0, table=table)
        if isinstance(derived, SessionCoverage):
            lines.append(derived)
            continue
        release, payload = derived
        names = published_index_names(payload, filename=unit.filename)
        lines.append(
            SessionCoverage(
                unit.session,
                Outcome.PUBLISHED,
                indices=len(names),
                facts=len(release.facts),
                unmapped=tuple(dict.fromkeys(n for n in names if not table.knows(n))),
            )
        )
    return lines


@dataclass(slots=True)
class RederiveReport:
    """What one `rederive` did: sessions rewritten from L0, and those it could not."""

    sessions: int = 0
    facts: int = 0
    missing: int = 0
    refused: list[str] = field(default_factory=list)


def rederive(
    plan: Sequence[SessionUnit],
    *,
    l0: L0Store,
    table: IndexAliasTable,
    data_root: Path | None = None,
) -> RederiveReport:
    """Rewrite this source's `macro_series` facts for every planned session from L0.

    What it does: re-parse each stored payload with `table` and write it with
    `replace_source=True`, so a fact moves to its (new) canonical `series_id` and no row stays
    behind under the old one. Rows from other sources in the same partition are untouched.
    What it assumes: no other writer of `macro_series` runs over the same dates (last write wins
    per partition), and `sync_state` already says which sessions are PUBLISHED — this never reads
    or changes it; a session is rewritten exactly when its L0 payload is its own session's.
    What it never does: fetch, take a host lease, or write a session whose payload is absent or
    refused (those are counted, not guessed).
    """
    report = RederiveReport()
    for unit in plan:
        derived = _from_l0(unit, l0=l0, table=table)
        if isinstance(derived, SessionCoverage):
            if derived.outcome is Outcome.PENDING:
                report.missing += 1
            else:
                report.refused.append(f"{unit.session.isoformat()}: {derived.detail}")
            continue
        release, _payload = derived
        write_release(release, data_root=data_root, replace_source=True)
        report.sessions += 1
        report.facts += len(release.facts)
    _LOG.info(
        "macro_backfill.rederived",
        source=SOURCE_ID,
        sessions=report.sessions,
        facts=report.facts,
        missing=report.missing,
        refused=len(report.refused),
        state="NORMALIZED",
    )
    return report


def unmapped_names(coverage: Sequence[SessionCoverage]) -> list[UnmappedName]:
    """Every name the alias table does not know, with first/last session seen, by first seen."""
    first: dict[str, date] = {}
    last: dict[str, date] = {}
    count: dict[str, int] = {}
    for line in coverage:
        for name in line.unmapped:
            first.setdefault(name, line.session)
            last[name] = line.session
            count[name] = count.get(name, 0) + 1
    return sorted(
        (UnmappedName(n, first[n], last[n], count[n]) for n in first),
        key=lambda u: (u.first_seen, u.name),
    )


def write_coverage_csv(coverage: Sequence[SessionCoverage], path: Path) -> None:
    """The per-session coverage, one row per planned session."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["session", "outcome", "indices", "facts", "unmapped", "detail"])
        for line in coverage:
            writer.writerow(
                [
                    line.session.isoformat(),
                    line.outcome.value,
                    line.indices,
                    line.facts,
                    len(line.unmapped),
                    line.detail,
                ]
            )


def render_report(
    *,
    from_date: date,
    to_date: date,
    report: MacroBackfillReport,
    coverage: Sequence[SessionCoverage],
) -> str:
    """The Markdown summary an operator reads after a run: this run, then the whole window."""
    by_outcome: dict[Outcome, int] = {}
    for line in coverage:
        by_outcome[line.outcome] = by_outcome.get(line.outcome, 0) + 1
    published = [line for line in coverage if line.outcome is Outcome.PUBLISHED]
    unknown = [line for line in published if line.unmapped]
    names = unmapped_names(coverage)
    last_published = max((line.session for line in published), default=None)
    lines = [
        "# M11.2 — Index valuation backfill coverage",
        "",
        f"- Window: {from_date.isoformat()} .. {to_date.isoformat()} ({len(coverage)} sessions)",
        "",
        "## This run",
        "",
        f"- Requests made: {report.requests} (L0 payloads reused: {report.l0_reused})",
        f"- Sessions published: {report.published}; resumed (already published): "
        f"{report.resumed}; closed (non-retryable): {report.closed}",
        f"- Not published at the source (404): {report.not_published}; refused: {report.refused}; "
        f"failed (retryable): {report.failed}",
        f"- Facts written: {report.facts_written}",
        f"- Stopped early: {report.stopped_early or 'no'}",
        "",
        "## Whole window (surveyed from L0 and sync_state)",
        "",
    ]
    lines += [f"- {outcome.value}: {by_outcome.get(outcome, 0)}" for outcome in Outcome]
    lines += [
        f"- Facts in published sessions: {sum(line.facts for line in published)}",
        f"- Sessions publishing at least one unmapped name: {len(unknown)}",
    ]
    if report.parked:
        lines += [
            "",
            "## PARKED — reserved decision, run stopped",
            "",
            f"- Reason: `{report.park_reason.value if report.park_reason else 'UNKNOWN'}`",
            f"- Detail: {report.park_detail or '(none recorded)'}",
        ]
    partial = [line for line in published if line.detail]
    if partial:
        lines += ["", "## Published sessions with subjects withheld", ""]
        lines += [f"- {p.session.isoformat()}: {p.detail}" for p in partial]
    gaps = [line for line in coverage if line.outcome not in (Outcome.PUBLISHED, Outcome.PENDING)]
    if gaps:
        lines += ["", "## Sessions not published, with cause", ""]
        lines += [f"- {g.session.isoformat()} {g.outcome.value}: {g.detail}" for g in gaps]
    ended = [u for u in names if u.last_seen != last_published]
    live = [u for u in names if u.last_seen == last_published]
    lines += [
        "",
        f"## Published index names the alias table does not know ({len(names)})",
        "",
        "The alias table carries only names with rename evidence, so a name it does not know is",
        "not an error: it resolves to itself. The list that matters is the first one — names that",
        "stopped appearing before the latest published session, each either retired or renamed",
        "into a later name. Mapping one is a research task with evidence, never a guess.",
        "",
        f"### Stopped appearing ({len(ended)}) — the input to widening the name history",
        "",
        *_name_table(ended),
        "",
        f"### Still published, never renamed on the evidence so far ({len(live)})",
        "",
        *_name_table(live),
    ]
    lines.append("")
    return "\n".join(lines)


def _name_table(names: Sequence[UnmappedName]) -> list[str]:
    rows = ["| Name | First seen | Last seen | Sessions |", "|---|---|---|---|"]
    rows += [
        f"| {u.name} | {u.first_seen.isoformat()} | {u.last_seen.isoformat()} | {u.sessions} |"
        for u in names
    ]
    return rows


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def _deadline(clock: Clock, hhmm: str | None) -> Callable[[], str | None]:
    """A `should_stop` that fires once the clock reaches today's `HH:MM` IST (if given)."""
    if hhmm is None:
        return lambda: None
    hour, minute = (int(part) for part in hhmm.split(":"))
    start = clock.now().astimezone(IST)
    cutoff = datetime.combine(start.date(), time(hour, minute), tzinfo=IST)
    if cutoff <= start:
        raise ValueError(f"--stop-before {hhmm} IST has already passed today ({start:%H:%M})")

    def check() -> str | None:
        now = clock.now().astimezone(IST)
        return f"--stop-before {hhmm} IST reached" if now >= cutoff else None

    return check


def _install_stop(state: dict[str, bool]) -> None:
    """First SIGINT/SIGTERM: finish the session in flight and stop. A second SIGINT force-quits."""

    def handle(signum: int, _frame: FrameType | None) -> None:
        if state["stop"] and signum == signal.SIGINT:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            return
        state["stop"] = True
        print("\nstop requested — finishing the current session and stopping.", file=sys.stderr)

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Exit 0 on a clean or stopped run, 2 on a planning error, 3 when parked,
    4 when another driver holds the archive host's lease."""
    ap = argparse.ArgumentParser(prog="macro-backfill", description=__doc__)
    ap.add_argument("--from", dest="from_date", type=date.fromisoformat, default=ARCHIVE_EPOCH)
    ap.add_argument("--to", dest="to_date", type=date.fromisoformat, required=True)
    ap.add_argument("--dry-run", action="store_true", help="print the plan and count, no fetch")
    ap.add_argument("--max-sessions", type=int, default=None, help="cap sessions attempted")
    ap.add_argument("--stop-before", default=None, help="stop cleanly at HH:MM IST today")
    ap.add_argument(
        "--report",
        type=Path,
        default=Path("ops/reports/macro-backfill-latest.md"),
        help="Markdown coverage summary (overwritten every run); a .csv sibling holds per-session",
    )
    offline = ap.add_mutually_exclusive_group()
    offline.add_argument(
        "--unknown-names",
        action="store_true",
        help="read-only: count the published names the alias table does not know, from L0 alone",
    )
    offline.add_argument(
        "--rederive",
        action="store_true",
        help="rewrite this source's macro_series facts from L0 with the current alias table; "
        "no fetch, no lease, no sync_state change",
    )
    ap.add_argument(
        "--aliases", type=Path, default=None, help="alias table for --unknown-names/--rederive"
    )
    args = ap.parse_args(argv)
    if (args.unknown_names or args.rederive) and args.stop_before is not None:
        # An offline pass holds no lease and makes no request, so there is no evening window to
        # step out of; silently ignoring a deadline would let an operator believe one applied.
        print(
            "--stop-before applies only to the fetching run; --unknown-names and --rederive "
            "make no request and run to completion (a re-derive is idempotent: re-run it if "
            "interrupted)",
            file=sys.stderr,
        )
        return 2

    settings = get_settings()
    clock: Clock = SystemClock()
    calendar = trading_calendar()
    register = load_register()
    try:
        plan = build_plan(args.from_date, args.to_date, calendar=calendar, register=register)
        external_stop = _deadline(clock, args.stop_before)
    except ValueError as exc:
        print(f"cannot plan macro backfill: {exc}", file=sys.stderr)
        return 2

    if args.unknown_names or args.rederive:
        table = load_index_aliases(args.aliases)
        l0 = L0Store(clock=clock, data_root=settings.data_root)
        if args.unknown_names:
            return _print_unknown(survey_l0(plan, l0=l0, table=table))
        done = rederive(plan, l0=l0, table=table, data_root=settings.data_root)
        print(
            f"macro re-derive from L0: {done.sessions} sessions rewritten, {done.facts} facts, "
            f"{done.missing} not in L0, {len(done.refused)} refused (0 requests)"
        )
        for line in done.refused:
            print(f"  refused {line}")
        return 0

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
        print(f"macro backfill refused to start: {busy}", file=sys.stderr)
        return 4


def _print_unknown(coverage: Sequence[SessionCoverage]) -> int:
    published = [line for line in coverage if line.outcome is Outcome.PUBLISHED]
    names = unmapped_names(coverage)
    last = max((line.session for line in published), default=None)
    stopped = [u for u in names if u.last_seen != last]
    print(
        f"{len(published)} sessions read from L0 (0 requests); {len(names)} published names the "
        f"alias table does not know, {len(stopped)} of them no longer published"
    )
    for u in names:
        print(f"  {u.name}\t{u.first_seen.isoformat()}\t{u.last_seen.isoformat()}\t{u.sessions}")
    return 0


def _run_live(
    args: argparse.Namespace,
    plan: Sequence[SessionUnit],
    *,
    settings: Settings,
    clock: Clock,
    calendar: TradingCalendar,
    register: SourceRegister,
    external_stop: Callable[[], str | None],
) -> int:
    stop_state = {"stop": False}
    _install_stop(stop_state)

    def should_stop() -> str | None:
        return "stop signal received" if stop_state["stop"] else external_stop()

    table = load_index_aliases()
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    with (
        connection(settings) as conn,
        leased_fetcher(
            ["nsearchives.nseindia.com"],
            clock=clock,
            command="dataplatform.ingest.macro.backfill",
            settings=settings,
            register=register,
        ) as fetcher,
    ):
        sync = SyncStateStore(conn, clock=clock, calendar=calendar)
        runner = MacroBackfillRunner(
            fetcher=fetcher,
            l0=l0,
            sync=sync,
            commit=conn.commit,
            rollback=conn.rollback,
            should_stop=should_stop,
            data_root=settings.data_root,
            table=table,
            max_sessions=args.max_sessions,
        )
        report = runner.run(plan)
        coverage = survey(plan, l0=l0, sync=sync, table=table)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        render_report(
            from_date=args.from_date, to_date=args.to_date, report=report, coverage=coverage
        ),
        encoding="utf-8",
    )
    write_coverage_csv(coverage, args.report.with_suffix(".csv"))
    summary = (
        f"macro backfill: {report.published} sessions published, {report.resumed} resumed, "
        f"{report.requests} requests, {report.not_published} not published (404), "
        f"{report.failed} failed, {report.refused} refused, {report.facts_written} facts"
    )
    if report.stopped_early:
        summary += f" — stopped early ({report.stopped_early})"
    if report.parked:
        summary += f" — PARKED ({report.park_reason.value if report.park_reason else 'UNKNOWN'})"
    print(summary)
    if report.park_detail:
        print(report.park_detail, file=sys.stderr)
    return 3 if report.parked else 0


if __name__ == "__main__":
    sys.exit(main())
