"""X1: order staging — the EOD half of the two-step order lifecycle, and the internal book.

EXECUTION_PLAN §6, M5.12 spec: "EOD decisions produce STAGED orders → executed next session → a
daily reconciliation job comparing broker positions/ledger against the internal book." This module
owns the first two arrows and the *internal book* the third one reconciles against.

The order lifecycle has two halves that happen a session apart:

1. **Stage (after the close).** A decision hands an `OrderRequest` to `StagingCoordinator.stage`.
   Before anything reaches the broker the kill switch is consulted (`require_placement_allowed`) —
   a halted account places nothing. The order is placed on the injected `Broker` (STAGED) and then
   *journaled* into durable storage before the next session ever runs. That ordering is the point
   of acceptance criterion 3: a staged order that is not written down before it can execute is an
   order the platform could execute and then have no record of having intended.

2. **Execute (the next session).** `execute` runs the broker's session, and for every fill posts
   the result back to the same order record (STAGED → EXECUTED) and into the **internal book** —
   the platform's own tally of what it believes it holds and how much cash it has, built from the
   fills *it* processed. That book is deliberately a separate object from whatever the broker
   reports: reconciliation (`recon.py`) only means something if the two sides are independent, so
   the book is our expectation and `broker.holdings()` is the broker's claim, and a daily job
   checks they still agree.

The order journal is a seam (`OrderJournal`), not a hard dependency on Postgres.
`PostgresOrderJournal` persists to the `order_` table (0001_init.sql) for the real daily loop;
`InMemoryOrderJournal` is the same contract without a database, for the paper run (M5.13) and for
tests that do not need durability. The coordinator depends only on the protocol — it never learns
which one it was handed, exactly as it never learns whether the `Broker` is `SimBroker` or
`KiteBroker` (invariant #5).

Money is `Decimal` throughout; identity is the ISIN and only the ISIN (invariant #2); time is an
injected `Clock` (B10). Nothing here reads a wall clock or joins on a symbol.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

import structlog
from psycopg.types.json import Json

from dataplatform.clock import Clock, SystemClock
from dataplatform.store.db import Connection
from execution.broker import Exchange, Fill, Order, OrderRequest, OrderStatus, Side
from execution.kill_switch import KillSwitch
from execution.sim_broker import SimBroker

_LOG = structlog.get_logger(__name__)

__all__ = [
    "BookPosition",
    "InMemoryOrderJournal",
    "InternalBook",
    "OrderJournal",
    "PostgresOrderJournal",
    "StagedOrder",
    "StagingCoordinator",
]


# ── the staged-order record ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StagedOrder:
    """One order as the staging layer records it — the request plus its case, sleeve and lifecycle.

    Distinct from `broker.Order`: the broker knows an order by its own id and lifecycle; the
    platform additionally knows which *case* and *sleeve* it belongs to (the analyst's context,
    which the broker neither has nor needs) and carries its own idempotency key, `order_uid`, so a
    retried placement after an ambiguous broker response cannot become two records. Frozen — each
    transition is a new record, matching the append-then-update shape of the `order_` table.
    """

    order_uid: str
    case_id: str
    isin: str
    side: Side
    quantity: int
    exchange: Exchange
    sleeve: str
    broker_order_id: str
    staged_at: datetime
    staged_for_date: date
    state: OrderStatus = OrderStatus.STAGED
    fill: Fill | None = None

    def executed(self, fill: Fill) -> StagedOrder:
        """A COMPLETE copy carrying the fill — the post-execution transition of this record."""
        return StagedOrder(
            order_uid=self.order_uid,
            case_id=self.case_id,
            isin=self.isin,
            side=self.side,
            quantity=self.quantity,
            exchange=self.exchange,
            sleeve=self.sleeve,
            broker_order_id=self.broker_order_id,
            staged_at=self.staged_at,
            staged_for_date=self.staged_for_date,
            state=OrderStatus.COMPLETE,
            fill=fill,
        )


# ── the internal book ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BookPosition:
    """The platform's belief about one holding: a whole-share count, keyed by ISIN (#2)."""

    isin: str
    exchange: Exchange
    quantity: int


class InternalBook:
    """The platform's own tally of holdings and cash — the "internal book" recon checks the broker.

    Built only from fills the coordinator processed, so it is genuinely independent of what the
    broker later reports: if the two diverge, one of them is wrong, and that divergence is exactly
    what daily reconciliation exists to catch (§6). A buy adds shares and pays cash (turnover plus
    costs); a sell removes shares and receives cash (turnover minus costs) — the same signed cash
    effect the broker's ledger records, computed here from `Fill.net_cash` so the two use one
    definition of what a fill costs (invariant #4, via `execution.costs`).

    What it never does: read the broker. It is the *expectation* side of the reconciliation; reading
    the broker to build it would make the comparison compare the broker to itself.
    """

    __slots__ = ("_cash", "_exchange", "_holdings")

    def __init__(self, *, opening_cash: Decimal) -> None:
        if not isinstance(opening_cash, Decimal):
            raise TypeError("opening_cash must be a Decimal — money is never float (CLAUDE.md)")
        self._cash = opening_cash
        self._holdings: dict[str, int] = {}
        self._exchange: dict[str, Exchange] = {}

    @property
    def cash(self) -> Decimal:
        """Cash the platform believes it has, after every fill it has applied."""
        return self._cash

    def apply(self, fill: Fill) -> None:
        """Post a fill to the book: adjust the share count for its ISIN and the cash balance.

        A sell that would drive the held quantity negative raises rather than recording an
        impossible position — the platform selling more than it thinks it holds is itself a
        reconciliation-grade defect, and it should fail here loudly, not silently net to a short.
        """
        held = self._holdings.get(fill.isin, 0)
        if fill.side is Side.BUY:
            self._holdings[fill.isin] = held + fill.quantity
            self._exchange[fill.isin] = fill.exchange
        else:
            remaining = held - fill.quantity
            if remaining < 0:
                raise ValueError(
                    f"internal book would go short on {fill.isin}: holds {held}, "
                    f"sell fill is {fill.quantity}"
                )
            if remaining == 0:
                self._holdings.pop(fill.isin, None)
                self._exchange.pop(fill.isin, None)
            else:
                self._holdings[fill.isin] = remaining
        self._cash += fill.net_cash

    def holdings(self) -> tuple[BookPosition, ...]:
        """The believed holdings, one per ISIN, in ISIN order (a stable order for recon)."""
        return tuple(
            BookPosition(isin=isin, exchange=self._exchange[isin], quantity=quantity)
            for isin, quantity in sorted(self._holdings.items())
        )

    def quantities(self) -> Mapping[str, int]:
        """The believed share count per ISIN — the shape recon compares to the broker's."""
        return dict(sorted(self._holdings.items()))

    # ── drill support ──────────────────────────────────────────────────────────────────────────

    def force_set_quantity(self, isin: str, quantity: int, *, exchange: Exchange) -> None:
        """Overwrite a believed quantity directly — for reconciliation *drills* only (§8.3.5).

        A drill injects a mismatch between the book and the broker to prove the recon job freezes
        and alerts. There is no production path to this: the book is otherwise only ever moved by a
        real fill, and a divergence created any other way is precisely the bug recon guards against.
        """
        if quantity < 0:
            raise ValueError(f"quantity must be non-negative, got {quantity}")
        if quantity == 0:
            self._holdings.pop(isin, None)
            self._exchange.pop(isin, None)
        else:
            self._holdings[isin] = quantity
            self._exchange[isin] = exchange


# ── the order-journal seam ─────────────────────────────────────────────────────────────────────


class OrderJournal(Protocol):
    """Where staged orders are recorded, and where their executions are posted back.

    A protocol so the coordinator never depends on Postgres directly: the real daily loop injects
    `PostgresOrderJournal` (the durable `order_` table), and the paper run and tests inject
    `InMemoryOrderJournal`. Both honour the same two-step contract — record on stage, update on
    execution — which is what makes "journaled before execution" a property of the coordinator
    rather than of one storage backend.
    """

    def record_staged(self, order: StagedOrder) -> None:
        """Persist a newly staged order. Called before the target session can execute it."""

    def record_executed(self, order: StagedOrder) -> None:
        """Post a fill back to an already-recorded order, moving it to EXECUTED."""

    def staged(self, order_uid: str) -> StagedOrder:
        """Read one recorded order back by its uid. Raises `KeyError` if unknown."""


class InMemoryOrderJournal:
    """An `OrderJournal` kept in a dict — durable enough for a paper run, offline for a test.

    Records and updates the same `StagedOrder` records `PostgresOrderJournal` would, without a
    database. Used by the reconciliation drill and by M5.13's paper run, where the point is the
    end-to-end lifecycle, not persistence across a process boundary (the kill switch owns that).
    """

    __slots__ = ("_orders",)

    def __init__(self) -> None:
        self._orders: dict[str, StagedOrder] = {}

    def record_staged(self, order: StagedOrder) -> None:
        if order.order_uid in self._orders:
            raise ValueError(f"order {order.order_uid} is already staged")
        self._orders[order.order_uid] = order

    def record_executed(self, order: StagedOrder) -> None:
        if order.order_uid not in self._orders:
            raise KeyError(f"cannot record execution of unknown order {order.order_uid}")
        self._orders[order.order_uid] = order

    def staged(self, order_uid: str) -> StagedOrder:
        return self._orders[order_uid]

    def all(self) -> tuple[StagedOrder, ...]:
        """Every recorded order, in insertion order — the paper run's read-back surface."""
        return tuple(self._orders.values())


class PostgresOrderJournal:
    """An `OrderJournal` backed by the `order_` table (0001_init.sql) — the durable prod store.

    `record_staged` inserts the row STAGED, before the session that fills it runs; `record_executed`
    updates it to EXECUTED with the fill's price, gross, costs and the cost breakdown from the one
    shared cost model (invariant #4), so a later ledger break against the broker can be attributed
    to a component. The caller owns the transaction (matching `dataplatform.store.db`), so a day can
    stage several orders and journal them atomically.

    What it assumes: the `case_` row and the `security_master` rows the orders reference already
    exist — orders are keyed on real cases and real ISINs, and the FKs enforce that here.
    """

    __slots__ = ("_broker_tag", "_clock", "_conn")

    def __init__(
        self, conn: Connection, *, broker_tag: str = "SIM", clock: Clock | None = None
    ) -> None:
        if broker_tag not in ("SIM", "KITE"):
            raise ValueError(f"broker_tag must be 'SIM' or 'KITE', got {broker_tag!r}")
        self._conn = conn
        self._broker_tag = broker_tag
        self._clock = SystemClock() if clock is None else clock

    def record_staged(self, order: StagedOrder) -> None:
        now = self._clock.now()
        self._conn.execute(
            "INSERT INTO order_ (order_uid, case_id, isin, exchange, sleeve, side, order_type, "
            "quantity, state, broker, broker_order_id, staged_at, staged_for_date, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'MARKET', %s, 'STAGED', %s, %s, %s, %s, %s)",
            (
                order.order_uid,
                order.case_id,
                order.isin,
                order.exchange.value,
                order.sleeve,
                order.side.value,
                order.quantity,
                self._broker_tag,
                order.broker_order_id,
                order.staged_at,
                order.staged_for_date,
                now,
            ),
        )

    def record_executed(self, order: StagedOrder) -> None:
        fill = order.fill
        if fill is None:
            raise ValueError(f"order {order.order_uid} has no fill to record as executed")
        now = self._clock.now()
        cost = fill.cost
        # Per-component, as the shared cost model emitted it (invariant #4), rendered as strings so
        # a Decimal round-trips exactly through jsonb — a JSON number read back is a float, a bug.
        breakdown = {
            "brokerage": str(cost.brokerage),
            "securities_transaction_tax": str(cost.securities_transaction_tax),
            "exchange_transaction_charge": str(cost.exchange_transaction_charge),
            "sebi_turnover_fee": str(cost.sebi_turnover_fee),
            "goods_and_services_tax": str(cost.goods_and_services_tax),
            "stamp_duty": str(cost.stamp_duty),
            "depository_charge": str(cost.depository_charge),
        }
        rows = self._conn.execute(
            "UPDATE order_ SET state = 'EXECUTED', executed_at = %s, filled_quantity = %s, "
            "avg_fill_price_inr = %s, gross_value_inr = %s, costs_inr = %s, net_value_inr = %s, "
            "cost_breakdown = %s, broker_order_id = %s, updated_at = %s "
            "WHERE order_uid = %s AND state = 'STAGED'",
            (
                now,
                fill.quantity,
                fill.fill_price,
                fill.gross,
                fill.cost.total,
                fill.cost.net_amount,
                Json(breakdown),
                order.broker_order_id,
                now,
                order.order_uid,
            ),
        ).rowcount
        if rows != 1:
            raise KeyError(
                f"cannot record execution of {order.order_uid}: no STAGED row matched "
                "(unknown order, or it was already executed)"
            )

    def staged(self, order_uid: str) -> StagedOrder:
        row = self._conn.execute(
            "SELECT order_uid, case_id, isin, exchange, sleeve, side, quantity, "
            "broker_order_id, staged_at, staged_for_date, state FROM order_ WHERE order_uid = %s",
            (order_uid,),
        ).fetchone()
        if row is None:
            raise KeyError(f"no order_ row with order_uid {order_uid}")
        return StagedOrder(
            order_uid=row[0],
            case_id=row[1],
            isin=row[2],
            side=Side(row[5]),
            quantity=int(row[6]),
            exchange=Exchange(row[3]),
            sleeve=row[4],
            broker_order_id=row[7] or "",
            staged_at=row[8],
            staged_for_date=row[9],
            state=OrderStatus(row[10]),
        )

    def due_for(self, session: date) -> tuple[StagedOrder, ...]:
        """Every order staged for `session` that is still STAGED — the execution read surface."""
        rows = self._conn.execute(
            "SELECT order_uid, case_id, isin, exchange, sleeve, side, quantity, broker_order_id, "
            "staged_at, staged_for_date, state FROM order_ "
            "WHERE staged_for_date = %s AND state = 'STAGED' ORDER BY staged_at, order_uid",
            (session,),
        ).fetchall()
        return tuple(
            StagedOrder(
                order_uid=row[0],
                case_id=row[1],
                isin=row[2],
                side=Side(row[5]),
                quantity=int(row[6]),
                exchange=Exchange(row[3]),
                sleeve=row[4],
                broker_order_id=row[7] or "",
                staged_at=row[8],
                staged_for_date=row[9],
                state=OrderStatus(row[10]),
            )
            for row in rows
        )

    def state_of(self, order_uid: str) -> str:
        """The lifecycle state string of one order — the drill's cheap "is it STAGED yet" probe."""
        row = self._conn.execute(
            "SELECT state FROM order_ WHERE order_uid = %s", (order_uid,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no order_ row with order_uid {order_uid}")
        return str(row[0])


# ── the coordinator ────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class StagingCoordinator:
    """Stages EOD orders through a broker and journals them, then executes and books the fills.

    Wires the four collaborators of the order lifecycle: the `Broker` (invariant #5 — a protocol,
    never a concrete broker), the `KillSwitch` (consulted before every placement), the
    `OrderJournal` (staged orders written down before they can execute), and the `InternalBook`
    (the platform's tally, which `recon.py` checks against the broker). Time is an injected `Clock`.

    The flow is exactly the M5.12 spec's: `stage` places one order (STAGED) and journals it;
    `execute(session)` fills the session's staged orders and posts each fill to the journal and the
    book. Reconciliation is a separate job (`recon.py`) run after execution, so a mismatch it finds
    is a mismatch between two things that were built independently.
    """

    broker: SimBroker
    kill_switch: KillSwitch
    journal: OrderJournal
    book: InternalBook
    clock: Clock = field(default_factory=SystemClock)
    _seq: int = field(default=0, init=False)

    def stage(
        self, request: OrderRequest, *, case_id: str, sleeve: str, uid: str | None = None
    ) -> StagedOrder:
        """Place `request` (STAGED) and journal it — refusing outright if the switch is tripped.

        The kill switch is consulted *first*: a halted account never reaches the broker, so a trip
        by rails or recon halts placement immediately (acceptance 2). The broker then stages the
        order for the next session, and it is journaled before this method returns — before any
        session can execute it (acceptance 3). Returns the recorded `StagedOrder`.
        """
        self.kill_switch.require_placement_allowed()
        placed: Order = self.broker.place(request)
        order = StagedOrder(
            order_uid=self._issue_uid() if uid is None else uid,
            case_id=case_id,
            isin=request.isin,
            side=request.side,
            quantity=request.quantity,
            exchange=request.exchange,
            sleeve=sleeve,
            broker_order_id=placed.order_id,
            staged_at=self.clock.now(),
            staged_for_date=placed.target_session,
        )
        self.journal.record_staged(order)
        _LOG.info(
            "staging.staged",
            order_uid=order.order_uid,
            case_id=case_id,
            isin=order.isin,
            side=order.side.value,
            quantity=order.quantity,
            staged_for=order.staged_for_date.isoformat(),
        )
        return order

    def execute(self, session: date) -> tuple[StagedOrder, ...]:
        """Fill the session's staged orders, then journal and book every completed fill.

        Runs the broker's session (settlement plus fills), and for each COMPLETE order posts the
        fill back to the journal (STAGED → EXECUTED) and into the internal book. Rejected orders
        are left as the broker resolved them and are not booked — a fill that did not happen must
        not move the platform's tally. Returns the completed staged orders.
        """
        filled = self.broker.execute_session(session)
        by_broker_id = {order.broker_order_id: order for order in self._recorded_for(session)}
        executed: list[StagedOrder] = []
        for resolved in filled:
            if resolved.status is not OrderStatus.COMPLETE or resolved.fill is None:
                continue
            staged = by_broker_id.get(resolved.order_id)
            if staged is None:  # pragma: no cover - a fill for an order we never staged is a bug
                raise KeyError(f"broker filled {resolved.order_id} but no staged order records it")
            done = staged.executed(resolved.fill)
            self.journal.record_executed(done)
            self.book.apply(resolved.fill)
            executed.append(done)
            _LOG.info(
                "staging.executed",
                order_uid=done.order_uid,
                isin=done.isin,
                fill_price=str(resolved.fill.fill_price),
            )
        return tuple(executed)

    def _recorded_for(self, session: date) -> tuple[StagedOrder, ...]:
        """The staged orders due for `session`, read back from the journal to match against fills.

        Both concrete journals can enumerate their orders for a session; the bare `OrderJournal`
        protocol cannot, so `execute` needs one of the two concrete stores rather than an arbitrary
        recorder. That is a genuine requirement of the two-step lifecycle, not a leak — you cannot
        post fills back to records you cannot list.
        """
        if isinstance(self.journal, InMemoryOrderJournal):
            return tuple(o for o in self.journal.all() if o.staged_for_date == session)
        if isinstance(self.journal, PostgresOrderJournal):
            return self.journal.due_for(session)
        raise TypeError(
            "execute() needs an enumerable order journal; inject InMemoryOrderJournal or "
            "PostgresOrderJournal"
        )

    def _issue_uid(self) -> str:
        # Sequential and deterministic so a replay produces identical uids (B10, §8.3.3).
        uid = f"ORD-{self._seq:08d}"
        self._seq += 1
        return uid
