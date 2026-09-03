"""D6 · the archive publisher (§4.5, M1.12) — builds one date's downloadable daily bundle.

For a backfilled trading date this reads the normalized L1 partitions, materializes each as both
Parquet (a byte-identical copy of the L1 file) and CSV, checksums every output file, records which
L0 payloads each file was derived from, writes a `manifest.json`, and inserts the `archive_bundle`
row that `GET /archives?date=` reads. The bundle is the product's downloadable form; the manifest
is what makes it verifiable and traceable back to immutable L0 (invariant #1).

Determinism: the L1 parquet is already sorted and quantised (M1.8), the CSV is written from it with
pyarrow, and the manifest carries no wall-clock field — so re-publishing the same date from the same
L0 produces byte-identical files and an identical manifest sha256. The DB row carries `published_at`
(from the injected clock, B10); the bundle bytes do not.

Legal posture — **HUMAN_GATE (§10, AGENTIC_CONTEXT §3.8).** These bundles are for local and personal
download only. Public redistribution of exchange (NSE/BSE) data likely requires an exchange data
license, which is a legal question reserved to the human — this module never exposes a bundle beyond
the operator's own host, and the download route is gated to the same status-API host. See
`ops/runbooks/archive-publisher.md`.

Offline by construction: reads L1 parquet and L0 refs, writes files and one DB row. No network.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
from psycopg.types.json import Json

from dataplatform.archives.manifest import (
    MANIFEST_FILENAME,
    L0Lineage,
    Manifest,
    ManifestFile,
    manifest_sha256,
)
from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.logging import configure_logging, get_logger
from dataplatform.store.db import Connection, connection
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA

__all__ = [
    "ARCHIVE_SUBDIR",
    "BUNDLE_SCHEMA_VERSION",
    "ArchivePublishError",
    "PublishReport",
    "bundle_dir",
    "main",
    "publish_bundle",
]

_LOG = get_logger(__name__)

#: The bundle/manifest schema version. Bumped only when the manifest shape or the bundle layout
#: changes in a way a reader must branch on — a v1 reader must be able to trust this string.
BUNDLE_SCHEMA_VERSION = "1"

#: Archives live under `<archive_root>/archives/<date>/`; `bundle_path` is stored relative to the
#: archive root (`archives/<date>`) so one row means the same bundle from the container, a restored
#: backup, or a developer's checkout (archive_bundle.bundle_path COMMENT).
ARCHIVE_SUBDIR = "archives"

#: Datasets a v1 daily bundle publishes. Just the canonical normalized price partition for now;
#: the quarantine sidecar is a gap record, not a product, and is deliberately not redistributed.
_DEFAULT_DATASETS: tuple[str, ...] = (PRICES_RAW_DATASET,)

_DATASET_SCHEMAS = {PRICES_RAW_DATASET: PRICES_RAW_SCHEMA}


class ArchivePublishError(RuntimeError):
    """A bundle could not be built for a date — a missing L1 partition, or no L0 lineage for it."""


@dataclass(frozen=True, slots=True)
class PublishReport:
    """The outcome of publishing one date's bundle — the counts a caller and the DB row agree on."""

    logical_date: date
    bundle_dir: Path
    bundle_path: str
    manifest_path: Path
    manifest_sha256: str
    file_count: int
    total_bytes: int
    filenames: tuple[str, ...]


def bundle_dir(archive_root: Path, logical_date: date) -> Path:
    """The on-disk directory a date's bundle occupies: `<archive_root>/archives/<date>`."""
    return archive_root / ARCHIVE_SUBDIR / logical_date.isoformat()


def _bundle_path(logical_date: date) -> str:
    """The bundle's location relative to the archive root, as stored in `archive_bundle`."""
    return f"{ARCHIVE_SUBDIR}/{logical_date.isoformat()}"


def publish_bundle(
    conn: Connection,
    store: L0Store,
    logical_date: date,
    *,
    clock: Clock,
    archive_root: Path,
    data_root: Path | None = None,
    datasets: tuple[str, ...] = _DEFAULT_DATASETS,
) -> PublishReport:
    """Build, checksum, and record one date's archive bundle; return what was published.

    What it does: for each dataset, reads its L1 partition for `logical_date`, copies the parquet
    and writes a CSV into `<archive_root>/archives/<date>/`, computes each file's sha256/size/rows,
    attaches the L0 lineage for the date, writes `manifest.json`, and upserts the `archive_bundle`
    row `GET /archives?date=` serves. Returns a `PublishReport` whose `file_count`/`total_bytes`
    equal the manifest's, so a caller can reconcile without re-reading the row.
    What it assumes: `logical_date` is backfilled — its L1 partitions exist and L0 holds the raw
    payloads for the date. A missing partition or absent L0 lineage raises `ArchivePublishError`
    rather than publishing an empty or unprovable bundle.
    What it never does: publish a file with no lineage (invariant #1 / acceptance 3), store an
    absolute host path in the row, or expose the bundle beyond this host (§10 legal gate).

    Idempotent: bundles are derived, not L0, so re-publishing a date overwrites its files and its
    row. The files and manifest are byte-deterministic; only `published_at` in the row moves.
    """
    root = archive_root
    out_dir = bundle_dir(root, logical_date)
    out_dir.mkdir(parents=True, exist_ok=True)

    lineage = _l0_lineage(store, logical_date)
    if not lineage:
        raise ArchivePublishError(
            f"no L0 payloads for {logical_date.isoformat()}: a bundle's files must map to the L0 "
            "refs they derived from (M1.12 acceptance 3), and there are none. Backfill L0 first."
        )

    files: list[ManifestFile] = []
    for dataset in datasets:
        files.extend(
            _publish_dataset(
                dataset, logical_date, out_dir=out_dir, data_root=data_root, lineage=lineage
            )
        )

    manifest = Manifest(
        schema_version=BUNDLE_SCHEMA_VERSION,
        logical_date=logical_date,
        bundle_path=_bundle_path(logical_date),
        files=tuple(files),
    )
    manifest_bytes = manifest.to_json()
    manifest_path = out_dir / MANIFEST_FILENAME
    manifest_path.write_bytes(manifest_bytes)
    digest = manifest_sha256(manifest_bytes)

    published_at = clock.now()
    _record_bundle(conn, manifest=manifest, manifest_sha256=digest, published_at=published_at)

    _LOG.info(
        "archive.published",
        logical_date=logical_date.isoformat(),
        bundle_path=manifest.bundle_path,
        file_count=manifest.file_count,
        total_bytes=manifest.total_bytes,
        manifest_sha256=digest,
        files=[file.name for file in manifest.files],
        state="PUBLISHED",
    )
    return PublishReport(
        logical_date=logical_date,
        bundle_dir=out_dir,
        bundle_path=manifest.bundle_path,
        manifest_path=manifest_path,
        manifest_sha256=digest,
        file_count=manifest.file_count,
        total_bytes=manifest.total_bytes,
        filenames=tuple(file.name for file in manifest.files),
    )


def _publish_dataset(
    dataset: str,
    logical_date: date,
    *,
    out_dir: Path,
    data_root: Path | None,
    lineage: tuple[L0Lineage, ...],
) -> list[ManifestFile]:
    """Write one dataset's Parquet and CSV into the bundle and describe them for the manifest.

    The Parquet is a byte-identical copy of the L1 partition (already normalized and deterministic,
    M1.8); the CSV is written from the same table so the two are exactly the same rows. Both carry
    the date's L0 lineage — for `prices_raw` the day's bhavcopy and delivery payloads are the raw
    bytes every output row descends from.
    """
    schema = _DATASET_SCHEMAS.get(dataset)
    if schema is None:
        raise ArchivePublishError(f"no known schema for dataset {dataset!r}; cannot bundle it")

    partition = l1_partition_path(dataset, logical_date, data_root=data_root)
    if not partition.exists():
        raise ArchivePublishError(
            f"no L1 {dataset} partition for {logical_date.isoformat()} at {partition}; "
            "the date is not backfilled, so there is nothing to archive"
        )

    parquet_bytes = partition.read_bytes()
    table = pq.read_table(partition, schema=schema)
    rows = table.num_rows

    parquet_name = f"{dataset}.parquet"
    csv_name = f"{dataset}.csv"
    parquet_path = out_dir / parquet_name
    csv_path = out_dir / csv_name

    parquet_path.write_bytes(parquet_bytes)
    csv_bytes = _to_csv_bytes(table)
    csv_path.write_bytes(csv_bytes)

    return [
        ManifestFile(
            name=parquet_name,
            path=parquet_name,
            sha256=_sha256(parquet_bytes),
            bytes=len(parquet_bytes),
            rows=rows,
            lineage=lineage,
        ),
        ManifestFile(
            name=csv_name,
            path=csv_name,
            sha256=_sha256(csv_bytes),
            bytes=len(csv_bytes),
            rows=rows,
            lineage=lineage,
        ),
    ]


def _l0_lineage(store: L0Store, logical_date: date) -> tuple[L0Lineage, ...]:
    """The L0 payloads for a date, as lineage entries — the raw bytes the L1 rows derived from.

    A prices_raw partition for date D is derived from exactly the L0 payloads logically dated D (the
    bhavcopy and, when present, the delivery file). Iterating L0 by that date is therefore the
    honest provenance map, and it re-hashes nothing — the sha256 is the one L0 already recorded.
    """
    return tuple(
        L0Lineage(
            source=ref.source,
            logical_date=ref.logical_date,
            filename=ref.filename,
            sha256=ref.sha256,
            size_bytes=ref.size_bytes,
        )
        for ref in _sorted_refs(store, logical_date)
    )


def _sorted_refs(store: L0Store, logical_date: date) -> list[L0Ref]:
    """L0 refs for the date, ordered so the manifest is stable across re-publishes."""
    refs = list(store.iter_refs(start=logical_date, end=logical_date))
    refs.sort(key=lambda ref: (ref.source, ref.filename))
    return refs


def _to_csv_bytes(table: pa.Table) -> bytes:
    """Render a table to deterministic CSV bytes — decimals as plain strings, dates as ISO."""
    sink = pa.BufferOutputStream()
    pacsv.write_csv(table, sink)
    payload: bytes = sink.getvalue().to_pybytes()
    return payload


def _sha256(payload: bytes) -> str:
    """Lowercase-hex sha256 of some bytes."""
    return hashlib.sha256(payload).hexdigest()


_UPSERT_BUNDLE_SQL = """
INSERT INTO archive_bundle
    (logical_date, schema_version, bundle_path, manifest_sha256, file_count, total_bytes,
     manifest, published_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (logical_date) DO UPDATE SET
    schema_version = EXCLUDED.schema_version,
    bundle_path = EXCLUDED.bundle_path,
    manifest_sha256 = EXCLUDED.manifest_sha256,
    file_count = EXCLUDED.file_count,
    total_bytes = EXCLUDED.total_bytes,
    manifest = EXCLUDED.manifest,
    published_at = EXCLUDED.published_at
"""


def _record_bundle(
    conn: Connection,
    *,
    manifest: Manifest,
    manifest_sha256: str,
    published_at: datetime,
) -> None:
    """Upsert the `archive_bundle` row `GET /archives?date=` reads.

    Upsert rather than insert-only because a bundle is derived: re-publishing a date after a
    re-derivation replaces its row, it does not conflict with L0 immutability. The commit is the
    caller's — a publisher that half-wrote the lake and then committed its own row would be a bundle
    the row promises but the disk does not have.
    """
    conn.execute(
        _UPSERT_BUNDLE_SQL,
        (
            manifest.logical_date,
            manifest.schema_version,
            manifest.bundle_path,
            manifest_sha256,
            manifest.file_count,
            manifest.total_bytes,
            Json(manifest.db_document()),
            published_at,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point — publish one backfilled date's bundle into the operator's own lake.

    What it does: builds the real wiring from settings (the configured `data_root` lake and
    Postgres, B10 `SystemClock`), publishes the date's bundle with `publish_bundle`, commits the
    `archive_bundle` row, and prints the manifest sha256, file count and byte total. The bundle is
    then immediately downloadable at `GET /archives?date=` on the same host (§10 legal gate — local
    and personal download only). This is how an operator publishes an archive for a date the daily
    EOD job never covered (e.g. a range backfilled by M1.13, whose runner does not itself archive).
    What it assumes: the date is backfilled — its L1 `prices_raw` partition and the L0 payloads it
    derived from both exist under `data_root`. It refuses loud (`ArchivePublishError`, exit 1)
    rather than emit an empty or unprovable bundle. Never opens a socket.
    """
    parser = argparse.ArgumentParser(prog="archive-publisher", description=__doc__)
    parser.add_argument(
        "--date",
        dest="logical_date",
        required=True,
        type=date.fromisoformat,
        help="the backfilled trading date to publish (YYYY-MM-DD)",
    )
    args = parser.parse_args(argv)

    configure_logging()
    settings: Settings = get_settings()
    clock = SystemClock()
    store = L0Store(clock=clock, data_root=settings.data_root)

    try:
        with connection(settings) as conn:
            report = publish_bundle(
                conn,
                store,
                args.logical_date,
                clock=clock,
                archive_root=settings.data_root,
                data_root=settings.data_root,
            )
            conn.commit()
    except ArchivePublishError as error:
        print(f"cannot publish {args.logical_date.isoformat()}: {error}", file=sys.stderr)
        return 1

    print(
        f"{report.logical_date.isoformat()}: {report.file_count} files, "
        f"{report.total_bytes} bytes at {report.bundle_path} "
        f"(manifest {report.manifest_sha256})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
