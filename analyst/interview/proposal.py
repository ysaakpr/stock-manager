"""A2: the PROPOSAL document — one ratifiable case, assembled from the interview and its inputs.

§5.1 is emphatic that what the human approves is *one document, one ratification*: "universe with
purity scores, per-holding theses with break conditions, rotation dial, rails, exit menu, cash
policy, benchmark pair. One document, one ratification." Not seven approvals for seven policies and
one more per holding — a single act over the whole case. This module is that document.

A `Proposal` gathers, into one frozen object:

* the **universe** — the theme map's candidates (`analyst.mapper.ProxyCandidate`) with their
  disclosed purity scores, minus what the human excluded;
* a **thesis per holding** — the §5.3 `Thesis` (`analyst.thesis`) drafted for each core name, in
  `PROPOSAL` status, carrying its falsifiable break conditions;
* the **seven §5.2 policies** — the `PolicySet` recommended by `flow.recommend_policies`, dial and
  rails and all, each with the reasoning that ties it to the interview; and
* the **recommendations** — one note per policy, so a reviewer reads *why* before approving.

The one property the whole module exists to provide is a single content hash over all of it
(`content_hash`), and a single `ratified_with` that pins that hash — so ratification is one act and
a ratification granted for one version cannot travel onto an edited one (acceptance 3, and the guard
M5.8's "cannot ratify a proposal that changed since it was displayed" builds on). The hash covers
the concrete ratifiable outputs — the theme, the universe, the theses and the policies — not the
recommendations or the raw answers, exactly as `PolicySet` hashes its policies and not its
provenance: an edit that changes an answer changes the policy it drives, and so changes the hash.

Clockless and networkless by construction: the proposal is a deterministic function of the interview
answers, the theme map (taken as-of a date, M4.2) and the drafted theses. The only time in it is the
`Ratification.at`, which arrives from an injected `Clock` (B10) at the moment of approval, never
from this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from analyst.cases.policies import (
    PolicySet,
    PolicyStatus,
    Ratification,
)
from analyst.interview.flow import (
    DEFAULT_PARKING_ISIN,
    DEFAULT_PARKING_SYMBOL,
    InterviewAnswers,
    Recommendation,
    recommend_policies,
)
from analyst.journal import canonical_bytes, digest_of
from analyst.mapper import ProxyCandidate, ThemeMap
from analyst.thesis import Sleeve, Thesis, ThesisStatus

__all__ = [
    "PROPOSAL_CONTENT_FIELDS",
    "EmptyUniverseError",
    "IncompleteProposalError",
    "Proposal",
    "ProposalError",
    "ProposalRatificationMismatchError",
    "ProposalStatus",
    "build_proposal",
]

#: The parts of a proposal a ratification pins — the concrete outputs a human approves. Named once
#: so the content hash and any reader agree what "the proposal" is; provenance (the raw answers, the
#: reasoning notes) is stored for review but not hashed, mirroring `PolicySet.policies`.
PROPOSAL_CONTENT_FIELDS: Final[tuple[str, ...]] = (
    "theme",
    "as_of",
    "universe",
    "theses",
    "policies",
)


class ProposalError(Exception):
    """Base for every proposal-assembly failure, so a caller can catch the module."""


class EmptyUniverseError(ProposalError):
    """Every mapped candidate was excluded, so there is no universe to propose.

    A proposal with no holdings is not a case; raised rather than returned so the caller sees that
    the exclusions (or the theme map) left nothing, not a silently empty document.
    """


class IncompleteProposalError(ProposalError):
    """A proposed holding has no thesis, or a thesis was supplied for a holding not proposed.

    §5.5 requires every core holding to carry a thesis before it can be bought; a proposal is where
    that thesis is drafted for ratification, so a universe name without one is an incomplete
    proposal, and a thesis without a matching universe name is a thesis for a holding nobody
    proposed — both are fail-loud mistakes, not documents to ratify.
    """


class ProposalRatificationMismatchError(ProposalError):
    """A ratification was presented for content other than this proposal's.

    The same guarantee `PolicySet.ratified_with` gives, at the whole-proposal level: a ratification
    that could be moved onto an edited proposal ratifies nothing, and "one document, one
    ratification" (§5.1) rests on it being un-movable.
    """


class ProposalStatus(StrEnum):
    """Where a proposal stands. A pre-funding document has just two states.

    A proposal is edited by building a fresh one (pre-funding, the whole document may be replaced),
    so there is no revise/supersede here — a change is a new proposal with a new hash, and a
    ratification pinned to the old hash is refused.
    """

    PROPOSAL = "PROPOSAL"
    """Drafted and complete, awaiting the one ratification act (§5.1)."""

    RATIFIED = "RATIFIED"
    """Approved. Immutable; the ratification pins the exact content approved."""


def _apply_exclusions(
    candidates: tuple[ProxyCandidate, ...], exclusions: tuple[str, ...]
) -> tuple[ProxyCandidate, ...]:
    """Drop candidates the human excluded, by ISIN or by a keyword in name or value-chain stage.

    A candidate is excluded if an exclusion token equals its ISIN, or (case-insensitively) appears
    in its name or its value-chain stage — so "no fossil fuels" or a specific ISIN both work. The
    survivors keep the map's ISIN order.
    """
    if not exclusions:
        return candidates
    folded = [token.casefold() for token in exclusions]
    kept: list[ProxyCandidate] = []
    for candidate in candidates:
        haystack = f"{candidate.isin} {candidate.name} {candidate.value_chain_stage}".casefold()
        excluded = any(token == candidate.isin.casefold() or token in haystack for token in folded)
        if not excluded:
            kept.append(candidate)
    return tuple(kept)


class Proposal(BaseModel):
    """One ratifiable case (§5.1): universe, per-holding theses, seven §5.2 policies, as one act.

    What it does: holds the whole proposal, hashes the ratifiable content as one document, and
    ratifies it against that single hash.
    What it assumes: it was assembled by `build_proposal`, so the universe, the theses and the
    policies are already consistent (a thesis per holding, the policy set in `PROPOSAL`); the
    validators re-check that so a hand-built inconsistent proposal is rejected.
    What it never does: change, or ratify piecemeal. Every model is frozen; there is exactly one
    `content_hash` and one `ratified_with`, so a case is approved in one act, not a series of them
    (acceptance 3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1, description="The case this proposal is for.")
    theme: str = Field(min_length=1, description="The theme the case expresses.")
    as_of: date = Field(description="The date the universe was taken as-of (the theme map's date).")
    answers: InterviewAnswers = Field(description="What the §5.1 interview elicited — for review.")
    universe: tuple[ProxyCandidate, ...] = Field(
        min_length=1, description="Proposed holdings with purity scores, sorted by ISIN."
    )
    theses: tuple[Thesis, ...] = Field(
        min_length=1, description="One §5.3 thesis per holding, sorted by ISIN, in PROPOSAL."
    )
    policy_set: PolicySet = Field(description="The seven §5.2 policies, recommended, in PROPOSAL.")
    recommendations: tuple[Recommendation, ...] = Field(
        min_length=1, description="Why each policy was recommended (§5.2 order) — the reasoning."
    )
    status: ProposalStatus = Field(
        default=ProposalStatus.PROPOSAL, description="PROPOSAL or RATIFIED."
    )
    ratification: Ratification | None = Field(
        default=None, description="Present exactly when the status is RATIFIED."
    )

    @field_validator("universe")
    @classmethod
    def _universe_sorted_unique(
        cls, value: tuple[ProxyCandidate, ...]
    ) -> tuple[ProxyCandidate, ...]:
        """The universe is ordered by ISIN and each name appears once, so it hashes reproducibly."""
        isins = [candidate.isin for candidate in value]
        duplicates = sorted({isin for isin in isins if isins.count(isin) > 1})
        if duplicates:
            raise ValueError(f"a holding appears more than once: {', '.join(duplicates)}")
        if isins != sorted(isins):
            raise ValueError("universe must be sorted by ISIN for a reproducible proposal")
        return value

    @field_validator("theses")
    @classmethod
    def _theses_sorted_unique(cls, value: tuple[Thesis, ...]) -> tuple[Thesis, ...]:
        """Theses are ordered by ISIN and one per holding."""
        isins = [thesis.isin for thesis in value]
        duplicates = sorted({isin for isin in isins if isins.count(isin) > 1})
        if duplicates:
            raise ValueError(f"more than one thesis for: {', '.join(duplicates)}")
        if isins != sorted(isins):
            raise ValueError("theses must be sorted by ISIN for a reproducible proposal")
        return value

    @model_validator(mode="after")
    def _document_is_consistent(self) -> Proposal:
        """The universe, the theses and the policy set must describe the same case."""
        universe_isins = {candidate.isin for candidate in self.universe}
        thesis_isins = {thesis.isin for thesis in self.theses}
        if universe_isins != thesis_isins:
            missing = sorted(universe_isins - thesis_isins)
            extra = sorted(thesis_isins - universe_isins)
            raise IncompleteProposalError(
                "every proposed holding needs exactly one thesis and vice versa; "
                f"holdings without a thesis: {missing or 'none'}; "
                f"theses without a holding: {extra or 'none'} (§5.5)"
            )
        for thesis in self.theses:
            if thesis.case_id != self.case_id:
                raise IncompleteProposalError(
                    f"thesis for {thesis.isin} belongs to case {thesis.case_id!r}, not "
                    f"{self.case_id!r}"
                )
            if thesis.sleeve is not Sleeve.CORE:
                raise IncompleteProposalError(
                    f"thesis for {thesis.isin} is {thesis.sleeve.value}; a proposal's holdings are "
                    "core, and only core holdings carry a thesis (§5.5)"
                )
            if thesis.status is not ThesisStatus.PROPOSAL:
                raise IncompleteProposalError(
                    f"thesis for {thesis.isin} is {thesis.status.value}; a proposal carries draft "
                    "theses ratified with the proposal in one act, not pre-ratified ones (§5.1)"
                )
        if self.policy_set.case_id != self.case_id:
            raise IncompleteProposalError(
                f"policy set belongs to case {self.policy_set.case_id!r}, not {self.case_id!r}"
            )
        if self.policy_set.status is not PolicyStatus.PROPOSAL:
            raise IncompleteProposalError(
                f"the policy set is {self.policy_set.status.value}; a proposal carries a draft "
                "policy set ratified with the proposal in one act (§5.1)"
            )

        approved = self.status is ProposalStatus.RATIFIED
        if approved and self.ratification is None:
            raise ValueError(
                "a RATIFIED proposal must carry the ratification that approved it; an approved "
                "proposal with no record of who approved it is not governed"
            )
        if not approved and self.ratification is not None:
            raise ValueError(
                "a PROPOSAL proposal must not carry a ratification: proposing is never ratifying "
                "(§5.1)"
            )
        if self.ratification is not None and self.ratification.content_hash != self.content_hash:
            raise ProposalRatificationMismatchError(
                f"ratification pins {self.ratification.content_hash} but this proposal hashes to "
                f"{self.content_hash}: the approved document is not this one"
            )
        return self

    @property
    def ratifiable_content(self) -> Mapping[str, Any]:
        """The one document a ratification pins: theme, date, universe, theses and the policies.

        JSON-safe and deterministic — the universe is reduced to each holding's ISIN and its
        disclosed purity score (a string so it hashes exactly), the theses to their §5.3 content,
        and the policies to the seven §5.2 policies. The recommendations and the raw answers are
        deliberately absent: they explain the document, they are not the document.
        """
        return {
            "theme": self.theme,
            "as_of": self.as_of.isoformat(),
            "universe": [
                {"isin": candidate.isin, "purity_score": str(candidate.purity.score)}
                for candidate in self.universe
            ],
            "theses": [thesis.content for thesis in self.theses],
            "policies": self.policy_set.policies,
        }

    @property
    def content_hash(self) -> str:
        """`sha256:<hex>` over the whole ratifiable document — what the one ratification pins."""
        return "sha256:" + digest_of(canonical_bytes(dict(self.ratifiable_content)))

    def ratified_with(self, ratification: Ratification) -> Proposal:
        """Return this proposal as `RATIFIED`, checking the ratification covers this exact content.

        What it does: attaches the one approval to the whole document and flips the status, once.
        What it assumes: the caller obtained `ratification.content_hash` from *this* proposal — a
        ratification granted for an earlier draft is refused, which is M5.8's "cannot ratify a
        proposal that changed since it was displayed" enforced in the model.
        What it never does: ratify piecemeal or re-ratify. There is one hash for the whole case, and
        a proposal already ratified raises; a change is a fresh proposal (pre-funding, §5.1).
        """
        if self.status is not ProposalStatus.PROPOSAL:
            raise ProposalError(
                f"proposal for case {self.case_id} is already {self.status.value}; a ratified "
                "proposal is immutable and a change is a new proposal"
            )
        if ratification.content_hash != self.content_hash:
            raise ProposalRatificationMismatchError(
                f"ratification pins {ratification.content_hash} but this proposal hashes to "
                f"{self.content_hash}: the document changed after it was displayed"
            )
        return self.model_validate(
            {
                **self.model_dump(),
                "status": ProposalStatus.RATIFIED,
                "ratification": ratification.model_dump(),
            }
        )


def build_proposal(
    *,
    case_id: str,
    answers: InterviewAnswers,
    theme_map: ThemeMap,
    theses: Mapping[str, Thesis],
    parking_isin: str = DEFAULT_PARKING_ISIN,
    parking_symbol: str = DEFAULT_PARKING_SYMBOL,
    policy_version: int = 1,
) -> Proposal:
    """Assemble one ratifiable `Proposal` from the interview and its inputs (§5.1's PROPOSAL step).

    What it does: filters the theme map's candidates by the interview's exclusions to form the
    universe, requires exactly one drafted §5.3 thesis per surviving holding, recommends the seven
    §5.2 policies from the answers (`recommend_policies`), and folds the lot into one `Proposal`
    with a single content hash (acceptance 1, 3).
    What it assumes: `theme_map` was mapped for this theme as-of its date (M4.2, A3), and `theses`
    holds a `PROPOSAL`, `CORE` thesis for this `case_id` keyed by ISIN — one per holding the human
    did not exclude.
    What it never does: leave a §5.2 policy blank, propose a holding without a thesis, or ratify —
    the result is a `PROPOSAL`, and a human (or the B9 fixture) ratifies it in one act (§5.1).

    Raises `ProposalError` (and its subclasses) for a theme that does not match the map, a universe
    emptied by exclusions, or a holding/thesis mismatch.
    """
    if answers.theme.strip().casefold() != theme_map.theme.strip().casefold():
        raise ProposalError(
            f"the interview theme {answers.theme!r} does not match the theme map "
            f"{theme_map.theme!r}; the universe was mapped for a different theme"
        )

    universe = _apply_exclusions(theme_map.candidates, answers.exclusions)
    if not universe:
        raise EmptyUniverseError(
            f"the interview's exclusions {list(answers.exclusions)} removed every one of the "
            f"{len(theme_map.candidates)} mapped candidates; there is no universe to propose"
        )

    universe_isins = {candidate.isin for candidate in universe}
    supplied_isins = set(theses)
    missing = sorted(universe_isins - supplied_isins)
    if missing:
        raise IncompleteProposalError(
            f"no thesis drafted for proposed holdings: {', '.join(missing)}; every core holding "
            "carries a thesis before it can be ratified for a buy (§5.5)"
        )
    extra = sorted(supplied_isins - universe_isins)
    if extra:
        raise IncompleteProposalError(
            f"a thesis was supplied for holdings not in the proposed universe: {', '.join(extra)}; "
            "either they were excluded or the wrong theses were passed"
        )

    ordered_theses = tuple(theses[isin] for isin in sorted(universe_isins))
    recommended = recommend_policies(
        answers, parking_isin=parking_isin, parking_symbol=parking_symbol
    )
    policy_set = PolicySet(
        case_id=case_id,
        version=policy_version,
        status=PolicyStatus.PROPOSAL,
        capital_plan=recommended.capital_plan,
        horizon=recommended.horizon,
        rotation_dial=recommended.rotation_dial,
        rails=recommended.rails,
        exit_menu=recommended.exit_menu,
        cash_policy=recommended.cash_policy,
        monitoring=recommended.monitoring,
        ratification=None,
    )
    return Proposal(
        case_id=case_id,
        theme=theme_map.theme,
        as_of=theme_map.as_of,
        answers=answers,
        universe=tuple(sorted(universe, key=lambda candidate: candidate.isin)),
        theses=ordered_theses,
        policy_set=policy_set,
        recommendations=recommended.recommendations,
    )
