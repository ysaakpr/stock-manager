"""M4.6: the portfolio book — XIRR, and the corporate actions paper books silently get wrong.

The three acceptance criteria are the three things a naive backtest book breaks on, so each has a
test that fails if the arithmetic is reversed (AGENTIC_CONTEXT §5.4):

* **XIRR on an irregular SIP** must match an independently-computed value — the same ACT/365 root a
  spreadsheet's ``XIRR`` finds — or every return figure built on it is wrong.
* **A split** must change the share count without changing the position's value; the classic bug is
  multiplying the quantity while leaving the per-share basis alone, inventing value from nothing.
* **A demerger** must *create* the resulting position and move basis into it; the classic bug is
  losing the holding (and its value) entirely because the book has nowhere to put the new shares.

Everything is offline and deterministic (CLAUDE.md, B8): fills are constructed directly with the
shared cost model's ``CostBreakdown``, so the book is exercised exactly as ``SimBroker`` would feed
it, with no network and no clock.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backtest.accounting import (
    BenchmarkComparison,
    CorporateActionError,
    InsufficientCashError,
    InsufficientSharesError,
    PortfolioBook,
    PriceUnavailableError,
)
from backtest.xirr import Cashflow, XIRRError, npv, xirr
from dataplatform.ingest.indices import TriPoint, TriSeries
from execution.broker import Fill
from execution.costs import CostBreakdown, CostModel, Exchange, Side, Trade, load_rate_card

_ISIN_A = "INE000A01018"
_ISIN_B = "INE000B01017"
_D = Decimal


def _free_cost(side: Side, turnover: Decimal) -> CostBreakdown:
    """A zero-charge breakdown, so hand-computed values stay exact where costs are not the point."""
    return CostBreakdown(
        schedule_id="test",
        side=side,
        turnover=turnover,
        brokerage=_D("0"),
        securities_transaction_tax=_D("0"),
        exchange_transaction_charge=_D("0"),
        sebi_turnover_fee=_D("0"),
        goods_and_services_tax=_D("0"),
        stamp_duty=_D("0"),
        depository_charge=_D("0"),
    )


def _fill(
    isin: str,
    side: Side,
    quantity: int,
    price: Decimal,
    session: date,
    cost: CostBreakdown | None = None,
) -> Fill:
    """A fill at ``price`` with no slippage — the book reads quantity, side and the cost line."""
    turnover = price * quantity
    return Fill(
        isin=isin,
        session=session,
        side=side,
        quantity=quantity,
        exchange=Exchange.NSE,
        reference_price=price,
        slippage_bps=_D("0"),
        fill_price=price,
        cost=cost if cost is not None else _free_cost(side, turnover),
    )


def _tri(slug: str, name: str, levels: dict[date, str]) -> TriSeries:
    """A published total-return series from ``date → level`` pairs."""
    points = tuple(
        TriPoint(
            index_slug=slug,
            index_name=name,
            as_of=on,
            tri_value=_D(level),
            method="published",
        )
        for on, level in sorted(levels.items())
    )
    return TriSeries(index_slug=slug, index_name=name, method="published", points=points)


# ── acceptance 1: XIRR matches a hand/spreadsheet value for an irregular SIP ────────────────────


def test_xirr_matches_spreadsheet_value_for_irregular_sip() -> None:
    # An irregular SIP: three unevenly-spaced instalments in, one terminal value out. The ACT/365
    # XIRR of this exact stream is 0.20563762 (verified against an independent bisection solve and
    # the formula Excel/LibreOffice XIRR use).
    stream = [
        Cashflow(date(2020, 1, 1), _D("-10000")),
        Cashflow(date(2020, 3, 1), _D("-10000")),
        Cashflow(date(2020, 6, 15), _D("-10000")),
        Cashflow(date(2021, 1, 10), _D("35000")),
    ]
    rate = xirr(stream)
    assert abs(rate - _D("0.20563762")) < _D("0.000001")
    # The defining property: NPV at the solved rate is (near) zero.
    assert abs(npv(float(rate), sorted(stream, key=lambda c: c.when))) < 1e-3


def test_xirr_through_the_book_from_deposits_and_terminal_value() -> None:
    # The same stream, but driven the way the book actually produces it: deposits in, holdings
    # marked to market at the end. A single lump grows 10000 -> 12000 over exactly one year.
    book = PortfolioBook()
    book.deposit(date(2023, 1, 1), _D("10000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 100, _D("100"), date(2023, 1, 1)))
    rate = book.xirr(date(2024, 1, 1), {_ISIN_A: _D("120")})
    # 20% over a 365-day year.
    assert abs(rate - _D("0.20")) < _D("0.0005")


def test_xirr_refuses_a_same_signed_stream() -> None:
    with pytest.raises(XIRRError):
        xirr([Cashflow(date(2020, 1, 1), _D("-1")), Cashflow(date(2021, 1, 1), _D("-1"))])


def test_xirr_refuses_a_single_cashflow() -> None:
    with pytest.raises(XIRRError):
        xirr([Cashflow(date(2020, 1, 1), _D("-1"))])


def test_xirr_handles_a_loss() -> None:
    # Money in, less money out a year later — a negative rate, found via the bracketed fallback.
    rate = xirr([Cashflow(date(2020, 1, 1), _D("-10000")), Cashflow(date(2021, 1, 1), _D("8000"))])
    assert rate < _D("0")
    assert abs(rate - _D("-0.20")) < _D("0.0005")


# ── acceptance 2: a split changes quantity without changing value ───────────────────────────────


def test_split_changes_quantity_but_not_value() -> None:
    book = PortfolioBook(opening_cash=_D("100000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 100, _D("50"), date(2022, 1, 3)))

    before = book.position(_ISIN_A)
    assert before is not None
    assert before.quantity == 100
    assert before.cost_basis == _D("5000")
    value_before = book.market_value({_ISIN_A: _D("50")})
    assert value_before == _D("5000")

    # ₹10 -> ₹2 face-value split: quantity multiplies by 5, price divides by 5.
    book.apply_split(_ISIN_A, from_face_value=_D("10"), to_face_value=_D("2"))

    after = book.position(_ISIN_A)
    assert after is not None
    assert after.quantity == 500  # quantity CHANGED
    assert after.cost_basis == _D("5000")  # basis UNCHANGED
    assert after.average_price == _D("10")  # per-share basis divided by 5
    # Valued at the post-split price, the position is worth exactly what it was.
    value_after = book.market_value({_ISIN_A: _D("10")})
    assert value_after == value_before


def test_bonus_changes_quantity_but_not_value() -> None:
    book = PortfolioBook()
    book.deposit(date(2022, 1, 1), _D("10000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 100, _D("100"), date(2022, 1, 3)))
    value_before = book.market_value({_ISIN_A: _D("100")})

    # 1:1 bonus: holder of 100 ends with 200; price halves.
    book.apply_bonus(_ISIN_A, new_shares=_D("1"), held_shares=_D("1"))

    after = book.position(_ISIN_A)
    assert after is not None
    assert after.quantity == 200
    assert after.cost_basis == _D("10000")
    assert book.market_value({_ISIN_A: _D("50")}) == value_before


def test_split_that_would_leave_a_fraction_is_refused() -> None:
    book = PortfolioBook(opening_cash=_D("100000"))
    # 3 shares cannot survive a 2:1 face-value split (1.5 shares) — the book refuses.
    book.record_fill(_fill(_ISIN_A, Side.BUY, 3, _D("50"), date(2022, 1, 3)))
    with pytest.raises(CorporateActionError):
        book.apply_split(_ISIN_A, from_face_value=_D("2"), to_face_value=_D("4"))


def test_corporate_action_on_absent_position_is_refused() -> None:
    book = PortfolioBook()
    with pytest.raises(InsufficientSharesError):
        book.apply_split(_ISIN_A, from_face_value=_D("10"), to_face_value=_D("2"))


# ── acceptance 3: a demerger creates the resulting position rather than losing value ─────────────


def test_demerger_creates_resulting_position_and_conserves_value() -> None:
    book = PortfolioBook(opening_cash=_D("100000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 100, _D("100"), date(2022, 1, 3)))
    assert book.position(_ISIN_B) is None  # the resulting entity is not held yet
    total_basis_before = book.position(_ISIN_A).cost_basis  # type: ignore[union-attr]
    assert total_basis_before == _D("10000")

    # 1:1 demerger into ISIN_B; 30% of the parent's cost basis follows the demerged business.
    book.apply_demerger(
        _ISIN_A,
        resulting_isin=_ISIN_B,
        shares_received=_D("1"),
        shares_held=_D("1"),
        cost_fraction_to_resulting=_D("0.3"),
    )

    parent = book.position(_ISIN_A)
    resulting = book.position(_ISIN_B)
    assert parent is not None and resulting is not None
    # The resulting position now EXISTS — the value did not vanish.
    assert resulting.quantity == 100
    assert resulting.cost_basis == _D("3000")
    assert parent.quantity == 100
    assert parent.cost_basis == _D("7000")
    # Value is redistributed, never destroyed: the two bases still sum to the original.
    assert parent.cost_basis + resulting.cost_basis == total_basis_before
    # Marked at prices that reflect the split of value, NAV is unchanged.
    nav = book.net_asset_value({_ISIN_A: _D("70"), _ISIN_B: _D("30")})
    assert nav == _D("100000")  # opening cash unchanged; holdings still worth 10000


def test_demerger_into_an_already_held_isin_is_refused() -> None:
    book = PortfolioBook(opening_cash=_D("100000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 100, _D("100"), date(2022, 1, 3)))
    book.record_fill(_fill(_ISIN_B, Side.BUY, 10, _D("100"), date(2022, 1, 3)))
    with pytest.raises(CorporateActionError):
        book.apply_demerger(
            _ISIN_A,
            resulting_isin=_ISIN_B,
            shares_received=_D("1"),
            shares_held=_D("1"),
            cost_fraction_to_resulting=_D("0.3"),
        )


def test_demerger_cost_fraction_out_of_range_is_refused() -> None:
    book = PortfolioBook(opening_cash=_D("100000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 100, _D("100"), date(2022, 1, 3)))
    with pytest.raises(CorporateActionError):
        book.apply_demerger(
            _ISIN_A,
            resulting_isin=_ISIN_B,
            shares_received=_D("1"),
            shares_held=_D("1"),
            cost_fraction_to_resulting=_D("1.5"),
        )


# ── positions, cash, realized/unrealized P&L ────────────────────────────────────────────────────


def test_buy_moves_cash_and_sets_cost_basis() -> None:
    book = PortfolioBook(opening_cash=_D("10000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 50, _D("100"), date(2022, 1, 3)))
    assert book.cash == _D("5000")
    pos = book.position(_ISIN_A)
    assert pos is not None and pos.quantity == 50 and pos.cost_basis == _D("5000")


def test_buy_beyond_cash_is_refused() -> None:
    book = PortfolioBook(opening_cash=_D("1000"))
    with pytest.raises(InsufficientCashError):
        book.record_fill(_fill(_ISIN_A, Side.BUY, 50, _D("100"), date(2022, 1, 3)))


def test_sell_more_than_held_is_refused() -> None:
    book = PortfolioBook(opening_cash=_D("10000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 10, _D("100"), date(2022, 1, 3)))
    with pytest.raises(InsufficientSharesError):
        book.record_fill(_fill(_ISIN_A, Side.SELL, 20, _D("120"), date(2022, 2, 3)))


def test_partial_sell_books_realized_pnl_and_keeps_proportional_basis() -> None:
    book = PortfolioBook(opening_cash=_D("100000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 100, _D("100"), date(2022, 1, 3)))
    book.record_fill(_fill(_ISIN_A, Side.SELL, 40, _D("150"), date(2022, 2, 3)))
    # Sold 40 of 100 at 150; basis removed = 40/100 * 10000 = 4000; proceeds = 6000.
    assert book.realized_pnl == _D("2000")
    remaining = book.position(_ISIN_A)
    assert remaining is not None
    assert remaining.quantity == 60
    assert remaining.cost_basis == _D("6000")


def test_unrealized_pnl_marks_to_market() -> None:
    book = PortfolioBook(opening_cash=_D("100000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 100, _D("100"), date(2022, 1, 3)))
    assert book.unrealized_pnl({_ISIN_A: _D("130")}) == _D("3000")


def test_valuation_refuses_a_missing_price() -> None:
    book = PortfolioBook(opening_cash=_D("100000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 100, _D("100"), date(2022, 1, 3)))
    with pytest.raises(PriceUnavailableError):
        book.market_value({})


def test_realized_pnl_reflects_the_shared_cost_model() -> None:
    # Invariant #4: costs come from the one cost model, carried on the fill. A round-trip through
    # real charges leaves realized P&L strictly below the cost-free 5000 gross gain.
    model = CostModel(load_rate_card())
    buy_cost = model.charge(
        Trade(_ISIN_A, date(2022, 1, 3), Side.BUY, 100, _D("100"), Exchange.NSE)
    )
    sell_cost = model.charge(
        Trade(_ISIN_A, date(2022, 2, 3), Side.SELL, 100, _D("150"), Exchange.NSE)
    )
    book = PortfolioBook(opening_cash=_D("100000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 100, _D("100"), date(2022, 1, 3), buy_cost))
    book.record_fill(_fill(_ISIN_A, Side.SELL, 100, _D("150"), date(2022, 2, 3), sell_cost))
    assert _D("0") < book.realized_pnl < _D("5000")


# ── ledger ───────────────────────────────────────────────────────────────────────────────────


def test_ledger_is_append_only_and_tracks_balance() -> None:
    book = PortfolioBook()
    book.deposit(date(2022, 1, 1), _D("10000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 50, _D("100"), date(2022, 1, 3)))
    entries = book.ledger()
    assert [e.seq for e in entries] == [1, 2]
    assert entries[0].credit == _D("10000") and entries[0].balance == _D("10000")
    assert entries[1].debit == _D("5000") and entries[1].balance == _D("5000")


# ── benchmark comparison ───────────────────────────────────────────────────────────────────────


def test_compare_to_benchmarks_is_apples_to_apples_on_the_same_cashflows() -> None:
    book = PortfolioBook()
    book.deposit(date(2023, 1, 2), _D("10000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 100, _D("100"), date(2023, 1, 2)))

    as_of = date(2024, 1, 2)
    # NIFTY-TRI doubled over the year; the theme proxy went up 50%.
    nifty = _tri("niftytri", "NIFTY 50 TRI", {date(2023, 1, 2): "1000", as_of: "2000"})
    theme = _tri("themetri", "Theme Proxy TRI", {date(2023, 1, 2): "1000", as_of: "1500"})

    result = book.compare_to_benchmarks(as_of, {_ISIN_A: _D("130")}, benchmark=nifty, theme=theme)
    assert isinstance(result, BenchmarkComparison)
    # Portfolio +30%, NIFTY +100%, theme +50% — all money-weighted over the same single lump.
    assert abs(result.portfolio_xirr - _D("0.30")) < _D("0.001")
    assert abs(result.benchmark_xirr - _D("1.00")) < _D("0.001")
    assert abs(result.theme_xirr - _D("0.50")) < _D("0.001")
    # Trailed the broad market and the theme.
    assert result.excess_over_benchmark < _D("0")
    assert result.excess_over_theme < _D("0")


def test_benchmark_level_uses_last_known_value_before_a_cashflow_date() -> None:
    book = PortfolioBook()
    # Deposit lands on a non-index day; the benchmark level must step from the prior point.
    book.deposit(date(2023, 1, 3), _D("10000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 100, _D("100"), date(2023, 1, 3)))
    as_of = date(2024, 1, 2)
    nifty = _tri("niftytri", "NIFTY 50 TRI", {date(2023, 1, 2): "1000", as_of: "1100"})
    theme = _tri("themetri", "Theme Proxy TRI", {date(2023, 1, 2): "1000", as_of: "1100"})
    result = book.compare_to_benchmarks(as_of, {_ISIN_A: _D("110")}, benchmark=nifty, theme=theme)
    # 10% both sides — the Jan-3 deposit bought at the Jan-2 level (1000).
    assert abs(result.benchmark_xirr - _D("0.10")) < _D("0.001")


def test_benchmark_before_series_start_is_refused() -> None:
    book = PortfolioBook()
    book.deposit(date(2022, 1, 1), _D("10000"))
    book.record_fill(_fill(_ISIN_A, Side.BUY, 100, _D("100"), date(2022, 1, 1)))
    as_of = date(2023, 1, 1)
    nifty = _tri("niftytri", "NIFTY 50 TRI", {date(2022, 6, 1): "1000", as_of: "1100"})
    with pytest.raises(PriceUnavailableError):
        book.compare_to_benchmarks(as_of, {_ISIN_A: _D("110")}, benchmark=nifty, theme=nifty)
