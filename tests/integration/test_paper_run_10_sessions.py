"""M5.13 — the §5.2 AI/Robotics reference case, funded on paper and run for ten sessions.

This is the M5 gate's end-to-end proof: the analyst agent, wired out of the parts M5 built (A1 case
service, A2 interview, A5 T0 monitor + interlock, A7 cash manager, A8 rails, A9 journal, X1 staging
and the paper broker), taken through a case's whole opening act and then run unattended for ten
consecutive simulated sessions via the scheduler. The four acceptance criteria are each proved here:

1. **Created through the interview flow and ratified.** The case is *not* hand-built: the §5.1
   interview transcript from the B9 fixture (`tests/fixtures/cases/ai_robotics.yaml`) is run through
   `conduct_interview` → `build_proposal`, and the seven §5.2 policies are *derived* from it. The
   derived policy set is proposed and ratified (FIXTURE — B9's paper/test path, never real money),
   the case is funded PAPER and activated. The ratified rails and dial are asserted against the
   fixture's `expected_policies`, so a drift in the recommendation tables fails loudly.
2. **Ten consecutive sessions, a journal entry every day.** The scheduler's `run_once` drives one
   `paper_session` job per day, with a `FrozenClock` advanced one day between runs (B10). Every one
   of the ten trading dates carries at least one journal entry, and every day writes a T0 HEARTBEAT
   (invariant #9 — a day with nothing to act on still records what it looked at).
3. **An injected oversized order is blocked by rails and journaled RAIL_BLOCK.** On one session a
   deliberately fat-fingered order (well over the ratified per-order cap) is put through A8; it is
   refused and a `RAIL_BLOCK` line names the breached rails (invariant #6).
4. **A SIP instalment is parked then deployed.** On the SIP day the ₹10k instalment is parked in the
   liquid ETF the same session (§5.6, decision #10); on a later valid tactical trigger the parked
   cash is deployed into a position. Both are journaled `BUY`s — the park under the CASH sleeve at
   the parking ISIN, the deployment under TACTICAL.

The order journal and broker are the paper stack (`InMemoryOrderJournal`, `SimBroker` over an
in-memory market): "paper mode" is `FundingMode.PAPER` and the same decision path a real broker
would plug into (invariant #5). The decision journal, case service and scheduler are the real
Postgres ones, so this needs the docker postgres (`make up`) and skips loudly if unreachable, as
`test_journal.py`/`test_recon_drill.py` do. Time is frozen and advanced explicitly; no network.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

import psycopg
import pytest
import yaml

from analyst.cases import (
    CaseService,
    CaseState,
    FundingMode,
    PolicySet,
    RatificationKind,
)
from analyst.cash import CashManager
from analyst.cash.queue import CashSource, DeploymentQueue
from analyst.interview import build_proposal, conduct_interview
from analyst.interview.flow import InterviewField
from analyst.journal import (
    Actor,
    Decision,
    EvidenceStore,
    Journal,
    JournalFilter,
    Sleeve,
)
from analyst.journal.models import TokenSpend
from analyst.mapper import (
    ProxyCandidate,
    PurityEvidence,
    PurityEvidenceKind,
    ThemeMap,
    ValueChainStage,
    score_purity,
)
from analyst.monitor import (
    CORE_DATASETS,
    InMemoryEscalationQueue,
    T0Inputs,
    T0Monitor,
    T0Outcome,
)
from analyst.monitor.interlock import GreenLike
from analyst.rails import Portfolio, ProposedOrder, RailEngine
from analyst.thesis import (
    BreakCondition,
    BreakConditionType,
    EvaluationTier,
    Thesis,
    ThesisStatus,
    authorize_buy,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.query import AnnouncementIndex
from dataplatform.scheduler import (
    Job,
    JobContext,
    JobRegistry,
    JobState,
    SchedulerRunner,
)
from dataplatform.store.db import connect, connection, with_dbname
from dataplatform.store.migrate import migrate
from execution.broker import Exchange, OrderRequest, Side
from execution.costs import CostModel, load_rate_card
from execution.kill_switch import KillSwitch
from execution.sim_broker import (
    FillPolicy,
    NoReferenceBarError,
    ReferenceBar,
    ReferencePrice,
    SimBroker,
)
from execution.staging import InMemoryOrderJournal, InternalBook, StagingCoordinator

pytestmark = pytest.mark.integration

#: Pid-suffixed so concurrent build agents do not drop each other's scratch DB (cf test_migrations).
SCRATCH_DB: Final = f"trading_m5_13_paper_run_{os.getpid()}"

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "ai_robotics.yaml"

#: The reference case opens (create → interview → ratify → fund → activate) at this instant, before
#: the run begins — a distinct, earlier clock so the setup journal entries do not fall in the ten
#: trading-date window the run asserts over.
SETUP_AT: Final = datetime(2026, 8, 15, 10, 0, tzinfo=IST)

#: The run's first session. The clock is advanced one calendar day per session, so the ten sessions
#: are 2026-09-01 .. 2026-09-10 inclusive. The 1st is the SIP day (fixture `sip_day: 1`).
RUN_START: Final = datetime(2026, 9, 1, 18, 30, tzinfo=IST)
SESSIONS: Final = tuple(date(2026, 9, day) for day in range(1, 11))
SIP_DATE: Final = date(2026, 9, 1)
DEPLOY_DATE: Final = date(2026, 9, 3)
RAIL_BLOCK_DATE: Final = date(2026, 9, 5)

#: The instrument the deployment buys and the one the oversized order is fat-fingered on — both from
#: the fixture universe. The ETF is the fixture's parking instrument.
DEPLOY_ISIN: Final = "INE100A01010"
DEPLOY_SECTOR: Final = "compute & silicon"
OVERSIZED_ISIN: Final = "INE200B01020"
OVERSIZED_SECTOR: Final = "sensors & actuators"

#: One reference price for every fill and every rail valuation in the run — keeps the arithmetic
#: exact and the assertions legible. The SIP (₹10k) buys exactly ten ETF units at this price.
UNIT_PRICE: Final = Decimal("1000")
OPENING_CASH: Final = Decimal("500000")


# ── the in-memory market (offline stand-in for M4.1, as in test_recon_drill) ─────────────────────


class InMemoryMarket:
    """A `SessionMarket` from a session list and a bar table — the paper broker's data feed."""

    def __init__(self, sessions: list[date], bars: dict[tuple[str, date], ReferenceBar]) -> None:
        self._sessions = sorted(sessions)
        self._bars = bars

    def next_session(self, after: date) -> date:
        for session in self._sessions:
            if session > after:
                return session
        raise NoReferenceBarError(f"no session after {after.isoformat()}")

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        try:
            return self._bars[(isin, session)]
        except KeyError:
            raise NoReferenceBarError(f"no bar for {isin} on {session.isoformat()}") from None


def _bar(isin: str, session: date) -> ReferenceBar:
    return ReferenceBar(
        isin=isin,
        session=session,
        exchange=Exchange.NSE,
        open=UNIT_PRICE,
        vwap=UNIT_PRICE,
        traded_value=Decimal("100000000"),
    )


# ── an always-green interlock (no status DB needed to prove the loop runs green) ──────────────────


@dataclass(frozen=True, slots=True)
class _Green:
    """A `GreenLike` verdict that is always truthy — the interlock passing on a clean day."""

    reason: str = "all core datasets PUBLISHED and quality-green"

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class _AlwaysGreenGate:
    """A `GreenGate` that reports every session green — the fake `test_ratify_ux` documents.

    Lets the paper run prove the ten sessions ran *green* without seeding the status tables; the
    red-day short-circuit (invariant #10) is proved separately in `tests/unit/test_t0.py`.
    """

    def __call__(self, trading_date: date) -> GreenLike:
        return _Green()


# ── the reference case, built through the real §5.1 interview flow from the B9 fixture ────────────


def _load_fixture() -> dict[str, Any]:
    """Read the B9 reference-case fixture (`ai_robotics.yaml`)."""
    return cast("dict[str, Any]", yaml.safe_load(FIXTURE.read_text(encoding="utf-8")))


def _transcript(data: dict[str, Any]) -> dict[InterviewField, str]:
    """The §5.1 interview transcript from the fixture, keyed by `InterviewField`."""
    return {InterviewField(key): str(value) for key, value in data["interview"].items()}


def _theme_map(data: dict[str, Any]) -> ThemeMap:
    """The A3 theme map from the fixture universe, purity scored from disclosed segment splits."""
    stages = {row["stage"] for row in data["universe"]}
    value_chain = tuple(
        ValueChainStage(name=stage, description=f"{stage} stage of the {data['theme']} value chain")
        for stage in sorted(stages)
    )
    candidates = tuple(
        ProxyCandidate(
            isin=row["isin"],
            name=row["name"],
            value_chain_stage=row["stage"],
            purity=score_purity(
                row["isin"],
                (
                    PurityEvidence(
                        kind=PurityEvidenceKind.SEGMENT_DISCLOSURE,
                        source="FY26 annual report segment note",
                        label="theme segment",
                        revenue_fraction=Decimal(row["theme_revenue_fraction"]),
                        expresses_theme=True,
                    ),
                    PurityEvidence(
                        kind=PurityEvidenceKind.SEGMENT_DISCLOSURE,
                        source="FY26 annual report segment note",
                        label="other segment",
                        revenue_fraction=Decimal(row["other_revenue_fraction"]),
                        expresses_theme=False,
                    ),
                ),
            ),
        )
        for row in data["universe"]
    )
    return ThemeMap(
        theme=data["theme"],
        as_of=date.fromisoformat(str(data["as_of"])),
        value_chain=value_chain,
        candidates=tuple(sorted(candidates, key=lambda candidate: candidate.isin)),
        universe_size=42,
        provider="stub",
        model="stub-model",
        tokens=TokenSpend(tokens_in=100, tokens_out=50, cost_inr=Decimal("0.10")),
        rendered_prompt=f"map the {data['theme']} value chain to listed proxies",
    )


def _theses(data: dict[str, Any]) -> dict[str, Thesis]:
    """One §5.3 PROPOSAL/CORE thesis per universe holding, from the fixture."""
    theses: dict[str, Thesis] = {}
    for row in data["theses"]:
        theses[row["isin"]] = Thesis(
            case_id=data["case_id"],
            isin=row["isin"],
            version=1,
            status=ThesisStatus.PROPOSAL,
            driver=row["driver"],
            theme_purity=Decimal(row["theme_purity"]),
            expected_evidence=tuple(row["expected_evidence"]),
            break_conditions=tuple(
                BreakCondition(
                    id=condition["id"],
                    type=BreakConditionType(condition["type"]),
                    condition=condition["condition"],
                    evaluation_tier=EvaluationTier(condition["evaluation_tier"]),
                    evaluation=condition["evaluation"],
                )
                for condition in row["break_conditions"]
            ),
        )
    return theses


def _open_reference_case(settings: Settings, evidence: EvidenceStore) -> PolicySet:
    """Create, ratify (FIXTURE), fund PAPER and activate the reference case — acceptance 1.

    The case is built through the real §5.1 flow: the fixture transcript is run through
    `conduct_interview`, `build_proposal` derives the seven §5.2 policies from it, and the *derived*
    policy set is what is proposed and ratified. Nothing here hand-writes a dial or a rail.
    """
    data = _load_fixture()
    answers = conduct_interview(_transcript(data))
    proposal = build_proposal(
        case_id=data["case_id"],
        answers=answers,
        theme_map=_theme_map(data),
        theses=_theses(data),
        parking_isin=data["parking"]["isin"],
        parking_symbol=data["parking"]["symbol"],
    )
    clock = FrozenClock(SETUP_AT)
    with connection(settings) as conn:
        journal = Journal(conn, clock=clock, evidence=evidence)
        cases = CaseService(conn, journal=journal, clock=clock)
        cases.create(data["case_id"], f"{data['theme']} reference case", theme=data["theme"])
        cases.begin_interview(data["case_id"])
        cases.propose(data["case_id"], proposal.policy_set)
        cases.ratify(data["case_id"], by=data["ratification"]["by"], kind=RatificationKind.FIXTURE)
        cases.fund(data["case_id"], FundingMode.PAPER, by=data["ratification"]["by"])
        cases.activate(data["case_id"])
        policy = cases.require_policy(data["case_id"])
        conn.commit()
    return policy


# ── the daily analyst session the scheduler runs (the composed loop of M5's parts) ───────────────


@dataclass(slots=True)
class _RunState:
    """Mutable state that persists across the ten `run_once` calls (the scheduler is stateless).

    The deployment queue in particular spans sessions: the SIP instalment enqueued on the 1st is
    still waiting when it is deployed on the 3rd, so the queue is carried here, not rebuilt per day.
    """

    queue: DeploymentQueue = field(default_factory=DeploymentQueue)
    parked: bool = False
    deployed: bool = False
    rail_blocked: bool = False
    sessions: list[date] = field(default_factory=list)


def _empty_book(case_id: str) -> Portfolio:
    """The thesis book T0 reviews before any deployment — no positions, so a clean heartbeat.

    The parked ETF is a CASH-sleeve holding, not a thesis position T0 reviews, so an empty book here
    is faithful, not a shortcut: on a day with nothing in the core/tactical sleeves the mechanical
    sweep finds nothing to act on and records the checks it performed (invariant #9).
    """
    return Portfolio(case_id=case_id, lots=(), cash=Decimal(0))


def _make_paper_session(
    *,
    case_id: str,
    policy: PolicySet,
    evidence: EvidenceStore,
    coordinator: StagingCoordinator,
    state: _RunState,
) -> Job:
    """Build the `paper_session` job: one day of the composed analyst loop, run by the scheduler.

    The job reads its trading date from the injected clock (B10), so advancing the runner's clock is
    all that moves the simulation forward. Each session, in order: fill anything the broker had
    staged for today; run the data-red interlock and the T0 sweep (heartbeat); on the SIP day park
    the instalment; on the fat-finger day put an oversized order through the rails; on the trigger
    day deploy the parked cash. Every write lands in one transaction committed at day's end.
    """
    gate = _AlwaysGreenGate()
    rails = policy.rails
    sip_amount = policy.capital_plan.sip_amount_inr

    def paper_session(context: JobContext) -> None:
        trading_date = context.clock.today()
        state.sessions.append(trading_date)
        with connection(context.settings) as conn:
            journal = Journal(conn, clock=context.clock, evidence=evidence)
            rail_engine = RailEngine(journal, clock=context.clock)
            cash = CashManager(policy, rail_engine, journal, clock=context.clock)
            t0 = T0Monitor(gate, journal, InMemoryEscalationQueue(), clock=context.clock)

            # Fill whatever the paper broker had staged to execute this session (invariant #5).
            coordinator.execute(trading_date)

            # The mechanical daily sweep, interlock first — a HEARTBEAT on a clean day.
            result = t0.run(
                trading_date,
                lambda: T0Inputs(
                    portfolio=_empty_book(case_id),
                    rails=rails,
                    case_value_series=(OPENING_CASH, OPENING_CASH),
                    holdings=(),
                    announcements=AnnouncementIndex([]),
                ),
                datasets=CORE_DATASETS,
            )
            assert result.outcome is T0Outcome.HEARTBEAT, result.reason

            book = Portfolio(case_id=case_id, lots=(), cash=OPENING_CASH)

            # The SIP instalment: queued and parked in the liquid ETF the same session (§5.6).
            if trading_date == SIP_DATE and not state.parked:
                state.queue = state.queue.enqueue(
                    CashSource.SIP_INSTALMENT, sip_amount, trading_date
                )
                parking = cash.park(
                    book,
                    sources={CashSource.SIP_INSTALMENT: sip_amount},
                    price=UNIT_PRICE,
                    trading_date=trading_date,
                )
                assert parking.parked and parking.order is not None
                coordinator.stage(parking.order.request, case_id=case_id, sleeve=Sleeve.CASH.value)
                state.parked = True

            # A deliberately oversized order: refused by the rails, journaled RAIL_BLOCK.
            if trading_date == RAIL_BLOCK_DATE and not state.rail_blocked:
                oversized = ProposedOrder(
                    request=OrderRequest(isin=OVERSIZED_ISIN, side=Side.BUY, quantity=200),
                    price=UNIT_PRICE,
                    sector=OVERSIZED_SECTOR,
                )
                assessment = rail_engine.guard_order(
                    oversized, book, rails, trading_date=trading_date, sleeve=Sleeve.TACTICAL
                )
                assert not assessment.allowed
                state.rail_blocked = True

            # A valid tactical trigger: deploy the parked cash into a position.
            if trading_date == DEPLOY_DATE and state.parked and not state.deployed:
                deploy_order = ProposedOrder(
                    request=OrderRequest(isin=DEPLOY_ISIN, side=Side.BUY, quantity=10),
                    price=UNIT_PRICE,
                    sector=DEPLOY_SECTOR,
                )
                authorization = authorize_buy(
                    case_id=case_id,
                    isin=DEPLOY_ISIN,
                    sleeve=Sleeve.TACTICAL,
                    rationale="momentum breakout on the compute leg — a valid tactical trigger",
                )
                deployment = cash.deploy(
                    deploy_order,
                    book,
                    authorization=authorization,
                    queue=state.queue,
                    trading_date=trading_date,
                    rationale="deploy the parked SIP instalment on a tactical opportunity (§5.6)",
                )
                assert deployment.placed and deployment.entry is not None
                state.queue = deployment.queue
                coordinator.stage(
                    deploy_order.request, case_id=case_id, sleeve=Sleeve.TACTICAL.value
                )
                state.deployed = True

            conn.commit()

    return Job(
        name="paper_session",
        cron="0 4 1 1 *",  # a valid crontab that will not fire on its own during the test
        fn=paper_session,
        timeout=timedelta(minutes=5),
        description="M5.13 paper-run daily analyst session",
    )


# ── database fixtures (the scratch-DB pattern from test_journal.py / test_recon_drill.py) ─────────


def _settings_for(dbname: str) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url, dbname))


@pytest.fixture(scope="session")
def paper_settings() -> Iterator[Settings]:
    """An empty scratch database with the schema applied, dropped at the end of the session."""
    admin = _settings_for("postgres")
    try:
        conn = connect(admin, autocommit=True)
    except psycopg.OperationalError as error:  # pragma: no cover - environment, not logic
        pytest.skip(f"postgres is not reachable — run `make up` first: {error}")
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    finally:
        conn.close()

    scratch = _settings_for(SCRATCH_DB)
    migrate(scratch, clock=FrozenClock(SETUP_AT))
    yield scratch

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture(scope="session")
def paper_run(
    paper_settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> tuple[PolicySet, _RunState]:
    """Open the reference case, then run the ten sessions through the scheduler. The whole run.

    Session-scoped and run exactly once: the reference case is created and committed here, so a
    second creation would raise `DuplicateCaseError`, and re-running the ten sessions per test would
    be pure waste. Every acceptance test reads the one committed run. Returns the ratified policy
    and the accumulated run state.
    """
    root = tmp_path_factory.mktemp("paper_run")
    evidence = EvidenceStore(root=root / "evidence")
    policy = _open_reference_case(paper_settings, evidence)

    run_clock = FrozenClock(RUN_START)
    market = InMemoryMarket(
        [date(2026, 9, day) for day in range(2, 13)],
        {
            (policy.cash_policy.parking_isin, date(2026, 9, 2)): _bar(
                policy.cash_policy.parking_isin, date(2026, 9, 2)
            ),
            (DEPLOY_ISIN, date(2026, 9, 4)): _bar(DEPLOY_ISIN, date(2026, 9, 4)),
        },
    )
    broker = SimBroker(
        clock=run_clock,
        cost_model=CostModel(load_rate_card()),
        market=market,
        opening_cash=OPENING_CASH,
        policy=FillPolicy(reference=ReferencePrice.OPEN),
    )
    coordinator = StagingCoordinator(
        broker=broker,
        kill_switch=KillSwitch(root / "killswitch.json", clock=run_clock),
        journal=InMemoryOrderJournal(),
        book=InternalBook(opening_cash=OPENING_CASH),
        clock=run_clock,
    )
    state = _RunState()
    job = _make_paper_session(
        case_id=policy.case_id,
        policy=policy,
        evidence=evidence,
        coordinator=coordinator,
        state=state,
    )
    runner = SchedulerRunner(
        JobRegistry([job]), settings=paper_settings, clock=run_clock, instance="paper:1"
    )

    for _ in SESSIONS:
        run = runner.run_once("paper_session")
        assert run.state is JobState.SUCCEEDED, f"{run.state}: {run.error}"
        run_clock.advance(timedelta(days=1))

    return policy, state


@pytest.fixture
def paper_journal(paper_settings: Settings, tmp_path: Path) -> Iterator[Journal]:
    """A read connection onto the committed run, for the assertions to query the journal.

    Its evidence store is a throwaway — the assertions read the journal (`count`/`entries`), which
    never touches evidence bytes; reconstruction is proved in `test_journal.py`.
    """
    with connection(paper_settings) as conn:
        yield Journal(conn, clock=FrozenClock(RUN_START), evidence=EvidenceStore(root=tmp_path))


# ── acceptance 1: created through the interview flow and ratified ─────────────────────────────────


def test_case_created_through_interview_flow_and_ratified(
    paper_run: tuple[PolicySet, _RunState], paper_settings: Settings, tmp_path: Path
) -> None:
    """The case is ratified (FIXTURE), funded PAPER, active — and its policies match the fixture."""
    policy, _ = paper_run
    data = _load_fixture()
    expected = data["expected_policies"]

    with connection(paper_settings) as conn:
        cases = CaseService(
            conn,
            journal=Journal(
                conn, clock=FrozenClock(SETUP_AT), evidence=EvidenceStore(root=tmp_path)
            ),
            clock=FrozenClock(SETUP_AT),
        )
        record = cases.require(policy.case_id)

    assert record.state is CaseState.ACTIVE
    assert record.funding_mode is FundingMode.PAPER
    assert policy.ratification is not None
    assert policy.ratification.kind is RatificationKind.FIXTURE  # B9: never real money

    # The seven §5.2 policies were derived from the interview, and match the reference case (B9).
    assert policy.rotation_dial.tactical_pct == Decimal(expected["rotation_dial_tactical_pct"])
    assert policy.rails.max_position_pct == Decimal(expected["max_position_pct"])
    assert policy.rails.max_sector_pct == Decimal(expected["max_sector_pct"])
    assert policy.rails.min_holdings == expected["min_holdings"]
    assert policy.rails.drawdown_review_pct == Decimal(expected["drawdown_review_pct"])
    assert policy.rails.max_order_value_inr == Decimal(expected["max_order_value_inr"])
    assert policy.monitoring.t2_cadence.value == expected["t2_cadence"]


# ── acceptance 2: ten consecutive sessions, a journal entry every day ─────────────────────────────


def test_ten_sessions_run_with_a_journal_entry_for_every_day(
    paper_run: tuple[PolicySet, _RunState], paper_journal: Journal
) -> None:
    """Every one of the ten trading dates carries an entry, and a T0 heartbeat each day."""
    policy, state = paper_run
    assert state.sessions == list(SESSIONS)  # ten consecutive sessions actually ran, in order

    for session in SESSIONS:
        entries = paper_journal.count(
            JournalFilter(case_id=policy.case_id, start=session, end=session)
        )
        assert entries >= 1, f"no journal entry for {session.isoformat()}"

    heartbeats = paper_journal.count(
        JournalFilter(case_id=policy.case_id, decision=Decision.HEARTBEAT)
    )
    assert heartbeats == len(SESSIONS)  # invariant #9: a heartbeat every day, including quiet ones


# ── acceptance 3: an injected oversized order is blocked and journaled RAIL_BLOCK ─────────────────


def test_oversized_order_is_blocked_by_rails_and_journaled(
    paper_run: tuple[PolicySet, _RunState], paper_journal: Journal
) -> None:
    """The fat-fingered order is refused, and a RAIL_BLOCK line by the RAILS actor records it."""
    policy, state = paper_run
    assert state.rail_blocked

    blocks = paper_journal.entries(
        JournalFilter(case_id=policy.case_id, decision=Decision.RAIL_BLOCK)
    )
    assert len(blocks) == 1
    (block,) = blocks
    assert block.trading_date == RAIL_BLOCK_DATE
    assert block.actor is Actor.RAILS
    assert block.isin == OVERSIZED_ISIN
    assert "MAX_ORDER_VALUE" in block.payload["rails"]


# ── acceptance 4: a SIP instalment is parked, then deployed ───────────────────────────────────────


def test_sip_instalment_is_parked_then_deployed(
    paper_run: tuple[PolicySet, _RunState], paper_journal: Journal
) -> None:
    """The instalment is parked in the ETF on the SIP day and deployed on the later trigger."""
    policy, state = paper_run
    assert state.parked and state.deployed
    assert state.queue.is_empty  # the parked instalment left the queue on deployment

    buys = paper_journal.entries(JournalFilter(case_id=policy.case_id, decision=Decision.BUY))

    parks = [b for b in buys if b.sleeve is Sleeve.CASH]
    assert len(parks) == 1
    (park,) = parks
    assert park.trading_date == SIP_DATE
    assert park.isin == policy.cash_policy.parking_isin  # parked in the ratified liquid ETF

    deploys = [b for b in buys if b.sleeve is Sleeve.TACTICAL]
    assert len(deploys) == 1
    (deploy,) = deploys
    assert deploy.trading_date == DEPLOY_DATE
    assert deploy.isin == DEPLOY_ISIN
    assert deploy.payload["deployed_inr"] == str(policy.capital_plan.sip_amount_inr)


if __name__ == "__main__":  # pragma: no cover - convenience for a direct run
    raise SystemExit(pytest.main([__file__, "-q"]))
