"""A4: the §5.3 thesis engine — falsifiable break conditions, versioned, ratified before a core buy.

A thesis is what makes a core holding a *decision* rather than a bet: it states the driver, how
purely the holding expresses the theme, the evidence that would confirm it, and — the part the plan
insists on — the falsifiable break conditions that would end it. This package owns three guarantees,
each proved in `tests/unit/test_thesis.py`:

* **No unfalsifiable break condition can exist.** `assert_falsifiable` runs inside `BreakCondition`
  construction, so a vague condition ("the story weakens") is rejected at draft time with a reason —
  there is no way to hold one in memory, let alone ratify one (§5.3).
* **No core buy without a ratified thesis.** `authorize_buy` is the gate the order path passes; a
  `CORE` buy with no ratified thesis for its ISIN raises and yields no authorization (§5.5). A
  tactical position carries a journaled `TacticalRationale` instead.
* **Editing a ratified thesis is a new version.** Every model is frozen; `Thesis.revise()` returns
  version N+1 in `PROPOSAL` and leaves the ratified version's record untouched (§5.1). The
  `ThesisBook` keeps the whole history so "which thesis backed that buy" stays answerable.

The governance artifact (`Ratification`, `RatificationKind`) is M5.3's, reused unchanged: a thesis
is ratified by the same act as a policy set (§5.1). The LLM boundary is X3's `LLM` protocol, so the
engine drafts against `StubLLM` in tests and a real client in production without a code change (B4).
"""

from analyst.thesis.engine import (
    THESIS_TOOL,
    BuyAuthorization,
    CoreBuyError,
    DraftError,
    TacticalRationaleRequiredError,
    UnratifiedCoreBuyError,
    authorize_buy,
    draft_thesis,
)
from analyst.thesis.models import (
    THESIS_CONTENT_FIELDS,
    BreakCondition,
    BreakConditionType,
    EvaluationTier,
    Purity,
    Ratification,
    RatificationKind,
    Sleeve,
    TacticalRationale,
    Thesis,
    ThesisError,
    ThesisRatificationMismatchError,
    ThesisStatus,
    ThesisVersionError,
    UnfalsifiableBreakConditionError,
    assert_falsifiable,
    thesis_digest,
)
from analyst.thesis.ratify import ThesisBook, ThesisKey, UnknownThesisError

__all__ = [
    "THESIS_CONTENT_FIELDS",
    "THESIS_TOOL",
    "BreakCondition",
    "BreakConditionType",
    "BuyAuthorization",
    "CoreBuyError",
    "DraftError",
    "EvaluationTier",
    "Purity",
    "Ratification",
    "RatificationKind",
    "Sleeve",
    "TacticalRationale",
    "TacticalRationaleRequiredError",
    "Thesis",
    "ThesisBook",
    "ThesisError",
    "ThesisKey",
    "ThesisRatificationMismatchError",
    "ThesisStatus",
    "ThesisVersionError",
    "UnfalsifiableBreakConditionError",
    "UnknownThesisError",
    "UnratifiedCoreBuyError",
    "assert_falsifiable",
    "authorize_buy",
    "draft_thesis",
    "thesis_digest",
]
