"""M17 multi-book paper execution: one paper book per manager and control, one kill switch for all.

Pre-registration §4 steps 5-7 and §7. Every M17 manager and control book is its own paper book on
the M15.3 shared path — `StagingCoordinator.stage`, then the paper broker (OPEN reference, the
existing `SlippageModel`, the one shared cost model), then `Reconciler` — and every order it
stages has first cleared the M17 rails in `analyst.rails` (`RailEngine.guard_book_order`). This
module is the book side of that path: what a book holds, which orders the rails let through, what
it journals. The account side (the paper broker, its accounting book, idle-cash interest) is
`backtest.fm_paper.M17PaperAccount`, behind the `BookAccount` protocol here, because the decision
layer never names a concrete broker (invariant #5) — and the only account M17 can build is paper.

**One session of one book**, in the order the daily job (M17.7) drives it:

1. `FundBook.execute(session)` — yesterday's staged orders fill at this session's open, idle cash
   accrues repo - 50 bp (credited monthly, `backtest.cash_interest`), and the account's book is
   reconciled against the broker. A clean reconciliation journals a `HEARTBEAT`
   (``payload.event = RECONCILIATION``) with both sides as evidence; a break trips the kill switch
   and journals an `ESCALATE` (``payload.event = RECON_BREAK``). Before the fills, the account
   books the corporate actions it knows of on held names (M17.7, the paper session's rule: on time
   through ``BookActionApplier``, late ones on this session); each is journaled
   (``payload.event = CORPORATE_ACTION``), and one that cannot be booked mechanically is an
   ``ESCALATE`` that trips the kill switch. A buy whose fill session is locked at the upper price
   band is left unfilled and journaled ``payload.event = UNFILLED_UPPER_CIRCUIT`` (Amendment 1 e).
2. `FundBook.decide(session, orders)` — each `BookOrder` is valued at the session's close and
   cleared through the rails: sells first, against what the book holds; then buys, against the
   book without those sells (a sale frees neither a name slot nor cash before it has filled and
   settled). A refused order is journaled `RAIL_BLOCK` with ``payload.rails`` naming every rail
   that refused it; an allowed one is staged and journaled `BUY`/`SELL`. A sell that only the
   participation rail refuses is not refused (M17.7): it becomes one parent exit worked as a child
   per session, each child the most participation allows that session and cleared through every
   rail, until the book holds what the parent leaves (`PendingExit`). A buy is never sliced.

**A suspended holding** (M17.13, owner decision 2026-10-10). A held name that is still listed but
has no bar on a session that otherwise printed normally (the data interlock is green and the
session's L1 coverage is at its usual level — `SuspendedNames` says which) is SUSPENDED for that
session, not a data fault: the book values it at its last traded raw close (the source and
semantics `DelistedNames` uses), journals ``payload.event = SUSPENDED_HOLDING`` each session it is
suspended (`journal_suspended_holdings`), and never trades it at a made-up price. A sell of it
(a manager's SELL/TRIM, a control's exit, a ``STOP_EXIT``) stays unfilled: a staged sell the
broker could not fill for want of a bar is journaled ``UNFILLED_SUSPENDED``, a sell decided while
the name has no close is journaled ``SUSPENDED_EXIT_HELD``, and either way it is held over as a
`PendingExit` and re-offered every session until the name prints again, when it clears the rails
and is staged at that session's close like any sell. A buy of a name with no close is never
staged (``UNPRICED``), and the paper broker rejects any order whose fill session has no bar. A
session whose market data is broadly missing is red data, stopped by the interlock before any
book is touched; a held gap the suspension test does not cover stays a loud `BookError`.

**One kill switch stops every M17 book** (§4 step 1): `m17_kill_switch` is the single switch file,
`FundDesk` refuses books that do not share it, and a tripped switch makes every book journal a
no-op (``payload.event = KILL_SWITCH_TRIPPED``), lapse the orders due that session and stage
nothing. `FundDesk.execute` reads the switch once, before any book fills, so a break in one book
halts the others from the next decision on, never half-way through a session's fills.

**Isolation** (acceptance 3): a book owns its account (positions and cash), its rails, its
last-buy-fill record and its journal stream — every entry it writes carries its own book id as
``case_id``, stamped here, never taken from the caller. `FundDesk` refuses two books with one id,
one account or one account id.

**No future data** (invariant #7): the participation median, the series and the marks are read
for the decision session, and a market that answers with a traded value dated after it is refused
(`FutureDataError`) rather than used.

What this module never does: import or construct a broker (the `BookAccount` is injected, and
`tests/unit/test_fm_books.py` proves `execution.kite_broker` is unreachable from M17), read a wall
clock, resize a refused order, or let one book read another's state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from analyst.fundmanager.mandate import (
    BenchMandate,
    ControlMandate,
    M17Rails,
    ManagerMandate,
    Roster,
)
from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, RecordedEntry, Sleeve
from analyst.rails import (
    BookOrderFacts,
    BookRails,
    Lot,
    Portfolio,
    ProposedOrder,
    RailAssessment,
    RailEngine,
    apply_order,
)
from dataplatform.clock import Clock
from dataplatform.logging import get_logger
from execution.broker import Fill, OrderRequest, Side
from execution.kill_switch import KillSwitch, TripSource, kill_switch_path
from execution.recon import ReconResult

__all__ = [
    "BOOK_HALTED_EVENT",
    "BOOK_SLEEVE",
    "CORPORATE_ACTION_EVENT",
    "EXIT_COMPLETE_EVENT",
    "EXIT_SUPERSEDED_EVENT",
    "M17_KILL_SWITCH_ACCOUNT",
    "ORDER_STAGED_EVENT",
    "PAPER_MODE",
    "RECON_BREAK_EVENT",
    "RECON_EVENT",
    "SUSPENDED_EXIT_HELD_EVENT",
    "SUSPENDED_HOLDING_EVENT",
    "UNFILLED_SUSPENDED_EVENT",
    "UNFILLED_UPPER_CIRCUIT_EVENT",
    "AccountSession",
    "BookAccount",
    "BookError",
    "BookJournal",
    "BookMarket",
    "BookOrder",
    "BookedCorporateAction",
    "CorporateActionStatus",
    "DecisionReport",
    "DelistedNames",
    "ExecutionReport",
    "FundBook",
    "FundDesk",
    "FutureDataError",
    "LastTraded",
    "PendingExit",
    "RejectedOrder",
    "SuspendedHolding",
    "SuspendedNames",
    "UnfilledOrder",
    "book_rails",
    "m17_kill_switch",
    "median_traded_value",
    "paper_account_id",
    "tradable_mandates",
]

_LOG = get_logger(__name__)

_ZERO: Final = Decimal(0)

#: The one kill-switch account every M17 book shares: ``<data_root>/kill_switch/<this>.json``.
M17_KILL_SWITCH_ACCOUNT: Final = "m17_fund_managers"
#: Stamped on every entry a book writes, so a paper decision is never read as a real-money one.
PAPER_MODE: Final = "PAPER"
#: The sleeve an M17 trade is filed under: a manager's book is active, discretionary capital.
BOOK_SLEEVE: Final = Sleeve.TACTICAL
#: ``payload.event`` on a clean reconciliation's HEARTBEAT and on a break's ESCALATE.
RECON_EVENT: Final = "RECONCILIATION"
RECON_BREAK_EVENT: Final = "RECON_BREAK"
#: ``payload.event`` on the no-op a book journals while the shared kill switch is tripped.
BOOK_HALTED_EVENT: Final = "KILL_SWITCH_TRIPPED"
#: ``payload.event`` on an ordinary order's staging line (`BookOrder.event`'s default).
ORDER_STAGED_EVENT: Final = "STAGED"
#: ``payload.event`` on a corporate action the account booked (or escalated) on a held name.
CORPORATE_ACTION_EVENT: Final = "CORPORATE_ACTION"
#: ``payload.event`` on a buy left unfilled because its session was locked at the upper band.
UNFILLED_UPPER_CIRCUIT_EVENT: Final = "UNFILLED_UPPER_CIRCUIT"
#: ``payload.event`` when a parent exit worked across sessions is done, or replaced by a decision.
EXIT_COMPLETE_EVENT: Final = "EXIT_COMPLETE"
EXIT_SUPERSEDED_EVENT: Final = "EXIT_SUPERSEDED"
#: ``payload.event`` on the line a book journals for each held, suspended name every session.
SUSPENDED_HOLDING_EVENT: Final = "SUSPENDED_HOLDING"
#: ``payload.event`` on a staged order the broker left unfilled because its name had no bar.
UNFILLED_SUSPENDED_EVENT: Final = "UNFILLED_SUSPENDED"
#: ``payload.event`` on a sell of a suspended name that could not be staged this session.
SUSPENDED_EXIT_HELD_EVENT: Final = "SUSPENDED_EXIT_HELD"
#: The booked corporate actions that move a holding to another ISIN (`BookedCorporateAction.kind`).
_ISIN_CHANGES: Final = frozenset({"REISSUE", "SWAP"})


class BookError(RuntimeError):
    """An M17 book cannot proceed — always loud, never absorbed into a quiet no-op."""


class FutureDataError(BookError):
    """The market answered a decision-session question with data dated after that session."""


# ── configuration: the rails of one book ────────────────────────────────────────────────────────


def book_rails(mandate: ManagerMandate | ControlMandate, rails: M17Rails) -> BookRails:
    """The A8 `BookRails` of one manager or control book: its mandate's caps plus the shared rails.

    The one place the roster's numbers become rail limits — nothing in the rail engine or here
    writes a cap down a second time. The buyable series is the mandate's universe series.
    """
    return BookRails(
        max_position_pct=mandate.max_position_pct,
        max_sector_pct=mandate.max_sector_pct,
        max_positions=mandate.max_positions,
        participation_max_pct=rails.participation_max_pct,
        participation_lookback_sessions=rails.participation_lookback_sessions,
        min_hold_sessions=rails.min_hold_sessions,
        equity_series=frozenset({mandate.universe.series}),
    )


def paper_account_id(book_id: str) -> str:
    """The lower snake id of an M17 book's paper account: ``FM-SWING-10L`` → ``m17_fm_swing_10l``.

    The shape the M15.3 paper-session ledger (``paper_session.book_id``) and the order uids key on,
    so each M17 book is its own row stream there with no new schema.
    """
    account = "m17_" + book_id.lower().replace("-", "_")
    if not account.replace("_", "").isalnum() or len(account) > 64:
        raise ValueError(f"book id {book_id!r} does not map to a paper account id")
    return account


def m17_kill_switch(data_root: Path, *, clock: Clock) -> KillSwitch:
    """The single kill switch of every M17 book (pre-registration §4 step 1)."""
    return KillSwitch(kill_switch_path(data_root, M17_KILL_SWITCH_ACCOUNT), clock=clock)


def median_traded_value(values: Sequence[Decimal]) -> Decimal:
    """The exact median of ``values`` (the mean of the middle two for an even count)."""
    if not values:
        raise ValueError("the median of no sessions is not a number")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


# ── the seams: the account, the market, the journal ─────────────────────────────────────────────


class CorporateActionStatus(StrEnum):
    """What the account did with a corporate action on a held name."""

    BOOKED = "BOOKED"
    """Applied to the broker and the accounting book alike."""

    ESCALATED = "ESCALATED"
    """Not safe to book mechanically (late, and the name traded since; or no modelled terms):
    the book is left as it was and the owner decides."""


@dataclass(frozen=True, slots=True)
class BookedCorporateAction:
    """One corporate action the account saw on a held name in a session, and what it did.

    ``identity`` is the paper session's ``action_identity`` (kind, ISIN, ex-date), so an action is
    booked once whatever its later corrections. ``late`` says the account had already executed
    past its ex-date when it learnt of it. ``rescale`` is ``(numerator, denominator)`` when shares
    of ``isin`` were multiplied by that ratio, so the book can rescale what it keeps in prices or
    shares (a stop level, a parent exit's remainder); ``cash`` is what was credited.
    """

    identity: str
    isin: str
    ex_date: date
    kind: str
    status: CorporateActionStatus
    late: bool
    entitled: int
    detail: str
    cash: Decimal = Decimal(0)
    rescale: tuple[Decimal, Decimal] | None = None


@dataclass(frozen=True, slots=True)
class UnfilledOrder:
    """An order the account cancelled at its fill session instead of filling (Amendment 1 e).

    ``basis`` says which test found the lock: ``BAND`` (the session's price band) or
    ``SMALLEST_BAND`` (no band known for the session, so the smallest NSE band stood in).
    """

    order_id: str
    isin: str
    quantity: int
    session: date
    open: Decimal
    prev_close: Decimal
    threshold: Decimal
    basis: str


@dataclass(frozen=True, slots=True)
class RejectedOrder:
    """An order due this session that the paper broker rejected instead of filling.

    ``order_uid`` is the staging uid (the one the order's ``STAGED`` line names) and ``reason``
    the broker's own words (no bar, no cash, nothing to deliver).
    """

    order_uid: str
    isin: str
    side: Side
    quantity: int
    session: date
    reason: str


@dataclass(frozen=True, slots=True)
class AccountSession:
    """What one session did to a book's paper account: its fills, interest and reconciliation."""

    session: date
    fills: tuple[Fill, ...]
    interest_credited: Decimal
    recon: ReconResult
    cash: Decimal
    quantities: Mapping[str, int]
    broker_cash: Decimal
    broker_quantities: Mapping[str, int]
    corporate_actions: tuple[BookedCorporateAction, ...] = ()
    unfilled: tuple[UnfilledOrder, ...] = ()
    rejected: tuple[RejectedOrder, ...] = ()


class BookAccount(Protocol):
    """One book's paper account on the M15.3 shared path (`backtest.fm_paper.M17PaperAccount`)."""

    @property
    def account_id(self) -> str:
        """The account's lower snake id (`paper_account_id`)."""
        ...

    @property
    def spendable_cash(self) -> Decimal:
        """Settled cash a buy staged now can pay with."""
        ...

    @property
    def cash_value(self) -> Decimal:
        """Cash the account owns, settled or in settlement — what a valuation adds to the marks."""
        ...

    def quantities(self) -> Mapping[str, int]:
        """Shares held per ISIN, settled and unsettled."""
        ...

    def execute_session(self, session: date) -> AccountSession:
        """Credit interest due, fill the session's staged orders, accrue, reconcile."""
        ...

    def lapse(self, session: date) -> tuple[str, ...]:
        """Cancel every order staged for ``session`` (the book is halted); their broker ids."""
        ...

    def stage(self, request: OrderRequest) -> str:
        """Stage ``request`` through the staging coordinator; the staged order's uid."""
        ...


class BookMarket(Protocol):
    """What a book reads about the market, every answer as of a session it names."""

    def close(self, isin: str, session: date) -> Decimal | None:
        """The raw close of ``isin`` on ``session`` (the decision's reference price), or None."""
        ...

    def series(self, isin: str, session: date) -> str | None:
        """The series ``isin`` traded in on ``session`` (``EQ``, ``BE``, …), or None if none."""
        ...

    def sector(self, isin: str) -> str:
        """The sector the sector cap groups ``isin`` under."""
        ...

    def traded_values(
        self, isin: str, *, through: date, sessions: int
    ) -> Sequence[tuple[date, Decimal]]:
        """Up to the last ``sessions`` (session, traded value) pairs of ``isin`` on or before
        ``through``; fewer when the name has less history."""
        ...

    def sessions_between(self, start: date, end: date) -> int:
        """How many trading sessions fall in ``(start, end]``."""
        ...


@dataclass(frozen=True, slots=True)
class LastTraded:
    """A delisted name's last traded session, with its raw and adjusted closes there."""

    session: date
    raw_close: Decimal
    adjusted_close: Decimal


class DelistedNames(Protocol):
    """Which names' listings have ended, and where they last traded (the listing record).

    M17.7: a held name whose listing has ended is valued at its last traded raw close — by the
    book's caps (`FundBook.valuation_close`), its mark (`scoreboard.mark_book`) and its decision's
    outcome (`scoreboard.resolve_outcome`) — until a corporate action converts it. A listed name
    with no close is not a delisting: it is SUSPENDED when `SuspendedNames` says the session
    otherwise printed normally (M17.13), and a loud error otherwise.
    """

    def last_traded(self, isin: str, session: date) -> LastTraded | None:
        """For ``isin`` delisted on or before ``session``: its last traded session and closes.
        None for a name still listed on ``session``."""
        ...


class SuspendedNames(Protocol):
    """Which still-listed names are suspended on a session, and where they last traded.

    M17.13: a held name that is still listed but has no bar on a session that printed normally is
    SUSPENDED for that session (module docstring). The implementation states its own test for
    "printed normally" (production: `backtest.fm_world.LakeSuspendedNames`); a market-wide gap is
    never a suspension — it is red data, and the interlock stops the session.
    """

    def last_traded(self, isin: str, session: date) -> LastTraded | None:
        """For ``isin`` still listed, with no bar on ``session``, on a session that printed
        normally: its last traded session (before ``session``) and closes there. None when the
        name printed on ``session``, has delisted, never printed, or the session's market data is
        broadly missing."""
        ...


@dataclass(frozen=True, slots=True)
class SuspendedHolding:
    """One held name suspended on ``session``: valued at ``last.raw_close``, never traded there.

    ``sessions_suspended`` counts the sessions in ``(last.session, session]`` — 1 on the first
    session without a print.
    """

    isin: str
    quantity: int
    session: date
    last: LastTraded
    sessions_suspended: int


class BookJournal(Protocol):
    """The slice of `analyst.journal.Journal` a book writes through. Append-only by shape."""

    def append(self, entry: JournalEntry, *, evidence: EvidenceBundle | None = None) -> object:
        """Append one entry, storing ``evidence`` first when given."""
        ...


# ── value objects ────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BookOrder:
    """One order a manager's decision (or a control's rebalance) asks its book to place.

    Whole shares of one ISIN, one side, and the reason the decision gave — carried into the
    journal line the order produces, so a trade is never unexplained.
    """

    isin: str
    side: Side
    quantity: int
    rationale: str
    #: ``payload.event`` on the order's staging line: ``STAGED`` for a decision's order,
    #: ``STOP_EXIT`` for a mechanical stop (`analyst.fundmanager.stops`).
    event: str = ORDER_STAGED_EVENT

    def __post_init__(self) -> None:
        # OrderRequest validates the ISIN and the whole-share quantity; fail at construction.
        OrderRequest(isin=self.isin, side=self.side, quantity=self.quantity)
        if not self.rationale.strip():
            raise ValueError("a book order carries the decision's rationale")
        if not self.event.strip():
            raise ValueError("a book order names the event its staging line records")


@dataclass(frozen=True, slots=True)
class PendingExit:
    """A sell the participation rail let through only in part: one parent, a child per session.

    ``floor`` is what the book holds once the whole parent has sold; each session the remainder is
    what is held above it (so a child that did not fill is simply offered again). ``parent_uid`` is
    the first child's order uid — every child's journal line names it, so the children read as one
    exit. Persisted with the book (`FundBook.pending_exits_document`).
    """

    isin: str
    parent_uid: str
    decided: date
    parent_quantity: int
    floor: int
    rationale: str
    event: str
    children: int

    def to_document(self) -> dict[str, str]:
        return {
            "isin": self.isin,
            "parent_uid": self.parent_uid,
            "decided": self.decided.isoformat(),
            "parent_quantity": str(self.parent_quantity),
            "floor": str(self.floor),
            "rationale": self.rationale,
            "event": self.event,
            "children": str(self.children),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, str]) -> PendingExit:
        return cls(
            isin=document["isin"],
            parent_uid=document["parent_uid"],
            decided=date.fromisoformat(document["decided"]),
            parent_quantity=int(document["parent_quantity"]),
            floor=int(document["floor"]),
            rationale=document["rationale"],
            event=document["event"],
            children=int(document["children"]),
        )


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """What `FundBook.execute` did for one session."""

    book_id: str
    session: date
    halted: bool
    fills: tuple[Fill, ...] = ()
    lapsed: tuple[str, ...] = ()
    recon: ReconResult | None = None
    interest_credited: Decimal = _ZERO
    corporate_actions: tuple[BookedCorporateAction, ...] = ()
    unfilled: tuple[UnfilledOrder, ...] = ()
    rejected: tuple[RejectedOrder, ...] = ()


@dataclass(frozen=True, slots=True)
class DecisionReport:
    """What `FundBook.decide` did for one session: what it staged and what the rails refused."""

    book_id: str
    session: date
    halted: bool
    staged: tuple[tuple[BookOrder, str], ...] = ()
    refused: tuple[tuple[BookOrder, RailAssessment], ...] = ()
    unpriced: tuple[BookOrder, ...] = ()


class _StampedRailJournal:
    """A `RailJournal` that files A8's `RAIL_BLOCK` lines in one book's stream."""

    __slots__ = ("_book", "_count")

    def __init__(self, book: FundBook) -> None:
        self._book = book
        self._count = 0

    def append(self, entry: JournalEntry) -> RecordedEntry:
        stamped = self._book._write(entry)
        self._count += 1
        return RecordedEntry(**stamped.model_dump(), id=self._count, recorded_at=stamped.ts)


# ── one book ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class FundBook:
    """One M17 manager or control book: its rails, its paper account and its journal stream.

    What it does: execute a session's fills and reconcile them (`execute`), and clear a session's
    orders through the M17 rails and stage the ones they allow (`decide`), journaling every step
    under ``book_id``.
    What it assumes: ``account`` belongs to this book alone and ``kill_switch`` is the one M17
    switch (`FundDesk` checks both); ``clock`` is frozen on the session being decided, as in every
    replay, so a session's entries are a pure function of the session.
    What it never does: place an order the rails refused, journal under another book's id, or
    read the market for any day but the decision session.
    """

    book_id: str
    rails: BookRails
    account: BookAccount
    market: BookMarket
    journal: BookJournal
    kill_switch: KillSwitch
    clock: Clock
    last_buy_fill: dict[str, date] = field(default_factory=dict)
    pending_exits: dict[str, PendingExit] = field(default_factory=dict)
    #: The listing record: a held name that delisted is valued at its last traded close.
    delisted: DelistedNames | None = None
    #: M17.13: a held, still-listed name with no bar on a normal session is valued at its last
    #: traded close and its sells are held over until it prints (module docstring).
    suspended: SuspendedNames | None = None
    #: The sells staged for the next session, by staging uid: (event, rationale). Read once, when
    #: that session executes, so a sell the broker could not fill on a suspended name is held over
    #: with the event (``STOP_EXIT``, ``STAGED``) and the rationale it was decided with.
    staged_sells: dict[str, tuple[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.book_id.strip():
            raise ValueError("a book needs an id")
        self.last_buy_fill = dict(self.last_buy_fill)
        self.pending_exits = dict(self.pending_exits)
        self.staged_sells = dict(self.staged_sells)

    # -- journal stream ---------------------------------------------------------------------------

    def _write(self, entry: JournalEntry, evidence: EvidenceBundle | None = None) -> JournalEntry:
        """Append ``entry`` to this book's stream: its own ``case_id`` and tag, whatever it had."""
        stamped = entry.model_copy(
            update={
                "case_id": self.book_id,
                "payload": {**entry.payload, "book": self.book_id, "mode": PAPER_MODE},
            }
        )
        self.journal.append(stamped, evidence=evidence)
        return stamped

    def _entry(
        self,
        session: date,
        *,
        actor: Actor,
        decision: Decision,
        rationale: str,
        payload: Mapping[str, str],
        isin: str | None = None,
        evidence_ref: str | None = None,
    ) -> JournalEntry:
        return JournalEntry(
            ts=self.clock.now(),
            trading_date=session,
            case_id=self.book_id,
            actor=actor,
            decision=decision,
            isin=isin,
            sleeve=BOOK_SLEEVE if isin is not None else None,
            rationale=rationale,
            payload=dict(payload),
            evidence_snapshot_ref=evidence_ref,
        )

    def _journal_halt(self, session: date, step: str, lapsed: tuple[str, ...] = ()) -> None:
        state = self.kill_switch.state
        self._write(
            self._entry(
                session,
                actor=Actor.EXEC,
                decision=Decision.SKIPPED_DATA_RED,
                rationale=(
                    f"the M17 kill switch is tripped ({state.reason}); the book {step} nothing "
                    "this session and stays halted until the owner resets the switch"
                ),
                payload={
                    "event": BOOK_HALTED_EVENT,
                    "step": step,
                    "source": "" if state.source is None else state.source.value,
                    "lapsed": ",".join(lapsed),
                },
            )
        )

    # -- step 1: fills, interest, reconciliation --------------------------------------------------

    def execute(self, session: date) -> ExecutionReport:
        """Fill the session's staged orders and reconcile — or, while the switch is tripped, lapse
        them and journal the halt. Returns what happened."""
        if self.kill_switch.is_tripped:
            return self.halt(session)
        return self._execute_fills(session)

    def halt(self, session: date) -> ExecutionReport:
        """A tripped-switch session: the orders due now lapse unfilled and the halt is journaled."""
        lapsed = self.account.lapse(session)
        self.staged_sells.clear()
        self._journal_halt(session, "filled", lapsed)
        _LOG.warning("fm_books.halted", book=self.book_id, session=session.isoformat())
        return ExecutionReport(self.book_id, session, halted=True, lapsed=lapsed)

    def _execute_fills(self, session: date) -> ExecutionReport:
        run = self.account.execute_session(session)
        for fill in run.fills:
            if fill.side is Side.BUY:
                self.last_buy_fill[fill.isin] = fill.session
        self._journal_corporate_actions(run)
        self._journal_unfilled(run)
        self._hold_over_rejected(run)
        self._journal_recon(run)
        _LOG.info(
            "fm_books.executed",
            book=self.book_id,
            session=session.isoformat(),
            fills=len(run.fills),
            recon="CLEAN" if run.recon.ok else "BREAK",
            interest=str(run.interest_credited),
        )
        return ExecutionReport(
            self.book_id,
            session,
            halted=False,
            fills=run.fills,
            recon=run.recon,
            interest_credited=run.interest_credited,
            corporate_actions=run.corporate_actions,
            unfilled=run.unfilled,
            rejected=run.rejected,
        )

    def _journal_corporate_actions(self, run: AccountSession) -> None:
        """Journal each corporate action the account saw on a held name; trip on an escalation.

        A rescale also rescales a parent exit's floor in that name, so the exit still leaves the
        same fraction of the position it meant to leave.
        """
        for action in run.corporate_actions:
            pending = self.pending_exits.get(action.isin)
            if (
                pending is not None
                and action.status is CorporateActionStatus.BOOKED
                and action.kind in _ISIN_CHANGES
            ):
                self._supersede_on_isin_change(run.session, pending, action)
                pending = None
            if action.rescale is not None and pending is not None:
                numerator, denominator = action.rescale
                floor = int(
                    (Decimal(pending.floor) * numerator / denominator).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                )
                self.pending_exits[action.isin] = replace(pending, floor=floor)
            escalated = action.status is CorporateActionStatus.ESCALATED
            self._write(
                self._entry(
                    run.session,
                    actor=Actor.EXEC,
                    decision=Decision.ESCALATE if escalated else Decision.HOLD,
                    isin=action.isin,
                    rationale=(
                        f"{'not booked' if escalated else 'booked'}"
                        f"{' late' if action.late else ''}: {action.detail}"
                    ),
                    payload={
                        "event": CORPORATE_ACTION_EVENT,
                        "action": action.identity,
                        "kind": action.kind,
                        "ex_date": action.ex_date.isoformat(),
                        "status": action.status.value,
                        "late": str(action.late).lower(),
                        "entitled": str(action.entitled),
                        "cash": str(action.cash),
                        "rescale": (
                            ""
                            if action.rescale is None
                            else f"{action.rescale[0]}:{action.rescale[1]}"
                        ),
                    },
                )
            )
            if escalated:
                # The book's share count is in question; trading on it would compound the error.
                self.kill_switch.trip(
                    reason=(
                        f"{self.book_id}: corporate action {action.identity} on a held name "
                        "cannot be booked mechanically; owner review"
                    ),
                    source=TripSource.RECON,
                )

    def _supersede_on_isin_change(
        self, session: date, pending: PendingExit, action: BookedCorporateAction
    ) -> None:
        """A parent exit whose ISIN was reissued or swapped away ends here, journaled: its floor
        is in shares of an ISIN the book no longer holds, so working it on would complete with
        nothing sold. The holding now sits under the successor ISIN for the next decision."""
        del self.pending_exits[pending.isin]
        self._write(
            self._entry(
                session,
                actor=Actor.EXEC,
                decision=Decision.HOLD,
                isin=pending.isin,
                rationale=(
                    f"exit {pending.parent_uid} of {pending.parent_quantity} ended after "
                    f"{pending.children} child order(s): {action.kind.lower()} moved the holding "
                    f"off {pending.isin} ({action.detail}); the next decision sees the new ISIN"
                ),
                payload={
                    "event": EXIT_SUPERSEDED_EVENT,
                    "exit_parent": pending.parent_uid,
                    "exit_parent_quantity": str(pending.parent_quantity),
                    "exit_children": str(pending.children),
                    "reason": f"ISIN_CHANGE:{action.kind}",
                    "action": action.identity,
                },
            )
        )

    def _journal_unfilled(self, run: AccountSession) -> None:
        for order in run.unfilled:
            self._write(
                self._entry(
                    run.session,
                    actor=Actor.EXEC,
                    decision=Decision.HOLD,
                    isin=order.isin,
                    rationale=(
                        f"buy of {order.quantity} {order.isin} left unfilled: the session opened "
                        f"and stayed at {order.open} (open = high = low), at or above the upper "
                        f"band {order.threshold} over the previous close {order.prev_close} "
                        f"({order.basis})"
                    ),
                    payload={
                        "event": UNFILLED_UPPER_CIRCUIT_EVENT,
                        "order_id": order.order_id,
                        "quantity": str(order.quantity),
                        "open": str(order.open),
                        "prev_close": str(order.prev_close),
                        "threshold": str(order.threshold),
                        "basis": order.basis,
                    },
                )
            )

    def _hold_over_rejected(self, run: AccountSession) -> None:
        """Journal each order the broker left unfilled on a suspended name; hold its sells over.

        Only an order whose name is suspended this session (`suspension`) is handled here: it is
        journaled ``UNFILLED_SUSPENDED`` and, for a sell, becomes a `PendingExit` (unless one
        already works that name — its remainder is re-offered by itself) so it is re-offered every
        session until the name prints. A buy is not re-offered: the decision that sized it is
        stale by the time the name trades again.
        """
        staged = dict(self.staged_sells)
        self.staged_sells.clear()
        for order in run.rejected:
            last = self.suspension(order.isin, run.session)
            if last is None:
                continue
            selling = order.side is Side.SELL
            event, rationale = staged.get(
                order.order_uid, (ORDER_STAGED_EVENT, f"sell of {order.isin}")
            )
            held = run.quantities.get(order.isin, 0)
            exiting = self.pending_exits.get(order.isin)
            if selling and exiting is None and held > 0:
                self.pending_exits[order.isin] = PendingExit(
                    isin=order.isin,
                    parent_uid=order.order_uid,
                    decided=run.session,
                    parent_quantity=order.quantity,
                    floor=max(held - order.quantity, 0),
                    rationale=rationale,
                    event=event,
                    children=1,
                )
            self._write(
                self._entry(
                    run.session,
                    actor=Actor.EXEC,
                    decision=Decision.HOLD,
                    isin=order.isin,
                    rationale=(
                        f"{order.side.value.lower()} of {order.quantity} {order.isin} left "
                        f"unfilled: the name is suspended (held, not trading since "
                        f"{last.session.isoformat()}), so there is no price to fill at; "
                        + (
                            "re-offered every session until it prints"
                            if selling
                            else "a buy is not re-offered"
                        )
                    ),
                    payload={
                        "event": UNFILLED_SUSPENDED_EVENT,
                        "order_id": order.order_uid,
                        "side": order.side.value,
                        "quantity": str(order.quantity),
                        "order_event": event if selling else ORDER_STAGED_EVENT,
                        "last_trade_date": last.session.isoformat(),
                        "reoffered": "true" if selling else "false",
                    },
                )
            )
            _LOG.warning(
                "fm_books.unfilled_suspended",
                book=self.book_id,
                isin=order.isin,
                side=order.side.value,
                session=run.session.isoformat(),
                last_trade_date=last.session.isoformat(),
            )

    def _journal_recon(self, run: AccountSession) -> None:
        items: list[EvidenceItem] = []
        for source, cash, quantities in (
            ("paper_book", run.cash, run.quantities),
            ("paper_broker", run.broker_cash, run.broker_quantities),
        ):
            items.append(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source=source,
                    label="cash",
                    as_of=run.session,
                    value=cash,
                )
            )
            items.extend(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source=source,
                    label="quantity",
                    isin=isin,
                    as_of=run.session,
                    value=Decimal(quantity),
                )
                for isin, quantity in sorted(quantities.items())
            )
        evidence = EvidenceBundle(trading_date=run.session, actor=Actor.EXEC, items=tuple(items))
        payload = {
            "event": RECON_EVENT if run.recon.ok else RECON_BREAK_EVENT,
            "fills": str(len(run.fills)),
            "interest_credited": str(run.interest_credited),
            "positions": str(len(run.quantities)),
            "cash": str(run.cash),
        }
        if run.recon.ok:
            decision = Decision.HEARTBEAT
            rationale = (
                f"reconciliation clean: the book and the paper broker agree on "
                f"{len(run.quantities)} position(s) and cash {run.cash} after "
                f"{len(run.fills)} fill(s)"
            )
        else:
            decision = Decision.ESCALATE
            breaks = [item.describe() for item in run.recon.breaks]
            payload["breaks"] = " | ".join(breaks)
            rationale = (
                f"reconciliation break: {'; '.join(breaks)}. The M17 kill switch is tripped and "
                "every M17 book stages nothing until the owner resolves it and resets the switch"
            )
        self._write(
            self._entry(
                run.session,
                actor=Actor.EXEC,
                decision=decision,
                rationale=rationale,
                payload=payload,
                evidence_ref=evidence.ref().ref,
            ),
            evidence,
        )

    # -- step 2: rails, then staging --------------------------------------------------------------

    def decide(self, session: date, orders: Sequence[BookOrder]) -> DecisionReport:
        """Clear ``orders`` through the M17 rails at ``session``'s close and stage what they allow.

        Parent exits still being worked (`PendingExit`) go first, a child each, unless this
        session's orders name the same ISIN — a new decision on a name replaces the old exit
        (journaled ``EXIT_SUPERSEDED``). Then sells are cleared against what the book holds, then
        buys against the book without those sells; within a side, in the order given. A sell that
        only participation refuses is staged as its first child (`RailEngine.guard_book_exit`) and
        the rest becomes a `PendingExit`; a buy is cleared whole. A refused order is journaled
        `RAIL_BLOCK` by A8; an order with no close to value it at is journaled `DEFERRED`; a
        staged order is journaled `BUY`/`SELL` with its uid. A session with no orders and no exit
        to work journals a `HOLD`.
        A sell of a suspended holding (`suspension`) is never staged: it is journaled
        ``SUSPENDED_EXIT_HELD`` and held over as a `PendingExit`, re-offered every session until
        the name prints (M17.13).
        Raises `BookError` for two orders in one ISIN (one decision per name), or a held name with
        no close that is neither delisted nor suspended (the book cannot be valued, so no cap can
        be checked); a delisted or suspended holding is valued at its last traded close
        (`valuation_close`).
        """
        if self.kill_switch.is_tripped:
            self._journal_halt(session, "staged")
            return DecisionReport(self.book_id, session, halted=True)
        names = [order.isin for order in orders]
        if len(set(names)) != len(names):
            raise BookError(f"{self.book_id}: two orders for one ISIN on {session.isoformat()}")
        book = self._portfolio(session)
        engine = RailEngine(_StampedRailJournal(self), clock=self.clock)
        staged: list[tuple[BookOrder, str]] = []
        refused: list[tuple[BookOrder, RailAssessment]] = []
        unpriced: list[BookOrder] = []

        sellable = book
        worked = self._work_pending_exits(session, set(names), sellable, engine, staged, refused)
        for isin in worked:
            sellable = _less(sellable, isin, worked[isin])
        for order in (o for o in orders if o.side is Side.SELL):
            proposed = self._proposed(order, session)
            if proposed is None:
                unpriced.append(order)
                continue
            facts = self._facts(proposed, session, spendable=self.account.spendable_cash)
            clearance = engine.guard_book_exit(
                proposed, sellable, self.rails, facts, trading_date=session, sleeve=BOOK_SLEEVE
            )
            if not clearance.allowed:
                refused.append((order, clearance.assessment))
                continue
            child = clearance.child
            if clearance.sliced:
                held = sellable.lot(order.isin)
                uid = self._stage(
                    replace(order, quantity=child.quantity),
                    child,
                    session,
                    exit_payload={**clearance.payload(), "exit_child": "1"},
                )
                self.pending_exits[order.isin] = PendingExit(
                    isin=order.isin,
                    parent_uid=uid,
                    decided=session,
                    parent_quantity=order.quantity,
                    floor=(0 if held is None else held.quantity) - order.quantity,
                    rationale=order.rationale,
                    event=order.event,
                    children=1,
                )
                staged.append((order, uid))
            else:
                staged.append((order, self._stage(order, proposed, session)))
            sellable = _less(sellable, order.isin, child.quantity)

        spendable = self.account.spendable_cash
        buying = book
        for order in (o for o in orders if o.side is Side.BUY):
            proposed = self._proposed(order, session)
            if proposed is None:
                unpriced.append(order)
                continue
            facts = self._facts(proposed, session, spendable=spendable)
            verdict = engine.guard_book_order(
                proposed, buying, self.rails, facts, trading_date=session, sleeve=BOOK_SLEEVE
            )
            if not verdict.allowed:
                refused.append((order, verdict))
                continue
            if proposed.value > spendable or proposed.value > buying.cash:
                # The margin rail allowed what the account cannot pay for: a rail defect, not a
                # market event. Stop loudly before the order reaches the account.
                raise BookError(
                    f"{self.book_id}: rails cleared a {proposed.value} buy of {order.isin} with "
                    f"{spendable} spendable"
                )
            staged.append((order, self._stage(order, proposed, session)))
            buying = apply_order(buying, proposed)
            spendable -= proposed.value

        for order in unpriced:
            last = self.suspension(order.isin, session) if order.side is Side.SELL else None
            lot = book.lot(order.isin)
            if last is not None and lot is not None:
                self._hold_over_sell(session, order, lot.quantity, last)
                continue
            self._write(
                self._entry(
                    session,
                    actor=Actor.EXEC,
                    decision=Decision.DEFERRED,
                    isin=order.isin,
                    rationale=(
                        f"no close for {order.isin} on {session.isoformat()} to value the order "
                        "at; nothing staged"
                    ),
                    payload={"event": "UNPRICED", "side": order.side.value},
                )
            )
        if not orders and not worked and not self.pending_exits:
            self._write(
                self._entry(
                    session,
                    actor=Actor.EXEC,
                    decision=Decision.HOLD,
                    rationale="no order for the book this session; nothing staged",
                    payload={"event": "NO_ORDERS"},
                )
            )
        _LOG.info(
            "fm_books.decided",
            book=self.book_id,
            session=session.isoformat(),
            staged=len(staged),
            refused=len(refused),
            unpriced=len(unpriced),
        )
        return DecisionReport(
            self.book_id,
            session,
            halted=False,
            staged=tuple(staged),
            refused=tuple(refused),
            unpriced=tuple(unpriced),
        )

    def _hold_over_sell(self, session: date, order: BookOrder, held: int, last: LastTraded) -> None:
        """A sell decided on a suspended name: nothing staged, the exit held over (M17.13).

        It becomes a `PendingExit` leaving ``held - quantity`` shares, so `_work_pending_exits`
        offers it again every session until the name prints and the rails clear it.
        """
        parent = f"{self.book_id}:{order.isin}:{session.isoformat()}:held"
        self.pending_exits[order.isin] = PendingExit(
            isin=order.isin,
            parent_uid=parent,
            decided=session,
            parent_quantity=order.quantity,
            floor=max(held - order.quantity, 0),
            rationale=order.rationale,
            event=order.event,
            children=0,
        )
        self._journal_exit_held(session, order.isin, parent, order.event, last)

    def _journal_exit_held(
        self, session: date, isin: str, parent: str, event: str, last: LastTraded
    ) -> None:
        self._write(
            self._entry(
                session,
                actor=Actor.EXEC,
                decision=Decision.DEFERRED,
                isin=isin,
                rationale=(
                    f"{isin} is suspended (held, not trading since {last.session.isoformat()}): "
                    "the sell is not staged at a made-up price and is offered again next session"
                ),
                payload={
                    "event": SUSPENDED_EXIT_HELD_EVENT,
                    "exit_parent": parent,
                    "order_event": event,
                    "last_trade_date": last.session.isoformat(),
                },
            )
        )

    def _stage(
        self,
        order: BookOrder,
        proposed: ProposedOrder,
        session: date,
        *,
        exit_payload: Mapping[str, str] | None = None,
    ) -> str:
        uid = self.account.stage(proposed.request)
        if order.side is Side.SELL:
            self.staged_sells[uid] = (order.event, order.rationale)
        payload = {
            "event": order.event,
            "order_uid": uid,
            "quantity": str(proposed.quantity),
            "reference_price": str(proposed.price),
            "notional": str(proposed.value),
        }
        if exit_payload is not None:
            payload.update(exit_payload)
            payload.setdefault("exit_parent", uid)
        self._write(
            self._entry(
                session,
                actor=Actor.EXEC,
                decision=Decision.BUY if order.side is Side.BUY else Decision.SELL,
                isin=order.isin,
                rationale=order.rationale,
                payload=payload,
            ).model_copy(update={"orders_ref": uid})
        )
        return uid

    def _work_pending_exits(
        self,
        session: date,
        named: set[str],
        sellable: Portfolio,
        engine: RailEngine,
        staged: list[tuple[BookOrder, str]],
        refused: list[tuple[BookOrder, RailAssessment]],
    ) -> dict[str, int]:
        """Stage this session's child of every parent exit still open; the shares each staged.

        The remainder is what the book holds above the parent's floor *now*, after this
        session's fills — so a child that filled is gone from it and one that did not is offered
        again. A child is cleared through every rail against this session's facts; a refusal is
        journaled by A8 and the exit stays open for the next session.
        """
        worked: dict[str, int] = {}
        for isin in sorted(self.pending_exits):
            pending = self.pending_exits[isin]
            lot = sellable.lot(isin)
            remainder = (0 if lot is None else lot.quantity) - pending.floor
            if isin in named or remainder <= 0:
                superseded = isin in named and remainder > 0
                del self.pending_exits[isin]
                self._write(
                    self._entry(
                        session,
                        actor=Actor.EXEC,
                        decision=Decision.HOLD,
                        isin=isin,
                        rationale=(
                            f"exit {pending.parent_uid} of {pending.parent_quantity} "
                            + (
                                f"replaced by this session's decision with {remainder} unsold"
                                if superseded
                                else f"complete after {pending.children} child order(s)"
                            )
                        ),
                        payload={
                            "event": EXIT_SUPERSEDED_EVENT if superseded else EXIT_COMPLETE_EVENT,
                            "exit_parent": pending.parent_uid,
                            "exit_parent_quantity": str(pending.parent_quantity),
                            "exit_children": str(pending.children),
                            "exit_remaining": str(max(remainder, 0)),
                        },
                    )
                )
                continue
            order = BookOrder(isin, Side.SELL, remainder, pending.rationale, pending.event)
            proposed = self._proposed(order, session)
            last = None if proposed is not None else self.suspension(isin, session)
            if last is not None:
                self._journal_exit_held(session, isin, pending.parent_uid, pending.event, last)
                continue
            if proposed is None:
                self._write(
                    self._entry(
                        session,
                        actor=Actor.EXEC,
                        decision=Decision.DEFERRED,
                        isin=isin,
                        rationale=(
                            f"no close for {isin} on {session.isoformat()} to value exit "
                            f"{pending.parent_uid}'s next child at; offered again next session"
                        ),
                        payload={"event": "UNPRICED", "exit_parent": pending.parent_uid},
                    )
                )
                continue
            facts = self._facts(proposed, session, spendable=self.account.spendable_cash)
            clearance = engine.guard_book_exit(
                proposed, sellable, self.rails, facts, trading_date=session, sleeve=BOOK_SLEEVE
            )
            if not clearance.allowed:
                refused.append((order, clearance.assessment))
                continue
            child = clearance.child
            number = pending.children + 1
            uid = self._stage(
                replace(order, quantity=child.quantity),
                child,
                session,
                exit_payload={
                    "exit_parent": pending.parent_uid,
                    "exit_parent_quantity": str(pending.parent_quantity),
                    "exit_child_quantity": str(child.quantity),
                    "exit_remaining": str(remainder - child.quantity),
                    "exit_child": str(number),
                },
            )
            staged.append((order, uid))
            self.pending_exits[isin] = replace(pending, children=number)
            worked[isin] = child.quantity
        return worked

    def pending_exits_document(self) -> list[dict[str, str]]:
        """The parent exits still being worked, to persist with the book."""
        return [self.pending_exits[isin].to_document() for isin in sorted(self.pending_exits)]

    @staticmethod
    def pending_exits_from(documents: Sequence[Mapping[str, str]]) -> dict[str, PendingExit]:
        """The inverse of `pending_exits_document`."""
        exits = [PendingExit.from_document(document) for document in documents]
        return {pending.isin: pending for pending in exits}

    def valuation_close(self, isin: str, session: date) -> Decimal | None:
        """What a held ``isin`` is worth a share at ``session``'s close: its close, or — for a name
        `delisted` says has delisted, or `suspended` says is suspended — its last traded raw
        close. None when none of them exists.

        For valuing the book only (caps, marks, the manager's weights). An order is still priced
        at the session's own close (`_proposed`), so a delisted or suspended name is never offered
        to the market at a stale price.
        """
        price = self.market.close(isin, session)
        if price is None and self.delisted is not None:
            last = self.delisted.last_traded(isin, session)
            if last is not None:
                price = last.raw_close
        if price is None:
            suspended = self.suspension(isin, session)
            if suspended is not None:
                price = suspended.raw_close
        return price

    def suspension(self, isin: str, session: date) -> LastTraded | None:
        """Where ``isin`` last traded, if it is suspended on ``session`` (module docstring): no
        close today, not delisted, and `suspended` says the session otherwise printed normally.
        None for a name that printed, delisted, or a gap the suspension test does not cover."""
        if self.suspended is None or self.market.close(isin, session) is not None:
            return None
        if self.delisted is not None and self.delisted.last_traded(isin, session) is not None:
            return None
        return self.suspended.last_traded(isin, session)

    def suspended_holdings(self, session: date) -> tuple[SuspendedHolding, ...]:
        """Every held name suspended on ``session``, by ISIN."""
        out: list[SuspendedHolding] = []
        for isin, quantity in sorted(self.account.quantities().items()):
            if quantity <= 0:
                continue
            last = self.suspension(isin, session)
            if last is not None:
                out.append(
                    SuspendedHolding(
                        isin=isin,
                        quantity=quantity,
                        session=session,
                        last=last,
                        sessions_suspended=self.market.sessions_between(last.session, session),
                    )
                )
        return tuple(out)

    def journal_suspended_holdings(self, session: date) -> tuple[SuspendedHolding, ...]:
        """Journal ``SUSPENDED_HOLDING`` for every held name suspended on ``session``; them.

        One line per name per session it is suspended (a `HEARTBEAT`: bookkeeping, not a
        decision), carrying the last trade date, its raw close the book is valued at, and how many
        sessions it has been suspended.
        """
        held = self.suspended_holdings(session)
        for holding in held:
            evidence = EvidenceBundle(
                case_id=self.book_id,
                trading_date=session,
                actor=Actor.EXEC,
                items=(
                    EvidenceItem(
                        kind=EvidenceKind.PRICE,
                        source="m17_suspended",
                        label="last_traded_close",
                        isin=holding.isin,
                        as_of=holding.last.session,
                        value=holding.last.raw_close,
                    ),
                    EvidenceItem(
                        kind=EvidenceKind.POSITION,
                        source="m17_suspended",
                        label="quantity",
                        isin=holding.isin,
                        as_of=session,
                        value=Decimal(holding.quantity),
                    ),
                ),
            )
            self._write(
                self._entry(
                    session,
                    actor=Actor.EXEC,
                    decision=Decision.HEARTBEAT,
                    isin=holding.isin,
                    rationale=(
                        f"held, not trading since {holding.last.session.isoformat()}: suspended "
                        f"for {holding.sessions_suspended} session(s); valued at its last traded "
                        f"close {holding.last.raw_close}, never traded at a made-up price"
                    ),
                    payload={
                        "event": SUSPENDED_HOLDING_EVENT,
                        "last_trade_date": holding.last.session.isoformat(),
                        "last_close": str(holding.last.raw_close),
                        "quantity": str(holding.quantity),
                        "sessions_suspended": str(holding.sessions_suspended),
                    },
                    evidence_ref=evidence.ref().ref,
                ),
                evidence,
            )
            _LOG.warning(
                "fm_books.suspended_holding",
                book=self.book_id,
                isin=holding.isin,
                session=session.isoformat(),
                last_trade_date=holding.last.session.isoformat(),
                sessions_suspended=holding.sessions_suspended,
            )
        return held

    def _portfolio(self, session: date) -> Portfolio:
        lots: list[Lot] = []
        for isin, quantity in sorted(self.account.quantities().items()):
            if quantity <= 0:
                continue
            price = self.valuation_close(isin, session)
            if price is None:
                raise BookError(
                    f"{self.book_id}: held {isin} has no close on {session.isoformat()} and is "
                    "neither delisted nor suspended on a session that printed normally; the book "
                    "cannot be valued, so no cap can be checked"
                )
            lots.append(
                Lot(isin=isin, sector=self.market.sector(isin), quantity=quantity, price=price)
            )
        return Portfolio(case_id=self.book_id, lots=tuple(lots), cash=self.account.cash_value)

    def _proposed(self, order: BookOrder, session: date) -> ProposedOrder | None:
        price = self.market.close(order.isin, session)
        if price is None:
            return None
        request = OrderRequest(
            isin=order.isin, side=order.side, quantity=order.quantity, tag=self.account.account_id
        )
        return ProposedOrder(request=request, price=price, sector=self.market.sector(order.isin))

    def _facts(self, order: ProposedOrder, session: date, *, spendable: Decimal) -> BookOrderFacts:
        lookback = self.rails.participation_lookback_sessions
        history = self.market.traded_values(order.isin, through=session, sessions=lookback)
        late = [day for day, _ in history if day > session]
        if late:
            raise FutureDataError(
                f"{self.book_id}: the market answered {order.isin}'s traded value at "
                f"{session.isoformat()} with {late[0].isoformat()}; a decision never reads ahead"
            )
        values = [value for _, value in history][-lookback:]
        bought = self.last_buy_fill.get(order.isin)
        return BookOrderFacts(
            decision_session=session,
            series=self.market.series(order.isin, session),
            median_traded_value=median_traded_value(values) if len(values) >= lookback else None,
            median_sessions=len(values),
            sessions_since_buy_fill=(
                None if bought is None else self.market.sessions_between(bought, session)
            ),
            spendable_cash=spendable,
        )

    def staged_sells_document(self) -> dict[str, list[str]]:
        """The sells staged for the next session (uid → [event, rationale]), to persist."""
        return {uid: [event, why] for uid, (event, why) in sorted(self.staged_sells.items())}

    @staticmethod
    def staged_sells_from(document: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, str]]:
        """The inverse of `staged_sells_document`."""
        return {uid: (pair[0], pair[1]) for uid, pair in document.items()}

    def buy_fills_document(self) -> dict[str, str]:
        """The last-buy-fill record (ISIN → ISO date): the min-hold rail's state, to persist."""
        return {isin: day.isoformat() for isin, day in sorted(self.last_buy_fill.items())}

    @staticmethod
    def buy_fills_from(document: Mapping[str, str]) -> dict[str, date]:
        """The inverse of `buy_fills_document`."""
        return {isin: date.fromisoformat(day) for isin, day in document.items()}


def _less(portfolio: Portfolio, isin: str, quantity: int) -> Portfolio:
    """``portfolio`` with ``quantity`` fewer shares of ``isin`` sellable (cash untouched)."""
    lots: list[Lot] = []
    for lot in portfolio.lots:
        if lot.isin != isin:
            lots.append(lot)
        elif lot.quantity > quantity:
            lots.append(replace(lot, quantity=lot.quantity - quantity))
    return Portfolio(case_id=portfolio.case_id, lots=tuple(lots), cash=portfolio.cash)


# ── every book ───────────────────────────────────────────────────────────────────────────────────


class FundDesk:
    """Every M17 manager and control book, under the one kill switch.

    What it does: run a session's fills for every book (`execute`) and clear and stage every
    book's orders (`decide`). It reads the kill switch once at the top of `execute`, so either
    every book fills or every book halts — a break found reconciling one book trips the switch for
    every book's *decision*, never for the fills already due.
    What it assumes: each book's account, journal stream and rails are its own (checked here).
    What it never does: move an order, a share or a rupee between books.
    """

    __slots__ = ("_books", "_kill_switch")

    def __init__(self, books: Sequence[FundBook], *, kill_switch: KillSwitch) -> None:
        ids = [book.book_id for book in books]
        if len(set(ids)) != len(ids):
            raise BookError(f"two M17 books share an id: {sorted(ids)}")
        if len({id(book.account) for book in books}) != len(books):
            raise BookError("two M17 books share one paper account")
        accounts = [book.account.account_id for book in books]
        if len(set(accounts)) != len(accounts):
            raise BookError(f"two M17 books share an account id: {sorted(accounts)}")
        if any(book.kill_switch is not kill_switch for book in books):
            raise BookError("every M17 book trades under the one M17 kill switch")
        self._books = {book.book_id: book for book in books}
        self._kill_switch = kill_switch

    @property
    def books(self) -> Mapping[str, FundBook]:
        return dict(self._books)

    def execute(self, session: date) -> dict[str, ExecutionReport]:
        """Every book's fills for ``session`` — or every book's halt, if the switch is tripped."""
        halted = self._kill_switch.is_tripped
        return {
            book_id: book.halt(session) if halted else book._execute_fills(session)
            for book_id, book in self._books.items()
        }

    def decide(
        self, session: date, orders: Mapping[str, Sequence[BookOrder]]
    ) -> dict[str, DecisionReport]:
        """Every book's orders for ``session``; a book absent from ``orders`` decides nothing."""
        unknown = sorted(set(orders) - set(self._books))
        if unknown:
            raise BookError(f"orders for books the desk does not hold: {unknown}")
        return {
            book_id: book.decide(session, orders.get(book_id, ()))
            for book_id, book in self._books.items()
        }


def tradable_mandates(roster: Roster) -> tuple[ManagerMandate | ControlMandate, ...]:
    """The roster's books that trade a paper account: managers and controls, not the bench."""
    return tuple(b for b in roster.books if not isinstance(b, BenchMandate))
