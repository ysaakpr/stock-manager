"""M4.7: SIP instalment mechanics under the no-fractional-shares constraint (EXECUTION_PLAN §5.6).

The three acceptance criteria each get a test that fails if the mechanic is wrong:

* **Whole shares + carried residual.** A ₹10k instalment over eight names must come out as integer
  share counts whose total cost never exceeds the cash available, with the unspendable remainder
  carried forward — not silently rounded away, and never a fractional share.
* **Drift is computed and reported.** Every model name (even those that got nothing this month)
  must appear in the drift report, actual weights measured on the invested book, and the summed L1
  drift exposed as a single number to journal.
* **Determinism.** The same inputs must produce byte-identical allocations, because a replay
  (M4.8) asserts exactly that (§8.3.3).

Everything is offline and clock-free (CLAUDE.md): an instalment's allocation is a pure function of
its inputs, so the tests construct amounts, weights and prices directly and assert on the result.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backtest.sip import (
    SipAllocation,
    SipError,
    SipOrder,
    WeightDrift,
    simulate_sip_instalment,
)
from execution.broker import OrderRequest, Side

_D = Decimal

# Eight real-format ISINs (checksum digit not validated here — only the shape is).
_EIGHT = [
    "INE001A01011",
    "INE002A01018",
    "INE003A01024",
    "INE004A01020",
    "INE005A01019",
    "INE006A01025",
    "INE007A01021",
    "INE008A01028",
]


def _equal_weights(isins: list[str]) -> dict[str, Decimal]:
    """Equal-weight model over the given names, summing to exactly one."""
    n = len(isins)
    each = (_ONE / n).quantize(_D("0.0001"))
    weights = dict.fromkeys(isins, each)
    # Absorb the rounding remainder into the first name so the model sums to exactly 1.
    weights[isins[0]] += _ONE - sum(weights.values(), _ZERO)
    return weights


_ZERO = _D("0")
_ONE = _D("1")


# ── acceptance 1: whole-share orders + carried residual ──────────────────────────────────────────


def test_instalment_produces_whole_shares_and_carries_residual() -> None:
    """A ₹10k instalment over 8 equal-weight names → integer buys plus a non-zero residual."""
    weights = _equal_weights(_EIGHT)
    # Prices that do not divide ₹10,000 cleanly into whole shares, so a remainder is unavoidable.
    prices = {
        _EIGHT[0]: _D("310"),
        _EIGHT[1]: _D("455"),
        _EIGHT[2]: _D("1270"),
        _EIGHT[3]: _D("640"),
        _EIGHT[4]: _D("205"),
        _EIGHT[5]: _D("880"),
        _EIGHT[6]: _D("1515"),
        _EIGHT[7]: _D("395"),
    }
    result = simulate_sip_instalment(instalment=_D("10000"), targets=weights, prices=prices)

    assert isinstance(result, SipAllocation)
    assert result.orders, "an instalment of 10k over sub-2k names must buy something"

    # Every order is a whole, positive share count (no fractional shares — the whole point).
    for order in result.orders:
        assert isinstance(order.quantity, int)
        assert order.quantity > 0

    # Cash conservation: deployed + residual == available, and nothing overspends.
    assert result.deployed + result.residual_cash == result.available
    assert result.available == _D("10000")
    assert result.deployed <= _D("10000")

    # The residual is genuinely carried forward, not rounded away.
    assert result.residual_cash > _ZERO
    # Stopping is justified: no affordable purchase left would reduce tracking drift further.
    assert _no_affordable_purchase_helps(result, weights, prices)


def _no_affordable_purchase_helps(
    result: SipAllocation, weights: dict[str, Decimal], prices: dict[str, Decimal]
) -> bool:
    """True if no still-affordable share would lower L1 drift from the final book (stop rule)."""
    total_capital = result.available  # fresh SIP, no existing holdings
    held = {o.isin: o.quantity for o in result.orders}
    values = {isin: prices[isin] * held.get(isin, 0) for isin in weights}
    current = sum((abs(values[i] / total_capital - weights[i]) for i in weights), _ZERO)
    for isin, price in prices.items():
        if price > result.residual_cash:
            continue
        after = sum(
            (
                abs((values[i] + (price if i == isin else _ZERO)) / total_capital - weights[i])
                for i in weights
            ),
            _ZERO,
        )
        if after < current:
            return False
    return True


def test_orders_convert_to_broker_requests() -> None:
    """Each SIP order becomes a whole-share market BUY the broker interface accepts."""
    weights = _equal_weights(_EIGHT)
    prices = {isin: _D("500") for isin in _EIGHT}
    result = simulate_sip_instalment(instalment=_D("10000"), targets=weights, prices=prices)
    for order in result.orders:
        req = order.to_order_request()
        assert isinstance(req, OrderRequest)
        assert req.side is Side.BUY
        assert req.quantity == order.quantity
        assert req.isin == order.isin


def test_expensive_name_is_skipped_and_its_cash_waits() -> None:
    """A name too expensive for its slice this month gets zero shares; the cash carries forward."""
    weights = _equal_weights(_EIGHT)
    prices = {isin: _D("300") for isin in _EIGHT}
    prices[_EIGHT[6]] = _D("40000")  # one name far above any single-name slice of a 10k instalment
    result = simulate_sip_instalment(instalment=_D("10000"), targets=weights, prices=prices)

    bought = {o.isin: o.quantity for o in result.orders}
    assert _EIGHT[6] not in bought, (
        "buying a 40k share from a 10k instalment would wreck the weights"
    )
    assert result.residual_cash > _ZERO


# ── acceptance 2: tracking drift computed and reported ───────────────────────────────────────────


def test_drift_is_reported_for_every_model_name() -> None:
    """Drift is reported for all 8 names (including any that got nothing), actual on the book."""
    weights = _equal_weights(_EIGHT)
    prices = {
        _EIGHT[0]: _D("310"),
        _EIGHT[1]: _D("455"),
        _EIGHT[2]: _D("1270"),
        _EIGHT[3]: _D("640"),
        _EIGHT[4]: _D("205"),
        _EIGHT[5]: _D("880"),
        _EIGHT[6]: _D("1515"),
        _EIGHT[7]: _D("395"),
    }
    result = simulate_sip_instalment(instalment=_D("10000"), targets=weights, prices=prices)

    assert len(result.drifts) == len(_EIGHT)
    reported = {d.isin for d in result.drifts}
    assert reported == set(_EIGHT)

    for d in result.drifts:
        assert isinstance(d, WeightDrift)
        assert d.target_weight == weights[d.isin]
        assert d.drift == d.actual_weight - d.target_weight

    # Actual weights are shares of the invested book — they sum to one (residual cash is excluded).
    total_actual = sum((d.actual_weight for d in result.drifts), _ZERO)
    assert total_actual == _ONE
    assert result.tracking_drift == sum((abs(d.drift) for d in result.drifts), _ZERO)
    assert result.tracking_drift >= _ZERO


def test_more_shares_track_the_model_more_closely() -> None:
    """A larger instalment (more whole shares) approximates an *unequal* model far more closely.

    With equal weights and equal prices the invested book is a perfect equal split at any size, so
    the lumpiness only shows against an unequal model: a tiny instalment can only buy one or two
    shares and gets the shape crudely wrong, while a large one lands almost exactly on the weights.
    """
    weights = {
        _EIGHT[0]: _D("0.40"),
        _EIGHT[1]: _D("0.30"),
        _EIGHT[2]: _D("0.15"),
        _EIGHT[3]: _D("0.10"),
        _EIGHT[4]: _D("0.05"),
    }
    prices = {isin: _D("100") for isin in weights}
    small = simulate_sip_instalment(instalment=_D("500"), targets=weights, prices=prices)
    large = simulate_sip_instalment(instalment=_D("5000000"), targets=weights, prices=prices)
    assert large.tracking_drift < small.tracking_drift


def test_existing_holdings_bias_the_instalment_toward_the_underweight() -> None:
    """With one name already heavily held, the instalment steers cash to the underweight names."""
    weights = _equal_weights(_EIGHT)
    prices = {isin: _D("100") for isin in _EIGHT}
    # _EIGHT[0] is already massively overweight; the instalment should not add to it.
    existing = {_EIGHT[0]: _D("100000")}
    result = simulate_sip_instalment(
        instalment=_D("10000"), targets=weights, prices=prices, existing_value=existing
    )
    bought = {o.isin: o.quantity for o in result.orders}
    assert bought.get(_EIGHT[0], 0) == 0, "an already-overweight name should not be topped up"
    # Every rupee it could spend on underweight names reduces drift, so little should be left over.
    assert result.deployed >= _D("9900")


# ── acceptance 3: determinism ────────────────────────────────────────────────────────────────────


def test_same_inputs_produce_identical_allocations() -> None:
    """Byte-for-byte identical allocation across repeated runs (§8.3.3 replay determinism)."""
    weights = _equal_weights(_EIGHT)
    prices = {
        _EIGHT[0]: _D("310"),
        _EIGHT[1]: _D("455"),
        _EIGHT[2]: _D("1270"),
        _EIGHT[3]: _D("640"),
        _EIGHT[4]: _D("205"),
        _EIGHT[5]: _D("880"),
        _EIGHT[6]: _D("1515"),
        _EIGHT[7]: _D("395"),
    }
    first = simulate_sip_instalment(instalment=_D("10000"), targets=weights, prices=prices)
    second = simulate_sip_instalment(instalment=_D("10000"), targets=weights, prices=prices)
    assert first == second
    assert first.orders == second.orders
    assert first.residual_cash == second.residual_cash
    assert first.drifts == second.drifts


def _run_sip_months(
    months: int, weights: dict[str, Decimal], prices: dict[str, Decimal]
) -> list[tuple[tuple[SipOrder, ...], Decimal]]:
    """Drive a running SIP: each month feeds the prior book back as existing_value, carrying cash.

    This is how the replay engine (M4.8) will use the allocator — the book grows month over month —
    and it is what lets an expensive name become reachable once the book is large enough to hold its
    model weight without overshooting. Returns each month's (orders, residual) for trace comparison.
    """
    book: dict[str, Decimal] = {}
    carried = _ZERO
    trace: list[tuple[tuple[SipOrder, ...], Decimal]] = []
    for _ in range(months):
        result = simulate_sip_instalment(
            instalment=_D("10000"),
            targets=weights,
            prices=prices,
            carried_in=carried,
            existing_value=book,
        )
        for order in result.orders:
            book[order.isin] = book.get(order.isin, _ZERO) + order.cost
        carried = result.residual_cash
        trace.append((result.orders, result.residual_cash))
    return trace


def test_accumulation_reaches_an_expensive_name_once_the_book_is_large_enough() -> None:
    """A name too dear for early instalments enters once the book can hold its model weight."""
    weights = _equal_weights(_EIGHT)  # 12.5% each
    prices = {isin: _D("300") for isin in _EIGHT}
    prices[_EIGHT[6]] = _D("3000")  # 10x the others: unbuyable early, reachable as the book grows

    trace = _run_sip_months(24, weights, prices)
    first_month = next(
        (m for m, (orders, _) in enumerate(trace) if any(o.isin == _EIGHT[6] for o in orders)),
        None,
    )
    assert first_month is not None, (
        "the expensive name should be bought once the book is large enough"
    )
    assert first_month > 0, "and not in month 0, when a single share would blow its 12.5% weight"

    # The whole accumulation is deterministic: an identical replay walks an identical path.
    assert _run_sip_months(24, weights, prices) == trace


# ── validation: the allocator fails loud, never silently ─────────────────────────────────────────


def test_weights_must_sum_to_one() -> None:
    prices = {isin: _D("100") for isin in _EIGHT[:2]}
    bad = {_EIGHT[0]: _D("0.3"), _EIGHT[1]: _D("0.3")}
    with pytest.raises(SipError, match="sum to 1"):
        simulate_sip_instalment(instalment=_D("10000"), targets=bad, prices=prices)


def test_missing_price_is_refused() -> None:
    weights = {_EIGHT[0]: _D("0.5"), _EIGHT[1]: _D("0.5")}
    prices = {_EIGHT[0]: _D("100")}  # _EIGHT[1] has no price
    with pytest.raises(SipError, match="no price"):
        simulate_sip_instalment(instalment=_D("10000"), targets=weights, prices=prices)


def test_money_must_be_decimal_not_float() -> None:
    weights = {_EIGHT[0]: _D("0.5"), _EIGHT[1]: _D("0.5")}
    prices = {_EIGHT[0]: _D("100"), _EIGHT[1]: _D("100")}
    with pytest.raises(SipError, match="Decimal"):
        simulate_sip_instalment(
            instalment=10000,  # type: ignore[arg-type]
            targets=weights,
            prices=prices,
        )


def test_non_positive_weight_is_refused() -> None:
    weights = {_EIGHT[0]: _D("1.0"), _EIGHT[1]: _D("0")}
    prices = {_EIGHT[0]: _D("100"), _EIGHT[1]: _D("100")}
    with pytest.raises(SipError, match="must be positive"):
        simulate_sip_instalment(instalment=_D("10000"), targets=weights, prices=prices)


def test_empty_targets_is_refused() -> None:
    with pytest.raises(SipError, match="empty"):
        simulate_sip_instalment(instalment=_D("10000"), targets={}, prices={})
