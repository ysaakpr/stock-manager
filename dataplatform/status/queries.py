"""D5: the status-API reads that no other module already owns.

Three of the six §4.4 endpoints read through somebody else's module on purpose, because a second
implementation would be a second answer to the same question: `sync_state` is read through
`SyncStateStore` (M1.3), which the trading interlock also uses, the scheduler heartbeat through
`dataplatform.scheduler.read_heartbeat` (M0.6), which is written by the same module that beats,
and `/status/gaps` through `dataplatform.quality.gaps.GapScanner` (M1.11), which owns the one
definition of what an unexplained missing day is. This file is the remainder of the surface: the
open D7 flags behind `/status/quality`, and the published bundle behind `/archives`.

Split from `api.py` so the HTTP layer is only routing and status codes, and so "where does this
number come from" is answered by one file of plain SQL. Every function takes an open connection
and returns wire models; none opens a connection, reads the clock, or writes. The status surface
is read-only by construction — it reports the platform's state and is never a way to change it.

The instant a query is relative to is always passed in, never taken from the database's `now()`:
the clock is injected (B10, invariant #11), and a `/health` answering from the server clock would
ignore the frozen clock a test or a replay set.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from dataplatform.quality.quarantine import read_quarantine, step_changes
from dataplatform.status.models import (
    ArchiveBundleOut,
    ArchiveFileOut,
    ArchivesOut,
    CheckCountOut,
    KillSwitchOut,
    ManagerBookOut,
    ManagerDecisionOut,
    ManagerScoreOut,
    ManagersOut,
    PaperBookStatusOut,
    PaperOut,
    QualityFlagOut,
    QualityOut,
    QuarantineCountOut,
    QuarantineOut,
    QuarantineStepOut,
    SeverityCountOut,
    SuspendedHoldingOut,
)
from dataplatform.store.db import Connection

__all__ = ["StatusQueryError", "read_archives", "read_paper_status", "read_quality"]


class StatusQueryError(RuntimeError):
    """State in the database the status contract cannot describe — a defect, not a bad request."""


_QUALITY_FLAGS_SQL = """
SELECT id, logical_date, check_name, severity, isin, source, observed_value, threshold,
       detail, raised_at
FROM quality_flag
WHERE NOT resolved
ORDER BY raised_at DESC, id DESC
LIMIT %s
"""

_QUALITY_COUNTS_SQL = """
SELECT severity, count(*)
FROM quality_flag
WHERE NOT resolved
GROUP BY severity
ORDER BY severity
"""

#: Open flags grouped by what actually raised them. `/status/quality` reported one number and a
#: truncated page of rows, so a saturated queue ("2,487 open" every day, for two years) rendered
#: identically to a fresh incident. The two columns that separate them are the last two: how many
#: of these arrived today, and how many in the last week.
_QUALITY_BY_CHECK_SQL = """
SELECT check_name,
       severity,
       count(*),
       min(logical_date),
       max(logical_date),
       count(*) FILTER (WHERE raised_at >= %s),
       count(*) FILTER (WHERE raised_at >= %s)
FROM quality_flag
WHERE NOT resolved
GROUP BY check_name, severity
ORDER BY count(*) DESC, check_name
"""

_ARCHIVES_SQL = """
SELECT logical_date, schema_version, bundle_path, manifest_sha256, file_count, total_bytes,
       manifest, published_at
FROM archive_bundle
WHERE logical_date = %s
"""


def read_quality(conn: Connection, as_of: datetime, limit: int) -> QualityOut:
    """Open D7 sentinel flags, newest first, plus the totals the limit would otherwise hide.

    Grouped by check as well as by severity, because "how many are open" is not the operational
    question — "is today worse than yesterday" is, and a permanently saturated queue answers the
    first one identically every day. `raised_today`/`raised_last_7_days` are that difference.
    """
    midnight = datetime.combine(as_of.date(), time.min, tzinfo=as_of.tzinfo)
    week_ago = midnight - timedelta(days=7)
    counts = [
        SeverityCountOut(severity=row[0], count=int(row[1]))
        for row in conn.execute(_QUALITY_COUNTS_SQL).fetchall()
    ]
    by_check = [
        CheckCountOut(
            check_name=str(row[0]),
            severity=row[1],
            count=int(row[2]),
            first_date=row[3],
            last_date=row[4],
            raised_today=int(row[5]),
            raised_last_7_days=int(row[6]),
        )
        for row in conn.execute(_QUALITY_BY_CHECK_SQL, (midnight, week_ago)).fetchall()
    ]
    flags = [
        QualityFlagOut(
            id=int(row[0]),
            date=row[1],
            check_name=str(row[2]),
            severity=row[3],
            isin=row[4],
            source=row[5],
            observed_value=row[6],
            threshold=row[7],
            detail=row[8],
            raised_at=row[9],
        )
        for row in conn.execute(_QUALITY_FLAGS_SQL, (limit,)).fetchall()
    ]
    return QualityOut(
        as_of=as_of,
        open_total=sum(entry.count for entry in counts),
        counts=counts,
        by_check=by_check,
        flags=flags,
        limit=limit,
    )


def read_archives(conn: Connection, logical_date: date) -> ArchivesOut:
    """The published archive bundle for one date, or `bundle=null` when there is none."""
    row = conn.execute(_ARCHIVES_SQL, (logical_date,)).fetchone()
    if row is None:
        return ArchivesOut(date=logical_date)
    return ArchivesOut(
        date=row[0],
        bundle=ArchiveBundleOut(
            date=row[0],
            schema_version=str(row[1]),
            bundle_path=str(row[2]),
            manifest_sha256=str(row[3]),
            file_count=int(row[4]),
            total_bytes=int(row[5]),
            published_at=row[7],
            files=_manifest_files(row[6], logical_date),
        ),
    )


def _manifest_files(manifest: object, logical_date: date) -> list[ArchiveFileOut]:
    """Project a stored manifest's `files` array onto the response contract.

    A manifest with no `files` key yields an empty list — that is a publisher that recorded a
    bundle without describing it, and `/archives` reports what is there. A `files` entry that does
    not match `ArchiveFileOut` raises instead: the manifest is the checksum record for a data
    archive, and dropping the entries we cannot read would turn a corrupt manifest into a shorter
    one that looks perfectly fine.
    """
    if not isinstance(manifest, dict):
        raise StatusQueryError(
            f"archive_bundle.manifest for {logical_date.isoformat()} is "
            f"{type(manifest).__name__}, not a JSON object"
        )
    entries = manifest.get("files", [])
    if not isinstance(entries, list):
        raise StatusQueryError(
            f"archive_bundle.manifest.files for {logical_date.isoformat()} is "
            f"{type(entries).__name__}, not a list"
        )
    try:
        return [ArchiveFileOut.model_validate(entry) for entry in entries]
    except ValidationError as error:
        raise StatusQueryError(
            f"archive_bundle.manifest.files for {logical_date.isoformat()} does not match the "
            f"ArchiveFileOut contract: {error}"
        ) from error


def read_quarantine_status(
    from_date: date, to_date: date, *, limit: int, data_root: Path | None = None
) -> QuarantineOut:
    """`GET /status/quarantine` — what L1 refused over a range, and which sessions are news.

    The per-session enumeration is capped the way `/status/quality`'s is: `rows`, `totals` and
    `steps` are computed over the whole range, so the cap changes how much you read and never what
    is true. `steps` is never capped — there are single digits of them over a decade, and a
    truncated list of the only actionable field would be worse than useless.
    """
    report = read_quarantine(from_date, to_date, data_root=data_root)
    steps = step_changes(report.counts)
    return QuarantineOut(
        from_date=from_date,
        to_date=to_date,
        rows=report.rows,
        partitions_read=report.partitions_read,
        totals=report.totals(),
        steps=[
            QuarantineStepOut(
                trade_date=step.trade_date,
                exchange=step.exchange,
                reason=step.reason,
                rows=step.rows,
                baseline=step.baseline,
                multiple=step.multiple,
                detail=step.detail(),
            )
            for step in steps
        ],
        counts=[
            QuarantineCountOut(
                trade_date=count.trade_date,
                exchange=count.exchange,
                reason=count.reason,
                rows=count.rows,
            )
            # Newest first: an operator opening this wants the recent sessions, and the ten-year
            # tail is what the cap is there to keep out.
            for count in sorted(report.counts, key=lambda c: c.trade_date, reverse=True)[:limit]
        ],
        limit=limit,
    )


# ── /status/paper ───────────────────────────────────────────────────────────────────────────

_PAPER_LATEST_SQL = """
SELECT DISTINCT ON (book_id) book_id, trading_date, outcome, reason, recon->>'status'
FROM paper_session ORDER BY book_id, trading_date DESC
"""

#: Every escalated corporate action and every reconciliation break with no resolution row — the
#: same (key, terms) the paper session itself refuses to trade past.
_PAPER_UNRESOLVED_SQL = """
WITH raised AS (
    SELECT p.book_id, a->>'key' AS key, a->>'terms' AS terms
    FROM paper_session p, jsonb_array_elements(p.actions) AS a
    WHERE a->>'status' = 'ESCALATED'
    UNION
    SELECT book_id, recon->>'key', recon->>'terms'
    FROM paper_session WHERE recon->>'status' = 'BREAK'
)
SELECT r.book_id, r.key || '@' || r.terms
FROM raised r
WHERE NOT EXISTS (
    SELECT 1 FROM paper_session_resolution s
    WHERE s.book_id = r.book_id AND s.action_key = r.key AND s.terms = r.terms
)
ORDER BY 1, 2
"""


def read_paper_status(conn: Connection, *, data_root: Path, as_of: datetime) -> PaperOut:
    """Each paper book's latest session, kill switch and unresolved blocks (M15.3).

    The books are the union of those with a ``paper_session`` row and those with a kill-switch file
    under ``<data_root>/kill_switch`` — a switch tripped before a book's first row still shows. A
    switch file that cannot be read is reported with its error and counts as unhealthy, never as
    armed (the switch itself refuses to treat it as armed, too).
    """
    from execution.kill_switch import KILL_SWITCH_DIRNAME, KillSwitch, kill_switch_path

    latest = {row[0]: row for row in conn.execute(_PAPER_LATEST_SQL).fetchall()}
    unresolved: dict[str, list[str]] = {}
    for book_id, item in conn.execute(_PAPER_UNRESOLVED_SQL).fetchall():
        unresolved.setdefault(book_id, []).append(item)
    switch_dir = data_root / KILL_SWITCH_DIRNAME
    with_switch = (
        {path.stem for path in switch_dir.glob("*.json")} if switch_dir.is_dir() else set()
    )
    books: list[PaperBookStatusOut] = []
    for book_id in sorted(set(latest) | with_switch):
        try:
            state = KillSwitch(kill_switch_path(data_root, book_id)).state
            switch = KillSwitchOut(
                tripped=state.tripped,
                source=None if state.source is None else state.source.value,
                reason=state.reason,
                tripped_at=state.tripped_at,
            )
        except (OSError, ValueError) as error:
            switch = KillSwitchOut(
                tripped=None, source=None, reason=None, tripped_at=None, error=str(error)
            )
        row = latest.get(book_id)
        blocks = unresolved.get(book_id, [])
        books.append(
            PaperBookStatusOut(
                book_id=book_id,
                healthy=switch.tripped is False
                and (row is None or row[2] != "RECON_BREAK")
                and not blocks,
                kill_switch=switch,
                latest_date=None if row is None else row[1],
                latest_outcome=None if row is None else row[2],
                latest_reason=None if row is None else row[3],
                latest_recon=None if row is None else row[4],
                unresolved=blocks,
            )
        )
    return PaperOut(as_of=as_of, healthy=all(book.healthy for book in books), books=books)


# ── /status/managers (M17.6) ───────────────────────────────────────────────────────────────────


def read_m17_roster() -> Any:
    """The M17 roster (`analyst.fundmanager.load_roster`), imported lazily."""
    from analyst.fundmanager import load_roster

    return load_roster()


def read_m17_journal(conn: Connection, *, data_root: Path, roster: Any) -> tuple[Any, ...]:
    """Every journal entry of the M17 books, in append order (S0 is journaled in each book's
    stream, beside its mandate hash).

    Read through `analyst.journal.Journal` — the one reader of `decision_journal` — and imported
    here lazily, like the kill switch above, so importing the status API stays free of System 2.
    """
    from analyst.journal import EvidenceStore, Journal, JournalFilter

    journal = Journal(conn, evidence=EvidenceStore(data_root / "evidence"))
    books = [book.id for book in roster.books]
    entries = [e for book in books for e in journal.entries(JournalFilter(case_id=book))]
    return tuple(sorted(entries, key=lambda e: e.id))


def read_managers_status(entries: Sequence[Any], *, roster: Any, as_of: datetime) -> ManagersOut:
    """The `/status/managers` body from the M17 journal ``entries`` (in append order).

    The scoreboard is rebuilt from the entries on every call, the same way the daily job builds
    it, so the page can never show a number the journal does not reproduce. A journal the
    scoreboard refuses (a missing mark, a malformed line) is reported in ``scoreboard_error`` with
    no scores, never papered over with partial numbers; the decisions still show.
    """
    from analyst.fundmanager.scoreboard import (
        ScoreboardError,
        ScoreboardInputs,
        build_scoreboard,
        inputs_from_journal,
        suspended_holdings_on,
        todays_decisions,
    )

    session, lines = todays_decisions(entries, roster)
    _, suspended = suspended_holdings_on(entries, roster, session=session)
    error: str | None = None
    try:
        scoreboard = build_scoreboard(roster, inputs_from_journal(entries, roster))
    except ScoreboardError as exc:
        error = str(exc)
        scoreboard = build_scoreboard(roster, ScoreboardInputs(s0=None))
    managers: list[ManagerScoreOut] = []
    for score in scoreboard.managers:
        window = score.extension or score.primary
        managers.append(
            ManagerScoreOut(
                manager_id=score.manager_id,
                control_id=score.control_id,
                phase=score.phase.value,
                verdict=score.verdict.value,
                window=None if window is None else window.label,
                window_sessions=None if window is None else window.sessions,
                window_end=None if window is None else window.end,
                excess_vs_control_pp=None if window is None else window.excess_vs_control_pp,
                excess_vs_bench_pp=(
                    None if window is None else window.secondary.excess_vs_bench_pp
                ),
                max_drawdown_pp=None if window is None else window.manager_max_drawdown_pp,
                bench_max_drawdown_pp=None if window is None else window.bench_max_drawdown_pp,
                brier=None if window is None else window.brier,
                resolved_decisions=None if window is None else window.resolved_decisions,
                suspended_resolved_decisions=(
                    None if window is None else window.suspended_resolved_decisions
                ),
            )
        )
    return ManagersOut(
        as_of=as_of,
        s0=scoreboard.s0,
        scoreboard_as_of=scoreboard.as_of,
        sessions_elapsed=scoreboard.sessions_elapsed,
        k_of_n=scoreboard.k_of_n,
        passed=scoreboard.passed,
        scoreboard_digest=None if error is not None else scoreboard.digest(),
        scoreboard_error=error,
        books=[ManagerBookOut(**book.model_dump()) for book in scoreboard.books],
        managers=[] if error is not None else managers,
        decisions_session=session,
        decisions=[ManagerDecisionOut(**line.model_dump()) for line in lines],
        suspended_holdings=[SuspendedHoldingOut(**line.model_dump()) for line in suspended],
    )
