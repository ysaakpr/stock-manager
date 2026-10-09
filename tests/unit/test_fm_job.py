"""M17.7 — the daily M17 job: interlock, managers under a deadline, stops, S0, the scoreboard.

Offline throughout: the market is an in-memory `FmMarket`, the Commons are the M17.9 synthetic
world (or a scripted shortlist), the model is `StubLLM` scripted per manager, and the journal and
the desk ledger are in memory. Each acceptance criterion and each brief requirement is a test:

1. a red data day journals ``SKIPPED_DATA_RED`` for every book and stages nothing;
2. a manager past the deadline journals ``MISSED_SESSION`` and the others still run (a late answer,
   and a rate limit that cannot be waited out inside the deadline);
3. a stop breach stages ``STOP_EXIT`` with zero model calls;
4. ``--start`` writes ``M17_S0`` and the mandate hash in every book's stream;
5. a full three-session synthetic run's scoreboard is identical when rebuilt from the journal;
plus the data wait, the dry stream's isolation and a held delisted name.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from analyst.commons import build_screens, load_table
from analyst.fundmanager import ManagerMandate, Roster, load_roster
from analyst.fundmanager.books import LastTraded
from analyst.fundmanager.bundle import RoundTrip
from analyst.fundmanager.contract import ContractVerdict
from analyst.fundmanager.controls import BenchmarkLevels
from analyst.fundmanager.job import (
    DATA_GAPS_EVENT,
    DATA_WAIT_EVENT,
    MISSED_SESSION_EVENT,
    SKIPPED_DATA_RED_EVENT,
    STREAM_DRY,
    STREAM_LIVE,
    DeadlineLLM,
    StreamJournal,
    mandate_fingerprints,
)
from analyst.fundmanager.render import PromptTemplate
from analyst.fundmanager.runtime import (
    MANAGER_ERROR_EVENT,
    NO_ACTION_EVENT,
    ManagerCommons,
    ManagerSessionResult,
    SessionStatus,
)
from analyst.fundmanager.schemas import ManagerDecision
from analyst.fundmanager.scoreboard import (
    DECISION_EVENT,
    MARK_EVENT,
    OUTCOME_EVENT,
    S0_EVENT,
    DecisionAction,
    ScoredDecision,
    build_scoreboard,
    decision_payload,
    inputs_from_journal,
)
from analyst.fundmanager.stops import STOP_EXIT_EVENT
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve
from analyst.llm import (
    DEFAULT_MAX_TOKENS,
    LLMError,
    LLMRateLimitError,
    LLMResponse,
    Message,
    StopReason,
    StubLLM,
    StubReply,
    ToolCall,
    ToolSpec,
    prompt_digest,
)
from backtest.book_actions import BookActionCalendar, BookActionSource
from backtest.fm_circuit import CircuitMarket, NoCircuitData
from backtest.fm_job import (
    DESK_IDS,
    M17JobError,
    M17SessionResult,
    RunOutcome,
    SessionCommons,
    WaitPolicy,
    run_m17_session,
)
from backtest.fm_world import LakeDelistedNames
from backtest.paper_session import InMemoryPaperSessionStore, RecordingJournal, SessionOutcome
from dataplatform.clock import IST, FrozenClock
from dataplatform.identity.master import ListingStatus
from dataplatform.query.universe import InMemoryListingCalendar, ListingWindow
from execution.kill_switch import KillSwitch, TripSource
from execution.sim_broker import SessionMarket
from tests.fm_books_support import Bar, FmMarket, switch_at, weekdays
from tests.fm_scoreboard_support import DictBench, compounding, mini_roster, shortlist_of
from tests.unit.commons_screen_world import (
    BREAKOUT,
    FUTURE_SESSIONS,
    LEADER,
    NAMES,
    SECTOR,
    SESSION,
    FakeScreenSource,
    screen_world,
    sheets_for,
)
from tests.unit.commons_screen_world import clock as world_clock
from tests.unit.test_fm_runtime import FIXTURE, World, _commons, _shortlist, buy, hold, research

CALENDAR = weekdays(date(2026, 8, 3), 70)  # 2026-08-03 .. 2026-11-06, no holidays
FLAT = Decimal("100")
HELD = NAMES[4]  # a filler of the synthetic world, outside the shortlist
BENCH = DictBench(compounding(CALENDAR, Decimal("0.001")))
EVENING = time(22, 0)


@pytest.fixture(scope="module")
def world() -> World:
    """The M17.9 synthetic Commons for SESSION (the runtime tests' world)."""
    lake = screen_world()
    sheets = sheets_for(lake)
    screens = build_screens(
        sheets,
        shortlist=None,
        source=FakeScreenSource(lake),
        clock=world_clock(),
        future_sessions=FUTURE_SESSIONS,
    )
    return World(sheets, screens, _shortlist(sheets, [LEADER, BREAKOUT]), load_table(FIXTURE))


# ── the world ────────────────────────────────────────────────────────────────────────────────────


def flat_market(
    overrides: Mapping[tuple[str, date], Bar] | None = None, names: Sequence[str] = NAMES
) -> FmMarket:
    bars = {
        (isin, day): Bar(FLAT, FLAT, Decimal("10000000000")) for isin in names for day in CALENDAR
    }
    bars.update(overrides or {})
    return FmMarket(CALENDAR, bars, {isin: SECTOR.get(isin, "Tech") for isin in names})


@dataclass
class FakeWorld:
    """`M17World` over one `FmMarket` and a `DictBench`."""

    market: FmMarket
    bench: DictBench = BENCH
    missing: list[tuple[str, ...]] = field(default_factory=list)
    delisted_names: Any = None
    readiness_calls: int = 0

    def is_session(self, day: date) -> bool:
        return day in self.market.sessions

    def sessions(self, start: date, end: date) -> Sequence[date]:
        return [s for s in self.market.sessions if start <= s <= end]

    def previous_session(self, day: date) -> date:
        return max(s for s in self.market.sessions if s < day)

    def next_session(self, day: date) -> date:
        return min(s for s in self.market.sessions if s > day)

    def fill_market(self, held: Callable[[], Iterable[str]]) -> SessionMarket:
        return self.market

    def book_market(self) -> FmMarket:
        return self.market

    def circuit(self) -> CircuitMarket:
        return NoCircuitData()

    def corporate_actions(self) -> BookActionSource:
        return BookActionCalendar()

    def delisted(self) -> Any:
        return self.delisted_names

    def adjusted_close(self, isin: str, session: date) -> Decimal | None:
        return self.market.close(isin, session)

    def bench_levels(self, label: str, *, through: date, method: str | None) -> BenchmarkLevels:
        return self.bench

    def readiness(self, session: date) -> tuple[str, ...]:
        self.readiness_calls += 1
        return self.missing.pop(0) if self.missing else ()


@dataclass(frozen=True)
class Verdict:
    ok: bool
    why: str = "all datasets PUBLISHED"

    @property
    def reason(self) -> str:
        return self.why

    def __bool__(self) -> bool:
        return self.ok


def green(_: date) -> Verdict:
    return Verdict(True)


@dataclass
class Builder:
    """A `CommonsBuilder` handing out the synthetic Commons (or a scripted shortlist)."""

    make: Callable[[date], SessionCommons]
    built: list[date] = field(default_factory=list)

    def build(self, session: date, *, clock: Any) -> SessionCommons:
        self.built.append(session)
        return self.make(session)


def synthetic_commons(world_: World, tmp_path: Path) -> Builder:
    commons = _commons(world_, tmp_path)
    return Builder(
        lambda _: SessionCommons(
            shortlist=world_.shortlist,
            manager=commons,
            sheets=world_.sheets,
            screens=world_.screens,
        )
    )


def evening(day: date, at: time = EVENING) -> FrozenClock:
    return FrozenClock(datetime.combine(day, at, tzinfo=IST))


# ── a model scripted per manager ─────────────────────────────────────────────────────────────────


Effect = Mapping[str, Any] | Exception | Callable[[], Mapping[str, Any] | Exception]


class PerManagerLLM:
    """`StubLLM` answering each manager's calls from that manager's own script.

    A script step is a structured output, an exception to raise, or a callable (run at call time,
    e.g. to move the clock) returning either. The manager is the one whose id the prompt names.
    """

    def __init__(self, scripts: Mapping[str, Sequence[Effect]]) -> None:
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.stub = StubLLM(synthesize_unknown=False)
        self.calls: list[str] = []

    def _who(self, prompt: str) -> str:
        found = [m for m in self.scripts if m in prompt]
        assert len(found) == 1, f"cannot tell the manager from the prompt: {found}"
        return found[0]

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        who = self._who(messages[-1].content)
        self.calls.append(who)
        step: Any = self.scripts[who].pop(0)
        if callable(step):
            step = step()
        if isinstance(step, Exception):
            raise step
        digest = prompt_digest(messages, model=model, tools=tools, system=system)
        self.stub.register(
            digest,
            StubReply(
                text="",
                tool_calls=(ToolCall(id="structured_output", name=tools[0].name, arguments=step),),
                stop_reason=StopReason.TOOL_USE,
            ),
        )
        return self.stub.complete(
            messages, model=model, tools=tools, system=system, max_tokens=max_tokens
        )


NOTHING = {"no_action": True, "no_action_reason": "nothing clears the hurdle", "decisions": []}


def quiet() -> list[Effect]:
    """An empty-book manager's session: round 0 asks for nothing, then nothing today."""
    return [research(holdings=False), NOTHING]


# ── running the job ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Desk:
    """One stream's persistent side across sessions: the journal, the ledger, the switch."""

    tmp: Path
    stream: str = STREAM_DRY
    sink: RecordingJournal = field(default_factory=RecordingJournal)
    store: InMemoryPaperSessionStore = field(default_factory=InMemoryPaperSessionStore)
    journal: StreamJournal = field(init=False)
    switch: KillSwitch = field(init=False)

    def __post_init__(self) -> None:
        self.journal = StreamJournal(self.sink, self.stream)
        self.switch = switch_at(self.tmp, evening(CALENDAR[0]))

    def entries(self) -> list[JournalEntry]:
        return [self.journal.unprefixed(e) for e in self.sink.entries]

    def events(
        self, event: str, *, book: str | None = None, on: date | None = None
    ) -> list[JournalEntry]:
        return [
            e
            for e in self.entries()
            if e.payload.get("event") == event
            and (book is None or e.case_id == book)
            and (on is None or e.trading_date == on)
        ]

    def run(
        self,
        session: date,
        world_: FakeWorld,
        builder: Builder,
        llm: Any,
        *,
        clock: FrozenClock | None = None,
        roster: Roster | None = None,
        start: bool = False,
        gate: Any = green,
        runner: Any = None,
        sleep: Callable[[float], None] | None = None,
        wait: WaitPolicy | None = None,
    ) -> M17SessionResult:
        clock = clock or evening(session)
        kwargs: dict[str, Any] = {} if runner is None else {"runner": runner}
        return run_m17_session(
            session,
            world=world_,
            commons=builder,
            store=self.store,
            journal=self.journal,
            gate=gate,
            llm=llm,
            fetcher=_NoFetch(),
            clock=clock,
            kill_switch=self.switch,
            roster=roster,
            start=start,
            sleep=sleep or (lambda seconds: clock.advance(timedelta(seconds=seconds))),
            wait=wait,
            digest_dir=self.tmp / "digest",
            entries_reader=lambda: list(self.sink.entries),
            **kwargs,
        )


class _NoFetch:
    @property
    def name(self) -> str:
        return "none:test"

    def fetch(self, request: Any) -> Any:
        raise AssertionError("no test asks the web")


def staged(desk: Desk, book: str | None = None) -> list[JournalEntry]:
    return [
        e
        for e in desk.entries()
        if e.decision in (Decision.BUY, Decision.SELL)
        and e.orders_ref is not None
        and (book is None or e.case_id == book)
    ]


# ── acceptance 1: a red day ──────────────────────────────────────────────────────────────────────


def test_a_red_data_day_skips_every_book_and_stages_nothing(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    roster = load_roster()
    llm = PerManagerLLM({m.id: [] for m in roster.managers})
    builder = Builder(lambda _: pytest.fail("no Commons are built on a red day"))

    result = desk.run(
        SESSION,
        FakeWorld(flat_market()),
        builder,
        llm,
        gate=lambda _: Verdict(False, "nse_bhavcopy is FAILED"),
    )

    assert result.outcome is RunOutcome.SKIPPED_DATA_RED
    skipped = desk.events(SKIPPED_DATA_RED_EVENT)
    assert sorted(e.case_id or "" for e in skipped) == sorted(b.id for b in roster.books)
    assert all(e.decision is Decision.SKIPPED_DATA_RED for e in skipped)
    assert all("nse_bhavcopy is FAILED" in (e.rationale or "") for e in skipped)
    assert not staged(desk) and not llm.calls and not builder.built
    record = desk.store.get(DESK_IDS[STREAM_DRY], SESSION)
    assert record is not None and record.outcome is SessionOutcome.SKIPPED_DATA_RED
    assert record.book_state is None and not record.orders


def test_a_status_read_that_fails_is_red_never_green(tmp_path: Path) -> None:
    desk = Desk(tmp_path)

    def broken(_: date) -> Verdict:
        raise ConnectionError("status database unreachable")

    result = desk.run(
        SESSION,
        FakeWorld(flat_market()),
        Builder(lambda _: pytest.fail("never built")),
        PerManagerLLM({}),
        roster=mini_roster(),
        gate=broken,
    )
    assert result.outcome is RunOutcome.SKIPPED_DATA_RED
    assert len(desk.events(SKIPPED_DATA_RED_EVENT)) == 3 and not staged(desk)


# ── acceptance 2: the deadline ───────────────────────────────────────────────────────────────────


def test_a_manager_past_the_deadline_misses_the_session_and_the_others_still_run(
    world: World, tmp_path: Path
) -> None:
    desk = Desk(tmp_path)
    clock = evening(SESSION)
    deadline = datetime.combine(date(2026, 10, 9), time(8, 30), tzinfo=IST)

    def late() -> Mapping[str, Any]:
        clock.freeze_at(deadline + timedelta(minutes=5))  # the answer arrives after 08:30
        return research(holdings=False)

    roster = load_roster()
    first, second, third, last = (m.id for m in roster.managers)
    llm = PerManagerLLM({first: quiet(), second: quiet(), third: quiet(), last: [late]})

    result = desk.run(
        SESSION, FakeWorld(flat_market()), synthetic_commons(world, tmp_path), llm, clock=clock
    )

    status = {m.book_id: m.status for m in result.managers}
    assert status == {
        first: SessionStatus.NO_ACTION.value,
        second: SessionStatus.NO_ACTION.value,
        third: SessionStatus.NO_ACTION.value,
        last: MISSED_SESSION_EVENT,
    }
    (missed,) = desk.events(MISSED_SESSION_EVENT)
    assert missed.case_id == last and missed.payload["late_calls"] == "1"
    assert missed.payload["deadline"] == deadline.isoformat()
    assert not desk.events(DECISION_EVENT, book=last)  # a late answer is never a decision
    for manager in (first, second, third):
        assert len(desk.events(NO_ACTION_EVENT, book=manager)) == 1
    # the rest of the evening still ran: controls rebalanced, every book marked
    assert {e.case_id for e in desk.events("CONTROL_REBALANCE")} == {c.id for c in roster.controls}
    assert {e.case_id for e in desk.events(MARK_EVENT)} == {b.id for b in roster.books}
    assert not staged(desk, last)


def test_a_rate_limit_backs_off_inside_the_deadline_and_one_that_cannot_misses(
    world: World, tmp_path: Path
) -> None:
    desk = Desk(tmp_path)
    clock = evening(SESSION)
    deadline = datetime.combine(date(2026, 10, 9), time(8, 30), tzinfo=IST)
    roster = load_roster()
    first, second, third, last = (m.id for m in roster.managers)

    def near_deadline() -> Mapping[str, Any]:
        clock.freeze_at(deadline - timedelta(minutes=3))  # a long night of queued calls
        return NOTHING

    limited = LLMRateLimitError("rate limit reached for the subscription")
    llm = PerManagerLLM(
        {
            # two limits waited out (2 + 4 minutes), then the session runs to its end
            first: [limited, limited, research(holdings=False), near_deadline],
            # at 08:27 one 2-minute wait still fits; the next 4-minute one would end past 08:30
            second: [limited, limited],
            third: quiet(),
            last: quiet(),
        }
    )

    result = desk.run(
        SESSION, FakeWorld(flat_market()), synthetic_commons(world, tmp_path), llm, clock=clock
    )

    status = {m.book_id: m.status for m in result.managers}
    assert status[first] == SessionStatus.NO_ACTION.value
    assert status[second] == MISSED_SESSION_EVENT
    assert status[third] == status[last] == SessionStatus.NO_ACTION.value  # the queue moves on
    (missed,) = desk.events(MISSED_SESSION_EVENT)
    assert missed.case_id == second and missed.payload["rate_limit_retries"] == "1"
    assert desk.events(MANAGER_ERROR_EVENT, book=second)  # the runtime's own line, beside it
    assert llm.calls.count(first) == 4 and llm.calls.count(second) == 2


def test_deadline_llm_refuses_to_start_after_the_deadline() -> None:
    clock = evening(SESSION)
    llm = DeadlineLLM(StubLLM(), clock.now() - timedelta(seconds=1), clock, lambda _: None)
    with pytest.raises(LLMError, match="deadline"):
        llm.complete([Message(role=_user(), content="hello")], model="claude-opus-5-5")
    assert llm.missed


def _user() -> Any:
    from analyst.llm import Role

    return Role.USER


# ── stops: mechanical, no model call ─────────────────────────────────────────────────────────────


def _verdict(
    decision: ManagerDecision, *, quantity: int = 0, stop: Decimal | None = None
) -> ContractVerdict:
    trip = None
    if quantity:
        trip = RoundTrip(
            quantity=quantity,
            turnover=FLAT * quantity,
            charges_pct=Decimal("0.2"),
            slippage_bps_per_side=Decimal("2"),
            slippage_pct=Decimal("0.04"),
            total_pct=Decimal("0.24"),
        )
    return ContractVerdict(
        decision=decision, reasons=(), citations=(), round_trip=trip, stop_price=stop
    )


@dataclass
class ScriptedRunner:
    """A `run_manager` stand-in: journals each scripted decision as the runtime does
    (``FM_DECISION``, the scoreboard's flat payload) and returns it accepted. Counts its calls."""

    script: Mapping[tuple[str, date], Sequence[ContractVerdict]]
    asked: list[tuple[str, date]] = field(default_factory=list)

    def __call__(
        self,
        mandate: ManagerMandate,
        session: date,
        commons: ManagerCommons,
        book: Any,
        llm: Any,
        fetcher: Any,
        *,
        journal: Any,
        clock: Any,
        pricer: Any = None,
        template: Any = None,
    ) -> ManagerSessionResult:
        self.asked.append((mandate.id, session))
        verdicts = tuple(self.script.get((mandate.id, session), ()))
        for verdict in verdicts:
            d = verdict.decision
            scored = ScoredDecision(
                book_id=mandate.id,
                decided_on=session,
                isin=d.isin,
                action=DecisionAction(d.action.value),
                horizon_sessions=d.horizon_sessions,
                p_beat_bench=d.p_beat_bench,
                target_weight=d.target_weight,
                expected_excess_pct=d.expected_excess_pct,
                edge_type=d.edge_type.value,
                stop_pct=d.stop_pct,
            )
            kind = {"BUY": Decision.BUY, "SELL": Decision.SELL, "TRIM": Decision.SELL}.get(
                d.action.value, Decision.HOLD
            )
            journal.append(
                JournalEntry(
                    ts=clock.now(),
                    trading_date=session,
                    case_id=mandate.id,
                    actor=Actor.T2,
                    decision=kind,
                    isin=d.isin,
                    sleeve=Sleeve.TACTICAL if kind in (Decision.BUY, Decision.SELL) else None,
                    rationale=d.rationale,
                    model=mandate.models.decision,
                    payload={**decision_payload(scored), "event": DECISION_EVENT},
                )
            )
        status = SessionStatus.DECIDED if verdicts else SessionStatus.NO_ACTION
        return ManagerSessionResult(
            book_id=mandate.id,
            session=session,
            status=status,
            calls=0,
            repairs=0,
            accepted=verdicts,
        )


def _decision(isin: str, action: str, **over: Any) -> ManagerDecision:
    if action == "BUY":
        body = buy(Decimal("0.02"), isin=isin)
    else:
        body = hold(
            isin, action=action, edge_type="NONE" if action in ("WATCH", "PASS") else "TREND_LEADER"
        )
    body.update(over)
    return ManagerDecision.model_validate(body)


def test_a_stop_breach_stages_a_stop_exit_with_zero_model_calls(
    world: World, tmp_path: Path
) -> None:
    desk = Desk(tmp_path)
    roster = mini_roster()
    manager = roster.managers[0].id
    bought = date(2026, 10, 5)
    # bought on 10-05 at 100 with a 4% stop (96); 10-06 fills; 10-08 closes at 80, below it (two
    # sessions after the fill, so the min-hold rail lets the stop's sell through)
    market = flat_market(
        {(HELD, SESSION): Bar(Decimal("85"), Decimal("80"), Decimal("10000000000"))}
    )
    test_world = FakeWorld(market)
    runner = ScriptedRunner(
        {(manager, bought): [_verdict(_decision(HELD, "BUY", stop_pct=4), quantity=500)]}
    )
    builder = Builder(
        lambda day: SessionCommons(
            shortlist=shortlist_of(day, NAMES[:2]), manager=_commons(world, tmp_path)
        )
    )
    for day in (bought, date(2026, 10, 6), date(2026, 10, 7)):
        desk.run(day, test_world, builder, PerManagerLLM({}), runner=runner, roster=roster)
    assert desk.store.get(DESK_IDS[STREAM_DRY], date(2026, 10, 7)) is not None

    # SESSION's evening: the job starts after the deadline, so the manager is never asked
    llm = PerManagerLLM({manager: []})
    late = evening(date(2026, 10, 9), time(9, 0))
    result = desk.run(SESSION, test_world, builder, llm, clock=late, runner=runner, roster=roster)

    (exit_line,) = [e for e in staged(desk, manager) if e.payload.get("event") == STOP_EXIT_EVENT]
    assert exit_line.trading_date == SESSION and exit_line.isin == HELD
    assert exit_line.decision is Decision.SELL and exit_line.payload["quantity"] == "500"
    assert "no model call" in (exit_line.rationale or "")
    assert not llm.calls and (manager, SESSION) not in runner.asked
    assert {m.book_id: m.status for m in result.managers}[manager] == MISSED_SESSION_EVENT
    assert result.managers[0].stop_exits == 1


def test_a_stopped_names_own_decision_is_dropped_for_the_stop(world: World, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    roster = mini_roster()
    manager = roster.managers[0].id
    bought = date(2026, 10, 5)
    market = flat_market(
        {(HELD, SESSION): Bar(Decimal("85"), Decimal("80"), Decimal("10000000000"))}
    )
    runner = ScriptedRunner(
        {
            (manager, bought): [_verdict(_decision(HELD, "BUY", stop_pct=4), quantity=500)],
            (manager, SESSION): [_verdict(_decision(HELD, "HOLD"))],
        }
    )
    builder = Builder(
        lambda day: SessionCommons(
            shortlist=shortlist_of(day, NAMES[:2]), manager=_commons(world, tmp_path)
        )
    )
    for day in (bought, date(2026, 10, 6), date(2026, 10, 7), SESSION):
        desk.run(day, FakeWorld(market), builder, PerManagerLLM({}), runner=runner, roster=roster)
    sells = [e for e in staged(desk, manager) if e.trading_date == SESSION]
    assert [e.payload["event"] for e in sells] == [STOP_EXIT_EVENT]


def test_a_stop_inside_the_min_hold_window_is_refused_by_the_rail_not_bypassed(
    world: World, tmp_path: Path
) -> None:
    desk = Desk(tmp_path)
    roster = mini_roster()
    manager = roster.managers[0].id
    bought = date(2026, 10, 6)  # fills 10-07; a 10-08 stop exit is one session after the fill
    market = flat_market(
        {(HELD, SESSION): Bar(Decimal("85"), Decimal("80"), Decimal("10000000000"))}
    )
    runner = ScriptedRunner(
        {(manager, bought): [_verdict(_decision(HELD, "BUY", stop_pct=4), quantity=500)]}
    )
    builder = Builder(scripted_commons)
    for day in (bought, date(2026, 10, 7), SESSION):
        desk.run(day, FakeWorld(market), builder, PerManagerLLM({}), runner=runner, roster=roster)
    (block,) = [
        e for e in desk.entries() if e.decision is Decision.RAIL_BLOCK and e.trading_date == SESSION
    ]
    assert block.isin == HELD and "MIN_HOLD" in block.payload["rails"]


# ── --start S0 ───────────────────────────────────────────────────────────────────────────────────


def test_start_journals_m17_s0_and_every_mandate_hash_in_every_stream(tmp_path: Path) -> None:
    desk = Desk(tmp_path, stream=STREAM_LIVE)
    roster = load_roster()
    runner = ScriptedRunner({})
    builder = Builder(scripted_commons)
    world_ = FakeWorld(flat_market())
    with pytest.raises(M17JobError, match="has not started"):
        desk.run(SESSION, world_, builder, PerManagerLLM({}), runner=runner)

    result = desk.run(SESSION, world_, builder, PerManagerLLM({}), runner=runner, start=True)

    expected = mandate_fingerprints(roster)
    lines = desk.events(S0_EVENT)
    assert sorted(e.case_id or "" for e in lines) == sorted(b.id for b in roster.books)
    for line in lines:
        assert line.trading_date == SESSION and line.evidence_snapshot_ref
        assert line.payload["mandate_hash"] == expected[line.case_id or ""].mandate_hash
    assert result.scoreboard is not None and result.scoreboard.s0 == SESSION
    assert inputs_from_journal(desk.entries(), roster).s0 == SESSION
    # S0 opens a stream once
    with pytest.raises(M17JobError, match="S0 opens a stream once"):
        desk.run(date(2026, 10, 9), world_, builder, PerManagerLLM({}), runner=runner, start=True)


def test_mandate_hashes_cover_the_prompt_and_differ_by_book() -> None:
    roster = load_roster()
    base = mandate_fingerprints(roster)
    assert len({f.mandate_hash for f in base.values()}) == len(roster.books)
    edited = PromptTemplate(PromptTemplate.load().text + "\n<!-- one more byte -->\n")
    changed = mandate_fingerprints(roster, template=edited)
    for manager in roster.managers:
        assert changed[manager.id].mandate_hash != base[manager.id].mandate_hash
    for control in roster.controls:  # a control's policy has no prompt in it
        assert changed[control.id].mandate_hash == base[control.id].mandate_hash


class _Sentinel:
    """A ManagerCommons stand-in for runs whose runner never reads it."""


def scripted_commons(day: date) -> SessionCommons:
    """A scripted shortlist of the first two names and a Commons view no runner reads."""
    return SessionCommons(
        shortlist=shortlist_of(day, NAMES[:2]), manager=cast(ManagerCommons, _Sentinel())
    )


# ── the full loop: three sessions, a scoreboard the journal reproduces ───────────────────────────


def test_a_three_session_run_rebuilds_the_same_scoreboard_from_the_journal(tmp_path: Path) -> None:
    desk = Desk(tmp_path, stream=STREAM_DRY)
    roster = mini_roster()
    manager = roster.managers[0].id
    s1, s2, s3 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
    names = NAMES
    drift = {isin: Decimal("0.002") * (k - 4) for k, isin in enumerate(names)}
    from tests.fm_scoreboard_support import drifting_market

    market = drifting_market(
        CALENDAR, names, drift=drift, sectors={i: SECTOR.get(i, "Tech") for i in names}
    )
    runner = ScriptedRunner(
        {
            (manager, s1): [
                _verdict(_decision(names[0], "BUY", stop_pct=6), quantity=400),
                _verdict(_decision(names[1], "WATCH", horizon_sessions=1, p_beat_bench=0.7)),
            ],
            (manager, s2): [
                _verdict(_decision(names[2], "PASS", horizon_sessions=1, p_beat_bench=0.2))
            ],
            (manager, s3): [
                _verdict(
                    _decision(
                        names[0],
                        "SELL",
                        what_changed={"kind": "BETTER_USE", "text": "a stronger name"},
                    )
                )
            ],
        }
    )
    builder = Builder(
        lambda day: SessionCommons(
            shortlist=shortlist_of(day, names[3:6]), manager=cast(ManagerCommons, _Sentinel())
        )
    )
    results = [
        desk.run(
            day,
            FakeWorld(market),
            builder,
            PerManagerLLM({}),
            roster=roster,
            runner=runner,
            start=day == s1,
        )
        for day in (s1, s2, s3)
    ]

    for result in results:
        assert result.outcome is RunOutcome.COMPLETED and result.scoreboard_error is None
        assert result.scoreboard is not None and result.rebuilt_digest == result.scoreboard.digest()
    rebuilt = build_scoreboard(roster, inputs_from_journal(desk.entries(), roster))
    final = results[-1].scoreboard
    assert final is not None and rebuilt.canonical_bytes() == final.canonical_bytes()
    assert final.s0 == s1 and final.sessions_elapsed == 3
    # the loop really ran: marks for every book every session, outcomes, a rail refusal, fills
    for day in (s1, s2, s3):
        assert {e.case_id for e in desk.events(MARK_EVENT, on=day)} == {b.id for b in roster.books}
    assert len(desk.events(OUTCOME_EVENT)) == 2
    assert [e for e in desk.entries() if e.decision is Decision.RAIL_BLOCK]  # sold inside min-hold
    assert staged(desk, manager) and staged(desk, roster.controls[0].id)
    # and nothing of the dry run reached the live streams
    assert all((e.case_id or "m17-dry:").startswith("m17-dry:") for e in desk.sink.entries)
    assert all(e.payload.get("stream") == STREAM_DRY for e in desk.sink.entries)
    assert (tmp_path / "digest" / f"digest-{s3.isoformat()}.md").is_file()


def test_a_rerun_of_a_completed_session_is_a_no_op(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    roster = mini_roster()
    builder = Builder(scripted_commons)
    runner = ScriptedRunner({})
    desk.run(
        SESSION, FakeWorld(flat_market()), builder, PerManagerLLM({}), roster=roster, runner=runner
    )
    before = len(desk.sink.entries)
    again = desk.run(
        SESSION, FakeWorld(flat_market()), builder, PerManagerLLM({}), roster=roster, runner=runner
    )
    assert again.outcome is RunOutcome.ALREADY_DONE and len(desk.sink.entries) == before


# ── waiting for data ─────────────────────────────────────────────────────────────────────────────


def test_the_job_waits_for_late_data_and_journals_the_wait(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    roster = mini_roster()
    world_ = FakeWorld(flat_market(), missing=[("index level IN.NSE.NIFTY_50.CLOSE",)] * 2)
    builder = Builder(scripted_commons)
    clock = evening(SESSION)
    result = desk.run(
        SESSION,
        world_,
        builder,
        PerManagerLLM({}),
        roster=roster,
        runner=ScriptedRunner({}),
        clock=clock,
        wait=WaitPolicy(max_wait=timedelta(minutes=60), poll=timedelta(minutes=10)),
    )
    phases = [e.payload["phase"] for e in desk.events(DATA_WAIT_EVENT)]
    assert phases == ["START", "LANDED"] and not result.data_gaps
    assert clock.now() == evening(SESSION).now() + timedelta(minutes=20)


def test_past_the_wait_bound_the_job_runs_with_the_gaps_recorded(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    roster = mini_roster()
    gap = ("L2 prices_adjusted refresh (10 of the 10 most liquid names end before 2026-10-08)",)
    world_ = FakeWorld(flat_market(), missing=[gap] * 20)
    builder = Builder(scripted_commons)
    result = desk.run(
        SESSION,
        world_,
        builder,
        PerManagerLLM({}),
        roster=roster,
        runner=ScriptedRunner({}),
        wait=WaitPolicy(max_wait=timedelta(minutes=30), poll=timedelta(minutes=10)),
    )
    (gaps,) = desk.events(DATA_GAPS_EVENT)
    assert gaps.payload["missing"] == gap[0] and result.data_gaps == gap
    assert result.outcome is RunOutcome.COMPLETED and builder.built == [SESSION]


# ── the kill switch ──────────────────────────────────────────────────────────────────────────────


def test_a_tripped_kill_switch_halts_every_book_and_calls_no_model(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    roster = mini_roster()
    builder = Builder(scripted_commons)
    runner = ScriptedRunner({})
    desk.run(
        date(2026, 10, 7),
        FakeWorld(flat_market()),
        builder,
        PerManagerLLM({}),
        roster=roster,
        runner=runner,
    )
    desk.switch.trip(reason="owner drill", source=TripSource.MANUAL)
    result = desk.run(
        SESSION, FakeWorld(flat_market()), builder, PerManagerLLM({}), roster=roster, runner=runner
    )
    assert (
        result.outcome is RunOutcome.HALTED and (roster.managers[0].id, SESSION) not in runner.asked
    )
    halted = desk.events("KILL_SWITCH_TRIPPED", on=SESSION)
    assert {e.case_id for e in halted} >= {roster.managers[0].id, roster.controls[0].id}
    assert not [e for e in staged(desk) if e.trading_date == SESSION]
    assert {e.case_id for e in desk.events(MARK_EVENT, on=SESSION)} == {b.id for b in roster.books}


# ── a held delisted name ─────────────────────────────────────────────────────────────────────────


@dataclass
class _Delisted:
    isin: str
    last: LastTraded

    def last_traded(self, isin: str, session: date) -> LastTraded | None:
        return self.last if isin == self.isin and session > self.last.session else None


def test_a_held_delisted_name_is_valued_at_its_last_close_and_never_raises(
    world: World, tmp_path: Path
) -> None:
    desk = Desk(tmp_path)
    roster = mini_roster()
    manager = roster.managers[0].id
    bought = date(2026, 10, 6)
    gone = {(HELD, day): None for day in CALENDAR if day >= SESSION}
    market = flat_market()
    for key in gone:
        market.bars.pop(key, None)
    delisted = _Delisted(HELD, LastTraded(date(2026, 10, 7), FLAT, FLAT))
    runner = ScriptedRunner(
        {(manager, bought): [_verdict(_decision(HELD, "BUY", stop_pct=4), quantity=500)]}
    )
    builder = Builder(scripted_commons)
    for day in (bought, date(2026, 10, 7), SESSION):
        result = desk.run(
            day,
            FakeWorld(market, delisted_names=delisted),
            builder,
            PerManagerLLM({}),
            roster=roster,
            runner=runner,
        )
        assert result.outcome is RunOutcome.COMPLETED
    (mark,) = desk.events(MARK_EVENT, book=manager, on=SESSION)
    assert '"positions":1' in mark.payload["record"]


def test_lake_delisted_names_reads_the_listing_record_not_a_missing_print() -> None:
    gone, gap = NAMES[0], NAMES[1]
    listings = InMemoryListingCalendar(
        (
            ListingWindow(gone, date(2010, 1, 1), date(2026, 10, 8), ListingStatus.DELISTED),
            ListingWindow(gap, date(2010, 1, 1), None, ListingStatus.ACTIVE),
        )
    )

    class Reader:
        def last_prints(self, upto: date) -> dict[str, date]:
            return {gone: date(2026, 10, 7), gap: date(2026, 10, 7)}

        def closes_on(self, session: date) -> dict[str, Decimal]:
            return (
                {gone: Decimal("41.5"), gap: Decimal("12")} if session == date(2026, 10, 7) else {}
            )

        def restricted_close(self, isin: str, session: date) -> None:
            return None

    names = LakeDelistedNames(listings, reader=Reader(), adjusted=lambda i, d: Decimal("40"))  # type: ignore[arg-type]
    last = names.last_traded(gone, date(2026, 10, 9))
    assert last == LastTraded(date(2026, 10, 7), Decimal("41.5"), Decimal("40"))
    assert names.last_traded(gone, date(2026, 10, 7)) is None  # still listed that day
    assert names.last_traded(gap, date(2026, 10, 9)) is None  # a gap is not a delisting
