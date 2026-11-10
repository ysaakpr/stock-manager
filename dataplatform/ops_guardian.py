"""The ops guardian's bridge into the platform (OPS-GUARD): its alerts page, its silence shows.

The ops guardian is a box-local systemd user service (`ops-guardian.service`, outside this repo)
that watches what the platform cannot watch from inside itself: the status API and scheduler
being up, scheduled jobs that are OVERDUE / FAILING / NEVER_RAN past a fire, the backtest
campaign logs, the nightly M17 result, the disk, and a durable agenda of planned work that
replaces session-only crons. Every tick it rewrites one JSON file — its heartbeat and its open
alerts — and that file is the whole interface. This module is the platform's side of it:

* the **`ops_guardian` scheduler job** reads the file every five minutes. A fresh heartbeat makes
  the job SUCCEEDED; a missing, unreadable or stale one (older than
  `ops_guardian_stale_after_minutes`) makes it FAILED, so the Ops page shows the guardian as its
  own row in error and `failure_alerts` pages it. That is half of the mutual watch; the guardian
  watching the scheduler heartbeat is the other half.
* with a fresh heartbeat, the job **reconciles the guardian's open alerts** into
  `alert_condition` under `Trigger.OPS_GUARDIAN` (0026) with `alert_triggers.reconcile` — an alert
  that opens pages once through the configured alerter, one that clears pages one resolution — so
  guardian alerts reach telegram or email the day `ALERT_PROVIDER` selects one, and show among the
  open alerts on the Ops page meanwhile;
* **`python -m dataplatform.ops_guardian notify`** sends one alert straight through the configured
  alerter, for the one case the job cannot carry: the scheduler itself being down.

The file is read as untrusted text: keys are validated, messages pass through `redact` before
they reach a title, a body or the ledger, and a malformed file fails the run rather than
resolving anything (absence of evidence is not a cleared alarm).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, TextIO

from dataplatform.alert_triggers import (
    AlertCondition,
    ConditionLedger,
    TickReport,
    Trigger,
    TriggerEvaluationError,
    reconcile,
    redact,
)
from dataplatform.alerts import AlertConfigurationError, Alerter, AlertOutcome, Severity
from dataplatform.logging import get_logger

if TYPE_CHECKING:  # the scheduler imports this module lazily; keep the edge one-way at runtime
    from dataplatform.config import Settings
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "KEY_PREFIX",
    "GuardianAlert",
    "GuardianStaleError",
    "GuardianState",
    "GuardianStateError",
    "guardian_conditions",
    "main",
    "parse_state",
    "read_state",
    "run_ops_guardian",
    "run_ops_guardian_job",
]

#: Every guardian condition's dedup key starts with this, so it can never collide with a key the
#: four platform triggers own.
KEY_PREFIX: Final = "guardian:"

#: A guardian alert key: what the guardian's `raise KEY MSG` uses. Narrow on purpose — the key
#: becomes a ledger primary key and part of a page, and the file is not trusted.
_KEY: Final = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")

#: How long a guardian title may be before it is truncated; the body carries the full message.
_TITLE_CHARS: Final = 160

log = get_logger(__name__)


class GuardianStateError(RuntimeError):
    """The guardian's state file is missing, unreadable or malformed — the guardian is not proven
    alive, so the job fails and nothing is resolved."""


class GuardianStaleError(GuardianStateError):
    """The guardian's heartbeat is older than the configured limit: it has stopped ticking."""


@dataclass(frozen=True, slots=True)
class GuardianAlert:
    """One alert the guardian holds open (its `state/alert.<key>`), as of its last tick."""

    key: str
    message: str
    since: datetime | None


@dataclass(frozen=True, slots=True)
class GuardianState:
    """The guardian's last tick: when it beat, and what it held open."""

    heartbeat_at: datetime
    alerts: tuple[GuardianAlert, ...]


def _instant(value: object, what: str) -> datetime:
    if not isinstance(value, str):
        raise GuardianStateError(f"{what} is not an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise GuardianStateError(f"{what} is not an ISO-8601 instant") from error
    if parsed.tzinfo is None:
        raise GuardianStateError(f"{what} carries no UTC offset")
    return parsed


def parse_state(text: str) -> GuardianState:
    """Parse the guardian's JSON. Raises `GuardianStateError` on anything but the expected shape.

    What it assumes: `{"heartbeat_at": iso, "alerts": [{"key", "message", "since"?}, ...]}`;
    other top-level fields (pid, agenda counts) are the guardian's and are ignored.
    What it never does: accept a key outside `[a-z0-9_.-]` or a naive timestamp, or keep a
    message unredacted.
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise GuardianStateError(f"state file is not JSON: {error.msg}") from error
    if not isinstance(raw, dict):
        raise GuardianStateError("state file is not a JSON object")
    heartbeat = _instant(raw.get("heartbeat_at"), "heartbeat_at")
    entries = raw.get("alerts", [])
    if not isinstance(entries, list):
        raise GuardianStateError("alerts is not a list")
    alerts: dict[str, GuardianAlert] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise GuardianStateError("an alert is not a JSON object")
        key, message = entry.get("key"), entry.get("message")
        if not isinstance(key, str) or not _KEY.match(key):
            raise GuardianStateError(f"alert key {redact(str(key), limit=80)!r} is not valid")
        if not isinstance(message, str):
            raise GuardianStateError(f"alert {key} has no message")
        since = entry.get("since")
        alerts[key] = GuardianAlert(
            key=key,
            message=redact(message),
            since=None if since in (None, "") else _instant(since, f"alert {key} since"),
        )
    return GuardianState(heartbeat_at=heartbeat, alerts=tuple(alerts.values()))


def read_state(path: Path) -> GuardianState:
    """`parse_state` of the file at `path`; missing or unreadable is a `GuardianStateError`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise GuardianStateError(
            f"guardian state file {path} unreadable: {type(error).__name__}"
        ) from error
    return parse_state(text)


def check_fresh(state: GuardianState, *, now: datetime, stale_after: timedelta) -> None:
    """Raise `GuardianStaleError` when the heartbeat is older than `stale_after` at `now`."""
    age = now - state.heartbeat_at
    if age > stale_after:
        raise GuardianStaleError(
            f"ops guardian heartbeat is {int(age.total_seconds() // 60)} min old "
            f"(last {state.heartbeat_at.isoformat()}, stale after "
            f"{int(stale_after.total_seconds() // 60)} min): ops-guardian.service is not ticking"
        )


def guardian_conditions(state: GuardianState) -> list[AlertCondition]:
    """One CRITICAL `OPS_GUARDIAN` condition per alert the guardian holds open.

    The key is the guardian's own (`guardian:<key>`), so an alert whose wording changes from tick
    to tick ("silent for 31 min" → "35 min") is the same news and pages once.
    """
    conditions = []
    for alert in state.alerts:
        since = "" if alert.since is None else f" since {alert.since.isoformat()}"
        conditions.append(
            AlertCondition(
                trigger=Trigger.OPS_GUARDIAN,
                dedup_key=f"{KEY_PREFIX}{alert.key}",
                severity=Severity.CRITICAL,
                title=f"ops guardian: {redact(alert.message, limit=_TITLE_CHARS)}",
                body=(
                    f"The ops guardian holds [{alert.key}] open{since}:\n{alert.message}\n"
                    "Details: ~/campaign/watchdog/events.log on the box; the guardian is the "
                    "ops-guardian.service user unit."
                ),
            )
        )
    return conditions


def run_ops_guardian(
    *,
    read: Callable[[], GuardianState],
    ledger: ConditionLedger,
    alerter: Alerter,
    now: datetime,
    stale_after: timedelta,
) -> TickReport:
    """Prove the guardian alive, then page what its open alerts changed since the last run.

    What it does: reads the state, raises `GuardianStateError`/`GuardianStaleError` unless it is
    readable and fresh, and otherwise `reconcile`s its alerts against the open `OPS_GUARDIAN` rows.
    What it never does: reconcile on a stale or unreadable state — that would resolve every open
    guardian alert because the guardian died, the opposite of what happened.
    """
    state = read()
    check_fresh(state, now=now, stale_after=stale_after)
    report = TickReport()
    reconcile(
        Trigger.OPS_GUARDIAN,
        guardian_conditions(state),
        ledger=ledger,
        alerter=alerter,
        at=now,
        report=report,
    )
    log.info(
        "ops_guardian.tick",
        heartbeat_at=state.heartbeat_at.isoformat(),
        open=len(state.alerts),
        opened=report.opened,
        resolved=report.resolved,
        errors=len(report.errors),
    )
    return report


def run_ops_guardian_job(context: JobContext) -> None:
    """The `ops_guardian` scheduler job body: one `run_ops_guardian` against production wiring.

    Raises when the guardian is not proven alive or an alert could not be delivered, so the run is
    FAILED — the Ops row turns red and `failure_alerts` pages it.
    """
    from dataplatform.alerts import build_alerter
    from dataplatform.store.db import connection

    settings = context.settings
    path = settings.ops_guardian_state_file
    with connection(settings, autocommit=True) as conn:
        report = run_ops_guardian(
            read=lambda: read_state(path),
            ledger=ConditionLedger(conn),
            alerter=build_alerter(settings, clock=context.clock),
            now=context.clock.now(),
            stale_after=timedelta(minutes=settings.ops_guardian_stale_after_minutes),
        )
    if not report.ok:
        raise TriggerEvaluationError("; ".join(report.errors))


# ── direct send, for when the scheduler is the thing that is down ───────────────────────────


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    stdin: TextIO | None = None,
) -> int:
    """`notify`: send one alert through the configured alerter; the body is read from stdin.

    Exit 0 when the alerter reports SENT, 1 when delivery raised, 2 when the selected channel is
    not configured (the message names the missing keys, never their values). `settings` and
    `stdin` are injectable for tests; production reads `.env` and the process's stdin.
    """
    from dataplatform.alerts import build_alerter
    from dataplatform.clock import SystemClock
    from dataplatform.config import get_settings

    settings = get_settings() if settings is None else settings
    parser = argparse.ArgumentParser(prog="python -m dataplatform.ops_guardian")
    sub = parser.add_subparsers(dest="command", required=True)
    notify = sub.add_parser("notify", help="send one alert; body on stdin")
    notify.add_argument("--severity", choices=[s.value for s in Severity], required=True)
    notify.add_argument("--key", required=True, help="dedup key, e.g. scheduler_stale")
    notify.add_argument("--title", required=True)
    args = parser.parse_args(argv)

    if not _KEY.match(args.key) and not _KEY.match(args.key.removesuffix(":resolved")):
        parser.error("--key must match [a-z0-9][a-z0-9_.-]* (optionally ending :resolved)")
    source = sys.stdin if stdin is None else stdin
    body = "" if source.isatty() else redact(source.read(), limit=2000)
    title = redact(args.title, limit=_TITLE_CHARS)
    try:
        alerter = build_alerter(settings, clock=SystemClock())
    except AlertConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2
    try:
        outcome = alerter.send(Severity(args.severity), title, body, f"{KEY_PREFIX}{args.key}")
    except Exception as error:
        log.error("ops_guardian.notify_failed", key=args.key, error_type=type(error).__name__)
        print(f"not delivered: {type(error).__name__}", file=sys.stderr)
        return 1
    log.info("ops_guardian.notify", key=args.key, outcome=outcome.value)
    return 0 if outcome is AlertOutcome.SENT else 1


if __name__ == "__main__":
    from dataplatform.logging import configure_logging

    configure_logging()  # the entrypoint owns the process's logging, not `main` (tests call it)
    raise SystemExit(main())
