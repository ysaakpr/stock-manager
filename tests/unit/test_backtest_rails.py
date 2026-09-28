"""X2 x A8 — the replay's orders pass the rails, and a block is journalled (invariant #6, #9).

Each test here would pass against an unrailed engine only if it were wrong:

* an order that breaches a cap never reaches the broker, and the session's journal carries A8's
  ``RAIL_BLOCK`` naming the rail — a block is an entry, never an absence;
* orders are cleared in sequence against the book the earlier ones leave, so a basket of sells
  cannot together take a book below ``min_holdings``;
* the backtest calls the *same* A8 entry point as the paper loop — ``RailEngine.guard_order`` and,
  beneath it, ``check_order`` — rather than a parallel implementation;
* unlabelled ISINs pool into one ``UNKNOWN`` sector that counts toward the sector cap;
* the rail policy in force is part of the run's output and digest, and blocked runs replay
  byte-identically.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Json

import analyst.rails.engine as rails_engine
from analyst.cases import RiskRails
from analyst.journal import Journal
from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve
from analyst.rails import Portfolio, ProposedOrder, RailEngine
from backtest.rails import (
    RATIFIED_SECTOR_SOURCE,
    UNEXECUTABLE_EVENT,
    UNKNOWN_SECTOR,
    BacktestRailPolicy,
    RailGate,
    SectorMap,
    ratified_backtest_rail_policy,
    ratified_sector_map,
)
from backtest.replay import ReplayEngine, SessionContext, SessionDecision
from dataplatform.clock import FrozenClock
from execution.broker import (
    Exchange,
    Holding,
    LedgerEntry,
    Margins,
    Order,
    OrderRequest,
    OrderStatus,
    Position,
    Side,
)

S1, S2, S3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
SESSIONS = (S1, S2, S3)
PRICE = Decimal("100")

#: Twelve shape-valid ISINs: A..F labelled across two sectors, U1..U3 unlabelled.
NAMES = tuple(f"INE{index:03d}A01010" for index in range(1, 13))
SECTORS = {isin: ("IT" if index % 2 else "PHARMA") for index, isin in enumerate(NAMES[:9])}
UNLABELLED = NAMES[9:]


# ── doubles ─────────────────────────────────────────────────────────────────────────────────────


class _FlatBroker:
    """A replay broker that fills every staged order at ``PRICE`` the next session, cost-free.

    Cost-free and flat on purpose: the book it ends on is exactly the book A8 projected, so a cap
    in the result is a cap A8 cleared, not one moved by slippage or a price path.
    """

    def __init__(self, cash: Decimal, holdings: dict[str, int] | None = None) -> None:
        self.cash = cash
        self.held: dict[str, int] = dict(holdings or {})
        self.placed: list[OrderRequest] = []
        self._staged: list[OrderRequest] = []

    def execute_session(self, session: date) -> tuple[Order, ...]:
        for request in self._staged:
            value = PRICE * request.quantity
            if request.side is Side.BUY:
                assert value <= self.cash, "the gate let through a buy the book cannot fund"
                self.cash -= value
                self.held[request.isin] = self.held.get(request.isin, 0) + request.quantity
            else:
                assert self.held.get(request.isin, 0) >= request.quantity, "oversold"
                self.cash += value
                self.held[request.isin] -= request.quantity
                if self.held[request.isin] == 0:
                    del self.held[request.isin]
        self._staged = []
        return ()

    def session_valid(self) -> bool:
        return True

    def place(self, request: OrderRequest) -> Order:
        self.placed.append(request)
        self._staged.append(request)
        return Order(
            order_id=f"F-{len(self.placed):04d}",
            request=request,
            status=OrderStatus.STAGED,
            decision_date=S1,
            target_session=S2,
        )

    def modify(self, order_id: str, *, quantity: int) -> Order:
        raise NotImplementedError

    def cancel(self, order_id: str) -> Order:
        raise NotImplementedError

    def positions(self) -> tuple[Position, ...]:
        return ()

    def holdings(self) -> tuple[Holding, ...]:
        return tuple(
            Holding(isin=isin, exchange=Exchange.NSE, quantity=quantity, average_price=PRICE)
            for isin, quantity in sorted(self.held.items())
        )

    def ledger(self) -> tuple[LedgerEntry, ...]:
        return ()

    def margins(self) -> Margins:
        return Margins(available=self.cash, utilised=Decimal(0))


def _evidence(session: date) -> EvidenceBundle:
    return EvidenceBundle(
        trading_date=session,
        actor=Actor.T0,
        items=(EvidenceItem(kind=EvidenceKind.PRICE, source="t", label="c", value=PRICE),),
    )


class _Scripted:
    """Returns the scripted orders on their session (each with its BUY/SELL entry), else nothing."""

    def __init__(self, orders: Mapping[date, Sequence[OrderRequest]]) -> None:
        self._orders = orders

    def decide(self, ctx: SessionContext) -> SessionDecision:
        orders = tuple(self._orders.get(ctx.session, ()))
        entries = tuple(
            JournalEntry(
                ts=ctx.clock.now(),
                trading_date=ctx.session,
                actor=Actor.T0,
                decision=Decision.BUY if order.side is Side.BUY else Decision.SELL,
                isin=order.isin,
                sleeve=Sleeve.TACTICAL,
                rationale="scripted",
            )
            for order in orders
        )
        return SessionDecision(evidence=_evidence(ctx.session), orders=orders, entries=entries)


def _marks(session: date) -> dict[str, Decimal]:
    return dict.fromkeys(NAMES, PRICE)


def _policy(**caps: Any) -> BacktestRailPolicy:
    """The ratified rails (with any cap overridden for a test), over the test sector map."""
    rails = ratified_backtest_rail_policy().rails.model_copy(update=caps)
    return BacktestRailPolicy(
        policy_id="test-ratified-caps",
        version=1,
        rails=RiskRails.model_validate(rails.model_dump()),
        sectors=SectorMap(source="test", sha256="test", by_isin=SECTORS),
        provenance="test",
    )


def _run(
    orders: Mapping[date, Sequence[OrderRequest]],
    broker: _FlatBroker,
    policy: BacktestRailPolicy | None = None,
) -> Any:
    engine = ReplayEngine(
        policy=_Scripted(orders),
        broker=broker,
        clock=FrozenClock(S1),
        sessions=SESSIONS,
        rails=RailGate(_policy() if policy is None else policy, _marks),
    )
    return engine.run()


def _buy(isin: str, quantity: int) -> OrderRequest:
    return OrderRequest(isin=isin, side=Side.BUY, quantity=quantity, exchange=Exchange.NSE)


def _sell(isin: str, quantity: int) -> OrderRequest:
    return OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, exchange=Exchange.NSE)


def _blocks(result: Any) -> list[JournalEntry]:
    return [entry for entry in result.journal if entry.decision is Decision.RAIL_BLOCK]


# ── a block is placed nowhere and journalled, naming the rail ──────────────────────────────────


def test_a_blocked_order_never_reaches_the_broker_and_is_journalled_naming_the_rail() -> None:
    # 20% of a ₹1,00,000 book in one name: over the 15% position cap (and the 15% per-order cap).
    broker = _FlatBroker(Decimal("100000"))
    result = _run({S1: [_buy(NAMES[0], 200)]}, broker, _policy(max_order_value_inr=Decimal(10**9)))

    assert broker.placed == []
    [block] = _blocks(result)
    assert block.actor is Actor.RAILS
    assert block.trading_date == S1
    assert block.isin == NAMES[0]
    assert block.sleeve is Sleeve.TACTICAL
    assert block.payload is not None
    assert set(block.payload["rails"].split(",")) == {"MAX_POSITION", "MAX_ORDER_PCT"}
    assert block.rationale is not None and "MAX_POSITION" in block.rationale
    # Stamped with the evidence the decision was made on, like every other entry of the session.
    assert block.evidence_snapshot_ref == _evidence(S1).ref().ref
    # The decision it judged comes first: "T0 decided BUY; RAILS blocked it".
    session_one = [entry for entry in result.journal if entry.trading_date == S1]
    assert [entry.decision for entry in session_one] == [Decision.BUY, Decision.RAIL_BLOCK]
    assert result.rail_blocks == {"MAX_ORDER_PCT": 1, "MAX_POSITION": 1}


def test_an_order_inside_every_cap_is_placed_and_journals_no_block() -> None:
    broker = _FlatBroker(Decimal("100000"))
    result = _run({S1: [_buy(NAMES[0], 100)]}, broker)  # 10%: inside 15%
    assert broker.placed == [_buy(NAMES[0], 100)]
    assert _blocks(result) == []
    assert broker.held == {NAMES[0]: 100}


def test_orders_are_cleared_against_the_book_the_earlier_ones_leave() -> None:
    # Two 10% buys of one name: each alone is inside 15%, together they are 20%.
    broker = _FlatBroker(Decimal("100000"))
    result = _run({S1: [_buy(NAMES[0], 100), _buy(NAMES[1], 100), _buy(NAMES[0], 100)]}, broker)
    assert broker.placed == [_buy(NAMES[0], 100), _buy(NAMES[1], 100)]
    [block] = _blocks(result)
    assert block.isin == NAMES[0]
    assert block.payload is not None and block.payload["rails"] == "MAX_POSITION"


def test_a_basket_of_sells_cannot_take_the_book_below_min_holdings() -> None:
    # Ten names held, all ten sold on one risk-off session: two may go, the other eight are refused.
    held = dict.fromkeys(NAMES[:10], 10)
    broker = _FlatBroker(Decimal("100000"), held)
    result = _run({S1: [_sell(isin, 10) for isin in NAMES[:10]]}, broker)

    assert [order.isin for order in broker.placed] == list(NAMES[:2])
    blocks = _blocks(result)
    assert [block.isin for block in blocks] == list(NAMES[2:10])
    assert all(block.payload is not None for block in blocks)
    assert {block.payload["rails"] for block in blocks if block.payload} == {"MIN_HOLDINGS"}
    assert len(broker.held) == 8


def test_an_order_the_book_cannot_hold_is_escalated_not_placed_unchecked() -> None:
    broker = _FlatBroker(Decimal("1000"))
    result = _run({S1: [_buy(NAMES[0], 100), _sell(NAMES[1], 5)]}, broker)
    assert broker.placed == []
    escalations = [e for e in result.journal if e.decision is Decision.ESCALATE]
    assert [e.isin for e in escalations] == [NAMES[0], NAMES[1]]
    assert all(e.actor is Actor.EXEC for e in escalations)
    assert all(
        e.payload is not None and e.payload["event"] == UNEXECUTABLE_EVENT for e in escalations
    )


# ── the same entry point as the paper loop ─────────────────────────────────────────────────────


class _RecordingConnection:
    """Echoes an insert's parameters back as the returned row, like ``INSERT ... RETURNING``."""

    def __init__(self) -> None:
        self.inserts = 0

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        unwrapped = [p.obj if isinstance(p, Json) else p for p in (params or ())]
        self.inserts += 1
        row = (self.inserts, *unwrapped)

        class _Cursor:
            def fetchone(self) -> tuple[Any, ...]:
                return row

        return _Cursor()


def test_backtest_and_paper_call_the_same_rail_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spy on A8's two public verdict entry points; both paths must go through both."""
    calls: list[str] = []
    real_check = rails_engine.check_order
    real_guard = RailEngine.guard_order

    def spy_check(*args: Any, **kwargs: Any) -> Any:
        calls.append("check_order")
        return real_check(*args, **kwargs)

    def spy_guard(self: RailEngine, *args: Any, **kwargs: Any) -> Any:
        calls.append("guard_order")
        return real_guard(self, *args, **kwargs)

    monkeypatch.setattr(rails_engine, "check_order", spy_check)
    monkeypatch.setattr(RailEngine, "guard_order", spy_guard)

    # The backtest path: one oversized order through the replay engine.
    broker = _FlatBroker(Decimal("100000"))
    _run({S1: [_buy(NAMES[0], 200)]}, broker)
    backtest_calls, calls[:] = list(calls), []

    # The paper path, as the daily loop wires it (analyst/cash/manager.py, analyst/rotation, the
    # M5 paper run): a RailEngine over the Postgres-shaped Journal, asked by guard_order.
    clock = FrozenClock(S1)
    journal = Journal(_RecordingConnection(), clock=clock)  # type: ignore[arg-type]
    paper = RailEngine(journal, clock=clock)
    book = Portfolio(case_id="PAPER", lots=(), cash=Decimal("100000"))
    order = ProposedOrder(request=_buy(NAMES[0], 200), price=PRICE, sector="IT")
    paper.guard_order(order, book, _policy().rails, trading_date=S1)
    paper_calls = list(calls)

    assert backtest_calls == ["guard_order", "check_order"]
    assert paper_calls == ["guard_order", "check_order"]


def test_the_backtest_holds_no_rail_logic_of_its_own() -> None:
    """No cap is compared anywhere under backtest/: the numbers are read only to build a policy."""
    backtest_dir = Path(__file__).resolve().parents[2] / "backtest"
    fields = ("max_position_pct", "max_sector_pct", "min_holdings", "max_order_")
    for path in sorted(backtest_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "def check_order" not in source, path
        assert "_breach" not in source, path
        for name in fields:
            for line in source.splitlines():
                if f".{name}" in line:
                    raise AssertionError(f"{path}: reads a cap outside A8: {line.strip()}")
    # And the gate's verdict is RailEngine's, by reference.
    assert "engine.guard_order(" in inspect.getsource(RailGate.clear)


def test_the_engine_has_no_unrailed_constructor() -> None:
    with pytest.raises(TypeError, match="rails"):
        ReplayEngine(  # type: ignore[call-arg]
            policy=_Scripted({}),
            broker=_FlatBroker(Decimal(1)),
            clock=FrozenClock(S1),
            sessions=SESSIONS,
        )


# ── unlabelled sectors pool, and count ─────────────────────────────────────────────────────────


def test_unlabelled_names_pool_into_one_unknown_sector_that_counts_toward_the_cap() -> None:
    # Three unlabelled names at 14% each: 28% pooled is fine, the third takes UNKNOWN to 42% > 35%.
    broker = _FlatBroker(Decimal("100000"))
    result = _run({S1: [_buy(isin, 140) for isin in UNLABELLED]}, broker)
    assert [order.isin for order in broker.placed] == list(UNLABELLED[:2])
    [block] = _blocks(result)
    assert block.isin == UNLABELLED[2]
    assert block.payload is not None and block.payload["rails"] == "MAX_SECTOR"
    assert block.rationale is not None and f"sector {UNKNOWN_SECTOR!r}" in block.rationale


def test_labelled_names_do_not_pool_with_the_unknown_bucket() -> None:
    # Two unlabelled at 14% (28% UNKNOWN) plus two IT names at 14% (28% IT): nothing breaches.
    it_names = [isin for isin, sector in SECTORS.items() if sector == "IT"][:2]
    broker = _FlatBroker(Decimal("100000"))
    orders = [_buy(isin, 140) for isin in (*UNLABELLED[:2], *it_names)]
    result = _run({S1: orders}, broker)
    assert broker.placed == orders
    assert _blocks(result) == []


def test_a_sector_map_may_not_use_the_reserved_unknown_label() -> None:
    with pytest.raises(ValueError, match="reserved"):
        SectorMap(source="t", sha256="t", by_isin={NAMES[0]: UNKNOWN_SECTOR})


# ── the ratified default, pinned ─────────────────────────────────────────────────────────────


def test_the_default_policy_is_the_ratified_rails() -> None:
    policy = ratified_backtest_rail_policy()
    rails = policy.rails
    assert rails.max_position_pct == Decimal(15)
    assert rails.max_sector_pct == Decimal(35)
    assert rails.min_holdings == 8
    assert rails.drawdown_review_pct == 25
    assert rails.max_order_pct_of_case == 15
    assert rails.max_order_value_inr == Decimal("120000")  # 12 x the ₹10k default SIP
    assert policy.label == "ratified-default@v1"


def test_the_default_sector_map_is_the_pinned_nse_classification() -> None:
    sectors = ratified_sector_map()
    assert sectors.source == "ind_niftytotalmarket_list.csv"
    assert len(sectors.by_isin) == 755
    assert sectors.sector_of("INE009A01021") == "Information Technology"  # Infosys
    assert sectors.sector_of("INE000X00000") == UNKNOWN_SECTOR


def test_a_classification_whose_bytes_changed_is_refused(tmp_path: Path) -> None:
    tampered = tmp_path / "ind.csv"
    tampered.write_bytes(RATIFIED_SECTOR_SOURCE.read_bytes() + b"X,Y,Z,EQ,INE999Z01011\n")
    with pytest.raises(ValueError, match="pinned"):
        SectorMap.from_industry_csv(tampered, expected_sha256=ratified_sector_map().sha256)


# ── the policy is in the output, and blocked runs replay identically ────────────────────────────


def test_the_rail_policy_in_force_is_in_the_result_and_its_digest() -> None:
    orders = {S1: [_buy(NAMES[0], 100)]}
    strict = _run(orders, _FlatBroker(Decimal("100000")))
    loose = _run(
        orders, _FlatBroker(Decimal("100000")), _policy(max_order_value_inr=Decimal(10**9))
    )
    assert strict.rail_policy == _policy().to_document()
    # Same orders, same journal, same book — a different policy is still a different run.
    assert strict.journal_bytes() == loose.journal_bytes()
    assert strict.book_bytes() == loose.book_bytes()
    assert strict.digest() != loose.digest()


def test_a_run_with_blocks_replays_byte_identically() -> None:
    orders = {
        S1: [_buy(NAMES[0], 200), _buy(NAMES[1], 100)],
        S2: [_buy(isin, 140) for isin in UNLABELLED],
        S3: [_sell(NAMES[1], 100)],
    }
    first = _run(orders, _FlatBroker(Decimal("100000")))
    second = _run(orders, _FlatBroker(Decimal("100000")))
    assert _blocks(first)
    assert first.journal_bytes() == second.journal_bytes()
    assert first.digest() == second.digest()
