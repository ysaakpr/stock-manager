"""The L0 integrity sweep, scheduled (2026-09-06 audit, finding N6).

`L0Store.verify_checksums` has existed since M0: it re-hashes every stored payload and reports the
four ways a raw tree can be wrong. Every reference to it in the repository was in
`tests/unit/test_l0.py`. No CLI, no scheduler job, no cron, no runbook step — so the immutable lake
that invariant #1 makes *everything* re-derivable from had no integrity sweep at all, and damage
would have been found only when a re-derivation happened to read the damaged file. Over 3.1 GB and
61,511 payloads, that could be years.

`ops/BACKLOG.md` made it worse by recording the opposite belief: *"the evidence store has no
integrity sweep … whereas `L0Store.verify_checksums` reports L0 damage proactively."* The
capability existed; the proactivity did not.

**Rolling, with a full pass once a month.** Re-hashing 3.1 GB weekly is affordable but pointless:
L0 is write-once, so a payload that verified last week and was not touched will verify again. What
changes is what was *written* since — so the weekly sweep covers a trailing window, and the first
sweep of each month covers everything, which is what catches bit-rot in the old tail that nothing
has read in years.

**Reports, never repairs.** `L0Store` will not modify or delete a stored payload for any reason,
including corruption (AGENTIC_CONTEXT §3.10), and this job inherits that: a defect becomes a
CRITICAL alert and an ERROR `quality_flag`, and a human decides. A sweep that "fixed" L0 by
re-fetching would destroy the one copy of the evidence that something went wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from dataplatform.alerts import Alerter, Severity, build_alerter
from dataplatform.clock import Clock
from dataplatform.config import Settings
from dataplatform.logging import get_logger
from dataplatform.quality.sentinel import QualityFinding, finding_fingerprint, persist_findings
from dataplatform.store.db import Connection
from dataplatform.store.l0 import L0Defect, L0Store, L0VerificationReport

__all__ = [
    "CHECK_NAME",
    "DEFAULT_WINDOW_DAYS",
    "L0VerifyResult",
    "run_l0_verify",
    "sweep_window",
]

_LOG = get_logger(__name__)

#: The `quality_flag.check_name` L0 damage is filed under.
CHECK_NAME: Final = "l0_integrity"

#: How far back a rolling sweep reaches. Wider than the weekly cadence on purpose: a payload
#: written on a Saturday and swept the following Saturday must not fall between two windows.
DEFAULT_WINDOW_DAYS: Final = 45


@dataclass(frozen=True, slots=True)
class L0VerifyResult:
    """What one sweep covered and what it found."""

    start: date | None
    end: date
    full: bool
    report: L0VerificationReport
    flags_written: int
    alerts_sent: int

    @property
    def ok(self) -> bool:
        """True when every payload swept matched its recorded checksum."""
        return self.report.ok

    def summary(self) -> str:
        """One line for a log or a runbook."""
        scope = "the whole lake" if self.full else f"{self.start}..{self.end}"
        return (
            f"L0 sweep over {scope}: {self.report.checked} payload(s) re-hashed, "
            f"{len(self.report.defects)} defect(s)"
        )


def sweep_window(
    today: date, *, window_days: int = DEFAULT_WINDOW_DAYS
) -> tuple[date | None, bool]:
    """The range this run should sweep, and whether it is the monthly full pass.

    A full pass on the first seven days of the month rather than on the 1st: the job runs weekly, so
    pinning it to one date would skip the month whenever that date is not the job's day.
    """
    if today.day <= 7:
        return None, True
    return today - timedelta(days=window_days), False


def run_l0_verify(
    *,
    conn: Connection,
    settings: Settings,
    clock: Clock,
    store: L0Store | None = None,
    alerter: Alerter | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> L0VerifyResult:
    """Re-hash L0 for this run's window and report every payload that does not match its record.

    What it does: sweeps, writes one ERROR `quality_flag` per defect, and sends one CRITICAL alert
    when anything is wrong. ERROR is deliberate — invariant #10 counts open ERROR flags, so damaged
    L0 stops the platform trading until a human has looked, which is the correct response to "the
    bytes everything is re-derivable from are not the bytes we stored".
    What it assumes: the caller owns the transaction. Nothing here commits.
    What it never does: touch L0. Not a repair, not a re-fetch, not a deletion — a corrupt payload
    is the evidence, and `L0Store` refuses to modify one for any reason (AGENTIC_CONTEXT §3.10).
    """
    resolved_store = L0Store(clock=clock, data_root=settings.data_root) if store is None else store
    resolved_alerter = build_alerter(settings, clock=clock) if alerter is None else alerter
    today = clock.today()
    start, full = sweep_window(today, window_days=window_days)

    report = resolved_store.verify_checksums(start=start)
    findings = tuple(_finding(defect, today) for defect in report.defects)
    counts = persist_findings(conn, findings, clock=clock)

    sent = 0
    if report.defects:
        outcome = resolved_alerter.send(
            Severity.CRITICAL,
            f"L0 integrity: {len(report.defects)} defect(s) in the raw lake",
            f"{len(report.defects)} payload(s) in L0 do not match what was recorded for them, "
            f"found sweeping {report.checked} record(s). L0 is what every L1 and L2 value is "
            f"re-derived from (invariant #1), so this is not a warning. Nothing has been "
            f"repaired — L0 is never modified, including when it is wrong. First few: "
            + "; ".join(f"{d.kind.value} {d.path}" for d in report.defects[:5]),
            f"l0_integrity:{today.isoformat()}",
        )
        sent = 1 if outcome is not None else 0

    _LOG.info(
        "l0.sweep_done" if report.ok else "l0.sweep_found_damage",
        start=None if start is None else start.isoformat(),
        end=today.isoformat(),
        full=full,
        checked=report.checked,
        defects=len(report.defects),
        flags_written=counts.written,
    )
    return L0VerifyResult(
        start=start,
        end=today,
        full=full,
        report=report,
        flags_written=counts.written,
        alerts_sent=sent,
    )


def _finding(defect: L0Defect, today: date) -> QualityFinding:
    """One defect as a D7 finding. The path is the fingerprint — one flag per damaged file."""
    return QualityFinding(
        logical_date=today,
        check_name=CHECK_NAME,
        severity="ERROR",
        detail={"kind": defect.kind.value, "path": str(defect.path), "message": defect.detail},
        fingerprint=finding_fingerprint(
            f"{CHECK_NAME}:{defect.kind.value}:{defect.path}", None, today
        ),
    )
