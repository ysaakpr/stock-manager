"""A8: risk rails — deterministic pre-trade checks the agent cannot override.

Every order from A5/A6/A7 passes through `check_order` before it reaches the broker (invariant #6),
and the daily loop runs `assess_drawdown` to trip the forced review of §6. The checks are pure
functions of the order, the book and the ratified `analyst.cases.RiskRails`; no LLM is anywhere
near them and none of them takes an override — a rail with a bypass is not a rail.

`RailEngine` wires the pure checks to the decision journal (§0): a blocked order writes a
`RAIL_BLOCK` line naming every breached rail, and a breached drawdown writes a forced-review
`ESCALATE` line by the `RAILS` actor. A8 decides whether an order may be placed and records that
decision; X1 does the placing. The rail engine has no broker on purpose — a second path to the
market is the one thing invariant #6 forbids.
"""

from analyst.rails.engine import (
    FORCED_REVIEW_EVENT,
    RailEngine,
    apply_order,
    assess_drawdown,
    check_order,
)
from analyst.rails.policies import (
    DrawdownStatus,
    HouseholdExposure,
    Lot,
    Portfolio,
    ProposedOrder,
    RailAssessment,
    RailBreach,
    RailId,
    drawdown_of,
)

__all__ = [
    "FORCED_REVIEW_EVENT",
    "DrawdownStatus",
    "HouseholdExposure",
    "Lot",
    "Portfolio",
    "ProposedOrder",
    "RailAssessment",
    "RailBreach",
    "RailEngine",
    "RailId",
    "apply_order",
    "assess_drawdown",
    "check_order",
    "drawdown_of",
]
