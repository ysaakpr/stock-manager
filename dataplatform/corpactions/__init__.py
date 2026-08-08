"""D3: corporate actions + adjustment engine.

Public surface so far (M2.1): the normalized action taxonomy, its structured terms models, the
free-text purpose-string normalizer, and the manual-entry queue an unparseable string lands in.
The factor chain and the retroactive recompute path arrive with M2.4.
"""

from dataplatform.corpactions.parse_terms import (
    CorporateActionNormalizer,
    ManualEntryQueue,
    ManualQueueEntry,
    ManualQueueReason,
    ParseOutcome,
    classify,
    parse_purpose,
)
from dataplatform.corpactions.taxonomy import (
    TERMS_ADAPTER,
    TERMS_BY_ACTION,
    ActionType,
    DividendKind,
    DividendTerms,
    ExchangeRatioTerms,
    FaceValueTerms,
    NameChangeTerms,
    ParsedAction,
    PriceTerms,
    RatioTerms,
    RightsTerms,
    Terms,
    UnquantifiedTerms,
    describe,
)

__all__ = [
    "TERMS_ADAPTER",
    "TERMS_BY_ACTION",
    "ActionType",
    "CorporateActionNormalizer",
    "DividendKind",
    "DividendTerms",
    "ExchangeRatioTerms",
    "FaceValueTerms",
    "ManualEntryQueue",
    "ManualQueueEntry",
    "ManualQueueReason",
    "NameChangeTerms",
    "ParseOutcome",
    "ParsedAction",
    "PriceTerms",
    "RatioTerms",
    "RightsTerms",
    "Terms",
    "UnquantifiedTerms",
    "classify",
    "describe",
    "parse_purpose",
]
