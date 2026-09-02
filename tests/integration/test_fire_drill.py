"""M6.7 — the thesis-break fire drill, end to end and repeatable under StubLLM (§5.4/§5.7).

This is the M6 gate drill wired through the real modules on a real Postgres journal: an integrity
disclosure is *injected* onto a held name, and the whole monitoring path is exercised in the order
production runs it —

    interlock (green) → T0 mechanical sweep → announcement keyword hit → ESCALATE + queue (T0→T1)
    → evidence bundle → T1 strong-model review under StubLLM → verdict per break condition
    → in-policy action, validated in code, journaled → evidence pack reads it back.

Nothing here is a mock of the pipeline: `T0Monitor` runs its real checks behind the real data-red
interlock, `T1Reviewer` makes a metered `StubLLM` call priced on the checked-in card and journals
the decision through the real `Journal`, and `generate_pack` reconstructs the drill from those rows
alone. The three M6.7 acceptance criteria are each proved, each with the inversion that would fail
if the logic were reversed (the CLAUDE.md rule for anything touching a decision boundary):

1. **Injected news escalates T0→T1, produces a verdict, and results in a journaled in-policy
   action.** The T0 sweep escalates the disclosure (its outcome is `ESCALATED`, the flag is queued
   for T1, and an `ESCALATE` row is journaled); T1 then reads the bundle, returns a schema-valid
   verdict per break condition, and — because the break is BROKEN on a core holding — journals an
   `ESCALATE` carrying an EXIT directive that `validate_action` cleared against the ratified menu.
   The inversion: an out-of-policy exit (an IMMEDIATE the menu does not unlock) never triggers the
   A7 path and is journaled as a human escalation instead (`test_out_of_policy_action_*`).

2. **The drill is automated and repeatable.** It is a pytest that touches no network and reads no
   wall clock, and running it twice into two independent case journals produces the same verdict,
   the same outcome, the same in-policy action and byte-identical token spend
   (`test_the_drill_is_repeatable`).

3. **Token cost per decision is visible in the journal line.** The T1 `ESCALATE` row carries its
   model and a non-zero `TokenSpend`, and the evidence pack's cost-burn section attributes that
   spend to the T1 tier — so "what did this decision cost" is answerable from the journal alone
   (`test_token_cost_per_decision_is_visible_in_the_journal`).

Needs the docker postgres (`make up`); skips loudly if it is unreachable, as `test_journal.py`,
`test_evidence_pack.py` and `test_paper_run_10_sessions.py` do. Time is frozen and injected (B10);
money is `Decimal`; identity is ISIN (invariant #2).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import psycopg
import pytest

from accounting.tokens import MeteredLLM, TokenPricer, load_price_card
from analyst.cases.policies import ExitMenu, ExitStrategy, RiskRails
from analyst.journal import (
    Actor,
    Decision,
    EvidenceStore,
    Journal,
    JournalEntry,
    JournalFilter,
    NavPoint,
    PackTrigger,
    PackValuation,
    Sleeve,
    Verdict,
    generate_pack,
    render_markdown,
)
from analyst.llm import StubLLM, Usage, prompt_digest
from analyst.llm.stub import StubReply
from analyst.monitor import (
    BuiltBundle,
    BundleBuilder,
    BundleRequest,
    InMemoryEscalationQueue,
    KeywordWatch,
    PriceFact,
    T0Holding,
    T0Inputs,
    T0Monitor,
    T0Outcome,
    T1Outcome,
    T1Request,
    T1Result,
    T1Reviewer,
    build_messages,
)
from analyst.monitor.t1 import SYSTEM_PROMPT, T1_MODEL
from analyst.monitor.verdicts import (
    BreakConditionVerdict,
    ProposedAction,
    ProposedActionKind,
    T1Verdict,
)
from analyst.rails import Lot, Portfolio
from analyst.thesis import (
    BreakCondition,
    BreakConditionType,
    EvaluationTier,
    Thesis,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.announcements import AnnouncementRow
from dataplatform.ingest.indices import TriPoint, TriSeries
from dataplatform.query import AnnouncementIndex, KeywordQuery
from dataplatform.store.db import connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

#: Pid-suffixed so concurrent build agents do not drop each other's scratch DB (cf test_journal).
SCRATCH_DB: Final = f"trading_m6_7_fire_drill_{os.getpid()}"

ISIN: Final = "INE001A01001"
SECTOR: Final = "industrial automation"

#: The disclosure keyword set for the drill's break condition — §5.3 BC3, an integrity event. A hit
#: on it is what §5.6 lets exit *immediately*, so the drill exercises the fastest escalation path.
BC3_QUERY: Final = KeywordQuery(
    any_of=("auditor resign", "resignation of auditor"), none_of=("reappoint",)
)

WINDOW_START: Final = date(2026, 8, 1)
SIP_DATE: Final = date(2026, 8, 3)
DRILL_DATE: Final = date(2026, 8, 7)
AS_OF: Final = date(2026, 8, 31)
WINDOW_END: Final = AS_OF

#: A frozen wall clock for every write, so the run's `ts`/`recorded_at` are unambiguous.
RUN_AT: Final = datetime(2026, 8, 31, 18, 30, tzinfo=IST)
#: The exchange dissemination instant of the injected disclosure — its natural PIT, on the drill
#: date and knowable before the review.
DISCLOSED_AT: Final = datetime(2026, 8, 7, 10, 0, tzinfo=IST)

SIP_AMOUNT: Final = Decimal("10000")
CASE_VALUE: Final = Decimal("100000")

#: Pinned so the recorded rupee cost does not move when the bundle is reworded — the drill asserts a
#: specific, non-zero, deterministic per-decision cost. Independent of the prompt, which is what
#: makes the repeatability comparison byte-identical across two differently-named cases.
STUB_USAGE: Final = Usage(input_tokens=1500, output_tokens=200)


# ── builders (the ratified thesis, its rails and exit menu, the injected disclosure) ─────────────


def make_thesis(case_id: str) -> Thesis:
    """A minimal thesis carrying a T1 fundamental break and a T0 integrity break (§5.3)."""
    return Thesis(
        case_id=case_id,
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
                evaluation="T0 keyword on the announcement feed → immediate T1",
            ),
        ),
    )


def make_rails() -> RiskRails:
    """Rails wide enough that only the injected disclosure fires — the drill isolates one path."""
    return RiskRails(
        max_position_pct=Decimal("100"),
        max_sector_pct=Decimal("100"),
        min_holdings=1,
        drawdown_review_pct=Decimal("25"),
        max_order_value_inr=Decimal("1000000"),
        max_order_pct_of_case=Decimal("100"),
    )


def full_exit_menu() -> ExitMenu:
    """Every strategy ratified; IMMEDIATE unlocked on integrity breaks (§5.6)."""
    return ExitMenu(
        allowed=(ExitStrategy.STAGED, ExitStrategy.IMMEDIATE, ExitStrategy.EXIT_AND_REDEPLOY),
        default=ExitStrategy.STAGED,
        immediate_allowed_on=("integrity",),
    )


def make_portfolio(case_id: str) -> Portfolio:
    """A one-name book marked at the case value — no rail is near its cap."""
    return Portfolio(
        case_id=case_id,
        lots=(Lot(isin=ISIN, sector=SECTOR, quantity=100, price=Decimal("1000")),),
        cash=Decimal("0"),
    )


def injected_disclosure() -> AnnouncementRow:
    """The fire drill's injected news: an auditor-resignation disclosure on the held name (§5.3)."""
    return AnnouncementRow(
        ts=DISCLOSED_AT,
        source="nse_announcements",
        isin=ISIN,
        subject="Resignation of Auditor",
        category="Board Meeting / Auditor",
        body="The company informs the exchange of the resignation of auditor, effective at once.",
        source_ref="FIREDRILL-1",
    )


def make_t0_inputs(case_id: str) -> T0Inputs:
    """Everything the T0 sweep reads for the drill: the book, the rails, and the disclosure feed."""
    return T0Inputs(
        portfolio=make_portfolio(case_id),
        rails=make_rails(),
        case_value_series=(CASE_VALUE,),
        holdings=(
            T0Holding(
                isin=ISIN,
                case_id=case_id,
                sector=SECTOR,
                sleeve=Sleeve.CORE,
                keyword_watches=(KeywordWatch(break_condition_id="BC3", query=BC3_QUERY),),
            ),
        ),
        announcements=AnnouncementIndex((injected_disclosure(),)),
    )


def price_facts() -> tuple[PriceFact, ...]:
    """The price context the T1 bundle carries — knowable before the review (PIT)."""
    return (
        PriceFact(
            isin=ISIN,
            label="close",
            value=Decimal("1000"),
            as_of=DRILL_DATE,
            knowable_at=datetime(2026, 8, 7, 15, 30, tzinfo=IST),
        ),
    )


def broken_verdict_json() -> str:
    """The model's answer for the drill: BC3 BROKEN, an in-policy IMMEDIATE exit (integrity)."""
    return T1Verdict(
        isin=ISIN,
        verdicts=(
            BreakConditionVerdict(id="BC1", verdict=Verdict.INTACT, observed="results in line"),
            BreakConditionVerdict(
                id="BC3", verdict=Verdict.BROKEN, observed="statutory auditor resigned"
            ),
        ),
        proposed_action=ProposedAction(
            kind=ProposedActionKind.EXIT,
            exit_strategy=ExitStrategy.IMMEDIATE,
            rationale="integrity break; exit immediately per the ratified menu",
        ),
        summary="auditor resignation confirms the integrity break condition",
    ).model_dump_json()


# ── the drill, wired through the real modules ─────────────────────────────────────────────────────


class _AlwaysGreen:
    """A `GreenGate` that reports the trading date green — the drill is about a break, not red data.

    The red-data short-circuit is proved in the T0 suite (`test_t0.py`); here the interlock must say
    green so the sweep runs at all, and this makes that precondition explicit and injected.
    """

    def __call__(self, trading_date: date) -> _GreenStatus:
        return _GreenStatus()


@dataclass(frozen=True, slots=True)
class _GreenStatus:
    """A `GreenLike` truthy verdict — the shape `T0Monitor` reads from the gate."""

    reason: str = "all core datasets PUBLISHED and quality-green"

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class DrillResult:
    """What one drill run produced — the T0 outcome, the T1 outcome, and the review's spend."""

    t0_outcome: T0Outcome
    t0_flag_summary: str
    queued_isin: str | None
    t1_result: T1Result


def _reviewer(llm: StubLLM, journal: Journal, clock: FrozenClock) -> T1Reviewer:
    metered = MeteredLLM(llm, pricer=TokenPricer(load_price_card()), ledger=None, clock=clock)
    return T1Reviewer(metered, journal, clock=clock, model=T1_MODEL, max_attempts=2)


def _stub_for(built: BuiltBundle, reply: str) -> StubLLM:
    """A strict `StubLLM` that answers exactly the request the reviewer will send, with pinned cost.

    Keyed on the digest of the *actual* built bundle — so the stub cannot answer a prompt the drill
    did not really produce — and constructed `synthesize_unknown=False`, so a drift between what is
    registered and what is sent is a loud failure, never a synthesized sentence read as judgement.
    """
    digest = prompt_digest(
        build_messages(built.rendered_prompt),
        model=T1_MODEL,
        system=SYSTEM_PROMPT,
    )
    return StubLLM({digest: StubReply(text=reply, usage=STUB_USAGE)}, synthesize_unknown=False)


def run_drill(
    journal: Journal, case_id: str, *, clock: FrozenClock, verdict_json: str
) -> DrillResult:
    """Run the whole fire drill against one journal: T0 sweep → escalation → T1 review.

    Writes a SIP park instalment first (so the case has a cashflow the evidence pack's return can be
    struck on), then runs the real T0 monitor over the injected disclosure, takes the queued
    escalation, builds its evidence bundle, and reviews it under a StubLLM returning `verdict_json`.
    The caller owns the transaction; nothing here commits.
    """
    # A SIP instalment parked the same session — the A7 park payload the pack's return reads.
    journal.append(
        JournalEntry(
            ts=clock.now(),
            trading_date=SIP_DATE,
            case_id=case_id,
            actor=Actor.EXEC,
            decision=Decision.BUY,
            isin="INE500E01050",
            sleeve=Sleeve.CASH,
            rationale="parking the SIP instalment in the liquid ETF the same session (§5.6)",
            payload={
                "source_SIP_INSTALMENT": str(SIP_AMOUNT),
                "shares": "10",
                "price": "1000",
                "residual_cash": "0",
            },
        )
    )

    # T0 sweep behind the (green) interlock — the real monitor, the real checks.
    queue = InMemoryEscalationQueue()
    monitor = T0Monitor(_AlwaysGreen(), journal, queue)
    t0_result = monitor.run(
        DRILL_DATE, lambda: make_t0_inputs(case_id), datasets=("nse_eod", "nse_corporate_actions")
    )

    # The escalation T0 raised is the T1 input — build its bundle from that flag, then review it.
    (escalation,) = queue.pending
    built = BundleBuilder().build(
        BundleRequest(
            case_id=case_id,
            isin=ISIN,
            trading_date=DRILL_DATE,
            flag=escalation.flag,
            thesis=make_thesis(case_id),
            prices=price_facts(),
            actor=Actor.T1,
        )
    )
    reviewer = _reviewer(_stub_for(built, verdict_json), journal, clock)
    t1_result = reviewer.review(
        T1Request(built=built, thesis=make_thesis(case_id), exit_menu=full_exit_menu())
    )

    return DrillResult(
        t0_outcome=t0_result.outcome,
        t0_flag_summary=t0_result.flags[0].summary if t0_result.flags else "",
        queued_isin=escalation.flag.isin,
        t1_result=t1_result,
    )


# ── valuation (the only non-journal input the evidence pack takes) ────────────────────────────────


def _tri(slug: str, name: str, levels: dict[date, str]) -> TriSeries:
    return TriSeries(
        index_slug=slug,
        index_name=name,
        method="published",
        points=tuple(
            TriPoint(
                index_slug=slug,
                index_name=name,
                as_of=as_of,
                tri_value=Decimal(level),
                method="published",
            )
            for as_of, level in sorted(levels.items())
        ),
    )


def _valuation() -> PackValuation:
    nav = (
        NavPoint(as_of=SIP_DATE, nav_inr=Decimal("10000")),
        NavPoint(as_of=DRILL_DATE, nav_inr=Decimal("10200")),
        NavPoint(as_of=AS_OF, nav_inr=Decimal("10500")),
    )
    benchmark = _tri("nifty50_tri", "NIFTY 50 TRI", {WINDOW_START: "20000", AS_OF: "20400"})
    theme = _tri("robotics_proxy", "Robotics theme proxy", {WINDOW_START: "1000", AS_OF: "1010"})
    return PackValuation(nav_series=nav, benchmark=benchmark, theme=theme)


# ── database fixtures (the scratch-DB pattern from test_evidence_pack.py) ─────────────────────────


def _settings_for(dbname: str) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url, dbname))


def _insert_case(conn: psycopg.Connection, case_id: str) -> None:
    """A minimal ACTIVE/PAPER case_ row — decision_journal.case_id is a foreign key onto it."""
    conn.execute(
        "INSERT INTO case_ (case_id, title, state, funding_mode, theme, created_at, updated_at) "
        "VALUES (%s, %s, 'ACTIVE', 'PAPER', %s, %s, %s)",
        (case_id, f"Fire-drill case {case_id}", "ai & robotics", RUN_AT, RUN_AT),
    )


@pytest.fixture(scope="session")
def scratch(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Settings]:
    """An empty scratch database with the schema applied; dropped at the end of the session."""
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

    settings = _settings_for(SCRATCH_DB)
    migrate(settings, clock=FrozenClock(RUN_AT))
    yield settings

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def evidence_root(tmp_path: Path) -> Path:
    return tmp_path / "evidence"


def _committed_drill(
    scratch: Settings, evidence_root: Path, case_id: str, *, verdict_json: str
) -> DrillResult:
    """Run the drill for a fresh case and commit it, returning the run's result."""
    with connection(scratch) as conn:
        _insert_case(conn, case_id)
        journal = Journal(
            conn, clock=FrozenClock(RUN_AT), evidence=EvidenceStore(root=evidence_root)
        )
        result = run_drill(journal, case_id, clock=FrozenClock(RUN_AT), verdict_json=verdict_json)
        conn.commit()
    return result


# ── acceptance 1: injected news escalates T0→T1, verdict produced, in-policy action journaled ──


def test_injected_news_escalates_t0_to_t1_and_journals_an_in_policy_action(
    scratch: Settings, evidence_root: Path
) -> None:
    case_id = "firedrill-escalates"
    result = _committed_drill(scratch, evidence_root, case_id, verdict_json=broken_verdict_json())

    # T0→T1: the disclosure escalated, was queued for T1, and named the held ISIN (invariant #2).
    assert result.t0_outcome is T0Outcome.ESCALATED
    assert result.queued_isin == ISIN
    assert "BC3" in result.t0_flag_summary

    # A verdict was produced, schema-valid, one per break condition.
    t1 = result.t1_result
    assert t1.verdict is not None
    assert {v.id for v in t1.verdict.verdicts} == {"BC1", "BC3"}

    # The BROKEN core produced an in-policy EXIT that validate_action cleared before the rails.
    assert t1.outcome is T1Outcome.BROKEN
    assert t1.exit_triggered is True
    assert t1.proposed_action_kind is ProposedActionKind.EXIT
    assert t1.exit_strategy == ExitStrategy.IMMEDIATE.value
    assert t1.rejection is None

    # It is journaled: a T0 ESCALATE for the flag, and a T1 ESCALATE carrying the exit directive —
    # T1 never places the order itself (invariant #6), it hands the validated exit to A7.
    with connection(scratch) as conn:
        journal = Journal(
            conn, clock=FrozenClock(RUN_AT), evidence=EvidenceStore(root=evidence_root)
        )
        window = JournalFilter(case_id=case_id, start=WINDOW_START, end=WINDOW_END)
        escalations = journal.entries(dataclass_replace_decision(window, Decision.ESCALATE))
        actors = {e.actor for e in escalations}
        assert Actor.T0 in actors and Actor.T1 in actors
        (t1_entry,) = [e for e in escalations if e.actor is Actor.T1]
        assert t1_entry.isin == ISIN
        assert t1_entry.payload["proposed_action"] == ProposedActionKind.EXIT.value
        assert t1_entry.payload["exit_strategy"] == ExitStrategy.IMMEDIATE.value
        # No BUY/SELL was placed by the monitoring path — the exit is A7's to carry out.
        assert journal.count(dataclass_replace_decision(window, Decision.SELL)) == 0


def test_out_of_policy_action_is_rejected_before_rails_and_escalated_to_the_human(
    scratch: Settings, evidence_root: Path
) -> None:
    """The inversion: an IMMEDIATE exit the menu does not unlock never triggers A7 — it escalates.

    Same injected break, but the model proposes IMMEDIATE on the *fundamental* BC1 (which the menu
    unlocks only on integrity). `validate_action` must reject it in code: no A7 exit is triggered,
    the action does not survive the gate, and the decision goes to the human — invariant #6 holds
    even when the model asks for something off-policy.
    """
    out_of_policy = T1Verdict(
        isin=ISIN,
        verdicts=(
            BreakConditionVerdict(id="BC1", verdict=Verdict.BROKEN, observed="revenue collapsed"),
            BreakConditionVerdict(id="BC3", verdict=Verdict.INTACT, observed="no integrity flag"),
        ),
        proposed_action=ProposedAction(
            kind=ProposedActionKind.EXIT,
            exit_strategy=ExitStrategy.IMMEDIATE,
            rationale="wants immediate on a fundamental break",
        ),
        summary="fundamental break, asks to bypass the staged default",
    ).model_dump_json()

    result = _committed_drill(
        scratch, evidence_root, "firedrill-offpolicy", verdict_json=out_of_policy
    )
    t1 = result.t1_result
    assert t1.outcome is T1Outcome.ESCALATED
    assert t1.exit_triggered is False
    assert t1.proposed_action_kind is None  # the action did not survive the gate
    assert t1.rejection is not None


# ── acceptance 3: token cost per decision is visible in the journal line ──────────────────────────


def test_token_cost_per_decision_is_visible_in_the_journal(
    scratch: Settings, evidence_root: Path
) -> None:
    """The T1 decision's row carries its model and a non-zero rupee cost, and the pack sums it."""
    case_id = "firedrill-cost"
    _committed_drill(scratch, evidence_root, case_id, verdict_json=broken_verdict_json())

    with connection(scratch) as conn:
        journal = Journal(
            conn, clock=FrozenClock(RUN_AT), evidence=EvidenceStore(root=evidence_root)
        )
        window = JournalFilter(case_id=case_id, start=WINDOW_START, end=WINDOW_END)
        (t1_entry,) = [
            e
            for e in journal.entries(dataclass_replace_decision(window, Decision.ESCALATE))
            if e.actor is Actor.T1
        ]
        # The cost is on the decision line itself — model named, tokens and rupees non-zero.
        assert t1_entry.model == T1_MODEL
        assert t1_entry.tokens is not None
        assert t1_entry.tokens.tokens_in == STUB_USAGE.input_tokens
        assert t1_entry.tokens.tokens_out == STUB_USAGE.output_tokens
        assert t1_entry.tokens.cost_inr > Decimal("0")
        # The T0 mechanical escalation cost nothing — it holds no model (§5.4: T0 is ~₹0).
        (t0_entry,) = [
            e
            for e in journal.entries(dataclass_replace_decision(window, Decision.ESCALATE))
            if e.actor is Actor.T0
        ]
        assert t0_entry.tokens is None

        # The evidence pack attributes that same spend to the T1 tier — traceable to the journal.
        pack = generate_pack(
            journal,
            case_id=case_id,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            valuation=_valuation(),
            generated_at=RUN_AT,
            trigger=PackTrigger.MONTHLY,
        )
        assert {t.actor for t in pack.cost_burn.by_tier} == {Actor.T1}
        assert pack.cost_burn.total_cost_inr == t1_entry.tokens.cost_inr
        assert pack.cost_burn.total_tokens_in == t1_entry.tokens.tokens_in


# ── acceptance 1 (pack side): the evidence pack reflects the fire drill ──


def test_evidence_pack_reflects_the_fire_drill(scratch: Settings, evidence_root: Path) -> None:
    """The drill's verdict and its BROKEN break condition surface in the pack's decision review."""
    case_id = "firedrill-pack"
    _committed_drill(scratch, evidence_root, case_id, verdict_json=broken_verdict_json())

    with connection(scratch) as conn:
        journal = Journal(
            conn, clock=FrozenClock(RUN_AT), evidence=EvidenceStore(root=evidence_root)
        )
        pack = generate_pack(
            journal,
            case_id=case_id,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            valuation=_valuation(),
            generated_at=RUN_AT,
            trigger=PackTrigger.MONTHLY,
        )

    # The T1 review is in the decision review, with the integrity condition read BROKEN.
    reviews = pack.decision_review.reviews
    assert any(r.actor is Actor.T1 for r in reviews)
    broken = [r for r in reviews if r.verdict == "BROKEN"]
    assert len(broken) == 1
    assert broken[0].break_condition_id == "BC3"
    assert broken[0].isin == ISIN

    # The rails held throughout — the drill triggered no breach (invariant #6).
    assert pack.rail_breaches.breach_count == 0

    # The rendered pack is one readable document naming the drilled case and the review.
    rendered = render_markdown(pack)
    assert case_id in rendered
    assert "BROKEN" in rendered
    assert "$" not in rendered  # no unfilled template placeholders leaked


# ── acceptance 2: the drill is automated and repeatable ───────────────────────────────────────────


def test_the_drill_is_repeatable(scratch: Settings, evidence_root: Path) -> None:
    """Two independent runs under StubLLM agree on verdict, outcome, action and token spend."""
    first = _committed_drill(
        scratch, evidence_root, "firedrill-repeat-a", verdict_json=broken_verdict_json()
    )
    second = _committed_drill(
        scratch, evidence_root, "firedrill-repeat-b", verdict_json=broken_verdict_json()
    )

    a, b = first.t1_result, second.t1_result
    assert first.t0_outcome is second.t0_outcome is T0Outcome.ESCALATED
    assert a.outcome is b.outcome
    assert a.exit_triggered == b.exit_triggered
    assert a.exit_strategy == b.exit_strategy
    # Same verdict per condition, byte-identical spend — determinism is the whole point (inv #11).
    assert a.verdict is not None and b.verdict is not None
    assert a.verdict.verdicts == b.verdict.verdicts
    assert a.token_spend == b.token_spend


# ── a tiny helper so the tests read the journal without re-importing dataclasses.replace ──


def dataclass_replace_decision(window: JournalFilter, decision: Decision) -> JournalFilter:
    """A copy of `window` narrowed to one decision type — the pack's own `replace` idiom, local."""
    from dataclasses import replace

    return replace(window, decision=decision)


if __name__ == "__main__":  # pragma: no cover - convenience for a direct run
    raise SystemExit(pytest.main([__file__, "-q"]))
