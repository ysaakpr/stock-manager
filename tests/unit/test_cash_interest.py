"""X2 — interest on idle cash: RBI repo less 50 bp, daily on settled cash, credited monthly, taxed.

Each test is built to fail if the logic it guards is inverted: interest paid on zero cash, on
proceeds still in settlement, at one rate across a mid-month change, for a day the schedule does
not cover, or left out of the tax bill. Offline: an in-memory market, a frozen clock, no network.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from backtest.accounting import PortfolioBook
from backtest.cash_interest import (
    HAIRCUT,
    REPO_RATES_PATH,
    CashInterestAccrual,
    CashInterestError,
    Provenance,
    RepoRateCoverageError,
    RepoRateSchedule,
    accrue_cash_interest,
    current_cash_interest_identity,
    load_repo_rate_schedule,
)
from backtest.run import _AccountingBroker
from backtest.run_ledger import build_run_ledger, run_spec
from backtest.tax import (
    InterestIncome,
    InvestorProfile,
    MappingGrandfatheringPrices,
    PaymentTiming,
    RunLedger,
    TaxScheduleCoverageError,
    compute_after_tax,
    load_tax_schedule,
)
from backtest.tax_report import read_run_ledger, write_run_ledger
from backtest.xirr import Cashflow
from dataplatform.clock import FrozenClock
from execution.broker import OrderRequest, OrderStatus
from execution.costs import CostModel, Side, load_rate_card
from execution.sim_broker import SimBroker
from tests.unit.test_sim_broker import INFY, InMemoryMarket, _bar

SCHEDULE = load_repo_rate_schedule()
PROFILE = InvestorProfile(
    residency="resident_individual",
    slab_rate=Decimal("0.30"),
    cg_surcharge_rate=Decimal("0"),
    dividend_surcharge_rate=Decimal("0"),
    payment_timing=PaymentTiming.FY_END,
)


def _schedule(tmp_path: Path, rows: list[tuple[str, str]], through: str) -> RepoRateSchedule:
    body = "".join(
        f'  - effective_from: "{d}"\n    repo_rate_pct: "{r}"\n    provenance: reconstructed\n'
        f'    source: "test"\n'
        for d, r in rows
    )
    path = tmp_path / "rates.yaml"
    path.write_text(
        f'version: 1\ncoverage:\n  from: "{rows[0][0]}"\n  through: "{through}"\nchanges:\n{body}',
        encoding="utf-8",
    )
    return load_repo_rate_schedule(path)


# ── the schedule ───────────────────────────────────────────────────────────────────────────────


def test_schedule_covers_2006_to_now_with_every_row_cited() -> None:
    assert SCHEDULE.coverage_from <= date(2006, 1, 1)
    assert SCHEDULE.coverage_through >= date(2026, 9, 29)
    assert all(c.source.strip() for c in SCHEDULE.changes)
    assert {c.provenance for c in SCHEDULE.changes} <= set(Provenance)


def test_schedule_file_has_no_unquoted_number() -> None:
    text = REPO_RATES_PATH.read_text(encoding="utf-8")
    assert not re.search(r"repo_rate_pct:\s*[0-9]", text)


def test_a_bare_float_in_the_schedule_is_refused(tmp_path: Path) -> None:
    text = REPO_RATES_PATH.read_text(encoding="utf-8").replace(
        'repo_rate_pct: "5.25"', "repo_rate_pct: 5.25"
    )
    bad = tmp_path / "rates.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(CashInterestError, match="quoted strings"):
        load_repo_rate_schedule(bad)


def test_known_rate_changes_take_effect_on_their_date() -> None:
    assert SCHEDULE.repo_rate(date(2022, 5, 3)) == Decimal("0.04")
    assert SCHEDULE.repo_rate(date(2022, 5, 4)) == Decimal("0.044")
    assert SCHEDULE.repo_rate(date(2026, 9, 29)) == Decimal("0.0525")


def test_october_2026_mpc_hike_is_in_force_from_its_announcement() -> None:
    # RBI press release prid=63742 (MPC 5-7 Oct 2026): repo raised 25 bp to 5.50%.
    assert SCHEDULE.coverage_through >= date(2026, 10, 9)
    assert SCHEDULE.repo_rate(date(2026, 10, 6)) == Decimal("0.0525")
    assert SCHEDULE.repo_rate(date(2026, 10, 7)) == Decimal("0.055")
    assert SCHEDULE.repo_rate(date(2026, 10, 9)) == Decimal("0.055")
    latest = SCHEDULE.changes[-1]
    assert latest.provenance is Provenance.VERIFIED
    assert "prid=63742" in latest.source


def test_earning_rate_is_repo_less_fifty_basis_points() -> None:
    day = date(2024, 1, 15)  # repo 6.50%
    assert Decimal("0.0050") == HAIRCUT
    assert SCHEDULE.earning_rate(day) == Decimal("0.06")
    assert SCHEDULE.earning_rate(day) < SCHEDULE.repo_rate(day)


@pytest.mark.parametrize("day", [date(2005, 12, 31), date(2099, 1, 1)])
def test_a_date_outside_coverage_raises(day: date) -> None:
    with pytest.raises(RepoRateCoverageError):
        SCHEDULE.earning_rate(day)
    with pytest.raises(RepoRateCoverageError):
        CashInterestAccrual(SCHEDULE).close_session(day, Decimal("1000"))


def test_accrual_walking_past_coverage_raises_rather_than_borrowing_a_rate(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path, [("2024-01-01", "6.50")], through="2024-01-31")
    accrual = CashInterestAccrual(schedule)
    accrual.close_session(date(2024, 1, 30), Decimal("100000"))
    with pytest.raises(RepoRateCoverageError):
        accrual.credit_due(date(2024, 2, 2))


# ── the accrual ────────────────────────────────────────────────────────────────────────────────


def test_zero_cash_earns_zero_interest() -> None:
    accrual = CashInterestAccrual(SCHEDULE)
    accrual.close_session(date(2024, 1, 2), Decimal("0"))
    assert accrual.credit_due(date(2024, 2, 1)) is None
    assert accrual.credits == []


def test_a_month_is_credited_once_on_the_first_session_of_the_next() -> None:
    accrual = CashInterestAccrual(SCHEDULE)
    accrual.close_session(date(2024, 1, 1), Decimal("100000"))
    assert accrual.credit_due(date(2024, 1, 20)) is None  # still inside January: nothing due
    accrual.close_session(date(2024, 1, 20), Decimal("100000"))
    credit = accrual.credit_due(date(2024, 2, 1))
    assert credit is not None
    # 100000 x 6.00% x 31/365 — at 6.50% (no haircut) it would be 552.05, at 7.00% 594.52.
    assert credit.amount == Decimal("509.59")
    assert (credit.period_start, credit.period_end) == (date(2024, 1, 1), date(2024, 1, 31))
    assert credit.credited == date(2024, 2, 1)
    assert credit.description.startswith("INTEREST")
    accrual.close_session(date(2024, 2, 1), Decimal("100000"))
    assert accrual.credit_due(date(2024, 2, 2)) is None  # January is not paid twice


def test_a_mid_month_rate_change_splits_the_accrual(tmp_path: Path) -> None:
    schedule = _schedule(
        tmp_path, [("2024-01-01", "6.50"), ("2024-01-16", "7.50")], through="2024-03-31"
    )
    accrual = CashInterestAccrual(schedule)
    accrual.close_session(date(2024, 1, 1), Decimal("365000"))
    credit = accrual.credit_due(date(2024, 2, 1))
    assert credit is not None
    # 15 days at 6% (900.00) + 16 days at 7% (1120.00). One rate for the month would give 1860.00
    # (the old rate) or 2170.00 (the new).
    assert credit.amount == Decimal("2020.00")


def test_the_balance_that_earns_is_the_one_left_at_each_session_close() -> None:
    accrual = CashInterestAccrual(SCHEDULE)
    accrual.close_session(date(2024, 1, 1), Decimal("365000"))  # Jan 1..9: 9 days
    accrual.credit_due(date(2024, 1, 10))
    accrual.close_session(date(2024, 1, 10), Decimal("0"))  # Jan 10..31 earns nothing
    credit = accrual.credit_due(date(2024, 2, 1))
    assert credit is not None
    assert credit.amount == Decimal("540.00")  # 365000 x 6% x 9/365


# ── settlement: unsettled proceeds earn nothing ────────────────────────────────────────────────


_JAN = [date(2024, 1, d) for d in (2, 3, 10, 11)] + [date(2024, 2, 1)]


def test_unsettled_sale_proceeds_do_not_earn_until_they_settle() -> None:
    """A T+1 (2024) sale on Jan 10 earns from Jan 11, when it settles — not from the fill day.

    The broker releases those proceeds into spendable cash at the end of Jan 10 so that night's
    decision can fund a Jan 11 buy; they are still not paid out, and must not earn for Jan 10.
    """
    bars = {(INFY, s): _bar(INFY, s, open_="500", vwap="500") for s in _JAN}
    market = InMemoryMarket(_JAN, bars)
    clock = FrozenClock(date(2024, 1, 1))
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card()),
        market=market,
        opening_cash=Decimal("1000000"),
    )
    book = PortfolioBook()
    book.deposit(date(2024, 1, 1), Decimal("1000000"))
    with accrue_cash_interest(SCHEDULE):
        broker = _AccountingBroker(sim, book)

    sim.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=1000))
    balances: dict[date, Decimal] = {}
    proceeds = Decimal("0")
    for session in _JAN:
        clock.freeze_at(session)
        filled = broker.execute_session(session)
        balances[session] = sim.interest_bearing_cash
        if session == date(2024, 1, 10):
            (sale,) = filled
            assert sale.status is OrderStatus.COMPLETE and sale.fill is not None
            proceeds = sale.fill.cost.net_amount
            assert sim.cash == balances[session] + proceeds  # spendable tonight, not yet settled
        if session == date(2024, 1, 3):
            sim.place(OrderRequest(isin=INFY, side=Side.SELL, quantity=1000))

    after_buy = balances[date(2024, 1, 2)]
    assert balances[date(2024, 1, 10)] == after_buy
    assert balances[date(2024, 1, 11)] == after_buy + proceeds
    rate = Decimal("0.06") / Decimal("365")
    # Jan 2..10 (9 days) on the post-buy cash; Jan 11..31 (21 days) with the settled proceeds.
    expected = (rate * (after_buy * 9 + (after_buy + proceeds) * 21)).quantize(Decimal("0.01"))
    earning_from_fill_day = (rate * (after_buy * 8 + (after_buy + proceeds) * 22)).quantize(
        Decimal("0.01")
    )
    assert expected != earning_from_fill_day
    (credit,) = broker.interest_credits
    assert credit.credited == date(2024, 2, 1)
    assert credit.amount == expected
    # Credited to both books, as income: cash up, the external (XIRR) stream untouched.
    assert book.interest_income == expected
    assert book.cash == sim.cash
    assert book.external_flows == (Cashflow(date(2024, 1, 1), Decimal("-1000000")),)
    assert any(e.description.startswith("INTEREST") for e in sim.ledger())


def test_off_by_default_the_broker_credits_nothing() -> None:
    sessions = [date(2024, 1, 2), date(2024, 2, 1)]
    market = InMemoryMarket(sessions, {})
    clock = FrozenClock(date(2024, 1, 1))
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card()),
        market=market,
        opening_cash=Decimal("1000000"),
    )
    broker = _AccountingBroker(sim, PortfolioBook(Decimal("1000000")))
    for session in sessions:
        clock.freeze_at(session)
        broker.execute_session(session)
    assert broker.interest_credits == ()
    assert sim.cash == Decimal("1000000")


# ── identity: the run spec records it, and only when on ────────────────────────────────────────


def _spec() -> dict[str, str]:
    return run_spec(
        "t",
        start=date(2024, 1, 1),
        end=date(2024, 2, 1),
        opening_cash=Decimal("1"),
        book_actions=None,
    )


def test_run_spec_records_interest_only_when_on() -> None:
    assert current_cash_interest_identity() is None
    assert "cash_interest" not in _spec()
    with accrue_cash_interest(SCHEDULE):
        on = _spec()
    assert on["cash_interest"] == SCHEDULE.identity()
    assert on["cash_interest"].startswith("repo-50bp:")


# ── tax: income from other sources, at slab, in the FY credited ────────────────────────────────


def _ledger(interest: tuple[InterestIncome, ...]) -> RunLedger:
    return RunLedger(
        source="test",
        trades=(),
        external_flows=(Cashflow(date(2023, 4, 3), Decimal("-1000000")),),
        terminal_date=date(2025, 3, 31),
        terminal_nav=Decimal("1100000"),
        terminal_prices={},
        interest=interest,
    )


def test_interest_is_taxed_at_slab_in_the_fy_it_is_credited() -> None:
    run = _ledger(
        (
            InterestIncome(date(2024, 3, 1), Decimal("1000")),  # FY 2023-24
            InterestIncome(date(2024, 4, 1), Decimal("2000")),  # March's accrual: FY 2024-25
        )
    )
    result = compute_after_tax(
        run, PROFILE, schedule=load_tax_schedule(), fmv=MappingGrandfatheringPrices({})
    )
    fy = {f.fy: f for f in result.fy_taxes}
    assert fy[2023].interest == Decimal("1000.00")
    assert fy[2023].interest_tax == Decimal("300.00")
    assert fy[2024].interest_tax == Decimal("600.00")
    assert fy[2024].cess == Decimal("24.00")  # 4% on the slab tax
    assert result.total_tax == Decimal("936.00")
    assert result.interest_credits == 2 and result.interest_income == Decimal("3000")
    assert result.after_tax_xirr_realised is not None
    assert result.after_tax_xirr_realised < result.pre_tax_xirr


def test_no_interest_means_no_interest_tax() -> None:
    result = compute_after_tax(
        _ledger(()), PROFILE, schedule=load_tax_schedule(), fmv=MappingGrandfatheringPrices({})
    )
    assert result.total_tax == Decimal("0")
    assert result.after_tax_xirr_realised == result.pre_tax_xirr


def test_interest_outside_the_tax_schedule_raises() -> None:
    with pytest.raises(TaxScheduleCoverageError):
        compute_after_tax(
            _ledger((InterestIncome(date(2099, 1, 1), Decimal("1")),)),
            PROFILE,
            schedule=load_tax_schedule(),
            fmv=MappingGrandfatheringPrices({}),
        )


def test_float_interest_is_refused() -> None:
    with pytest.raises(TypeError):
        InterestIncome(date(2024, 1, 1), cast(Decimal, 1.5))


# ── the persisted ledger carries it ────────────────────────────────────────────────────────────


def test_ledger_round_trips_interest_and_stays_byte_identical_without_it(tmp_path: Path) -> None:
    with_interest = _ledger((InterestIncome(date(2024, 4, 1), Decimal("12.34")),))
    path = tmp_path / "with.json"
    write_run_ledger(with_interest, path)
    assert read_run_ledger(path).interest == with_interest.interest

    without = tmp_path / "without.json"
    write_run_ledger(_ledger(()), without)
    assert '"interest"' not in without.read_text(encoding="utf-8")
    assert read_run_ledger(without).interest == ()


def test_build_run_ledger_carries_the_walks_credits() -> None:
    accrual = CashInterestAccrual(SCHEDULE)
    accrual.close_session(date(2024, 1, 1), Decimal("100000"))
    accrual.credit_due(date(2024, 2, 1))
    ledger = build_run_ledger(
        source="t",
        fills=(),
        applied=(),
        external_flows=(Cashflow(date(2024, 1, 1), Decimal("-100000")),),
        terminal_date=date(2024, 2, 1) + timedelta(days=1),
        terminal_nav=Decimal("100509.59"),
        terminal_prices={},
        closing_quantities={},
        interest=accrual.credits,
    )
    assert ledger.interest == (InterestIncome(date(2024, 2, 1), Decimal("509.59")),)
