"""X1: `SimBroker` — the paper/backtest fill model behind the `Broker` interface.

EXECUTION_PLAN §6, invariant #5: `SimBroker` is one of the two things that satisfy `Broker`; the
decision code cannot tell it from `KiteBroker`. It simulates how an EOD decision actually fills:

* **Next-session execution.** An order placed after the close (`place`) is staged for the *next*
  trading session and fills only when that session is run (`execute_session`). A decision made on
  the evidence of day D never fills at day D's price — that would be trading on information the
  price already reflects.
* **A configured reference price.** The fill starts from either the next session's open or a
  conservative point inside its VWAP band (`ReferencePrice`), chosen once in the `FillPolicy` — not
  the close, which no EOD order can actually get.
* **Slippage in bps, scaled by liquidity.** On top of the reference, an adverse slippage whose size
  grows with the order's participation in the session's traded value: a large order in a thin name
  moves the price against itself more than a small order in a liquid one (`SlippageModel`). The
  resulting price is quantised to the paisa tick, adversely, so a fill is always a price an exchange
  could have printed.
* **The one shared cost model.** Every fill is priced by `execution.costs` — the *same* module the
  backtest uses (invariant #4). Two cost implementations would make paper and replay disagree about
  a strategy neither ever ran.
* **No fractional shares.** Enforced by `OrderRequest` at the interface; a fill is always a whole
  number of shares.
* **The settlement cycle of the trade date.** T+N is read per fill from the dated schedule in
  `execution.settlement` (T+2 from 2003, T+1 from 2023) and N is counted in *trading sessions* on
  the market's calendar (`SessionMarket.next_session`), never calendar days. A buy's shares become
  a deliverable holding, and a sale's proceeds spendable cash, only when their cycle allows.

Market data is injected as a `SessionMarket`, not read from DuckDB here: the query service (M4.1) is
one implementation of it, and a test supplies bars directly so the unit suite never touches the
store. Time is an injected `Clock` (B10) — `place` reads the decision date from it and nothing else
reads a wall clock, so a replay through `SimBroker` is byte-reproducible.

What it never does: fill at a price it cannot justify. A session with no reference bar, a buy the
cash cannot cover, or a sell with nothing to deliver is *rejected* with a reason on the order — a
visible refusal in the order book, never a silent skip or a phantom fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Protocol

import structlog

from dataplatform.clock import Clock
from execution.broker import (
    BrokerError,
    Exchange,
    Fill,
    Holding,
    LedgerEntry,
    Margins,
    Order,
    OrderNotModifiableError,
    OrderRequest,
    OrderStatus,
    Position,
    Side,
    UnknownOrderError,
)
from execution.costs import CostModel, Trade
from execution.settlement import SettlementSchedule, load_settlement_schedule

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS = Decimal("10000")
#: Fill prices are quoted to the paisa, as every exchange print is. Without this the slippage ratio
#: (order turnover / session turnover) puts a 28-digit repeating decimal on the fill, the ledger
#: tracks cash to 1e-25 of a rupee, and two sums of the same fills disagree in the last digit.
_TICK = Decimal("0.01")

_log = structlog.get_logger(__name__)

__all__ = [
    "DuplicateStagedOrderError",
    "FillPolicy",
    "NoReferenceBarError",
    "ReferenceBar",
    "ReferencePrice",
    "SessionMarket",
    "SimBroker",
    "SlippageModel",
]


# ── errors ─────────────────────────────────────────────────────────────────────────────────────


class NoReferenceBarError(BrokerError, LookupError):
    """The injected market has no bar for an (ISIN, session) a staged order needs to fill."""


class DuplicateStagedOrderError(BrokerError):
    """A second order was staged for a scrip that already has one staged for the same session.

    The EOD model nets to one order per scrip per session. The one exception is a sell staged
    beside other sells of the same scrip: the child orders of an exit A8 sliced to fit its
    per-order caps (`analyst.rails.slice_exit`). Those fill in the same session, are charged the
    per-scrip, per-day DP sell charge once between them (`CostModel.charge_all`), and pay slippage
    on their combined participation. A buy beside anything, or a sell beside a buy, is still
    refused: modify the standing order instead of stacking a second.
    """


# ── market data the fill model reads ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReferenceBar:
    """The next session's execution reference for one ISIN, as the fill model needs it.

    `open` and `vwap` are the two reference prices the policy can fill from; `traded_value` is the
    session's total turnover (₹), the liquidity that slippage is scaled against. All `Decimal`.
    A raw (unadjusted) execution price is correct here: a fill happens at the price that actually
    traded, not an adjusted one — adjusted series are for analysis (invariant #3), never execution.
    """

    isin: str
    session: date
    exchange: Exchange
    open: Decimal
    vwap: Decimal
    traded_value: Decimal

    def __post_init__(self) -> None:
        for name in ("open", "vwap", "traded_value"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal — money is never float (CLAUDE.md)")
        if self.open <= _ZERO or self.vwap <= _ZERO:
            raise ValueError("reference prices must be positive")
        if self.traded_value <= _ZERO:
            raise ValueError("traded_value must be positive; a zero-liquidity session cannot fill")


class SessionMarket(Protocol):
    """Where `SimBroker` gets the next session and its reference bars.

    The query service (M4.1) is the production implementation; a test supplies bars in memory. Kept
    a protocol so the fill model never imports DuckDB and the unit suite never touches the store.
    """

    def next_session(self, after: date) -> date:
        """The first trading session strictly after `after`."""

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        """The reference bar for `isin` on `session`. Raises `NoReferenceBarError` if absent."""


# ── the fill policy ──────────────────────────────────────────────────────────────────────────


class ReferencePrice(StrEnum):
    """Which reference price a fill starts from (EXECUTION_PLAN §6: open or conservative VWAP band).

    OPEN takes the next session's open. VWAP_BAND takes the session VWAP nudged conservatively —
    up for a buy, down for a sell, by `FillPolicy.vwap_band_bps` — modelling that an order worked
    through the session does not get the volume-weighted average exactly, but a little the wrong
    side of it.
    """

    OPEN = "OPEN"
    VWAP_BAND = "VWAP_BAND"


@dataclass(frozen=True, slots=True)
class SlippageModel:
    """Adverse slippage in basis points, scaled by the order's participation in session liquidity.

    `effective_bps = base_bps + impact_bps * participation`, where participation is the order's
    turnover divided by the session's traded value. A small order in a deep name pays essentially
    `base_bps`; an order that is a large fraction of the day's turnover pays much more — the linear
    market-impact intuition, kept deliberately simple and monotonic so it is easy to reason about
    and impossible to invert into a *favourable* fill.

    Both terms are `Decimal` bps. This is a modelling assumption, not a measured cost, which is why
    it is separate from `execution.costs` (those are real, published charges).
    """

    base_bps: Decimal = Decimal("2")
    impact_bps: Decimal = Decimal("50")

    def __post_init__(self) -> None:
        for name in ("base_bps", "impact_bps"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal")
            if value < _ZERO:
                raise ValueError(f"{name} must be non-negative, got {value}")

    def bps_for(self, *, order_turnover: Decimal, traded_value: Decimal) -> Decimal:
        """Effective slippage in bps for an order of `order_turnover` against `traded_value`.

        Participation is capped at 1: an order larger than the whole day's turnover would not fill
        at a single reference price anyway, so the model does not extrapolate impact past 100%.
        """
        participation = min(order_turnover / traded_value, _ONE)
        return self.base_bps + self.impact_bps * participation


@dataclass(frozen=True, slots=True)
class FillPolicy:
    """How `SimBroker` turns a reference bar into a fill price: reference choice, band and slippage.

    `vwap_band_bps` is used only when `reference is VWAP_BAND`. All adverse: whatever the reference,
    a buy fills at or above it and a sell at or below it, so the simulator never flatters a strategy
    with a fill better than the market plausibly gives.
    """

    reference: ReferencePrice = ReferencePrice.OPEN
    slippage: SlippageModel = field(default_factory=SlippageModel)
    vwap_band_bps: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        if not isinstance(self.vwap_band_bps, Decimal):
            raise TypeError("vwap_band_bps must be a Decimal")
        if self.vwap_band_bps < _ZERO:
            raise ValueError(f"vwap_band_bps must be non-negative, got {self.vwap_band_bps}")

    def reference_price(self, bar: ReferenceBar, side: Side) -> Decimal:
        """The reference price for `side` before slippage — open, or the conservative VWAP band."""
        if self.reference is ReferencePrice.OPEN:
            return bar.open
        band = _adverse(bar.vwap, side, self.vwap_band_bps)
        return band


def _adverse(price: Decimal, side: Side, bps: Decimal) -> Decimal:
    """Move `price` `bps` basis points in the direction that hurts `side` (up buy, down sell)."""
    factor = _ONE + bps / _BPS if side is Side.BUY else _ONE - bps / _BPS
    return price * factor


def _floor_ratio(quantity: int, numerator: Decimal, denominator: Decimal) -> int:
    """`quantity * numerator / denominator` floored to whole shares (multiply first)."""
    return int((quantity * numerator / denominator).to_integral_value(rounding=ROUND_FLOOR))


def _quantise_adverse(price: Decimal, side: Side) -> Decimal:
    """Round `price` to the paisa tick in the direction that hurts `side`.

    A buy rounds up to the next paisa, a sell down to the previous one, so quantisation can never
    flatter a fill. The exchange prints to a tick; a paper fill with 25 decimals is not a price any
    contract note could show, and it is also the source of last-digit disagreement between a ledger
    and an independent re-summing of its fills (`tests/property/test_sim_broker_property.py`).
    """
    rounding = ROUND_CEILING if side is Side.BUY else ROUND_FLOOR
    return price.quantize(_TICK, rounding=rounding)


# ── internal book ────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _PendingLot:
    """A buy that has filled but not settled: its shares are not yet deliverable.

    `traded` is the fill session and `lag` the N of its T+N, fixed at fill time from the schedule
    in force on `traded`: a trade dealt just before an era boundary keeps its own cycle.
    """

    isin: str
    traded: date
    lag: int
    lot: _Lot


@dataclass(frozen=True, slots=True)
class _Receivable:
    """A sale's net proceeds, owed by the clearing house until the trade settles."""

    isin: str
    traded: date
    lag: int
    amount: Decimal


@dataclass(slots=True)
class _Lot:
    """A running position in one scrip: whole-share quantity and its total cost basis (ex-charges).

    `average_price` is `cost / quantity`. Mutable and internal; the public `Position`/`Holding` are
    frozen snapshots taken from it.
    """

    exchange: Exchange
    quantity: int
    cost: Decimal

    @property
    def average_price(self) -> Decimal:
        return self.cost / self.quantity


# ── the simulator ────────────────────────────────────────────────────────────────────────────


class SimBroker:
    """A deterministic paper broker: stage EOD, fill next session, price with the shared cost model.

    Satisfies `Broker` (invariant #5). Construct it with an injected `Clock` (B10), the one shared
    `CostModel` (invariant #4), a `SessionMarket` for reference bars, a `FillPolicy`, and the
    opening cash, plus optionally the dated `SettlementSchedule` (the checked-in one by default).
    `place` stages an order for the next session; `execute_session(session)` settles whatever the
    cycle says is due, then fills every order staged for that session.

    What it never does: read a wall clock, invent a fill, or hold two implementations of Indian
    costs. Given the same market and the same order stream it produces byte-identical fills, which
    is what the replay engine (X2) is built on.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        cost_model: CostModel,
        market: SessionMarket,
        opening_cash: Decimal,
        policy: FillPolicy | None = None,
        settlement: SettlementSchedule | None = None,
    ) -> None:
        if not isinstance(opening_cash, Decimal):
            raise TypeError("opening_cash must be a Decimal — money is never float (CLAUDE.md)")
        if opening_cash < _ZERO:
            raise ValueError(f"opening_cash must be non-negative, got {opening_cash}")
        self._clock = clock
        self._costs = cost_model
        self._market = market
        self._policy = policy if policy is not None else FillPolicy()
        self._settlement = settlement if settlement is not None else load_settlement_schedule()

        self._cash: Decimal = opening_cash  # settled, spendable
        self._orders: dict[str, Order] = {}
        self._holdings: dict[str, _Lot] = {}  # settled
        self._positions: list[_PendingLot] = []  # filled buys awaiting settlement, in fill order
        self._receivables: list[
            _Receivable
        ] = []  # sale proceeds awaiting settlement, in fill order
        self._current_session: date | None = None
        # Proceeds released at the end of the last session for the *next* session's fills: in
        # `_cash` so tonight's decision can spend them, but not paid out until that session.
        self._released_ahead: Decimal = _ZERO
        self._ledger: list[LedgerEntry] = []
        self._next_order_seq: int = 0
        self._next_ledger_seq: int = 0

    # ── Broker: session ──────────────────────────────────────────────────────────────────────

    def session_valid(self) -> bool:
        """Always `True`: a paper session cannot expire, so the auth interlock never blocks it.

        The daily OAuth+2FA logout that dead-ends a real broker session (INVG/73992 §8.3.2.1.8) has
        no analogue here — `SimBroker` holds no API token to lose. Returning `True` unconditionally
        is what makes the AUTH_REQUIRED interlock a no-op in paper mode (acceptance 2) while it
        still guards the same seam that `KiteBroker` will fail at M8 (invariant #5).
        """
        return True

    # ── Broker: order lifecycle ──────────────────────────────────────────────────────────────

    def place(self, request: OrderRequest) -> Order:
        """Stage `request` for the next trading session after today (`Clock.today`).

        Rejects a second staged order for the same scrip and session (`DuplicateStagedOrderError`):
        the EOD model nets to one order per scrip — except that sells may stand beside sells, which
        is how a sliced exit's children reach the session together. The returned order is `STAGED`;
        it fills only when `execute_session` runs.
        """
        decision_date = self._clock.today()
        target = self._market.next_session(decision_date)
        for existing in self._orders.values():
            if (
                existing.status is OrderStatus.STAGED
                and existing.request.isin == request.isin
                and existing.target_session == target
                and not (existing.request.side is Side.SELL and request.side is Side.SELL)
            ):
                raise DuplicateStagedOrderError(
                    f"{request.isin} already has a staged order ({existing.order_id}) for "
                    f"{target.isoformat()}; modify it instead of staging a second"
                )
        order_id = self._issue_order_id()
        order = Order(
            order_id=order_id,
            request=request,
            status=OrderStatus.STAGED,
            decision_date=decision_date,
            target_session=target,
        )
        self._orders[order_id] = order
        _log.info(
            "sim_broker.staged",
            order_id=order_id,
            isin=request.isin,
            side=str(request.side),
            quantity=request.quantity,
            target_session=target.isoformat(),
        )
        return order

    def modify(self, order_id: str, *, quantity: int) -> Order:
        """Replace a staged order's quantity. Raises if the order is unknown or not staged."""
        order = self._staged_or_raise(order_id)
        new_request = OrderRequest(
            isin=order.request.isin,
            side=order.request.side,
            quantity=quantity,  # OrderRequest re-validates whole-share-ness and raises if not
            exchange=order.request.exchange,
            order_type=order.request.order_type,
            limit_price=order.request.limit_price,
            tag=order.request.tag,
        )
        modified = Order(
            order_id=order.order_id,
            request=new_request,
            status=OrderStatus.STAGED,
            decision_date=order.decision_date,
            target_session=order.target_session,
        )
        self._orders[order_id] = modified
        _log.info("sim_broker.modified", order_id=order_id, quantity=quantity)
        return modified

    def cancel(self, order_id: str) -> Order:
        """Cancel a staged order. Raises if the order is unknown or not staged."""
        order = self._staged_or_raise(order_id)
        cancelled = Order(
            order_id=order.order_id,
            request=order.request,
            status=OrderStatus.CANCELLED,
            decision_date=order.decision_date,
            target_session=order.target_session,
            reason="cancelled by caller",
        )
        self._orders[order_id] = cancelled
        _log.info("sim_broker.cancelled", order_id=order_id)
        return cancelled

    # ── fill model ───────────────────────────────────────────────────────────────────────────

    def execute_session(self, session: date) -> tuple[Order, ...]:
        """Fill every order staged for `session`, in placement order; return them post-fill.

        Settles first: every buy whose T+N has arrived rolls from positions into holdings, and every
        sale whose T+N has arrived credits its proceeds, so a sell staged for `session` can deliver
        shares that settled by now. Then each staged order for `session` is priced (reference →
        slippage → shared cost model) and either `COMPLETE` with a `Fill` or `REJECTED` with a
        reason (no bar, no cash, nothing to deliver).

        Several sells of one scrip (a sliced exit) are priced as the one order they are: slippage on
        their combined participation, so slicing never flatters the impact model, and the DP
        charge on the first of them alone, as the depository bills it.

        After the fills, sale proceeds that settle on the *next* session are released too: an EOD
        decision made tonight can only fill tomorrow, when that cash has been paid out. Proceeds
        from a sale filled in `session` are never usable by another fill in `session` (T+0).
        """
        self._settle_into(session)
        due = [
            order
            for order in self._orders.values()
            if order.status is OrderStatus.STAGED and order.target_session == session
        ]
        session_quantity: dict[tuple[str, Side], int] = {}
        for order in due:
            key = (order.request.isin, order.request.side)
            session_quantity[key] = session_quantity.get(key, 0) + order.request.quantity
        sold: dict[str, Trade] = {}
        filled: list[Order] = []
        for order in due:
            key = (order.request.isin, order.request.side)
            resolved = self._fill(
                order,
                session,
                session_quantity=session_quantity[key],
                earlier_sell=sold.get(order.request.isin),
            )
            self._orders[order.order_id] = resolved
            filled.append(resolved)
            if resolved.fill is not None and resolved.fill.side is Side.SELL:
                sold.setdefault(
                    order.request.isin,
                    Trade(
                        isin=resolved.fill.isin,
                        trade_date=session,
                        side=Side.SELL,
                        quantity=resolved.fill.quantity,
                        price=resolved.fill.fill_price,
                        exchange=resolved.fill.exchange,
                    ),
                )
        self._released_ahead = self._release_proceeds(session, inclusive=True)
        return tuple(filled)

    def _fill(
        self,
        order: Order,
        session: date,
        *,
        session_quantity: int,
        earlier_sell: Trade | None,
    ) -> Order:
        """Fill one staged order. ``session_quantity`` is every share of this scrip and side due
        this session (the order's own, unless it is one child of a sliced exit), and
        ``earlier_sell`` the first sell of this scrip already filled this session, if any."""
        request = order.request
        try:
            bar = self._market.reference_bar(request.isin, session)
        except NoReferenceBarError as exc:
            return self._reject(order, f"no reference bar for {session.isoformat()}: {exc}")

        reference = self._policy.reference_price(bar, request.side)
        turnover_at_reference = reference * session_quantity
        slippage_bps = self._policy.slippage.bps_for(
            order_turnover=turnover_at_reference, traded_value=bar.traded_value
        )
        fill_price = _quantise_adverse(
            _adverse(reference, request.side, slippage_bps), request.side
        )

        # Delivery reality: you cannot sell what has not settled, or buy without the cash.
        if request.side is Side.SELL:
            held = self._holdings.get(request.isin)
            if held is None or held.quantity < request.quantity:
                have = 0 if held is None else held.quantity
                return self._reject(
                    order, f"insufficient holdings to sell {request.quantity} (have {have})"
                )

        trade = Trade(
            isin=request.isin,
            trade_date=session,
            side=request.side,
            quantity=request.quantity,
            price=fill_price,
            exchange=request.exchange,
        )
        # The DP charge is per scrip per sell day: a later child of the same exit pays none.
        cost = (
            self._costs.charge(trade)
            if earlier_sell is None
            else self._costs.charge_all((earlier_sell, trade))[-1]
        )

        if request.side is Side.BUY and cost.net_amount > self._cash:
            return self._reject(
                order,
                f"insufficient cash for buy: need {cost.net_amount}, have {self._cash}",
            )

        fill = Fill(
            isin=request.isin,
            session=session,
            side=request.side,
            quantity=request.quantity,
            exchange=request.exchange,
            reference_price=reference,
            slippage_bps=slippage_bps,
            fill_price=fill_price,
            cost=cost,
        )
        self._apply(fill)
        _log.info(
            "sim_broker.filled",
            order_id=order.order_id,
            isin=request.isin,
            side=str(request.side),
            quantity=request.quantity,
            reference_price=str(reference),
            fill_price=str(fill_price),
            slippage_bps=str(slippage_bps),
            cost=str(cost.total),
        )
        return Order(
            order_id=order.order_id,
            request=request,
            status=OrderStatus.COMPLETE,
            decision_date=order.decision_date,
            target_session=order.target_session,
            fill=fill,
        )

    def _apply(self, fill: Fill) -> None:
        """Post a completed fill to the book: cash, positions/holdings, and one ledger line.

        A buy pays at once (the cash check already required it settled) and its shares wait for
        T+N as a position. A sell delivers settled shares at once and its proceeds wait for T+N as
        a receivable — never spendable cash on the fill session.
        """
        gross = fill.gross
        # Raises NoSettlementCycleError for a date the schedule does not cover — before the book
        # moves, so an unsettleable trade leaves no half-posted fill behind.
        lag = self._settlement.lag_for(fill.session, fill.isin)
        if fill.side is Side.BUY:
            self._cash += fill.net_cash  # net_cash is negative on a buy
            for pending in self._positions:
                if pending.isin == fill.isin and pending.traded == fill.session:
                    pending.lot.quantity += fill.quantity
                    pending.lot.cost += gross
                    break
            else:
                self._positions.append(
                    _PendingLot(
                        fill.isin, fill.session, lag, _Lot(fill.exchange, fill.quantity, gross)
                    )
                )
            self._post_ledger(fill, debit=fill.cost.net_amount, credit=_ZERO)
        else:
            self._receivables.append(_Receivable(fill.isin, fill.session, lag, fill.net_cash))
            self._reduce_holding(fill.isin, fill.quantity)
            self._post_ledger(fill, debit=_ZERO, credit=fill.cost.net_amount)

    def _reduce_holding(self, isin: str, quantity: int) -> None:
        """Remove `quantity` shares from a settled holding, cost basis reduced proportionally."""
        lot = self._holdings[isin]  # existence checked before the fill was allowed
        remaining = lot.quantity - quantity
        if remaining == 0:
            del self._holdings[isin]
            return
        # Cost basis scales with the shares that remain, so average_price is unchanged by a partial
        # sell — the sold shares leave at the same basis they entered.
        lot.cost = lot.cost * remaining / lot.quantity
        lot.quantity = remaining

    def _settle_into(self, session: date) -> None:
        """Advance to `session`, settling every buy and sale whose T+N is on or before it.

        A buy traded on T with cycle N becomes a deliverable holding for fills on the N-th trading
        session after T onward; its sale-side twin, a receivable, becomes spendable for the same
        fills. Called at the start of `execute_session`. Idempotent within a session: a settled
        lot or receivable is removed once rolled, so running a session twice never double-settles.
        """
        if self._current_session == session:
            return
        if self._current_session is not None and session < self._current_session:
            raise ValueError(
                f"cannot execute {session.isoformat()} after already settling "
                f"{self._current_session.isoformat()}; sessions run forward only"
            )
        still_pending: list[_PendingLot] = []
        for pending in self._positions:
            if not self._settles_by(pending.traded, pending.lag, session, inclusive=False):
                still_pending.append(pending)
                continue
            lot = pending.lot
            held = self._holdings.get(pending.isin)
            if held is None:
                self._holdings[pending.isin] = _Lot(lot.exchange, lot.quantity, lot.cost)
            else:
                held.quantity += lot.quantity
                held.cost += lot.cost
            _log.info(
                "sim_broker.settled_buy",
                isin=pending.isin,
                traded=pending.traded.isoformat(),
                lag_sessions=pending.lag,
                session=session.isoformat(),
            )
        self._positions = still_pending
        self._release_proceeds(session, inclusive=False)
        self._current_session = session

    def _release_proceeds(self, session: date, *, inclusive: bool) -> Decimal:
        """Credit to spendable cash every receivable due for a fill in `session` (or the next one).

        `inclusive=False` (start of a session) releases what settles on or before `session`;
        `inclusive=True` (end of a session) also releases what settles on the next session, the
        earliest any order decided tonight can fill. Returns the total released.
        """
        released = _ZERO
        still_owed: list[_Receivable] = []
        for receivable in self._receivables:
            if self._settles_by(receivable.traded, receivable.lag, session, inclusive=inclusive):
                self._cash += receivable.amount
                released += receivable.amount
                _log.info(
                    "sim_broker.settled_sale",
                    isin=receivable.isin,
                    traded=receivable.traded.isoformat(),
                    lag_sessions=receivable.lag,
                    session=session.isoformat(),
                    amount=str(receivable.amount),
                )
            else:
                still_owed.append(receivable)
        self._receivables = still_owed
        return released

    def _settles_by(self, traded: date, lag: int, session: date, *, inclusive: bool) -> bool:
        """Whether a trade on `traded` settling T+`lag` is settled for a fill on `session`.

        Walks trading sessions on the market's calendar, never calendar days. With `inclusive`,
        "for a fill on the session after `session`" instead. Only ever calls `next_session` on a
        date before `session`, so the walk never asks the market about a date past the replay.
        """
        # Settled for a fill on S  ⇔  the (lag-1)-th session after `traded` is strictly before S.
        # Settled for a fill on next(S)  ⇔  that session is on or before S.
        day = traded
        for _ in range(lag - 1):
            if day >= session:
                return False
            day = self._market.next_session(day)
        return day <= session if inclusive else day < session

    def _reject(self, order: Order, reason: str) -> Order:
        _log.warning(
            "sim_broker.rejected", order_id=order.order_id, isin=order.request.isin, reason=reason
        )
        return Order(
            order_id=order.order_id,
            request=order.request,
            status=OrderStatus.REJECTED,
            decision_date=order.decision_date,
            target_session=order.target_session,
            reason=reason,
        )

    # ── corporate actions (the backtest walk only) ───────────────────────────────────────────
    #
    # Not part of `Broker`: a live broker's account is adjusted by the depository, not by the
    # caller. The backtest walk (`backtest.book_actions`) is the only caller, on an ex-date, before
    # that session's `execute_session`. None of these reads a clock, a price or a signal.
    #
    # Entitlement follows the trade date, not the settlement state. The exchange fixes the record
    # date so that a buy traded before the ex-date is on the register: under T+2 the record date is
    # the session after the ex-date and a buy on ex-1 settles on it; under T+1 (from 2023) the two
    # coincide and a buy on ex-1 settles that day. Either way a buy traded before the ex-date is
    # entitled — to the split shares, the bonus, the dividend — even while its shares are still
    # pending on the ex-date, and a sale traded before it is not. So every lot below is counted by
    # `traded < ex_date`, pending or settled, and a rescaled pending lot settles its rescaled count.

    def held_quantity(self, isin: str, *, bought_before: date | None = None) -> int:
        """Shares of `isin` on the book: settled holdings plus every pending (unsettled) buy.

        With `bought_before`, a pending lot counts only if it traded before that date — the ex-date
        entitlement rule above. Settled holdings always traded earlier than any pending lot.
        """
        held = self._holdings.get(isin)
        total = 0 if held is None else held.quantity
        for pending in self._positions:
            if pending.isin != isin:
                continue
            if bought_before is not None and pending.traded >= bought_before:
                continue
            total += pending.lot.quantity
        return total

    def apply_share_rescale(
        self, isin: str, *, numerator: Decimal, denominator: Decimal, ex_date: date
    ) -> tuple[int, int]:
        """Multiply the entitled shares of `isin` by `numerator / denominator` (a split or bonus).

        Returns `(old, new)` whole-share counts over the entitled lots — settled holdings and every
        pending buy traded before `ex_date`. The *combined* count is rescaled and floored once, the
        same arithmetic `PortfolioBook` applies to its single position, so the two books agree share
        for share (the floored fraction is forfeited, see `PortfolioBook._rescale_quantity`). Each
        pending lot is scaled and floored on its own, so it settles the rescaled count at its T+N;
        whatever whole shares the per-lot floors leave over go to the settled holding, or, with
        none, to the latest pending lot. Cost basis is untouched on every lot. Every order still
        staged for `isin` is rescaled the same way (a limit price inversely, rounded against the
        trader) so a sell sized on the pre-split count still exits the whole position; one floored
        to zero is cancelled with the reason on it.
        """
        if numerator <= _ZERO or denominator <= _ZERO:
            raise ValueError("a share rescale needs positive terms")
        old = self.held_quantity(isin, bought_before=ex_date)
        new = _floor_ratio(old, numerator, denominator)
        entitled = [p for p in self._positions if p.isin == isin and p.traded < ex_date]
        for pending in entitled:
            pending.lot.quantity = _floor_ratio(pending.lot.quantity, numerator, denominator)
        remainder = new - sum(p.lot.quantity for p in entitled)
        held = self._holdings.get(isin)
        if held is not None:
            if remainder <= 0:
                del self._holdings[isin]
            else:
                held.quantity = remainder
        elif remainder > 0:
            entitled[-1].lot.quantity += remainder  # fill order: the last traded, last to settle
        self._positions = [p for p in self._positions if p.lot.quantity > 0]
        for order in list(self._orders.values()):
            if order.status is not OrderStatus.STAGED or order.request.isin != isin:
                continue
            self._orders[order.order_id] = self._rescaled_order(order, numerator, denominator)
        _log.info(
            "sim_broker.share_rescale",
            isin=isin,
            old_quantity=old,
            new_quantity=new,
            pending_lots=len(entitled),
            numerator=str(numerator),
            denominator=str(denominator),
        )
        return old, new

    def carry_over(self, from_isin: str, to_isin: str) -> int:
        """Move every share of `from_isin` to `to_isin`, 1:1, basis carried — an ISIN reissue.

        A face-value split on NSE usually retires the ISIN; the holder's shares continue under the
        successor. The settled holding, every pending lot (each keeping its own trade date and
        T+N, so it still settles when it would have) and every staged order move; returns the
        share count moved (0 when nothing was held under `from_isin`).
        """
        moved = 0
        lot = self._holdings.pop(from_isin, None)
        if lot is not None:
            moved += lot.quantity
            target = self._holdings.get(to_isin)
            if target is None:
                self._holdings[to_isin] = _Lot(lot.exchange, lot.quantity, lot.cost)
            else:
                target.quantity += lot.quantity
                target.cost += lot.cost
        merged: list[_PendingLot] = []
        for pending in self._positions:
            if pending.isin == from_isin:
                moved += pending.lot.quantity
                pending = _PendingLot(to_isin, pending.traded, pending.lag, pending.lot)
            twin = next(
                (m for m in merged if m.isin == pending.isin and m.traded == pending.traded), None
            )
            if twin is None:
                merged.append(pending)
            else:  # one position per (ISIN, fill session), as `_apply` keeps it
                twin.lot.quantity += pending.lot.quantity
                twin.lot.cost += pending.lot.cost
        self._positions = merged
        for order in list(self._orders.values()):
            if order.status is not OrderStatus.STAGED or order.request.isin != from_isin:
                continue
            self._orders[order.order_id] = replace(
                order, request=replace(order.request, isin=to_isin)
            )
        if moved:
            _log.info("sim_broker.carry_over", from_isin=from_isin, to_isin=to_isin, moved=moved)
        return moved

    def surrender(self, isin: str, *, ex_date: date) -> int:
        """Remove every entitled share of `isin` from the book — a delisting's cash exit.

        Settled holdings and every pending buy traded before `ex_date` go (cost and all); the
        caller credits the exit consideration with `credit_corporate_cash`. Every order still
        staged for `isin` is cancelled with the reason on it: the name no longer trades. Returns
        the share count surrendered (0 when nothing was held).
        """
        surrendered = self.held_quantity(isin, bought_before=ex_date)
        self._holdings.pop(isin, None)
        self._positions = [
            p for p in self._positions if not (p.isin == isin and p.traded < ex_date)
        ]
        for order in list(self._orders.values()):
            if order.status is not OrderStatus.STAGED or order.request.isin != isin:
                continue
            self._orders[order.order_id] = replace(
                order,
                status=OrderStatus.CANCELLED,
                reason="cancelled: the name was delisted and its shares surrendered for cash",
            )
        if surrendered:
            _log.info("sim_broker.surrender", isin=isin, quantity=surrendered)
        return surrendered

    def credit_corporate_cash(
        self, session: date, isin: str, amount: Decimal, description: str
    ) -> None:
        """Credit `amount` of corporate-action cash (dividend, exit) to free cash, with a ledger row

        Spendable at once, like the book's credit on the ex-date (`PortfolioBook.credit_dividend`
        says why the ex-date, not the payment date): a dividend is not a trade and has no T+N.
        """
        if not isinstance(amount, Decimal):
            raise TypeError("amount must be a Decimal — money is never float (CLAUDE.md)")
        if amount <= _ZERO:
            raise ValueError(f"a corporate cash credit must be positive, got {amount}")
        self._credit_cash(session, isin, amount, description)

    def credit_interest(self, session: date, amount: Decimal, description: str) -> None:
        """Credit `amount` of interest on idle cash to free cash, with a ledger row (no ISIN).

        The backtest's measurement of what idle money earns (`backtest.cash_interest`); a real
        broker account pays none, so like the corporate-action credits this is not on `Broker`.
        """
        if not isinstance(amount, Decimal):
            raise TypeError("amount must be a Decimal — money is never float (CLAUDE.md)")
        if amount <= _ZERO:
            raise ValueError(f"an interest credit must be positive, got {amount}")
        self._credit_cash(session, "", amount, description)

    def _credit_cash(self, session: date, isin: str, amount: Decimal, description: str) -> None:
        self._cash += amount
        self._ledger.append(
            LedgerEntry(
                seq=self._next_ledger_seq,
                session=session,
                isin=isin,
                description=description,
                debit=_ZERO,
                credit=amount,
                balance=self._cash,
            )
        )
        self._next_ledger_seq += 1

    def _rescaled_order(self, order: Order, numerator: Decimal, denominator: Decimal) -> Order:
        request = order.request
        quantity = _floor_ratio(request.quantity, numerator, denominator)
        if quantity <= 0:
            return replace(
                order,
                status=OrderStatus.CANCELLED,
                reason="cancelled: corporate action left less than one share to trade",
            )
        limit = request.limit_price
        if limit is not None:
            rounding = ROUND_FLOOR if request.side is Side.BUY else ROUND_CEILING
            limit = (limit * denominator / numerator).quantize(_TICK, rounding=rounding)
        return replace(order, request=replace(request, quantity=quantity, limit_price=limit))

    # ── Broker: account views ────────────────────────────────────────────────────────────────

    def positions(self) -> tuple[Position, ...]:
        """Filled, not-yet-settled buys — one per (ISIN, fill session), in ISIN then session order.

        Under T+1 that is at most the last session's buys; under T+2 an ISIN bought on two
        consecutive sessions shows two positions, each with its own fill session.
        """
        return tuple(
            Position(
                isin=pending.isin,
                exchange=pending.lot.exchange,
                quantity=pending.lot.quantity,
                average_price=pending.lot.average_price,
                session=pending.traded,
            )
            for pending in sorted(self._positions, key=lambda p: (p.isin, p.traded))
        )

    def holdings(self) -> tuple[Holding, ...]:
        """Settled delivery holdings, one per ISIN, in ISIN order."""
        return tuple(
            Holding(
                isin=isin,
                exchange=lot.exchange,
                quantity=lot.quantity,
                average_price=lot.average_price,
            )
            for isin, lot in sorted(self._holdings.items())
        )

    def ledger(self) -> tuple[LedgerEntry, ...]:
        """The cash ledger in posting order — append-only (invariant #12)."""
        return tuple(self._ledger)

    def margins(self) -> Margins:
        """Free cash, cash tied up in positions and holdings (cost basis), and their total.

        `available` is settled cash only: proceeds still in settlement are reported separately as
        `unsettled_proceeds`, so a policy sizing from `available` cannot spend them early and a
        valuation from `cash_value` does not lose them.
        """
        utilised = sum(
            (lot.cost for lot in (*(p.lot for p in self._positions), *self._holdings.values())),
            _ZERO,
        )
        return Margins(
            available=self._cash, utilised=utilised, unsettled_proceeds=self.unsettled_proceeds
        )

    @property
    def cash(self) -> Decimal:
        """Free (settled, spendable) cash right now. Excludes `unsettled_proceeds`."""
        return self._cash

    @property
    def interest_bearing_cash(self) -> Decimal:
        """Cash actually settled by the end of the current session — what earns interest.

        `cash` less the proceeds released early for the next session's fills: those are spendable
        by tonight's decision but are not paid out until the next session settles them.
        """
        return self._cash - self._released_ahead

    @property
    def unsettled_proceeds(self) -> Decimal:
        """Net sale proceeds filled but not yet released by the settlement cycle."""
        return sum((receivable.amount for receivable in self._receivables), _ZERO)

    def order(self, order_id: str) -> Order:
        """The current state of one order. Raises `UnknownOrderError` if never issued."""
        try:
            return self._orders[order_id]
        except KeyError:
            raise UnknownOrderError(order_id) from None

    # ── internals ────────────────────────────────────────────────────────────────────────────

    def _staged_or_raise(self, order_id: str) -> Order:
        order = self.order(order_id)
        if order.status is not OrderStatus.STAGED:
            raise OrderNotModifiableError(
                f"order {order_id} is {order.status}, not STAGED; it can no longer be changed"
            )
        return order

    def _issue_order_id(self) -> str:
        # Sequential, not random or time-based: replay must produce identical ids (B10, §8.3.3).
        order_id = f"SIM-{self._next_order_seq:06d}"
        self._next_order_seq += 1
        return order_id

    def _post_ledger(self, fill: Fill, *, debit: Decimal, credit: Decimal) -> None:
        self._ledger.append(
            LedgerEntry(
                seq=self._next_ledger_seq,
                session=fill.session,
                isin=fill.isin,
                description=f"{fill.side} {fill.quantity} @ {fill.fill_price}",
                debit=debit,
                credit=credit,
                # The account balance: spendable cash plus proceeds still in settlement, so the
                # ledger reconciles to its own debits and credits whatever the cycle.
                balance=self._cash + self.unsettled_proceeds,
            )
        )
        self._next_ledger_seq += 1
