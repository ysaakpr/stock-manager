"""X1: daily reconciliation — the job that proves the broker and the internal book still agree.

EXECUTION_PLAN §6, M5.12 spec: "a daily reconciliation job comparing broker positions/ledger against
the internal book; ANY mismatch freezes trading and alerts." This module is that job. It is the last
line between a quiet divergence and trading on a book that no longer describes reality — a filled
order the platform missed, a corporate action the broker applied and we did not, a manual trade at
the broker, a fill we recorded that never happened.

The contract is deliberately unforgiving: reconciliation is not a report with a tolerance, it is a
tripwire. Any break at all — a share count that differs by one, a cash balance off by a rupee —
freezes trading (trips the kill switch, source `RECON`) and raises an alert. There is no "small
enough to ignore": the whole value of the check is that it does not negotiate, because the failure
mode it guards against (acting on a wrong book) is unbounded and the cost of a false freeze is a
human glancing at an alert.

What it compares:

* **Positions** — the believed share count per ISIN (`InternalBook.quantities`) against the broker's
  settled holdings *and* unsettled positions, aggregated by ISIN. Identity is the ISIN and only the
  ISIN (invariant #2); a holding the book knows nothing about, or one the book has and the broker
  does not, is a break as much as a quantity difference is.
* **Cash** — the believed cash (`InternalBook.cash`) against the broker's available cash. Costs are
  computed on both sides from the one shared cost model (invariant #4), so an exact match is the
  expectation, not an approximate one.

What it never does: repair a break. Reconciliation *detects* and *halts*; deciding what the correct
state is and re-arming the switch is a human act (`KillSwitch.reset`). A recon job that silently
"corrected" the book to match the broker would erase the very evidence of the divergence.

Time is an injected `Clock` (B10). The alerter is a seam so the alert can go to a log in a test and
to a real channel in production without this module knowing the difference.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

import structlog

from dataplatform.clock import Clock, SystemClock
from execution.broker import Broker
from execution.kill_switch import KillSwitch, TripSource
from execution.staging import InternalBook

_LOG = structlog.get_logger(__name__)

__all__ = [
    "Alert",
    "Alerter",
    "BreakKind",
    "LoggingAlerter",
    "ReconBreak",
    "ReconResult",
    "Reconciler",
    "RecordingAlerter",
]


# ── the alerting seam ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Alert:
    """One reconciliation alert — the session, why it fired, and the breaks that fired it.

    Frozen and self-contained so it can be logged, recorded in a test, or (in production) formatted
    into a message without the alerter reaching back into the reconciler for context.
    """

    session: date
    raised_at: datetime
    summary: str
    breaks: tuple[ReconBreak, ...]


class Alerter(Protocol):
    """Where a reconciliation alert goes. A protocol so the destination is injectable.

    Production wires an alerter that reaches a human channel; tests wire `RecordingAlerter` and
    assert on what was sent. `send` must not raise on a delivery problem in a way that stops the
    freeze — the freeze has already happened by the time the alert is sent, and an alerter that
    threw would turn "we froze and could not alert" into "we did not finish reconciling".
    """

    def send(self, alert: Alert) -> None:
        """Deliver one alert. Called only when reconciliation found at least one break."""


class RecordingAlerter:
    """An `Alerter` that keeps every alert in a list — the test's assertion surface."""

    __slots__ = ("alerts",)

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


class LoggingAlerter:
    """An `Alerter` that emits a structured log line — the boring production default.

    A real deployment replaces or wraps this with something that reaches a human; a log line is the
    floor, so a freeze is never completely silent even before a channel is wired.
    """

    __slots__ = ()

    def send(self, alert: Alert) -> None:
        _LOG.error(
            "recon.alert",
            session=alert.session.isoformat(),
            summary=alert.summary,
            breaks=[b.describe() for b in alert.breaks],
        )


# ── the break and the result ───────────────────────────────────────────────────────────────────


class BreakKind(StrEnum):
    """What kind of disagreement a break records."""

    POSITION = "POSITION"
    """A share count for an ISIN differs between the book and the broker (or exists on only one)."""

    CASH = "CASH"
    """The believed cash differs from the broker's available cash."""


@dataclass(frozen=True, slots=True)
class ReconBreak:
    """One disagreement between the internal book and the broker.

    `isin` is set for a position break and None for a cash break. `expected` is the book's value,
    `actual` is the broker's — the naming is from the book's point of view, because the book is what
    the platform *believed* and the broker is the ground truth it is checked against.
    """

    kind: BreakKind
    expected: Decimal
    actual: Decimal
    isin: str | None = None

    @property
    def difference(self) -> Decimal:
        """Broker minus book — positive when the broker has more than the platform believed."""
        return self.actual - self.expected

    def describe(self) -> str:
        """A one-line human description, for an alert or a log."""
        where = "cash" if self.isin is None else self.isin
        return (
            f"{self.kind.value} {where}: book={self.expected} broker={self.actual} "
            f"(diff {self.difference})"
        )


@dataclass(frozen=True, slots=True)
class ReconResult:
    """The outcome of one reconciliation: the breaks found, and whether it froze trading.

    `ok` is true only when there were no breaks at all. `froze` records whether this run tripped the
    kill switch — true whenever there was any break, because a break always freezes (that is the
    whole contract), captured explicitly so a caller can assert on it without re-deriving it.
    """

    session: date
    breaks: tuple[ReconBreak, ...]
    froze: bool

    @property
    def ok(self) -> bool:
        """True when the book and the broker agreed on everything."""
        return not self.breaks


# ── the reconciler ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Reconciler:
    """Compares the internal book to the broker each session; freezes and alerts on any break.

    Construct it with the `Broker` (a protocol — never a concrete broker, invariant #5), the
    `InternalBook` the staging layer maintains, the shared `KillSwitch`, and an `Alerter`. Call
    `reconcile(session)` after the session's fills are booked. It builds both sides independently,
    diffs them, and if anything disagrees it trips the switch (source `RECON`) and sends one alert
    carrying every break — then returns the `ReconResult` for the caller to record.

    What it never does: negotiate a tolerance, or fix a break. A difference of any size freezes; the
    correction is a human's, after they read the alert.
    """

    broker: Broker
    book: InternalBook
    kill_switch: KillSwitch
    alerter: Alerter
    clock: Clock = field(default_factory=SystemClock)

    def reconcile(self, session: date) -> ReconResult:
        """Reconcile the book against the broker for `session`; freeze and alert on any break."""
        breaks = (*self._position_breaks(), *self._cash_break())
        if not breaks:
            _LOG.info("recon.clean", session=session.isoformat())
            return ReconResult(session=session, breaks=(), froze=False)

        # ANY mismatch freezes — the switch is tripped before the alert is sent, so trading is
        # halted the instant the break is known, not only once someone reads the message.
        summary = f"{len(breaks)} reconciliation break(s) on {session.isoformat()}"
        self.kill_switch.trip(reason=summary, source=TripSource.RECON)
        alert = Alert(
            session=session,
            raised_at=self.clock.now(),
            summary=summary,
            breaks=breaks,
        )
        self.alerter.send(alert)
        _LOG.error(
            "recon.break",
            session=session.isoformat(),
            count=len(breaks),
            breaks=[b.describe() for b in breaks],
        )
        return ReconResult(session=session, breaks=breaks, froze=True)

    def _position_breaks(self) -> tuple[ReconBreak, ...]:
        """Every ISIN where the book and the broker disagree on the share count.

        The broker's holdings and unsettled positions are summed per ISIN: the book's belief is a
        single quantity per scrip, and reconciling it against only settled holdings would flag a
        same-session buy that has not settled yet as a phantom break. The union of ISINs is walked
        so a holding on exactly one side is caught, not just a quantity that differs on both.
        """
        book = self.book.quantities()
        broker: dict[str, int] = defaultdict(int)
        for holding in self.broker.holdings():
            broker[holding.isin] += holding.quantity
        for position in self.broker.positions():
            broker[position.isin] += position.quantity

        breaks: list[ReconBreak] = []
        for isin in sorted(set(book) | set(broker)):
            expected = book.get(isin, 0)
            actual = broker.get(isin, 0)
            if expected != actual:
                breaks.append(
                    ReconBreak(
                        kind=BreakKind.POSITION,
                        isin=isin,
                        expected=Decimal(expected),
                        actual=Decimal(actual),
                    )
                )
        return tuple(breaks)

    def _cash_break(self) -> tuple[ReconBreak, ...]:
        """A break if the believed cash differs from the broker's available cash, else nothing."""
        expected = self.book.cash
        actual = self.broker.margins().available
        if expected == actual:
            return ()
        return (ReconBreak(kind=BreakKind.CASH, expected=expected, actual=actual),)
