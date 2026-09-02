"""A9: decision journal + evidence packs, append-only.

§0 says the journal is the product, so this package is the one every other analyst module writes
through: A5's tiered checks, A6's rotations, A7's exits, A8's rail blocks and the daily loop's
heartbeats all land in `decision_journal` via `Journal`, and every one of them carries the
content-addressed evidence bundle it was decided on.

Three rules are worth knowing before using it:

* **Append-only, both ends.** The table rejects UPDATE, DELETE and TRUNCATE at the database level
  (`reject_mutation()` triggers, `0001_init.sql`) and this package offers no method that would
  attempt one (invariant #12). A correction is a new entry.
* **No-ops are entries.** `Journal.heartbeat` exists because "checked, nothing happened" is a
  decision with evidence behind it, not an absence (invariant #9).
* **Evidence is addressed by its content.** `EvidenceStore` stores the exact bytes shown to a
  model under their sha256, so `Journal.reconstruct` returns what the agent saw or raises — never
  a plausible reassembly of it.
"""

from analyst.journal.evidence import (
    EVIDENCE_DIRNAME,
    EvidenceBundle,
    EvidenceChecksumError,
    EvidenceError,
    EvidenceItem,
    EvidenceKind,
    EvidenceNotFoundError,
    EvidenceRef,
    EvidenceStore,
    canonical_bytes,
    digest_of,
    parse_ref,
)
from analyst.journal.evidence_pack import (
    CostBurnSection,
    DataQualitySection,
    DecisionReview,
    DecisionReviewSection,
    DrawdownSection,
    EvidencePack,
    EvidencePackError,
    NavPoint,
    PackTrigger,
    PackValuation,
    RailBreachSection,
    ReturnsSection,
    SleeveTurnover,
    TaxEvent,
    TierCost,
    TurnoverTaxSection,
    generate_pack,
    render_markdown,
)
from analyst.journal.models import (
    EVIDENCE_REF_PATTERN,
    ISIN_PATTERN,
    REQUIRES_EVIDENCE,
    REQUIRES_INSTRUMENT,
    REQUIRES_RATIONALE,
    Actor,
    BreakConditionEvaluation,
    Decision,
    JournalEntry,
    Money,
    RecordedEntry,
    Sleeve,
    TokenSpend,
    Verdict,
)
from analyst.journal.writer import (
    Journal,
    JournalError,
    JournalFilter,
    Reconstruction,
    UnknownEntryError,
)

__all__ = [
    "EVIDENCE_DIRNAME",
    "EVIDENCE_REF_PATTERN",
    "ISIN_PATTERN",
    "REQUIRES_EVIDENCE",
    "REQUIRES_INSTRUMENT",
    "REQUIRES_RATIONALE",
    "Actor",
    "BreakConditionEvaluation",
    "CostBurnSection",
    "DataQualitySection",
    "Decision",
    "DecisionReview",
    "DecisionReviewSection",
    "DrawdownSection",
    "EvidenceBundle",
    "EvidenceChecksumError",
    "EvidenceError",
    "EvidenceItem",
    "EvidenceKind",
    "EvidenceNotFoundError",
    "EvidencePack",
    "EvidencePackError",
    "EvidenceRef",
    "EvidenceStore",
    "Journal",
    "JournalEntry",
    "JournalError",
    "JournalFilter",
    "Money",
    "NavPoint",
    "PackTrigger",
    "PackValuation",
    "RailBreachSection",
    "Reconstruction",
    "RecordedEntry",
    "ReturnsSection",
    "Sleeve",
    "SleeveTurnover",
    "TaxEvent",
    "TierCost",
    "TokenSpend",
    "TurnoverTaxSection",
    "UnknownEntryError",
    "Verdict",
    "canonical_bytes",
    "digest_of",
    "generate_pack",
    "parse_ref",
    "render_markdown",
]
