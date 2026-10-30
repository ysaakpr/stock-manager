"""X3: the `LLM` backed by Claude on Amazon Bedrock — the Mantle endpoint, billed per token to AWS.

The M17 desk's primary transport from the owner's 2026-10-30 decision, with the Claude CLI as the
per-call backup (`analyst.llm.fallback`). It answers the same protocol `ClaudeCliLLM` does, in the
same shape, so no call site changes: one offered `ToolSpec` becomes a demand for structured output
(`output_config.format`, the CLI's `--json-schema`) and comes back as one `ToolCall` carrying the
parsed object.

Four things are pinned here rather than left to a default, each because the default is wrong:

- **The credential and the region come from `Settings` only.** `.env` is read by pydantic and never
  exported, so the SDK's own lookup of `AWS_BEARER_TOKEN_BEDROCK` / `AWS_REGION` would not see it
  — and on a box whose environment holds AWS settings for the S3 mirror, an ambient lookup is how
  a call would silently go out on the wrong account. The key is handed to the SDK explicitly, the
  endpoint URL is built from `BEDROCK_REGION`, and nothing ambient is consulted.
- **Effort** (`output_config.effort`) comes from `LLM_EFFORT`, default `xhigh`: what the desk has
  actually run at since S0, inherited until now from the owner's personal CLI setting. Thinking is
  adaptive; on Opus 5.5 and Sonnet 5.5 it cannot be switched off.
- **`max_tokens`** counts thinking, so a caller's 2,000-token digest ceiling would truncate an
  `xhigh` answer before it began. The request carries `max(asked, BEDROCK_MAX_TOKENS)`, clamped to
  the model's own ceiling, and always streams: the SDK refuses a non-streaming request whose
  ceiling could outlast its ten-minute limit.
- **The model id** comes from `bedrock_models.yaml`. An unmapped model raises before any request.

Every failure becomes an `LLMError`, sorted by whose fault it is. The provider's — unreachable,
timed out, throttled after the SDK's retries, a 5xx or an error event mid-stream, a rejected key
— is an `LLMUnavailableError`, which a `FallbackLLM` re-asks the backup on. The request's — a 400,
a refusal, a truncation, an answer that is not the JSON object asked for — is not: it is raised
as it is, because sending the same broken question elsewhere would only hide it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal, assert_never, cast

import anthropic
import httpx
import yaml
from anthropic.types import Message as SdkMessage
from anthropic.types import MessageParam
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from analyst.llm.client import (
    DEFAULT_MAX_TOKENS,
    LLMCredentialError,
    LLMError,
    LLMRefusalError,
    LLMResponse,
    LLMUnavailableCredentialError,
    LLMUnavailableError,
    LLMUnavailableRateLimitError,
    Message,
    Role,
    StopReason,
    ToolCall,
    ToolSpec,
    UnavailableReason,
    Usage,
)
from dataplatform.config import LlmEffort, Settings, get_settings
from dataplatform.logging import get_logger
from dataplatform.redaction import MASK, mask_secrets

__all__ = [
    "BEDROCK_MODELS_PATH",
    "BEDROCK_PROVIDER",
    "BedrockAdapter",
    "BedrockLLM",
    "BedrockModelMap",
    "BedrockModelRoute",
    "load_bedrock_models",
    "structured_output_schema",
]

_LOG = get_logger(__name__)

#: What lands in `token_usage.provider` and `LLMResponse.provider` for a Bedrock call. Distinct
#: from `anthropic` and `claude_cli`: the same model, billed by a different party at its own rate.
BEDROCK_PROVIDER: Final[str] = "bedrock"

#: The model-to-adapter map (see the file's header).
BEDROCK_MODELS_PATH: Final[Path] = Path(__file__).with_name("bedrock_models.yaml")

#: The same id `ClaudeCliLLM` gives its synthetic structured-output call, so a caller that
#: correlates by id sees one value whichever provider answered.
STRUCTURED_TOOL_CALL_ID: Final[str] = "structured_output"

#: Wall-clock ceiling per read of the stream, matching the other implementations' 600 s: an
#: `xhigh` answer on a full evidence pack legitimately thinks for minutes before its first token.
_TIMEOUT_SECONDS: Final[float] = 600.0

#: Retries the SDK performs on connection errors, 408/409/429 and 5xx before this module sees the
#: failure. The SDK default; a fallback is the next step, not more retries.
_MAX_RETRIES: Final[int] = 2

#: How much of a provider error message an `LLMError` quotes, after masking. An `LLMError`'s text
#: reaches the append-only journal through the M17 runtime, so it is bounded and masked here.
_DETAIL_CHARS: Final[int] = 400

#: JSON Schema keywords structured outputs do not compile (numeric and length bounds, array
#: maxima). They are moved into the node's description — the model reads them there — and the
#: caller's own parser enforces them on the answer, exactly as it does for every other provider.
_UNCOMPILED_KEYWORDS: Final[tuple[str, ...]] = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
)

#: Error-event types a stream can end with that are the provider's fault (an HTTP 200 whose body
#: then failed), mapped to the same reasons as their status-code twins.
_STREAM_ERROR_REASONS: Final[dict[str, UnavailableReason]] = {
    "overloaded_error": UnavailableReason.SERVER_ERROR,
    "api_error": UnavailableReason.SERVER_ERROR,
    "rate_limit_error": UnavailableReason.RATE_LIMIT,
}


# ── the model map ─────────────────────────────────────────────────────────────────────────────────


class BedrockAdapter(StrEnum):
    """How a Bedrock model is spoken to. The seam for models that are not Claude.

    `ANTHROPIC_MESSAGES` is Claude on the Mantle endpoint's Anthropic Messages API — the only
    adapter built. A model served through bedrock-runtime Converse or Mantle's OpenAI-compatible
    Chat Completions would add a value here and a branch in `BedrockLLM.complete`, its own request
    and response mapping beside `_messages`; the map in `bedrock_models.yaml` routes each model.
    """

    ANTHROPIC_MESSAGES = "anthropic_messages"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BedrockModelRoute(_Frozen):
    """One model's route: the adapter that speaks to it, its Bedrock id, its output ceiling."""

    adapter: BedrockAdapter
    bedrock_id: str = Field(min_length=1)
    max_output_tokens: int = Field(ge=1)


class BedrockModelMap(_Frozen):
    """Every model a caller may name on Bedrock, by the name the caller uses."""

    version: int
    models: dict[str, BedrockModelRoute] = Field(min_length=1)

    def route(self, model: str) -> BedrockModelRoute:
        """The route for ``model``, or an `LLMError` naming the models that are mapped.

        Not an `LLMUnavailableError`: an unmapped model is a configuration bug, and asking the
        backup the same question would hide it until the day the backup is down too.
        """
        try:
            return self.models[model]
        except KeyError:
            raise LLMError(
                f"model {model!r} is not mapped to Bedrock; the map covers {sorted(self.models)}. "
                f"Add it to {BEDROCK_MODELS_PATH.name} with its Bedrock id — never guess an id."
            ) from None


@lru_cache(maxsize=1)
def load_bedrock_models(path: Path = BEDROCK_MODELS_PATH) -> BedrockModelMap:
    """Parse and validate the model map, once per process. A schema break raises."""
    with path.open(encoding="utf-8") as handle:
        return BedrockModelMap.model_validate(yaml.safe_load(handle))


# ── the schema a structured output can compile ───────────────────────────────────────────────────


def structured_output_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """``schema`` as `output_config.format` accepts it: the same shape, uncompiled bounds moved.

    What it does: walks the schema and moves every keyword structured outputs cannot compile
    (`_UNCOMPILED_KEYWORDS`, and a `minItems` above 1) out of its node and into that node's
    description as ``{maxLength: 500, minLength: 1}``, the same rendering the SDK's own
    `transform_schema` uses. Everything else — types, enums, patterns, `anyOf`, `required`,
    `additionalProperties: false` — is kept as it was.
    What it assumes: the caller validates the answer against the *original* schema, as every M17
    parser does (a bound the model ignored is a `MalformedOutputError` and the session's repair).
    What it never does: change a property's name, type or requiredness, or touch the input.

    Written here rather than taken from `anthropic.transform_schema` because that helper rejects a
    `type` list (`["string", "null"]`), which every M17 schema uses for a nullable field.
    """
    node: dict[str, Any] = {}
    moved: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNCOMPILED_KEYWORDS or (key == "minItems" and value not in (0, 1)):
            moved[key] = value
        elif key == "properties" and isinstance(value, Mapping):
            node[key] = {name: structured_output_schema(sub) for name, sub in value.items()}
        elif key in ("items", "additionalProperties") and isinstance(value, Mapping):
            node[key] = structured_output_schema(value)
        elif key in ("anyOf", "allOf", "oneOf") and isinstance(value, list):
            node[key] = [structured_output_schema(sub) for sub in value]
        elif key in ("$defs", "definitions") and isinstance(value, Mapping):
            node[key] = {name: structured_output_schema(sub) for name, sub in value.items()}
        else:
            node[key] = value
    if moved:
        bounds = "{" + ", ".join(f"{key}: {value}" for key, value in sorted(moved.items())) + "}"
        description = node.get("description")
        node["description"] = bounds if not description else f"{description}\n\n{bounds}"
    return node


# ── the client ───────────────────────────────────────────────────────────────────────────────────


class BedrockLLM:
    """The `LLM` on Claude in Amazon Bedrock. Constructing one without key and region raises.

    What it does: one streamed Messages call per `complete`, with the mapped Bedrock id, adaptive
    thinking, the pinned effort, a thinking-sized output ceiling and — when a tool is offered —
    structured output against its schema; returns the text or the parsed object and the usage.
    What it assumes: the key and the region came from `Settings`; the caller validates a
    structured answer against the schema it offered.
    What it never does: read a credential or region from the environment, fall back to another
    provider (that is `FallbackLLM`'s job), give the model a tool, or return a refusal, a
    truncation or an unparsable answer as though it were one.
    """

    __slots__ = (
        "_api_key",
        "_client",
        "_effort",
        "_http_client",
        "_max_retries",
        "_max_tokens",
        "_models",
        "_region",
        "_timeout_seconds",
    )

    def __init__(
        self,
        api_key: SecretStr | None,
        region: str | None,
        *,
        effort: LlmEffort = LlmEffort.XHIGH,
        max_tokens: int = 64_000,
        models: BedrockModelMap | None = None,
        timeout_seconds: float = _TIMEOUT_SECONDS,
        max_retries: int = _MAX_RETRIES,
        http_client: httpx.Client | None = None,
    ) -> None:
        missing = [
            name
            for name, value in (("AWS_BEARER_TOKEN_BEDROCK", api_key), ("BEDROCK_REGION", region))
            if value is None
            or not (value.get_secret_value() if isinstance(value, SecretStr) else value).strip()
        ]
        if missing:
            raise LLMCredentialError(
                f"BedrockLLM is not configured: set {' and '.join(missing)} in .env. There is no "
                "silent substitute — with M17_LLM_FALLBACK set, every call goes to the backup and "
                "says so; without it, nothing runs."
            )
        if max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {max_tokens}")
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds}")
        if max_retries < 0:
            raise ValueError(f"max_retries must not be negative, got {max_retries}")
        assert api_key is not None and region is not None  # narrowed by `missing` above
        self._api_key = api_key
        self._region = region
        self._effort = effort
        self._max_tokens = max_tokens
        self._models = load_bedrock_models() if models is None else models
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._http_client = http_client
        self._client: anthropic.AnthropicBedrockMantle | None = None

    def __repr__(self) -> str:
        # The key is a SecretStr and is never interpolated; a repr in a log must stay safe.
        return f"{type(self).__name__}(region={self._region!r}, effort={self._effort.value!r})"

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, http_client: httpx.Client | None = None
    ) -> BedrockLLM:
        """Build from configuration; raises `LLMCredentialError` without key and region."""
        resolved = get_settings() if settings is None else settings
        return cls(
            resolved.aws_bearer_token_bedrock,
            resolved.bedrock_region,
            effort=resolved.llm_effort,
            max_tokens=resolved.bedrock_max_tokens,
            http_client=http_client,
        )

    @property
    def endpoint(self) -> str:
        """The Mantle endpoint this client calls — built from the region, never from the env."""
        return f"https://bedrock-mantle.{self._region}.api.aws/anthropic"

    @property
    def _sdk(self) -> anthropic.AnthropicBedrockMantle:
        """The SDK client, built on first use, with the key and endpoint passed explicitly."""
        if self._client is None:
            self._client = anthropic.AnthropicBedrockMantle(
                api_key=self._api_key.get_secret_value(),
                aws_region=self._region,
                base_url=self.endpoint,
                timeout=self._timeout_seconds,
                max_retries=self._max_retries,
                http_client=self._http_client,
            )
        return self._client

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        """Ask the model on Bedrock, and report what the answer cost in tokens.

        What it does: routes ``model`` through `bedrock_models.yaml` and answers through the
        route's adapter. At most one tool, which becomes structured output (as on the CLI).
        What it never does: honour a ``max_tokens`` below `BEDROCK_MAX_TOKENS` — thinking counts
        against it, and a ceiling sized for the answer alone truncates it.
        """
        if not messages:
            raise ValueError("complete() needs at least one message")
        if max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {max_tokens}")
        if len(tools) > 1:
            raise ValueError(
                f"BedrockLLM expresses one structured-output schema per call, not {len(tools)} "
                "tools (the same contract as the Claude CLI, so the two stay interchangeable)"
            )
        route = self._models.route(model)
        match route.adapter:
            case BedrockAdapter.ANTHROPIC_MESSAGES:
                return self._messages(
                    messages,
                    model=model,
                    route=route,
                    tool=tools[0] if tools else None,
                    system=system,
                    max_tokens=max_tokens,
                )
            case _:  # pragma: no cover — exhaustive over BedrockAdapter
                assert_never(route.adapter)

    # -- the Anthropic Messages adapter ----------------------------------------------------------

    def _messages(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        route: BedrockModelRoute,
        tool: ToolSpec | None,
        system: str | None,
        max_tokens: int,
    ) -> LLMResponse:
        ceiling = min(route.max_output_tokens, max(max_tokens, self._max_tokens))
        output_config: dict[str, Any] = {"effort": self._effort.value}
        if tool is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": structured_output_schema(tool.input_schema),
            }
        started = time.monotonic()
        try:
            with self._sdk.messages.stream(
                model=route.bedrock_id,
                max_tokens=ceiling,
                messages=[_message_param(message) for message in messages],
                system=anthropic.omit if system is None else system,
                thinking={"type": "adaptive"},
                output_config=cast("Any", output_config),
            ) as stream:
                raw = stream.get_final_message()
        except anthropic.APIError as exc:
            raise self._classified(exc, model=model) from exc
        except httpx.TransportError as exc:
            # A connection that broke mid-stream surfaces as httpx's own error, not the SDK's.
            reason = (
                UnavailableReason.TIMEOUT
                if isinstance(exc, httpx.TimeoutException)
                else UnavailableReason.CONNECTION
            )
            raise self._unavailable(
                reason, model=model, detail=f"{type(exc).__name__}: {exc}"
            ) from exc
        latency_ms = round((time.monotonic() - started) * 1000)
        return self._response(
            raw, model=model, route=route, tool=tool, ceiling=ceiling, latency_ms=latency_ms
        )

    def _response(
        self,
        raw: SdkMessage,
        *,
        model: str,
        route: BedrockModelRoute,
        tool: ToolSpec | None,
        ceiling: int,
        latency_ms: int,
    ) -> LLMResponse:
        usage = Usage(
            input_tokens=raw.usage.input_tokens,
            output_tokens=raw.usage.output_tokens,
            cache_write_tokens=raw.usage.cache_creation_input_tokens or 0,
            cache_read_tokens=raw.usage.cache_read_input_tokens or 0,
        )
        served = raw.model if raw.model and raw.model.strip() else route.bedrock_id
        fields = {
            "provider": BEDROCK_PROVIDER,
            "model": model,
            "served_model": served,
            "effort": self._effort.value,
            "max_tokens": ceiling,
            "stop_reason": raw.stop_reason,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "latency_ms": latency_ms,
        }
        if raw.stop_reason == "refusal":
            _LOG.warning("llm.bedrock.refused", **fields)
            category = None if raw.stop_details is None else raw.stop_details.category
            raise LLMRefusalError(
                f"{model} on Bedrock declined the request (category {category!r}); there is no "
                "answer to act on. Do not retry the same prompt — journal the refusal and escalate."
            )
        if raw.stop_reason == "max_tokens":
            # Billed all the same; the usage is logged so a truncation is never invisible spend.
            _LOG.error("llm.bedrock.truncated", **fields)
            raise LLMError(
                f"{model} on Bedrock hit its {ceiling}-token output ceiling before it finished — "
                "thinking counts against that ceiling — so the answer is incomplete and is not "
                "used. Raise BEDROCK_MAX_TOKENS (up to the model's "
                f"{route.max_output_tokens}) or lower LLM_EFFORT."
            )
        if raw.stop_reason not in ("end_turn", "stop_sequence"):
            _LOG.error("llm.bedrock.unexpected_stop", **fields)
            raise LLMError(
                f"{model} on Bedrock stopped with {raw.stop_reason!r}, which a single-turn call "
                "with no tools cannot complete on; treating it as an answer could accept half one"
            )
        _LOG.info("llm.bedrock.call", **fields)

        text = "".join(block.text for block in raw.content if block.type == "text")
        if tool is None:
            if not text.strip():
                raise LLMRefusalError(
                    f"{model} on Bedrock returned an empty completion; there is no answer to act "
                    "on. Journal it and escalate rather than treating silence as agreement."
                )
            return LLMResponse(
                provider=BEDROCK_PROVIDER,
                model=served,
                text=text,
                usage=usage,
                stop_reason=StopReason.END_TURN,
            )
        return LLMResponse(
            provider=BEDROCK_PROVIDER,
            model=served,
            text=text,
            usage=usage,
            stop_reason=StopReason.TOOL_USE,
            tool_calls=(
                ToolCall(
                    id=STRUCTURED_TOOL_CALL_ID,
                    name=tool.name,
                    arguments=_structured(text, model=model),
                ),
            ),
        )

    # -- failures ---------------------------------------------------------------------------------

    def _classified(self, exc: anthropic.APIError, *, model: str) -> LLMError:
        """An SDK failure as this package's error: unavailable (a fallback trigger) or not."""
        detail = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, anthropic.APITimeoutError):
            return self._unavailable(UnavailableReason.TIMEOUT, model=model, detail=detail)
        if isinstance(exc, anthropic.APIConnectionError):
            return self._unavailable(UnavailableReason.CONNECTION, model=model, detail=detail)
        if isinstance(exc, anthropic.APIStatusError):
            reason = _status_reason(exc)
            if reason is not None:
                return self._unavailable(reason, model=model, detail=detail)
            return LLMError(
                f"{model} on Bedrock rejected the request (HTTP {exc.status_code}); this is a "
                f"defect in the request, not an outage, and is not retried elsewhere: "
                f"{self._detail(detail)}"
            )
        return LLMError(f"{model} on Bedrock failed: {self._detail(detail)}")

    def _unavailable(
        self, reason: UnavailableReason, *, model: str, detail: str
    ) -> LLMUnavailableError:
        message = (
            f"Bedrock ({self._region}) could not serve {model} ({reason.value}): "
            f"{self._detail(detail)}"
        )
        match reason:
            case UnavailableReason.RATE_LIMIT:
                return LLMUnavailableRateLimitError(
                    message, provider=BEDROCK_PROVIDER, reason=reason
                )
            case UnavailableReason.AUTH | UnavailableReason.NOT_CONFIGURED:
                return LLMUnavailableCredentialError(
                    message, provider=BEDROCK_PROVIDER, reason=reason
                )
            case _:
                return LLMUnavailableError(message, provider=BEDROCK_PROVIDER, reason=reason)

    def _detail(self, text: str) -> str:
        """Provider text as an error may quote it: the key itself, then anything credential-shaped,
        masked; then bounded. The key is masked by value first because a Bedrock API key has no
        prefix the generic patterns are guaranteed to know."""
        masked = mask_secrets(text.replace(self._api_key.get_secret_value(), MASK))
        if len(masked) <= _DETAIL_CHARS:
            return masked
        return masked[: _DETAIL_CHARS - 1] + "…"


def _status_reason(exc: anthropic.APIStatusError) -> UnavailableReason | None:
    """The fallback reason of an HTTP failure, or None when the request itself is at fault.

    401/403 are the credential (expired, revoked, or an IAM policy without
    `bedrock-mantle:CreateInference`); 429 is throttling the SDK already retried; 5xx and 529 are
    the provider's. An error *event* inside a 200 stream carries its type in the body instead.
    Everything else — 400, 404, 409, 413, 422 — is the request's, and is never a fallback.
    """
    status = exc.status_code
    if status in (401, 403):
        return UnavailableReason.AUTH
    if status == 429:
        return UnavailableReason.RATE_LIMIT
    if status >= 500:
        return UnavailableReason.SERVER_ERROR
    if status < 400:
        body = exc.body
        error = body.get("error") if isinstance(body, Mapping) else None
        kind = error.get("type") if isinstance(error, Mapping) else None
        return _STREAM_ERROR_REASONS.get(kind) if isinstance(kind, str) else None
    return None


def _message_param(message: Message) -> MessageParam:
    """One turn in the SDK's wire shape."""
    role: Literal["user", "assistant"] = "user" if message.role is Role.USER else "assistant"
    return MessageParam(role=role, content=message.content)


def _structured(text: str, *, model: str) -> dict[str, Any]:
    """The structured answer as an object, or an `LLMError` — never a fallback trigger.

    `output_config.format` constrains decoding, so a non-object here means the provider broke its
    own contract; asking another provider would hide that rather than fix it.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(
            f"{model} on Bedrock was asked for structured output and returned text that is not "
            f"JSON ({exc.msg} at {exc.pos}); a verdict that did not parse is not a verdict"
        ) from None
    if not isinstance(payload, dict):
        raise LLMError(
            f"{model} on Bedrock returned a JSON {type(payload).__name__}, not the object its "
            "schema asks for"
        )
    return {str(key): value for key, value in payload.items()}
