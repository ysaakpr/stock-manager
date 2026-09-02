"""M5.5: a theme maps to a value chain and purity-scored proxies, drawn only from the PIT universe.

The three claims this task makes, and how each is shown here:

1. **An AI/Robotics theme maps to a value chain and candidate ISINs with purity scores and evidence
   refs.** `map_theme` against a `StubLLM` returns a `ThemeMap` whose value chain has stages, whose
   candidates are ISINs placed in those stages, and each of whose purity scores carries a
   content-addressed evidence trail — with the number equal to the hand-computed disclosed share.
2. **Purity scoring is deterministic under StubLLM and journaled with its inputs.** The same canned
   answer yields byte-identical scores and a byte-identical evidence bundle address across runs, and
   `ThemeMap.proposal_entry` produces a `POLICY_PROPOSAL` line pinning that bundle, the model and
   the priced token cost; the stored bundle reloads with every disclosed revenue share intact.
3. **Candidates are drawn from the PIT universe, never a hardcoded list.** A proposed proxy outside
   the universe is dropped, shrinking the universe drops the candidate that leaves it, and a map
   whose proposals are all outside the universe raises rather than inventing a candidate set.

Everything is offline: the model is `StubLLM` (B4), time is a `FrozenClock` (B10), pricing is the
checked-in card, and joins are on ISIN (#2). No network, no database, no wall clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from analyst.journal import Actor, Decision, EvidenceStore
from analyst.llm import Message, Role, StopReason, StubLLM, StubReply, ToolCall, prompt_digest
from analyst.mapper import (
    MapError,
    PurityEvidence,
    PurityEvidenceKind,
    ThemeMap,
    UndisclosedPurityError,
    map_theme,
    score_purity,
)
from analyst.mapper import engine as mapper_engine
from dataplatform.clock import FrozenClock
from dataplatform.query.universe import PitUniverse

THEME = "AI/Robotics"
MODEL = "claude-opus-5"  # on the checked-in price card
AS_OF = date(2026, 9, 2)

# Candidate ISINs. The last one is deliberately *not* placed in the universe below.
COMPUTE = "INE009A01021"  # compute/silicon stage
SENSORS = "INE467B01029"  # sensors & actuators stage
INTEG = "INE040A01034"  # systems integration stage — a 1/3 purity, to exercise quantization
OUT_OF_UNIVERSE = "INE075A01022"  # proposed by the model, never in the universe


def _universe(*isins: str, as_of: date = AS_OF) -> PitUniverse:
    """A PIT universe holding exactly the given ISINs (plus filler so it is a realistic pool)."""
    filler = {"INE002A01018", "INE030A01027", "INE062A01020"}
    return PitUniverse(as_of=as_of, isins=frozenset({*isins, *filler}))


# ── canned model answer ────────────────────────────────────────────────────────────────────────


def _evidence(
    kind: str,
    label: str,
    *,
    source: str = "FY26 annual report segment note",
    revenue_fraction: str | None = None,
    expresses_theme: bool = False,
    note: str | None = None,
) -> dict[str, object]:
    return {
        "kind": kind,
        "source": source,
        "label": label,
        "revenue_fraction": revenue_fraction,
        "expresses_theme": expresses_theme,
        "note": note,
    }


def _map_arguments(*candidate_isins: str) -> dict[str, object]:
    """A well-formed `map_theme` answer. `candidate_isins` selects which candidates to include."""
    all_candidates = {
        COMPUTE: {
            "isin": COMPUTE,
            "name": "Compute Silicon Ltd",
            "value_chain_stage": "compute & silicon",
            "evidence": [
                _evidence(
                    "segment_disclosure",
                    "AI accelerators",
                    revenue_fraction="0.6",
                    expresses_theme=True,
                ),
                _evidence("segment_disclosure", "Legacy switchgear", revenue_fraction="0.4"),
                _evidence(
                    "announcement",
                    "New robotics compute JV",
                    source="BSE announcement 2026-08-14",
                    note="joint venture for edge-AI modules",
                ),
            ],
        },
        SENSORS: {
            "isin": SENSORS,
            "name": "Sensor Actuator Corp",
            "value_chain_stage": "sensors & actuators",
            "evidence": [
                _evidence(
                    "revenue_mix",
                    "Robotics integration",
                    source="Q3FY26 investor ppt",
                    revenue_fraction="0.25",
                    expresses_theme=True,
                ),
                _evidence(
                    "revenue_mix", "EPC", source="Q3FY26 investor ppt", revenue_fraction="0.75"
                ),
            ],
        },
        INTEG: {
            "isin": INTEG,
            "name": "Integration Systems Ltd",
            "value_chain_stage": "systems integration",
            "evidence": [
                _evidence(
                    "segment_disclosure",
                    "Autonomous systems",
                    revenue_fraction="0.1",
                    expresses_theme=True,
                ),
                _evidence("segment_disclosure", "Industrial services", revenue_fraction="0.2"),
            ],
        },
        OUT_OF_UNIVERSE: {
            "isin": OUT_OF_UNIVERSE,
            "name": "Delisted Robotics Ltd",
            "value_chain_stage": "systems integration",
            "evidence": [
                _evidence(
                    "segment_disclosure",
                    "Robotics",
                    revenue_fraction="0.9",
                    expresses_theme=True,
                ),
                _evidence("segment_disclosure", "Other", revenue_fraction="0.1"),
            ],
        },
    }
    return {
        "value_chain": [
            {"name": "compute & silicon", "description": "AI accelerators and processors"},
            {"name": "sensors & actuators", "description": "perception and motion hardware"},
            {"name": "systems integration", "description": "assembling autonomous systems"},
        ],
        "candidates": [all_candidates[isin] for isin in candidate_isins],
    }


def _reply(arguments: Mapping[str, object]) -> StubReply:
    return StubReply(
        text="mapped",
        tool_calls=(ToolCall(id="call-1", name="map_theme", arguments=arguments),),
        stop_reason=StopReason.TOOL_USE,
    )


def _register(stub: StubLLM, universe: PitUniverse, arguments: Mapping[str, object]) -> None:
    """Register a canned map answer for exactly the request `map_theme` will make for `universe`."""
    brief = mapper_engine._brief(THEME, universe)
    digest = prompt_digest(
        [Message(role=Role.USER, content=brief)],
        model=MODEL,
        tools=[mapper_engine.MAPPER_TOOL],
        system=mapper_engine._SYSTEM,
    )
    stub.register(digest, _reply(arguments))


def _mapped(universe: PitUniverse, arguments: Mapping[str, object]) -> ThemeMap:
    stub = StubLLM(synthesize_unknown=False)
    _register(stub, universe, arguments)
    return map_theme(stub, theme=THEME, universe=universe, model=MODEL)


# ── acceptance 1: theme → value chain + candidate ISINs + purity + evidence refs ─────────────────


def test_theme_maps_to_value_chain_and_scored_candidates() -> None:
    universe = _universe(COMPUTE, SENSORS, INTEG)
    theme_map = _mapped(universe, _map_arguments(COMPUTE, SENSORS, INTEG))

    assert theme_map.theme == THEME
    assert theme_map.as_of == AS_OF
    assert {stage.name for stage in theme_map.value_chain} == {
        "compute & silicon",
        "sensors & actuators",
        "systems integration",
    }
    assert [c.isin for c in theme_map.candidates] == sorted([COMPUTE, SENSORS, INTEG])

    by_isin = {c.isin: c for c in theme_map.candidates}
    # Hand-computed disclosed purity: theme-expressing share / total disclosed share.
    assert by_isin[COMPUTE].purity.score == Decimal("0.600000")  # 0.6 / (0.6 + 0.4)
    assert by_isin[SENSORS].purity.score == Decimal("0.250000")  # 0.25 / (0.25 + 0.75)
    assert by_isin[INTEG].purity.score == Decimal("0.333333")  # 0.1 / (0.1 + 0.2), half-even

    # Each candidate sits in a real stage and carries an evidence trail addressed by content.
    for candidate in theme_map.candidates:
        assert candidate.value_chain_stage in {s.name for s in theme_map.value_chain}
        assert candidate.purity.evidence_refs
        assert all(ref.startswith("sha256:") for ref in candidate.purity.evidence_refs)
        # The announcement (COMPUTE) is kept in the trail but moves no number.
        assert len(candidate.purity.evidence) >= 2


def test_score_is_arithmetic_over_disclosure_not_a_guess() -> None:
    """The purity equals the disclosed shares exactly; the trail explains the number."""
    evidence = (
        PurityEvidence(
            kind=PurityEvidenceKind.SEGMENT_DISCLOSURE,
            source="FY26 segment note",
            label="Robotics",
            revenue_fraction=Decimal("0.45"),
            expresses_theme=True,
        ),
        PurityEvidence(
            kind=PurityEvidenceKind.SEGMENT_DISCLOSURE,
            source="FY26 segment note",
            label="Other",
            revenue_fraction=Decimal("0.55"),
        ),
    )
    score = score_purity(COMPUTE, evidence)
    assert score.score == Decimal("0.450000")
    assert score.theme_disclosed_share == Decimal("0.45")
    assert score.total_disclosed_share == Decimal("1.00")
    assert score.isin == COMPUTE


# ── acceptance 2: deterministic under StubLLM and journaled with its inputs ───────────────────────


def test_mapping_is_deterministic() -> None:
    universe = _universe(COMPUTE, SENSORS, INTEG)
    args = _map_arguments(COMPUTE, SENSORS, INTEG)
    first = _mapped(universe, args)
    second = _mapped(universe, args)

    assert [c.purity.score for c in first.candidates] == [c.purity.score for c in second.candidates]
    # The whole input set hashes to the same content address on both runs (§8.3.3).
    assert first.evidence_bundle().ref().sha256 == second.evidence_bundle().ref().sha256


def test_purity_score_ref_is_stable() -> None:
    universe = _universe(COMPUTE, SENSORS, INTEG)
    theme_map = _mapped(universe, _map_arguments(COMPUTE, SENSORS, INTEG))
    a = theme_map.candidates[0].purity
    # Recomputing from the same evidence gives the same score and the same content address.
    again = score_purity(a.isin, a.evidence)
    assert again.ref == a.ref
    assert again.score == a.score


def test_proposal_entry_journals_the_mapping_with_its_inputs() -> None:
    universe = _universe(COMPUTE, SENSORS, INTEG)
    theme_map = _mapped(universe, _map_arguments(COMPUTE, SENSORS, INTEG))

    clock = FrozenClock(AS_OF)
    entry = theme_map.proposal_entry(clock=clock, case_id="case-ai-robotics")

    assert entry.decision is Decision.POLICY_PROPOSAL
    assert entry.actor is Actor.T2
    assert entry.model == MODEL
    # The call cost is priced and journaled (§5.7): a real Decimal rupee figure, never a float.
    assert entry.tokens is not None
    assert isinstance(entry.tokens.cost_inr, Decimal)
    assert entry.tokens.cost_inr > 0
    assert entry.tokens.tokens_in > 0 and entry.tokens.tokens_out > 0

    # The entry pins the exact evidence bundle of purity inputs.
    bundle_ref = theme_map.evidence_bundle(case_id="case-ai-robotics").ref()
    assert entry.evidence_snapshot_ref == bundle_ref.ref

    # The score of every candidate is in the strings-only payload, exactly.
    for candidate in theme_map.candidates:
        assert entry.payload[f"purity:{candidate.isin}"] == str(candidate.purity.score)
    assert entry.payload["theme"] == THEME


def test_evidence_bundle_round_trips_every_disclosed_input(tmp_path: Path) -> None:
    """The inputs are journaled, not just referenced: the stored bundle reloads with the exact
    disclosed revenue shares that produced the scores.
    """
    universe = _universe(COMPUTE, SENSORS, INTEG)
    theme_map = _mapped(universe, _map_arguments(COMPUTE, SENSORS, INTEG))
    bundle = theme_map.evidence_bundle()

    store = EvidenceStore(root=tmp_path / "evidence")
    ref = store.put(bundle)
    reloaded = store.load(ref)

    # Every weighted disclosure survives as an exact Decimal (a float would have been rejected).
    compute_shares = {
        item.label: item.value
        for item in reloaded.items
        if item.isin == COMPUTE and item.value is not None
    }
    assert compute_shares[f"{COMPUTE}:AI accelerators"] == Decimal("0.6")
    assert compute_shares[f"{COMPUTE}:Legacy switchgear"] == Decimal("0.4")
    assert compute_shares[f"{COMPUTE}:purity"] == Decimal("0.600000")


# ── acceptance 3: candidates drawn from the PIT universe, never a hardcoded list ──────────────────


def test_candidate_outside_universe_is_dropped() -> None:
    """The model proposes a name not in the universe; the map drops it rather than widening."""
    universe = _universe(COMPUTE, SENSORS, INTEG)  # OUT_OF_UNIVERSE deliberately absent
    theme_map = _mapped(universe, _map_arguments(COMPUTE, SENSORS, INTEG, OUT_OF_UNIVERSE))

    isins = {c.isin for c in theme_map.candidates}
    assert OUT_OF_UNIVERSE not in isins
    assert isins == {COMPUTE, SENSORS, INTEG}


def test_shrinking_the_universe_removes_the_candidate_that_leaves_it() -> None:
    """Proof the universe is the candidate source: drop an ISIN from it and its candidate goes."""
    args = _map_arguments(COMPUTE, SENSORS, INTEG)

    full = _mapped(_universe(COMPUTE, SENSORS, INTEG), args)
    assert SENSORS in {c.isin for c in full.candidates}

    smaller = _mapped(_universe(COMPUTE, INTEG), args)  # SENSORS no longer in the universe
    assert SENSORS not in {c.isin for c in smaller.candidates}
    assert {c.isin for c in smaller.candidates} == {COMPUTE, INTEG}


def test_map_with_no_candidate_in_universe_raises() -> None:
    """A map whose every proposal is outside the universe is a hardcoded list, and refused."""
    universe = _universe(COMPUTE)  # only COMPUTE is in scope
    with pytest.raises(MapError):
        _mapped(universe, _map_arguments(OUT_OF_UNIVERSE))


# ── failure modes: fail loud rather than fabricate ───────────────────────────────────────────────


def test_purity_without_disclosure_is_refused() -> None:
    """A candidate whose only evidence is an announcement discloses no share — no number is made."""
    announcement_only = (
        PurityEvidence(
            kind=PurityEvidenceKind.ANNOUNCEMENT,
            source="BSE announcement",
            label="entered robotics",
            note="press release only",
        ),
    )
    with pytest.raises(UndisclosedPurityError):
        score_purity(COMPUTE, announcement_only)


def test_float_revenue_share_is_rejected() -> None:
    """A float share would not hash reproducibly; it is refused at the boundary (CLAUDE.md)."""
    with pytest.raises(ValueError, match="exact decimal"):
        PurityEvidence(
            kind=PurityEvidenceKind.SEGMENT_DISCLOSURE,
            source="FY26 note",
            label="Robotics",
            revenue_fraction=0.6,  # type: ignore[arg-type]
            expresses_theme=True,
        )


def test_kept_candidate_without_a_share_fails_loud() -> None:
    """A proxy the model keeps but does not substantiate raises rather than getting a default."""
    universe = _universe(COMPUTE)
    args = {
        "value_chain": [{"name": "compute & silicon", "description": "chips"}],
        "candidates": [
            {
                "isin": COMPUTE,
                "name": "Compute Silicon Ltd",
                "value_chain_stage": "compute & silicon",
                "evidence": [
                    _evidence(
                        "announcement",
                        "entered AI",
                        source="BSE",
                        note="no revenue disclosed",
                    )
                ],
            }
        ],
    }
    with pytest.raises(UndisclosedPurityError):
        _mapped(universe, args)


def test_candidate_in_unknown_stage_is_refused() -> None:
    """A candidate placed in a stage the value chain does not name is a broken map."""
    universe = _universe(COMPUTE)
    args = {
        "value_chain": [{"name": "compute & silicon", "description": "chips"}],
        "candidates": [
            {
                "isin": COMPUTE,
                "name": "Compute Silicon Ltd",
                "value_chain_stage": "sensors & actuators",  # not in the value chain
                "evidence": [
                    _evidence(
                        "segment_disclosure",
                        "AI",
                        revenue_fraction="0.5",
                        expresses_theme=True,
                    ),
                    _evidence("segment_disclosure", "Other", revenue_fraction="0.5"),
                ],
            }
        ],
    }
    with pytest.raises(MapError):
        _mapped(universe, args)


def test_no_tool_call_raises() -> None:
    """A refusal (a text-only answer) is not a map: map_theme raises rather than fabricating one."""
    universe = _universe(COMPUTE, SENSORS, INTEG)
    stub = StubLLM({}, synthesize_unknown=True)  # synthesizes text, no tool call
    with pytest.raises(MapError):
        map_theme(stub, theme=THEME, universe=universe, model=MODEL)
