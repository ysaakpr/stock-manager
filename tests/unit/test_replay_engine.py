"""M4.8 — the replay engine's structural checks, offline (EXECUTION_PLAN §7, §8.3.3).

The integration determinism harness proves byte-identity over a real trading policy. These tests
pin the engine's *guards* — the refusals that keep a replay honest — each with a case that would
pass if the guard were removed:

* sessions must be non-empty and strictly increasing (a replay runs forward, once);
* a decision's evidence and every entry must be about the session under replay (invariant #7);
* the session context is scoped to exactly the session (``pit.as_of == session``) and a mismatched
  context is refused at construction;
* an empty decision still journals one ``HEARTBEAT`` by ``T0`` stamped with the evidence reference
  (invariant #9), while an entry that names its own snapshot keeps it;
* the clock is frozen at each session before the policy runs (B10) and the policy's orders are
  placed in the order returned, with no order of the engine's own.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry
from backtest.replay import (
    ReplayEngine,
    ReplayError,
    SessionContext,
    SessionDecision,
)
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import PitContext
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
ISIN = "INE009A01021"


class _Broker:
    """A recording broker: enough of the surface for the engine, and a log of what it was asked."""

    def __init__(self) -> None:
        self.executed: list[date] = []
        self.placed: list[OrderRequest] = []

    def execute_session(self, session: date) -> tuple[Order, ...]:
        self.executed.append(session)
        return ()

    def session_valid(self) -> bool:
        return True

    def place(self, request: OrderRequest) -> Order:
        self.placed.append(request)
        return Order(
            order_id=f"T-{len(self.placed):04d}",
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
        return ()

    def ledger(self) -> tuple[LedgerEntry, ...]:
        return ()

    def margins(self) -> Margins:
        return Margins(available=Decimal("1000"), utilised=Decimal("0"))


def _evidence(session: date) -> EvidenceBundle:
    return EvidenceBundle(
        trading_date=session,
        actor=Actor.T0,
        items=(
            EvidenceItem(kind=EvidenceKind.PRICE, source="test", label="close", value=Decimal(1)),
        ),
    )


class _Policy:
    """Returns whatever the test scripted for each session, and records what it saw."""

    def __init__(self, scripted: dict[date, SessionDecision]) -> None:
        self._scripted = scripted
        self.seen: list[SessionContext] = []

    def decide(self, ctx: SessionContext) -> SessionDecision:
        self.seen.append(ctx)
        return self._scripted.get(ctx.session, SessionDecision(evidence=_evidence(ctx.session)))


def _engine(policy: _Policy, sessions: list[date], broker: _Broker | None = None) -> ReplayEngine:
    return ReplayEngine(
        policy=policy,
        broker=broker if broker is not None else _Broker(),
        clock=FrozenClock(sessions[0]),
        sessions=sessions,
    )


def test_an_empty_session_list_is_refused() -> None:
    with pytest.raises(ReplayError, match="at least one session"):
        ReplayEngine(policy=_Policy({}), broker=_Broker(), clock=FrozenClock(S1), sessions=[])


@pytest.mark.parametrize("sessions", [[S2, S1], [S1, S1], [S1, S3, S2]])
def test_sessions_must_strictly_increase(sessions: list[date]) -> None:
    with pytest.raises(ReplayError, match="strictly increasing"):
        _engine(_Policy({}), sessions)


def test_each_context_is_scoped_to_its_own_session_and_the_clock_is_frozen_there() -> None:
    policy = _Policy({})
    broker = _Broker()
    _engine(policy, [S1, S2, S3], broker).run()
    assert [ctx.session for ctx in policy.seen] == [S1, S2, S3]
    assert [ctx.pit.as_of for ctx in policy.seen] == [S1, S2, S3]
    # The broker settled/filled each session before the policy decided on it (T+1 shape).
    assert broker.executed == [S1, S2, S3]


def test_a_context_whose_pit_scope_is_not_the_session_is_refused() -> None:
    with pytest.raises(ReplayError, match="inconsistent"):
        SessionContext(
            session=S1, pit=PitContext(as_of=S2), broker=_Broker(), clock=FrozenClock(S1)
        )


def test_evidence_about_another_session_is_refused() -> None:
    policy = _Policy({S1: SessionDecision(evidence=_evidence(S2))})
    with pytest.raises(ReplayError, match="evidence must be about the session"):
        _engine(policy, [S1, S2]).run()


def test_an_entry_dated_to_another_session_is_refused() -> None:
    # An entry about S1 returned while deciding S2: the model allows a past-dated entry (it only
    # forbids future-dated ones), so this is exactly the mismatch the engine must catch itself.
    clock = FrozenClock(S2)
    stray = JournalEntry(
        ts=clock.now(), trading_date=S1, actor=Actor.T0, decision=Decision.HOLD, rationale="x"
    )
    policy = _Policy({S2: SessionDecision(evidence=_evidence(S2), entries=(stray,))})
    with pytest.raises(ReplayError, match="journal entry for"):
        _engine(policy, [S1, S2]).run()


def test_an_empty_decision_journals_one_heartbeat_with_the_evidence_ref() -> None:
    result = _engine(_Policy({}), [S1, S2]).run()
    assert len(result.journal) == 2
    for entry, session in zip(result.journal, (S1, S2), strict=True):
        assert entry.decision is Decision.HEARTBEAT
        assert entry.actor is Actor.T0
        assert entry.trading_date == session
        assert entry.evidence_snapshot_ref == _evidence(session).ref().ref


def test_entries_without_a_snapshot_are_stamped_and_those_with_one_are_kept() -> None:
    clock = FrozenClock(S1)
    own_ref = _evidence(S3).ref().ref  # a different bundle the policy claims it decided on
    stamped = JournalEntry(
        ts=clock.now(), trading_date=S1, actor=Actor.T0, decision=Decision.HOLD, rationale="a"
    )
    kept = JournalEntry(
        ts=clock.now(),
        trading_date=S1,
        actor=Actor.T0,
        decision=Decision.HOLD,
        rationale="b",
        evidence_snapshot_ref=own_ref,
    )
    policy = _Policy({S1: SessionDecision(evidence=_evidence(S1), entries=(stamped, kept))})
    result = _engine(policy, [S1]).run()
    assert result.journal[0].evidence_snapshot_ref == _evidence(S1).ref().ref
    assert result.journal[1].evidence_snapshot_ref == own_ref
    assert all(entry.decision is not Decision.HEARTBEAT for entry in result.journal)


def test_orders_are_placed_in_the_order_returned_and_none_are_the_engines_own() -> None:
    first = OrderRequest(isin=ISIN, side=Side.BUY, quantity=3, exchange=Exchange.NSE)
    second = OrderRequest(isin="INE002A01018", side=Side.SELL, quantity=1, exchange=Exchange.NSE)
    policy = _Policy({S1: SessionDecision(evidence=_evidence(S1), orders=(first, second))})
    broker = _Broker()
    _engine(policy, [S1, S2], broker).run()
    assert broker.placed == [first, second]


def test_the_result_digest_moves_with_the_journal_and_the_book() -> None:
    a = _engine(_Policy({}), [S1, S2]).run()
    b = _engine(_Policy({}), [S1, S2]).run()
    c = _engine(_Policy({}), [S1, S2, S3]).run()
    assert a.digest() == b.digest()
    assert a.digest() != c.digest()
    assert a.journal_bytes() == b.journal_bytes()
    assert a.book_bytes() == b.book_bytes()
