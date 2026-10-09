"""M17.5 — one M17 book's paper account on the M15.3 shared execution path.

`analyst.fundmanager.books` holds the book side of an M17 manager or control (rails, journal
stream); this is the account side, behind its `BookAccount` protocol: the paper ``SimBroker`` (the
OPEN reference and the existing ``SlippageModel`` at their defaults, the one shared cost model),
the account's own accounting book, and the three ``execution`` objects the M15.3 paper session runs
— ``StagingCoordinator`` (kill switch first, then the broker, then the order journal),
``Reconciler`` (the accounting book against the broker after every session's fills) and the
shared ``KillSwitch``. It lives under ``backtest/`` beside ``paper_session`` because the decision
layer never names a concrete broker (invariant #5).

**Idle cash** earns repo - 50 bp through the existing schedule (``backtest.cash_interest``),
exactly as in the backtests: the month's credit at the top of a session, before its fills, posted
to the broker and to the accounting book alike; the settled balance recorded after the fills.
A session outside ``repo_rates.yaml``'s coverage raises rather than borrowing a rate.

**Corporate actions** (M17.7) are booked as the M15.3 paper session books them, at the top of a
session before its fills, on both the broker and the accounting book. The account is handed the
corporate-action store (a ``BookActionSource``) and remembers which actions it has seen
(``paper_session.action_identity``), so each is booked once. An action whose ex-date is after the
last session the account executed is booked the ordinary way, through ``BookActionApplier``. One
learnt *late* — the store refreshes weekly, so its ex-date has already passed — is booked on this
session against the holding entering its ex-date (the account keeps the last
`HISTORY_SESSIONS` sessions' holdings for this): a dividend is credited at that entitlement; a split
or bonus is applied when the name has not traded since and the holding is unchanged; anything
else on a held name (a late merger, an action the store has no terms for, a held name traded
since) is not booked and comes back ``ESCALATED`` for the book to journal and halt on. Each one
the account saw on a held name comes back in ``AccountSession.corporate_actions``.

**Upper-circuit buys** (Amendment 1 e, `backtest.fm_circuit`): before the fills, every staged buy
whose fill session is locked at the upper band is cancelled and comes back in
``AccountSession.unfilled``.

**Paper only, structurally.** The one broker this module builds is a ``SimBroker``, checked by
``backtest.paper_session.require_paper_broker`` (exactly ``SimBroker``, no subclass) before any
order reaches it; nothing here takes a broker, reads ``Settings.broker_provider`` or imports
``execution.kite_broker`` (tests/unit/test_fm_books.py walks the import graph).

**Persistence reuses the M15.3 ledger.** `M17PaperAccount.to_document` is shaped like a
``paper_session.book_state`` (its ``broker`` key is a ``SimBrokerState`` document, so
``PaperSessionRecord.broker_state()`` reads it), ``book_digest`` is the same digest the paper
session checks on restore, and ``paper_account_id`` is a valid ``paper_session.book_id`` — so each
M17 book is its own row stream in the existing tables, with no new schema.

What it never does: read a wall clock, take a float, or share any state with another account.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any, Final

from analyst.fundmanager.books import (
    AccountSession,
    BookedCorporateAction,
    CorporateActionStatus,
    UnfilledOrder,
)
from analyst.journal.evidence import canonical_bytes, digest_of
from backtest.accounting import BookPosition, PortfolioBook
from backtest.book_actions import (
    AppliedCarry,
    AppliedCashExit,
    AppliedDividend,
    AppliedMerger,
    AppliedRescale,
    AppliedSchemeCash,
    BookAction,
    BookActionApplier,
    BookActionCalendar,
    BookActionSource,
    CashDividend,
    IsinReissue,
    RescaleKind,
    ShareRescale,
    UnmodelledAction,
)
from backtest.cash_interest import CashInterestAccrual, RepoRateSchedule
from backtest.fm_circuit import CircuitMarket, upper_circuit_lock
from backtest.paper_session import action_identity, require_paper_broker
from dataplatform.clock import Clock, FrozenClock
from dataplatform.logging import get_logger
from execution.broker import Fill, OrderRequest, OrderStatus, Side
from execution.costs import CostModel, load_rate_card
from execution.kill_switch import KillSwitch
from execution.recon import Alerter, LoggingAlerter, Reconciler
from execution.sim_broker import SessionMarket, SimBroker, SimBrokerState
from execution.staging import InMemoryOrderJournal, StagedOrder, StagingCoordinator

__all__ = [
    "ACCOUNT_STATE",
    "HISTORY_SESSIONS",
    "M17_ORDER_SLEEVE",
    "CorporateActionLedger",
    "M17PaperAccount",
    "state_digest",
]

_LOG = get_logger(__name__)

#: The account's state for state-wise stamp duty — the same a-priori choice every backtest makes
#: (``backtest.run``), so an M17 book pays exactly the costs its backtests assumed.
ACCOUNT_STATE: Final = "MH"
#: The ``order_.sleeve`` an M17 order is staged under.
M17_ORDER_SLEEVE: Final = "M17"
#: How many executed sessions' holdings the account keeps to book a late action against. The
#: corporate-action store refreshes weekly, so a late action is days late; ~4 months is ample, and
#: one older than that is escalated rather than booked on a guessed entitlement.
HISTORY_SESSIONS: Final = 90


def state_digest(state: SimBrokerState) -> str:
    """sha256 of the broker state's canonical bytes — ``paper_session.book_digest``'s definition."""
    return digest_of(canonical_bytes(state.to_document()))


class _AccountBook:
    """The account's accounting book: what reconciliation checks the broker against.

    Posted with every fill the staging step processed and every interest credit, persisted with the
    account and restored from that — never re-read off the broker, which would make reconciliation
    compare the broker with itself. Satisfies ``execution.staging.FillLedger`` and
    ``execution.recon.ReconBook``.
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
    def from_document(cls, document: Mapping[str, Any]) -> _AccountBook:
        return cls(
            PortfolioBook.seeded(
                Decimal(document["cash"]),
                [
                    BookPosition(
                        position["isin"], int(position["quantity"]), Decimal(position["cost_basis"])
                    )
                    for position in document["positions"]
                ],
            )
        )


def _order_uid(account_id: str, decided: date, seq: int) -> str:
    return f"{account_id}:{decided.isoformat()}:{seq:03d}"


class CorporateActionLedger:
    """What the account remembers about corporate actions: when it opened, what it has booked,
    and the holdings (and names traded) of its last `HISTORY_SESSIONS` executed sessions.

    Persisted with the account (``to_document``); never re-derived from the broker.
    """

    __slots__ = ("booked", "history", "opened_on")

    def __init__(
        self,
        opened_on: date,
        booked: set[str] | None = None,
        history: list[tuple[date, dict[str, int], frozenset[str]]] | None = None,
    ) -> None:
        self.opened_on = opened_on
        self.booked: set[str] = set() if booked is None else set(booked)
        self.history: list[tuple[date, dict[str, int], frozenset[str]]] = (
            [] if history is None else list(history)
        )

    @property
    def last_session(self) -> date | None:
        return self.history[-1][0] if self.history else None

    def entering(self, isin: str, ex_date: date) -> int | None:
        """Shares of ``isin`` held entering ``ex_date`` — after the last executed session before
        it. Zero when the account had executed no session before it since opening; None when that
        session has aged out of the history (the entitlement is not known)."""
        before = [entry for entry in self.history if entry[0] < ex_date]
        if before:
            return before[-1][1].get(isin, 0)
        if len(self.history) >= HISTORY_SESSIONS:
            return None
        return 0

    def traded_since(self, isin: str, ex_date: date) -> bool:
        return any(isin in traded for day, _, traded in self.history if day >= ex_date)

    def record(self, session: date, held: Mapping[str, int], traded: frozenset[str]) -> None:
        self.history.append((session, {k: v for k, v in held.items() if v}, traded))
        del self.history[:-HISTORY_SESSIONS]

    def to_document(self) -> dict[str, Any]:
        return {
            "opened_on": self.opened_on.isoformat(),
            "booked": sorted(self.booked),
            "history": [
                {
                    "session": day.isoformat(),
                    "held": {isin: str(qty) for isin, qty in sorted(held.items())},
                    "traded": sorted(traded),
                }
                for day, held, traded in self.history
            ],
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> CorporateActionLedger:
        return cls(
            date.fromisoformat(document["opened_on"]),
            set(document["booked"]),
            [
                (
                    date.fromisoformat(entry["session"]),
                    {isin: int(qty) for isin, qty in entry["held"].items()},
                    frozenset(entry["traded"]),
                )
                for entry in document["history"]
            ],
        )


class M17PaperAccount:
    """One M17 book's paper account: ``SimBroker`` + accounting book + staging + recon + interest.

    Satisfies `analyst.fundmanager.books.BookAccount`. Build a new one with `open`, continue a
    persisted one with `restore`. What it never does: hold a broker it did not build itself.
    """

    __slots__ = (
        "_account_id",
        "_accrual",
        "_actions",
        "_book",
        "_circuit",
        "_clock",
        "_coordinator",
        "_ledger",
        "_orders",
        "_reconciler",
        "_seq",
        "_sim",
    )

    def __init__(
        self,
        *,
        account_id: str,
        sim: SimBroker,
        book: _AccountBook,
        accrual: CashInterestAccrual,
        kill_switch: KillSwitch,
        clock: Clock,
        alerter: Alerter,
        corporate_actions: BookActionSource,
        circuit: CircuitMarket,
        ledger: CorporateActionLedger,
    ) -> None:
        self._sim = require_paper_broker(sim)
        self._account_id = account_id
        self._book = book
        self._accrual = accrual
        self._clock = clock
        self._actions = corporate_actions
        self._circuit = circuit
        self._ledger = ledger
        self._orders = InMemoryOrderJournal()
        self._seq: dict[date, int] = {}
        for order in self._rehydrated():
            self._orders.record_staged(order)
        self._coordinator = StagingCoordinator(
            broker=self._sim, kill_switch=kill_switch, journal=self._orders, book=book, clock=clock
        )
        self._reconciler = Reconciler(
            broker=self._sim, book=book, kill_switch=kill_switch, alerter=alerter, clock=clock
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(account_id={self._account_id!r})"

    @classmethod
    def open(
        cls,
        *,
        account_id: str,
        opening_cash: Decimal,
        market: SessionMarket,
        kill_switch: KillSwitch,
        clock: Clock,
        schedule: RepoRateSchedule,
        corporate_actions: BookActionSource,
        circuit: CircuitMarket,
        alerter: Alerter | None = None,
    ) -> M17PaperAccount:
        """A fresh account holding ``opening_cash`` and nothing else, opened on ``clock.today()``.

        ``corporate_actions`` is the store's actions (an action ex on or before the opening day
        is never the account's); ``circuit`` reads fill sessions' bars and bands.
        """
        if not isinstance(opening_cash, Decimal) or opening_cash <= 0:
            raise ValueError(f"opening cash must be a positive Decimal, got {opening_cash!r}")
        sim = SimBroker(
            clock=clock, cost_model=_cost_model(), market=market, opening_cash=opening_cash
        )
        return cls(
            account_id=account_id,
            sim=sim,
            book=_AccountBook(PortfolioBook(opening_cash)),
            accrual=CashInterestAccrual(schedule),
            kill_switch=kill_switch,
            clock=clock,
            alerter=LoggingAlerter() if alerter is None else alerter,
            corporate_actions=corporate_actions,
            circuit=circuit,
            ledger=CorporateActionLedger(clock.today()),
        )

    @classmethod
    def restore(
        cls,
        document: Mapping[str, Any],
        *,
        account_id: str,
        market: SessionMarket,
        kill_switch: KillSwitch,
        clock: Clock,
        schedule: RepoRateSchedule,
        book_digest: str,
        corporate_actions: BookActionSource,
        circuit: CircuitMarket,
        alerter: Alerter | None = None,
    ) -> M17PaperAccount:
        """The account ``to_document`` described, refusing a broker state that misses its digest.

        A document without the corporate-action ledger predates M17.7 and is refused: restoring it
        with an empty ledger would re-book every action since the account opened.
        """
        if "corporate_actions" not in document:
            raise ValueError(
                f"{account_id}: the persisted account has no corporate-action ledger; refusing "
                "to guess which actions it has already booked"
            )
        state = SimBrokerState.from_document(document["broker"])
        if state_digest(state) != book_digest:
            raise ValueError(
                f"{account_id}: the persisted broker state does not reproduce its digest "
                f"{book_digest}; refusing to trade on an altered book"
            )
        sim = SimBroker.restore(state, clock=clock, cost_model=_cost_model(), market=market)
        return cls(
            account_id=account_id,
            sim=sim,
            book=_AccountBook.from_document(document["expected_book"]),
            accrual=CashInterestAccrual.from_document(schedule, document["cash_interest"]),
            kill_switch=kill_switch,
            clock=clock,
            alerter=LoggingAlerter() if alerter is None else alerter,
            corporate_actions=corporate_actions,
            circuit=circuit,
            ledger=CorporateActionLedger.from_document(document["corporate_actions"]),
        )

    # -- BookAccount ------------------------------------------------------------------------------

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def spendable_cash(self) -> Decimal:
        return self._sim.margins().available

    @property
    def cash_value(self) -> Decimal:
        return self._sim.margins().cash_value

    def quantities(self) -> Mapping[str, int]:
        held: dict[str, int] = {}
        for holding in self._sim.holdings():
            held[holding.isin] = held.get(holding.isin, 0) + holding.quantity
        for position in self._sim.positions():
            held[position.isin] = held.get(position.isin, 0) + position.quantity
        return held

    def execute_session(self, session: date) -> AccountSession:
        """Interest due → corporate actions → upper-circuit buys cancelled → the session's fills
        through the staging step → accrual → reconcile."""
        last = self._ledger.last_session
        if last is not None and session <= last:
            raise ValueError(
                f"{self._account_id}: session {session.isoformat()} is not after the last "
                f"executed session {last.isoformat()}"
            )
        credited = Decimal(0)
        credit = self._accrual.credit_due(session)
        if credit is not None:
            self._sim.credit_interest(session, credit.amount, credit.description)
            self._book.book.credit_interest(session, credit.amount)
            credited = credit.amount
        actions = self._book_corporate_actions(session)
        unfilled = self._cancel_locked_buys(session)
        executed = self._coordinator.execute(session)
        self._accrual.close_session(session, self._sim.interest_bearing_cash)
        recon = self._reconciler.reconcile(session)
        fills = tuple(done.fill for done in executed if done.fill is not None)
        self._ledger.record(session, self.quantities(), frozenset(f.isin for f in fills))
        return AccountSession(
            session=session,
            fills=fills,
            interest_credited=credited,
            recon=recon,
            cash=self._book.cash,
            quantities=dict(self._book.quantities()),
            broker_cash=self._sim.margins().cash_value,
            broker_quantities=self.quantities(),
            corporate_actions=actions,
            unfilled=unfilled,
        )

    # -- corporate actions ------------------------------------------------------------------------

    def _book_corporate_actions(self, session: date) -> tuple[BookedCorporateAction, ...]:
        last = self._ledger.last_session
        due = [
            action
            for action in self._actions.between(self._ledger.opened_on, session)
            if action_identity(action) not in self._ledger.booked
        ]
        on_time = [a for a in due if last is None or a.ex_date > last]
        late = [a for a in due if last is not None and a.ex_date <= last]
        seen: list[BookedCorporateAction] = []
        for action in late:
            record = self._book_late(action, session)
            if record is not None:
                seen.append(record)
        seen.extend(self._book_on_time(on_time, session))
        self._ledger.booked.update(action_identity(a) for a in due)
        for record in seen:
            _LOG.info(
                "fm_paper.corporate_action",
                account=self._account_id,
                action=record.identity,
                status=record.status.value,
                late=record.late,
                session=session.isoformat(),
            )
        return tuple(seen)

    def _book_on_time(
        self, actions: list[BookAction], session: date
    ) -> list[BookedCorporateAction]:
        """``BookActionApplier`` over the actions ex in ``(last session, session]``."""
        if not actions:
            return []
        # Which actions found shares to act on, read before the applier changes the holdings.
        held_before = {a.isin: self._sim.held_quantity(a.isin) for a in actions}
        for action in actions:
            if isinstance(action, IsinReissue):
                held_before[action.from_isin] = self._sim.held_quantity(action.from_isin)
        applier = BookActionApplier(BookActionCalendar(actions))
        applier.apply(session, sim=self._sim, book=self._book.book)
        seen: list[BookedCorporateAction] = []
        for applied in applier.log:
            seen.append(self._record_applied(applied, actions))
        for action in actions:
            unbooked = isinstance(action, UnmodelledAction) or (
                isinstance(action, IsinReissue) and not action.explained
            )
            isin = action.from_isin if isinstance(action, IsinReissue) else action.isin
            if unbooked and held_before.get(isin, 0) > 0:
                seen.append(
                    BookedCorporateAction(
                        identity=action_identity(action),
                        isin=isin,
                        ex_date=action.ex_date,
                        kind=_kind_of(action),
                        status=CorporateActionStatus.ESCALATED,
                        late=False,
                        entitled=held_before[isin],
                        detail=(
                            f"{_kind_of(action)} on held {isin} ex {action.ex_date.isoformat()} "
                            "has no terms the book can apply"
                        ),
                    )
                )
        return seen

    @staticmethod
    def _record_applied(
        applied: AppliedDividend
        | AppliedCarry
        | AppliedRescale
        | AppliedMerger
        | AppliedCashExit
        | AppliedSchemeCash,
        actions: list[BookAction],
    ) -> BookedCorporateAction:
        booked = CorporateActionStatus.BOOKED
        if isinstance(applied, AppliedDividend):
            action = next(
                a for a in actions if isinstance(a, CashDividend) and a.isin == applied.isin
            )
            return BookedCorporateAction(
                identity=action_identity(action),
                isin=applied.isin,
                ex_date=action.ex_date,
                kind="DIVIDEND",
                status=booked,
                late=False,
                entitled=int(applied.amount / action.per_share),
                detail=f"dividend {action.per_share}/share on {applied.isin} = {applied.amount}",
                cash=applied.amount,
            )
        if isinstance(applied, AppliedRescale):
            return BookedCorporateAction(
                identity=f"RESCALE:{applied.isin}:{applied.ex_date.isoformat()}:{applied.kind.value}",
                isin=applied.isin,
                ex_date=applied.ex_date,
                kind=applied.kind.value,
                status=booked,
                late=False,
                entitled=applied.old_quantity,
                detail=(
                    f"{applied.kind.value} {applied.numerator}:{applied.denominator} on "
                    f"{applied.isin}: {applied.old_quantity} shares became {applied.new_quantity}"
                ),
                rescale=(applied.numerator, applied.denominator),
            )
        if isinstance(applied, AppliedCarry):
            return BookedCorporateAction(
                identity=f"REISSUE:{applied.isin}:{applied.ex_date.isoformat()}",
                isin=applied.from_isin,
                ex_date=applied.ex_date,
                kind="REISSUE",
                status=booked,
                late=False,
                entitled=0,
                detail=f"holding in {applied.from_isin} carried 1:1 to {applied.isin}",
            )
        if isinstance(applied, AppliedMerger):
            return BookedCorporateAction(
                identity=f"SWAP:{applied.from_isin}:{applied.ex_date.isoformat()}",
                isin=applied.from_isin,
                ex_date=applied.ex_date,
                kind="SWAP",
                status=booked,
                late=False,
                entitled=applied.old_quantity,
                detail=(
                    f"{applied.old_quantity} {applied.from_isin} became {applied.new_quantity} "
                    f"{applied.isin} at {applied.numerator}:{applied.denominator}"
                ),
            )
        if isinstance(applied, AppliedCashExit):
            return BookedCorporateAction(
                identity=f"EXIT:{applied.isin}:{applied.ex_date.isoformat()}",
                isin=applied.isin,
                ex_date=applied.ex_date,
                kind="EXIT",
                status=booked,
                late=False,
                entitled=applied.quantity,
                detail=f"{applied.quantity} {applied.isin} surrendered at {applied.price}",
                cash=applied.amount,
            )
        return BookedCorporateAction(
            identity=f"SWAP:{applied.isin}:{applied.ex_date.isoformat()}:CASH",
            isin=applied.isin,
            ex_date=applied.ex_date,
            kind="SCHEME_CASH",
            status=booked,
            late=False,
            entitled=applied.quantity,
            detail=f"scheme cash {applied.per_share}/share on {applied.isin} = {applied.amount}",
            cash=applied.amount,
        )

    def _book_late(self, action: BookAction, session: date) -> BookedCorporateAction | None:
        """One action learnt after the account executed past its ex-date (module docstring)."""
        isin = action.from_isin if isinstance(action, IsinReissue) else action.isin
        entitled = self._ledger.entering(isin, action.ex_date)
        held_now = self._sim.held_quantity(isin)
        if entitled == 0 and held_now == 0:
            return None
        kind = _kind_of(action)
        what = (
            f"{kind} on {isin} ex {action.ex_date.isoformat()}, learnt {session.isoformat()} "
            "after the book had executed past its ex-date"
        )

        def escalated(why: str) -> BookedCorporateAction:
            return BookedCorporateAction(
                identity=action_identity(action),
                isin=isin,
                ex_date=action.ex_date,
                kind=kind,
                status=CorporateActionStatus.ESCALATED,
                late=True,
                entitled=0 if entitled is None else entitled,
                detail=f"{what}; {why}",
            )

        if entitled is None:
            return escalated("its ex-date is older than the account's holding history")
        if entitled == 0:
            return None  # bought after the ex-date: not entitled, nothing to book
        if isinstance(action, CashDividend):
            amount = action.per_share * entitled
            self._sim.credit_corporate_cash(
                session,
                isin,
                amount,
                f"LATE DIVIDEND {entitled} x {action.per_share} ex {action.ex_date.isoformat()}",
            )
            self._book.book.credit_late_dividend(session, isin, amount)
            return BookedCorporateAction(
                identity=action_identity(action),
                isin=isin,
                ex_date=action.ex_date,
                kind=kind,
                status=CorporateActionStatus.BOOKED,
                late=True,
                entitled=entitled,
                detail=f"{what}; credited {entitled} x {action.per_share} = {amount}",
                cash=amount,
            )
        if (
            isinstance(action, ShareRescale)
            and action.carried_from is None
            and not self._ledger.traded_since(isin, action.ex_date)
            and held_now == entitled
        ):
            old, new = self._sim.apply_share_rescale(
                isin,
                numerator=action.numerator,
                denominator=action.denominator,
                ex_date=session,
            )
            _rescale_book(self._book.book, action)
            return BookedCorporateAction(
                identity=action_identity(action),
                isin=isin,
                ex_date=action.ex_date,
                kind=kind,
                status=CorporateActionStatus.BOOKED,
                late=True,
                entitled=entitled,
                detail=(
                    f"{what}; {action.numerator}:{action.denominator} rescaled {old} shares "
                    f"to {new}"
                ),
                rescale=(action.numerator, action.denominator),
            )
        return escalated(
            "the book has traded the name since, or the action is not a dividend or a "
            "split/bonus, so it cannot be booked mechanically — owner review"
        )

    # -- upper-circuit buys -----------------------------------------------------------------------

    def _cancel_locked_buys(self, session: date) -> tuple[UnfilledOrder, ...]:
        """Cancel every buy due this session whose session is locked at the upper band."""
        unfilled: list[UnfilledOrder] = []
        for order in self._sim.export_state().staged:
            if order.target_session != session or order.request.side is not Side.BUY:
                continue
            isin = order.request.isin
            bar = self._circuit.session_print(isin, session)
            if bar is None:
                continue
            check = upper_circuit_lock(bar, self._circuit.price_band(isin, session))
            if check is None or not check.locked:
                continue
            self._sim.cancel(order.order_id)
            unfilled.append(
                UnfilledOrder(
                    order_id=order.order_id,
                    isin=isin,
                    quantity=order.request.quantity,
                    session=session,
                    open=bar.open,
                    prev_close=bar.prev_close,
                    threshold=check.threshold,
                    basis=check.basis.value,
                )
            )
            _LOG.info(
                "fm_paper.unfilled_upper_circuit",
                account=self._account_id,
                order_id=order.order_id,
                isin=isin,
                session=session.isoformat(),
                basis=check.basis.value,
            )
        return tuple(unfilled)

    def lapse(self, session: date) -> tuple[str, ...]:
        lapsed: list[str] = []
        for order in self._sim.export_state().staged:
            if order.target_session <= session and order.status is OrderStatus.STAGED:
                self._sim.cancel(order.order_id)
                lapsed.append(order.order_id)
                _LOG.info(
                    "fm_paper.order_lapsed",
                    account=self._account_id,
                    order_id=order.order_id,
                    isin=order.request.isin,
                    target_session=order.target_session.isoformat(),
                )
        return tuple(lapsed)

    def stage(self, request: OrderRequest) -> str:
        day = self._clock.today()
        seq = self._seq.get(day, 0)
        staged = self._coordinator.stage(
            request,
            case_id=self._account_id,
            sleeve=M17_ORDER_SLEEVE,
            uid=_order_uid(self._account_id, day, seq),
        )
        self._seq[day] = seq + 1
        return staged.order_uid

    # -- persistence ------------------------------------------------------------------------------

    def to_document(self) -> dict[str, Any]:
        """The account as a ``paper_session.book_state``-shaped document (strings, no floats)."""
        return {
            "broker": self._sim.export_state().to_document(),
            "expected_book": self._book.to_document(),
            "cash_interest": self._accrual.to_document(),
            "corporate_actions": self._ledger.to_document(),
        }

    @property
    def book_digest(self) -> str:
        """The digest `restore` checks the broker part against."""
        return state_digest(self._sim.export_state())

    def _rehydrated(self) -> list[StagedOrder]:
        """Staging records for the orders still staged on a restored broker (same uids)."""
        records: list[StagedOrder] = []
        for order in self._sim.export_state().staged:
            seq = self._seq.get(order.decision_date, 0)
            self._seq[order.decision_date] = seq + 1
            records.append(
                StagedOrder(
                    order_uid=_order_uid(self._account_id, order.decision_date, seq),
                    case_id=self._account_id,
                    isin=order.request.isin,
                    side=order.request.side,
                    quantity=order.request.quantity,
                    exchange=order.request.exchange,
                    sleeve=M17_ORDER_SLEEVE,
                    broker_order_id=order.order_id,
                    staged_at=FrozenClock(order.decision_date).now(),
                    staged_for_date=order.target_session,
                )
            )
        return records


def _kind_of(action: BookAction) -> str:
    if isinstance(action, CashDividend):
        return "DIVIDEND"
    if isinstance(action, ShareRescale):
        return action.kind.value
    if isinstance(action, IsinReissue):
        return "REISSUE"
    if isinstance(action, UnmodelledAction):
        return f"UNMODELLED:{action.action_type}"
    return type(action).__name__.upper()


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


def _cost_model() -> CostModel:
    return CostModel(load_rate_card(), account_state=ACCOUNT_STATE)
