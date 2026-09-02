"""A5 · the data-red interlock — the gate every daily decision passes before it is made.

Invariant #10, §4.4: *bad data never becomes decisions*. Before the analyst does anything on a
trading date it reads `/status/sync`; unless the date's core datasets are `PUBLISHED` and no
ERROR-severity quality flag is open, it records `SKIPPED_DATA_RED` and does not trade. This module
is that read, in one place, so the rule is not re-implemented per caller.

The interlock is expressed as a `GreenGate` — a callable `(trading_date) -> GreenStatus` — for two
reasons. It keeps the decision path (`T0Monitor`) free of a database dependency: the monitor asks
the gate, it does not open a connection. And it makes the interlock trivially substitutable — the
production wiring (`StatusApiGate`) is a thin adapter over `dataplatform.status.is_green` with the
ratified core-dataset list, while a test injects a fake that returns red or green on demand and so
proves the short-circuit without a Postgres.

`is_green` deliberately does not swallow a connection error (a status check that reported green
when it could not reach the store would be the worst failure mode), so `StatusApiGate` does not
either: the daily loop must see the exception and journal `SKIPPED_DATA_RED`, not trade blind.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final, Protocol, runtime_checkable

from dataplatform.clock import Clock
from dataplatform.status import GreenStatus, is_green

#: The datasets a trading decision depends on — the interlock requires every one `PUBLISHED` and
#: quality-green before the day is safe to act on. The daily loop passes the case's own list where
#: it differs; this is the platform default (§4.2's EOD price/CA core), named once so "core
#: dataset" is not re-guessed per caller.
CORE_DATASETS: Final[tuple[str, ...]] = (
    "nse_eod",
    "bse_eod",
    "nse_corporate_actions",
)


@runtime_checkable
class GreenLike(Protocol):
    """The slice of a green verdict the interlock's callers use: its truthiness and its reason.

    `GreenStatus` satisfies it (it is truthy when green and carries a `reason` line otherwise), and
    so does any stand-in a test wants to inject — the decision path only ever asks these two things.
    """

    reason: str

    def __bool__(self) -> bool: ...


@runtime_checkable
class GreenGate(Protocol):
    """The interlock as a decision path consumes it: is this trading date safe to act on?

    Returns something `GreenLike` — truthy when the date's core datasets are `PUBLISHED` and no
    ERROR-severity quality flag is open, with a `reason` when not. Injected rather than called
    directly so the caller has no database dependency and the check is faked in a test.
    """

    def __call__(self, trading_date: date) -> GreenLike:
        """The interlock answer for `trading_date`. Truthy means green; `.reason` says why not."""


@dataclass(frozen=True, slots=True)
class StatusApiGate:
    """The production `GreenGate`: `dataplatform.status.is_green` over the core dataset list (§4.4).

    What it does: on each call, open a status connection, ask whether every core dataset for the
    date is `PUBLISHED` and quality-green, and return the `GreenStatus`. That is the whole of the
    interlock's read side.
    What it assumes: the status database is reachable. It does *not* catch a failure to reach it —
    reporting green when the store is unreachable is the one failure mode invariant #10 cannot
    tolerate, so the exception propagates and the daily loop journals `SKIPPED_DATA_RED`.
    What it never does: write anything, or decide what to do about a red day — that is the caller's
    (`T0Monitor` records the skip; the daily loop moves on).
    """

    datasets: Sequence[str] = CORE_DATASETS
    clock: Clock | None = None

    def __call__(self, trading_date: date) -> GreenStatus:
        return is_green(trading_date, self.datasets, clock=self.clock)


__all__ = [
    "CORE_DATASETS",
    "GreenGate",
    "GreenLike",
    "StatusApiGate",
]
