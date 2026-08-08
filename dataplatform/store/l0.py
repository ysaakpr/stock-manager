"""L0: the immutable raw store (invariant #1, EXECUTION_PLAN §4.2).

Every byte this platform ever fetched lands here first, exactly as the source served it, with a
sha256 and a sidecar recording where it came from and when. Nothing downstream is authoritative:
L1 is a normalization of L0 and L2 is a derivation of L1, so a parser bug, a schema change or a
source going dark costs a recompute rather than history. That guarantee is only worth something
if the bytes cannot drift, which is why this module is write-once **by construction** rather than
by convention:

* payloads and sidecars are created with `O_CREAT | O_EXCL` — the kernel refuses the second
  create, so there is no check-then-write window for two concurrent fetchers to lose,
* they are created mode `0o444`, so a later bug (or a careless shell) has to escalate before it
  can even attempt a rewrite,
* and the module calls no destructive filesystem primitive at all — no unlink, no rename, no
  truncate, no re-open for writing. `tests/unit/test_l0.py` enforces that by parsing this file.

Consequences a caller must know about:

* `put` is idempotent for identical bytes and raises `L0ImmutabilityError` for different bytes
  under the same key. It never overwrites, and there is no force flag: a genuine conflict means
  the source restated a file we already have, which is a fact about the world worth stopping for.
* A crash midway through a write leaves a short payload behind. This module will not clean that
  up — `verify_checksums()` reports it and a human resolves it, because deleting or rewriting L0
  is reserved to the owner (AGENTIC_CONTEXT §3.10) precisely so that "it looked corrupt" can
  never become an agent's excuse for destroying evidence.
* `get` re-checksums on every read, so a corrupt file cannot silently become an L1 row.

Time comes from an injected `Clock` (B10): `fetched_at` is the only mutable-looking field here and
it must be reproducible in a replay.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dataplatform.clock import Clock
from dataplatform.config import get_settings
from dataplatform.logging import get_logger
from dataplatform.store.paths import Layer, l0_meta_path, l0_path, layer_root

__all__ = [
    "DEFAULT_CONTENT_TYPE",
    "L0ChecksumError",
    "L0Defect",
    "L0DefectKind",
    "L0Error",
    "L0ImmutabilityError",
    "L0MetadataError",
    "L0NotFoundError",
    "L0Ref",
    "L0Store",
    "L0VerificationReport",
]

_LOG = get_logger(__name__)

#: Suffix of the sidecar written beside every payload (`paths.l0_meta_path` builds the path).
_META_SUFFIX: Final = ".meta.json"

#: Creation mode for everything in L0: readable by all, writable by none. Defence in depth behind
#: `O_EXCL` — a bug that reaches for an existing payload hits EACCES before it reaches the bytes.
_READ_ONLY: Final = 0o444

#: Streaming chunk for checksums. Bhavcopies are ~200 kB but a decade of them is not, and the
#: verification sweep must not need a whole file in memory to decide it is intact.
_CHUNK_BYTES: Final = 1 << 20

#: What a source's `Content-Type` becomes when it did not send a usable one. Recorded rather than
#: guessed from the extension: L0 stores what the source said, including that it said nothing.
DEFAULT_CONTENT_TYPE: Final = "application/octet-stream"


class L0Error(Exception):
    """Base for every L0 failure, so callers can catch the layer without catching the world."""


class L0ImmutabilityError(L0Error):
    """A put would have changed bytes that already exist under a key. Invariant #1 forbids it."""


class L0ChecksumError(L0Error):
    """Stored bytes do not match the sha256 recorded for them — the file changed under us."""


class L0NotFoundError(L0Error):
    """No payload (or no sidecar) exists for the requested key."""


class L0MetadataError(L0Error):
    """A sidecar exists but is not a readable `L0Ref` document."""


class L0Ref(BaseModel):
    """The identity of one raw payload: where it came from, when, and what it hashes to.

    This is the only handle other modules hold on L0 — the fetcher (D1) returns one instead of
    parsed data, and every L1 row can name the ref it was derived from. It is also, verbatim, the
    on-disk sidecar: one representation, so a record read back years later cannot disagree with
    the object that wrote it.

    Frozen and `extra="forbid"`: a ref that could be edited in flight, or that quietly absorbed an
    unknown field from a sidecar written by different code, would make the checksum meaningless.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(description="source id from the Source Register, e.g. 'nse_bhavcopy'")
    logical_date: date = Field(description="the trading/logical date this payload is *about*")
    filename: str = Field(description="the source's own filename, kept exactly as served")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="sha256 of the payload, lowercase")
    size_bytes: int = Field(ge=0, description="payload length in bytes")
    fetched_at: datetime = Field(description="tz-aware instant the fetch completed (injected)")
    content_type: str = Field(description="Content-Type as the source declared it")

    @field_validator("fetched_at")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        """A naive instant is an error, not an assumption about the host's locale (B10)."""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(f"fetched_at must be tz-aware, got naive {value.isoformat()!r}")
        return value

    @property
    def key(self) -> str:
        """`source/date/filename` — the write-once key, for logs and error messages."""
        return f"{self.source}/{self.logical_date.isoformat()}/{self.filename}"


class L0DefectKind(StrEnum):
    """What `verify_checksums` can find wrong with a stored record."""

    CHECKSUM_MISMATCH = "checksum_mismatch"
    MISSING_PAYLOAD = "missing_payload"
    ORPHAN_PAYLOAD = "orphan_payload"
    UNREADABLE_METADATA = "unreadable_metadata"


class L0Defect(BaseModel):
    """One thing wrong with one file, named precisely enough to act on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: L0DefectKind
    path: Path
    detail: str


class L0VerificationReport(BaseModel):
    """Result of a sweep: how many records were checked and everything found wrong."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checked: int = Field(ge=0, description="records whose payload was re-hashed")
    defects: tuple[L0Defect, ...] = ()

    @property
    def ok(self) -> bool:
        """True when every record checked matched its recorded checksum."""
        return not self.defects


class L0Store:
    """Write-once, checksummed access to the raw layer.

    What it does: stores fetched bytes under `L0/{source}/{yyyy}/{mm}/{filename}` with a sidecar,
    hands them back verified, iterates them by source and date range, and sweeps them for damage.
    What it assumes: it is the only writer of that tree — other processes may read it freely, and
    concurrent `put`s of the same key are safe, but nothing else creates files there.
    What it never does: modify or delete a stored payload, for any reason, including corruption
    (AGENTIC_CONTEXT §3.10). Repair is a human action; this class only reports.
    """

    def __init__(self, *, clock: Clock, data_root: Path | None = None) -> None:
        """Wire a store at `data_root` (default: the configured lake) with an injected clock."""
        self._clock = clock
        self.data_root = get_settings().data_root if data_root is None else data_root

    def __repr__(self) -> str:
        return f"{type(self).__name__}(root={str(self.root)!r})"

    @property
    def root(self) -> Path:
        """The `L0` directory this store owns."""
        return layer_root(Layer.L0, data_root=self.data_root)

    # ── write ────────────────────────────────────────────────────────────────────────────────

    def put(
        self,
        source: str,
        logical_date: date,
        filename: str,
        payload: bytes,
        *,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> L0Ref:
        """Store `payload` under `(source, logical_date, filename)` and return its ref.

        Write-once with two outcomes and no third: identical bytes for an existing key are a
        no-op returning the *original* ref (original `fetched_at` included — the record of when
        this content was first seen is itself immutable), and different bytes raise
        `L0ImmutabilityError` having touched nothing.

        Assumes the caller has the complete payload in memory, which is what an EOD file is, and
        that `filename` distinguishes the logical dates it is used for: the layout partitions by
        month, so the same filename twice in one month is one key however many dates the caller
        meant (that collision raises rather than merging). Every real source dates its filenames.
        Never overwrites, never deletes, and offers no flag that would.
        """
        digest = hashlib.sha256(payload).hexdigest()
        payload_path = l0_path(source, logical_date, filename, data_root=self.data_root)
        payload_path.parent.mkdir(parents=True, exist_ok=True)

        candidate = L0Ref(
            source=source,
            logical_date=logical_date,
            filename=filename,
            sha256=digest,
            size_bytes=len(payload),
            fetched_at=self._clock.now(),
            content_type=content_type,
        )
        # Sidecar first: it is the record that a key is claimed, and claiming it exclusively is
        # what makes the conflict check race-free. A crash between the two leaves a claim with no
        # payload, which the next put of the same bytes completes and the sweep reports meanwhile.
        ref = self._claim_key(l0_meta_path(payload_path), candidate)
        stored = self._store_payload(payload_path, payload, digest)

        _LOG.info(
            "l0.put",
            source=ref.source,
            logical_date=ref.logical_date.isoformat(),
            filename=ref.filename,
            sha256=ref.sha256,
            size_bytes=ref.size_bytes,
            content_type=ref.content_type,
            state="STORED" if stored else "DUPLICATE",
        )
        return ref

    # ── read ─────────────────────────────────────────────────────────────────────────────────

    def path_of(self, ref: L0Ref) -> Path:
        """Filesystem path of a ref's payload."""
        return l0_path(ref.source, ref.logical_date, ref.filename, data_root=self.data_root)

    def meta_path_of(self, ref: L0Ref) -> Path:
        """Filesystem path of a ref's sidecar."""
        return l0_meta_path(self.path_of(ref))

    def get(self, ref: L0Ref) -> bytes:
        """Return the payload for `ref`, re-checksummed on the way out.

        Verification is not optional here: the whole claim of this layer is that L1 and L2 are
        re-derivable from bytes that have not changed, and a silent bit flip would propagate into
        every derived table. Raises `L0ChecksumError` instead of returning suspect bytes.
        """
        path = self.path_of(ref)
        try:
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise L0NotFoundError(f"no L0 payload for {ref.key} at {path}") from exc

        digest = hashlib.sha256(payload).hexdigest()
        if digest != ref.sha256:
            raise L0ChecksumError(
                f"L0 payload {ref.key} hashes to {digest} but its ref records {ref.sha256}; "
                f"the file at {path} changed after it was written — do not derive from it, and "
                "do not repair it here (AGENTIC_CONTEXT §3.10)"
            )
        return payload

    def open(self, ref: L0Ref) -> BinaryIO:
        """Open a ref's payload for streaming reads.

        For payloads too large to want in memory (a zip handed straight to `zipfile`). Unlike
        `get`, this cannot verify the checksum up front — the caller reads the bytes, not this
        method — so use `get` when the content is small enough, or `verify_checksums` first.
        """
        path = self.path_of(ref)
        try:
            return path.open("rb")
        except FileNotFoundError as exc:
            raise L0NotFoundError(f"no L0 payload for {ref.key} at {path}") from exc

    def ref_for(self, source: str, logical_date: date, filename: str) -> L0Ref:
        """Load the stored ref for a key, without needing the one `put` returned."""
        path = l0_path(source, logical_date, filename, data_root=self.data_root)
        return self._read_meta(l0_meta_path(path))

    def exists(self, source: str, logical_date: date, filename: str) -> bool:
        """Whether a payload is already stored for a key — the cheap "skip this fetch" check."""
        return l0_path(source, logical_date, filename, data_root=self.data_root).is_file()

    def iter_refs(
        self,
        source: str | None = None,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> Iterator[L0Ref]:
        """Yield stored refs for a source and an inclusive logical-date range.

        Ordered by source, then date, then filename, and stable across runs: a backfill that
        replays L0 must see the same files in the same order every time. Omitting `source`
        iterates every source; omitting a bound leaves that side open.
        """
        if start is not None and end is not None and start > end:
            raise ValueError(f"empty date range: start {start.isoformat()} > end {end.isoformat()}")

        for month_dir in self._iter_month_dirs(source, start, end):
            for meta_path in sorted(month_dir.glob(f"*{_META_SUFFIX}")):
                ref = self._read_meta(meta_path)
                if (start is None or ref.logical_date >= start) and (
                    end is None or ref.logical_date <= end
                ):
                    yield ref

    # ── integrity ────────────────────────────────────────────────────────────────────────────

    def verify_checksums(
        self,
        source: str | None = None,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> L0VerificationReport:
        """Re-hash stored payloads and report everything that does not match its record.

        Covers the four ways a raw tree can be wrong: a payload whose bytes no longer hash to the
        recorded sha256, a sidecar whose payload is gone, a payload with no sidecar (an
        interrupted write, or a file someone dropped in by hand), and a sidecar that will not
        parse. Returns findings rather than raising, because an operator sweeping ten years of
        files wants the whole list — and repairs nothing, by design (§3.10).
        """
        defects: list[L0Defect] = []
        checked = 0

        for month_dir in self._iter_month_dirs(source, start, end):
            claimed: set[str] = set()
            for meta_path in sorted(month_dir.glob(f"*{_META_SUFFIX}")):
                claimed.add(meta_path.name.removesuffix(_META_SUFFIX))
                try:
                    ref = self._read_meta(meta_path)
                except L0MetadataError as exc:
                    defects.append(
                        L0Defect(
                            kind=L0DefectKind.UNREADABLE_METADATA, path=meta_path, detail=str(exc)
                        )
                    )
                    continue
                if (start is not None and ref.logical_date < start) or (
                    end is not None and ref.logical_date > end
                ):
                    continue
                checked += 1
                defect = self._verify_one(ref)
                if defect is not None:
                    defects.append(defect)

            defects.extend(self._orphans(month_dir, claimed))

        for defect in defects:
            _LOG.error("l0.verify.defect", kind=defect.kind.value, path=str(defect.path))
        _LOG.info("l0.verify", checked=checked, defects=len(defects), source=source)
        return L0VerificationReport(checked=checked, defects=tuple(defects))

    def _verify_one(self, ref: L0Ref) -> L0Defect | None:
        """Re-hash one payload against its ref."""
        path = self.path_of(ref)
        if not path.is_file():
            return L0Defect(
                kind=L0DefectKind.MISSING_PAYLOAD,
                path=path,
                detail=f"{ref.key} has a sidecar but no payload",
            )
        digest = _digest_of(path)
        if digest != ref.sha256:
            return L0Defect(
                kind=L0DefectKind.CHECKSUM_MISMATCH,
                path=path,
                detail=f"{ref.key} hashes to {digest}, sidecar records {ref.sha256}",
            )
        return None

    @staticmethod
    def _orphans(month_dir: Path, claimed: set[str]) -> Iterator[L0Defect]:
        """Payload files in `month_dir` that no sidecar claims."""
        for path in sorted(month_dir.iterdir()):
            if path.is_file() and not path.name.endswith(_META_SUFFIX) and path.name not in claimed:
                yield L0Defect(
                    kind=L0DefectKind.ORPHAN_PAYLOAD,
                    path=path,
                    detail="payload has no .meta.json sidecar — interrupted write, or not ours",
                )

    # ── internals ────────────────────────────────────────────────────────────────────────────

    def _claim_key(self, meta_path: Path, candidate: L0Ref) -> L0Ref:
        """Claim a key by creating its sidecar, or return the ref that already holds it.

        Raises if the incumbent records a different checksum: same key, different bytes is the
        conflict invariant #1 exists to stop.
        """
        document = candidate.model_dump_json(indent=2) + "\n"
        if _create_exclusively(meta_path, document.encode("utf-8")):
            return candidate

        existing = self._read_meta(meta_path)
        if existing.logical_date != candidate.logical_date:
            # The layout partitions by month (paths.l0_dir), so source + filename + month is the
            # physical key while the caller thinks in days. Two dates in one month sharing a
            # filename is therefore a collision, and returning the incumbent would hand back a ref
            # dated to the wrong day. Real sources put the date in the filename; one that does not
            # needs a disambiguating name from its fetcher, not a silent merge here.
            raise L0ImmutabilityError(
                f"{candidate.filename!r} from {candidate.source} is already stored for "
                f"{existing.logical_date.isoformat()}, and L0 partitions by month, so storing it "
                f"for {candidate.logical_date.isoformat()} would collide with it. Give the "
                "payload a filename that distinguishes the two dates."
            )
        if existing.sha256 != candidate.sha256:
            raise L0ImmutabilityError(
                f"L0 key {candidate.key} already holds sha256 {existing.sha256} "
                f"({existing.size_bytes} bytes, fetched {existing.fetched_at.isoformat()}); "
                f"refusing to store different content with sha256 {candidate.sha256}. L0 is "
                "write-once (invariant #1) and there is no override — if the source genuinely "
                "restated this file, that is a finding for the owner (AGENTIC_CONTEXT §3.10)."
            )
        return existing

    @staticmethod
    def _store_payload(path: Path, payload: bytes, digest: str) -> bool:
        """Create the payload file if absent; otherwise confirm the stored bytes are identical.

        Returns True when this call wrote the file. Never writes over existing bytes: the
        conflicting-content case reaches here only when the sidecar agrees but the file on disk
        does not, which is damage rather than a new fetch, so it raises.
        """
        if _create_exclusively(path, payload):
            return True

        stored = _digest_of(path)
        if stored != digest:
            raise L0ImmutabilityError(
                f"payload at {path} hashes to {stored}, not the {digest} being stored, while its "
                "sidecar records the latter — the stored file has been damaged. L0 is never "
                "overwritten; run verify_checksums() and escalate (AGENTIC_CONTEXT §3.10)."
            )
        return False

    @staticmethod
    def _read_meta(meta_path: Path) -> L0Ref:
        """Parse a sidecar into an `L0Ref`, failing loudly on anything unexpected."""
        try:
            raw = meta_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise L0NotFoundError(f"no L0 sidecar at {meta_path}") from exc
        try:
            return L0Ref.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise L0MetadataError(f"sidecar {meta_path} is not a readable L0Ref: {exc}") from exc

    def _iter_month_dirs(
        self, source: str | None, start: date | None, end: date | None
    ) -> Iterator[Path]:
        """Yield `L0/{source}/{yyyy}/{mm}` directories overlapping a range, in sorted order.

        The layout puts the logical date's year and month in the path, so a range prunes whole
        directories without reading a sidecar — which is what keeps a ten-year sweep of one
        source from opening every other source's files.
        """
        root = self.root
        if not root.is_dir():
            return
        first = None if start is None else _month_index(start.year, start.month)
        last = None if end is None else _month_index(end.year, end.month)

        for source_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            if source is not None and source_dir.name != source:
                continue
            for year_dir in sorted(path for path in source_dir.iterdir() if path.is_dir()):
                year = _as_int(year_dir.name)
                if year is None:
                    continue
                for month_dir in sorted(path for path in year_dir.iterdir() if path.is_dir()):
                    month = _as_int(month_dir.name)
                    if month is None:
                        continue
                    index = _month_index(year, month)
                    if (first is None or index >= first) and (last is None or index <= last):
                        yield month_dir


def _create_exclusively(path: Path, data: bytes) -> bool:
    """Create `path` containing `data`, or report False because it already exists.

    `O_EXCL` is the write-once guarantee itself: the kernel, not this code, decides who wins, so
    two fetchers racing on one key cannot both believe they created it, and no code path here can
    reach the bytes of a file that already exists. The file is created without write permission,
    the descriptor from the creating call being the only one that ever writes it.

    Never cleans up after a failed write: a short file left by a crash is evidence, and deleting
    L0 is reserved to the owner (AGENTIC_CONTEXT §3.10). `verify_checksums()` will report it.
    """
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _READ_ONLY)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)
    return True


def _fsync_directory(directory: Path) -> None:
    """Flush the directory entry, so a crash cannot lose a file whose bytes are already durable."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _digest_of(path: Path) -> str:
    """sha256 of a file, read in chunks so a sweep never needs a whole payload in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _month_index(year: int, month: int) -> int:
    """A comparable ordinal for a (year, month), for pruning directories against a date range."""
    return year * 12 + month


def _as_int(name: str) -> int | None:
    """Parse a numeric directory name; None for anything else, which is not part of the layout."""
    return int(name) if name.isdigit() else None
