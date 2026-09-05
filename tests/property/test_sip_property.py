"""M4.7 / EXECUTION_PLAN §5.6 — properties of the whole-share SIP allocator under generated inputs.

The unit tests show the allocator does the right thing on a handful of hand-built models. A SIP
allocator's real claims are universal, and this file generates the cases the hand-written ones do
not reach: a model of any shape, prices of any scale, an instalment that may not afford a single
share of anything, cash carried in from an earlier month, and an existing book of any size.

Properties asserted on every generated instalment:

* **Cash is conserved.** ``deployed + residual_cash == instalment + carried_in`` exactly — a paisa
  neither appears nor vanishes, and the allocator never spends what it does not have.
* **Every order is a positive whole share of a model name** at the model's price (no fractional
  share, no name outside the model, no invented price — invariant echoed by ``OrderRequest``).
* **Nothing affordable is left unbought when it would reduce drift** — the stopping condition is
  exact: after the allocation, no single additional affordable share of any name lowers the L1
  drift. Reversing the greedy's comparison would leave such a share on the table and fail here.
* **Deploying is never worse than idling** — the allocation's drift is no larger than the drift of
  buying nothing at all, measured on the same fixed capital base the allocator uses.
* **Determinism** — the same inputs produce an identical allocation (§8.3.3).

All Decimal, no clock, no store (CLAUDE.md, B8).
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from backtest.sip import SipAllocation, simulate_sip_instalment

_ZERO = Decimal("0")
_ONE = Decimal("1")

_ISINS = (
    "INE001A01011",
    "INE002A01018",
    "INE003A01024",
    "INE004A01020",
    "INE005A01019",
    "INE006A01025",
)


@st.composite
def _model(draw: st.DrawFn) -> dict[str, Decimal]:
    """A model of 1..6 names with positive weights summing to exactly one (quantised)."""
    n = draw(st.integers(min_value=1, max_value=len(_ISINS)))
    raw = draw(st.lists(st.integers(min_value=1, max_value=1000), min_size=n, max_size=n))
    total = Decimal(sum(raw))
    weights: dict[str, Decimal] = {}
    running = _ZERO
    for isin, part in zip(_ISINS[: n - 1], raw[: n - 1], strict=True):
        weight = (Decimal(part) / total).quantize(Decimal("0.000001"))
        weights[isin] = weight
        running += weight
    weights[_ISINS[n - 1]] = _ONE - running  # the last name absorbs the quantisation residue
    return weights


# ₹50..₹20,000 per share, and instalments small enough that a case never buys more than a few
# hundred shares — the greedy is one share per step, so the generator bounds the walk, not the
# logic.
_price = st.integers(min_value=5_000, max_value=2_000_000).map(lambda p: Decimal(p) / 100)
_money = st.integers(min_value=0, max_value=20_000).map(Decimal)


@st.composite
def _instalment_case(
    draw: st.DrawFn,
) -> tuple[dict[str, Decimal], dict[str, Decimal], Decimal, Decimal, dict[str, Decimal]]:
    weights = draw(_model())
    prices = {isin: draw(_price) for isin in weights}
    instalment = draw(_money)
    carried = draw(st.integers(min_value=0, max_value=5_000).map(Decimal))
    existing = {
        isin: draw(st.integers(min_value=0, max_value=50_000).map(Decimal))
        for isin in weights
        if draw(st.booleans())
    }
    return weights, prices, instalment, carried, existing


def _drift(
    weights: dict[str, Decimal],
    prices: dict[str, Decimal],
    existing: dict[str, Decimal],
    bought: dict[str, int],
    capital: Decimal,
) -> Decimal:
    """The allocator's own L1 drift on its fixed capital base — re-derived independently here."""
    return sum(
        (
            abs(
                (existing.get(isin, _ZERO) + prices[isin] * bought.get(isin, 0)) / capital
                - weights[isin]
            )
            for isin in weights
        ),
        _ZERO,
    )


@given(case=_instalment_case())
@settings(max_examples=150, deadline=None)
def test_cash_is_conserved_and_orders_are_whole_shares_of_model_names(
    case: tuple[dict[str, Decimal], dict[str, Decimal], Decimal, Decimal, dict[str, Decimal]],
) -> None:
    weights, prices, instalment, carried, existing = case
    allocation = simulate_sip_instalment(
        instalment=instalment,
        targets=weights,
        prices=prices,
        carried_in=carried,
        existing_value=existing or None,
    )
    assert isinstance(allocation, SipAllocation)
    assert allocation.deployed + allocation.residual_cash == instalment + carried
    assert allocation.residual_cash >= _ZERO
    for order in allocation.orders:
        assert order.isin in weights
        assert isinstance(order.quantity, int) and order.quantity > 0
        assert order.price == prices[order.isin]
    # Every model name is reported in the drift table, bought or not.
    assert {d.isin for d in allocation.drifts} == set(weights)


@given(case=_instalment_case())
@settings(max_examples=150, deadline=None)
def test_the_stopping_condition_is_exact_and_deploying_never_hurts(
    case: tuple[dict[str, Decimal], dict[str, Decimal], Decimal, Decimal, dict[str, Decimal]],
) -> None:
    """No single further affordable share would lower drift; and the result beats an idle book."""
    weights, prices, instalment, carried, existing = case
    allocation = simulate_sip_instalment(
        instalment=instalment,
        targets=weights,
        prices=prices,
        carried_in=carried,
        existing_value=existing or None,
    )
    capital = sum(existing.values(), _ZERO) + instalment + carried
    if capital == _ZERO:
        assert allocation.orders == ()
        return
    bought = {order.isin: order.quantity for order in allocation.orders}
    final = _drift(weights, prices, existing, bought, capital)
    idle = _drift(weights, prices, existing, {}, capital)
    assert final <= idle
    for isin in weights:
        if prices[isin] <= allocation.residual_cash:
            plus_one = dict(bought)
            plus_one[isin] = plus_one.get(isin, 0) + 1
            assert _drift(weights, prices, existing, plus_one, capital) >= final, (
                f"one more affordable share of {isin} would still reduce drift"
            )


@given(case=_instalment_case())
@settings(max_examples=50, deadline=None)
def test_same_inputs_give_the_same_allocation(
    case: tuple[dict[str, Decimal], dict[str, Decimal], Decimal, Decimal, dict[str, Decimal]],
) -> None:
    weights, prices, instalment, carried, existing = case
    first = simulate_sip_instalment(
        instalment=instalment,
        targets=weights,
        prices=prices,
        carried_in=carried,
        existing_value=existing or None,
    )
    second = simulate_sip_instalment(
        instalment=instalment,
        targets=weights,
        prices=prices,
        carried_in=carried,
        existing_value=existing or None,
    )
    assert first == second
