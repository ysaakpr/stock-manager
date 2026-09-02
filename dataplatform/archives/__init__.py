"""D6: archive publisher (§4.5).

Builds a date's downloadable daily bundle (normalized Parquet + CSV) with a checksummed manifest
that maps every output file back to the L0 refs it derived from, and records the `archive_bundle`
row `GET /archives?date=` serves. Public redistribution of exchange data is a legal question
reserved to the human (§10, AGENTIC_CONTEXT §3.8) — local/personal download only.
"""

from dataplatform.archives.manifest import (
    MANIFEST_FILENAME,
    L0Lineage,
    Manifest,
    ManifestError,
    ManifestFile,
    manifest_sha256,
)
from dataplatform.archives.publisher import (
    ARCHIVE_SUBDIR,
    BUNDLE_SCHEMA_VERSION,
    ArchivePublishError,
    PublishReport,
    bundle_dir,
    publish_bundle,
)

__all__ = [
    "ARCHIVE_SUBDIR",
    "BUNDLE_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "ArchivePublishError",
    "L0Lineage",
    "Manifest",
    "ManifestError",
    "ManifestFile",
    "PublishReport",
    "bundle_dir",
    "manifest_sha256",
    "publish_bundle",
]
