"""X1 · the broker-session auth interlock's read side, and where its alert goes.

EXECUTION_PLAN §4.4's interlock refuses to trade on a day the *data* is not green. M5.15 adds the
counterpart for a day the *broker session* is not authenticated: Indian brokers force a daily API
logout that only an interactive OAuth + 2FA login re-opens (NSE consolidated NNF circular
INVG/73992 §8.3.2.1.8), so the daily loop can wake at market open holding a dead session. Acting on
a lapsed session is the same failure class as acting on red data — an invalid precondition — and
until M5.15 it was uncovered.

This module owns the *execution* half of that interlock: reading whether the session is live, and
delivering the "please re-authenticate" alert. The *decision* half — journalling `AUTH_REQUIRED`,
deferring the day's staged decisions, and firing the alert once per streak — lives in
`analyst.monitor.interlock`, the same place the data-red skip is recorded, and consumes what is
defined here. The split follows the layering the rest of the system uses: `execution/` knows the
broker and never imports `analyst/`; `analyst/` wires the two together.

`BrokerSessionGate` is the exact analogue of `analyst.monitor.interlock.StatusApiGate`: a callable
`(trading_date) -> SessionStatus` over a `Broker`, structurally satisfying the decision path's
`AuthGate`. It deliberately does *not* swallow a `SessionExpired` into an exception the loop never
sees — it turns it into an *invalid* status, because a session check that reported "valid" when it
could not confirm the session is the one failure mode this interlock cannot tolerate, exactly as
`is_green` will not report green when the status store is unreachable.

Time is an injected `Clock` (B10); nothing here reads a wall clock. There is no order path in this
module at all — an interlock that could place an order would defeat its own purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

import structlog

from dataplatform.clock import Clock
from execution.broker import Broker, SessionExpired

_LOG = structlog.get_logger(__name__)

__all__ = [
    "REAUTH_INSTRUCTION",
    "AuthAlert",
    "AuthAlerter",
    "BrokerSessionGate",
    "LoggingAuthAlerter",
    "RecordingAuthAlerter",
    "SessionStatus",
]

#: The one line the interlock puts in front of the owner on an AUTH_REQUIRED day — what to do and
#: what the loop is doing until they do it. Kept here, next to the read side, so the journal entry,
#: the alert and the runbook (ops/runbooks/broker-reauth.md) all quote one source rather than three
#: drifting copies. The runbook is the long form; this is the actionable summary.
REAUTH_INSTRUCTION: str = (
    "Broker API session expired (daily OAuth+2FA logout). Re-authenticate via the broker login "
    "flow to restore the session; until then the daily loop places no orders and defers each "
    "day's staged decisions to the next authenticated session (no decision is dropped). "
    "See ops/runbooks/broker-reauth.md."
)


# ── the session verdict ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionStatus:
    """Whether the broker session is usable today, and — when it is not — why.

    Deliberately the same `GreenLike` shape the data-red interlock speaks in (truthy when good,
    with a `reason` line when not), so the daily loop treats the auth check and the data check
    uniformly: `if not gate(date): skip`. `valid` is the fact; `reason` is empty when valid and a
    human-readable cause when not.
    """

    valid: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.valid


# ── the read side: a Broker as an auth gate ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BrokerSessionGate:
    """The production auth gate: `Broker.session_valid()` rendered as a `SessionStatus` (§4.4).

    What it does: on each call, ask the injected broker whether its API session is authenticated,
    and return a `SessionStatus`. A `False` becomes an invalid status; a `SessionExpired` raised by
    the broker becomes an invalid status carrying the exception's message — the two are the same
    fact reported two ways, and the interlock must not care which.
    What it assumes: nothing about *which* broker — `SimBroker` (always valid) and `KiteBroker`
    (checks the real token at M8) satisfy the identical seam (invariant #5).
    What it never does: place, modify or read an order; decide what to do about a dead session
    (that is the decision layer's `AuthInterlock`); or turn an expired session into an exception
    the loop never sees — the whole point is that dead auth reaches the journal, not a stack trace.
    """

    broker: Broker
    clock: Clock | None = None

    def __call__(self, trading_date: date) -> SessionStatus:
        try:
            valid = self.broker.session_valid()
        except SessionExpired as expired:
            return SessionStatus(
                valid=False,
                reason=str(expired) or "broker session expired",
            )
        if valid:
            return SessionStatus(valid=True)
        return SessionStatus(
            valid=False,
            reason="broker reports its API session is not authenticated",
        )


# ── where the alert goes ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AuthAlert:
    """One re-authenticate alert — the day it fired, when, why, and the fix.

    Frozen and self-contained so it can be logged, recorded in a test, or (in production) formatted
    into a message without the sender reaching back for context. `streak_started` is the first day
    of the current dead-session streak: an alert fires once per streak (M5.15 acceptance 4), and
    this is the day the streak — and the outage the owner must act on — began.
    """

    trading_date: date
    raised_at: datetime
    reason: str
    instruction: str
    streak_started: date


@runtime_checkable
class AuthAlerter(Protocol):
    """Where a re-authenticate alert goes. A protocol so the destination is injectable.

    Production wires an alerter that reaches a human channel; tests wire `RecordingAuthAlerter` and
    assert on what was sent. `send` must not raise on a delivery problem in a way that stops the
    interlock — the day has already been journalled and the orders already withheld by the time the
    alert is sent, and an alerter that threw would turn "we withheld orders and could not alert"
    into "we did not finish the interlock".
    """

    def send(self, alert: AuthAlert) -> None:
        """Deliver one alert. Called once per dead-session streak, not once per check."""


class RecordingAuthAlerter:
    """An `AuthAlerter` that keeps every alert in a list — the test's assertion surface."""

    __slots__ = ("alerts",)

    def __init__(self) -> None:
        self.alerts: list[AuthAlert] = []

    def send(self, alert: AuthAlert) -> None:
        self.alerts.append(alert)


class LoggingAuthAlerter:
    """An `AuthAlerter` that emits a structured log line — the boring production default.

    A real deployment replaces or wraps this with something that reaches a human; a log line is the
    floor, so a dead session is never completely silent even before a channel is wired.
    """

    __slots__ = ()

    def send(self, alert: AuthAlert) -> None:
        _LOG.error(
            "broker.auth_required",
            trading_date=alert.trading_date.isoformat(),
            reason=alert.reason,
            streak_started=alert.streak_started.isoformat(),
            instruction=alert.instruction,
        )
