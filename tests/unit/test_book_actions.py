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
from backtest.accounting import PortfolioBook
from backtest.book_actions import (
    BookActionCalendar,
    CashDividend,
    RescaleKind,
    ShareRescale,
    UnmodelledAction,
    _to_book_actions,
    book_corporate_actions,
)
from backtest.policies.naive_momentum import MomentumParameters, MomentumRecord, NaiveMomentumPolicy
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

A = "INE001A01036"
P = "INE335Y01012"  # retired at the reissue
S = "INE335Y01020"  # the survivor

D1, D2, D3, D4, D5 = (date(2024, 1, d) for d in (1, 2, 3, 4, 5))
SESSIONS = (D1, D2, D3, D4, D5)
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
        self.broker = _AccountingBroker(
            self.sim, self.book, nav_sink=self._sample, corporate_actions=actions
        )
        self.engine = ReplayEngine(
            policy=policy,  # type: ignore[arg-type]
            broker=self.broker,
            clock=clock,
            sessions=sessions,
        )

    def _sample(self, session: date) -> None:
        closes = {
            isin: price for (isin, day), price in self.market.prices.items() if day == session
        }
        self.nav[session] = self.book.net_asset_value(closes)
        self.cash[session] = self.book.cash

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
    walk = _Walk(
        _flat_then(A, Decimal("100"), Decimal("50")),
        _Scripted({D1: (_buy(A, 100),), D2: (_sell(A, 100),)}),
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
    out = _to_book_actions(rows, lambda isin: chain.get(isin, (isin,)), effective.get)
    assert out == [
        ShareRescale(S, reissue, RescaleKind.SPLIT, Decimal(10), Decimal(2), carried_from=P),
        CashDividend(P, date(2021, 8, 1), Decimal("2.5")),  # before the reissue: paid on P
        CashDividend(S, date(2022, 8, 1), Decimal("3")),
        ShareRescale(A, date(2022, 1, 3), RescaleKind.BONUS, Decimal(3), Decimal(2)),
        UnmodelledAction(A, date(2022, 2, 1), "DEMERGER"),
        # the percent-of-face-value dividend is skipped, not guessed
        UnmodelledAction(A, date(2022, 4, 1), "MERGER"),
    ]


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
