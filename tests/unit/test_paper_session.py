"""M13.1 — the daily paper session: deterministic, idempotent, red-safe, journaled, paper-only.

Every test drives ``backtest.paper_session.run_paper_session`` over the frozen fixture fortnight in
``tests/paper_session_support.py`` with the in-memory ledger and journal, one call per trading day,
exactly as the scheduler would — so the paper book's only memory between days is what the ledger
recorded. The acceptance criteria, one block each:

* a session over a fixture day produces the expected journal and book, and the same days replayed
  from scratch produce byte-identical journal and book bytes;
* the paper book *is* the replay: a run of days one process at a time equals one ``ReplayEngine``
  walk over the same days (same entries, same book), including the redeploy state carried across;
* red data journals ``SKIPPED_DATA_RED``, places nothing, and is journaled once however often the
  job is rerun; a failed status read is red; a missing decision input is red;
* a rerun on a decided date is a no-op; a holiday is a no-op;
* a book that no longer rebuilds to its recorded digest fails loud.

The KiteBroker-unreachability tests are in ``test_paper_session_paper_only.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

import pytest

from analyst.journal.models import Decision, JournalEntry
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar
from backtest.paper_session import (
    PAPER_MODE,
    InMemoryPaperSessionStore,
    PaperBookDivergenceError,
    PaperSessionRecord,
    PaperSessionResult,
    RecordingJournal,
    RunVerdict,
    SessionOutcome,
    run_paper_session,
)
from backtest.policies.momentum_v2 import MomentumV2Parameters, MomentumV2Policy
from backtest.rails import RailGate
from backtest.replay import ReplayEngine
from backtest.run import _ACCOUNT_STATE, _AccountingBroker, _held_by
from dataplatform.clock import IST, FrozenClock
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import SimBroker
from tests.paper_session_support import (
    FIXTURE_CASH,
    HOLIDAY,
    ISINS,
    OCT_FIRST,
    OCT_FOURTH,
    OCT_SECOND,
    OCT_THIRD,
    FixtureWorld,
    calendar_sessions,
    fixture_spec,
)

RUN_AT = datetime(2026, 10, 1, 20, 30, tzinfo=IST)


@dataclass(frozen=True, slots=True)
class _Verdict:
    green: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.green


def _green(_: date) -> _Verdict:
    return _Verdict(True)


def _red_on(*days: date) -> object:
    def gate(day: date) -> _Verdict:
        if day in days:
            return _Verdict(False, f"nse_bhavcopy not PUBLISHED for {day.isoformat()}")
        return _Verdict(True)

    return gate


@dataclass
class _Desk:
    """One paper book's durable state — the ledger and the journal — and the world it reads."""

    world: FixtureWorld
    store: InMemoryPaperSessionStore
    journal: RecordingJournal
    spec: object

    @classmethod
    def fresh(cls, world: FixtureWorld | None = None, spec: object | None = None) -> _Desk:
        return cls(
            world=world or FixtureWorld(),
            store=InMemoryPaperSessionStore(),
            journal=RecordingJournal(),
            spec=spec or fixture_spec(),
        )

    def run(self, day: date, gate: object = _green) -> PaperSessionResult:
        return run_paper_session(
            trading_date=day,
            spec=self.spec,  # type: ignore[arg-type]
            world=self.world,
            store=self.store,
            journal=self.journal,
            gate=gate,  # type: ignore[arg-type]
            clock=FrozenClock(RUN_AT),
        )


def _decisions(entries: tuple[JournalEntry, ...] | list[JournalEntry]) -> list[Decision]:
    return [entry.decision for entry in entries]


# ── the expected journal and book, deterministically ─────────────────────────────────────────────


def test_the_first_session_rebalances_into_the_ratified_basket_through_the_rails() -> None:
    desk = _Desk.fresh()
    result = desk.run(OCT_FIRST)

    assert result.verdict is RunVerdict.DECIDED
    assert result.record is not None and result.record.rebalanced
    # Ten candidates under top-20: the whole fixture universe is bought, one BUY per name.
    assert _decisions(result.entries) == [Decision.BUY] * len(ISINS)
    assert sorted(str(entry.isin) for entry in result.entries) == sorted(ISINS)
    assert len(result.record.orders) == len(ISINS)
    for entry in result.entries:
        assert entry.trading_date == OCT_FIRST
        assert entry.payload["mode"] == PAPER_MODE
        assert entry.payload["paper_book"] == "paper_fixture_book"
        assert entry.evidence_snapshot_ref in desk.journal.bundles
    # A8 cleared each buy under the ratified per-order cap (₹1.2 L) and position cap (15 %).
    for order in result.record.orders:
        price = desk.world.closes(OCT_FIRST)[order.isin]
        assert Decimal(order.quantity) * price <= Decimal("120000")
    # Orders are staged for the next session, not filled at today's prices: no cash spent yet.
    assert result.book is not None
    assert result.book.cash == FIXTURE_CASH
    assert result.book.holdings == () and result.book.positions == ()


def test_the_next_session_fills_the_staged_orders_and_writes_a_heartbeat() -> None:
    desk = _Desk.fresh()
    desk.run(OCT_FIRST)
    second = desk.run(OCT_SECOND)  # Friday 2 Oct is a holiday: the next session is Monday

    assert second.verdict is RunVerdict.DECIDED
    assert second.record is not None and not second.record.rebalanced
    assert _decisions(second.entries) == [Decision.HEARTBEAT]
    assert second.record.orders == ()
    assert second.book is not None
    # Filled on Monday's bars at T+1: ten unsettled positions, cash spent with costs.
    assert len(second.book.positions) == len(ISINS)
    assert second.book.cash < FIXTURE_CASH * Decimal("0.05")

    third = desk.run(OCT_THIRD)
    assert third.book is not None
    assert len(third.book.holdings) == len(ISINS)  # settled
    assert third.book.positions == ()


def test_the_same_days_from_scratch_are_byte_identical() -> None:
    days = [OCT_FIRST, OCT_SECOND, OCT_THIRD, OCT_FOURTH]
    first, second = _Desk.fresh(), _Desk.fresh()
    runs_a = [first.run(day) for day in days]
    runs_b = [second.run(day) for day in days]

    for a, b in zip(runs_a, runs_b, strict=True):
        assert a.journal_bytes() == b.journal_bytes()
        assert a.book_bytes() == b.book_bytes()
        assert a.record == b.record
    assert first.journal.entries == second.journal.entries
    assert first.journal.bundles == second.journal.bundles


# ── the paper book is the replay, one process per day ────────────────────────────────────────────

#: A book the October ranking reversal turns over: top-9 with no wider band, so the September
#: leader drops out and is sold, leaving eight names — the ratified holdings floor — under a 15 %
#: position cap that the redeploy's buys still fit.
_TURNOVER = MomentumV2Parameters(
    top_n=9,
    use_12_1=True,
    sell_band=9,
    regime_filter=True,
    vol_scaled=True,
    redeploy_next_session=True,
)


def test_a_book_decided_a_day_at_a_time_equals_one_replay_over_the_same_days() -> None:
    """The decision path is the backtest's: same entries, same book, redeploy state carried."""
    days = calendar_sessions(date(2026, 9, 28), date(2026, 10, 9))
    spec = fixture_spec(_TURNOVER)
    desk = _Desk.fresh(spec=spec)
    paper = [desk.run(day) for day in days]
    assert all(run.verdict is RunVerdict.DECIDED for run in paper)

    world = FixtureWorld()
    clock = FrozenClock(days[0])
    book = PortfolioBook()
    book.deposit(days[0], FIXTURE_CASH)
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state=_ACCOUNT_STATE),
        market=world.market(first=days[0], through=days[-1], held=_held_by(book)),
        opening_cash=FIXTURE_CASH,
    )
    broker = _AccountingBroker(sim, book, corporate_actions=BookActionCalendar(()))
    replay = ReplayEngine(
        policy=MomentumV2Policy(
            world.momentum_data(days[0], _TURNOVER), _TURNOVER, order_caps=spec.rail_policy.rails
        ),
        broker=broker,
        clock=clock,
        sessions=days,
        rails=RailGate(spec.rail_policy, world.marks(_held_by(book))),
    ).run()

    untagged = [
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
    ]
    assert untagged == list(replay.journal)
    assert paper[-1].book_bytes() == replay.book.canonical_bytes()
    # The fixture really exercised the carried state: October's rebalance sold, the next session
    # redeployed the proceeds, and that redeploy happened in a different process from the sells.
    october = {run.trading_date: run for run in paper}
    assert Decision.SELL in _decisions(october[OCT_FIRST].entries)
    rebalance = october[OCT_FIRST].record
    assert rebalance is not None and rebalance.pending is not None
    assert Decision.BUY in _decisions(october[OCT_SECOND].entries)


# ── red data ─────────────────────────────────────────────────────────────────────────────────────


def test_red_data_journals_a_skip_and_places_nothing() -> None:
    desk = _Desk.fresh()
    result = desk.run(OCT_FIRST, gate=_red_on(OCT_FIRST))

    assert result.verdict is RunVerdict.SKIPPED_DATA_RED
    assert _decisions(result.entries) == [Decision.SKIPPED_DATA_RED]
    (entry,) = desk.journal.entries
    assert entry.decision is Decision.SKIPPED_DATA_RED
    assert entry.rationale is not None and "nse_bhavcopy" in entry.rationale
    assert entry.payload["mode"] == PAPER_MODE and entry.payload["datasets"] == "nse_bhavcopy"
    record = desk.store.get("paper_fixture_book", OCT_FIRST)
    assert record is not None and record.outcome is SessionOutcome.SKIPPED_DATA_RED
    assert record.orders == () and record.book_digest is None
    # Red means the data was not even read (invariant #10): no signal, no regime.
    assert desk.world.reads == []


def test_a_red_rerun_is_not_journaled_twice() -> None:
    desk = _Desk.fresh()
    desk.run(OCT_FIRST, gate=_red_on(OCT_FIRST))
    again = desk.run(OCT_FIRST, gate=_red_on(OCT_FIRST))

    assert again.verdict is RunVerdict.STILL_RED
    assert again.entries == ()
    assert _decisions(desk.journal.entries) == [Decision.SKIPPED_DATA_RED]


def test_a_failed_status_read_is_red_and_never_quotes_the_error() -> None:
    def broken(_: date) -> _Verdict:
        raise ConnectionError("could not connect to postgresql://user:hunter2@db/trading")

    desk = _Desk.fresh()
    result = desk.run(OCT_FIRST, gate=broken)

    assert result.verdict is RunVerdict.SKIPPED_DATA_RED
    assert "ConnectionError" in result.reason
    assert "hunter2" not in result.reason
    assert desk.journal.entries[0].rationale is not None
    assert "hunter2" not in desk.journal.entries[0].rationale


def test_status_green_but_no_prices_in_l1_is_red() -> None:
    desk = _Desk.fresh(world=FixtureWorld(unpriced={OCT_FIRST}))
    result = desk.run(OCT_FIRST)
    assert result.verdict is RunVerdict.SKIPPED_DATA_RED
    assert "no NSE prices" in result.reason


def test_a_rebalance_without_the_regime_index_level_is_red_not_a_crash() -> None:
    desk = _Desk.fresh(world=FixtureWorld(no_regime={OCT_FIRST}))
    result = desk.run(OCT_FIRST)

    assert result.verdict is RunVerdict.SKIPPED_DATA_RED
    assert "decision input unavailable" in result.reason
    assert desk.store.get("paper_fixture_book", OCT_FIRST) is not None
    assert all(entry.decision is Decision.SKIPPED_DATA_RED for entry in desk.journal.entries)


def test_a_red_first_session_moves_the_rebalance_to_the_next_green_one() -> None:
    desk = _Desk.fresh()
    desk.run(OCT_FIRST, gate=_red_on(OCT_FIRST))
    second = desk.run(OCT_SECOND)

    assert second.verdict is RunVerdict.DECIDED
    assert second.record is not None and second.record.rebalanced
    assert _decisions(second.entries) == [Decision.BUY] * len(ISINS)
    # Once the month has rebalanced, the next session is a heartbeat.
    third = desk.run(OCT_THIRD)
    assert _decisions(third.entries) == [Decision.HEARTBEAT]


def test_a_red_date_that_turns_green_is_decided_on_rerun() -> None:
    desk = _Desk.fresh()
    desk.run(OCT_FIRST, gate=_red_on(OCT_FIRST))
    healed = desk.run(OCT_FIRST)

    assert healed.verdict is RunVerdict.DECIDED
    record = desk.store.get("paper_fixture_book", OCT_FIRST)
    assert record is not None and record.outcome is SessionOutcome.COMPLETED
    # The journal is append-only: the skip stays, the decision follows it.
    assert _decisions(desk.journal.entries) == [Decision.SKIPPED_DATA_RED] + [Decision.BUY] * len(
        ISINS
    )


def test_orders_staged_for_a_red_session_lapse_unfilled() -> None:
    desk = _Desk.fresh()
    desk.run(OCT_FIRST)  # stages ten buys for Monday
    desk.run(OCT_SECOND, gate=_red_on(OCT_SECOND))  # Monday is red: not decided
    third = desk.run(OCT_THIRD)

    assert third.book is not None
    assert third.book.holdings == () and third.book.positions == ()
    assert third.book.cash == FIXTURE_CASH


# ── idempotency ──────────────────────────────────────────────────────────────────────────────────


def test_a_rerun_on_a_decided_date_is_a_no_op() -> None:
    desk = _Desk.fresh()
    first = desk.run(OCT_FIRST)
    journaled = list(desk.journal.entries)
    again = desk.run(OCT_FIRST)

    assert again.verdict is RunVerdict.ALREADY_DECIDED
    assert again.entries == ()
    assert desk.journal.entries == journaled
    assert desk.store.get("paper_fixture_book", OCT_FIRST) == first.record
    # And the next day sees one set of orders, not two.
    second = desk.run(OCT_SECOND)
    assert second.book is not None and len(second.book.positions) == len(ISINS)


def test_a_holiday_is_not_a_session_and_writes_nothing() -> None:
    desk = _Desk.fresh()
    result = desk.run(HOLIDAY)

    assert result.verdict is RunVerdict.NOT_A_SESSION
    assert desk.journal.entries == []
    assert desk.store.get("paper_fixture_book", HOLIDAY) is None


# ── the rebuilt book must reproduce the recorded one ─────────────────────────────────────────────


def test_a_book_that_no_longer_rebuilds_to_its_recorded_digest_fails_loud() -> None:
    desk = _Desk.fresh()
    desk.run(OCT_FIRST)
    desk.run(OCT_SECOND)
    recorded = desk.store.get("paper_fixture_book", OCT_SECOND)
    assert recorded is not None
    tampered = InMemoryPaperSessionStore()
    for record in desk.store.history("paper_fixture_book", before=OCT_THIRD):
        if record.trading_date == OCT_SECOND:
            record = replace(record, book_digest="0" * 64)
        tampered.record(record, recorded_at=RUN_AT)
    desk.store = tampered

    with pytest.raises(PaperBookDivergenceError, match=OCT_SECOND.isoformat()):
        desk.run(OCT_THIRD)


def test_a_decided_session_is_never_rewritten() -> None:
    desk = _Desk.fresh()
    result = desk.run(OCT_FIRST)
    assert result.record is not None
    red = PaperSessionRecord(
        book_id="paper_fixture_book",
        trading_date=OCT_FIRST,
        outcome=SessionOutcome.SKIPPED_DATA_RED,
        reason="late red",
        rebalanced=False,
        journal_digest="x",
    )
    with pytest.raises(Exception, match="never rewritten"):
        desk.store.record(red, recorded_at=RUN_AT)


def test_a_record_round_trips_through_its_documents() -> None:
    desk = _Desk.fresh(spec=fixture_spec(_TURNOVER))
    for day in calendar_sessions(date(2026, 9, 28), OCT_FIRST):
        desk.run(day)
    record = desk.store.get("paper_fixture_book", OCT_FIRST)
    assert record is not None and record.pending is not None and record.orders

    restored = PaperSessionRecord.from_documents(
        book_id=record.book_id,
        trading_date=record.trading_date,
        outcome=record.outcome.value,
        reason=record.reason,
        rebalanced=record.rebalanced,
        journal_digest=record.journal_digest,
        orders=record.orders_document(),
        pending=record.pending_document(),
        book_digest=record.book_digest,
    )
    assert restored == record
