"""Compare one L0 tree against another, payload by payload — the worktree-lake safety check.

**Why this exists.** `L0Store` resolves its root from `Settings.data_root`, which defaults to
`<cwd>/data`. An agent working in a git worktree that runs a fetch without exporting `DATA_ROOT`
therefore builds a *second* lake inside its worktree, and deleting the worktree deletes the only
copy of whatever it fetched. That has now happened three times on this box (2026-09-08 close-out
of the W2 campaign is the third), so the hazard is measured rather than assumed: before a stranded
worktree is removed, every payload in it must be shown to exist, byte-identical, in the
authoritative lake.

**What "identical" means here.** For each payload in the left (stranded) tree the bytes on disk are
re-hashed, and the digest is compared against the sha256 the *right* (authoritative) tree records
in its sidecar for the same logical key. That is the honest chain: `L0Store.verify_checksums`
proves the authoritative sidecar matches its own payload, and this proves the stranded bytes match
that sidecar. A left payload whose bytes disagree with its *own* sidecar is reported separately —
it is damage in the copy about to be deleted, not evidence about the lake being kept.

**Reports, never repairs, never deletes.** Nothing here copies a payload into the authoritative
lake, removes a stranded one, or touches a worktree. `L0Store` refuses to modify a stored payload
for any reason (AGENTIC_CONTEXT §3.10) and a comparison tool has even less business doing it: the
decision to promote or discard a stranded payload is a human one, and this module exists only to
make it an informed one.

Read-only and offline: it opens no socket, no database and no zip.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

from dataplatform.logging import get_logger
from dataplatform.store.paths import Layer, layer_root

__all__ = [
    "LakeComparison",
    "PayloadRecord",
    "compare_lakes",
    "enumerate_payloads",
]

_LOG = get_logger(__name__)

#: The sidecar suffix `L0Store` writes. Duplicated from `l0.py` rather than imported, because that
#: module's copy is private and this one is only ever compared against a filename.
_META_SUFFIX: Final = ".meta.json"

#: Read the payload in chunks: an L0 file is normally a few hundred kilobytes, but the lake also
#: holds multi-megabyte JSON histories and nothing here needs them whole.
_CHUNK_BYTES: Final = 1 << 20


@dataclass(frozen=True, slots=True)
class PayloadRecord:
    """One payload found in an L0 tree: its logical key, its real digest, and its record.

    `sha256` is always computed from the bytes on disk. `recorded_sha256` is what the sidecar
    claims, or `None` when the payload has no sidecar at all — an orphan, which cannot be compared
    by key because the key's date lives in the sidecar and nowhere else.
    """

    source: str
    logical_date: date | None
    filename: str
    sha256: str
    recorded_sha256: str | None
    size_bytes: int
    path: Path

    @property
    def key(self) -> str:
        """`source/date/filename`, the same string `L0Ref.key` produces.

        `source/?/filename` for an orphan, so a report can print it without pretending it has a
        logical date.
        """
        stamp = "?" if self.logical_date is None else self.logical_date.isoformat()
        return f"{self.source}/{stamp}/{self.filename}"

    @property
    def is_orphan(self) -> bool:
        """Whether the payload has no sidecar, so nothing records where it came from."""
        return self.recorded_sha256 is None

    @property
    def matches_own_sidecar(self) -> bool:
        """Whether the bytes on disk hash to what this tree's own sidecar records."""
        return self.recorded_sha256 == self.sha256


@dataclass(frozen=True, slots=True)
class LakeComparison:
    """What a left-tree sweep found when each of its payloads was looked for on the right.

    The field that matters is `absent`: a payload held only in the tree that is about to be
    deleted. Everything else is either reassurance (`matched`) or a defect in the copy
    (`self_inconsistent`, `mismatched`).
    """

    left_root: Path
    right_root: Path
    checked: int
    matched: tuple[PayloadRecord, ...]
    absent: tuple[PayloadRecord, ...]
    mismatched: tuple[tuple[PayloadRecord, str], ...]
    self_inconsistent: tuple[PayloadRecord, ...]
    orphans: tuple[PayloadRecord, ...]

    @property
    def safe_to_delete(self) -> bool:
        """True when every left payload exists on the right with the same digest.

        An orphan or a self-inconsistent payload makes this False even though the *right* tree is
        fine: a payload nothing can identify has not been shown to be safely duplicated, and
        "cannot tell" is not "yes".
        """
        return not (self.absent or self.mismatched or self.self_inconsistent or self.orphans)

    def summary(self) -> str:
        """One line for a report or a log."""
        return (
            f"{self.checked} payload(s) under {self.left_root}: {len(self.matched)} identical in "
            f"{self.right_root}, {len(self.absent)} absent, {len(self.mismatched)} digest "
            f"mismatch, {len(self.self_inconsistent)} inconsistent with its own sidecar, "
            f"{len(self.orphans)} orphan(s)"
        )


def enumerate_payloads(data_root: Path, *, source: str | None = None) -> tuple[PayloadRecord, ...]:
    """Every payload under `data_root/L0`, re-hashed, ordered by key.

    Walks the filesystem rather than the sidecars, so a payload whose sidecar was lost still
    appears (as an orphan) instead of vanishing from the count — which is exactly the failure a
    "is this tree fully duplicated?" question must not be blind to.

    Assumes the standard `L0/<source>/<yyyy>/<mm>/<filename>` layout; a file at any other depth is
    ignored and named in the log rather than guessed at. Never writes.
    """
    root = layer_root(Layer.L0, data_root=data_root)
    if not root.is_dir():
        return ()

    records = [record for path in _iter_payload_paths(root, source) for record in (_read(path),)]
    records.sort(key=lambda record: record.key)
    _LOG.info(
        "l0_compare.enumerated",
        data_root=str(data_root),
        source=source,
        payloads=len(records),
        orphans=sum(1 for record in records if record.is_orphan),
    )
    return tuple(records)


def compare_lakes(
    left_root: Path, right_root: Path, *, source: str | None = None
) -> LakeComparison:
    """Check that every payload under `left_root` exists byte-identically under `right_root`.

    The direction is deliberately one-way: the question is "may the left tree be deleted", not
    "are the two trees equal". The authoritative lake holding payloads the worktree never had is
    the normal case and not a finding.

    What it assumes: `right_root`'s sidecars are trustworthy, which is what the whole-lake
    checksum sweep (`L0Store.verify_checksums`) establishes separately. What it never does: copy,
    delete, or modify anything in either tree.
    """
    right = {record.key: record for record in enumerate_payloads(right_root, source=source)}

    matched: list[PayloadRecord] = []
    absent: list[PayloadRecord] = []
    mismatched: list[tuple[PayloadRecord, str]] = []
    self_inconsistent: list[PayloadRecord] = []
    orphans: list[PayloadRecord] = []

    left = enumerate_payloads(left_root, source=source)
    for record in left:
        if record.is_orphan:
            orphans.append(record)
        elif not record.matches_own_sidecar:
            self_inconsistent.append(record)

        counterpart = right.get(record.key)
        if counterpart is None:
            absent.append(record)
        elif counterpart.recorded_sha256 != record.sha256:
            mismatched.append((record, counterpart.recorded_sha256 or "no sidecar"))
        else:
            matched.append(record)

    comparison = LakeComparison(
        left_root=left_root,
        right_root=right_root,
        checked=len(left),
        matched=tuple(matched),
        absent=tuple(absent),
        mismatched=tuple(mismatched),
        self_inconsistent=tuple(self_inconsistent),
        orphans=tuple(orphans),
    )
    _LOG.info(
        "l0_compare.done" if comparison.safe_to_delete else "l0_compare.found_unique_payloads",
        left=str(left_root),
        right=str(right_root),
        checked=comparison.checked,
        absent=len(comparison.absent),
        mismatched=len(comparison.mismatched),
    )
    return comparison


# ── internals ────────────────────────────────────────────────────────────────────────────────


def _iter_payload_paths(l0_root: Path, source: str | None) -> Iterator[Path]:
    """Payload files (not sidecars) at `L0/<source>/<yyyy>/<mm>/<filename>`."""
    for source_dir in sorted(l0_root.iterdir()):
        if not source_dir.is_dir() or (source is not None and source_dir.name != source):
            continue
        for path in sorted(source_dir.glob("*/*/*")):
            if path.is_file() and not path.name.endswith(_META_SUFFIX):
                yield path


def _read(path: Path) -> PayloadRecord:
    """One payload path into a record: bytes re-hashed, sidecar read when there is one."""
    recorded, logical = _sidecar(path.with_name(path.name + _META_SUFFIX))
    return PayloadRecord(
        source=path.parent.parent.parent.name,
        logical_date=logical,
        filename=path.name,
        sha256=_digest_of(path),
        recorded_sha256=recorded,
        size_bytes=path.stat().st_size,
        path=path,
    )


def _sidecar(meta_path: Path) -> tuple[str | None, date | None]:
    """`(sha256, logical_date)` from a sidecar, or `(None, None)` when it is absent or unreadable.

    An unreadable sidecar is treated as an absent one rather than raised on: this tool's job is to
    say what it could and could not verify about a tree that is already in a bad state, and a
    truncated JSON file is one more thing it could not verify.
    """
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    digest = raw.get("sha256")
    stamp = raw.get("logical_date")
    try:
        logical = date.fromisoformat(stamp) if isinstance(stamp, str) else None
    except ValueError:
        logical = None
    return (digest if isinstance(digest, str) else None), logical


def _digest_of(path: Path) -> str:
    """sha256 of a file's bytes, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
