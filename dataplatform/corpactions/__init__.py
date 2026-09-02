"""D3: corporate actions + adjustment engine.

Public surface so far: the normalized action taxonomy, its structured terms models, the free-text
purpose-string normalizer and the manual-entry queue an unparseable string lands in (M2.1); the
cross-exchange reconciliation engine and its `/status/quality` queue (M2.3). The factor chain and
the retroactive recompute path arrive with M2.4.
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

# Imported after taxonomy/parse_terms so those submodules are fully loaded first: reconcile pulls
# in `dataplatform.ingest.corp_actions`, which imports back into this package's submodules.
from dataplatform.corpactions.reconcile import (
    CA_RECONCILIATION_CHECK,
    DEFAULT_EX_DATE_TOLERANCE_DAYS,
    PersistCounts,
    QualityFlagRecord,
    ReconciledAction,
    ReconcileError,
    ReconciliationConflict,
    ReconciliationReason,
    ReconciliationResult,
    eligible_for_factor_chain,
    load_reconciled_actions,
    persist_reconciliation,
    reconcile,
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
    "CA_RECONCILIATION_CHECK",
    "DEFAULT_EX_DATE_TOLERANCE_DAYS",
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
    "PersistCounts",
    "PriceTerms",
    "QualityFlagRecord",
    "RatioTerms",
    "ReconcileError",
    "ReconciledAction",
    "ReconciliationConflict",
    "ReconciliationReason",
    "ReconciliationResult",
    "RightsTerms",
    "Terms",
    "UnquantifiedTerms",
    "classify",
    "describe",
    "eligible_for_factor_chain",
    "load_reconciled_actions",
    "parse_purpose",
    "persist_reconciliation",
    "reconcile",
]
