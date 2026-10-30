"""`BedrockLLM.run_tools`: a bounded client-side tool loop, offline, with fake tools.

The capability the Commons' own search and fetch tools (Brave, Tavily, Firecrawl) will run on: the
model calls a tool the caller offered, the caller's code runs it, the result goes back, up to a
bounded number of rounds. Pinned here: `strict` tool schemas and `tool_choice: auto` only (a forced
choice is a 400 on Opus 5.5 / Sonnet 5.5); the assistant turn — thinking and its signature
included — goes back whole; a tool's failure is shown to the model, a caller's bug is not
swallowed; the round bound holds; and the usage of every round is summed and priceable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from typing import Any

import pytest
from structlog.testing import capture_logs

from accounting import TokenPricer, load_price_card
from analyst.llm import LLMError, LLMUnavailableError, Message, Role, ToolCall, ToolSpec
from analyst.llm.bedrock import (
    ToolExecutionError,
    ToolLoopExhaustedError,
    ToolOutcome,
    structured_output_schema,
)
from tests.unit.test_llm_bedrock import Endpoint, api_error, bedrock, message_stream, sse, streamed

ASK = [Message(Role.USER, "What did the company announce this week?")]

WEB_SEARCH = ToolSpec(
    name="web_search",
    description="Search the web.",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string", "maxLength": 200}},
        "required": ["query"],
    },
)
WEB_FETCH = ToolSpec(
    name="web_fetch",
    description="Fetch one page.",
    input_schema={
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
        "additionalProperties": False,
    },
)
SUMMARY = ToolSpec(
    name="summary",
    description="The answer.",
    input_schema={
        "type": "object",
        "properties": {"headline": {"type": "string"}},
        "required": ["headline"],
        "additionalProperties": False,
    },
)


def tool_round(*calls: tuple[str, str, Mapping[str, Any]], input_tokens: int = 1_000) -> bytes:
    """One streamed round that stops for `tool_use`: thinking, then each tool call."""
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": 200,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 100,
    }
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_round",
                "type": "message",
                "role": "assistant",
                "model": "anthropic.claude-sonnet-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {**usage, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "search first"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig-1"},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    for offset, (call_id, name, arguments) in enumerate(calls, start=1):
        events += [
            {
                "type": "content_block_start",
                "index": offset,
                "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": offset,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(arguments)},
            },
            {"type": "content_block_stop", "index": offset},
        ]
    events += [
        {
            "type": "message_delta",
            "delta": {"type": "message_delta", "stop_reason": "tool_use", "stop_sequence": None},
            "usage": usage,
        },
        {"type": "message_stop"},
    ]
    return sse(*events)


class Tools:
    """The caller's executor: records every call, answers from a script."""

    def __init__(self, answers: Mapping[str, str | Exception] | None = None) -> None:
        self.calls: list[ToolCall] = []
        self.answers = answers or {}

    def __call__(self, call: ToolCall) -> ToolOutcome:
        self.calls.append(call)
        answer = self.answers.get(call.name, f"results for {dict(call.arguments)}")
        if isinstance(answer, Exception):
            raise answer
        return ToolOutcome(answer)


def run(endpoint: Endpoint, tools: Tools, **kwargs: Any) -> Any:
    return bedrock(endpoint).run_tools(
        ASK,
        model="claude-sonnet-5-5",
        tools=(WEB_SEARCH, WEB_FETCH),
        execute=tools,
        **kwargs,
    )


def test_a_tool_call_is_run_and_its_result_sent_back_until_the_model_answers() -> None:
    endpoint = Endpoint(
        streamed(tool_round(("toolu_1", "web_search", {"query": "XYZ results"}))),
        streamed(message_stream("XYZ declared a final dividend.", input_tokens=1_500)),
    )
    tools = Tools({"web_search": "1. XYZ board approves dividend"})
    result = run(endpoint, tools)

    assert result.rounds == 2
    assert result.response.text == "XYZ declared a final dividend."
    assert [c.name for c in result.tool_calls] == ["web_search"]
    assert tools.calls[0].arguments == {"query": "XYZ results"}

    first, second = (json.loads(r.content) for r in endpoint.requests)
    assert first["tool_choice"] == {"type": "auto"}
    assert [t["name"] for t in first["tools"]] == ["web_search", "web_fetch"]
    assert all(t["strict"] is True for t in first["tools"])
    assert first["tools"][0]["input_schema"] == structured_output_schema(WEB_SEARCH.input_schema)
    assert first["tools"][0]["input_schema"]["additionalProperties"] is False
    assert first["output_config"]["effort"] == "medium"
    assert first["thinking"] == {"type": "adaptive"}

    user, assistant, results = second["messages"]
    assert user == {"role": "user", "content": ASK[0].content}
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {
        "type": "thinking",
        "thinking": "search first",
        "signature": "sig-1",
    }
    assert assistant["content"][1]["type"] == "tool_use"
    assert assistant["content"][1]["id"] == "toolu_1"
    assert assistant["content"][1]["input"] == {"query": "XYZ results"}
    assert results == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "1. XYZ board approves dividend",
            }
        ],
    }


def test_usage_is_summed_over_every_round_and_priceable() -> None:
    endpoint = Endpoint(
        streamed(tool_round(("toolu_1", "web_search", {"query": "a"}), input_tokens=1_000)),
        streamed(
            tool_round(("toolu_2", "web_fetch", {"url": "https://x.example"}), input_tokens=2_000)
        ),
        streamed(message_stream("done", input_tokens=3_000, output_tokens=640, cache_read=300)),
    )
    with capture_logs() as logs:
        result = run(endpoint, Tools())
    usage = result.response.usage
    assert result.rounds == 3
    assert [u.input_tokens for u in result.round_usage] == [1_000, 2_000, 3_000]
    assert usage.input_tokens == 6_000
    assert usage.output_tokens == 200 + 200 + 640
    assert usage.cache_read_tokens == 100 + 100 + 300
    assert len([e for e in logs if e["event"] == "llm.bedrock.tool_round"]) == 3
    priced = TokenPricer(load_price_card()).price(
        result.response, on=date(2026, 10, 30), purpose="commons_research"
    )
    assert priced.provider == "bedrock" and priced.usage == usage


def test_several_calls_in_one_round_are_all_run_and_all_answered() -> None:
    endpoint = Endpoint(
        streamed(
            tool_round(
                ("toolu_1", "web_search", {"query": "a"}),
                ("toolu_2", "web_fetch", {"url": "https://x.example"}),
            )
        ),
        streamed(message_stream("done")),
    )
    tools = Tools()
    run(endpoint, tools)
    assert [c.id for c in tools.calls] == ["toolu_1", "toolu_2"]
    results = json.loads(endpoint.requests[1].content)["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_1", "toolu_2"]


def test_a_failing_tool_is_shown_to_the_model_as_an_error_result() -> None:
    endpoint = Endpoint(
        streamed(tool_round(("toolu_1", "web_search", {"query": "a"}))),
        streamed(message_stream("could not search")),
    )
    tools = Tools({"web_search": ToolExecutionError("Brave answered 503")})
    run(endpoint, tools)
    [result] = json.loads(endpoint.requests[1].content)["messages"][-1]["content"]
    assert result == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "Brave answered 503",
        "is_error": True,
    }


def test_a_tool_that_was_not_offered_is_never_run() -> None:
    endpoint = Endpoint(
        streamed(tool_round(("toolu_1", "shell", {"cmd": "ls"}))),
        streamed(message_stream("ok")),
    )
    tools = Tools()
    run(endpoint, tools)
    assert tools.calls == []
    [result] = json.loads(endpoint.requests[1].content)["messages"][-1]["content"]
    assert result["is_error"] is True and "shell" in result["content"]


def test_a_bug_in_the_callers_executor_propagates() -> None:
    endpoint = Endpoint(streamed(tool_round(("toolu_1", "web_search", {"query": "a"}))))
    with pytest.raises(KeyError):
        run(endpoint, Tools({"web_search": KeyError("bug")}))


def test_the_round_bound_holds_and_the_billed_usage_is_kept() -> None:
    endpoint = Endpoint(streamed(tool_round(("toolu_1", "web_search", {"query": "again"}))))
    with pytest.raises(ToolLoopExhaustedError) as raised:
        run(endpoint, Tools(), max_rounds=3)
    assert len(endpoint.requests) == 3
    assert raised.value.rounds == 3
    assert raised.value.usage.input_tokens == 3_000


def test_a_structured_final_answer_comes_back_as_its_tool_call() -> None:
    endpoint = Endpoint(
        streamed(tool_round(("toolu_1", "web_search", {"query": "a"}))),
        streamed(message_stream(json.dumps({"headline": "dividend"}))),
    )
    result = run(endpoint, Tools(), output=SUMMARY)
    assert json.loads(endpoint.requests[0].content)["output_config"]["format"]["type"] == (
        "json_schema"
    )
    [call] = result.response.tool_calls
    assert call.name == "summary" and call.arguments == {"headline": "dividend"}


def test_a_provider_failure_mid_loop_is_unavailable_and_runs_no_more_tools() -> None:
    endpoint = Endpoint(
        streamed(tool_round(("toolu_1", "web_search", {"query": "a"}))),
        api_error(503, "api_error"),
    )
    tools = Tools()
    with pytest.raises(LLMUnavailableError):
        run(endpoint, tools)
    assert len(tools.calls) == 1


def test_a_truncated_final_round_raises() -> None:
    endpoint = Endpoint(
        streamed(tool_round(("toolu_1", "web_search", {"query": "a"}))),
        streamed(message_stream("half", stop_reason="max_tokens")),
    )
    with pytest.raises(LLMError, match="output ceiling"):
        run(endpoint, Tools())


@pytest.mark.parametrize(
    ("tools", "rounds", "message"),
    [
        ((), 3, "at least one tool"),
        ((WEB_SEARCH, WEB_SEARCH), 3, "unique"),
        ((WEB_SEARCH,), 0, "max_rounds"),
    ],
)
def test_a_malformed_loop_is_refused_before_any_request(
    tools: tuple[ToolSpec, ...], rounds: int, message: str
) -> None:
    endpoint = Endpoint(streamed(b""))
    with pytest.raises(ValueError, match=message):
        bedrock(endpoint).run_tools(
            ASK, model="claude-sonnet-5-5", tools=tools, execute=Tools(), max_rounds=rounds
        )
    assert endpoint.requests == []
