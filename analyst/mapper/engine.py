"""A3: the theme mapper — theme → value chain → listed proxies, each with a disclosed purity.

§5.2's INTERVIEW/RECOMMEND flow starts from a *theme*: "AI/Robotics", "defence indigenisation",
"energy transition". A theme is not tradeable; a value chain of it is, and only the parts of that
chain that are *listed* and that a real business actually derives revenue from. This module is that
translation, and it makes three promises the task pins it to:

* **It maps a theme to a value chain and to candidate ISINs.** `map_theme` asks an `LLM` (the X3
  protocol, so `StubLLM` in tests and a real client in production — B4, invariant #5) for the value
  chain of a theme and, for each stage, the listed proxies that sit in it, together with the
  disclosure that says how much of each proxy's business expresses the theme. The value chain and
  the candidates come back structured (a tool call, not prose to scrape).

* **Every candidate carries a disclosed purity with its evidence.** The model gathers the evidence
  — the segment note, the revenue-mix slide, the announcement — and `purity.score_purity` turns that
  evidence into a number *deterministically* (`purity.py`). The number is arithmetic over disclosed
  revenue shares, not a model's guess, so it is reproducible and explainable: the `PurityScore`
  carries the whole trail and each piece is content-addressed (acceptance 1, 2).

* **Candidates are drawn from the PIT universe, never a hardcoded list.** The universe of what could
  even be a candidate is the point-in-time universe query (M4.2, `PitUniverse`): a name the model
  proposes that was not listed-and-in-scope as of the mapping date is *dropped*, however plausible.
  This is the guard that keeps survivorship and look-ahead out of the candidate set (acceptance 3):
  the mapper never widens the universe, it only ranks within it.

The whole run is journalable with its inputs (§5.7, invariant #9): `ThemeMap.evidence_bundle`
assembles a content-addressed `EvidenceBundle` of every purity input, and `ThemeMap.proposal_entry`
builds the `POLICY_PROPOSAL` journal line that pins it — so the candidate universe the mapper
proposes for ratification records the exact evidence and the exact cost it was produced at.

Clockless and networkless by construction: the model call is a pure function of its inputs (the stub
makes that a replayable fact), the universe is passed in already resolved as-of a date, and a
journal entry's timestamp arrives from an injected `Clock` (B10).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from accounting.tokens import TokenPricer, load_price_card
from analyst.journal import (
    Actor,
    Decision,
    EvidenceBundle,
    EvidenceItem,
    EvidenceKind,
    JournalEntry,
    TokenSpend,
)
from analyst.llm.client import LLM, Message, Role, ToolCall, ToolSpec
from analyst.mapper.purity import PurityEvidence, PurityEvidenceKind, PurityScore, score_purity

if TYPE_CHECKING:
    from dataplatform.clock import Clock
    from dataplatform.query.universe import PitUniverse

__all__ = [
    "MAPPER_TOOL",
    "MAP_PURPOSE",
    "MapError",
    "ProxyCandidate",
    "ThemeMap",
    "ValueChainStage",
    "map_theme",
]

#: The burn-report purpose label for a mapping call (§5.7's per-purpose accounting).
MAP_PURPOSE: Final[str] = "theme_mapping"

#: How each disclosure kind is filed when the purity inputs are journaled. A revenue disclosure is a
#: point-in-time fundamental; an announcement is a filing; guidance is commentary. Kept explicit so
#: the evidence pack can slice "which candidates were scored without a single filing behind them".
_EVIDENCE_KIND: Final[Mapping[PurityEvidenceKind, EvidenceKind]] = {
    PurityEvidenceKind.SEGMENT_DISCLOSURE: EvidenceKind.FUNDAMENTAL,
    PurityEvidenceKind.REVENUE_MIX: EvidenceKind.FUNDAMENTAL,
    PurityEvidenceKind.ANNOUNCEMENT: EvidenceKind.FILING,
    PurityEvidenceKind.GUIDANCE: EvidenceKind.NEWS,
}


class MapError(Exception):
    """The model's answer could not be turned into a valid theme map.

    Covers a missing or ambiguous tool call, an unparseable value chain or candidate, a candidate
    placed in a stage the value chain does not contain, and a map with no candidate left inside the
    PIT universe. Distinct from `purity.UndisclosedPurityError`, which is the more specific "a kept
    candidate disclosed no revenue share" and is left to propagate with its own reason.
    """


class ValueChainStage(BaseModel):
    """One stage of a theme's value chain (§5.2) — where along the chain value is created.

    The stages are the skeleton the candidates hang from: "compute/silicon", "sensors & actuators",
    "systems integration", "end applications". A candidate names the stage it sits in, and the map
    refuses a candidate whose stage is not one of these, so the value chain and the proxy list stay
    coherent rather than two lists that happen to travel together.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, description="The stage, e.g. 'sensors & actuators'.")
    description: str = Field(
        min_length=1, description="What happens at this stage and why it expresses the theme."
    )


class ProxyCandidate(BaseModel):
    """One listed proxy for a theme: an ISIN, where it sits in the value chain, and its purity.

    What it does: tie a candidate ISIN to the value-chain stage it plays in and to its disclosed
    theme purity — the number *and* the evidence behind it.
    What it assumes: the ISIN was confirmed to be in the PIT universe before this was built (the
    engine drops the rest); the purity was computed from disclosure by `score_purity`.
    What it never does: carry a name-based identity — the join key is the ISIN (#2); `name` is for a
    human reading the proposal, never for a query.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(
        pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$",
        description="The listed proxy — ISIN, the only join key (#2).",
    )
    name: str = Field(
        min_length=1, description="Human-readable name, for the proposal, not a join."
    )
    value_chain_stage: str = Field(
        min_length=1, description="Which ValueChainStage.name this proxy sits in."
    )
    purity: PurityScore = Field(
        description="The disclosed theme purity and its full evidence trail."
    )

    @model_validator(mode="after")
    def _purity_is_about_this_isin(self) -> ProxyCandidate:
        """The purity's ISIN must be this candidate's — a score computed for another holding pinned
        here would misattribute the evidence trail.
        """
        if self.purity.isin != self.isin:
            raise ValueError(
                f"candidate {self.isin} carries a purity computed for {self.purity.isin}; "
                "the evidence trail would be misattributed"
            )
        return self


class ThemeMap(BaseModel):
    """A theme mapped to its value chain and its listed proxies, as of a date — the A3 output.

    What it does: hold the theme, the date it was mapped as-of (the PIT universe date), the value
    chain, the candidates that survived the universe filter (with purity + evidence), the size of
    the universe it drew from, and what the model call cost — everything a proposal or a T2 refresh
    needs, and everything the journal needs to record the run with its inputs.
    What it assumes: `candidates` were all confirmed inside the PIT universe as of `as_of`; the
    token spend is the priced cost of the one model call that produced the map.
    What it never does: contain a candidate outside the universe (the engine drops those), or carry
    a creation timestamp — the map is a deterministic function of the theme, the universe and the
    model's answer, so *when* it was built belongs to the journal entry that records it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    theme: str = Field(min_length=1, description="The theme, e.g. 'AI/Robotics'.")
    as_of: date = Field(description="The date the PIT universe was taken as of (§4.5).")
    value_chain: tuple[ValueChainStage, ...] = Field(
        min_length=1, description="The stages of the theme's value chain."
    )
    candidates: tuple[ProxyCandidate, ...] = Field(
        min_length=1, description="Listed proxies inside the PIT universe, sorted by ISIN."
    )
    universe_size: int = Field(
        ge=1,
        description="How many ISINs the PIT universe held — the pool candidates were drawn from.",
    )
    provider: str = Field(
        min_length=1, description="Which LLM provider answered ('stub'/'anthropic')."
    )
    model: str = Field(min_length=1, description="The model id the call was billed to.")
    tokens: TokenSpend = Field(description="Token and rupee cost of the mapping call (§5.7).")
    rendered_prompt: str = Field(
        min_length=1,
        description="The exact brief sent to the model — part of the journaled inputs.",
    )

    @field_validator("candidates")
    @classmethod
    def _sorted_and_unique(cls, value: tuple[ProxyCandidate, ...]) -> tuple[ProxyCandidate, ...]:
        """Candidates are ordered by ISIN and appear once, so the map hashes reproducibly and no
        proxy is scored twice.
        """
        isins = [candidate.isin for candidate in value]
        duplicates = sorted({isin for isin in isins if isins.count(isin) > 1})
        if duplicates:
            raise ValueError(f"a proxy appears more than once: {', '.join(duplicates)}")
        if isins != sorted(isins):
            raise ValueError("candidates must be sorted by ISIN for a reproducible map")
        return value

    @model_validator(mode="after")
    def _candidate_stages_exist(self) -> ThemeMap:
        """Every candidate sits in a stage the value chain actually names (acceptance 1)."""
        stages = {stage.name for stage in self.value_chain}
        stray = sorted({c.isin for c in self.candidates if c.value_chain_stage not in stages})
        if stray:
            raise ValueError(
                f"candidates {', '.join(stray)} name a value-chain stage not in the map; "
                f"known stages: {sorted(stages)}"
            )
        return self

    def evidence_bundle(
        self, *, case_id: str | None = None, actor: Actor = Actor.T2
    ) -> EvidenceBundle:
        """The content-addressed bundle of every purity input behind this map (invariant #9).

        What it does: turn each candidate's purity evidence into an `EvidenceItem` (the revenue
        share and whether it expresses the theme, the source it was disclosed in), plus a summary
        item per candidate carrying the computed score — so the bundle *is* the reproducible input
        set for the arithmetic, addressable by its own sha256.
        What it assumes: the map is complete; a bundle that omitted a disclosure would make the
        score irreproducible from the record.
        What it never does: carry a timestamp — the same map always hashes to the same address
        (§8.3.3), and *when* it was seen is the journal entry's field.
        """
        items: list[EvidenceItem] = []
        for candidate in self.candidates:
            for piece in candidate.purity.evidence:
                items.append(
                    EvidenceItem(
                        kind=_EVIDENCE_KIND[piece.kind],
                        source=piece.source,
                        label=f"{candidate.isin}:{piece.label}",
                        isin=candidate.isin,
                        as_of=self.as_of,
                        value=piece.revenue_fraction,
                        text=piece.note,
                        detail={
                            "purity_evidence_kind": piece.kind.value,
                            "expresses_theme": str(piece.expresses_theme).lower(),
                            "evidence_ref": piece.ref,
                            "value_chain_stage": candidate.value_chain_stage,
                        },
                    )
                )
            items.append(
                EvidenceItem(
                    kind=EvidenceKind.FUNDAMENTAL,
                    source="A3 theme mapper",
                    label=f"{candidate.isin}:purity",
                    isin=candidate.isin,
                    as_of=self.as_of,
                    value=candidate.purity.score,
                    detail={
                        "theme": self.theme,
                        "method": candidate.purity.method,
                        "theme_disclosed_share": str(candidate.purity.theme_disclosed_share),
                        "total_disclosed_share": str(candidate.purity.total_disclosed_share),
                        "purity_ref": candidate.purity.ref,
                    },
                )
            )
        return EvidenceBundle(
            case_id=case_id,
            trading_date=self.as_of,
            actor=actor,
            rendered_prompt=self.rendered_prompt,
            items=tuple(items),
        )

    def proposal_entry(
        self,
        *,
        clock: Clock,
        case_id: str | None = None,
        actor: Actor = Actor.T2,
    ) -> JournalEntry:
        """The `POLICY_PROPOSAL` journal line for this map, pinned to its evidence (§5.7).

        What it does: build the entry that records the mapper proposing a candidate universe for
        ratification — carrying the model and token cost of the call that produced it, the evidence
        snapshot ref of every purity input, and a strings-only payload of the theme and each
        candidate's score, so the proposal is reviewable without re-running the model.
        What it assumes: `clock` is injected (B10); the caller stores `evidence_bundle()` in the
        `EvidenceStore` before or alongside writing this entry, exactly as `Journal.append` does —
        the ref this pins is that bundle's content address.
        What it never does: ratify. Proposing is never ratifying (§5.1); this is a proposal line and
        the human ratifies the universe elsewhere (M5.8).
        """
        bundle_ref = self.evidence_bundle(case_id=case_id, actor=actor).ref()
        payload = {"theme": self.theme, "universe_size": str(self.universe_size)}
        payload.update(
            {
                f"purity:{candidate.isin}": str(candidate.purity.score)
                for candidate in self.candidates
            }
        )
        return JournalEntry(
            ts=clock.now(),
            trading_date=self.as_of,
            case_id=case_id,
            actor=actor,
            decision=Decision.POLICY_PROPOSAL,
            evidence_snapshot_ref=bundle_ref.ref,
            rationale=(
                f"A3 mapped theme {self.theme!r} to {len(self.value_chain)} value-chain stages and "
                f"{len(self.candidates)} listed proxies drawn from a {self.universe_size}-name PIT "
                "universe, each with a disclosed purity score; proposed for ratification"
            ),
            model=self.model,
            tokens=self.tokens,
            payload=payload,
        )


#: The tool the model answers with — its schema *is* the value-chain-plus-candidates shape. The
#: model gathers disclosure; it does not compute the purity number (that is `score_purity`, so the
#: score is arithmetic rather than judgement). `revenue_fraction` is a decimal string so it hashes
#: exactly; `kind` and the disclosure kinds are `enum`-constrained so the model cannot invent a
#: category the arithmetic does not know how to weight.
MAPPER_TOOL: Final[ToolSpec] = ToolSpec(
    name="map_theme",
    description=(
        "Map an investment theme to its value chain and the listed proxies in each stage. For each "
        "proxy give its ISIN and the disclosure of how much of its business expresses the theme: "
        "segment revenue shares and revenue-mix lines (summing toward the whole business), plus "
        "any corroborating announcement or guidance. Do not output a purity number; state the "
        "disclosed revenue shares and the system computes the purity. Return one call."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["value_chain", "candidates"],
        "properties": {
            "value_chain": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "description"],
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            },
            "candidates": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["isin", "name", "value_chain_stage", "evidence"],
                    "properties": {
                        "isin": {"type": "string"},
                        "name": {"type": "string"},
                        "value_chain_stage": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["kind", "source", "label"],
                                "properties": {
                                    "kind": {
                                        "enum": [
                                            "segment_disclosure",
                                            "revenue_mix",
                                            "announcement",
                                            "guidance",
                                        ]
                                    },
                                    "source": {"type": "string"},
                                    "label": {"type": "string"},
                                    "revenue_fraction": {"type": ["string", "null"]},
                                    "expresses_theme": {"type": "boolean"},
                                    "note": {"type": ["string", "null"]},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
)

_SYSTEM: Final[str] = (
    "You are the theme mapper (A3). Given an investment theme and the point-in-time universe of "
    "listed securities, lay out the theme's value chain and place listed proxies in it. For each "
    "proxy, disclose how much of its business expresses the theme using its own reported segment "
    "revenue shares — never an estimate of purity, only the disclosed shares and their sources. "
    "Propose only securities that appear in the supplied universe; the system will drop any that "
    "do not. Return exactly one map_theme tool call."
)


def _brief(theme: str, universe: PitUniverse) -> str:
    """The user turn for a mapping call — the theme and the PIT universe it must draw from.

    Lists the universe ISINs so the candidate set is grounded in the point-in-time universe rather
    than the model's memory of what is listed (acceptance 3); the engine still hard-filters the
    answer against the same set, so the brief is guidance and the filter is the guarantee.
    """
    isins = ", ".join(sorted(universe.isins))
    return (
        f"Theme: {theme}\n"
        f"As of: {universe.as_of.isoformat()}\n"
        f"Point-in-time universe ({len(universe.isins)} ISINs), propose only from these:\n"
        f"{isins}"
    )


def map_theme(
    llm: LLM,
    *,
    theme: str,
    universe: PitUniverse,
    model: str,
    pricer: TokenPricer | None = None,
    on: date | None = None,
) -> ThemeMap:
    """Map a theme to its value chain and its listed, purity-scored proxies (§5.2, A3).

    What it does: asks `llm` for the value chain and the candidate proxies with their disclosure
    (`MAPPER_TOOL`), keeps only the candidates whose ISIN is in `universe`, computes each survivor's
    disclosed purity from its evidence (`score_purity`, deterministic), prices the call, and returns
    a `ThemeMap`.
    What it assumes: `universe` was already taken as-of its date (M4.2); `model` names the model to
    bill the call to; `pricer` prices on the dated card (defaults to the checked-in card), on the
    universe's `as_of` unless `on` overrides it.
    What it never does: widen the universe — a proposed candidate outside `universe` is dropped, so
    the candidate set can never contain a name that was not listed-and-in-scope as of the date
    (acceptance 3). It also never fabricates a purity: a kept candidate that disclosed no revenue
    share raises `UndisclosedPurityError` (via `score_purity`) rather than getting a default number.

    Raises `MapError` for an answer that is not a single valid map, or one with no candidate left
    inside the universe.
    """
    brief = _brief(theme, universe)
    response = llm.complete(
        [Message(role=Role.USER, content=brief)],
        model=model,
        tools=[MAPPER_TOOL],
        system=_SYSTEM,
    )
    arguments = _map_arguments(response.tool_calls, theme=theme)

    value_chain = _parse_value_chain(arguments, theme=theme)
    stage_names = {stage.name for stage in value_chain}
    candidates = _parse_candidates(
        arguments, theme=theme, universe=universe, stage_names=stage_names
    )
    if not candidates:
        raise MapError(
            f"no proposed proxy for theme {theme!r} is in the {len(universe.isins)}-name PIT "
            f"universe as of {universe.as_of.isoformat()}; the map would be a hardcoded list "
            "rather than drawn from the universe, which is not allowed (acceptance 3)"
        )

    resolved_pricer = TokenPricer(load_price_card()) if pricer is None else pricer
    priced = resolved_pricer.price(
        response, on=universe.as_of if on is None else on, purpose=MAP_PURPOSE
    )
    return ThemeMap(
        theme=theme,
        as_of=universe.as_of,
        value_chain=value_chain,
        candidates=tuple(sorted(candidates, key=lambda candidate: candidate.isin)),
        universe_size=len(universe.isins),
        provider=response.provider,
        model=priced.model,
        tokens=priced.token_spend,
        rendered_prompt=brief,
    )


def _map_arguments(tool_calls: Sequence[ToolCall], *, theme: str) -> Mapping[str, Any]:
    """The arguments of the single `map_theme` call, or raise `MapError`."""
    maps = [call for call in tool_calls if call.name == MAPPER_TOOL.name]
    if not maps:
        raise MapError(
            f"the model returned no {MAPPER_TOOL.name} call for theme {theme!r}; a map was "
            "requested and none was produced (a refusal is not a map)"
        )
    if len(maps) > 1:
        raise MapError(
            f"the model returned {len(maps)} {MAPPER_TOOL.name} calls for theme {theme!r}; a theme "
            "maps to one value chain, so which one to record is ambiguous"
        )
    return maps[0].arguments


def _parse_value_chain(arguments: Mapping[str, Any], *, theme: str) -> tuple[ValueChainStage, ...]:
    """Turn the answer's `value_chain` into stages, or raise `MapError`."""
    try:
        raw = arguments["value_chain"]
        stages = tuple(ValueChainStage.model_validate(stage) for stage in raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise MapError(f"the value chain for theme {theme!r} is not valid: {exc}") from exc
    if not stages:
        raise MapError(f"the map for theme {theme!r} has an empty value chain")
    names = [stage.name for stage in stages]
    if len(set(names)) != len(names):
        raise MapError(f"the value chain for theme {theme!r} has duplicate stage names")
    return stages


def _parse_candidates(
    arguments: Mapping[str, Any],
    *,
    theme: str,
    universe: PitUniverse,
    stage_names: set[str],
) -> list[ProxyCandidate]:
    """Turn the answer's `candidates` into scored proxies, dropping any outside the PIT universe.

    Candidates not in `universe` are silently dropped (that is the acceptance-3 guarantee, not an
    error); a kept candidate that is malformed or names an unknown value-chain stage raises
    `MapError`, and one that disclosed no revenue share raises `UndisclosedPurityError` from
    `score_purity` — a proxy the model kept but did not substantiate is a loud failure, not a
    guessed number.
    """
    try:
        raw = arguments["candidates"]
    except (KeyError, TypeError) as exc:
        raise MapError(f"the map for theme {theme!r} has no candidates field: {exc}") from exc

    candidates: list[ProxyCandidate] = []
    for entry in raw:
        try:
            isin = entry["isin"]
        except (KeyError, TypeError) as exc:
            raise MapError(f"a candidate in theme {theme!r} has no isin: {exc}") from exc
        if isin not in universe:
            continue  # not listed-and-in-scope as of the date; drop, do not widen the universe
        try:
            evidence = tuple(PurityEvidence.model_validate(piece) for piece in entry["evidence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MapError(
                f"candidate {isin} in theme {theme!r} has invalid evidence: {exc}"
            ) from exc
        purity = score_purity(isin, evidence)
        try:
            candidates.append(
                ProxyCandidate(
                    isin=isin,
                    name=entry["name"],
                    value_chain_stage=entry["value_chain_stage"],
                    purity=purity,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MapError(f"candidate {isin} in theme {theme!r} is not valid: {exc}") from exc
    stray = sorted({c.isin for c in candidates if c.value_chain_stage not in stage_names})
    if stray:
        raise MapError(
            f"candidates {', '.join(stray)} in theme {theme!r} name a value-chain stage the map "
            f"does not contain; known stages: {sorted(stage_names)}"
        )
    return candidates
