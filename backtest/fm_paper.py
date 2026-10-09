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

from analyst.fundmanager.books import AccountSession
from analyst.journal.evidence import canonical_bytes, digest_of
from backtest.accounting import BookPosition, PortfolioBook
from backtest.cash_interest import CashInterestAccrual, RepoRateSchedule
from backtest.paper_session import require_paper_broker
from dataplatform.clock import Clock, FrozenClock
from dataplatform.logging import get_logger
from execution.broker import Fill, OrderRequest, OrderStatus
from execution.costs import CostModel, load_rate_card
from execution.kill_switch import KillSwitch
from execution.recon import Alerter, LoggingAlerter, Reconciler
from execution.sim_broker import SessionMarket, SimBroker, SimBrokerState
from execution.staging import InMemoryOrderJournal, StagedOrder, StagingCoordinator

__all__ = ["ACCOUNT_STATE", "M17_ORDER_SLEEVE", "M17PaperAccount", "state_digest"]

_LOG = get_logger(__name__)

#: The account's state for state-wise stamp duty — the same a-priori choice every backtest makes
#: (``backtest.run``), so an M17 book pays exactly the costs its backtests assumed.
ACCOUNT_STATE: Final = "MH"
#: The ``order_.sleeve`` an M17 order is staged under.
M17_ORDER_SLEEVE: Final = "M17"


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


class M17PaperAccount:
    """One M17 book's paper account: ``SimBroker`` + accounting book + staging + recon + interest.

    Satisfies `analyst.fundmanager.books.BookAccount`. Build a new one with `open`, continue a
    persisted one with `restore`. What it never does: hold a broker it did not build itself.
    """

    __slots__ = (
        "_account_id",
        "_accrual",
        "_book",
        "_clock",
        "_coordinator",
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
    ) -> None:
        self._sim = require_paper_broker(sim)
        self._account_id = account_id
        self._book = book
        self._accrual = accrual
        self._clock = clock
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
        alerter: Alerter | None = None,
    ) -> M17PaperAccount:
        """A fresh account holding ``opening_cash`` and nothing else."""
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
        alerter: Alerter | None = None,
    ) -> M17PaperAccount:
        """The account ``to_document`` described, refusing a broker state that misses its digest."""
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
        """Interest due → the session's fills through the staging step → accrual → reconcile."""
        credited = Decimal(0)
        credit = self._accrual.credit_due(session)
        if credit is not None:
            self._sim.credit_interest(session, credit.amount, credit.description)
            self._book.book.credit_interest(session, credit.amount)
            credited = credit.amount
        executed = self._coordinator.execute(session)
        self._accrual.close_session(session, self._sim.interest_bearing_cash)
        recon = self._reconciler.reconcile(session)
        fills = tuple(done.fill for done in executed if done.fill is not None)
        return AccountSession(
            session=session,
            fills=fills,
            interest_credited=credited,
            recon=recon,
            cash=self._book.cash,
            quantities=dict(self._book.quantities()),
            broker_cash=self._sim.margins().cash_value,
            broker_quantities=self.quantities(),
        )

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


def _cost_model() -> CostModel:
    return CostModel(load_rate_card(), account_state=ACCOUNT_STATE)
