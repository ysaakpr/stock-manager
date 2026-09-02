"""A7: cash manager — idle-cash parking and the deployment queue (§5.6).

§5.6 gives a case's cash two states and two transitions between them, and this package is those
transitions. Exit proceeds and the monthly SIP instalment land in a **deployment queue**
(`queue.py`), are **parked** in the liquid ETF the same session they arrive (`manager.park`,
decision #10), and are **deployed** into a real position only against a ratified-thesis core
replacement or a tactical opportunity (`manager.deploy`, §5.5). Both transitions clear A8
(invariant #6) and are journaled decisions with a rationale (invariant #9).

Two properties this package exists to guarantee, both proved in `tests/unit/test_cash.py`:

* **Idle cash is parked same-session, in whole shares.** `park` turns queued rupees into the
  largest whole-share ETF buy that fits (the India constraint applies to the ETF too) and journals
  it; the remainder carries forward.
* **Cash deploys only when §5.6 permits.** `deploy` refuses to move cash into a position without a
  `BuyAuthorization` (from A4) proving a ratified thesis (core) or a journaled tactical rationale —
  the precondition is enforced by the type of proof `deploy` demands, not by discipline.
"""

from analyst.cash.manager import (
    PARKING_SECTOR,
    BelowMinimumDeploymentError,
    CashError,
    CashManager,
    DeploymentDecision,
    ParkingDecision,
    UndeployableError,
    UnratifiedCashPolicyError,
)
from analyst.cash.queue import (
    CashSource,
    DeploymentQueue,
    InsufficientQueuedCashError,
    QueuedCash,
    QueueError,
)

__all__ = [
    "PARKING_SECTOR",
    "BelowMinimumDeploymentError",
    "CashError",
    "CashManager",
    "CashSource",
    "DeploymentDecision",
    "DeploymentQueue",
    "InsufficientQueuedCashError",
    "ParkingDecision",
    "QueueError",
    "QueuedCash",
    "UndeployableError",
    "UnratifiedCashPolicyError",
]
