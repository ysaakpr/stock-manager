"""D3: a "bonus" of preference shares, debentures or warrants is never an equity bonus.

NSE files TVS Holdings' 2023-03-24 distribution of non-convertible redeemable preference shares as
`Bonus Ncrps 1:116`. Parsed as an equity BONUS it was a 116:117 factor on every earlier price and
one share per 116 credited to the books — a share split nobody made — while the close fell from
4,816.70 to 3,853.40 on the day for a reason that factor did not explain. BSE files the same day as
`Scheme of Arrangement`, which is what it is: a structural event the equity factor chain does not
price (a break, never a return), represented exactly as a demerger is.

Every non-equity bonus line in L0 (2026-10-25: seven NSE lines, five ISINs) is below, verbatim.
Offline: no network, no database.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final

import pytest

from dataplatform.corpactions import (
    ActionType,
    RatioTerms,
    ReconciliationReason,
    SingleSourcePolicy,
    UnquantifiedTerms,
    build_chain_for_isin,
    collapse_reconciled_rows,
    reconcile,
)
from dataplatform.corpactions.parse_terms import (
    classify,
    names_non_equity_bonus,
    parse_purpose,
    split_compound,
)
from dataplatform.ingest.ca_reparse_non_equity import plan_refilings
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.store.l2_overlay import OverlayKind, plan_events

NSE: Final = "nse_corp_actions"
BSE: Final = "bse_corp_actions"
TVS: Final = "INE105A01035"
NCRPS_EX: Final = date(2023, 3, 24)

#: Every non-equity bonus line in the NSE and BSE CA payloads in L0, as published.
L0_LINES: Final = (
    "Bonus Ncrps 1:116",  # TVSHLTD 2023-03-24
    "Scheme Of Arrangement - Bonus Ncrps 46:1",  # TVSHLTD 2026-09-08
    "Scheme Of Arrangement - Bonus Ncrps 3:1",  # SIYSIL 2026-08-21
    "Scheme Of Arrangement - Bonus Ncrps 4:1",  # SIYSIL 2026-08-21, TVSMOTOR 2025-08-25
    "Scheme Of Arrangement - Bonus Ncrps 1:10",  # RADIOCITY 2023-01-13
    " Scheme Of Arangement- Bonus - 1 Debenture For 1 Equity Share Held",  # BRITANNIA 2019, 2021
)


@pytest.mark.parametrize(
    "line",
    [
        *L0_LINES,
        # The other instruments the rule names, in the feeds' own phrasing.
        "Bonus Preference Shares 1:1",
        "Bonus issue of Non Convertible Debentures",
        "Bonus NCDs 1:1",
        "Bonus Warrants 1:5",
        "Bonus Redeemable Preference Shares 1:10",
    ],
)
def test_a_non_equity_bonus_is_a_scheme_of_arrangement_never_an_equity_bonus(line: str) -> None:
    """Fails if a preference-share (or debenture, warrant) bonus is admitted as an equity bonus."""
    assert names_non_equity_bonus(line)
    assert ActionType.BONUS not in classify(line)
    outcome = parse_purpose(line, source=NSE)
    assert outcome.action is not None
    assert outcome.action.action_type is ActionType.SCHEME_OF_ARRANGEMENT
    assert isinstance(outcome.action.terms, UnquantifiedTerms)  # its ratio is not an exchange
    assert outcome.queue_entry is not None  # a human sees it once, as BSE's bare scheme


@pytest.mark.parametrize(
    "line",
    ["Bonus 1:1", "Bonus issue 1:1", "BONUS 1:1", "Bonus Equity Shares 1:2", "Bonus issue 3:2"],
)
def test_an_equity_bonus_is_still_an_equity_bonus(line: str) -> None:
    assert not names_non_equity_bonus(line)
    outcome = parse_purpose(line, source=BSE)
    assert outcome.action is not None
    assert outcome.action.action_type is ActionType.BONUS
    assert isinstance(outcome.action.terms, RatioTerms)


def test_a_compound_line_keeps_its_equity_bonus_beside_a_dividend() -> None:
    segments = split_compound("Bonus 2:1/Dividend- Rs 1.60 Per Share")
    assert [classify(s) for s in segments] == [(ActionType.BONUS,), (ActionType.DIVIDEND,)]


def _row(
    source: str, ex: date, action_type: ActionType, terms: object, raw: str
) -> CorporateAction:
    return CorporateAction.model_validate(
        {
            "isin": TVS,
            "source": source,
            "ex_date": ex,
            "action_type": action_type,
            "terms": terms,
            "raw_text": raw,
            "knowable_date": ex,
        }
    )


#: The store as it stands: NSE's row parsed before the fix, BSE's scheme and its genuine 2009 bonus.
STORED_NCRPS: Final = _row(
    NSE,
    NCRPS_EX,
    ActionType.BONUS,
    RatioTerms(new_shares=Decimal(1), held_shares=Decimal(116)),
    "Bonus Ncrps 1:116",
)
BSE_SCHEME: Final = _row(
    BSE, NCRPS_EX, ActionType.SCHEME_OF_ARRANGEMENT, UnquantifiedTerms(), "Scheme of Arrangement"
)
BSE_2009_BONUS: Final = _row(
    BSE,
    date(2009, 10, 9),
    ActionType.BONUS,
    RatioTerms(new_shares=Decimal(1), held_shares=Decimal(1)),
    "Bonus issue 1:1",
)


def test_a_stored_non_equity_bonus_is_never_admitted_whatever_the_policy() -> None:
    result = reconcile(
        [STORED_NCRPS, BSE_SCHEME, BSE_2009_BONUS], single_source_policy=SingleSourcePolicy.ACCEPT
    )
    (queued,) = result.queue
    assert queued.reason is ReconciliationReason.SINGLE_SOURCE
    assert queued.records == (STORED_NCRPS,)
    assert RatioTerms(new_shares=Decimal(1), held_shares=Decimal(116)) not in {
        r.terms for r in result.reconciled
    }
    # The genuine 2009 1:1 bonus (the close halved, 250.45 -> 119.70) is admitted single-source.
    chain = build_chain_for_isin(
        TVS, collapse_reconciled_rows(a for r in result.reconciled for a in r.sources)
    )
    assert chain.price_factor_asof(date(2009, 10, 8)) == Decimal("0.5")
    assert chain.price_factor_asof(date(2023, 3, 23)) == Decimal(1)


def test_even_an_agreeing_feed_cannot_reconcile_a_non_equity_bonus_into_a_factor() -> None:
    """Two feeds both reading the same NCRPS line as an equity bonus still get no factor."""
    bse_twin = STORED_NCRPS.model_copy(update={"source": BSE})
    result = reconcile([STORED_NCRPS, bse_twin], single_source_policy=SingleSourcePolicy.ACCEPT)
    assert result.reconciled == ()
    assert len(result.queue) == 2


def test_the_refiled_row_is_a_scheme_both_feeds_agree_on_and_a_break_not_a_factor() -> None:
    (refiling,) = plan_refilings([STORED_NCRPS, BSE_SCHEME, BSE_2009_BONUS])
    refiled = refiling.refiled
    assert (refiled.isin, refiled.ex_date, refiled.source, refiled.raw_text) == (
        TVS,
        NCRPS_EX,
        NSE,
        "Bonus Ncrps 1:116",
    )
    assert refiled.action_type is ActionType.SCHEME_OF_ARRANGEMENT
    result = reconcile(
        [refiled, BSE_SCHEME, BSE_2009_BONUS], single_source_policy=SingleSourcePolicy.ACCEPT
    )
    assert result.queue == ()
    scheme = next(r for r in result.reconciled if r.ex_date == NCRPS_EX)
    assert scheme.cross_verified  # NSE and BSE now say the same thing
    events = plan_events(
        TVS, recorded=[refiled], curated=(), after=date(2023, 3, 1), as_of=date(2023, 4, 1)
    )
    assert [e.kind for e in events] == [OverlayKind.BREAK]  # flagged; the move is not a return
