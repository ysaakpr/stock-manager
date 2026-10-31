"""M17.4 — the fund-manager runtime: bounded research rounds and the decision contract.

Offline: the model is `StubLLM` (scripted per call by prompt digest), the fetcher is a stub over a
real `SnapshotStore` in a temp dir, and the Commons are the M17.9 synthetic world. The acceptance
criteria, each tested here:

1. an invented citation id, scenarios not summing to 1, an edge_type NONE BUY, or a loosened stop is
   refused;
2. the rendered prompt carries no cost basis or P&L for any holding, and only the manager's own
   STYLE block;
3. with StubLLM a full session runs <= 4 calls and journals every decision and the bundle digests;
4. a SELL without what_changed is refused; p_beat_bench outside [0, 1] is refused;
5. a manager's prompt never contains another manager's id, book or decisions.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from accounting import TokenPricer, load_price_card
from analyst.commons import (
    CommonsScreens,
    CommonsSheets,
    DigestBody,
    DigestFailure,
    FetchedPage,
    FetchError,
    FetchRequest,
    FetchResponse,
    FilingDigest,
    FilingKind,
    OnDemandDigests,
    Shortlist,
    ShortlistEntry,
    SnapshotStore,
    build_dossiers,
    build_screens,
    load_table,
)
from analyst.commons.base_rates import BaseRateTable
from analyst.fundmanager import ManagerMandate, ModelIds, load_roster
from analyst.fundmanager.bundle import Holding, InvalidationStatus, ManagerBook
from analyst.fundmanager.contract import (
    P_DEAD_BAND_PP,
    P_TOLERANCE,
    ReasonCode,
    implied_p_beat,
    p_consistent,
)
from analyst.fundmanager.render import (
    PROMPT_PATH,
    ROUND_FINAL,
    ROUND_RESEARCH,
    ROUND_ZERO,
    PromptTemplate,
    TemplateError,
    render_holdings,
)
from analyst.fundmanager.runtime import (
    CALL_EVENT,
    CONTRACT_REPAIR_FAILED_EVENT,
    CONTRACT_REPAIR_STAGE,
    FETCH_CONCURRENCY,
    MANAGER_ERROR_EVENT,
    NO_ACTION_EVENT,
    REFUSED_EVENT,
    RESEARCH_EVENT,
    TRUNCATION_EVENT,
    ManagerCommons,
    ManagerSessionResult,
    SessionStatus,
    _Session,
    run_manager,
)
from analyst.fundmanager.schemas import (
    MAX_SHORT_CHARS,
    MAX_TEXT_CHARS,
    Action,
    MalformedOutputError,
    Scenario,
    ScenarioName,
    decision_schema,
    parse_decisions,
    parse_research,
    research_schema,
)
from analyst.fundmanager.scoreboard import (
    DECISION_EVENT,
    DecisionAction,
    inputs_from_journal,
    scored_decision_from_entry,
)
from analyst.journal.evidence import EvidenceBundle
from analyst.journal.models import Decision, JournalEntry
from analyst.llm import (
    Fallback,
    LLMError,
    LLMResponse,
    Message,
    StopReason,
    StubLLM,
    StubReply,
    ToolCall,
    ToolSpec,
    UnavailableReason,
    prompt_digest,
)
from analyst.llm.claude_cli import ClaudeCliLLM
from analyst.llm.client import DEFAULT_MAX_TOKENS
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import SlippageModel
from tests.unit.commons_screen_world import (
    BREAKOUT,
    FILLERS,
    FUTURE_SESSIONS,
    LEADER,
    SESSION,
    FakeScreenSource,
    clock,
    screen_world,
    sheets_for,
)

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "commons" / "base_rates" / "synthetic.json"
)
HELD = FILLERS[0]
CELL = "S1.ALL.RISK_ON.h20"
NAV = Decimal("1034567.89")  # distinctive, so a test can prove it is never rendered
HELD_STOP = Decimal("93.4567")


# ── the world ────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class World:
    sheets: CommonsSheets
    screens: CommonsScreens
    shortlist: Shortlist
    table: BaseRateTable


def _shortlist(sheets: CommonsSheets, isins: Sequence[str]) -> Shortlist:
    entries = tuple(
        ShortlistEntry(
            position=k,
            isin=isin,
            momentum_12_1=None,
            relative_strength_20=None,
            earnings_surprise=None,
            log_liquidity=None,
            rank_momentum_12_1=Decimal("0.5"),
            rank_relative_strength_20=Decimal("0.5"),
            rank_earnings_surprise=Decimal("0.5"),
            rank_log_liquidity=Decimal("0.5"),
            composite=Decimal(1) - Decimal(k) / 10,
        )
        for k, isin in enumerate(isins, start=1)
    )
    body: dict[str, Any] = {
        "trading_date": sheets.trading_date,
        "build_digest": sheets.build_digest,
        "shortlist_version": "commons-shortlist/test",
        "rule_hash": "0" * 64,
        "universe_size": len(sheets.universe),
        "coverage": {},
        "entries": entries,
        "gaps": (),
    }
    return Shortlist(**body, shortlist_digest=Shortlist.digest_of(**body), built_at=clock().now())


@pytest.fixture(scope="module")
def world() -> World:
    lake = screen_world()
    sheets = sheets_for(lake)
    screens = build_screens(
        sheets,
        shortlist=None,
        source=FakeScreenSource(lake),
        clock=clock(),
        future_sessions=FUTURE_SESSIONS,
    )
    return World(sheets, screens, _shortlist(sheets, [LEADER, BREAKOUT]), load_table(FIXTURE))


@pytest.fixture(scope="module")
def atr_pct(world: World) -> dict[str, Decimal]:
    lake = screen_world()
    dossiers = build_dossiers(
        [LEADER, BREAKOUT, HELD],
        screens=world.screens,
        sheets=world.sheets,
        source=FakeScreenSource(lake),
    )
    out = {}
    for d in dossiers:
        value = d.fields["atr14_pct"]
        assert isinstance(value, Decimal)
        out[d.isin] = value
    return out


def _digest() -> FilingDigest:
    return FilingDigest(
        filing_id="nse_announcements:9002",
        kind=FilingKind.ANNOUNCEMENT,
        isin=LEADER,
        knowable_date=SESSION,
        trading_date=SESSION,
        digest_version="commons-digest/1",
        input_digest="a" * 64,
        input_truncated=False,
        prompt_digest="b" * 64,
        provider="stub",
        model="claude-sonnet-5-5",
        body=DigestBody(headline="Order win", disclosed="Leader Ltd received an order."),
        input_tokens=1,
        output_tokens=1,
        cache_write_tokens=0,
        cache_read_tokens=0,
        digested_at=clock().now(),
    )


def _on_demand(isins: Sequence[str]) -> OnDemandDigests:
    return OnDemandDigests(
        trading_date=SESSION,
        isins=tuple(isins),
        since=SESSION,
        digests=(_digest(),) if LEADER in isins else (),
        digested=(),
        cached=(),
        failures=(DigestFailure(filing_id="nse_announcements:9003", reason="stub refused"),),
        gaps=(),
    )


class StubFetcher:
    """A fetcher that transcribes a fixed page per request and counts what it was asked."""

    def __init__(self, *, fail: frozenset[str] = frozenset()) -> None:
        self.asked: list[FetchRequest] = []
        self.fail = fail

    @property
    def name(self) -> str:
        return "stub:fetcher"

    def fetch(self, request: FetchRequest) -> FetchResponse:
        self.asked.append(request)
        if request.target in self.fail:
            raise FetchError(f"stub refused {request.target}")
        return FetchResponse(
            pages=(
                FetchedPage(
                    url="https://www.nseindia.com/a",
                    title=f"Result for {request.target}",
                    text=f"Exchange filing text about {request.target}.",
                ),
            )
        )


def _commons(world: World, tmp_path: Path, *, digests: bool = True) -> ManagerCommons:
    return ManagerCommons.from_builds(
        sheets=world.sheets,
        screens=world.screens,
        shortlist=world.shortlist,
        base_rates=world.table,
        source=FakeScreenSource(screen_world()),
        snapshots=SnapshotStore(tmp_path / "fetch", clock=clock()),
        cost_model=CostModel(load_rate_card()),
        slippage=SlippageModel(),
        digests=_on_demand if digests else None,
    )


def _mandate(book_id: str = "FM-SWING-BRK-10L", *, priced: bool = False) -> ManagerMandate:
    mandate = load_roster().get(book_id)
    assert isinstance(mandate, ManagerMandate)
    if priced:
        models = ModelIds(
            decision="claude-opus-5", digest=mandate.models.digest, research=mandate.models.research
        )
        mandate = mandate.model_copy(update={"models": models})
    return mandate


def _book(book_id: str = "FM-SWING-BRK-10L", *, stop: Decimal | None = HELD_STOP) -> ManagerBook:
    return ManagerBook(
        book_id=book_id,
        nav=NAV,
        cash_pct=Decimal("92.00"),
        holdings=(
            Holding(
                isin=HELD,
                sector="Banks",
                weight_pct=Decimal("8.00"),
                sessions_held=7,
                opening_thesis="Trend continuation in a steady filler name.",
                invalidation=(InvalidationStatus("close below the 50-session mean", "not hit"),),
                stop_price=stop,
                evidence_since_entry=("no new filings",),
            ),
        ),
    )


@dataclass
class ListJournal:
    entries: list[JournalEntry] = field(default_factory=list)
    evidence: dict[str, EvidenceBundle] = field(default_factory=dict)

    def append(self, entry: JournalEntry, *, evidence: EvidenceBundle | None = None) -> object:
        if evidence is not None:
            assert entry.evidence_snapshot_ref == evidence.ref().ref
            self.evidence[evidence.ref().ref] = evidence
        self.entries.append(entry)
        return entry

    def events(self, event: str) -> list[JournalEntry]:
        return [e for e in self.entries if e.payload.get("event") == event]


class ScriptedStub:
    """`StubLLM`, answering each call with the next scripted structured output.

    Each reply is registered under the exact prompt digest of the call it answers, so the response
    (text, usage, provider) is the stub's own, deterministic one.
    """

    def __init__(self, replies: Sequence[Mapping[str, Any] | Exception]) -> None:
        self.stub = StubLLM(synthesize_unknown=False)
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.tools: list[str] = []
        self.breaks: list[tuple[int, ...]] = []
        self.schemas: list[Mapping[str, Any]] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        self.prompts.append(messages[-1].content)
        self.tools.append(tools[0].name)
        self.breaks.append(messages[-1].cache_breaks)
        self.schemas.append(tools[0].input_schema)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        digest = prompt_digest(messages, model=model, tools=tools, system=system)
        self.stub.register(
            digest,
            StubReply(
                text="",
                tool_calls=(ToolCall(id="structured_output", name=tools[0].name, arguments=reply),),
                stop_reason=StopReason.TOOL_USE,
            ),
        )
        return self.stub.complete(
            messages, model=model, tools=tools, system=system, max_tokens=max_tokens
        )

    @property
    def calls(self) -> int:
        return len(self.stub.calls)


# ── scripted answers ─────────────────────────────────────────────────────────────────────────────


def research(
    requests: Sequence[str] = (),
    queries: Sequence[str] = (),
    *,
    holdings: bool = True,
) -> dict[str, Any]:
    return {
        "holdings": [{"isin": HELD, "triage": "CLEAR", "note": "thesis stands"}]
        if holdings
        else [],
        "requests": [
            {"isin": i, "claim": f"{i} keeps trending", "kill_test": "last results reaction"}
            for i in requests
        ],
        "queries": [
            {"kind": "QUERY", "target": q, "isin": LEADER, "purpose": "check the order book"}
            for q in queries
        ],
    }


def hold(isin: str = HELD, **over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "isin": isin,
        "action": "HOLD",
        "target_weight": None,
        "rationale": f"The thesis and its invalidation stand; checked [F:{isin}.ret_13w].",
        "horizon_sessions": 15,
        "p_beat_bench": 0.5,
        "expected_excess_pct": 0.5,
        "stop_pct": None,
        "new_stop_pct": None,
        "invalidation": [],
        "evidence_refs": [f"F:{isin}.close"],
        "what_changed": None,
        "thesis": None,
        "catalyst": None,
        "already_priced_in": None,
        "edge_type": "TREND_LEADER",
        "base_rate_cell": None,
        "adjustments": [],
        "scenarios": [],
        "cost_hurdle_check": None,
        "premortem": [],
    }
    out.update(over)
    return out


def scenarios(bull: float = 0.3, base: float = 0.45, bear: float = 0.25) -> list[dict[str, Any]]:
    return [
        {"name": "BULL", "probability": bull, "excess_pct": 10, "description": "trend extends"},
        {"name": "BASE", "probability": base, "excess_pct": 2, "description": "drifts up"},
        {"name": "BEAR", "probability": bear, "excess_pct": -6, "description": "reverses"},
    ]


def buy(atr: Decimal, isin: str = LEADER, **over: Any) -> dict[str, Any]:
    # scenarios(): mean 0.3*10 + 0.45*2 - 0.25*6 = 2.4; implied P(excess > 0) = 0.75.
    out = hold(
        isin,
        action="BUY",
        target_weight=5,
        rationale=(
            f"S1 leader with a fresh order win [F:{isin}.ret_12_1] [F:nse_announcements:9002]."
        ),
        horizon_sessions=20,
        p_beat_bench=0.62,
        expected_excess_pct=2.4,
        stop_pct=float((2 * atr * 100).quantize(Decimal("0.01"))),
        invalidation=["close below the 50-session mean by 2026-11-06"],
        thesis="The market underrates the order-book step-up; results inside the horizon show it.",
        catalyst="None, trend continuation",
        already_priced_in=f"Up [F:{isin}.ret_4w] since; the order is not yet in estimates.",
        edge_type="TREND_LEADER",
        base_rate_cell={
            "cell_id": CELL,
            "p_beat": 0.6,
            "median_excess": None,
            "iqr_excess": None,
        },
        adjustments=[
            {
                "reason": "fresh order win",
                "direction": "UP",
                "size_pp": 2,
                "citation": f"[F:{isin}.ann_9002]",
            }
        ],
        scenarios=scenarios(),
        cost_hurdle_check=2.1,
        premortem=["the market reversed", "the order was small", "sector rotated out"],
    )
    out.update(over)
    return out


def passed(isin: str = BREAKOUT, **over: Any) -> dict[str, Any]:
    return hold(
        isin, action="PASS", edge_type="NONE", p_beat_bench=0.45, expected_excess_pct=0, **over
    )


def final(*decisions: dict[str, Any]) -> dict[str, Any]:
    return {"no_action": False, "no_action_reason": None, "decisions": list(decisions)}


def _run(
    world: World,
    tmp_path: Path,
    replies: Sequence[Mapping[str, Any] | Exception],
    *,
    mandate: ManagerMandate | None = None,
    book: ManagerBook | None = None,
    fetcher: StubFetcher | None = None,
) -> tuple[ManagerSessionResult, ListJournal, ScriptedStub]:
    mandate = mandate or _mandate()
    journal = ListJournal()
    llm = ScriptedStub(replies)
    result = run_manager(
        mandate,
        SESSION,
        _commons(world, tmp_path),
        book or _book(mandate.id),
        llm,
        fetcher or StubFetcher(),
        journal=journal,
        clock=clock(),
    )
    return result, journal, llm


def _one_buy_session(
    world: World, tmp_path: Path, decision: dict[str, Any]
) -> tuple[ManagerSessionResult, ListJournal]:
    result, journal, _ = _run(
        world,
        tmp_path,
        [research([LEADER]), research(), final(hold(), decision)],
    )
    return result, journal


def _reasons(result: ManagerSessionResult, isin: str = LEADER) -> str:
    refused = [v for v in result.refused if v.decision.isin == isin]
    assert refused, f"{isin} was not refused; accepted {[v.decision.isin for v in result.accepted]}"
    return " | ".join(refused[0].reasons)


# ── acceptance 3: a full session, <= 4 calls, everything journaled ───────────────────────────────


def test_a_full_session_runs_four_calls_and_journals_every_decision_and_bundle(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    fetcher = StubFetcher()
    result, journal, llm = _run(
        world,
        tmp_path,
        [
            research([LEADER], ["leader ltd order book q2 2026"]),
            research([BREAKOUT], ["breakout ltd board meeting results date"]),
            research(),
            final(hold(), buy(atr_pct[LEADER]), passed()),
        ],
        fetcher=fetcher,
    )
    assert result.status is SessionStatus.DECIDED
    assert llm.calls == result.calls == 4 and result.repairs == 0
    assert llm.tools == ["research_request"] * 3 + ["manager_decisions"]
    assert {v.decision.isin for v in result.accepted} == {HELD, LEADER, BREAKOUT}
    assert not result.refused

    calls = journal.events(CALL_EVENT)
    assert len(calls) == 4
    for entry in calls:
        assert entry.case_id == "FM-SWING-BRK-10L" and entry.decision is Decision.HEARTBEAT
        bundle = journal.evidence[entry.evidence_snapshot_ref or ""]
        assert bundle.rendered_prompt in llm.prompts
        assert int(entry.payload["input_tokens"]) > 0 and entry.payload["repair"] == "false"

    fulfilled = journal.events(RESEARCH_EVENT)
    assert [e.payload["round"] for e in fulfilled] == ["0", "1"]
    dossier_digests = {
        pair.split(":")[0]: pair.split(":")[1]
        for e in fulfilled
        for pair in e.payload["dossiers"].split(",")
        if pair
    }
    assert set(dossier_digests) == {HELD, LEADER, BREAKOUT}
    assert all(len(d) == 64 for d in dossier_digests.values())
    snapshot_ids = [s for e in fulfilled for s in e.payload["snapshots"].split(",") if s]
    assert len(snapshot_ids) == 2 and len(fetcher.asked) == 2
    assert fulfilled[0].payload["digests"] == "nse_announcements:9002"
    assert "nse_announcements:9003" in fulfilled[0].payload["unfulfilled"]  # a digest failure
    assert all(
        journal.evidence[e.evidence_snapshot_ref or ""].rendered_prompt is None for e in fulfilled
    )
    final_bundle = journal.evidence[calls[-1].evidence_snapshot_ref or ""]
    assert {i.text for i in final_bundle.items if i.label == "dossier"} == set(
        dossier_digests.values()
    )
    assert {i.text for i in final_bundle.items if i.label == "snapshot"} == set(snapshot_ids)
    for sid in snapshot_ids:
        assert f"[S:{sid}]" in llm.prompts[-1]

    decisions = journal.events(DECISION_EVENT)
    assert len(decisions) == 3
    scored = {s.isin: s for s in map(scored_decision_from_entry, decisions) if s is not None}
    leader = scored[LEADER]
    assert leader.action is DecisionAction.BUY and leader.p_beat_bench == Decimal("0.62")
    assert leader.base_rate_p == Decimal("0.600000") and leader.regime == "RISK_ON"
    assert leader.cap_tier == "mid" and leader.in_shortlist is True
    assert leader.stop_pct is not None and leader.target_weight == Decimal(5)
    assert scored[HELD].action is DecisionAction.HOLD and scored[BREAKOUT].edge_type == "NONE"
    buy_line = next(e for e in decisions if e.isin == LEADER)
    assert (
        buy_line.decision is Decision.BUY
        and buy_line.evidence_snapshot_ref == calls[-1].evidence_snapshot_ref
    )
    assert "F:nse_announcements:9002" in buy_line.payload["citations"]
    assert Decimal(buy_line.payload["round_trip_pct"]) > 0


def test_no_bundle_item_is_rendered_twice_across_a_sessions_rounds(
    world: World, tmp_path: Path
) -> None:
    """M17.12: every round re-sends every bundle, so a repeated item is paid for on every call.

    The on-demand digest stub reports the same failed filing on every call, as a digest source gap
    does on every round that asks for names; it is shown once. Dossiers, digests and snapshots
    were already shown once each; this pins it.
    """
    _, journal, llm = _run(
        world,
        tmp_path,
        [
            research([LEADER, FILLERS[1]], ["leader ltd order book q2 2026", "filler results"]),
            research([BREAKOUT], ["breakout ltd board meeting results date"]),
            research([LEADER], ["leader ltd order book q2 2026"]),  # a repeat: nothing new
            final(hold(), passed(LEADER), passed(), passed(FILLERS[1])),
        ],
    )
    last = llm.prompts[-1]
    for isin in (HELD, LEADER, FILLERS[1], BREAKOUT):
        assert last.count(f"#### Dossier {isin} ") == 1, isin
    assert last.count("- [F:nse_announcements:9002] ") == 1
    snapshot_ids = re.findall(r"#### \[S:([0-9a-f]{64})\]", last)
    assert len(snapshot_ids) == len(set(snapshot_ids)) == 3
    assert last.count("filing digest nse_announcements:9003: stub refused") == 1
    # The journal still says what each round could not fulfil.
    rounds = journal.events(RESEARCH_EVENT)
    assert all("nse_announcements:9003" in e.payload["unfulfilled"] for e in rounds[:2])


def test_research_rounds_stop_at_two_so_a_session_never_exceeds_four_calls(
    world: World, tmp_path: Path
) -> None:
    result, journal, llm = _run(
        world,
        tmp_path,
        [
            research([LEADER], ["q one"]),
            research([], ["q two"]),
            research([], ["q three"]),  # the second research round asks again: fulfilled, shown
            final(hold(), passed(LEADER)),
        ],
    )
    assert llm.calls == result.calls == 4
    assert [e.payload["round"] for e in journal.events(RESEARCH_EVENT)] == ["0", "1", "2"]
    assert "## Your task this round: decide" in llm.prompts[-1]
    assert "Exchange filing text about q three" in llm.prompts[-1]


def test_every_call_marks_the_session_header_and_each_research_bundle_for_the_prompt_cache(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    """Transport only (`Message.cache_breaks`): one header shared byte for byte by every call and
    reaching all the way to the round's task, so the mandate, market sheet, holdings, screens,
    shortlist, base rates and cost hurdles are read from the cache after the first call; then
    each research bundle's end, so a round reads what the round before it wrote."""
    result, _, llm = _run(
        world,
        tmp_path,
        [
            research([LEADER], ["leader ltd order book q2 2026"]),
            research([BREAKOUT], ["breakout ltd board meeting results date"]),
            research(),
            final(hold(), buy(atr_pct[LEADER]), passed()),
        ],
    )
    assert result.status is SessionStatus.DECIDED
    prompts, breaks = llm.prompts, llm.breaks
    head = breaks[0][0]
    header = prompts[0][:head]
    assert all(b[0] == head and p.startswith(header) for p, b in zip(prompts, breaks, strict=True))
    # The header is the whole stable context: the next line is the round's task, in every round.
    assert all(p[head:].startswith("## Your task this round") for p in prompts)
    assert "### Research fulfilled" not in header
    # round 0: only the header; research1: + bundle 0; research2: + bundles 0 and 1.
    assert [len(b) for b in breaks[:3]] == [1, 2, 3]
    r1_end = breaks[1][-1]
    assert r1_end in breaks[2] and prompts[2][:r1_end] == prompts[1][:r1_end]
    assert prompts[1][:r1_end].endswith(prompts[2][breaks[2][1] - 40 : breaks[2][1]])
    # The final round marks the header and both bundles.
    assert len(breaks[3]) == 3


def test_a_schema_repair_reuses_its_originals_cache_breaks(world: World, tmp_path: Path) -> None:
    result, _, llm = _run(
        world,
        tmp_path,
        [research([LEADER], ["q one"]), research(), final(hold(action="SELL")), final(hold())],
    )
    assert result.repairs == 1
    original, repair = llm.prompts[-2], llm.prompts[-1]
    assert repair.startswith(original) and llm.breaks[-1] == llm.breaks[-2]
    assert len(llm.breaks[-1]) == 2


def test_an_empty_round_zero_goes_straight_to_the_final_call(world: World, tmp_path: Path) -> None:
    result, journal, llm = _run(world, tmp_path, [research(), final(hold())])
    assert llm.calls == result.calls == 2 and result.status is SessionStatus.DECIDED
    assert journal.events(RESEARCH_EVENT)[0].payload["dossiers"].startswith(HELD)


def test_over_asking_is_truncated_to_the_mandate_caps_and_journaled(
    world: World, tmp_path: Path
) -> None:
    mandate = _mandate()
    many = [*world.sheets.universe]
    isins = [r.isin for r in many] + [LEADER, LEADER]  # 12 names, then repeats
    extra = ["INE000777018", "INE000778016"]  # outside the universe, past the cap
    queries = [f"query number {k}" for k in range(11)]
    result, journal, _ = _run(
        world,
        tmp_path,
        [
            research([*isins, *extra], queries),
            research(),
            final(hold(), *[passed(i) for i in isins[:12] if i != HELD]),
        ],
        mandate=mandate,
    )
    truncated = journal.events(TRUNCATION_EVENT)
    assert len(truncated) == 1
    payload = truncated[0].payload
    assert payload["isins_dropped"] == ",".join(extra)
    assert json.loads(payload["queries_dropped"]) == queries[mandate.rounds.max_queries :]
    request = json.loads(journal.events(RESEARCH_EVENT)[0].payload["request"])
    assert len(request["requests"]) == mandate.rounds.max_isins
    assert len(request["queries"]) == mandate.rounds.max_queries
    assert result.status is SessionStatus.DECIDED


def test_the_research_schema_caps_isins_and_queries_at_the_mandate_limits() -> None:
    limits = _mandate().rounds
    schema = research_schema(limits)
    assert schema["properties"]["requests"]["maxItems"] == limits.max_isins == 12
    assert schema["properties"]["queries"]["maxItems"] == limits.max_queries == 8


def test_a_whole_session_no_action_is_journaled_with_its_reason(
    world: World, tmp_path: Path
) -> None:
    result, journal, _ = _run(
        world,
        tmp_path,
        [
            research(),
            {"no_action": True, "no_action_reason": "nothing clears the hurdle", "decisions": []},
        ],
    )
    assert result.status is SessionStatus.NO_ACTION and not result.accepted
    (line,) = journal.events(NO_ACTION_EVENT)
    assert line.payload["reason"] == "nothing clears the hurdle"
    assert line.rationale == "nothing clears the hurdle" and line.evidence_snapshot_ref


def test_token_usage_is_journaled_per_call_priced_when_the_card_knows_the_model(
    world: World, tmp_path: Path
) -> None:
    priced = _mandate(priced=True)
    _, journal, _ = _run(world, tmp_path, [research(), final(hold())], mandate=priced)
    calls = journal.events(CALL_EVENT)
    assert len(calls) == 2
    assert all(c.tokens is not None and c.tokens.cost_inr > 0 for c in calls)
    assert all(c.payload["priced"] == "true" for c in calls)
    roster = load_roster()
    inputs = inputs_from_journal(journal.entries, roster)
    assert len(inputs.calls) == 2  # the scoreboard counts each call once, decisions carry none
    assert len(inputs.decisions) == 1

    # The roster's own model is not on the card in force on 2026-10-08 (it is priced from
    # 2026-10-09): counts journaled, no rupee invented.
    _, unpriced, _ = _run(world, tmp_path / "u", [research(), final(hold())])
    for call in unpriced.events(CALL_EVENT):
        assert call.tokens is None and call.payload["priced"] == "false"
        assert (
            int(call.payload["output_tokens"]) > 0
            and "claude-opus-5-5" in call.payload["unpriced_reason"]
        )


class FellBackStub(ScriptedStub):
    """`ScriptedStub` whose answers arrive as a `FallbackLLM`'s backup would deliver them."""

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        response = super().complete(
            messages, model=model, tools=tools, system=system, max_tokens=max_tokens
        )
        return dataclasses.replace(
            response,
            provider="claude_cli",
            fallback=Fallback(from_provider="bedrock", reason=UnavailableReason.RATE_LIMIT),
        )


def test_a_call_the_backup_answered_journals_which_provider_failed_and_why(
    world: World, tmp_path: Path
) -> None:
    """The FM_CALL line of a fallback says the backup answered, which primary failed, and why;
    a call the primary answered carries no fallback fields at all."""
    mandate = _mandate()
    journal = ListJournal()
    run_manager(
        mandate,
        SESSION,
        _commons(world, tmp_path),
        _book(mandate.id),
        FellBackStub([research(), final(hold())]),
        StubFetcher(),
        journal=journal,
        clock=clock(),
    )
    calls = journal.events(CALL_EVENT)
    assert calls
    for call in calls:
        assert call.payload["provider"] == "claude_cli"
        assert call.payload["fallback_from"] == "bedrock"
        assert call.payload["fallback_reason"] == "rate_limit"

    _, direct, _ = _run(world, tmp_path / "d", [research(), final(hold())])
    for call in direct.events(CALL_EVENT):
        assert "fallback_from" not in call.payload and "fallback_reason" not in call.payload


class ConcurrencyProbe(StubFetcher):
    """Counts fetches in flight; each holds until three are open at once (or a timeout)."""

    def __init__(self, *, fail: frozenset[str] = frozenset(), want: int = 3) -> None:
        super().__init__(fail=fail)
        self.want = want
        self.lock = threading.Condition()
        self.in_flight = 0
        self.peak = 0

    def fetch(self, request: FetchRequest) -> FetchResponse:
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.lock.notify_all()
            # Wait for company: proves the fetches overlap. Never longer than 2 s, so a serial
            # implementation fails on `peak` rather than hanging the suite.
            self.lock.wait_for(lambda: self.peak >= self.want, timeout=2)
        try:
            # Later fetches finish first, so request order in the result is not finish order.
            time.sleep(0.01 * (8 - int(request.target.rsplit(" ", 1)[-1])))
            return super().fetch(request)
        finally:
            with self.lock:
                self.in_flight -= 1


def test_fetches_run_three_at_once_and_come_back_in_request_order(
    world: World, tmp_path: Path
) -> None:
    """M17.12: eight serial ~80 s fetches took 6-9 minutes a round on 2026-10-09."""
    queries = [f"leader ltd query {n}" for n in range(1, 8)]
    fetcher = ConcurrencyProbe(fail=frozenset({"leader ltd query 4"}))
    _, journal, llm = _run(
        world, tmp_path, [research([], queries), research(), final(hold())], fetcher=fetcher
    )
    assert fetcher.peak == FETCH_CONCURRENCY == 3  # never more than three, and really three
    assert sorted(r.target for r in fetcher.asked) == sorted(queries)
    (fulfilled,) = journal.events(RESEARCH_EVENT)
    snapshot_ids = fulfilled.payload["snapshots"].split(",")
    assert len(snapshot_ids) == 6
    shown = [
        m.group(1) for m in re.finditer(r"\[S:([0-9a-f]{64})\] [A-Z]+ '([^']+)'", llm.prompts[1])
    ]
    targets = re.findall(r"\[S:[0-9a-f]{64}\] [A-Z]+ '([^']+)'", llm.prompts[1])
    assert targets == [q for q in queries if q != "leader ltd query 4"]  # request order
    assert shown == snapshot_ids
    assert "leader ltd query 4" in fulfilled.payload["unfulfilled"]


def test_identical_requests_from_two_managers_at_once_cost_one_fetch(
    world: World, tmp_path: Path
) -> None:
    commons = _commons(world, tmp_path)
    fetcher = ConcurrencyProbe(want=1)
    request = FetchRequest.query("leader ltd query 1", SESSION)
    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(
            pool.map(lambda _: commons.snapshots.get_or_fetch(request, fetcher), range(3))
        )
    assert len(fetcher.asked) == 1
    assert sorted(o.cache_hit for o in outcomes) == [False, True, True]


def test_a_query_that_failed_is_not_refetched_when_asked_again_and_says_why(
    world: World, tmp_path: Path
) -> None:
    fetcher = StubFetcher(fail=frozenset({"broken query"}))
    _, journal, llm = _run(
        world,
        tmp_path,
        [research([], ["broken query"]), research([], ["broken query"]), research(), final(hold())],
        fetcher=fetcher,
    )
    assert [r.target for r in fetcher.asked] == ["broken query"]
    first, second = journal.events(RESEARCH_EVENT)
    assert "stub refused broken query" in first.payload["unfulfilled"]
    assert "already attempted this session" in second.payload["unfulfilled"]
    assert "already attempted this session" in llm.prompts[2]


def test_a_failed_fetch_is_shown_as_unfulfilled_and_a_repeat_query_is_a_cache_hit(
    world: World, tmp_path: Path
) -> None:
    fetcher = StubFetcher(fail=frozenset({"broken query"}))
    _, journal, llm = _run(
        world,
        tmp_path,
        [
            research([], ["broken query", "good query"]),
            research([], ["good query"]),
            research(),
            final(hold()),
        ],
        fetcher=fetcher,
    )
    fulfilled = journal.events(RESEARCH_EVENT)
    assert "broken query" in fulfilled[0].payload["unfulfilled"]
    assert "Not fulfilled" in llm.prompts[1]
    assert fulfilled[1].payload["snapshots"] == ""  # asked twice in the session, fetched once
    # Each asked exactly once. The two run concurrently (M17.12), so the order they reached the
    # fetcher in is not a property; the order they are shown in is (tested above).
    assert sorted(r.target for r in fetcher.asked) == ["broken query", "good query"]


# ── acceptance 1: the contract refuses what the prompt promises it will ──────────────────────────


def test_an_invented_citation_voids_the_decision(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    bad = buy(atr_pct[LEADER], rationale=f"Strong [F:{LEADER}.made_up_field] and [S:{'0' * 64}].")
    result, journal = _one_buy_session(world, tmp_path, bad)
    reasons = _reasons(result)
    assert f"unknown citation [F:{LEADER}.made_up_field]" in reasons
    assert f"unknown citation [S:{'0' * 64}]" in reasons
    assert {v.decision.isin for v in result.accepted} == {HELD}  # the rest of the session stands
    (line,) = journal.events(REFUSED_EVENT)
    assert line.isin == LEADER and line.decision is Decision.HOLD
    assert not [e for e in journal.events(DECISION_EVENT) if e.isin == LEADER]


def test_a_refusal_names_its_reason_codes_on_the_log_line_the_journal_and_the_digest(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    """M17.12: ``fm.decision_refused`` logged ``reasons=1`` and nothing a reader could act on."""
    from analyst.fundmanager.digest import render_digest
    from analyst.fundmanager.scoreboard import (
        ScoreboardInputs,
        build_scoreboard,
        todays_decisions,
    )

    # A credential-shaped value, assembled at runtime so this file trips no secret scanner.
    planted = "-".join(("sk", "ant", "api03", "".join(chr(65 + i % 26) for i in range(32))))
    secret_ish = f"adjusted for the {planted} rumour"
    bad = buy(
        atr_pct[LEADER],
        rationale=f"Strong [F:{LEADER}.made_up_field].",
        adjustments=[{"reason": secret_ish, "direction": "UP", "size_pp": 1, "citation": "none"}],
    )
    watch = {**passed(BREAKOUT), "action": "WATCH", "target_weight": 3}
    with capture_logs() as logs:
        result, journal, _ = _run(
            world,
            tmp_path,
            [research([LEADER, BREAKOUT]), research(), final(hold(), bad, watch)],
        )
    by_isin = {v.decision.isin: v for v in result.refused}
    assert by_isin[LEADER].codes == (
        ReasonCode.UNCITED_ADJUSTMENT,
        ReasonCode.UNKNOWN_CITATION,
    )
    assert by_isin[BREAKOUT].codes == (ReasonCode.WEIGHT,)

    lines = {e["isin"]: e for e in logs if e["event"] == "fm.decision_refused"}
    assert lines[LEADER]["reason_codes"] == ["UNCITED_ADJUSTMENT", "UNKNOWN_CITATION"]
    assert f"unknown citation [F:{LEADER}.made_up_field]" in lines[LEADER]["reasons"]
    assert all(planted not in r for r in lines[LEADER]["reasons"])  # masked on the log
    assert lines[BREAKOUT]["reason_codes"] == ["WEIGHT"]
    assert lines[BREAKOUT]["reasons"] == ["a WATCH carries no weight, got 3"]

    refused = {e.isin: e for e in journal.events(REFUSED_EVENT)}
    assert refused[LEADER].payload["reason_codes"] == "UNCITED_ADJUSTMENT,UNKNOWN_CITATION"
    assert refused[BREAKOUT].payload["reason_codes"] == "WEIGHT"

    roster = load_roster()
    session, decided = todays_decisions(journal.entries, roster)
    assert session == SESSION
    page = render_digest(build_scoreboard(roster, ScoreboardInputs(s0=None)), SESSION, decided)
    assert "| Refused for |" in page
    leader_row = next(r for r in page.splitlines() if "FM_DECISION_REFUSED" in r and LEADER in r)
    assert leader_row.rstrip().endswith("| UNCITED_ADJUSTMENT,UNKNOWN_CITATION |")
    assert "made_up_field" not in page and "rumour" not in page  # codes, never messages


def test_every_contract_breach_carries_a_code_one_per_reason(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    bad = buy(
        atr_pct[LEADER],
        edge_type="NONE",
        target_weight=50,
        stop_pct=40,
        scenarios=scenarios(0.3, 0.45, 0.35),
        base_rate_cell={"cell_id": "S9.none", "p_beat": 0.6},
    )
    result, _ = _one_buy_session(world, tmp_path, bad)
    (verdict,) = result.refused
    assert len(verdict.codes) == len(verdict.reasons) >= 5
    assert {
        ReasonCode.EDGE_NONE_BUY,
        ReasonCode.WEIGHT,
        ReasonCode.SCENARIO_SUM,
        ReasonCode.CELL_NOT_SHOWN,
        ReasonCode.STOP_OVER_CEILING,
    } <= set(verdict.codes)


def test_a_real_citation_of_every_kind_resolves(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    good = buy(
        atr_pct[LEADER],
        evidence_refs=[
            f"F:{LEADER}.atr14_pct",
            "[F:market.breadth_above_sma50]",
            f"F:{CELL}",
            "F:cost.mid",
            f"F:{LEADER}.deal_1",
        ],
    )
    result, _ = _one_buy_session(world, tmp_path, good)
    assert LEADER in {v.decision.isin for v in result.accepted}, result.refused


def test_scenarios_not_summing_to_one_are_refused(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    bad = buy(atr_pct[LEADER], scenarios=scenarios(0.3, 0.45, 0.35))  # sums to 1.10
    result, _ = _one_buy_session(world, tmp_path, bad)
    assert "sum to 1.10, not 1" in _reasons(result)


def _scen(bull: str, base: str, bear: str, *, base_x: str = "1") -> list[Scenario]:
    return [
        Scenario(
            name=ScenarioName.BULL,
            probability=Decimal(bull),
            excess_pct=Decimal(10),
            description="a",
        ),
        Scenario(
            name=ScenarioName.BASE,
            probability=Decimal(base),
            excess_pct=Decimal(base_x),
            description="b",
        ),
        Scenario(
            name=ScenarioName.BEAR,
            probability=Decimal(bear),
            excess_pct=Decimal(-8),
            description="c",
        ),
    ]


def test_a_base_case_within_two_pp_of_zero_is_a_coin_flip_not_a_win() -> None:
    """Amendment 3. Fails if the dead band is removed: v1 counted the +1 % base as a win (0.70)."""
    pinned = (P_DEAD_BAND_PP, P_TOLERANCE)
    assert pinned == (Decimal(2), Decimal("0.20"))
    assert implied_p_beat(_scen("0.3", "0.4", "0.3", base_x="1")) == Decimal("0.50")
    assert implied_p_beat(_scen("0.3", "0.4", "0.3", base_x="-1")) == Decimal("0.50")
    assert implied_p_beat(_scen("0.3", "0.4", "0.3", base_x="2")) == Decimal("0.50")
    # Outside the band the base case counts whole, either way.
    assert implied_p_beat(_scen("0.3", "0.4", "0.3", base_x="2.5")) == Decimal("0.70")
    assert implied_p_beat(_scen("0.3", "0.4", "0.3", base_x="-2.5")) == Decimal("0.30")


def test_a_zero_excess_scenario_counts_as_a_coin_flip() -> None:
    assert implied_p_beat(_scen("0.2", "0.6", "0.2", base_x="0")) == Decimal("0.5")


def test_p_must_be_on_the_scenarios_side_of_one_half_and_within_the_band() -> None:
    """Amendment 3's rule; each assertion fails if its half of the rule is inverted or dropped."""
    # The dry run's template memo: P* = 0.50, and an honest 0.53 now agrees with it.
    assert p_consistent(Decimal("0.53"), Decimal("0.50"))
    assert p_consistent(Decimal("0.30"), Decimal("0.50"))  # at P* = 1/2 either side is fine
    # Sign: within 0.20 but on the other side of 1/2.
    assert not p_consistent(Decimal("0.48"), Decimal("0.60"))
    assert not p_consistent(Decimal("0.55"), Decimal("0.40"))
    assert p_consistent(Decimal("0.52"), Decimal("0.60"))
    assert p_consistent(Decimal("0.50"), Decimal("0.60"))
    # Band: same side, but more than 0.20 away.
    assert not p_consistent(Decimal("0.90"), Decimal("0.60"))
    assert p_consistent(Decimal("0.80"), Decimal("0.60"))


def test_p_beat_bench_must_agree_with_the_scenarios_under_the_amended_rule(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    # scenarios(): BASE +2 is inside the dead band, so P* = 0.3 + 0.45/2 = 0.525.
    assert implied_p_beat(_scen("0.3", "0.45", "0.25", base_x="2")) == Decimal("0.525")
    result, _ = _one_buy_session(world, tmp_path, buy(atr_pct[LEADER], p_beat_bench=0.53))
    assert LEADER in {v.decision.isin for v in result.accepted}, result.refused
    result, _, _ = _run(
        world,
        tmp_path / "b",
        [
            research([LEADER]),
            research(),
            final(hold(), buy(atr_pct[LEADER], p_beat_bench=0.49)),
            final(hold(), buy(atr_pct[LEADER], p_beat_bench=0.49)),  # the repair, unchanged
        ],
    )
    assert "implied P* = 0.525" in _reasons(result)
    assert "same side of 0.5" in _reasons(result)


def _sell(**over: Any) -> dict[str, Any]:
    return hold(
        action="SELL",
        what_changed={"kind": "INVALIDATION", "text": "close below the 50-session mean"},
        **over,
    )


def test_an_exit_is_never_voided_by_its_memo_arithmetic(world: World, tmp_path: Path) -> None:
    """Amendment 3 (EVT's 2026-10-15 Swaraj sell). Fails if exits are checked like buys."""
    # P* = 0.3 + 0.45 = 0.75 (BASE +3 is outside the dead band) against p = 0.30, and the
    # probabilities sum to 1.05: both would void a BUY, neither voids the exit.
    memo = [
        {"name": "BULL", "probability": 0.3, "excess_pct": 5, "description": "a"},
        {"name": "BASE", "probability": 0.45, "excess_pct": 3, "description": "b"},
        {"name": "BEAR", "probability": 0.3, "excess_pct": -5, "description": "c"},
    ]
    result, _, llm = _run(
        world, tmp_path, [research(), final(_sell(p_beat_bench=0.3, scenarios=memo))]
    )
    assert [v.decision.action for v in result.accepted] == [Action.SELL], result.refused
    assert not result.refused and llm.calls == 2  # no contract repair either
    trim = _sell(p_beat_bench=0.3, scenarios=memo)
    trim.update(action="TRIM", target_weight=1)
    result, _, _ = _run(world, tmp_path / "t", [research(), final(trim)])
    assert [v.decision.action for v in result.accepted] == [Action.TRIM], result.refused


def test_an_exits_other_checks_still_stand(world: World, tmp_path: Path) -> None:
    """Only the arithmetic is waived: an exit citing something it was never shown is voided."""
    bad = _sell(rationale=f"Broke down [F:{HELD}.made_up_field].")
    result, _, _ = _run(world, tmp_path, [research(), final(bad)])
    assert not result.accepted
    assert ReasonCode.UNKNOWN_CITATION in result.refused[0].codes


# ── Amendment 3: one contract repair for P_DISAGREES ──────────────────────────────────────────────


def test_a_p_disagreement_gets_one_contract_repair_and_the_repaired_buy_is_staged(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    disagreeing = buy(atr_pct[LEADER], p_beat_bench=0.45)  # P* = 0.525: the wrong side of 1/2
    fixed = buy(atr_pct[LEADER], p_beat_bench=0.55)
    result, journal, llm = _run(
        world,
        tmp_path,
        [research([LEADER]), research(), final(hold(), disagreeing), final(hold(), fixed)],
    )
    assert result.status is SessionStatus.DECIDED
    assert result.calls == 3 and result.repairs == 1 and llm.calls == 4
    assert "voided by the decision contract" in llm.prompts[-1]
    assert f"BUY {LEADER}:" in llm.prompts[-1] and "within 0.20" in llm.prompts[-1]
    # The first-pass void is journaled, then the repaired decision, citing the repair call.
    (void,) = journal.events(REFUSED_EVENT)
    assert void.isin == LEADER and "contract_repair" not in void.payload
    repair_call = journal.events(CALL_EVENT)[-1]
    assert repair_call.payload["repair"] == "true"
    assert repair_call.payload["stage"] == CONTRACT_REPAIR_STAGE
    assert int(repair_call.payload["input_tokens"]) > 0  # its tokens are counted
    repaired = [e for e in journal.events(DECISION_EVENT) if e.isin == LEADER]
    assert len(repaired) == 1 and repaired[0].payload["contract_repair"] == "true"
    assert repaired[0].evidence_snapshot_ref == repair_call.evidence_snapshot_ref
    assert LEADER in {v.decision.isin for v in result.accepted}
    _assert_every_ref_resolves(journal)


def test_the_contract_repair_reads_only_the_voided_names(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    disagreeing = buy(atr_pct[LEADER], p_beat_bench=0.45)
    sneaky = _sell()  # the repair also tries to change the accepted HOLD: ignored
    result, journal, _ = _run(
        world,
        tmp_path,
        [
            research([LEADER]),
            research(),
            final(hold(), disagreeing),
            final(sneaky, buy(atr_pct[LEADER], p_beat_bench=0.56)),
        ],
    )
    held = [v.decision for v in result.accepted if v.decision.isin == HELD]
    assert [d.action for d in held] == [Action.HOLD]
    assert not [e for e in journal.events(DECISION_EVENT) if e.payload.get("action") == "SELL"]


def test_a_repair_that_still_disagrees_is_voided_again_and_journaled(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    disagreeing = buy(atr_pct[LEADER], p_beat_bench=0.45)
    result, journal, llm = _run(
        world,
        tmp_path,
        [research([LEADER]), research(), final(hold(), disagreeing), final(hold(), disagreeing)],
    )
    assert llm.calls == 4 and LEADER not in {v.decision.isin for v in result.accepted}
    voids = journal.events(REFUSED_EVENT)
    assert [v.payload.get("contract_repair") for v in voids] == [None, "true"]


def test_a_malformed_contract_repair_leaves_the_voids_and_never_ends_the_session(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    disagreeing = buy(atr_pct[LEADER], p_beat_bench=0.45)
    result, journal, _ = _run(
        world,
        tmp_path,
        [research([LEADER]), research(), final(hold(), disagreeing), {"decisions": "nope"}],
    )
    assert result.status is SessionStatus.DECIDED
    assert {v.decision.isin for v in result.accepted} == {HELD}
    (failed,) = journal.events(CONTRACT_REPAIR_FAILED_EVENT)
    assert failed.payload["stage"] == CONTRACT_REPAIR_STAGE
    assert not journal.events(MANAGER_ERROR_EVENT)


def test_a_failed_contract_repair_call_is_journaled_not_a_manager_error(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    secret = _fake_secret("contract-repair-call")
    disagreeing = buy(atr_pct[LEADER], p_beat_bench=0.45)
    result, journal, _ = _run(
        world,
        tmp_path,
        [
            research([LEADER]),
            research(),
            final(hold(), disagreeing),
            LLMError(f"cli exited 1 password={secret}"),
        ],
    )
    assert result.status is SessionStatus.DECIDED
    assert journal.events(CONTRACT_REPAIR_FAILED_EVENT)
    assert secret not in _journaled_text(journal)


def test_a_void_for_any_other_reason_gets_no_contract_repair(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    bad = buy(atr_pct[LEADER], rationale=f"Strong [F:{LEADER}.made_up_field].")
    result, _, llm = _run(world, tmp_path, [research([LEADER]), research(), final(hold(), bad)])
    assert llm.calls == 3 and result.repairs == 0


def test_the_contract_repair_prompt_is_masked_and_recorded_as_sent(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    """A voided decision's reasons can quote the model (an uncited adjustment's reason)."""
    secret = _fake_secret("contract-repair-reason")
    disagreeing = buy(
        atr_pct[LEADER],
        p_beat_bench=0.45,
        adjustments=[
            {"reason": f"token={secret}", "direction": "UP", "size_pp": 1, "citation": "none"}
        ],
    )
    result, journal, llm = _run(
        world,
        tmp_path,
        [research([LEADER]), research(), final(hold(), disagreeing), final(hold(), passed(LEADER))],
    )
    assert result.repairs == 1 and not any(secret in p for p in llm.prompts)
    repair_call = journal.events(CALL_EVENT)[-1]
    assert (
        journal.evidence[repair_call.evidence_snapshot_ref or ""].rendered_prompt
        == (llm.prompts[-1])
    )
    assert secret not in _journaled_text(journal)
    _assert_every_ref_resolves(journal)


def test_an_edge_type_none_buy_is_refused(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    result, _ = _one_buy_session(world, tmp_path, buy(atr_pct[LEADER], edge_type="NONE"))
    assert "edge_type NONE forces PASS, WATCH or HOLD" in _reasons(result)


def test_a_buy_must_clear_its_own_round_trip_on_the_shared_cost_model(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    result, _ = _one_buy_session(world, tmp_path, buy(atr_pct[LEADER], cost_hurdle_check=-0.1))
    assert "needs a positive cost_hurdle_check" in _reasons(result)
    # A positive stated check cannot carry a trade whose own expected excess is below the trip.
    thin = buy(
        atr_pct[LEADER],
        expected_excess_pct=0.05,
        p_beat_bench=0.5,
        scenarios=[
            {"name": "BULL", "probability": 0.3, "excess_pct": 1, "description": "a"},
            {"name": "BASE", "probability": 0.4, "excess_pct": 0, "description": "b"},
            {"name": "BEAR", "probability": 0.3, "excess_pct": -0.8, "description": "c"},
        ],
    )
    result, _ = _one_buy_session(world, tmp_path / "t", thin)
    reasons = _reasons(result)
    assert "does not clear the" in reasons and "scenarios' mean excess 0.06" in reasons


def test_a_buy_stop_outside_one_and_a_half_to_three_atr_or_over_fifteen_pct_is_refused(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    atr = atr_pct[LEADER] * 100
    tight = float((atr * Decimal("1.4")).quantize(Decimal("0.01")))
    wide = float((atr * Decimal("3.1")).quantize(Decimal("0.01")))
    for k, stop in enumerate((tight, wide)):
        result, _ = _one_buy_session(world, tmp_path / str(k), buy(atr_pct[LEADER], stop_pct=stop))
        assert "outside 1.5-3 x ATR" in _reasons(result)
    result, _ = _one_buy_session(world, tmp_path / "x", buy(atr_pct[LEADER], stop_pct=16))
    assert "exceeds the 15% ceiling" in _reasons(result)


def test_a_loosened_stop_is_refused_and_a_tightened_one_accepted(
    world: World, tmp_path: Path
) -> None:
    close = next(r.close for r in world.sheets.universe if r.isin == HELD)
    current = (close - HELD_STOP) / close * 100  # the stop's distance below today's close
    looser = float((current + 1).quantize(Decimal("0.01")))
    tighter = float((current - 1).quantize(Decimal("0.01")))
    result, journal, _ = _run(world, tmp_path, [research(), final(hold(new_stop_pct=looser))])
    assert "a stop can be tightened, never loosened" in _reasons(result, HELD)
    assert not journal.events(DECISION_EVENT)
    result, journal, _ = _run(
        world, tmp_path / "t", [research(), final(hold(new_stop_pct=tighter))]
    )
    (verdict,) = result.accepted
    assert verdict.stop_price is not None and verdict.stop_price > HELD_STOP
    assert journal.events(DECISION_EVENT)[0].payload["new_stop_pct"] == str(Decimal(str(tighter)))


def test_a_base_rate_cell_of_the_wrong_tier_or_a_misquoted_number_is_refused(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    wrong_tier = buy(
        atr_pct[LEADER],
        base_rate_cell={
            "cell_id": "S1.large.RISK_ON.h20",
            "p_beat": 0.6,
            "median_excess": None,
            "iqr_excess": None,
        },
    )
    result, _ = _one_buy_session(world, tmp_path, wrong_tier)
    assert "is tier large; " in _reasons(result)
    misquoted = buy(
        atr_pct[LEADER],
        base_rate_cell={"cell_id": CELL, "p_beat": 0.7, "median_excess": None, "iqr_excess": None},
    )
    result, _ = _one_buy_session(world, tmp_path / "m", misquoted)
    assert "is not the table's 0.600000" in _reasons(result)


def test_a_decision_on_a_name_never_researched_is_refused(world: World, tmp_path: Path) -> None:
    result, _, _ = _run(world, tmp_path, [research(), final(hold(), passed(BREAKOUT))])
    assert "neither held nor researched" in _reasons(result, BREAKOUT)


def test_no_buy_is_admitted_when_the_surveillance_list_is_stale(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    blocked_screens = world.screens.model_copy(
        update={"exclusions": world.screens.exclusions.model_copy(update={"buys_blocked": True})}
    )
    blocked_screens = blocked_screens.model_copy(
        update={"screens_digest": CommonsScreens.digest_of(blocked_screens.body())}
    )
    blocked = World(world.sheets, blocked_screens, world.shortlist, world.table)
    result, _ = _one_buy_session(blocked, tmp_path, buy(atr_pct[LEADER]))
    assert "no new BUY is admitted this session" in _reasons(result)


# ── acceptance 4: SELL needs what_changed; p in [0, 1] ───────────────────────────────────────────


def test_a_sell_without_what_changed_is_refused_by_the_schema() -> None:
    answer = final(hold(action="SELL"))
    with pytest.raises(MalformedOutputError, match="must name what_changed"):
        parse_decisions(answer, holdings=[HELD], researched=[])
    trimmed = final(hold(action="TRIM", target_weight=4))
    with pytest.raises(MalformedOutputError, match="must name what_changed"):
        parse_decisions(trimmed, holdings=[HELD], researched=[])
    named = final(
        hold(action="SELL", what_changed={"kind": "INVALIDATION", "text": "close broke the mean"})
    )
    assert parse_decisions(named, holdings=[HELD], researched=[]).decisions[0].action is Action.SELL


@pytest.mark.parametrize("p", [1.2, -0.01])
def test_p_beat_bench_outside_zero_one_is_refused(p: float) -> None:
    with pytest.raises(MalformedOutputError, match="p_beat_bench"):
        parse_decisions(final(hold(p_beat_bench=p)), holdings=[HELD], researched=[])


def test_a_malformed_answer_gets_one_repair_retry_outside_the_budget(
    world: World, tmp_path: Path
) -> None:
    result, journal, llm = _run(
        world,
        tmp_path,
        [research(), final(hold(action="SELL")), final(hold())],
    )
    assert result.status is SessionStatus.DECIDED and result.calls == 2 and result.repairs == 1
    assert llm.calls == 3
    assert "Your previous answer was refused" in llm.prompts[-1]
    assert [c.payload["repair"] for c in journal.events(CALL_EVENT)] == ["false", "false", "true"]


def test_a_second_malformed_answer_journals_manager_error_and_stages_nothing(
    world: World, tmp_path: Path
) -> None:
    result, journal, llm = _run(
        world,
        tmp_path,
        [research(), final(hold(p_beat_bench=1.5)), final(hold(action="SELL"))],
    )
    assert result.status is SessionStatus.MANAGER_ERROR and not result.accepted
    assert llm.calls == 3 and result.repairs == 1
    (error,) = journal.events(MANAGER_ERROR_EVENT)
    assert error.decision is Decision.ESCALATE and error.payload["stage"] == "final"
    assert "nothing is staged" in (error.rationale or "")
    assert not journal.events(DECISION_EVENT)


def test_the_repair_is_one_per_session_not_one_per_call(world: World, tmp_path: Path) -> None:
    result, journal, llm = _run(
        world,
        tmp_path,
        [{"holdings": "nope"}, research(), final(hold(p_beat_bench=2))],
    )
    assert result.status is SessionStatus.MANAGER_ERROR and llm.calls == 3
    assert journal.events(MANAGER_ERROR_EVENT)[0].payload["stage"] == "final"


def _long_claim(text: str) -> dict[str, Any]:
    answer = research([LEADER])
    answer["requests"][0]["claim"] = text
    return answer


def test_why_an_answer_was_refused_is_logged_by_field_and_rule_never_by_its_text(
    world: World, tmp_path: Path
) -> None:
    """A refused answer names the field path, the broken rule, its bound and the value's length on
    the ``fm.call_malformed`` line, the repair's ``fm.call`` line, the ``fm.manager_error`` line
    and the MANAGER_ERROR payload — and none of them carries the answer's text."""
    secret_ish = "Z" * 610
    with capture_logs() as logs:
        result, journal, llm = _run(
            world, tmp_path, [_long_claim(secret_ish), _long_claim(secret_ish + "Z")]
        )
    assert result.status is SessionStatus.MANAGER_ERROR and llm.calls == 2
    malformed = [e for e in logs if e["event"] == "fm.call_malformed"]
    assert [e["violations"] for e in malformed] == [
        ["requests.0.claim string_too_long max_length=500 actual=610"],
        ["requests.0.claim string_too_long max_length=500 actual=611"],
    ]
    assert [e["repair"] for e in malformed] == [False, True]
    (repair_call,) = [e for e in logs if e["event"] == "fm.call" and e["repair"]]
    assert repair_call["repair_violations"] == [
        "requests.0.claim string_too_long max_length=500 actual=610"
    ]
    (error_line,) = [e for e in logs if e["event"] == "fm.manager_error"]
    assert error_line["violations"] == [
        "requests.0.claim string_too_long max_length=500 actual=611"
    ]
    assert "at most 500 characters" in error_line["reason"]
    (error,) = journal.events(MANAGER_ERROR_EVENT)
    assert json.loads(error.payload["violations"]) == error_line["violations"]
    # The repair prompt tells the model by how much it overshot.
    assert "(yours has 610 characters)" in llm.prompts[-1]
    assert not any("ZZZZZZZZZZ" in json.dumps(e, default=str) for e in logs)


def test_a_failed_model_call_is_a_manager_error(world: World, tmp_path: Path) -> None:
    result, journal, _ = _run(world, tmp_path, [LLMError("cli exited 1")])
    assert result.status is SessionStatus.MANAGER_ERROR and result.calls == 1
    (error,) = journal.events(MANAGER_ERROR_EVENT)
    assert error.payload["stage"] == "round0" and error.evidence_snapshot_ref is not None
    assert not journal.events(CALL_EVENT)


def test_every_holding_and_researched_name_needs_a_decision() -> None:
    with pytest.raises(MalformedOutputError, match="missing"):
        parse_decisions(final(hold()), holdings=[HELD], researched=[LEADER])


def test_action_vocabulary_matches_the_scoreboard() -> None:
    assert {a.value for a in Action} == {a.value for a in DecisionAction}


# ── acceptance 2 and 5: what the prompt shows ────────────────────────────────────────────────────


def _session(world: World, tmp_path: Path, mandate: ManagerMandate, book: ManagerBook) -> _Session:
    return _Session(
        mandate,
        SESSION,
        _commons(world, tmp_path),
        book,
        StubLLM(),
        StubFetcher(),
        journal=ListJournal(),
        clock=clock(),
        pricer=TokenPricer(load_price_card()),
        template=PromptTemplate.load(),
    )


def _all_prompts(world: World, tmp_path: Path, mandate: ManagerMandate) -> dict[str, str]:
    run = _session(world, tmp_path, mandate, _book(mandate.id))
    run._fulfil(parse_research_request([LEADER], ["leader ltd order book"], run), 0)
    return {key: run._prompt(key) for key in (ROUND_ZERO, ROUND_RESEARCH, ROUND_FINAL)}


def parse_research_request(isins: Sequence[str], queries: Sequence[str], run: _Session) -> Any:
    from analyst.fundmanager.schemas import parse_research

    request, _ = parse_research(research(isins, queries), run.limits)
    return request


def test_holdings_are_rendered_without_cost_basis_or_pnl(world: World, tmp_path: Path) -> None:
    # The view has no field a cost price or a profit could live in: the whole field set, pinned.
    assert {f.name for f in fields(Holding)} == {
        "isin",
        "sector",
        "weight_pct",
        "sessions_held",
        "opening_thesis",
        "invalidation",
        "stop_price",
        "evidence_since_entry",
        "forced_review",
        "suspended_since",  # M17.13: a date, never a price
    }
    book = _book()
    closes = {r.isin: r.close for r in world.sheets.universe}
    block = render_holdings(book, closes)
    assert HELD in block and "8.00%" in block and "below today's close" in block
    for word in ("cost", "p&l", "pnl", "profit", "loss", "paid", "entry price", "average"):
        assert word not in block.lower()
    for prompt in _all_prompts(world, tmp_path, _mandate()).values():
        assert str(HELD_STOP) not in prompt  # the stop price, which with the entry stop % is a cost
        assert str(NAV) not in prompt and f"{NAV:,}" not in prompt  # the book's value is its P&L
        assert "unrealised" not in prompt.lower() and "unrealized" not in prompt.lower()


#: Amendment 2 (f): each style's playbook heading in `prompts/manager.md`.
PLAYBOOKS = {
    "FM-SWING-BRK-10L": "### Your playbook: swing breakout (1\u20134 weeks)",
    "FM-SWING-EVT-10L": "### Your playbook: swing event (1\u20134 weeks)",
    "FM-POS-TREND-10L": "### Your playbook: positional trend (1\u20133 months)",
    "FM-POS-FUND-10L": "### Your playbook: positional fundamentals (1\u20133 months)",
}


@pytest.mark.parametrize("book_id", list(PLAYBOOKS))
def test_only_the_managers_own_style_block_and_the_current_round_block_render(
    world: World, tmp_path: Path, book_id: str
) -> None:
    mandate = _mandate(book_id)
    own = PLAYBOOKS[book_id]
    prompts = _all_prompts(world, tmp_path, mandate)
    titles = {
        ROUND_ZERO: "## Your task this round: triage and research requests",
        ROUND_RESEARCH: "## Your task this round: read the evidence",
        ROUND_FINAL: "## Your task this round: decide",
    }
    for key, prompt in prompts.items():
        assert own in prompt
        assert all(other not in prompt for other in PLAYBOOKS.values() if other != own)
        assert prompt.count("### Your playbook:") == 1
        assert titles[key] in prompt
        assert all(t not in prompt for k, t in titles.items() if k != key)
        assert "[[" not in prompt and "{{" not in prompt and "<!--" not in prompt
        assert f"# You are {mandate.manager}," in prompt
        assert f"Style **{mandate.style.value}**" in prompt


def test_a_mirror_book_is_never_handed_to_a_manager(world: World, tmp_path: Path) -> None:
    # Amendment 2 (b): the manager decides on its primary book only; the mirror follows it.
    with pytest.raises(ValueError, match="decides on its primary book only"):
        _run(world, tmp_path, [], mandate=_mandate("FM-SWING-BRK-1CR"))


def test_a_prompt_never_contains_another_managers_id_book_or_decisions(
    world: World, tmp_path: Path, atr_pct: dict[str, Decimal]
) -> None:
    roster = load_roster()
    own = "FM-SWING-BRK-10L"
    others = [b.id for b in roster.books if b.id != own]
    # Another manager's session first, into the same snapshot store: its decisions are journaled
    # in its own stream and must not reach this manager's prompts.
    _, other_journal, _ = _run(
        world,
        tmp_path,
        [research([LEADER], ["leader ltd order book"]), research(), final(passed(LEADER), hold())],
        mandate=_mandate("FM-POS-TREND-10L"),
        book=_book("FM-POS-TREND-10L"),
    )
    assert other_journal.events(DECISION_EVENT)
    _, journal, llm = _run(
        world,
        tmp_path,
        [
            research([LEADER], ["leader ltd order book"]),
            research(),
            final(hold(), buy(atr_pct[LEADER])),
        ],
    )
    for prompt in llm.prompts:
        for other in others:
            assert other not in prompt
        assert "FM-POS-TREND-10L" not in prompt
    assert all(e.case_id == own for e in journal.entries)
    for bundle in journal.evidence.values():
        assert bundle.case_id == own
        assert all(other not in (bundle.rendered_prompt or "") for other in others)


def test_a_manager_is_never_handed_another_managers_book(world: World, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sees only its own"):
        run_manager(
            _mandate(),
            SESSION,
            _commons(world, tmp_path),
            _book("FM-POS-TREND-10L"),
            StubLLM(),
            StubFetcher(),
            journal=ListJournal(),
            clock=clock(),
        )


def test_the_template_refuses_a_missing_value_and_never_rescans_injected_text() -> None:
    template = PromptTemplate.load()
    with pytest.raises(TemplateError, match="no value for placeholders"):
        template.render(style="SWING_BREAKOUT", round_key=ROUND_ZERO, values={})
    with pytest.raises(TemplateError, match="STYLE:INDEX"):
        template.render(style="INDEX", round_key=ROUND_ZERO, values={})
    with pytest.raises(TemplateError, match="STYLE:SWING"):  # the pre-Amendment-2 block is gone
        template.render(style="SWING", round_key=ROUND_ZERO, values={})
    names = set(re.findall(r"\{\{([a-z_]+)\}\}", template.text))
    injected = "{{manager_id}} [[STYLE:POSITIONAL_TREND]]"
    values = dict.fromkeys(names, "x") | {"research_bundles": injected}
    out = template.render(style="SWING_BREAKOUT", round_key=ROUND_FINAL, values=values)
    assert injected in out


# ── invariant #13: a harness error never reaches the journal or its evidence unmasked ────────────


def _fake_secret(seed: str) -> str:
    """Credential-shaped and built at runtime, so no literal here trips the repo's secret scan."""
    return hashlib.sha256(seed.encode()).hexdigest()


def _journaled_text(journal: ListJournal) -> str:
    """Everything the journal and its evidence store would hold, as one searchable string."""
    rows = [e.model_dump_json() for e in journal.entries]
    bundles = [b.canonical_bytes().decode() for b in journal.evidence.values()]
    return "\n".join(rows + bundles)


def test_a_claude_cli_failure_never_reaches_the_journal_or_evidence_unmasked(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real `ClaudeCliLLM`, its subprocess stubbed to fail with a secret on stderr.

    Fails on the code before the fix: the CLI's raw stderr went into the `LLMError`, the runtime
    wrapped it into the manager error, and `_write` journaled it as the rationale verbatim.
    """
    secret = _fake_secret("cli-stderr")
    stderr = f"fatal: request rejected; Authorization: Bearer {secret}\napi_key={secret}"
    monkeypatch.setattr("analyst.llm.claude_cli.shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        "analyst.llm.claude_cli.subprocess.run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 1, "", stderr),
    )
    mandate = _mandate()
    journal = ListJournal()
    result = run_manager(
        mandate,
        SESSION,
        _commons(world, tmp_path),
        _book(mandate.id),
        ClaudeCliLLM(),
        StubFetcher(),
        journal=journal,
        clock=clock(),
    )
    assert result.status is SessionStatus.MANAGER_ERROR
    (error,) = journal.events(MANAGER_ERROR_EVENT)
    assert "fatal: request rejected" in (error.rationale or "")  # still diagnosable
    assert secret not in _journaled_text(journal)


def test_a_raw_error_is_masked_again_at_the_journal_write(world: World, tmp_path: Path) -> None:
    """Defence in depth: an `LLMError` that skipped masking upstream is masked by `_write`."""
    secret = _fake_secret("raw-llm-error")
    result, journal, _ = _run(world, tmp_path, [LLMError(f"boom password={secret}")])
    assert result.status is SessionStatus.MANAGER_ERROR
    (error,) = journal.events(MANAGER_ERROR_EVENT)
    assert "boom password=***" in (error.rationale or "")
    assert secret not in _journaled_text(journal)


def test_a_digest_failure_is_masked_in_the_next_prompt_the_bundle_and_the_payload(
    world: World, tmp_path: Path
) -> None:
    """An on-demand digest error becomes an unfulfilled reason: shown, journaled, never raw."""
    secret = _fake_secret("digest-error")

    def failing_digests(isins: Sequence[str]) -> OnDemandDigests:
        raise LLMError(f"the Claude CLI failed: token={secret}")

    commons = ManagerCommons.from_builds(
        sheets=world.sheets,
        screens=world.screens,
        shortlist=world.shortlist,
        base_rates=world.table,
        source=FakeScreenSource(screen_world()),
        snapshots=SnapshotStore(tmp_path / "fetch", clock=clock()),
        cost_model=CostModel(load_rate_card()),
        slippage=SlippageModel(),
        digests=failing_digests,
    )
    mandate = _mandate()
    journal = ListJournal()
    llm = ScriptedStub([research([LEADER]), research(), final(hold(), passed(LEADER))])
    run_manager(
        mandate,
        SESSION,
        commons,
        _book(mandate.id),
        llm,
        StubFetcher(),
        journal=journal,
        clock=clock(),
    )
    (fulfilled, *_) = journal.events(RESEARCH_EVENT)
    assert "token=***" in fulfilled.payload["unfulfilled"]
    assert any("token=***" in p for p in llm.prompts[1:])  # the manager is still told why
    assert not any(secret in p for p in llm.prompts)
    assert secret not in _journaled_text(journal)


def _assert_every_ref_resolves(journal: ListJournal) -> None:
    """Every evidence ref the journal cites names a bundle the store actually holds."""
    cited = [e.evidence_snapshot_ref for e in journal.entries if e.evidence_snapshot_ref]
    assert cited, "the session cited no evidence at all"
    missing = sorted({r for r in cited if r not in journal.evidence})
    assert not missing, f"cited but never stored: {missing}"


def test_a_masked_repair_keeps_every_cited_ref_stored_and_the_sent_prompt_recorded(
    world: World, tmp_path: Path
) -> None:
    """A validation error quoting something maskable, then a valid repair.

    Fails on the code before the fix twice over: the repair prompt went out unmasked while the
    bundle recorded it masked, and the decisions cited the *unmasked* bundle's address, which was
    never stored.
    """
    secret = _fake_secret("repair-validation")
    malformed = final(hold(), passed(LEADER))
    malformed["decisions"][0][f"password={secret}"] = 1  # the schema error quotes the key
    result, journal, llm = _run(
        world,
        tmp_path,
        [research([LEADER]), research(), malformed, final(hold(), passed(LEADER))],
    )
    assert result.status is SessionStatus.DECIDED and result.repairs == 1
    assert not any(secret in p for p in llm.prompts)
    repair_call = journal.events(CALL_EVENT)[-1]
    assert repair_call.payload["repair"] == "true"
    recorded = journal.evidence[repair_call.evidence_snapshot_ref or ""].rendered_prompt
    assert llm.prompts[-1] == recorded
    assert "password=***" in llm.prompts[-1]
    decisions = journal.events(DECISION_EVENT)
    assert decisions and {d.evidence_snapshot_ref for d in decisions} == {
        repair_call.evidence_snapshot_ref
    }
    _assert_every_ref_resolves(journal)
    assert secret not in _journaled_text(journal)


def test_every_evidence_ref_a_full_session_writes_resolves_to_a_stored_bundle(
    world: World, tmp_path: Path
) -> None:
    result, journal, _ = _run(
        world,
        tmp_path,
        [research([LEADER], ["nse holidays 2026"]), research(), final(hold(), passed(LEADER))],
    )
    assert result.status is SessionStatus.DECIDED
    _assert_every_ref_resolves(journal)


def test_every_evidence_ref_a_manager_error_writes_resolves_to_a_stored_bundle(
    world: World, tmp_path: Path
) -> None:
    secret = _fake_secret("manager-error-ref")
    malformed = final(hold(), passed(LEADER))
    malformed[f"token={secret}"] = 1
    result, journal, _ = _run(world, tmp_path, [research(), malformed, malformed])
    assert result.status is SessionStatus.MANAGER_ERROR
    _assert_every_ref_resolves(journal)
    assert secret not in _journaled_text(journal)


def test_a_refused_credential_bearing_url_is_masked_in_the_journal_payload(
    world: World, tmp_path: Path
) -> None:
    """`FetchRequest` refuses the target; the unfulfilled `what` quoting it must not be raw."""
    secret = _fake_secret("refused-target")
    target = f"https://example.com/report?api_key={secret}"
    _, journal, llm = _run(world, tmp_path, [research([], [target]), research(), final(hold())])
    (fulfilled, *_) = journal.events(RESEARCH_EVENT)
    assert "api_key=***" in fulfilled.payload["unfulfilled"]
    assert secret not in _journaled_text(journal)
    assert not any(secret in p for p in llm.prompts[1:])


def test_the_prompt_states_the_contracts_probability_rule_with_its_numbers() -> None:
    """Amendment 3: the model is told the exact rule; fails if prompt and contract drift apart."""
    text = " ".join(PROMPT_PATH.read_text(encoding="utf-8").split())
    band = f"{P_DEAD_BAND_PP:f}"
    assert f"whose excess is above +{band} pp, plus half the mass" in text
    assert f"within ±{band} pp of zero" in text
    assert f"same side of 0.5 as `P*` (either may be exactly 0.5) and within {P_TOLERANCE}" in text
    assert "An exit is never voided for its scenarios' arithmetic" in text


# ── text-length limits the model can see (2026-10-31: notes of 510 chars on Bedrock) ────────────


def _bounded_strings(node: Any, path: str = "") -> list[tuple[str, dict[str, Any]]]:
    """Every schema node that is a string with a ``maxLength``, with its path."""
    out: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        kind = node.get("type")
        if "maxLength" in node and (kind == "string" or "string" in (kind or [])):
            out.append((path, node))
        for key, value in node.items():
            out += _bounded_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out += _bounded_strings(value, f"{path}[{i}]")
    return out


@pytest.mark.parametrize("which", ["research", "decision"])
def test_every_bounded_text_field_states_its_own_limit_where_bedrock_can_read_it(
    which: str,
) -> None:
    """Bedrock's structured output cannot compile ``maxLength``, so the limit must reach the model
    as words in the description — the same number the parser enforces, on every bounded field,
    and still there after the Bedrock transform. Dropping or changing either side fails here."""
    from analyst.llm.bedrock import structured_output_schema

    schema = research_schema(_mandate().rounds) if which == "research" else decision_schema()
    bounded = _bounded_strings(schema)
    assert len(bounded) >= (5 if which == "research" else 12)
    sent = json.dumps(structured_output_schema(schema))
    for path, node in bounded:
        limit = node["maxLength"]
        assert limit in (MAX_SHORT_CHARS, MAX_TEXT_CHARS), path
        assert f"at most {limit:,} characters" in node["description"], path
        assert json.dumps(node["description"])[1:-1] in sent, path


def test_the_bound_is_enforced_exactly_and_never_by_truncation() -> None:
    """A value at the limit is kept byte for byte; one character over is refused with the field,
    the rule and the length — never cut to fit (a cut note or claim changes what was asked)."""
    limits = _mandate().rounds
    at_limit = "n" * MAX_SHORT_CHARS
    answer = research([LEADER])
    answer["holdings"][0]["note"] = at_limit
    request, _ = parse_research(answer, limits)
    assert request.holdings[0].note == at_limit
    answer["holdings"][0]["note"] = at_limit + "n"
    with pytest.raises(MalformedOutputError) as refused:
        parse_research(answer, limits)
    (violation,) = refused.value.violations
    assert (violation.path, violation.constraint, violation.actual) == (
        "holdings.0.note",
        "string_too_long",
        MAX_SHORT_CHARS + 1,
    )
    assert violation.limit == f"max_length={MAX_SHORT_CHARS}"


def test_every_round_tells_the_manager_the_text_limits_the_schema_enforces(
    world: World, tmp_path: Path
) -> None:
    prompts = {k: " ".join(v.split()) for k, v in _all_prompts(world, tmp_path, _mandate()).items()}
    short = f"at most {MAX_SHORT_CHARS:,} characters"
    for key in (ROUND_ZERO, ROUND_RESEARCH):
        assert short in prompts[key] and "`claim`" in prompts[key], key
    assert "`note`" in prompts[ROUND_ZERO]
    assert "Do this working in your thinking" in prompts[ROUND_RESEARCH]
    assert "no holdings triage this round" in prompts[ROUND_RESEARCH]
    final_text = prompts[ROUND_FINAL]
    assert f"at most {MAX_TEXT_CHARS:,} characters each" in final_text
    assert f"`no_action_reason` — at most {MAX_SHORT_CHARS:,}" in final_text


def test_only_round_zero_is_offered_a_holdings_triage(world: World, tmp_path: Path) -> None:
    """Triage is round 0's step; the research rounds' schema has no ``holdings`` at all, so a
    manager cannot pour its per-name analysis into notes it would overrun (2026-10-31: 510 and 539
    characters on Bedrock, which cannot enforce ``maxLength``). Requests and queries keep exactly
    round 0's shape and caps."""
    result, _, llm = _run(
        world,
        tmp_path,
        [
            research([LEADER], ["q one"]),
            research([BREAKOUT], ["q two"]),
            research(),
            final(hold(), passed(LEADER), passed(BREAKOUT)),
        ],
    )
    assert result.status is SessionStatus.DECIDED
    round0, research1, research2, final_schema = llm.schemas
    assert "holdings" in round0["properties"] and "holdings" in round0["required"]
    for followup in (research1, research2):
        assert "holdings" not in followup["properties"]
        assert "holdings" not in followup["required"]
        assert followup["additionalProperties"] is False
        for key in ("requests", "queries"):
            assert followup["properties"][key] == round0["properties"][key]
    assert "decisions" in final_schema["properties"]


def test_the_followup_schema_is_hashed_with_the_others() -> None:
    from analyst.fundmanager.schemas import schema_bytes

    limits = _mandate().rounds
    document = json.loads(schema_bytes(limits, system_prompt="s"))
    assert document["research_followup"] == research_schema(limits, triage=False)
    assert "holdings" not in document["research_followup"]["properties"]


def test_identifier_fields_are_described_as_identifiers_never_as_prose(
    world: World, tmp_path: Path
) -> None:
    """An ``evidence_refs`` entry must be one bare citation (MALFORMED_REF otherwise), a cell id an
    id. Described as "text … about 80 words", they drew prose after the id on 2026-10-31 and the
    contract voided all ten of a manager's decisions. They keep their bound, stated as such."""
    item = decision_schema()["properties"]["decisions"]["items"]["properties"]
    ref = item["evidence_refs"]["items"]["description"]
    cell = item["base_rate_cell"]["anyOf"][0]["properties"]["cell_id"]["description"]
    citation = item["adjustments"]["items"]["properties"]["citation"]["description"]
    target = research_schema(_mandate().rounds)["properties"]["queries"]["items"]["properties"][
        "target"
    ]["description"]
    for text in (ref, cell, citation, target):
        assert "words" not in text and "text" not in text.lower()
        assert f"at most {MAX_SHORT_CHARS:,} characters" in text
    assert "[F:<field_id>] or [S:<snapshot_id>]" in ref and "nothing else" in ref
    final_text = " ".join(_all_prompts(world, tmp_path, _mandate())[ROUND_FINAL].split())
    assert "Each `evidence_refs` entry is one bare citation" in final_text
    texts = final_text.split("every other text —", 1)[1].split("A longer value is refused", 1)[0]
    assert "evidence_refs" not in texts and "cell_id" not in texts
