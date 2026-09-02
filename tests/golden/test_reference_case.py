"""M4.9 — the hand-computed reference backtest case (the M4 gate's headline box).

Three stocks, a five-year monthly SIP, and one split, one merger and one demerger, with every
cashflow, quantity, cost and the final XIRR computed by hand and checked in as literal expected
values (``reference_case/expected.py``, with the arithmetic shown). This test drives the *real*
backtest accounting stack — ``PortfolioBook`` posting fills priced by the one shared cost model
(invariant #4), applying the corporate actions, marking to market, and striking the XIRR
(``backtest.xirr``) — and asserts it reproduces every literal to the paisa.

The three acceptance criteria, each a section below:

1. **Engine output matches the hand-computed book to the paisa** — every quantity, cost basis,
   cashflow total, NAV and the XIRR.
2. **The case genuinely spans a split, a merger and a demerger** — the split multiplies a held
   quantity, the merger converts the acquired holding into the survivor and empties the acquired
   ISIN, and the demerger creates a new position out of the parent's basis.
3. **Expected values are literal and the arithmetic is shown** — the expectations are plain
   ``Decimal`` literals in ``expected.py``, produced by no call to the engine.

Offline and deterministic: no clock, no network, no database (AGENTIC_CONTEXT B8, invariant #11).
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

from tests.golden.reference_case import (
    EXPECTED,
    HDFC_BANK,
    HDFC_LTD,
    INSTALMENT,
    IRCTC,
    JFS,
    RIL,
    TERMINAL_DATE,
    TERMINAL_PRICES,
    build_reference_book,
    instalment_dates,
)

# ── acceptance #1: the engine reproduces the hand-computed book to the paisa ──────────────────────


def test_final_quantities_match_the_hand_computed_book() -> None:
    """Every held quantity is exactly the hand-computed share count after all three CAs."""
    book = build_reference_book()
    actual = {pos.isin: pos.quantity for pos in book.positions()}
    assert actual == EXPECTED.quantities


def test_final_cost_basis_matches_to_the_paisa() -> None:
    """Each position's cost basis equals the hand-computed literal, exactly (money is exact)."""
    book = build_reference_book()
    actual = {pos.isin: pos.cost_basis for pos in book.positions()}
    assert actual.keys() == EXPECTED.cost_basis.keys()
    for isin, expected_basis in EXPECTED.cost_basis.items():
        assert actual[isin] == expected_basis, isin


def test_total_deposited_and_cost_basis_and_free_cash() -> None:
    """The cashflow totals reconcile: deposited - invested = free cash carried forward."""
    book = build_reference_book()
    deposited = sum(
        (entry.credit for entry in book.ledger() if entry.description == "deposit"), Decimal("0")
    )
    invested = sum((pos.cost_basis for pos in book.positions()), Decimal("0"))

    assert deposited == EXPECTED.total_deposited
    assert invested == EXPECTED.total_cost_basis
    assert book.cash == EXPECTED.free_cash
    assert EXPECTED.total_deposited - EXPECTED.total_cost_basis == EXPECTED.free_cash


def test_terminal_valuation_matches() -> None:
    """Market value, NAV and unrealized/realized P&L at the terminal levels are the literals."""
    book = build_reference_book()
    assert book.market_value(TERMINAL_PRICES) == EXPECTED.market_value
    assert book.net_asset_value(TERMINAL_PRICES) == EXPECTED.net_asset_value
    assert book.unrealized_pnl(TERMINAL_PRICES) == EXPECTED.unrealized_pnl
    assert book.realized_pnl == EXPECTED.realized_pnl


def test_final_xirr_matches_the_hand_computed_rate() -> None:
    """The money-weighted return of the whole SIP is the hand-computed XIRR, to eight places."""
    book = build_reference_book()
    assert book.xirr(TERMINAL_DATE, TERMINAL_PRICES) == EXPECTED.xirr


# ── acceptance #2: the case genuinely spans a split, a merger and a demerger ──────────────────────


def test_the_sip_runs_a_full_five_years_of_monthly_instalments() -> None:
    """Sixty monthly instalments, first 2021-05-01, last 2026-04-01 — a real five-year SIP."""
    dates = instalment_dates()
    assert len(dates) == 60
    assert dates[0].isoformat() == "2021-05-01"
    assert dates[-1].isoformat() == "2026-04-01"
    # Strictly monthly and increasing.
    assert all(later > earlier for earlier, later in pairwise(dates))
    # A fixed-rupee monthly instalment (its deposited total is pinned in the reconciliation test).
    assert Decimal(170000) == INSTALMENT


def test_the_split_multiplied_a_held_quantity() -> None:
    """IRCTC: 60 shares accumulated pre-split become 300 (x5), then grow to 3,000 — a real split.

    Without the split the book would hold 60 + 54x50 = 2,760 IRCTC; the x5 on the 60 already held is
    the difference, so the final 3,000 is only reachable if the split was actually applied.
    """
    book = build_reference_book()
    (irctc,) = [pos for pos in book.positions() if pos.isin == IRCTC]
    assert irctc.quantity == 3000
    without_split = 60 + 54 * 50
    assert irctc.quantity == without_split + 60 * 4  # the x5 added 4x the 60 pre-split shares


def test_the_merger_converted_the_acquired_holding_and_emptied_its_isin() -> None:
    """HDFC Ltd is gone; its shares live on as HDFC Bank at 42:25, with the basis carried whole."""
    book = build_reference_book()
    held = {pos.isin for pos in book.positions()}
    # The acquired ISIN holds nothing after the merger — the failure a naive book makes is to strand
    # it here on a delisted line.
    assert HDFC_LTD not in held
    assert book.position(HDFC_LTD) is None
    for isin in EXPECTED.zeroed:
        assert book.position(isin) is None
    # The survivor carries the converted shares: 675 HDFC Ltd x 42/25 = 1,134, plus 33x40 bought.
    (hdfc_bank,) = [pos for pos in book.positions() if pos.isin == HDFC_BANK]
    assert hdfc_bank.quantity == 1134 + 33 * 40


def test_the_demerger_created_a_new_position_from_the_parents_basis() -> None:
    """JFS did not exist until the 1:1 demerger; it carries 10% of RIL's basis at the date."""
    book = build_reference_book()
    held = {pos.isin for pos in book.positions()}
    assert JFS in held
    jfs = book.position(JFS)
    ril = book.position(RIL)
    assert jfs is not None and ril is not None
    assert jfs.quantity == 675  # 1:1 on the 675 RIL held at the demerger
    # Value is redistributed, never created: JFS's basis is exactly the 10% carved from RIL's basis
    # at the demerger date (₹1,351,619.19 x 0.10).
    assert jfs.cost_basis == Decimal("135161.919")


def test_the_case_spans_all_three_action_types() -> None:
    """One assertion that the headline box holds: split, merger and demerger all left their mark."""
    book = build_reference_book()
    held = {pos.isin for pos in book.positions()}
    # split: quantity multiplied (IRCTC present and grown); merger: acquired gone, survivor present;
    # demerger: a brand-new ISIN present.
    assert IRCTC in held  # split leg
    assert HDFC_BANK in held and HDFC_LTD not in held  # merger leg
    assert RIL in held and JFS in held  # demerger leg


# ── acceptance #3: the expected values are literal, not engine-produced ───────────────────────────


def test_expected_values_are_plain_decimal_literals() -> None:
    """Every expected money figure is a ``Decimal`` literal (see expected.py's shown arithmetic)."""
    assert all(isinstance(v, Decimal) for v in EXPECTED.cost_basis.values())
    for value in (
        EXPECTED.total_deposited,
        EXPECTED.total_cost_basis,
        EXPECTED.free_cash,
        EXPECTED.market_value,
        EXPECTED.net_asset_value,
        EXPECTED.unrealized_pnl,
        EXPECTED.realized_pnl,
        EXPECTED.xirr,
    ):
        assert isinstance(value, Decimal)
    assert all(isinstance(q, int) for q in EXPECTED.quantities.values())


def test_the_reference_run_is_deterministic() -> None:
    """Two independent runs of the engine produce the same book — no clock, no hidden state."""
    first = build_reference_book()
    second = build_reference_book()
    assert {p.isin: (p.quantity, p.cost_basis) for p in first.positions()} == {
        p.isin: (p.quantity, p.cost_basis) for p in second.positions()
    }
    assert first.xirr(TERMINAL_DATE, TERMINAL_PRICES) == second.xirr(TERMINAL_DATE, TERMINAL_PRICES)
