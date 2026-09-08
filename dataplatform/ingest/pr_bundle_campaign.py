"""W2: the NSE daily report bundle sweep — every `PR<DDMMYY>.zip` from 2010-01-04 forward.

Phase 1 registered the source and wrote its readers (`dataplatform.ingest.nse.pr_bundle`); this is
the driver that fills the lake with the bundles those readers will one day be pointed at. It is a
module of its own rather than a `SOURCE_SETS` entry or a flag on `legacy_backfill`, because three
things about this archive differ, and each difference *is* driver behaviour:

**1. It acquires into L0 and stops there.** Every member of every bundle is keyed by trading
symbol and carries no ISIN, symbols are reused across issuers over sixteen years, and the only
symbol→ISIN resolver we hold (`EQUITY_L.csv`) is a present-day listing and therefore
survivorship-biased. ISIN is the only join key (invariant #2), so these rows cannot be joined yet
and **nothing here writes to L1, to `corporate_actions`, or to `sync_state`**. That is not a gap
to be closed by a later flag on this driver: promotion is a separate, separately-reviewed task
that needs the W4 identity work first. L0 is the immutable record, storage is cheap, and this is
the one chance to never re-fetch — exactly the trade W1 made for its pre-ISIN era.

**2. A 404 means something narrower here than it does for bhavcopy.** The bhavcopy sweep plans
candidate dates and lets the archive decide which were sessions, so an absence there is a closed
exchange. This plan is built from sessions the C.2 calendar already vouched for, over a span the
calendar fully covers (2006-01-01..2026-12-31), so an absence here says *the exchange traded and
no bundle was published* — which is a fact about the archive, not about the market. It is recorded
under its own evidence label, it does **not** count toward the error hard stop, and it is never a
`FAILED` row. The consecutive-404 stop is kept for the other reading of a long silence: an archive
that moved and now answers 404 for everything is indistinguishable from a run of closures one
request at a time, and over four thousand requests the first would manufacture years of phantom
evidence.

**3. The floor is pinned, so the range refuses to widen below it.** `ARCHIVE_START` is
2010-01-04 by measurement, not by bracket: `PR311209.zip` and `PR010110.zip` are both 404 while
`PR040110.zip` is 200, and nine probes across 2005-2009 are all 404. A range starting earlier is
a mistake worth a loud refusal rather than a few hundred requests spent re-proving that.

Resume is L0 plus the journal, with no database and no `sync_state` row: a payload already under
its key is skipped without a request, and so is a date the journal already records a 404 for. A
second run over an acquired range therefore performs **zero** fetches.

`acquire` logs the resolved lake root before it opens a socket. A campaign this long pointed at
the wrong `data_root` builds a second lake that then has to be transferred by hand, and finding
that out after four thousand requests is the expensive way to find it out.

Operator flow (~4,124 sessions at >=2.5 s spacing is roughly three hours). `RANGE` below is
`--from 2010-01-04 --to 2026-09-04`, the whole archive as Phase 1 verified its ends:

    uv run python -m dataplatform.ingest.pr_bundle_campaign plan     $RANGE
    uv run python -m dataplatform.ingest.pr_bundle_campaign acquire  $RANGE
    uv run python -m dataplatform.ingest.pr_bundle_campaign report   $RANGE
    uv run python -m dataplatform.ingest.pr_bundle_campaign pin-eras
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.ingest.backfill import sample_dates
from dataplatform.ingest.calendar import (
    CalendarCoverageError,
    TradingCalendar,
    trading_calendar,
)
from dataplatform.ingest.fetcher import (
    Fetcher,
    FetchHTTPError,
    ForbiddenError,
    ForbiddenSpikeError,
    leased_fetcher,
)
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.no_session_journal import NoSessionJournal, journal_path
from dataplatform.ingest.nse.pr_bundle import (
    ARCHIVE_START,
    PR_BUNDLE_SOURCE_ID,
    URL_TEMPLATE,
    MemberKind,
    PrBundle,
)
from dataplatform.ingest.source_register import Source, SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "DEFAULT_ERROR_STREAK_LIMIT",
    "DEFAULT_NO_SESSION_STREAK_LIMIT",
    "EVIDENCE_NO_BUNDLE",
    "JOURNAL_FILENAME",
    "MCAP_ARRIVAL",
    "NAMING_CUTOVER",
    "AcquisitionReport",
    "BoundaryProbe",
    "BoundaryResult",
    "BundleAcquisition",
    "SessionOutcome",
    "SessionPlan",
    "SessionState",
    "bisect_boundary",
    "bundle_url",
    "coverage_report",
    "journal_path_for",
    "main",
    "plan_sessions",
]

_LOG = get_logger(__name__)

#: Consecutive *unexpected* failures before the run stops. 404s are expected and excluded.
DEFAULT_ERROR_STREAK_LIMIT: Final = 5

#: Consecutive 404s before the run stops — W1's limit, for W1's reason. A 404 is data, but "the
#: archive moved and now answers 404 for everything" and "the bundle was not published for these
#: sessions" are indistinguishable one request at a time. The longest unpublished run this archive
#: has been observed to have is a single session, so twenty in a row is a source change, and the
#: run stops and says so rather than writing a year of phantom evidence.
DEFAULT_NO_SESSION_STREAK_LIMIT: Final = 20

#: What an absence means for *this* source. Not `HOLIDAY_OR_NO_SESSION`: the plan is built from
#: sessions the calendar vouched for, so a 404 here is a claim about the archive's publication and
#: says nothing about whether the market traded. See `no_session_journal`.
EVIDENCE_NO_BUNDLE: Final = "NO_BUNDLE_PUBLISHED"

#: One journal per archive, so neither campaign consumes the other's observations as resume state.
JOURNAL_FILENAME: Final = "nse_pr_bundle_no_session.jsonl"

#: How often the run logs a cumulative line, so a supervisor reading only the log sees progress
#: without having to count `progress=` lines.
_HEARTBEAT_EVERY: Final = 100

_HTTP_NOT_FOUND: Final = 404

_WEEKEND_START: Final = 5


def journal_path_for(data_root: Path) -> Path:
    """Where this campaign's 404 evidence journal lives for a lake root."""
    return journal_path(data_root, filename=JOURNAL_FILENAME)


def _source_row(register: SourceRegister) -> Source:
    """The register row this driver serves, or a loud failure."""
    row = next((entry for entry in register.sources if entry.id == PR_BUNDLE_SOURCE_ID), None)
    if row is None:  # pragma: no cover — the register is validated at load
        raise KeyError(f"source {PR_BUNDLE_SOURCE_ID!r} is not in the source register")
    return row


def bundle_url(day: date, *, register: SourceRegister) -> str:
    """The archive URL for one session's bundle, from the register's verified template.

    What it does: fills `{DDMMYY}` from the register row, so a URL change stays one edit in the
    register (C.1), and cross-checks that template against the parser package's `URL_TEMPLATE`.
    What it assumes: `day` is a trading date. It makes no claim a bundle exists for it.
    What it never does: guess a URL for a date below `ARCHIVE_START`, or paper over a register
    that has drifted from the package the register names as its parser. The two disagreeing is a
    defect either way round — a driver fetching from one address while the readers document
    another — and it costs one string comparison to refuse instead of four thousand requests to
    discover.
    """
    if day < ARCHIVE_START:
        raise ValueError(
            f"{day.isoformat()} is before the archive's pinned floor {ARCHIVE_START.isoformat()}; "
            "nothing older is published (PR311209.zip and PR010110.zip are 404, PR040110.zip is "
            "200, and nine probes across 2005-2009 are 404)"
        )
    template = _source_row(register).url_template
    if template != URL_TEMPLATE:
        raise ValueError(
            f"source_register.yaml serves {PR_BUNDLE_SOURCE_ID!r} from {template!r} but "
            f"dataplatform.ingest.nse.pr_bundle documents {URL_TEMPLATE!r}; the register and its "
            "parser package must name one address"
        )
    return template.format(DDMMYY=day.strftime("%d%m%y"))


def _filename(url: str) -> str:
    """The L0 filename for a URL — its last path segment, which the archive already dates."""
    return url.rsplit("/", 1)[-1]


# ── planning ─────────────────────────────────────────────────────────────────────────────────


def _weekday_candidates(start: date, end: date) -> list[date]:
    """Every Monday-to-Friday date in the inclusive range — the calendar-free fallback."""
    day = start
    out: list[date] = []
    while day <= end:
        if day.weekday() < _WEEKEND_START:
            out.append(day)
        day += timedelta(days=1)
    return out


@dataclass(frozen=True, slots=True)
class SessionPlan:
    """The candidate dates one run will consider, and where they came from.

    `basis` is `calendar` when a calendar covering the range narrowed the plan to its
    expected-data dates, and `weekday` when it could not. An operator needs to know which, because
    the two differ in request count and in what an absence proves: under `calendar` a 404 means
    the archive published nothing for a session the exchange had, and under `weekday` it may only
    mean the exchange was shut.
    """

    start: date
    end: date
    dates: tuple[date, ...]
    basis: str
    note: str

    def __len__(self) -> int:
        return len(self.dates)


def plan_sessions(
    start: date,
    end: date,
    *,
    calendar: TradingCalendar | None = None,
    limit: int | None = None,
) -> SessionPlan:
    """The candidate sessions for a range, from the calendar when it covers it and weekdays if not.

    What it does: prefers `calendar.expected_data_dates`, which excludes declared holidays and
    *includes* weekend Muhurat sessions, and widens to every weekday when the calendar refuses the
    range. `limit` samples evenly across the whole span, so a smoke run crosses every format era
    rather than sitting in January 2010.
    What it assumes: the calendar now covers 2006-01-01..2026-12-31, so the whole of this
    archive's life takes the narrow branch. The fallback is kept anyway — a data file's coverage
    is not a thing driver code should assume, and W1's suite went red once for assuming it.
    What it never does: widen the calendar, plan below `ARCHIVE_START`, or narrow the plan on a
    guess. The fallback is louder and more expensive, never quieter.
    """
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    if start < ARCHIVE_START:
        raise ValueError(
            f"the bundle archive begins at {ARCHIVE_START.isoformat()} (pinned by measurement, "
            f"not bracketed); a plan from {start.isoformat()} would spend requests re-proving "
            "that nothing older is published"
        )
    if calendar is None:
        dates = _weekday_candidates(start, end)
        basis, note = "weekday", "no calendar supplied; every weekday is a candidate"
    else:
        try:
            dates = calendar.expected_data_dates(start, end)
            basis = "calendar"
            note = (
                f"calendar coverage {calendar.coverage_start.isoformat()}.."
                f"{calendar.coverage_end.isoformat()} spans the range; declared holidays are not "
                "requested and Muhurat weekends are"
            )
        except CalendarCoverageError as exc:
            dates = _weekday_candidates(start, end)
            basis = "weekday"
            note = (
                f"calendar refused the range ({exc}); falling back to every weekday, so every "
                "absence is proved by a 404 rather than assumed. Weekend Muhurat sessions are "
                "NOT candidates under this basis, and a 404 no longer distinguishes an "
                "unpublished bundle from a closed exchange"
            )
            _LOG.warning(
                "pr_bundle_campaign.calendar_gap",
                source=PR_BUNDLE_SOURCE_ID,
                start=start.isoformat(),
                end=end.isoformat(),
                coverage_start=calendar.coverage_start.isoformat(),
                coverage_end=calendar.coverage_end.isoformat(),
                basis=basis,
                state="PLANNED",
            )
    chosen = tuple(sample_dates(dates, limit))
    _LOG.info(
        "pr_bundle_campaign.planned",
        source=PR_BUNDLE_SOURCE_ID,
        start=start.isoformat(),
        end=end.isoformat(),
        basis=basis,
        candidates=len(chosen),
        state="PLANNED",
    )
    return SessionPlan(start=start, end=end, dates=chosen, basis=basis, note=note)


# ── acquisition: candidate sessions → L0 payloads and 404 evidence ───────────────────────────


class SessionState(StrEnum):
    """What one candidate session's acquisition attempt came to."""

    #: The archive served the bundle and it is now in L0. One request spent.
    FETCHED = "FETCHED"

    #: A payload is already under this key. Zero requests — the resume path.
    ALREADY_IN_L0 = "ALREADY_IN_L0"

    #: The archive answered 404: no bundle was published for a session the calendar vouched for.
    #: One request spent, no error counted.
    NO_BUNDLE = "NO_BUNDLE"

    #: The journal already recorded a 404 for this session. Zero requests — the other resume path.
    KNOWN_NO_BUNDLE = "KNOWN_NO_BUNDLE"

    #: Something unexpected. Counted toward the hard stop; the session stays retryable.
    FAILED = "FAILED"

    @property
    def spent_a_request(self) -> bool:
        """Whether reaching this state cost one request against the host's budget."""
        return self in {SessionState.FETCHED, SessionState.NO_BUNDLE}

    @property
    def is_retained(self) -> bool:
        """Whether the session's payload is in L0 after this outcome."""
        return self in {SessionState.FETCHED, SessionState.ALREADY_IN_L0}


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """One candidate session's acquisition result — the row a report and a test both read."""

    trade_date: date
    state: SessionState
    url: str
    filename: str
    sha256: str | None = None
    size_bytes: int | None = None
    error: str | None = None


@dataclass(slots=True)
class AcquisitionReport:
    """What one acquisition run did, and why it ended if it ended early."""

    requested: int
    outcomes: list[SessionOutcome] = field(default_factory=list)
    hard_stopped: bool = False
    stop_reason: str | None = None

    def _count(self, state: SessionState) -> int:
        return sum(1 for outcome in self.outcomes if outcome.state is state)

    @property
    def fetched(self) -> int:
        """Sessions whose bundle this run brought into L0."""
        return self._count(SessionState.FETCHED)

    @property
    def already_in_l0(self) -> int:
        """Sessions skipped because their bundle was already stored."""
        return self._count(SessionState.ALREADY_IN_L0)

    @property
    def no_bundle(self) -> int:
        """Sessions this run proved no bundle was published for (a fresh 404)."""
        return self._count(SessionState.NO_BUNDLE)

    @property
    def known_no_bundle(self) -> int:
        """Sessions already known to have no bundle, skipped without a request."""
        return self._count(SessionState.KNOWN_NO_BUNDLE)

    @property
    def failed(self) -> int:
        """Unexpected failures. A 404 is never one of these."""
        return self._count(SessionState.FAILED)

    @property
    def requests_spent(self) -> int:
        """Requests this run put to the host — the number the budget rule cares about."""
        return sum(1 for outcome in self.outcomes if outcome.state.spent_a_request)

    @property
    def bytes_fetched(self) -> int:
        """Payload bytes this run added to the lake."""
        return sum(outcome.size_bytes or 0 for outcome in self.outcomes)

    @property
    def no_bundle_dates(self) -> tuple[date, ...]:
        """Sessions this run observed a 404 for, ascending."""
        return tuple(
            sorted(
                outcome.trade_date
                for outcome in self.outcomes
                if outcome.state is SessionState.NO_BUNDLE
            )
        )

    def summary(self) -> str:
        """One line for the operator and the campaign log."""
        return (
            f"{self.requested} sessions: {self.fetched} fetched, "
            f"{self.already_in_l0} already in L0, {self.no_bundle} no bundle published (404), "
            f"{self.known_no_bundle} already known unpublished, {self.failed} failed; "
            f"{self.requests_spent} requests spent"
            + (f" — STOPPED: {self.stop_reason}" if self.hard_stopped else "")
        )


class BundleAcquisition:
    """Brings PR bundles into L0, resumably, and records every 404 as evidence.

    What it does: for each candidate session, skips it for free if L0 already holds the bundle or
    the journal already proves it unpublished; otherwise fetches it. A 404 becomes a journal entry,
    a 403 spike ends the run, and anything else is a counted failure that leaves the session
    retryable.
    What it assumes: L0 immutability does the deduplication it advertises — mode `0o444`, writes
    that refuse to overwrite, differing bytes at one key raising. Nothing here deletes or rewrites
    a payload, and there is no flag that would.
    What it never does: parse a bundle, promote anything, write to `corporate_actions` or
    `sync_state`, touch the calendar, or lower the request spacing. Spacing and the 403 hard stop
    belong to the fetcher's crawl policy and are not weakened here.
    """

    def __init__(
        self,
        *,
        fetcher: Fetcher,
        l0: L0Store,
        journal: NoSessionJournal,
        register: SourceRegister,
        error_streak_limit: int = DEFAULT_ERROR_STREAK_LIMIT,
        no_session_streak_limit: int = DEFAULT_NO_SESSION_STREAK_LIMIT,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self._fetcher = fetcher
        self._l0 = l0
        self._journal = journal
        self._register = register
        self._error_limit = error_streak_limit
        self._no_session_limit = no_session_streak_limit
        self._should_stop = should_stop

    def run(self, sessions: Sequence[date]) -> AcquisitionReport:
        """Acquire every candidate session in order, stopping only for the documented reasons.

        The first thing it logs is the lake it is about to write to. A three-hour campaign aimed
        at the wrong `data_root` is recoverable only by hand, and the log line that would have
        prevented it is worth more before the first request than after the last.
        """
        _LOG.info(
            "pr_bundle_campaign.lake",
            source=PR_BUNDLE_SOURCE_ID,
            data_root=str(self._l0.data_root),
            l0_root=str(self._l0.root),
            journal=str(self._journal.path),
            journal_records=len(self._journal.records),
            sessions=len(sessions),
            state="PLANNED",
        )
        report = AcquisitionReport(requested=len(sessions))
        error_streak = 0
        no_bundle_streak = 0
        total = len(sessions)

        for index, day in enumerate(sessions, start=1):
            if self._should_stop():
                report.hard_stopped = True
                report.stop_reason = "stop requested (SIGINT)"
                _LOG.warning(
                    "pr_bundle_campaign.stopping",
                    source=PR_BUNDLE_SOURCE_ID,
                    date=day.isoformat(),
                    progress=f"{index}/{total}",
                    remaining=total - index + 1,
                    state="STOPPED",
                )
                break

            outcome = self._acquire_one(day, index=index, total=total)
            report.outcomes.append(outcome)

            if outcome.state is SessionState.FAILED:
                error_streak += 1
            elif outcome.state.spent_a_request:
                # A 404 is a completed round-trip and proof the host is answering, so it clears an
                # error streak. It is counted on its own streak instead — see the constant.
                error_streak = 0

            no_bundle_streak = (
                no_bundle_streak + 1 if outcome.state is SessionState.NO_BUNDLE else 0
            )

            if index % _HEARTBEAT_EVERY == 0:
                _LOG.info(
                    "pr_bundle_campaign.heartbeat",
                    source=PR_BUNDLE_SOURCE_ID,
                    progress=f"{index}/{total}",
                    date=day.isoformat(),
                    fetched=report.fetched,
                    already_in_l0=report.already_in_l0,
                    no_bundle=report.no_bundle,
                    failed=report.failed,
                    requests_spent=report.requests_spent,
                    bytes_fetched=report.bytes_fetched,
                    state="RUNNING",
                )

            if error_streak >= self._error_limit:
                report.hard_stopped = True
                report.stop_reason = f"{error_streak} consecutive unexpected failures"
                _LOG.critical(
                    "pr_bundle_campaign.hard_stop",
                    source=PR_BUNDLE_SOURCE_ID,
                    date=day.isoformat(),
                    progress=f"{index}/{total}",
                    streak=error_streak,
                    reason=report.stop_reason,
                    state="HARD_STOPPED",
                )
                break
            if no_bundle_streak >= self._no_session_limit:
                report.hard_stopped = True
                report.stop_reason = (
                    f"{no_bundle_streak} consecutive 404s — longer than any unpublished run this "
                    "archive has shown, so this is a source change, not missing bundles"
                )
                _LOG.critical(
                    "pr_bundle_campaign.hard_stop",
                    source=PR_BUNDLE_SOURCE_ID,
                    date=day.isoformat(),
                    progress=f"{index}/{total}",
                    streak=no_bundle_streak,
                    reason=report.stop_reason,
                    state="HARD_STOPPED",
                )
                break
            if self._fetcher.is_stopped(_source_row(self._register).host):
                report.hard_stopped = True
                report.stop_reason = "403 spike: the fetcher has hard-stopped this host"
                _LOG.critical(
                    "pr_bundle_campaign.hard_stop",
                    source=PR_BUNDLE_SOURCE_ID,
                    date=day.isoformat(),
                    progress=f"{index}/{total}",
                    reason=report.stop_reason,
                    state="HARD_STOPPED",
                )
                break

        _LOG.info(
            "pr_bundle_campaign.acquire_done",
            source=PR_BUNDLE_SOURCE_ID,
            requested=report.requested,
            fetched=report.fetched,
            already_in_l0=report.already_in_l0,
            no_bundle=report.no_bundle,
            known_no_bundle=report.known_no_bundle,
            failed=report.failed,
            requests_spent=report.requests_spent,
            bytes_fetched=report.bytes_fetched,
            hard_stopped=report.hard_stopped,
            state="STOPPED" if report.hard_stopped else "DONE",
        )
        return report

    def acquire_one(self, day: date) -> SessionOutcome:
        """One session, outside a planned run — what the era bisection probes with."""
        return self._acquire_one(day, index=1, total=1)

    def ref_for(self, day: date) -> L0Ref:
        """The stored ref for a session's bundle. Raises if it is not in L0."""
        name = _filename(bundle_url(day, register=self._register))
        return self._l0.ref_for(PR_BUNDLE_SOURCE_ID, day, name)

    def _acquire_one(self, day: date, *, index: int, total: int) -> SessionOutcome:
        """One candidate session. Never raises: every outcome is a `SessionOutcome`."""
        try:
            url = bundle_url(day, register=self._register)
        except ValueError as exc:
            return SessionOutcome(
                trade_date=day,
                state=SessionState.FAILED,
                url="",
                filename="",
                error=str(exc),
            )
        name = _filename(url)

        if self._l0.exists(PR_BUNDLE_SOURCE_ID, day, name):
            _LOG.info(
                "pr_bundle_campaign.skip_stored",
                source=PR_BUNDLE_SOURCE_ID,
                date=day.isoformat(),
                progress=f"{index}/{total}",
                state=SessionState.ALREADY_IN_L0.value,
            )
            return SessionOutcome(
                trade_date=day, state=SessionState.ALREADY_IN_L0, url=url, filename=name
            )

        if self._journal.knows(day):
            _LOG.info(
                "pr_bundle_campaign.skip_no_bundle",
                source=PR_BUNDLE_SOURCE_ID,
                date=day.isoformat(),
                progress=f"{index}/{total}",
                state=SessionState.KNOWN_NO_BUNDLE.value,
            )
            return SessionOutcome(
                trade_date=day, state=SessionState.KNOWN_NO_BUNDLE, url=url, filename=name
            )

        try:
            ref = self._fetcher.fetch(PR_BUNDLE_SOURCE_ID, url, day, filename=name)
        except ForbiddenSpikeError as spike:
            # Deliberately not a per-session failure that a retry could clear: the host is refused
            # for the life of the process (§8), and the loop's own check ends the run next tick.
            _LOG.critical(
                "pr_bundle_campaign.forbidden_spike",
                source=PR_BUNDLE_SOURCE_ID,
                date=day.isoformat(),
                error=str(spike),
                state=SessionState.FAILED.value,
            )
            return SessionOutcome(
                trade_date=day,
                state=SessionState.FAILED,
                url=url,
                filename=name,
                error=f"ForbiddenSpikeError: {spike}",
            )
        except ForbiddenError as refused:
            return self._failed(day, url, name, f"ForbiddenError: {refused}")
        except FetchHTTPError as http_error:
            if http_error.status_code != _HTTP_NOT_FOUND:
                return self._failed(day, url, name, f"HTTP {http_error.status_code}")
            record = self._journal.record(day, url=url, http_status=http_error.status_code)
            _LOG.info(
                "pr_bundle_campaign.no_bundle",
                source=PR_BUNDLE_SOURCE_ID,
                date=day.isoformat(),
                progress=f"{index}/{total}",
                http_status=record.http_status,
                evidence=record.evidence,
                journal=str(self._journal.path),
                state=SessionState.NO_BUNDLE.value,
            )
            return SessionOutcome(
                trade_date=day, state=SessionState.NO_BUNDLE, url=url, filename=name
            )
        except Exception as exc:  # transport, L0, anything unforeseen — counted, never swallowed
            return self._failed(day, url, name, f"{type(exc).__name__}: {exc}")

        _LOG.info(
            "pr_bundle_campaign.stored",
            source=PR_BUNDLE_SOURCE_ID,
            date=day.isoformat(),
            progress=f"{index}/{total}",
            sha256=ref.sha256,
            size_bytes=ref.size_bytes,
            l0_key=ref.key,
            state=SessionState.FETCHED.value,
        )
        return SessionOutcome(
            trade_date=day,
            state=SessionState.FETCHED,
            url=url,
            filename=name,
            sha256=ref.sha256,
            size_bytes=ref.size_bytes,
        )

    def _failed(self, day: date, url: str, name: str, message: str) -> SessionOutcome:
        """Record one unexpected failure loudly. The session stays retryable on the next run."""
        _LOG.error(
            "pr_bundle_campaign.session_failed",
            source=PR_BUNDLE_SOURCE_ID,
            date=day.isoformat(),
            url=url,
            error=message,
            state=SessionState.FAILED.value,
        )
        return SessionOutcome(
            trade_date=day, state=SessionState.FAILED, url=url, filename=name, error=message
        )


# ── era boundaries: the two brackets Phase 1 could not afford to buy down ────────────────────


@dataclass(frozen=True, slots=True)
class BoundaryProbe:
    """A monotone yes/no question about a bundle, and the bracket it is being pinned inside.

    Phase 1 measured four format eras and pinned two of the three boundaries between them; the
    other two it left as *brackets* rather than guessing them to a date. Each is the first session
    at which one question about the payload flips from no to yes, and `bisect_boundary` finds that
    session in ~log2(n) requests instead of n.

    `monotone_because` is the assumption the bisection rests on, written down where it can be
    read: a question that flips back and forth inside the bracket would make a bisection return a
    boundary that is merely *a* flip rather than *the* one. It is not provable from inside the
    search, so the bisection also probes the session after the answer to check the flip holds.
    """

    name: str
    question: str
    predicate: Callable[[PrBundle], bool]
    after: date
    until: date
    monotone_because: str


def _has_mcap(bundle: PrBundle) -> bool:
    """Whether the bundle carries an `mcap` member — the question era 3 turns on."""
    return bundle.has(MemberKind.MCAP)


def _bc_name_is_lowercase(bundle: PrBundle) -> bool:
    """Whether the `Bc` member's own name is lowercase — the question era 4 turns on.

    Asked of the `Bc` member specifically, not of the bundle: the readme members every bundle
    ships are lowercase in every era, so `all(member.name.islower())` would answer the wrong
    question. Raises when the bundle carries no `Bc` member at all, which for this archive would
    itself be the format change worth stopping for.
    """
    member = bundle.member(MemberKind.BC)
    if member is None:
        raise ParseError(
            "carries no Bc member; the naming cutover cannot be read from a bundle that does "
            "not publish the file it is about",
            filename=bundle.filename,
        )
    return member.name.islower()


#: `classic` → `mcap_upper`: the first bundle carrying an `mcap` member. Phase 1 measured
#: 2024-01-02 without one and 2024-07-01 with one, and its 40-request budget stopped there.
MCAP_ARRIVAL: Final = BoundaryProbe(
    name="mcap_arrival",
    question="does the bundle carry an mcap member?",
    predicate=_has_mcap,
    after=date(2024, 1, 2),
    until=date(2024, 7, 1),
    monotone_because=(
        "a member the exchange started publishing daily; every bundle measured after 2024-07-01 "
        "carries one, and no era after it drops the member"
    ),
)

#: `mcap_upper` → `lowercase`: the first bundle whose `Bc` member name is lowercase. Phase 1
#: measured 2025-10-01 uppercase and 2025-11-03 lowercase.
NAMING_CUTOVER: Final = BoundaryProbe(
    name="naming_cutover",
    question="is the Bc member's name lowercase?",
    predicate=_bc_name_is_lowercase,
    after=date(2025, 10, 1),
    until=date(2025, 11, 3),
    monotone_because=(
        "a one-time rename of the exchange's own output; every bundle measured after the cutover "
        "is lowercase and every one before it is not"
    ),
)


@dataclass(frozen=True, slots=True)
class BoundaryResult:
    """Where one era boundary actually falls, and what it cost to find out."""

    probe: str
    last_false: date
    first_true: date
    requests_spent: int
    probed: tuple[tuple[date, bool], ...]
    confirmed_next: date | None = None
    unresolved: str | None = None

    def summary(self) -> str:
        """One line for the operator, the evidence log and the register note."""
        if self.unresolved is not None:
            return (
                f"{self.probe}: NOT PINNED — {self.unresolved}; still bracketed "
                f"({self.last_false.isoformat()}, {self.first_true.isoformat()}]"
            )
        held = (
            f", confirmed still true at {self.confirmed_next.isoformat()}"
            if self.confirmed_next is not None
            else ", not confirmed past the boundary"
        )
        return (
            f"{self.probe}: first true {self.first_true.isoformat()}, last false "
            f"{self.last_false.isoformat()} ({self.requests_spent} requests{held})"
        )


def bisect_boundary(
    probe: BoundaryProbe,
    *,
    driver: BundleAcquisition,
    l0: L0Store,
    calendar: TradingCalendar,
) -> BoundaryResult:
    """Pin one era boundary to a session by bisection over the calendar's sessions in its bracket.

    What it does: asks `probe.question` of the two known ends, then halves the bracket, ~log2(n)
    requests instead of n. Every probe goes through the normal acquisition path, so its payload
    lands checksummed in L0 and the campaign that follows gets it for free, and a re-run of the
    bisection over the same bracket costs zero requests.
    What it assumes: the answer is monotone across the bracket (`probe.monotone_because`), and the
    calendar covers it. It checks the ends rather than trusting Phase 1's measurement, because an
    end that has flipped means the bracket, not the boundary, is wrong.
    What it never does: guess. A bracket it cannot resolve — a probe that fails, or a bundle
    missing where the search needs an answer — comes back `unresolved` with the bracket it got to,
    and the register keeps saying "NOT PINNED".
    """
    sessions = calendar.expected_data_dates(probe.after, probe.until)
    if len(sessions) < 2 or sessions[0] != probe.after or sessions[-1] != probe.until:
        raise ValueError(
            f"{probe.name}: the bracket ends {probe.after.isoformat()} and "
            f"{probe.until.isoformat()} must both be sessions the calendar expects data for"
        )

    spent = 0
    probed: list[tuple[date, bool]] = []

    def ask(day: date) -> bool | None:
        """The probe's answer for one session, or None when the archive/parse could not answer."""
        nonlocal spent
        outcome = driver.acquire_one(day)
        if outcome.state.spent_a_request:
            spent += 1
        if not outcome.state.is_retained:
            _LOG.warning(
                "pr_bundle_campaign.probe_unanswered",
                source=PR_BUNDLE_SOURCE_ID,
                probe=probe.name,
                date=day.isoformat(),
                outcome=outcome.state.value,
                state="SKIPPED",
            )
            return None
        with PrBundle.from_l0(l0, driver.ref_for(day)) as bundle:
            answer = probe.predicate(bundle)
        probed.append((day, answer))
        _LOG.info(
            "pr_bundle_campaign.probe",
            source=PR_BUNDLE_SOURCE_ID,
            probe=probe.name,
            date=day.isoformat(),
            question=probe.question,
            answer=answer,
            requests_spent=spent,
            state="MEASURED",
        )
        return answer

    def unresolved(reason: str, lo: int, hi: int) -> BoundaryResult:
        _LOG.error(
            "pr_bundle_campaign.boundary_unresolved",
            source=PR_BUNDLE_SOURCE_ID,
            probe=probe.name,
            reason=reason,
            bracket=f"({sessions[lo].isoformat()}, {sessions[hi].isoformat()}]",
            state="FAILED",
        )
        return BoundaryResult(
            probe=probe.name,
            last_false=sessions[lo],
            first_true=sessions[hi],
            requests_spent=spent,
            probed=tuple(probed),
            unresolved=reason,
        )

    lo, hi = 0, len(sessions) - 1
    low_answer = ask(sessions[lo])
    if low_answer is None:
        return unresolved("the bracket's lower end could not be answered", lo, hi)
    if low_answer:
        return unresolved(
            f"the bracket's lower end {sessions[lo].isoformat()} already answers yes, so the "
            "boundary is earlier than Phase 1 bracketed it",
            lo,
            hi,
        )
    high_answer = ask(sessions[hi])
    if high_answer is None:
        return unresolved("the bracket's upper end could not be answered", lo, hi)
    if not high_answer:
        return unresolved(
            f"the bracket's upper end {sessions[hi].isoformat()} still answers no, so the "
            "boundary is later than Phase 1 bracketed it",
            lo,
            hi,
        )

    while hi - lo > 1:
        at = (lo + hi) // 2
        answer = ask(sessions[at])
        # An unanswerable session is not a verdict: walk toward `hi` for the next one that can
        # answer rather than assuming either side, and give up loudly if none can.
        while answer is None and at + 1 < hi:
            at += 1
            answer = ask(sessions[at])
        if answer is None:
            return unresolved("no session inside the bracket could be answered", lo, hi)
        if answer:
            hi = at
        else:
            lo = at

    confirmed: date | None = None
    following = calendar.expected_data_dates(
        sessions[hi] + timedelta(days=1), sessions[hi] + timedelta(days=14)
    )
    if following:
        # One request to check the flip holds. `None` is not a counter-example — an unpublished
        # bundle says nothing about the format — but `False` is, and it means the bisection found
        # *a* flip rather than *the* boundary.
        holds = ask(following[0])
        if holds is True:
            confirmed = following[0]
        elif holds is False:
            return unresolved(
                f"the answer flipped back at {following[0].isoformat()}, so this bracket holds "
                f"more than one flip and the monotonicity it assumes "
                f"({probe.monotone_because}) does not hold",
                lo,
                hi,
            )

    result = BoundaryResult(
        probe=probe.name,
        last_false=sessions[lo],
        first_true=sessions[hi],
        requests_spent=spent,
        probed=tuple(probed),
        confirmed_next=confirmed,
    )
    _LOG.info(
        "pr_bundle_campaign.boundary_pinned",
        source=PR_BUNDLE_SOURCE_ID,
        probe=probe.name,
        first_true=result.first_true.isoformat(),
        last_false=result.last_false.isoformat(),
        requests_spent=result.requests_spent,
        state="MEASURED",
    )
    return result


# ── report: what the lake holds, read off the lake ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _YearRow:
    year: int
    candidates: int
    in_l0: int
    no_bundle: int
    not_attempted: int
    bytes_stored: int


def coverage_report(
    plan: SessionPlan,
    *,
    l0: L0Store,
    journal: NoSessionJournal,
    register: SourceRegister,
) -> str:
    """The markdown coverage artefact: per-year sessions and bytes, and the 404 list.

    Offline and read-only — it opens no socket and no database, and it opens no zip either: every
    number comes from L0 keys, L0 sidecars and the journal, so the artefact is regenerable and
    cannot drift from the lake. There is no promoted-rows column on purpose: nothing in this
    campaign promotes, and a column of zeros would read as a pipeline that had failed rather than
    one that does not exist yet.
    """
    stored: dict[date, int] = {
        ref.logical_date: ref.size_bytes
        for ref in l0.iter_refs(PR_BUNDLE_SOURCE_ID, start=plan.start, end=plan.end)
    }
    rows: list[_YearRow] = []
    for year in sorted({day.year for day in plan.dates}):
        days = [day for day in plan.dates if day.year == year]
        in_l0 = sum(1 for day in days if day in stored)
        no_bundle = sum(1 for day in days if day not in stored and journal.knows(day))
        rows.append(
            _YearRow(
                year=year,
                candidates=len(days),
                in_l0=in_l0,
                no_bundle=no_bundle,
                not_attempted=len(days) - in_l0 - no_bundle,
                bytes_stored=sum(stored.get(day, 0) for day in days),
            )
        )

    lines = [
        f"### NSE PR bundle coverage — {plan.start.isoformat()}..{plan.end.isoformat()}",
        "",
        f"Plan basis: **{plan.basis}** ({plan.note})",
        "",
        "Acquired into L0 only — every member is symbol-keyed with no ISIN, so nothing here is "
        "promoted to L1 or reconciled against `corporate_actions` (invariant #2).",
        "",
        "| year | sessions | in L0 | 404 (no bundle) | not attempted | MB stored |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {row.year} | {row.candidates} | {row.in_l0} | {row.no_bundle} | "
        f"{row.not_attempted} | {row.bytes_stored / 1e6:.1f} |"
        for row in rows
    ]
    lines += [
        f"| **total** | **{sum(r.candidates for r in rows)}** | "
        f"**{sum(r.in_l0 for r in rows)}** | **{sum(r.no_bundle for r in rows)}** | "
        f"**{sum(r.not_attempted for r in rows)}** | "
        f"**{sum(r.bytes_stored for r in rows) / 1e6:.1f}** |",
        "",
        "#### 404 evidence (a session the calendar expects, for which no bundle was published)",
        "",
    ]
    observed = sorted(day for day in journal.dates if plan.start <= day <= plan.end)
    lines.append(
        ", ".join(day.isoformat() for day in observed) if observed else "_none observed yet_"
    )
    lines += [
        "",
        f"Source: `{PR_BUNDLE_SOURCE_ID}` at `{_source_row(register).url_template}`; "
        f"journal `{journal.path}`.",
    ]
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def _install_sigint(state: dict[str, bool]) -> None:
    """Flip `state['stop']` on the first SIGINT so the run stops between sessions.

    Graceful by design: an in-flight fetch finishes and its payload lands in L0 rather than the
    process being torn down mid-write. A second SIGINT restores the default handler, so an
    impatient operator can still hard-kill.
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pr-bundle-campaign", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("plan", "print the candidate sessions and their URLs; opens nothing"),
        ("acquire", "fetch candidate bundles into L0, recording every 404 as evidence"),
        ("report", "print the markdown coverage artefact from L0 and the journal"),
        ("pin-eras", "bisect the two format-era boundaries Phase 1 left as brackets"),
    ):
        child = sub.add_parser(name, help=help_text)
        if name == "pin-eras":
            continue
        child.add_argument("--from", dest="from_date", type=date.fromisoformat)
        child.add_argument("--to", dest="to_date", type=date.fromisoformat)
        child.add_argument(
            "--limit",
            type=int,
            default=None,
            help="sample this many sessions, spread evenly across the range",
        )
        child.add_argument(
            "--no-calendar",
            action="store_true",
            help="plan from weekdays even when the calendar covers the range",
        )
        if name == "acquire":
            child.add_argument("--error-streak-limit", type=int, default=DEFAULT_ERROR_STREAK_LIMIT)
            child.add_argument(
                "--no-session-streak-limit", type=int, default=DEFAULT_NO_SESSION_STREAK_LIMIT
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Exit 0 on a clean run, 2 on a bad invocation, 3 on a hard stop."""
    args = _build_parser().parse_args(argv)
    settings: Settings = get_settings()
    clock: Clock = SystemClock()
    register = load_register()
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    journal = NoSessionJournal(
        journal_path_for(settings.data_root), clock=clock, evidence=EVIDENCE_NO_BUNDLE
    )
    host = _source_row(register).host

    if args.command == "pin-eras":
        # Named after what it costs: ~14 requests against the same host and the same budget the
        # campaign uses, so it takes the same lease and runs to completion before the sweep.
        print(f"lake: {settings.data_root}  L0: {l0.root}")
        with leased_fetcher(
            [host],
            clock=clock,
            command="pr_bundle_campaign pin-eras",
            settings=settings,
            register=register,
        ) as fetcher:
            driver = BundleAcquisition(fetcher=fetcher, l0=l0, journal=journal, register=register)
            results = [
                bisect_boundary(probe, driver=driver, l0=l0, calendar=trading_calendar())
                for probe in (MCAP_ARRIVAL, NAMING_CUTOVER)
            ]
        for result in results:
            print(result.summary())
        return 0 if all(result.unresolved is None for result in results) else 3

    calendar = None if args.no_calendar else trading_calendar()
    if args.from_date is None or args.to_date is None:
        print("give both --from and --to", file=sys.stderr)
        return 2
    try:
        plan = plan_sessions(args.from_date, args.to_date, calendar=calendar, limit=args.limit)
    except ValueError as exc:
        print(f"cannot plan: {exc}", file=sys.stderr)
        return 2

    if args.command == "plan":
        for day in plan.dates:
            print(f"{day.isoformat()}\t{bundle_url(day, register=register)}")
        print(f"\n{len(plan)} sessions, basis={plan.basis} ({plan.note})")
        print(f"lake: {settings.data_root}  L0: {l0.root}")
        return 0

    if args.command == "report":
        print(coverage_report(plan, l0=l0, journal=journal, register=register))
        return 0

    # `leased_fetcher`, not `build_fetcher`: the archive host carries prices, delivery, corporate
    # actions and fundamentals, and this campaign holds its budget for hours. The lease is the
    # enforced form of the one-budget-per-host rule, and a second driver on this box refuses to
    # start rather than halving the spacing.
    print(f"lake: {settings.data_root}  L0: {l0.root}  journal: {journal.path}", flush=True)
    state = {"stop": False}
    _install_sigint(state)
    with leased_fetcher(
        [host],
        clock=clock,
        command=f"pr_bundle_campaign acquire {plan.start.isoformat()}..{plan.end.isoformat()}",
        settings=settings,
        register=register,
    ) as fetcher:
        report = BundleAcquisition(
            fetcher=fetcher,
            l0=l0,
            journal=journal,
            register=register,
            error_streak_limit=args.error_streak_limit,
            no_session_streak_limit=args.no_session_streak_limit,
            should_stop=lambda: state["stop"],
        ).run(plan.dates)
    print(report.summary())
    return 3 if report.hard_stopped else 0


if __name__ == "__main__":
    sys.exit(main())
