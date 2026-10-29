"""D3: several dividends per feed per ex-date — stored apart, reconciled by component.

`corporate_actions` kept one dividend per feed per day, so a regular and a special dividend declared
together kept whichever row landed first. BSE publishes them as two records; NSE as one line joined
by `/`, `&`, `+`, `And` or nothing. Each feed dropped a different one, and the two then disagreed:
139 of the 183 open dividend RATIO_MISMATCH flags (2026-10-24) were that, not a real disagreement,
and every dropped special dividend was missing from the total-return series.

The rows below are the live store's own (ISIN, ex-date, raw text). Offline: no network, no database.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Final

import pytest
from pydantic import ValidationError

from dataplatform.clock import IST, FrozenClock
from dataplatform.corpactions import (
    ActionType,
    ReconciliationReason,
    SingleSourcePolicy,
    collapse_reconciled_rows,
    reconcile,
)
from dataplatform.corpactions.parse_terms import (
    names_several_dividends,
    parse_purpose,
    split_compound,
    split_dividends,
)
from dataplatform.corpactions.taxonomy import DividendKind, DividendTerms, RatioTerms
from dataplatform.identity.master import (
    Exchange,
    IdentityMaster,
    ListingStatus,
    Security,
    SymbolWindow,
)
from dataplatform.ingest.corp_actions import (
    CorporateAction,
    dividend_component,
    dividend_components,
)
from dataplatform.ingest.nse.corp_actions import parse as parse_nse

NSE: Final = "nse_corp_actions"
BSE: Final = "bse_corp_actions"


# ── splitting one published line into its dividends ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "pieces"),
    [
        (
            "Dividend - Rs 4.50 Per Share & Special Dividend - Rs 7.50 Per Share",
            ("Dividend - Rs 4.50 Per Share &", "Special Dividend - Rs 7.50 Per Share"),
        ),
        (
            "Interim Dividend Rs 15.50 Per Share + Special Dividend Rs 4/- Per Share",
            ("Interim Dividend Rs 15.50 Per Share +", "Special Dividend Rs 4/- Per Share"),
        ),
        (
            "Interim Dividend - Rs 9 Per Share Special Dividend - Rs 18 Per Share",
            ("Interim Dividend - Rs 9 Per Share", "Special Dividend - Rs 18 Per Share"),
        ),
        (
            "Dividend - Rs 26 Per Share And Special Dividend Rs 22 Per Share",
            ("Dividend - Rs 26 Per Share And", "Special Dividend Rs 22 Per Share"),
        ),
    ],
)
def test_a_line_naming_two_dividends_is_cut_into_two(line: str, pieces: tuple[str, ...]) -> None:
    assert split_dividends(line) == pieces
    assert names_several_dividends(line)


@pytest.mark.parametrize(
    "line",
    [
        # One distribution, however it is qualified.
        "Special Interim Dividend Rs 12/- Per Share",
        "Interim Dividend - Rs 2 Per Share",
        # A breakdown of one distribution into its parts — split, they would be counted twice.
        "Distribution - Rs 12.71 Per Unit Consisting Of Rs 11.22 Per Unit Taxable Dividend "
        "& Rs 1.38 Per Unit Exempt Dividend",
        # The whole stated before the first phrase.
        "Rs 17.50 Per Share Being Dividend Rs 10 And Special Dividend Rs 7.50",
        # A piece would state two amounts.
        "Dividend Rs 10 Rs 2 Per Share Special Dividend Rs 5",
    ],
)
def test_a_line_that_is_not_a_list_of_dividends_stays_whole(line: str) -> None:
    assert split_dividends(line) == (line,)


def test_each_piece_parses_to_its_own_dividend() -> None:
    line = "Dividend - Rs 4.50 Per Share & Special Dividend - Rs 7.50 Per Share"
    terms = [parse_purpose(piece, source=NSE).action for piece in split_dividends(line)]
    assert [(_dividend(t.terms).dividend_kind, _amount(t.terms)) for t in terms if t] == [
        (DividendKind.UNSPECIFIED, Decimal("4.5")),
        (DividendKind.SPECIAL, Decimal("7.5")),
    ]
    # Whole, the same line is two amounts in one dividend and kept neither — the old behaviour.
    whole = parse_purpose(line, source=NSE).action
    assert whole is not None and whole.terms == DividendTerms(dividend_kind=DividendKind.SPECIAL)


def _dividend(terms: object) -> DividendTerms:
    assert isinstance(terms, DividendTerms)
    return terms


def _amount(terms: object) -> Decimal | None:
    return _dividend(terms).amount_inr


# ── components: the key that keeps them apart ──────────────────────────────────────────────────


def _div(kind: DividendKind, amount: str | None) -> DividendTerms:
    return DividendTerms(dividend_kind=kind, amount_inr=None if amount is None else Decimal(amount))


def test_components_are_the_kind_then_an_ordinal_within_one_line() -> None:
    terms = [
        _div(DividendKind.UNSPECIFIED, "0.62"),  # a REIT's taxable dividend
        _div(DividendKind.UNSPECIFIED, "0.20"),  # ...and its exempt one, same kind, same line
        _div(DividendKind.SPECIAL, "1"),
        RatioTerms(new_shares=Decimal(1), held_shares=Decimal(1)),
        _div(DividendKind.UNSPECIFIED, "0.01"),
    ]
    assert dividend_components(terms) == (
        "UNSPECIFIED",
        "UNSPECIFIED#2",
        "SPECIAL",
        "",
        "UNSPECIFIED#3",
    )
    # The stored blob gives what the migration backfills: `ratio_terms->>'dividend_kind'`.
    blob = json.loads(_div(DividendKind.FINAL, "1.25").model_dump_json())
    assert dividend_component(blob) == "FINAL" == dividend_component(_div(DividendKind.FINAL, "1"))


def ca(
    isin: str,
    source: str,
    ex: date,
    terms: DividendTerms,
    raw: str,
    *,
    component: str | None = None,
) -> CorporateAction:
    fields: dict[str, object] = {
        "isin": isin,
        "source": source,
        "ex_date": ex,
        "action_type": ActionType.DIVIDEND,
        "terms": terms,
        "raw_text": raw,
        "knowable_date": ex,
    }
    if component is not None:
        fields["component"] = component
    return CorporateAction.model_validate(fields)


LINDE: Final = "INE473A01011"
LINDE_EX: Final = date(2025, 8, 7)
LINDE_NSE_LINE: Final = "Dividend - Rs 4.50 Per Share & Special Dividend - Rs 7.50 Per Share"


def test_a_dividend_defaults_to_its_kind_and_refuses_another() -> None:
    row = ca(LINDE, BSE, LINDE_EX, _div(DividendKind.FINAL, "4.5"), "Final Dividend - Rs. - 4.5000")
    assert row.component == "FINAL"
    assert ca(LINDE, NSE, LINDE_EX, _div(DividendKind.SPECIAL, "7.5"), "x", component="SPECIAL#2")
    with pytest.raises(ValidationError, match="does not name its kind"):
        ca(LINDE, BSE, LINDE_EX, _div(DividendKind.FINAL, "4.5"), "x", component="SPECIAL")
    with pytest.raises(ValidationError, match="only a dividend"):
        CorporateAction(
            isin=LINDE,
            source=BSE,
            ex_date=LINDE_EX,
            action_type=ActionType.BONUS,
            terms=RatioTerms(new_shares=Decimal(1), held_shares=Decimal(1)),
            raw_text="Bonus issue 1:1",
            knowable_date=LINDE_EX,
            component="FINAL",
        )


def test_the_nse_parser_files_each_dividend_of_a_line_under_its_own_component() -> None:
    master = IdentityMaster(
        [SymbolWindow(Exchange.NSE, "LINDEINDIA", date(2000, 1, 1), None, LINDE, "EQ", "test")],
        securities=[
            Security(LINDE, "Linde India", Exchange.NSE, ListingStatus.ACTIVE, date(2000, 1, 1))
        ],
        listings=[],
    )
    payload = json.dumps(
        [
            {
                "symbol": "LINDEINDIA",
                "isin": LINDE,
                "exDate": "07-Aug-2025",
                "subject": LINDE_NSE_LINE,
                "series": "EQ",
                "caBroadcastDate": "24-Jul-2025",
            }
        ]
    ).encode()
    clock = FrozenClock(datetime(2026, 10, 24, 10, 0, tzinfo=IST))
    result = parse_nse(payload, filename="t.json", master=master, clock=clock)
    assert [(a.component, a.terms, a.raw_text) for a in result.actions] == [
        ("UNSPECIFIED", _div(DividendKind.UNSPECIFIED, "4.5"), LINDE_NSE_LINE),
        ("SPECIAL", _div(DividendKind.SPECIAL, "7.5"), LINDE_NSE_LINE),
    ]
    assert result.queued == ()


# ── reconciliation, component by component ─────────────────────────────────────────────────────


def _linde() -> list[CorporateAction]:
    """Linde India's 2025-08-07 regular and special, as each feed published them."""
    return [
        ca(LINDE, BSE, LINDE_EX, _div(DividendKind.FINAL, "4.5"), "Final Dividend - Rs. - 4.5000"),
        ca(
            LINDE,
            BSE,
            LINDE_EX,
            _div(DividendKind.SPECIAL, "7.5"),
            "Special Dividend - Rs. - 7.5000",
        ),
        ca(LINDE, NSE, LINDE_EX, _div(DividendKind.SPECIAL, "7.5"), LINDE_NSE_LINE),
        ca(LINDE, NSE, LINDE_EX, _div(DividendKind.UNSPECIFIED, "4.5"), LINDE_NSE_LINE),
    ]


def test_two_feeds_two_dividends_reconcile_pairwise() -> None:
    """Paired by terms, not by row order. By date alone NSE's special (which sorts first) met BSE's
    final — ₹7.50 against ₹4.50 — and the day read as a ratio mismatch: this fails if the component
    pass is removed."""
    result = reconcile(_linde())
    assert result.queue == ()
    assert sorted(_amount(r.terms) or Decimal(0) for r in result.reconciled) == [
        Decimal("4.5"),
        Decimal("7.5"),
    ]
    # The unspecified NSE regular takes BSE's "final": the money agrees, one feed names the kind.
    regular = next(r for r in result.reconciled if _amount(r.terms) == Decimal("4.5"))
    assert regular.terms == _div(DividendKind.FINAL, "4.5")
    assert all(r.cross_verified for r in result.reconciled)


def test_both_dividends_reach_the_factor_chain_once_each() -> None:
    rows = [a for r in reconcile(_linde()).reconciled for a in r.sources]
    events = collapse_reconciled_rows(rows)
    assert sorted((e.ex_date, _amount(e.terms)) for e in events) == [
        (LINDE_EX, Decimal("4.5")),
        (LINDE_EX, Decimal("7.5")),
    ]


BPCL: Final = "INE029A01011"
BPCL_EX: Final = date(2017, 2, 28)
BPCL_LINE: Final = "Interim Dividend Rs 15.50 Per Share + Special Dividend Rs 4/- Per Share"


def _bpcl(*, bse_total: str) -> list[CorporateAction]:
    """BPCL's 2017-02-28: NSE itemizes ₹15.50 + ₹4, BSE one record; and BSE's lone 2000 dividend."""
    return [
        ca(BPCL, NSE, BPCL_EX, _div(DividendKind.INTERIM, "15.5"), BPCL_LINE),
        ca(BPCL, NSE, BPCL_EX, _div(DividendKind.SPECIAL, "4"), BPCL_LINE),
        ca(
            BPCL,
            BSE,
            BPCL_EX,
            _div(DividendKind.INTERIM, bse_total),
            f"Interim Dividend - Rs. - {bse_total}",
        ),
        ca(
            BPCL,
            BSE,
            date(2000, 7, 25),
            _div(DividendKind.UNSPECIFIED, "12.5"),
            "Dividend - Rs. - 12.5000",
        ),
    ]


def test_a_same_day_extra_never_unreconciles_an_old_one_feed_dividend() -> None:
    """Before components, NSE stored one row here; split, its extra would sit beside BSE's 2000
    dividend as "leftovers on both sides" and queue that one — which ACCEPT had admitted — with
    it. The extra is the day's business, not a misdating: this fails if it rejoins the leftovers."""
    result = reconcile(_bpcl(bse_total="15.5"), single_source_policy=SingleSourcePolicy.ACCEPT)
    assert result.queue == ()
    old = next(r for r in result.reconciled if r.ex_date == date(2000, 7, 25))
    assert old.cross_verified is False  # single-source, admitted as before
    special = next(r for r in result.reconciled if r.terms == _div(DividendKind.SPECIAL, "4"))
    assert special.source_ids == (NSE,) and not special.cross_verified


def test_a_same_day_extra_on_a_disputed_day_is_queued_with_the_dispute() -> None:
    """BSE states the ₹19.50 total, NSE ₹15.50 + ₹4: the pair disagrees, so the extra is not
    admitted on one feed's word either — but the 2000 dividend still is."""
    result = reconcile(_bpcl(bse_total="19.5"), single_source_policy=SingleSourcePolicy.ACCEPT)
    reasons = sorted(c.reason for c in result.queue)
    assert reasons == [ReconciliationReason.RATIO_MISMATCH, ReconciliationReason.SINGLE_SOURCE]
    assert [r.ex_date for r in result.reconciled] == [date(2000, 7, 25)]


def test_a_one_dividend_line_keeps_the_fingerprint_it_had() -> None:
    """Flags raised before components existed must still be found, not closed and re-raised."""
    bse = ca(LINDE, BSE, LINDE_EX, _div(DividendKind.FINAL, "4.5"), "Final Dividend - Rs. - 4.5000")
    nse = ca(LINDE, NSE, LINDE_EX, _div(DividendKind.FINAL, "9"), "Dividend - Rs 9 Per Share")
    (conflict,) = reconcile([bse, nse]).queue
    parts = [LINDE, "DIVIDEND", "RATIO_MISMATCH"]
    for a in sorted((bse, nse), key=lambda a: (a.source, a.ex_date, a.raw_text)):
        parts.extend((a.source, a.ex_date.isoformat(), a.raw_text))
    assert conflict.fingerprint() == hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def test_two_components_of_one_line_never_share_a_flag() -> None:
    nse = [
        ca(LINDE, NSE, LINDE_EX, _div(DividendKind.SPECIAL, "7.5"), LINDE_NSE_LINE),
        ca(LINDE, NSE, LINDE_EX, _div(DividendKind.UNSPECIFIED, "4.5"), LINDE_NSE_LINE),
    ]
    queue = reconcile(nse).queue  # QUEUE: two single-source components of one line
    assert len(queue) == 2
    assert len({c.fingerprint() for c in queue}) == 2


def test_the_split_respects_the_compound_separator_first() -> None:
    line = "AGM/ Final Dividend- Rs 1.25 Per Share/ Special Dividend- Rs 5 Per Share"
    segments = split_compound(line)
    assert [p for s in segments for p in split_dividends(s)] == list(segments)
