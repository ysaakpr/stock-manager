"""`BedrockLLM`: Claude on Amazon Bedrock behind the same `LLM` protocol as the Claude CLI (X3).

Offline: the SDK's HTTP layer is an `httpx.MockTransport`, so every request the real
`AnthropicBedrockMantle` client builds is captured and answered from a scripted Server-Sent-Events
stream — the same bytes the Mantle endpoint streams — and nothing reaches the network.

What is pinned here:

1. **The request.** The roster's model name goes out as its mapped Bedrock id, with adaptive
   thinking, the configured effort, the offered tool's schema as `output_config.format`, a
   thinking-sized `max_tokens`, streaming, the key as a bearer token and the region's endpoint.
2. **The answer.** A structured answer comes back as one `ToolCall` with the parsed object, as the
   CLI's does; the usage buckets are the provider's.
3. **The failure classes.** Which failures are `LLMUnavailableError` (the provider's, a fallback
   trigger) and which are not (the request's) — the fallback tests build on this split.
4. **Secret hygiene.** The key never reaches the CLI's environment, a repr, a log line or an
   error message, and the status app container is not handed it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import SecretStr
from structlog.testing import capture_logs

from analyst.llm import (
    BEDROCK_PROVIDER,
    BedrockLLM,
    LLMCredentialError,
    LLMError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMUnavailableError,
    Message,
    Role,
    StopReason,
    ToolSpec,
    UnavailableReason,
    claude_cli_env,
)
from analyst.llm.bedrock import (
    BedrockAdapter,
    BedrockModelMap,
    load_bedrock_models,
    structured_output_schema,
)
from analyst.llm.effort import EffortPolicy, load_effort_policy
from dataplatform.config import REPO_ROOT, LlmEffort, LlmProvider, Settings

#: A key-shaped value with no prefix any generic masking pattern knows — the case the module masks
#: by value. Not a credential: tests never reach the network.
FAKE_KEY = "ABSKZmFrZS1iZWRyb2NrLWtleS1mb3ItdGVzdHMtb25seQ"

ASK = [Message(Role.USER, "Is the thesis intact?")]

VERDICT = ToolSpec(
    name="verdict",
    description="Return the verdict.",
    input_schema={
        "type": "object",
        "properties": {
            "intact": {"type": "boolean"},
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "note": {"type": ["string", "null"], "maxLength": 200},
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "refs": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        },
        "required": ["intact", "reason", "note", "score", "refs"],
        "additionalProperties": False,
    },
)


# ── a scripted Mantle endpoint ───────────────────────────────────────────────────────────────────


def sse(*events: Mapping[str, Any]) -> bytes:
    """Events in the wire format the Messages API streams."""
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )


def message_stream(
    text: str,
    *,
    model: str = "anthropic.claude-opus-5-5",
    stop_reason: str = "end_turn",
    input_tokens: int = 1_200,
    output_tokens: int = 640,
    cache_write: int = 0,
    cache_read: int = 300,
) -> bytes:
    """A complete streamed message: a thinking block, then the text, then the stop and usage."""
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
    }
    return sse(
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": model,
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
            "delta": {"type": "thinking_delta", "thinking": "weighing the filing"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig"},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"type": "message_delta", "stop_reason": stop_reason, "stop_sequence": None},
            "usage": usage,
        },
        {"type": "message_stop"},
    )


ANSWER = {"intact": True, "reason": "guidance held", "note": None, "score": 0.7, "refs": ["e1"]}


class Endpoint:
    """The Mantle endpoint as a `MockTransport` handler: records requests, plays a script."""

    def __init__(self, *responses: httpx.Response | Callable[[httpx.Request], httpx.Response]):
        self.script = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return step(request) if callable(step) else step

    @property
    def body(self) -> dict[str, Any]:
        """The JSON body of the last request."""
        loaded: dict[str, Any] = json.loads(self.requests[-1].content)
        return loaded


def streamed(body: bytes) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def api_error(status: int, kind: str, message: str = "nope") -> httpx.Response:
    return httpx.Response(
        status,
        # Retry immediately: a test proves the SDK retried, it does not wait for it.
        headers={"retry-after-ms": "0"},
        json={"type": "error", "error": {"type": kind, "message": message}},
    )


def bedrock(endpoint: Endpoint, *, max_retries: int = 0, **kwargs: Any) -> BedrockLLM:
    return BedrockLLM(
        SecretStr(FAKE_KEY),
        "us-east-1",
        max_retries=max_retries,
        http_client=httpx.Client(transport=httpx.MockTransport(endpoint)),
        **kwargs,
    )


# ── 1. the request ───────────────────────────────────────────────────────────────────────────────


def test_the_request_carries_the_bedrock_id_the_schema_the_effort_and_adaptive_thinking() -> None:
    endpoint = Endpoint(streamed(message_stream(json.dumps(ANSWER))))
    bedrock(endpoint).complete(
        ASK, model="claude-opus-5-5", tools=(VERDICT,), system="You are a fund manager."
    )

    request = endpoint.requests[0]
    body = endpoint.body
    assert request.url == "https://bedrock-mantle.us-east-1.api.aws/anthropic/v1/messages"
    assert body["model"] == "anthropic.claude-opus-5-5"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "high"  # a decision call (effort.yaml)
    assert body["output_config"]["format"] == {
        "type": "json_schema",
        "schema": structured_output_schema(VERDICT.input_schema),
    }
    assert body["stream"] is True
    assert body["system"] == "You are a fund manager."
    assert body["messages"] == [{"role": "user", "content": "Is the thesis intact?"}]
    assert "tools" not in body and "tool_choice" not in body
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"


def test_a_summary_call_is_sent_at_medium() -> None:
    """Sonnet serves the digests: the `summary` call type, `medium` (owner, 2026-10-30)."""
    endpoint = Endpoint(streamed(message_stream(json.dumps(ANSWER))))
    bedrock(endpoint).complete(ASK, model="claude-sonnet-5-5", tools=(VERDICT,))
    assert endpoint.body["output_config"]["effort"] == "medium"
    assert endpoint.body["model"] == "anthropic.claude-sonnet-5-5"


def test_the_effort_is_the_policys_not_a_constant() -> None:
    policy = EffortPolicy(
        version=1,
        call_types={"decision": LlmEffort.LOW, "summary": LlmEffort.MAX},
        models={"claude-opus-5-5": "decision", "claude-sonnet-5-5": "summary"},
    )
    endpoint = Endpoint(streamed(message_stream(json.dumps(ANSWER))))
    bedrock(endpoint, efforts=policy).complete(ASK, model="claude-opus-5-5", tools=(VERDICT,))
    assert endpoint.body["output_config"]["effort"] == "low"


def test_the_checked_in_policy_pins_decisions_high_and_summaries_medium() -> None:
    policy = load_effort_policy()
    assert policy.effort_for("claude-opus-5-5") is LlmEffort.HIGH
    assert policy.effort_for("claude-opus-5") is LlmEffort.HIGH
    assert policy.effort_for("claude-sonnet-5-5") is LlmEffort.MEDIUM
    with pytest.raises(LLMError, match="no call type"):
        policy.effort_for("claude-unknown-9")


def test_a_model_without_adaptive_thinking_or_effort_is_sent_neither() -> None:
    """Haiku 4.5 accepts neither; sending them would be a 400, not a fallback."""
    endpoint = Endpoint(
        streamed(message_stream(json.dumps(ANSWER), model="anthropic.claude-haiku-4-5"))
    )
    bedrock(endpoint).complete(ASK, model="claude-haiku-4-5", tools=(VERDICT,))
    body = endpoint.body
    assert body["model"] == "anthropic.claude-haiku-4-5"
    assert "thinking" not in body
    assert "effort" not in body["output_config"] and "format" in body["output_config"]


def test_a_callers_small_ceiling_is_raised_so_thinking_cannot_truncate_the_answer() -> None:
    """The digest asks for 2,000 tokens; at xhigh the thinking alone outruns that."""
    endpoint = Endpoint(streamed(message_stream(json.dumps(ANSWER))))
    bedrock(endpoint, max_tokens=64_000).complete(
        ASK, model="claude-sonnet-5-5", tools=(VERDICT,), max_tokens=2_000
    )
    assert endpoint.body["max_tokens"] == 64_000


def test_the_ceiling_never_exceeds_the_models_own() -> None:
    endpoint = Endpoint(streamed(message_stream(json.dumps(ANSWER))))
    bedrock(endpoint, max_tokens=64_000).complete(
        ASK, model="claude-opus-5-5", tools=(VERDICT,), max_tokens=500_000
    )
    assert endpoint.body["max_tokens"] == 128_000


def test_an_unmapped_model_is_refused_before_any_request() -> None:
    endpoint = Endpoint(streamed(message_stream("{}")))
    with pytest.raises(LLMError, match="not mapped to Bedrock") as raised:
        bedrock(endpoint).complete(ASK, model="claude-sonnet-5")
    assert not isinstance(raised.value, LLMUnavailableError)  # a config bug, never a fallback
    assert endpoint.requests == []


def test_the_key_and_region_come_from_settings_never_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`.env` is read by pydantic, not exported: the SDK's own env lookup must play no part."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "ambient-key-must-not-be-used")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "https://elsewhere.example")
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        aws_bearer_token_bedrock=SecretStr(FAKE_KEY),
        bedrock_region="us-east-1",
    )
    endpoint = Endpoint(streamed(message_stream(json.dumps(ANSWER))))
    llm = BedrockLLM.from_settings(
        settings, http_client=httpx.Client(transport=httpx.MockTransport(endpoint))
    )
    llm.complete(ASK, model="claude-opus-5-5", tools=(VERDICT,))
    assert endpoint.requests[0].url.host == "bedrock-mantle.us-east-1.api.aws"
    assert endpoint.requests[0].headers["authorization"] == f"Bearer {FAKE_KEY}"


@pytest.mark.parametrize(
    ("key", "region", "named"),
    [
        (None, "us-east-1", "AWS_BEARER_TOKEN_BEDROCK"),
        (SecretStr(FAKE_KEY), None, "BEDROCK_REGION"),
        (SecretStr("  "), "us-east-1", "AWS_BEARER_TOKEN_BEDROCK"),
    ],
)
def test_a_missing_key_or_region_refuses_construction_naming_what_to_set(
    key: SecretStr | None, region: str | None, named: str
) -> None:
    with pytest.raises(LLMCredentialError, match=named):
        BedrockLLM(key, region)


def test_settings_refuse_a_region_that_is_not_a_region_name() -> None:
    with pytest.raises(ValueError, match="AWS region"):
        Settings(_env_file=None, bedrock_region="us-east-1.evil.example/")  # type: ignore[call-arg]


# ── 2. the answer ────────────────────────────────────────────────────────────────────────────────


def test_a_structured_answer_comes_back_as_one_tool_call_with_its_usage() -> None:
    endpoint = Endpoint(
        streamed(
            message_stream(
                json.dumps(ANSWER),
                input_tokens=1_500,
                output_tokens=900,
                cache_write=40,
                cache_read=300,
            )
        )
    )
    response = bedrock(endpoint).complete(ASK, model="claude-opus-5-5", tools=(VERDICT,))

    assert response.provider == BEDROCK_PROVIDER
    assert response.model == "anthropic.claude-opus-5-5"
    assert response.stop_reason is StopReason.TOOL_USE
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.name == "verdict"
    assert dict(call.arguments) == ANSWER
    assert response.usage.input_tokens == 1_500
    assert response.usage.output_tokens == 900
    assert response.usage.cache_write_tokens == 40
    assert response.usage.cache_read_tokens == 300
    assert response.fallback is None


def test_a_plain_completion_returns_the_text_without_its_thinking() -> None:
    endpoint = Endpoint(streamed(message_stream("Intact: guidance held.")))
    response = bedrock(endpoint).complete(ASK, model="claude-sonnet-5-5")
    assert response.text == "Intact: guidance held."
    assert response.stop_reason is StopReason.END_TURN
    assert "output_config" in endpoint.body and "format" not in endpoint.body["output_config"]


def test_each_call_is_logged_with_its_usage_and_latency() -> None:
    endpoint = Endpoint(streamed(message_stream(json.dumps(ANSWER))))
    with capture_logs() as logs:
        bedrock(endpoint).complete(ASK, model="claude-opus-5-5", tools=(VERDICT,))
    [event] = [e for e in logs if e["event"] == "llm.bedrock.call"]
    assert event["provider"] == "bedrock"
    assert event["served_model"] == "anthropic.claude-opus-5-5"
    assert event["input_tokens"] == 1_200
    assert event["effort"] == "high"
    assert isinstance(event["latency_ms"], int)


def test_two_tools_raise_rather_than_one_being_dropped() -> None:
    with pytest.raises(ValueError, match="one structured-output schema"):
        bedrock(Endpoint(streamed(b""))).complete(
            ASK, model="claude-opus-5-5", tools=(VERDICT, VERDICT)
        )


# ── 3. failures: the provider's, or the request's ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (api_error(401, "authentication_error"), UnavailableReason.AUTH),
        (
            api_error(403, "permission_error", "no bedrock-mantle:CreateInference"),
            UnavailableReason.AUTH,
        ),
        (api_error(429, "rate_limit_error"), UnavailableReason.RATE_LIMIT),
        (api_error(500, "api_error"), UnavailableReason.SERVER_ERROR),
        (api_error(503, "api_error"), UnavailableReason.SERVER_ERROR),
        (api_error(529, "overloaded_error"), UnavailableReason.SERVER_ERROR),
    ],
)
def test_a_provider_failure_is_unavailable_with_its_reason(
    response: httpx.Response, reason: UnavailableReason
) -> None:
    with pytest.raises(LLMUnavailableError) as raised:
        bedrock(Endpoint(response)).complete(ASK, model="claude-opus-5-5", tools=(VERDICT,))
    assert raised.value.reason is reason
    assert raised.value.provider == "bedrock"


def test_a_rate_limit_is_raised_only_after_the_sdks_own_retries() -> None:
    endpoint = Endpoint(api_error(429, "rate_limit_error"))
    with pytest.raises(LLMUnavailableError) as raised:
        bedrock(endpoint, max_retries=2).complete(ASK, model="claude-opus-5-5", tools=(VERDICT,))
    assert len(endpoint.requests) == 3
    # Still a rate limit to a caller with no backup: DeadlineLLM backs off on it.
    assert isinstance(raised.value, LLMRateLimitError)


def test_a_rejected_key_is_still_a_credential_error_to_a_caller_without_a_backup() -> None:
    with pytest.raises(LLMCredentialError):
        bedrock(Endpoint(api_error(401, "authentication_error"))).complete(
            ASK, model="claude-opus-5-5"
        )


def _raise(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ConnectError("connection refused"), UnavailableReason.CONNECTION),
        (httpx.ReadTimeout("read timed out"), UnavailableReason.TIMEOUT),
    ],
)
def test_an_unreachable_or_silent_endpoint_is_unavailable(
    error: Exception, reason: UnavailableReason
) -> None:
    with pytest.raises(LLMUnavailableError) as raised:
        bedrock(Endpoint(_raise(error))).complete(ASK, model="claude-opus-5-5")
    assert raised.value.reason is reason


def test_an_error_event_mid_stream_is_the_providers_fault() -> None:
    body = message_stream(json.dumps(ANSWER)).split(b"event: content_block_start")[0] + sse(
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    )
    with pytest.raises(LLMUnavailableError) as raised:
        bedrock(Endpoint(streamed(body))).complete(ASK, model="claude-opus-5-5")
    assert raised.value.reason is UnavailableReason.SERVER_ERROR


class _BrokenStream(httpx.SyncByteStream):
    """A stream whose connection drops after the first event."""

    def __init__(self, head: bytes) -> None:
        self._head = head

    def __iter__(self) -> Iterator[bytes]:
        yield self._head
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message")


def test_a_connection_that_drops_mid_stream_is_unavailable() -> None:
    head = message_stream(json.dumps(ANSWER)).split(b"event: content_block_start")[0]
    response = httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=_BrokenStream(head)
    )
    with pytest.raises(LLMUnavailableError) as raised:
        bedrock(Endpoint(response)).complete(ASK, model="claude-opus-5-5")
    assert raised.value.reason is UnavailableReason.CONNECTION


@pytest.mark.parametrize(
    "response",
    [
        api_error(400, "invalid_request_error", "output_config.format: schema too complex"),
        api_error(404, "not_found_error", "model not found"),
        api_error(413, "request_too_large"),
        api_error(422, "invalid_request_error"),
    ],
)
def test_a_request_the_provider_rejects_is_not_unavailable(response: httpx.Response) -> None:
    with pytest.raises(LLMError) as raised:
        bedrock(Endpoint(response)).complete(ASK, model="claude-opus-5-5", tools=(VERDICT,))
    assert not isinstance(raised.value, LLMUnavailableError)
    assert "defect in the request" in str(raised.value)


def test_a_refusal_raises_and_is_not_unavailable() -> None:
    endpoint = Endpoint(streamed(message_stream("", stop_reason="refusal")))
    with pytest.raises(LLMRefusalError) as raised:
        bedrock(endpoint).complete(ASK, model="claude-opus-5-5", tools=(VERDICT,))
    assert not isinstance(raised.value, LLMUnavailableError)


def test_a_truncated_answer_raises_with_the_reason_and_logs_its_billed_usage() -> None:
    endpoint = Endpoint(streamed(message_stream('{"intact": tr', stop_reason="max_tokens")))
    with capture_logs() as logs, pytest.raises(LLMError, match="output ceiling") as raised:
        bedrock(endpoint).complete(ASK, model="claude-opus-5-5", tools=(VERDICT,))
    assert not isinstance(raised.value, LLMUnavailableError)
    [event] = [e for e in logs if e["event"] == "llm.bedrock.truncated"]
    assert event["output_tokens"] == 640


@pytest.mark.parametrize("text", ["not json at all", "[1, 2]", ""])
def test_an_answer_that_is_not_the_object_asked_for_raises_and_is_not_unavailable(
    text: str,
) -> None:
    endpoint = Endpoint(streamed(message_stream(text)))
    with pytest.raises(LLMError) as raised:
        bedrock(endpoint).complete(ASK, model="claude-opus-5-5", tools=(VERDICT,))
    assert not isinstance(raised.value, LLMUnavailableError)


# ── the schema structured outputs compile ────────────────────────────────────────────────────────


def test_uncompiled_bounds_move_into_descriptions_and_nothing_else_changes() -> None:
    schema = structured_output_schema(VERDICT.input_schema)
    props = schema["properties"]
    assert props["reason"] == {"type": "string", "description": "{maxLength: 500, minLength: 1}"}
    assert props["note"] == {"type": ["string", "null"], "description": "{maxLength: 200}"}
    assert props["score"] == {"type": "number", "description": "{maximum: 1, minimum: 0}"}
    assert props["refs"] == {
        "type": "array",
        "items": {"type": "string"},
        "description": "{maxItems: 8}",
    }
    assert schema["required"] == VERDICT.input_schema["required"]
    assert schema["additionalProperties"] is False
    assert "maxLength" in VERDICT.input_schema["properties"]["reason"]  # the input is untouched


def test_the_m17_schemas_keep_every_field_type_and_requirement() -> None:
    """The decision, research and digest schemas transform without losing a field."""
    from analyst.commons.digests import DIGEST_TOOL
    from analyst.fundmanager.mandate import RoundLimits
    from analyst.fundmanager.schemas import decision_tool, research_tool

    def shape(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: shape(v)
                for k, v in node.items()
                if k in ("type", "properties", "items", "required", "enum", "anyOf", "pattern")
            }
        if isinstance(node, list):
            return [shape(v) for v in node]
        return node

    for tool in (
        decision_tool(),
        research_tool(RoundLimits(max_isins=8, max_queries=6, max_research_rounds=2)),
        DIGEST_TOOL,
    ):
        original = dict(tool.input_schema)
        transformed = structured_output_schema(original)
        assert shape(transformed) == shape(original)
        text = json.dumps(transformed)
        for keyword in ("minLength", "maxLength", "minimum", "maximum", "maxItems"):
            assert f'"{keyword}"' not in text


def test_the_model_map_routes_every_roster_model_and_its_cli_backup() -> None:
    """Amendment 5: decisions and research on Opus 5.5 (Messages), digests on DeepSeek V3.2
    (Chat Completions) whose Claude CLI backup is Sonnet 5.5 — and every role is mapped."""
    from analyst.fundmanager.mandate import load_roster

    models = load_bedrock_models()
    roster = load_roster()
    for book in roster.primaries:
        for name in (book.models.decision, book.models.research):
            route = models.route(name)
            assert route.adapter is BedrockAdapter.ANTHROPIC_MESSAGES
            assert route.bedrock_id == f"anthropic.{name}"
            assert models.backup_model(name) == name
        digest = models.route(book.models.digest)
        assert digest.adapter is BedrockAdapter.OPENAI_CHAT
        assert digest.bedrock_id == "deepseek.v3.2"
        assert models.backup_model(book.models.digest) == "claude-sonnet-5-5"


def test_an_unknown_adapter_in_the_map_is_a_load_error() -> None:
    """The seam is the enum: a Converse model is refused until its adapter exists."""
    with pytest.raises(ValueError, match="adapter"):
        BedrockModelMap.model_validate(
            {
                "version": 1,
                "models": {
                    "deepseek-v3-2": {
                        "adapter": "converse",
                        "bedrock_id": "deepseek.v3.2",
                        "max_output_tokens": 8192,
                    }
                },
            }
        )


def test_every_bedrock_id_is_priced_on_the_card_in_force() -> None:
    from datetime import date

    from accounting import load_price_card

    schedule = load_price_card().schedule_for(date(2026, 10, 30))
    for route in load_bedrock_models().models.values():
        price = schedule.price_for(route.bedrock_id)
        if route.adapter is BedrockAdapter.ANTHROPIC_MESSAGES:
            # Bedrock's in-region rate for Claude is its own figure, not the first-party one.
            first_party = schedule.price_for(route.bedrock_id.removeprefix("anthropic."))
            assert price.input > first_party.input


# ── 4. secret hygiene ────────────────────────────────────────────────────────────────────────────


def test_the_claude_cli_never_receives_the_bedrock_key() -> None:
    env = claude_cli_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/x",
            "AWS_BEARER_TOKEN_BEDROCK": FAKE_KEY,
            "ANTHROPIC_AWS_API_KEY": FAKE_KEY,
            "BEDROCK_REGION": "us-east-1",
        }
    )
    assert env == {"PATH": "/usr/bin", "HOME": "/home/x"}
    assert FAKE_KEY not in json.dumps(env)


def test_settings_never_print_the_key() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        aws_bearer_token_bedrock=SecretStr(FAKE_KEY),
        bedrock_region="us-east-1",
    )
    assert settings.aws_bearer_token_bedrock is not None
    for rendered in (repr(settings), str(settings), json.dumps(settings.model_dump(mode="json"))):
        assert FAKE_KEY not in rendered
    with capture_logs() as logs:
        from dataplatform.logging import get_logger

        get_logger("test").info(
            "settings.dump", settings=settings, key=settings.aws_bearer_token_bedrock
        )
    assert FAKE_KEY not in repr(logs)


def test_the_key_never_reaches_a_repr_a_log_line_or_an_error_message() -> None:
    echo = api_error(
        403, "permission_error", f"token {FAKE_KEY} lacks bedrock-mantle:CreateInference"
    )
    llm = bedrock(Endpoint(echo))
    with capture_logs() as logs, pytest.raises(LLMUnavailableError) as raised:
        llm.complete(ASK, model="claude-opus-5-5")
    assert FAKE_KEY not in repr(llm)
    assert FAKE_KEY not in str(raised.value)
    assert "bedrock-mantle:CreateInference" in str(raised.value)
    assert FAKE_KEY not in repr(logs)


def test_the_status_app_container_is_not_handed_the_bedrock_key() -> None:
    """The app takes the repo-root .env whole; `environment` (which wins) blanks the key."""
    compose = yaml.safe_load(Path(REPO_ROOT, "ops", "docker-compose.yml").read_text())
    app = compose["services"]["app"]
    assert any(str(e.get("path", e)).endswith(".env") for e in app["env_file"])
    assert app["environment"]["AWS_BEARER_TOKEN_BEDROCK"] == ""
    # Nor the web research vendors' keys: /status/websearch needs none of them.
    for key in ("BRAVE_SEARCH_API_KEY", "TAVILY_API_KEY", "FIRECRAWL_API_KEY"):
        assert app["environment"][key] == ""


def test_the_startup_wiring_line_says_set_or_not_set_never_the_key() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider=LlmProvider.BEDROCK,
        m17_llm_provider=LlmProvider.BEDROCK,
        aws_bearer_token_bedrock=SecretStr(FAKE_KEY),
    )
    wiring = settings.llm_wiring()
    assert wiring["llm_provider"] == "bedrock" and wiring["llm_fallback"] == "claude_cli"
    assert wiring["bedrock_key_set"] is True and wiring["bedrock_region_set"] is False
    assert FAKE_KEY not in json.dumps(wiring)


def test_the_response_names_the_bedrock_id_even_when_mantle_reports_the_bare_name() -> None:
    """Measured live 2026-10-30: Mantle reports `claude-opus-5-5`. The record must still say
    `anthropic.claude-opus-5-5`, so the call prices at the Bedrock rate, not first-party."""
    endpoint = Endpoint(streamed(message_stream(json.dumps(ANSWER), model="claude-opus-5-5")))
    with capture_logs() as logs:
        response = bedrock(endpoint).complete(ASK, model="claude-opus-5-5", tools=(VERDICT,))
    assert response.model == "anthropic.claude-opus-5-5"
    assert not [e for e in logs if e["event"] == "llm.bedrock.served_other_model"]


def test_a_different_served_model_is_logged() -> None:
    endpoint = Endpoint(streamed(message_stream(json.dumps(ANSWER), model="claude-opus-9")))
    with capture_logs() as logs:
        response = bedrock(endpoint).complete(ASK, model="claude-opus-5-5", tools=(VERDICT,))
    assert response.model == "anthropic.claude-opus-5-5"
    [event] = [e for e in logs if e["event"] == "llm.bedrock.served_other_model"]
    assert event["served_model"] == "claude-opus-9"
