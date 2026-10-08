"""M13.1 — the daily paper-trading session for the D13-ratified momentum v2 book.

HUMAN_DECISIONS D13 ratified a momentum configuration for **paper mode**
(:data:`~backtest.policies.momentum_v2.PAPER_RATIFIED_2026_09_06`) and D15 said to run it daily.
This module is that run: once per trading day, after the EOD data publishes, it decides one session
of the paper book and journals it. It is the replay engine run *forward* — the same
``ReplayEngine`` → ``RailGate`` (A8) → ``SimBroker`` path every backtest of the configuration went
through, one session at a time, so the paper book is the backtest's book continued past the end of
history rather than a second implementation of the strategy (§7, invariant #5).

**Disabled by default** (``Settings.paper_session_enabled``). The ratified regime filter reads the
*published NIFTY 50 TRI* level for the session itself, and nothing lands that level the same
evening yet: ``tri_refresh`` is weekly and stops at the session before the day it runs, and the
close-all snapshot is a different series (the price index — compared over 3,159 overlapping
sessions it never matches the TRI; ops/runbooks/daily-eod.md has the numbers). Until a same-evening
TRI source exists, every rebalance would be journaled red, so the job is registered but does
nothing until the flag is set.

**Which session** (:func:`owed_session`). The job decides an explicit date: the latest trading
session whose EOD is due by the run's clock — today's from :data:`EOD_DUE_AT` IST, otherwise the
previous session. A retry after midnight therefore decides the session that failed, never the new
calendar day.

**What one run does** (:func:`run_paper_session`), in order, stopping at the first step that says
stop:

1. **Trading-day check.** A date the holiday calendar does not call a session writes nothing.
2. **Idempotency.** A date already ``COMPLETED`` for the book is a no-op — a rerun never trades
   twice. A date recorded ``SKIPPED_DATA_RED`` is re-checked (the data may have healed), but a
   still-red rerun writes nothing new.
3. **The data-red interlock (invariant #10).** The status API is read first; a red verdict, a
   status read that failed, or a decision input that is missing (no L1 prices for the date, no
   published regime index level on a rebalance, no investable universe) journals one
   ``SKIPPED_DATA_RED`` entry and places no order. A failed status read is red, never green.
   **The kill switch** (M15.3) is read next: a tripped switch journals the same no-op, with
   ``payload.event = KILL_SWITCH_TRIPPED`` and the trip's source and reason, and places nothing;
   so does an unresolved corporate-action escalation or reconciliation break (step 7).
4. **Restore the paper book.** The broker is restored from the state the latest ``COMPLETED``
   session persisted (``SimBrokerState``), and must reproduce that session's ``book_digest``
   before it is used. Nothing is replayed: the book rolls forward one session from its last
   snapshot, so the cost of a run does not grow with the book's age. An order staged for a session
   the book did not decide (red, missed) lapses unfilled.
5. **Book the corporate actions known now.** Every reconciled action with an ex-date after the
   book opened and on or before the session, not yet booked by this book, is booked *now*: one
   whose ex-date falls after the last decided session the ordinary way (before the session's
   fills); one whose ex-date the book has already decided past — learnt late, because the
   corporate-action store refreshes weekly — on this session, with an explicit journal entry
   (:func:`_book_late`). The past is never rewritten, so a late action cannot break the restore.
6. **Decide.** ``ReplayEngine`` runs exactly one session — fill yesterday's orders, ask the policy,
   clear every order through A8, place what A8 allowed — and the entries it produced (BUY/SELL,
   RAIL_BLOCK, the HOLD of a risk-off rebalance with nothing to sell, or the HEARTBEAT of a day
   with nothing to do: invariant #9) are appended to the journal and the session recorded
   ``COMPLETED`` with the new state, in the caller's one transaction. A rebalance the regime
   filter parked in cash is recorded with reason ``rebalance_risk_off``.
7. **Stage, execute, reconcile — the live path's code (M15.3).** Orders reach the paper
   ``SimBroker`` only through ``execution.staging.StagingCoordinator``: ``stage`` consults the
   kill switch before every placement and records the order, and ``execute`` fills the session's
   staged orders and posts each fill into the paper book's own accounting book. Right after the
   fills — before the policy is asked — ``execution.recon.Reconciler`` compares that book with the
   ``SimBroker``'s positions and cash. Clean, the session journals one ``HEARTBEAT``
   (``payload.event = RECONCILIATION``) after its decisions. A break trips the kill switch
   (source ``RECON``), so the staging step refuses the session's orders; the session is recorded
   ``RECON_BREAK`` — red, with the book as the fills left it — and journaled ``ESCALATE``
   (``payload.event = RECON_BREAK``), and every later session is refused until the owner records a
   resolution for that break, exactly as for a corporate-action escalation, and resets the switch.
   The accounting book is persisted with the session (``expected_book``) and restored from it, so
   the two sides are built independently across days, never re-derived from the broker.

**Journal timestamps.** An entry's ``ts`` is midnight IST of the session it decides: the engine
freezes its clock on the session date, exactly as in every backtest, so the decision is a pure
function of the session and replays byte-for-byte. When the row actually landed is
``recorded_at``, which the journal stamps from the job's real clock.

**Rebalance timing.** The ratified backtest rebalances on the first session of each month. A paper
book cannot rebalance on a day it was not allowed to decide, so here a rebalance is due on the
first session of the month *this book decides*: a red first session moves the rebalance to the next
green one instead of skipping the month. The first session the book ever decides rebalances too.

**Paper only, structurally.** Nothing here takes a broker. The one broker this module can build is
a ``SimBroker``, constructed or restored inside :func:`_restore_book` and checked by
:func:`require_paper_broker` before a single order reaches it; ``Settings.broker_provider`` is never
read and ``execution.kite_broker`` is never imported (tests pin all three). The staging,
reconciliation and kill-switch code it runs is the shared ``execution`` code a real-money loop will
run (invariant #5); only the broker under it differs. A real-money version is a separate
ratification (AGENTIC_CONTEXT §3.2) and a separate job.

What it never does: read a wall clock for a decision, trade on red data, journal a decision it did
not make, rewrite a past session, or update a ``COMPLETED`` one. Money is ``Decimal`` throughout;
identity is the ISIN.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from psycopg.types.json import Json

from analyst.journal import EVIDENCE_DIRNAME, EvidenceStore, Journal
from analyst.journal.evidence import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceKind,
    EvidenceRef,
    canonical_bytes,
    digest_of,
)
from analyst.journal.models import Actor, Decision, JournalEntry
from analyst.monitor.interlock import StatusApiGate
from backtest.accounting import BookPosition, PortfolioBook
from backtest.book_actions import (
    BookAction,
    BookActionCalendar,
    BookActionSource,
    CashDividend,
    CashExit,
    IsinReissue,
    RescaleKind,
    RescaleSource,
    ShareRescale,
    ShareSwap,
)
from backtest.policies.momentum_v2 import (
    PAPER_RATIFIED_2026_09_06,
    MomentumV2Data,
    MomentumV2Parameters,
    MomentumV2Policy,
    MomentumV2Record,
    RegimeReading,
    regime_parked,
)
from backtest.rails import (
    BACKTEST_CASE_ID,
    BacktestRailPolicy,
    RailGate,
    ratified_backtest_rail_policy,
)
from backtest.replay import (
    BookSnapshot,
    ReplayEngine,
    SessionContext,
    SessionDecision,
)

# The L1 wiring the ten-year momentum runs use, reused rather than re-implemented so the paper
# book reads the market exactly as the backtest that justified it did (as forecast_run does).
from backtest.run import (
    _ACCOUNT_STATE,
    IndexCoverageError,
    RegimeSourceError,
    UniverseParameters,
    _AccountingBroker,
    _AdjustedCloseSource,
    _HoldingMarks,
    _InvestableUniverse,
    _L1Market,
    _L1MomentumV2Data,
    _L1Reader,
    _RegimeSource,
)
from dataplatform.clock import IST, Clock, FrozenClock
from dataplatform.config import Settings
from dataplatform.logging import get_logger
from dataplatform.query.pit import Dataset
from dataplatform.store.db import Connection, connection
from execution.broker import Exchange, Fill, Order, OrderRequest, OrderType, Side
from execution.costs import CostModel, load_rate_card
from execution.kill_switch import KillSwitch, TradingHaltedError, kill_switch_path
from execution.recon import Alerter, LoggingAlerter, ReconBreak, Reconciler, ReconResult
from execution.sim_broker import SessionMarket, SimBroker, SimBrokerState
from execution.staging import InMemoryOrderJournal, StagedOrder, StagingCoordinator

if TYPE_CHECKING:
    from analyst.monitor.interlock import GreenLike
    from dataplatform.ingest.calendar import TradingCalendar
    from dataplatform.query.service import QueryService
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "AMBIGUOUS_ACTION_EVENT",
    "CHANGED_ACTION_EVENT",
    "EOD_DUE_AT",
    "KILL_SWITCH_EVENT",
    "LATE_ACTION_EVENT",
    "PAPER_BOOK_ID",
    "PAPER_DATASETS",
    "PAPER_MODE",
    "PAPER_OPENING_CASH",
    "RECON_BREAK_EVENT",
    "RECON_EVENT",
    "REGIME_TRI_INPUT",
    "ActionStatus",
    "BookedAction",
    "InMemoryPaperSessionStore",
    "JournalSink",
    "L1PaperWorld",
    "PaperBookDivergenceError",
    "PaperBookSpec",
    "PaperModeViolationError",
    "PaperSessionError",
    "PaperSessionRecord",
    "PaperSessionResult",
    "PaperSessionStore",
    "PaperSessionSummary",
    "PaperWorld",
    "PostgresPaperSessionStore",
    "ReconRecord",
    "ReconStatus",
    "RecordingJournal",
    "RunVerdict",
    "SessionOutcome",
    "action_identity",
    "action_terms",
    "owed_session",
    "paper_kill_switch",
    "ratified_paper_book",
    "require_paper_broker",
    "run_paper_session",
    "run_paper_session_job",
]

_LOG = get_logger(__name__)

#: The book D13/D15 put into paper trading. One id per ratified configuration: a different
#: configuration is a different book with its own ledger, never a mutation of this one.
PAPER_BOOK_ID: Final = "momentum_v2_paper_2026_09_06"

#: The capital every M9 momentum v2 report was struck on (₹10 lakh nominal) — the paper book opens
#: with the capital its evidence assumed, so its results are comparable to that evidence.
PAPER_OPENING_CASH: Final = Decimal("1000000")

#: The ``sync_state`` sources whose ``PUBLISHED``, quality-green state the interlock requires for
#: the session (invariant #10). Momentum v2 ranks NSE equities on NSE closes and fills on NSE
#: bars, so the NSE bhavcopy is its whole same-day input. ``CORE_DATASETS``' corporate-action feed
#: is deliberately *not* here: it refreshes weekly (``ca_refresh``, Saturdays), so requiring it
#: would make four sessions in five red. Corporate actions are booked when they become known
#: instead — on their ex-date when known in time, otherwise as an explicit late action (module
#: docstring, step 5).
PAPER_DATASETS: Final[tuple[str, ...]] = ("nse_bhavcopy",)

#: The regime filter's input on a rebalance: the session's published NIFTY 50 TRI level, as
#: ``tri_evening`` (M13.7) files it. Named in a red day's reason and ``payload.missing_input``.
REGIME_TRI_INPUT: Final = "nifty_tri_history/nifty50"

#: Stamped on every journal entry and in the payload, so a paper decision is never mistaken for a
#: real-money one and every entry names the book it belongs to.
PAPER_MODE: Final = "PAPER"

#: From this time (IST) a session's EOD is due and the job owes that session a decision; before it,
#: the job owes the previous session. The EOD pipeline fires at 18:30.
EOD_DUE_AT: Final = time(18, 30)

_BOOK_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
#: How far past the session the fill calendar is extended: an order staged on the session needs the
#: next session to target, and the longest Indian market closure runs to a few days.
_CALENDAR_HEADROOM = timedelta(days=45)
#: How far back :func:`owed_session` looks for a session (the longest closure, with margin).
_OWED_LOOKBACK_DAYS = 10
#: The payload ``event`` on a late corporate action's journal entry.
LATE_ACTION_EVENT: Final = "LATE_CORPORATE_ACTION"
#: The payload ``event`` on an already-seen action whose terms have since changed.
CHANGED_ACTION_EVENT: Final = "CHANGED_CORPORATE_ACTION"
#: The payload ``event`` on a rescale that matches a booked one's ratio with neither side implied.
AMBIGUOUS_ACTION_EVENT: Final = "AMBIGUOUS_CORPORATE_ACTION"
#: The payload ``event`` on a session's reconciliation entry (a HEARTBEAT when clean, M15.3).
RECON_EVENT: Final = "RECONCILIATION"
#: The payload ``event`` on a reconciliation break's ESCALATE entry.
RECON_BREAK_EVENT: Final = "RECON_BREAK"
#: The payload ``event`` on the no-op a tripped kill switch journals.
KILL_SWITCH_EVENT: Final = "KILL_SWITCH_TRIPPED"


# ── errors ───────────────────────────────────────────────────────────────────────────────────────


class PaperSessionError(Exception):
    """The paper session could not proceed. Raised, so the scheduler records the run FAILED."""


class PaperModeViolationError(PaperSessionError):
    """Something other than the paper ``SimBroker`` reached the paper session's order path."""


class PaperBookDivergenceError(PaperSessionError):
    """The restored paper book does not reproduce the digest recorded with its state.

    The persisted state and its digest were written in one transaction, so a mismatch means the
    stored row was altered or the serialisation changed — not that the market moved (a late
    corporate action is booked forward, never replayed). An operator must look
    (ops/runbooks/daily-eod.md); the book is never silently re-based.
    """


# ── the book, its outcomes ───────────────────────────────────────────────────────────────────────


class SessionOutcome(StrEnum):
    """What a recorded session concluded (``paper_session.outcome``)."""

    COMPLETED = "COMPLETED"
    """Decided: the journal holds the decision and the orders the paper broker was given."""

    SKIPPED_DATA_RED = "SKIPPED_DATA_RED"
    """The interlock refused the day; one ``SKIPPED_DATA_RED`` entry, no order (invariant #10)."""

    RECON_BREAK = "RECON_BREAK"
    """Red: the session's fills left the paper book and the ``SimBroker`` disagreeing (M15.3).

    The kill switch tripped and the staging step placed nothing; the row carries the book as the
    fills left it (they happened), and later sessions are refused until the break is resolved."""

    @property
    def moved_book(self) -> bool:
        """Whether a session with this outcome filled orders and carries the book it left.

        ``COMPLETED`` and ``RECON_BREAK`` both did: the next session restores from either, and
        neither may be decided again. A ``SKIPPED_DATA_RED`` day never touched the book.
        """
        return self is not SessionOutcome.SKIPPED_DATA_RED


class RunVerdict(StrEnum):
    """What one invocation did — the job's log line and the test surface."""

    DECIDED = "DECIDED"
    SKIPPED_DATA_RED = "SKIPPED_DATA_RED"
    ALREADY_DECIDED = "ALREADY_DECIDED"
    """The date was already ``COMPLETED``: nothing journaled, nothing placed."""
    STILL_RED = "STILL_RED"
    """The date was already recorded red and is still red: the skip is not journaled twice."""
    NOT_A_SESSION = "NOT_A_SESSION"
    """A holiday or a weekend: no decision is owed."""
    HALTED = "HALTED"
    """The kill switch was tripped: one no-op journaled, nothing staged (recorded red)."""
    RECON_BREAK = "RECON_BREAK"
    """The fills broke reconciliation: the switch tripped, nothing staged, the session is red."""


class ReconStatus(StrEnum):
    """What the session's reconciliation found (``paper_session.recon.status``)."""

    CLEAN = "CLEAN"
    BREAK = "BREAK"


@dataclass(frozen=True, slots=True)
class ReconRecord:
    """One session's reconciliation of the paper book against the ``SimBroker`` (M15.3).

    ``key`` and ``terms`` name a break the way an escalated corporate action is named — ``key`` is
    ``RECON:<session>``, ``terms`` a digest of the breaks — so the owner resolves it with the same
    ``paper_session_resolution`` row. ``breaks`` are one-line descriptions (book vs broker, per
    ISIN or cash); ``staged`` and ``executed`` the broker order ids the staging step placed and
    filled this session. ``seeded`` marks the one session whose book had no persisted accounting
    book to restore (a row written before M15.3) and was seeded from the broker instead — that
    session's recon proves only that the session itself was consistent.
    """

    status: ReconStatus
    key: str
    terms: str
    breaks: tuple[str, ...] = ()
    staged: tuple[str, ...] = ()
    executed: tuple[str, ...] = ()
    seeded: bool = False

    def __post_init__(self) -> None:
        if (self.status is ReconStatus.BREAK) != bool(self.breaks):
            raise ValueError("a reconciliation is a BREAK exactly when it found breaks")

    @classmethod
    def of(
        cls,
        result: ReconResult,
        *,
        staged: Sequence[str],
        executed: Sequence[str],
        seeded: bool,
    ) -> ReconRecord:
        breaks = tuple(item.describe() for item in result.breaks)
        return cls(
            status=ReconStatus.CLEAN if result.ok else ReconStatus.BREAK,
            key=f"RECON:{result.session.isoformat()}",
            terms=digest_of(canonical_bytes([_break_document(b) for b in result.breaks]))[:16],
            breaks=breaks,
            staged=tuple(staged),
            executed=tuple(executed),
            seeded=seeded,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "key": self.key,
            "terms": self.terms,
            "breaks": list(self.breaks),
            "staged": list(self.staged),
            "executed": list(self.executed),
            "seeded": "true" if self.seeded else "false",
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ReconRecord:
        return cls(
            status=ReconStatus(document["status"]),
            key=document["key"],
            terms=document["terms"],
            breaks=tuple(document.get("breaks", ())),
            staged=tuple(document.get("staged", ())),
            executed=tuple(document.get("executed", ())),
            seeded=document.get("seeded") == "true",
        )


def _break_document(item: ReconBreak) -> dict[str, str]:
    return {
        "kind": item.kind.value,
        "isin": item.isin or "",
        "expected": str(item.expected),
        "actual": str(item.actual),
    }


@dataclass(frozen=True, slots=True)
class PaperBookSpec:
    """The book being paper-traded: its id, the ratified parameters, capital, and rails.

    Fixed by construction — the job builds the one ratified spec (:func:`ratified_paper_book`) and a
    test builds its own. The rail policy is required: an unrailed paper book would report results
    for a portfolio this system would never be allowed to hold (invariant #6).
    """

    book_id: str
    parameters: MomentumV2Parameters
    opening_cash: Decimal
    rail_policy: BacktestRailPolicy
    datasets: tuple[str, ...] = PAPER_DATASETS

    def __post_init__(self) -> None:
        if not _BOOK_ID.match(self.book_id):
            raise ValueError(f"book_id {self.book_id!r} must be lower snake_case, 3-64 chars")
        if not isinstance(self.opening_cash, Decimal):
            raise TypeError("opening_cash must be a Decimal — money is never float (CLAUDE.md)")
        if self.opening_cash <= 0:
            raise ValueError(f"opening_cash must be positive, got {self.opening_cash}")
        if not self.datasets:
            raise ValueError("a paper book must name the datasets its interlock checks")


def ratified_paper_book() -> PaperBookSpec:
    """The D13-ratified momentum v2 paper book: the M9 report's capital under the ratified rails."""
    return PaperBookSpec(
        book_id=PAPER_BOOK_ID,
        parameters=PAPER_RATIFIED_2026_09_06,
        opening_cash=PAPER_OPENING_CASH,
        rail_policy=ratified_backtest_rail_policy(),
    )


class ActionStatus(StrEnum):
    """What the book did with a corporate action it has seen (``paper_session.actions``)."""

    BOOKED = "BOOKED"
    """Applied to the book — or nothing to apply, because the book did not hold the name."""

    ESCALATED = "ESCALATED"
    """Not applied: booking it mechanically was not safe. Blocks the book until the owner records
    a resolution (``paper_session_resolution``), as red data blocks it (invariant #10)."""


@dataclass(frozen=True, slots=True)
class BookedAction:
    """One corporate action as this book has seen it: identity, terms, effect, outcome.

    ``key`` is the action's *identity* (:func:`action_identity`) — what it is, never its terms —
    and ``terms`` a digest of its economic terms (:func:`action_terms`), so a corrected record is
    recognised as the same action with different terms rather than a new one. One key may carry
    several terms (two dividends on one ex-date are one identity); each (key, terms) is booked
    once. ``held`` says the book held the name when the action was seen, i.e. the action did (or
    would have) moved it.
    """

    key: str
    terms: str
    held: bool
    status: ActionStatus
    #: Where a rescale came from (``feed``/``curated``/``implied``); empty for other kinds. The
    #: twin rule needs it: two same-ratio rescales are one event only if one side was implied.
    source: str = ""

    def to_document(self) -> dict[str, str]:
        return {
            "key": self.key,
            "terms": self.terms,
            "held": "true" if self.held else "false",
            "status": self.status.value,
            "source": self.source,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, str]) -> BookedAction:
        return cls(
            key=document["key"],
            terms=document["terms"],
            held=document["held"] == "true",
            status=ActionStatus(document["status"]),
            source=document.get("source", ""),
        )


@dataclass(frozen=True, slots=True)
class PaperSessionRecord:
    """One ``paper_session`` row: what a session concluded and what the next one resumes from.

    ``orders`` are the requests placed on the paper broker after A8 cleared them, in placement
    order; ``pending`` the momentum policy's redeploy target carried to the next session;
    ``book_state`` the broker's whole state after the session (``{"broker": SimBrokerState
    document, "session_ledger": [...]}``) and ``book_digest`` the sha256 of its broker part;
    ``actions`` the corporate actions this session saw for the first time, or saw with changed
    terms. ``recon`` is the session's reconciliation and ``expected_book`` the paper book's own
    accounting book after it (cash and positions, the side recon checks the broker against) — both
    ``None`` on a row written before M15.3. A red record holds none of them.
    """

    book_id: str
    trading_date: date
    outcome: SessionOutcome
    reason: str
    rebalanced: bool
    journal_digest: str
    orders: tuple[OrderRequest, ...] = ()
    pending: Mapping[str, Decimal] | None = None
    book_state: Mapping[str, Any] | None = None
    book_digest: str | None = None
    actions: tuple[BookedAction, ...] = ()
    recon: ReconRecord | None = None
    expected_book: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.outcome is SessionOutcome.SKIPPED_DATA_RED and (
            self.orders
            or self.pending is not None
            or self.rebalanced
            or self.book_digest
            or self.book_state is not None
            or self.actions
            or self.recon is not None
            or self.expected_book is not None
        ):
            raise ValueError("a SKIPPED_DATA_RED session places nothing and carries no book state")
        if self.outcome.moved_book and (self.book_digest is None or self.book_state is None):
            raise ValueError(
                f"a {self.outcome.value} session must record the book state it ended on"
            )
        if self.outcome is SessionOutcome.RECON_BREAK and (
            self.orders
            or self.rebalanced
            or self.recon is None
            or self.recon.status is not ReconStatus.BREAK
        ):
            raise ValueError(
                "a RECON_BREAK session records its break, stages no order and is never the "
                "month's rebalance"
            )
        if self.outcome is SessionOutcome.COMPLETED and (
            self.recon is not None and self.recon.status is not ReconStatus.CLEAN
        ):
            raise ValueError("a COMPLETED session's reconciliation is clean")

    def broker_state(self) -> SimBrokerState:
        """The paper broker's state after this session; raises on a red record."""
        if self.book_state is None:
            raise PaperSessionError(f"{self.trading_date.isoformat()} recorded no book state")
        return SimBrokerState.from_document(self.book_state["broker"])

    def summary(self) -> PaperSessionSummary:
        """This record without its book state — what the per-run index reads."""
        return PaperSessionSummary(
            trading_date=self.trading_date,
            outcome=self.outcome,
            rebalanced=self.rebalanced,
            traded=frozenset(order.isin for order in self.orders),
            actions=self.actions,
            recon_break=(
                (self.recon.key, self.recon.terms)
                if self.recon is not None and self.recon.status is ReconStatus.BREAK
                else None
            ),
        )

    def orders_document(self) -> list[dict[str, str | None]]:
        """``orders`` as JSON-safe strings — Decimals never travel as JSON numbers."""
        return [_order_document(order) for order in self.orders]

    def pending_document(self) -> dict[str, str] | None:
        """``pending`` as ISIN -> weight strings, or ``None``."""
        if self.pending is None:
            return None
        return {isin: str(weight) for isin, weight in sorted(self.pending.items())}

    def actions_document(self) -> list[dict[str, str]]:
        """``actions`` as JSON-safe strings."""
        return [action.to_document() for action in self.actions]

    @classmethod
    def from_documents(
        cls,
        *,
        book_id: str,
        trading_date: date,
        outcome: str,
        reason: str,
        rebalanced: bool,
        journal_digest: str,
        orders: Sequence[Mapping[str, str | None]],
        pending: Mapping[str, str] | None,
        book_state: Mapping[str, Any] | None,
        book_digest: str | None,
        actions: Sequence[Mapping[str, str]],
        recon: Mapping[str, Any] | None = None,
        expected_book: Mapping[str, Any] | None = None,
    ) -> PaperSessionRecord:
        """The record a stored row describes (the inverse of the ``*_document`` methods)."""
        return cls(
            book_id=book_id,
            trading_date=trading_date,
            outcome=SessionOutcome(outcome),
            reason=reason,
            rebalanced=rebalanced,
            journal_digest=journal_digest,
            orders=tuple(_order_from(document) for document in orders),
            pending=(
                None
                if pending is None
                else {isin: Decimal(weight) for isin, weight in pending.items()}
            ),
            book_state=book_state,
            book_digest=book_digest,
            actions=tuple(BookedAction.from_document(action) for action in actions),
            recon=None if recon is None else ReconRecord.from_document(recon),
            expected_book=expected_book,
        )


@dataclass(frozen=True, slots=True)
class PaperSessionSummary:
    """A ``paper_session`` row without its book state: what each run indexes over the history.

    A run needs the full state of one session only (the latest decided one, or the one entering a
    late action's ex-date); for every other row it needs only the date, the outcome, whether it
    rebalanced, which names it traded and which corporate actions it saw — so that is all the
    store reads for them.
    """

    trading_date: date
    outcome: SessionOutcome
    rebalanced: bool
    traded: frozenset[str]
    actions: tuple[BookedAction, ...]
    #: (key, terms) of the session's reconciliation break, or ``None`` when it had none.
    recon_break: tuple[str, str] | None = None


def _order_document(order: OrderRequest) -> dict[str, str | None]:
    return {
        "isin": order.isin,
        "side": order.side.value,
        "quantity": str(order.quantity),
        "exchange": order.exchange.value,
        "order_type": order.order_type.value,
        "limit_price": None if order.limit_price is None else str(order.limit_price),
        "tag": order.tag,
    }


def _order_from(document: Mapping[str, str | None]) -> OrderRequest:
    def required(key: str) -> str:
        value = document.get(key)
        if value is None:
            raise PaperSessionError(f"recorded order is missing {key!r}: {dict(document)!r}")
        return value

    limit = document.get("limit_price")
    return OrderRequest(
        isin=required("isin"),
        side=Side(required("side")),
        quantity=int(required("quantity")),
        exchange=Exchange(required("exchange")),
        order_type=OrderType(required("order_type")),
        limit_price=None if limit is None else Decimal(limit),
        tag=document.get("tag"),
    )


def _state_digest(state: SimBrokerState) -> str:
    return digest_of(canonical_bytes(state.to_document()))


# ── persistence: the session ledger ──────────────────────────────────────────────────────────────


class PaperSessionStore(Protocol):
    """Where a paper book's session records live — Postgres in production, a dict in a test.

    Reads are shaped so a run loads one full book state (two for a late corporate action), never
    every row's: ``summaries`` for the index, ``latest_completed`` for the snapshot.
    """

    def get(self, book_id: str, trading_date: date) -> PaperSessionRecord | None:
        """The full record for one date, or ``None``."""

    def summaries(self, book_id: str, *, before: date) -> tuple[PaperSessionSummary, ...]:
        """Every record strictly before ``before``, without book state, oldest first."""

    def latest_completed(self, book_id: str, *, before: date) -> PaperSessionRecord | None:
        """The latest record that moved the book strictly before ``before``, in full, or ``None``.

        ``COMPLETED`` or ``RECON_BREAK`` (:attr:`SessionOutcome.moved_book`): a break session's
        fills happened, so the book continues from it once the break is resolved.
        """

    def resolutions(self, book_id: str) -> frozenset[tuple[str, str]]:
        """The (key, terms) escalations the owner resolved (``paper_session_resolution``).

        Per terms, not per key: resolving one correction of an action does not pre-approve the
        next one. A reconciliation break is resolved the same way, under its ``RECON:<date>`` key.
        """

    def record(self, record: PaperSessionRecord, *, recorded_at: datetime) -> None:
        """Write a record. A date that moved the book is final; only a skip may be superseded."""


class InMemoryPaperSessionStore:
    """A ``PaperSessionStore`` over a dict — the same contract as the Postgres one, no database."""

    __slots__ = ("_resolved", "_rows")

    def __init__(self) -> None:
        self._rows: dict[tuple[str, date], PaperSessionRecord] = {}
        self._resolved: set[tuple[str, str, str]] = set()

    def get(self, book_id: str, trading_date: date) -> PaperSessionRecord | None:
        return self._rows.get((book_id, trading_date))

    def _before(self, book_id: str, before: date) -> list[PaperSessionRecord]:
        return [
            self._rows[key] for key in sorted(self._rows) if key[0] == book_id and key[1] < before
        ]

    def summaries(self, book_id: str, *, before: date) -> tuple[PaperSessionSummary, ...]:
        return tuple(record.summary() for record in self._before(book_id, before))

    def latest_completed(self, book_id: str, *, before: date) -> PaperSessionRecord | None:
        completed = [
            record for record in self._before(book_id, before) if record.outcome.moved_book
        ]
        return completed[-1] if completed else None

    def resolutions(self, book_id: str) -> frozenset[tuple[str, str]]:
        return frozenset((key, terms) for book, key, terms in self._resolved if book == book_id)

    def records(self, book_id: str) -> tuple[PaperSessionRecord, ...]:
        """Every full record of the book, oldest first — for a test to inspect or copy."""
        return tuple(self._before(book_id, date.max))

    def resolve(self, book_id: str, key: str, terms: str) -> None:
        """What the owner's ``INSERT INTO paper_session_resolution`` does (runbook)."""
        self._resolved.add((book_id, key, terms))

    def record(self, record: PaperSessionRecord, *, recorded_at: datetime) -> None:
        key = (record.book_id, record.trading_date)
        existing = self._rows.get(key)
        if existing is not None and existing.outcome is not SessionOutcome.SKIPPED_DATA_RED:
            raise PaperSessionError(
                f"{record.book_id} {record.trading_date.isoformat()} is already "
                f"{existing.outcome.value}; a decided session is never rewritten"
            )
        self._rows[key] = record


_COLUMNS = (
    "book_id, trading_date, outcome, reason, rebalanced, orders, pending, journal_digest, "
    "book_state, book_digest, actions, recon, expected_book"
)


class PostgresPaperSessionStore:
    """``paper_session`` (0012, 0015). Nothing here commits — the job owns the transaction.

    ``record`` inserts, or replaces a ``SKIPPED_DATA_RED`` row for the same date; it never touches a
    ``COMPLETED`` or ``RECON_BREAK`` row, and a write that would is an error rather than a silent
    no-op, because the caller is about to commit journal entries that claim the session was decided.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def get(self, book_id: str, trading_date: date) -> PaperSessionRecord | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM paper_session WHERE book_id = %s AND trading_date = %s",
            (book_id, trading_date),
        ).fetchone()
        return None if row is None else _record_of(row)

    def summaries(self, book_id: str, *, before: date) -> tuple[PaperSessionSummary, ...]:
        rows = self._conn.execute(
            "SELECT trading_date, outcome, rebalanced, "
            "ARRAY(SELECT DISTINCT o->>'isin' FROM jsonb_array_elements(orders) AS o), actions, "
            "recon->>'status', recon->>'key', recon->>'terms' "
            "FROM paper_session WHERE book_id = %s AND trading_date < %s ORDER BY trading_date",
            (book_id, before),
        ).fetchall()
        return tuple(
            PaperSessionSummary(
                trading_date=trading_date,
                outcome=SessionOutcome(outcome),
                rebalanced=rebalanced,
                traded=frozenset(traded),
                actions=tuple(BookedAction.from_document(action) for action in actions),
                recon_break=(key, terms) if status == ReconStatus.BREAK.value else None,
            )
            for trading_date, outcome, rebalanced, traded, actions, status, key, terms in rows
        )

    def latest_completed(self, book_id: str, *, before: date) -> PaperSessionRecord | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM paper_session WHERE book_id = %s AND trading_date < %s "
            "AND outcome IN ('COMPLETED', 'RECON_BREAK') ORDER BY trading_date DESC LIMIT 1",
            (book_id, before),
        ).fetchone()
        return None if row is None else _record_of(row)

    def resolutions(self, book_id: str) -> frozenset[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT action_key, terms FROM paper_session_resolution WHERE book_id = %s",
            (book_id,),
        ).fetchall()
        return frozenset((row[0], row[1]) for row in rows)

    def record(self, record: PaperSessionRecord, *, recorded_at: datetime) -> None:
        row = self._conn.execute(
            "INSERT INTO paper_session (book_id, trading_date, outcome, reason, rebalanced, "
            "orders, pending, journal_digest, book_state, book_digest, actions, recon, "
            "expected_book, recorded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (book_id, trading_date) DO UPDATE SET "
            "outcome = EXCLUDED.outcome, reason = EXCLUDED.reason, "
            "rebalanced = EXCLUDED.rebalanced, orders = EXCLUDED.orders, "
            "pending = EXCLUDED.pending, journal_digest = EXCLUDED.journal_digest, "
            "book_state = EXCLUDED.book_state, book_digest = EXCLUDED.book_digest, "
            "actions = EXCLUDED.actions, recon = EXCLUDED.recon, "
            "expected_book = EXCLUDED.expected_book, recorded_at = EXCLUDED.recorded_at "
            "WHERE paper_session.outcome = 'SKIPPED_DATA_RED' "
            "RETURNING trading_date",
            (
                record.book_id,
                record.trading_date,
                record.outcome.value,
                record.reason,
                record.rebalanced,
                Json(record.orders_document()),
                None if record.pending is None else Json(record.pending_document()),
                record.journal_digest,
                None if record.book_state is None else Json(dict(record.book_state)),
                record.book_digest,
                Json(record.actions_document()),
                None if record.recon is None else Json(record.recon.to_document()),
                None if record.expected_book is None else Json(dict(record.expected_book)),
                recorded_at,
            ),
        ).fetchone()
        if row is None:
            raise PaperSessionError(
                f"{record.book_id} {record.trading_date.isoformat()} is already decided; a "
                "decided session is never rewritten"
            )


def _record_of(row: Sequence[Any]) -> PaperSessionRecord:
    (
        book_id,
        trading_date,
        outcome,
        reason,
        rebalanced,
        orders,
        pending,
        journal_digest,
        book_state,
        book_digest,
        actions,
        recon,
        expected_book,
    ) = row
    return PaperSessionRecord.from_documents(
        book_id=book_id,
        trading_date=trading_date,
        outcome=outcome,
        reason=reason,
        rebalanced=rebalanced,
        journal_digest=journal_digest,
        orders=orders,
        pending=pending,
        book_state=book_state,
        book_digest=book_digest,
        actions=actions,
        recon=recon,
        expected_book=expected_book,
    )


# ── the journal the session writes to ────────────────────────────────────────────────────────────


class JournalSink(Protocol):
    """The slice of ``analyst.journal.Journal`` the session writes through.

    ``Journal`` satisfies it; :class:`RecordingJournal` is the offline stand-in. Append-only by
    shape: there is nothing here that could edit or remove an entry (invariant #12).
    """

    def snapshot(self, bundle: EvidenceBundle) -> EvidenceRef:
        """Store an evidence bundle, content-addressed."""

    def append(self, entry: JournalEntry, *, evidence: EvidenceBundle | None = None) -> object:
        """Append one entry."""


class RecordingJournal:
    """A ``JournalSink`` that keeps bundles and entries in memory — tests and dry runs."""

    __slots__ = ("bundles", "entries")

    def __init__(self) -> None:
        self.bundles: dict[str, EvidenceBundle] = {}
        self.entries: list[JournalEntry] = []

    def snapshot(self, bundle: EvidenceBundle) -> EvidenceRef:
        ref = bundle.ref()
        self.bundles[ref.ref] = bundle
        return ref

    def append(self, entry: JournalEntry, *, evidence: EvidenceBundle | None = None) -> object:
        if evidence is not None:
            entry = entry.model_copy(update={"evidence_snapshot_ref": self.snapshot(evidence).ref})
        self.entries.append(entry)
        return entry


# ── the market the session reads ─────────────────────────────────────────────────────────────────


class PaperWorld(Protocol):
    """Everything the session reads about the market — the calendar, bars, marks, signal, actions.

    Production is :class:`L1PaperWorld` (the holiday calendar and the L1/L2 lake, read exactly as
    the momentum backtests read it); a test supplies a fixture day. Nothing here can place an order.
    """

    def is_session(self, day: date) -> bool:
        """Whether ``day`` is a normal trading session per the holiday calendar."""

    def sessions(self, start: date, end: date) -> Sequence[date]:
        """The calendar's trading sessions in ``[start, end]``, ascending."""

    def prices_ready(self, day: date) -> bool:
        """Whether the session's NSE bars are in the store — fills and marks need them."""

    def market(
        self, *, first: date, through: date, held: Callable[[], Iterable[str]]
    ) -> SessionMarket:
        """The fill market over the calendar from ``first`` to past ``through``."""

    def marks(self, held: Callable[[], Iterable[str]]) -> Callable[[date], Mapping[str, Decimal]]:
        """Session -> ISIN -> raw close: what A8 values the book at."""

    def momentum_data(self, day: date, parameters: MomentumV2Parameters) -> MomentumV2Data:
        """The momentum signal and regime for the decision on ``day``."""

    def corporate_actions(self) -> BookActionSource | None:
        """Splits, bonuses, dividends and exits, as currently known, keyed by ex-date."""


def owed_session(world: PaperWorld, now: datetime) -> date | None:
    """The session the job owes a decision for at ``now`` — an explicit date, never "today".

    The latest trading session whose EOD is due: from :data:`EOD_DUE_AT` IST that is today (when
    today is a session), before it the latest session before today. So the 21:45 run decides
    today, and a retry at 00:30 decides yesterday's — the session that failed — not the new day,
    whose market has not even opened. ``None`` only if no session lies in the lookback.
    """
    local = now.astimezone(IST)
    day = local.date() if local.time() >= EOD_DUE_AT else local.date() - timedelta(days=1)
    for _ in range(_OWED_LOOKBACK_DAYS):
        if world.is_session(day):
            return day
        day -= timedelta(days=1)
    return None


# ── the paper broker ─────────────────────────────────────────────────────────────────────────────


def require_paper_broker(broker: object) -> SimBroker:
    """Return ``broker`` if it is the paper ``SimBroker`` itself; raise otherwise.

    What it does: the last check before the paper session's order path is used — exactly
    ``SimBroker``, not a subclass and not anything else satisfying ``Broker``, so no wrapper or
    adapter can route a paper decision to a real account. What it never does: accept a
    ``KiteBroker``, in dry-run or not.
    """
    if type(broker) is not SimBroker:
        raise PaperModeViolationError(
            f"the paper session trades only on SimBroker, got {type(broker).__module__}."
            f"{type(broker).__qualname__}; real-money routing is a separate ratification "
            "(AGENTIC_CONTEXT §3.2)"
        )
    return broker


def paper_kill_switch(data_root: Path, book_id: str, *, clock: Clock | None = None) -> KillSwitch:
    """The paper book's kill switch: ``<data_root>/kill_switch/<book_id>.json`` (M15.3).

    One switch per paper book, so halting the paper book halts nothing else. Tripped by a
    reconciliation break or by hand (``python -m execution.kill_switch trip --account <book>``),
    re-armed only by hand (``reset``); ops/runbooks/daily-eod.md has the procedure.
    """
    return KillSwitch(kill_switch_path(data_root, book_id), clock=clock)


class _ExpectedBook:
    """The paper book's own accounting book — what reconciliation checks the ``SimBroker`` against.

    A ``PortfolioBook`` posted with every fill the staging step processed and every corporate
    action the session booked, and persisted with the session (``expected_book``), so across days
    it is built only from what the session did — never re-read off the broker, which would make
    reconciliation compare the broker to itself. Satisfies ``execution.staging.FillLedger`` (the
    coordinator posts fills into it) and ``execution.recon.ReconBook`` (recon reads it).
    """

    __slots__ = ("book",)

    def __init__(self, book: PortfolioBook) -> None:
        self.book = book

    def apply(self, fill: Fill) -> None:
        self.book.record_fill(fill)

    @property
    def cash(self) -> Decimal:
        return self.book.cash

    def quantities(self) -> Mapping[str, int]:
        return {position.isin: position.quantity for position in self.book.positions()}

    def to_document(self) -> dict[str, Any]:
        """Cash and positions as JSON-safe strings — what the next session restores."""
        return {
            "cash": str(self.book.cash),
            "positions": [
                {
                    "isin": position.isin,
                    "quantity": str(position.quantity),
                    "cost_basis": str(position.cost_basis),
                }
                for position in self.book.positions()
            ],
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> _ExpectedBook:
        return cls(
            PortfolioBook.seeded(
                Decimal(document["cash"]),
                [
                    BookPosition(
                        position["isin"],
                        int(position["quantity"]),
                        Decimal(position["cost_basis"]),
                    )
                    for position in document["positions"]
                ],
            )
        )


class _PaperBroker(_AccountingBroker):
    """The backtest's accounting broker over the paper ``SimBroker``, driven through the live path.

    The engine sees the ``Broker`` it always saw; underneath, ``place`` goes through
    ``StagingCoordinator.stage`` (kill switch first, then the ``SimBroker``, then the order
    journal) and ``execute_session`` through ``StagingCoordinator.execute`` (fills posted to the
    order journal and to the paper book's accounting book), followed at once by a
    ``Reconciler`` pass of that book against the ``SimBroker`` — the same three ``execution``
    objects a real-money loop runs (M15.3, invariant #5).
    """

    def __init__(
        self,
        sim: SimBroker,
        book: PortfolioBook,
        *,
        clock: Clock,
        corporate_actions: BookActionSource | None,
        kill_switch: KillSwitch,
        alerter: Alerter,
        book_id: str,
        sleeve: str,
    ) -> None:
        # Checked before the accounting wrapper exists, so no half-built broker ever holds it.
        self._paper_sim = require_paper_broker(sim)
        super().__init__(sim, book, corporate_actions=corporate_actions)
        if self._interest is not None:
            # The ratified book earns no interest on idle cash and execute_session below has no
            # step for it; refusing beats a book that silently differs from its backtest.
            raise PaperSessionError("the paper session does not model interest on idle cash")
        self._clock = clock
        self._book_id = book_id
        self._sleeve = sleeve
        self.expected = _ExpectedBook(book)
        self._orders = InMemoryOrderJournal()
        for order in _rehydrated(self._paper_sim, book_id, sleeve):
            self._orders.record_staged(order)
        self._coordinator = StagingCoordinator(
            broker=self._paper_sim,
            kill_switch=kill_switch,
            journal=self._orders,
            book=self.expected,
            clock=clock,
        )
        self._reconciler = Reconciler(
            broker=self._paper_sim,
            book=self.expected,
            kill_switch=kill_switch,
            alerter=alerter,
            clock=clock,
        )
        self._placed: list[tuple[date, Order]] = []
        self.executed: tuple[StagedOrder, ...] = ()
        self.recon: ReconResult | None = None

    def execute_session(self, session: date) -> tuple[Order, ...]:
        """Book the session's corporate actions, fill through the staging step, then reconcile."""
        if self._actions is not None:
            self._actions.apply(session, sim=self._sim, book=self._book)
        self.executed = self._coordinator.execute(session)
        for done in self.executed:
            if done.fill is not None:
                self.fills.append(done.fill)
                self.total_charges += done.fill.cost.total
        # Before the policy is asked: a break trips the switch, so nothing is staged on it.
        self.recon = self._reconciler.reconcile(session)
        return tuple(self._sim.order(done.broker_order_id) for done in self.executed)

    def place(self, request: OrderRequest) -> Order:
        day = self._clock.today()
        seq = sum(1 for placed_on, _ in self._placed if placed_on == day)
        staged = self._coordinator.stage(
            request,
            case_id=self._book_id,
            sleeve=self._sleeve,
            uid=_order_uid(self._book_id, day, seq),
        )
        order = self._sim.order(staged.broker_order_id)
        self._placed.append((day, order))
        return order

    def placed_on(self, session: date) -> tuple[OrderRequest, ...]:
        """The requests placed while ``session`` was being decided, in placement order."""
        return tuple(order.request for day, order in self._placed if day == session)

    def staged_ids(self, session: date) -> tuple[str, ...]:
        """The broker order ids the staging step placed while ``session`` was decided."""
        return tuple(order.order_id for day, order in self._placed if day == session)


def _order_uid(book_id: str, decided: date, seq: int) -> str:
    """The staging layer's id for the ``seq``-th order the book placed deciding ``decided``."""
    return f"{book_id}:{decided.isoformat()}:{seq:03d}"


def _rehydrated(sim: SimBroker, book_id: str, sleeve: str) -> list[StagedOrder]:
    """The staging records of the orders still staged on a restored broker, as staged.

    The order journal of a paper run lives as long as the run; what survives between runs is the
    broker's staged orders (``SimBrokerState.staged``, in placement order). This rebuilds the
    records the staging step wrote for them — same uid, same decision-date stamp — so ``execute``
    can post each fill back to the order it belongs to.
    """
    records: list[StagedOrder] = []
    seen: dict[date, int] = {}
    for order in sim.export_state().staged:
        seq = seen.get(order.decision_date, 0)
        seen[order.decision_date] = seq + 1
        records.append(
            StagedOrder(
                order_uid=_order_uid(book_id, order.decision_date, seq),
                case_id=book_id,
                isin=order.request.isin,
                side=order.request.side,
                quantity=order.request.quantity,
                exchange=order.request.exchange,
                sleeve=sleeve,
                broker_order_id=order.order_id,
                staged_at=FrozenClock(order.decision_date).now(),
                staged_for_date=order.target_session,
            )
        )
    return records


class _Held:
    """``held()`` for the market and the marks, bound to the broker once it exists."""

    __slots__ = ("sim",)

    def __init__(self) -> None:
        self.sim: SimBroker | None = None

    def __call__(self) -> list[str]:
        if self.sim is None:
            return []
        held = {holding.isin for holding in self.sim.holdings()}
        return sorted(held | {position.isin for position in self.sim.positions()})


def _entries_digest(entries: Sequence[JournalEntry]) -> str:
    return digest_of(canonical_bytes([entry.model_dump(mode="json") for entry in entries]))


def _restore_book(
    spec: PaperBookSpec,
    world: PaperWorld,
    last: PaperSessionRecord | None,
    trading_date: date,
    clock: FrozenClock,
    held: _Held,
) -> SimBroker:
    """The paper broker as the last decided session left it, or a fresh one on the first session.

    Restores the persisted ``SimBrokerState`` and checks it reproduces the recorded digest, then
    lapses every order still staged for a session before ``trading_date`` — a session the book did
    not decide, so its orders never reached a market.
    """
    if last is None:
        first = trading_date
        state = None
    else:
        state = last.broker_state()
        if _state_digest(state) != last.book_digest:
            raise PaperBookDivergenceError(
                f"{spec.book_id}: the state recorded for {last.trading_date.isoformat()} does not "
                f"reproduce its digest {last.book_digest}; refusing to trade on an altered book "
                "(see ops/runbooks/daily-eod.md)"
            )
        traded = [lot[1] for lot in state.pending] + [r[1] for r in state.receivables]
        first = min([last.trading_date, *traded])
    market = world.market(first=first, through=trading_date, held=held)
    cost_model = CostModel(load_rate_card(), account_state=_ACCOUNT_STATE)
    if state is None:
        sim = SimBroker(
            clock=clock, cost_model=cost_model, market=market, opening_cash=spec.opening_cash
        )
    else:
        sim = SimBroker.restore(state, clock=clock, cost_model=cost_model, market=market)
        for order in state.staged:
            if order.target_session < trading_date:
                sim.cancel(order.order_id)
                _LOG.info(
                    "paper_session.order_lapsed",
                    book=spec.book_id,
                    order_id=order.order_id,
                    isin=order.request.isin,
                    target_session=order.target_session.isoformat(),
                    reason="the book did not decide that session; the order never filled",
                )
    held.sim = require_paper_broker(sim)
    return sim


def _mirror(sim: SimBroker) -> PortfolioBook:
    """An accounting book seeded from the broker — only for a book with none persisted.

    The paper book's accounting book is restored from the last session's ``expected_book``; a row
    written before M15.3 has none, and this seeds it once from the broker (the book is then
    carried forward on its own). Never used when a persisted one exists.
    """
    lots: dict[str, tuple[int, Decimal]] = {}
    for holding in sim.holdings():
        lots[holding.isin] = (holding.quantity, holding.average_price * holding.quantity)
    for position in sim.positions():
        quantity, cost = lots.get(position.isin, (0, Decimal(0)))
        lots[position.isin] = (
            quantity + position.quantity,
            cost + position.average_price * position.quantity,
        )
    return PortfolioBook.seeded(
        sim.margins().cash_value,
        [BookPosition(isin, quantity, cost) for isin, (quantity, cost) in sorted(lots.items())],
    )


# ── corporate actions: booked once, on the first session they are known ──────────────────────────


def action_identity(action: BookAction) -> str:
    """What a corporate action *is* — stable across corrections, re-sourcing and code changes.

    Built from fields that name the event and nothing else: the kind of action (a fixed tag, not
    the class name), its ISIN and ex-date,
    and for a share rescale its kind (a split and a bonus on one ex-date are two events). Never
    its terms (a corrected ratio or amount is the same event with different terms — see
    :func:`action_terms`), never its provenance (an ``IMPLIED`` split replaced by its ``FEED`` row
    is the same split) and never the dataclass as a whole, so a field added to the class later
    cannot re-key every action the book has seen and book it a second time.
    """
    base = f"{_tag_of(action)}:{action.isin}:{action.ex_date.isoformat()}"
    if isinstance(action, ShareRescale):
        return f"{base}:{action.kind.value}"
    return base


def _tag_of(action: BookAction) -> str:
    """A fixed name per kind of action — not the class name, which a subclass or rename changes."""
    if isinstance(action, CashDividend):
        return "DIVIDEND"
    if isinstance(action, ShareRescale):
        return "RESCALE"
    if isinstance(action, IsinReissue):
        return "REISSUE"
    if isinstance(action, ShareSwap):
        return "SWAP"
    if isinstance(action, CashExit):
        return "EXIT"
    return "UNMODELLED"


def _terms_of(action: BookAction) -> dict[str, str]:
    """The economic terms of an action, by an explicit allow-list per type (see action_terms)."""
    if isinstance(action, CashDividend):
        return {"per_share": str(action.per_share.normalize())}
    if isinstance(action, ShareRescale):
        # The ratio, not the pair: 2:1 and 10:5 are one split, whoever reported it.
        return {"ratio": str((action.numerator / action.denominator).normalize())}
    if isinstance(action, IsinReissue):
        return {"from_isin": action.from_isin, "explained": str(action.explained)}
    if isinstance(action, ShareSwap):
        return {
            "surviving_isin": action.surviving_isin,
            "ratio": str((action.numerator / action.denominator).normalize()),
            "cash_per_share": str(action.cash_per_share.normalize()),
        }
    if isinstance(action, CashExit):
        return {"price": str(action.price.normalize())}
    return {"action_type": action.action_type}


def action_terms(action: BookAction) -> str:
    """A digest of what an action does to a book — the amount, the ratio, the counterparty.

    An explicit allow-list per type rather than the whole record: provenance (``source``,
    ``carried_from``), knowability (``knowable_date``, ``record_date``) and any field a later
    version adds are not terms, so they can neither make a re-sourced action look corrected nor a
    corrected one look new. Decimals are normalised, so ``2`` and ``2.0`` agree.
    """
    return digest_of(canonical_bytes(_terms_of(action)))[:16]


def _rescale_family(action: BookAction) -> str | None:
    """The kind-free identity of a rescale: an implied SPLIT re-reported as a BONUS is one event."""
    if isinstance(action, ShareRescale):
        return f"RESCALE:{action.isin}:{action.ex_date.isoformat()}"
    return None


def _source_of(action: BookAction) -> str:
    return action.source.value if isinstance(action, ShareRescale) else ""


@dataclass(slots=True)
class _ActionIndex:
    """Every corporate action this book has seen, from the summaries: all terms per identity."""

    #: identity -> every set of terms seen under it (booked, recorded or escalated).
    terms: dict[str, set[str]]
    #: identity -> whether the book held the name when any of its terms was seen.
    held: dict[str, bool]
    #: (identity, terms) the book escalated rather than booked.
    escalations: set[tuple[str, str]]
    #: kind-free rescale identity -> (terms, source) of each rescale seen under it.
    rescales: dict[str, list[tuple[str, str]]]
    #: (``RECON:<date>``, terms) of every reconciliation break the book recorded (M15.3).
    recon_breaks: set[tuple[str, str]] = field(default_factory=set)

    @classmethod
    def of(cls, summaries: Sequence[PaperSessionSummary]) -> _ActionIndex:
        index = cls({}, {}, set(), {})
        for summary in summaries:
            if summary.recon_break is not None:
                index.recon_breaks.add(summary.recon_break)
            for booked in summary.actions:
                index.terms.setdefault(booked.key, set()).add(booked.terms)
                index.held[booked.key] = index.held.get(booked.key, False) or booked.held
                if booked.status is ActionStatus.ESCALATED:
                    index.escalations.add((booked.key, booked.terms))
                if booked.key.startswith("RESCALE:"):
                    family = booked.key.rsplit(":", 1)[0]
                    index.rescales.setdefault(family, []).append((booked.terms, booked.source))
        return index

    def unresolved(self, resolved: frozenset[tuple[str, str]]) -> list[str]:
        """Every escalated (key, terms) the owner has not resolved, as ``key@terms``."""
        return sorted(f"{key}@{terms}" for key, terms in self.escalations - resolved)

    def unresolved_recon(self, resolved: frozenset[tuple[str, str]]) -> list[str]:
        """Every reconciliation break the owner has not resolved, as ``RECON:<date>@terms``."""
        return sorted(f"{key}@{terms}" for key, terms in self.recon_breaks - resolved)


@dataclass(frozen=True, slots=True)
class _Sorted:
    """The known actions of one session, sorted by what the book must do with each."""

    on_time: tuple[BookAction, ...]
    late: tuple[BookAction, ...]
    #: Seen identity, unseen terms: a correction. (action, whether the book held the name.)
    changed: tuple[tuple[BookAction, bool], ...]
    #: A new rescale identity whose ratio matches one already seen under the other kind, with
    #: neither side implied: two feed records of one split, or a split and a bonus — not decidable.
    ambiguous: tuple[BookAction, ...]
    #: Seen before with these terms (or an implied rescale's twin): nothing to do.
    unchanged: int


_IMPLIED = RescaleSource.IMPLIED.value


def _sort_actions(
    actions: Iterable[BookAction], index: _ActionIndex, last_decided: date
) -> _Sorted:
    """Sort the session's known actions against everything the book has seen.

    An identity seen before is unchanged when its terms are among the terms seen under it, and a
    correction otherwise. A new identity is booked — every distinct set of terms under it, so two
    dividends a company declares for one ex-date are both credited — unless it is a rescale whose
    ratio the book already saw under the other kind on that name and ex-date: then it is the same
    event when one of the two was implied (L2's inferred row and the feed's), and undecidable when
    neither was.
    """
    on_time: list[BookAction] = []
    late: list[BookAction] = []
    changed: list[tuple[BookAction, bool]] = []
    ambiguous: list[BookAction] = []
    unchanged = 0
    batch: set[tuple[str, str]] = set()
    for action in actions:
        key, terms = action_identity(action), action_terms(action)
        seen = index.terms.get(key)
        if seen is not None:
            if terms in seen:
                unchanged += 1
            else:
                changed.append((action, index.held.get(key, False)))
            continue
        family = _rescale_family(action)
        twins = [src for t, src in index.rescales.get(family, []) if t == terms] if family else []
        if twins:
            if _IMPLIED in twins or _source_of(action) == _IMPLIED:
                unchanged += 1
            else:
                ambiguous.append(action)
            continue
        if (key, terms) in batch:
            unchanged += 1  # the very same record twice in one read
            continue
        batch.add((key, terms))
        (late if action.ex_date <= last_decided else on_time).append(action)
    return _Sorted(tuple(on_time), tuple(late), tuple(changed), tuple(ambiguous), unchanged)


def _held_in(state: SimBrokerState, isin: str) -> int:
    """Shares of ``isin`` in a persisted state: settled holdings plus pending buys."""
    settled = sum(quantity for held, _, quantity, _ in state.holdings if held == isin)
    pending = sum(quantity for held, _, _, _, quantity, _ in state.pending if held == isin)
    return settled + pending


def _entitled_entering(store: PaperSessionStore, spec: PaperBookSpec, action: BookAction) -> int:
    """Shares of the action's name the book held entering its ex-date — the entitlement.

    The state the last decided session before the ex-date persisted: orders staged then fill on or
    after the ex-date (or lapse), so they are not entitled. Not what the book holds *now* — a name
    sold since the ex-date was still entitled to the action, and whether the book held it then is
    what decides if a late, changed or ambiguous action concerns the book at all.
    """
    entering = store.latest_completed(spec.book_id, before=action.ex_date)
    return 0 if entering is None else _held_in(entering.broker_state(), action.isin)


def _action_entry(
    action: BookAction,
    decision: Decision,
    rationale: str,
    payload: Mapping[str, str],
    trading_date: date,
    spec: PaperBookSpec,
    clock: Clock,
) -> JournalEntry:
    _LOG.warning(
        "paper_session.corporate_action",
        book=spec.book_id,
        action=action_identity(action),
        decision=decision.value,
        payload_event=payload.get("event", ""),
    )
    return JournalEntry(
        ts=clock.now(),
        trading_date=trading_date,
        actor=Actor.SYSTEM,
        decision=decision,
        isin=action.isin,
        sleeve=spec.parameters.sleeve,
        rationale=rationale,
        payload={**payload, **_tag(spec)},
    )


def _changed_terms(
    changed: Sequence[tuple[BookAction, bool]],
    ambiguous: Sequence[BookAction],
    store: PaperSessionStore,
    trading_date: date,
    spec: PaperBookSpec,
    clock: Clock,
) -> tuple[list[JournalEntry], list[BookedAction]]:
    """Actions the book must not book mechanically: corrections, and unpaired rescale twins.

    A correction (a seen identity with terms never seen under it): the book already acted on the
    earlier terms, or had nothing to act on. If it held the name, the difference is for the owner
    to judge — journaled ``ESCALATE`` and never credited or rescaled again, and the book is blocked
    until that (key, terms) is resolved; if it did not, the new terms are recorded silently. An
    ambiguous rescale is handled the same way. "Held" is the entitlement entering the ex-date
    (:func:`_entitled_entering`), never what the book holds now: a name sold since the ex-date was
    still moved by the action. A correction is also escalated when the book recorded the name as
    held when it first booked the action.
    """
    entries: list[JournalEntry] = []
    seen: list[BookedAction] = []
    flagged = [
        (action, booked_held or _entitled_entering(store, spec, action) > 0, CHANGED_ACTION_EVENT)
        for action, booked_held in changed
    ] + [
        (action, _entitled_entering(store, spec, action) > 0, AMBIGUOUS_ACTION_EVENT)
        for action in ambiguous
    ]
    for action, held, event in flagged:
        key, terms = action_identity(action), action_terms(action)
        source = _source_of(action)
        if not held:
            seen.append(BookedAction(key, terms, False, ActionStatus.BOOKED, source))
            continue
        why = (
            f"{key} was already booked with other terms; the store now also reports terms "
            f"{terms} ({_terms_of(action)})"
            if event == CHANGED_ACTION_EVENT
            else f"{key} matches the ratio of a rescale already booked on this name and ex-date "
            "under the other kind, and neither record is implied, so it is either the same split "
            "reported twice or a second action"
        )
        entries.append(
            _action_entry(
                action,
                Decision.ESCALATE,
                f"not booked: {why}. The book is not credited or rescaled for it — owner review, "
                "and the book trades no more until this (key, terms) is resolved",
                {"event": event, "action": key, "terms": terms},
                trading_date,
                spec,
                clock,
            )
        )
        seen.append(BookedAction(key, terms, True, ActionStatus.ESCALATED, source))
    return entries, seen


def _book_late(
    late: Sequence[BookAction],
    sim: SimBroker,
    book: PortfolioBook,
    store: PaperSessionStore,
    summaries: Sequence[PaperSessionSummary],
    trading_date: date,
    spec: PaperBookSpec,
    clock: Clock,
) -> tuple[list[JournalEntry], list[BookedAction]]:
    """Book corporate actions learnt after the book decided past their ex-date, on this session.

    Entitlement is the book as it stood entering the ex-date — the state the last decided session
    before it persisted (orders staged then fill on or after the ex-date, or lapse, so they are not
    entitled). A dividend is credited now at that entitlement. A split or bonus is applied now when
    the book has not traded the name since, so the entitled shares are exactly the ones still held;
    otherwise, and for any other kind of action on a held name, the mechanical booking is not safe
    and the action is escalated to the owner instead. Each one is journaled; none rewrites the past.
    What is booked is booked on both sides — the ``SimBroker`` and the paper book's accounting
    ``book`` — so the session's reconciliation sees them agree.
    """
    entries: list[JournalEntry] = []
    seen: list[BookedAction] = []
    for action in late:
        key, terms = action_identity(action), action_terms(action)
        entering = store.latest_completed(spec.book_id, before=action.ex_date)
        entitled = _entitled_entering(store, spec, action)
        if entering is None or entitled == 0:
            seen.append(BookedAction(key, terms, False, ActionStatus.BOOKED, _source_of(action)))
            continue
        traded_since = any(
            action.isin in summary.traded
            for summary in summaries
            if summary.trading_date >= entering.trading_date
        )
        what = (
            f"{type(action).__name__} on {action.isin} ex {action.ex_date.isoformat()}, learnt "
            f"{trading_date.isoformat()} after the book had decided past its ex-date"
        )
        payload = {
            "event": LATE_ACTION_EVENT,
            "action": key,
            "terms": terms,
            "ex_date": action.ex_date.isoformat(),
            "entitled": str(entitled),
        }
        status = ActionStatus.BOOKED
        if isinstance(action, CashDividend):
            amount = action.per_share * entitled
            description = (
                f"LATE DIVIDEND {entitled} x {action.per_share} ex {action.ex_date.isoformat()}"
            )
            sim.credit_corporate_cash(trading_date, action.isin, amount, description)
            book.credit_late_dividend(trading_date, action.isin, amount)
            decision = Decision.HOLD
            rationale = f"booked late: {what}; credited {entitled} x {action.per_share} = {amount}"
            payload["amount"] = str(amount)
        elif (
            isinstance(action, ShareRescale)
            and not traded_since
            and sim.held_quantity(action.isin) == entitled
        ):
            old, new = sim.apply_share_rescale(
                action.isin,
                numerator=action.numerator,
                denominator=action.denominator,
                ex_date=trading_date,
            )
            _rescale_book(book, action)
            decision = Decision.HOLD
            rationale = (
                f"booked late: {what}; {action.kind.value} {action.numerator}:"
                f"{action.denominator} rescaled {old} shares to {new}"
            )
        else:
            decision, status = Decision.ESCALATE, ActionStatus.ESCALATED
            rationale = (
                f"not booked: {what}; the book has traded the name since or the action is not a "
                "dividend or a split/bonus, so it cannot be booked mechanically — owner review, "
                "and the book trades no more until resolved"
            )
        entries.append(
            _action_entry(action, decision, rationale, payload, trading_date, spec, clock)
        )
        seen.append(BookedAction(key, terms, True, status, _source_of(action)))
    return entries, seen


def _rescale_book(book: PortfolioBook, action: ShareRescale) -> None:
    """A split or bonus on the accounting book, in the terms ``BookActionApplier`` uses."""
    if action.kind is RescaleKind.SPLIT:
        book.apply_split(
            action.isin,
            from_face_value=action.numerator,
            to_face_value=action.denominator,
            forfeit_fraction=True,
        )
    else:
        book.apply_bonus(
            action.isin,
            new_shares=action.numerator - action.denominator,
            held_shares=action.denominator,
            forfeit_fraction=True,
        )


# ── the policy, as the paper book drives it ──────────────────────────────────────────────────────


class _PaperMomentumData:
    """The world's momentum data with the paper book's rebalance rule in place of the calendar's."""

    __slots__ = ("_data", "_rebalance_on")

    def __init__(self, data: MomentumV2Data, *, rebalance_on: date | None) -> None:
        self._data = data
        self._rebalance_on = rebalance_on

    def is_rebalance(self, session: date) -> bool:
        return session == self._rebalance_on

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        return self._data.signal(as_of)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        return self._data.regime(as_of)


class _Capturing:
    """Delegates to the policy and keeps the evidence it decided on, for the journal snapshot."""

    __slots__ = ("_policy", "evidence")

    def __init__(self, policy: MomentumV2Policy) -> None:
        self._policy = policy
        self.evidence: EvidenceBundle | None = None

    def decide(self, ctx: SessionContext) -> SessionDecision:
        decision = self._policy.decide(ctx)
        self.evidence = decision.evidence
        return decision


#: Inputs whose absence on a decision day is red data, not a crash: the regime index without the
#: session's published level, and the investable universe without membership coverage.
_MISSING_INPUT: Final = (RegimeSourceError, IndexCoverageError)


@dataclass(frozen=True, slots=True)
class _InputGap:
    """Why this session's decision inputs are not all there, and which input is missing."""

    reason: str
    #: The ``sync_state``-style id of the missing input, when it is one — journaled as
    #: ``payload.missing_input`` so a red day can be grouped by cause without parsing prose.
    missing: str | None = None


def _input_gap(
    data: MomentumV2Data,
    policy: MomentumV2Policy,
    parameters: MomentumV2Parameters,
    trading_date: date,
    *,
    rebalance: bool,
) -> _InputGap | None:
    """Why the decision cannot be made on this session's inputs, or ``None`` when it can.

    Reads exactly what the policy is about to read, before it reads it: a rebalance with the regime
    filter needs the session's published NIFTY 50 TRI level (landed by ``tri_evening``, M13.7) —
    checked only on a rebalance, the one day the regime is read, so a TRI that is late on an
    ordinary day never turns that day red — and a rebalance or a redeploy needs a non-empty
    candidate set. An empty set on a rebalance would sell the whole book on missing data, so it is
    red rather than a decision.
    """
    if rebalance and parameters.regime_filter:
        try:
            data.regime(trading_date)
        except RegimeSourceError as error:
            # Any RegimeSourceError lands here — a missing level, a series too short for the
            # moving average, no series at all — so the reason quotes the source's own words
            # rather than asserting which one it was.
            return _InputGap(
                f"regime input unavailable ({REGIME_TRI_INPUT}, the session's published NIFTY 50 "
                f"TRI, landed same-evening by tri_evening, M13.7) for "
                f"{trading_date.isoformat()}: {error}; the rebalance waits for a session that "
                "has it",
                REGIME_TRI_INPUT,
            )
    try:
        needs_signal = rebalance or policy.pending is not None
        if needs_signal and not data.signal(trading_date).records:
            return _InputGap(f"no momentum candidates for {trading_date.isoformat()}")
    except _MISSING_INPUT as error:
        return _InputGap(f"decision input unavailable: {error}")
    return None


# ── one session ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PaperSessionResult:
    """What one invocation did: the verdict, the entries it journaled and the record it wrote."""

    trading_date: date
    verdict: RunVerdict
    reason: str
    entries: tuple[JournalEntry, ...] = ()
    record: PaperSessionRecord | None = None
    book: BookSnapshot | None = None
    #: The fills the staging step booked this session, in fill order (each carries its costs).
    fills: tuple[Fill, ...] = ()

    def journal_bytes(self) -> bytes:
        """The canonical bytes of the entries this invocation journaled."""
        return canonical_bytes([entry.model_dump(mode="json") for entry in self.entries])

    def book_bytes(self) -> bytes:
        """The canonical bytes of the persisted book state (empty when nothing was decided)."""
        if self.record is None or self.record.book_state is None:
            return b""
        return canonical_bytes(dict(self.record.book_state))


def run_paper_session(
    *,
    trading_date: date,
    spec: PaperBookSpec,
    world: PaperWorld,
    store: PaperSessionStore,
    journal: JournalSink,
    gate: Callable[[date], GreenLike],
    clock: Clock,
    kill_switch: KillSwitch,
    alerter: Alerter | None = None,
) -> PaperSessionResult:
    """Decide one session of the paper book, or record why not (see the module docstring).

    What it does: the trading-day check, the idempotency check, the data-red interlock, the kill
    switch, the book restore, the corporate actions known now and one ``ReplayEngine`` session —
    its orders staged, filled and reconciled through ``execution``'s staging coordinator and
    reconciler — then appends the session's entries to ``journal`` and its record to ``store``.
    What it assumes: ``journal`` and ``store`` share the caller's transaction, which the caller
    commits after this returns — so a crash leaves neither half — and ``clock`` is the run's clock,
    used only for the record's landing time. ``kill_switch`` is the paper book's own
    (:func:`paper_kill_switch`); a reconciliation break trips it before the transaction commits,
    so a crash after a break leaves the book halted, never trading.
    What it never does: take or build any broker but the paper ``SimBroker``, place an order on a
    red day or with the switch tripped, journal a still-red rerun twice, rewrite a past session, or
    touch one that moved the book.
    """
    log = _LOG.bind(book=spec.book_id, trading_date=trading_date.isoformat(), mode=PAPER_MODE)
    if not world.is_session(trading_date):
        log.info("paper_session.not_a_session")
        return PaperSessionResult(trading_date, RunVerdict.NOT_A_SESSION, "not a trading session")

    existing = store.get(spec.book_id, trading_date)
    if existing is not None and existing.outcome.moved_book:
        log.info("paper_session.already_decided", journal_digest=existing.journal_digest)
        return PaperSessionResult(
            trading_date, RunVerdict.ALREADY_DECIDED, "already decided", record=existing
        )

    newest = store.latest_completed(spec.book_id, before=date.max)
    if newest is not None and trading_date < newest.trading_date:
        raise PaperSessionError(
            f"{trading_date.isoformat()} is before {newest.trading_date.isoformat()}, the book's "
            "latest decided session; a paper book decides forward only (deciding an earlier date "
            "would fork it from a stale snapshot)"
        )

    # Frozen on the session date: every entry this session journals is stamped midnight IST of the
    # session, as in every replay (module docstring, "Journal timestamps").
    session_clock = FrozenClock(trading_date)
    red = _red_reason(gate, trading_date, world)
    if red is not None:
        return _skip(spec, trading_date, red, existing, store, journal, session_clock, clock)

    if kill_switch.is_tripped:
        # The staging step would refuse every order anyway; refusing the whole session here keeps
        # yesterday's staged orders from filling on a halted book (they lapse, as on a red day).
        return _skip(
            spec,
            trading_date,
            _halted_reason(kill_switch),
            existing,
            store,
            journal,
            session_clock,
            clock,
            event=KILL_SWITCH_EVENT,
            verdict=RunVerdict.HALTED,
        )

    summaries = store.summaries(spec.book_id, before=trading_date)
    decided = [s for s in summaries if s.outcome.moved_book]
    index = _ActionIndex.of(summaries)
    resolved = store.resolutions(spec.book_id)
    unresolved = index.unresolved(resolved)
    if unresolved:
        # An escalated corporate action means the book's state is in question; trading on it would
        # be trading on data the owner has not accepted — the same rule as red data (#10).
        reason = (
            "unresolved corporate-action escalation(s) on a held name: "
            + ", ".join(unresolved)
            + "; resolve per ops/runbooks/daily-eod.md before the book trades again"
        )
        return _skip(spec, trading_date, reason, existing, store, journal, session_clock, clock)
    breaks = index.unresolved_recon(resolved)
    if breaks:
        # The same rule for a reconciliation break: the book and its broker disagreed, and which
        # one is right is the owner's call, recorded as a resolution (M15.3).
        reason = (
            "unresolved reconciliation break(s): "
            + ", ".join(breaks)
            + "; resolve per ops/runbooks/daily-eod.md before the book trades again"
        )
        return _skip(
            spec,
            trading_date,
            reason,
            existing,
            store,
            journal,
            session_clock,
            clock,
            event=RECON_BREAK_EVENT,
        )

    last = store.latest_completed(spec.book_id, before=trading_date)
    rebalance = not any(
        summary.rebalanced
        and (summary.trading_date.year, summary.trading_date.month)
        == (trading_date.year, trading_date.month)
        for summary in decided
    )
    data = world.momentum_data(trading_date, spec.parameters)
    policy = MomentumV2Policy(
        _PaperMomentumData(data, rebalance_on=trading_date if rebalance else None),
        spec.parameters,
        order_caps=spec.rail_policy.rails,
    )
    carried = last.pending if last is not None else None
    policy.resume(carried)
    gap = _input_gap(data, policy, spec.parameters, trading_date, rebalance=rebalance)
    if gap is not None:
        return _skip(
            spec,
            trading_date,
            gap.reason,
            existing,
            store,
            journal,
            session_clock,
            clock,
            missing=gap.missing,
        )

    held = _Held()
    sim = _restore_book(spec, world, last, trading_date, session_clock, held)
    expected, seeded = _restore_expected(spec, last, sim)

    # Corporate actions known now. Nothing on or before the day the book opened can concern it (it
    # held nothing until that session's orders filled). Each is sorted against what the book has
    # already seen: new and on time, new and late, seen with the same terms, seen with new terms.
    source = world.corporate_actions()
    known: list[BookAction] = []
    if last is not None and source is not None:
        known = list(source.between(decided[0].trading_date, trading_date))
    sorted_ = _sort_actions(known, index, last.trading_date if last is not None else trading_date)
    changed_entries, changed_seen = _changed_terms(
        sorted_.changed, sorted_.ambiguous, store, trading_date, spec, session_clock
    )
    late_entries, late_seen = _book_late(
        sorted_.late, sim, expected, store, summaries, trading_date, spec, session_clock
    )
    on_time_seen = [
        BookedAction(
            action_identity(action),
            action_terms(action),
            held=sim.held_quantity(action.isin, bought_before=action.ex_date) > 0,
            status=ActionStatus.BOOKED,
            source=_source_of(action),
        )
        for action in sorted_.on_time
    ]
    on_time = list(sorted_.on_time)
    late = sorted_.late

    broker = _PaperBroker(
        sim,
        expected,
        clock=session_clock,
        corporate_actions=BookActionCalendar(on_time),
        kill_switch=kill_switch,
        alerter=LoggingAlerter() if alerter is None else alerter,
        book_id=spec.book_id,
        sleeve=spec.parameters.sleeve,
    )
    capturing = _Capturing(policy)
    engine = ReplayEngine(
        policy=capturing,
        broker=broker,
        clock=session_clock,
        sessions=(trading_date,),
        rails=RailGate(spec.rail_policy, world.marks(held)),
    )
    halted: TradingHaltedError | None = None
    result = None
    try:
        result = engine.run()
    except TradingHaltedError as error:
        halted = error
    recon = broker.recon
    if recon is None:  # the engine always fills before it decides; a skipped recon is a defect
        raise PaperSessionError(f"{trading_date.isoformat()}: the session was not reconciled")
    if halted is not None and recon.ok:
        raise halted  # tripped by something other than this session's reconciliation: surface it
    reconciled = ReconRecord.of(
        recon,
        staged=broker.staged_ids(trading_date),
        executed=tuple(done.broker_order_id for done in broker.executed),
        seeded=seeded,
    )
    recon_entry, recon_evidence = _recon_entry(
        reconciled, broker.expected, sim, trading_date, spec, session_clock
    )

    if result is None:
        # The break tripped the switch and the staging step refused the session's first order:
        # nothing was placed, and the decision the engine was making is not journaled as made.
        engine_entries: tuple[JournalEntry, ...] = ()
        pending = carried
        rebalanced = False
        risk_off = False
    else:
        engine_entries = tuple(_tagged(entry, spec) for entry in result.journal)
        pending = policy.pending
        rebalanced = rebalance
        risk_off = (
            rebalance and capturing.evidence is not None and regime_parked(capturing.evidence)
        )
    if not recon.ok:
        # A break day never counts as the month's rebalance and never advances the redeploy state,
        # whether the policy wanted orders (refused above) or wanted none: the decision was made on
        # a book recon said was wrong, so the next green session makes it again on a sound one.
        pending = carried
        rebalanced = False
        risk_off = False
    entries = (*changed_entries, *late_entries, *engine_entries, recon_entry)
    if result is not None and capturing.evidence is not None:
        journal.snapshot(capturing.evidence)
    for entry in entries:
        journal.append(entry, evidence=recon_evidence if entry is recon_entry else None)
    state = sim.export_state()
    outcome = SessionOutcome.COMPLETED if recon.ok else SessionOutcome.RECON_BREAK
    record = PaperSessionRecord(
        book_id=spec.book_id,
        trading_date=trading_date,
        outcome=outcome,
        reason=(
            _decided_reason(rebalance=rebalance, risk_off=risk_off)
            if recon.ok
            else f"reconciliation break: {'; '.join(reconciled.breaks)}"
        ),
        rebalanced=rebalanced,
        journal_digest=_entries_digest(entries),
        orders=broker.placed_on(trading_date),
        pending=pending,
        book_state={
            "broker": state.to_document(),
            "session_ledger": [
                {
                    "seq": str(line.seq),
                    "session": line.session.isoformat(),
                    "isin": line.isin,
                    "description": line.description,
                    "debit": str(line.debit),
                    "credit": str(line.credit),
                    "balance": str(line.balance),
                }
                for line in sim.ledger()
            ],
        },
        book_digest=_state_digest(state),
        actions=(*changed_seen, *late_seen, *on_time_seen),
        recon=reconciled,
        expected_book=broker.expected.to_document(),
    )
    store.record(record, recorded_at=clock.now())
    book = BookSnapshot.of(broker) if result is None else result.book
    log.info(
        "paper_session.decided" if recon.ok else "paper_session.recon_break",
        rebalance=rebalance,
        risk_off=risk_off,
        entries=len(entries),
        decisions=sorted({entry.decision.value for entry in entries}),
        orders=len(record.orders),
        corporate_actions=len(on_time),
        late_corporate_actions=len(late),
        changed_corporate_actions=len(sorted_.changed),
        ambiguous_corporate_actions=len(sorted_.ambiguous),
        unchanged_corporate_actions=sorted_.unchanged,
        redeploy_pending=record.pending is not None,
        recon=reconciled.status.value,
        recon_breaks=len(reconciled.breaks),
        expected_book_seeded=seeded,
        cash=str(book.cash),
        holdings=len(book.holdings),
        book_digest=record.book_digest,
    )
    return PaperSessionResult(
        trading_date,
        RunVerdict.DECIDED if recon.ok else RunVerdict.RECON_BREAK,
        record.reason,
        entries=entries,
        record=record,
        book=book,
        fills=tuple(broker.fills),
    )


def _restore_expected(
    spec: PaperBookSpec, last: PaperSessionRecord | None, sim: SimBroker
) -> tuple[PortfolioBook, bool]:
    """The paper book's accounting book as the last session left it, and whether it was seeded.

    Restored from the last session's ``expected_book``. The first session opens it with the
    book's capital. Two cases seed it from the restored broker instead — logged, and marked
    ``seeded`` on the session's reconciliation — and carry it on its own from then on: a last
    session recorded before M15.3, which persisted none; and a last session that broke
    reconciliation, which the session only reaches once the owner has resolved the break — the
    resolution is the owner's acceptance of the broker's side (ops/runbooks/daily-eod.md).
    """
    if last is None:
        return PortfolioBook.seeded(spec.opening_cash, ()), False
    if last.expected_book is not None and last.outcome is not SessionOutcome.RECON_BREAK:
        return _ExpectedBook.from_document(last.expected_book).book, False
    _LOG.warning(
        "paper_session.expected_book_seeded",
        book=spec.book_id,
        from_session=last.trading_date.isoformat(),
        reason=(
            "the break recorded on that session was resolved; the book is re-based on the broker"
            if last.outcome is SessionOutcome.RECON_BREAK
            else "the last session predates M15.3 and persisted no accounting book"
        ),
    )
    return _mirror(sim), True


def _recon_entry(
    reconciled: ReconRecord,
    expected: _ExpectedBook,
    sim: SimBroker,
    trading_date: date,
    spec: PaperBookSpec,
    clock: Clock,
) -> tuple[JournalEntry, EvidenceBundle]:
    """The session's reconciliation, journaled: a HEARTBEAT when clean, an ESCALATE on a break.

    The evidence bundle is both sides as compared — the accounting book's cash and share counts
    and the ``SimBroker``'s — so the entry shows what was checked, not only the verdict.
    """
    broker_quantities: dict[str, int] = {}
    for holding in sim.holdings():
        broker_quantities[holding.isin] = broker_quantities.get(holding.isin, 0) + holding.quantity
    for position in sim.positions():
        broker_quantities[position.isin] = (
            broker_quantities.get(position.isin, 0) + position.quantity
        )
    items: list[EvidenceItem] = [
        EvidenceItem(
            kind=EvidenceKind.POSITION,
            source=source,
            label="cash",
            as_of=trading_date,
            value=cash,
        )
        for source, cash in (
            ("paper_book", expected.cash),
            ("sim_broker", sim.margins().cash_value),
        )
    ]
    for source, quantities in (
        ("paper_book", expected.quantities()),
        ("sim_broker", broker_quantities),
    ):
        items.extend(
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source=source,
                label="quantity",
                isin=isin,
                as_of=trading_date,
                value=Decimal(quantity),
            )
            for isin, quantity in sorted(quantities.items())
        )
    evidence = EvidenceBundle(trading_date=trading_date, actor=Actor.SYSTEM, items=tuple(items))
    payload = {
        "event": RECON_EVENT if reconciled.status is ReconStatus.CLEAN else RECON_BREAK_EVENT,
        "recon": reconciled.status.value,
        "key": reconciled.key,
        "terms": reconciled.terms,
        "staged": ",".join(reconciled.staged),
        "executed": ",".join(reconciled.executed),
        "positions": str(len(expected.quantities())),
        "cash": str(expected.cash),
        **({"seeded": "true"} if reconciled.seeded else {}),
        **_tag(spec),
    }
    if reconciled.status is ReconStatus.CLEAN:
        decision = Decision.HEARTBEAT
        rationale = (
            f"reconciliation clean: the paper book and SimBroker agree on "
            f"{len(expected.quantities())} position(s) and cash {expected.cash}; staged "
            f"{len(reconciled.staged)} order(s), filled {len(reconciled.executed)}"
        )
    else:
        decision = Decision.ESCALATE
        payload["breaks"] = " | ".join(reconciled.breaks)
        rationale = (
            f"reconciliation break: {'; '.join(reconciled.breaks)}. The kill switch is tripped and "
            "nothing was staged; the book trades no more until the owner resolves "
            f"{reconciled.key}@{reconciled.terms} and resets the switch (ops/runbooks/daily-eod.md)"
        )
    entry = JournalEntry(
        ts=clock.now(),
        trading_date=trading_date,
        actor=Actor.SYSTEM,
        decision=decision,
        evidence_snapshot_ref=evidence.ref().ref,
        rationale=rationale,
        payload=payload,
    )
    return entry, evidence


def _halted_reason(kill_switch: KillSwitch) -> str:
    state = kill_switch.state
    source = "unknown" if state.source is None else state.source.value
    when = "" if state.tripped_at is None else f" at {state.tripped_at.isoformat()}"
    return (
        f"kill switch tripped by {source}{when}: {state.reason or 'no reason recorded'}; nothing "
        "staged — reset it per ops/runbooks/daily-eod.md once the cause is dealt with"
    )


def _decided_reason(*, rebalance: bool, risk_off: bool) -> str:
    """A ``COMPLETED`` row's reason: ``decided``, ``rebalance``, or ``rebalance_risk_off``.

    ``risk_off`` is a rebalance the regime filter parked in cash, named on the row so the book's
    being in cash is legible without following the evidence link (M14.4). The reason is prose for a
    reader; nothing keys on it — the rebalance rule reads ``rebalanced``.
    """
    if not rebalance:
        return "decided"
    return "rebalance_risk_off" if risk_off else "rebalance"


def _red_reason(
    gate: Callable[[date], GreenLike], trading_date: date, world: PaperWorld
) -> str | None:
    """Why the session is red, or ``None``. A status read that raised is red, never green."""
    try:
        verdict = gate(trading_date)
    except Exception as error:  # any failure to confirm green is red (invariant #10)
        # The type only: a driver's message can quote the connection string (invariant #13).
        return f"status check failed ({type(error).__name__}); trading refused"
    if not verdict:
        return verdict.reason or "status not green"
    if not world.prices_ready(trading_date):
        return f"status green but no NSE prices in L1 for {trading_date.isoformat()}"
    return None


def _skip(
    spec: PaperBookSpec,
    trading_date: date,
    reason: str,
    existing: PaperSessionRecord | None,
    store: PaperSessionStore,
    journal: JournalSink,
    session_clock: FrozenClock,
    clock: Clock,
    *,
    missing: str | None = None,
    event: str | None = None,
    verdict: RunVerdict = RunVerdict.SKIPPED_DATA_RED,
) -> PaperSessionResult:
    """Journal one ``SKIPPED_DATA_RED`` for the date (once) and place nothing (invariant #10).

    ``missing`` names the input that made the day red, when it is one input (the session's TRI
    level on a rebalance); it is journaled as ``payload.missing_input``. ``event`` names a refusal
    that is not about the data — a tripped kill switch, an unresolved reconciliation break — as
    ``payload.event``; ``verdict`` is what the run reports (the record is red either way).
    """
    log = _LOG.bind(book=spec.book_id, trading_date=trading_date.isoformat(), mode=PAPER_MODE)
    if existing is not None:
        log.warning("paper_session.still_red", reason=reason, first_reason=existing.reason)
        return PaperSessionResult(trading_date, RunVerdict.STILL_RED, reason, record=existing)
    entry = JournalEntry(
        ts=session_clock.now(),
        trading_date=trading_date,
        actor=Actor.SYSTEM,
        decision=Decision.SKIPPED_DATA_RED,
        rationale=reason,
        payload={
            **_tag(spec),
            "datasets": ",".join(spec.datasets),
            **({"missing_input": missing} if missing is not None else {}),
            **({"event": event} if event is not None else {}),
        },
    )
    journal.append(entry)
    record = PaperSessionRecord(
        book_id=spec.book_id,
        trading_date=trading_date,
        outcome=SessionOutcome.SKIPPED_DATA_RED,
        reason=reason,
        rebalanced=False,
        journal_digest=_entries_digest((entry,)),
    )
    store.record(record, recorded_at=clock.now())
    log.warning("paper_session.data_red", reason=reason, refusal=event or "data_red")
    return PaperSessionResult(trading_date, verdict, reason, entries=(entry,), record=record)


def _tag(spec: PaperBookSpec) -> dict[str, str]:
    return {"paper_book": spec.book_id, "mode": PAPER_MODE}


def _tagged(entry: JournalEntry, spec: PaperBookSpec) -> JournalEntry:
    """``entry`` as the paper book journals it: tagged with the book and the mode.

    The rail gate files a caseless order's block under ``BACKTEST_CASE_ID``, a placeholder that
    names no ``case_`` row — fine in an offline replay, refused by the live journal's foreign key.
    The paper book is not a case (D13 ratified a sleeve configuration, not a §5.1 case), so here
    the placeholder becomes what it stands for, *no case*, and the book is named in the payload.
    """
    case_id = None if entry.case_id == BACKTEST_CASE_ID else entry.case_id
    return entry.model_copy(update={"case_id": case_id, "payload": {**entry.payload, **_tag(spec)}})


# ── the production world: the holiday calendar and the lake ──────────────────────────────────────


@dataclass(slots=True)
class L1PaperWorld:
    """The paper book's market, read from the lake exactly as the momentum v2 backtests read it.

    The calendar is the checked-in NSE holiday calendar, not the dates on disk: today's session has
    no successor on disk, and an order staged on it must still target tomorrow. Bars, marks, the
    L2-adjusted signal closes, the investable universe (M9.3) and the published NIFTY 50 regime
    index come from ``backtest.run``'s readers; corporate actions from the store the job's own
    settings name. Everything is opened lazily, so a holiday or a red day opens nothing, and closed
    by :meth:`close`.
    """

    data_root: Path | None = None
    settings: Settings | None = None
    calendar: TradingCalendar | None = None
    universe: UniverseParameters = field(default_factory=UniverseParameters)
    adjusted: bool = True
    _reader: _L1Reader | None = None
    _service: QueryService | None = None
    _actions: BookActionSource | None = None

    def _calendar(self) -> TradingCalendar:
        if self.calendar is None:
            from dataplatform.ingest.calendar import trading_calendar

            self.calendar = trading_calendar()
        return self.calendar

    def _l1(self) -> _L1Reader:
        if self._reader is None:
            self._reader = _L1Reader(data_root=self.data_root)
        return self._reader

    def close(self) -> None:
        """Release the lake connections this world opened."""
        if self._service is not None:
            self._service.close()
            self._service = None
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def is_session(self, day: date) -> bool:
        return self._calendar().is_session(day)

    def sessions(self, start: date, end: date) -> Sequence[date]:
        return self._calendar().expected_sessions(start, end)

    def prices_ready(self, day: date) -> bool:
        return bool(self._l1().closes_on(day))

    def market(
        self, *, first: date, through: date, held: Callable[[], Iterable[str]]
    ) -> SessionMarket:
        calendar = self._calendar()
        end = min(through + _CALENDAR_HEADROOM, calendar.coverage_end)
        return _L1Market(self._l1(), calendar.expected_sessions(first, end), held=held)

    def marks(self, held: Callable[[], Iterable[str]]) -> Callable[[date], Mapping[str, Decimal]]:
        return _HoldingMarks(self._l1(), held)

    def momentum_data(self, day: date, parameters: MomentumV2Parameters) -> MomentumV2Data:
        return _LazyL1MomentumData(self, day, parameters)

    def corporate_actions(self) -> BookActionSource | None:
        if self._actions is None:
            from backtest.book_actions import load_store_book_actions

            self._actions = load_store_book_actions(
                data_root=self.data_root, settings=self.settings
            )
        return self._actions

    def _build_momentum(self, day: date, parameters: MomentumV2Parameters) -> _L1MomentumV2Data:
        reader = self._l1()
        signal_closes = None
        if self.adjusted:
            if self._service is None:
                from dataplatform.query.service import QueryService

                self._service = QueryService(data_root=self.data_root)
            signal_closes = _AdjustedCloseSource(self._service, reader)
        return _L1MomentumV2Data(
            reader,
            [day],
            _RegimeSource.published(
                through=day, ma_days=parameters.regime_ma_days, data_root=self.data_root
            ),
            signal_closes=signal_closes,
            universe_filter=_InvestableUniverse(reader, self.universe, data_root=self.data_root),
            lookback_sessions=reader.all_sessions(),
        )


class _LazyL1MomentumData:
    """``_L1MomentumV2Data`` for one session, built on first read.

    Building it computes the session's ranked candidate set (a year of look-back closes and the
    universe screen), so a heartbeat session — which reads no signal — never pays for it, and a
    missing regime series or universe coverage surfaces at the read, where the session turns it
    into a red day.
    """

    __slots__ = ("_built", "_day", "_parameters", "_world")

    def __init__(self, world: L1PaperWorld, day: date, parameters: MomentumV2Parameters) -> None:
        self._world = world
        self._day = day
        self._parameters = parameters
        self._built: _L1MomentumV2Data | None = None

    def _data(self) -> _L1MomentumV2Data:
        if self._built is None:
            self._built = self._world._build_momentum(self._day, self._parameters)
        return self._built

    def is_rebalance(self, session: date) -> bool:
        return self._data().is_rebalance(session)

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        return self._data().signal(as_of)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        return self._data().regime(as_of)


# ── the scheduler entry point ────────────────────────────────────────────────────────────────────


def run_paper_session_job(
    context: JobContext,
    *,
    world: PaperWorld | None = None,
    trading_date: date | None = None,
) -> PaperSessionResult | None:
    """The ``paper_session`` scheduler job: the owed session of the ratified paper book.

    What it does: unless ``Settings.paper_session_enabled`` is off (the default — module
    docstring), opens one Postgres transaction, runs :func:`run_paper_session` for
    ``trading_date`` — by default :func:`owed_session` at the context's clock, an explicit date,
    never the calendar day; an explicit one may be neither after the owed session nor before the
    book's latest decided session — with the ratified spec, the status interlock over the job's own
    database, the L1 world, the real append-only journal and ``paper_session``, the book's kill
    switch under ``Settings.data_root`` (:func:`paper_kill_switch`) and a logging recon alerter,
    and commits.
    What it assumes: the database is migrated through 0015 and the EOD pipeline has run for the
    owed session. ``world`` is injectable for a test; production passes nothing.
    What it never does: route to a real broker — there is no broker parameter anywhere on this
    path and ``Settings.broker_provider`` is never read — or commit a half-written session: an
    exception rolls the whole session back and the runner records the run FAILED.
    """
    settings = context.settings
    if not settings.paper_session_enabled:
        _LOG.warning(
            "paper_session.disabled",
            run_id=str(context.run_id),
            reason=(
                "PAPER_SESSION_ENABLED is off: the regime filter has no same-evening source for "
                "the session's published NIFTY 50 TRI (ops/runbooks/daily-eod.md)"
            ),
        )
        return None
    spec = ratified_paper_book()
    with ExitStack() as stack:
        if world is None:
            l1 = L1PaperWorld(data_root=settings.data_root, settings=settings)
            stack.callback(l1.close)
            world = l1
        due = owed_session(world, context.clock.now())
        if due is None:
            raise PaperSessionError(
                f"no trading session in the {_OWED_LOOKBACK_DAYS} days to "
                f"{context.clock.now().isoformat()}; the holiday calendar is wrong"
            )
        if trading_date is not None and trading_date > due:
            raise PaperSessionError(
                f"{trading_date.isoformat()} is after the owed session {due.isoformat()}: its EOD "
                "is not due yet, so deciding it would trade on data that does not exist"
            )
        # A date before the book's latest decided session is refused by run_paper_session itself.
        owed = trading_date if trading_date is not None else due
        conn = stack.enter_context(connection(settings))
        result = run_paper_session(
            trading_date=owed,
            spec=spec,
            world=world,
            store=PostgresPaperSessionStore(conn),
            journal=Journal(
                conn,
                clock=context.clock,
                evidence=EvidenceStore(settings.data_root / EVIDENCE_DIRNAME),
            ),
            gate=StatusApiGate(datasets=spec.datasets, clock=context.clock, settings=settings),
            clock=context.clock,
            kill_switch=paper_kill_switch(settings.data_root, spec.book_id, clock=context.clock),
            alerter=LoggingAlerter(),
        )
        conn.commit()
    _LOG.info(
        "paper_session.job_done",
        book=spec.book_id,
        trading_date=owed.isoformat(),
        verdict=result.verdict.value,
        reason=result.reason,
        run_id=str(context.run_id),
    )
    return result
