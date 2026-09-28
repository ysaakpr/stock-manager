"""X2: after-tax returns — each rule has a test that fails if the rule is inverted.

The acceptance contract: the 12-month threshold (11 months STCG, 13 months LTCG), the pre-2018
LTCG exemption, the 2018 regime with grandfathering, the 2024 STCG rate, the Sec 70/74 set-off
order, FIFO lot matching, and a loud error outside the schedule's coverage. Every figure is a
``Decimal`` and every expected value is hand-computed in the test, so a wrong rate or a reversed
comparison moves a number the test pins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from backtest.tax import (
    SCHEDULE_PATH,
    BonusEvent,
    DividendCredit,
    FyTax,
    InvestorProfile,
    LotMatchError,
    MappingGrandfatheringPrices,
    MissingGrandfatheringPriceError,
    PaymentTiming,
    RunLedger,
    SplitEvent,
    TaxScheduleCoverageError,
    TaxScheduleError,
    TaxTrade,
    Term,
    compute_after_tax,
    financial_year,
    fy_label,
    is_long_term,
    load_tax_schedule,
    match_lots,
)
from backtest.tax_report import (
    L1GrandfatheringPrices,
    LedgerFormatError,
    read_run_ledger,
    render_after_tax_report,
    trades_from_ledger_rows,
    write_run_ledger,
)
from backtest.xirr import Cashflow
from dataplatform.identity import Exchange
from dataplatform.query import AdjustedPoint, AdjustedSeries, AdjustedSeriesRequest, QueryService
from execution.broker import Side
from execution.costs import CostModel, Trade, load_rate_card

A = "INE000A01011"
B = "INE000B01012"
SCHEDULE = load_tax_schedule()
NO_FMV = MappingGrandfatheringPrices({})

PROFILE = InvestorProfile(
    residency="resident_individual",
    slab_rate=Decimal("0.30"),
    cg_surcharge_rate=Decimal("0"),
    dividend_surcharge_rate=Decimal("0"),
    payment_timing=PaymentTiming.FY_END,
)


def buy(isin: str, on: date, qty: int, amount: str, stt: str = "0") -> TaxTrade:
    return TaxTrade(isin, on, Side.BUY, qty, Decimal(amount), Decimal(stt), True)


def sell(isin: str, on: date, qty: int, amount: str, stt: str = "0") -> TaxTrade:
    return TaxTrade(isin, on, Side.SELL, qty, Decimal(amount), Decimal(stt), True)


def ledger(
    trades: list[TaxTrade],
    *,
    dividends: tuple[DividendCredit, ...] = (),
    events: tuple[SplitEvent | BonusEvent, ...] = (),
    terminal: date | None = None,
    nav: str = "1000000",
    prices: dict[str, Decimal] | None = None,
) -> RunLedger:
    first = min([t.trade_date for t in trades] + [d.received for d in dividends])
    last = terminal or max([t.trade_date for t in trades] + [d.received for d in dividends])
    return RunLedger(
        source="test",
        trades=tuple(trades),
        external_flows=(Cashflow(first, Decimal("-1000000")),),
        terminal_date=last,
        terminal_nav=Decimal(nav),
        terminal_prices=prices or {},
        dividends=dividends,
        corporate_events=events,
    )


def taxes(run: RunLedger, fmv: MappingGrandfatheringPrices = NO_FMV) -> dict[int, FyTax]:
    result = compute_after_tax(run, PROFILE, schedule=SCHEDULE, fmv=fmv)
    return {f.fy: f for f in result.fy_taxes}


# ── holding period ─────────────────────────────────────────────────────────────────────────────


def test_eleven_months_is_short_term_thirteen_months_is_long_term() -> None:
    bought = date(2021, 1, 15)
    assert not is_long_term(bought, date(2021, 12, 15), SCHEDULE)  # 11 months
    assert is_long_term(bought, date(2022, 2, 15), SCHEDULE)  # 13 months


def test_exactly_twelve_months_is_still_short_term() -> None:
    # Sec 2(42A): "not more than twelve months" is short-term; long-term starts the day after.
    assert not is_long_term(date(2021, 1, 15), date(2022, 1, 15), SCHEDULE)
    assert is_long_term(date(2021, 1, 15), date(2022, 1, 16), SCHEDULE)


def test_holding_period_classifies_realisations() -> None:
    run = ledger(
        [
            buy(A, date(2021, 1, 15), 10, "1000"),
            buy(B, date(2021, 1, 15), 10, "1000"),
            sell(A, date(2021, 12, 15), 10, "2000"),
            sell(B, date(2022, 2, 15), 10, "2000"),
        ]
    )
    realised, _ = match_lots(run, SCHEDULE, NO_FMV)
    terms = {r.isin: r.term for r in realised}
    assert terms == {A: Term.SHORT, B: Term.LONG}
    fy = taxes(run)
    # FY2021-22: STCG 1000 at 15%; LTCG 1000 under the Rs 1 lakh exemption -> untaxed.
    assert fy[2021].stcg_taxable == {Decimal("0.15"): Decimal("1000.00")}
    assert fy[2021].ltcg_taxable == {Decimal("0.10"): Decimal("0.00")}
    assert fy[2021].cg_tax == Decimal("150.00")


# ── LTCG regimes ───────────────────────────────────────────────────────────────────────────────


def test_2017_ltcg_is_exempt_under_section_10_38() -> None:
    run = ledger(
        [buy(A, date(2016, 1, 11), 1000, "100000"), sell(A, date(2017, 6, 12), 1000, "900000")]
    )
    fy = taxes(run)[2017]
    assert fy.exempt_ltcg_net == Decimal("800000.00")
    assert fy.ltcg_taxable == {}
    assert fy.total == Decimal("0.00")


def test_2019_ltcg_taxed_at_ten_percent_above_one_lakh_with_grandfathering() -> None:
    # Bought 2017 at Rs 100 (10,000 shares), highest price on 31-01-2018 Rs 150, sold 2019 at 200.
    # Sec 55(2)(ac) cost = max(10L, min(15L, 20L)) = 15L -> gain 5L; minus 1L exemption -> 4L @10%.
    fmv = MappingGrandfatheringPrices({A: Decimal("150")})
    run = ledger(
        [buy(A, date(2017, 6, 1), 10000, "1000000"), sell(A, date(2019, 6, 3), 10000, "2000000")]
    )
    realised, _ = match_lots(run, SCHEDULE, fmv)
    assert realised[0].grandfathered and realised[0].cost == Decimal("1500000")
    fy = taxes(run, fmv)[2019]
    assert fy.ltcg_gross == {Decimal("0.10"): Decimal("500000.00")}
    assert fy.exemption_used == Decimal("100000.00")
    assert fy.cg_tax == Decimal("40000.00")
    assert fy.cess == Decimal("1600.00")  # 4% health & education cess from FY2018-19
    assert fy.total == Decimal("41600.00")


def test_grandfathering_never_creates_a_loss_when_fmv_exceeds_the_sale_price() -> None:
    # FMV 250 > sale 200: cost = max(100, min(250, 200)) = 200 -> nil gain, not a loss of 50.
    fmv = MappingGrandfatheringPrices({A: Decimal("250")})
    run = ledger([buy(A, date(2017, 6, 1), 100, "10000"), sell(A, date(2019, 6, 3), 100, "20000")])
    realised, _ = match_lots(run, SCHEDULE, fmv)
    assert realised[0].gain == Decimal("0")


def test_grandfathering_does_not_apply_to_a_lot_bought_after_31_jan_2018() -> None:
    fmv = MappingGrandfatheringPrices({})  # would raise if consulted
    run = ledger([buy(A, date(2018, 2, 1), 100, "10000"), sell(A, date(2019, 6, 3), 100, "20000")])
    realised, _ = match_lots(run, SCHEDULE, fmv)
    assert not realised[0].grandfathered and realised[0].gain == Decimal("10000")


def test_missing_grandfathering_price_raises() -> None:
    run = ledger([buy(A, date(2017, 6, 1), 100, "10000"), sell(A, date(2019, 6, 3), 100, "20000")])
    with pytest.raises(MissingGrandfatheringPriceError):
        match_lots(run, SCHEDULE, NO_FMV)


def test_ltcg_from_23_july_2024_at_twelve_and_half_above_one_and_quarter_lakh() -> None:
    run = ledger(
        [buy(A, date(2023, 6, 1), 100, "100000"), sell(A, date(2024, 8, 1), 100, "425000")]
    )
    fy = taxes(run)[2024]
    # 3.25L gain - 1.25L exemption (the whole FY2024-25) = 2L @ 12.5% = 25,000.
    assert fy.exemption_used == Decimal("125000.00")
    assert fy.cg_tax == Decimal("25000.00")


def test_fy2024_25_exemption_comes_off_the_higher_rate_slice_first() -> None:
    run = ledger(
        [
            buy(A, date(2023, 1, 2), 1, "100000"),
            sell(A, date(2024, 5, 2), 1, "300000"),  # 2L LTCG at 10%
            buy(B, date(2023, 1, 2), 1, "100000"),
            sell(B, date(2024, 8, 2), 1, "300000"),  # 2L LTCG at 12.5%
        ]
    )
    fy = taxes(run)[2024]
    assert fy.ltcg_taxable == {
        Decimal("0.10"): Decimal("200000.00"),
        Decimal("0.125"): Decimal("75000.00"),
    }


# ── STCG rates ─────────────────────────────────────────────────────────────────────────────────


def test_2025_stcg_taxed_at_twenty_percent() -> None:
    run = ledger(
        [buy(A, date(2025, 1, 10), 100, "100000"), sell(A, date(2025, 5, 12), 100, "150000")]
    )
    fy = taxes(run)[2025]
    assert fy.stcg_taxable == {Decimal("0.20"): Decimal("50000.00")}
    assert fy.cg_tax == Decimal("10000.00")


def test_stcg_rate_boundaries() -> None:
    assert SCHEDULE.stcg_rate(date(2008, 3, 31)) == Decimal("0.10")
    assert SCHEDULE.stcg_rate(date(2008, 4, 1)) == Decimal("0.15")
    assert SCHEDULE.stcg_rate(date(2024, 7, 22)) == Decimal("0.15")
    assert SCHEDULE.stcg_rate(date(2024, 7, 23)) == Decimal("0.20")
    assert SCHEDULE.ltcg_regime(date(2018, 3, 31)) == (False, Decimal("0"))
    assert SCHEDULE.ltcg_regime(date(2018, 4, 1)) == (True, Decimal("0.10"))
    assert SCHEDULE.ltcg_regime(date(2024, 7, 23)) == (True, Decimal("0.125"))
    assert SCHEDULE.ltcg_exemption(2023) == Decimal("100000")
    assert SCHEDULE.ltcg_exemption(2024) == Decimal("125000")


# ── loss set-off ───────────────────────────────────────────────────────────────────────────────


def test_short_term_loss_absorbs_stcg_first_then_ltcg() -> None:
    run = ledger(
        [
            buy(A, date(2024, 9, 2), 1, "100000"),
            sell(A, date(2025, 1, 2), 1, "130000"),  # STCG 30k at 20%
            buy(B, date(2023, 6, 1), 1, "100000"),
            sell(B, date(2025, 1, 2), 1, "300000"),  # LTCG 2L at 12.5%
            buy("INE000C01013", date(2024, 9, 2), 1, "100000"),
            sell("INE000C01013", date(2025, 1, 2), 1, "50000"),  # STCL 50k
        ]
    )
    fy = taxes(run)[2024]
    # STCL 50k: 30k against STCG, 20k against LTCG -> LTCG 1.8L - 1.25L = 55k @ 12.5%.
    assert fy.stcg_taxable == {Decimal("0.20"): Decimal("0.00")}
    assert fy.ltcg_taxable == {Decimal("0.125"): Decimal("55000.00")}
    assert fy.cg_tax == Decimal("6875.00")


def test_long_term_loss_never_sets_off_stcg_and_carries_forward_to_ltcg() -> None:
    run = ledger(
        [
            buy(A, date(2020, 1, 2), 1, "100000"),
            sell(A, date(2021, 6, 1), 1, "50000"),  # LTCL 50k in FY2021-22
            buy(B, date(2021, 1, 4), 1, "100000"),
            sell(B, date(2021, 6, 1), 1, "200000"),  # STCG 1L in FY2021-22
            buy("INE000C01013", date(2021, 1, 4), 1, "100000"),
            sell("INE000C01013", date(2022, 6, 1), 1, "300000"),  # LTCG 2L in FY2022-23
        ]
    )
    fy = taxes(run)
    assert fy[2021].stcg_taxable == {Decimal("0.15"): Decimal("100000.00")}
    assert fy[2021].carried_forward_lt == Decimal("50000.00")
    # FY2022-23: 2L LTCG - 50k brought forward = 1.5L; - 1L exemption = 50k @ 10%.
    assert fy[2022].brought_forward_used == Decimal("50000.00")
    assert fy[2022].ltcg_taxable == {Decimal("0.10"): Decimal("50000.00")}
    assert fy[2022].carried_forward_lt == Decimal("0.00")


def test_current_year_losses_are_set_off_before_brought_forward_losses() -> None:
    run = ledger(
        [
            buy(A, date(2021, 1, 4), 1, "100000"),
            sell(A, date(2021, 6, 1), 1, "60000"),  # STCL 40k, FY2021-22 -> carried
            buy(B, date(2022, 5, 2), 1, "100000"),
            sell(B, date(2022, 9, 1), 1, "150000"),  # STCG 50k, FY2022-23
            buy("INE000C01013", date(2022, 5, 2), 1, "100000"),
            sell("INE000C01013", date(2022, 9, 1), 1, "70000"),  # STCL 30k, FY2022-23
        ]
    )
    fy = taxes(run)[2022]
    # Current STCL 30k first -> 20k left; then 20k of the 40k brought forward; 20k carries on.
    assert fy.brought_forward_used == Decimal("20000.00")
    assert fy.stcg_taxable == {Decimal("0.15"): Decimal("0.00")}
    assert fy.carried_forward_st == Decimal("20000.00")


def test_carried_forward_loss_expires_after_eight_years() -> None:
    run = ledger(
        [
            buy(A, date(2009, 5, 4), 1, "100000"),
            sell(A, date(2009, 9, 1), 1, "60000"),  # STCL 40k, FY2009-10
            buy(B, date(2018, 5, 2), 1, "100000"),
            sell(B, date(2018, 9, 3), 1, "150000"),  # STCG 50k, FY2018-19: 9 years later
        ]
    )
    fy = taxes(run)[2018]
    assert fy.expired == Decimal("40000.00")
    assert fy.stcg_taxable == {Decimal("0.15"): Decimal("50000.00")}


def test_loss_from_the_exempt_era_is_disregarded() -> None:
    run = ledger(
        [
            buy(A, date(2016, 1, 4), 1, "100000"),
            sell(A, date(2017, 6, 1), 1, "50000"),  # LTCL under 10(38): disregarded
            buy(B, date(2017, 5, 2), 1, "100000"),
            sell(B, date(2019, 6, 3), 1, "300000"),  # LTCG 2L under 112A
        ]
    )
    fmv = MappingGrandfatheringPrices({B: Decimal("100000")})  # FMV == cost: no step-up
    fy = taxes(run, fmv)
    assert fy[2017].carried_forward_lt == Decimal("0.00")
    assert fy[2019].brought_forward_used == Decimal("0.00")


# ── FIFO ───────────────────────────────────────────────────────────────────────────────────────


def test_fifo_matches_the_oldest_lot_first() -> None:
    run = ledger(
        [
            buy(A, date(2021, 1, 4), 100, "1000"),
            buy(A, date(2021, 3, 1), 100, "3000"),
            sell(A, date(2021, 6, 1), 150, "6000"),
        ]
    )
    realised, open_lots = match_lots(run, SCHEDULE, NO_FMV)
    assert [(r.acquired, r.quantity, r.cost, r.proceeds) for r in realised] == [
        (date(2021, 1, 4), 100, Decimal("1000"), Decimal("4000")),
        (date(2021, 3, 1), 50, Decimal("1500"), Decimal("2000")),
    ]
    (lot,) = open_lots[A]
    assert (lot.acquired, lot.quantity, lot.cost) == (date(2021, 3, 1), 50, Decimal("1500"))


def test_fifo_classifies_each_slice_by_its_own_lot() -> None:
    run = ledger(
        [
            buy(A, date(2020, 1, 2), 10, "1000"),
            buy(A, date(2021, 3, 1), 10, "1000"),
            sell(A, date(2021, 6, 1), 20, "4000"),
        ]
    )
    realised, _ = match_lots(run, SCHEDULE, NO_FMV)
    assert [r.term for r in realised] == [Term.LONG, Term.SHORT]


def test_selling_more_than_the_lots_hold_raises() -> None:
    run = ledger([buy(A, date(2021, 1, 4), 10, "1000"), sell(A, date(2021, 6, 1), 11, "2000")])
    with pytest.raises(LotMatchError):
        match_lots(run, SCHEDULE, NO_FMV)


def test_stt_is_not_deductible() -> None:
    # Buy net 1010 incl. STT 1 -> cost 1009; sell net 1990 after STT 2 -> consideration 1992.
    run = ledger(
        [
            buy(A, date(2021, 1, 4), 1, "1010", stt="1"),
            sell(A, date(2021, 6, 1), 1, "1990", stt="2"),
        ]
    )
    realised, _ = match_lots(run, SCHEDULE, NO_FMV)
    assert realised[0].gain == Decimal("983")


def _dust_round_trip() -> tuple[TaxTrade, TaxTrade]:
    """The campaign's real dust exit: 1 x INE270A01011 bought @ 7.16, sold a week later @ 7.04.

    Priced by the one cost model, the sell's flat DP charge (13.50 + service tax/SBC = 15.46)
    exceeds its turnover, so the account pays 8.42 to deliver the share.
    """
    model = CostModel(load_rate_card(), account_state="MH")
    isin = "INE270A01011"
    trades = []
    for on, side, price in (
        (date(2015, 11, 30), Side.BUY, "7.16"),
        (date(2015, 12, 7), Side.SELL, "7.04"),
    ):
        cost = model.charge(
            Trade(isin=isin, trade_date=on, side=side, quantity=1, price=Decimal(price))
        )
        trades.append(
            TaxTrade(isin, on, side, 1, cost.net_amount, cost.securities_transaction_tax, True)
        )
    return trades[0], trades[1]  # fmt: skip


def test_a_sell_whose_charges_exceed_its_turnover_is_a_capital_loss(tmp_path: Path) -> None:
    bought, sold = _dust_round_trip()
    assert sold.net_amount == Decimal("-8.42")
    run = ledger([bought, sold])
    path = tmp_path / "ledger.json"
    write_run_ledger(run, path)
    assert read_run_ledger(path) == run
    realised, _ = match_lots(read_run_ledger(path), SCHEDULE, NO_FMV)
    # The loss is the whole cost plus what the exit cost on top — never clamped at the cost.
    assert [r.gain for r in realised] == [-(bought.tax_amount + Decimal("8.42"))]
    assert realised[0].gain == Decimal("-15.58")


def test_a_buy_with_a_non_positive_net_amount_is_refused() -> None:
    for amount in ("0", "-8.42"):
        with pytest.raises(ValueError, match="buy net amount must be positive"):
            buy(A, date(2021, 1, 4), 1, amount)


def test_split_after_grandfathering_date_scales_lot_not_fmv() -> None:
    # 100 shares bought 2017, FMV 150/share on 31-01-2018, 1:5 split in 2019 -> 500 shares.
    fmv = MappingGrandfatheringPrices({A: Decimal("150")})
    run = ledger(
        [buy(A, date(2017, 6, 1), 100, "10000"), sell(A, date(2020, 6, 1), 500, "20000")],
        events=(SplitEvent(A, date(2019, 1, 7), 5, 1),),
    )
    realised, _ = match_lots(run, SCHEDULE, fmv)
    # FMV total = 150 x 500 / 5 = 15,000 (not 75,000): gain 5,000.
    assert realised[0].cost == Decimal("15000") and realised[0].gain == Decimal("5000")


def test_bonus_creates_a_nil_cost_lot_dated_on_allotment() -> None:
    run = ledger(
        [buy(A, date(2021, 1, 4), 100, "10000"), sell(A, date(2022, 3, 1), 200, "30000")],
        events=(BonusEvent(A, date(2021, 6, 1), 1, 1),),
    )
    realised, _ = match_lots(run, SCHEDULE, NO_FMV)
    assert [(r.term, r.cost, r.gain) for r in realised] == [
        (Term.LONG, Decimal("10000"), Decimal("5000")),
        (Term.SHORT, Decimal("0"), Decimal("15000")),
    ]


# ── coverage and Decimal discipline ────────────────────────────────────────────────────────────


def test_transfer_before_schedule_coverage_raises() -> None:
    run = ledger([buy(A, date(2004, 1, 5), 1, "1000"), sell(A, date(2004, 9, 30), 1, "2000")])
    with pytest.raises(TaxScheduleCoverageError, match="outside the tax schedule's coverage"):
        match_lots(run, SCHEDULE, NO_FMV)


def test_transfer_after_schedule_coverage_raises() -> None:
    run = ledger([buy(A, date(2026, 5, 4), 1, "1000"), sell(A, date(2027, 4, 1), 1, "2000")])
    with pytest.raises(TaxScheduleCoverageError):
        match_lots(run, SCHEDULE, NO_FMV)


def test_dividend_outside_coverage_raises() -> None:
    run = ledger(
        [buy(A, date(2026, 5, 4), 1, "1000")],
        dividends=(DividendCredit(A, date(2027, 6, 1), Decimal("10")),),
        prices={A: Decimal("1")},
    )
    with pytest.raises(TaxScheduleCoverageError):
        compute_after_tax(run, PROFILE, schedule=SCHEDULE, fmv=NO_FMV)


def test_float_money_is_refused() -> None:
    with pytest.raises(TypeError):
        TaxTrade(A, date(2021, 1, 4), Side.BUY, 1, cast(Decimal, 100.0), Decimal("0"), True)


def test_schedule_with_a_bare_float_is_refused(tmp_path: Path) -> None:
    text = SCHEDULE_PATH.read_text(encoding="utf-8").replace('rate: "0.15"', "rate: 0.15")
    bad = tmp_path / "schedule.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(TaxScheduleError, match="quoted string"):
        load_tax_schedule(bad)


def test_every_schedule_boundary_is_cited() -> None:
    rows = SCHEDULE.provenance_rows()
    assert rows and all(citation.strip() for *_, citation in rows)


def test_investor_profile_is_resident_individual_only() -> None:
    with pytest.raises(ValueError, match="resident individual"):
        InvestorProfile(
            residency="non_resident",
            slab_rate=Decimal("0.3"),
            cg_surcharge_rate=Decimal("0"),
            dividend_surcharge_rate=Decimal("0"),
            payment_timing=PaymentTiming.FY_END,
        )


# ── dividends ──────────────────────────────────────────────────────────────────────────────────


def test_dividend_regimes() -> None:
    run = ledger(
        [buy(A, date(2015, 1, 5), 1, "1000")],
        dividends=(
            DividendCredit(A, date(2015, 8, 3), Decimal("2000000")),  # DDT era: exempt
            DividendCredit(A, date(2019, 8, 1), Decimal("1200000")),  # 115BBDA: 10% over 10L
            DividendCredit(A, date(2021, 8, 2), Decimal("100000")),  # slab 30%
        ),
        prices={A: Decimal("1")},
    )
    fy = taxes(
        run, MappingGrandfatheringPrices({A: Decimal("1000")})
    )  # deemed sale of the 2015 lot
    assert fy[2015].dividend_tax == Decimal("0.00")
    assert fy[2019].dividend_taxable == Decimal("200000.00")
    assert fy[2019].dividend_tax == Decimal("20000.00")
    assert fy[2021].dividend_tax == Decimal("30000.00")
    assert fy[2021].cess == Decimal("1200.00")


# ── XIRR ───────────────────────────────────────────────────────────────────────────────────────


def test_after_tax_xirr_is_below_pre_tax_by_the_tax_flows() -> None:
    run = ledger(
        [buy(A, date(2022, 4, 4), 100, "1000000"), sell(A, date(2022, 10, 3), 100, "1200000")],
        terminal=date(2023, 4, 3),
        nav="1200000",
    )
    result = compute_after_tax(run, PROFILE, schedule=SCHEDULE, fmv=NO_FMV)
    # 2L STCG @ 15% = 30,000 + 4% cess = 31,200, paid 31-03-2023.
    assert result.total_tax == Decimal("31200.00")
    assert result.fy_taxes[0].payment_date == date(2023, 3, 31)
    assert result.after_tax_xirr_realised < result.pre_tax_xirr
    assert result.after_tax_xirr_liquidated == result.after_tax_xirr_realised  # nothing open


def test_no_tax_means_after_tax_equals_pre_tax() -> None:
    run = ledger(
        [buy(A, date(2016, 1, 4), 100, "1000000"), sell(A, date(2017, 6, 1), 100, "1200000")],
        nav="1200000",
    )
    result = compute_after_tax(run, PROFILE, schedule=SCHEDULE, fmv=NO_FMV)
    assert result.total_tax == Decimal("0")
    assert result.after_tax_xirr_realised == result.pre_tax_xirr


def test_deemed_liquidation_taxes_open_lots() -> None:
    run = ledger(
        [buy(A, date(2025, 1, 6), 100, "1000000")],
        terminal=date(2025, 6, 2),
        nav="1500000",
        prices={A: Decimal("15000")},
    )
    result = compute_after_tax(run, PROFILE, schedule=SCHEDULE, fmv=NO_FMV)
    assert result.total_tax == Decimal("0")
    assert result.total_tax_liquidated == Decimal("104000.00")  # 5L @ 20% + 4% cess
    assert result.after_tax_xirr_liquidated is not None
    assert result.after_tax_xirr_liquidated < result.after_tax_xirr_realised


# ── report, adapters, persistence ──────────────────────────────────────────────────────────────


def test_report_states_investor_assumptions() -> None:
    run = ledger(
        [buy(A, date(2025, 1, 10), 100, "100000"), sell(A, date(2025, 5, 12), 100, "150000")]
    )
    result = compute_after_tax(run, PROFILE, schedule=SCHEDULE, fmv=NO_FMV)
    text = render_after_tax_report(result, SCHEDULE)
    for needle in (
        "resident_individual",
        "Slab rate",
        "Surcharge",
        "Cess",
        "fy_end",
        "Rate provenance",
        "Sec 111A",
    ):
        assert needle in text
    assert "Dividends credited in this run: **0**" in text


def test_ledger_rows_parse_into_trades_and_dividends() -> None:
    rows = [
        {"seq": "1", "session": "2021-01-04", "isin": A, "description": "BUY 10 @ 100.5",
         "debit": "1006.20", "credit": "0", "balance": "0"},
        {"seq": "2", "session": "2021-06-01", "isin": A, "description": "SELL 10 @ 120",
         "debit": "0", "credit": "1195.10", "balance": "0"},
        {"seq": "3", "session": "2021-07-01", "isin": A, "description": "DIVIDEND 10 x 2.5",
         "debit": "0", "credit": "25", "balance": "0"},
    ]  # fmt: skip
    trades, dividends = trades_from_ledger_rows(rows)
    assert [(t.side, t.quantity, t.net_amount, t.stt_known) for t in trades] == [
        (Side.BUY, 10, Decimal("1006.20"), False),
        (Side.SELL, 10, Decimal("1195.10"), False),
    ]
    assert dividends == (DividendCredit(A, date(2021, 7, 1), Decimal("25")),)


def test_unrecognised_ledger_row_raises() -> None:
    row = {"seq": "9", "session": "2021-01-04", "isin": A, "description": "FEE",
           "debit": "1", "credit": "0", "balance": "0"}  # fmt: skip
    with pytest.raises(LedgerFormatError):
        trades_from_ledger_rows([row])


def test_run_ledger_round_trips_through_json(tmp_path: Path) -> None:
    run = ledger(
        [buy(A, date(2021, 1, 4), 100, "1000.10", stt="1"), sell(A, date(2021, 6, 1), 100, "2000")],
        dividends=(DividendCredit(A, date(2021, 7, 1), Decimal("25")),),
        events=(SplitEvent(A, date(2021, 2, 1), 2, 1), BonusEvent(B, date(2021, 3, 1), 1, 2)),
        prices={A: Decimal("21.5")},
    )
    path = tmp_path / "ledger.json"
    write_run_ledger(run, path)
    assert read_run_ledger(path) == run


# ── grandfathering FMV from L1 ─────────────────────────────────────────────────────────────────


@dataclass
class _FakeQuery:
    points: dict[Exchange, list[AdjustedPoint]]

    def adjusted_series(self, request: AdjustedSeriesRequest) -> AdjustedSeries:
        assert request.primary is not None
        pts = tuple(self.points.get(request.primary, []))
        return AdjustedSeries(
            isin=request.isin,
            primary=request.primary,
            points=pts,
            first=pts[0].trade_date if pts else None,
            last=pts[-1].trade_date if pts else None,
        )


def _point(
    exchange: Exchange, day: date, adj_high: str, factor: str, fell_back: bool = False
) -> AdjustedPoint:
    p = Decimal(adj_high)
    return AdjustedPoint(
        isin=A, trade_date=day, exchange=exchange, primary=exchange, fell_back=fell_back,
        adj_open=p, adj_high=p, adj_low=p, adj_close=p, adj_volume=Decimal("1"), tr_close=p,
        cum_price_factor=Decimal(factor), cum_qty_factor=Decimal("1") / Decimal(factor),
    )  # fmt: skip


def test_l1_fmv_is_the_raw_highest_price_across_exchanges_on_the_last_traded_day() -> None:
    fake = _FakeQuery(
        {
            # Adjusted for a later 1:5 split (factor 0.2): raw high = adj / 0.2.
            Exchange.NSE: [
                _point(Exchange.NSE, date(2018, 1, 30), "40", "0.2"),
                _point(Exchange.NSE, date(2018, 1, 31), "30", "0.2"),
            ],
            Exchange.BSE: [
                _point(Exchange.BSE, date(2018, 1, 31), "31", "0.2"),
                _point(Exchange.BSE, date(2018, 1, 31), "99", "0.2", fell_back=True),
            ],
        }
    )
    source = L1GrandfatheringPrices(cast(QueryService, fake), fmv_date=date(2018, 1, 31))
    assert source.fmv_per_share(A) == Decimal("155")  # BSE's 31/0.2, not NSE's 30/0.2 or 30-Jan


def test_l1_fmv_missing_raises() -> None:
    source = L1GrandfatheringPrices(cast(QueryService, _FakeQuery({})), fmv_date=date(2018, 1, 31))
    with pytest.raises(MissingGrandfatheringPriceError):
        source.fmv_per_share(A)


def test_fy_helpers() -> None:
    assert financial_year(date(2019, 3, 31)) == 2018
    assert financial_year(date(2019, 4, 1)) == 2019
    assert fy_label(2019) == "FY2019-20"
