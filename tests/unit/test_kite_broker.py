"""M8.1 — `KiteBroker`, the real-money broker adapter, tested against recorded Kite payloads.

No Kite credential exists (B4) and the unit suite never touches the network, so `KiteBroker` is
exercised entirely against fixture responses that mirror Kite Connect's documented payloads
(`tests/fixtures/kite/`), replayed through a transport that records every request it is asked to
send. What is under test, against the three acceptance criteria:

1. **Protocol parity (acceptance 1).** `KiteBroker` *is* a `Broker`, and it passes the same
   `Broker`-contract tests as `SimBroker` — the shared `contract` suite below is parametrized over
   both implementations, so every assertion it makes holds for the paper broker and the real one
   alike (invariant #5).
2. **Dry-run sends nothing (acceptance 2).** In dry-run every write constructs the exact Kite
   request and provably transmits nothing — the recording transport sees zero POST/PUT/DELETE calls
   — while the constructed request is asserted byte-for-byte.
3. **No real order without a human (acceptance 3).** In live mode a placement (or modify/cancel)
   without ``live_confirm=True`` raises and sends nothing; a real order goes out only with both live
   mode and the explicit flag.

Plus the parsing surface: holdings, positions, the ledger and margins parse from recorded payloads
into the ISIN-keyed value objects, with money as `Decimal` and never a float. The clock is frozen
(B10); the whole file is offline and deterministic.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.clock import FrozenClock
from execution.broker import (
    Broker,
    Exchange,
    Holding,
    LedgerEntry,
    Margins,
    OrderNotModifiableError,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    SessionExpired,
    Side,
    UnknownOrderError,
)
from execution.costs import CostModel, load_rate_card
from execution.kite_broker import (
    KiteBroker,
    KiteRequest,
    LiveKiteTransport,
    LiveOrderNotConfirmedError,
)
from execution.sim_broker import ReferenceBar, SimBroker

FIXTURES: Final[Path] = Path(__file__).resolve().parents[1] / "fixtures" / "kite"

INFY: Final[str] = "INE009A01021"
TCS: Final[str] = "INE467B01029"
_ISIN_PATTERN: Final[str] = r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"

DECISION_DAY: Final[date] = date(2024, 1, 1)
S1: Final[date] = date(2024, 1, 2)

LIVE_ORDER_ID: Final[str] = "240102000000001"
UNKNOWN_ORDER_ID: Final[str] = "UNKNOWN-999"


# ── test doubles ─────────────────────────────────────────────────────────────────────────────


class InMemoryResolver:
    """An `InstrumentResolver` from a fixed ISIN ↔ tradingsymbol table."""

    def __init__(self, table: dict[tuple[str, Exchange], str]) -> None:
        self._by_isin = dict(table)
        self._by_symbol = {(symbol, ex): isin for (isin, ex), symbol in table.items()}

    def tradingsymbol(self, isin: str, exchange: Exchange) -> str:
        try:
            return self._by_isin[(isin, exchange)]
        except KeyError:
            raise KeyError(f"no tradingsymbol for {isin} on {exchange}") from None

    def isin_for(self, tradingsymbol: str, exchange: Exchange) -> str:
        try:
            return self._by_symbol[(tradingsymbol, exchange)]
        except KeyError:
            raise KeyError(f"no ISIN for {tradingsymbol} on {exchange}") from None


RESOLVER: Final[InMemoryResolver] = InMemoryResolver(
    {
        (INFY, Exchange.NSE): "INFY",
        (TCS, Exchange.NSE): "TCS",
    }
)

#: The default route table: every read endpoint plus the order write acks, keyed (method, path).
_DEFAULT_ROUTES: Final[dict[tuple[str, str], str]] = {
    ("GET", "/user/profile"): "profile.json",
    ("GET", "/portfolio/holdings"): "holdings.json",
    ("GET", "/portfolio/positions"): "positions.json",
    ("GET", "/user/margins"): "margins.json",
    ("GET", "/ledger"): "ledger.json",
    ("POST", "/orders/regular"): "order_place.json",
    ("PUT", f"/orders/regular/{LIVE_ORDER_ID}"): "order_modify.json",
    ("DELETE", f"/orders/regular/{LIVE_ORDER_ID}"): "order_cancel.json",
    ("GET", f"/orders/{LIVE_ORDER_ID}"): "order_open.json",
    ("GET", f"/orders/{UNKNOWN_ORDER_ID}"): "error_unknown_order.json",
}


class RecordedTransport:
    """A `KiteTransport` that replays fixtures and records every request it is handed.

    Uses the *production* `LiveKiteTransport._unwrap`, so the fixtures pass through the same
    envelope-parsing and error-mapping the real transport uses — money parsed as `Decimal`, a
    `TokenException` raised as a session error. A request to an unregistered route is a test bug and
    fails loudly rather than returning a stale fixture.
    """

    def __init__(self, routes: dict[tuple[str, str], str] | None = None) -> None:
        self.routes = dict(_DEFAULT_ROUTES if routes is None else routes)
        self.sent: list[KiteRequest] = []

    def send(self, request: KiteRequest) -> object:
        self.sent.append(request)
        try:
            fixture = self.routes[(request.method, request.path)]
        except KeyError:
            raise AssertionError(
                f"unexpected request in test: {request.method} {request.path}"
            ) from None
        text = (FIXTURES / fixture).read_text(encoding="utf-8")
        return LiveKiteTransport._unwrap(text, 200)

    @property
    def writes(self) -> list[KiteRequest]:
        """Every mutating request actually sent — the surface acceptance #2 must keep empty."""
        return [r for r in self.sent if r.method in {"POST", "PUT", "DELETE"}]


def _kite(
    *,
    dry_run: bool = True,
    routes: dict[tuple[str, str], str] | None = None,
    transport: RecordedTransport | None = None,
) -> KiteBroker:
    return KiteBroker(
        clock=FrozenClock(DECISION_DAY),
        transport=transport if transport is not None else RecordedTransport(routes),
        resolver=RESOLVER,
        dry_run=dry_run,
    )


# ══ the shared Broker contract, parametrized over SimBroker and KiteBroker (acceptance 1) ═══════


def _make_sim() -> Broker:
    market = _SimMarket([S1], {})
    return SimBroker(
        clock=FrozenClock(DECISION_DAY),
        cost_model=CostModel(load_rate_card()),
        market=market,
        opening_cash=Decimal("1000000"),
    )


def _make_kite() -> Broker:
    return _kite(dry_run=True)


class _SimMarket:
    """A minimal `SessionMarket` for the shared contract's SimBroker instance."""

    def __init__(self, sessions: list[date], bars: dict[tuple[str, date], ReferenceBar]) -> None:
        self._sessions = sorted(sessions)
        self._bars = bars

    def next_session(self, after: date) -> date:
        for session in self._sessions:
            if session > after:
                return session
        raise LookupError(f"no session after {after.isoformat()}")

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        return self._bars[(isin, session)]


#: The two implementations under the one contract. Each entry is a no-arg factory.
BROKER_FACTORIES: Final[list[Callable[[], Broker]]] = [_make_sim, _make_kite]


@pytest.mark.parametrize("make_broker", BROKER_FACTORIES)
def test_contract_is_a_broker(make_broker: Callable[[], Broker]) -> None:
    """Both implementations structurally satisfy the `Broker` protocol (invariant #5)."""
    assert isinstance(make_broker(), Broker)


@pytest.mark.parametrize("make_broker", BROKER_FACTORIES)
def test_contract_session_valid_true_for_a_live_session(
    make_broker: Callable[[], Broker],
) -> None:
    assert make_broker().session_valid() is True


@pytest.mark.parametrize("make_broker", BROKER_FACTORIES)
def test_contract_place_returns_a_staged_order_echoing_the_request(
    make_broker: Callable[[], Broker],
) -> None:
    """A placed order is STAGED, carries the request unchanged, and has a non-empty string id."""
    broker = make_broker()
    request = OrderRequest(isin=INFY, side=Side.BUY, quantity=10)
    order = broker.place(request)
    assert order.status is OrderStatus.STAGED
    assert order.request == request
    assert isinstance(order.order_id, str) and order.order_id


@pytest.mark.parametrize("make_broker", BROKER_FACTORIES)
def test_contract_modify_unknown_order_raises(make_broker: Callable[[], Broker]) -> None:
    """Modifying an id the broker never issued is an `UnknownOrderError`, not a silent no-op."""
    broker = make_broker()
    with pytest.raises(UnknownOrderError):
        broker.modify(UNKNOWN_ORDER_ID, quantity=1)


@pytest.mark.parametrize("make_broker", BROKER_FACTORIES)
def test_contract_holdings_are_isin_keyed_and_decimal(
    make_broker: Callable[[], Broker],
) -> None:
    """Whatever holdings a broker reports are well-typed: ISIN key, int qty, Decimal money."""
    for holding in make_broker().holdings():
        assert isinstance(holding, Holding)
        assert re.fullmatch(_ISIN_PATTERN, holding.isin)
        assert isinstance(holding.quantity, int) and holding.quantity > 0
        assert isinstance(holding.average_price, Decimal)
        assert isinstance(holding.exchange, Exchange)


@pytest.mark.parametrize("make_broker", BROKER_FACTORIES)
def test_contract_positions_are_isin_keyed_and_decimal(
    make_broker: Callable[[], Broker],
) -> None:
    for position in make_broker().positions():
        assert isinstance(position, Position)
        assert re.fullmatch(_ISIN_PATTERN, position.isin)
        assert isinstance(position.quantity, int) and position.quantity > 0
        assert isinstance(position.average_price, Decimal)


@pytest.mark.parametrize("make_broker", BROKER_FACTORIES)
def test_contract_ledger_is_ordered_and_decimal(make_broker: Callable[[], Broker]) -> None:
    ledger = make_broker().ledger()
    assert [e.seq for e in ledger] == list(range(len(ledger)))
    for entry in ledger:
        assert isinstance(entry, LedgerEntry)
        assert isinstance(entry.debit, Decimal)
        assert isinstance(entry.credit, Decimal)
        assert isinstance(entry.balance, Decimal)
        assert isinstance(entry.session, date)


@pytest.mark.parametrize("make_broker", BROKER_FACTORIES)
def test_contract_margins_total_is_available_plus_utilised(
    make_broker: Callable[[], Broker],
) -> None:
    margins = make_broker().margins()
    assert isinstance(margins, Margins)
    assert isinstance(margins.available, Decimal)
    assert isinstance(margins.utilised, Decimal)
    assert margins.total == margins.available + margins.utilised


# ══ acceptance 2: dry-run constructs the exact request and sends nothing ════════════════════════


def test_dry_run_place_constructs_the_exact_request_and_sends_nothing() -> None:
    transport = RecordedTransport()
    broker = _kite(dry_run=True, transport=transport)
    request = OrderRequest(isin=INFY, side=Side.BUY, quantity=10, tag="reb-1")

    order = broker.place(request)

    # Nothing at all was transmitted — not the write, not anything.
    assert transport.sent == []
    assert transport.writes == []
    # The constructed request is exactly the Kite call that a live placement would send.
    assert broker.place_request(request) == KiteRequest(
        "POST",
        "/orders/regular",
        {
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "transaction_type": "BUY",
            "order_type": "MARKET",
            "quantity": "10",
            "product": "CNC",
            "validity": "DAY",
            "tag": "reb-1",
        },
    )
    # The returned order is a clearly-marked dry-run placeholder, not a real staged order.
    assert order.status is OrderStatus.STAGED
    assert order.order_id.startswith("DRYRUN-")
    assert order.reason is not None and "not sent" in order.reason


def test_dry_run_limit_order_carries_the_price_and_market_order_does_not() -> None:
    broker = _kite(dry_run=True)
    limit = OrderRequest(
        isin=INFY,
        side=Side.BUY,
        quantity=3,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("1500"),
    )
    market = OrderRequest(isin=INFY, side=Side.SELL, quantity=3)
    assert broker.place_request(limit).params["price"] == "1500"
    assert "price" not in broker.place_request(market).params


def test_dry_run_modify_and_cancel_send_no_write() -> None:
    transport = RecordedTransport()
    broker = _kite(dry_run=True, transport=transport)

    modified = broker.modify(LIVE_ORDER_ID, quantity=20)
    assert modified.request.quantity == 20
    assert modified.status is OrderStatus.STAGED

    cancelled = broker.cancel(LIVE_ORDER_ID)
    assert cancelled.status is OrderStatus.CANCELLED

    # Reads (the order-state lookups) may happen; no mutating request ever went out.
    assert transport.writes == []


def test_dry_run_ignores_live_confirm_and_still_sends_nothing() -> None:
    """Dry-run is the outer safety: even ``live_confirm=True`` transmits nothing in dry-run mode."""
    transport = RecordedTransport()
    broker = _kite(dry_run=True, transport=transport)
    broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10), live_confirm=True)
    assert transport.writes == []


# ══ acceptance 3: no real order without the explicit live-confirm flag ══════════════════════════


def test_live_place_without_confirm_raises_and_sends_nothing() -> None:
    transport = RecordedTransport()
    broker = _kite(dry_run=False, transport=transport)
    with pytest.raises(LiveOrderNotConfirmedError):
        broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    assert transport.writes == []


def test_live_modify_without_confirm_raises_and_sends_nothing() -> None:
    transport = RecordedTransport()
    broker = _kite(dry_run=False, transport=transport)
    with pytest.raises(LiveOrderNotConfirmedError):
        broker.modify(LIVE_ORDER_ID, quantity=20)
    assert transport.writes == []


def test_live_cancel_without_confirm_raises_and_sends_nothing() -> None:
    transport = RecordedTransport()
    broker = _kite(dry_run=False, transport=transport)
    with pytest.raises(LiveOrderNotConfirmedError):
        broker.cancel(LIVE_ORDER_ID)
    assert transport.writes == []


def test_live_place_with_confirm_sends_exactly_one_order() -> None:
    transport = RecordedTransport()
    broker = _kite(dry_run=False, transport=transport)
    request = OrderRequest(isin=INFY, side=Side.BUY, quantity=10, tag="reb-2024-01-02")

    order = broker.place(request, live_confirm=True)

    assert order.order_id == LIVE_ORDER_ID
    assert order.status is OrderStatus.STAGED
    assert order.request == request
    # Exactly one write, and it is the placement with the constructed params.
    (sent,) = transport.writes
    assert sent == broker.place_request(request)


def test_live_modify_with_confirm_sends_a_put() -> None:
    transport = RecordedTransport()
    broker = _kite(dry_run=False, transport=transport)
    broker.modify(LIVE_ORDER_ID, quantity=20, live_confirm=True)
    assert [(r.method, r.path) for r in transport.writes] == [
        ("PUT", f"/orders/regular/{LIVE_ORDER_ID}")
    ]


def test_live_cancel_with_confirm_sends_a_delete() -> None:
    transport = RecordedTransport()
    broker = _kite(dry_run=False, transport=transport)
    broker.cancel(LIVE_ORDER_ID, live_confirm=True)
    assert [(r.method, r.path) for r in transport.writes] == [
        ("DELETE", f"/orders/regular/{LIVE_ORDER_ID}")
    ]


# ══ session / auth interlock (M5.15 seam) ══════════════════════════════════════════════════════


def test_session_valid_true_on_a_live_token() -> None:
    assert _kite().session_valid() is True


def test_expired_token_raises_session_expired() -> None:
    """A Kite TokenException surfaces as SessionExpired — the AUTH_REQUIRED trigger (M5.15)."""
    routes = dict(_DEFAULT_ROUTES)
    routes[("GET", "/user/profile")] = "error_token.json"
    with pytest.raises(SessionExpired):
        _kite(routes=routes).session_valid()


def test_non_token_session_error_returns_false_fail_safe() -> None:
    """A profile check failing for a non-token reason cannot assert the session; returns False."""
    routes = dict(_DEFAULT_ROUTES)
    routes[("GET", "/user/profile")] = "error_unknown_order.json"
    assert _kite(routes=routes).session_valid() is False


# ══ parsing recorded payloads into ISIN-keyed value objects ═════════════════════════════════════


def test_holdings_parse_from_the_recorded_payload() -> None:
    holdings = _kite().holdings()
    # WIPRO (quantity 0) is dropped; the rest come back ISIN-sorted.
    assert [h.isin for h in holdings] == [INFY, TCS]
    infy = holdings[0]
    assert infy.quantity == 40
    assert infy.average_price == Decimal("1500.50")
    assert infy.exchange is Exchange.NSE


def test_positions_parse_and_are_keyed_by_resolved_isin() -> None:
    positions = _kite().positions()
    # The squared-off TCS net line (quantity 0) is excluded.
    (infy,) = positions
    assert infy.isin == INFY  # resolved from the tradingsymbol Kite reports
    assert infy.quantity == 5
    assert infy.average_price == Decimal("1490.00")
    assert infy.session == DECISION_DAY  # stamped from the injected clock


def test_margins_parse_available_and_utilised() -> None:
    margins = _kite().margins()
    assert margins.available == Decimal("125000.75")
    assert margins.utilised == Decimal("74999.25")
    assert margins.total == Decimal("200000.00")


def test_ledger_parses_ordered_rows_with_scrip_and_cash_lines() -> None:
    ledger = _kite().ledger()
    assert [e.seq for e in ledger] == [0, 1, 2]
    cash_row = ledger[0]
    assert cash_row.isin == ""  # a pure cash row has no scrip
    assert cash_row.credit == Decimal("200000.00")
    assert cash_row.session == date(2024, 1, 2)
    scrip_row = ledger[1]
    assert scrip_row.isin == INFY
    assert scrip_row.debit == Decimal("60020.30")
    assert scrip_row.balance == Decimal("139979.70")


def test_money_is_decimal_never_float() -> None:
    """CLAUDE.md: money is `Decimal`. The transport must parse JSON numbers straight to Decimal."""
    infy = _kite().holdings()[0]
    assert type(infy.average_price) is Decimal
    margins = _kite().margins()
    assert type(margins.available) is Decimal


# ══ order lifecycle read-back ══════════════════════════════════════════════════════════════════


def test_order_lookup_maps_a_complete_order() -> None:
    routes = dict(_DEFAULT_ROUTES)
    routes[("GET", f"/orders/{LIVE_ORDER_ID}")] = "order_complete.json"
    order = _kite(routes=routes).order(LIVE_ORDER_ID)
    assert order.status is OrderStatus.COMPLETE
    assert order.request.isin == INFY
    assert order.fill is None  # Kite's order object carries no cost breakdown; that is the ledger's


def test_order_lookup_of_an_unknown_id_raises() -> None:
    with pytest.raises(UnknownOrderError):
        _kite().order(UNKNOWN_ORDER_ID)


def test_modify_a_terminal_order_raises_not_modifiable() -> None:
    routes = dict(_DEFAULT_ROUTES)
    routes[("GET", f"/orders/{LIVE_ORDER_ID}")] = "order_complete.json"
    broker = _kite(dry_run=False, routes=routes)
    with pytest.raises(OrderNotModifiableError):
        broker.modify(LIVE_ORDER_ID, quantity=5, live_confirm=True)
