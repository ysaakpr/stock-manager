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
   and journals an `ESCALATE` (``payload.event = RECON_BREAK``).
2. `FundBook.decide(session, orders)` — each `BookOrder` is valued at the session's close and
   cleared through the rails: sells first, against what the book holds; then buys, against the
   book without those sells (a sale frees neither a name slot nor cash before it has filled and
   settled). A refused order is journaled `RAIL_BLOCK` with ``payload.rails`` naming every rail
   that refused it; an allowed one is staged and journaled `BUY`/`SELL`.

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
from decimal import Decimal
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
from execution.kill_switch import KillSwitch, kill_switch_path
from execution.recon import ReconResult

__all__ = [
    "BOOK_HALTED_EVENT",
    "BOOK_SLEEVE",
    "M17_KILL_SWITCH_ACCOUNT",
    "PAPER_MODE",
    "RECON_BREAK_EVENT",
    "RECON_EVENT",
    "AccountSession",
    "BookAccount",
    "BookError",
    "BookJournal",
    "BookMarket",
    "BookOrder",
    "DecisionReport",
    "ExecutionReport",
    "FundBook",
    "FundDesk",
    "FutureDataError",
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

    def __post_init__(self) -> None:
        # OrderRequest validates the ISIN and the whole-share quantity; fail at construction.
        OrderRequest(isin=self.isin, side=self.side, quantity=self.quantity)
        if not self.rationale.strip():
            raise ValueError("a book order carries the decision's rationale")


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

    def __post_init__(self) -> None:
        if not self.book_id.strip():
            raise ValueError("a book needs an id")
        self.last_buy_fill = dict(self.last_buy_fill)

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
        self._journal_halt(session, "filled", lapsed)
        _LOG.warning("fm_books.halted", book=self.book_id, session=session.isoformat())
        return ExecutionReport(self.book_id, session, halted=True, lapsed=lapsed)

    def _execute_fills(self, session: date) -> ExecutionReport:
        run = self.account.execute_session(session)
        for fill in run.fills:
            if fill.side is Side.BUY:
                self.last_buy_fill[fill.isin] = fill.session
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

        Sells are cleared first against what the book holds, then buys against the book without
        those sells; within a side, in the order given. A refused order is journaled `RAIL_BLOCK`
        by A8; an order with no close to value it at is journaled `DEFERRED`; a staged order is
        journaled `BUY`/`SELL` with its uid. A session with no orders journals a `HOLD`.
        Raises `BookError` for two orders in one ISIN (one decision per name), or a held name with
        no close (the book cannot be valued, so no cap can be checked).
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
        for order in (o for o in orders if o.side is Side.SELL):
            proposed = self._proposed(order, session)
            if proposed is None:
                unpriced.append(order)
                continue
            facts = self._facts(proposed, session, spendable=self.account.spendable_cash)
            verdict = engine.guard_book_order(
                proposed, sellable, self.rails, facts, trading_date=session, sleeve=BOOK_SLEEVE
            )
            if not verdict.allowed:
                refused.append((order, verdict))
                continue
            staged.append((order, self._stage(order, proposed, session)))
            sellable = _less(sellable, order.isin, order.quantity)

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
        if not orders:
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

    def _stage(self, order: BookOrder, proposed: ProposedOrder, session: date) -> str:
        uid = self.account.stage(proposed.request)
        self._write(
            self._entry(
                session,
                actor=Actor.EXEC,
                decision=Decision.BUY if order.side is Side.BUY else Decision.SELL,
                isin=order.isin,
                rationale=order.rationale,
                payload={
                    "event": "STAGED",
                    "order_uid": uid,
                    "quantity": str(order.quantity),
                    "reference_price": str(proposed.price),
                    "notional": str(proposed.value),
                },
            ).model_copy(update={"orders_ref": uid})
        )
        return uid

    def _portfolio(self, session: date) -> Portfolio:
        lots: list[Lot] = []
        for isin, quantity in sorted(self.account.quantities().items()):
            if quantity <= 0:
                continue
            price = self.market.close(isin, session)
            if price is None:
                raise BookError(
                    f"{self.book_id}: held {isin} has no close on {session.isoformat()}; the book "
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
