"""A3: the theme mapper — theme → value chain → listed proxies with disclosed purity scores.

A theme ("AI/Robotics") is not tradeable; the listed parts of its value chain are, and only in
proportion to how much of each business actually expresses the theme. This package is that
translation (§5.2), and it holds three guarantees, each proved in `tests/unit/test_mapper.py`:

* **A theme maps to a value chain and candidate ISINs with purity + evidence.** `map_theme` asks an
  `LLM` (X3's protocol, so `StubLLM` in tests — B4) for the value chain and the listed proxies with
  their disclosure, and returns a `ThemeMap` of stages and scored `ProxyCandidate`s.
* **Purity is disclosed and deterministic.** `score_purity` computes purity as arithmetic over
  disclosed revenue shares (`purity.py`), not as a model's guess — so the same evidence always
  yields the same number, and the `PurityScore` carries the whole content-addressed evidence trail
  behind it (a number with no evidence trail is not acceptable output).
* **Candidates are drawn from the PIT universe.** A proxy the model proposes that is not in the
  point-in-time universe (M4.2) is dropped; the mapper ranks within the universe and never widens
  it, which is the survivorship/look-ahead guard on the candidate set.

The run is journalable with its inputs: `ThemeMap.evidence_bundle` is the content-addressed set of
every purity input, and `ThemeMap.proposal_entry` is the `POLICY_PROPOSAL` line that pins it and the
call's cost (§5.7, invariant #9). The LLM boundary is X3's, and time on any journal entry comes from
an injected `Clock` (B10).
"""

from analyst.mapper.engine import (
    MAP_PURPOSE,
    MAPPER_TOOL,
    MapError,
    ProxyCandidate,
    ThemeMap,
    ValueChainStage,
    map_theme,
)
from analyst.mapper.purity import (
    DEFAULT_PURITY_PLACES,
    PURITY_METHOD,
    Fraction,
    PurityError,
    PurityEvidence,
    PurityEvidenceKind,
    PurityScore,
    UndisclosedPurityError,
    score_purity,
)

__all__ = [
    "DEFAULT_PURITY_PLACES",
    "MAPPER_TOOL",
    "MAP_PURPOSE",
    "PURITY_METHOD",
    "Fraction",
    "MapError",
    "ProxyCandidate",
    "PurityError",
    "PurityEvidence",
    "PurityEvidenceKind",
    "PurityScore",
    "ThemeMap",
    "UndisclosedPurityError",
    "ValueChainStage",
    "map_theme",
    "score_purity",
]
