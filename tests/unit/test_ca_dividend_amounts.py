"""M2.3: the dividend RATIO_MISMATCH flags our own code raised, and the rows behind them.

Of the 48 open dividend RATIO_MISMATCH flags (2026-11-01), 21 were ours, not the feeds':

* **19: an amount stated as `<n> Per Share` with no currency marker before it.** NSE writes
  `Dividend - 1.40 Per Share`, `Special Dividend 243 Per Share`, `Dividend - 1 Rs Per Share`. The
  money regex demands a marker, so each was stored as "amount not stated" and disputed BSE's row
  stating the very same figure. Nine were the special half of a regular-plus-special line.
* **2: `Special Interim Dividend`.** One phrase naming two kinds; the parser keeps SPECIAL, BSE
  files the same money as INTERIM, and the pair read as two different dividends.

And the stale rows reach the store only through `ca_dividend_reparse`, which until now touched
only day-groups that re-parse into several dividends — ten of the 19 are single-dividend lines.

The strings below are the live store's own (ISIN, ex-date, raw text). Offline: no network, no
database.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final

import pytest

from dataplatform.corpactions import (
    ActionType,
    ReconciliationReason,
    SingleSourcePolicy,
    collapse_reconciled_rows,
    reconcile,
)
from dataplatform.corpactions.parse_terms import parse_purpose, split_compound, split_dividends
from dataplatform.corpactions.taxonomy import DividendKind, DividendTerms
from dataplatform.ingest.ca_dividend_reparse import plan_reparse
from dataplatform.ingest.corp_actions import CorporateAction

NSE: Final = "nse_corp_actions"
BSE: Final = "bse_corp_actions"


def _dividends(line: str) -> list[tuple[DividendKind, Decimal | None]]:
    """What NSE's parser makes of one subject line: each dividend's kind and amount, in order."""
    pieces = [p for seg in split_compound(line) for p in split_dividends(seg)]
    out: list[tuple[DividendKind, Decimal | None]] = []
    for piece in pieces:
        action = parse_purpose(piece, source=NSE).action
        if action is not None and action.action_type is ActionType.DIVIDEND:
            terms = action.terms
            assert isinstance(terms, DividendTerms)
            out.append((terms.dividend_kind, terms.amount_inr))
    return out


U, F, S, INTERIM = (
    DividendKind.UNSPECIFIED,
    DividendKind.FINAL,
    DividendKind.SPECIAL,
    DividendKind.INTERIM,
)


# ── an amount stated per share with no currency marker before it ──────────────────────────────


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # INE109C01017 2016-09-12 (BSE: Rs. - 1.4000)
        ("Aannual General Meeting/Dividend - 1.40 Per Share", [(U, Decimal("1.4"))]),
        # INE094A01015 2018-02-28 (BSE: Rs. - 14.5000)
        ("Interim Dividend - 14.50 Per Share", [(INTERIM, Decimal("14.5"))]),
        # INE032B01021 2024-06-20: the marker after the number (BSE: Rs. - 1.0000)
        ("Annual General Meeting /Dividend - 1 Rs Per Share", [(U, Decimal("1"))]),
        # INE760L01018 2022-08-19: a broken marker, the amount still per share (BSE: 1.0000)
        ("Annual General Meeting/Dividend - R E 1 Per Share", [(U, Decimal("1"))]),
        # INE058A01010 2020-06-29: the special half (BSE: Final 106, Special 243)
        (
            "Dividend - Rs 106 Per Share Special Dividend 243 Per Share",
            [(U, Decimal("106")), (S, Decimal("243"))],
        ),
        # INE764D01017 2018-08-02 (BSE: Final 15, Special 35)
        (
            "Annual General Meeting/Final Dividend- Rs 15 Per Share/"
            " Special Dividend- 35 Per Share",
            [(F, Decimal("15")), (S, Decimal("35"))],
        ),
        # INE531A01024 2025-06-23 (BSE: Final 2.50, Special 1.25)
        (
            "Dividend - Rs 2.50 Per Share/Special Dividend - 1.25 Per Share",
            [(U, Decimal("2.5")), (S, Decimal("1.25"))],
        ),
        # Unchanged: the marked forms read exactly as before.
        ("Dividend - Rs 10/- Per Share", [(U, Decimal("10"))]),
        ("Interim Dividend - Re 0.80 Per Share", [(INTERIM, Decimal("0.8"))]),
    ],
)
def test_an_amount_stated_per_share_is_an_amount(
    line: str, expected: list[tuple[DividendKind, Decimal | None]]
) -> None:
    """Fails if the per-share form is dropped: each of these then parses as "not stated"."""
    assert _dividends(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        # NSE's own typos (BSE: 0.30, 0.70, 0.50): left unquantified, never read as Rs 30 or Rs 70.
        "Annual General Meeting/Dividend - Re 0..30 Per Share",
        "Dividend - Rs 0 .70 Per Share",
        "Interim Dividend - Rs Per 0.50 Share",
        # The same typo with no marker: nothing else stands between `0..30` and Rs 30.
        "Dividend - 0..30 Per Share",
        "Dividend - .70 Per Share",
        # Paise are not rupees: never Rs 50.
        "Dividend - 50 Paise Per Share",
        # No amount at all.
        "Annual General Meeting/Dividend - Rs  Per Share",
    ],
)
def test_a_garbled_amount_is_never_read_as_a_different_one(line: str) -> None:
    """Fails if the lookbehind is removed (`0..30` would read as 30) or paise read as rupees."""
    ((_, amount),) = _dividends(line)
    assert amount is None


def _row(
    isin: str, source: str, ex: date, kind: DividendKind, amount: str | None, raw: str
) -> CorporateAction:
    return CorporateAction.model_validate(
        {
            "isin": isin,
            "source": source,
            "ex_date": ex,
            "action_type": ActionType.DIVIDEND,
            "terms": DividendTerms(
                dividend_kind=kind, amount_inr=None if amount is None else Decimal(amount)
            ),
            "raw_text": raw,
            "knowable_date": ex,
        }
    )


def test_the_reparsed_row_agrees_with_bse_and_clears_the_mismatch() -> None:
    """INE372C01037 2017-08-10: NSE's `2.25 Per Share` now meets BSE's final Rs 2.25."""
    ex = date(2017, 8, 10)
    line = "Annual General Meeting/Dividend - 2.25 Per Share"
    ((kind, amount),) = _dividends(line)
    nse = _row("INE372C01037", NSE, ex, kind, str(amount), line)
    bse = _row("INE372C01037", BSE, ex, F, "2.25", "Final Dividend - Rs. - 2.2500")

    result = reconcile([nse, bse])

    assert not result.queue
    (agreed,) = result.reconciled
    assert agreed.cross_verified
    assert agreed.terms == DividendTerms(dividend_kind=F, amount_inr=Decimal("2.25"))


# ── `Special Interim Dividend`: one phrase, two kinds ─────────────────────────────────────────

BPCL: Final = "INE029A01011"
BPCL_EX: Final = date(2017, 3, 24)
SPECIAL_INTERIM: Final = "Special Interim Dividend Rs 12/- Per Share"


def _special_interim_pair() -> list[CorporateAction]:
    return [
        _row(BPCL, NSE, BPCL_EX, S, "12", SPECIAL_INTERIM),
        _row(BPCL, BSE, BPCL_EX, INTERIM, "12", "Interim Dividend - Rs. - 12.0000"),
    ]


def test_a_special_interim_dividend_meets_the_interim_one() -> None:
    """Fails if the rule is removed: the pair is then a RATIO_MISMATCH, as it was."""
    result = reconcile(_special_interim_pair())

    assert not result.queue
    (agreed,) = result.reconciled
    assert agreed.cross_verified
    # The kind both feeds wrote: BSE's INTERIM, which NSE's phrase also names.
    assert agreed.terms == DividendTerms(dividend_kind=INTERIM, amount_inr=Decimal("12"))


def test_the_folded_event_carries_the_same_terms() -> None:
    """The factor chain's fold must accept what reconcile accepted, or it raises on stale marks."""
    (folded,) = collapse_reconciled_rows(_special_interim_pair())
    assert folded.terms == DividendTerms(dividend_kind=INTERIM, amount_inr=Decimal("12"))
    assert folded.source == BSE


@pytest.mark.parametrize(
    ("nse_kind", "nse_amount", "nse_raw"),
    [
        # Different money, whatever the words. (Kinds named outright differently on the same
        # money — INE100A01010 2019-11-04 — reconcile on the amount since 2026-11-01: rule C,
        # `test_ca_dividend_rules`.)
        (S, "11", "Special Interim Dividend Rs 11/- Per Share"),
        (INTERIM, "11", "Interim Dividend - Rs 11 Per Share"),
    ],
)
def test_kinds_or_money_stated_differently_are_still_a_mismatch(
    nse_kind: DividendKind, nse_amount: str, nse_raw: str
) -> None:
    """Fails if the rule is loosened to any same-day pair: it must never pick between amounts."""
    nse = _row(BPCL, NSE, BPCL_EX, nse_kind, nse_amount, nse_raw)
    bse = _row(BPCL, BSE, BPCL_EX, INTERIM, "12", "Interim Dividend - Rs. - 12.0000")

    result = reconcile([nse, bse], single_source_policy=SingleSourcePolicy.ACCEPT)

    assert [c.reason for c in result.queue] == [ReconciliationReason.RATIO_MISMATCH]


def test_a_line_listing_several_dividends_never_names_a_kind_for_another() -> None:
    """`Interim Dividend Rs 15.50 + Special Dividend Rs 4` names both kinds because it lists two
    dividends, not because one is both; its special half never meets BSE's interim."""
    line = "Interim Dividend Rs 15.50 Per Share + Special Dividend Rs 4/- Per Share"
    nse = _row(BPCL, NSE, BPCL_EX, S, "4", line)
    bse = _row(BPCL, BSE, BPCL_EX, INTERIM, "4", "Interim Dividend - Rs. - 4.0000")
    # A busy day — two dividends in each feed — so rule C (a lone pair, `test_ca_dividend_rules`)
    # cannot reconcile the special 4 and the interim 4 on the amount; only this fill could.
    nse_other = _row(BPCL, NSE, BPCL_EX, U, "15.5", line)
    bse_other = _row(BPCL, BSE, BPCL_EX, F, "9", "Final Dividend - Rs. - 9.0000")

    result = reconcile([nse, bse, nse_other, bse_other])

    assert result.reconciled == ()
    assert [c.reason for c in result.queue] == [ReconciliationReason.RATIO_MISMATCH] * 2


# ── the repair: a single-dividend day-group the parser now reads differently ──────────────────


def test_the_reparse_rewrites_a_restated_single_dividend_and_nothing_else() -> None:
    """Fails if `plan_reparse` selects only day-groups with several dividends (it did until now)."""
    ex = date(2016, 9, 12)
    line = "Aannual General Meeting/Dividend - 1.40 Per Share"
    stale = _row("INE109C01017", NSE, ex, U, None, line).model_copy(
        update={"knowable_date": date(2026, 9, 3)}
    )
    fresh = _row("INE109C01017", NSE, ex, U, "1.4", line).model_copy(
        update={"knowable_date": date(2026, 11, 1)}
    )
    unchanged = _row("INE844O01030", NSE, ex, U, "2.5", "Dividend - Rs 2.50 Per Share")

    plan = plan_reparse(
        [stale, unchanged],
        {
            (stale.isin, ex, NSE): (fresh,),
            (unchanged.isin, ex, NSE): (unchanged,),
        },
    )

    assert [g.key[0] for g in plan.groups] == ["INE109C01017"]
    assert plan.deletes == (stale,)
    (inserted,) = plan.inserts
    assert inserted.terms == fresh.terms
    # Knowable when the stored row was, not when the re-parse ran (invariant #7).
    assert inserted.knowable_date == date(2026, 9, 3)
