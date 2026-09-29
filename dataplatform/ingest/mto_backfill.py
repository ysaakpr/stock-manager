"""W3: the NSE MTO delivery deep backfill — the ISIN era's delivery figures, 2011-06-22 onward.

W1 put 2011-06-22 → 2016-09-01 prices into `prices_raw`, and every one of those 1,300-odd sessions
still carries `deliv_qty = NULL`: the lake's MTO payloads begin 2016-09-02, where the platform's
first price history did. A delivery-weighted signal read across 2011-2016 is therefore reading a
column that is structurally empty for a third of its span, and nothing downstream can tell that
apart from "no delivery happened". This driver fills it from the one archive that serves it,
`MTO_DDMMYYYY.DAT`, which reaches back to 2002.

It is a separate driver from `dataplatform.ingest.backfill` for the same reasons W1's is:

**1. A 404 is data, not a failure.** Every candidate is a session the calendar vouches for (the
calendar reaches 2006 and was reconciled against W1's 404 evidence with zero disagreement), so an
MTO 404 on one means the exchange traded and published no delivery report. That is recorded in
this campaign's own append-only journal as `NO_MTO_PUBLISHED`, never counted as an error, and never
retried. `sync_state` would file it as `FAILED` and a later run would pay for it again.

**2. Acquisition and promotion are separate steps.** Acquisition needs the network and a request
budget but no database; promotion needs Postgres (the identity master, `sync_state`) and no
network. Splitting them is what lets the budgeted half run once, uninterrupted, and the offline
half be re-run as often as a parser fix demands, at zero requests.

**3. The lake root is asserted before request #1.** Settings anchor a relative `data_root` at the
*checkout* root, and a worktree is a checkout: a campaign launched from one silently builds a second
lake — and takes its host lease there, where no other driver can see it. That has happened twice.
`acquire` and `promote` therefore require `--expect-l0-root` and refuse to open a socket or a
partition unless the resolved L0 root is exactly it.

**4. The quarantine partition is written whole, so promotion refuses to clobber it.** A session's
`prices_raw_quarantine` partition holds the bhavcopy's placeholder-ISIN rows (W1) *and* the
delivery rows that could not be placed, and `write_prices_raw` writes it in one piece from what it
is handed. The write step here is `backfill.SOURCE_SETS[NSE_DELIVERY]`'s, which passes both sets
in one call; `guard_quarantine` additionally proves, before each write, that every row already in
the partition that is not a replaceable delivery row will be re-derived by it. A row the write
would not reproduce — another exchange's, or one from a writer this driver does not know — stops
that session loudly instead of disappearing.

Resume is L0 plus the journal for acquisition (zero requests for a session already stored or
already proved absent), and `sync_state` for promotion (a `PUBLISHED` session is skipped).

Operator flow (the whole campaign is ~1,300 sessions at >=2.5 s, about an hour):

    RANGE="--from 2011-06-22 --to 2016-09-01"
    ROOT="--expect-l0-root /abs/path/to/data/L0"
    uv run python -m dataplatform.ingest.mto_backfill plan    $RANGE
    uv run python -m dataplatform.ingest.mto_backfill acquire $RANGE $ROOT
    uv run python -m dataplatform.ingest.mto_backfill promote $RANGE $ROOT
    uv run python -m dataplatform.ingest.mto_backfill report  $RANGE
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Final

import pyarrow.parquet as pq

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.identity.master import Exchange, IdentityMaster, IdentityStore
from dataplatform.ingest.backfill import (
    NSE_DELIVERY,
    SOURCE_SETS,
    WriteContext,
    _bhavcopy_request,
    sample_dates,
)
from dataplatform.ingest.calendar import TradingCalendar, trading_calendar
from dataplatform.ingest.fetcher import (
    Fetcher,
    FetchHTTPError,
    ForbiddenError,
    ForbiddenSpikeError,
    leased_fetcher,
)
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.no_session_journal import NoSessionJournal, journal_path
from dataplatform.ingest.nse import bhavcopy, delivery, eras, mto
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import PricesRawWriteReport
from dataplatform.store.paths import Layer, partition_path
from dataplatform.store.schemas import (
    PRICES_RAW_DATASET,
    PRICES_RAW_QUARANTINE_DATASET,
    PRICES_RAW_QUARANTINE_SCHEMA,
    PriceQuarantineReason,
)

__all__ = [
    "CAMPAIGN_FLOOR",
    "DEFAULT_ERROR_STREAK_LIMIT",
    "DEFAULT_NO_SESSION_STREAK_LIMIT",
    "EVIDENCE_NO_MTO",
    "JOURNAL_FILENAME",
    "MIN_SPACING_SECONDS",
    "MTO_ERA_END",
    "AcquisitionReport",
    "LakeRootMismatchError",
    "MtoAcquisition",
    "MtoPromotion",
    "PromotedSession",
    "PromotionReport",
    "QuarantineClobberError",
    "SessionOutcome",
    "SessionPlan",
    "SessionState",
    "coverage_report",
    "guard_quarantine",
    "journal_path_for",
    "main",
    "mto_url",
    "plan_sessions",
    "price_sessions",
    "require_l0_root",
]

_LOG = get_logger(__name__)

#: The first session this campaign promotes. Below it the bhavcopy has no ISIN column (era E1), so
#: there is no `prices_raw` row for a delivery figure to join onto — fetching MTO there buys a
#: payload with nothing to attach it to. Extending below is W4 identity work, not a range flag.
CAMPAIGN_FLOOR: Final = eras.ISIN_ERA_START

#: The first session MTO is *not* the delivery source. From here `sec_bhavdata_full` is, and the
#: shallow backfill already owns it; this driver refusing the range keeps the splice in one place.
MTO_ERA_END: Final = delivery.SEC_BHAVDATA_ERA_START

#: Consecutive unexpected failures before the run stops. 404s are expected and excluded.
DEFAULT_ERROR_STREAK_LIMIT: Final = 5

#: Consecutive 404s before the run stops. Lower than W1's twenty on purpose: every candidate here
#: is a session the calendar already vouched for, so even one absence is notable, and five in a row
#: is an archive that moved rather than a run of unpublished reports.
DEFAULT_NO_SESSION_STREAK_LIMIT: Final = 5

#: The request spacing floor the owner signed off on. The fetcher's policy is the enforcement; this
#: is the refusal to start if configuration has lowered it.
MIN_SPACING_SECONDS: Final = 2.5

#: What an MTO 404 on a calendar session means. Not `HOLIDAY_OR_NO_SESSION`: the calendar says the
#: exchange traded, so the absence is a report that was never published, and conflating the two
#: would put phantom holidays into the evidence the calendar is reconciled against.
EVIDENCE_NO_MTO: Final = "NO_MTO_PUBLISHED"

#: One journal per archive (`no_session_journal`), so no campaign consumes another's evidence.
JOURNAL_FILENAME: Final = "nse_mto_no_session.jsonl"

_HTTP_NOT_FOUND: Final = 404

#: Quarantine reasons a delivery write legitimately replaces: they are re-derived from the MTO
#: payload and the identity master on every write, so a re-promotion supersedes them by design.
_DELIVERY_REASONS: Final = frozenset(
    {PriceQuarantineReason.SYMBOL_UNRESOLVED, PriceQuarantineReason.NO_MATCHING_PRICE}
)


def journal_path_for(data_root: Path) -> Path:
    """Where this campaign's 404 evidence journal lives for a lake root."""
    return journal_path(data_root, filename=JOURNAL_FILENAME)


# ── the lake-root guard ──────────────────────────────────────────────────────────────────────


class LakeRootMismatchError(RuntimeError):
    """The L0 root this run resolved to is not the one the operator declared."""


def require_l0_root(l0: L0Store, expected: Path | None) -> Path:
    """Return the resolved L0 root, or refuse the run if it is not `expected`.

    What it does: resolves both paths (symlinks included) and compares them exactly.
    What it assumes: `expected` is the authoritative lake, stated by the operator rather than
    inferred — inferring it is precisely how a worktree ended up with its own lake.
    What it never does: create either directory, or accept a missing `expected`. A run that writes
    to the lake must say which lake; there is no default, because the default is the bug.
    """
    if expected is None:
        raise LakeRootMismatchError(
            "--expect-l0-root is required: state the absolute L0 root this run must write to"
        )
    resolved = l0.root.resolve()
    if resolved != expected.resolve():
        raise LakeRootMismatchError(
            f"L0 resolved to {resolved} but the operator declared {expected.resolve()}; a relative "
            "data_root is anchored at the checkout, and a worktree is a checkout. Set DATA_ROOT "
            "to the authoritative lake and re-run"
        )
    return resolved


# ── planning ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionPlan:
    """The candidate sessions one run will consider, and where they came from."""

    start: date
    end: date
    dates: tuple[date, ...]
    basis: str
    note: str

    def __len__(self) -> int:
        return len(self.dates)


def _require_range(start: date, end: date) -> None:
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    if start < CAMPAIGN_FLOOR:
        raise ValueError(
            f"{start.isoformat()} is before {CAMPAIGN_FLOOR.isoformat()}, where the bhavcopy gains "
            "its ISIN column; below it there is no prices_raw row for delivery to join onto"
        )
    if end >= MTO_ERA_END:
        raise ValueError(
            f"{end.isoformat()} is in the sec_bhavdata_full era (from {MTO_ERA_END.isoformat()}); "
            "that delivery source belongs to `dataplatform.ingest.backfill --source nse_delivery`"
        )


def price_sessions(*, data_root: Path | None) -> frozenset[date]:
    """Every session with a `prices_raw` partition: the sessions a delivery figure can join."""
    root = partition_path(Layer.L1, PRICES_RAW_DATASET, CAMPAIGN_FLOOR, data_root=data_root)
    dataset = root.parent.parent
    if not dataset.is_dir():
        return frozenset()
    return frozenset(
        date.fromisoformat(child.name.removeprefix("date="))
        for child in dataset.glob("date=*")
        if (child / root.name).is_file()
    )


def plan_sessions(
    start: date,
    end: date,
    *,
    calendar: TradingCalendar,
    limit: int | None = None,
    priced: Iterable[date] = (),
) -> SessionPlan:
    """The candidate sessions for a range: the calendar's expected-data dates plus every session
    `priced` names (a `prices_raw` partition exists), optionally sampled.

    What it does: refuses a range outside `[CAMPAIGN_FLOOR, MTO_ERA_END)`, asks the calendar, and
    unions in the in-range `priced` sessions. The union is not a widening: a W1 price partition is
    the exchange's own bhavcopy for that day, and the seven special Saturday sessions of 2012-2015
    (2012-01-07 … 2015-02-28) are sessions the calendar does not list and W1 holds prices for — a
    calendar-only plan would leave them without delivery for no reason but the plan.
    What it assumes: the calendar covers the range. Unlike W1 there is no weekday fallback — W1's
    campaign *was* the calendar's evidence, and a range the calendar cannot vouch for is one this
    driver has no business spending requests on. `CalendarCoverageError` propagates.
    What it never does: invent a session, or request a date neither the calendar nor L1 vouches for.
    """
    _require_range(start, end)
    expected = calendar.expected_data_dates(start, end)
    extra = sorted({day for day in priced if start <= day <= end} - set(expected))
    dates = sorted({*expected, *extra})
    chosen = tuple(sample_dates(dates, limit))
    note = (
        f"calendar coverage {calendar.coverage_start.isoformat()}.."
        f"{calendar.coverage_end.isoformat()}; declared holidays are not requested; "
        f"{len(extra)} priced session(s) outside the calendar added"
        + (f" ({', '.join(d.isoformat() for d in extra)})" if extra else "")
    )
    _LOG.info(
        "mto_backfill.planned",
        source=mto.MTO_SOURCE_ID,
        start=start.isoformat(),
        end=end.isoformat(),
        candidates=len(chosen),
        priced_outside_calendar=len(extra),
        state="PLANNED",
    )
    return SessionPlan(start=start, end=end, dates=chosen, basis="calendar+prices_raw", note=note)


def _source_row(register: SourceRegister) -> tuple[str, str]:
    """`(host, url_template)` for the MTO archive, from the register rather than spelled here."""
    row = next((s for s in register.sources if s.id == mto.MTO_SOURCE_ID), None)
    if row is None:
        raise KeyError(f"source {mto.MTO_SOURCE_ID!r} is not in the source register")
    return row.host, row.url_template


def mto_url(trade_date: date, *, register: SourceRegister) -> str:
    """The archive URL for one session's MTO file, from the register's verified template."""
    _, template = _source_row(register)
    return template.replace("{DDMMYYYY}", f"{trade_date:%d%m%Y}")


def _filename(url: str) -> str:
    return url.rsplit("/", 1)[-1]


# ── acquisition ──────────────────────────────────────────────────────────────────────────────


class SessionState(StrEnum):
    """What one candidate session's acquisition attempt came to."""

    FETCHED = "FETCHED"
    ALREADY_IN_L0 = "ALREADY_IN_L0"
    #: The archive answered 404 for a session: no MTO report was published. Evidence, not error.
    NO_MTO = "NO_MTO"
    KNOWN_NO_MTO = "KNOWN_NO_MTO"
    #: The archive served bytes that state a different session. Stored (L0 keeps what it was
    #: served), counted as a failure, and never promoted: the parser's date check refuses it.
    STALE_PAYLOAD = "STALE_PAYLOAD"
    FAILED = "FAILED"

    @property
    def spent_a_request(self) -> bool:
        """Whether reaching this state cost one request against the host's budget."""
        return self in {SessionState.FETCHED, SessionState.NO_MTO, SessionState.STALE_PAYLOAD}

    @property
    def is_failure(self) -> bool:
        """Whether this outcome counts toward the error hard stop."""
        return self in {SessionState.FAILED, SessionState.STALE_PAYLOAD}


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """One candidate session's acquisition result."""

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

    def count(self, state: SessionState) -> int:
        """Outcomes in `state`."""
        return sum(1 for outcome in self.outcomes if outcome.state is state)

    @property
    def requests_spent(self) -> int:
        """Requests this run put to the host — the number the budget rule cares about."""
        return sum(1 for outcome in self.outcomes if outcome.state.spent_a_request)

    def summary(self) -> str:
        """One line for the operator and the campaign log."""
        parts = ", ".join(f"{self.count(s)} {s.value.lower()}" for s in SessionState)
        return f"{self.requested} candidates: {parts}; {self.requests_spent} requests spent" + (
            f" — STOPPED: {self.stop_reason}" if self.hard_stopped else ""
        )


class MtoAcquisition:
    """Brings MTO payloads into L0, resumably, and records every 404 as evidence.

    What it does: for each candidate session, skips it for free if L0 already holds the payload or
    the journal already proves it absent; otherwise fetches it, then checks the date the bytes state
    about themselves against the date asked for. Stops on five consecutive failures, five
    consecutive 404s, a 403 spike, or SIGINT.
    What it assumes: L0 immutability does the deduplication — nothing here deletes or rewrites a
    payload. Spacing and the 403 rule belong to the fetcher's crawl policy and are not weakened.
    What it never does: touch the database, the calendar, or L1.
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
        self._host, _ = _source_row(register)
        self._error_limit = error_streak_limit
        self._no_session_limit = no_session_streak_limit
        self._should_stop = should_stop

    def run(self, sessions: Sequence[date]) -> AcquisitionReport:
        """Acquire every candidate session in order, stopping only for the documented reasons."""
        _LOG.info(
            "mto_backfill.lake",
            source=mto.MTO_SOURCE_ID,
            l0_root=str(self._l0.root),
            journal=str(self._journal.path),
            journal_records=len(self._journal.records),
            sessions=len(sessions),
            state="PLANNED",
        )
        report = AcquisitionReport(requested=len(sessions))
        error_streak = 0
        no_mto_streak = 0
        total = len(sessions)
        for index, day in enumerate(sessions, start=1):
            if self._should_stop():
                report.hard_stopped = True
                report.stop_reason = "stop requested (SIGINT)"
                break
            outcome = self._acquire_one(day, index=index, total=total)
            report.outcomes.append(outcome)

            if outcome.state.is_failure:
                error_streak += 1
            elif outcome.state.spent_a_request:
                error_streak = 0
            no_mto_streak = no_mto_streak + 1 if outcome.state is SessionState.NO_MTO else 0

            reason: str | None = None
            if error_streak >= self._error_limit:
                reason = f"{error_streak} consecutive unexpected failures"
            elif no_mto_streak >= self._no_session_limit:
                reason = (
                    f"{no_mto_streak} consecutive 404s on calendar sessions — an archive change, "
                    "not a run of unpublished reports"
                )
            elif self._fetcher.is_stopped(self._host):
                reason = "403 spike: the fetcher has hard-stopped this host"
            if reason is not None:
                report.hard_stopped = True
                report.stop_reason = reason
                _LOG.critical(
                    "mto_backfill.hard_stop",
                    source=mto.MTO_SOURCE_ID,
                    date=day.isoformat(),
                    reason=reason,
                    state="HARD_STOPPED",
                )
                break

        _LOG.info(
            "mto_backfill.acquire_done",
            source=mto.MTO_SOURCE_ID,
            requested=report.requested,
            requests_spent=report.requests_spent,
            **{s.value.lower(): report.count(s) for s in SessionState},
            hard_stopped=report.hard_stopped,
            state="STOPPED" if report.hard_stopped else "DONE",
        )
        return report

    def _acquire_one(self, day: date, *, index: int, total: int) -> SessionOutcome:
        """One candidate session. Never raises: every outcome is a `SessionOutcome`."""
        url = mto_url(day, register=self._register)
        name = _filename(url)
        progress = f"{index}/{total}"

        if self._l0.exists(mto.MTO_SOURCE_ID, day, name):
            self._log("skip_stored", day, progress, SessionState.ALREADY_IN_L0)
            return SessionOutcome(day, SessionState.ALREADY_IN_L0, url, name)
        if self._journal.knows(day):
            self._log("skip_no_mto", day, progress, SessionState.KNOWN_NO_MTO)
            return SessionOutcome(day, SessionState.KNOWN_NO_MTO, url, name)

        try:
            ref = self._fetcher.fetch(mto.MTO_SOURCE_ID, url, day, filename=name)
        except ForbiddenSpikeError as spike:
            return self._failed(day, url, name, f"ForbiddenSpikeError: {spike}")
        except ForbiddenError as refused:
            return self._failed(day, url, name, f"ForbiddenError: {refused}")
        except FetchHTTPError as http_error:
            if http_error.status_code != _HTTP_NOT_FOUND:
                return self._failed(day, url, name, f"HTTP {http_error.status_code}")
            self._journal.record(day, url=url, http_status=http_error.status_code)
            self._log("no_mto", day, progress, SessionState.NO_MTO, evidence=EVIDENCE_NO_MTO)
            return SessionOutcome(day, SessionState.NO_MTO, url, name)
        except Exception as exc:  # transport, L0, anything unforeseen — counted, never swallowed
            return self._failed(day, url, name, f"{type(exc).__name__}: {exc}")

        stated: date | None = None
        problem: str | None = None
        try:
            stated = mto.stated_date(self._l0.get(ref), filename=name)
        except ParseError as exc:
            problem = str(exc)
        if stated is not None and stated != day:
            problem = f"file states {stated.isoformat()}"
        if problem is not None:
            _LOG.error(
                "mto_backfill.stale_payload",
                source=mto.MTO_SOURCE_ID,
                date=day.isoformat(),
                stated=None if stated is None else stated.isoformat(),
                error=problem,
                l0_key=ref.key,
                state=SessionState.STALE_PAYLOAD.value,
            )
            return SessionOutcome(
                day, SessionState.STALE_PAYLOAD, url, name, ref.sha256, ref.size_bytes, problem
            )

        self._log(
            "stored",
            day,
            progress,
            SessionState.FETCHED,
            sha256=ref.sha256,
            size_bytes=ref.size_bytes,
            l0_key=ref.key,
        )
        return SessionOutcome(day, SessionState.FETCHED, url, name, ref.sha256, ref.size_bytes)

    def _log(
        self, event: str, day: date, progress: str, state: SessionState, **extra: object
    ) -> None:
        _LOG.info(
            f"mto_backfill.{event}",
            source=mto.MTO_SOURCE_ID,
            date=day.isoformat(),
            progress=progress,
            state=state.value,
            **extra,
        )

    def _failed(self, day: date, url: str, name: str, message: str) -> SessionOutcome:
        _LOG.error(
            "mto_backfill.session_failed",
            source=mto.MTO_SOURCE_ID,
            date=day.isoformat(),
            url=url,
            error=message,
            state=SessionState.FAILED.value,
        )
        return SessionOutcome(day, SessionState.FAILED, url, name, error=message)


# ── the quarantine clobber guard ─────────────────────────────────────────────────────────────


class QuarantineClobberError(RuntimeError):
    """A write would drop quarantine rows it does not re-derive."""


def guard_quarantine(
    day: date,
    *,
    l0: L0Store,
    register: SourceRegister,
    data_root: Path | None,
) -> int:
    """Prove the session's delivery write re-derives every quarantine row it is about to replace.

    Why: `write_prices_raw` writes `prices_raw_quarantine/date=…` whole, from exactly the rows it is
    handed. The NSE delivery write hands it the bhavcopy's placeholder-ISIN refusals and the
    delivery rows it cannot place — so every *other* row in the partition would silently vanish.
    What it does: reads the existing partition, discards the NSE delivery rows (they are re-derived
    from the MTO payload on every write, by design), and checks each remaining row is among the
    refusals the stored bhavcopy re-parses to. Returns how many rows it proved will survive.
    What it never does: write. It raises `QuarantineClobberError` naming the rows at risk, and the
    session fails loudly rather than trading a delivery figure for a lost enumeration.
    """
    path = partition_path(Layer.L1, PRICES_RAW_QUARANTINE_DATASET, day, data_root=data_root)
    if not path.is_file():
        return 0
    existing = pq.read_table(path, schema=PRICES_RAW_QUARANTINE_SCHEMA).to_pylist()
    kept = [
        row
        for row in existing
        if not (row["exchange"] == Exchange.NSE.value and row["reason"] in _DELIVERY_REASONS)
    ]
    if not kept:
        return 0
    stored = _bhavcopy_request(day, register)
    parsed = bhavcopy.parse_l0_report(l0, l0.ref_for(stored.fetch_source, day, stored.filename))
    # The reason is part of the key: the delivery write stamps every bhavcopy refusal
    # `isin_not_published`, so a row filed under any other reason (W1's pre-ISIN
    # `isin_column_absent`, say) would come back relabelled — a changed fact, not a surviving one.
    rederived = Counter(
        (
            Exchange.NSE.value,
            row.symbol,
            row.series,
            row.stated_isin or None,
            PriceQuarantineReason.ISIN_NOT_PUBLISHED,
        )
        for row in parsed.refused
    )
    at_risk = Counter(
        (
            str(row["exchange"]),
            str(row["symbol"]),
            str(row["series"]),
            row["isin"],
            str(row["reason"]),
        )
        for row in kept
    )
    missing = at_risk - rederived
    if missing:
        sample = ", ".join(
            f"{ex}:{sym}/{ser} ({why})" for ex, sym, ser, _, why in sorted(missing, key=str)[:5]
        )
        raise QuarantineClobberError(
            f"{day.isoformat()}: the delivery write would drop {sum(missing.values())} quarantine "
            f"row(s) it does not re-derive ({sample}); the partition is written whole"
        )
    return sum(at_risk.values())


# ── promotion ────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PromotedSession:
    """One session's promotion result."""

    trade_date: date
    state: str
    delivery_rows: int = 0
    delivery_joined: int = 0
    delivery_unresolved: int = 0
    delivery_orphaned: int = 0
    error: str | None = None


@dataclass(slots=True)
class PromotionReport:
    """What one promotion run landed. Delivery counts reconcile per session and in total."""

    requested: int
    sessions: list[PromotedSession] = field(default_factory=list)

    def count(self, state: str) -> int:
        """Sessions that ended in `state`."""
        return sum(1 for session in self.sessions if session.state == state)

    def total(self, attribute: str) -> int:
        """A delivery count summed across sessions."""
        return sum(int(getattr(session, attribute)) for session in self.sessions)

    def summary(self) -> str:
        """One line for the operator and the campaign log."""
        rows = self.total("delivery_rows")
        joined = self.total("delivery_joined")
        rate = f"{joined / rows:.1%}" if rows else "n/a"
        return (
            f"{self.requested} sessions: {self.count('PUBLISHED')} published, "
            f"{self.count('SKIPPED_PUBLISHED')} already published, "
            f"{self.count('MISSING_IN_L0')} not in L0, {self.count('FAILED')} failed; "
            f"delivery rows {rows}: {joined} joined ({rate}), "
            f"{self.total('delivery_unresolved')} unresolved, "
            f"{self.total('delivery_orphaned')} orphaned (quarantined, never dropped)"
        )


class MtoPromotion:
    """Turns stored MTO payloads into delivery columns on `prices_raw`, one session at a time.

    What it does: for each session with an MTO payload, runs `guard_quarantine`, then the delivery
    source set's own parse and write (`backfill.SOURCE_SETS[NSE_DELIVERY]` — the same code the
    shallow backfill and `price_rebuild` use, so the era splice cannot drift), driving the
    `nse_delivery` `sync_state` row to `PUBLISHED` and committing per session.
    What it assumes: the session's bhavcopy is already in L0 (W1). The write rebuilds the
    `prices_raw` partition from it; a session whose bhavcopy is missing fails, it is never written
    with prices absent.
    What it never does: fetch, resolve a symbol other than through the D2 master, or drop a row.
    """

    def __init__(
        self,
        *,
        l0: L0Store,
        sync: SyncStateStore,
        commit: Callable[[], None],
        register: SourceRegister,
        master: IdentityMaster,
        data_root: Path | None = None,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self._l0 = l0
        self._sync = sync
        self._commit = commit
        self._register = register
        self._data_root = data_root
        self._set = SOURCE_SETS[NSE_DELIVERY]
        self._ctx = WriteContext(l0=l0, data_root=data_root, master=master, register=register)
        self._should_stop = should_stop

    def promote(self, sessions: Sequence[date]) -> PromotionReport:
        """Promote every session that has a payload; report the ones that do not."""
        report = PromotionReport(requested=len(sessions))
        for index, day in enumerate(sessions, start=1):
            if self._should_stop():
                break
            report.sessions.append(self._promote_one(day, progress=f"{index}/{len(sessions)}"))
        _LOG.info(
            "mto_backfill.promote_done",
            source=NSE_DELIVERY,
            requested=report.requested,
            published=report.count("PUBLISHED"),
            failed=report.count("FAILED"),
            delivery_rows=report.total("delivery_rows"),
            delivery_joined=report.total("delivery_joined"),
            state="DONE",
        )
        return report

    def _promote_one(self, day: date, *, progress: str) -> PromotedSession:
        """One session. Never raises for a data error: the failure is filed in `sync_state`."""
        request = self._set.build_request(day, self._register)
        if request.fetch_source != mto.MTO_SOURCE_ID:  # pragma: no cover — _require_range holds
            raise ValueError(f"{day} is not an MTO-era session")
        if not self._l0.exists(request.fetch_source, day, request.filename):
            _LOG.info(
                "mto_backfill.promote_missing",
                source=NSE_DELIVERY,
                date=day.isoformat(),
                progress=progress,
                state="MISSING_IN_L0",
            )
            return PromotedSession(trade_date=day, state="MISSING_IN_L0")
        existing = self._sync.get(NSE_DELIVERY, day)
        if existing is not None and existing.state is SyncState.PUBLISHED:
            return PromotedSession(trade_date=day, state="SKIPPED_PUBLISHED")

        try:
            ref = self._l0.ref_for(request.fetch_source, day, request.filename)
            self._sync.begin(NSE_DELIVERY, day)
            self._sync.mark_fetched(NSE_DELIVERY, day, checksum=ref.sha256, l0_path=ref.key)
            rows = self._set.parse(self._l0, ref)
            self._sync.mark_validated(NSE_DELIVERY, day)
            guard_quarantine(day, l0=self._l0, register=self._register, data_root=self._data_root)
            written = self._set.write(rows, self._ctx)
            if not isinstance(written, PricesRawWriteReport):  # pragma: no cover — contract
                raise TypeError(f"write returned {type(written).__name__}")
            self._sync.mark_normalized(NSE_DELIVERY, day)
            self._sync.mark_published(NSE_DELIVERY, day)
            self._commit()
        except ParseError as exc:
            return self._fail(day, f"parse failed: {exc}")
        except Exception as exc:  # L0, L1, the guard or the DB — recorded loudly, the run continues
            return self._fail(day, f"{type(exc).__name__}: {exc}")

        _LOG.info(
            "mto_backfill.promoted",
            source=NSE_DELIVERY,
            date=day.isoformat(),
            progress=progress,
            delivery_rows=written.delivery_rows,
            delivery_joined=written.delivery_joined,
            delivery_unresolved=written.delivery_unresolved,
            delivery_orphaned=written.delivery_orphaned,
            state="PUBLISHED",
        )
        return PromotedSession(
            trade_date=day,
            state="PUBLISHED",
            delivery_rows=written.delivery_rows,
            delivery_joined=written.delivery_joined,
            delivery_unresolved=written.delivery_unresolved,
            delivery_orphaned=written.delivery_orphaned,
        )

    def _fail(self, day: date, message: str) -> PromotedSession:
        """File one session's failure in `sync_state` so a broken source reaches the status API."""
        self._rollback()
        try:
            self._sync.begin(NSE_DELIVERY, day)
            self._sync.mark_failed(NSE_DELIVERY, day, message, retryable=True)
            self._commit()
        except Exception as exc:
            self._rollback()
            _LOG.error(
                "mto_backfill.fail_record_failed",
                source=NSE_DELIVERY,
                date=day.isoformat(),
                error=f"{type(exc).__name__}: {exc}",
                state="FAILED",
            )
        _LOG.error(
            "mto_backfill.promote_failed",
            source=NSE_DELIVERY,
            date=day.isoformat(),
            error=message,
            state="FAILED",
        )
        return PromotedSession(trade_date=day, state="FAILED", error=message)

    def _rollback(self) -> None:
        conn = getattr(self._sync, "_conn", None)
        rollback = getattr(conn, "rollback", None)
        if callable(rollback):
            rollback()


# ── reporting ────────────────────────────────────────────────────────────────────────────────


def _delivery_filled(day: date, *, data_root: Path | None) -> tuple[int, int]:
    """`(NSE EQ rows, of which with deliv_qty)` in the session's `prices_raw` partition."""
    path = partition_path(Layer.L1, PRICES_RAW_DATASET, day, data_root=data_root)
    if not path.is_file():
        return 0, 0
    table = pq.read_table(path, columns=["exchange", "series", "deliv_qty"]).to_pylist()
    eq = [row for row in table if row["exchange"] == Exchange.NSE.value and row["series"] == "EQ"]
    return len(eq), sum(1 for row in eq if row["deliv_qty"] is not None)


def coverage_report(
    plan: SessionPlan,
    *,
    l0: L0Store,
    journal: NoSessionJournal,
    register: SourceRegister,
    data_root: Path | None = None,
) -> str:
    """The markdown coverage artefact: per year, sessions in L0 / 404 / not attempted, and how many
    NSE EQ price rows carry a delivery figure — read off the lake, never out of a run's memory.

    Offline and read-only. The block-shape column counts payloads per settlement-block layout
    (`mto.block_shape`), which is the per-era evidence the parser's segmentation rests on.
    """
    lines = [
        f"### NSE MTO delivery coverage — {plan.start.isoformat()}..{plan.end.isoformat()}",
        "",
        "| year | sessions | in L0 | 404 (no MTO) | not attempted | NSE EQ rows | with delivery "
        "| block shapes |",
        "|---:|---:|---:|---:|---:|---:|---:|:---|",
    ]
    totals = Counter[str]()
    for year in sorted({day.year for day in plan.dates}):
        days = [day for day in plan.dates if day.year == year]
        shapes = Counter[str]()
        in_l0 = no_mto = eq_rows = filled = 0
        for day in days:
            name = _filename(mto_url(day, register=register))
            if l0.exists(mto.MTO_SOURCE_ID, day, name):
                in_l0 += 1
                try:
                    shapes[mto.block_shape(l0.get(l0.ref_for(mto.MTO_SOURCE_ID, day, name)))] += 1
                except ParseError:
                    shapes["unreadable"] += 1
            elif journal.knows(day):
                no_mto += 1
            rows, with_delivery = _delivery_filled(day, data_root=data_root)
            eq_rows += rows
            filled += with_delivery
        row = Counter(
            sessions=len(days),
            in_l0=in_l0,
            no_mto=no_mto,
            eq_rows=eq_rows,
            filled=filled,
        )
        totals.update(row)
        shape_text = ", ".join(f"{k} x{v}" for k, v in sorted(shapes.items())) or "—"
        lines.append(
            f"| {year} | {len(days)} | {in_l0} | {no_mto} | {len(days) - in_l0 - no_mto} | "
            f"{eq_rows} | {filled} | {shape_text} |"
        )
    lines += [
        f"| **total** | **{totals['sessions']}** | **{totals['in_l0']}** | "
        f"**{totals['no_mto']}** | **{totals['sessions'] - totals['in_l0'] - totals['no_mto']}** "
        f"| **{totals['eq_rows']}** | **{totals['filled']}** | |",
        "",
        "#### 404 evidence (calendar session, no MTO published)",
        "",
    ]
    in_range = sorted(day for day in journal.dates if plan.start <= day <= plan.end)
    planned = set(plan.dates)
    observed = [day for day in in_range if day in planned]
    probes = [day for day in in_range if day not in planned]
    lines.append(", ".join(d.isoformat() for d in observed) if observed else "_none observed_")
    if probes:
        # A date outside the plan is one an operator named explicitly — a declared holiday probed
        # to exercise the 404 path. Listed apart so it never reads as a missing report.
        lines += [
            "",
            "404s on dates outside the plan (explicit probes, e.g. a declared holiday): "
            + ", ".join(d.isoformat() for d in probes),
        ]
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def _install_sigint(state: dict[str, bool]) -> None:
    """Flip `state['stop']` on the first SIGINT so the run stops between sessions."""

    def handle(_signum: int, _frame: FrameType | None) -> None:
        state["stop"] = True
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    signal.signal(signal.SIGINT, handle)


def _parse_dates(raw: str) -> list[date]:
    return sorted({date.fromisoformat(token.strip()) for token in raw.split(",") if token.strip()})


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mto-backfill", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("plan", "print the candidate sessions; opens nothing"),
        ("acquire", "fetch candidate sessions into L0, recording every 404 as evidence"),
        ("promote", "join stored MTO payloads onto prices_raw through the identity master"),
        ("report", "print the markdown coverage artefact from L0, the journal and L1"),
    ):
        child = sub.add_parser(name, help=help_text)
        child.add_argument("--from", dest="from_date", type=date.fromisoformat)
        child.add_argument("--to", dest="to_date", type=date.fromisoformat)
        child.add_argument(
            "--dates",
            type=_parse_dates,
            default=None,
            help="explicit comma-separated dates instead of a range plan (a smoke run; may name a "
            "holiday to exercise the 404 path)",
        )
        child.add_argument("--limit", type=int, default=None, help="sample N sessions evenly")
        if name in ("acquire", "promote"):
            child.add_argument(
                "--expect-l0-root",
                type=Path,
                default=None,
                help="absolute L0 root this run must resolve to; required",
            )
        if name == "acquire":
            child.add_argument("--error-streak-limit", type=int, default=DEFAULT_ERROR_STREAK_LIMIT)
            child.add_argument(
                "--no-session-streak-limit", type=int, default=DEFAULT_NO_SESSION_STREAK_LIMIT
            )
    return parser


def _plan_from_args(
    args: argparse.Namespace, *, calendar: TradingCalendar, priced: Iterable[date]
) -> SessionPlan:
    if args.dates:
        dates = list(args.dates)
        _require_range(dates[0], dates[-1])
        return SessionPlan(
            start=dates[0],
            end=dates[-1],
            dates=tuple(dates),
            basis="explicit",
            note=f"{len(dates)} dates named on the command line",
        )
    if args.from_date is None or args.to_date is None:
        raise ValueError("give either --dates or both --from and --to")
    return plan_sessions(
        args.from_date, args.to_date, calendar=calendar, limit=args.limit, priced=priced
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Exit 0 on a clean run, 2 on a bad invocation, 3 on a hard stop/failure."""
    args = _build_parser().parse_args(argv)
    settings: Settings = get_settings()
    clock: Clock = SystemClock()
    register = load_register()
    calendar = trading_calendar()
    try:
        plan = _plan_from_args(
            args, calendar=calendar, priced=price_sessions(data_root=settings.data_root)
        )
    except ValueError as exc:
        print(f"cannot plan: {exc}", file=sys.stderr)
        return 2

    l0 = L0Store(clock=clock, data_root=settings.data_root)
    journal = NoSessionJournal(
        journal_path_for(settings.data_root), clock=clock, evidence=EVIDENCE_NO_MTO
    )

    if args.command == "plan":
        for day in plan.dates:
            print(f"{day.isoformat()}\t{mto_url(day, register=register)}")
        print(f"\n{len(plan)} sessions, basis={plan.basis} ({plan.note})")
        print(f"L0: {l0.root}")
        return 0
    if args.command == "report":
        print(
            coverage_report(
                plan, l0=l0, journal=journal, register=register, data_root=settings.data_root
            )
        )
        return 0

    try:
        root = require_l0_root(l0, args.expect_l0_root)
    except LakeRootMismatchError as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    print(f"L0: {root}  journal: {journal.path}", flush=True)
    state = {"stop": False}
    _install_sigint(state)

    if args.command == "acquire":
        if settings.http_min_interval_seconds < MIN_SPACING_SECONDS:
            print(
                f"refusing to run: http_min_interval_seconds={settings.http_min_interval_seconds} "
                f"is below the signed-off {MIN_SPACING_SECONDS}s floor",
                file=sys.stderr,
            )
            return 2
        host, _ = _source_row(register)
        with leased_fetcher(
            [host],
            clock=clock,
            command=f"mto_backfill acquire {plan.start.isoformat()}..{plan.end.isoformat()}",
            settings=settings,
            register=register,
        ) as fetcher:
            report = MtoAcquisition(
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

    with connection(settings) as conn:
        master = IdentityStore(conn, clock=clock).load_master()
        promoted = MtoPromotion(
            l0=l0,
            sync=SyncStateStore(conn, clock=clock, calendar=calendar),
            commit=conn.commit,
            register=register,
            master=master,
            data_root=settings.data_root,
            should_stop=lambda: state["stop"],
        ).promote(plan.dates)
    print(promoted.summary())
    return 0 if promoted.count("FAILED") == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
