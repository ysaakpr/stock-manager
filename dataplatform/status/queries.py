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

from datetime import date, datetime, time, timedelta
from pathlib import Path

from pydantic import ValidationError

from dataplatform.quality.quarantine import read_quarantine, step_changes
from dataplatform.status.models import (
    ArchiveBundleOut,
    ArchiveFileOut,
    ArchivesOut,
    CheckCountOut,
    QualityFlagOut,
    QualityOut,
    QuarantineCountOut,
    QuarantineOut,
    QuarantineStepOut,
    SeverityCountOut,
)
from dataplatform.store.db import Connection

__all__ = ["StatusQueryError", "read_archives", "read_quality"]


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
