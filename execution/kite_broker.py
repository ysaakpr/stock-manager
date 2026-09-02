"""X1 · `KiteBroker` — the real-money broker behind the `Broker` interface (Kite Connect REST).

Invariant #5 (AGENTIC_CONTEXT §6): paper and real money run the *same* decision code path and differ
only by which `Broker` implementation is injected. `KiteBroker` is the real-money one — the exact
counterpart of `SimBroker` — so the decision layer cannot tell them apart. `SimBroker` simulates
fills; `KiteBroker` places orders on the exchange through Zerodha's Kite Connect REST API and reads
positions, holdings, the ledger and margins back from it.

Two hard safety rails, both enforced *here*, above the transport:

* **No real order without a human in the loop.** A real broker order is placed, modified or
  cancelled only when the caller passes ``live_confirm=True`` *at the call site* (AGENTIC_CONTEXT
  §3.5: a human fires the first — and every — real order). A write attempted in live mode without
  that flag raises `LiveOrderNotConfirmedError` and sends nothing. This is the structural
  realization of the plan's HARD RULE for M8.1.
* **Dry-run by default.** A `KiteBroker` is constructed in ``dry_run=True`` unless told otherwise.
  In dry-run every write *constructs and logs the exact Kite request* and then sends nothing at
  all — the safe way to verify what would go to the exchange without touching the account. A real
  order is transmitted only when **both** ``dry_run=False`` **and** ``live_confirm=True``; any
  other combination is provably send-free.

No credential exists yet (B4), so this module is exercised entirely against recorded fixture
responses that mirror Kite's documented payloads: a `KiteTransport` is injected, the unit suite
wires one that replays fixtures and records every request, and `LiveKiteTransport` (httpx) is the
production wiring that the M8.3 human-gated live sessions use. The transport is the *only* seam that
touches the network; everything above it is offline and deterministic.

Identity stays ISIN-only (invariant #2) everywhere the decision layer can see. Kite's API speaks
`tradingsymbol`, not ISIN, so an `InstrumentResolver` (the identity master, D2) translates ISIN →
symbol on the way out and symbol → ISIN on the way back. The symbol appears at exactly one place —
the edge where the external API demands it — and never becomes a join key inside the system.

Time is an injected `Clock` (B10); nothing here reads a wall clock. Money is `Decimal` end to end:
the transport parses JSON numbers with `parse_float=Decimal`, so a price never passes through a
binary float on its way from the exchange into a `Holding` or a `LedgerEntry`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

import structlog

from dataplatform.clock import Clock
from execution.broker import (
    BrokerError,
    Exchange,
    Holding,
    LedgerEntry,
    Margins,
    Order,
    OrderNotModifiableError,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    SessionExpired,
    Side,
    UnknownOrderError,
)

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    import httpx

_LOG = structlog.get_logger(__name__)

_ZERO: Final[Decimal] = Decimal("0")

#: Kite Connect API base and the version header every request carries.
KITE_API_BASE: Final[str] = "https://api.kite.trade"
KITE_API_VERSION: Final[str] = "3"

#: Delivery equity is CNC (cash-and-carry), day validity, plain "regular" variety. These are the
#: only values this adapter ever sends: it exists for the EOD delivery strategy, not intraday or
#: F&O, and a request that tried to send anything else would be a different product needing review.
_PRODUCT_CNC: Final[str] = "CNC"
_VALIDITY_DAY: Final[str] = "DAY"
_VARIETY_REGULAR: Final[str] = "regular"

# Endpoint paths (relative to KITE_API_BASE).
_PATH_PROFILE: Final[str] = "/user/profile"
_PATH_HOLDINGS: Final[str] = "/portfolio/holdings"
_PATH_POSITIONS: Final[str] = "/portfolio/positions"
_PATH_MARGINS: Final[str] = "/user/margins"
_PATH_LEDGER: Final[str] = "/ledger"

#: Kite order lifecycle strings → our four-state `OrderStatus`. Every in-flight/pending state maps
#: to STAGED (live at the exchange but not yet terminal); only the three terminal states map to
#: themselves. A string Kite might add that we do not know is treated as STAGED rather than guessed
#: into a terminal state — reporting a live order as terminal is the more dangerous error.
_TERMINAL_STATUS: Final[Mapping[str, OrderStatus]] = {
    "COMPLETE": OrderStatus.COMPLETE,
    "REJECTED": OrderStatus.REJECTED,
    "CANCELLED": OrderStatus.CANCELLED,
}

__all__ = [
    "KITE_API_BASE",
    "KITE_API_VERSION",
    "InstrumentResolver",
    "KiteApiError",
    "KiteBroker",
    "KiteMalformedResponseError",
    "KiteRequest",
    "KiteRequestError",
    "KiteSessionError",
    "KiteTransport",
    "LiveKiteTransport",
    "LiveOrderNotConfirmedError",
]


# ── errors ───────────────────────────────────────────────────────────────────────────────────


class KiteApiError(BrokerError):
    """Base for every failure that originates in the Kite API or its response.

    Distinct from `BrokerError`'s other subclasses so a caller can tell an exchange-side refusal
    (this) from a client-side interface violation (e.g. `LiveOrderNotConfirmedError`).
    """


class KiteSessionError(KiteApiError):
    """Kite reported the access token is invalid or expired (a `TokenException`, HTTP 403).

    The transport raises this; `KiteBroker.session_valid()` turns it into a `SessionExpired`, which
    the daily loop's auth interlock (M5.15) treats as an AUTH_REQUIRED day: no orders, defer the
    day's decisions, alert once per streak. It is the real-broker analogue of `SimBroker` never
    expiring — the seam invariant #5 promised would fail here and nowhere upstream.
    """


class KiteRequestError(KiteApiError):
    """A non-session error the Kite API returned (bad input, order rejected, margin, rate limit …).

    Carries the API's own `error_type` and `message` so the adapter can map specific ones (an
    unknown order id → `UnknownOrderError`) and surface the rest verbatim rather than swallowing
    them (CLAUDE.md: fail loud and specific).
    """

    def __init__(self, error_type: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.message = message
        self.status_code = status_code


class KiteMalformedResponseError(KiteApiError):
    """A response that parsed as JSON but did not have the shape the documented payload guarantees.

    Raised rather than letting a `KeyError`/`TypeError` escape from deep in a parser: a broker that
    silently coerced a missing field into a zero position or a `None` price would be exactly the
    kind of quiet corruption the fail-loud rule exists to prevent.
    """


class LiveOrderNotConfirmedError(BrokerError):
    """A real order write was attempted in live mode without the explicit ``live_confirm`` flag.

    The HARD RULE for M8.1 (AGENTIC_CONTEXT §3.5): no code path in this repo may place, modify or
    cancel a real broker order without an explicit human action. The adapter refuses — nothing is
    sent — unless the caller passes ``live_confirm=True`` at the call site. This is not a
    `KiteApiError` because the exchange was never contacted; the refusal happened here, on purpose.
    """


# ── the transport seam ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class KiteRequest:
    """One Kite Connect REST call, fully constructed but not yet sent.

    `method` is the HTTP verb, `path` the endpoint relative to `KITE_API_BASE`, and `params` the
    query string (GET/DELETE) or form body (POST/PUT) — exactly the bytes that would go on the wire.
    Frozen and comparable so a test (and the dry-run log) can assert the *exact* request the adapter
    would send, byte for byte, without a network in the loop.
    """

    method: str
    path: str
    params: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class KiteTransport(Protocol):
    """Where a `KiteRequest` actually goes. The one seam that touches the network.

    An implementation performs the HTTP call, parses the JSON envelope (`{"status", "data"}`) with
    money as `Decimal`, and returns the `data` payload on success. On a Kite error it raises
    `KiteSessionError` for an expired/invalid token and `KiteRequestError` for everything else, so
    the broker above never has to inspect HTTP status codes. `LiveKiteTransport` is the production
    implementation; the unit suite injects one that replays recorded fixtures and records each call.
    """

    def send(self, request: KiteRequest) -> Any:
        """Perform `request`, return the parsed `data` payload, or raise a `KiteApiError`."""


@runtime_checkable
class InstrumentResolver(Protocol):
    """Translates between the identity master's ISIN (invariant #2) and Kite's `tradingsymbol`.

    Kite's REST API keys orders and portfolio rows on `tradingsymbol`, which the rest of this system
    never uses as an identifier. This protocol is the single, narrow adapter to that reality: ISIN →
    symbol on the way out to place an order, symbol → ISIN on the way back to key a `Position`. The
    identity master (D2) is the production implementation; the unit suite wires a small in-memory
    one.
    """

    def tradingsymbol(self, isin: str, exchange: Exchange) -> str:
        """The Kite `tradingsymbol` for `isin` on `exchange`. Raises if it cannot be resolved."""

    def isin_for(self, tradingsymbol: str, exchange: Exchange) -> str:
        """The ISIN for a Kite `tradingsymbol` on `exchange`. Raises if it cannot be resolved."""


# ── payload coercion helpers (fail loud) ───────────────────────────────────────────────────────


def _as_mapping(value: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise KiteMalformedResponseError(f"{where}: expected an object, got {type(value).__name__}")
    return value


def _as_list(value: Any, *, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise KiteMalformedResponseError(f"{where}: expected a list, got {type(value).__name__}")
    return value


def _field(row: Mapping[str, Any], key: str, *, where: str) -> Any:
    if key not in row:
        raise KiteMalformedResponseError(f"{where}: missing field {key!r}")
    return row[key]


def _as_decimal(value: Any, *, where: str) -> Decimal:
    """Coerce a payload number to `Decimal` without ever passing through a binary float.

    The transport parses JSON floats as `Decimal` already; ints arrive as `int` and strings as
    `str`. A native `float` reaching here means the transport skipped `parse_float=Decimal` — a
    wiring bug that must fail loudly, not be silently rounded into money.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; it is never a money value
        raise KiteMalformedResponseError(f"{where}: expected a number, got a bool")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            raise KiteMalformedResponseError(f"{where}: {value!r} is not a number") from None
    raise KiteMalformedResponseError(
        f"{where}: money must arrive as Decimal/int/str, got {type(value).__name__} — the "
        f"transport must parse JSON with parse_float=Decimal (CLAUDE.md: money is never float)"
    )


def _as_int(value: Any, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KiteMalformedResponseError(f"{where}: expected an integer quantity, got {value!r}")
    return value


def _as_exchange(value: Any, *, where: str) -> Exchange:
    try:
        return Exchange(str(value))
    except ValueError:
        raise KiteMalformedResponseError(f"{where}: {value!r} is not a known exchange") from None


# ── the adapter ────────────────────────────────────────────────────────────────────────────────


class KiteBroker:
    """The real-money broker: `Broker` over Kite Connect REST, dry-run-safe and human-gated.

    Satisfies `Broker` (invariant #5), so the decision layer injects it exactly where it injects
    `SimBroker` and nothing upstream changes. What it never does: place, modify or cancel a real
    order without both live mode *and* an explicit ``live_confirm=True`` at the call site; read a
    wall clock; or let a symbol become an identifier anywhere the decision layer can see (ISIN in,
    ISIN out).

    Construct it with an injected `Clock` (B10), a `KiteTransport` (the network seam), and an
    `InstrumentResolver` (ISIN ↔ tradingsymbol). `dry_run` defaults to `True`: the broker will
    construct and log every write and send none until it is deliberately built with `dry_run=False`,
    and even then a write still requires ``live_confirm=True``.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        transport: KiteTransport,
        resolver: InstrumentResolver,
        dry_run: bool = True,
    ) -> None:
        self._clock = clock
        self._transport = transport
        self._resolver = resolver
        self._dry_run = dry_run
        # Deterministic, local ids for the orders a dry-run "places": nothing was sent, so there is
        # no exchange id to report, and a monotonic counter keeps them distinguishable and stable.
        self._dry_run_seq = 0

    @property
    def dry_run(self) -> bool:
        """Whether this broker is in dry-run mode (constructs and logs writes, sends none)."""
        return self._dry_run

    # ── request construction (pure; the dry-run log and the live path share it) ─────────────────

    def place_request(self, request: OrderRequest) -> KiteRequest:
        """The exact Kite request that placing `request` would send — constructed, not sent.

        Public and pure so the dry-run path, the live path and the test suite all agree on one
        definition of "the request", and a test can assert it without a network. Translates the
        order's ISIN to a Kite `tradingsymbol` here, at the edge, and nowhere else.
        """
        params: dict[str, str] = {
            "tradingsymbol": self._resolver.tradingsymbol(request.isin, request.exchange),
            "exchange": request.exchange.value,
            "transaction_type": request.side.value,
            "order_type": request.order_type.value,
            "quantity": str(request.quantity),
            "product": _PRODUCT_CNC,
            "validity": _VALIDITY_DAY,
        }
        if request.order_type is OrderType.LIMIT:
            if request.limit_price is None:  # guaranteed by OrderRequest; guarded for safety
                raise ValueError("a LIMIT order needs a limit_price")
            params["price"] = str(request.limit_price)
        if request.tag is not None:
            params["tag"] = request.tag
        return KiteRequest("POST", f"/orders/{_VARIETY_REGULAR}", params)

    def modify_request(self, order_id: str, *, quantity: int) -> KiteRequest:
        """The exact Kite request that modifying `order_id`'s quantity would send."""
        return KiteRequest(
            "PUT",
            f"/orders/{_VARIETY_REGULAR}/{order_id}",
            {"quantity": str(quantity)},
        )

    def cancel_request(self, order_id: str) -> KiteRequest:
        """The exact Kite request that cancelling `order_id` would send."""
        return KiteRequest("DELETE", f"/orders/{_VARIETY_REGULAR}/{order_id}")

    # ── Broker: session ──────────────────────────────────────────────────────────────────────

    def session_valid(self) -> bool:
        """Whether Kite still honours the access token today (the auth precondition, M5.15).

        Calls the profile endpoint. A success means the token is live → `True`. A `TokenException`
        (the daily OAuth+2FA logout, NSE INVG/73992 §8.3.2.1.8) is raised as `SessionExpired`, which
        the interlock treats as AUTH_REQUIRED. Any other API error cannot *confirm* the session, so
        it returns `False` (fail safe: returning `True` asserts a live session and must never be a
        guess) after logging the cause.
        """
        try:
            self._transport.send(KiteRequest("GET", _PATH_PROFILE))
        except KiteSessionError as expired:
            raise SessionExpired(str(expired) or "kite access token expired") from expired
        except KiteApiError as err:
            _LOG.warning("kite_broker.session_check_failed", error=str(err))
            return False
        return True

    # ── Broker: order lifecycle ──────────────────────────────────────────────────────────────

    def place(self, request: OrderRequest, *, live_confirm: bool = False) -> Order:
        """Place `request` on the exchange — or, in dry-run, construct and log it and send nothing.

        The HARD RULE lives here: a real order is transmitted only when this broker is in live mode
        (`dry_run=False`) **and** the caller passes ``live_confirm=True``. In dry-run the request is
        built, logged and *not* sent, and a placeholder `STAGED` order is returned. In live mode
        without ``live_confirm`` it raises `LiveOrderNotConfirmedError` and sends nothing.
        """
        req = self.place_request(request)
        decision_day = self._clock.today()
        if self._dry_run:
            self._log_would_send("place", req)
            return self._dry_run_order(request, decision_day)
        self._require_live_confirm(live_confirm, "place")
        data = self._transport.send(req)
        order_id = self._order_id_from(data, where="place response")
        _LOG.info(
            "kite_broker.placed",
            order_id=order_id,
            isin=request.isin,
            side=request.side.value,
            quantity=request.quantity,
            live=True,
        )
        return Order(
            order_id=order_id,
            request=request,
            status=OrderStatus.STAGED,
            decision_date=decision_day,
            target_session=decision_day,
        )

    def modify(self, order_id: str, *, quantity: int, live_confirm: bool = False) -> Order:
        """Change a live order's quantity — or, in dry-run, construct and log it and send nothing.

        Reads the order first (`order`) so a modify against an unknown id raises `UnknownOrderError`
        and a modify against a terminal order raises `OrderNotModifiableError` before any write is
        even attempted — the same refusals `SimBroker` makes. Whole-share validation is re-run via
        `OrderRequest`. The live write is gated exactly as `place` is.
        """
        existing = self.order(order_id)
        if existing.status is not OrderStatus.STAGED:
            raise OrderNotModifiableError(
                f"order {order_id} is {existing.status}, not live; it can no longer be modified"
            )
        # Re-validate the new quantity through the same guard the interface uses everywhere else.
        new_request = OrderRequest(
            isin=existing.request.isin,
            side=existing.request.side,
            quantity=quantity,
            exchange=existing.request.exchange,
            order_type=existing.request.order_type,
            limit_price=existing.request.limit_price,
            tag=existing.request.tag,
        )
        req = self.modify_request(order_id, quantity=quantity)
        if self._dry_run:
            self._log_would_send("modify", req)
            return Order(
                order_id=order_id,
                request=new_request,
                status=OrderStatus.STAGED,
                decision_date=existing.decision_date,
                target_session=existing.target_session,
            )
        self._require_live_confirm(live_confirm, "modify")
        self._transport.send(req)
        _LOG.info("kite_broker.modified", order_id=order_id, quantity=quantity, live=True)
        return self.order(order_id)

    def cancel(self, order_id: str, *, live_confirm: bool = False) -> Order:
        """Cancel a live order — or, in dry-run, construct and log it and send nothing.

        Like `modify`, it reads the order first so an unknown or already-terminal order is refused
        before any write, and the live write is gated by ``live_confirm``.
        """
        existing = self.order(order_id)
        if existing.status is not OrderStatus.STAGED:
            raise OrderNotModifiableError(
                f"order {order_id} is {existing.status}, not live; it can no longer be cancelled"
            )
        req = self.cancel_request(order_id)
        if self._dry_run:
            self._log_would_send("cancel", req)
            return Order(
                order_id=order_id,
                request=existing.request,
                status=OrderStatus.CANCELLED,
                decision_date=existing.decision_date,
                target_session=existing.target_session,
                reason="dry-run: cancel constructed and logged, not sent",
            )
        self._require_live_confirm(live_confirm, "cancel")
        self._transport.send(req)
        _LOG.info("kite_broker.cancelled", order_id=order_id, live=True)
        return self.order(order_id)

    def order(self, order_id: str) -> Order:
        """The current state of one order, read from Kite's order-history endpoint.

        Kite returns the order's state transitions as a list; the last entry is the latest state.
        An unknown id is an `UnknownOrderError` (the exchange refuses the lookup). This is a read:
        it runs in dry-run too, and never places anything.
        """
        try:
            data = self._transport.send(KiteRequest("GET", f"/orders/{order_id}"))
        except KiteRequestError as err:
            # Kite answers a lookup for an id it never issued with an input/order error, not a 404.
            raise UnknownOrderError(f"kite has no order {order_id!r}: {err.message}") from err
        states = _as_list(data, where=f"order {order_id}")
        if not states:
            raise UnknownOrderError(f"kite returned no state for order {order_id!r}")
        return self._order_from_state(_as_mapping(states[-1], where=f"order {order_id} state"))

    # ── Broker: account views ────────────────────────────────────────────────────────────────

    def positions(self) -> tuple[Position, ...]:
        """Open, not-yet-settled positions (Kite's net book), one per ISIN, in ISIN order.

        Kite positions carry a `tradingsymbol`, not an ISIN, so each is translated back through the
        resolver — the one place a symbol is read, immediately keyed to its ISIN. The session
        stamped on each is the current trading date from the injected clock (a position is today's
        unsettled fill); Kite does not report a settlement session on the position itself.
        """
        data = _as_mapping(
            self._transport.send(KiteRequest("GET", _PATH_POSITIONS)), where="positions"
        )
        net = _as_list(_field(data, "net", where="positions"), where="positions.net")
        session = self._clock.today()
        out: list[Position] = []
        for raw in net:
            row = _as_mapping(raw, where="positions.net[]")
            quantity = _as_int(_field(row, "quantity", where="position"), where="position.quantity")
            if quantity == 0:  # a squared-off net line is not an open position
                continue
            exchange = _as_exchange(_field(row, "exchange", where="position"), where="position")
            symbol = str(_field(row, "tradingsymbol", where="position"))
            out.append(
                Position(
                    isin=self._resolver.isin_for(symbol, exchange),
                    exchange=exchange,
                    quantity=quantity,
                    average_price=_as_decimal(
                        _field(row, "average_price", where="position"),
                        where="position.average_price",
                    ),
                    session=session,
                )
            )
        return tuple(sorted(out, key=lambda p: p.isin))

    def holdings(self) -> tuple[Holding, ...]:
        """Settled delivery holdings, one per ISIN, in ISIN order.

        Kite's holdings payload carries the ISIN directly, so it is read from the row rather than
        resolved — the exchange's own identity for the scrip.
        """
        rows = _as_list(self._transport.send(KiteRequest("GET", _PATH_HOLDINGS)), where="holdings")
        out: list[Holding] = []
        for raw in rows:
            row = _as_mapping(raw, where="holdings[]")
            quantity = _as_int(_field(row, "quantity", where="holding"), where="holding.quantity")
            if quantity == 0:
                continue
            out.append(
                Holding(
                    isin=str(_field(row, "isin", where="holding")),
                    exchange=_as_exchange(
                        _field(row, "exchange", where="holding"), where="holding"
                    ),
                    quantity=quantity,
                    average_price=_as_decimal(
                        _field(row, "average_price", where="holding"),
                        where="holding.average_price",
                    ),
                )
            )
        return tuple(sorted(out, key=lambda h: h.isin))

    def ledger(self) -> tuple[LedgerEntry, ...]:
        """The cash ledger in posting order — what reconciliation (§6) compares to the inner book.

        Kite Connect's core REST surface has no order-independent cash ledger; Zerodha exposes it
        through the Console reports API, and this reads that report's rows: date, particulars,
        debit, credit and the running balance. A row for a scrip carries its ISIN (invariant #2); a
        cash row (a charge, a payin) legitimately has none and keys to an empty ISIN. The sequence
        number is the row's position in the returned, already-ordered report.
        """
        rows = _as_list(self._transport.send(KiteRequest("GET", _PATH_LEDGER)), where="ledger")
        out: list[LedgerEntry] = []
        for seq, raw in enumerate(rows):
            row = _as_mapping(raw, where="ledger[]")
            out.append(
                LedgerEntry(
                    seq=seq,
                    session=_parse_date(
                        _field(row, "posting_date", where="ledger"), where="ledger.posting_date"
                    ),
                    isin=str(row.get("isin", "")),
                    description=str(_field(row, "particulars", where="ledger")),
                    debit=_as_decimal(_field(row, "debit", where="ledger"), where="ledger.debit"),
                    credit=_as_decimal(
                        _field(row, "credit", where="ledger"), where="ledger.credit"
                    ),
                    balance=_as_decimal(
                        _field(row, "balance", where="ledger"), where="ledger.balance"
                    ),
                )
            )
        return tuple(out)

    def margins(self) -> Margins:
        """Free cash, cash tied up, and their total, from Kite's equity margins.

        Maps `available.live_balance` to free cash and `utilised.debits` to deployed capital, the
        two figures the decision layer needs; the `total` is recomputed by `Margins` as their sum
        (delivery equity is fully paid, so utilised is cost, not leverage).
        """
        data = _as_mapping(self._transport.send(KiteRequest("GET", _PATH_MARGINS)), where="margins")
        equity = _as_mapping(_field(data, "equity", where="margins"), where="margins.equity")
        available = _as_mapping(
            _field(equity, "available", where="margins.equity"), where="margins.equity.available"
        )
        utilised = _as_mapping(
            _field(equity, "utilised", where="margins.equity"), where="margins.equity.utilised"
        )
        return Margins(
            available=_as_decimal(
                _field(available, "live_balance", where="margins.available"),
                where="margins.available.live_balance",
            ),
            utilised=_as_decimal(
                _field(utilised, "debits", where="margins.utilised"),
                where="margins.utilised.debits",
            ),
        )

    # ── internals ────────────────────────────────────────────────────────────────────────────

    def _require_live_confirm(self, live_confirm: bool, action: str) -> None:
        """Refuse a live write unless the human passed ``live_confirm=True`` at the call site."""
        if not live_confirm:
            raise LiveOrderNotConfirmedError(
                f"refusing to {action} a real broker order without live_confirm=True — a real "
                f"order requires an explicit human action (AGENTIC_CONTEXT §3.5). Nothing was sent."
            )

    def _log_would_send(self, action: str, req: KiteRequest) -> None:
        _LOG.info(
            "kite_broker.dry_run",
            action=action,
            method=req.method,
            path=req.path,
            params=dict(req.params),
            sent=False,
        )

    def _dry_run_order(self, request: OrderRequest, decision_day: date) -> Order:
        order_id = f"DRYRUN-{self._dry_run_seq:06d}"
        self._dry_run_seq += 1
        return Order(
            order_id=order_id,
            request=request,
            status=OrderStatus.STAGED,
            decision_date=decision_day,
            target_session=decision_day,
            reason="dry-run: request constructed and logged, not sent",
        )

    def _order_id_from(self, data: Any, *, where: str) -> str:
        row = _as_mapping(data, where=where)
        return str(_field(row, "order_id", where=where))

    def _order_from_state(self, state: Mapping[str, Any]) -> Order:
        """Build an `Order` from one Kite order-book state.

        The lifecycle state is mapped to `OrderStatus`; the request is reconstructed with the ISIN
        resolved from the state's tradingsymbol (invariant #2). The realized fill's cost breakdown
        is *not* reconstructed here — Kite's order object does not carry the Indian contract-note
        charges, which reach the system through `ledger()` and are reconciled in `recon.py`. So a
        COMPLETE order is reported with its lifecycle status and `fill=None`; the money is in the
        ledger, not invented from the order object.
        """
        order_id = str(_field(state, "order_id", where="order state"))
        exchange = _as_exchange(_field(state, "exchange", where="order state"), where="order state")
        symbol = str(_field(state, "tradingsymbol", where="order state"))
        side = Side(str(_field(state, "transaction_type", where="order state")))
        order_type = OrderType(str(_field(state, "order_type", where="order state")))
        quantity = _as_int(
            _field(state, "quantity", where="order state"), where="order state quantity"
        )
        limit_price: Decimal | None = None
        if order_type is OrderType.LIMIT:
            limit_price = _as_decimal(
                _field(state, "price", where="order state"), where="order state price"
            )
        tag_raw = state.get("tag")
        request = OrderRequest(
            isin=self._resolver.isin_for(symbol, exchange),
            side=side,
            quantity=quantity,
            exchange=exchange,
            order_type=order_type,
            limit_price=limit_price,
            tag=None if tag_raw is None else str(tag_raw),
        )
        kite_status = str(_field(state, "status", where="order state")).upper()
        status = _TERMINAL_STATUS.get(kite_status, OrderStatus.STAGED)
        reason = None
        if status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
            reason = str(state.get("status_message") or kite_status)
        decision_day = self._clock.today()
        return Order(
            order_id=order_id,
            request=request,
            status=status,
            decision_date=decision_day,
            target_session=decision_day,
            reason=reason,
        )


def _parse_date(value: Any, *, where: str) -> date:
    """Parse an ISO date (optionally an ISO timestamp) from a Kite payload to a `date`."""
    text = str(value)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise KiteMalformedResponseError(f"{where}: {value!r} is not an ISO date") from None


# ── production transport (httpx; not exercised by the offline suite, needs a live token) ─────────


class LiveKiteTransport:
    """The production `KiteTransport`: real HTTPS calls to Kite Connect (httpx).

    Not exercised by the unit suite — it needs a real access token that does not exist yet (B4) and
    a network, both of which the tests never touch. It is the wiring the M8.3 human-gated live
    sessions use. It parses every response body with `parse_float=Decimal` so money never becomes a
    float, and maps a `TokenException` to `KiteSessionError` and every other API error to
    `KiteRequestError`, so the broker above deals only in typed exceptions.
    """

    __slots__ = ("_base_url", "_client", "_headers")

    def __init__(
        self,
        *,
        api_key: str,
        access_token: str,
        client: httpx.Client,
        base_url: str = KITE_API_BASE,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        # Kite authenticates with the api_key:access_token pair in the Authorization header. The
        # token is the daily-expiring one; api_key is the app id. Neither is logged.
        self._headers = {
            "X-Kite-Version": KITE_API_VERSION,
            "Authorization": f"token {api_key}:{access_token}",
        }

    def send(self, request: KiteRequest) -> Any:
        url = f"{self._base_url}{request.path}"
        params = dict(request.params)
        if request.method == "GET":
            response = self._client.get(url, params=params, headers=self._headers)
        elif request.method == "POST":
            response = self._client.post(url, data=params, headers=self._headers)
        elif request.method == "PUT":
            response = self._client.put(url, data=params, headers=self._headers)
        elif request.method == "DELETE":
            response = self._client.request("DELETE", url, params=params, headers=self._headers)
        else:  # pragma: no cover - the adapter never constructs another verb
            raise KiteRequestError("InputException", f"unsupported method {request.method}")
        return self._unwrap(response.text, response.status_code)

    @staticmethod
    def _unwrap(body: str, status_code: int) -> Any:
        """Parse the Kite envelope, money as Decimal, and raise on an error status."""
        try:
            envelope = json.loads(body, parse_float=Decimal)
        except json.JSONDecodeError as err:
            raise KiteMalformedResponseError(
                f"kite returned non-JSON (HTTP {status_code})"
            ) from err
        env = _as_mapping(envelope, where="kite envelope")
        if env.get("status") == "success":
            return env.get("data")
        error_type = str(env.get("error_type") or "KiteException")
        message = str(env.get("message") or f"HTTP {status_code}")
        if error_type == "TokenException" or status_code == 403:
            raise KiteSessionError(message)
        raise KiteRequestError(error_type, message, status_code=status_code)
