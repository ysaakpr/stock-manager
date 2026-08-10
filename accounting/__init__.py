"""X3: token and cost accounting per decision.

Decision #12 says model choices are quality-first and cost-blind; this package is the clause that
makes "blind" a policy rather than an accident. Every model call is priced on a dated card
(`model_prices.yaml`) and lands twice — as a `token_usage` row and as the `model`/`tokens` fields
of the journal line for the decision it bought — both written from one figure, so they cannot
disagree.

`MeteredLLM` wraps any `analyst.llm.LLM`, so a stub call and a real call are accounted for by the
same code and separated afterwards only by `provider`. A model the card does not price raises
rather than booking ₹0.
"""

from accounting.tokens import (
    PRICES_PATH,
    AccountingError,
    CardMeta,
    LedgerError,
    MeteredCompletion,
    MeteredLLM,
    ModelPrice,
    NoPriceScheduleError,
    PriceCard,
    PricedUsage,
    PriceSchedule,
    Provenance,
    TokenLedger,
    TokenPricer,
    UnknownModelError,
    UnknownUsageError,
    load_price_card,
)

__all__ = [
    "PRICES_PATH",
    "AccountingError",
    "CardMeta",
    "LedgerError",
    "MeteredCompletion",
    "MeteredLLM",
    "ModelPrice",
    "NoPriceScheduleError",
    "PriceCard",
    "PriceSchedule",
    "PricedUsage",
    "Provenance",
    "TokenLedger",
    "TokenPricer",
    "UnknownModelError",
    "UnknownUsageError",
    "load_price_card",
]
