"""M15.3 — the paper session goes through the live path's staging, reconciliation and kill switch.

Real-money readiness item (c): invariant #5 says paper and real share one decision path, and until
M15.3 the daily paper session placed straight onto ``SimBroker``, skipping the staging step, the
reconciliation job and the kill switch a real-money loop will run. These tests pin each half of the
acceptance contract over the frozen fixture market (``tests/paper_session_support.py``):

* **Nothing about the trading changes.** A four-month paper run (two rebalances that turn the
  basket over, a risk-off rebalance that sells, an on-time dividend and split on held names)
  decided one process per day through the new path equals one plain ``ReplayEngine`` walk — the
  backtest path, with no staging, recon or switch — fill for fill, cost for cost, ledger line for
  ledger line and Decimal for Decimal. The journal differs by exactly one reconciliation
  ``HEARTBEAT`` at the end of each decided session, and by nothing else.
* **The kill switch is consulted, and a tripped one refuses.** Tripped, the session journals one
  no-op (``KILL_SWITCH_TRIPPED``) and stages nothing; armed, it trades. Both directions are pinned,
  so removing the check or inverting it fails a test. The staging step's own check is pinned too.
* **Reconciliation runs, and a break is red until resolved.** Every decided session carries a
  clean recon; a book that no longer matches the broker is caught after the fills, trips the
  switch, stages nothing, is recorded ``RECON_BREAK`` and journaled ``ESCALATE``, and blocks every
  later session until the switch is reset *and* the owner records a resolution — the corporate-
  action escalation pattern. A recon that was skipped would let the break through.
* **Idempotency and crash-safety (M13.1) hold.** A decided date and a break date never run twice; a
  crash after a break tripped the switch leaves the book halted, never trading.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from analyst.journal.models import Decision, JournalEntry
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar, CashDividend, RescaleKind, ShareRescale
from backtest.paper_session import (
    KILL_SWITCH_EVENT,
    LATE_ACTION_EVENT,
    RECON_BREAK_EVENT,
    RECON_EVENT,
    InMemoryPaperSessionStore,
    PaperSessionRecord,
    PaperSessionResult,
    ReconStatus,
    RecordingJournal,
    RunVerdict,
    SessionOutcome,
    _PaperBroker,
    paper_kill_switch,
    run_paper_session,
)
from backtest.policies.momentum_v2 import MomentumV2Parameters, MomentumV2Policy
from backtest.rails import BACKTEST_CASE_ID, RailGate
from backtest.replay import ReplayEngine
from backtest.run import _ACCOUNT_STATE, _AccountingBroker, _held_by
from dataplatform.clock import IST, FrozenClock
from execution import kill_switch as kill_switch_module
from execution.broker import Exchange, Fill, OrderRequest, OrderType, Side
from execution.costs import CostModel, load_rate_card
from execution.kill_switch import KillSwitch, TradingHaltedError, TripSource
from execution.recon import Reconciler, RecordingAlerter
from execution.sim_broker import SimBroker
from execution.staging import StagingCoordinator
from tests.paper_session_support import (
    FIXTURE_CASH,
    ISINS,
    OCT_FIRST,
    OCT_FOURTH,
    OCT_SECOND,
    OCT_THIRD,
    SEPT_LAST,
    FixtureWorld,
    calendar_sessions,
    fixture_spec,
    fresh_kill_switch,
)

RUN_AT = datetime(2026, 10, 1, 20, 30, tzinfo=IST)
BOOK = "paper_fixture_book"

#: Top-9 of ten names with no wider band: the monthly ranking flip sells one name and buys another
#: at each rebalance, so the replay really trades after the first month.
_TURNOVER = MomentumV2Parameters(
    top_n=9,
    use_12_1=True,
    sell_band=9,
    regime_filter=True,
    vol_scaled=True,
    redeploy_next_session=True,
)


@dataclass(frozen=True, slots=True)
class _Green:
    reason: str = ""

    def __bool__(self) -> bool:
        return True


@dataclass
class _Desk:
    """One paper book's durable state — ledger, journal, kill switch — and the world it reads."""

    world: FixtureWorld = field(default_factory=FixtureWorld)
    store: InMemoryPaperSessionStore = field(default_factory=InMemoryPaperSessionStore)
    journal: RecordingJournal = field(default_factory=RecordingJournal)
    kill_switch: KillSwitch = field(default_factory=fresh_kill_switch)
    alerter: RecordingAlerter = field(default_factory=RecordingAlerter)
    parameters: MomentumV2Parameters | None = None

    @staticmethod
    def run_quantities(record: PaperSessionRecord) -> list[tuple[str, int]]:
        """(ISIN, shares) the broker held after ``record``'s session: settled plus pending."""
        out: dict[str, int] = {}
        state = record.broker_state()
        for isin, _exchange, quantity, _cost in state.holdings:
            out[isin] = out.get(isin, 0) + quantity
        for isin, _traded, _lag, _exchange, quantity, _cost in state.pending:
            out[isin] = out.get(isin, 0) + quantity
        return sorted(out.items())

    def run(self, day: date) -> PaperSessionResult:
        spec = fixture_spec() if self.parameters is None else fixture_spec(self.parameters)
        return run_paper_session(
            trading_date=day,
            spec=spec,
            world=self.world,
            store=self.store,
            journal=self.journal,
            gate=lambda _day: _Green(),
            clock=FrozenClock(RUN_AT),
            kill_switch=self.kill_switch,
            alerter=self.alerter,
        )


def _recon_entries(entries: tuple[JournalEntry, ...] | list[JournalEntry]) -> list[JournalEntry]:
    return [entry for entry in entries if entry.payload.get("event") == RECON_EVENT]


def _fill_bytes(fills: list[Fill] | tuple[Fill, ...]) -> bytes:
    """Fills as canonical bytes, every Decimal by its exact string."""
    return json.dumps(
        [dataclasses.asdict(fill) for fill in fills], default=str, sort_keys=True
    ).encode()


# ── 1. the trading outcome does not change ───────────────────────────────────────────────────────

_LAST = date(2026, 12, 31)
_RISK_OFF = date(2026, 12, 1)
_DIVIDEND = CashDividend(isin=ISINS[4], ex_date=date(2026, 10, 20), per_share=Decimal("3.5"))
_SPLIT = ShareRescale(
    isin=ISINS[5],
    ex_date=date(2026, 11, 10),
    kind=RescaleKind.SPLIT,
    numerator=Decimal("2"),
    denominator=Decimal("1"),
)


def _four_month_world() -> FixtureWorld:
    return FixtureWorld(last=_LAST, risk_off={_RISK_OFF}, actions=[_DIVIDEND, _SPLIT])


def test_four_months_through_the_shared_path_trade_exactly_as_the_plain_replay() -> None:
    days = calendar_sessions(date(2026, 9, 28), _LAST)
    desk = _Desk(world=_four_month_world(), parameters=_TURNOVER)
    paper = [desk.run(day) for day in days]
    assert all(run.verdict is RunVerdict.DECIDED for run in paper)

    # Without the new path: the backtest's own walk — SimBroker under the accounting wrapper, no
    # staging coordinator, no reconciler, no kill switch — over the same world and days.
    world = _four_month_world()
    spec = fixture_spec(_TURNOVER)
    clock = FrozenClock(days[0])
    book = PortfolioBook()
    book.deposit(days[0], FIXTURE_CASH)
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state=_ACCOUNT_STATE),
        market=world.market(first=days[0], through=days[-1], held=_held_by(book)),
        opening_cash=FIXTURE_CASH,
    )
    plain = _AccountingBroker(sim, book, corporate_actions=BookActionCalendar(world.actions))
    replay = ReplayEngine(
        policy=MomentumV2Policy(
            world.momentum_data(days[0], _TURNOVER), _TURNOVER, order_caps=spec.rail_policy.rails
        ),
        broker=plain,
        clock=clock,
        sessions=days,
        rails=RailGate(spec.rail_policy, world.marks(_held_by(book))),
    ).run()

    # The fixture really exercised the path: sells and buys at each rebalance, the risk-off sale,
    # the dividend credited and the split applied on names the book held.
    sides = {fill.side for run in paper for fill in run.fills}
    assert sides == {Side.BUY, Side.SELL}
    assert any(e.decision is Decision.SELL and e.trading_date == _RISK_OFF for e in replay.journal)
    assert any(line.description.startswith("DIVIDEND") for line in sim.ledger())
    assert plain.corporate_actions_applied == {"DIVIDEND": 1, "SPLIT": 1}

    # Trades and costs: byte-identical fills, Decimal-identical total charges.
    paper_fills = [fill for run in paper for fill in run.fills]
    assert _fill_bytes(paper_fills) == _fill_bytes(plain.fills)
    paper_costs = sum((fill.cost.total for fill in paper_fills), Decimal(0))
    assert isinstance(paper_costs, Decimal) and paper_costs == plain.total_charges
    # Book and cash: the last persisted broker state is the walk's, the per-session ledgers
    # concatenate to its ledger, and the accounting book reconciled against it ends on its cash.
    final = paper[-1].record
    assert final is not None and final.book_state is not None and final.expected_book is not None
    assert final.book_state["broker"] == sim.export_state().to_document()
    assert final.book_state["broker"]["cash"] == str(sim.cash)
    assert [
        line
        for run in paper
        if run.record and run.record.book_state
        for line in run.record.book_state["session_ledger"]
    ] == [
        {
            "seq": str(line.seq),
            "session": line.session.isoformat(),
            "isin": line.isin,
            "description": line.description,
            "debit": str(line.debit),
            "credit": str(line.credit),
            "balance": str(line.balance),
        }
        for line in sim.ledger()
    ]
    assert Decimal(final.expected_book["cash"]) == book.cash == sim.margins().cash_value
    assert paper[-1].book is not None
    assert (paper[-1].book.cash, paper[-1].book.holdings, paper[-1].book.positions) == (
        replay.book.cash,
        replay.book.holdings,
        replay.book.positions,
    )

    # The journal: exactly one added entry per decided session — its reconciliation, last, a clean
    # HEARTBEAT naming what was staged and filled — and the rest entry for entry the replay's.
    for run in paper:
        assert run.record is not None and run.record.recon is not None
        (recon,) = _recon_entries(run.entries)
        assert run.entries[-1] is recon
        assert recon.decision is Decision.HEARTBEAT and recon.payload["recon"] == "CLEAN"
        assert recon.evidence_snapshot_ref in desk.journal.bundles
        staged = [i for i in recon.payload["staged"].split(",") if i]
        executed = [i for i in recon.payload["executed"].split(",") if i]
        assert len(staged) == len(run.record.orders)
        assert len(executed) == len(run.fills)
    decisions = [
        entry.model_copy(
            update={
                "payload": {
                    key: value
                    for key, value in entry.payload.items()
                    if key not in ("paper_book", "mode")
                }
            }
        )
        for run in paper
        for entry in run.entries
        if entry.payload.get("event") != RECON_EVENT
    ]
    replayed = [
        entry.model_copy(update={"case_id": None}) if entry.case_id == BACKTEST_CASE_ID else entry
        for entry in replay.journal
    ]
    assert decisions == replayed


def test_every_order_reaches_the_broker_through_the_staging_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged_by_coordinator: list[OrderRequest] = []
    executed_sessions: list[date] = []
    real_stage, real_execute = StagingCoordinator.stage, StagingCoordinator.execute

    def stage(self: StagingCoordinator, request: OrderRequest, **kwargs: Any) -> Any:
        staged_by_coordinator.append(request)
        return real_stage(self, request, **kwargs)

    def execute(self: StagingCoordinator, session: date) -> Any:
        executed_sessions.append(session)
        return real_execute(self, session)

    monkeypatch.setattr(StagingCoordinator, "stage", stage)
    monkeypatch.setattr(StagingCoordinator, "execute", execute)
    desk = _Desk()
    first = desk.run(OCT_FIRST)
    assert staged_by_coordinator == list(first.record.orders if first.record else ())
    assert first.record is not None and first.record.recon is not None and first.record.orders
    state = first.record.broker_state()
    # Every order the SimBroker holds staged was placed by the staging coordinator, under the
    # book's deterministic uid, and is named on the session's reconciliation.
    assert first.record.recon.staged == tuple(order.order_id for order in state.staged)
    assert [order.request for order in state.staged] == list(first.record.orders)
    second = desk.run(OCT_SECOND)
    assert second.record is not None and second.record.recon is not None
    assert second.record.recon.executed == first.record.recon.staged
    assert executed_sessions == [OCT_FIRST, OCT_SECOND]


# ── 2. the kill switch ───────────────────────────────────────────────────────────────────────────


def test_a_tripped_kill_switch_journals_a_no_op_and_stages_nothing() -> None:
    desk = _Desk()
    desk.kill_switch.trip(reason="drill: operator halt", source=TripSource.MANUAL)

    halted = desk.run(OCT_FIRST)

    assert halted.verdict is RunVerdict.HALTED
    (entry,) = halted.entries
    assert entry.decision is Decision.SKIPPED_DATA_RED
    assert entry.payload["event"] == KILL_SWITCH_EVENT
    assert "kill switch tripped by MANUAL" in str(entry.rationale)
    assert "drill: operator halt" in str(entry.rationale)
    assert halted.record is not None
    assert halted.record.outcome is SessionOutcome.SKIPPED_DATA_RED
    assert halted.record.orders == () and halted.record.book_state is None
    assert desk.world.reads == [], "a halted day reads no decision data"

    # A rerun while still tripped writes nothing more; once reset, the same date trades.
    assert desk.run(OCT_FIRST).verdict is RunVerdict.STILL_RED
    assert len(desk.journal.entries) == 1
    desk.kill_switch.reset(note="drill over")
    traded = desk.run(OCT_FIRST)
    assert traded.verdict is RunVerdict.DECIDED
    assert traded.record is not None and len(traded.record.orders) == len(ISINS)


def test_an_armed_kill_switch_lets_the_session_trade() -> None:
    desk = _Desk()
    assert not desk.kill_switch.is_tripped
    result = desk.run(OCT_FIRST)
    assert result.verdict is RunVerdict.DECIDED
    assert result.record is not None and len(result.record.orders) == len(ISINS)
    assert not desk.kill_switch.is_tripped


def test_the_staging_step_itself_refuses_on_a_tripped_switch() -> None:
    """The second line: even past the session's own check, no order reaches the broker."""
    switch = fresh_kill_switch()
    sim = SimBroker(
        clock=FrozenClock(OCT_FIRST),
        cost_model=CostModel(load_rate_card(), account_state=_ACCOUNT_STATE),
        market=FixtureWorld().market(first=OCT_FIRST, through=OCT_FIRST, held=list),
        opening_cash=FIXTURE_CASH,
    )
    broker = _PaperBroker(
        sim,
        PortfolioBook.seeded(FIXTURE_CASH, ()),
        clock=FrozenClock(OCT_FIRST),
        corporate_actions=BookActionCalendar(()),
        kill_switch=switch,
        alerter=RecordingAlerter(),
        book_id=BOOK,
        sleeve="TACTICAL",
    )
    request = OrderRequest(
        isin=ISINS[0],
        side=Side.BUY,
        quantity=10,
        exchange=Exchange.NSE,
        order_type=OrderType.MARKET,
    )
    switch.trip(reason="drill", source=TripSource.MANUAL)
    with pytest.raises(TradingHaltedError):
        broker.place(request)
    assert sim.export_state().staged == ()
    switch.reset(note="drill over")
    assert broker.place(request).request == request
    assert len(sim.export_state().staged) == 1


def test_the_paper_book_has_its_own_switch_under_the_lake(tmp_path: Path) -> None:
    switch = paper_kill_switch(tmp_path, BOOK)
    switch.trip(reason="x", source=TripSource.MANUAL)
    assert (tmp_path / "kill_switch" / f"{BOOK}.json").exists()
    assert paper_kill_switch(tmp_path, BOOK).is_tripped, "a fresh instance is the same switch"
    assert not paper_kill_switch(tmp_path, "another_book").is_tripped


def test_the_operator_cli_trips_reports_and_resets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "switch.json"
    assert kill_switch_module.main(["status", "--path", str(path)]) == 0
    assert kill_switch_module.main(["trip", "--path", str(path), "--reason", "manual halt"]) == 0
    assert kill_switch_module.main(["status", "--path", str(path)]) == 3
    assert json.loads(capsys.readouterr().out.split("\n}\n")[-2] + "\n}")["source"] == "MANUAL"
    with pytest.raises(ValueError, match="note"):
        kill_switch_module.main(["reset", "--path", str(path)])
    assert kill_switch_module.main(["reset", "--path", str(path), "--note", "owner: ok"]) == 0
    assert kill_switch_module.main(["status", "--path", str(path)]) == 0


# ── 3. reconciliation, and a break ───────────────────────────────────────────────────────────────


def _tamper_expected_cash(store: InMemoryPaperSessionStore, day: date, delta: Decimal) -> None:
    """Make the persisted accounting book disagree with the broker — what recon exists to catch.

    The ledger refuses to rewrite a decided row, so the test reaches into the in-memory rows: this
    is the stand-in for any divergence (a missed fill, a corporate action booked on one side only).
    """
    record = store.get(BOOK, day)
    assert record is not None and record.expected_book is not None
    expected: dict[str, Any] = dict(record.expected_book)
    expected["cash"] = str(Decimal(expected["cash"]) + delta)
    store._rows[(BOOK, day)] = dataclasses.replace(record, expected_book=expected)


def test_every_decided_session_is_reconciled_clean() -> None:
    desk = _Desk()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        result = desk.run(day)
        assert result.record is not None and result.record.recon is not None
        assert result.record.recon.status is ReconStatus.CLEAN
        assert result.record.recon.key == f"RECON:{day.isoformat()}"
        assert len(_recon_entries(result.entries)) == 1
    assert desk.alerter.alerts == []


def test_a_recon_break_is_red_trips_the_switch_and_blocks_until_resolved() -> None:
    desk = _Desk(parameters=_TURNOVER)
    for day in calendar_sessions(date(2026, 9, 28), SEPT_LAST):
        desk.run(day)
    _tamper_expected_cash(desk.store, SEPT_LAST, Decimal("1"))

    # October's first session is a rebalance that would trade; the break is caught after the
    # fills and before the policy is asked, so nothing is staged on the broken book.
    broken = desk.run(OCT_FIRST)

    assert broken.verdict is RunVerdict.RECON_BREAK
    record = broken.record
    assert record is not None and record.outcome is SessionOutcome.RECON_BREAK
    assert record.orders == () and not record.rebalanced
    assert record.book_state is not None, "the fills happened: the row carries the book they left"
    assert record.recon is not None and record.recon.status is ReconStatus.BREAK
    assert record.recon.breaks and "CASH cash" in record.recon.breaks[0]
    assert desk.kill_switch.is_tripped
    assert desk.kill_switch.state.source is TripSource.RECON
    assert len(desk.alerter.alerts) == 1
    escalation = broken.entries[-1]
    assert escalation.decision is Decision.ESCALATE
    assert escalation.payload["event"] == RECON_BREAK_EVENT
    assert escalation.payload["key"] == record.recon.key == f"RECON:{OCT_FIRST.isoformat()}"
    assert escalation.payload["terms"] == record.recon.terms
    assert not [e for e in broken.entries if e.decision in (Decision.BUY, Decision.SELL)]

    # The break date is final: a rerun neither re-fills nor re-journals.
    assert desk.run(OCT_FIRST).verdict is RunVerdict.ALREADY_DECIDED

    # Still tripped: the next session is a journaled no-op.
    halted = desk.run(OCT_SECOND)
    assert halted.verdict is RunVerdict.HALTED
    assert halted.entries[0].payload["event"] == KILL_SWITCH_EVENT

    # Reset without a resolution: still refused, now naming the break.
    desk.kill_switch.reset(note="owner reviewed the alert")
    blocked = desk.run(OCT_THIRD)
    assert blocked.verdict is RunVerdict.SKIPPED_DATA_RED
    (refusal,) = blocked.entries
    assert refusal.payload["event"] == RECON_BREAK_EVENT
    assert f"{record.recon.key}@{record.recon.terms}" in str(refusal.rationale)

    # Resolved: the book trades again, its accounting book re-based on the broker (and saying so),
    # and the rebalance the break pre-empted is made on this first green session.
    desk.store.resolve(BOOK, record.recon.key, record.recon.terms)
    resumed = desk.run(OCT_FOURTH)
    assert resumed.verdict is RunVerdict.DECIDED
    assert resumed.record is not None and resumed.record.rebalanced
    assert resumed.record.recon is not None and resumed.record.recon.seeded
    assert resumed.record.recon.status is ReconStatus.CLEAN
    assert resumed.record.orders


def test_a_skipped_reconciliation_would_let_a_break_through() -> None:
    """The same tampering one session later: without recon, the session would decide; it may not."""
    desk = _Desk()
    for day in (OCT_FIRST, OCT_SECOND):
        desk.run(day)
    _tamper_expected_cash(desk.store, OCT_SECOND, Decimal("-0.01"))
    result = desk.run(OCT_THIRD)
    assert result.verdict is RunVerdict.RECON_BREAK
    assert result.record is not None and result.record.recon is not None
    (only,) = result.record.recon.breaks
    assert only.startswith("CASH cash:") and only.endswith("(diff 0.01)")


# ── 4. idempotency and crash-safety ──────────────────────────────────────────────────────────────


def test_a_rerun_of_a_decided_day_stages_nothing_twice() -> None:
    desk = _Desk()
    first = desk.run(OCT_FIRST)
    entries = list(desk.journal.entries)
    again = desk.run(OCT_FIRST)
    assert again.verdict is RunVerdict.ALREADY_DECIDED
    assert desk.journal.entries == entries
    assert again.record == first.record
    record = desk.store.get(BOOK, OCT_FIRST)
    assert record is not None and len(record.broker_state().staged) == len(ISINS)


class _CrashingStore(InMemoryPaperSessionStore):
    """Dies writing one date's row — the process killed between the trip and the commit."""

    def __init__(self, crash_on: date) -> None:
        super().__init__()
        self.crash_on = crash_on

    def record(self, record: PaperSessionRecord, *, recorded_at: datetime) -> None:
        if record.trading_date == self.crash_on:
            raise RuntimeError("killed mid-commit")
        super().record(record, recorded_at=recorded_at)


def test_a_crash_after_a_break_tripped_the_switch_leaves_the_book_halted() -> None:
    desk = _Desk(store=_CrashingStore(crash_on=OCT_SECOND))
    desk.run(OCT_FIRST)
    _tamper_expected_cash(desk.store, OCT_FIRST, Decimal("1"))
    with pytest.raises(RuntimeError, match="killed mid-commit"):
        desk.run(OCT_SECOND)
    # Nothing of the session was recorded, but the switch the break tripped is on disk: the rerun
    # refuses rather than deciding the date on a book recon said was wrong.
    assert desk.store.get(BOOK, OCT_SECOND) is None
    assert desk.kill_switch.is_tripped
    desk.store.crash_on = date.max  # type: ignore[attr-defined]
    rerun = desk.run(OCT_SECOND)
    assert rerun.verdict is RunVerdict.HALTED
    assert rerun.record is not None and rerun.record.orders == ()


# ── 5. review round: the mid-session halt, a late split, break-day rebalance, the switch file ───


def test_a_switch_tripped_mid_session_by_anything_but_recon_raises_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recon is clean, then something else trips the switch before the first order is staged.

    The staging step refuses the order; since the session's own reconciliation found nothing, the
    refusal is not a break the session can record — it is surfaced, and the caller's transaction
    rolls back. Nothing is recorded or journaled for the date.
    """
    real = Reconciler.reconcile

    def reconcile_then_trip(self: Reconciler, session: date) -> Any:
        result = real(self, session)
        self.kill_switch.trip(reason="tripped by another process mid-run", source=TripSource.MANUAL)
        return result

    monkeypatch.setattr(Reconciler, "reconcile", reconcile_then_trip)
    desk = _Desk()
    with pytest.raises(TradingHaltedError, match="another process"):
        desk.run(OCT_FIRST)  # the first session rebalances: it would stage ten orders
    assert desk.store.get(BOOK, OCT_FIRST) is None
    assert desk.journal.entries == []
    # The switch stays tripped: the rerun is the journaled no-op, never a decision.
    monkeypatch.setattr(Reconciler, "reconcile", real)
    assert desk.run(OCT_FIRST).verdict is RunVerdict.HALTED


def test_a_late_split_on_a_held_name_is_booked_on_both_sides_and_recon_stays_clean() -> None:
    desk = _Desk()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        desk.run(day)
    third = desk.store.get(BOOK, OCT_THIRD)
    assert third is not None and third.expected_book is not None
    isin = str(third.broker_state().holdings[0][0])
    held = dict(desk.run_quantities(third))[isin]
    # Ex-date Tuesday 6 Oct, already decided, untraded since: learnt on Wednesday, booked then.
    desk.world.actions.append(
        ShareRescale(
            isin=isin,
            ex_date=OCT_THIRD,
            kind=RescaleKind.SPLIT,
            numerator=Decimal("2"),
            denominator=Decimal("1"),
        )
    )
    fourth = desk.run(OCT_FOURTH)

    assert fourth.verdict is RunVerdict.DECIDED
    late = [e for e in fourth.entries if e.payload.get("event") == LATE_ACTION_EVENT]
    assert [e.decision for e in late] == [Decision.HOLD]
    assert fourth.record is not None and fourth.record.recon is not None
    assert fourth.record.recon.status is ReconStatus.CLEAN
    assert dict(desk.run_quantities(fourth.record))[isin] == 2 * held
    assert fourth.record.expected_book is not None
    (booked,) = [p for p in fourth.record.expected_book["positions"] if p["isin"] == isin]
    assert int(booked["quantity"]) == 2 * held


def test_a_break_day_never_counts_as_the_months_rebalance_even_with_no_orders_wanted() -> None:
    """The ratified book already holds every fixture name, so October's rebalance wants no order.

    The engine finishes (nothing was refused), yet the break day is still not the rebalance and
    does not advance the redeploy state — exactly as when the staging step refused the orders —
    and the first green session after the resolution makes the rebalance.
    """
    desk = _Desk()
    for day in calendar_sessions(date(2026, 9, 28), SEPT_LAST):
        desk.run(day)
    september = desk.store.get(BOOK, SEPT_LAST)
    assert september is not None
    _tamper_expected_cash(desk.store, SEPT_LAST, Decimal("1"))

    broken = desk.run(OCT_FIRST)

    assert broken.verdict is RunVerdict.RECON_BREAK
    decided = [
        e for e in broken.entries if e.payload.get("event") not in (RECON_EVENT, RECON_BREAK_EVENT)
    ]
    assert decided, "the engine finished: this is the policy-wanted-nothing branch"
    assert not [e for e in decided if e.decision in (Decision.BUY, Decision.SELL)]
    assert broken.record is not None
    assert not broken.record.rebalanced
    assert broken.record.pending == september.pending
    desk.kill_switch.reset(note="owner reviewed")
    assert broken.record.recon is not None
    desk.store.resolve(BOOK, broken.record.recon.key, broken.record.recon.terms)
    resumed = desk.run(OCT_SECOND)
    assert resumed.verdict is RunVerdict.DECIDED
    assert resumed.record is not None and resumed.record.rebalanced


def test_a_reset_leaves_a_durable_trace_in_the_state_file(tmp_path: Path) -> None:
    path = tmp_path / "switch.json"
    tripped_at = datetime(2026, 10, 8, 9, 0, tzinfo=IST)
    switch = KillSwitch(path, clock=FrozenClock(tripped_at))
    switch.trip(reason="drill halt", source=TripSource.MANUAL)
    reset_at = datetime(2026, 10, 8, 9, 30, tzinfo=IST)
    KillSwitch(path, clock=FrozenClock(reset_at)).reset(note="drill over", by="owner")

    reread = KillSwitch(path).state  # a fresh process reads the trace back
    assert not reread.tripped and reread.last_reset is not None
    trace = reread.last_reset
    assert (trace.at, trace.note, trace.by) == (reset_at, "drill over", "owner")
    assert (trace.cleared_reason, trace.cleared_source, trace.cleared_tripped_at) == (
        "drill halt",
        TripSource.MANUAL,
        tripped_at,
    )
    # A later trip keeps the trace; a no-op reset of an armed switch never overwrites it.
    KillSwitch(path).trip(reason="again", source=TripSource.RECON)
    assert KillSwitch(path).state.last_reset == trace
    assert not list(tmp_path.glob(".killswitch-*")), "written atomically, no temp file left"


def test_the_cli_reset_records_who_reset_it(tmp_path: Path) -> None:
    path = tmp_path / "switch.json"
    kill_switch_module.main(["trip", "--path", str(path), "--reason", "manual"])
    kill_switch_module.main(["reset", "--path", str(path), "--note", "ok", "--by", "ops"])
    trace = KillSwitch(path).state.last_reset
    assert trace is not None and trace.by == "ops" and trace.cleared_reason == "manual"


def test_a_missing_file_is_armed_but_a_file_without_tripped_fails_closed(tmp_path: Path) -> None:
    assert not KillSwitch(tmp_path / "never_written.json").is_tripped
    for broken in (
        {"version": 1},
        {"version": 1, "tripped": None},
        {"version": 1, "tripped": "no"},
    ):
        path = tmp_path / "switch.json"
        path.write_text(json.dumps(broken))
        with pytest.raises(ValueError, match="tripped"):
            KillSwitch(path)
    # A state file written before `last_reset` existed still reads, as never reset.
    path.write_text(json.dumps({"version": 1, "tripped": True, "reason": "x", "source": "MANUAL"}))
    assert KillSwitch(path).is_tripped and KillSwitch(path).state.last_reset is None
