"""X1 — sale proceeds in settlement are the book's: valuation and reconciliation must count them.

Under T+2 a sale's proceeds leave `margins().available` for a session before they settle. Sizing a
buy from `available` is right; *valuing* the book from it is not — every sale would read as a
one-to-two-session drawdown that is only the settlement cycle. So:

1. `Margins.cash_value` (and `total`) include `unsettled_proceeds`; `available` does not.
2. The two backtest valuation sites — momentum v2's capital and forecast-daily's per-name target —
   are continuous across a sale in settlement, and fail if the proceeds are dropped.
3. Reconciliation compares the internal book (which credits a sale at fill) against the broker's
   cash *including* proceeds in settlement: a pending sale is not a break, a genuine cash
   mismatch still is.

Offline: the 2019 Holi scenario from `test_settlement` on the checked-in exchange calendar.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

from backtest.policies.forecast_daily import (
    ForecastDailyParameters,
    ForecastDailyPolicy,
)
from backtest.policies.momentum_v2 import MomentumV2Policy
from backtest.replay import SessionContext
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import PitContext
from execution.broker import Holding, Margins, OrderRequest, OrderStatus
from execution.costs import CostModel, Side, load_rate_card
from execution.kill_switch import KillSwitch
from execution.recon import BreakKind, Reconciler, RecordingAlerter
from execution.sim_broker import SimBroker
from execution.staging import InternalBook
from tests.unit.test_forecast_daily import _Data as _ForecastData
from tests.unit.test_forecast_daily import _record
from tests.unit.test_momentum_v2 import _Data as _MomentumData
from tests.unit.test_settlement import SELL_2019, T1_2019, _market, _run, _sessions
from tests.unit.test_sim_broker import INFY

_STATE: Final[str] = "KA"
_OPENING: Final[Decimal] = Decimal("110000")
_MARK: Final[Decimal] = Decimal("1000")  # the in-memory market opens and trades INFY at 1000


def _ctx(session: date, broker: object) -> SessionContext:
    return SessionContext(
        session=session,
        pit=PitContext(as_of=session),
        broker=broker,  # type: ignore[arg-type]  # SimBroker or a fake read surface
        clock=FrozenClock(session),
    )


def _holding_then_sale(
    book: InternalBook | None = None,
) -> tuple[SimBroker, FrozenClock, Decimal]:
    """Hold 100 INFY by the evening of 2019-03-19; return the broker and that evening's value.

    If `book` is given, every fill is also posted to it, as the staging coordinator would.
    """
    clock = FrozenClock(date(2019, 3, 12))
    broker = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state=_STATE),
        market=_market(_sessions(date(2019, 3, 1), date(2019, 4, 30))),
        opening_cash=_OPENING,
    )
    bought = _run(
        broker,
        clock,
        date(2019, 3, 12),
        OrderRequest(isin=INFY, side=Side.BUY, quantity=100),
        date(2019, 3, 13),
    )
    assert bought.fill is not None
    if book is not None:
        book.apply(bought.fill)
    for session in _sessions(date(2019, 3, 14), date(2019, 3, 19)):
        broker.execute_session(session)
    value_before = broker.margins().cash_value + 100 * _MARK
    return broker, clock, value_before


def _sell(broker: SimBroker, clock: FrozenClock, book: InternalBook | None = None) -> Decimal:
    """Sell the 100 INFY on 2019-03-20 (T+2 → settles 03-25); return the net proceeds."""
    sold = _run(
        broker,
        clock,
        date(2019, 3, 19),
        OrderRequest(isin=INFY, side=Side.SELL, quantity=100),
        SELL_2019,
    )
    assert sold.status is OrderStatus.COMPLETE and sold.fill is not None
    if book is not None:
        book.apply(sold.fill)
    # The situation under test: the proceeds are owed, not free.
    assert broker.unsettled_proceeds == sold.fill.net_cash > 0
    return sold.fill.net_cash


# ── Margins ────────────────────────────────────────────────────────────────────────────────────


def test_margins_value_proceeds_in_settlement_but_do_not_make_them_available() -> None:
    margins = Margins(
        available=Decimal("100"), utilised=Decimal("50"), unsettled_proceeds=Decimal("30")
    )
    assert margins.available == Decimal("100")
    assert margins.cash_value == Decimal("130")
    assert margins.total == Decimal("180")
    # A broker that reports no separate figure is unchanged.
    assert Margins(available=Decimal("100"), utilised=Decimal("50")).total == Decimal("150")


def test_sim_broker_reports_proceeds_in_settlement_on_its_margins() -> None:
    broker, clock, _ = _holding_then_sale()
    proceeds = _sell(broker, clock)
    margins = broker.margins()
    assert margins.unsettled_proceeds == proceeds
    assert margins.cash_value == margins.available + proceeds

    broker.execute_session(T1_2019)  # the eve of settlement: released for tomorrow's fills
    assert broker.margins().unsettled_proceeds == Decimal("0")


# ── valuation is continuous across a sale in settlement ─────────────────────────────────────────


def _continuous(before: Decimal, after: Decimal) -> bool:
    """Within the sale's own friction (slippage + costs, well under 1 %), not a missing sale."""
    return abs(after - before) < before / 100


def test_momentum_v2_capital_is_continuous_across_a_sale_in_settlement() -> None:
    """The evening of the sale, capital still counts the proceeds. Dropped, it falls ~90 %."""
    broker, clock, before = _holding_then_sale()
    policy = MomentumV2Policy(_MomentumData(()))
    held_before = {h.isin: h for h in broker.holdings()}
    assert policy._capital(_ctx(date(2019, 3, 19), broker), held_before, {INFY: _MARK}, ()) == (
        before
    )

    _sell(broker, clock)
    held_after: dict[str, Holding] = {h.isin: h for h in broker.holdings()}
    assert held_after == {}
    after = policy._capital(_ctx(SELL_2019, broker), held_after, {}, ())
    assert _continuous(before, after), (before, after)


class _SettlingBroker:
    """A read surface with some cash free and the rest of the book in settlement."""

    def __init__(self, *, available: Decimal, unsettled: Decimal) -> None:
        self._margins = Margins(
            available=available, utilised=Decimal("0"), unsettled_proceeds=unsettled
        )

    def holdings(self) -> tuple[Holding, ...]:
        return ()

    def margins(self) -> Margins:
        return self._margins


def test_forecast_daily_sizes_each_name_off_the_whole_book_including_unsettled_cash() -> None:
    """₹20 lakh book, ₹19 lakh of it in settlement: the per-name target is ₹1 lakh, not ₹5,000.

    With `top_n` 20 the target is one twentieth of the book. Counting only the free ₹1 lakh would
    size the name at ₹5,000 (50 shares at ₹100); counting the book sizes it at the target, capped
    by what is free to spend (98 % of ₹1 lakh = 980 shares).
    """
    params = ForecastDailyParameters()
    assert params.top_n == 20
    policy = ForecastDailyPolicy(_ForecastData([_record("INE001A01010", "0.050")]), params)
    broker = _SettlingBroker(available=Decimal("100000"), unsettled=Decimal("1900000"))

    decision = policy.decide(_ctx(date(2024, 3, 1), broker))

    (order,) = decision.orders
    assert order.side is Side.BUY
    assert order.quantity > 500, "per-name target ignored the proceeds in settlement"
    assert order.quantity * Decimal("100") <= Decimal("100000")  # the spend is still free cash


# ── reconciliation ─────────────────────────────────────────────────────────────────────────────


def _reconciler(broker: SimBroker, book: InternalBook, tmp_path: Path) -> Reconciler:
    clock = FrozenClock(SELL_2019)
    return Reconciler(
        broker=broker,
        book=book,
        kill_switch=KillSwitch(tmp_path / "kill_switch.json", clock=clock),
        alerter=RecordingAlerter(),
        clock=clock,
    )


def test_a_sale_pending_settlement_is_not_a_recon_break(tmp_path: Path) -> None:
    """The book credits the sale at fill; the broker owes it until T+2. Same money — no break."""
    book = InternalBook(opening_cash=_OPENING)
    broker, clock, _ = _holding_then_sale(book)
    _sell(broker, clock, book)
    assert book.cash != broker.margins().available  # would break on available alone

    reconciler = _reconciler(broker, book, tmp_path)
    result = reconciler.reconcile(SELL_2019)
    assert result.ok, result
    assert not reconciler.kill_switch.is_tripped


def test_a_genuine_cash_mismatch_during_settlement_still_breaks(tmp_path: Path) -> None:
    """One rupee the book believes in and the broker does not hold still freezes trading."""
    book = InternalBook(opening_cash=_OPENING + Decimal("1"))
    broker, clock, _ = _holding_then_sale(book)
    _sell(broker, clock, book)

    reconciler = _reconciler(broker, book, tmp_path)
    result = reconciler.reconcile(SELL_2019)
    assert not result.ok
    assert [b.kind for b in result.breaks] == [BreakKind.CASH]
    assert reconciler.kill_switch.is_tripped
