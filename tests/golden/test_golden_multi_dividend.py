"""Golden: a regular and a special dividend on one ex-date — both reinvested, each once.

Federal-Mogul Goetze (India), INE529A01010, ex 2026-09-04: an interim ₹7.50 and a special ₹86.50,
declared together. BSE published two records; NSE one line. Until components (migration 0020) each
feed kept one dividend for the day and the total-return series reinvested ₹7.50 of the ₹94 paid —
the close fell from 613.70 to 501.45 on the ex-date, and the TR series showed almost all of it as a
loss.

The pipeline runs from the feeds' own text: both exchange strings are parsed (NSE's through the
production parser, which cuts its line in two), reconciled, collapsed to one action per event, and
reinvested by the M2.4 engine. The expected closes are literals with the arithmetic beside them;
nothing here calls the engine to produce one (reference B, `casebook.py`).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Final

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.corpactions import (
    ActionType,
    PricePoint,
    collapse_reconciled_rows,
    reconcile,
    total_return_series,
)
from dataplatform.corpactions.parse_terms import parse_purpose
from dataplatform.identity.master import (
    Exchange,
    IdentityMaster,
    ListingStatus,
    Security,
    SymbolWindow,
)
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.ingest.nse.corp_actions import parse as parse_nse

pytestmark = pytest.mark.golden

ISIN: Final = "INE529A01010"
EX: Final = date(2026, 9, 4)
NSE_LINE: Final = "Interim Dividend - Rs 7.50 Per Share & Special Dividend Rs 86.50 Per Share"
BSE_RECORDS: Final = ("Interim Dividend - Rs. - 7.5000", "Special Dividend - Rs. - 86.5000")

#: Raw NSE closes around the ex-date (L1, 2026-09-02 .. 2026-09-07).
CLOSES: Final = {
    date(2026, 9, 2): Decimal("612.40"),
    date(2026, 9, 3): Decimal("613.70"),
    date(2026, 9, 4): Decimal("501.45"),
    date(2026, 9, 7): Decimal("482.05"),
}

#: Hand-computed TR closes, to 4 dp. P = 613.70, the cum-dividend close of 2026-09-03.
#: Each dividend scales every earlier close by (P - D) / P (`factors.total_return_series`).
EXPECTED_TR: Final = {
    # 612.40 x (606.20 / 613.70) x (527.20 / 613.70) = 612.40 x 0.848553222 = 519.6540
    date(2026, 9, 2): Decimal("519.6540"),
    # 613.70 x (606.20 / 613.70) x (527.20 / 613.70) = 606.20 x 527.20 / 613.70 = 520.7571
    date(2026, 9, 3): Decimal("520.7571"),
    # on and after the ex-date nothing is left to reinvest: the raw close
    date(2026, 9, 4): Decimal("501.4500"),
    date(2026, 9, 7): Decimal("482.0500"),
}

#: What the one-dividend-per-feed-per-day key gave: the ₹7.50 alone. 613.70 x 606.20 / 613.70.
ONE_DIVIDEND_TR_0903: Final = Decimal("606.2000")


def _bse_rows() -> list[CorporateAction]:
    rows = []
    for text in BSE_RECORDS:
        parsed = parse_purpose(text, source="bse_corp_actions").action
        assert parsed is not None
        rows.append(
            CorporateAction.from_parsed(
                parsed, isin=ISIN, source="bse_corp_actions", ex_date=EX, knowable_date=EX
            )
        )
    return rows


def _nse_rows() -> list[CorporateAction]:
    master = IdentityMaster(
        [SymbolWindow(Exchange.NSE, "FMGOETZE", date(2000, 1, 1), None, ISIN, "EQ", "golden")],
        securities=[
            Security(
                ISIN, "Federal-Mogul Goetze", Exchange.NSE, ListingStatus.ACTIVE, date(2000, 1, 1)
            )
        ],
        listings=[],
    )
    payload = json.dumps(
        [{"symbol": "FMGOETZE", "isin": ISIN, "exDate": "04-Sep-2026", "subject": NSE_LINE}]
    ).encode()
    clock = FrozenClock(datetime(2026, 9, 1, 18, 0, tzinfo=IST))
    return list(parse_nse(payload, filename="golden.json", master=master, clock=clock).actions)


def _tr(actions: list[CorporateAction]) -> dict[date, Decimal]:
    prices = [PricePoint(date=d, close=c) for d, c in CLOSES.items()]
    return {
        p.date: p.adj_close.quantize(Decimal("0.0001"))
        for p in total_return_series(actions, prices)
    }


def test_both_dividends_are_reinvested_once_each() -> None:
    result = reconcile([*_bse_rows(), *_nse_rows()])
    assert result.queue == ()
    events = collapse_reconciled_rows([a for r in result.reconciled for a in r.sources])
    assert [e.action_type for e in events] == [ActionType.DIVIDEND, ActionType.DIVIDEND]
    assert _tr(list(events)) == EXPECTED_TR


def test_the_literals_tell_one_dividend_from_two() -> None:
    """The golden literal is not what the old key produced: one dividend per feed per day."""
    first_per_feed = {a.source: a for a in reversed([*_bse_rows(), *_nse_rows()])}
    result = reconcile(list(first_per_feed.values()))
    events = collapse_reconciled_rows([a for r in result.reconciled for a in r.sources])
    tr = _tr(list(events))
    assert tr[date(2026, 9, 3)] == ONE_DIVIDEND_TR_0903
    assert tr[date(2026, 9, 3)] != EXPECTED_TR[date(2026, 9, 3)]
