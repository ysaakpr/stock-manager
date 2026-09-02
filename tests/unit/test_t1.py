"""M6.4 — the T1 triggered LLM review (§5.4), its verdict schema, and its policy gate.

The three acceptance criteria, made concrete, each with an inversion that fails if the logic is
reversed (the CLAUDE.md rule for anything touching rails, costs, or a decision boundary):

1. **Verdicts are schema-validated per break condition and journaled with token/cost.** A
   well-formed answer parses into a `T1Verdict` carrying one verdict per condition; the journal
   line records those evaluations, the model, and a non-zero rupee cost. An answer that is not
   JSON, or that skips or invents a break condition, is *malformed* — retried, then escalated to
   the human — never read loosely (`test_intact_*`, `test_malformed_*`, `test_missing_condition_*`,
   `test_retry_then_valid`).
2. **A proposed action outside ratified policy is rejected by code before reaching rails.** An exit
   strategy off the ratified menu, an IMMEDIATE exit on a break the menu does not unlock, an exit
   with no break, and a HOLD on a BROKEN core are each rejected by `validate_action` and escalated —
   `exit_triggered` stays False and no order path is reached. A BROKEN verdict with an in-policy
   exit *does* trigger the A7 exit path (`test_action_*`, `test_broken_*`).
3. **Runs deterministically under StubLLM.** The same request and the same canned reply produce a
   byte-identical verdict, outcome and token cost across two reviews (`test_deterministic_*`).

Nothing here hits the network or a database: the LLM is a `StubLLM` keyed on the prompt digest, the
price card is the checked-in one, and the journal round-trips through the same recording connection
the other analyst suites use.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg.types.json import Json

from accounting.tokens import MeteredLLM, TokenPricer, load_price_card
from analyst.cases.policies import ExitMenu, ExitStrategy
from analyst.journal import (
    Actor,
    Decision,
    EvidenceStore,
    Journal,
    Verdict,
)
from analyst.journal.writer import _WRITE_COLUMNS
from analyst.llm import (
    DEFAULT_MAX_TOKENS,
    LLM,
    LLMResponse,
    Message,
    StopReason,
    StubLLM,
    ToolSpec,
    Usage,
    prompt_digest,
)
from analyst.monitor import (
    BundleBuilder,
    BundleRequest,
    PriceFact,
    T0Check,
    T0Flag,
    T1Outcome,
    T1Request,
    T1Reviewer,
    T1Verdict,
    build_messages,
    validate_action,
)
from analyst.monitor.t1 import SYSTEM_PROMPT, T1_MODEL
from analyst.monitor.verdicts import (
    BreakConditionVerdict,
    MalformedVerdictError,
    PolicyViolationError,
    ProposedAction,
    ProposedActionKind,
)
from analyst.thesis import (
    BreakCondition,
    BreakConditionType,
    EvaluationTier,
    Thesis,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.store.db import Connection

CASE_ID = "AI_ROBOTICS"
ISIN = "INE001A01001"
OTHER_ISIN = "INE002A01009"
TRADING_DATE = datetime(2026, 8, 7, 19, 30, tzinfo=IST).date()
DECIDED_AT = datetime(2026, 8, 7, 19, 30, tzinfo=IST)


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


def make_thesis() -> Thesis:
    """A minimal ratifiable thesis with a fundamental (T1) and an integrity (T0) break condition."""
    return Thesis(
        case_id=CASE_ID,
        isin=ISIN,
        version=1,
        driver="pure-play exposure to industrial automation capex",
        theme_purity=Decimal("0.8"),
        expected_evidence=("order book growth two consecutive quarters",),
        break_conditions=(
            BreakCondition(
                id="BC1",
                type=BreakConditionType.FUNDAMENTAL,
                condition="segment revenue falls two consecutive quarters",
                evaluation_tier=EvaluationTier.T1,
                evaluation="T1 on the quarterly results filing",
            ),
            BreakCondition(
                id="BC3",
                type=BreakConditionType.INTEGRITY,
                condition="auditor resignation disclosed",
                evaluation_tier=EvaluationTier.T0,
                evaluation="T0 keyword on the announcement feed",
            ),
        ),
    )


def make_flag() -> T0Flag:
    return T0Flag(
        check=T0Check.ANNOUNCEMENT,
        isin=ISIN,
        case_id=CASE_ID,
        summary="break condition BC1 keyword hit on 1 announcement: Quarterly results",
        detail={"break_condition_id": "BC1", "hits": "1"},
    )


def make_price() -> PriceFact:
    return PriceFact(
        isin=ISIN,
        label="close",
        value=Decimal("1234.55"),
        as_of=TRADING_DATE,
        knowable_at=datetime(2026, 8, 7, 15, 30, tzinfo=IST),
    )


def make_bundle_request() -> BundleRequest:
    return BundleRequest(
        case_id=CASE_ID,
        isin=ISIN,
        trading_date=TRADING_DATE,
        flag=make_flag(),
        thesis=make_thesis(),
        prices=(make_price(),),
        actor=Actor.T1,
    )


def full_exit_menu() -> ExitMenu:
    """Every strategy ratified; IMMEDIATE unlocked on integrity breaks (§5.6)."""
    return ExitMenu(
        allowed=(
            ExitStrategy.STAGED,
            ExitStrategy.IMMEDIATE,
            ExitStrategy.EXIT_AND_REDEPLOY,
        ),
        default=ExitStrategy.STAGED,
        immediate_allowed_on=("integrity",),
    )


def staged_only_menu() -> ExitMenu:
    """Only staged exits ratified — IMMEDIATE is off the menu entirely."""
    return ExitMenu(allowed=(ExitStrategy.STAGED,), default=ExitStrategy.STAGED)


def verdict_json(
    *,
    bc1: Verdict = Verdict.INTACT,
    bc3: Verdict = Verdict.INTACT,
    action: ProposedAction | None = None,
    isin: str = ISIN,
    summary: str = "thesis reviewed",
) -> str:
    """A full, schema-valid T1 verdict as the model would return it (JSON)."""
    if action is None:
        action = ProposedAction(kind=ProposedActionKind.HOLD, rationale="nothing changed")
    return T1Verdict(
        isin=isin,
        verdicts=(
            BreakConditionVerdict(id="BC1", verdict=bc1, observed="results in line"),
            BreakConditionVerdict(id="BC3", verdict=bc3, observed="no integrity flag"),
        ),
        proposed_action=action,
        summary=summary,
    ).model_dump_json()


def make_reviewer(
    llm: LLM,
    conn: _RecordingConnection,
    tmp_path: Path,
    *,
    max_attempts: int = 2,
) -> T1Reviewer:
    clock = FrozenClock(DECIDED_AT)
    journal = Journal(
        cast(Connection, conn),
        clock=clock,
        evidence=EvidenceStore(tmp_path / "evidence"),
    )
    metered = MeteredLLM(llm, pricer=TokenPricer(load_price_card()), ledger=None, clock=clock)
    return T1Reviewer(metered, journal, clock=clock, model=T1_MODEL, max_attempts=max_attempts)


def stub_returning(reply: str) -> StubLLM:
    """A StubLLM that returns `reply` for exactly the request the reviewer sends."""
    built = BundleBuilder().build(make_bundle_request())
    digest = prompt_digest(
        build_messages(built.rendered_prompt), model=T1_MODEL, system=SYSTEM_PROMPT
    )
    return StubLLM({digest: reply}, synthesize_unknown=False)


def review_with(reply: str, conn: _RecordingConnection, tmp_path: Path, menu: ExitMenu) -> Any:
    built = BundleBuilder().build(make_bundle_request())
    reviewer = make_reviewer(stub_returning(reply), conn, tmp_path)
    return reviewer.review(T1Request(built=built, thesis=make_thesis(), exit_menu=menu))


@pytest.fixture
def conn() -> _RecordingConnection:
    return _RecordingConnection()


# ── criterion 1: schema-validated verdicts, journaled with token/cost ────────────────────────────


def test_intact_verdict_journals_a_hold_with_evaluations_model_and_cost(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    result = review_with(verdict_json(), conn, tmp_path, full_exit_menu())

    assert result.outcome is T1Outcome.INTACT
    assert result.verdict is not None
    assert {v.id for v in result.verdict.verdicts} == {"BC1", "BC3"}
    assert result.exit_triggered is False

    # one HOLD entry, carrying the per-condition evaluations, the model and a non-zero rupee cost.
    assert _decisions(conn) == [Decision.HOLD.value]
    params = conn.inserts[0]
    evaluated = _insert_field(params, "break_conditions_evaluated")
    assert {e["id"] for e in evaluated} == {"BC1", "BC3"}
    assert _insert_field(params, "model") == T1_MODEL
    assert _insert_field(params, "cost_inr") > Decimal(0)
    assert _insert_field(params, "tokens_in") > 0
    assert result.token_spend.cost_inr > Decimal(0)


def test_the_journalled_evidence_ref_reconstructs_the_exact_bundle(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    built = BundleBuilder().build(make_bundle_request())
    reviewer = make_reviewer(stub_returning(verdict_json()), conn, tmp_path)
    reviewer.review(T1Request(built=built, thesis=make_thesis(), exit_menu=full_exit_menu()))
    ref = _insert_field(conn.inserts[0], "evidence_snapshot_ref")
    assert ref == built.ref  # the entry names exactly the bundle that was built


def test_malformed_answer_is_retried_then_escalated_never_interpreted(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    # A synthesize-mode stub returns non-JSON prose for any prompt — malformed every attempt.
    built = BundleBuilder().build(make_bundle_request())
    reviewer = make_reviewer(StubLLM(synthesize_unknown=True), conn, tmp_path, max_attempts=2)
    result = reviewer.review(
        T1Request(built=built, thesis=make_thesis(), exit_menu=full_exit_menu())
    )

    assert result.outcome is T1Outcome.ESCALATED
    assert result.verdict is None  # nothing parsed; not interpreted loosely
    assert result.attempts == 2  # retried
    assert result.exit_triggered is False
    assert _decisions(conn) == [Decision.ESCALATE.value]
    # the failed attempts still cost tokens, and that cost is journaled, not lost
    assert _insert_field(conn.inserts[0], "cost_inr") > Decimal(0)


def test_a_verdict_missing_a_break_condition_is_malformed(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    partial = T1Verdict(
        isin=ISIN,
        verdicts=(BreakConditionVerdict(id="BC1", verdict=Verdict.INTACT, observed="ok"),),
        proposed_action=ProposedAction(kind=ProposedActionKind.HOLD, rationale="ok"),
        summary="partial",
    ).model_dump_json()
    result = review_with(partial, conn, tmp_path, full_exit_menu())
    assert result.outcome is T1Outcome.ESCALATED
    assert result.verdict is None


def test_retry_then_valid_produces_a_verdict(conn: _RecordingConnection, tmp_path: Path) -> None:
    class _FlakyLLM:
        """Malformed on the first call, a valid verdict on the second."""

        def __init__(self) -> None:
            self.calls = 0

        def complete(
            self,
            messages: Sequence[Message],
            *,
            model: str,
            tools: Sequence[ToolSpec] = (),
            system: str | None = None,
            max_tokens: int = DEFAULT_MAX_TOKENS,
        ) -> LLMResponse:
            self.calls += 1
            text = "not json at all" if self.calls == 1 else verdict_json()
            return LLMResponse(
                provider="stub",
                model=model,
                text=text,
                usage=Usage(input_tokens=100, output_tokens=20),
                stop_reason=StopReason.END_TURN,
            )

    built = BundleBuilder().build(make_bundle_request())
    reviewer = make_reviewer(cast(LLM, _FlakyLLM()), conn, tmp_path, max_attempts=2)
    result = reviewer.review(
        T1Request(built=built, thesis=make_thesis(), exit_menu=full_exit_menu())
    )
    assert result.attempts == 2
    assert result.outcome is T1Outcome.INTACT
    assert result.verdict is not None


# ── criterion 2: out-of-policy actions are rejected in code before rails ──────────────────────────


def test_broken_with_in_policy_staged_exit_triggers_the_a7_path(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    action = ProposedAction(
        kind=ProposedActionKind.EXIT,
        exit_strategy=ExitStrategy.STAGED,
        rationale="fundamental break; stage the exit",
    )
    result = review_with(
        verdict_json(bc1=Verdict.BROKEN, action=action), conn, tmp_path, full_exit_menu()
    )
    assert result.outcome is T1Outcome.BROKEN
    assert result.exit_triggered is True
    assert result.proposed_action_kind is ProposedActionKind.EXIT
    assert result.exit_strategy == ExitStrategy.STAGED.value
    # T1 escalates the exit to A7; it never places a BUY/SELL itself (invariant #6).
    assert _decisions(conn) == [Decision.ESCALATE.value]
    assert Decision.SELL.value not in _decisions(conn)


def test_exit_strategy_off_the_menu_is_rejected_before_rails(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    action = ProposedAction(
        kind=ProposedActionKind.EXIT,
        exit_strategy=ExitStrategy.IMMEDIATE,
        rationale="wants immediate",
    )
    # staged-only menu: IMMEDIATE is not ratified at all.
    result = review_with(
        verdict_json(bc1=Verdict.BROKEN, action=action), conn, tmp_path, staged_only_menu()
    )
    assert result.outcome is T1Outcome.ESCALATED
    assert result.exit_triggered is False
    assert result.proposed_action_kind is None  # the action did not survive the gate
    assert result.rejection is not None
    assert _decisions(conn) == [Decision.ESCALATE.value]


def test_immediate_exit_not_unlocked_for_the_break_is_rejected(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    # IMMEDIATE is on the menu but only unlocked for integrity; the break is BC1 (fundamental).
    action = ProposedAction(
        kind=ProposedActionKind.EXIT,
        exit_strategy=ExitStrategy.IMMEDIATE,
        rationale="wants immediate on a fundamental break",
    )
    result = review_with(
        verdict_json(bc1=Verdict.BROKEN, action=action), conn, tmp_path, full_exit_menu()
    )
    assert result.outcome is T1Outcome.ESCALATED
    assert result.exit_triggered is False


def test_immediate_exit_unlocked_on_an_integrity_break_is_allowed(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    # BC3 is the integrity condition; the full menu unlocks IMMEDIATE on integrity.
    action = ProposedAction(
        kind=ProposedActionKind.EXIT,
        exit_strategy=ExitStrategy.IMMEDIATE,
        rationale="integrity break; exit now",
    )
    result = review_with(
        verdict_json(bc3=Verdict.BROKEN, action=action), conn, tmp_path, full_exit_menu()
    )
    assert result.outcome is T1Outcome.BROKEN
    assert result.exit_triggered is True


def test_hold_on_a_broken_core_is_rejected(conn: _RecordingConnection, tmp_path: Path) -> None:
    # The model tries to sit on a break — §5.5 says membership changes on a break.
    result = review_with(verdict_json(bc1=Verdict.BROKEN), conn, tmp_path, full_exit_menu())
    assert result.outcome is T1Outcome.ESCALATED
    assert result.exit_triggered is False


def test_validate_action_rejects_an_exit_with_no_break() -> None:
    thesis = make_thesis()
    verdict = T1Verdict(
        isin=ISIN,
        verdicts=(
            BreakConditionVerdict(id="BC1", verdict=Verdict.WEAKENED, observed="soft"),
            BreakConditionVerdict(id="BC3", verdict=Verdict.INTACT, observed="ok"),
        ),
        proposed_action=ProposedAction(
            kind=ProposedActionKind.EXIT,
            exit_strategy=ExitStrategy.STAGED,
            rationale="exit anyway",
        ),
        summary="weakened",
    )
    with pytest.raises(PolicyViolationError):
        validate_action(verdict, thesis, full_exit_menu())


def test_weakened_resolves_to_a_journaled_hold_not_an_exit(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    result = review_with(verdict_json(bc1=Verdict.WEAKENED), conn, tmp_path, full_exit_menu())
    assert result.outcome is T1Outcome.WEAKENED
    assert result.exit_triggered is False
    assert _decisions(conn) == [Decision.HOLD.value]


# ── criterion 3: deterministic under StubLLM ─────────────────────────────────────────────────────


def test_deterministic_under_stub_llm(tmp_path: Path) -> None:
    reply = verdict_json()
    first = review_with(reply, _RecordingConnection(), tmp_path / "a", full_exit_menu())
    second = review_with(reply, _RecordingConnection(), tmp_path / "b", full_exit_menu())

    assert first.outcome is second.outcome
    assert first.verdict == second.verdict
    assert first.token_spend == second.token_spend
    assert first.exit_triggered == second.exit_triggered


def test_a_wrong_isin_in_the_verdict_is_malformed(
    conn: _RecordingConnection, tmp_path: Path
) -> None:
    result = review_with(verdict_json(isin=OTHER_ISIN), conn, tmp_path, full_exit_menu())
    assert result.outcome is T1Outcome.ESCALATED
    assert result.verdict is None


def test_parse_verdict_tolerates_a_code_fence() -> None:
    from analyst.monitor.verdicts import parse_verdict

    fenced = "```json\n" + verdict_json() + "\n```"
    parsed = parse_verdict(fenced)
    assert parsed.isin == ISIN


def test_parse_verdict_rejects_non_json() -> None:
    from analyst.monitor.verdicts import parse_verdict

    with pytest.raises(MalformedVerdictError):
        parse_verdict("the thesis looks fine to me")
