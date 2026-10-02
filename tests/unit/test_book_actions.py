"""X2 — corporate actions applied to the backtest book on their ex-dates (``book_actions``).

Each test walks the real stack the drivers use — ``ReplayEngine`` → ``_AccountingBroker`` →
``SimBroker`` + ``PortfolioBook`` — over an in-memory market, and each fails if the adjustment is
skipped *or* inverted:

* a 2:1 split doubles the count, halves the per-share basis, and leaves NAV continuous across the
  ex-date (a skipped split shows a 50 % drawdown, an inverted one a 75 % drawdown);
* a bonus does the same with its additive arithmetic, and an odd count floors identically in both
  books;
* a cash dividend lands as exactly ``qty x DPS``, once, as a ``Decimal``, on the ex-date, and only
  on shares bought before it;
* a split across an ISIN reissue carries the retired holding to the survivor;
* the store rows translate onto the ISIN that was live on the ex-date;
* a policy's every data read is identical with and without the wiring, and the module is
  physically outside the decision path (the PIT caution in the module docstring);
* two runs with the wiring are byte-identical (§8.3.3).
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor
from backtest.accounting import BookError, PortfolioBook
from backtest.book_actions import (
    BookActionApplier,
    BookActionCalendar,
    CashDividend,
    IsinReissue,
    RescaleKind,
    ShareRescale,
    UnmodelledAction,
    _to_book_actions,
    book_corporate_actions,
)
from backtest.policies.naive_momentum import MomentumParameters, MomentumRecord, NaiveMomentumPolicy
from backtest.policies.swing_composite import (
    RegimeReading,
    SwingCompositeParameters,
    SwingCompositePolicy,
    SwingRecord,
)
from backtest.replay import ReplayEngine, ReplayResult, SessionContext, SessionDecision
from backtest.run import _AccountingBroker
from dataplatform.clock import FrozenClock
from dataplatform.corpactions import (
    ActionType,
    DividendKind,
    DividendTerms,
    ExchangeRatioTerms,
    FaceValueTerms,
    RatioTerms,
    UnquantifiedTerms,
)
from dataplatform.query.pit import Dataset
from execution.broker import Exchange, OrderRequest, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SimBroker
from tests.rails_support import mechanics_gate

A = "INE001A01036"
P = "INE335Y01012"  # retired at the reissue
S = "INE335Y01020"  # the survivor

D1, D2, D3, D4, D5 = (date(2024, 1, d) for d in (1, 2, 3, 4, 5))
SESSIONS = (D1, D2, D3, D4, D5)
_ONE = Decimal("1")
_CASH = Decimal("1000000")
_LIQUID = Decimal("1000000000000")  # huge turnover: slippage stays at the 2 bps base


class _Market:
    """A ``SessionMarket`` over a fixed price table: ``prices[(isin, session)]`` is open = close."""

    def __init__(
        self, prices: Mapping[tuple[str, date], Decimal], sessions: tuple[date, ...]
    ) -> None:
        self.prices = dict(prices)
        self._calendar = (*sessions, date(2024, 1, 8))

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


def _evidence(session: date) -> EvidenceBundle:
    return EvidenceBundle(
        trading_date=session,
        actor=Actor.T0,
        items=(EvidenceItem(kind=EvidenceKind.PRICE, source="test", label="x", value=Decimal(1)),),
    )


class _Scripted:
    """Places the scripted orders on each session; decides nothing else."""

    def __init__(self, orders: Mapping[date, tuple[OrderRequest, ...]]) -> None:
        self._orders = orders

    def decide(self, ctx: SessionContext) -> SessionDecision:
        return SessionDecision(
            evidence=_evidence(ctx.session), orders=self._orders.get(ctx.session, ())
        )


def _buy(isin: str, quantity: int) -> OrderRequest:
    return OrderRequest(isin=isin, side=Side.BUY, quantity=quantity)


def _sell(isin: str, quantity: int) -> OrderRequest:
    return OrderRequest(isin=isin, side=Side.SELL, quantity=quantity)


class _Walk:
    """One replay through the drivers' accounting broker, with a NAV read after every session."""

    def __init__(
        self,
        prices: Mapping[tuple[str, date], Decimal],
        policy: object,
        actions: BookActionCalendar | None,
        sessions: tuple[date, ...] = SESSIONS,
    ) -> None:
        self.market = _Market(prices, sessions)
        clock = FrozenClock(D1)
        self.sim = SimBroker(
            clock=clock,
            cost_model=CostModel(load_rate_card(), account_state="MH"),
            market=self.market,
            opening_cash=_CASH,
        )
        self.book = PortfolioBook()
        self.book.deposit(D1, _CASH)
        self.nav: dict[date, Decimal] = {}
        self.cash: dict[date, Decimal] = {}
        self.settled: dict[date, dict[str, int]] = {}
        self.pending: dict[date, tuple[tuple[str, date, int], ...]] = {}
        self.broker = _AccountingBroker(
            self.sim, self.book, nav_sink=self._sample, corporate_actions=actions
        )
        self.engine = ReplayEngine(
            policy=policy,  # type: ignore[arg-type]
            broker=self.broker,
            clock=clock,
            sessions=sessions,
            rails=mechanics_gate(prices),
        )

    def _sample(self, session: date) -> None:
        closes = {
            isin: price for (isin, day), price in self.market.prices.items() if day == session
        }
        self.nav[session] = self.book.net_asset_value(closes)
        self.cash[session] = self.book.cash
        # The broker's two share states at the session's end: delivered, and still in settlement.
        self.settled[session] = {h.isin: h.quantity for h in self.sim.holdings()}
        self.pending[session] = tuple((p.isin, p.session, p.quantity) for p in self.sim.positions())

    def run(self) -> ReplayResult:
        return self.engine.run()


def _flat_then(
    isin: str, before: Decimal, after: Decimal, ex: date = D3
) -> dict[tuple[str, date], Decimal]:
    return {(isin, day): (before if day < ex else after) for day in SESSIONS}


# ── split ──────────────────────────────────────────────────────────────────────────────────────


def _split_walk(actions: BookActionCalendar | None) -> _Walk:
    walk = _Walk(
        _flat_then(A, Decimal("100"), Decimal("50")),
        _Scripted({D1: (_buy(A, 100),)}),
        actions,
    )
    walk.run()
    return walk


_SPLIT_2_FOR_1 = ShareRescale(
    isin=A,
    ex_date=D3,
    kind=RescaleKind.SPLIT,
    numerator=Decimal("10"),
    denominator=Decimal("5"),
)


def test_a_split_doubles_the_count_halves_the_basis_and_keeps_nav_continuous() -> None:
    walk = _split_walk(BookActionCalendar([_SPLIT_2_FOR_1]))

    position = walk.book.position(A)
    assert position is not None
    assert position.quantity == 200
    assert walk.sim.held_quantity(A) == 200
    basis_before = _split_walk(BookActionCalendar()).book.position(A)
    assert basis_before is not None
    assert position.cost_basis == basis_before.cost_basis  # total basis untouched
    assert position.average_price == basis_before.average_price / 2  # per-share basis halves
    # No fake drawdown: NAV on the ex-date equals NAV the session before, to the paisa.
    assert walk.nav[D3] == walk.nav[D2]
    assert walk.nav[D5] == walk.nav[D2]


def test_a_skipped_split_is_the_fake_fifty_percent_drawdown() -> None:
    walk = _split_walk(BookActionCalendar())
    position = walk.book.position(A)
    assert position is not None and position.quantity == 100
    assert walk.nav[D3] < walk.nav[D2] - Decimal("4900")


def test_an_inverted_split_is_caught() -> None:
    inverted = ShareRescale(
        isin=A,
        ex_date=D3,
        kind=RescaleKind.SPLIT,
        numerator=Decimal("5"),
        denominator=Decimal("10"),
    )
    walk = _split_walk(BookActionCalendar([inverted]))
    position = walk.book.position(A)
    assert position is not None and position.quantity == 50
    assert walk.nav[D3] != walk.nav[D2]


def test_a_sell_staged_before_the_split_exits_the_whole_post_split_position() -> None:
    # A second, untouched holding (S, flat) keeps the book at two names, so the full exit of A is
    # not a sell-out of the whole book — which A8's min-holdings rail (floor >= 1) always refuses.
    prices = {**_flat_then(A, Decimal("100"), Decimal("50")), **_flat_then(S, _ONE, _ONE)}
    walk = _Walk(
        prices,
        _Scripted({D1: (_buy(A, 100), _buy(S, 10)), D2: (_sell(A, 100),)}),
        BookActionCalendar([_SPLIT_2_FOR_1]),
    )
    walk.run()
    assert walk.book.position(A) is None
    assert walk.sim.held_quantity(A) == 0
    sells = [e for e in walk.book.ledger() if e.description == "sell"]
    assert len(sells) == 1 and sells[0].session == D3


# ── bonus ──────────────────────────────────────────────────────────────────────────────────────


def _bonus(new: str, held: str) -> ShareRescale:
    return ShareRescale(
        isin=A,
        ex_date=D3,
        kind=RescaleKind.BONUS,
        numerator=Decimal(new) + Decimal(held),
        denominator=Decimal(held),
    )


def test_a_one_for_one_bonus_doubles_the_count_and_keeps_nav_continuous() -> None:
    walk = _split_walk(BookActionCalendar([_bonus("1", "1")]))
    position = walk.book.position(A)
    assert position is not None and position.quantity == 200
    assert walk.nav[D3] == walk.nav[D2]


def test_an_inverted_bonus_is_caught() -> None:
    # Read as held:new on a 1:1 it is identical, so invert a 1:2 bonus: 3/2 vs 3/1.
    walk = _Walk(
        _flat_then(A, Decimal("150"), Decimal("100")),
        _Scripted({D1: (_buy(A, 100),)}),
        BookActionCalendar([_bonus("1", "2")]),
    )
    walk.run()
    position = walk.book.position(A)
    assert position is not None and position.quantity == 150
    assert walk.nav[D3] == walk.nav[D2]


def test_a_fractional_bonus_entitlement_floors_identically_in_both_books() -> None:
    # 3:2 on 101 shares is 252.5 — the half share is forfeited, not invented.
    walk = _Walk(
        _flat_then(A, Decimal("100"), Decimal("40")),
        _Scripted({D1: (_buy(A, 101),)}),
        BookActionCalendar([_bonus("3", "2")]),
    )
    walk.run()
    position = walk.book.position(A)
    assert position is not None and position.quantity == 252
    assert walk.sim.held_quantity(A) == 252
    assert walk.nav[D3] == walk.nav[D2] - Decimal("20")  # half a share at the ex price, forfeited


# ── dividend ───────────────────────────────────────────────────────────────────────────────────

_DPS = Decimal("7.35")


def _dividend_walk(
    actions: BookActionCalendar | None, orders: Mapping[date, tuple[OrderRequest, ...]]
) -> _Walk:
    walk = _Walk(_flat_then(A, Decimal("100"), Decimal("100") - _DPS), _Scripted(orders), actions)
    walk.run()
    return walk


def test_a_cash_dividend_credits_exactly_qty_times_dps_once_on_the_ex_date() -> None:
    dividend = BookActionCalendar([CashDividend(isin=A, ex_date=D3, per_share=_DPS)])
    walk = _dividend_walk(dividend, {D1: (_buy(A, 100),)})
    baseline = _dividend_walk(BookActionCalendar(), {D1: (_buy(A, 100),)})

    credit = walk.cash[D3] - walk.cash[D2]
    assert isinstance(credit, Decimal)
    assert credit == Decimal("735.00")  # 100 x 7.35, exact
    assert baseline.cash[D3] - baseline.cash[D2] == 0  # skipped: nothing
    assert walk.book.dividend_income == Decimal("735.00")  # once, not twice
    assert walk.sim.cash == walk.book.cash  # the policy's cash sees it too
    assert walk.nav[D3] == walk.nav[D2]  # the price drop is paid for exactly
    rows = [e for e in walk.sim.ledger() if e.description.startswith("DIVIDEND")]
    assert len(rows) == 1 and rows[0].session == D3 and rows[0].credit == Decimal("735.00")


def test_a_dividend_is_paid_on_shares_bought_before_the_ex_date_only() -> None:
    dividend = BookActionCalendar([CashDividend(isin=A, ex_date=D3, per_share=_DPS)])
    # Staged D2, filled on the ex-date itself: bought ex, not entitled.
    late = _dividend_walk(dividend, {D2: (_buy(A, 100),)})
    assert late.book.dividend_income == 0
    # Sold on the ex-date: held at the record date, still entitled.
    sold = _dividend_walk(dividend, {D1: (_buy(A, 100),), D2: (_sell(A, 100),)})
    assert sold.book.dividend_income == Decimal("735.00")


# ── ISIN reissue ───────────────────────────────────────────────────────────────────────────────


def test_a_split_across_a_reissue_carries_the_retired_holding_to_the_survivor() -> None:
    prices = {(P, D1): Decimal("100"), (P, D2): Decimal("100")}
    prices.update({(S, day): Decimal("50") for day in (D3, D4, D5)})
    split = ShareRescale(
        isin=S,
        ex_date=D3,
        kind=RescaleKind.SPLIT,
        numerator=Decimal("2"),
        denominator=Decimal("1"),
        carried_from=P,
    )
    walk = _Walk(prices, _Scripted({D1: (_buy(P, 100),)}), BookActionCalendar([split]))
    walk.run()
    assert walk.book.position(P) is None and walk.sim.held_quantity(P) == 0
    survivor = walk.book.position(S)
    assert survivor is not None and survivor.quantity == 200
    assert walk.nav[D3] == walk.nav[D2]


# ── translating the store's rows ───────────────────────────────────────────────────────────────


class _Row:
    def __init__(self, isin: str, ex_date: date, action_type: ActionType, terms: object) -> None:
        self.isin, self.ex_date, self.action_type, self.terms = isin, ex_date, action_type, terms


def test_store_rows_land_on_the_isin_live_on_their_ex_date() -> None:
    reissue = date(2021, 10, 29)
    chain = {S: (P, S)}
    effective = {P: reissue}
    rows = [
        _Row(
            S,
            reissue,
            ActionType.SPLIT,
            FaceValueTerms(from_value=Decimal(10), to_value=Decimal(2)),
        ),
        _Row(
            S,
            date(2021, 8, 1),
            ActionType.DIVIDEND,
            DividendTerms(dividend_kind=DividendKind.FINAL, amount_inr=Decimal("2.5")),
        ),
        _Row(
            S,
            date(2022, 8, 1),
            ActionType.DIVIDEND,
            DividendTerms(dividend_kind=DividendKind.FINAL, amount_inr=Decimal("3")),
        ),
        _Row(
            A,
            date(2022, 1, 3),
            ActionType.BONUS,
            RatioTerms(new_shares=Decimal(1), held_shares=Decimal(2)),
        ),
        _Row(A, date(2022, 2, 1), ActionType.DEMERGER, UnquantifiedTerms()),
        _Row(
            A,
            date(2022, 3, 1),
            ActionType.DIVIDEND,
            DividendTerms(dividend_kind=DividendKind.FINAL, percent_of_face_value=Decimal(50)),
        ),
        _Row(
            A,
            date(2022, 4, 1),
            ActionType.MERGER,
            ExchangeRatioTerms(shares_received=Decimal(1), shares_held=Decimal(1)),
        ),
    ]
    out = _to_book_actions(
        rows, lambda isin: chain.get(isin, (isin,)), effective.get, reissues=[(P, S, reissue)]
    )
    assert out == [
        ShareRescale(S, reissue, RescaleKind.SPLIT, Decimal(10), Decimal(2)),
        CashDividend(P, date(2021, 8, 1), Decimal("2.5")),  # before the reissue: paid on P
        CashDividend(S, date(2022, 8, 1), Decimal("3")),
        ShareRescale(A, date(2022, 1, 3), RescaleKind.BONUS, Decimal(3), Decimal(2)),
        UnmodelledAction(A, date(2022, 2, 1), "DEMERGER"),
        # the percent-of-face-value dividend is skipped, not guessed
        UnmodelledAction(A, date(2022, 4, 1), "MERGER"),
        # the split on the reissue date explains it: carried (before the split, see _ORDER)
        IsinReissue(S, reissue, P, explained=True),
    ]


# ── the split ex on the retired ISIN's last session, the survivor trading from the next ─────────
#
# How NSE actually prints it (HDFC Bank 2019, Britannia 2018, IGL 2017, BEL 2017, IRCTC 2021): the
# split is ex on 19-09-2019 and the *old* ISIN INE040A01026 carries that day's halved bar; the new
# ISIN INE040A01034 first trades on 20-09-2019, the lineage edge's effective date. The ex-date is
# one session before the reissue, so a carry keyed on "a split on the effective date" never fired:
# the doubled holding stayed on the dead ISIN, marked at its last price for ever.

HDFC_P = "INE040A01026"
HDFC_S = "INE040A01034"
HDFC_EX, HDFC_NEW = date(2019, 9, 19), date(2019, 9, 20)


def _hdfc_rows() -> list[_Row]:
    return [
        _Row(
            HDFC_S,  # stored against the survivor, as the store files it
            HDFC_EX,
            ActionType.SPLIT,
            FaceValueTerms(from_value=Decimal(2), to_value=Decimal(1)),
        )
    ]


def _hdfc_actions(*, reissues: bool = True) -> list[object]:
    chain = {HDFC_S: (HDFC_P, HDFC_S)}
    return list(
        _to_book_actions(
            _hdfc_rows(),
            lambda isin: chain.get(isin, (isin,)),
            {HDFC_P: HDFC_NEW}.get,
            reissues=[(HDFC_P, HDFC_S, HDFC_NEW)] if reissues else (),
        )
    )


def test_a_split_ex_the_session_before_the_reissue_lands_on_the_retired_isin_then_carries() -> None:
    assert _hdfc_actions() == [
        # P's own bar prints the split on 19-09: the rescale is P's ...
        ShareRescale(HDFC_P, HDFC_EX, RescaleKind.SPLIT, Decimal(2), Decimal(1)),
        # ... and the holding moves to S on S's first session.
        IsinReissue(HDFC_S, HDFC_NEW, HDFC_P, explained=True),
    ]


def test_a_reissue_no_split_or_bonus_explains_is_not_carried() -> None:
    far = [(HDFC_P, HDFC_S, date(2019, 12, 2))]  # the split is 74 days earlier
    chain = {HDFC_S: (HDFC_P, HDFC_S)}
    out = _to_book_actions(
        _hdfc_rows(), lambda isin: chain.get(isin, (isin,)), {HDFC_P: far[0][2]}.get, reissues=far
    )
    assert IsinReissue(HDFC_S, date(2019, 12, 2), HDFC_P, explained=False) in out


class _StaleMarkWalk(_Walk):
    """A walk that marks a held ISIN with no bar at its last close, as the drivers' NAV does."""

    def _sample(self, session: date) -> None:
        last: dict[str, Decimal] = {}
        for (isin, day), price in sorted(self.market.prices.items(), key=lambda kv: kv[0][1]):
            if day <= session:
                last[isin] = price
        self.nav[session] = self.book.net_asset_value(last)
        self.cash[session] = self.book.cash
        self.settled[session] = {h.isin: h.quantity for h in self.sim.holdings()}
        self.pending[session] = tuple((p.isin, p.session, p.quantity) for p in self.sim.positions())


# T+2 era sessions around the HDFC split: Tue 17-09 .. Mon 23-09-2019.
H1, H2, H3, H4, H5 = (date(2019, 9, d) for d in (17, 18, 19, 20, 23))
HDFC_SESSIONS = (H1, H2, H3, H4, H5)
assert (H3, H4) == (HDFC_EX, HDFC_NEW)


def _hdfc_prices(survivor_close: Decimal = Decimal("1230")) -> dict[tuple[str, date], Decimal]:
    prices = {(HDFC_P, H1): Decimal("2240"), (HDFC_P, H2): Decimal("2200")}
    prices[(HDFC_P, H3)] = Decimal("1100")  # the ex-date bar, still on the old ISIN
    prices[(HDFC_S, H4)] = Decimal("1100")
    prices[(HDFC_S, H5)] = survivor_close
    return prices


def _hdfc_walk(policy: object, *, reissues: bool = True) -> _StaleMarkWalk:
    walk = _StaleMarkWalk(
        _hdfc_prices(),
        policy,
        BookActionCalendar(_hdfc_actions(reissues=reissues)),  # type: ignore[arg-type]
        sessions=HDFC_SESSIONS,
    )
    walk.run()
    return walk


def test_hdfc_2019_the_split_holding_follows_the_stock_to_its_new_isin() -> None:
    walk = _hdfc_walk(_Scripted({H1: (_buy(HDFC_P, 100),)}))
    assert walk.sim.held_quantity(HDFC_P) == 0 and walk.book.position(HDFC_P) is None
    survivor = walk.book.position(HDFC_S)
    assert survivor is not None and survivor.quantity == 200
    # NAV is continuous through the ex-date and the reissue, then follows the survivor's price.
    assert walk.nav[H3] == walk.nav[H2]  # 100 x 2200 became 200 x 1100 on the old ISIN
    assert walk.nav[H4] == walk.nav[H3]
    assert walk.nav[H5] - walk.nav[H4] == 200 * (Decimal("1230") - Decimal("1100"))
    assert walk.broker.corporate_actions_applied == {"REISSUE": 1, "SPLIT": 1}


def test_hdfc_2019_without_the_carry_the_holding_is_stranded_at_a_stale_mark() -> None:
    walk = _hdfc_walk(_Scripted({H1: (_buy(HDFC_P, 100),)}), reissues=False)
    assert walk.sim.held_quantity(HDFC_P) == 200  # split applied, never carried
    assert walk.nav[H5] == walk.nav[H4]  # the survivor's +₹130 never reaches the book


def test_hdfc_2019_the_tax_ledger_keeps_the_original_acquisition_date() -> None:
    walk = _hdfc_walk(_Scripted({H1: (_buy(HDFC_P, 100),)}))
    from backtest.tax import ReissueEvent, SplitEvent

    ledger = walk.broker.run_ledger(
        source="hdfc",
        terminal=H5,
        terminal_nav=walk.nav[H5],
        terminal_prices={HDFC_S: Decimal("1230")},
    )
    assert ledger.corporate_events == (
        SplitEvent(HDFC_P, H3, 2, 1, 200),
        ReissueEvent(HDFC_S, H4, HDFC_P),
    )
    (buy,) = ledger.trades
    assert (buy.isin, buy.trade_date) == (HDFC_P, H2)  # the lots carry this date across the hop


def test_t2_a_pending_lot_is_split_on_the_old_isin_and_settles_under_the_new_one() -> None:
    # Staged H1, filled H2: under T+2 the lot is pending through the ex-date H3 and settles on
    # H4 — the reissue session, after the carry has moved it.
    walk = _hdfc_walk(_Scripted({H1: (_buy(HDFC_P, 100),)}))
    assert walk.settled[H3] == {}
    assert walk.pending[H3] == ((HDFC_P, H2, 200),)  # rescaled while still in settlement
    assert walk.settled[H4] == {HDFC_S: 200}  # carried pending, delivered under the survivor
    assert walk.pending[H4] == ()


def test_an_unmodelled_action_on_a_held_name_is_counted_and_changes_nothing() -> None:
    walk = _split_walk(BookActionCalendar([UnmodelledAction(A, D3, "DEMERGER")]))
    assert walk.broker.corporate_actions_applied == {"unmodelled:DEMERGER": 1}
    position = walk.book.position(A)
    assert position is not None and position.quantity == 100


def test_an_ex_date_between_sessions_is_applied_on_the_next_session() -> None:
    # The walk visits D1, D2, D4, D5; the ex-date D3 is not a replayed session.
    walk = _Walk(
        _flat_then(A, Decimal("100"), Decimal("50")),
        _Scripted({D1: (_buy(A, 100),)}),
        BookActionCalendar([_SPLIT_2_FOR_1]),
        sessions=(D1, D2, D4, D5),
    )
    walk.run()
    position = walk.book.position(A)
    assert position is not None and position.quantity == 200
    assert walk.nav[D4] == walk.nav[D2]


# ── PIT: the wiring never reaches a decision input ─────────────────────────────────────────────


class _RecordingData:
    """A ``MomentumData`` that logs every read the policy makes through it."""

    def __init__(self) -> None:
        self.reads: list[tuple[date, tuple[MomentumRecord, ...]]] = []

    def is_rebalance(self, session: date) -> bool:
        return True

    def signal(self, as_of: date) -> Dataset[MomentumRecord]:
        price = Decimal("100") if as_of < D3 else Decimal("50")  # the raw split, as L1 prints it
        records = (
            MomentumRecord(isin=A, momentum=Decimal("0.1"), price=price, knowable_date=as_of),
        )
        self.reads.append((as_of, records))
        return Dataset.declaring(
            f"m@{as_of.isoformat()}", records, knowable_date=lambda r: r.knowable_date
        )


class _RecordingPolicy:
    """The real naive momentum policy, with the PIT scope of every context it was handed logged."""

    def __init__(self, data: _RecordingData) -> None:
        self._inner = NaiveMomentumPolicy(data, MomentumParameters(top_n=1))
        self.scopes: list[date] = []

    def decide(self, ctx: SessionContext) -> SessionDecision:
        self.scopes.append(ctx.pit.as_of)
        return self._inner.decide(ctx)


class _HdfcRecordingData(_RecordingData):
    """The decision-side view of the HDFC fixture: the raw bar of whichever ISIN traded."""

    def signal(self, as_of: date) -> Dataset[MomentumRecord]:
        isin, price = (HDFC_P, Decimal("2200")) if as_of < H4 else (HDFC_S, Decimal("1100"))
        if as_of == H3:
            price = Decimal("1100")
        records = (
            MomentumRecord(isin=isin, momentum=Decimal("0.1"), price=price, knowable_date=as_of),
        )
        self.reads.append((as_of, records))
        return Dataset.declaring(
            f"m@{as_of.isoformat()}", records, knowable_date=lambda r: r.knowable_date
        )


def test_the_reissue_carry_changes_no_decision_input() -> None:
    """With and without the carry: every signal read, PIT scope and split factor byte-identical."""
    from backtest.book_actions import current_signal_split_factors, signal_split_factors

    runs = []
    for reissues in (False, True):
        data = _HdfcRecordingData()
        policy = _RecordingPolicy(data)
        calendar = BookActionCalendar(_hdfc_actions(reissues=reissues))  # type: ignore[arg-type]
        with signal_split_factors(calendar):
            factors = current_signal_split_factors()
        walk = _StaleMarkWalk(_hdfc_prices(), policy, calendar, sessions=HDFC_SESSIONS)
        walk.run()
        runs.append((repr(data.reads).encode(), policy.scopes, repr(factors).encode(), walk))
    (reads_off, scopes_off, factors_off, off), (reads_on, scopes_on, factors_on, on) = runs
    assert reads_on == reads_off
    assert scopes_on == scopes_off == list(HDFC_SESSIONS)
    assert factors_on == factors_off
    # ...while the account did change: the fix is live.
    assert on.sim.held_quantity(HDFC_P) != off.sim.held_quantity(HDFC_P)


def test_a_policys_decision_inputs_are_identical_with_and_without_the_wiring() -> None:
    runs = []
    for actions in (None, BookActionCalendar([_SPLIT_2_FOR_1])):
        data = _RecordingData()
        policy = _RecordingPolicy(data)
        walk = _Walk(_flat_then(A, Decimal("100"), Decimal("50")), policy, actions)
        walk.run()
        runs.append((data.reads, policy.scopes, walk))
    (reads_off, scopes_off, off), (reads_on, scopes_on, on) = runs
    assert reads_on == reads_off  # every signal read, record for record
    assert scopes_on == scopes_off == list(SESSIONS)  # PIT scope is the session, untouched
    # ...while the account the policy trades against did change (the wiring is live).
    assert on.sim.held_quantity(A) != off.sim.held_quantity(A)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_book_action_read_is_physically_outside_the_decision_path() -> None:
    root = Path(__file__).resolve().parents[2]
    own = _imports(root / "backtest" / "book_actions.py")
    assert not any(m.startswith("dataplatform.query") for m in own), own
    for pkg in ("analyst", "backtest/policies"):
        for path in (root / pkg).rglob("*.py"):
            assert "backtest.book_actions" not in _imports(path), path


# ── determinism ────────────────────────────────────────────────────────────────────────────────


def test_two_runs_with_corporate_actions_are_byte_identical() -> None:
    actions = BookActionCalendar(
        [_SPLIT_2_FOR_1, CashDividend(isin=A, ex_date=D4, per_share=Decimal("1.5"))]
    )
    first = _Walk(
        _flat_then(A, Decimal("100"), Decimal("50")), _Scripted({D1: (_buy(A, 100),)}), actions
    ).run()
    second = _Walk(
        _flat_then(A, Decimal("100"), Decimal("50")), _Scripted({D1: (_buy(A, 100),)}), actions
    ).run()
    assert first.journal_bytes() == second.journal_bytes()
    assert first.book_bytes() == second.book_bytes()
    assert b"DIVIDEND" in first.book_bytes()


def test_the_context_switch_reaches_an_accounting_broker_built_inside_it() -> None:
    actions = BookActionCalendar([_SPLIT_2_FOR_1])
    with book_corporate_actions(actions):
        walk = _Walk(
            _flat_then(A, Decimal("100"), Decimal("50")), _Scripted({D1: (_buy(A, 100),)}), None
        )
    walk.run()
    assert walk.sim.held_quantity(A) == 200


def test_a_float_dividend_is_refused() -> None:
    with pytest.raises(TypeError):
        CashDividend(isin=A, ex_date=D3, per_share=7.35)  # type: ignore[arg-type]


# ── under settlement: T+2 (2019), where a buy is still pending on the ex-date ───────────────────
#
# 2019 is the T+2 rolling era (execution/settlement/cycles.yaml). A buy staged on T1 fills on T2
# and its shares are deliverable for fills from T4; on T3 they are pending. Entitlement follows the
# trade date, so a split or dividend ex on T3 reaches them anyway — and every test below fails if
# the pending lot is skipped (the broker and the book part, or the NAV steps down).

T1, T2, T3, T4, T5 = (date(2019, 3, d) for d in (4, 5, 6, 7, 8))
T2_ERA = (T1, T2, T3, T4, T5)


def _t2_prices(
    isin: str, before: Decimal, after: Decimal, ex: date = T3
) -> dict[tuple[str, date], Decimal]:
    return {(isin, day): (before if day < ex else after) for day in T2_ERA}


def test_t2_a_split_ex_while_the_buy_is_pending_delivers_twice_the_shares() -> None:
    split = ShareRescale(A, T3, RescaleKind.SPLIT, Decimal("10"), Decimal("5"))
    walk = _Walk(
        _t2_prices(A, Decimal("100"), Decimal("50")),
        _Scripted({T1: (_buy(A, 100),)}),
        BookActionCalendar([split]),
        sessions=T2_ERA,
    )
    walk.run()
    # On the ex-date the lot is still in settlement — and already rescaled.
    assert walk.settled[T3] == {}
    assert walk.pending[T3] == ((A, T2, 200),)
    # Its settlement delivers the rescaled count, not the traded one.
    assert walk.settled[T4] == {A: 200}
    assert walk.pending[T4] == ()
    position = walk.book.position(A)
    assert position is not None and position.quantity == 200
    assert walk.nav[T3] == walk.nav[T2]
    assert walk.nav[T5] == walk.nav[T2]


def test_t2_the_same_walk_without_the_action_is_the_fake_drawdown() -> None:
    walk = _Walk(
        _t2_prices(A, Decimal("100"), Decimal("50")),
        _Scripted({T1: (_buy(A, 100),)}),
        BookActionCalendar(),
        sessions=T2_ERA,
    )
    walk.run()
    assert walk.settled[T4] == {A: 100}
    assert walk.nav[T3] < walk.nav[T2] - Decimal("4900")


def test_t2_two_pending_lots_are_both_rescaled_and_both_settle_the_rescaled_count() -> None:
    # Bought on T2 and T3; a 3:2 bonus ex T4 finds both pending (one per fill session). 101 + 101
    # shares x 5/2 is 505 exactly, while each lot alone floors 252.5 to 252: the left-over share
    # goes to the later lot so the broker agrees with the book's single position.
    bonus = ShareRescale(A, T4, RescaleKind.BONUS, Decimal("5"), Decimal("2"))
    walk = _Walk(
        _t2_prices(A, Decimal("100"), Decimal("40"), ex=T4),
        _Scripted({T1: (_buy(A, 101),), T2: (_buy(A, 101),)}),
        BookActionCalendar([bonus]),
        sessions=T2_ERA,
    )
    walk.run()
    assert walk.pending[T3] == ((A, T2, 101), (A, T3, 101))
    assert walk.pending[T4] == ((A, T3, 253),)  # T2's lot settled into holdings on T4
    assert walk.settled[T4] == {A: 252}
    assert walk.settled[T5] == {A: 505}
    position = walk.book.position(A)
    assert position is not None and position.quantity == 505
    assert walk.nav[T4] == walk.nav[T3]


def test_t2_a_dividend_ex_while_the_buy_is_pending_is_credited_once() -> None:
    dps = Decimal("4.20")
    walk = _Walk(
        _t2_prices(A, Decimal("100"), Decimal("100") - dps),
        _Scripted({T1: (_buy(A, 100),)}),
        BookActionCalendar([CashDividend(isin=A, ex_date=T3, per_share=dps)]),
        sessions=T2_ERA,
    )
    walk.run()
    assert walk.pending[T3] == ((A, T2, 100),)  # still in settlement on the ex-date
    assert walk.cash[T3] - walk.cash[T2] == Decimal("420.00")
    assert walk.book.dividend_income == Decimal("420.00")  # once — not again when it settles
    rows = [e for e in walk.sim.ledger() if e.description.startswith("DIVIDEND")]
    assert len(rows) == 1 and rows[0].credit == Decimal("420.00")
    assert walk.sim.cash == walk.book.cash
    assert walk.nav[T3] == walk.nav[T2]


def test_t2_a_buy_filled_on_the_ex_date_is_not_entitled() -> None:
    dps = Decimal("4.20")
    walk = _Walk(
        _t2_prices(A, Decimal("100"), Decimal("100") - dps),
        _Scripted({T2: (_buy(A, 100),)}),
        BookActionCalendar([CashDividend(isin=A, ex_date=T3, per_share=dps)]),
        sessions=T2_ERA,
    )
    walk.run()
    assert walk.book.dividend_income == 0


def test_t2_a_reissue_carries_a_pending_lot_which_settles_under_the_survivor() -> None:
    prices = {(P, T1): Decimal("100"), (P, T2): Decimal("100")}
    prices.update({(S, day): Decimal("50") for day in (T3, T4, T5)})
    split = ShareRescale(S, T3, RescaleKind.SPLIT, Decimal("2"), Decimal("1"), carried_from=P)
    walk = _Walk(
        prices, _Scripted({T1: (_buy(P, 100),)}), BookActionCalendar([split]), sessions=T2_ERA
    )
    walk.run()
    assert walk.pending[T3] == ((S, T2, 200),)  # moved and rescaled, keeping its trade date
    assert walk.settled[T4] == {S: 200}
    assert walk.nav[T3] == walk.nav[T2]


def test_a_lot_traded_on_the_ex_date_is_refused_rather_than_let_the_books_part() -> None:
    # Walk T1..T2 only: A is bought on T2 and still pending. An action dated T2 applied late would
    # hit a lot the ex-date rule excludes in the broker but the date-less book would include.
    walk = _Walk(
        _t2_prices(A, Decimal("100"), Decimal("50")),
        _Scripted({T1: (_buy(A, 100),)}),
        BookActionCalendar(),
        sessions=(T1, T2),
    )
    walk.run()
    assert walk.pending[T2] == ((A, T2, 100),)
    late = BookActionApplier(
        BookActionCalendar([ShareRescale(A, T2, RescaleKind.SPLIT, Decimal(2), Decimal(1))])
    )
    with pytest.raises(BookError):
        late.apply(T3, sim=walk.sim, book=walk.book)


# ── the swing policy's trailing stop reads the split off the account ──────────────────────────


class _SwingMarks:
    """A ``SwingCompositeData`` with no candidates: only the day's raw closes, for the stop."""

    def __init__(self, prices: Mapping[tuple[str, date], Decimal]) -> None:
        self._prices = prices

    def is_rebalance(self, session: date) -> bool:
        return False

    def signal(self, as_of: date) -> Dataset[SwingRecord]:
        return Dataset.declaring("swing", (), knowable_date=lambda r: r.knowable_date)

    def marks(self, as_of: date) -> Dataset[SwingRecord]:
        zero = Decimal("0")
        records = tuple(
            SwingRecord(
                isin=isin,
                high_proximity=_ONE,
                delivery_share=zero,
                momentum_12_1=zero,
                volatility=zero,
                price=price,
                knowable_date=as_of,
            )
            for (isin, day), price in sorted(self._prices.items())
            if day == as_of
        )
        return Dataset.declaring("marks", records, knowable_date=lambda r: r.knowable_date)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        return Dataset.declaring("regime", (), knowable_date=lambda r: r.knowable_date)


class _BuyThenSwing:
    """Buys 100 A on D1, then leaves every decision to the swing policy — whose stops it records."""

    def __init__(self, swing: SwingCompositePolicy) -> None:
        self._swing = swing
        self.stop_sells: list[date] = []

    def decide(self, ctx: SessionContext) -> SessionDecision:
        decision = self._swing.decide(ctx)
        self.stop_sells += [ctx.session for o in decision.orders if o.side is Side.SELL]
        if ctx.session != D1:
            return decision
        return SessionDecision(evidence=decision.evidence, orders=(*decision.orders, _buy(A, 100)))


def _swing_split_walk(actions: BookActionCalendar, after: Decimal) -> _BuyThenSwing:
    """100 A bought on D1 (settles D3 under T+1), closes 100 until a D5 ex-date, then ``after``."""
    prices = _flat_then(A, Decimal("100"), after, ex=D5)
    policy = _BuyThenSwing(
        SwingCompositePolicy(
            _SwingMarks(prices),
            SwingCompositeParameters(trailing_stop=Decimal("0.25"), min_hold_sessions=0),
        )
    )
    _Walk(prices, policy, actions).run()
    return policy


def test_the_swing_stop_does_not_sell_a_split_the_book_applied() -> None:
    """Real stack: the applier doubles the count on D5 at an unchanged basis, the close halves,
    and the swing policy reads that off its broker — no stop."""
    split_on_d5 = ShareRescale(
        isin=A, ex_date=D5, kind=RescaleKind.SPLIT, numerator=Decimal("2"), denominator=_ONE
    )
    assert _swing_split_walk(BookActionCalendar([split_on_d5]), Decimal("50")).stop_sells == []


def test_the_swing_stop_sells_the_same_close_with_no_split_on_the_account() -> None:
    """The inversion: the same halved close with the count unchanged is a crash, and exits."""
    assert _swing_split_walk(BookActionCalendar(), Decimal("50")).stop_sells == [D5]
