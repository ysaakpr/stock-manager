"""The GPT-6.1 Sol decision shadow evaluation (2026-11-27): routed, priced, rehearsal-only.

Pins that Sol is capability and never configuration: no roster role names it, the 21 books'
`mandate_hash` are the ones S0 journaled, and the decision-model override exists only for a
``--dry-run --memory`` rehearsal.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from accounting import TokenPricer, load_price_card
from analyst.fundmanager import load_roster
from analyst.fundmanager.job import mandate_fingerprints
from analyst.fundmanager.mandate import ManagerMandate
from analyst.llm.bedrock import load_bedrock_models
from analyst.llm.effort import load_effort_policy
from analyst.llm.openai_chat import ChatEndpoint
from backtest.fm_job import M17JobError, main
from backtest.fm_world import rehearsal_roster
from tests.unit.test_llm_bedrock import ASK
from tests.unit.test_llm_openai_chat import Endpoint, bedrock

SOL = "gpt-6-1-sol"
RUNTIME = "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"

#: The first 16 hex digits of every book's `mandate_hash` journaled at M17_S0 (2026-11-18), as
#: Amendment 6 (g) lists them and as the live journal holds them (checked 2026-11-27).
S0_HASHES = {
    "FM-POS-FUND-10L": "50fea31db617359f",
    "FM-POS-FUND-1CR": "73bde5d68a3db9f4",
    "FM-POS-TREND-10L": "cbb9528283592446",
    "FM-POS-TREND-1CR": "d926bb544562c100",
    "FM-SWING-BRK-10L": "2d318cddbc662d6e",
    "FM-SWING-BRK-1CR": "26b1557ea3af1e6f",
    "FM-SWING-EVT-10L": "e12af7509ebdf176",
    "FM-SWING-EVT-1CR": "330fe5a2f02399bd",
    "CTRL-FM-POS-FUND-10L": "944075938e7d40bc",
    "CTRL-FM-POS-FUND-1CR": "86df3c7d18dd8655",
    "CTRL-FM-POS-TREND-10L": "a869ea7f5dfb70f0",
    "CTRL-FM-POS-TREND-1CR": "84c8f994f3265034",
    "CTRL-FM-SWING-BRK-10L": "9122d824085a7613",
    "CTRL-FM-SWING-BRK-1CR": "52a85449cdc49988",
    "CTRL-FM-SWING-EVT-10L": "c20520e9657a08a0",
    "CTRL-FM-SWING-EVT-1CR": "7376d82cc7266f94",
    "STYLE-FM-POS-FUND": "42b94da5c0c390dd",
    "STYLE-FM-POS-TREND": "111cd6be17096c07",
    "STYLE-FM-SWING-BRK": "aa002f1e50d9105f",
    "STYLE-FM-SWING-EVT": "3beac69c14232f21",
    "BENCH-N500": "4cc6e90eb67306c4",
}


def test_the_21_books_keep_the_hashes_s0_started_from() -> None:
    hashes = {k: v.mandate_hash[:16] for k, v in mandate_fingerprints(load_roster()).items()}
    assert hashes == S0_HASHES


def test_no_roster_role_names_sol() -> None:
    for book in load_roster().books:
        if isinstance(book, ManagerMandate):
            assert book.models.decision == "claude-opus-5-5"
            assert SOL not in (book.models.digest, book.models.research)


def test_sol_is_routed_on_the_global_profile_with_opus_behind_it() -> None:
    models = load_bedrock_models()
    route = models.route(SOL)
    assert route.bedrock_id == "global.openai.gpt-6.1-sol"
    assert route.chat_endpoint is ChatEndpoint.RUNTIME_OPENAI
    assert route.bedrock_fallback_model == "claude-opus-5-5"
    assert models.backup_model(SOL) == "claude-opus-5-5"
    # decides at the decision effort, as Opus does
    policy = load_effort_policy()
    assert policy.effort_for(SOL) == policy.effort_for("claude-opus-5-5")


def _sol_answer(**details: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "global.openai.gpt-6.1-sol",
            "choices": [
                {"index": 0, "message": {"content": "ok"}, "finish_reason": "stop"},
            ],
            "usage": {
                "prompt_tokens": 1_000_000,
                "completion_tokens": 100_000,
                "prompt_tokens_details": details,
                "completion_tokens_details": {"reasoning_tokens": 60_000},
            },
        },
    )


def test_a_sol_cache_write_is_its_own_bucket_and_priced_at_the_global_rate() -> None:
    endpoint = Endpoint(_sol_answer(cached_tokens=600_000, cache_write_tokens=300_000))
    response = bedrock(endpoint).complete(ASK, model=SOL)
    assert str(endpoint.requests[-1].url) == f"{RUNTIME}/chat/completions"
    assert json.loads(endpoint.requests[-1].content)["reasoning_effort"] == "high"
    usage = response.usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (
        100_000,
        600_000,
        300_000,
    )
    priced = TokenPricer(load_price_card()).price(
        response, on=date(2026, 11, 18), purpose="m17_manager"
    )
    # 0.1 M x $2.00 + 0.6 M x $0.10 + 0.3 M x $2.50 + 0.1 M x $10.00
    assert priced.cost_usd == Decimal("2.01")


def test_without_cache_details_every_prompt_token_is_input() -> None:
    response = bedrock(Endpoint(_sol_answer())).complete(ASK, model=SOL)
    assert response.usage.input_tokens == 1_000_000
    assert response.usage.cache_write_tokens == 0


@pytest.mark.parametrize(
    ("memory", "dry_run", "start"),
    [
        (False, True, None),
        (True, False, None),
        (False, False, None),
        (True, True, date(2026, 11, 26)),
    ],
)
def test_the_decision_model_override_is_refused_outside_a_dry_memory_rehearsal(
    memory: bool, dry_run: bool, start: date | None
) -> None:
    with pytest.raises(M17JobError, match="shadow-rehearsal override"):
        rehearsal_roster(
            load_roster(), decision_model=SOL, memory=memory, dry_run=dry_run, start=start
        )


def test_the_cli_refuses_the_override_on_a_persisted_run_before_reading_anything() -> None:
    with pytest.raises(M17JobError, match="shadow-rehearsal override"):
        main(["--dry-run", "--session", "2026-11-26", "--decision-model", SOL])


def test_an_unrouted_decision_model_is_refused() -> None:
    from analyst.llm.client import LLMError

    with pytest.raises(LLMError, match="not mapped"):
        rehearsal_roster(
            load_roster(), decision_model="gpt-guess", memory=True, dry_run=True, start=None
        )


def test_the_override_changes_only_the_decision_model_and_only_in_the_copy() -> None:
    roster = load_roster()
    shadow = rehearsal_roster(roster, decision_model=SOL, memory=True, dry_run=True, start=None)
    for live, copy in zip(roster.books, shadow.books, strict=True):
        if isinstance(live, ManagerMandate):
            assert isinstance(copy, ManagerMandate)
            assert copy.models.decision == SOL
            assert copy.models.digest == live.models.digest
            assert copy.models.research == live.models.research
            assert live.models.decision == "claude-opus-5-5"
        else:
            assert copy == live
    # the copy is a different manager (its hash moves); the roster on disk is untouched
    shadow_hashes = {k: v.mandate_hash[:16] for k, v in mandate_fingerprints(shadow).items()}
    assert shadow_hashes["FM-SWING-BRK-10L"] != S0_HASHES["FM-SWING-BRK-10L"]
    assert shadow_hashes["CTRL-FM-SWING-BRK-10L"] == S0_HASHES["CTRL-FM-SWING-BRK-10L"]
    test_the_21_books_keep_the_hashes_s0_started_from()
