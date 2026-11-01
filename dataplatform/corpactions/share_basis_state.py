"""The share-basis guard's verdicts, as the lake keeps them: admitted factors and quarantines.

The guard (`dataplatform.quality.share_basis`) decides, without a human, what to do with each
share-basis change the feeds missed — the AvenuesAI case, a consolidation whose ex-date NSE left
blank and whose ISIN was reissued across a suspension. It reaches one of two verdicts:

* **admitted** — the terms are stated (NSE's book-closure line, or the face values the identity
  master holds for both ISINs of a reissue) and L1's own price step across the event agrees with
  them, so the factor is composed into L2 exactly like a curated row (`AdmittedAction.as_action`,
  under `BC_SOURCE` or `REISSUE_SOURCE`, never a feed's id);
* **quarantined** — something moved the share basis and nothing explains it well enough to adjust
  (terms that disagree with the price, a long-gap step at a clean multiple, any unexplained step
  across a reissue). The ISIN's bars before the step are withheld from every decision-facing
  reader, by the same predicate as the curated unsourced steps (D22):
  `dataplatform.query.default_price_quarantine` reads `windows()`.

This module is only the record: one JSON document under the L2 root (`state_path`), written whole
by the guard and read by the L2 composer (`dataplatform.store.l2`), the L2 overlay and the query
layer's quarantine. It is derived state — recomputable at any time from L0 (the bc lines), L1
(the prices) and the identity tables — so it lives beside L2, not in L0, and a missing file reads
as "nothing admitted, nothing quarantined". Readers re-read it when its bytes change
(`current_state`), so a long-lived process (the scheduler) never acts on a stale verdict.

What it never does: decide anything, read prices, or touch Postgres.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from dataplatform.corpactions.taxonomy import ActionType, FaceValueTerms
from dataplatform.ingest.models import ISIN_PATTERN

if TYPE_CHECKING:
    from dataplatform.ingest.corp_actions import CorporateAction

__all__ = [
    "BC_SOURCE",
    "REISSUE_SOURCE",
    "STATE_DIR",
    "AdmissionRule",
    "AdmittedAction",
    "QuarantineWindow",
    "ShareBasisState",
    "current_state",
    "load_state",
    "state_path",
    "write_state",
]

#: Source ids of the guard's admitted factors — never a feed's, so neither can pass for one.
BC_SOURCE: Final = "nse_pr_bundle_bc"
REISSUE_SOURCE: Final = "share_basis_reissue"

#: Under the L2 root: derived from L0 + L1 + identity, like every L2 dataset.
STATE_DIR: Final = "share_basis"
_STATE_FILE: Final = "state.json"


class AdmissionRule:
    """Why a factor was admitted without a human (string constants; recorded on the row)."""

    #: NSE's bc line states the face values; ex-date stated or derived (T+1); price agrees.
    BC_STATED_TERMS: Final = "BC_STATED_TERMS"
    #: A lineage reissue whose two ISINs' face values differ in the master; price agrees.
    REISSUE_FACE_VALUE: Final = "REISSUE_FACE_VALUE"


class AdmittedAction(BaseModel):
    """One split/consolidation the guard composes into L2, with the evidence that admitted it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN, description="the L2 partition (lineage survivor)")
    filed_against_isin: str | None = Field(default=None, pattern=ISIN_PATTERN)
    ex_date: date
    from_value: Decimal = Field(gt=0)
    to_value: Decimal = Field(gt=0)
    source: str
    rule: str = Field(description="an `AdmissionRule` value")
    raw_text: str = Field(description="the bc purpose, or the master's face values, verbatim")
    knowable_date: date
    record_date: date | None = None
    l0_key: str | None = None
    ex_date_rule: str | None = Field(
        default=None, description="`ExDateRule` when the source left the ex-date blank"
    )
    evidence: str = Field(description="the price check that admitted it, in words")

    def as_action(self) -> CorporateAction:
        """This row as a `CorporateAction` under its own source, for the factor math."""
        from dataplatform.ingest.corp_actions import CorporateAction

        return CorporateAction(
            isin=self.isin,
            filed_against_isin=self.filed_against_isin,
            ex_date=self.ex_date,
            action_type=ActionType.SPLIT,
            terms=FaceValueTerms(from_value=self.from_value, to_value=self.to_value),
            source=self.source,
            raw_text=self.raw_text,
            knowable_date=self.knowable_date,
            record_date=self.record_date,
            source_ref=self.rule,
            l0_key=self.l0_key,
            ex_date_rule=self.ex_date_rule,
        )


class QuarantineWindow(BaseModel):
    """An unexplained share-basis step: bars of `isin` before `first_session` are withheld."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN)
    first_session: date = Field(description="the step's own session: the first admissible bar")
    prev_date: date | None = None
    ratio: str = Field(description="the step as observed, e.g. 'x9.4035'")
    kind: str = Field(description="what kind of step (`share_basis_step` finding kinds)")
    reason: str


class ShareBasisState(BaseModel):
    """The guard's whole verdict at one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    computed_at: datetime | None = None
    admitted: tuple[AdmittedAction, ...] = ()
    quarantined: tuple[QuarantineWindow, ...] = ()

    def admitted_for(self, isins: Iterable[str]) -> tuple[AdmittedAction, ...]:
        """The admitted rows keyed to any of `isins` (a survivor and its lineage chain)."""
        wanted = set(isins)
        return tuple(a for a in self.admitted if a.isin in wanted)

    def windows(self) -> Mapping[str, date]:
        """ISIN → first admissible session; the later step wins where an ISIN has two."""
        out: dict[str, date] = {}
        for q in self.quarantined:
            out[q.isin] = max(q.first_session, out.get(q.isin, q.first_session))
        return out


def state_path(*, data_root: Path | None = None) -> Path:
    """Where the guard's state lives: ``<L2>/share_basis/state.json``."""
    from dataplatform.store.paths import Layer, layer_root

    return layer_root(Layer.L2, data_root=data_root) / STATE_DIR / _STATE_FILE


def load_state(*, data_root: Path | None = None) -> ShareBasisState:
    """Read the state; an absent file is the empty state (nothing admitted, nothing withheld)."""
    path = state_path(data_root=data_root)
    if not path.is_file():
        return ShareBasisState()
    return ShareBasisState.model_validate_json(path.read_bytes())


def write_state(state: ShareBasisState, *, data_root: Path | None = None) -> Path:
    """Write the state whole via a staging rename, sorted, so equal verdicts are equal bytes."""
    path = state_path(data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = state.model_copy(
        update={
            "admitted": tuple(
                sorted(state.admitted, key=lambda a: (a.isin, a.ex_date, a.source, a.rule))
            ),
            "quarantined": tuple(
                sorted(state.quarantined, key=lambda q: (q.isin, q.first_session, q.kind))
            ),
        }
    )
    body = json.dumps(json.loads(ordered.model_dump_json()), indent=1, sort_keys=True) + "\n"
    staging = path.with_name(f".{path.name}.partial")
    staging.write_text(body, encoding="utf-8")
    staging.replace(path)
    return path


_CACHE: dict[Path, tuple[tuple[int, int], ShareBasisState]] = {}


def current_state(*, data_root: Path | None = None) -> ShareBasisState:
    """`load_state`, re-read only when the file's size or mtime changed since the last read."""
    path = state_path(data_root=data_root)
    try:
        stat = path.stat()
    except FileNotFoundError:
        _CACHE.pop(path, None)
        return ShareBasisState()
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    state = load_state(data_root=data_root)
    _CACHE[path] = (key, state)
    return state
