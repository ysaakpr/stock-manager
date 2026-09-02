"""The golden CA suite's vocabulary: one `GoldenCase` shape, its builders, and case discovery.

This is reference **B** of the two independent references EXECUTION_PLAN §4.3 demands (ratified as
B2 in AGENTIC_CONTEXT §2): adjusted closes recomputed *by hand* from the published corporate-action
terms and checked into the repo as literal `Decimal` values, so a shared-direction error in a
third-party adjusted series (reference A, `yfinance`, M2.7) cannot pass the suite. The method is
independent by construction — nothing here calls the factor engine to *produce* an expected value;
every expected close is a literal a human wrote next to the arithmetic that yields it.

A case is one self-contained file under `cases/` exposing a module-level ``CASE: GoldenCase``. The
harness (`test_golden_ca.py`) discovers every such file with `load_cases()` and asserts the M2.4
factor engine reproduces each literal. **Adding the ~13 ugly backfill cases is therefore one new
file each — no edit to the harness, no registry to append to** (acceptance 3). `cases/README` in the
suite's `README.md` spells out the recipe.

Everything is `Decimal`. Nothing here reads a clock, a database or the network (AGENTIC_CONTEXT B8).
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from dataplatform.corpactions import (
    ActionType,
    ExchangeRatioTerms,
    FaceValueTerms,
    FactorChain,
    PricePoint,
    RatioTerms,
    build_chain_for_isin,
)
from dataplatform.ingest.corp_actions import CorporateAction
from tests.golden import cases as _cases_pkg

__all__ = [
    "Expectation",
    "GoldenCase",
    "bonus",
    "build_case_chain",
    "chain_has_scaling",
    "demerger",
    "dvr_conversion",
    "inverted_chain",
    "load_cases",
    "merger",
    "split",
]


@dataclass(frozen=True, slots=True)
class Expectation:
    """One trading date's raw close and the hand-computed close the adjusted series must show.

    ``adj_close`` is a **literal** the case author wrote; ``arithmetic`` is the one-line derivation
    that produced it (``"2658.00 x 0.5 = 1329.00"``), kept beside the literal so a reader can check
    the number without re-running anything. The harness compares the M2.4 engine's output to
    ``adj_close`` — the literal is the reference, the engine the thing under test, so nothing here
    calls the engine to *produce* an expected value.
    """

    day: date
    raw_close: Decimal
    adj_close: Decimal
    arithmetic: str


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """One published corporate action (or a stack of them on one ISIN) and its checked-in truth.

    ``actions`` are the CA terms exactly as the exchange published them, normalized into the M2.1
    taxonomy; ``expectations`` are the raw closes around each ex-date and the hand-computed adjusted
    closes; ``bridged_ex_dates`` are the structural-break ex-dates whose *return* the series must
    bridge (a merger/demerger gap is not a return — §4.3 rule 3). A pure split/bonus case leaves
    ``bridged_ex_dates`` empty; a pure merger/demerger case has unit price factors, so its adjusted
    closes equal its raw closes and the meaningful assertion is the bridge.
    """

    case_id: str
    title: str
    isin: str
    published_terms: str
    actions: tuple[CorporateAction, ...]
    expectations: tuple[Expectation, ...]
    bridged_ex_dates: tuple[date, ...] = field(default=())

    def price_points(self) -> tuple[PricePoint, ...]:
        """The raw price series the derived series are computed from (invariant #3: raw only)."""
        return tuple(
            PricePoint(date=e.day, close=e.raw_close) for e in sorted(self.expectations, key=_day)
        )


def _day(e: Expectation) -> date:
    return e.day


# ── action builders ──────────────────────────────────────────────────────────────────────────
#
# Thin constructors so a case file reads as the published terms, not as pydantic boilerplate. Each
# takes the exchange's purpose string verbatim (``raw_text``) so the case shows what was published,
# and defaults ``knowable_date`` to the ex-date — the factor arithmetic under test does not depend
# on it, and a golden case is a statement about terms, not about ingestion timing.


def split(isin: str, ex_date: date, *, fv_from: str, fv_to: str, raw_text: str) -> CorporateAction:
    """A face-value SPLIT (sub-division). ``price_factor = fv_to / fv_from`` in the engine."""
    return CorporateAction(
        isin=isin,
        ex_date=ex_date,
        action_type=ActionType.SPLIT,
        terms=FaceValueTerms(from_value=Decimal(fv_from), to_value=Decimal(fv_to)),
        source="golden",
        raw_text=raw_text,
        knowable_date=ex_date,
    )


def bonus(isin: str, ex_date: date, *, new: str, held: str, raw_text: str) -> CorporateAction:
    """A BONUS issue, new-per-held. ``price_factor = held / (new + held)`` in the engine."""
    return CorporateAction(
        isin=isin,
        ex_date=ex_date,
        action_type=ActionType.BONUS,
        terms=RatioTerms(new_shares=Decimal(new), held_shares=Decimal(held)),
        source="golden",
        raw_text=raw_text,
        knowable_date=ex_date,
    )


def _structural(
    isin: str,
    ex_date: date,
    action_type: ActionType,
    *,
    received: str,
    held: str,
    raw_text: str,
) -> CorporateAction:
    return CorporateAction(
        isin=isin,
        ex_date=ex_date,
        action_type=action_type,
        terms=ExchangeRatioTerms(shares_received=Decimal(received), shares_held=Decimal(held)),
        source="golden",
        raw_text=raw_text,
        knowable_date=ex_date,
    )


def merger(isin: str, ex_date: date, *, received: str, held: str, raw_text: str) -> CorporateAction:
    """A MERGER — a structural break on the surviving ISIN (unit factor, bridged return)."""
    return _structural(
        isin, ex_date, ActionType.MERGER, received=received, held=held, raw_text=raw_text
    )


def demerger(
    isin: str, ex_date: date, *, received: str, held: str, raw_text: str
) -> CorporateAction:
    """A DEMERGER — a structural break on the parent ISIN (unit factor, bridged return)."""
    return _structural(
        isin, ex_date, ActionType.DEMERGER, received=received, held=held, raw_text=raw_text
    )


def dvr_conversion(
    isin: str, ex_date: date, *, received: str, held: str, raw_text: str
) -> CorporateAction:
    """A DVR_CONVERSION — a structural break on the ordinary ISIN (unit factor, bridged return)."""
    return _structural(
        isin, ex_date, ActionType.DVR_CONVERSION, received=received, held=held, raw_text=raw_text
    )


# ── engine helpers used by the harness ─────────────────────────────────────────────────────────


def build_case_chain(case: GoldenCase) -> FactorChain:
    """The M2.4 factor chain for a case — the object under test, built from the published terms."""
    return build_chain_for_isin(case.isin, case.actions)


def inverted_chain(chain: FactorChain) -> FactorChain:
    """The same chain with the price/quantity convention flipped — for the inversion guard.

    Swapping ``price_factor`` and ``qty_factor`` (and their cumulatives) is exactly the mistake the
    factor convention exists to prevent: adjusting by the reciprocal, so a 1:5 split *multiplies* a
    2019 close by 5 instead of by 0.2. `test_golden_ca` asserts the golden literals are *not*
    reproduced under this inversion, which is what makes acceptance 2 ("fails loudly if the factor
    convention is inverted") a property of the suite and not just a hope.
    """
    rows = tuple(
        row.model_copy(
            update={
                "price_factor": row.qty_factor,
                "qty_factor": row.price_factor,
                "cum_price_factor": row.cum_qty_factor,
                "cum_qty_factor": row.cum_price_factor,
            }
        )
        for row in chain.rows
    )
    return chain.model_copy(update={"rows": rows})


def chain_has_scaling(chain: FactorChain) -> bool:
    """True when some row actually rescales price — i.e. inverting the convention would change it.

    Pure structural-break cases (merger/demerger/DVR) carry unit factors, so inversion is a no-op on
    them; only split/bonus cases discriminate the direction. The harness uses this to route the
    inversion guard at the cases that can prove it, and asserts at least one such case exists.
    """
    return any(row.price_factor != Decimal(1) for row in chain.rows)


# ── discovery ──────────────────────────────────────────────────────────────────────────────────


def load_cases() -> list[GoldenCase]:
    """Every ``CASE`` defined by a module under `cases/`, sorted by id.

    This is the whole reason a new case is one self-contained file: discovery is by package walk, so
    a file that defines ``CASE: GoldenCase`` is picked up with no registration. A module under
    `cases/` that does not define a ``GoldenCase`` ``CASE`` is skipped, not an error, so helper
    modules could live there — though the recipe keeps `cases/` to one case per file.
    """
    found: list[GoldenCase] = []
    for info in pkgutil.iter_modules(_cases_pkg.__path__):
        module = importlib.import_module(f"{_cases_pkg.__name__}.{info.name}")
        case = getattr(module, "CASE", None)
        if isinstance(case, GoldenCase):
            found.append(case)
    return sorted(found, key=lambda c: c.case_id)
