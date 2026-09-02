"""A4: the §5.3 thesis object — a falsifiable, versioned, ratifiable core-sleeve document.

§5.3 sketches the thesis as: a `driver`, a `theme_purity`, the `expected_evidence` that would
confirm it, and a list of `break_conditions` — each *typed* (fundamental / structural / integrity),
each carrying the *tier* it is evaluated at (T0 mechanical, T1 evidential), and each stating a
condition that would falsify the thesis. This module is that object, and it enforces three rules
the plan states rather than leaving them to the caller:

* **A break condition must be falsifiable.** §5.3 and the task are explicit: "falsifiable is a hard
  requirement, not an aspiration". A condition that cannot be mechanically or evidentially
  evaluated — "the story stops working", "results disappoint", "sentiment turns" — is rejected the
  moment a `BreakCondition` is constructed, with a reason naming what is wrong. The check lives in
  the model validator, so it is un-bypassable (`enforce it in code, not in documentation`); there
  is no way to hold an unfalsifiable break condition in memory, let alone ratify one.

* **A thesis is a core-sleeve artifact.** §5.5 draws the line: core holdings carry a ratified
  thesis, tactical positions carry a journaled lightweight rationale instead. So a `Thesis`
  refuses any sleeve but `CORE`; the tactical rationale is a different, lighter object
  (`TacticalRationale`) that is journaled, never ratified.

* **Editing a ratified thesis is a new version, not a mutation.** Every model is `frozen`, so an
  assignment to a ratified thesis raises. A change is `revise()`, which returns version N+1 in
  `PROPOSAL` carrying `supersedes_version`, dropping the ratification, and leaving version N
  exactly as it was — the same governance shape `analyst.cases.policies.PolicySet` uses for policy
  sets, because a thesis is ratified under the same act (§5.1) and pinned by the same
  content-hash ratification record (`analyst.cases.policies.Ratification`).

Money would be `Decimal` here if there were any (CLAUDE.md); `theme_purity` is a 0..1 score and is
carried as an exact `Decimal` for the same reason percentages are in `policies.py` — a content hash
over a `float` is not reproducible, and `canonical_bytes` refuses `NaN`. Nothing here reads a
clock, a database or the network: a ratification's timestamp is supplied by the caller from an
injected `Clock` (B10).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# The governance artifact is shared, not reimplemented: a thesis is ratified by the same act and
# pinned by the same content-hash record as a policy set (M5.3), so it reuses that type verbatim.
from analyst.cases.policies import Ratification, RatificationKind
from analyst.journal import Sleeve, canonical_bytes, digest_of

__all__ = [
    "THESIS_CONTENT_FIELDS",
    "BreakCondition",
    "BreakConditionType",
    "EvaluationTier",
    "Purity",
    "Ratification",
    "RatificationKind",
    "Sleeve",
    "TacticalRationale",
    "Thesis",
    "ThesisError",
    "ThesisRatificationMismatchError",
    "ThesisStatus",
    "ThesisVersionError",
    "UnfalsifiableBreakConditionError",
    "assert_falsifiable",
    "thesis_digest",
]


def _reject_float(value: Any) -> Any:
    """Refuse a `float` where an exact decimal is required (the `policies.py` rule, for purity).

    `theme_purity` is content that gets hashed; a `float` coerced to `Decimal` reads back with a
    binary-floating-point tail, so `Decimal("0.6")` and `0.6` would hash to different theses.
    Purity is written `Decimal("0.6")` or `"0.6"`, never `0.6`.
    """
    if isinstance(value, float):
        raise ValueError(
            f"theme_purity must be an exact decimal, got float {value!r}; pass a Decimal or a "
            "string (a content hash over a float is not reproducible)"
        )
    return value


#: A theme-purity score in 0..1 (§5.3's `theme_purity`), exact so a thesis hashes reproducibly.
Purity = Annotated[Decimal, BeforeValidator(_reject_float)]

#: ISO 6166 shape, as everywhere else in the codebase. Shape only — the check digit is D2's job.
ISIN_PATTERN: Final = r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"


class ThesisError(Exception):
    """Base for every thesis failure, so a caller can catch the module.

    Deliberately not a `ValueError`: raised from inside a pydantic validator it must *not* be
    wrapped into a `ValidationError`, because the whole point is that the caller sees a typed,
    reasoned refusal (pydantic re-wraps only `ValueError`/`AssertionError`).
    """


class UnfalsifiableBreakConditionError(ThesisError):
    """A break condition was drafted that cannot be mechanically or evidentially evaluated.

    Carries the reason — which word or missing anchor made it unfalsifiable — because "rejected at
    draft time with a reason" (acceptance) is only useful if the reason tells the drafter what to
    fix.
    """


class ThesisVersionError(ThesisError):
    """A version was built or a transition attempted that the versioning rules do not permit."""


class ThesisRatificationMismatchError(ThesisError):
    """A ratification was attached to thesis content other than what it was granted for.

    Same guarantee as `policies.RatificationMismatchError`: a ratification that could be moved onto
    an edited thesis ratifies nothing, and the governance model (§5.1, decisions #4/#5/#9) rests on
    it being un-movable.
    """


# ── the break-condition vocabulary (§5.3) ──────────────────────────────────────────────────────


class BreakConditionType(StrEnum):
    """What kind of thing would break the thesis (§5.3's `type`).

    The three types are not decoration: §5.6 keys the exit strategy off them — an `INTEGRITY` break
    is what unlocks an `IMMEDIATE` exit, where a `FUNDAMENTAL` one is worked through staged. The
    type therefore has to be a closed vocabulary, not free text.
    """

    FUNDAMENTAL = "fundamental"
    """The business case decays — margins, order book, segment revenue, the numbers the driver rests
    on.
    """

    STRUCTURAL = "structural"
    """The thesis's structure changes — the theme is exited, divested, or the company reorganizes
    out
    of it.
    """

    INTEGRITY = "integrity"
    """Trust in the numbers breaks — auditor resignation, fraud investigation, promoter pledge.
    Unlocks
    IMMEDIATE (§5.6).
    """


class EvaluationTier(StrEnum):
    """The tier at which a break condition is judged (§5.4). T2 is a case-level cadence, not a per-
    condition tier.
    """

    T0 = "T0"
    """Mechanical — a keyword hit, a price move, a filed number crossing a threshold. Costs ~₹0."""

    T1 = "T1"
    """Evidential — a triggered model review reading the filing or announcement behind a T0 flag."""


#: Words that make a condition a matter of opinion rather than a matter of fact. A break condition
#: containing one of these is not falsifiable: no rule and no evidence review can return a
#: definite verdict on "the story weakens", so the condition is rejected with the offending word
#: named. Kept explicit (not a cleverer NLP heuristic) because a rule that decides what may end a
#: position must be auditable and stable, not a model's guess about a model's guess.
_VAGUE_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "seems",
        "seem",
        "appears",
        "appear",
        "looks",
        "look",
        "feels",
        "feel",
        "maybe",
        "might",
        "probably",
        "possibly",
        "somewhat",
        "generally",
        "sentiment",
        "momentum",
        "story",
        "narrative",
        "vibe",
        "disappoints",
        "disappointing",
        "disappoint",
        "underperforms",
        "underperform",
        "underperformance",
        "outperform",
        "weakens",
        "weakening",
        "soften",
        "softens",
        "softening",
        "deteriorates",
        "deteriorating",
        "expensive",
        "cheap",
        "overvalued",
        "undervalued",
        "concerning",
        "worrying",
        "uncomfortable",
        "conviction",
        "unattractive",
        "bad",
        "worse",
        "poor",
        "trouble",
    }
)

#: Discrete, observable corporate events. Any one of these is inherently binary — it happened or it
#: did not — so a condition naming one is evidentially evaluable even without a number. Matched as
#: whole words, case-folded.
_EVENT_ANCHORS: Final[frozenset[str]] = frozenset(
    {
        "resignation",
        "resigns",
        "resign",
        "resigned",
        "divestment",
        "divests",
        "divest",
        "divested",
        "exit",
        "exits",
        "exited",
        "delisting",
        "delisted",
        "delist",
        "investigation",
        "fraud",
        "pledge",
        "pledged",
        "default",
        "defaults",
        "defaulted",
        "insolvency",
        "bankruptcy",
        "litigation",
        "sanction",
        "sanctioned",
        "suspension",
        "suspended",
        "restatement",
        "restates",
        "restated",
        "impairment",
        "qualification",
        "qualified",
        "disclosure",
        "discloses",
        "disclosed",
        "announcement",
        "announces",
        "announced",
        "filing",
        "files",
        "filed",
        "acquisition",
        "acquired",
        "merger",
        "closure",
        "shutdown",
        "recall",
        "ban",
        "banned",
        "revocation",
        "revoked",
        "cancellation",
        "cancelled",
    }
)

#: A measurable anchor: a number, a percentage, a comparator, or an explicit period over which a
#: trend is counted. Any one of these makes a condition a threshold that can be checked against a
#: filed figure. `\d` catches "20%", ">50%", "two"→ no (spelled numbers), so the spelled-out
#: period words ("consecutive", "quarters", …) are listed alongside.
_THRESHOLD_PATTERN: Final = re.compile(
    r"\d|%|[<>≤≥]|\b(?:consecutive|quarters?|months?|weeks?|years?|"
    r"half|halves|double|doubles|triple|triples|below|above|exceeds?|"
    r"falls?|declines?|drops?|breaches?|crosses?)\b",
    re.IGNORECASE,
)

_WORD = re.compile(r"[a-z]+")


def _words(text: str) -> set[str]:
    """The case-folded alphabetic words in `text`, for whole-word marker matching."""
    return set(_WORD.findall(text.lower()))


def assert_falsifiable(*, condition: str, evaluation: str) -> None:
    """Raise unless a break condition is mechanically or evidentially evaluable (§5.3).

    What it does: rejects a condition that is a matter of opinion (`_VAGUE_MARKERS`) or that names
    no checkable anchor at all — neither a discrete corporate event (`_EVENT_ANCHORS`) nor a
    measurable threshold (`_THRESHOLD_PATTERN`) — and requires the `evaluation` field to actually
    describe how the condition is watched.
    What it assumes: `condition` and `evaluation` are the human-readable fields of one break
    condition. Both must be non-blank; a blank `evaluation` is an unwatched condition, which is the
    same defect as an unfalsifiable one.
    What it never does: judge whether the condition is *true* — only whether a verdict on it could
    ever be reached. Falsifiability is a property of the statement, not of the world.

    Raises `UnfalsifiableBreakConditionError` with a reason, which is a `ThesisError` and not a
    `ValueError`, so it propagates out of `BreakCondition` construction unwrapped.
    """
    stripped = condition.strip()
    if not stripped:
        raise UnfalsifiableBreakConditionError(
            "a break condition states nothing; a thesis with an empty break condition has no way "
            "to be wrong"
        )
    if not evaluation.strip():
        raise UnfalsifiableBreakConditionError(
            f"break condition {condition!r} names no way to evaluate it; a condition nobody "
            "watches cannot be evaluated mechanically or evidentially (§5.3)"
        )

    offending = sorted(_words(condition) & _VAGUE_MARKERS)
    if offending:
        raise UnfalsifiableBreakConditionError(
            f"break condition {condition!r} is not falsifiable: {', '.join(offending)!r} is a "
            "matter of opinion, not a fact a rule or an evidence review can return a verdict on. "
            "State a "
            "concrete event (e.g. auditor resignation) or a measurable threshold (e.g. segment "
            "revenue falls two consecutive quarters)"
        )

    has_event = bool(_words(condition) & _EVENT_ANCHORS)
    has_threshold = bool(_THRESHOLD_PATTERN.search(condition))
    if not (has_event or has_threshold):
        raise UnfalsifiableBreakConditionError(
            f"break condition {condition!r} names no checkable anchor: it points to no discrete "
            "event and no measurable threshold, so no mechanical check and no evidence review "
            "could ever "
            "confirm it. Give it a number, a period, or a named corporate event (§5.3)"
        )


class BreakCondition(BaseModel):
    """One falsifiable way the thesis could be wrong (§5.3's `break_conditions[]`).

    What it does: carries the id, the type, the falsifiable condition, the tier it is judged at,
    and how it is watched — and refuses to exist if the condition is not falsifiable.
    What it assumes: `id` is stable within a thesis (e.g. `BC1`), because the journal records a
    verdict *per break-condition id* (`analyst.journal.BreakConditionEvaluation`) and a renamed id
    orphans the history.
    What it never does: hold a vague condition. `assert_falsifiable` runs in the model validator,
    so `BreakCondition(...)` itself raises `UnfalsifiableBreakConditionError` for anything that
    cannot be evaluated — there is no unfalsifiable break condition anywhere in the process.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, description="Stable id within the thesis, e.g. 'BC1'.")
    type: BreakConditionType = Field(description="fundamental / structural / integrity (§5.3).")
    condition: str = Field(
        min_length=1,
        description="The falsifiable statement — what, concretely, would break the thesis.",
    )
    evaluation_tier: EvaluationTier = Field(
        description="T0 (mechanical) or T1 (evidential) — the tier that returns the verdict (§5.4)."
    )
    evaluation: str = Field(
        min_length=1,
        description="How the condition is watched, e.g. 'T1 on results filing' or 'T0 keyword'.",
    )

    @field_validator("id")
    @classmethod
    def _id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a break condition needs an id; the journal records verdicts per id")
        return value

    @model_validator(mode="after")
    def _must_be_falsifiable(self) -> BreakCondition:
        """The hard requirement: no unfalsifiable break condition may be constructed (§5.3)."""
        assert_falsifiable(condition=self.condition, evaluation=self.evaluation)
        return self


class ThesisStatus(StrEnum):
    """Where a thesis version stands in the ratification workflow.

    The same three states a policy set moves through (`policies.PolicyStatus`), kept as a separate
    enum because a thesis and a policy set are ratified independently — a reader of a thesis status
    should not have to know it happens to share values with policies.
    """

    PROPOSAL = "PROPOSAL"
    """Drafted, awaiting ratification. May be revised freely; may not back a core buy."""

    RATIFIED = "RATIFIED"
    """Approved and current. Immutable — an edit is a new version (§5.1)."""

    SUPERSEDED = "SUPERSEDED"
    """Approved, then replaced by a higher version. Kept so "which thesis backed that buy" stays
    answerable.
    """


#: The §5.3 content that is ratified and hashed — the thesis proper, without its governance
#: bookkeeping. Named once so the digest, `revise()` and the tests all agree what "the thesis" is.
THESIS_CONTENT_FIELDS: Final[tuple[str, ...]] = (
    "driver",
    "theme_purity",
    "expected_evidence",
    "break_conditions",
)


def thesis_digest(content: Mapping[str, Any]) -> str:
    """`sha256:<hex>` of the canonical bytes of the §5.3 thesis content.

    What it does: hashes exactly the ratifiable content (`THESIS_CONTENT_FIELDS`) and nothing else,
    so the same thesis proposed twice hashes the same and a ratification can be checked against what
    it covers.
    What it assumes: `content` is JSON-safe (a `model_dump(mode="json")`) and carries every content
    field.
    What it never does: include version, status or ratification — hashing those would make an
    unchanged thesis look edited when it is renumbered.
    """
    missing = [field for field in THESIS_CONTENT_FIELDS if field not in content]
    if missing:
        raise ThesisError(
            f"cannot hash a partial thesis; missing {', '.join(missing)} "
            "(a ratification pins the whole §5.3 thesis, not part of it)"
        )
    return "sha256:" + digest_of(
        canonical_bytes({field: content[field] for field in THESIS_CONTENT_FIELDS})
    )


class Thesis(BaseModel):
    """One version of a core holding's §5.3 thesis: the content plus its governance state.

    What it does: holds the ratifiable thesis, hashes it, ratifies it against a pinned hash, and
    produces the next version when it is edited.
    What it assumes: version numbers are 1-based per (case, isin); a ratification's `at` came from
    an injected `Clock` (B10) and its `content_hash` was computed from this thesis.
    What it never does: change. Every model here is frozen, so `thesis.driver = ...` raises;
    `revise()` returns a new object in `PROPOSAL` and leaves this one untouched — that is the whole
    of "editing a ratified thesis creates a new version".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1, description="The case this thesis belongs to.")
    isin: str = Field(
        pattern=ISIN_PATTERN, description="The holding — ISIN, the only join key (#2)."
    )
    sleeve: Sleeve = Field(
        default=Sleeve.CORE,
        description="Always CORE: only core holdings carry a ratified thesis (§5.5).",
    )
    version: int = Field(
        ge=1, description="1-based, per (case, isin). Assigned when the version is made."
    )
    status: ThesisStatus = Field(
        default=ThesisStatus.PROPOSAL, description="PROPOSAL / RATIFIED / SUPERSEDED."
    )
    supersedes_version: int | None = Field(
        default=None, ge=1, description="The version this one replaces; None for v1."
    )

    # ── §5.3 content ──
    driver: str = Field(
        min_length=1, description="The thesis in one line — why this holding, this theme."
    )
    theme_purity: Purity = Field(
        ge=0,
        le=1,
        description="0..1: how purely this holding expresses the theme (§5.3). Exact decimal.",
    )
    expected_evidence: tuple[str, ...] = Field(
        min_length=1,
        description="What, if seen, would confirm the driver — the thesis's positive case.",
    )
    break_conditions: tuple[BreakCondition, ...] = Field(
        min_length=1, description="The falsifiable ways the thesis could be wrong (§5.3)."
    )

    ratification: Ratification | None = Field(
        default=None, description="Present exactly when the status is RATIFIED or SUPERSEDED."
    )

    @field_validator("sleeve")
    @classmethod
    def _thesis_is_core_only(cls, value: Sleeve) -> Sleeve:
        """A thesis is a core-sleeve artifact; a tactical position carries a rationale instead
        (§5.5).
        """
        if value is not Sleeve.CORE:
            raise ValueError(
                f"a thesis is a CORE-sleeve artifact, not {value.value}; a {value.value} position "
                "carries a journaled lightweight rationale (TacticalRationale), not a ratified "
                "thesis (§5.5)"
            )
        return value

    @field_validator("expected_evidence")
    @classmethod
    def _evidence_not_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("an expected-evidence item must not be blank")
        return value

    @field_validator("break_conditions")
    @classmethod
    def _break_condition_ids_unique(
        cls, value: tuple[BreakCondition, ...]
    ) -> tuple[BreakCondition, ...]:
        """Ids must be unique: the journal records a verdict per id, so two BC1s make history
        ambiguous.
        """
        ids = [bc.id for bc in value]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(
                "break-condition ids must be unique within a thesis; "
                f"repeated: {', '.join(duplicates)}"
            )
        return value

    @model_validator(mode="after")
    def _governance_state_must_be_consistent(self) -> Thesis:
        """A status and a ratification record that disagree would make the audit trail a guess."""
        approved = self.status in (ThesisStatus.RATIFIED, ThesisStatus.SUPERSEDED)
        if approved and self.ratification is None:
            raise ValueError(
                f"a {self.status.value} thesis must carry the ratification that approved it; "
                "an approved thesis with no record of who approved it is not governed"
            )
        if not approved and self.ratification is not None:
            raise ValueError(
                f"a {self.status.value} thesis must not carry a ratification: proposing is never "
                "ratifying (§5.1)"
            )
        if self.version == 1 and self.supersedes_version is not None:
            raise ValueError("version 1 supersedes nothing")
        if self.version > 1 and self.supersedes_version is None:
            raise ValueError(
                f"version {self.version} must name the version it supersedes, or the history of "
                "which thesis backed which buy is not reconstructable"
            )
        if self.supersedes_version is not None and self.supersedes_version >= self.version:
            raise ValueError(
                f"version {self.version} cannot supersede version {self.supersedes_version}: "
                "versions only move forward"
            )
        if self.ratification is not None and self.ratification.content_hash != self.content_hash:
            raise ThesisRatificationMismatchError(
                f"ratification pins {self.ratification.content_hash} but this thesis hashes to "
                f"{self.content_hash}: the approved thesis is not this one"
            )
        return self

    @property
    def content(self) -> Mapping[str, Any]:
        """Just the §5.3 thesis content, JSON-safe — the ratifiable, hashable document."""
        dumped = self.model_dump(mode="json")
        return {field: dumped[field] for field in THESIS_CONTENT_FIELDS}

    @property
    def content_hash(self) -> str:
        """`sha256:<hex>` over the §5.3 content. What a ratification pins."""
        return thesis_digest(self.content)

    @property
    def is_ratified(self) -> bool:
        """Whether this version is the current, ratified thesis — the only kind that backs a core
        buy.
        """
        return self.status is ThesisStatus.RATIFIED

    def revise(self, **changes: Any) -> Thesis:
        """Return the next version, in `PROPOSAL`, with `changes` applied (§5.1: an edit is a new
        version).

        What it does: builds version N+1 from this one, carrying `supersedes_version`, dropping the
        ratification and re-validating the whole thesis — so an edit that introduces an
        unfalsifiable break condition is refused here, at draft time.
        What it assumes: `changes` name §5.3 content fields. Editing `version`, `status` or the
        identity (`case_id`, `isin`) through here is refused; those are bookkeeping this method
        owns.
        What it never does: touch this object, or produce a version that changes nothing — a no-op
        revision would demand a fresh ratification for an identical thesis.
        """
        unknown = sorted(set(changes) - set(THESIS_CONTENT_FIELDS))
        if unknown:
            raise ThesisVersionError(
                f"revise() edits the §5.3 thesis content, not {', '.join(unknown)}; "
                f"editable: {', '.join(THESIS_CONTENT_FIELDS)}"
            )
        if not changes:
            raise ThesisVersionError(
                "revise() with no changes would create an identical version needing a fresh "
                "ratification for nothing"
            )
        document = self.model_dump()
        document.update(changes)
        document.update(
            {
                "version": self.version + 1,
                "supersedes_version": self.version,
                "status": ThesisStatus.PROPOSAL,
                "ratification": None,
            }
        )
        revised = Thesis.model_validate(document)
        if revised.content_hash == self.content_hash:
            raise ThesisVersionError(
                f"the revision is byte-identical to version {self.version} ({self.content_hash}); "
                "nothing to re-ratify"
            )
        return revised

    def ratified_with(self, ratification: Ratification) -> Thesis:
        """Return this version as `RATIFIED`, checking the ratification covers this exact content.

        What it does: attaches the approval and flips the status, once.
        What it assumes: the caller obtained `ratification.content_hash` from *this* thesis — a
        ratification granted for an earlier draft is refused, so a thesis that changed after it was
        displayed cannot be ratified against a stale approval.
        What it never does: re-ratify. A version already ratified raises; the way to change a
        ratified thesis is `revise()`.
        """
        if self.status is not ThesisStatus.PROPOSAL:
            raise ThesisVersionError(
                f"version {self.version} is already {self.status.value}; a ratified thesis is "
                "immutable and a change is a new version (revise())"
            )
        if ratification.content_hash != self.content_hash:
            raise ThesisRatificationMismatchError(
                f"ratification pins {ratification.content_hash} but version {self.version} hashes "
                f"to {self.content_hash}: the thesis changed after it was displayed"
            )
        return self.model_validate(
            {
                **self.model_dump(),
                "status": ThesisStatus.RATIFIED,
                "ratification": ratification.model_dump(),
            }
        )

    def superseded(self) -> Thesis:
        """Return this ratified version marked `SUPERSEDED`, keeping its ratification record.

        Called when a later version is ratified: the old thesis is not deleted (its record must
        stay answerable — "which thesis backed that buy"), it is retired.
        """
        if self.status is not ThesisStatus.RATIFIED:
            raise ThesisVersionError(
                f"only a RATIFIED thesis is superseded; version {self.version} is "
                f"{self.status.value}"
            )
        return self.model_validate({**self.model_dump(), "status": ThesisStatus.SUPERSEDED})


class TacticalRationale(BaseModel):
    """A tactical-sleeve position's lightweight, journaled rationale — not a ratified thesis (§5.5).

    What it does: carries the one-line reason a tactical buy was made, so it can be journaled.
    What it assumes: the tactical sleeve is where the agent has discretion inside the ratified dial
    (§5.5); a rationale is a record of that discretion, not a governance approval.
    What it never does: get ratified or versioned. The whole point of the sleeve is that it does
    not carry the weight a core thesis does — a tactical position that deserved a ratified thesis
    belongs in the core sleeve.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN, description="The tactical holding — ISIN (#2).")
    sleeve: Sleeve = Field(
        default=Sleeve.TACTICAL,
        description="Always TACTICAL: the core sleeve carries a ratified thesis instead.",
    )
    rationale: str = Field(min_length=1, description="One line: why this tactical position, now.")

    @field_validator("sleeve")
    @classmethod
    def _rationale_is_tactical_only(cls, value: Sleeve) -> Sleeve:
        if value is not Sleeve.TACTICAL:
            raise ValueError(
                f"a lightweight rationale is a TACTICAL-sleeve record, not {value.value}; a "
                f"{value.value} holding carries a ratified thesis (§5.5)"
            )
        return value

    @field_validator("rationale")
    @classmethod
    def _rationale_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "a tactical position must carry a rationale, even a lightweight one (§5.5)"
            )
        return value
