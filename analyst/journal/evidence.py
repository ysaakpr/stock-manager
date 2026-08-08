"""A9: content-addressed evidence snapshots — the exact material a decision was made on.

A journal entry that says *what* was decided and *why* is half the record. The other half is what
the agent was looking at when it decided: the closes, the filing, the headline, the rendered
prompt. §5.7 calls that the evidence snapshot and §8.3.3 requires a decision to be reconstructable
from it, which only holds if the bundle cannot drift after the fact.

So a bundle is addressed by the sha256 of its own canonical bytes. Two consequences follow, and
they are the whole design:

* **The reference verifies itself.** `sha256:<hex>` on the entry is not a pointer into a mutable
  table — re-reading it re-hashes the bytes, and content that changed cannot answer to its own
  name. `get()` raises rather than returning suspect bytes, exactly as L0 does (invariant #1's
  reasoning, one layer up).
* **Identical evidence is stored once.** A T0 sweep that shows the same twenty closes to the same
  case on the same day produces one file however many entries reference it, and storing it twice
  is a verified no-op rather than a rewrite.

Canonical bytes mean UTF-8 JSON with sorted keys and no insignificant whitespace, over pydantic's
JSON dump — so `Decimal` is a string and never a float, and two bundles that differ only in the
order a caller happened to build a mapping hash the same. The bundle deliberately carries **no**
creation timestamp: a clock in the content would give the same evidence a different address on
every run and defeat both the dedup and replay determinism (§8.3.3). *When* it was seen belongs to
the journal entry that references it.

Storage is the filesystem, under `<data_root>/evidence/<aa>/<bb>/<sha256>.json`, for the same
reason prices are not in Postgres (§4.2): the database holds the small transactional record, the
lake holds the bytes. Files are created write-once with `O_EXCL` and mode 0o444, and this module
calls no destructive filesystem primitive at all.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from analyst.journal.models import ISIN_PATTERN, Actor, Money
from dataplatform.config import get_settings
from dataplatform.logging import get_logger

__all__ = [
    "EVIDENCE_DIRNAME",
    "EvidenceBundle",
    "EvidenceChecksumError",
    "EvidenceError",
    "EvidenceItem",
    "EvidenceKind",
    "EvidenceNotFoundError",
    "EvidenceRef",
    "EvidenceStore",
    "canonical_bytes",
    "digest_of",
    "parse_ref",
]

_LOG = get_logger(__name__)

#: Directory under the lake root that holds evidence bundles. Not an L0/L1/L2 layer: a snapshot is
#: neither raw source data nor a derivation of it — it is a record of what was shown, and unlike
#: L2 it is not recomputable from anything.
EVIDENCE_DIRNAME: Final = "evidence"

#: Creation mode: readable by all, writable by none. Defence in depth behind `O_EXCL`.
_READ_ONLY: Final = 0o444

#: How a bundle is named on a journal entry. The algorithm is spelled out in the reference so a
#: record read years later does not depend on knowing which hash was fashionable when it was made.
_REF_PREFIX: Final = "sha256:"


class EvidenceError(Exception):
    """Base for every evidence-store failure, so callers can catch the layer."""


class EvidenceNotFoundError(EvidenceError):
    """No bundle is stored under this reference."""


class EvidenceChecksumError(EvidenceError):
    """Stored bytes do not hash to the reference that names them — the file changed under us."""


class EvidenceKind(StrEnum):
    """What sort of fact one evidence item is.

    Enumerated rather than free text because the evidence pack (§5.7) and the replay engine both
    slice by it: "which decisions were made without a single filing in front of them" is a
    question the journal should be able to answer.
    """

    PRICE = "PRICE"
    """A price, volume or delivery figure from the platform's own L1/L2 (§4.2)."""

    FUNDAMENTAL = "FUNDAMENTAL"
    """A reported financial figure, point-in-time as of `knowable_at` (invariant #7, #8)."""

    FILING = "FILING"
    """An exchange filing or announcement — the text the agent actually read."""

    NEWS = "NEWS"
    """A news item shown to the model. Stored as shown, not as summarized afterwards."""

    CORPORATE_ACTION = "CORPORATE_ACTION"
    """A CA event and its terms (D3)."""

    POSITION = "POSITION"
    """The book as the agent saw it: a holding, its quantity, its weight."""

    POLICY = "POLICY"
    """A clause of the ratified policy set the decision was checked against (§5.2)."""

    RAIL = "RAIL"
    """A rail's threshold and the measured value it was compared with (A8)."""

    STATUS = "STATUS"
    """Data-platform health as of the decision — the §4.4 green check (invariant #10)."""

    THESIS = "THESIS"
    """The inclusion thesis and its break conditions, as evaluated (§5.3)."""


class EvidenceItem(BaseModel):
    """One fact that was in front of the agent, with its provenance.

    The shape is deliberately narrow. `value` is a `Decimal` and `detail` is strings only, so no
    binary float can enter a snapshot and no round-trip can turn ₹1234.55 into ₹1234.5500000001 —
    the evidence must read back exactly as it was shown, or the reconstruction is a near-miss
    rather than a reconstruction. Anything that is genuinely prose goes in `text`.

    `knowable_at` is the point-in-time stamp: when this fact became knowable, as opposed to when
    it is about. It is optional because not every source publishes one, but recording it is what
    lets the PIT leak test (invariant #7) audit a *past* decision rather than only future ones.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EvidenceKind = Field(description="What sort of fact this is.")
    source: str = Field(
        min_length=1,
        description="Where it came from: a Source Register id, 'L1', 'policy_set', 'book'.",
    )
    label: str = Field(min_length=1, description="What it is, e.g. 'close', 'delivery_pct'.")
    isin: str | None = Field(
        default=None, pattern=ISIN_PATTERN, description="Instrument, when it is about one."
    )
    as_of: date | None = Field(default=None, description="The date the fact is about.")
    knowable_at: datetime | None = Field(
        default=None, description="When it became knowable, tz-aware (invariant #7)."
    )
    value: Money | None = Field(default=None, description="The number shown. Decimal, never float.")
    text: str | None = Field(default=None, description="The text shown, verbatim.")
    detail: Mapping[str, str] = Field(
        default_factory=dict, description="Everything else, as strings. No float can enter here."
    )

    @field_validator("knowable_at")
    @classmethod
    def _must_be_aware(cls, value: datetime | None) -> datetime | None:
        """A naive point-in-time stamp is worse than none — it cannot be compared across zones."""
        if value is not None and (value.tzinfo is None or value.tzinfo.utcoffset(value) is None):
            raise ValueError(f"knowable_at must be tz-aware, got naive {value.isoformat()!r}")
        return value

    @field_validator("text")
    @classmethod
    def _must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("text must not be blank; omit it instead")
        return value


class EvidenceBundle(BaseModel):
    """Everything shown to one decision, as one addressable document.

    What it does: collects the items the agent saw, plus the prompt they were rendered into when a
    model was involved, into a value whose canonical bytes are its identity.
    What it assumes: it is complete — a bundle that omits an item the model saw makes the
    reconstruction a lie, which is worse than having no snapshot at all.
    What it never does: carry a timestamp of its own. The same evidence must always hash to the
    same address, or replay is not byte-reproducible (§8.3.3) and nothing dedups.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str | None = Field(
        default=None, min_length=1, description="Case this evidence was gathered for."
    )
    trading_date: date = Field(description="The session the evidence is about, in IST.")
    actor: Actor = Field(description="Which tier or component assembled and saw it.")
    rendered_prompt: str | None = Field(
        default=None,
        description="The exact prompt text sent to the model, when one was called. Verbatim.",
    )
    items: tuple[EvidenceItem, ...] = Field(
        min_length=1, description="The facts themselves, in the order they were presented."
    )

    def canonical_bytes(self) -> bytes:
        """The bytes this bundle is addressed by: UTF-8 JSON, keys sorted, no padding."""
        return canonical_bytes(self.model_dump(mode="json"))

    def ref(self) -> EvidenceRef:
        """The content address of this bundle, computed without storing it."""
        payload = self.canonical_bytes()
        return EvidenceRef(
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            item_count=len(self.items),
            case_id=self.case_id,
            trading_date=self.trading_date,
        )


class EvidenceRef(BaseModel):
    """The identity of one stored bundle — what the journal entry holds.

    Only `sha256` addresses anything; the rest is what an operator reading a journal row needs in
    order to decide whether to fetch the bundle at all, and is checked against the bundle on load
    so a ref that disagrees with its content is a loud failure rather than a misleading summary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="sha256 of the canonical bytes.")
    size_bytes: int = Field(ge=0, description="Length of those bytes.")
    item_count: int = Field(ge=1, description="How many evidence items the bundle holds.")
    case_id: str | None = Field(default=None, description="Case the bundle was gathered for.")
    trading_date: date = Field(description="Session the bundle is about.")

    @property
    def ref(self) -> str:
        """`sha256:<hex>` — the string stored in `decision_journal.evidence_snapshot_ref`."""
        return f"{_REF_PREFIX}{self.sha256}"

    def __str__(self) -> str:
        return self.ref


def canonical_bytes(document: Any) -> bytes:
    """Serialize an already-JSON-safe document to its one canonical byte string.

    Sorted keys and tight separators so that the address depends on the content and nothing else —
    not on the order a caller built a mapping, not on a pretty-printer's indent setting. `NaN` and
    `Infinity` are refused: they are not JSON, and their presence means a float reached a place
    this module has spent some effort keeping floats out of.
    """
    text = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return text.encode("utf-8")


def digest_of(payload: bytes) -> str:
    """sha256 of a byte string, lowercase hex."""
    return hashlib.sha256(payload).hexdigest()


def parse_ref(reference: str) -> str:
    """The hex digest inside a `sha256:<hex>` reference, or raise.

    Refuses anything else rather than best-effort parsing: a reference that is not a content
    address is a pointer to something that could have changed, which is the one thing this module
    exists to make impossible.
    """
    if not reference.startswith(_REF_PREFIX):
        raise ValueError(
            f"{reference!r} is not an evidence reference; expected {_REF_PREFIX}<64 hex digits>"
        )
    digest = reference[len(_REF_PREFIX) :]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{reference!r} does not carry a lowercase hex sha256")
    return digest


class EvidenceStore:
    """Write-once, content-addressed storage for evidence bundles.

    What it does: stores a bundle under the sha256 of its canonical bytes and hands those exact
    bytes back, re-hashed on every read.
    What it assumes: it is the only writer of its tree. Concurrent `put`s of the same bundle are
    safe — the kernel decides who creates the file and the loser verifies rather than overwrites.
    What it never does: modify or delete a stored bundle. There is no such method and no flag that
    would add one: evidence that can be edited after a decision is not evidence (invariant #12).
    """

    __slots__ = ("root",)

    def __init__(self, root: Path | None = None) -> None:
        """Wire a store at `root`, defaulting to `<data_root>/evidence`."""
        self.root = (get_settings().data_root / EVIDENCE_DIRNAME) if root is None else root

    def __repr__(self) -> str:
        return f"{type(self).__name__}(root={str(self.root)!r})"

    def path_of(self, reference: EvidenceRef | str) -> Path:
        """Where a reference's bytes live.

        Two levels of hex fan-out: a decade of daily decisions across several cases is tens of
        thousands of bundles, and one flat directory of them is a directory nobody can list.
        """
        digest = reference.sha256 if isinstance(reference, EvidenceRef) else parse_ref(reference)
        return self.root / digest[:2] / digest[2:4] / f"{digest}.json"

    def put(self, bundle: EvidenceBundle) -> EvidenceRef:
        """Store `bundle` and return its reference.

        Idempotent by construction: the address *is* the content, so a bundle already present is
        verified against its digest and left alone. A stored file that no longer hashes to its own
        name raises `EvidenceChecksumError` instead of being silently replaced — that is damage,
        and repairing it here would destroy the only copy of what a past decision saw.
        """
        payload = bundle.canonical_bytes()
        ref = bundle.ref()
        path = self.path_of(ref)
        path.parent.mkdir(parents=True, exist_ok=True)

        created = _create_exclusively(path, payload)
        if not created:
            stored = path.read_bytes()
            if digest_of(stored) != ref.sha256:
                raise EvidenceChecksumError(
                    f"evidence bundle at {path} no longer hashes to {ref.sha256} — the file has "
                    "been damaged since it was written. Do not reconstruct a decision from it, "
                    "and do not repair it here; the original is unrecoverable and that is a "
                    "finding for the owner."
                )

        _LOG.info(
            "journal.evidence.put",
            sha256=ref.sha256,
            size_bytes=ref.size_bytes,
            items=ref.item_count,
            case_id=ref.case_id,
            trading_date=ref.trading_date.isoformat(),
            actor=bundle.actor.value,
            state="STORED" if created else "DUPLICATE",
        )
        return ref

    def get(self, reference: EvidenceRef | str) -> bytes:
        """The exact bytes stored under `reference`, re-hashed on the way out.

        Verification is not optional: the claim this module makes is that a decision can be
        reconstructed from what was actually shown, and bytes that no longer match their address
        would make that claim false without saying so.
        """
        digest = reference.sha256 if isinstance(reference, EvidenceRef) else parse_ref(reference)
        path = self.path_of(reference)
        try:
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise EvidenceNotFoundError(
                f"no evidence bundle for {_REF_PREFIX}{digest} at {path}"
            ) from exc

        actual = digest_of(payload)
        if actual != digest:
            raise EvidenceChecksumError(
                f"evidence bundle at {path} hashes to {actual} but is named {digest}; the file "
                "changed after it was written and cannot be used to reconstruct a decision"
            )
        return payload

    def load(self, reference: EvidenceRef | str) -> EvidenceBundle:
        """The bundle stored under `reference`, parsed.

        Raises `EvidenceChecksumError` for bytes that do not match their address, and
        `EvidenceError` for bytes that match but will not parse — the second means this store and
        the model that reads it have diverged, which must fail loudly rather than half-load.
        """
        payload = self.get(reference)
        try:
            return EvidenceBundle.model_validate_json(payload)
        except ValidationError as exc:
            raise EvidenceError(
                f"stored bundle {self.path_of(reference)} is not a readable EvidenceBundle: {exc}"
            ) from exc

    def exists(self, reference: EvidenceRef | str) -> bool:
        """Whether a bundle is stored, without reading or verifying it."""
        return self.path_of(reference).is_file()


def _create_exclusively(path: Path, data: bytes) -> bool:
    """Create `path` containing `data`, or report False because it already exists.

    `O_EXCL` is the write-once guarantee itself: the kernel decides who creates the file, so two
    processes snapshotting the same evidence cannot both believe they wrote it and no code path
    here can reach the bytes of a file that already exists. The file is created without write
    permission; the descriptor from the creating call is the only one that ever writes it.

    Never cleans up after a failed write — a short file is evidence of a crash, and this module
    deletes nothing.
    """
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _READ_ONLY)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return True
