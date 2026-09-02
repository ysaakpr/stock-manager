"""The golden corporate-action suite — reference B (EXECUTION_PLAN §4.3, M2.6).

This harness runs every case discovered under `cases/` against the M2.4 factor engine and asserts
the engine reproduces the hand-computed adjusted closes checked in as literals. It is deliberately
tiny and case-agnostic: all the truth lives in the case files, so the ~13 ugly backfill cases are
added one file at a time with **no change here** (acceptance 3).

The three acceptance criteria map to tests below:

1. *All 7 named cases have literal expected values with the arithmetic shown* —
   ``test_all_seven_named_cases_present`` (the ids exist) and
   ``test_adjusted_closes_match_hand_computed`` (each literal is reproduced by the engine); the
   arithmetic sits beside each literal in its file.
2. *The suite fails loudly if the factor convention is inverted* —
   ``test_golden_literals_are_direction_sensitive`` proves the literals cannot pass under an
   inverted (reciprocal) convention, and ``test_suite_contains_a_direction_discriminating_case``
   guarantees a split/bonus case exists to discriminate direction (a suite of only structural
   breaks, whose factors are 1, could not). Inverting `factors.py` itself also fails criterion 1.
3. *Adding a new case is a single self-contained file with no harness changes* —
   the whole harness is driven by ``load_cases()``; there is no per-case code here.

Offline and deterministic (AGENTIC_CONTEXT B8): no network, no database, no clock.
"""

from __future__ import annotations

import pytest

from dataplatform.corpactions import price_adjusted_series, return_series
from tests.golden.casebook import (
    GoldenCase,
    build_case_chain,
    chain_has_scaling,
    inverted_chain,
    load_cases,
)

pytestmark = pytest.mark.golden

#: The seven cases §4.3 names explicitly. Discovery must find at least these; the backfill's ugly
#: cases add more, and this set does not need editing when they do.
NAMED_CASE_IDS = frozenset(
    {
        "ltim_merger_2022",
        "jiofin_demerger_2023",
        "hdfc_merger_2023",
        "ril_bonus_2024",
        "irctc_split_2021",
        "tatamotors_dvr_2024",
        "tatamotors_demerger_2025",
    }
)

CASES: list[GoldenCase] = load_cases()

#: Cases whose chain actually rescales a price — the ones an inverted convention would break. Only
#: split/bonus cases qualify; structural breaks carry unit factors and cannot discriminate.
SCALING_CASES: list[GoldenCase] = [c for c in CASES if chain_has_scaling(build_case_chain(c))]

#: Cases that assert a bridged structural-break crossing (§4.3 rule 3).
BRIDGING_CASES: list[GoldenCase] = [c for c in CASES if c.bridged_ex_dates]


def _case_id(case: GoldenCase) -> str:
    return case.case_id


def test_all_seven_named_cases_present() -> None:
    """Acceptance 1 (part): every §4.3-named case is present as its own file."""
    present = {c.case_id for c in CASES}
    missing = NAMED_CASE_IDS - present
    assert not missing, f"golden cases missing from cases/: {sorted(missing)}"


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_adjusted_closes_match_hand_computed(case: GoldenCase) -> None:
    """Acceptance 1 (core): the engine reproduces every hand-computed literal (reference B).

    This is also the primary inversion guard: if `factors.py` adjusted by the reciprocal, a split's
    pre-ex close would come out multiplied instead of divided and this assertion would fail.
    """
    chain = build_case_chain(case)
    series = {p.date: p.adj_close for p in price_adjusted_series(chain, case.price_points())}
    for exp in case.expectations:
        assert series[exp.day] == exp.adj_close, (
            f"{case.case_id} {exp.day.isoformat()}: engine {series[exp.day]} != "
            f"hand-computed {exp.adj_close} ({exp.arithmetic})"
        )


def test_suite_contains_a_direction_discriminating_case() -> None:
    """Acceptance 2 (part): at least one case can prove the convention's direction.

    Without a split or bonus in the suite every factor is 1 and an inverted convention would pass
    silently — so the inversion guard would be vacuous. This fails if the suite ever degrades to
    structural breaks only.
    """
    assert SCALING_CASES, "no split/bonus case present; the inversion guard would be vacuous"


@pytest.mark.parametrize("case", SCALING_CASES, ids=_case_id)
def test_golden_literals_are_direction_sensitive(case: GoldenCase) -> None:
    """Acceptance 2 (core): the literals cannot be reproduced under an inverted convention.

    Flip ``price_factor`` and ``qty_factor`` (adjust by the reciprocal) and at least one checked-in
    close must no longer match — otherwise the literal would not actually pin the factor direction.
    """
    inverted = inverted_chain(build_case_chain(case))
    series = {p.date: p.adj_close for p in price_adjusted_series(inverted, case.price_points())}
    mismatches = [exp for exp in case.expectations if series[exp.day] != exp.adj_close]
    assert mismatches, (
        f"{case.case_id}: inverting the factor convention left every golden close unchanged, so "
        "the case does not pin the direction"
    )


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_returns_bridge_exactly_the_structural_breaks(case: GoldenCase) -> None:
    """§4.3 rule 3: the return series bridges every structural-break ex-date and nothing else.

    A merger/demerger/DVR crossing carries ``ret=None`` and ``bridged=True`` (its gap is a
    structural event, not a return); every other day carries a real return. This is the property
    that keeps a demerger's carve-out from entering a return index as a spurious crash.
    """
    rets = return_series(build_case_chain(case), case.price_points())
    bridged = {r.date for r in rets if r.bridged}
    assert bridged == set(case.bridged_ex_dates), (
        f"{case.case_id}: bridged {sorted(d.isoformat() for d in bridged)} != expected "
        f"{sorted(d.isoformat() for d in case.bridged_ex_dates)}"
    )
    for r in rets:
        if r.bridged:
            assert r.ret is None, f"{case.case_id} {r.date}: bridged crossing must have ret=None"
        else:
            assert r.ret is not None, f"{case.case_id} {r.date}: non-break day must have a return"
