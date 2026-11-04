"""X3: Bedrock models that speak OpenAI Chat Completions on the Mantle endpoint (not Claude).

`BedrockLLM` routes a model here when `bedrock_models.yaml` gives it ``adapter: openai_chat`` —
DeepSeek V3.2 (`deepseek.v3.2`) is the first; no caller names it today (Amendment 5 keeps the M17
digests on Sonnet 5.5), so this adapter is dormant capability. Mantle serves those models at
``/v1/chat/completions`` only: the Anthropic Messages path answers them 400 ("does not support the
'/anthropic/v1/messages' API", measured 2026-10-30).

The translation keeps the `LLM` protocol's contract exactly as `BedrockLLM`'s Claude path does:

- **One offered tool is a demand for structured output**, sent as ``response_format: json_schema``
  with ``strict: true`` against `structured_output_schema` of the tool's schema, and answered as
  one `ToolCall` carrying the parsed object. Not a forced function call: on DeepSeek that took
  20 s and chattered before calling, where the schema-constrained answer took about one second.
- **Usage** is the provider's: ``prompt_tokens`` less any ``cached_tokens`` is input,
  ``cached_tokens`` is a cache read, ``completion_tokens`` is output.
- **The recorded model** is the Bedrock id asked for (`route.bedrock_id`), the id the price card
  carries, whatever the endpoint reports.
- **Effort** is sent only when the route says the model accepts one (``effort: true``), as
  ``reasoning_effort``; DeepSeek V3.2's route says it does not, so it is sent none.
- **Failures** sort exactly as the Claude path's do: unreachable, timed out, 429 after retries,
  5xx, 401/403 are `LLMUnavailableError` (a `FallbackLLM` asks its backup); a 400 or any other
  4xx, a ``content_filter`` stop, a truncation or an answer that is not the JSON object asked for
  is not, because another provider would only hide a defect in the request.

The bearer token is held as a `SecretStr`, put on the request's ``Authorization`` header and
nowhere else — never in a repr, a log field, an error's text (masked by value, then by pattern)
or a URL.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any, Final

import httpx
from pydantic import SecretStr

from analyst.llm.client import (
    LLMError,
    LLMRefusalError,
    LLMResponse,
    LLMUnavailableCredentialError,
    LLMUnavailableError,
    LLMUnavailableRateLimitError,
    Message,
    StopReason,
    ToolCall,
    ToolSpec,
    UnavailableReason,
    Usage,
)
from dataplatform.logging import get_logger
from dataplatform.redaction import MASK, mask_secrets

__all__ = ["OPENAI_CHAT_PATH", "ChatEndpoint", "OpenAIChatClient"]

_LOG = get_logger(__name__)

#: Mantle's OpenAI-compatible endpoint, below the region's host (DeepSeek's route).
OPENAI_CHAT_PATH: Final[str] = "/v1/chat/completions"


class ChatEndpoint(StrEnum):
    """Where an `openai_chat` route is served (measured 2026-11-04).

    `MANTLE_V1`: bedrock-mantle ``/v1`` — DeepSeek V3.2 (``/openai/v1`` answers it 400).
    `MANTLE_OPENAI`: bedrock-mantle ``/openai/v1`` — OpenAI models in-region
    (``openai.gpt-5.6-luna``; ``/v1`` answers them 400, and the ``global.``/``us.`` ids 404 here).
    `RUNTIME_OPENAI`: bedrock-runtime ``/openai/v1`` — OpenAI models through a cross-region
    inference profile (``global.openai.gpt-5.6-luna``; the bare id is refused there: "on-demand
    throughput isn't supported").
    """

    MANTLE_V1 = "mantle_v1"
    MANTLE_OPENAI = "mantle_openai"
    RUNTIME_OPENAI = "runtime_openai"

    def base(self, region: str) -> str:
        if self is ChatEndpoint.MANTLE_V1:
            return f"https://bedrock-mantle.{region}.api.aws/v1"
        if self is ChatEndpoint.MANTLE_OPENAI:
            return f"https://bedrock-mantle.{region}.api.aws/openai/v1"
        return f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1"


#: The `ToolCall` id of a structured answer — the one `BedrockLLM` and `ClaudeCliLLM` use.
_STRUCTURED_TOOL_CALL_ID: Final[str] = "structured_output"

#: Statuses retried before the failure is reported (the Anthropic SDK's set for the Claude path).
_RETRIED: Final[frozenset[int]] = frozenset({408, 409, 429})

#: Longest wait between retries, whatever a ``retry-after`` header asks.
_MAX_BACKOFF_SECONDS: Final[float] = 8.0

#: How much provider text an error quotes, after masking (it can reach the journal).
_DETAIL_CHARS: Final[int] = 400


class OpenAIChatClient:
    """One Mantle region's Chat Completions endpoint, for the models routed ``openai_chat``.

    What it does: one non-streamed request per `complete`, retried on connection failures, 408,
    409, 429 and 5xx up to ``max_retries`` times; returns an `LLMResponse` under ``provider``.
    What it assumes: `BedrockLLM` resolved the route (Bedrock id, effort) and the output ceiling.
    What it never does: read a credential or region from the environment, force a tool choice,
    fall back to another provider, or return a refusal, a truncation or an unparsable answer.
    """

    __slots__ = (
        "_api_key",
        "_http_client",
        "_max_retries",
        "_owned",
        "_provider",
        "_region",
        "_sleep",
        "_timeout_seconds",
    )

    def __init__(
        self,
        api_key: SecretStr,
        region: str,
        *,
        provider: str,
        timeout_seconds: float,
        max_retries: int,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key.get_secret_value().strip() or not region.strip():
            raise ValueError("the Chat Completions client needs a key and a region")
        self._api_key = api_key
        self._region = region
        self._provider = provider
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._http_client = http_client
        self._owned: httpx.Client | None = None
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"{type(self).__name__}(region={self._region!r})"

    @property
    def endpoint(self) -> str:
        """The DeepSeek-era default URL (Mantle ``/v1``) — built from the region, never the env."""
        return self.url(ChatEndpoint.MANTLE_V1, "chat/completions")

    def url(self, where: ChatEndpoint, api: str) -> str:
        """The URL of ``api`` (``chat/completions`` or ``responses``) on ``where``."""
        return f"{where.base(self._region)}/{api}"

    @property
    def _client(self) -> httpx.Client:
        if self._http_client is not None:
            return self._http_client
        if self._owned is None:
            self._owned = httpx.Client(timeout=self._timeout_seconds)
        return self._owned

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        bedrock_id: str,
        system: str | None,
        output: ToolSpec | None,
        output_schema: Mapping[str, Any] | None,
        ceiling: int,
        effort: str | None,
        where: ChatEndpoint = ChatEndpoint.MANTLE_V1,
    ) -> LLMResponse:
        """Ask ``bedrock_id`` once; the answer as text, or as one structured `ToolCall`.

        ``output_schema`` is the strict-compilable form of ``output``'s schema (the caller's
        `structured_output_schema`); ``effort`` is None when the route sends none.
        """
        wire: list[dict[str, str]] = []
        if system is not None:
            wire.append({"role": "system", "content": system})
        wire.extend(m.as_dict() for m in messages)
        # OpenAI reasoning models refuse `max_tokens` (400); `max_completion_tokens` is the
        # Chat Completions name for the same ceiling (thinking included), and DeepSeek takes it too.
        body: dict[str, Any] = {
            "model": bedrock_id,
            "messages": wire,
            "max_completion_tokens": ceiling,
        }
        if output is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": output.name,
                    "description": output.description,
                    "strict": True,
                    "schema": dict(output_schema if output_schema is not None else {}),
                },
            }
        if effort is not None:
            body["reasoning_effort"] = effort
        started = time.monotonic()
        payload = self._post(body, model=model, url=self.url(where, "chat/completions"))
        latency_ms = round((time.monotonic() - started) * 1000)
        choice, usage = _choice_and_usage(payload, model=model)
        finish = choice.get("finish_reason")
        reported = payload.get("model")
        fields = {
            "provider": self._provider,
            "adapter": "openai_chat",
            "model": model,
            "served_model": reported if isinstance(reported, str) and reported else bedrock_id,
            "effort": effort,
            "max_tokens": ceiling,
            "stop_reason": finish,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "latency_ms": latency_ms,
        }
        self._check_finish(finish, model=model, ceiling=ceiling, fields=fields)
        _LOG.info("llm.bedrock.call", **fields)
        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        text = content if isinstance(content, str) else ""
        if not text.strip():
            raise LLMRefusalError(
                f"{model} on Bedrock returned an empty completion; there is no answer to act on. "
                "Journal it and escalate rather than treating silence as agreement."
            )
        if output is None:
            return LLMResponse(
                provider=self._provider,
                model=bedrock_id,
                text=text,
                usage=usage,
                stop_reason=StopReason.END_TURN,
            )
        return LLMResponse(
            provider=self._provider,
            model=bedrock_id,
            text=text,
            usage=usage,
            stop_reason=StopReason.TOOL_USE,
            tool_calls=(
                ToolCall(
                    id=_STRUCTURED_TOOL_CALL_ID,
                    name=output.name,
                    arguments=_structured(text, model=model),
                ),
            ),
        )

    # -- the request ------------------------------------------------------------------------------

    def _post(
        self, body: Mapping[str, Any], *, model: str, url: str | None = None
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        attempt = 0
        while True:
            try:
                response = self._client.post(
                    url or self.endpoint,
                    json=dict(body),
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    attempt += 1
                    self._sleep(_backoff(attempt, None))
                    continue
                reason = (
                    UnavailableReason.TIMEOUT
                    if isinstance(exc, httpx.TimeoutException)
                    else UnavailableReason.CONNECTION
                )
                raise self._unavailable(
                    reason, model=model, detail=f"{type(exc).__name__}: {exc}"
                ) from exc
            status = response.status_code
            if status == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise LLMError(
                        f"{model} on Bedrock answered 200 with a body that is not JSON"
                    ) from exc
                if not isinstance(payload, dict):
                    raise LLMError(f"{model} on Bedrock answered 200 with a non-object body")
                return payload
            if (status in _RETRIED or status >= 500) and attempt < self._max_retries:
                attempt += 1
                self._sleep(_backoff(attempt, response.headers))
                continue
            raise self._classified(status, _error_text(response), model=model)

    def _check_finish(
        self, finish: object, *, model: str, ceiling: int, fields: Mapping[str, Any]
    ) -> None:
        """Raise unless the choice ended as an answer; ``fields`` are logged, as it was billed."""
        if finish == "content_filter":
            _LOG.warning("llm.bedrock.refused", **fields)
            raise LLMRefusalError(
                f"{model} on Bedrock stopped on its content filter; there is no answer to act on. "
                "Do not retry the same prompt — journal the refusal and escalate."
            )
        if finish == "length":
            _LOG.error("llm.bedrock.truncated", **fields)
            raise LLMError(
                f"{model} on Bedrock hit its {ceiling}-token output ceiling before it finished, so "
                "the answer is incomplete and is not used"
            )
        if finish != "stop":
            _LOG.error("llm.bedrock.unexpected_stop", **fields)
            raise LLMError(
                f"{model} on Bedrock stopped with {finish!r}, which this call cannot complete on; "
                "treating it as an answer could accept half of one"
            )

    # -- failures ---------------------------------------------------------------------------------

    def _classified(self, status: int, detail: str, *, model: str) -> LLMError:
        reason = _status_reason(status)
        if reason is not None:
            return self._unavailable(reason, model=model, detail=f"HTTP {status}: {detail}")
        return LLMError(
            f"{model} on Bedrock rejected the request (HTTP {status}); this is a defect in the "
            f"request, not an outage, and is not retried elsewhere: {self._detail(detail)}"
        )

    def _unavailable(
        self, reason: UnavailableReason, *, model: str, detail: str
    ) -> LLMUnavailableError:
        message = (
            f"Bedrock ({self._region}) could not serve {model} ({reason.value}): "
            f"{self._detail(detail)}"
        )
        if reason is UnavailableReason.RATE_LIMIT:
            return LLMUnavailableRateLimitError(message, provider=self._provider, reason=reason)
        if reason in (UnavailableReason.AUTH, UnavailableReason.NOT_CONFIGURED):
            return LLMUnavailableCredentialError(message, provider=self._provider, reason=reason)
        return LLMUnavailableError(message, provider=self._provider, reason=reason)

    def _detail(self, text: str) -> str:
        masked = mask_secrets(text.replace(self._api_key.get_secret_value(), MASK))
        if len(masked) <= _DETAIL_CHARS:
            return masked
        return masked[: _DETAIL_CHARS - 1] + "…"


def _status_reason(status: int) -> UnavailableReason | None:
    """The fallback reason of an HTTP status, or None when the request itself is at fault.

    The same split as the Claude path's: 401/403 the credential, 429 throttling (after retries),
    408 a timeout, 5xx the provider. 400, 404, 409, 413, 422 are the request's — never a fallback.
    """
    if status in (401, 403):
        return UnavailableReason.AUTH
    if status == 429:
        return UnavailableReason.RATE_LIMIT
    if status == 408:
        return UnavailableReason.TIMEOUT
    if status >= 500:
        return UnavailableReason.SERVER_ERROR
    return None


def _backoff(attempt: int, headers: httpx.Headers | None) -> float:
    """Seconds before retry ``attempt``: the server's ``retry-after(-ms)``, else 0.5 s doubling."""
    if headers is not None:
        for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
            raw = headers.get(name)
            if raw is not None:
                try:
                    return min(max(float(raw) * scale, 0.0), _MAX_BACKOFF_SECONDS)
                except ValueError:
                    break
    return min(0.5 * float(2 ** (attempt - 1)), _MAX_BACKOFF_SECONDS)


def _error_text(response: httpx.Response) -> str:
    """The provider's error message (OpenAI shape ``{"error": {"message"}}``), or the raw text."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:_DETAIL_CHARS]
    error = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(error, Mapping) and isinstance(error.get("message"), str):
        kind = error.get("type") or error.get("code")
        return f"{kind}: {error['message']}" if kind else str(error["message"])
    if isinstance(body, Mapping) and isinstance(body.get("message"), str):
        return str(body["message"])
    return response.text[:_DETAIL_CHARS]


def _choice_and_usage(payload: Mapping[str, Any], *, model: str) -> tuple[dict[str, Any], Usage]:
    """The first choice and the usage of a 200 body, or an `LLMError` naming what is missing."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMError(f"{model} on Bedrock answered without a choice")
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        raise LLMError(f"{model} on Bedrock answered without usage; an unmetered call is refused")
    prompt = _count(raw, "prompt_tokens", model)
    completion = _count(raw, "completion_tokens", model)
    details = raw.get("prompt_tokens_details")
    cached = 0
    if isinstance(details, Mapping) and details.get("cached_tokens") is not None:
        cached = min(_count(details, "cached_tokens", model), prompt)
    usage = Usage(input_tokens=prompt - cached, output_tokens=completion, cache_read_tokens=cached)
    return choices[0], usage


def _count(mapping: Mapping[str, Any], key: str, model: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LLMError(f"{model} on Bedrock reported {key}={value!r}, not a token count")
    return value


def _structured(text: str, *, model: str) -> dict[str, Any]:
    """The structured answer as an object, or an `LLMError` — never a fallback trigger."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(
            f"{model} on Bedrock was asked for structured output and returned text that is not "
            f"JSON ({exc.msg} at {exc.pos}); an answer that did not parse is not an answer"
        ) from None
    if not isinstance(payload, dict):
        raise LLMError(
            f"{model} on Bedrock returned a JSON {type(payload).__name__}, not the object its "
            "schema asks for"
        )
    return {str(key): value for key, value in payload.items()}


# ── the client-side tool loop, on the Responses API ───────────────────────────────────────────────
#
# Chat Completions refuses function tools with any reasoning effort on GPT-5.6 Luna ("use
# /v1/responses or set reasoning_effort to 'none'", measured 2026-11-04 on bedrock-runtime), so a
# tool loop that keeps the pinned effort runs on `/responses`: function tools, `reasoning.effort`
# and a strict `text.format` json_schema together, `store: false` (nothing kept server-side), the
# reasoning items carried between rounds as `encrypted_content`.

#: Executes one tool call: (content, is_error). `BedrockLLM` wraps its `ToolExecutor` into this.
ChatToolRunner = Callable[[ToolCall], tuple[str, bool]]


def _responses_usage(payload: Mapping[str, Any], *, model: str) -> Usage:
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        raise LLMError(f"{model} on Bedrock answered without usage; an unmetered call is refused")
    prompt = _count(raw, "input_tokens", model)
    completion = _count(raw, "output_tokens", model)
    details = raw.get("input_tokens_details")
    cached = 0
    if isinstance(details, Mapping) and details.get("cached_tokens") is not None:
        cached = min(_count(details, "cached_tokens", model), prompt)
    return Usage(input_tokens=prompt - cached, output_tokens=completion, cache_read_tokens=cached)


def run_tools_responses(
    client: OpenAIChatClient,
    messages: Sequence[Message],
    *,
    model: str,
    bedrock_id: str,
    where: ChatEndpoint,
    system: str | None,
    tools: Sequence[ToolSpec],
    tool_schemas: Sequence[Mapping[str, Any]],
    run: ChatToolRunner,
    output: ToolSpec | None,
    output_schema: Mapping[str, Any] | None,
    ceiling: int,
    effort: str | None,
    max_rounds: int,
) -> tuple[LLMResponse | None, int, tuple[ToolCall, ...], tuple[Usage, ...]]:
    """A bounded tool loop on ``/responses``. Returns (final or None if exhausted, rounds, calls,
    per-round usage). The final's usage is the sum over rounds."""
    items: list[dict[str, Any]] = []
    if system is not None:
        items.append({"role": "system", "content": system})
    items.extend(m.as_dict() for m in messages)
    offered = {t.name for t in tools}
    body_base: dict[str, Any] = {
        "model": bedrock_id,
        "tools": [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "strict": True,
                "parameters": dict(schema),
            }
            for t, schema in zip(tools, tool_schemas, strict=True)
        ],
        "tool_choice": "auto",
        "max_output_tokens": ceiling,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
    if effort is not None:
        body_base["reasoning"] = {"effort": effort}
    if output is not None:
        body_base["text"] = {
            "format": {
                "type": "json_schema",
                "name": output.name,
                "description": output.description,
                "strict": True,
                "schema": dict(output_schema or {}),
            }
        }
    usages: list[Usage] = []
    called: list[ToolCall] = []
    url = client.url(where, "responses")
    for round_number in range(1, max_rounds + 1):
        payload = client._post({**body_base, "input": items}, model=model, url=url)
        usage = _responses_usage(payload, model=model)
        usages.append(usage)
        status = payload.get("status")
        out = payload.get("output")
        if not isinstance(out, list):
            raise LLMError(f"{model} on Bedrock answered /responses without an output list")
        _LOG.info(
            "llm.bedrock.tool_round",
            provider=client._provider,
            adapter="openai_responses",
            model=model,
            round=round_number,
            status=status,
            input_tokens=usage.input_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            output_tokens=usage.output_tokens,
        )
        if status == "incomplete":
            details = payload.get("incomplete_details") or {}
            reason = details.get("reason") if isinstance(details, Mapping) else None
            if reason == "content_filter":
                raise LLMRefusalError(f"{model} on Bedrock stopped on its content filter")
            raise LLMError(f"{model} on Bedrock stopped incomplete ({reason!r}); not used")
        if status != "completed":
            raise LLMError(f"{model} on Bedrock answered /responses with status {status!r}")
        calls = [o for o in out if isinstance(o, Mapping) and o.get("type") == "function_call"]
        if not calls:
            text = "".join(
                part.get("text", "")
                for o in out
                if isinstance(o, Mapping) and o.get("type") == "message"
                for part in (o.get("content") or [])
                if isinstance(part, Mapping) and part.get("type") == "output_text"
            )
            refusal = [
                part
                for o in out
                if isinstance(o, Mapping) and o.get("type") == "message"
                for part in (o.get("content") or [])
                if isinstance(part, Mapping) and part.get("type") == "refusal"
            ]
            if refusal or not text.strip():
                raise LLMRefusalError(f"{model} on Bedrock refused or returned no answer")
            total = Usage(
                input_tokens=sum(u.input_tokens for u in usages),
                output_tokens=sum(u.output_tokens for u in usages),
                cache_read_tokens=sum(u.cache_read_tokens for u in usages),
            )
            if output is None:
                final = LLMResponse(
                    provider=client._provider,
                    model=bedrock_id,
                    text=text,
                    usage=total,
                    stop_reason=StopReason.END_TURN,
                )
            else:
                final = LLMResponse(
                    provider=client._provider,
                    model=bedrock_id,
                    text=text,
                    usage=total,
                    stop_reason=StopReason.TOOL_USE,
                    tool_calls=(
                        ToolCall(
                            id=_STRUCTURED_TOOL_CALL_ID,
                            name=output.name,
                            arguments=_structured(text, model=model),
                        ),
                    ),
                )
            return final, round_number, tuple(called), tuple(usages)
        # Every output item goes back (reasoning with its encrypted content, the calls), then
        # one function_call_output per call.
        items.extend(dict(o) for o in out if isinstance(o, Mapping))
        for c in calls:
            try:
                args = json.loads(c.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = None
            call = ToolCall(
                id=str(c.get("call_id")),
                name=str(c.get("name")),
                arguments=args if isinstance(args, dict) else {},
            )
            called.append(call)
            if call.name not in offered or not isinstance(args, dict):
                content, is_error = f"no tool named {call.name!r} was offered", True
            else:
                content, is_error = run(call)
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call.id,
                    "output": f"ERROR: {content}" if is_error else content,
                }
            )
    return None, max_rounds, tuple(called), tuple(usages)
