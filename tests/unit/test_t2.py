"""M6.5 — the T2 scheduled deep review (§5.4): case health, steering, bench, and proposals.

The three acceptance criteria, made concrete, each with an inversion that fails if the logic is
reversed (the CLAUDE.md rule for anything touching a decision boundary or a governance act):

1. **A run produces a case health report with per-thesis assessments and a refreshed bench.** A
   well-formed answer parses into a `T2Review` covering exactly the case's theses; the report holds
   one `ThesisAssessment` per holding and a bench drawn from A3's `ThemeMap` with the held names
   removed and the rest ranked by purity. An answer that skips or invents a thesis is malformed —
   retried, then escalated — never read loosely (`test_review_*`, `test_bench_*`,
   `test_malformed_*`, `test_missing_thesis_*`).
2. **A policy change it recommends becomes a PROPOSAL, never an applied change.** A recommended dial
   move returns a new policy-set version in `PROPOSAL` (superseding the ratified one), journals a
   `POLICY_PROPOSAL`, and leaves the ratified set untouched; no order is placed (`test_dial_*`).
3. **Rotation-steering updates stay inside the ratified dial.** The steering's `dial_tactical_pct`
   is the ratified percentage even when the model wished for another, and the tactical target
   is computed from the ratified dial — a tilt toward a name the case does not hold in the core is
   refused (`test_steering_*`).

Nothing here hits the network or a database: the LLM is a `StubLLM` keyed on the prompt digest, the
price card is the checked-in one, and the journal round-trips through the same recording connection
the other analyst suites use.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg.types.json import Json

from accounting.tokens import MeteredLLM, TokenPricer, load_price_card
from analyst.cases import (
    CapitalPlan,
    CashPolicy,
    ExitMenu,
    ExitStrategy,
    HorizonAndBenchmarks,
    MonitoringCadence,
    PolicySet,
    PolicyStatus,
    Ratification,
    RatificationKind,
    RiskRails,
    RotationDial,
    T2Cadence,
    TriggerSensitivity,
)
from analyst.journal import (
    Decision,
    EvidenceStore,
    Journal,
    TokenSpend,
    Verdict,
)
from analyst.journal.writer import _WRITE_COLUMNS
from analyst.llm import LLM, StubLLM, prompt_digest
from analyst.mapper import ProxyCandidate, ThemeMap, ValueChainStage
from analyst.mapper.purity import PurityEvidence, PurityEvidenceKind, score_purity
from analyst.monitor import (
    BenchCandidate,
    CaseHealth,
    CycleContext,
    T2Request,
    T2Review,
    T2Reviewer,
    ThesisAssessmentVerdict,
    next_due,
    render_review,
)
from analyst.monitor.t2 import SYSTEM_PROMPT, T2_MODEL, build_messages
from analyst.rails import Lot, Portfolio
from analyst.thesis import (
    BreakCondition,
    BreakConditionType,
    EvaluationTier,
    Thesis,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.store.db import Connection

CASE_ID = "AI_ROBOTICS"
THEME = "AI/Robotics"
TRADING_DATE = datetime(2026, 9, 2, 19, 30, tzinfo=IST).date()
DECIDED_AT = datetime(2026, 9, 2, 19, 30, tzinfo=IST)
AS_OF = TRADING_DATE

CORE_A = "INE001A01001"  # held core holding
CORE_B = "INE002A01009"  # held core holding
BENCH_X = "INE009A01021"  # candidate, not held — high purity
BENCH_Y = "INE467B01029"  # candidate, not held — lower purity
PARKING_ISIN = "INF109AA1234"
STAGE = "systems integration"


# ── recording connection (INSERT ... RETURNING, offline) ─────────────────────────────────────────


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _RecordingConnection:
    """Echoes an insert's parameters back as the returned row, like `INSERT ... RETURNING`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any]]] = []
        self.next_id = 1

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> _FakeCursor:
        unwrapped = [p.obj if isinstance(p, Json) else p for p in (params or ())]
        self.calls.append((sql, unwrapped))
        if sql.lstrip().upper().startswith("INSERT"):
            row = (self.next_id, *unwrapped)
            self.next_id += 1
            return _FakeCursor([row])
        return _FakeCursor([])

    @property
    def inserts(self) -> list[list[Any]]:
        return [params for sql, params in self.calls if sql.lstrip().upper().startswith("INSERT")]


def _insert_field(params: Sequence[Any], name: str) -> Any:
    return params[_WRITE_COLUMNS.index(name)]


def _decisions(conn: _RecordingConnection) -> list[str]:
    return [_insert_field(params, "decision") for params in conn.inserts]


# ── builders ─────────────────────────────────────────────────────────────────────────────────────


def example_policies(**overrides: Any) -> dict[str, Any]:
    policies: dict[str, Any] = {
        "capital_plan": CapitalPlan(sip_amount_inr=Decimal("10000"), day_of_month=1),
        "horizon": HorizonAndBenchmarks(
            horizon_years=5, benchmark_primary="NIFTY-TRI", benchmark_secondary="NIFTY-IT"
        ),
        "rotation_dial": RotationDial(tactical_pct=Decimal("30")),
        "rails": RiskRails(
            max_position_pct=Decimal("15"),
            max_sector_pct=Decimal("35"),
            min_holdings=8,
            drawdown_review_pct=Decimal("25"),
            max_order_value_inr=Decimal("50000"),
            max_order_pct_of_case=Decimal("10"),
        ),
        "exit_menu": ExitMenu(
            allowed=(ExitStrategy.STAGED, ExitStrategy.IMMEDIATE),
            default=ExitStrategy.STAGED,
            immediate_allowed_on=("integrity",),
        ),
        "cash_policy": CashPolicy(
            parking_isin=PARKING_ISIN,
            parking_symbol="LIQUIDCASE",
            deploy_within_sessions=5,
            min_deployment_inr=Decimal("5000"),
        ),
        "monitoring": MonitoringCadence(
            t2_cadence=T2Cadence.MONTHLY, t1_sensitivity=TriggerSensitivity.STANDARD
        ),
    }
    policies.update(overrides)
    return policies


def ratified_policy(**overrides: Any) -> PolicySet:
    proposal = PolicySet(case_id=CASE_ID, version=1, **example_policies(**overrides))
    return proposal.ratified_with(
        Ratification(
            by="vysh",
            at=DECIDED_AT,
            kind=RatificationKind.HUMAN,
            content_hash=proposal.content_hash,
        )
    )


def make_thesis(isin: str, purity: str) -> Thesis:
    proposal = Thesis(
        case_id=CASE_ID,
        isin=isin,
        version=1,
        driver="pure-play exposure to industrial automation capex",
        theme_purity=Decimal(purity),
        expected_evidence=("order book growth two consecutive quarters",),
        break_conditions=(
            BreakCondition(
                id="BC1",
                type=BreakConditionType.FUNDAMENTAL,
                condition="segment revenue falls two consecutive quarters",
                evaluation_tier=EvaluationTier.T1,
                evaluation="T1 on the quarterly results filing",
            ),
        ),
    )
    return proposal.ratified_with(
        Ratification(
            by="vysh",
            at=DECIDED_AT,
            kind=RatificationKind.HUMAN,
            content_hash=proposal.content_hash,
        )
    )


def make_theses() -> tuple[Thesis, ...]:
    return (make_thesis(CORE_A, "0.8"), make_thesis(CORE_B, "0.7"))


def make_portfolio() -> Portfolio:
    return Portfolio(
        case_id=CASE_ID,
        lots=(
            Lot(isin=CORE_A, sector="Automation", quantity=100, price=Decimal("500")),
            Lot(isin=CORE_B, sector="Automation", quantity=50, price=Decimal("400")),
        ),
        cash=Decimal("20000"),
    )


def _candidate(isin: str, name: str, theme_fraction: str) -> ProxyCandidate:
    """A proxy candidate whose disclosed purity is `theme_fraction` of a fully-disclosed book."""
    other = str(Decimal(1) - Decimal(theme_fraction))
    evidence = (
        PurityEvidence(
            kind=PurityEvidenceKind.SEGMENT_DISCLOSURE,
            source="FY26 segment note",
            label="Robotics",
            revenue_fraction=Decimal(theme_fraction),
            expresses_theme=True,
        ),
        PurityEvidence(
            kind=PurityEvidenceKind.SEGMENT_DISCLOSURE,
            source="FY26 segment note",
            label="Other",
            revenue_fraction=Decimal(other),
        ),
    )
    return ProxyCandidate(
        isin=isin,
        name=name,
        value_chain_stage=STAGE,
        purity=score_purity(isin, evidence),
    )


def make_theme_map() -> ThemeMap:
    """A refreshed theme map: one held name (CORE_A) plus two unheld bench candidates."""
    candidates = tuple(
        sorted(
            (
                _candidate(CORE_A, "Core A Ltd", "0.6"),
                _candidate(BENCH_X, "Bench X Ltd", "0.9"),
                _candidate(BENCH_Y, "Bench Y Ltd", "0.25"),
            ),
            key=lambda c: c.isin,
        )
    )
    return ThemeMap(
        theme=THEME,
        as_of=AS_OF,
        value_chain=(ValueChainStage(name=STAGE, description="assembling autonomous systems"),),
        candidates=candidates,
        universe_size=50,
        provider="stub",
        model=T2_MODEL,
        tokens=TokenSpend(tokens_in=100, tokens_out=20, cost_inr=Decimal("0.5")),
        rendered_prompt="theme map brief",
    )


def make_cycle() -> CycleContext:
    return CycleContext(
        sector_rs=(("Automation", Decimal("1.2")), ("IT", Decimal("0.9"))),
        breadth_pct=Decimal("55"),
        flows_note="FII net buyers, DII flat",
    )


def make_request() -> T2Request:
    return T2Request(
        policy_set=ratified_policy(),
        trading_date=TRADING_DATE,
        theses=make_theses(),
        portfolio=make_portfolio(),
        tactical_isins=frozenset(),
        cycle=make_cycle(),
        theme_map=make_theme_map(),
    )


def review_json(
    *,
    a_verdict: Verdict = Verdict.INTACT,
    b_verdict: Verdict = Verdict.INTACT,
    health: CaseHealth = CaseHealth.GREEN,
    recommended_tactical_pct: str | None = None,
    tilts: tuple[str, ...] = (),
) -> str:
    """A full, schema-valid T2 deep review as the model would return it (JSON)."""
    return T2Review(
        case_health=health,
        assessments=(
            ThesisAssessmentVerdict(isin=CORE_A, verdict=a_verdict, assessment="results in line"),
            ThesisAssessmentVerdict(isin=CORE_B, verdict=b_verdict, assessment="thesis holds"),
        ),
        cycle_read="automation leading, breadth healthy",
        theme_development="order pipeline broadening across integrators",
        steering_rationale="tilt new SIP money toward the stronger core name",
        steering_tilts=tilts,
        recommended_tactical_pct=(
            None if recommended_tactical_pct is None else Decimal(recommended_tactical_pct)
        ),
    ).model_dump_json()


def make_reviewer(
    llm: LLM, conn: _RecordingConnection, tmp_path: Path, *, max_attempts: int = 2
) -> T2Reviewer:
    clock = FrozenClock(DECIDED_AT)
    journal = Journal(
        cast(Connection, conn),
        clock=clock,
        evidence=EvidenceStore(tmp_path / "evidence"),
    )
    metered = MeteredLLM(llm, pricer=TokenPricer(load_price_card()), ledger=None, clock=clock)
    return T2Reviewer(metered, journal, clock=clock, model=T2_MODEL, max_attempts=max_attempts)


def stub_returning(reply: str, request: T2Request) -> StubLLM:
    """A StubLLM that returns `reply` for exactly the request the reviewer sends."""
    digest = prompt_digest(
        build_messages(render_review(request)), model=T2_MODEL, system=SYSTEM_PROMPT
    )
    return StubLLM({digest: reply}, synthesize_unknown=False)


def review_with(reply: str, conn: _RecordingConnection, tmp_path: Path) -> Any:
    request = make_request()
    reviewer = make_reviewer(stub_returning(reply, request), conn, tmp_path)
    return reviewer.review(request)


@pytest.fixture
def conn() -> _RecordingConnection:
    return _RecordingConnection()


# ── criterion 1: case health report with per-thesis assessments + refreshed bench ────────────────


def test_review_produces_per_thesis_assessments_and_a_bench(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    report = review_with(review_json(), conn, tmp_path)

    assert report.health is CaseHealth.GREEN
    assert {a.isin for a in report.thesis_assessments} == {CORE_A, CORE_B}
    assert all(a.assessment for a in report.thesis_assessments)
    # the refreshed bench excludes the held name and is ranked by purity, highest first
    assert [b.isin for b in report.bench] == [BENCH_X, BENCH_Y]
    assert report.bench[0].purity > report.bench[1].purity
    assert not report.escalated


def test_review_journals_a_heartbeat_and_a_line_per_thesis_with_cost(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    report = review_with(review_json(), conn, tmp_path)

    # a HEARTBEAT summary (invariant #9) plus one HOLD per intact thesis
    assert _decisions(conn) == [
        Decision.HEARTBEAT.value,
        Decision.HOLD.value,
        Decision.HOLD.value,
    ]
    # the review is a metered strong-model call: its rupee cost is recorded and non-zero
    assert report.token_spend.cost_inr > Decimal(0)
    assert report.model == T2_MODEL


def test_bench_never_contains_a_held_name(conn: _RecordingConnection, tmp_path: Path) -> None:
    report = review_with(review_json(), conn, tmp_path)
    held = {CORE_A, CORE_B}
    assert not (held & {b.isin for b in report.bench})


def test_a_review_missing_a_thesis_is_malformed(conn: _RecordingConnection, tmp_path: Path) -> None:
    partial = T2Review(
        case_health=CaseHealth.GREEN,
        assessments=(
            ThesisAssessmentVerdict(isin=CORE_A, verdict=Verdict.INTACT, assessment="ok"),
        ),
        cycle_read="ok",
        theme_development="ok",
        steering_rationale="ok",
    ).model_dump_json()
    report = review_with(partial, conn, tmp_path)
    assert report.escalated is True
    assert report.thesis_assessments == ()
    assert _decisions(conn) == [Decision.ESCALATE.value]


def test_malformed_answer_is_retried_then_escalated_never_interpreted(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    request = make_request()
    reviewer = make_reviewer(StubLLM(synthesize_unknown=True), conn, tmp_path, max_attempts=2)
    report = reviewer.review(request)

    assert report.escalated is True
    assert report.health is CaseHealth.RED
    assert report.thesis_assessments == ()  # nothing parsed; not interpreted loosely
    assert report.attempts == 2  # retried
    assert _decisions(conn) == [Decision.ESCALATE.value]
    # the failed attempts still cost tokens, and that cost is journaled, not lost
    assert _insert_field(conn.inserts[0], "cost_inr") > Decimal(0)
    # the bench and steering need no model, so they still populate the escalated report
    assert [b.isin for b in report.bench] == [BENCH_X, BENCH_Y]
    assert report.steering.dial_tactical_pct == Decimal("30")


def test_a_broken_thesis_is_escalated(conn: _RecordingConnection, tmp_path: Path) -> None:
    report = review_with(
        review_json(a_verdict=Verdict.BROKEN, health=CaseHealth.RED), conn, tmp_path
    )
    assert report.escalated is True
    assert report.broken_theses == (CORE_A,)
    # the summary heartbeat, an ESCALATE for the broken thesis, a HOLD for the intact one
    assert _decisions(conn) == [
        Decision.HEARTBEAT.value,
        Decision.ESCALATE.value,
        Decision.HOLD.value,
    ]
    # T2 escalates a break; it never places a SELL itself (invariant #6)
    assert Decision.SELL.value not in _decisions(conn)


# ── criterion 2: a recommended policy change is a PROPOSAL, never applied ─────────────────────────


def test_dial_recommendation_becomes_a_proposal_not_an_applied_change(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    request = make_request()
    reviewer = make_reviewer(
        stub_returning(review_json(recommended_tactical_pct="45"), request), conn, tmp_path
    )
    report = reviewer.review(request)

    assert len(report.proposals) == 1
    proposal = report.proposals[0]
    assert proposal.kind == "rotation_dial"
    assert proposal.from_tactical_pct == Decimal("30")
    assert proposal.to_tactical_pct == Decimal("45")
    # the proposed set is a NEW version in PROPOSAL, superseding the ratified one
    assert proposal.proposed_policy_set.status is PolicyStatus.PROPOSAL
    assert proposal.proposed_policy_set.version == request.policy_set.version + 1
    assert proposal.proposed_policy_set.supersedes_version == request.policy_set.version
    assert proposal.proposed_policy_set.rotation_dial.tactical_pct == Decimal("45")
    # the ratified set in force is UNTOUCHED — the change was proposed, not applied
    assert request.policy_set.status is PolicyStatus.RATIFIED
    assert request.policy_set.rotation_dial.tactical_pct == Decimal("30")
    # it is journaled as a POLICY_PROPOSAL, and no order was placed
    assert Decision.POLICY_PROPOSAL.value in _decisions(conn)
    assert Decision.BUY.value not in _decisions(conn)
    assert Decision.SELL.value not in _decisions(conn)


def test_no_dial_change_produces_no_proposal(conn: _RecordingConnection, tmp_path: Path) -> None:
    report = review_with(review_json(recommended_tactical_pct=None), conn, tmp_path)
    assert report.proposals == ()
    assert Decision.POLICY_PROPOSAL.value not in _decisions(conn)


def test_recommending_the_ratified_dial_is_not_a_change(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    # recommending exactly the dial already in force is not a policy change
    report = review_with(review_json(recommended_tactical_pct="30"), conn, tmp_path)
    assert report.proposals == ()
    assert Decision.POLICY_PROPOSAL.value not in _decisions(conn)


def test_an_out_of_range_dial_recommendation_is_malformed(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    # 120% is not a valid tactical percentage — crafted as raw JSON the schema will reject, so the
    # answer is malformed, retried, then escalated (never silently clamped).
    raw = (
        '{"case_health": "GREEN", '
        '"assessments": ['
        f'{{"isin": "{CORE_A}", "verdict": "INTACT", "assessment": "ok"}}, '
        f'{{"isin": "{CORE_B}", "verdict": "INTACT", "assessment": "ok"}}], '
        '"cycle_read": "ok", "theme_development": "ok", '
        '"steering_rationale": "ok", "steering_tilts": [], '
        '"recommended_tactical_pct": "120"}'
    )
    report = review_with(raw, conn, tmp_path)
    assert report.escalated is True
    assert report.thesis_assessments == ()


# ── criterion 3: rotation-steering stays inside the ratified dial ─────────────────────────────────


def test_steering_uses_the_ratified_dial_not_the_recommended_one(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    # the model wants 45% tactical, but steering is bounded by the RATIFIED 30% dial
    report = review_with(review_json(recommended_tactical_pct="45"), conn, tmp_path)
    assert report.steering.dial_tactical_pct == Decimal("30")
    # the tactical target is 30% of case value (2 lots + cash), never 45%
    portfolio = make_portfolio()
    assert report.steering.tactical_target_inr == portfolio.total_value * Decimal("30") / Decimal(
        100
    )


def test_steering_tilts_within_the_core_are_carried(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    report = review_with(review_json(tilts=(CORE_A,)), conn, tmp_path)
    assert report.steering.core_tilts == (CORE_A,)


def test_a_tilt_toward_a_name_not_held_in_core_is_rejected(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    # tilting toward a bench name would ADD a core member — a ratified-thesis step, not a tilt
    report = review_with(review_json(tilts=(BENCH_X,)), conn, tmp_path)
    assert report.escalated is True
    assert report.thesis_assessments == ()


# ── determinism, request validation, cadence ─────────────────────────────────────────────────────


def test_deterministic_under_stub_llm(tmp_path: Path) -> None:
    reply = review_json()
    first = review_with(reply, _RecordingConnection(), tmp_path / "a")
    second = review_with(reply, _RecordingConnection(), tmp_path / "b")

    assert first.health is second.health
    assert first.thesis_assessments == second.thesis_assessments
    assert first.bench == second.bench
    assert first.token_spend == second.token_spend


def test_a_deep_review_refuses_an_unratified_policy_set() -> None:
    proposal = PolicySet(case_id=CASE_ID, version=1, **example_policies())
    with pytest.raises(ValueError, match="RATIFIED"):
        T2Request(
            policy_set=proposal,
            trading_date=TRADING_DATE,
            theses=make_theses(),
            portfolio=make_portfolio(),
            tactical_isins=frozenset(),
            cycle=make_cycle(),
            theme_map=make_theme_map(),
        )


def test_next_due_honours_the_ratified_cadence() -> None:
    # never reviewed → due; monthly cadence spans ~30 days
    assert next_due(T2Cadence.MONTHLY, None, on=TRADING_DATE) is True
    assert next_due(T2Cadence.MONTHLY, date(2026, 8, 20), on=TRADING_DATE) is False  # 13 days
    assert next_due(T2Cadence.MONTHLY, date(2026, 8, 1), on=TRADING_DATE) is True  # 32 days
    assert next_due(T2Cadence.WEEKLY, date(2026, 8, 29), on=TRADING_DATE) is False  # 4 days


def test_bench_candidate_carries_its_stage() -> None:
    bench = BenchCandidate(
        isin=BENCH_X, name="Bench X Ltd", purity=Decimal("0.9"), value_chain_stage=STAGE
    )
    assert bench.isin == BENCH_X
    assert bench.value_chain_stage == STAGE
