"""X2 — every backtest persists its fill ledger, and the tax post-processor reads it (run_ledger).

Each test walks the real stack the drivers use — ``ReplayEngine`` → ``_AccountingBroker`` →
``SimBroker`` + ``PortfolioBook`` — over an in-memory market, then builds the run's ledger the way
every runner does (``_AccountingBroker.run_ledger``) and persists it (``persist_run``):

* the ledger lands at ``<dir>/ledgers/<digest>.json``, and a re-run writes byte-identical bytes;
* the tax post-processor applied to the *file* strikes the after-tax XIRR a hand computation from
  the fills gives — so a ledger that drops the dividend or a fill fails, not just a missing file;
* a split, a bonus on an odd count and an ISIN reissue reach the ledger as share-count events that
  rebuild the closing book exactly, and one that did not would be refused;
* the digest is a function of the specification, and a location inside the lake is refused.

Offline: no lake, no network, no wall clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor
from backtest.accounting import PortfolioBook
from backtest.book_actions import (
    BookActionCalendar,
    CashDividend,
    IsinReissue,
    RescaleKind,
    ShareRescale,
)
from backtest.replay import ReplayEngine, SessionContext, SessionDecision
from backtest.run import _AccountingBroker
from backtest.run_ledger import (
    RunOutputLocationError,
    build_run_ledger,
    ledger_path,
    persist_run,
    persist_run_ledgers,
    refuse_lake_location,
    run_digest,
    run_spec,
)
from backtest.tax import (
    BonusEvent,
    InvestorProfile,
    MappingGrandfatheringPrices,
    PaymentTiming,
    ReissueEvent,
    RunLedger,
    SplitEvent,
    TaxTrade,
    compute_after_tax,
    load_tax_schedule,
)
from backtest.tax_report import LedgerFormatError, read_run_ledger
from backtest.xirr import Cashflow, xirr
from dataplatform.clock import FrozenClock
from execution.broker import Exchange, OrderRequest, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SimBroker
from tests.rails_support import mechanics_gate

A = "INE001A01036"
B = "INE002A01018"
P = "INE335Y01012"  # retired at the reissue
S = "INE335Y01020"  # the survivor

D1, D2, D3, D4, D5 = (date(2024, 1, d) for d in (1, 2, 3, 4, 5))
SESSIONS = (D1, D2, D3, D4, D5)
_CASH = Decimal("1000000")
_LIQUID = Decimal("1000000000000")

_PROFILE = InvestorProfile(
    residency="resident_individual",
    slab_rate=Decimal("0.30"),
    cg_surcharge_rate=Decimal("0"),
    dividend_surcharge_rate=Decimal("0"),
    payment_timing=PaymentTiming.FY_END,
)


class _Market:
    """Open = close = the table's price; one extra calendar day so a D5 order still has a bar."""

    def __init__(self, prices: Mapping[tuple[str, date], Decimal]) -> None:
        self.prices = dict(prices)
        self._calendar = (*SESSIONS, date(2024, 1, 8))

    def next_session(self, after: date) -> date:
        for session in self._calendar:
            if session > after:
                return session
        raise NoReferenceBarError(f"no session after {after}")

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        price = self.prices.get((isin, session))
        if price is None:
            raise NoReferenceBarError(f"{isin} {session}")
        return ReferenceBar(
            isin=isin,
            session=session,
            exchange=Exchange.NSE,
            open=price,
            vwap=price,
            traded_value=_LIQUID,
        )


class _Scripted:
    def __init__(self, orders: Mapping[date, tuple[OrderRequest, ...]]) -> None:
        self._orders = orders

    def decide(self, ctx: SessionContext) -> SessionDecision:
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(kind=EvidenceKind.PRICE, source="test", label="x", value=Decimal(1)),
            ),
        )
        return SessionDecision(evidence=evidence, orders=self._orders.get(ctx.session, ()))


def _order(isin: str, side: Side, quantity: int) -> OrderRequest:
    return OrderRequest(isin=isin, side=side, quantity=quantity)


def _walk(
    prices: Mapping[tuple[str, date], Decimal],
    orders: Mapping[date, tuple[OrderRequest, ...]],
    actions: BookActionCalendar,
) -> tuple[_AccountingBroker, PortfolioBook, RunLedger]:
    """One replay through the drivers' accounting broker; returns it, its book and its ledger."""
    market = _Market(prices)
    clock = FrozenClock(D1)
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=market,
        opening_cash=_CASH,
    )
    book = PortfolioBook()
    book.deposit(D1, _CASH)
    broker = _AccountingBroker(sim, book, corporate_actions=actions)
    ReplayEngine(
        policy=_Scripted(orders),
        broker=broker,
        clock=clock,
        sessions=SESSIONS,
        rails=mechanics_gate(prices),
    ).run()
    terminal_prices = {isin: prices[(isin, D5)] for isin in {p.isin for p in book.positions()}}
    ledger = broker.run_ledger(
        source="synthetic",
        terminal=D5,
        terminal_nav=book.net_asset_value(terminal_prices),
        terminal_prices=terminal_prices,
    )
    return broker, book, ledger


# ── the hand-computable case: one short-term round trip, one dividend, one name still held ─────

_DPS = Decimal("5")


def _round_trip_prices() -> dict[tuple[str, date], Decimal]:
    prices: dict[tuple[str, date], Decimal] = {}
    for day in SESSIONS:
        prices[(A, day)] = Decimal("100") if day < D4 else Decimal("130")
        prices[(B, day)] = Decimal("200")
    return prices


def _round_trip() -> tuple[_AccountingBroker, PortfolioBook, RunLedger]:
    # Buy 100 A and 10 B on D1 (fill D2); A pays a ₹5 dividend ex D3; sell A on D4 (fill D5).
    return _walk(
        _round_trip_prices(),
        {
            D1: (_order(A, Side.BUY, 100), _order(B, Side.BUY, 10)),
            D4: (_order(A, Side.SELL, 100),),
        },
        BookActionCalendar([CashDividend(isin=A, ex_date=D3, per_share=_DPS)]),
    )


def _spec(tag: str = "synthetic") -> dict[str, str]:
    return run_spec(
        tag, start=D1, end=D5, opening_cash=_CASH, book_actions=None, parameters="scripted"
    )


def test_a_run_persists_its_ledger_under_its_digest(tmp_path: Path) -> None:
    _broker, _book, ledger = _round_trip()
    with persist_run_ledgers(tmp_path):
        written = persist_run(_spec(), ledger, None)
    assert written == ledger_path(tmp_path, run_digest(_spec()))
    assert written is not None and written.is_file()
    assert read_run_ledger(written) == ledger


def test_persistence_is_off_unless_a_directory_is_in_force(tmp_path: Path) -> None:
    _broker, _book, ledger = _round_trip()
    assert persist_run(_spec(), ledger, None) is None
    assert not any(tmp_path.iterdir())


def test_a_rerun_writes_a_byte_identical_ledger(tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    for out in (first, second):
        _broker, _book, ledger = _round_trip()
        with persist_run_ledgers(out):
            persist_run(_spec(), ledger, None)
    digest = run_digest(_spec())
    assert ledger_path(first, digest).read_bytes() == ledger_path(second, digest).read_bytes()
    assert not list(first.rglob("*.partial"))  # the write is atomic: nothing half-written left


def test_the_ledger_carries_every_fill_and_the_dividend() -> None:
    broker, _book, ledger = _round_trip()
    assert [(t.isin, t.side, t.quantity) for t in ledger.trades] == [
        (A, Side.BUY, 100),
        (B, Side.BUY, 10),
        (A, Side.SELL, 100),
    ]
    assert all(t.stt_known and t.stt > 0 for t in ledger.trades)  # from the fills: STT exact
    assert [(d.isin, d.received, d.amount) for d in ledger.dividends] == [(A, D3, Decimal("500"))]
    assert ledger.external_flows == (Cashflow(D1, -_CASH),)
    assert set(ledger.terminal_prices) == {B}
    assert len(broker.fills) == 3


def _hand_after_tax(ledger: RunLedger) -> tuple[Decimal, Decimal]:
    """The tax and after-tax XIRR, by hand from the fills — the post-processor is not consulted.

    FY 2023-24 (every date is January 2024): STCG at 15 % (Sec 111A before 23-07-2024) on
    (sale consideration + STT) - (purchase cost - STT) — STT is not deductible (Sec 48) — plus the
    dividend at the 30 % slab, cess 4 % on both, no surcharge; paid 31-03-2024 (``FY_END``).
    """
    buy = next(t for t in ledger.trades if t.isin == A and t.side is Side.BUY)
    sell = next(t for t in ledger.trades if t.isin == A and t.side is Side.SELL)
    gain = (sell.net_amount + sell.stt) - (buy.net_amount - buy.stt)
    tax = gain * Decimal("0.15") + Decimal("500") * Decimal("0.30")
    total = (tax * Decimal("1.04")).quantize(Decimal("0.01"))
    stream = [
        Cashflow(D1, -_CASH),
        Cashflow(date(2024, 3, 31), -total),
        Cashflow(D5, ledger.terminal_nav),
    ]
    return total, xirr(stream)


def test_the_tax_post_processor_on_the_persisted_file_matches_the_hand_computation(
    tmp_path: Path,
) -> None:
    _broker, book, ledger = _round_trip()
    with persist_run_ledgers(tmp_path):
        path = persist_run(_spec(), ledger, None)
    assert path is not None
    persisted = read_run_ledger(path)
    result = compute_after_tax(
        persisted, _PROFILE, schedule=load_tax_schedule(), fmv=MappingGrandfatheringPrices({})
    )
    expected_tax, expected_xirr = _hand_after_tax(ledger)
    assert abs(result.total_tax - expected_tax) <= Decimal("0.02")  # paisa rounding per component
    # A paisa of rounding moves the eighth decimal at most; a dropped leg moves the fourth.
    assert result.after_tax_xirr_realised is not None
    assert abs(result.after_tax_xirr_realised - expected_xirr) <= Decimal("0.0000002")
    assert result.after_tax_xirr_realised < result.pre_tax_xirr
    # The pre-tax XIRR off the ledger is the book's own: same deposit, same terminal NAV.
    assert result.pre_tax_xirr == book.xirr(D5, dict(ledger.terminal_prices))


def test_a_ledger_that_dropped_the_dividend_would_not_match() -> None:
    _broker, _book, ledger = _round_trip()
    expected_tax, _ = _hand_after_tax(ledger)
    stripped = RunLedger(
        source=ledger.source,
        trades=ledger.trades,
        external_flows=ledger.external_flows,
        terminal_date=ledger.terminal_date,
        terminal_nav=ledger.terminal_nav,
        terminal_prices=ledger.terminal_prices,
    )
    result = compute_after_tax(stripped, _PROFILE, fmv=MappingGrandfatheringPrices({}))
    assert expected_tax - result.total_tax > Decimal("150")  # the dividend's ₹156 of tax


def test_a_ledger_that_dropped_a_fill_is_refused_against_the_book() -> None:
    broker, book, ledger = _round_trip()
    with pytest.raises(LedgerFormatError, match="does not reproduce the closing book"):
        build_run_ledger(
            source="synthetic",
            fills=broker.fills[:1],  # the B buy is gone
            applied=broker.applied_actions,
            external_flows=book.external_flows,
            terminal_date=D5,
            terminal_nav=ledger.terminal_nav,
            terminal_prices={B: Decimal("200")},
            closing_quantities={p.isin: p.quantity for p in book.positions()},
        )


# ── share-count events: the ledger rebuilds the book, and the tax lots follow ──────────────────


def test_a_split_and_an_odd_bonus_reach_the_ledger_as_the_counts_the_book_holds() -> None:
    prices = {(A, d): Decimal("100") if d < D3 else Decimal("50") for d in SESSIONS}
    prices |= {(B, d): Decimal("300") if d < D3 else Decimal("200") for d in SESSIONS}
    _broker, book, ledger = _walk(
        prices,
        {D1: (_order(A, Side.BUY, 101), _order(B, Side.BUY, 7))},
        BookActionCalendar(
            [
                ShareRescale(A, D3, RescaleKind.SPLIT, Decimal("10"), Decimal("5")),
                # 1:2 bonus — (1 + 2) / 2 — on 7 shares: 10.5, floored to 10.
                ShareRescale(B, D3, RescaleKind.BONUS, Decimal("3"), Decimal("2")),
            ]
        ),
    )
    assert {p.isin: p.quantity for p in book.positions()} == {A: 202, B: 10}
    assert sorted(ledger.corporate_events, key=lambda e: e.isin) == [
        SplitEvent(A, D3, 2, 1, 202),
        BonusEvent(B, D3, 1, 2, 10),
    ]
    result = compute_after_tax(ledger, _PROFILE, fmv=MappingGrandfatheringPrices({}))
    held = {r.isin: r.quantity for r in result.deemed_realisations if r.acquired == D2}
    bonus = [r for r in result.deemed_realisations if r.isin == B and r.acquired == D3]
    assert held == {A: 202, B: 7}
    assert [r.quantity for r in bonus] == [3] and bonus[0].actual_cost == 0  # nil-cost bonus lot


def test_a_reissue_carries_the_lots_to_the_survivor() -> None:
    prices = {(P, d): Decimal("100") for d in (D1, D2)}
    prices |= {(S, d): Decimal("50") for d in (D3, D4, D5)}
    _broker, book, ledger = _walk(
        prices,
        {D1: (_order(P, Side.BUY, 40),)},
        BookActionCalendar(
            [ShareRescale(S, D3, RescaleKind.SPLIT, Decimal("10"), Decimal("5"), carried_from=P)]
        ),
    )
    assert {p.isin: p.quantity for p in book.positions()} == {S: 80}
    assert ledger.corporate_events == (ReissueEvent(S, D3, P), SplitEvent(S, D3, 2, 1, 80))
    result = compute_after_tax(ledger, _PROFILE, fmv=MappingGrandfatheringPrices({}))
    (lot,) = result.deemed_realisations
    # Reissued after 31-01-2018: the FMV (were one needed) is the bar of the ISIN bought, P.
    assert (lot.quantity, lot.acquired, lot.isin) == (80, D2, P)


def test_a_split_ex_the_session_before_the_reissue_keeps_the_lot_date_across_the_hop() -> None:
    """NSE's real shape (HDFC Bank 2019): split ex on P's last session, S trading from the next.

    The lot bought under P on D2 is split on P, carried to S the next session, and still dates
    from D2 — the holding period runs from the original purchase, not the reissue.
    """
    prices = {(P, d): Decimal("100") for d in (D1, D2)}
    prices[(P, D3)] = Decimal("50")  # the ex-date bar is still the old ISIN's
    prices |= {(S, d): Decimal("50") for d in (D4, D5)}
    _broker, book, ledger = _walk(
        prices,
        {D1: (_order(P, Side.BUY, 40),)},
        BookActionCalendar(
            [
                ShareRescale(P, D3, RescaleKind.SPLIT, Decimal("10"), Decimal("5")),
                IsinReissue(S, D4, P, explained=True),
            ]
        ),
    )
    assert {p.isin: p.quantity for p in book.positions()} == {S: 80}
    assert ledger.corporate_events == (SplitEvent(P, D3, 2, 1, 80), ReissueEvent(S, D4, P))
    result = compute_after_tax(ledger, _PROFILE, fmv=MappingGrandfatheringPrices({}))
    (lot,) = result.deemed_realisations
    assert (lot.quantity, lot.acquired) == (80, D2)


def test_a_reissue_before_the_grandfathering_date_takes_its_fmv_from_the_survivor() -> None:
    """A lot bought under P, reissued as S in Dec 2017: on 31-01-2018 only S traded.

    Looking the FMV up on P finds no bar (the failure the first real-lake smoke hit on JSW
    Steel's 2017 split); inverted, the lot would be grandfathered at the wrong ISIN's price.
    """
    bought, reissued, sold = date(2016, 1, 4), date(2017, 12, 15), date(2019, 6, 3)
    buy = TaxTrade(P, bought, Side.BUY, 100, Decimal("10000"), Decimal("10"), True)
    sell = TaxTrade(S, sold, Side.SELL, 1000, Decimal("30000"), Decimal("30"), True)
    ledger = RunLedger(
        source="reissue",
        trades=(buy, sell),
        external_flows=(Cashflow(bought, -_CASH),),
        terminal_date=sold,
        terminal_nav=_CASH,
        terminal_prices={},
        corporate_events=(
            ReissueEvent(S, reissued, P),
            SplitEvent(S, reissued, 10, 1, 1000),
        ),
    )
    # FMV per S share (post-split units) on 31-01-2018: ₹25 -> grandfathered cost ₹25,000.
    result = compute_after_tax(ledger, _PROFILE, fmv=MappingGrandfatheringPrices({S: Decimal(25)}))
    (sale,) = result.realisations
    assert sale.grandfathered and sale.isin == S
    assert sale.cost == Decimal("25000")


# ── identity and location ──────────────────────────────────────────────────────────────────────


def test_the_digest_is_the_specification() -> None:
    assert run_digest(_spec()) == run_digest(_spec())
    assert run_digest(_spec()) != run_digest(_spec("other"))
    moved = run_spec(
        "synthetic", start=D2, end=D5, opening_cash=_CASH, book_actions=None, parameters="scripted"
    )
    assert run_digest(moved) != run_digest(_spec())
    on = run_spec(
        "synthetic",
        start=D1,
        end=D5,
        opening_cash=_CASH,
        book_actions=BookActionCalendar(),
        parameters="scripted",
    )
    assert run_digest(on) != run_digest(_spec())  # corporate actions in force are part of it


def test_a_ledger_directory_inside_the_lake_is_refused(tmp_path: Path) -> None:
    lake = tmp_path / "data"
    with pytest.raises(RunOutputLocationError):
        refuse_lake_location(lake / "L1" / "runs", lake)
    with pytest.raises(RunOutputLocationError):
        refuse_lake_location(lake, lake)
    assert refuse_lake_location(tmp_path / "runs", lake) == (tmp_path / "runs").resolve()


def test_a_missing_fmv_for_an_unsold_lot_withholds_only_the_liquidated_figure() -> None:
    """A pre-2018 lot still held at the end needs its FMV only for the deemed sale.

    The realised after-tax XIRR does not depend on it and stands; the liquidated one is withheld
    with the reason, never struck on a guessed cost.
    """
    bought, end = date(2016, 1, 4), date(2020, 1, 3)
    ledger = RunLedger(
        source="held",
        trades=(TaxTrade(A, bought, Side.BUY, 100, Decimal("10000"), Decimal("10"), True),),
        external_flows=(Cashflow(bought, -_CASH),),
        terminal_date=end,
        terminal_nav=Decimal("1200000"),
        terminal_prices={A: Decimal("300")},
    )
    result = compute_after_tax(ledger, _PROFILE, fmv=MappingGrandfatheringPrices({}))
    assert result.after_tax_xirr_realised == result.pre_tax_xirr  # nothing sold, no dividend
    assert result.after_tax_xirr_liquidated is None
    assert result.liquidation_error is not None and A in result.liquidation_error
    with_fmv = compute_after_tax(
        ledger, _PROFILE, fmv=MappingGrandfatheringPrices({A: Decimal("150")})
    )
    assert with_fmv.after_tax_xirr_liquidated is not None
    assert with_fmv.liquidation_error is None
