"""D5: the status-API reads that no other module already owns.

Two of the six §4.4 endpoints read through somebody else's module on purpose, because a second
implementation would be a second answer to the same question: `sync_state` is read through
`SyncStateStore` (M1.3), which the trading interlock also uses, and the scheduler heartbeat
through `dataplatform.scheduler.read_heartbeat` (M0.6), which is written by the same module that
beats. This file is the remainder of the surface: the stuck-pairs read behind `/status/gaps`, the
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

from datetime import date, datetime

from pydantic import ValidationError

from dataplatform.status.models import (
    ArchiveBundleOut,
    ArchiveFileOut,
    ArchivesOut,
    GapEntryOut,
    GapsOut,
    QualityFlagOut,
    QualityOut,
    SeverityCountOut,
)
from dataplatform.store.db import Connection

__all__ = ["StatusQueryError", "read_archives", "read_gaps", "read_quality"]


class StatusQueryError(RuntimeError):
    """State in the database the status contract cannot describe — a defect, not a bad request."""


# PUBLISHED is complete and GAP is an explained absence (weekend, holiday). Everything else in the
# range is stuck mid-pipeline or failed, and both owe someone an explanation.
_GAPS_SQL = """
SELECT source, logical_date, state, attempts, retryable, last_error, updated_at
FROM sync_state
WHERE logical_date BETWEEN %s AND %s
  AND state NOT IN ('PUBLISHED', 'GAP')
ORDER BY logical_date, source
"""

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

_ARCHIVES_SQL = """
SELECT logical_date, schema_version, bundle_path, manifest_sha256, file_count, total_bytes,
       manifest, published_at
FROM archive_bundle
WHERE logical_date = %s
"""


def read_gaps(conn: Connection, from_date: date, to_date: date) -> GapsOut:
    """Tracked `(source, date)` pairs in an inclusive range that are neither PUBLISHED nor GAP.

    Never invents a pair: a date no source ever attempted has no row and cannot appear here,
    because classifying an absence needs the trading calendar and the per-source cadence (M1.11).
    Reading an empty answer as "every missing day is explained" would be wrong until that lands,
    which is why `GapsOut.unexplained` documents exactly what populates it.
    """
    if from_date > to_date:
        raise StatusQueryError(f"from={from_date.isoformat()} is after to={to_date.isoformat()}")
    rows = conn.execute(_GAPS_SQL, (from_date, to_date)).fetchall()
    return GapsOut(
        from_date=from_date,
        to_date=to_date,
        unexplained=[
            GapEntryOut(
                source=str(row[0]),
                date=row[1],
                state=row[2],
                attempts=int(row[3]),
                retryable=bool(row[4]),
                last_error=row[5],
                updated_at=row[6],
            )
            for row in rows
        ],
    )


def read_quality(conn: Connection, as_of: datetime, limit: int) -> QualityOut:
    """Open D7 sentinel flags, newest first, plus the totals the limit would otherwise hide."""
    counts = [
        SeverityCountOut(severity=row[0], count=int(row[1]))
        for row in conn.execute(_QUALITY_COUNTS_SQL).fetchall()
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
