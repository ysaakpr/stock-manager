"""`BedrockLLM`'s `openai_chat` route: DeepSeek V3.2 on Mantle's Chat Completions (Amendment 5).

Offline: the HTTP layer is an `httpx.MockTransport` that replays Chat Completions JSON — the shape
Mantle answered `deepseek.v3.2` with on 2026-10-30 — and records every request.

What is pinned here:

1. **The request.** The roster name goes out as its Bedrock id to ``/v1/chat/completions``, the
   system prompt as the first message, a structured-output demand as a strict ``json_schema``
   response format (never a forced tool), the ceiling clamped to the model's 8K, and no effort.
2. **The answer.** Text, or one structured `ToolCall`; the usage buckets; the Bedrock id recorded.
3. **The failure classes** — the same split as the Claude path, so `FallbackLLM`'s rule is exact.
4. **The fallback for a model the CLI cannot run.** DeepSeek unavailable → Sonnet 5.5 on the CLI,
   and the digest records the CLI and Sonnet as its author; DeepSeek refusing the request → no
   fallback. Written so either inversion fails.
5. **Config and the card.** The roster names each role's model; the price card prices DeepSeek.
6. **Secret hygiene.** The key reaches the Authorization header and nothing else.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from accounting import TokenPricer, load_price_card
from analyst.commons.digests import (
    DIGEST_TOOL,
    FilingInput,
    FilingKind,
    digest_filing,
)
from analyst.fundmanager import load_roster
from analyst.llm import (
    BEDROCK_PROVIDER,
    BedrockLLM,
    FallbackLLM,
    LLMError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMUnavailableError,
    Message,
    Role,
    StopReason,
    UnavailableReason,
    build_llm,
)
from analyst.llm.bedrock import BedrockModelMap, load_bedrock_models
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import LlmProvider, Settings
from tests.unit.test_llm_bedrock import ASK, FAKE_KEY, VERDICT
from tests.unit.test_llm_fallback import Backup

DEEPSEEK = "deepseek-v3-2"
URL = "https://bedrock-mantle.us-east-1.api.aws/v1/chat/completions"

#: A filing digest as DeepSeek answered one (strict json_schema), for the digest round trips.
DIGEST_ANSWER = {
    "headline": "ABC Ltd: Q2 FY27 revenue Rs 1,234.5 crore",
    "disclosed": "The company reported revenue for the quarter.",
    "period": "Q2 FY27",
    "figures": [{"label": "Revenue", "value": "1,234.5", "unit": "Rs crore", "period": "Q2 FY27"}],
}


def completion(
    content: str,
    *,
    finish: str = "stop",
    prompt_tokens: int = 1_500,
    completion_tokens: int = 220,
    cached: int | None = None,
    model: str = "deepseek.v3.2",
) -> httpx.Response:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish,
                }
            ],
            "usage": usage,
        },
    )


def chat_error(status: int, message: str = "nope") -> httpx.Response:
    return httpx.Response(
        status,
        headers={"retry-after-ms": "0"},
        json={"error": {"message": message, "type": "invalid_request_error", "code": status}},
    )


class Endpoint:
    """Mantle as a `MockTransport` handler: records requests, plays a script (last step repeats)."""

    def __init__(self, *steps: httpx.Response | Callable[[httpx.Request], httpx.Response]):
        self.script = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return step(request) if callable(step) else step

    @property
    def body(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[-1].content)
        return loaded


def bedrock(endpoint: Endpoint, *, max_retries: int = 0, **kwargs: Any) -> BedrockLLM:
    return BedrockLLM(
        SecretStr(FAKE_KEY),
        "us-east-1",
        max_retries=max_retries,
        http_client=httpx.Client(transport=httpx.MockTransport(endpoint)),
        **kwargs,
    )


def _raise(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


# ── 1. the request ───────────────────────────────────────────────────────────────────────────────


def test_a_text_call_goes_to_chat_completions_with_the_bedrock_id_and_no_effort() -> None:
    endpoint = Endpoint(completion("Revenue rose."))
    response = bedrock(endpoint).complete(ASK, model=DEEPSEEK, system="Facts only.")

    request = endpoint.requests[-1]
    assert str(request.url) == URL
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = endpoint.body
    assert body["model"] == "deepseek.v3.2"
    assert body["messages"] == [
        {"role": "system", "content": "Facts only."},
        {"role": "user", "content": "Is the thesis intact?"},
    ]
    # The ceiling: a caller's 16K is clamped to the model card's 8K output limit.
    assert body["max_tokens"] == 8192
    # No reasoning control is documented for V3.2, so none is sent — nor anything Claude-only.
    for absent in ("reasoning_effort", "thinking", "output_config", "tools", "tool_choice"):
        assert absent not in body
    assert "response_format" not in body
    assert response.text == "Revenue rose."
    assert response.stop_reason is StopReason.END_TURN
    assert response.tool_calls == ()


def test_a_structured_call_is_a_strict_json_schema_response_format_not_a_forced_tool() -> None:
    endpoint = Endpoint(
        completion(
            json.dumps({"intact": True, "reason": "ok", "note": None, "score": 0.5, "refs": []})
        )
    )
    response = bedrock(endpoint).complete(ASK, model=DEEPSEEK, tools=(VERDICT,))

    fmt = endpoint.body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "verdict"
    assert fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    # The same strict-compilable schema the Claude path sends: bounds moved into descriptions.
    assert "maxLength" not in schema["properties"]["reason"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["note"]["type"] == ["string", "null"]
    assert "tools" not in endpoint.body and "tool_choice" not in endpoint.body
    (call,) = response.tool_calls
    assert call.name == "verdict" and call.id == "structured_output"
    assert call.arguments == {
        "intact": True,
        "reason": "ok",
        "note": None,
        "score": 0.5,
        "refs": [],
    }
    assert response.stop_reason is StopReason.TOOL_USE


def test_an_effort_is_sent_only_when_the_route_says_the_model_takes_one() -> None:
    models = BedrockModelMap.model_validate(
        {
            "version": 1,
            "models": {
                "claude-sonnet-5-5": {
                    "adapter": "anthropic_messages",
                    "bedrock_id": "anthropic.claude-sonnet-5-5",
                    "max_output_tokens": 128000,
                },
                "chatty": {
                    "adapter": "openai_chat",
                    "bedrock_id": "vendor.chatty",
                    "max_output_tokens": 4096,
                    "adaptive_thinking": False,
                    "cli_fallback_model": "claude-sonnet-5-5",
                },
            },
        }
    )
    from analyst.llm.effort import EffortPolicy

    policy = EffortPolicy.model_validate(
        {
            "version": 1,
            "call_types": {"decision": "high", "summary": "low"},
            "models": {"chatty": "summary"},
        }
    )
    endpoint = Endpoint(completion("ok", model="vendor.chatty"))
    bedrock(endpoint, models=models, efforts=policy).complete(ASK, model="chatty")
    assert endpoint.body["reasoning_effort"] == "low"
    assert endpoint.body["max_tokens"] == 4096


# ── 2. the answer ────────────────────────────────────────────────────────────────────────────────


def test_the_answer_records_bedrock_the_bedrock_id_and_the_providers_usage() -> None:
    # Mantle may report another spelling; the record carries the id the card prices.
    endpoint = Endpoint(
        completion(
            "ok", prompt_tokens=1_500, completion_tokens=220, cached=300, model="deepseek-v3.2"
        )
    )
    response = bedrock(endpoint).complete(ASK, model=DEEPSEEK)
    assert response.provider == BEDROCK_PROVIDER
    assert response.model == "deepseek.v3.2"
    assert response.usage.input_tokens == 1_200  # prompt less the cached part
    assert response.usage.cache_read_tokens == 300
    assert response.usage.cache_write_tokens == 0
    assert response.usage.output_tokens == 220
    assert response.fallback is None


def test_usage_without_cache_details_is_all_input() -> None:
    response = bedrock(Endpoint(completion("ok"))).complete(ASK, model=DEEPSEEK)
    assert (response.usage.input_tokens, response.usage.cache_read_tokens) == (1_500, 0)


def test_an_answer_without_usage_is_refused_rather_than_unmetered() -> None:
    body = completion("ok").json()
    del body["usage"]
    with pytest.raises(LLMError, match="without usage") as caught:
        bedrock(Endpoint(httpx.Response(200, json=body))).complete(ASK, model=DEEPSEEK)
    assert not isinstance(caught.value, LLMUnavailableError)


def test_the_call_is_logged_with_the_adapter_and_tokens() -> None:
    with capture_logs() as logs:
        bedrock(Endpoint(completion("ok"))).complete(ASK, model=DEEPSEEK)
    (event,) = [e for e in logs if e["event"] == "llm.bedrock.call"]
    assert event["adapter"] == "openai_chat" and event["model"] == DEEPSEEK
    assert event["input_tokens"] == 1_500 and event["effort"] is None


# ── 3. the failure classes ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, UnavailableReason.AUTH),
        (403, UnavailableReason.AUTH),
        (408, UnavailableReason.TIMEOUT),
        (429, UnavailableReason.RATE_LIMIT),
        (500, UnavailableReason.SERVER_ERROR),
        (503, UnavailableReason.SERVER_ERROR),
    ],
)
def test_a_provider_failure_is_unavailable(status: int, reason: UnavailableReason) -> None:
    with pytest.raises(LLMUnavailableError) as caught:
        bedrock(Endpoint(chat_error(status))).complete(ASK, model=DEEPSEEK)
    assert caught.value.reason is reason and caught.value.provider == BEDROCK_PROVIDER
    if status == 429:
        assert isinstance(caught.value, LLMRateLimitError)  # DeadlineLLM still backs off


@pytest.mark.parametrize("status", [400, 404, 413, 422])
def test_a_request_failure_is_not_unavailable(status: int) -> None:
    with pytest.raises(LLMError, match=f"HTTP {status}") as caught:
        bedrock(Endpoint(chat_error(status, "does not support"))).complete(ASK, model=DEEPSEEK)
    assert not isinstance(caught.value, LLMUnavailableError)


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (httpx.ConnectError("refused"), UnavailableReason.CONNECTION),
        (httpx.ReadTimeout("slow"), UnavailableReason.TIMEOUT),
    ],
)
def test_a_transport_failure_is_unavailable(exc: Exception, reason: UnavailableReason) -> None:
    with pytest.raises(LLMUnavailableError) as caught:
        bedrock(Endpoint(_raise(exc))).complete(ASK, model=DEEPSEEK)
    assert caught.value.reason is reason


def test_throttling_and_5xx_are_retried_before_they_are_reported() -> None:
    endpoint = Endpoint(chat_error(429), chat_error(503), completion("ok"))
    response = bedrock(endpoint, max_retries=2).complete(ASK, model=DEEPSEEK)
    assert response.text == "ok" and len(endpoint.requests) == 3
    endpoint = Endpoint(chat_error(400))
    with pytest.raises(LLMError):
        bedrock(endpoint, max_retries=2).complete(ASK, model=DEEPSEEK)
    assert len(endpoint.requests) == 1  # a bad request is never retried


def test_a_truncation_a_content_filter_and_unparsable_json_are_not_fallbacks() -> None:
    llm = bedrock(Endpoint(completion('{"half": ', finish="length")))
    with pytest.raises(LLMError, match="output ceiling") as truncated:
        llm.complete(ASK, model=DEEPSEEK, tools=(VERDICT,))
    llm = bedrock(Endpoint(completion("", finish="content_filter")))
    with pytest.raises(LLMRefusalError):
        llm.complete(ASK, model=DEEPSEEK)
    llm = bedrock(Endpoint(completion("not json")))
    with pytest.raises(LLMError, match="not JSON") as unparsable:
        llm.complete(ASK, model=DEEPSEEK, tools=(VERDICT,))
    llm = bedrock(Endpoint(completion("[1, 2]")))
    with pytest.raises(LLMError, match="JSON list"):
        llm.complete(ASK, model=DEEPSEEK, tools=(VERDICT,))
    for caught in (truncated, unparsable):
        assert not isinstance(caught.value, LLMUnavailableError)


def test_the_tool_loop_is_refused_on_a_chat_completions_model() -> None:
    from analyst.llm import ToolOutcome

    endpoint = Endpoint(completion("ok"))
    with pytest.raises(LLMError, match="no client-side tool loop"):
        bedrock(endpoint).run_tools(
            ASK, model=DEEPSEEK, tools=(VERDICT,), execute=lambda call: ToolOutcome("x")
        )
    assert endpoint.requests == []


# ── 4. the fallback for a model the CLI cannot run ──────────────────────────────────────────────


def _desk(endpoint: Endpoint, backup: Backup) -> FallbackLLM:
    return FallbackLLM(
        bedrock(endpoint),
        backup,
        primary_provider="bedrock",
        backup_provider="claude_cli",
        backup_model=load_bedrock_models().backup_model,
    )


def _filing() -> FilingInput:
    return FilingInput(
        filing_id="nse_announcements:TEST-1",
        kind=FilingKind.ANNOUNCEMENT,
        isin="INE000A01010",
        knowable_date=date(2026, 10, 29),
        text="Subject: Financial results\nText: Revenue Rs 1,234.5 crore for Q2 FY27.",
        truncated=False,
    )


def _digest(llm: Any) -> Any:
    return digest_filing(
        _filing(),
        llm=llm,
        trading_date=date(2026, 10, 29),
        clock=FrozenClock(datetime(2026, 10, 29, 19, 0, tzinfo=IST)),
        model=DEEPSEEK,
    )


class DigestBackup(Backup):
    """The CLI stand-in answering the digest tool."""

    def complete(self, messages: Any, *, model: str, **kwargs: Any) -> Any:
        import dataclasses

        response = super().complete(messages, model=model, **kwargs)
        (call,) = response.tool_calls
        return dataclasses.replace(
            response, tool_calls=(dataclasses.replace(call, arguments=DIGEST_ANSWER),)
        )


@pytest.mark.parametrize(
    "failure",
    [chat_error(503), chat_error(429), chat_error(401), _raise(httpx.ConnectError("down"))],
)
def test_deepseek_unavailable_falls_back_to_sonnet_on_the_cli_and_the_digest_says_so(
    failure: Any,
) -> None:
    backup = DigestBackup()
    digest = _digest(_desk(Endpoint(failure), backup))
    (call,) = backup.calls
    assert call["model"] == "claude-sonnet-5-5"  # never DeepSeek: the CLI cannot run it
    assert call["tools"] == [DIGEST_TOOL]
    # The digest records who actually wrote it.
    assert digest.provider == "claude_cli"
    assert digest.model == "claude-sonnet-5-5"
    assert digest.body.headline == DIGEST_ANSWER["headline"]


def test_deepseek_answering_is_recorded_as_bedrock_deepseek_and_the_cli_is_never_asked() -> None:
    backup = DigestBackup()
    digest = _digest(_desk(Endpoint(completion(json.dumps(DIGEST_ANSWER))), backup))
    assert backup.calls == []
    assert (digest.provider, digest.model) == ("bedrock", "deepseek.v3.2")
    assert digest.input_tokens == 1_500 and digest.output_tokens == 220


@pytest.mark.parametrize("status", [400, 422])
def test_deepseek_refusing_the_request_never_falls_back(status: int) -> None:
    backup = DigestBackup()
    with pytest.raises(LLMError) as caught:
        _digest(_desk(Endpoint(chat_error(status)), backup))
    assert not isinstance(caught.value, LLMUnavailableError)
    assert backup.calls == []


def test_a_claude_model_still_falls_back_to_itself() -> None:
    backup = Backup()
    response = _desk(Endpoint(chat_error(503)), backup).complete(
        ASK, model="claude-opus-5-5", tools=(VERDICT,)
    )
    assert backup.calls[0]["model"] == "claude-opus-5-5"
    assert response.fallback is not None and response.fallback.from_provider == "bedrock"


def test_the_fallback_warning_names_the_model_asked_of_the_backup() -> None:
    with capture_logs() as logs:
        _desk(Endpoint(chat_error(503)), DigestBackup()).complete(
            ASK, model=DEEPSEEK, tools=(DIGEST_TOOL,)
        )
    (warning,) = [e for e in logs if e["event"] == "llm.fallback"]
    assert warning["model"] == DEEPSEEK and warning["backup_model"] == "claude-sonnet-5-5"


def test_build_llm_wires_the_bedrock_maps_backup_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from analyst.llm.claude_cli import ClaudeCliLLM

    monkeypatch.setattr(
        ClaudeCliLLM, "from_settings", classmethod(lambda cls, settings=None: Backup())
    )
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, aws_bearer_token_bedrock=SecretStr(FAKE_KEY), bedrock_region="us-east-1"
    )
    llm = build_llm(settings, provider=LlmProvider.BEDROCK, fallback=LlmProvider.CLAUDE_CLI)
    assert isinstance(llm, FallbackLLM)
    assert llm._backup_model is not None
    assert llm._backup_model(DEEPSEEK) == "claude-sonnet-5-5"
    assert llm._backup_model("claude-opus-5-5") == "claude-opus-5-5"


@pytest.mark.parametrize(
    ("route", "match"),
    [
        ({"adapter": "openai_chat"}, "needs a cli_fallback_model"),
        ({"adapter": "openai_chat", "cli_fallback_model": "other"}, "must be a Claude model"),
        ({"adapter": "openai_chat", "cli_fallback_model": "absent"}, "must be a Claude model"),
    ],
)
def test_the_map_refuses_a_non_claude_model_without_a_claude_backup(
    route: Mapping[str, Any], match: str
) -> None:
    other = {
        "adapter": "openai_chat",
        "bedrock_id": "o.x",
        "max_output_tokens": 10,
        "cli_fallback_model": "claude-sonnet-5-5",
    }
    with pytest.raises(ValueError, match=match):
        BedrockModelMap.model_validate(
            {
                "version": 1,
                "models": {
                    "claude-sonnet-5-5": {
                        "adapter": "anthropic_messages",
                        "bedrock_id": "anthropic.claude-sonnet-5-5",
                        "max_output_tokens": 128000,
                    },
                    "other": other,
                    "deepseek-v3-2": {
                        "bedrock_id": "deepseek.v3.2",
                        "max_output_tokens": 8192,
                        **route,
                    },
                },
            }
        )


# ── 5. config and the card ───────────────────────────────────────────────────────────────────────


def test_the_roster_names_each_roles_model_and_each_is_routed() -> None:
    """A model change is the roster's (config only): Amendment 5's three roles, the
    pre-registered models (Opus decisions, Sonnet digests and research).

    Every role is a Claude model, so a desk on the Claude CLI (before the deploy, or rolled back)
    runs the roster's own models and a Bedrock call that falls back is asked of the same model.
    DeepSeek is routed but named by no role: a non-Claude role would need a CLI stand-in again.
    """
    models = load_bedrock_models()
    for book in load_roster().primaries:
        roles = (book.models.decision, book.models.digest, book.models.research)
        assert roles == ("claude-opus-5-5", "claude-sonnet-5-5", "claude-sonnet-5-5")
        for name in roles:
            assert models.route(name).adapter.is_claude
            assert models.backup_model(name) == name
        assert DEEPSEEK not in roles


def test_the_digest_module_has_no_model_of_its_own() -> None:
    import analyst.commons as commons
    import analyst.commons.digests as digests

    assert not hasattr(digests, "DIGEST_MODEL") and not hasattr(commons, "DIGEST_MODEL")
    with pytest.raises(TypeError, match="model"):
        digest_filing(  # type: ignore[call-arg]
            _filing(),
            llm=DigestBackup(),
            trading_date=date(2026, 10, 29),
            clock=FrozenClock(datetime(2026, 10, 29, 19, 0, tzinfo=IST)),
        )


def test_deepseek_is_priced_at_the_aws_us_east_1_on_demand_rate() -> None:
    """$0.62 / $1.85 per 1M (AWS Price List API, read 2026-10-30); no cache discount."""
    response = bedrock(
        Endpoint(completion("ok", prompt_tokens=1_000_000, completion_tokens=1_000_000))
    ).complete(ASK, model=DEEPSEEK)
    priced = TokenPricer(load_price_card()).price(
        response, on=date(2026, 10, 30), purpose="m17_digest"
    )
    assert priced.cost_usd == Decimal("2.47")
    cached = bedrock(
        Endpoint(completion("ok", prompt_tokens=1_000_000, completion_tokens=0, cached=400_000))
    ).complete(ASK, model=DEEPSEEK)
    priced = TokenPricer(load_price_card()).price(cached, on=date(2026, 10, 30), purpose="x")
    assert priced.cost_usd == Decimal("0.62")  # a cached token is never cheaper than input
    schedule = load_price_card().schedule_for(date(2026, 10, 30))
    assert schedule.price_for("deepseek.v3.2").input == Decimal("0.62")


# ── 6. secret hygiene ────────────────────────────────────────────────────────────────────────────


def test_the_key_stays_in_the_authorization_header_only() -> None:
    echoed = f"invalid key {FAKE_KEY} for this account"
    endpoint = Endpoint(chat_error(403, echoed))
    llm = bedrock(endpoint)
    with capture_logs() as logs, pytest.raises(LLMUnavailableError) as caught:
        llm.complete(ASK, model=DEEPSEEK)
    assert FAKE_KEY not in str(caught.value)
    assert FAKE_KEY not in repr(llm) and FAKE_KEY not in repr(llm._openai_chat)
    assert FAKE_KEY not in json.dumps(logs, default=str)
    request = endpoint.requests[-1]
    assert FAKE_KEY not in str(request.url) and FAKE_KEY not in request.content.decode()


def test_the_message_turns_keep_their_roles() -> None:
    endpoint = Endpoint(completion("ok"))
    turns = [Message(Role.USER, "a"), Message(Role.ASSISTANT, "b"), Message(Role.USER, "c")]
    bedrock(endpoint).complete(turns, model=DEEPSEEK)
    assert [m["role"] for m in endpoint.body["messages"]] == ["user", "assistant", "user"]
