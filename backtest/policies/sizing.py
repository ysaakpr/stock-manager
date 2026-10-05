"""Buy sizing every SIP-allocating policy shares: the per-order ceiling A8 will clear (X2).

A policy that sizes a buy as ``free cash / names`` with no regard for the per-order cap proposes,
once the book is large enough, buys A8 is bound to refuse. The refused cash stays idle, the next
rebalance spreads the larger idle balance over the same names, every buy grows further past the
cap, and the book drifts to all cash (PR #31: swing composite, 1,638 ``MAX_ORDER_VALUE`` blocks).
:func:`account_order_ceiling` is the one place a policy reads the ceiling for its own account; it
is handed to :func:`backtest.sip.simulate_sip_instalment` as ``order_ceiling``, so the shortfall
stays in cash and is topped up at the next rebalance instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from analyst.cases import RiskRails
from analyst.rails import order_value_ceiling
from execution.broker import Broker, Holding, Position

__all__ = ["BUY_SIZING_IDENTITY", "account_order_ceiling"]

#: What a run spec records for a policy whose buys are sized to :func:`account_order_ceiling`.
#: Its own key, so a run sized this way never shares a digest with one persisted before it was.
BUY_SIZING_IDENTITY = "order_value_ceiling/v1"


def account_order_ceiling(
    order_caps: RiskRails | None, broker: Broker, marks: Mapping[str, Decimal]
) -> Decimal | None:
    """A8's per-order ceiling for this account, or None when no rails were given.

    What it does: values the case as the rail book values it — cash including unsettled proceeds,
    plus every settled and pending lot at ``marks`` (the broker's cost basis for an unmarked lot) —
    and returns :func:`analyst.rails.order_value_ceiling` for that value.
    What it assumes: ``order_caps`` are the rails the gate will clear this policy's orders against.
    What it never does: split an order or loosen a cap. A buy sized to the ceiling is still one
    order, cleared whole by every rail; a buy over it is still refused.
    """
    if order_caps is None:
        return None
    value = broker.margins().cash_value
    lots: list[Holding | Position] = [*broker.holdings(), *broker.positions()]
    for lot in lots:
        value += marks.get(lot.isin, lot.average_price) * lot.quantity
    return order_value_ceiling(order_caps, value)
