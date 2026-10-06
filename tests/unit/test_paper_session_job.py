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
from uuid import uuid4

import pytest

from analyst.journal.models import Decision, JournalEntry
from backtest.accounting import PortfolioBook
from backtest.book_actions import (
    BookActionCalendar,
    CashDividend,
    RescaleKind,
    RescaleSource,
    ShareRescale,
)
from backtest.paper_session import (
    AMBIGUOUS_ACTION_EVENT,
    CHANGED_ACTION_EVENT,
    LATE_ACTION_EVENT,
    PAPER_BOOK_ID,
    PAPER_MODE,
    REGIME_TRI_INPUT,
    ActionStatus,
    InMemoryPaperSessionStore,
    PaperBookDivergenceError,
    PaperSessionError,
    PaperSessionRecord,
    PaperSessionResult,
    RecordingJournal,
    RunVerdict,
    SessionOutcome,
    action_identity,
    action_terms,
    owed_session,
    run_paper_session,
    run_paper_session_job,
)
from backtest.policies.momentum_v2 import MomentumV2Parameters, MomentumV2Policy
from backtest.rails import BACKTEST_CASE_ID, RailGate
from backtest.replay import ReplayEngine
from backtest.run import _ACCOUNT_STATE, _AccountingBroker, _held_by
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.scheduler import JobContext
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
    install_job_seams,
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
    # The one deliberate difference: the paper journal files a caseless rail block under no case
    # rather than the backtest's placeholder, which the live journal's foreign key refuses.
    replayed = [
        entry.model_copy(update={"case_id": None}) if entry.case_id == BACKTEST_CASE_ID else entry
        for entry in replay.journal
    ]
    assert untagged == replayed
    # The book rolled forward one process per day ends where the single walk ends: the persisted
    # broker state equals the replay broker's own, and the per-session ledgers concatenate to its
    # ledger line for line.
    final = paper[-1].record
    assert final is not None and final.book_state is not None
    assert final.book_state["broker"] == sim.export_state().to_document()
    session_lines = [
        line
        for run in paper
        if run.record is not None and run.record.book_state is not None
        for line in run.record.book_state["session_ledger"]
    ]
    assert session_lines == [
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
    # The fixture really exercised the carried state: October's rebalance sold, the next session
    # redeployed the proceeds, and that redeploy happened in a different process from the sells.
    october = {run.trading_date: run for run in paper}
    assert Decision.SELL in _decisions(october[OCT_FIRST].entries)
    rebalance = october[OCT_FIRST].record
    assert rebalance is not None and rebalance.pending is not None
    assert Decision.BUY in _decisions(october[OCT_SECOND].entries)


def test_a_rail_block_is_journaled_under_no_case_never_the_backtest_placeholder() -> None:
    """A8 refusing a paper buy lands as RAIL_BLOCK — and must fit the live journal's case FK."""
    tight = fixture_spec()
    tight = replace(
        tight,
        rail_policy=replace(
            tight.rail_policy,
            rails=tight.rail_policy.rails.model_copy(update={"max_sector_pct": Decimal("20")}),
        ),
    )
    desk = _Desk.fresh(spec=tight)
    result = desk.run(OCT_FIRST)

    blocks = [entry for entry in result.entries if entry.decision is Decision.RAIL_BLOCK]
    assert blocks, "the 20 % sector cap must refuse some of the fixture's buys"
    assert all(entry.case_id is None for entry in result.entries)
    assert all(entry.payload["paper_book"] == "paper_fixture_book" for entry in blocks)
    assert result.record is not None
    assert len(result.record.orders) == len(ISINS) - len(blocks), "refused orders never placed"


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
    assert desk.store.get("paper_fixture_book", OCT_FIRST) is not None
    assert all(entry.decision is Decision.SKIPPED_DATA_RED for entry in desk.journal.entries)


def test_a_missing_tri_level_is_named_in_the_journaled_red_reason() -> None:
    """The red day says which input is missing — the session's NIFTY 50 TRI, from tri_evening."""
    desk = _Desk.fresh(world=FixtureWorld(no_regime={OCT_FIRST}))
    result = desk.run(OCT_FIRST)

    (entry,) = desk.journal.entries
    assert entry.rationale is not None
    assert entry.rationale.startswith(f"regime input unavailable ({REGIME_TRI_INPUT}")
    assert "for 2026-10-01" in entry.rationale
    assert "has no level for 2026-10-01" in entry.rationale  # the source's own words, quoted
    assert REGIME_TRI_INPUT in entry.rationale
    assert entry.payload["missing_input"] == REGIME_TRI_INPUT == "nifty_tri_history/nifty50"
    assert result.reason == entry.rationale
    # Never read past the gap: the regime was asked for, the ranking was not.
    assert ("regime", OCT_FIRST) in desk.world.reads
    assert ("signal", OCT_FIRST) not in desk.world.reads


def test_a_missing_tri_level_never_reds_a_session_that_is_not_a_rebalance() -> None:
    """Only a rebalance reads the regime, so a late TRI on an ordinary day changes nothing."""
    desk = _Desk.fresh(world=FixtureWorld(no_regime={OCT_SECOND, OCT_THIRD}))
    assert desk.run(OCT_FIRST).verdict is RunVerdict.DECIDED  # the month's rebalance
    for day in (OCT_SECOND, OCT_THIRD):
        result = desk.run(day)
        assert result.verdict is RunVerdict.DECIDED
        assert "missing_input" not in {key for e in result.entries for key in e.payload}


def test_other_red_reasons_carry_no_missing_input() -> None:
    desk = _Desk.fresh()
    desk.run(OCT_FIRST, gate=_red_on(OCT_FIRST))
    (entry,) = desk.journal.entries
    assert "missing_input" not in entry.payload


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


# ── the restored book must reproduce its recorded state ──────────────────────────────────────────


def test_a_persisted_book_that_no_longer_matches_its_digest_fails_loud() -> None:
    desk = _Desk.fresh()
    desk.run(OCT_FIRST)
    desk.run(OCT_SECOND)
    tampered = InMemoryPaperSessionStore()
    for record in desk.store.records("paper_fixture_book"):
        if record.trading_date == OCT_SECOND and record.book_state is not None:
            broker = {**record.book_state["broker"], "cash": "99999999"}
            record = replace(record, book_state={**record.book_state, "broker": broker})
        tampered.record(record, recorded_at=RUN_AT)
    desk.store = tampered

    with pytest.raises(PaperBookDivergenceError, match=OCT_SECOND.isoformat()):
        desk.run(OCT_THIRD)


def test_a_run_restores_the_last_snapshot_and_reads_no_older_session() -> None:
    """Rolls forward from the latest snapshot: the restore never walks the book's history."""
    desk = _Desk.fresh()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        desk.run(day)
    reads: list[date] = []
    world = desk.world
    original = world.sessions

    def counting(start: date, end: date) -> list[date]:
        reads.append(start)
        return list(original(start, end))

    world.sessions = counting  # type: ignore[method-assign]
    assert desk.run(OCT_FOURTH).verdict is RunVerdict.DECIDED
    assert reads == [], "a run must not walk the calendar from inception"


# ── corporate actions: booked on the first session they are known ───────────────────────────────


def _held_isin(desk: _Desk) -> str:
    record = desk.store.get("paper_fixture_book", OCT_THIRD)
    assert record is not None and record.book_state is not None
    return str(record.book_state["broker"]["holdings"][0]["isin"])


def test_a_dividend_known_on_time_is_credited_on_its_ex_date_with_no_late_entry() -> None:
    desk = _Desk.fresh()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        desk.run(day)
    isin = _held_isin(desk)
    desk.world.actions.append(CashDividend(isin=isin, ex_date=OCT_FOURTH, per_share=Decimal("5")))
    fourth = desk.run(OCT_FOURTH)

    assert fourth.verdict is RunVerdict.DECIDED
    assert not [e for e in fourth.entries if e.payload.get("event") == LATE_ACTION_EVENT]
    assert fourth.record is not None and fourth.record.book_state is not None
    assert len(fourth.record.actions) == 1
    # Credited the ordinary way, before the session's fills, on the ex-date.
    (credit,) = [
        line
        for line in fourth.record.book_state["session_ledger"]
        if line["isin"] == isin and line["description"].startswith("DIVIDEND")
    ]
    assert Decimal(credit["credit"]) > 0


def test_a_dividend_learnt_after_its_ex_date_was_decided_is_booked_late_and_the_book_goes_on() -> (
    None
):
    """B1: the corporate-action store learns a dividend days after its ex-date. Before the fix the
    next run re-walked history with the new action, failed its recorded digest and raised
    ``PaperBookDivergenceError`` on every run from then on; now it is booked on the session it
    became known, journaled as a late action, and the book keeps deciding."""
    desk = _Desk.fresh()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        desk.run(day)
    isin = _held_isin(desk)
    third = desk.store.get("paper_fixture_book", OCT_THIRD)
    assert third is not None and third.book_state is not None
    cash_before = Decimal(third.book_state["broker"]["cash"])
    # Entitlement is the book entering the ex-date: what Monday's decided session left it holding.
    monday = desk.store.get("paper_fixture_book", OCT_SECOND)
    assert monday is not None and monday.book_state is not None
    held = sum(
        int(lot["quantity"])
        for key in ("holdings", "pending")
        for lot in monday.book_state["broker"][key]
        if lot["isin"] == isin
    )
    assert held > 0
    # Ex-date Tuesday 6 Oct, already decided; the store only learns of it on Wednesday.
    desk.world.actions.append(CashDividend(isin=isin, ex_date=OCT_THIRD, per_share=Decimal("7")))
    fourth = desk.run(OCT_FOURTH)

    assert fourth.verdict is RunVerdict.DECIDED
    late = [e for e in fourth.entries if e.payload.get("event") == LATE_ACTION_EVENT]
    assert len(late) == 1
    (entry,) = late
    assert entry.decision is Decision.HOLD and entry.isin == isin
    assert entry.payload["entitled"] == str(held)
    assert Decimal(entry.payload["amount"]) == Decimal("7") * held
    assert fourth.record is not None and fourth.record.book_state is not None
    assert Decimal(fourth.record.book_state["broker"]["cash"]) == cash_before + Decimal("7") * held
    # Booked once: the next session neither re-books it nor fails.
    fifth = desk.run(date(2026, 10, 8))
    assert fifth.verdict is RunVerdict.DECIDED
    assert not [e for e in fifth.entries if e.payload.get("event") == LATE_ACTION_EVENT]


def test_a_late_action_on_a_name_the_book_never_held_is_recorded_silently() -> None:
    desk = _Desk.fresh()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        desk.run(day)
    desk.world.actions.append(
        CashDividend(isin="INE999Z01019", ex_date=OCT_THIRD, per_share=Decimal("1"))
    )
    fourth = desk.run(OCT_FOURTH)
    assert fourth.verdict is RunVerdict.DECIDED
    assert not [e for e in fourth.entries if e.payload.get("event") == LATE_ACTION_EVENT]
    assert fourth.record is not None and len(fourth.record.actions) == 1


# ── the owed session: an explicit date ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("now", "owed"),
    [
        (datetime(2026, 10, 5, 21, 45, tzinfo=IST), OCT_SECOND),  # the scheduled 21:45 run: today
        (datetime(2026, 10, 6, 21, 45, tzinfo=IST), OCT_THIRD),  # the next evening's run: its day
        (datetime(2026, 10, 1, 21, 45, tzinfo=IST), OCT_FIRST),  # the evening before a holiday
        (datetime(2026, 10, 5, 23, 59, tzinfo=IST), OCT_SECOND),  # a late retry the same night
        (datetime(2026, 10, 5, 20, 30, tzinfo=IST), OCT_SECOND),  # any time after 18:30: today
        (datetime(2026, 10, 6, 0, 30, tzinfo=IST), OCT_SECOND),  # a retry after midnight
        (datetime(2026, 10, 6, 18, 29, tzinfo=IST), OCT_SECOND),  # before today's EOD is due
        (datetime(2026, 10, 3, 9, 0, tzinfo=IST), OCT_FIRST),  # Saturday, after the holiday
        (datetime(2026, 10, 2, 20, 30, tzinfo=IST), OCT_FIRST),  # the holiday's own evening
    ],
)
def test_the_owed_session_is_the_latest_whose_eod_is_due(now: datetime, owed: date) -> None:
    assert owed_session(FixtureWorld(), now) == owed


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
        book_state=record.book_state,
        book_digest=record.book_digest,
        actions=record.actions_document(),
    )
    assert restored == record


# ── the scheduler entry point: disabled by default, explicit date ────────────────────────────────


def _context(now: datetime, *, enabled: bool) -> JobContext:
    return JobContext(
        job_name="paper_session",
        run_id=uuid4(),
        clock=FrozenClock(now),
        settings=Settings(paper_session_enabled=enabled),
    )


def test_the_job_is_disabled_by_default_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2: no same-evening TRI for the regime filter yet, so the flag defaults off."""
    world, store, journal = FixtureWorld(), InMemoryPaperSessionStore(), RecordingJournal()
    install_job_seams(monkeypatch.setattr, world=world, store=store, journal=journal)

    assert Settings().paper_session_enabled is False
    result = run_paper_session_job(
        _context(datetime(2026, 10, 1, 20, 30, tzinfo=IST), enabled=False)
    )

    assert result is None
    assert journal.entries == [] and world.reads == []
    assert store.get(PAPER_BOOK_ID, OCT_FIRST) is None


def test_a_retry_after_midnight_decides_the_session_that_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N1: the 21:45 run failed; the 00:30 retry decides that session, not the new day."""
    world, store, journal = FixtureWorld(), InMemoryPaperSessionStore(), RecordingJournal()
    install_job_seams(monkeypatch.setattr, world=world, store=store, journal=journal)

    retry = run_paper_session_job(_context(datetime(2026, 10, 6, 0, 30, tzinfo=IST), enabled=True))

    assert retry is not None
    assert retry.trading_date == OCT_SECOND
    assert retry.verdict is RunVerdict.DECIDED
    assert {entry.trading_date for entry in journal.entries} == {OCT_SECOND}
    # The evening run of the new day then decides that day, and a rerun is a no-op.
    evening = run_paper_session_job(
        _context(datetime(2026, 10, 6, 20, 30, tzinfo=IST), enabled=True)
    )
    assert evening is not None and evening.trading_date == OCT_THIRD
    again = run_paper_session_job(_context(datetime(2026, 10, 6, 21, 0, tzinfo=IST), enabled=True))
    assert again is not None and again.verdict is RunVerdict.ALREADY_DECIDED


def test_the_job_decides_an_explicit_date_when_given_one(monkeypatch: pytest.MonkeyPatch) -> None:
    world, store, journal = FixtureWorld(), InMemoryPaperSessionStore(), RecordingJournal()
    install_job_seams(monkeypatch.setattr, world=world, store=store, journal=journal)
    result = run_paper_session_job(
        _context(datetime(2026, 10, 9, 20, 30, tzinfo=IST), enabled=True),
        trading_date=OCT_FIRST,
    )
    assert result is not None and result.trading_date == OCT_FIRST


# ── R1: a corrected or re-sourced corporate action is never booked twice ─────────────────────────


def _cash(record: PaperSessionRecord | None) -> Decimal:
    assert record is not None and record.book_state is not None
    return Decimal(record.book_state["broker"]["cash"])


def _held_quantity(record: PaperSessionRecord | None, isin: str) -> int:
    assert record is not None and record.book_state is not None
    broker = record.book_state["broker"]
    return sum(
        int(lot["quantity"])
        for key in ("holdings", "pending")
        for lot in broker[key]
        if lot["isin"] == isin
    )


def test_a_dividend_whose_terms_change_after_booking_is_escalated_and_never_recredited() -> None:
    desk = _Desk.fresh()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        desk.run(day)
    isin = _held_isin(desk)
    desk.world.actions.append(CashDividend(isin=isin, ex_date=OCT_FOURTH, per_share=Decimal("5")))
    fourth = desk.run(OCT_FOURTH)
    assert fourth.record is not None
    (booked,) = fourth.record.actions
    assert booked.status is ActionStatus.BOOKED and booked.held

    # The store corrects the amount: same dividend, new terms.
    desk.world.actions[:] = [CashDividend(isin=isin, ex_date=OCT_FOURTH, per_share=Decimal("6"))]
    fifth = desk.run(date(2026, 10, 8))

    assert fifth.verdict is RunVerdict.DECIDED
    escalations = [e for e in fifth.entries if e.payload.get("event") == CHANGED_ACTION_EVENT]
    assert [e.decision for e in escalations] == [Decision.ESCALATE]
    assert escalations[0].payload["action"] == booked.key
    # Never credited again: no ledger line for the name today, cash moved only by the session.
    assert fifth.record is not None and fifth.record.book_state is not None
    assert not [line for line in fifth.record.book_state["session_ledger"] if line["isin"] == isin]
    assert _cash(fifth.record) == _cash(fourth.record)


def test_after_an_escalation_the_book_is_red_until_the_owner_resolves_it() -> None:
    """Item 2: an escalated action on a held name stops the book, as red data does (#10)."""
    desk = _Desk.fresh()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        desk.run(day)
    isin = _held_isin(desk)
    desk.world.actions.append(CashDividend(isin=isin, ex_date=OCT_FOURTH, per_share=Decimal("5")))
    desk.run(OCT_FOURTH)
    desk.world.actions[:] = [CashDividend(isin=isin, ex_date=OCT_FOURTH, per_share=Decimal("6"))]
    escalated = desk.run(date(2026, 10, 8))
    assert escalated.record is not None
    (flagged,) = [a for a in escalated.record.actions if a.status is ActionStatus.ESCALATED]

    blocked = desk.run(date(2026, 10, 9))
    assert blocked.verdict is RunVerdict.SKIPPED_DATA_RED
    assert f"{flagged.key}@{flagged.terms}" in blocked.reason and "unresolved" in blocked.reason
    assert _decisions(blocked.entries) == [Decision.SKIPPED_DATA_RED]

    desk.store.resolve("paper_fixture_book", flagged.key, flagged.terms)
    resumed = desk.run(date(2026, 10, 12))
    assert resumed.verdict is RunVerdict.DECIDED
    # Resolved means accepted as it stands: the changed terms are not escalated again.
    assert not [e for e in resumed.entries if e.decision is Decision.ESCALATE]


@pytest.mark.parametrize("feed_kind", [RescaleKind.SPLIT, RescaleKind.BONUS])
def test_an_implied_split_followed_by_its_feed_record_is_neither_rebooked_nor_escalated(
    feed_kind: RescaleKind,
) -> None:
    desk = _Desk.fresh()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        desk.run(day)
    isin = _held_isin(desk)
    before = _held_quantity(desk.store.get("paper_fixture_book", OCT_THIRD), isin)
    desk.world.actions.append(
        ShareRescale(
            isin=isin,
            ex_date=OCT_FOURTH,
            kind=RescaleKind.SPLIT,
            numerator=Decimal("2"),
            denominator=Decimal("1"),
            source=RescaleSource.IMPLIED,
        )
    )
    fourth = desk.run(OCT_FOURTH)
    assert _held_quantity(fourth.record, isin) == 2 * before

    # The feed reports the same split (as 4:2, possibly filed as a bonus); L2 drops its implied row.
    desk.world.actions[:] = [
        ShareRescale(
            isin=isin,
            ex_date=OCT_FOURTH,
            kind=feed_kind,
            numerator=Decimal("4"),
            denominator=Decimal("2"),
            source=RescaleSource.FEED,
        )
    ]
    fifth = desk.run(date(2026, 10, 8))

    assert fifth.verdict is RunVerdict.DECIDED
    assert not [e for e in fifth.entries if e.decision is Decision.ESCALATE]
    assert not [e for e in fifth.entries if "event" in e.payload]
    assert _held_quantity(fifth.record, isin) == 2 * before
    assert fifth.record is not None and fifth.record.actions == ()


def test_a_field_added_to_an_action_class_does_not_change_its_identity_or_terms() -> None:
    """A later version of the class with an extra field is the same action — no re-booking."""

    @dataclass(frozen=True)
    class DividendWithTds(CashDividend):
        tds_rate: Decimal = Decimal("0.10")

    old = CashDividend(isin=ISINS[0], ex_date=OCT_FOURTH, per_share=Decimal("5"))
    new = DividendWithTds(isin=ISINS[0], ex_date=OCT_FOURTH, per_share=Decimal("5.0"))
    assert action_identity(new) == action_identity(old)
    assert action_terms(new) == action_terms(old)
    # Provenance is not terms either.
    implied = ShareRescale(
        isin=ISINS[0],
        ex_date=OCT_FOURTH,
        kind=RescaleKind.SPLIT,
        numerator=Decimal("2"),
        denominator=Decimal("1"),
        source=RescaleSource.IMPLIED,
    )
    feed = replace(implied, source=RescaleSource.FEED, knowable_date=OCT_FOURTH)
    assert (action_identity(feed), action_terms(feed)) == (
        action_identity(implied),
        action_terms(implied),
    )


# ── item 1: an explicit date is bounded by the book's history and the owed session ───────────────


def test_an_explicit_date_after_the_owed_session_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, store, journal = FixtureWorld(), InMemoryPaperSessionStore(), RecordingJournal()
    install_job_seams(monkeypatch.setattr, world=world, store=store, journal=journal)
    with pytest.raises(PaperSessionError, match="after the owed session"):
        run_paper_session_job(
            _context(datetime(2026, 10, 6, 0, 30, tzinfo=IST), enabled=True),
            trading_date=OCT_THIRD,  # 6 Oct's EOD is not due at 00:30 on 6 Oct
        )
    assert journal.entries == []


def test_an_explicit_date_before_the_latest_decided_session_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, store, journal = FixtureWorld(), InMemoryPaperSessionStore(), RecordingJournal()
    install_job_seams(monkeypatch.setattr, world=world, store=store, journal=journal)
    run_paper_session_job(
        _context(datetime(2026, 10, 5, 20, 30, tzinfo=IST), enabled=True)
    )  # decides 5 Oct
    with pytest.raises(PaperSessionError, match="decides forward only"):
        run_paper_session_job(
            _context(datetime(2026, 10, 5, 21, 0, tzinfo=IST), enabled=True),
            trading_date=OCT_FIRST,
        )
    # The latest decided date itself is allowed: it is the idempotent no-op.
    again = run_paper_session_job(
        _context(datetime(2026, 10, 5, 21, 0, tzinfo=IST), enabled=True),
        trading_date=OCT_SECOND,
    )
    assert again is not None and again.verdict is RunVerdict.ALREADY_DECIDED


# ── R2: a resolution covers one set of terms; distinct terms under one identity are all booked ──


def test_a_second_correction_after_a_resolution_blocks_the_book_again() -> None:
    """₹5 booked → corrected to ₹6 (escalated, resolved) → corrected to ₹7: red again."""
    desk = _Desk.fresh()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        desk.run(day)
    isin = _held_isin(desk)

    def dividend(per_share: str) -> CashDividend:
        return CashDividend(isin=isin, ex_date=OCT_FOURTH, per_share=Decimal(per_share))

    desk.world.actions.append(dividend("5"))
    desk.run(OCT_FOURTH)
    desk.world.actions[:] = [dividend("6")]
    first_fix = desk.run(date(2026, 10, 8))
    assert first_fix.record is not None
    (six,) = [a for a in first_fix.record.actions if a.status is ActionStatus.ESCALATED]
    desk.store.resolve("paper_fixture_book", six.key, six.terms)
    assert desk.run(date(2026, 10, 9)).verdict is RunVerdict.DECIDED

    desk.world.actions[:] = [dividend("7")]
    second_fix = desk.run(date(2026, 10, 12))
    assert second_fix.verdict is RunVerdict.DECIDED  # the session that sees it still decides
    assert second_fix.record is not None
    (seven,) = [a for a in second_fix.record.actions if a.status is ActionStatus.ESCALATED]
    assert seven.key == six.key and seven.terms != six.terms
    assert [e.payload["event"] for e in second_fix.entries if e.decision is Decision.ESCALATE] == [
        CHANGED_ACTION_EVENT
    ]

    again = desk.run(date(2026, 10, 13))
    assert again.verdict is RunVerdict.SKIPPED_DATA_RED
    assert f"{seven.key}@{seven.terms}" in again.reason
    # Never credited for either correction: no ledger line for the name after the original ₹5.
    credits = [
        line
        for run in (first_fix, second_fix)
        if run.record is not None and run.record.book_state is not None
        for line in run.record.book_state["session_ledger"]
        if line["isin"] == isin
    ]
    assert credits == []


def test_two_dividends_on_one_ex_date_are_both_credited_and_never_escalated() -> None:
    """An interim and a special dividend on one ex-date share an identity, not their terms."""
    desk = _Desk.fresh()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        desk.run(day)
    isin = _held_isin(desk)
    held = _held_quantity(desk.store.get("paper_fixture_book", OCT_THIRD), isin)
    desk.world.actions.extend(
        [
            CashDividend(isin=isin, ex_date=OCT_FOURTH, per_share=Decimal("5")),
            CashDividend(isin=isin, ex_date=OCT_FOURTH, per_share=Decimal("2")),
        ]
    )
    fourth = desk.run(OCT_FOURTH)

    assert fourth.record is not None and fourth.record.book_state is not None
    credited = sorted(
        Decimal(line["credit"])
        for line in fourth.record.book_state["session_ledger"]
        if line["isin"] == isin and line["description"].startswith("DIVIDEND")
    )
    assert credited == [Decimal("2") * held, Decimal("5") * held]
    assert len({a.key for a in fourth.record.actions}) == 1 and len(fourth.record.actions) == 2
    for day in (date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12)):
        later = desk.run(day)
        assert later.verdict is RunVerdict.DECIDED
        assert not [e for e in later.entries if e.decision is Decision.ESCALATE]
        assert later.record is not None and later.record.actions == ()


# ── item 1: rescale twins only when one side is implied ──────────────────────────────────────────


def _split(isin: str, kind: RescaleKind, source: RescaleSource, num: str, den: str) -> ShareRescale:
    return ShareRescale(
        isin=isin,
        ex_date=OCT_FOURTH,
        kind=kind,
        numerator=Decimal(num),
        denominator=Decimal(den),
        source=source,
    )


@pytest.mark.parametrize(
    ("first_source", "second_source"),
    [(RescaleSource.FEED, RescaleSource.IMPLIED), (RescaleSource.IMPLIED, RescaleSource.FEED)],
)
def test_a_same_ratio_rescale_is_a_twin_when_either_side_is_implied(
    first_source: RescaleSource, second_source: RescaleSource
) -> None:
    desk = _Desk.fresh()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        desk.run(day)
    isin = _held_isin(desk)
    before = _held_quantity(desk.store.get("paper_fixture_book", OCT_THIRD), isin)
    desk.world.actions.append(_split(isin, RescaleKind.SPLIT, first_source, "2", "1"))
    desk.run(OCT_FOURTH)
    desk.world.actions[:] = [_split(isin, RescaleKind.BONUS, second_source, "2", "1")]
    fifth = desk.run(date(2026, 10, 8))

    assert not [e for e in fifth.entries if e.decision is Decision.ESCALATE]
    assert _held_quantity(fifth.record, isin) == 2 * before
    assert fifth.record is not None and fifth.record.actions == ()


def test_two_feed_rescales_of_one_ratio_under_different_kinds_are_escalated_not_twinned() -> None:
    desk = _Desk.fresh()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        desk.run(day)
    isin = _held_isin(desk)
    before = _held_quantity(desk.store.get("paper_fixture_book", OCT_THIRD), isin)
    desk.world.actions.append(_split(isin, RescaleKind.SPLIT, RescaleSource.FEED, "2", "1"))
    desk.run(OCT_FOURTH)
    desk.world.actions.append(_split(isin, RescaleKind.BONUS, RescaleSource.FEED, "2", "1"))
    fifth = desk.run(date(2026, 10, 8))

    escalations = [e for e in fifth.entries if e.decision is Decision.ESCALATE]
    assert [e.payload["event"] for e in escalations] == [AMBIGUOUS_ACTION_EVENT]
    assert _held_quantity(fifth.record, isin) == 2 * before, "not rescaled a second time"
    assert desk.run(date(2026, 10, 9)).verdict is RunVerdict.SKIPPED_DATA_RED


# ── "held" is the entitlement entering the ex-date, not today's book ─────────────────────────────

_SEPT_30 = date(2026, 9, 30)


def _sold_since_ex_date(first: ShareRescale | CashDividend) -> _Desk:
    """A book that held ISINS[0] entering 30 Sep (and booked ``first`` then) and sold it in October.

    The turnover book holds the September leaders; October's ranking reversal drops ISINS[0] out of
    the band, sold on the 1st and filled on the 5th, so on the 6th the book no longer holds it.
    """
    desk = _Desk.fresh(spec=fixture_spec(_TURNOVER))
    desk.world.actions.append(first)
    for day in calendar_sessions(date(2026, 9, 28), OCT_SECOND):
        assert desk.run(day).verdict is RunVerdict.DECIDED
    entering = desk.store.get("paper_fixture_book", date(2026, 9, 29))
    assert _held_quantity(entering, ISINS[0]) > 0, "held entering the ex-date"
    assert _held_quantity(desk.store.get("paper_fixture_book", OCT_SECOND), ISINS[0]) == 0, (
        "sold since"
    )
    return desk


def test_an_ambiguous_rescale_on_a_name_sold_since_its_ex_date_is_escalated() -> None:
    """Held at the ex-date, sold since: the twin still concerns the book, so it is escalated."""
    split = _split(ISINS[0], RescaleKind.SPLIT, RescaleSource.FEED, "2", "1")
    desk = _sold_since_ex_date(replace(split, ex_date=_SEPT_30))
    desk.world.actions.append(
        replace(_split(ISINS[0], RescaleKind.BONUS, RescaleSource.FEED, "2", "1"), ex_date=_SEPT_30)
    )
    sixth = desk.run(OCT_THIRD)

    escalations = [e for e in sixth.entries if e.decision is Decision.ESCALATE]
    assert [e.payload["event"] for e in escalations] == [AMBIGUOUS_ACTION_EVENT]
    assert escalations[0].isin == ISINS[0]
    assert desk.run(OCT_FOURTH).verdict is RunVerdict.SKIPPED_DATA_RED


def test_a_changed_dividend_on_a_name_sold_since_its_ex_date_is_escalated() -> None:
    desk = _sold_since_ex_date(
        CashDividend(isin=ISINS[0], ex_date=_SEPT_30, per_share=Decimal("3"))
    )
    desk.world.actions[:] = [CashDividend(isin=ISINS[0], ex_date=_SEPT_30, per_share=Decimal("4"))]
    sixth = desk.run(OCT_THIRD)

    escalations = [e for e in sixth.entries if e.decision is Decision.ESCALATE]
    assert [e.payload["event"] for e in escalations] == [CHANGED_ACTION_EVENT]
    assert sixth.record is not None and sixth.record.book_state is not None
    assert not [
        line for line in sixth.record.book_state["session_ledger"] if line["isin"] == ISINS[0]
    ], "never re-credited"
