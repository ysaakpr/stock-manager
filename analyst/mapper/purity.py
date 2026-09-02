"""A3: the DISCLOSED purity score — what fraction of a business expresses a theme, and why.

A theme mapper that hands back "TICKER: 0.7" has produced a number no one can argue with, which is
the same as a number no one can trust. §5.2/§5.3 talk about *theme purity* as a real, defensible
property of a holding — the fraction of the business that actually expresses the theme — and the
task is explicit that it must be *disclosed*: "a number with no evidence trail is not acceptable
output" (CLAUDE.md's fail-loud rule, applied to analysis rather than ingestion).

So the score here is not an opinion a model emits; it is *arithmetic over disclosed evidence*. The
model's job (in `engine.py`) is to gather the evidence — the segment note in the annual report, the
revenue-mix slide, the announcement — and to say, per segment, what share of revenue it is and
whether that segment expresses the theme. This module's job is the part that must be deterministic
and auditable: given that evidence, the purity is

    purity = (revenue share of segments that express the theme) / (all disclosed revenue share)

and every input to that division travels with the result. Two consequences the design turns on:

* **It is deterministic.** No clock, no network, no model call. The same evidence produces the same
  `Decimal` score, quantized to a fixed number of places with a fixed rounding mode, in this process
  and any other — which is what lets a purity score be replayed (§8.3.3) and asserted on in a test.
* **It is explainable.** A `PurityScore` carries the full `PurityEvidence` trail it was computed
  from, and each piece of evidence is content-addressed (`sha256:<hex>`), so `evidence_refs` names
  exactly the material behind the number and a later reader can reconstruct the arithmetic.

Money is not involved (a purity is a ratio, not rupees), but the same discipline applies for the
same reason: a `float` share coerced to `Decimal` reads back with a binary tail and would make two
identical disclosures hash differently, so shares are `Decimal`/`str`, never `float`.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from analyst.journal import canonical_bytes, digest_of

__all__ = [
    "DEFAULT_PURITY_PLACES",
    "PURITY_METHOD",
    "Fraction",
    "PurityError",
    "PurityEvidence",
    "PurityEvidenceKind",
    "PurityScore",
    "UndisclosedPurityError",
    "score_purity",
]

#: Decimal places the score is quantized to. A ratio like 1/3 does not terminate; pinning the scale
#: and the rounding mode (below) is what makes the score reproducible, not platform-dependent.
DEFAULT_PURITY_PLACES: Final[int] = 6

#: The one-line description of how the number was reached, stored on every score so the method is
#: part of the record and not folded knowledge in this file. Rounding is half-even (banker's) so a
#: run of scores does not drift upward the way half-up would.
PURITY_METHOD: Final[str] = (
    "theme-expressing disclosed revenue share / total disclosed revenue share, quantized half-even"
)

_REF_PREFIX: Final = "sha256:"


def _reject_float(value: Any) -> Any:
    """Refuse a `float` where an exact decimal is required.

    A revenue share written `0.35` is already a binary float by the time pydantic sees it, and
    `Decimal(0.35)` is `0.34999999…`; two identical disclosures would then hash to different
    evidence. Shares are passed as `Decimal("0.35")` or the string `"0.35"`, never `0.35`.
    """
    if isinstance(value, float):
        raise ValueError(
            f"a revenue share must be an exact decimal, got float {value!r}; pass a Decimal or a "
            "string (a content hash over a float is not reproducible)"
        )
    return value


#: A disclosed revenue share in 0..1, exact so the evidence and the score hash reproducibly.
Fraction = Annotated[Decimal, BeforeValidator(_reject_float)]


class PurityError(Exception):
    """Base for every purity-scoring failure, so a caller can catch the module."""


class UndisclosedPurityError(PurityError):
    """A purity was requested for a candidate with no disclosure to compute it from.

    The whole point of a *disclosed* purity is that it rests on evidence: an announcement that a
    company "is in robotics" is not a revenue share, and a candidate whose only evidence is
    non-quantified cannot be given a number. Raised rather than defaulted, because a fabricated
    purity is exactly the untrustworthy number this module exists to refuse (CLAUDE.md: fail loud).
    """


class PurityEvidenceKind(StrEnum):
    """What sort of disclosure one piece of evidence is.

    The distinction is not cosmetic: `SEGMENT_DISCLOSURE` and `REVENUE_MIX` carry a revenue share
    and *enter the arithmetic*; `ANNOUNCEMENT` and `GUIDANCE` corroborate the story (they are part
    of the trail the score must show) but move no number, because a press release is not a
    proportion of revenue.
    """

    SEGMENT_DISCLOSURE = "segment_disclosure"
    """A reported business segment and its share of revenue (annual-report segment note)."""

    REVENUE_MIX = "revenue_mix"
    """A revenue-mix line from an investor presentation or filing — same weight as a segment."""

    ANNOUNCEMENT = "announcement"
    """A corporate announcement (order win, capacity, JV). Corroborating; carries no share."""

    GUIDANCE = "guidance"
    """Management guidance or commentary. Corroborating; carries no share."""


#: Evidence kinds whose `revenue_fraction` enters the purity arithmetic. Everything else is trail.
_WEIGHTED: Final[frozenset[PurityEvidenceKind]] = frozenset(
    {PurityEvidenceKind.SEGMENT_DISCLOSURE, PurityEvidenceKind.REVENUE_MIX}
)


class PurityEvidence(BaseModel):
    """One disclosed fact behind a purity score, with its provenance.

    What it does: carries where the fact was disclosed (`source`), what it is (`label`), and — for a
    weighted kind — the revenue share it represents and whether that share expresses the theme.
    What it assumes: `revenue_fraction` is a 0..1 share of the whole business; a weighted kind must
    carry one and a corroborating kind must not, so the arithmetic cannot silently skip a segment or
    double-count an announcement.
    What it never does: hold a `float` share (it would not hash reproducibly) or carry a share it
    does not use — a mismatched kind/share is rejected at construction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: PurityEvidenceKind = Field(description="Segment / revenue-mix / announcement / guidance.")
    source: str = Field(
        min_length=1,
        description="Where it was disclosed, e.g. 'FY25 annual report segment note', 'Q3FY26 ppt'.",
    )
    label: str = Field(min_length=1, description="What it is, e.g. 'Industrial automation'.")
    revenue_fraction: Fraction | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Share of total revenue (0..1) for a weighted kind; None for corroboration.",
    )
    expresses_theme: bool = Field(
        default=False,
        description="Whether the segment's revenue counts toward the theme; ignored if unweighted.",
    )
    note: str | None = Field(
        default=None, min_length=1, description="One line of context, verbatim from the source."
    )

    @model_validator(mode="after")
    def _share_matches_kind(self) -> PurityEvidence:
        """A weighted disclosure needs a share; a corroborating one must not carry a fake one."""
        weighted = self.kind in _WEIGHTED
        if weighted and self.revenue_fraction is None:
            raise ValueError(
                f"a {self.kind.value} discloses a revenue share and must carry revenue_fraction; "
                "without it the segment cannot enter the purity arithmetic"
            )
        if not weighted and self.revenue_fraction is not None:
            raise ValueError(
                f"a {self.kind.value} is corroborating evidence and carries no revenue share; "
                "a share on it would be counted as revenue it does not represent"
            )
        return self

    @property
    def is_weighted(self) -> bool:
        """Whether this evidence's share enters the purity arithmetic."""
        return self.kind in _WEIGHTED

    @property
    def ref(self) -> str:
        """`sha256:<hex>` of this evidence's canonical bytes — its content address in the trail."""
        return _REF_PREFIX + digest_of(canonical_bytes(self.model_dump(mode="json")))


class PurityScore(BaseModel):
    """A candidate's disclosed theme purity, and the evidence that produced it.

    What it does: hold the score (0..1, exact and quantized), the two revenue-share totals it was
    divided from, the method, and the full evidence trail — so the number is always explainable.
    What it assumes: it was produced by `score_purity`; the totals and the score agree by
    construction, and the validator re-checks that so a hand-built inconsistent score is rejected.
    What it never does: exist without evidence. `evidence` is non-empty and at least one item is
    weighted, because a disclosed purity with nothing disclosed is a contradiction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(
        pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$",
        description="The candidate the purity is about — ISIN, the only join key (#2).",
    )
    score: Fraction = Field(
        ge=0, le=1, description="0..1 disclosed theme purity, exact and quantized."
    )
    theme_disclosed_share: Decimal = Field(
        ge=0, description="Summed revenue share of the theme-expressing disclosed segments."
    )
    total_disclosed_share: Decimal = Field(
        gt=0, description="Summed revenue share of every disclosed segment (the denominator)."
    )
    places: int = Field(ge=0, description="Decimal places the score was quantized to.")
    method: str = Field(min_length=1, description="How the number was reached (PURITY_METHOD).")
    evidence: tuple[PurityEvidence, ...] = Field(
        min_length=1, description="The full disclosure trail the score was computed from."
    )

    @model_validator(mode="after")
    def _score_follows_from_evidence(self) -> PurityScore:
        """The number must be reproducible from the trail — a stored score that does not follow
        from its own evidence would be exactly the untrustworthy figure this module refuses.
        """
        weighted = [item for item in self.evidence if item.is_weighted]
        if not weighted:
            raise ValueError(
                "a purity score must carry at least one weighted disclosure; corroboration alone "
                "discloses no revenue share and cannot yield a number"
            )
        theme = sum(
            (item.revenue_fraction or Decimal(0) for item in weighted if item.expresses_theme),
            Decimal(0),
        )
        total = sum((item.revenue_fraction or Decimal(0) for item in weighted), Decimal(0))
        if theme != self.theme_disclosed_share or total != self.total_disclosed_share:
            raise ValueError(
                "purity totals do not match the evidence: recomputing the shares gives "
                f"theme={theme} total={total}, but the score records "
                f"theme={self.theme_disclosed_share} total={self.total_disclosed_share}"
            )
        expected = _quantize(theme / total, self.places)
        if expected != self.score:
            raise ValueError(
                f"purity score {self.score} does not follow from the evidence (expected {expected})"
            )
        return self

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        """The content address of each piece of evidence — the trail, addressable (acceptance 1)."""
        return tuple(item.ref for item in self.evidence)

    @property
    def content(self) -> Mapping[str, Any]:
        """The JSON-safe purity document — score plus its trail — for hashing and journaling."""
        return self.model_dump(mode="json")

    @property
    def ref(self) -> str:
        """`sha256:<hex>` of the whole score, so a journal line can pin the exact purity it saw."""
        return _REF_PREFIX + digest_of(canonical_bytes(self.content))


def _quantize(value: Decimal, places: int) -> Decimal:
    """Quantize a ratio to `places` decimals, half-even — the one rounding rule for every score."""
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def score_purity(
    isin: str,
    evidence: tuple[PurityEvidence, ...],
    *,
    places: int = DEFAULT_PURITY_PLACES,
) -> PurityScore:
    """Compute a candidate's disclosed theme purity from its evidence trail (deterministic).

    What it does: sums the revenue share of the theme-expressing disclosed segments and divides by
    the total disclosed revenue share, quantizing the ratio to `places` places half-even, and
    returns a `PurityScore` carrying the number, both totals, the method and the full trail.
    What it assumes: `evidence` is everything disclosed for this candidate; weighted items carry a
    revenue share (enforced on `PurityEvidence`). Corroborating items are kept in the trail but move
    no number.
    What it never does: invent a number. A candidate with no weighted disclosure raises
    `UndisclosedPurityError` rather than defaulting to 0 or 1 — a disclosed purity requires
    disclosure. It also never reads a clock, a file or the network, so the same evidence always
    yields the same score (§8.3.3).
    """
    if not evidence:
        raise UndisclosedPurityError(
            f"cannot score purity for {isin}: no evidence was supplied, and a disclosed purity "
            "has to rest on a disclosure"
        )
    weighted = [item for item in evidence if item.is_weighted]
    if not weighted:
        raise UndisclosedPurityError(
            f"cannot score purity for {isin}: its evidence is all corroboration (announcements, "
            "guidance) and discloses no revenue share, so no defensible fraction can be computed"
        )
    total = sum((item.revenue_fraction or Decimal(0) for item in weighted), Decimal(0))
    if total <= 0:
        raise UndisclosedPurityError(
            f"cannot score purity for {isin}: the disclosed revenue shares sum to {total}, so "
            "there is no denominator to divide by"
        )
    theme = sum(
        (item.revenue_fraction or Decimal(0) for item in weighted if item.expresses_theme),
        Decimal(0),
    )
    return PurityScore(
        isin=isin,
        score=_quantize(theme / total, places),
        theme_disclosed_share=theme,
        total_disclosed_share=total,
        places=places,
        method=PURITY_METHOD,
        evidence=tuple(evidence),
    )
