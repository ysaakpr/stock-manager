"""W1: the NSE legacy-bhavcopy deep backfill — 2006 to where the lake begins.

`dataplatform.ingest.backfill` drives a decade the platform already understands: every date in its
plan is a session the C.2 calendar vouched for, every payload parses to `PriceRow`s, and a fetch
that comes back 404 is a failure worth retrying. None of those three hold below 2016, so this is a
separate driver rather than another `SOURCE_SETS` entry, and the three differences are the whole
reason it exists.

**1. The calendar is an input, not a precondition.** `nse_holidays.yaml` covers 2016 onward and
refuses a range that leaves its span — correctly, because assuming an uncovered year has no
holidays would manufacture ~15 phantom sessions a year. So acquisition does not ask it. It
enumerates *candidate* dates and lets the archive answer, which needs no calendar at all: L0 is raw
bytes under a key. When a calendar covering the range **is** available the plan narrows to its
expected-data dates (which is also how weekend Muhurat sessions get fetched); when it is not, the
plan widens to every weekday and each absence is proved by a request rather than assumed. The
fallback is louder and more expensive, never quieter — and the guard in `calendar.py` is untouched.
Promotion and reconciliation take the calendar by injection, so a test can hand them one covering
2006-2016 and production picks up the extended file the moment it lands, with no edit here.

**2. A 404 is data.** It means the exchange was shut, and the campaign's by-product — the exact list
of dates the archive serves — is the highest-authority historical trading calendar that exists. A
404 is recorded as `HOLIDAY_OR_NO_SESSION` in an append-only evidence journal, does **not** count
toward the hard stop, and is never a `FAILED` row. Where that evidence disagrees with
`nse_holidays.yaml`, `reconcile_calendar` reports the diff; nothing here patches the calendar, and
nothing here widens it to make a fetch succeed.

**3. Half the range has no identity.** Era E1 (before 2011-06-22) has no ISIN column, and ISIN is
the only join key (invariant #2), so its rows cannot enter `prices_raw`. They are still fetched and
still stored — L0 is the immutable record, storage is cheap, and this is the one chance to never
re-fetch — and then enumerated into `prices_raw_quarantine`, which drops nothing. The per-year count
of those unresolved rows is published by `promote`, because that number is the honest bound on how
far back this platform can claim to reach. No symbol→ISIN mapping is offered: the only resolver
available is a current-day listing, and every company delisted before today is absent from it.

Resume is L0 plus the journal, not `sync_state`: a payload already under its key is skipped without
a request, and so is a date the journal already records a 404 for. A second run over an
already-acquired range therefore performs **zero** fetches, and needs neither a database nor a
calendar to establish that.

Operator flow (see `ops/runbooks/backfill.md` for the shallow one):

    uv run python -m dataplatform.ingest.legacy_backfill plan    --from 2006-01-02 --to 2016-09-01
    uv run python -m dataplatform.ingest.legacy_backfill acquire --from 2006-01-02 --to 2016-09-01
    uv run python -m dataplatform.ingest.legacy_backfill promote --from 2011-06-22 --to 2016-09-01
    uv run python -m dataplatform.ingest.legacy_backfill report  --from 2006-01-02 --to 2016-09-01
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.identity.master import Exchange
from dataplatform.ingest.backfill import NSE_BHAVCOPY, sample_dates
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
from dataplatform.ingest.nse import bhavcopy, bhavcopy_legacy, eras
from dataplatform.ingest.nse.eras import BhavcopyEra
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import write_prices_raw, write_unidentified_quarantine
from dataplatform.store.paths import Layer, partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_QUARANTINE_DATASET

__all__ = [
    "DEFAULT_ERROR_STREAK_LIMIT",
    "DEFAULT_NO_SESSION_STREAK_LIMIT",
    "NSE_BHAVCOPY_PRE_ISIN",
    "AcquisitionReport",
    "CalendarDiff",
    "LegacyAcquisition",
    "LegacyPromotion",
    "NoSessionJournal",
    "NoSessionRecord",
    "PromotedSession",
    "PromotionReport",
    "SessionOutcome",
    "SessionPlan",
    "SessionState",
    "coverage_report",
    "journal_path_for",
    "l1_row_counts",
    "legacy_url",
    "main",
    "plan_sessions",
    "reconcile_calendar",
    "require_legacy_era",
    "weekday_candidates",
]

_LOG = get_logger(__name__)

#: The `sync_state` source the pre-ISIN era publishes under — deliberately **not** `nse_bhavcopy`.
#: The two eras have different *identity* preconditions, so their coverage questions are different
#: ones: "is this date's price partition in L1?" for the ISIN era, and "is this date's raw payload
#: retained and enumerated?" for E1, whose rows can never reach `prices_raw`. Publishing E1 under
#: `nse_bhavcopy` would tell the trading interlock a 2008 date has prices. This is the same split
#: `backfill.BSE_BHAVCOPY_LEGACY` makes, for the same reason.
NSE_BHAVCOPY_PRE_ISIN: Final = "nse_bhavcopy_pre_isin"

#: Consecutive *unexpected* failures before the run stops. 404s are expected and excluded.
DEFAULT_ERROR_STREAK_LIMIT: Final = 5

#: Consecutive 404s before the run stops. A 404 is data, not an error — but "the archive moved and
#: now answers 404 for everything" and "the exchange was shut" are indistinguishable one request at
#: a time, and over 2,670 requests the first would silently record a decade of phantom holidays.
#: The longest closure NSE has ever had is a few sessions, so twenty in a row is not a holiday run;
#: it is a source change, and the run stops and says so rather than manufacturing evidence.
DEFAULT_NO_SESSION_STREAK_LIMIT: Final = 20

#: What the archive answers for a date the exchange did not trade: 404 with a small HTML error page,
#: never 200-with-no-rows (verified 2026-09-07 against `cm26JAN2026`).
_HTTP_NOT_FOUND: Final = 404

_WEEKEND_START: Final = 5

#: English month abbreviations for the legacy URL's `{MON}`. Spelled out rather than handed to
#: `strftime("%b")`, whose output follows `LC_TIME` — the same locale guard the parsers use.
_MON: Final[tuple[str, ...]] = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)  # fmt: skip


# ── the 404 evidence journal ─────────────────────────────────────────────────────────────────


class NoSessionRecord(BaseModel):
    """One dated observation that the archive served no file — a closed exchange, recorded.

    Written as one JSON object per line so the journal is append-only in the strongest sense the
    filesystem offers: a crash mid-campaign truncates at a line boundary and loses one
    observation, never the file. It is validated on the way back in because it is *evidence* — a
    line this schema does not recognise is a corrupt journal, and reading past it would quietly
    turn a lost 404 into a re-fetch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date = Field(description="the candidate session the archive had no file for")
    era: str = Field(description="the format era label the date falls in, e.g. 'E1'")
    url: str = Field(description="the exact URL that answered")
    http_status: int = Field(ge=100, le=599, description="the status observed, normally 404")
    observed_at: datetime = Field(description="tz-aware instant of the observation (injected)")
    evidence: str = Field(
        default="HOLIDAY_OR_NO_SESSION",
        description="what the absence means: the exchange was shut, not that a fetch failed",
    )


def journal_path_for(data_root: Path) -> Path:
    """Where the 404 evidence journal lives for a lake root.

    Beside the lake rather than inside `L0/`: `L0Store.verify_checksums` walks the L0 tree looking
    for payloads without sidecars, and a stray file there would be reported as an orphan defect.
    The journal is derived observation, not a fetched payload.
    """
    return data_root / "campaign" / "nse_bhavcopy_no_session.jsonl"


class NoSessionJournal:
    """The append-only record of which candidate dates the archive answered 404 for.

    What it does: remembers, across runs and without a database, that a date has already been
    proved a non-session — so the resume path costs zero requests for it — and hands the whole
    observation set to the calendar reconciler and the coverage report.
    What it assumes: it owns its file. Two concurrent campaigns over the same range would both
    append, which is harmless for the date set but duplicates lines.
    What it never does: forget, rewrite, or delete a line. This is the highest-authority record of
    the historical NSE trading calendar that exists, and the campaign only gets to observe each
    date once cheaply.
    """

    def __init__(self, path: Path, *, clock: Clock | None = None) -> None:
        self._path = path
        self._clock = SystemClock() if clock is None else clock
        self._records: list[NoSessionRecord] = list(_read_journal(path))
        self._dates = {record.trade_date for record in self._records}

    def __repr__(self) -> str:
        return f"NoSessionJournal(path={str(self._path)!r}, records={len(self._records)})"

    @property
    def path(self) -> Path:
        """The journal file, which may not exist yet."""
        return self._path

    @property
    def dates(self) -> frozenset[date]:
        """Every date observed to have no file."""
        return frozenset(self._dates)

    @property
    def records(self) -> tuple[NoSessionRecord, ...]:
        """Every observation, in the order it was written."""
        return tuple(self._records)

    def knows(self, day: date) -> bool:
        """Whether `day` has already been proved a non-session — the free half of resume."""
        return day in self._dates

    def record(self, day: date, *, era: BhavcopyEra, url: str, http_status: int) -> NoSessionRecord:
        """Append one observation and return it. Idempotent for a date already recorded."""
        existing = next((rec for rec in self._records if rec.trade_date == day), None)
        if existing is not None:
            return existing
        record = NoSessionRecord(
            trade_date=day,
            era=era.label,
            url=url,
            http_status=http_status,
            observed_at=self._clock.now(),
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")
        self._records.append(record)
        self._dates.add(day)
        return record


def _read_journal(path: Path) -> Iterable[NoSessionRecord]:
    """Parse an existing journal, failing loud on a line this schema does not recognise."""
    if not path.is_file():
        return ()
    records: list[NoSessionRecord] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                records.append(NoSessionRecord.model_validate(json.loads(line)))
            except (ValidationError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"{path}:{number} is not a no-session record ({exc}); the 404 evidence "
                    "journal is append-only and is not repaired automatically"
                ) from exc
    return records


# ── planning ─────────────────────────────────────────────────────────────────────────────────


def weekday_candidates(start: date, end: date) -> list[date]:
    """Every Monday-to-Friday date in the inclusive range.

    The calendar-free plan: a weekday is a date the exchange *might* have traded, and the archive
    is asked which. It over-requests by the ~15 holidays a year that fall on weekdays, and
    under-requests the handful of weekend Muhurat sessions — a bound stated in the plan's `note`
    rather than silently absorbed, and one that disappears the moment a calendar covering the range
    is available.
    """
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
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

    `basis` is `calendar` when a calendar covering the range narrowed the plan to its expected-data
    dates, and `weekday` when it could not and every weekday became a candidate. An operator needs
    to know which: the two differ in request count by ~6% and in what an absence *proves*.
    """

    start: date
    end: date
    dates: tuple[date, ...]
    basis: str
    note: str

    def __len__(self) -> int:
        return len(self.dates)

    @property
    def eras(self) -> tuple[BhavcopyEra, ...]:
        """The format eras this plan crosses."""
        return eras.eras_in(self.start, self.end)


def plan_sessions(
    start: date,
    end: date,
    *,
    calendar: TradingCalendar | None = None,
    limit: int | None = None,
) -> SessionPlan:
    """The candidate dates for a range, from the calendar when it covers it and weekdays when not.

    What it does: prefers `calendar.expected_data_dates`, which excludes declared holidays and
    *includes* weekend Muhurat sessions, and falls back to every weekday when the calendar refuses
    the range. `limit` samples the result evenly across the whole span (`backfill.sample_dates`), so
    a smoke run spans both eras rather than the first N days of 2006.
    What it assumes: a 404 is cheap and truthful. That is what makes the fallback safe — it widens
    the candidate set and proves each absence with a request, rather than narrowing it on a guess.
    What it never does: widen the calendar, or treat an uncovered range as holiday-free. The
    coverage error is caught here, at the driver boundary, logged with the gap named, and answered
    by asking the archive instead. `calendar.py` is unchanged and still refuses.
    """
    if calendar is None:
        dates = weekday_candidates(start, end)
        basis, note = "weekday", "no calendar supplied; every weekday is a candidate"
    else:
        try:
            dates = calendar.expected_data_dates(start, end)
            basis = "calendar"
            note = (
                f"calendar coverage {calendar.coverage_start.isoformat()}.."
                f"{calendar.coverage_end.isoformat()} spans the range; "
                "declared holidays are not requested and Muhurat weekends are"
            )
        except CalendarCoverageError as exc:
            dates = weekday_candidates(start, end)
            basis = "weekday"
            note = (
                f"calendar refused the range ({exc}); falling back to every weekday, so every "
                "absence is proved by a 404 rather than assumed. Weekend Muhurat sessions are "
                "NOT candidates under this basis"
            )
            _LOG.warning(
                "legacy_backfill.calendar_gap",
                source=NSE_BHAVCOPY,
                start=start.isoformat(),
                end=end.isoformat(),
                coverage_start=calendar.coverage_start.isoformat(),
                coverage_end=calendar.coverage_end.isoformat(),
                basis=basis,
                state="PLANNED",
            )
    chosen = tuple(sample_dates(dates, limit))
    _LOG.info(
        "legacy_backfill.planned",
        source=NSE_BHAVCOPY,
        start=start.isoformat(),
        end=end.isoformat(),
        basis=basis,
        candidates=len(chosen),
        eras=[era.label for era in eras.eras_in(start, end)],
        state="PLANNED",
    )
    return SessionPlan(start=start, end=end, dates=chosen, basis=basis, note=note)


def require_legacy_era(trade_date: date) -> BhavcopyEra:
    """The date's era, or a loud refusal if this driver does not serve it.

    E1 and E2 are this driver's whole subject; E3 is UDiFF and belongs to
    `dataplatform.ingest.backfill`. Refusing here rather than quietly building a legacy URL for a
    UDiFF date is what stops a mis-ranged run from spending 404s proving the obvious.
    """
    era = eras.era_for(trade_date)
    if era.source_id != bhavcopy_legacy.LEGACY_SOURCE_ID:
        raise ValueError(
            f"{trade_date.isoformat()} is era {era.label}, served by {era.source_id!r}; this "
            f"driver covers the pre-UDiFF archive only — use "
            f"`dataplatform.ingest.backfill --source {NSE_BHAVCOPY}`"
        )
    return era


def legacy_url(trade_date: date, *, register: SourceRegister) -> str:
    """The archive URL for one legacy-era session, from the register's verified template.

    Both E1 and E2 are served by the same `cm{DD}{MON}{YYYY}bhav.csv.zip` pattern — the eras differ
    in the file's *columns*, not in its address — so one filler covers the whole pre-UDiFF span.
    The template comes from `source_register.yaml` rather than being spelled here, so a URL change
    stays one edit in the register (C.1). A UDiFF-era date is a `ValueError`, not a wrong URL.
    """
    require_legacy_era(trade_date)
    source = next(
        (row for row in register.sources if row.id == bhavcopy_legacy.LEGACY_SOURCE_ID), None
    )
    if source is None:
        raise KeyError(f"source {bhavcopy_legacy.LEGACY_SOURCE_ID!r} is not in the source register")
    return (
        source.url_template.replace("{YYYY}", f"{trade_date:%Y}")
        .replace("{MON}", _MON[trade_date.month - 1])
        .replace("{DD}", f"{trade_date:%d}")
    )


def _filename(url: str) -> str:
    """The L0 filename for a URL — its last path segment, which the archive already dates."""
    return url.rsplit("/", 1)[-1]


# ── acquisition: candidate dates → L0 payloads and 404 evidence ──────────────────────────────


class SessionState(StrEnum):
    """What one candidate date's acquisition attempt came to."""

    #: The archive served a file and it is now in L0. One request spent.
    FETCHED = "FETCHED"

    #: A payload is already under this key. Zero requests — the resume path.
    ALREADY_IN_L0 = "ALREADY_IN_L0"

    #: The archive answered 404: the exchange was shut. One request spent, no error counted.
    HOLIDAY_OR_NO_SESSION = "HOLIDAY_OR_NO_SESSION"

    #: The journal already recorded a 404 for this date. Zero requests — the other resume path.
    KNOWN_NO_SESSION = "KNOWN_NO_SESSION"

    #: Something unexpected. Counted toward the hard stop; the date stays retryable.
    FAILED = "FAILED"

    @property
    def spent_a_request(self) -> bool:
        """Whether reaching this state cost one request against the host's budget."""
        return self in {SessionState.FETCHED, SessionState.HOLIDAY_OR_NO_SESSION}

    @property
    def is_retained(self) -> bool:
        """Whether the session's payload is in L0 after this outcome."""
        return self in {SessionState.FETCHED, SessionState.ALREADY_IN_L0}


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """One candidate date's acquisition result — the row a report and a test both read."""

    trade_date: date
    era: str
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
        """Sessions whose payload this run brought into L0."""
        return self._count(SessionState.FETCHED)

    @property
    def already_in_l0(self) -> int:
        """Sessions skipped because their payload was already stored."""
        return self._count(SessionState.ALREADY_IN_L0)

    @property
    def no_session(self) -> int:
        """Dates this run proved the exchange was shut on (a fresh 404)."""
        return self._count(SessionState.HOLIDAY_OR_NO_SESSION)

    @property
    def known_no_session(self) -> int:
        """Dates already known to be non-sessions, skipped without a request."""
        return self._count(SessionState.KNOWN_NO_SESSION)

    @property
    def failed(self) -> int:
        """Unexpected failures. A 404 is never one of these."""
        return self._count(SessionState.FAILED)

    @property
    def requests_spent(self) -> int:
        """Requests this run put to the host — the number the budget rule cares about."""
        return sum(1 for outcome in self.outcomes if outcome.state.spent_a_request)

    @property
    def no_session_dates(self) -> tuple[date, ...]:
        """Dates this run observed a 404 for, ascending."""
        return tuple(
            sorted(
                outcome.trade_date
                for outcome in self.outcomes
                if outcome.state is SessionState.HOLIDAY_OR_NO_SESSION
            )
        )

    def summary(self) -> str:
        """One line for the operator and the campaign log."""
        return (
            f"{self.requested} candidates: {self.fetched} fetched, "
            f"{self.already_in_l0} already in L0, {self.no_session} no-session (404), "
            f"{self.known_no_session} already known non-sessions, {self.failed} failed; "
            f"{self.requests_spent} requests spent"
            + (f" — STOPPED: {self.stop_reason}" if self.hard_stopped else "")
        )


class LegacyAcquisition:
    """Brings legacy bhavcopy payloads into L0, resumably, and records every 404 as evidence.

    What it does: for each candidate date, skips it for free if L0 already holds the payload or the
    journal already proves it a non-session; otherwise fetches it. A 404 becomes a journal entry, a
    403 spike ends the run, and anything else is a counted failure that leaves the date retryable.
    What it assumes: L0 immutability does the deduplication it advertises — mode `0o444`, writes
    that refuse to overwrite, and differing bytes at one key raising. Nothing here deletes or
    rewrites a payload, and there is no flag that would.
    What it never does: touch the calendar, touch the database, promote anything to L1, or lower
    the request spacing. Spacing and the 403 hard stop belong to the fetcher's crawl policy and are
    not weakened here.
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
        """Acquire every candidate date in order, stopping only for the reasons documented above."""
        report = AcquisitionReport(requested=len(sessions))
        error_streak = 0
        no_session_streak = 0
        total = len(sessions)

        for index, day in enumerate(sessions, start=1):
            if self._should_stop():
                report.hard_stopped = True
                report.stop_reason = "stop requested (SIGINT)"
                _LOG.warning(
                    "legacy_backfill.stopping",
                    source=NSE_BHAVCOPY,
                    date=day.isoformat(),
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

            no_session_streak = (
                no_session_streak + 1 if outcome.state is SessionState.HOLIDAY_OR_NO_SESSION else 0
            )

            if error_streak >= self._error_limit:
                report.hard_stopped = True
                report.stop_reason = f"{error_streak} consecutive unexpected failures"
                _LOG.critical(
                    "legacy_backfill.hard_stop",
                    source=NSE_BHAVCOPY,
                    date=day.isoformat(),
                    streak=error_streak,
                    reason=report.stop_reason,
                    state="HARD_STOPPED",
                )
                break
            if no_session_streak >= self._no_session_limit:
                report.hard_stopped = True
                report.stop_reason = (
                    f"{no_session_streak} consecutive 404s — longer than any NSE closure, so this "
                    "is a source change, not a holiday run"
                )
                _LOG.critical(
                    "legacy_backfill.hard_stop",
                    source=NSE_BHAVCOPY,
                    date=day.isoformat(),
                    streak=no_session_streak,
                    reason=report.stop_reason,
                    state="HARD_STOPPED",
                )
                break
            if self._fetcher.is_stopped(_host_of(self._register)):
                report.hard_stopped = True
                report.stop_reason = "403 spike: the fetcher has hard-stopped this host"
                break

        _LOG.info(
            "legacy_backfill.acquire_done",
            source=NSE_BHAVCOPY,
            requested=report.requested,
            fetched=report.fetched,
            already_in_l0=report.already_in_l0,
            no_session=report.no_session,
            known_no_session=report.known_no_session,
            failed=report.failed,
            requests_spent=report.requests_spent,
            hard_stopped=report.hard_stopped,
            state="STOPPED" if report.hard_stopped else "DONE",
        )
        return report

    def _acquire_one(self, day: date, *, index: int, total: int) -> SessionOutcome:
        """One candidate date. Never raises: every outcome is a `SessionOutcome`."""
        era = eras.era_for(day)
        try:
            require_legacy_era(day)
            url = legacy_url(day, register=self._register)
        except ValueError as exc:
            return SessionOutcome(
                trade_date=day,
                era=era.label,
                state=SessionState.FAILED,
                url="",
                filename="",
                error=str(exc),
            )
        name = _filename(url)

        if self._l0.exists(era.source_id, day, name):
            _LOG.info(
                "legacy_backfill.skip_stored",
                source=era.source_id,
                date=day.isoformat(),
                era=era.label,
                progress=f"{index}/{total}",
                state=SessionState.ALREADY_IN_L0.value,
            )
            return SessionOutcome(
                trade_date=day,
                era=era.label,
                state=SessionState.ALREADY_IN_L0,
                url=url,
                filename=name,
            )

        if self._journal.knows(day):
            _LOG.info(
                "legacy_backfill.skip_no_session",
                source=era.source_id,
                date=day.isoformat(),
                era=era.label,
                progress=f"{index}/{total}",
                state=SessionState.KNOWN_NO_SESSION.value,
            )
            return SessionOutcome(
                trade_date=day,
                era=era.label,
                state=SessionState.KNOWN_NO_SESSION,
                url=url,
                filename=name,
            )

        try:
            ref = self._fetcher.fetch(era.source_id, url, day, filename=name)
        except ForbiddenSpikeError as spike:
            # Not caught by the FetchHTTPError branch below and deliberately not turned into a
            # per-session failure: the host is refused for the life of the process (§8).
            _LOG.critical(
                "legacy_backfill.forbidden_spike",
                source=era.source_id,
                date=day.isoformat(),
                era=era.label,
                error=str(spike),
                state=SessionState.FAILED.value,
            )
            return SessionOutcome(
                trade_date=day,
                era=era.label,
                state=SessionState.FAILED,
                url=url,
                filename=name,
                error=f"ForbiddenSpikeError: {spike}",
            )
        except ForbiddenError as refused:
            _LOG.error(
                "legacy_backfill.forbidden",
                source=era.source_id,
                date=day.isoformat(),
                era=era.label,
                error=str(refused),
                state=SessionState.FAILED.value,
            )
            return SessionOutcome(
                trade_date=day,
                era=era.label,
                state=SessionState.FAILED,
                url=url,
                filename=name,
                error=f"ForbiddenError: {refused}",
            )
        except FetchHTTPError as http_error:
            if http_error.status_code != _HTTP_NOT_FOUND:
                return self._failed(day, era, url, name, f"HTTP {http_error.status_code}")
            record = self._journal.record(day, era=era, url=url, http_status=http_error.status_code)
            _LOG.info(
                "legacy_backfill.no_session",
                source=era.source_id,
                date=day.isoformat(),
                era=era.label,
                progress=f"{index}/{total}",
                http_status=record.http_status,
                evidence=record.evidence,
                journal=str(self._journal.path),
                state=SessionState.HOLIDAY_OR_NO_SESSION.value,
            )
            return SessionOutcome(
                trade_date=day,
                era=era.label,
                state=SessionState.HOLIDAY_OR_NO_SESSION,
                url=url,
                filename=name,
            )
        except Exception as exc:  # transport, L0, anything unforeseen — counted, never swallowed
            return self._failed(day, era, url, name, f"{type(exc).__name__}: {exc}")

        _LOG.info(
            "legacy_backfill.stored",
            source=era.source_id,
            date=day.isoformat(),
            era=era.label,
            progress=f"{index}/{total}",
            sha256=ref.sha256,
            size_bytes=ref.size_bytes,
            l0_key=ref.key,
            state=SessionState.FETCHED.value,
        )
        return SessionOutcome(
            trade_date=day,
            era=era.label,
            state=SessionState.FETCHED,
            url=url,
            filename=name,
            sha256=ref.sha256,
            size_bytes=ref.size_bytes,
        )

    def _failed(
        self, day: date, era: BhavcopyEra, url: str, name: str, message: str
    ) -> SessionOutcome:
        """Record one unexpected failure loudly. The date stays retryable on the next run."""
        _LOG.error(
            "legacy_backfill.session_failed",
            source=era.source_id,
            date=day.isoformat(),
            era=era.label,
            url=url,
            error=message,
            state=SessionState.FAILED.value,
        )
        return SessionOutcome(
            trade_date=day,
            era=era.label,
            state=SessionState.FAILED,
            url=url,
            filename=name,
            error=message,
        )


def _host_of(register: SourceRegister) -> str:
    """The archive host, from the register row rather than spelled here."""
    source = next(
        (row for row in register.sources if row.id == bhavcopy_legacy.LEGACY_SOURCE_ID), None
    )
    if source is None:  # pragma: no cover — the register is validated at load
        raise KeyError(f"source {bhavcopy_legacy.LEGACY_SOURCE_ID!r} is not in the source register")
    return source.host


# ── promotion: L0 payloads → L1, or → quarantine when the era has no identity ────────────────


@dataclass(frozen=True, slots=True)
class PromotedSession:
    """One session's promotion result. `price_rows` is always 0 for a pre-ISIN session."""

    trade_date: date
    era: str
    state: str
    price_rows: int = 0
    unresolved_rows: int = 0
    error: str | None = None


@dataclass(slots=True)
class PromotionReport:
    """What one promotion run landed, and the per-year identity bound it measured."""

    requested: int
    sessions: list[PromotedSession] = field(default_factory=list)

    def _in(self, state: str) -> int:
        return sum(1 for session in self.sessions if session.state == state)

    @property
    def published(self) -> int:
        """Sessions this run drove to `PUBLISHED`."""
        return self._in("PUBLISHED")

    @property
    def skipped(self) -> int:
        """Sessions already `PUBLISHED` before this run."""
        return self._in("SKIPPED_PUBLISHED")

    @property
    def missing(self) -> int:
        """Sessions with no L0 payload — acquisition's job, not promotion's."""
        return self._in("MISSING_IN_L0")

    @property
    def failed(self) -> int:
        """Sessions whose payload would not promote."""
        return self._in("FAILED")

    @property
    def price_rows(self) -> int:
        """Rows this run wrote to `prices_raw`."""
        return sum(session.price_rows for session in self.sessions)

    @property
    def unresolved_rows(self) -> int:
        """Rows this run wrote to `prices_raw_quarantine` for want of an ISIN."""
        return sum(session.unresolved_rows for session in self.sessions)

    def unresolved_by_year(self) -> dict[int, int]:
        """Unresolved rows per calendar year — the honest bound on how far back this reaches.

        Published rather than merely counted: "the platform has prices to 2006" and "the platform
        has 2.1 million rows from 2006-2011 it cannot key" are very different claims, and only the
        second one is true.
        """
        counter: Counter[int] = Counter()
        for session in self.sessions:
            counter[session.trade_date.year] += session.unresolved_rows
        return {year: counter[year] for year in sorted(counter) if counter[year]}

    def price_rows_by_year(self) -> dict[int, int]:
        """`prices_raw` rows per calendar year, for the coverage table."""
        counter: Counter[int] = Counter()
        for session in self.sessions:
            counter[session.trade_date.year] += session.price_rows
        return {year: counter[year] for year in sorted(counter) if counter[year]}

    def summary(self) -> str:
        """One line for the operator and the campaign log."""
        return (
            f"{self.requested} sessions: {self.published} published, {self.skipped} already "
            f"published, {self.missing} not in L0, {self.failed} failed; "
            f"{self.price_rows} price rows, {self.unresolved_rows} unresolved rows quarantined"
        )


class LegacyPromotion:
    """Turns stored legacy payloads into L1 — `prices_raw` where there is an ISIN, quarantine where
    there is not.

    What it does: reads each session's payload back out of L0 (re-checksummed on the way), parses
    it with the era's reader, writes it, and advances the era's `sync_state` row one state at a
    time, committing per session so a kill loses at most the session in flight.
    What it assumes: the caller injected the calendar its `SyncStateStore` should classify dates
    against, and that it owns each date's L1 partitions — a pre-ISIN session's quarantine partition
    is written whole, so a date that also has delivery rows quarantined must go through
    `write_prices_raw` instead.
    What it never does: fetch, invent an ISIN, publish a pre-ISIN session under `nse_bhavcopy`, or
    write an adjusted price. It also never calls `mark_gap` — a date with no file has its evidence
    in the 404 journal, and asking the calendar to classify a 2008 date it does not cover would
    raise, correctly.
    """

    def __init__(
        self,
        *,
        l0: L0Store,
        sync: SyncStateStore,
        commit: Callable[[], None],
        register: SourceRegister,
        data_root: Path | None = None,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self._l0 = l0
        self._sync = sync
        self._commit = commit
        self._register = register
        self._data_root = data_root
        self._should_stop = should_stop

    def promote(self, sessions: Sequence[date]) -> PromotionReport:
        """Promote every session that has a payload; report the ones that do not."""
        report = PromotionReport(requested=len(sessions))
        for index, day in enumerate(sessions, start=1):
            if self._should_stop():
                break
            report.sessions.append(self._promote_one(day, index=index, total=len(sessions)))
        _LOG.info(
            "legacy_backfill.promote_done",
            source=NSE_BHAVCOPY,
            requested=report.requested,
            published=report.published,
            skipped=report.skipped,
            missing=report.missing,
            failed=report.failed,
            price_rows=report.price_rows,
            unresolved_rows=report.unresolved_rows,
            state="DONE",
        )
        return report

    def _promote_one(self, day: date, *, index: int, total: int) -> PromotedSession:
        """One session. Never raises for a data error: the failure is filed in `sync_state`."""
        era = eras.era_for(day)
        state_source = NSE_BHAVCOPY if era.carries_isin else NSE_BHAVCOPY_PRE_ISIN
        name = _filename(legacy_url(day, register=self._register))

        if not self._l0.exists(era.source_id, day, name):
            _LOG.info(
                "legacy_backfill.promote_missing",
                source=state_source,
                date=day.isoformat(),
                era=era.label,
                progress=f"{index}/{total}",
                state="MISSING_IN_L0",
            )
            return PromotedSession(trade_date=day, era=era.label, state="MISSING_IN_L0")

        existing = self._sync.get(state_source, day)
        if existing is not None and existing.state is SyncState.PUBLISHED:
            _LOG.info(
                "legacy_backfill.promote_skip",
                source=state_source,
                date=day.isoformat(),
                era=era.label,
                progress=f"{index}/{total}",
                state="SKIPPED_PUBLISHED",
            )
            return PromotedSession(trade_date=day, era=era.label, state="SKIPPED_PUBLISHED")

        try:
            ref = self._l0.ref_for(era.source_id, day, name)
            self._sync.begin(state_source, day)
            self._sync.mark_fetched(state_source, day, checksum=ref.sha256, l0_path=ref.key)

            if era.carries_isin:
                parsed = bhavcopy.parse_l0_report(self._l0, ref)
                self._sync.mark_validated(state_source, day)
                write_prices_raw(
                    list(parsed.rows),
                    exchange=Exchange.NSE,
                    unidentified_rows=parsed.refused,
                    data_root=self._data_root,
                )
                price_rows, unresolved = len(parsed.rows), len(parsed.refused)
            else:
                rows = bhavcopy_legacy.parse_pre_isin_l0(self._l0, ref)
                self._sync.mark_validated(state_source, day)
                write_unidentified_quarantine(
                    rows, exchange=Exchange.NSE, trade_date=day, data_root=self._data_root
                )
                price_rows, unresolved = 0, len(rows)

            self._sync.mark_normalized(state_source, day)
            self._sync.mark_published(state_source, day)
            self._commit()
        except ParseError as exc:
            return self._fail(day, era, state_source, f"parse failed: {exc}")
        except Exception as exc:  # L0, L1 or DB — recorded loudly, the run continues
            return self._fail(day, era, state_source, f"{type(exc).__name__}: {exc}")

        _LOG.info(
            "legacy_backfill.promoted",
            source=state_source,
            date=day.isoformat(),
            era=era.label,
            progress=f"{index}/{total}",
            price_rows=price_rows,
            unresolved_rows=unresolved,
            state="PUBLISHED",
        )
        return PromotedSession(
            trade_date=day,
            era=era.label,
            state="PUBLISHED",
            price_rows=price_rows,
            unresolved_rows=unresolved,
        )

    def _fail(
        self, day: date, era: BhavcopyEra, state_source: str, message: str
    ) -> PromotedSession:
        """File one session's failure in `sync_state` so a broken source reaches the status API."""
        self._rollback()
        try:
            self._sync.begin(state_source, day)
            self._sync.mark_failed(state_source, day, message, retryable=True)
            self._commit()
        except Exception as exc:
            self._rollback()
            _LOG.error(
                "legacy_backfill.fail_record_failed",
                source=state_source,
                date=day.isoformat(),
                era=era.label,
                error=f"{type(exc).__name__}: {exc}",
                state="FAILED",
            )
        _LOG.error(
            "legacy_backfill.promote_failed",
            source=state_source,
            date=day.isoformat(),
            era=era.label,
            error=message,
            state="FAILED",
        )
        return PromotedSession(trade_date=day, era=era.label, state="FAILED", error=message)

    def _rollback(self) -> None:
        """Best-effort rollback between sessions, so a FAILED write lands on a clean transaction."""
        conn = getattr(self._sync, "_conn", None)
        rollback = getattr(conn, "rollback", None)
        if callable(rollback):
            rollback()


# ── reconciliation and reporting ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CalendarDiff:
    """Where the archive's own answers disagree with `nse_holidays.yaml`.

    Two directions, and they mean opposite things:

    * `undeclared_closures` — the calendar calls the date a session and the archive served no file.
      Either a real miss or a closure the calendar does not know about.
    * `unexpected_sessions` — the calendar calls the date closed and the archive served a file.
      The calendar is wrong, and it is suppressing a real session.

    `covered=False` means the calendar makes no claim about the range, so neither list means
    anything and both are empty. **Nothing here patches the calendar.** A correction is a separate,
    explicit commit citing the 404 evidence as provenance.
    """

    start: date
    end: date
    covered: bool
    undeclared_closures: tuple[date, ...] = ()
    unexpected_sessions: tuple[date, ...] = ()

    @property
    def agrees(self) -> bool:
        """Whether the archive and the calendar say the same thing across the range."""
        return self.covered and not self.undeclared_closures and not self.unexpected_sessions

    def summary(self) -> str:
        """One line an operator can act on."""
        if not self.covered:
            return (
                f"{self.start.isoformat()}..{self.end.isoformat()}: the calendar makes no claim "
                "about this range, so there is nothing to reconcile against yet"
            )
        return (
            f"{self.start.isoformat()}..{self.end.isoformat()}: "
            f"{len(self.undeclared_closures)} undeclared closures, "
            f"{len(self.unexpected_sessions)} sessions the calendar wrongly calls closed"
        )


def reconcile_calendar(
    *,
    start: date,
    end: date,
    served: Iterable[date],
    no_session: Iterable[date],
    calendar: TradingCalendar,
) -> CalendarDiff:
    """Compare the archive's observed answers against the checked-in calendar over a range.

    `served` is every date a payload exists for; `no_session` is every date the journal records a
    404 for. The calendar is injected, which is what lets this be tested against a fixture calendar
    covering 2006-2016 while production picks up the checked-in one.

    Returns an uncovered diff rather than raising when the calendar's span does not reach the
    range: "we cannot compare yet" is a legitimate answer and the campaign must not stop for it.
    """
    served_in_range = {day for day in served if start <= day <= end}
    closed_in_range = {day for day in no_session if start <= day <= end}
    try:
        expected = set(calendar.expected_data_dates(start, end))
    except CalendarCoverageError as exc:
        _LOG.warning(
            "legacy_backfill.reconcile_uncovered",
            source=NSE_BHAVCOPY,
            start=start.isoformat(),
            end=end.isoformat(),
            error=str(exc),
            state="UNCOVERED",
        )
        return CalendarDiff(start=start, end=end, covered=False)

    diff = CalendarDiff(
        start=start,
        end=end,
        covered=True,
        undeclared_closures=tuple(sorted(closed_in_range & expected)),
        unexpected_sessions=tuple(sorted(served_in_range - expected)),
    )
    _LOG.info(
        "legacy_backfill.reconciled",
        source=NSE_BHAVCOPY,
        start=start.isoformat(),
        end=end.isoformat(),
        undeclared_closures=len(diff.undeclared_closures),
        unexpected_sessions=len(diff.unexpected_sessions),
        state="RECONCILED",
    )
    return diff


def l1_row_counts(day: date, *, data_root: Path | None = None) -> tuple[int, int]:
    """`(prices_raw rows, prices_raw_quarantine rows)` for one session, read from parquet footers.

    Offline and cheap: `read_metadata` reads the footer, not the columns, so a decade of partitions
    costs a stat and a small read each rather than a full scan. A partition that does not exist is
    zero — which for `prices_raw` on a pre-ISIN date is the fact worth reporting, not a missing
    file.

    Read off disk rather than carried in a run's memory on purpose: the coverage artefact should
    describe what the lake *is*, so it can be regenerated after the fact and cannot drift from it.
    """
    counts: list[int] = []
    for dataset in (PRICES_RAW_DATASET, PRICES_RAW_QUARANTINE_DATASET):
        path = partition_path(Layer.L1, dataset, day, data_root=data_root)
        counts.append(pq.read_metadata(path).num_rows if path.is_file() else 0)
    priced, quarantined = counts
    return priced, quarantined


@dataclass(frozen=True, slots=True)
class _YearRow:
    year: int
    era_labels: str
    candidates: int
    in_l0: int
    no_session: int
    not_attempted: int
    promoted_rows: int = 0
    unresolved_rows: int = 0


def coverage_report(
    plan: SessionPlan,
    *,
    l0: L0Store,
    journal: NoSessionJournal,
    register: SourceRegister,
    calendar: TradingCalendar | None = None,
    data_root: Path | None = None,
) -> str:
    """The markdown coverage artefact: per-year sessions and rows, the 404 list, and the diff.

    Offline and read-only — it opens no socket and no database. Per year it reports candidates / in
    L0 / 404 / not yet attempted from L0 and the journal, and promoted / unresolved row counts from
    the L1 parquet footers (`l1_row_counts`). Reading the row counts off disk rather than from a
    promotion run's memory is what makes the artefact regenerable and unable to drift from the lake.

    The unresolved column is the number the whole wave rests on: it is the honest bound on how far
    back this platform can claim to reach, and a coverage table that showed only *sessions* would
    read as if 2006 were as usable as 2016.

    Raises `ValueError` for a plan reaching into the UDiFF era: this report is about the legacy
    archive, and silently omitting a year would make a coverage table that reads as complete.
    """
    rows: list[_YearRow] = []
    for year in sorted({day.year for day in plan.dates}):
        days = [day for day in plan.dates if day.year == year]
        in_l0 = 0
        no_session = 0
        promoted_rows = 0
        unresolved_rows = 0
        labels: list[str] = []
        for day in days:
            era = eras.era_for(day)
            if era.label not in labels:
                labels.append(era.label)
            name = _filename(legacy_url(day, register=register))
            if l0.exists(era.source_id, day, name):
                in_l0 += 1
            elif journal.knows(day):
                no_session += 1
            priced, quarantined = l1_row_counts(day, data_root=data_root)
            promoted_rows += priced
            unresolved_rows += quarantined
        rows.append(
            _YearRow(
                year=year,
                era_labels="/".join(labels),
                candidates=len(days),
                in_l0=in_l0,
                no_session=no_session,
                not_attempted=len(days) - in_l0 - no_session,
                promoted_rows=promoted_rows,
                unresolved_rows=unresolved_rows,
            )
        )

    lines = [
        f"### NSE legacy bhavcopy coverage — {plan.start.isoformat()}..{plan.end.isoformat()}",
        "",
        f"Plan basis: **{plan.basis}** ({plan.note})",
        "",
        "| year | era | candidates | in L0 | 404 (no session) | not attempted "
        "| promoted rows | unresolved rows |",
        "|---:|:---|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {row.year} | {row.era_labels} | {row.candidates} | {row.in_l0} | "
        f"{row.no_session} | {row.not_attempted} | {row.promoted_rows} | {row.unresolved_rows} |"
        for row in rows
    ]
    lines += [
        f"| **total** | | **{sum(r.candidates for r in rows)}** | "
        f"**{sum(r.in_l0 for r in rows)}** | **{sum(r.no_session for r in rows)}** | "
        f"**{sum(r.not_attempted for r in rows)}** | "
        f"**{sum(r.promoted_rows for r in rows)}** | "
        f"**{sum(r.unresolved_rows for r in rows)}** |",
        "",
        "#### 404 evidence (the exchange was shut)",
        "",
    ]
    observed = sorted(day for day in journal.dates if plan.start <= day <= plan.end)
    lines.append(
        ", ".join(day.isoformat() for day in observed) if observed else "_none observed yet_"
    )
    lines += ["", "#### Calendar diff vs nse_holidays.yaml", ""]
    # `served` here is every legacy payload L0 holds in the span, **not** the plan's dates. Those
    # are different sets and the difference is the whole point of the diff: a plan built from the
    # calendar can only contain dates the calendar already expects, so reconciling against it would
    # make `unexpected_sessions` structurally empty and the report would print "0 sessions the
    # calendar wrongly calls closed" while the lake held ten of them. Asking L0 what it actually
    # has is the only version of this question that can come back non-zero.
    stored = [
        ref.logical_date
        for ref in l0.iter_refs(bhavcopy_legacy.LEGACY_SOURCE_ID, start=plan.start, end=plan.end)
        if plan.start <= ref.logical_date <= plan.end
    ]
    diff = reconcile_calendar(
        start=plan.start,
        end=plan.end,
        served=stored,
        no_session=journal.dates,
        calendar=trading_calendar() if calendar is None else calendar,
    )
    lines.append(diff.summary())
    if diff.undeclared_closures:
        lines += [
            "",
            "Undeclared closures (calendar says session, archive served nothing): "
            + ", ".join(day.isoformat() for day in diff.undeclared_closures),
        ]
    if diff.unexpected_sessions:
        lines += [
            "",
            "Sessions the calendar wrongly calls closed (archive served a file): "
            + ", ".join(day.isoformat() for day in diff.unexpected_sessions),
        ]
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def _parse_dates(raw: str) -> list[date]:
    """`--dates 2011-06-21,2011-06-22` → the explicit candidate list, ascending and deduplicated."""
    return sorted({date.fromisoformat(token.strip()) for token in raw.split(",") if token.strip()})


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="legacy-backfill", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("plan", "print the candidate dates and their eras; opens nothing"),
        ("acquire", "fetch candidate sessions into L0, recording every 404 as evidence"),
        ("promote", "parse stored payloads into L1 prices_raw / prices_raw_quarantine"),
        ("report", "print the markdown coverage artefact from L0 and the journal"),
    ):
        child = sub.add_parser(name, help=help_text)
        child.add_argument("--from", dest="from_date", type=date.fromisoformat)
        child.add_argument("--to", dest="to_date", type=date.fromisoformat)
        child.add_argument(
            "--dates",
            type=_parse_dates,
            default=None,
            help="explicit comma-separated candidate dates, instead of a range plan",
        )
        child.add_argument(
            "--limit",
            type=int,
            default=None,
            help="sample this many candidates, spread evenly across the range",
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


def _plan_from_args(args: argparse.Namespace, *, calendar: TradingCalendar | None) -> SessionPlan:
    """The candidate plan for one CLI invocation, from `--dates` or from a range."""
    if args.dates:
        dates = list(args.dates)
        return SessionPlan(
            start=dates[0],
            end=dates[-1],
            dates=tuple(dates),
            basis="explicit",
            note=f"{len(dates)} dates named on the command line",
        )
    if args.from_date is None or args.to_date is None:
        raise ValueError("give either --dates or both --from and --to")
    return plan_sessions(args.from_date, args.to_date, calendar=calendar, limit=args.limit)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Exit 0 on a clean run, 2 on a bad invocation, 3 on a hard stop."""
    args = _build_parser().parse_args(argv)
    settings: Settings = get_settings()
    clock: Clock = SystemClock()
    register = load_register()
    calendar = None if args.no_calendar else trading_calendar()

    try:
        plan = _plan_from_args(args, calendar=calendar)
    except ValueError as exc:
        print(f"cannot plan: {exc}", file=sys.stderr)
        return 2

    l0 = L0Store(clock=clock, data_root=settings.data_root)
    journal = NoSessionJournal(journal_path_for(settings.data_root), clock=clock)

    if args.command == "plan":
        for day in plan.dates:
            era = eras.era_for(day)
            try:
                target = legacy_url(day, register=register)
            except ValueError as exc:
                target = f"(not this driver: {exc})"
            print(f"{day.isoformat()}\t{era.label}\t{target}")
        print(f"\n{len(plan)} candidates, basis={plan.basis} ({plan.note})")
        return 0

    if args.command == "report":
        print(
            coverage_report(
                plan,
                l0=l0,
                journal=journal,
                register=register,
                calendar=calendar,
                data_root=settings.data_root,
            )
        )
        return 0

    if args.command == "acquire":
        # `leased_fetcher`, not `build_fetcher`: the archive host carries prices, delivery,
        # corporate actions and fundamentals, and this campaign holds its budget for hours. The
        # lease is the enforced form of the one-budget-per-host rule a Phase 2 run must not
        # break, and a second driver on this box refuses to start rather than halving the spacing.
        host = _host_of(register)
        with leased_fetcher(
            [host],
            clock=clock,
            command=f"legacy_backfill acquire {plan.start.isoformat()}..{plan.end.isoformat()}",
            settings=settings,
            register=register,
        ) as fetcher:
            report = LegacyAcquisition(
                fetcher=fetcher,
                l0=l0,
                journal=journal,
                register=register,
                error_streak_limit=args.error_streak_limit,
                no_session_streak_limit=args.no_session_streak_limit,
            ).run(plan.dates)
        print(report.summary())
        return 3 if report.hard_stopped else 0

    with connection(settings) as conn:
        promotion = LegacyPromotion(
            l0=l0,
            sync=SyncStateStore(conn, clock=clock, calendar=trading_calendar()),
            commit=conn.commit,
            register=register,
            data_root=settings.data_root,
        )
        promoted = promotion.promote(plan.dates)
    print(promoted.summary())
    for year, count in promoted.unresolved_by_year().items():
        print(f"  {year}: {count} rows with no ISIN (quarantined, not promotable)")
    return 0 if promoted.failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
