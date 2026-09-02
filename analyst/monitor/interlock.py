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
from dataclasses import dataclass, field
from datetime import date
from typing import Final, Protocol, runtime_checkable

from analyst.journal import Actor, Decision, Journal, JournalEntry, Sleeve
from dataplatform.clock import Clock, SystemClock
from dataplatform.status import GreenStatus, is_green
from execution.broker import OrderRequest
from execution.session import REAUTH_INSTRUCTION, AuthAlert, AuthAlerter

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
    `reason` is a read-only property, not a settable variable, so a *frozen* verdict — `GreenStatus`
    and `execution.session.SessionStatus` both are — structurally satisfies it.
    """

    @property
    def reason(self) -> str: ...

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


# ── the broker-session (auth) interlock ────────────────────────────────────────────────────────
#
# §4.4's interlock refuses to trade on a day the *data* is not green; this is its counterpart for a
# day the *broker session* is not authenticated (M5.15). Indian brokers force a daily API logout
# that only an interactive OAuth+2FA login reopens (NSE consolidated NNF circular INVG/73992
# §8.3.2.1.8), so the daily loop can hold a dead session at market open. Acting on it is the same
# failure class as acting on red data — an invalid precondition — so it is recorded the same way,
# here, next to `SKIPPED_DATA_RED`. The read side (`BrokerSessionGate`) and the alert seam live in
# `execution.session`, the layer that knows the broker; this is the decision side that journals the
# skip, defers the day's decisions, and dedupes the alert to once per streak.


@runtime_checkable
class AuthGate(Protocol):
    """The auth interlock as the decision path consumes it: is the broker session usable today?

    The exact analogue of `GreenGate` for dead auth. Returns something `GreenLike` — truthy when
    the broker's API session is authenticated, with a `reason` when it is not. Injected rather than
    called directly so the decision layer has no dependency on a concrete broker, and so a test can
    force a valid or invalid session without one. `execution.session.BrokerSessionGate` is the
    production implementation over a real `Broker`.
    """

    def __call__(self, trading_date: date) -> GreenLike:
        """The auth answer for `trading_date`. Truthy means live; `.reason` says why not."""


@dataclass(frozen=True, slots=True)
class PendingDecision:
    """A decision that was about to be staged when the auth interlock fired.

    Carries exactly what staging (`execution.staging.StagingCoordinator.stage`) would need to place
    it later — the `OrderRequest`, its case and its sleeve — so an `AUTH_REQUIRED` day can *defer*
    the decision (carry it to the next authenticated session) instead of dropping it, and so the
    `DEFERRED` journal entry can name the instrument the decision concerned (invariant #2). A
    decision that evaporates because of an auth failure is a journal lie; this is what stops it.
    """

    request: OrderRequest
    case_id: str
    sleeve: Sleeve


@dataclass(frozen=True, slots=True)
class AuthInterlockResult:
    """What the auth interlock did on one trading day — the record its caller acts on.

    `session_valid` truthy means the day was clear and the caller proceeds to stage normally;
    nothing was journalled and `deferred` is empty. Otherwise no order was placed, `deferred` holds
    the decisions carried to the next authenticated session, `journal_entry_ids` are the
    `AUTH_REQUIRED` entry followed by one `DEFERRED` entry per deferred decision, and `alerted` says
    whether *this* day's check is the one that fired the once-per-streak alert.
    """

    trading_date: date
    session_valid: bool
    reason: str
    deferred: tuple[PendingDecision, ...]
    journal_entry_ids: tuple[int, ...]
    alerted: bool


@dataclass(slots=True)
class AuthInterlock:
    """The broker-session interlock the daily loop runs alongside the data-red one (M5.15).

    What it does: on `guard()`, ask the `AuthGate` whether the broker session is live. If it is,
    return immediately and let the caller stage. If it is not, journal an `AUTH_REQUIRED` entry
    (SYSTEM, no order, the same shape as `SKIPPED_DATA_RED`), journal one `DEFERRED` entry per
    staged decision so none is silently dropped, and fire the re-authenticate alert exactly once
    per streak of dead-session days — not once per check (acceptance 4).
    What it assumes: the caller owns the transaction (the `Journal` never commits), `ts` comes from
    the injected `Clock` (B10), and the same instance is reused across the days of a run so it can
    tell the first dead day of a streak from the rest. It holds no broker and no order path, so it
    *cannot* place an order on a dead day — the interlock is structural, not disciplinary.
    What it never does: decide *whether* a decision was worth making (that is upstream), or drop a
    deferred decision — it hands them all back for the caller to re-stage next valid session.
    """

    gate: AuthGate
    journal: Journal
    alerter: AuthAlerter
    clock: Clock = field(default_factory=SystemClock)
    instruction: str = REAUTH_INSTRUCTION
    _streak_started: date | None = field(default=None, init=False)

    def guard(
        self, trading_date: date, pending: Sequence[PendingDecision] = ()
    ) -> AuthInterlockResult:
        """Run the auth check for `trading_date`; on a dead session, defer `pending` and alert once.

        `pending` is the day's staged decisions — the orders the loop was about to place. On a live
        session they are the caller's to stage as usual; on a dead one they are journalled as
        `DEFERRED` and returned in the result, carried to the next authenticated session rather than
        executed now (acceptance 3). No order is placed on a dead day: this method never touches a
        broker.
        """
        status = self.gate(trading_date)
        if status:
            # A clear day ends any streak, so the next dead day alerts afresh.
            self._streak_started = None
            return AuthInterlockResult(
                trading_date=trading_date,
                session_valid=True,
                reason="",
                deferred=(),
                journal_entry_ids=(),
                alerted=False,
            )

        first_of_streak = self._streak_started is None
        if first_of_streak:
            self._streak_started = trading_date
        streak_started = self._streak_started
        assert streak_started is not None  # set immediately above; for the type checker

        now = self.clock.now()
        ids: list[int] = []
        auth_entry = self.journal.append(
            JournalEntry(
                ts=now,
                trading_date=trading_date,
                case_id=None,
                actor=Actor.SYSTEM,
                decision=Decision.AUTH_REQUIRED,
                rationale=status.reason,
                payload={
                    "reauth": self.instruction,
                    "deferred_count": str(len(pending)),
                    "streak_started": streak_started.isoformat(),
                },
            )
        )
        ids.append(auth_entry.id)

        for decision in pending:
            deferred_entry = self.journal.append(
                JournalEntry(
                    ts=now,
                    trading_date=trading_date,
                    case_id=decision.case_id,
                    actor=Actor.EXEC,
                    decision=Decision.DEFERRED,
                    isin=decision.request.isin,
                    sleeve=decision.sleeve,
                    rationale=(
                        "broker session not authenticated; decision carried to the next "
                        "authenticated session rather than placed today"
                    ),
                    payload={
                        "side": decision.request.side.value,
                        "quantity": str(decision.request.quantity),
                        "exchange": decision.request.exchange.value,
                        "order_type": decision.request.order_type.value,
                    },
                )
            )
            ids.append(deferred_entry.id)

        alerted = False
        if first_of_streak:
            self.alerter.send(
                AuthAlert(
                    trading_date=trading_date,
                    raised_at=now,
                    reason=status.reason,
                    instruction=self.instruction,
                    streak_started=streak_started,
                )
            )
            alerted = True

        return AuthInterlockResult(
            trading_date=trading_date,
            session_valid=False,
            reason=status.reason,
            deferred=tuple(pending),
            journal_entry_ids=tuple(ids),
            alerted=alerted,
        )


__all__ = [
    "CORE_DATASETS",
    "AuthGate",
    "AuthInterlock",
    "AuthInterlockResult",
    "GreenGate",
    "GreenLike",
    "PendingDecision",
    "StatusApiGate",
]
