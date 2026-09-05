"""M4.5 — the `Broker` interface and the `SimBroker` fill model.

What is under test:

1. **The fill model.** An EOD decision stages an order that fills at the *next* session's configured
   reference price (open, or a conservative VWAP band) with the shared cost model and liquidity-
   scaled slippage applied — and the direction of every adjustment is adverse, so the test fails if
   the slippage or the band is inverted into a favourable fill.
2. **No fractional shares.** The interface cannot express a fractional, zero, negative or
   non-integral quantity; every such construction raises.
3. **Invariant #5** — one decision code path. A grep over `analyst/` asserts nothing there imports a
   concrete broker module; the decision layer sees only the `Broker` protocol.

Plus the surrounding behaviour a broker must get right: T+1 settlement of buys into holdings,
rejection (never silent skipping) of unfillable orders, the cash ledger, margins, and the order
lifecycle. Nothing here touches the network, DuckDB, or the wall clock — the market is in memory and
the clock is frozen (B10), so the whole file is offline and deterministic.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.clock import FrozenClock
from execution.broker import (
    Broker,
    FractionalQuantityError,
    Holding,
    OrderNotModifiableError,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    UnknownOrderError,
)
from execution.broker import Exchange as BrokerExchange
from execution.broker import Side as BrokerSide
from execution.costs import CostModel, Exchange, Side, load_rate_card
from execution.sim_broker import (
    DuplicateStagedOrderError,
    FillPolicy,
    NoReferenceBarError,
    ReferenceBar,
    ReferencePrice,
    SimBroker,
    SlippageModel,
)
from tests.unit.test_clock_guard import code_lines

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

INFY: Final[str] = "INE009A01021"
TCS: Final[str] = "INE467B01029"

#: A uniform-stamp-duty date (post 2020-07-01), so the cost model needs no account state.
DECISION_DAY: Final[date] = date(2024, 1, 1)
S1: Final[date] = date(2024, 1, 2)
S2: Final[date] = date(2024, 1, 3)
S3: Final[date] = date(2024, 1, 4)


class InMemoryMarket:
    """A `SessionMarket` from a session list and a bar table — the offline stand-in for M4.1."""

    def __init__(self, sessions: list[date], bars: dict[tuple[str, date], ReferenceBar]) -> None:
        self._sessions = sorted(sessions)
        self._bars = bars

    def next_session(self, after: date) -> date:
        for session in self._sessions:
            if session > after:
                return session
        raise NoReferenceBarError(f"no session after {after.isoformat()}")

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        try:
            return self._bars[(isin, session)]
        except KeyError:
            raise NoReferenceBarError(f"no bar for {isin} on {session.isoformat()}") from None


def _bar(
    isin: str,
    session: date,
    *,
    open_: str,
    vwap: str,
    traded_value: str = "100000000",
) -> ReferenceBar:
    return ReferenceBar(
        isin=isin,
        session=session,
        exchange=Exchange.NSE,
        open=Decimal(open_),
        vwap=Decimal(vwap),
        traded_value=Decimal(traded_value),
    )


def _broker(
    market: InMemoryMarket,
    *,
    cash: str = "10000000",
    policy: FillPolicy | None = None,
    clock: FrozenClock | None = None,
) -> SimBroker:
    return SimBroker(
        clock=clock if clock is not None else FrozenClock(DECISION_DAY),
        cost_model=CostModel(load_rate_card()),
        market=market,
        opening_cash=Decimal(cash),
        policy=policy,
    )


# ── acceptance #1: an EOD decision fills at the next session's reference price ──────────────────


def test_eod_buy_fills_at_next_session_open_with_costs_and_slippage() -> None:
    """Place after the close, fill at the next session — at open, adverse slippage, real costs."""
    traded = Decimal("100000000")
    market = InMemoryMarket(
        [S1], {(INFY, S1): _bar(INFY, S1, open_="100", vwap="101", traded_value=str(traded))}
    )
    broker = _broker(market, policy=FillPolicy(reference=ReferencePrice.OPEN))

    order = broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    assert order.status is OrderStatus.STAGED
    assert order.decision_date == DECISION_DAY
    assert order.target_session == S1  # next session, not the decision day

    (filled,) = broker.execute_session(S1)
    assert filled.status is OrderStatus.COMPLETE
    fill = filled.fill
    assert fill is not None

    # Reference is the session open, before impact.
    assert fill.reference_price == Decimal("100")

    # Slippage: base + impact * participation, participation = (100*10)/traded_value.
    participation = (Decimal("100") * 10) / traded
    expected_bps = Decimal("2") + Decimal("50") * participation
    assert fill.slippage_bps == expected_bps

    # Fill is the reference nudged *up* by that slippage, then rounded *up* to the paisa tick — a
    # buy never fills below its reference and never at a price no exchange could print.
    unquantised = Decimal("100") * (Decimal("1") + expected_bps / Decimal("10000"))
    expected_fill = unquantised.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    assert unquantised < expected_fill <= unquantised + Decimal("0.01")
    assert fill.fill_price == expected_fill
    assert fill.fill_price > fill.reference_price

    # The shared cost model priced it, and cash fell by turnover + costs.
    assert fill.cost.total > Decimal("0")
    assert broker.cash == Decimal("10000000") - fill.cost.net_amount

    # A buy is an unsettled position today, a holding only after the next session settles.
    assert broker.positions() == (
        Position(
            isin=INFY,
            exchange=BrokerExchange.NSE,
            quantity=10,
            average_price=expected_fill,
            session=S1,
        ),
    )
    assert broker.holdings() == ()


def test_vwap_band_reference_is_conservative_on_each_side() -> None:
    """VWAP_BAND fills a buy above VWAP and a sell below it — the band is always adverse."""
    band = Decimal("5")
    huge = "100000000000"  # so slippage is ~base and does not obscure the band
    market = InMemoryMarket(
        [S1, S2, S3],
        {
            (INFY, S1): _bar(INFY, S1, open_="200", vwap="100", traded_value=huge),
            (INFY, S3): _bar(INFY, S3, open_="200", vwap="100", traded_value=huge),
        },
    )
    policy = FillPolicy(
        reference=ReferencePrice.VWAP_BAND,
        vwap_band_bps=band,
        slippage=SlippageModel(base_bps=Decimal("0"), impact_bps=Decimal("0")),
    )
    clock = FrozenClock(DECISION_DAY)
    broker = _broker(market, policy=policy, clock=clock)
    up = Decimal("100") * (Decimal("1") + band / Decimal("10000"))
    down = Decimal("100") * (Decimal("1") - band / Decimal("10000"))

    broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    (buy_filled,) = broker.execute_session(S1)
    assert buy_filled.fill is not None
    # Reference ignores the open (200) and uses VWAP (100) nudged up by the band.
    assert buy_filled.fill.reference_price == up

    # Now sell out of the settled holding; the band nudges the sell reference *down*.
    clock.freeze_at(S2)  # a later decision day whose next session is S3
    broker.place(OrderRequest(isin=INFY, side=Side.SELL, quantity=10))
    (sell_filled,) = broker.execute_session(S3)
    assert sell_filled.fill is not None
    assert sell_filled.fill.reference_price == down


def test_slippage_grows_with_participation() -> None:
    """The same order in a thinner session pays more slippage — liquidity scales the impact."""
    deep = InMemoryMarket(
        [S1], {(INFY, S1): _bar(INFY, S1, open_="100", vwap="100", traded_value="100000000")}
    )
    # Thin enough that the extra slippage is worth more than one paisa tick on a ₹100 name: at
    # ₹20,000 of session turnover a ₹1,000 order is 5% participation, i.e. +2.5 bps on the base.
    thin = InMemoryMarket(
        [S1], {(INFY, S1): _bar(INFY, S1, open_="100", vwap="100", traded_value="20000")}
    )
    deep_broker = _broker(deep)
    thin_broker = _broker(thin)

    deep_broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    thin_broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    (deep_fill,) = deep_broker.execute_session(S1)
    (thin_fill,) = thin_broker.execute_session(S1)
    assert deep_fill.fill is not None and thin_fill.fill is not None
    assert thin_fill.fill.slippage_bps > deep_fill.fill.slippage_bps
    assert thin_fill.fill.fill_price > deep_fill.fill.fill_price


# ── acceptance #2: fractional quantities are impossible ────────────────────────────────────────


def test_fractional_quantity_raises() -> None:
    with pytest.raises(FractionalQuantityError, match="whole number of shares"):
        OrderRequest(isin=INFY, side=Side.BUY, quantity=1.5)  # type: ignore[arg-type]


def test_decimal_quantity_raises() -> None:
    with pytest.raises(FractionalQuantityError, match="whole number of shares"):
        OrderRequest(isin=INFY, side=Side.BUY, quantity=Decimal("10"))  # type: ignore[arg-type]


def test_boolean_quantity_raises() -> None:
    """`True` is an int in Python but is not a share count."""
    with pytest.raises(FractionalQuantityError, match="whole number of shares"):
        OrderRequest(isin=INFY, side=Side.BUY, quantity=True)


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_quantity_raises(bad: int) -> None:
    with pytest.raises(FractionalQuantityError, match="positive"):
        OrderRequest(isin=INFY, side=Side.BUY, quantity=bad)


def test_modify_to_a_fractional_quantity_raises() -> None:
    """The whole-share guard holds on the modify path too, not only at first placement."""
    market = InMemoryMarket([S1], {(INFY, S1): _bar(INFY, S1, open_="100", vwap="100")})
    broker = _broker(market)
    order = broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    with pytest.raises(FractionalQuantityError):
        broker.modify(order.order_id, quantity=3.5)  # type: ignore[arg-type]


# ── acceptance #3: analyst/ imports no concrete broker (invariant #5) ──────────────────────────

#: The concrete brokers the decision layer must never name. `Broker` (the protocol) is allowed.
_CONCRETE_BROKER_TOKENS: Final[tuple[str, ...]] = (
    "sim_broker",
    "kite_broker",
    "SimBroker",
    "KiteBroker",
)


def _analyst_python_files() -> list[Path]:
    return sorted(
        path for path in (REPO_ROOT / "analyst").rglob("*.py") if "__pycache__" not in path.parts
    )


def _mentions_concrete_broker(source: str) -> list[str]:
    """Every code line (comments and strings blanked) that names a concrete broker."""
    return [
        line
        for line in code_lines(source)
        if any(token in line for token in _CONCRETE_BROKER_TOKENS)
    ]


def test_analyst_imports_no_concrete_broker() -> None:
    """Invariant #5: `analyst/` reaches execution only through the `Broker` protocol."""
    scanned = _analyst_python_files()
    assert len(scanned) > 3, f"only {len(scanned)} analyst files walked — this would pass vacuously"
    offenders = {
        path.relative_to(REPO_ROOT).as_posix(): hits
        for path in scanned
        if (hits := _mentions_concrete_broker(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "analyst/ must import the Broker protocol, never a concrete broker:\n"
        + "\n".join(f"{name}: {lines}" for name, lines in offenders.items())
    )


def test_the_grep_guard_would_catch_a_real_violation() -> None:
    """A vacuous guard is worse than none: a concrete import in analyst source must be caught."""
    clean = "from execution.broker import Broker\n\nbroker: Broker\n"
    assert _mentions_concrete_broker(clean) == []
    dirty = clean + "from execution.sim_broker import SimBroker\n"
    assert _mentions_concrete_broker(dirty)


# ── settlement, rejection, ledger, margins, lifecycle ──────────────────────────────────────────


def test_buy_settles_into_a_holding_on_the_next_session() -> None:
    """A buy is a position on its fill session and a settled holding once the next session runs."""
    market = InMemoryMarket(
        [S1, S2],
        {(INFY, S1): _bar(INFY, S1, open_="100", vwap="100")},
    )
    broker = _broker(market)
    broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    broker.execute_session(S1)
    assert [p.isin for p in broker.positions()] == [INFY]
    assert broker.holdings() == ()

    broker.execute_session(S2)  # nothing staged for S2, but S1's buy settles
    assert broker.positions() == ()
    (held,) = broker.holdings()
    assert held.isin == INFY
    assert held.quantity == 10


def test_sell_reduces_the_holding_and_credits_cash() -> None:
    market = InMemoryMarket(
        [S1, S2, S3],
        {
            (INFY, S1): _bar(INFY, S1, open_="100", vwap="100"),
            (INFY, S3): _bar(INFY, S3, open_="120", vwap="120"),
        },
    )
    clock = FrozenClock(DECISION_DAY)
    broker = _broker(market, clock=clock)
    broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    broker.execute_session(S1)

    clock.freeze_at(S2)
    broker.place(OrderRequest(isin=INFY, side=Side.SELL, quantity=4))
    cash_before = broker.cash
    (sold,) = broker.execute_session(S3)
    assert sold.status is OrderStatus.COMPLETE
    assert sold.fill is not None
    (held,) = broker.holdings()
    assert held.quantity == 6  # 10 bought, 4 sold
    assert broker.cash == cash_before + sold.fill.cost.net_amount  # proceeds net of costs


def test_sell_without_a_settled_holding_is_rejected() -> None:
    market = InMemoryMarket([S1], {(INFY, S1): _bar(INFY, S1, open_="100", vwap="100")})
    broker = _broker(market)
    order = broker.place(OrderRequest(isin=INFY, side=Side.SELL, quantity=5))
    (rejected,) = broker.execute_session(S1)
    assert rejected.status is OrderStatus.REJECTED
    assert rejected.fill is None
    assert rejected.reason is not None and "insufficient holdings" in rejected.reason
    assert order.order_id == rejected.order_id


def test_buy_beyond_cash_is_rejected() -> None:
    market = InMemoryMarket([S1], {(INFY, S1): _bar(INFY, S1, open_="100", vwap="100")})
    broker = _broker(market, cash="500")  # 10 @ ~100 costs > 500
    broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    (rejected,) = broker.execute_session(S1)
    assert rejected.status is OrderStatus.REJECTED
    assert rejected.reason is not None and "insufficient cash" in rejected.reason
    assert broker.cash == Decimal("500")  # nothing was debited
    assert broker.positions() == ()


def test_missing_reference_bar_is_rejected_not_crashed() -> None:
    market = InMemoryMarket([S1], {})  # session exists, but no bar for INFY
    broker = _broker(market)
    broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    (rejected,) = broker.execute_session(S1)
    assert rejected.status is OrderStatus.REJECTED
    assert rejected.reason is not None and "no reference bar" in rejected.reason


def test_ledger_records_the_fill_and_is_ordered() -> None:
    market = InMemoryMarket(
        [S1, S2, S3],
        {
            (INFY, S1): _bar(INFY, S1, open_="100", vwap="100"),
            (INFY, S3): _bar(INFY, S3, open_="110", vwap="110"),
        },
    )
    clock = FrozenClock(DECISION_DAY)
    broker = _broker(market, clock=clock)
    broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    broker.execute_session(S1)
    clock.freeze_at(S2)
    broker.place(OrderRequest(isin=INFY, side=Side.SELL, quantity=10))
    broker.execute_session(S3)

    ledger = broker.ledger()
    assert [e.seq for e in ledger] == [0, 1]
    buy_line, sell_line = ledger
    assert buy_line.debit > Decimal("0") and buy_line.credit == Decimal("0")
    assert sell_line.credit > Decimal("0") and sell_line.debit == Decimal("0")
    assert sell_line.balance == broker.cash  # last balance is current cash


def test_margins_split_cash_and_deployed_capital() -> None:
    market = InMemoryMarket([S1], {(INFY, S1): _bar(INFY, S1, open_="100", vwap="100")})
    broker = _broker(market, cash="1000000")
    broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=100))
    broker.execute_session(S1)
    margins = broker.margins()
    assert margins.available == broker.cash
    assert margins.utilised > Decimal("0")
    assert margins.total == margins.available + margins.utilised


# ── order lifecycle ────────────────────────────────────────────────────────────────────────────


def test_modify_replaces_the_staged_quantity() -> None:
    market = InMemoryMarket([S1], {(INFY, S1): _bar(INFY, S1, open_="100", vwap="100")})
    broker = _broker(market)
    order = broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    modified = broker.modify(order.order_id, quantity=25)
    assert modified.request.quantity == 25
    assert modified.status is OrderStatus.STAGED
    (filled,) = broker.execute_session(S1)
    assert filled.fill is not None and filled.fill.quantity == 25


def test_cancel_a_staged_order_stops_it_filling() -> None:
    market = InMemoryMarket([S1], {(INFY, S1): _bar(INFY, S1, open_="100", vwap="100")})
    broker = _broker(market)
    order = broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    cancelled = broker.cancel(order.order_id)
    assert cancelled.status is OrderStatus.CANCELLED
    assert broker.execute_session(S1) == ()  # nothing left staged for S1
    assert broker.positions() == ()


def test_modify_or_cancel_a_filled_order_raises() -> None:
    market = InMemoryMarket([S1], {(INFY, S1): _bar(INFY, S1, open_="100", vwap="100")})
    broker = _broker(market)
    order = broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    broker.execute_session(S1)
    with pytest.raises(OrderNotModifiableError):
        broker.modify(order.order_id, quantity=5)
    with pytest.raises(OrderNotModifiableError):
        broker.cancel(order.order_id)


def test_modify_unknown_order_raises() -> None:
    market = InMemoryMarket([S1], {})
    broker = _broker(market)
    with pytest.raises(UnknownOrderError):
        broker.modify("SIM-999999", quantity=1)


def test_a_second_staged_order_for_the_same_scrip_and_session_is_refused() -> None:
    """The EOD model nets to one order per scrip per session (keeps the per-day DP charge right)."""
    market = InMemoryMarket([S1], {(INFY, S1): _bar(INFY, S1, open_="100", vwap="100")})
    broker = _broker(market)
    broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    with pytest.raises(DuplicateStagedOrderError):
        broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=5))
    # A different scrip is fine.
    broker.place(OrderRequest(isin=TCS, side=Side.BUY, quantity=5))


def test_executing_an_earlier_session_after_a_later_one_raises() -> None:
    market = InMemoryMarket(
        [S1, S2],
        {(INFY, S1): _bar(INFY, S1, open_="100", vwap="100")},
    )
    broker = _broker(market)
    broker.execute_session(S2)
    with pytest.raises(ValueError, match="forward only"):
        broker.execute_session(S1)


def test_order_ids_are_deterministic() -> None:
    """Sequential ids, no clock or randomness — replay reproduces them byte for byte (B10)."""
    market = InMemoryMarket([S1], {(INFY, S1): _bar(INFY, S1, open_="100", vwap="100")})
    broker = _broker(market)
    first = broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=1))
    second = broker.place(OrderRequest(isin=TCS, side=Side.BUY, quantity=1))
    assert first.order_id == "SIM-000000"
    assert second.order_id == "SIM-000001"


def test_sim_broker_satisfies_the_broker_protocol() -> None:
    """The structural guarantee behind invariant #5: SimBroker *is* a Broker."""
    market = InMemoryMarket([S1], {})
    broker = _broker(market)
    assert isinstance(broker, Broker)


def test_broker_side_and_exchange_are_the_cost_models_own() -> None:
    """One vocabulary: the broker re-exports the cost model's Side/Exchange, not parallel copies."""
    assert BrokerSide is Side
    assert BrokerExchange is Exchange
    assert OrderType.MARKET == "MARKET"  # the enum exists and is usable
    assert Holding.__name__ == "Holding"  # exported symbol
