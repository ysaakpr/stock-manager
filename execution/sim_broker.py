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

Market data is injected as a `SessionMarket`, not read from DuckDB here: the query service (M4.1) is
one implementation of it, and a test supplies bars directly so the unit suite never touches the
store. Time is an injected `Clock` (B10) — `place` reads the decision date from it and nothing else
reads a wall clock, so a replay through `SimBroker` is byte-reproducible.

What it never does: fill at a price it cannot justify. A session with no reference bar, a buy the
cash cannot cover, or a sell with nothing to deliver is *rejected* with a reason on the order — a
visible refusal in the order book, never a silent skip or a phantom fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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

    The EOD model nets to one order per scrip per session — that is also what keeps the per-scrip,
    per-day DP sell charge (`execution.costs`) correct without reaching into its internals. Modify
    the standing order instead of stacking a second.
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
    opening cash. `place` stages an order for the next session; `execute_session(session)` fills
    every order staged for that session and settles the previous session's buys into holdings.

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
    ) -> None:
        if not isinstance(opening_cash, Decimal):
            raise TypeError("opening_cash must be a Decimal — money is never float (CLAUDE.md)")
        if opening_cash < _ZERO:
            raise ValueError(f"opening_cash must be non-negative, got {opening_cash}")
        self._clock = clock
        self._costs = cost_model
        self._market = market
        self._policy = policy if policy is not None else FillPolicy()

        self._cash: Decimal = opening_cash
        self._orders: dict[str, Order] = {}
        self._holdings: dict[str, _Lot] = {}  # settled
        self._positions: dict[str, _Lot] = {}  # current (unsettled) session's buys
        self._current_session: date | None = None
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
        the EOD model nets to one order per scrip, which also keeps the per-day DP sell charge
        correct. The returned order is `STAGED`; it fills only when `execute_session` runs.
        """
        decision_date = self._clock.today()
        target = self._market.next_session(decision_date)
        for existing in self._orders.values():
            if (
                existing.status is OrderStatus.STAGED
                and existing.request.isin == request.isin
                and existing.target_session == target
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

        Settles first: the previous session's buys roll from positions into holdings (T+1), so a
        sell staged for `session` can be filled out of what settled overnight. Then each staged
        order for `session` is priced (reference → slippage → shared cost model) and either
        `COMPLETE` with a `Fill` or `REJECTED` with a reason (no bar, no cash, nothing to deliver).
        """
        self._settle_into(session)
        filled: list[Order] = []
        for order in list(self._orders.values()):
            if order.status is not OrderStatus.STAGED or order.target_session != session:
                continue
            resolved = self._fill(order, session)
            self._orders[order.order_id] = resolved
            filled.append(resolved)
        return tuple(filled)

    def _fill(self, order: Order, session: date) -> Order:
        request = order.request
        try:
            bar = self._market.reference_bar(request.isin, session)
        except NoReferenceBarError as exc:
            return self._reject(order, f"no reference bar for {session.isoformat()}: {exc}")

        reference = self._policy.reference_price(bar, request.side)
        turnover_at_reference = reference * request.quantity
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
        cost = self._costs.charge(trade)

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
        """Post a completed fill to the book: cash, positions/holdings, and one ledger line."""
        gross = fill.gross
        if fill.side is Side.BUY:
            self._cash += fill.net_cash  # net_cash is negative on a buy
            lot = self._positions.get(fill.isin)
            if lot is None:
                self._positions[fill.isin] = _Lot(fill.exchange, fill.quantity, gross)
            else:
                lot.quantity += fill.quantity
                lot.cost += gross
            self._post_ledger(fill, debit=fill.cost.net_amount, credit=_ZERO)
        else:
            self._cash += fill.net_cash  # positive on a sell
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
        """Advance to `session`, rolling the previous session's buys into settled holdings (T+1).

        Called at the start of `execute_session`. Idempotent within a session: running the same
        session twice does not double-settle, because positions are cleared once rolled.
        """
        if self._current_session == session:
            return
        if self._current_session is not None and session < self._current_session:
            raise ValueError(
                f"cannot execute {session.isoformat()} after already settling "
                f"{self._current_session.isoformat()}; sessions run forward only"
            )
        for isin, lot in self._positions.items():
            held = self._holdings.get(isin)
            if held is None:
                self._holdings[isin] = _Lot(lot.exchange, lot.quantity, lot.cost)
            else:
                held.quantity += lot.quantity
                held.cost += lot.cost
        self._positions.clear()
        self._current_session = session

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

    # ── Broker: account views ────────────────────────────────────────────────────────────────

    def positions(self) -> tuple[Position, ...]:
        """Open, not-yet-settled positions — this session's buys, one per ISIN, in ISIN order."""
        return tuple(
            Position(
                isin=isin,
                exchange=lot.exchange,
                quantity=lot.quantity,
                average_price=lot.average_price,
                session=self._current_session if self._current_session is not None else date.min,
            )
            for isin, lot in sorted(self._positions.items())
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
        """Free cash, cash tied up in positions and holdings (cost basis), and their total."""
        utilised = sum(
            (lot.cost for lot in (*self._positions.values(), *self._holdings.values())),
            _ZERO,
        )
        return Margins(available=self._cash, utilised=utilised)

    @property
    def cash(self) -> Decimal:
        """Free cash right now. A convenience view; the ledger is the record of how it got here."""
        return self._cash

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
                balance=self._cash,
            )
        )
        self._next_ledger_seq += 1
