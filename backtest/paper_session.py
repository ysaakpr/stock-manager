"""M13.1 — the daily paper-trading session for the D13-ratified momentum v2 book.

HUMAN_DECISIONS D13 ratified a momentum configuration for **paper mode**
(:data:`~backtest.policies.momentum_v2.PAPER_RATIFIED_2026_09_06`) and D15 said to run it daily.
This module is that run: once per trading day, after the EOD data publishes, it decides one session
of the paper book and journals it. It is the replay engine run *forward* — the same
``ReplayEngine`` → ``RailGate`` (A8) → ``SimBroker`` path every backtest of the configuration went
through, one session at a time, so the paper book is the backtest's book continued past the end of
history rather than a second implementation of the strategy (§7, invariant #5).

**What one run does** (:func:`run_paper_session`), in order, stopping at the first step that says
stop:

1. **Trading-day check.** A date the holiday calendar does not call a session is not a decision
   day: nothing is journaled, nothing recorded.
2. **Idempotency.** A date already ``COMPLETED`` for the book is a no-op — a rerun never trades
   twice. A date recorded ``SKIPPED_DATA_RED`` is re-checked (the data may have healed), but a
   still-red rerun writes nothing new.
3. **The data-red interlock (invariant #10).** The status API is read first; a red verdict, a
   status read that failed, or a decision input that is missing (no L1 prices for the date, no
   published regime index level on a rebalance, no investable universe) journals one
   ``SKIPPED_DATA_RED`` entry and places no order. A failed status read is red, never green.
4. **Rebuild the paper book.** The broker is a fresh ``SimBroker`` — the paper book has no other
   state than the ``paper_session`` rows (migration 0012). Every calendar session from the book's
   first decided session up to yesterday is walked: corporate actions, settlement and fills run on
   every one; on a ``COMPLETED`` session the recorded orders are placed again; on any other session
   (red, missed) an order staged for it lapses unfilled. After each decided session the rebuilt
   book is checked byte-for-byte against the digest recorded when it was decided, so a book that no
   longer reproduces fails loud instead of trading on a different history than the one journaled.
5. **Decide.** ``ReplayEngine`` runs exactly one session — fill yesterday's orders, ask the policy,
   clear every order through A8, place what A8 allowed — and the entries it produced (BUY/SELL,
   RAIL_BLOCK, or the HEARTBEAT of a day with nothing to do: invariant #9) are appended to the
   journal and the session recorded ``COMPLETED``, in the caller's one transaction.

**Rebalance timing.** The ratified backtest rebalances on the first session of each month. A paper
book cannot rebalance on a day it was not allowed to decide, so here a rebalance is due on the
first session of the month *this book decides*: a red first session moves the rebalance to the next
green one instead of skipping the month. The first session the book ever decides rebalances too —
the book opens invested, not in cash until next month.

**Paper only, structurally.** Nothing here takes a broker. The one broker this module can build is
a ``SimBroker``, constructed inside :func:`_open_book` and checked by :func:`require_paper_broker`
before a single order reaches it; ``execution.kite_broker`` is never imported (a test pins both).
A real-money version is a separate ratification (AGENTIC_CONTEXT §3.2) and a separate job.

What it never does: read a wall clock for a decision (the engine's ``FrozenClock`` is set to the
session — B10), trade on red data, journal a decision it did not make, or update a ``COMPLETED``
session. Money is ``Decimal`` throughout; identity is the ISIN.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from psycopg.types.json import Json

from analyst.journal.evidence import (
    EvidenceBundle,
    EvidenceRef,
    canonical_bytes,
    digest_of,
)
from analyst.journal.models import Actor, Decision, JournalEntry
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionSource
from backtest.policies.momentum_v2 import (
    PAPER_RATIFIED_2026_09_06,
    MomentumV2Data,
    MomentumV2Parameters,
    MomentumV2Policy,
    MomentumV2Record,
    RegimeReading,
)
from backtest.rails import BacktestRailPolicy, RailGate, ratified_backtest_rail_policy
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
    _held_by,
    _HoldingMarks,
    _InvestableUniverse,
    _L1Market,
    _L1MomentumV2Data,
    _L1Reader,
    _RegimeSource,
)
from dataplatform.clock import Clock, FrozenClock
from dataplatform.config import Settings
from dataplatform.logging import get_logger
from dataplatform.query.pit import Dataset
from dataplatform.store.db import Connection
from execution.broker import Exchange, Order, OrderRequest, OrderStatus, OrderType, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import SessionMarket, SimBroker

if TYPE_CHECKING:
    from analyst.monitor.interlock import GreenLike
    from dataplatform.ingest.calendar import TradingCalendar
    from dataplatform.query.service import QueryService
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "PAPER_BOOK_ID",
    "PAPER_DATASETS",
    "PAPER_MODE",
    "PAPER_OPENING_CASH",
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
    "PaperWorld",
    "PostgresPaperSessionStore",
    "RecordingJournal",
    "RunVerdict",
    "SessionOutcome",
    "StatusGate",
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

#: The ``sync_state`` sources this decision reads, which must be ``PUBLISHED`` and quality-green for
#: the session (invariant #10). Momentum v2 ranks NSE equities on NSE closes and fills on NSE bars,
#: so the NSE bhavcopy is its whole price input; the regime index and the investable universe are
#: checked as decision inputs on the days they are read (see the module docstring).
PAPER_DATASETS: Final[tuple[str, ...]] = ("nse_bhavcopy",)

#: Stamped on every journal entry and in the payload, so a paper decision is never mistaken for a
#: real-money one and every entry names the book it belongs to.
PAPER_MODE: Final = "PAPER"

_BOOK_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
#: How far past the session the fill calendar is extended: an order staged on the session needs the
#: next session to target, and the longest Indian market closure runs to a few days.
_CALENDAR_HEADROOM = timedelta(days=45)


# ── errors ───────────────────────────────────────────────────────────────────────────────────────


class PaperSessionError(Exception):
    """The paper session could not proceed. Raised, so the scheduler records the run FAILED."""


class PaperModeViolationError(PaperSessionError):
    """Something other than the paper ``SimBroker`` reached the paper session's order path."""


class PaperBookDivergenceError(PaperSessionError):
    """The rebuilt paper book does not reproduce the book recorded when a session was decided.

    Means an input the rebuild reads — a raw bar a fill was priced on, a corporate action — changed
    after the session was decided. The book is not silently re-based on the new history: an operator
    must look (ops/runbooks/daily-eod.md).
    """


# ── the book, its outcomes ───────────────────────────────────────────────────────────────────────


class SessionOutcome(StrEnum):
    """What a recorded session concluded (``paper_session.outcome``)."""

    COMPLETED = "COMPLETED"
    """Decided: the journal holds the decision and the orders the paper broker was given."""

    SKIPPED_DATA_RED = "SKIPPED_DATA_RED"
    """The interlock refused the day; one ``SKIPPED_DATA_RED`` entry, no order (invariant #10)."""


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


@dataclass(frozen=True, slots=True)
class PaperSessionRecord:
    """One ``paper_session`` row: what a session concluded and what the next one resumes from.

    ``orders`` are the requests placed on the paper broker after A8 cleared them, in placement
    order; ``pending`` the momentum policy's redeploy target carried to the next session;
    ``book_digest`` the sha256 of the book after the session placed its orders — what the next
    run's rebuild must reproduce. A red record holds none of the three.
    """

    book_id: str
    trading_date: date
    outcome: SessionOutcome
    reason: str
    rebalanced: bool
    journal_digest: str
    orders: tuple[OrderRequest, ...] = ()
    pending: Mapping[str, Decimal] | None = None
    book_digest: str | None = None

    def __post_init__(self) -> None:
        if self.outcome is SessionOutcome.SKIPPED_DATA_RED and (
            self.orders or self.pending is not None or self.rebalanced or self.book_digest
        ):
            raise ValueError("a SKIPPED_DATA_RED session places nothing and carries no book state")
        if self.outcome is SessionOutcome.COMPLETED and self.book_digest is None:
            raise ValueError("a COMPLETED session must record the book digest it ended on")

    def orders_document(self) -> list[dict[str, str | None]]:
        """``orders`` as JSON-safe strings — Decimals never travel as JSON numbers."""
        return [_order_document(order) for order in self.orders]

    def pending_document(self) -> dict[str, str] | None:
        """``pending`` as ISIN -> weight strings, or ``None``."""
        if self.pending is None:
            return None
        return {isin: str(weight) for isin, weight in sorted(self.pending.items())}

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
        book_digest: str | None,
    ) -> PaperSessionRecord:
        """The record a stored row describes (the inverse of the two ``*_document`` methods)."""
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
            book_digest=book_digest,
        )


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


# ── persistence: the session ledger ──────────────────────────────────────────────────────────────


class PaperSessionStore(Protocol):
    """Where a paper book's session records live — Postgres in production, a dict in a test."""

    def get(self, book_id: str, trading_date: date) -> PaperSessionRecord | None:
        """The record for one date, or ``None``."""

    def history(self, book_id: str, *, before: date) -> tuple[PaperSessionRecord, ...]:
        """Every record strictly before ``before``, oldest first."""

    def record(self, record: PaperSessionRecord, *, recorded_at: datetime) -> None:
        """Write a record. A COMPLETED date is final; only a red record may be superseded."""


class InMemoryPaperSessionStore:
    """A ``PaperSessionStore`` over a dict — the same contract as the Postgres one, no database."""

    __slots__ = ("_rows",)

    def __init__(self) -> None:
        self._rows: dict[tuple[str, date], PaperSessionRecord] = {}

    def get(self, book_id: str, trading_date: date) -> PaperSessionRecord | None:
        return self._rows.get((book_id, trading_date))

    def history(self, book_id: str, *, before: date) -> tuple[PaperSessionRecord, ...]:
        return tuple(
            self._rows[key] for key in sorted(self._rows) if key[0] == book_id and key[1] < before
        )

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
    "book_digest"
)


class PostgresPaperSessionStore:
    """``paper_session`` (migration 0012). Nothing here commits — the job owns the transaction.

    ``record`` inserts, or replaces a ``SKIPPED_DATA_RED`` row for the same date; it never touches a
    ``COMPLETED`` row, and a write that would is an error rather than a silent no-op, because the
    caller is about to commit journal entries that claim the session was decided.
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

    def history(self, book_id: str, *, before: date) -> tuple[PaperSessionRecord, ...]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM paper_session WHERE book_id = %s AND trading_date < %s "
            "ORDER BY trading_date",
            (book_id, before),
        ).fetchall()
        return tuple(_record_of(row) for row in rows)

    def record(self, record: PaperSessionRecord, *, recorded_at: datetime) -> None:
        row = self._conn.execute(
            "INSERT INTO paper_session (book_id, trading_date, outcome, reason, rebalanced, "
            "orders, pending, journal_digest, book_digest, recorded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (book_id, trading_date) DO UPDATE SET "
            "outcome = EXCLUDED.outcome, reason = EXCLUDED.reason, "
            "rebalanced = EXCLUDED.rebalanced, orders = EXCLUDED.orders, "
            "pending = EXCLUDED.pending, journal_digest = EXCLUDED.journal_digest, "
            "book_digest = EXCLUDED.book_digest, recorded_at = EXCLUDED.recorded_at "
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
                record.book_digest,
                recorded_at,
            ),
        ).fetchone()
        if row is None:
            raise PaperSessionError(
                f"{record.book_id} {record.trading_date.isoformat()} is already COMPLETED; a "
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
        book_digest,
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
        book_digest=book_digest,
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
        """Splits, bonuses, dividends and exits applied to the book on their ex-dates."""


# ── the interlock ────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StatusGate:
    """The production ``GreenGate``: ``dataplatform.status.is_green`` over this book's datasets.

    Takes the job's own settings and clock rather than reading the process defaults, so a scheduler
    run against a given database checks that database's status. Does not catch a failure — the
    session turns one into a red day (:func:`run_paper_session`), never into a green one.
    """

    datasets: Sequence[str]
    settings: Settings | None = None
    clock: Clock | None = None

    def __call__(self, trading_date: date) -> GreenLike:
        from dataplatform.status import is_green

        return is_green(trading_date, self.datasets, settings=self.settings, clock=self.clock)


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


class _PaperBroker(_AccountingBroker):
    """The backtest's accounting broker over the paper ``SimBroker``, remembering what it placed.

    The placements are what a decided session records (and what the next run's rebuild places
    again); ``lapse`` cancels what was staged for a session the book did not decide.
    """

    def __init__(
        self,
        sim: SimBroker,
        book: PortfolioBook,
        *,
        clock: Clock,
        corporate_actions: BookActionSource | None,
    ) -> None:
        # Checked before the accounting wrapper exists, so no half-built broker ever holds it.
        self._paper_sim = require_paper_broker(sim)
        super().__init__(sim, book, corporate_actions=corporate_actions)
        self._clock = clock
        self._placed: list[tuple[date, Order]] = []

    def place(self, request: OrderRequest) -> Order:
        order = super().place(request)
        self._placed.append((self._clock.today(), order))
        return order

    def placed_on(self, session: date) -> tuple[OrderRequest, ...]:
        """The requests placed while ``session`` was being decided, in placement order."""
        return tuple(order.request for day, order in self._placed if day == session)

    def lapse(self, session: date) -> int:
        """Cancel every order still staged to fill on ``session``; return how many."""
        lapsed = 0
        for _, order in self._placed:
            if order.target_session != session:
                continue
            if self._paper_sim.order(order.order_id).status is OrderStatus.STAGED:
                self.cancel(order.order_id)
                lapsed += 1
        return lapsed


def _book_digest(broker: _PaperBroker) -> str:
    return digest_of(BookSnapshot.of(broker).canonical_bytes())


def _entries_digest(entries: Sequence[JournalEntry]) -> str:
    return digest_of(canonical_bytes([entry.model_dump(mode="json") for entry in entries]))


def _open_book(
    spec: PaperBookSpec,
    world: PaperWorld,
    completed: Sequence[PaperSessionRecord],
    trading_date: date,
    clock: FrozenClock,
) -> _PaperBroker:
    """Rebuild the paper book as it stood after the last session before ``trading_date``.

    Walks every calendar session from the first decided one: corporate actions, settlement and
    fills on each; the recorded orders placed again on each decided one, and checked against its
    recorded digest; an order staged for an undecided session lapses unfilled. ``clock`` is left
    frozen on the last walked session; the engine moves it to ``trading_date``.
    """
    first = completed[0].trading_date if completed else trading_date
    sessions = list(world.sessions(first, trading_date))
    if not sessions or sessions[-1] != trading_date:
        raise PaperSessionError(
            f"{trading_date.isoformat()} is not in the calendar's sessions from {first.isoformat()}"
        )
    decided = {record.trading_date: record for record in completed}
    off_calendar = sorted(set(decided) - set(sessions))
    if off_calendar:
        raise PaperSessionError(
            "decided sessions the calendar no longer calls sessions: "
            + ", ".join(day.isoformat() for day in off_calendar)
        )

    book = PortfolioBook()
    book.deposit(first, spec.opening_cash)
    held = _held_by(book)
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state=_ACCOUNT_STATE),
        market=world.market(first=first, through=trading_date, held=held),
        opening_cash=spec.opening_cash,
    )
    broker = _PaperBroker(sim, book, clock=clock, corporate_actions=world.corporate_actions())

    for session in sessions[:-1]:
        clock.freeze_at(session)
        record = decided.get(session)
        if record is None:
            lapsed = broker.lapse(session)
            if lapsed:
                _LOG.info(
                    "paper_session.orders_lapsed",
                    book=spec.book_id,
                    session=session.isoformat(),
                    orders=lapsed,
                    reason="the book did not decide this session; its staged orders never filled",
                )
        broker.execute_session(session)
        if record is None:
            continue
        for request in record.orders:
            broker.place(request)
        rebuilt = _book_digest(broker)
        if rebuilt != record.book_digest:
            raise PaperBookDivergenceError(
                f"{spec.book_id}: the book rebuilt through {session.isoformat()} has digest "
                f"{rebuilt}, but {record.book_digest} was recorded when that session was decided; "
                "an input the rebuild reads changed since (see ops/runbooks/daily-eod.md)"
            )
    return broker


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


def _input_gap(
    data: MomentumV2Data,
    policy: MomentumV2Policy,
    parameters: MomentumV2Parameters,
    trading_date: date,
    *,
    rebalance: bool,
) -> str | None:
    """Why the decision cannot be made on this session's inputs, or ``None`` when it can.

    Reads exactly what the policy is about to read, before it reads it: a rebalance with the regime
    filter needs the session's regime reading, and a rebalance or a redeploy needs a non-empty
    candidate set. An empty set on a rebalance would sell the whole book on missing data, so it is
    red rather than a decision.
    """
    try:
        if rebalance and parameters.regime_filter:
            data.regime(trading_date)
        needs_signal = rebalance or policy.pending is not None
        if needs_signal and not data.signal(trading_date).records:
            return f"no momentum candidates for {trading_date.isoformat()}"
    except _MISSING_INPUT as error:
        return f"decision input unavailable: {error}"
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

    def journal_bytes(self) -> bytes:
        """The canonical bytes of the entries this invocation journaled."""
        return canonical_bytes([entry.model_dump(mode="json") for entry in self.entries])

    def book_bytes(self) -> bytes:
        """The canonical bytes of the book after the session (empty when nothing was decided)."""
        return b"" if self.book is None else self.book.canonical_bytes()


def run_paper_session(
    *,
    trading_date: date,
    spec: PaperBookSpec,
    world: PaperWorld,
    store: PaperSessionStore,
    journal: JournalSink,
    gate: Callable[[date], GreenLike],
    clock: Clock,
) -> PaperSessionResult:
    """Decide one session of the paper book, or record why not (see the module docstring).

    What it does: the trading-day check, the idempotency check, the data-red interlock, the book
    rebuild and one ``ReplayEngine`` session, then appends the session's entries to ``journal`` and
    its record to ``store``.
    What it assumes: ``journal`` and ``store`` share the caller's transaction, which the caller
    commits after this returns — so a crash leaves neither half — and ``clock`` is the run's clock,
    used only for the record's landing time.
    What it never does: take or build any broker but the paper ``SimBroker``, place an order on a
    red day, journal a still-red rerun twice, or touch a ``COMPLETED`` session.
    """
    log = _LOG.bind(book=spec.book_id, trading_date=trading_date.isoformat(), mode=PAPER_MODE)
    if not world.is_session(trading_date):
        log.info("paper_session.not_a_session")
        return PaperSessionResult(trading_date, RunVerdict.NOT_A_SESSION, "not a trading session")

    existing = store.get(spec.book_id, trading_date)
    if existing is not None and existing.outcome is SessionOutcome.COMPLETED:
        log.info("paper_session.already_decided", journal_digest=existing.journal_digest)
        return PaperSessionResult(
            trading_date, RunVerdict.ALREADY_DECIDED, "already decided", record=existing
        )

    session_clock = FrozenClock(trading_date)
    red = _red_reason(gate, trading_date, world)
    if red is not None:
        return _skip(spec, trading_date, red, existing, store, journal, session_clock, clock)

    history = store.history(spec.book_id, before=trading_date)
    completed = [record for record in history if record.outcome is SessionOutcome.COMPLETED]
    rebalance = not any(
        record.rebalanced
        and (record.trading_date.year, record.trading_date.month)
        == (trading_date.year, trading_date.month)
        for record in completed
    )
    data = world.momentum_data(trading_date, spec.parameters)
    policy = MomentumV2Policy(
        _PaperMomentumData(data, rebalance_on=trading_date if rebalance else None),
        spec.parameters,
        order_caps=spec.rail_policy.rails,
    )
    policy.resume(completed[-1].pending if completed else None)
    gap = _input_gap(data, policy, spec.parameters, trading_date, rebalance=rebalance)
    if gap is not None:
        return _skip(spec, trading_date, gap, existing, store, journal, session_clock, clock)

    broker = _open_book(spec, world, completed, trading_date, session_clock)
    capturing = _Capturing(policy)
    result = ReplayEngine(
        policy=capturing,
        broker=broker,
        clock=session_clock,
        sessions=(trading_date,),
        rails=RailGate(spec.rail_policy, world.marks(_held_by(broker.book))),
    ).run()
    if capturing.evidence is None:  # pragma: no cover - the engine always asks the policy once
        raise PaperSessionError("the engine finished a session without asking the policy")

    entries = tuple(_tagged(entry, spec) for entry in result.journal)
    journal.snapshot(capturing.evidence)
    for entry in entries:
        journal.append(entry)
    record = PaperSessionRecord(
        book_id=spec.book_id,
        trading_date=trading_date,
        outcome=SessionOutcome.COMPLETED,
        reason="rebalance" if rebalance else "decided",
        rebalanced=rebalance,
        journal_digest=_entries_digest(entries),
        orders=broker.placed_on(trading_date),
        pending=policy.pending,
        book_digest=_book_digest(broker),
    )
    store.record(record, recorded_at=clock.now())
    log.info(
        "paper_session.decided",
        rebalance=rebalance,
        entries=len(entries),
        decisions=sorted({entry.decision.value for entry in entries}),
        orders=len(record.orders),
        redeploy_pending=record.pending is not None,
        cash=str(result.book.cash),
        holdings=len(result.book.holdings),
        book_digest=record.book_digest,
    )
    return PaperSessionResult(
        trading_date,
        RunVerdict.DECIDED,
        record.reason,
        entries=entries,
        record=record,
        book=result.book,
    )


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
) -> PaperSessionResult:
    """Journal one ``SKIPPED_DATA_RED`` for the date (once) and place nothing (invariant #10)."""
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
        payload={**_tag(spec), "datasets": ",".join(spec.datasets)},
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
    log.warning("paper_session.data_red", reason=reason)
    return PaperSessionResult(
        trading_date, RunVerdict.SKIPPED_DATA_RED, reason, entries=(entry,), record=record
    )


def _tag(spec: PaperBookSpec) -> dict[str, str]:
    return {"paper_book": spec.book_id, "mode": PAPER_MODE}


def _tagged(entry: JournalEntry, spec: PaperBookSpec) -> JournalEntry:
    return entry.model_copy(update={"payload": {**entry.payload, **_tag(spec)}})


# ── the production world: the holiday calendar and the lake ──────────────────────────────────────


@dataclass(slots=True)
class L1PaperWorld:
    """The paper book's market, read from the lake exactly as the momentum v2 backtests read it.

    The calendar is the checked-in NSE holiday calendar, not the dates on disk: today's session has
    no successor on disk, and an order staged on it must still target tomorrow. Bars, marks, the
    L2-adjusted signal closes, the investable universe (M9.3) and the published NIFTY 50 regime
    index come from ``backtest.run``'s readers. Everything is opened lazily, so a holiday or a red
    day opens nothing, and closed by :meth:`close`.
    """

    data_root: Path | None = None
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

            self._actions = load_store_book_actions(data_root=self.data_root)
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
    context: JobContext, *, world: PaperWorld | None = None
) -> PaperSessionResult:
    """The ``paper_session`` scheduler job: today's session of the ratified paper book.

    What it does: opens one Postgres transaction, runs :func:`run_paper_session` for
    ``context.clock.today()`` with the ratified spec, the status interlock over the job's own
    database, the L1 world, the real append-only journal and ``paper_session``, and commits.
    What it assumes: the database is migrated through 0012 and the EOD pipeline has run for today.
    ``world`` is injectable for a test; production passes nothing.
    What it never does: route to a real broker (there is no broker parameter anywhere on this
    path), or commit a half-written session — an exception rolls the whole session back and the
    runner records the run FAILED.
    """
    from analyst.journal import Journal
    from dataplatform.store.db import connection

    spec = ratified_paper_book()
    trading_date = context.clock.today()
    with ExitStack() as stack:
        if world is None:
            l1 = L1PaperWorld(data_root=context.settings.data_root)
            stack.callback(l1.close)
            world = l1
        conn = stack.enter_context(connection(context.settings))
        result = run_paper_session(
            trading_date=trading_date,
            spec=spec,
            world=world,
            store=PostgresPaperSessionStore(conn),
            journal=Journal(conn, clock=context.clock),
            gate=StatusGate(datasets=spec.datasets, settings=context.settings, clock=context.clock),
            clock=context.clock,
        )
        conn.commit()
    _LOG.info(
        "paper_session.job_done",
        book=spec.book_id,
        trading_date=trading_date.isoformat(),
        verdict=result.verdict.value,
        reason=result.reason,
        run_id=str(context.run_id),
    )
    return result
