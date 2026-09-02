"""The M4 gate's headline reference backtest — inputs and the engine run (M4.9).

EXECUTION_PLAN §5.2 / the M4 gate box: *3 stocks, 5 years, monthly SIP, spanning one split, one
merger and one demerger, with every cashflow, quantity, cost and the final XIRR hand-computed and
checked in as literal expected values.* This module holds the **inputs** — the monthly instalment
schedule, the whole-share basket bought each month, and the three corporate actions — and
:func:`build_reference_book`, which drives the real backtest accounting stack over them and returns
the book. The **expected literals** live beside it in ``expected.py``; the test in
``test_reference_case.py`` runs this engine and asserts the two agree to the paisa.

Nothing here is a toy re-implementation of the accounting: the cost of every buy comes from the one
shared cost model (``execution.costs`` — invariant #4), the corporate actions are applied by the
real ``backtest.accounting.PortfolioBook``, and the XIRR is the real ``backtest.xirr``. The only
thing this module *decides* is the scenario: which names, which levels, how many shares a month.

The three names are drawn from the M2 golden CA suite so their CA arithmetic is already
independently verified (reference B, ``tests/golden/cases/``):

* **IRCTC** — the 1:5 face-value split of 2021-10-28 (``irctc_split_2021``). Quantity x5, basis
  unchanged.
* **HDFC** — the 42:25 amalgamation of HDFC Ltd into HDFC Bank, effective 2023-07
  (``hdfc_merger_2023``). The SIP buys HDFC **Ltd** until the merger, whereupon the holding converts
  to HDFC **Bank** at 42:25 and the SIP continues in the survivor. The acquired ISIN ceases; its
  whole basis carries to the survivor.
* **RIL → JFS** — the 1:1 demerger of Jio Financial Services from Reliance Industries, ex 2023-07-20
  (``jiofin_demerger_2023``). A JFS position is created 1:1 and a fraction of RIL's basis moves into
  it.

**Modelling choices, stated so the hand-computation is unambiguous:**

* **Fully-deterministic monthly SIP.** On the 1st of each month for five years (2021-05-01 …
  2026-04-01, 60 instalments) the plan deposits a *fixed* ₹170,000 and buys a fixed whole-share
  basket at that month's reference levels. Levels are held piecewise-constant between corporate
  actions, so — exactly as a fixed-rupee SIP would under constant prices — the basket is the same
  each month within a phase, and the unspent remainder carries forward as free cash (§5.6). This is
  what makes 60 instalments hand-computable: a phase is one month's arithmetic times its length.
* **Fill price = reference level, no slippage.** The reference case pins the accounting — cashflows,
  quantities, costs, corporate actions, XIRR — so a buy fills exactly at the stated level; the
  SimBroker slippage model is exercised separately (M4.8). Broker *charges* are real, from the
  shared cost model at the rates in force on each trade date (four rate regimes span the window).
* **Reference levels** are round representative figures in the neighbourhood of the real prices of
  the period; the load-bearing, independently-verified facts are the CA *ratios* (1:5, 42:25, 1:1),
  not the levels. The demerger carves 10% of RIL's value into JFS (RIL ₹2000 cum → ₹1800 + JFS
  ₹200), matching the JioFin golden case's ~9.2% (₹261.85 of ₹2841.85) rounded to a clean tenth.

Money is ``Decimal`` throughout (CLAUDE.md); dates arrive with the events, no clock is read
(invariant #11); every join is on the ISIN (invariant #2). Nothing here touches the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from backtest.accounting import PortfolioBook
from execution.broker import Exchange, Fill, Side
from execution.costs import CostModel, Trade, load_rate_card

__all__ = [
    "HDFC_BANK",
    "HDFC_LTD",
    "INSTALMENT",
    "IRCTC",
    "JFS",
    "RIL",
    "TERMINAL_DATE",
    "TERMINAL_PRICES",
    "build_reference_book",
    "instalment_dates",
]

# ── the names (ISIN is the only identity — invariant #2) ──────────────────────────────────────────

IRCTC = "INE335Y01020"  # split 1:5, ex 2021-10-28
HDFC_LTD = "INE001A01036"  # acquired in the 42:25 amalgamation
HDFC_BANK = "INE040A01034"  # surviving entity
RIL = "INE002A01018"  # demerger parent
JFS = "INE758E01017"  # Jio Financial Services, received 1:1 in the demerger

# ── the SIP ───────────────────────────────────────────────────────────────────────────────────────

#: Fixed monthly deposit — a five-year, 60-instalment SIP.
INSTALMENT = Decimal("170000")

#: Corporate-action effective dates, as published (see the golden cases). They fall *between*
#: instalments, so each is applied once, after the instalment of its month.
_SPLIT_DATE = date(2021, 10, 28)
_MERGER_DATE = date(2023, 7, 13)
_DEMERGER_DATE = date(2023, 7, 20)

#: Reference levels, piecewise-constant between corporate actions (see the module docstring).
_IRCTC_PRE_SPLIT = Decimal("4000")
_IRCTC_POST_SPLIT = Decimal("800")
_HDFC_LTD_PRICE = Decimal("2800")
_HDFC_BANK_PRICE = Decimal("1600")
_RIL_PRE_DEMERGER = Decimal("2000")
_RIL_POST_DEMERGER = Decimal("1800")

#: Whole-share counts bought each month (constant within a phase — see docstring).
_IRCTC_PRE_QTY = 10  # x4000 = ₹40,000 turnover
_IRCTC_POST_QTY = 50  # x800  = ₹40,000 turnover (same turnover across the split)
_HDFC_LTD_QTY = 25  # x2800 = ₹70,000 turnover
_HDFC_BANK_QTY = 40  # x1600 = ₹64,000 turnover
_RIL_PRE_QTY = 25  # x2000 = ₹50,000 turnover
_RIL_POST_QTY = 25  # x1800 = ₹45,000 turnover

#: The 1:5 split's face-value terms and the two structural-break exchange ratios (verified in M2).
_SPLIT_FV_FROM = Decimal("10")
_SPLIT_FV_TO = Decimal("2")
_MERGER_RECEIVED = Decimal("42")
_MERGER_HELD = Decimal("25")
_DEMERGER_RECEIVED = Decimal("1")
_DEMERGER_HELD = Decimal("1")
#: JFS is a 10% carve-out of RIL's fair value on the demerger date (₹200 of ₹2000).
_DEMERGER_COST_FRACTION = Decimal("0.10")

#: When the book is marked to market and the XIRR is struck, and the levels used.
TERMINAL_DATE = date(2026, 4, 30)
TERMINAL_PRICES: dict[str, Decimal] = {
    IRCTC: Decimal("1100"),
    HDFC_BANK: Decimal("2000"),
    RIL: Decimal("2000"),
    JFS: Decimal("350"),
}


def instalment_dates() -> tuple[date, ...]:
    """The 60 SIP dates: the 1st of each month, 2021-05-01 through 2026-04-01."""
    dates: list[date] = []
    year, month = 2021, 5
    for _ in range(60):
        dates.append(date(year, month, 1))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return tuple(dates)


@dataclass(frozen=True, slots=True)
class _Buy:
    """One whole-share buy the SIP makes on a given date — the scenario's atomic instruction."""

    isin: str
    quantity: int
    price: Decimal


def _basket_for(when: date) -> tuple[_Buy, ...]:
    """The whole-share basket the SIP buys on ``when`` — the deterministic monthly rule.

    IRCTC switches price/quantity across the split (same turnover); the HDFC leg switches from the
    acquired to the surviving ISIN across the merger; RIL switches level across the demerger. The
    boundaries are the CA effective dates, so an instalment before a CA sees the pre-CA leg and one
    after sees the post-CA leg.
    """
    if when < _SPLIT_DATE:
        irctc = _Buy(IRCTC, _IRCTC_PRE_QTY, _IRCTC_PRE_SPLIT)
    else:
        irctc = _Buy(IRCTC, _IRCTC_POST_QTY, _IRCTC_POST_SPLIT)

    if when < _MERGER_DATE:
        hdfc = _Buy(HDFC_LTD, _HDFC_LTD_QTY, _HDFC_LTD_PRICE)
    else:
        hdfc = _Buy(HDFC_BANK, _HDFC_BANK_QTY, _HDFC_BANK_PRICE)

    if when < _DEMERGER_DATE:
        ril = _Buy(RIL, _RIL_PRE_QTY, _RIL_PRE_DEMERGER)
    else:
        ril = _Buy(RIL, _RIL_POST_QTY, _RIL_POST_DEMERGER)

    return (irctc, hdfc, ril)


def _fill_for(buy: _Buy, when: date, cost_model: CostModel) -> Fill:
    """Price ``buy`` with the shared cost model (invariant #4) and return the fill it settles at."""
    trade = Trade(
        isin=buy.isin,
        trade_date=when,
        side=Side.BUY,
        quantity=buy.quantity,
        price=buy.price,
        exchange=Exchange.NSE,
    )
    cost = cost_model.charge(trade)
    return Fill(
        isin=buy.isin,
        session=when,
        side=Side.BUY,
        quantity=buy.quantity,
        exchange=Exchange.NSE,
        reference_price=buy.price,
        slippage_bps=Decimal("0"),
        fill_price=buy.price,
        cost=cost,
    )


def build_reference_book() -> PortfolioBook:
    """Run the five-year SIP through the real accounting stack and return the resulting book.

    Each month: deposit the fixed instalment, then buy the month's basket (priced by the shared cost
    model). After the instalment whose month contains a corporate action, apply that action to the
    book — the 1:5 split, then (at the 2023-07 boundary) the 42:25 merger and the 1:1 demerger, in
    published date order. The book carries free cash forward and marks nothing until asked.
    """
    book = PortfolioBook()
    cost_model = CostModel(load_rate_card())

    split_done = merger_done = demerger_done = False
    for when in instalment_dates():
        book.deposit(when, INSTALMENT)
        for buy in _basket_for(when):
            book.record_fill(_fill_for(buy, when, cost_model))

        # Apply each corporate action once, after the instalment of the month it falls in.
        if not split_done and when >= date(_SPLIT_DATE.year, _SPLIT_DATE.month, 1):
            book.apply_split(IRCTC, from_face_value=_SPLIT_FV_FROM, to_face_value=_SPLIT_FV_TO)
            split_done = True
        if not merger_done and when >= date(_MERGER_DATE.year, _MERGER_DATE.month, 1):
            book.apply_merger(
                HDFC_LTD,
                surviving_isin=HDFC_BANK,
                shares_received=_MERGER_RECEIVED,
                shares_held=_MERGER_HELD,
            )
            merger_done = True
        if not demerger_done and when >= date(_DEMERGER_DATE.year, _DEMERGER_DATE.month, 1):
            book.apply_demerger(
                RIL,
                resulting_isin=JFS,
                shares_received=_DEMERGER_RECEIVED,
                shares_held=_DEMERGER_HELD,
                cost_fraction_to_resulting=_DEMERGER_COST_FRACTION,
            )
            demerger_done = True

    return book
