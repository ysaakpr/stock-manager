"""A7: the deployment queue — cash awaiting deployment, tagged by where it came from (§5.6).

§5.6 routes two kinds of cash into a queue before it is put to work: **exit proceeds** from a
`BROKEN` position wound down over the exit menu, and the **monthly SIP instalment**. Both land in
the queue, both are parked in the liquid ETF the same session (`manager.park`), and both leave the
queue only when the agent has somewhere ratified to put them — a ratified-thesis core replacement
or a tactical opportunity (`manager.deploy`).

This module is the queue itself: a small, immutable, first-in-first-out ledger of pending cash. It
is data and arithmetic, not decisions — the manager (`manager.py`) decides *when* cash is parked or
deployed and journals it; the queue only answers *how much is waiting, from which source, and since
when*. Keeping it separate means the manager's rail-and-journal logic and this bookkeeping can be
tested apart, the way A6 splits `engine.py` from `sleeves.py`.

Three properties this module guarantees, all proved in `tests/unit/test_cash.py`:

* **Every rupee is exact.** Amounts are `Decimal` (CLAUDE.md); a `float` is refused at construction,
  not coerced into a queue total that is nearly right.
* **Release is first-in-first-out and never overdraws.** `release` drains the oldest cash first and
  raises if asked for more than the queue holds — deploying cash that was never queued is a
  bookkeeping error, not a silent overdraft.
* **The queue is immutable.** `enqueue` and `release` return new queues; the original is unchanged,
  so a caller can compute a hypothetical deployment without disturbing the pending ledger.

Nothing here reads a clock, a database or the network. Arrival dates are supplied by the caller from
an injected `Clock` (B10), so a replayed queue carries the sessions its cash originally arrived on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

__all__ = [
    "CashSource",
    "DeploymentQueue",
    "InsufficientQueuedCashError",
    "QueueError",
    "QueuedCash",
]

_ZERO: Final = Decimal(0)


class QueueError(Exception):
    """Base for every deployment-queue refusal. The queue fails loud, never silently (CLAUDE.md)."""


class InsufficientQueuedCashError(QueueError):
    """A release asked for more cash than the queue holds — an overdraft of pending capital.

    Deployment draws from cash that was queued (exit proceeds or a SIP instalment); asking to
    release more than is queued would deploy capital the queue never accounted for, which is a
    bookkeeping error the manager wants surfaced, not absorbed into a negative queue.
    """


def _require_positive_money(name: str, value: object) -> Decimal:
    """Refuse a `float` or a non-positive amount; a queued rupee figure is exact and real.

    Typed `object` so the `float` guard runs against whatever the caller actually passed — a
    parameter already typed `Decimal` would make the check dead code to the type checker, the same
    reason `require_decimal` in the rails does it this way.
    """
    if isinstance(value, float):
        raise QueueError(
            f"{name} must be a Decimal, got float {value!r}; money is never float (CLAUDE.md)"
        )
    if not isinstance(value, Decimal):
        raise QueueError(f"{name} must be a Decimal, got {type(value).__name__}")
    if value <= _ZERO:
        raise QueueError(f"{name} must be positive, got {value}")
    return value


class CashSource(StrEnum):
    """Where a tranche of queued cash came from (§5.6). Recorded so the journal and the evidence
    pack can tell exit proceeds apart from fresh SIP money — the two are deployed under the same
    rules but answer different questions about a case's cash flow (§5.7).
    """

    SIP_INSTALMENT = "SIP_INSTALMENT"
    """The monthly systematic instalment (§5.2 policy 1), landing on the case's SIP day."""

    EXIT_PROCEEDS = "EXIT_PROCEEDS"
    """Cash raised winding down a position over the exit menu after a `BROKEN` verdict (§5.6)."""


@dataclass(frozen=True, slots=True)
class QueuedCash:
    """One tranche of cash waiting to be deployed: how much, from where, and since when.

    What it does: carry an exact rupee `amount`, the `source` that produced it, and the `arrived`
    trading date it landed — the last so `manager` can measure a tranche against the ratified
    `deploy_within_sessions` deadline (§5.2 policy 6) without re-deriving when it appeared.
    What it never does: hold a float amount, a zero or negative one, or key on a symbol — a tranche
    is money, not an instrument, so it carries no ISIN at all.
    """

    source: CashSource
    amount: Decimal
    arrived: date

    def __post_init__(self) -> None:
        _require_positive_money("amount", self.amount)

    def _with_amount(self, amount: Decimal) -> QueuedCash:
        """The same tranche with a reduced amount — used by a partial FIFO release."""
        return QueuedCash(source=self.source, amount=amount, arrived=self.arrived)


@dataclass(frozen=True, slots=True)
class DeploymentQueue:
    """The first-in-first-out ledger of cash awaiting deployment (§5.6). Immutable.

    What it does: hold the pending tranches in arrival order and answer the questions the manager
    asks — the total waiting, the total per source, the oldest tranche (for the deploy deadline),
    and the two mutations that return new queues: `enqueue` a fresh tranche, `release` cash on a
    deployment.
    What it assumes: tranches are appended in the order they arrived, so the head of `items` is the
    oldest — the manager builds them from an injected `Clock`, so the order is the real one.
    What it never does: mutate in place, or release more than it holds. A release that would
    overdraw raises rather than producing a negative queue (invariant-adjacent: cash the queue never
    saw is not the queue's to deploy).
    """

    items: tuple[QueuedCash, ...] = ()

    @property
    def total(self) -> Decimal:
        """Total rupees awaiting deployment across every tranche."""
        return sum((item.amount for item in self.items), _ZERO)

    @property
    def is_empty(self) -> bool:
        """True when no cash is waiting — the queue is drained."""
        return not self.items

    @property
    def oldest(self) -> QueuedCash | None:
        """The tranche that has waited longest, or None when the queue is empty.

        The deploy-within-sessions deadline (§5.2 policy 6) bites on the oldest tranche first, so
        this is what the manager measures against that limit; FIFO release keeps it the head.
        """
        return self.items[0] if self.items else None

    def amount_from(self, source: CashSource) -> Decimal:
        """Total queued cash that came from `source` — the split the journal records (§5.7)."""
        return sum((item.amount for item in self.items if item.source is source), _ZERO)

    def per_source(self) -> Mapping[CashSource, Decimal]:
        """Every source that has cash waiting, mapped to its total — for a parking rationale."""
        totals: dict[CashSource, Decimal] = {}
        for item in self.items:
            totals[item.source] = totals.get(item.source, _ZERO) + item.amount
        return totals

    def enqueue(self, source: CashSource, amount: Decimal, arrived: date) -> DeploymentQueue:
        """Return a new queue with one more tranche appended at the tail (§5.6).

        What it does: validate the amount and add the tranche after the ones already waiting, so the
        head stays the oldest and FIFO release drains it first.
        What it never does: merge tranches or reorder them — two SIP instalments a month apart are
        two tranches with two arrival dates, and collapsing them would lose the deadline of the
        earlier one.
        """
        return DeploymentQueue(items=(*self.items, QueuedCash(source, amount, arrived)))

    def release(self, amount: Decimal) -> tuple[DeploymentQueue, Decimal]:
        """Draw `amount` off the front of the queue, oldest tranche first (§5.6).

        What it does: consume whole tranches from the head until `amount` is met, splitting the
        tranche that straddles the boundary so its remainder stays queued with its original arrival
        date. Returns the drained queue and the amount released (always exactly `amount`).
        What it assumes: `amount` is positive and no larger than `total` — a deployment deploys cash
        that was queued.
        What it never does: overdraw. Asking for more than the queue holds raises
        `InsufficientQueuedCashError` rather than releasing a negative remainder.
        """
        wanted = _require_positive_money("release amount", amount)
        if wanted > self.total:
            raise InsufficientQueuedCashError(
                f"cannot release {wanted}: the deployment queue holds only {self.total}. "
                "Deployment draws from queued cash (exit proceeds or a SIP instalment), so a "
                "release larger than the queue is capital that was never earmarked"
            )
        remaining = wanted
        kept: list[QueuedCash] = []
        for index, item in enumerate(self.items):
            if remaining <= _ZERO:
                kept.extend(self.items[index:])
                break
            if item.amount <= remaining:
                remaining -= item.amount  # consume the whole tranche
            else:
                kept.append(item._with_amount(item.amount - remaining))  # split it
                remaining = _ZERO
        return DeploymentQueue(items=tuple(kept)), wanted
