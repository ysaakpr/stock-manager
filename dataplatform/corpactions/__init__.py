"""D3: corporate actions + adjustment engine.

Public surface so far: the normalized action taxonomy, its structured terms models, the free-text
purpose-string normalizer and the manual-entry queue an unparseable string lands in (M2.1); the
cross-exchange reconciliation engine and its `/status/quality` queue (M2.3); the adjustment factor
chain, the price-adjusted / return / total-return series derived from it, and the retroactive
recompute-and-invalidate seam (M2.4).
"""

from dataplatform.corpactions.factors import (
    AdjustedPoint,
    FactorChain,
    FactorError,
    FactorRow,
    PricePoint,
    ReturnPoint,
    build_chain_for_isin,
    build_factor_chain,
    price_adjusted_series,
    return_series,
    total_return_series,
)
from dataplatform.corpactions.parse_terms import (
    CorporateActionNormalizer,
    ManualEntryQueue,
    ManualQueueEntry,
    ManualQueueReason,
    ParseOutcome,
    classify,
    parse_purpose,
)
from dataplatform.corpactions.recompute import (
    RecomputeResult,
    recompute_for_actions,
    recompute_isin,
    recompute_isins,
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
    SingleSourcePolicy,
    collapse_reconciled_rows,
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
    "AdjustedPoint",
    "CorporateActionNormalizer",
    "DividendKind",
    "DividendTerms",
    "ExchangeRatioTerms",
    "FaceValueTerms",
    "FactorChain",
    "FactorError",
    "FactorRow",
    "ManualEntryQueue",
    "ManualQueueEntry",
    "ManualQueueReason",
    "NameChangeTerms",
    "ParseOutcome",
    "ParsedAction",
    "PersistCounts",
    "PricePoint",
    "PriceTerms",
    "QualityFlagRecord",
    "RatioTerms",
    "RecomputeResult",
    "ReconcileError",
    "ReconciledAction",
    "ReconciliationConflict",
    "ReconciliationReason",
    "ReconciliationResult",
    "ReturnPoint",
    "RightsTerms",
    "SingleSourcePolicy",
    "Terms",
    "UnquantifiedTerms",
    "build_chain_for_isin",
    "build_factor_chain",
    "classify",
    "collapse_reconciled_rows",
    "describe",
    "eligible_for_factor_chain",
    "load_reconciled_actions",
    "parse_purpose",
    "persist_reconciliation",
    "price_adjusted_series",
    "recompute_for_actions",
    "recompute_isin",
    "recompute_isins",
    "reconcile",
    "return_series",
    "total_return_series",
]
