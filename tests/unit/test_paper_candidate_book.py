"""M14.11 / D40 — the V2 + S+refill candidate paper book beside D13.

Pre-registration: ``ops/studies/preregistration-paper-v2-srefill-2026-11-07.md``. One block per
acceptance criterion:

* the candidate's parameters are the M14.10 arm's exactly, and D13 plus exactly three switches;
  everything else in its spec is D13's;
* it opens parked: nothing before its first session, an ``OPENED_PARKED`` journal entry on the
  first decided one, no rebalance in its opening month, the V2 trigger read every parked session;
* decided one process per day it equals one replay from the parked state — the parked flag and the
  rank map the rank-ordered redeploy reads are carried across sessions, not re-derived;
* in the job, D13's decisions, journal and book are byte-identical with and without the
  candidate, and a candidate failure leaves D13's session committed and fails the run loud.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime
from typing import Any
from uuid import uuid4

import pytest

from analyst.journal.models import Decision, JournalEntry
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar
from backtest.paper_session import (
    _BOOK_ID,
    CANDIDATE_BOOK_ID,
    CANDIDATE_FIRST_SESSION,
    CANDIDATE_PREREGISTRATION,
    OPENED_PARKED_EVENT,
    PAPER_BOOK_ID,
    RECON_EVENT,
    REGIME_TRI_INPUT,
    InMemoryPaperSessionStore,
    PaperBookSpec,
    PaperSessionError,
    PaperSessionRecord,
    PaperSessionResult,
    RecordingJournal,
    RunVerdict,
    SessionOutcome,
    candidate_paper_book,
    paper_books,
    ratified_paper_book,
    run_paper_session,
    run_paper_session_job,
)
from backtest.policies.early_entry import EarlyEntry
from backtest.policies.momentum_v2 import (
    D13_V2_SECTOR_REFILL,
    PAPER_CANDIDATE_V2_SREFILL_2026_11_07,
    PAPER_RATIFIED_2026_09_06,
    MomentumV2Parameters,
    MomentumV2Policy,
)
from backtest.rails import BACKTEST_CASE_ID, RailGate
from backtest.replay import ReplayEngine
from backtest.run import _ACCOUNT_STATE, _AccountingBroker, _held_by
from backtest.sweep import EARLY_V2_SECTOR_REFILL, NIFTY500_WF_ARMS
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.scheduler import JobContext
from dataplatform.status.queries_portfolio_paper import paper_policy
from execution.costs import CostModel, load_rate_card
from execution.kill_switch import KillSwitch
from execution.sim_broker import SimBroker
from tests.paper_session_support import (
    FIRST,
    FIXTURE_CASH,
    OCT_FIRST,
    OCT_FOURTH,
    OCT_SECOND,
    OCT_THIRD,
    FixtureWorld,
    calendar_sessions,
    candidate_fixture_spec,
    fixture_spec,
    fresh_kill_switch,
    install_job_seams,
)

RUN_AT = datetime(2026, 10, 1, 20, 30, tzinfo=IST)

#: A fixture-sized candidate: top-9 with no wider band, so a month's ranking reversal sells a name
#: and the next session redeploys (the shape ``test_paper_session_job._TURNOVER`` uses for D13),
#: with the candidate's three switches on.
_FIXTURE_CANDIDATE = MomentumV2Parameters(
    top_n=9,
    use_12_1=True,
    sell_band=9,
    regime_filter=True,
    vol_scaled=True,
    redeploy_next_session=True,
    early_entry=EarlyEntry.MOMENTUM_TURN,
    rank_ordered_buys=True,
    sector_refill=True,
)


_DEEP_CASH = FIXTURE_CASH * 3


@dataclass(frozen=True, slots=True)
class _Verdict:
    green: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.green


def _green(_: date) -> _Verdict:
    return _Verdict(True)


@dataclass
class _Desk:
    world: FixtureWorld
    spec: PaperBookSpec
    store: InMemoryPaperSessionStore = field(default_factory=InMemoryPaperSessionStore)
    journal: RecordingJournal = field(default_factory=RecordingJournal)
    kill_switch: KillSwitch = field(default_factory=fresh_kill_switch)

    def run(self, day: date) -> PaperSessionResult:
        return run_paper_session(
            trading_date=day,
            spec=self.spec,
            world=self.world,
            store=self.store,
            journal=self.journal,
            gate=_green,
            clock=FrozenClock(RUN_AT),
            kill_switch=self.kill_switch,
        )


def _events(entries: tuple[JournalEntry, ...] | list[JournalEntry]) -> list[str | None]:
    return [entry.payload.get("event") for entry in entries]


def _decisions(entries: tuple[JournalEntry, ...] | list[JournalEntry]) -> list[Decision]:
    """The policy's own decisions: no reconciliation entry, no opening entry."""
    return [
        entry.decision
        for entry in entries
        if entry.payload.get("event") not in (RECON_EVENT, OPENED_PARKED_EVENT)
    ]


# ── the parameters and the spec ──────────────────────────────────────────────────────────────────


def test_the_candidate_parameters_are_the_m14_10_arm_exactly() -> None:
    (arm,) = [arm for arm in NIFTY500_WF_ARMS if arm.label == EARLY_V2_SECTOR_REFILL]
    assert PAPER_CANDIDATE_V2_SREFILL_2026_11_07 == arm.v2 == D13_V2_SECTOR_REFILL
    # The repr is the run fingerprint: the same string the M14.10 runs were keyed by.
    assert repr(PAPER_CANDIDATE_V2_SREFILL_2026_11_07) == repr(arm.v2)


def test_the_candidate_is_d13_plus_exactly_three_switches() -> None:
    candidate = PAPER_CANDIDATE_V2_SREFILL_2026_11_07
    changed = {
        f.name
        for f in fields(MomentumV2Parameters)
        if getattr(candidate, f.name) != getattr(PAPER_RATIFIED_2026_09_06, f.name)
    }
    assert changed == {"early_entry", "rank_ordered_buys", "sector_refill"}
    assert candidate == replace(
        PAPER_RATIFIED_2026_09_06,
        early_entry=EarlyEntry.MOMENTUM_TURN,
        rank_ordered_buys=True,
        sector_refill=True,
    )


def test_the_candidate_spec_is_d13s_but_for_parameters_and_opening() -> None:
    d13, candidate = ratified_paper_book(), candidate_paper_book()
    assert [spec.book_id for spec in paper_books()] == [PAPER_BOOK_ID, CANDIDATE_BOOK_ID]
    assert _BOOK_ID.match(CANDIDATE_BOOK_ID)
    assert candidate.book_id == "momentum_v2_turn_srefill_paper_2026_11_09"
    assert candidate.parameters is PAPER_CANDIDATE_V2_SREFILL_2026_11_07
    assert candidate.opening_cash == d13.opening_cash
    assert candidate.datasets == d13.datasets
    assert candidate.rail_policy == d13.rail_policy
    assert candidate.first_session == CANDIDATE_FIRST_SESSION == date(2026, 11, 9)
    assert candidate.opens_parked and CANDIDATE_PREREGISTRATION in candidate.opening_basis
    assert candidate.carries_policy_state
    # D13 is exactly what it was: any session, no parked opening, no policy state carried.
    assert d13.parameters is PAPER_RATIFIED_2026_09_06
    assert d13.first_session is None and not d13.opens_parked and not d13.carries_policy_state


def test_the_candidate_has_its_own_portfolio_label() -> None:
    assert paper_policy(PAPER_BOOK_ID) == "momentum v2 (D13)"
    assert "D40" in paper_policy(CANDIDATE_BOOK_ID)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"parameters": replace(PAPER_RATIFIED_2026_09_06, early_entry=EarlyEntry.SCALE_IN)},
            "runner",
        ),
        (
            {"parameters": replace(PAPER_RATIFIED_2026_09_06, early_entry=EarlyEntry.BREADTH_TURN)},
            "runner",
        ),
        ({"parameters": replace(PAPER_RATIFIED_2026_09_06, regime_daily_reentry=True)}, "runner"),
        ({"first_session": None}, "first session"),
        ({"opening_basis": "  "}, "basis"),
    ],
)
def test_a_spec_the_forward_runner_cannot_honour_is_refused(
    change: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(candidate_fixture_spec(_FIXTURE_CANDIDATE), **change)


# ── the parked opening ───────────────────────────────────────────────────────────────────────────


def _candidate_desk(world: FixtureWorld | None = None, *, first: date = OCT_FIRST) -> _Desk:
    return _Desk(
        world=world or FixtureWorld(),
        spec=candidate_fixture_spec(_FIXTURE_CANDIDATE, first_session=first),
    )


def test_a_session_before_the_first_is_not_open_and_writes_nothing() -> None:
    desk = _candidate_desk(first=OCT_SECOND)
    result = desk.run(OCT_FIRST)
    assert result.verdict is RunVerdict.NOT_OPEN
    assert result.entries == () and result.record is None
    assert desk.journal.entries == [] and desk.store.records(desk.spec.book_id) == ()
    assert desk.world.reads == []


def test_the_first_session_opens_parked_and_journals_it_without_rebalancing() -> None:
    desk = _candidate_desk()
    result = desk.run(OCT_FIRST)  # the fixture's October rebalance day — not this book's

    assert result.verdict is RunVerdict.DECIDED
    record = result.record
    assert record is not None and not record.rebalanced and record.reason == "decided"
    assert record.orders == () and record.pending is None
    opening = result.entries[0]
    assert opening.payload["event"] == OPENED_PARKED_EVENT
    assert opening.decision is Decision.HEARTBEAT
    assert opening.payload["paper_book"] == desk.spec.book_id
    assert opening.payload["first_session"] == OCT_FIRST.isoformat()
    assert opening.rationale == desk.spec.opening_basis
    # The parked policy read its trigger and nothing else: no regime level, no ranked candidates.
    assert _decisions(result.entries) == [Decision.HEARTBEAT]
    assert set(desk.world.reads) == {("regime_trend", OCT_FIRST)}
    assert record.book_state is not None
    assert record.book_state["policy"] == {"parked": "true", "rank_of": {}}
    # The opening is journaled once: the next session is the book's own.
    second = desk.run(OCT_SECOND)
    assert OPENED_PARKED_EVENT not in _events(second.entries)


def test_a_red_first_session_journals_the_opening_on_the_first_decided_one() -> None:
    desk = _candidate_desk(FixtureWorld(no_trend={OCT_FIRST}))
    red = desk.run(OCT_FIRST)
    assert red.verdict is RunVerdict.SKIPPED_DATA_RED
    assert OPENED_PARKED_EVENT not in _events(red.entries)
    green = desk.run(OCT_SECOND)
    assert green.verdict is RunVerdict.DECIDED
    assert _events(green.entries)[0] == OPENED_PARKED_EVENT


def test_v2_fires_on_a_parked_session_and_reenters_off_the_monthly_rebalance() -> None:
    desk = _candidate_desk(FixtureWorld(trend_fires={OCT_THIRD}))
    for day in (OCT_FIRST, OCT_SECOND):
        parked = desk.run(day).record
        assert parked is not None and parked.book_state is not None
        assert parked.book_state["policy"]["parked"] == "true"
    fire = desk.run(OCT_THIRD)

    record = fire.record
    assert record is not None and record.reason == "early_reentry"
    assert not record.rebalanced  # the month's rebalance stays the calendar's, as in the backtest
    assert set(_decisions(fire.entries)) == {Decision.BUY}
    assert record.book_state is not None and record.book_state["policy"]["parked"] == "false"
    # Unparked, the next session reads no trigger and fills what the re-entry staged.
    desk.world.reads.clear()
    after = desk.run(OCT_FOURTH)
    assert ("regime_trend", OCT_FOURTH) not in desk.world.reads
    assert after.book is not None and after.book.positions


def test_a_trigger_that_does_not_fire_never_pays_for_the_signal() -> None:
    desk = _candidate_desk()
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD):
        desk.run(day)
    assert {kind for kind, _ in desk.world.reads} == {"regime_trend"}


def test_a_parked_candidate_without_its_trend_level_is_red_and_d13_is_not() -> None:
    world = FixtureWorld(no_trend={OCT_SECOND})
    candidate, d13 = _candidate_desk(world), _Desk(world=world, spec=fixture_spec())
    for day in (OCT_FIRST, OCT_SECOND):
        d13_result = d13.run(day)
        result = candidate.run(day)
    assert d13_result.verdict is RunVerdict.DECIDED
    assert result.verdict is RunVerdict.SKIPPED_DATA_RED
    (entry,) = result.entries
    assert entry.payload["missing_input"] == REGIME_TRI_INPUT
    assert "early re-entry" in str(entry.rationale)


def test_a_candidate_row_without_its_policy_state_fails_loud() -> None:
    desk = _candidate_desk()
    desk.run(OCT_FIRST)
    (record,) = desk.store.records(desk.spec.book_id)
    assert record.book_state is not None
    stripped = {key: value for key, value in record.book_state.items() if key != "policy"}
    tampered = InMemoryPaperSessionStore()
    tampered.record(replace(record, book_state=stripped), recorded_at=RUN_AT)
    desk.store = tampered
    with pytest.raises(PaperSessionError, match="no policy state"):
        desk.run(OCT_SECOND)


def test_d13_rows_carry_no_policy_state() -> None:
    desk = _Desk(world=FixtureWorld(), spec=fixture_spec())
    for day in (OCT_FIRST, OCT_SECOND):
        record = desk.run(day).record
        assert record is not None and record.book_state is not None
        assert set(record.book_state) == {"broker", "session_ledger"}


# ── the candidate is the replay, one process per day ─────────────────────────────────────────────


def _untagged(entries: list[JournalEntry]) -> list[JournalEntry]:
    return [
        entry.model_copy(
            update={
                "payload": {
                    key: value
                    for key, value in entry.payload.items()
                    if key not in ("paper_book", "mode")
                }
            }
        )
        for entry in entries
        if entry.payload.get("event") not in (RECON_EVENT, OPENED_PARKED_EVENT)
    ]


def test_the_candidate_a_day_at_a_time_equals_one_replay_from_the_parked_state() -> None:
    """Opened parked in September, V2 fires, October's reversal sells, the redeploy is ranked.

    The October redeploy submits its buys by the rank map of the October 1 rebalance — a session
    decided by an earlier process — so this equality fails if the rank map is not carried.
    """
    days = calendar_sessions(FIRST, date(2026, 10, 9))
    trigger = days[1]  # Tuesday 29 Sep: the first parked session after the opening
    world = FixtureWorld(trend_fires={trigger})
    # Three times the fixture's capital: every buy is cut to A8's ₹1.2 L order cap, so the whole
    # basket is still short of its weight when the October redeploy comes and it buys several names.
    spec = replace(
        candidate_fixture_spec(_FIXTURE_CANDIDATE, first_session=FIRST), opening_cash=_DEEP_CASH
    )
    desk = _Desk(world=world, spec=spec)
    paper = [desk.run(day) for day in days]
    assert all(run.verdict is RunVerdict.DECIDED for run in paper)

    replay_world = FixtureWorld(trend_fires={trigger}, rebalance_on={OCT_FIRST})
    clock = FrozenClock(days[0])
    book = PortfolioBook()
    book.deposit(days[0], _DEEP_CASH)
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state=_ACCOUNT_STATE),
        market=replay_world.market(first=days[0], through=days[-1], held=_held_by(book)),
        opening_cash=_DEEP_CASH,
    )
    broker = _AccountingBroker(sim, book, corporate_actions=BookActionCalendar(()))
    rails = desk.spec.rail_policy
    policy = MomentumV2Policy(
        replay_world.momentum_data(days[0], _FIXTURE_CANDIDATE),
        _FIXTURE_CANDIDATE,
        order_caps=rails.rails,
        sector_of=rails.sectors.sector_of,
    )
    policy.resume(None, parked=True)  # the declared opening state
    replay = ReplayEngine(
        policy=policy,
        broker=broker,
        clock=clock,
        sessions=days,
        rails=RailGate(rails, replay_world.marks(_held_by(book))),
    ).run()

    replayed = [
        entry.model_copy(update={"case_id": None}) if entry.case_id == BACKTEST_CASE_ID else entry
        for entry in replay.journal
    ]
    assert _untagged([e for run in paper for e in run.entries]) == replayed
    final = paper[-1].record
    assert final is not None and final.book_state is not None
    assert final.book_state["broker"] == sim.export_state().to_document()

    # The walk really exercised the carried state: the early re-entry, a monthly rebalance that
    # sold, and a redeploy by a later process whose buys go best-ranked first — October ranks the
    # high-index names first, the reverse of ISIN order.
    by_day = {run.trading_date: run for run in paper}
    assert by_day[trigger].reason == "early_reentry"
    rebalance = by_day[OCT_FIRST].record
    assert rebalance is not None and rebalance.rebalanced and rebalance.pending is not None
    assert Decision.SELL in _decisions(by_day[OCT_FIRST].entries)
    assert rebalance.book_state is not None and rebalance.book_state["policy"]["rank_of"]
    redeploy = [str(e.isin) for e in by_day[OCT_SECOND].entries if e.decision is Decision.BUY]
    assert len(redeploy) >= 2 and redeploy != sorted(redeploy)


def test_the_candidate_from_scratch_is_byte_identical() -> None:
    days = [OCT_FIRST, OCT_SECOND, OCT_THIRD, OCT_FOURTH]
    first = _candidate_desk(FixtureWorld(trend_fires={OCT_SECOND}))
    second = _candidate_desk(FixtureWorld(trend_fires={OCT_SECOND}))
    for day in days:
        a, b = first.run(day), second.run(day)
        assert a.journal_bytes() == b.journal_bytes() and a.book_bytes() == b.book_bytes()
    assert first.journal.entries == second.journal.entries


# ── the job: D13 first, isolated ─────────────────────────────────────────────────────────────────


def _context(now: datetime) -> JobContext:
    return JobContext(
        job_name="paper_session",
        run_id=uuid4(),
        clock=FrozenClock(now),
        settings=Settings(paper_session_enabled=True),
    )


#: Evenings after each session's EOD, from the October rebalance on.
_EVENINGS = [
    datetime(day.year, day.month, day.day, 21, 45, tzinfo=IST)
    for day in (OCT_FIRST, OCT_SECOND, OCT_THIRD, OCT_FOURTH)
]


@dataclass
class _JobDesk:
    world: FixtureWorld
    store: InMemoryPaperSessionStore = field(default_factory=InMemoryPaperSessionStore)
    journal: RecordingJournal = field(default_factory=RecordingJournal)

    def book_entries(self, book_id: str) -> list[JournalEntry]:
        return [e for e in self.journal.entries if e.payload.get("paper_book") == book_id]

    def records(self, book_id: str) -> tuple[PaperSessionRecord, ...]:
        return self.store.records(book_id)


@pytest.fixture
def kill_switch_root(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each book's kill switch in a fresh directory, armed — never the box's data root."""
    import backtest.paper_session as module

    def armed(_root: object, book_id: str, *, clock: object = None) -> KillSwitch:
        return KillSwitch(tmp_path / f"{book_id}.json", clock=FrozenClock(RUN_AT))

    monkeypatch.setattr(module, "paper_kill_switch", armed)
    yield


def _run_job(
    monkeypatch: pytest.MonkeyPatch,
    books: Callable[[], tuple[PaperBookSpec, ...]],
    *,
    trend_fires: frozenset[date] = frozenset({OCT_SECOND}),
) -> _JobDesk:
    import backtest.paper_session as module

    desk = _JobDesk(world=FixtureWorld(trend_fires=set(trend_fires)))
    install_job_seams(monkeypatch.setattr, world=desk.world, store=desk.store, journal=desk.journal)
    monkeypatch.setattr(module, "paper_books", books)
    for evening in _EVENINGS:
        run_paper_session_job(_context(evening))
    return desk


def _d13() -> PaperBookSpec:
    return fixture_spec()


def _candidate() -> PaperBookSpec:
    return candidate_fixture_spec(_FIXTURE_CANDIDATE)


@pytest.mark.usefixtures("kill_switch_root")
def test_d13_is_byte_identical_with_and_without_the_candidate_in_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alone = _run_job(monkeypatch, lambda: (_d13(),))
    both = _run_job(monkeypatch, lambda: (_d13(), _candidate()))

    d13 = _d13().book_id
    assert alone.records(d13) == both.records(d13)
    assert alone.book_entries(d13) == both.book_entries(d13)
    assert [r.book_digest for r in alone.records(d13)] == [r.book_digest for r in both.records(d13)]
    # The candidate really decided beside it, trades included (V2 fired on 5 Oct).
    candidate = both.records(_candidate().book_id)
    assert [r.outcome for r in candidate] == [SessionOutcome.COMPLETED] * len(_EVENINGS)
    assert any(r.reason == "early_reentry" and r.orders for r in candidate)


@pytest.mark.usefixtures("kill_switch_root")
def test_a_candidate_failure_leaves_d13_committed_and_unchanged_and_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backtest.paper_session as module
    from tests import paper_session_support as support

    alone = _run_job(monkeypatch, lambda: (_d13(),))

    real = module.run_paper_session
    broken = {_candidate().book_id}

    def candidate_breaks(**kwargs: Any) -> PaperSessionResult:
        if kwargs["spec"].book_id in broken:
            raise RuntimeError("candidate exploded")
        return real(**kwargs)

    desk = _JobDesk(world=FixtureWorld(trend_fires={OCT_SECOND}))
    install_job_seams(monkeypatch.setattr, world=desk.world, store=desk.store, journal=desk.journal)
    monkeypatch.setattr(module, "paper_books", lambda: (_d13(), _candidate()))
    monkeypatch.setattr(module, "run_paper_session", candidate_breaks)
    commits = support._NoConnection.commits
    for evening in _EVENINGS:
        with pytest.raises(PaperSessionError, match="failed") as raised:
            run_paper_session_job(_context(evening))
        assert isinstance(raised.value.__cause__, RuntimeError)
    # D13 decided and committed every evening, exactly as alone; the candidate wrote nothing.
    d13 = _d13().book_id
    assert support._NoConnection.commits - commits == len(_EVENINGS)
    assert desk.records(d13) == alone.records(d13)
    assert desk.book_entries(d13) == alone.book_entries(d13)
    assert desk.records(_candidate().book_id) == ()
    assert desk.book_entries(_candidate().book_id) == []

    # Fixed, the retry is a no-op for D13 and decides the candidate's owed session.
    broken.clear()
    result = run_paper_session_job(_context(_EVENINGS[-1]))
    assert result is not None and result.verdict is RunVerdict.ALREADY_DECIDED
    assert [r.trading_date for r in desk.records(_candidate().book_id)] == [OCT_FOURTH]


@pytest.mark.usefixtures("kill_switch_root")
def test_a_d13_failure_still_runs_the_candidate_and_raises_d13s_own_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backtest.paper_session as module

    real = module.run_paper_session

    def d13_breaks(**kwargs: Any) -> PaperSessionResult:
        if kwargs["spec"].book_id == _d13().book_id:
            raise PaperSessionError("d13 exploded")
        return real(**kwargs)

    desk = _JobDesk(world=FixtureWorld())
    install_job_seams(monkeypatch.setattr, world=desk.world, store=desk.store, journal=desk.journal)
    monkeypatch.setattr(module, "paper_books", lambda: (_d13(), _candidate()))
    monkeypatch.setattr(module, "run_paper_session", d13_breaks)
    with pytest.raises(PaperSessionError, match="d13 exploded"):
        run_paper_session_job(_context(_EVENINGS[0]))
    assert [r.trading_date for r in desk.records(_candidate().book_id)] == [OCT_FIRST]


@pytest.mark.usefixtures("kill_switch_root")
def test_the_real_books_leave_the_candidate_unopened_before_9_november(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production book list over the October fixture: D13 decides, the candidate is not open."""
    desk = _JobDesk(world=FixtureWorld())
    install_job_seams(monkeypatch.setattr, world=desk.world, store=desk.store, journal=desk.journal)
    result = run_paper_session_job(_context(_EVENINGS[0]))
    assert result is not None and result.verdict is RunVerdict.DECIDED
    assert desk.records(PAPER_BOOK_ID) and desk.records(CANDIDATE_BOOK_ID) == ()
    assert desk.book_entries(CANDIDATE_BOOK_ID) == []
