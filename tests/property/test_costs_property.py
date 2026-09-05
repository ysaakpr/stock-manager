"""M4.4 / invariant #4 — properties of the one Indian transaction-cost model under generated trades.

`tests/unit/test_costs.py` reconciles hand-priced contract notes line by line. This file asserts
what must hold for *every* trade the model can be asked to price, across every rate regime the card
carries (2017-07 GST rollout, 2020-07 uniform stamp duty, 2024-10 and 2025-04 rate changes):

* **Costs only ever work against the account.** Every line is non-negative, ``total`` is exactly
  their sum, and ``net_amount`` is above turnover on a buy and below it on a sell.
* **Whole-rupee taxes are whole rupees**, paisa-billed lines have at most two decimals, and every
  figure is a ``Decimal`` (CLAUDE.md).
* **Monotone in size.** One more share at the same price never *lowers* the total — half-up rounding
  is non-decreasing, so a regression that re-rounded a line the wrong way would show up here.
* **One trade, one price.** ``charge(trade)`` equals ``charge_all([trade])[0]``, and in a day's
  batch the DP charge lands exactly once per (scrip, day) and only on sells.
* **Stamp duty is one-sided in the uniform era** (buy only, from 2020-07-01) and levied on both legs
  before it — the regime switch encoded in the card, checked over generated dates.

No network, no clock: the trade date is generated and the rate card is the checked-in one.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from execution.costs import CostBreakdown, CostModel, Exchange, Side, Trade, load_rate_card

_ZERO = Decimal("0")
_PAISA = Decimal("0.01")
_RUPEE = Decimal("1")
_UNIFORM_STAMP_FROM = date(2020, 7, 1)
_CARD_START = date(2017, 7, 1)

_ISINS = ("INE009A01021", "INE002A01018", "INE040A01034", "INE467B01029")

_isin = st.sampled_from(_ISINS)
_side = st.sampled_from(list(Side))
_exchange = st.sampled_from(list(Exchange))
_quantity = st.integers(min_value=1, max_value=200_000)
# Paisa-quantised prices from ₹0.05 to ₹50,000.
_price = st.integers(min_value=5, max_value=5_000_000).map(lambda p: Decimal(p) / 100)
_trade_date = st.integers(min_value=0, max_value=(date(2026, 9, 1) - _CARD_START).days).map(
    lambda d: _CARD_START + timedelta(days=d)
)


@st.composite
def _trade(draw: st.DrawFn) -> Trade:
    return Trade(
        isin=draw(_isin),
        trade_date=draw(_trade_date),
        side=draw(_side),
        quantity=draw(_quantity),
        price=draw(_price),
        exchange=draw(_exchange),
    )


def _model() -> CostModel:
    # Maharashtra is the a-priori account state the backtest uses; needed for pre-2020 stamp duty.
    return CostModel(load_rate_card(), account_state="MH")


def _lines(breakdown: CostBreakdown) -> tuple[Decimal, ...]:
    return (
        breakdown.brokerage,
        breakdown.securities_transaction_tax,
        breakdown.exchange_transaction_charge,
        breakdown.sebi_turnover_fee,
        breakdown.goods_and_services_tax,
        breakdown.stamp_duty,
        breakdown.depository_charge,
    )


@given(trade=_trade())
@settings(max_examples=400, deadline=None)
def test_costs_always_work_against_the_account(trade: Trade) -> None:
    breakdown = _model().charge(trade)
    lines = _lines(breakdown)
    assert all(isinstance(line, Decimal) for line in lines)
    assert all(line >= _ZERO for line in lines)
    assert breakdown.total == sum(lines, _ZERO)
    assert breakdown.turnover == trade.price * trade.quantity
    if trade.side is Side.BUY:
        assert breakdown.net_amount >= breakdown.turnover
    else:
        assert breakdown.net_amount <= breakdown.turnover


@given(trade=_trade())
@settings(max_examples=300, deadline=None)
def test_taxes_round_to_the_rupee_and_fees_to_the_paisa(trade: Trade) -> None:
    breakdown = _model().charge(trade)
    assert breakdown.securities_transaction_tax == breakdown.securities_transaction_tax.quantize(
        _RUPEE
    )
    assert breakdown.stamp_duty == breakdown.stamp_duty.quantize(_RUPEE)
    for line in (
        breakdown.brokerage,
        breakdown.exchange_transaction_charge,
        breakdown.sebi_turnover_fee,
        breakdown.goods_and_services_tax,
        breakdown.depository_charge,
    ):
        assert line == line.quantize(_PAISA), f"{line} is not paisa-quantised"


@given(trade=_trade())
@settings(max_examples=300, deadline=None)
def test_one_more_share_never_lowers_the_total(trade: Trade) -> None:
    model = _model()
    bigger = Trade(
        isin=trade.isin,
        trade_date=trade.trade_date,
        side=trade.side,
        quantity=trade.quantity + 1,
        price=trade.price,
        exchange=trade.exchange,
    )
    assert model.charge(bigger).total >= model.charge(trade).total


@given(trade=_trade())
@settings(max_examples=200, deadline=None)
def test_a_single_trade_prices_the_same_alone_or_in_a_batch(trade: Trade) -> None:
    model = _model()
    assert model.charge(trade) == model.charge_all([trade])[0]


@given(trade=_trade())
@settings(max_examples=300, deadline=None)
def test_stamp_duty_sides_follow_the_regime_switch(trade: Trade) -> None:
    breakdown = _model().charge(trade)
    duty_expected_positive = trade.turnover >= Decimal("5000")  # half a rupee at 0.015% and below
    if trade.trade_date >= _UNIFORM_STAMP_FROM:
        if trade.side is Side.SELL:
            assert breakdown.stamp_duty == _ZERO
        elif duty_expected_positive:
            assert breakdown.stamp_duty > _ZERO
    elif duty_expected_positive:
        # State-wise era (Maharashtra): duty on both legs.
        assert breakdown.stamp_duty > _ZERO


@given(trade=_trade())
@settings(max_examples=200, deadline=None)
def test_the_dp_charge_is_sell_only_and_once_per_scrip_day(trade: Trade) -> None:
    model = _model()
    twin = Trade(
        isin=trade.isin,
        trade_date=trade.trade_date,
        side=trade.side,
        quantity=max(1, trade.quantity // 2),
        price=trade.price,
        exchange=trade.exchange,
    )
    first, second = model.charge_all([trade, twin])
    if trade.side is Side.BUY:
        assert first.depository_charge == _ZERO
        assert second.depository_charge == _ZERO
    else:
        assert first.depository_charge > _ZERO
        assert second.depository_charge == _ZERO
        # Charged alone, the twin would carry the DP line itself.
        assert model.charge(twin).depository_charge == first.depository_charge
