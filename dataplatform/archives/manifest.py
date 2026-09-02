"""D6 · the archive manifest — the checksummed description of one daily bundle (§4.5, M1.12).

A bundle is a directory of data files (normalized Parquet + CSV); the manifest is the document
that makes that directory *provable*. For every file it records a sha256, a byte size, a row count
and — the property this platform cares about most — the L0 references the file was derived from, so
a downloaded bundle can be verified against the manifest and the manifest can be traced back to the
immutable raw bytes it came from (invariant #1).

Two representations come out of one `Manifest` object, and neither drifts from the other:

* `to_json()` is the canonical on-disk `manifest.json`. It carries everything, lineage inline per
  file, and it is written *without* any wall-clock field so re-deriving the same date from the same
  L0 produces byte-identical manifest bytes — the manifest's own sha256 is then a stable identity
  for the bundle, not a timestamp.
* `db_document()` is the JSON stored in `archive_bundle.manifest`. Its `files` entries match
  `dataplatform.status.models.ArchiveFileOut` exactly (that model forbids extra keys, and
  `/archives` validates against it), so lineage lives beside `files` under a top-level `lineage`
  map rather than inside each entry.

Money and counts are integers/decimals, never floats. Nothing here reads the filesystem or the
clock: a `Manifest` is built from values the publisher already computed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MANIFEST_FILENAME",
    "L0Lineage",
    "Manifest",
    "ManifestError",
    "ManifestFile",
    "manifest_sha256",
]

#: The manifest's own filename inside a bundle. It is never listed among its own `files` — a file
#: cannot carry its own checksum — so its integrity is recorded separately as `manifest_sha256`.
MANIFEST_FILENAME = "manifest.json"


class ManifestError(ValueError):
    """A manifest that cannot describe a real bundle — e.g. an output file with no lineage."""


class L0Lineage(BaseModel):
    """One L0 payload an output file was derived from — the raw-bytes end of the lineage link.

    Enough to find the exact bytes again (`source`/`logical_date`/`filename` is the L0 key) and to
    prove they have not changed (`sha256`). This is a projection of `store.l0.L0Ref`, not the ref
    itself, so the manifest stays a plain document with no import cycle into the store.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(description="L0 source id, e.g. 'nse_bhavcopy_udiff'")
    logical_date: date = Field(description="the trading date the L0 payload is about")
    filename: str = Field(description="the source's own filename, as stored in L0")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="sha256 of the L0 payload")
    size_bytes: int = Field(ge=0, description="L0 payload length in bytes")


class ManifestFile(BaseModel):
    """One output file in a bundle, described completely enough to verify and to trace.

    `name`/`path`/`sha256`/`bytes`/`rows` are the wire contract `ArchiveFileOut` projects; `lineage`
    is the extra M1.12 acceptance criterion — the L0 refs this file derived from — kept out of the
    wire-file entry (that model forbids it) and re-attached under the manifest's `lineage` map.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="the file's name within the bundle")
    path: str = Field(description="path relative to the bundle root (== name for a flat bundle)")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="sha256 of the file's bytes")
    bytes: int = Field(ge=0, description="the file's length in bytes")
    rows: int | None = Field(
        default=None, ge=0, description="row count for a tabular file; null otherwise"
    )
    lineage: tuple[L0Lineage, ...] = Field(
        description="the L0 payloads this file was derived from; never empty for a data file"
    )

    def wire_entry(self) -> dict[str, object]:
        """This file as an `ArchiveFileOut`-shaped dict — lineage stripped, for the DB `files`."""
        return {
            "name": self.name,
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "rows": self.rows,
        }


class Manifest(BaseModel):
    """The full description of one daily bundle: its schema version, its files, and their lineage.

    Assumes every listed file has non-empty lineage — a bundle whose provenance cannot be stated is
    not publishable, so the constructor rejects it rather than emit an unverifiable archive.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(description="the bundle/manifest schema version")
    logical_date: date = Field(description="the trading date this bundle is for")
    bundle_path: str = Field(description="the bundle's location relative to the archive root")
    files: tuple[ManifestFile, ...] = Field(description="every data file in the bundle")

    def model_post_init(self, _context: object) -> None:
        """Reject a manifest that fails its own contract: no files, or a file without lineage."""
        if not self.files:
            raise ManifestError(
                f"manifest for {self.logical_date.isoformat()} lists no files; a bundle with "
                "nothing in it is not something to publish"
            )
        orphans = [file.name for file in self.files if not file.lineage]
        if orphans:
            raise ManifestError(
                f"manifest for {self.logical_date.isoformat()} has files with no L0 lineage: "
                f"{', '.join(orphans)}; every output file must map to the L0 refs it derived from "
                "(M1.12 acceptance 3)"
            )

    @property
    def total_bytes(self) -> int:
        """Sum of the listed files' byte sizes — the bundle's data weight, manifest excluded."""
        return sum(file.bytes for file in self.files)

    @property
    def file_count(self) -> int:
        """Number of data files described — the publisher's own count, stored in the row."""
        return len(self.files)

    def to_json(self) -> bytes:
        """Serialize to the canonical on-disk `manifest.json` bytes — deterministic, no clock.

        Sorted keys and a trailing newline so re-deriving the same date from the same L0 yields
        byte-identical manifest bytes, which is what makes `manifest_sha256` a stable bundle id
        rather than a timestamp.
        """
        document = self.model_dump(mode="json")
        return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")

    def db_document(self) -> dict[str, object]:
        """The JSON stored in `archive_bundle.manifest`.

        `files` matches `ArchiveFileOut` (no lineage inside — that model forbids extra keys); the
        lineage is preserved under a top-level `lineage` map so nothing is lost between the on-disk
        manifest and the row `/archives` reads.
        """
        return {
            "schema_version": self.schema_version,
            "files": [file.wire_entry() for file in self.files],
            "lineage": {
                file.name: [ref.model_dump(mode="json") for ref in file.lineage]
                for file in self.files
            },
        }


def manifest_sha256(manifest_bytes: bytes) -> str:
    """The sha256 of the manifest's on-disk bytes, lowercase hex."""
    return hashlib.sha256(manifest_bytes).hexdigest()
