"""X3: the `LLM` backed by Claude on Amazon Bedrock — the Mantle endpoint, billed per token to AWS.

The application's primary transport from the owner's 2026-10-30 decision, with the Claude CLI as
the per-call backup (`analyst.llm.fallback`). It answers the same protocol `ClaudeCliLLM` does, in
the same shape, so no call site changes: one offered `ToolSpec` becomes a demand for structured
output (`output_config.format`, the CLI's `--json-schema`) and comes back as one `ToolCall`
carrying the parsed object. Beyond the protocol, `run_tools` is a bounded client-side tool loop
(the model calls tools the caller passes in, the caller's code runs them), which the CLI cannot do.

Four things are pinned here rather than left to a default, each because the default is wrong:

- **The credential and the region come from `Settings` only.** `.env` is read by pydantic and never
  exported, so the SDK's own lookup of `AWS_BEARER_TOKEN_BEDROCK` / `AWS_REGION` would not see it
  — and on a box whose environment holds AWS settings for the S3 mirror, an ambient lookup is how
  a call would silently go out on the wrong account. The key is handed to the SDK explicitly, the
  endpoint URL is built from `BEDROCK_REGION`, and nothing ambient is consulted.
- **Effort** (`output_config.effort`) comes from `effort.yaml`, per call type — the same value the
  Claude CLI is given as `--effort`, so the primary and the backup reason equally hard. Thinking is
  adaptive where the model supports it; on Opus 5.5 and Sonnet 5.5 it cannot be switched off.
- **`max_tokens`** counts thinking, so a caller's 2,000-token digest ceiling would truncate an
  answer before it began. The request carries `max(asked, BEDROCK_MAX_TOKENS)`, clamped to the
  model's own ceiling, and always streams: the SDK refuses a non-streaming request whose ceiling
  could outlast its ten-minute limit.
- **The model id and what the model accepts** come from `bedrock_models.yaml`. An unmapped model
  raises before any request.

Every failure becomes an `LLMError`, sorted by whose fault it is. The provider's — unreachable,
timed out, throttled after the SDK's retries, a 5xx or an error event mid-stream, a rejected key
— is an `LLMUnavailableError`, which a `FallbackLLM` re-asks the backup on. The request's — a 400,
a refusal, a truncation, an answer that is not the JSON object asked for — is not: it is raised
as it is, because sending the same broken question elsewhere would only hide it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
from analyst.llm.effort import EffortPolicy, load_effort_policy
from dataplatform.config import Settings, get_settings
from dataplatform.logging import get_logger
from dataplatform.redaction import MASK, mask_secrets

__all__ = [
    "BEDROCK_MODELS_PATH",
    "BEDROCK_PROVIDER",
    "DEFAULT_TOOL_ROUNDS",
    "BedrockAdapter",
    "BedrockLLM",
    "BedrockModelMap",
    "BedrockModelRoute",
    "ToolExecutionError",
    "ToolExecutor",
    "ToolLoopExhaustedError",
    "ToolLoopResult",
    "ToolOutcome",
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

#: Model calls a `run_tools` loop may make unless the caller bounds it tighter.
DEFAULT_TOOL_ROUNDS: Final[int] = 8

#: Wall-clock ceiling per read of the stream, matching the other implementations' 600 s: a hard
#: answer on a full evidence pack legitimately thinks for minutes before its first token.
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
    Chat Completions would add a value here and a branch in `BedrockLLM.complete` (and
    `run_tools`), its own request and response mapping beside `_stream`; the map in
    `bedrock_models.yaml` routes each model.
    """

    ANTHROPIC_MESSAGES = "anthropic_messages"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BedrockModelRoute(_Frozen):
    """One model's route: its adapter and Bedrock id, its output ceiling, what it accepts."""

    adapter: BedrockAdapter
    bedrock_id: str = Field(min_length=1)
    max_output_tokens: int = Field(ge=1)
    #: `thinking: {type: adaptive}` and `output_config.effort` are accepted (Opus/Sonnet 5.x);
    #: a model without them (Haiku 4.5) is sent neither, rather than a 400.
    adaptive_thinking: bool = True
    effort: bool = True


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


# ── the schema a structured output or a strict tool can compile ──────────────────────────────────


def structured_output_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """``schema`` as `output_config.format` (or a `strict` tool) accepts it, bounds moved aside.

    What it does: walks the schema and moves every keyword structured outputs cannot compile
    (`_UNCOMPILED_KEYWORDS`, and a `minItems` above 1) out of its node and into that node's
    description as ``{maxLength: 500, minLength: 1}``, the same rendering the SDK's own
    `transform_schema` uses; and closes an object that left `additionalProperties` unsaid, which
    strict decoding requires. Everything else — types, enums, patterns, `anyOf`, `required`, an
    explicit `additionalProperties: false` — is kept as it was.
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
    if node.get("type") == "object" and "additionalProperties" not in node:
        node["additionalProperties"] = False
    if moved:
        bounds = "{" + ", ".join(f"{key}: {value}" for key, value in sorted(moved.items())) + "}"
        description = node.get("description")
        node["description"] = bounds if not description else f"{description}\n\n{bounds}"
    return node


# ── the client-side tool loop's vocabulary ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What a tool returned, as the model will read it. ``is_error`` marks a failed run."""

    content: str
    is_error: bool = False


class ToolExecutionError(Exception):
    """A tool could not do its job (a search API down, a page that would not load).

    Raised by a caller's executor; `run_tools` hands its message to the model as an error result,
    so the model can try another source. Any other exception from an executor is a defect in the
    caller's code and propagates.
    """


#: Runs one tool call the model made. Receives the call (name and parsed arguments).
ToolExecutor = Callable[[ToolCall], ToolOutcome]


@dataclass(frozen=True, slots=True)
class ToolLoopResult:
    """A finished tool loop: the final answer, and everything it took to get there.

    ``response.usage`` is the sum over every round — what the whole loop was billed — so a caller
    that meters ``response`` (`accounting.TokenPricer`) records all of it; ``round_usage`` keeps
    the per-round split.
    """

    response: LLMResponse
    rounds: int
    tool_calls: tuple[ToolCall, ...]
    round_usage: tuple[Usage, ...]


class ToolLoopExhaustedError(LLMError):
    """The model still wanted a tool after the last round the caller allowed. Carries the usage
    of every round, because they were all billed."""

    def __init__(self, message: str, *, usage: Usage, rounds: int) -> None:
        super().__init__(message)
        self.usage = usage
        self.rounds = rounds


def _sum(usages: Sequence[Usage]) -> Usage:
    return Usage(
        input_tokens=sum(u.input_tokens for u in usages),
        output_tokens=sum(u.output_tokens for u in usages),
        cache_write_tokens=sum(u.cache_write_tokens for u in usages),
        cache_read_tokens=sum(u.cache_read_tokens for u in usages),
    )


# ── the client ───────────────────────────────────────────────────────────────────────────────────


class BedrockLLM:
    """The `LLM` on Claude in Amazon Bedrock. Constructing one without key and region raises.

    What it does: one streamed Messages call per `complete`, with the mapped Bedrock id, adaptive
    thinking, the pinned effort, a thinking-sized output ceiling and — when a tool is offered —
    structured output against its schema; returns the text or the parsed object and the usage.
    `run_tools` runs a bounded client-side tool loop on the same request settings.
    What it assumes: the key and the region came from `Settings`; the caller validates a
    structured answer against the schema it offered.
    What it never does: read a credential or region from the environment, fall back to another
    provider (that is `FallbackLLM`'s job), force a tool choice, or return a refusal, a truncation
    or an unparsable answer as though it were one.
    """

    __slots__ = (
        "_api_key",
        "_client",
        "_efforts",
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
        efforts: EffortPolicy | None = None,
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
                "silent substitute — with a fallback configured, every call goes to the backup "
                "and says so; without one, nothing runs."
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
        self._efforts = load_effort_policy() if efforts is None else efforts
        self._max_tokens = max_tokens
        self._models = load_bedrock_models() if models is None else models
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._http_client = http_client
        self._client: anthropic.AnthropicBedrockMantle | None = None

    def __repr__(self) -> str:
        # The key is a SecretStr and is never interpolated; a repr in a log must stay safe.
        return f"{type(self).__name__}(region={self._region!r})"

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, http_client: httpx.Client | None = None
    ) -> BedrockLLM:
        """Build from configuration; raises `LLMCredentialError` without key and region."""
        resolved = get_settings() if settings is None else settings
        return cls(
            resolved.aws_bearer_token_bedrock,
            resolved.bedrock_region,
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

    # -- the protocol -----------------------------------------------------------------------------

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
                f"BedrockLLM.complete expresses one structured-output schema per call, not "
                f"{len(tools)} tools (the Claude CLI's contract, so the two stay interchangeable); "
                "a model that calls tools is `run_tools`"
            )
        route = self._models.route(model)
        tool = tools[0] if tools else None
        match route.adapter:
            case BedrockAdapter.ANTHROPIC_MESSAGES:
                ceiling = self._ceiling(route, max_tokens)
                raw, usage, latency_ms = self._stream(
                    [_message_param(m) for m in messages],
                    model=model,
                    route=route,
                    system=system,
                    ceiling=ceiling,
                    output=tool,
                    tools=None,
                )
                self._check_stop(raw, model=model, route=route, ceiling=ceiling, usage=usage)
                _LOG.info(
                    "llm.bedrock.call",
                    **self._fields(raw, model, route, ceiling, usage, latency_ms),
                )
                return self._final(raw, model=model, route=route, output=tool, usage=usage)
            case _:  # pragma: no cover — exhaustive over BedrockAdapter
                assert_never(route.adapter)

    # -- the client-side tool loop ----------------------------------------------------------------

    def run_tools(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec],
        execute: ToolExecutor,
        system: str | None = None,
        max_rounds: int = DEFAULT_TOOL_ROUNDS,
        output: ToolSpec | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> ToolLoopResult:
        """Let the model call ``tools``, run each call with ``execute``, until it answers.

        What it does: each round is one streamed request offering ``tools`` as `strict` tools with
        `tool_choice: auto`. When the model stops for `tool_use`, every call is run through
        ``execute`` and the results go back in the next round (thinking blocks included, as the
        API requires); when it stops with an answer, that is the result — structured against
        ``output`` if one is given (`output_config.format`). Usage is summed over every round.
        What it assumes: ``execute`` is the caller's own code (a search or fetch client); a
        `ToolExecutionError` it raises is shown to the model as an error result.
        What it never does: force a tool (`any`/`tool` choice is a 400 on Opus 5.5 / Sonnet 5.5),
        run a tool the caller did not offer, exceed ``max_rounds`` model calls, or fall back to
        another provider — the CLI cannot run client tools.
        """
        if not messages:
            raise ValueError("run_tools() needs at least one message")
        if not tools:
            raise ValueError("run_tools() needs at least one tool; a plain call is complete()")
        names = [t.name for t in tools]
        if len(set(names)) != len(names):
            raise ValueError(f"tool names must be unique, got {names}")
        if max_rounds < 1:
            raise ValueError(f"max_rounds must be at least 1, got {max_rounds}")
        if max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {max_tokens}")
        route = self._models.route(model)
        match route.adapter:
            case BedrockAdapter.ANTHROPIC_MESSAGES:
                return self._messages_tool_loop(
                    messages,
                    model=model,
                    route=route,
                    tools=tools,
                    execute=execute,
                    system=system,
                    max_rounds=max_rounds,
                    output=output,
                    ceiling=self._ceiling(route, max_tokens),
                )
            case _:  # pragma: no cover — exhaustive over BedrockAdapter
                assert_never(route.adapter)

    def _messages_tool_loop(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        route: BedrockModelRoute,
        tools: Sequence[ToolSpec],
        execute: ToolExecutor,
        system: str | None,
        max_rounds: int,
        output: ToolSpec | None,
        ceiling: int,
    ) -> ToolLoopResult:
        offered = {t.name for t in tools}
        tool_params = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": structured_output_schema(t.input_schema),
                "strict": True,
            }
            for t in tools
        ]
        conversation: list[Any] = [_message_param(m) for m in messages]
        usages: list[Usage] = []
        called: list[ToolCall] = []
        for round_number in range(1, max_rounds + 1):
            raw, usage, latency_ms = self._stream(
                conversation,
                model=model,
                route=route,
                system=system,
                ceiling=ceiling,
                output=output,
                tools=tool_params,
            )
            usages.append(usage)
            fields = self._fields(raw, model, route, ceiling, usage, latency_ms)
            _LOG.info("llm.bedrock.tool_round", round=round_number, **fields)
            if raw.stop_reason != "tool_use":
                total = _sum(usages)
                self._check_stop(raw, model=model, route=route, ceiling=ceiling, usage=total)
                final = self._final(raw, model=model, route=route, output=output, usage=total)
                _LOG.info(
                    "llm.bedrock.tool_loop",
                    model=model,
                    rounds=round_number,
                    tool_calls=len(called),
                    input_tokens=total.input_tokens,
                    output_tokens=total.output_tokens,
                    cache_write_tokens=total.cache_write_tokens,
                    cache_read_tokens=total.cache_read_tokens,
                )
                return ToolLoopResult(
                    response=final,
                    rounds=round_number,
                    tool_calls=tuple(called),
                    round_usage=tuple(usages),
                )
            calls = [
                ToolCall(id=block.id, name=block.name, arguments=_arguments(block.input))
                for block in raw.content
                if block.type == "tool_use"
            ]
            called.extend(calls)
            # The assistant turn goes back whole — thinking blocks and their signatures included —
            # because the API requires the thinking that led to a tool call alongside it.
            conversation.append(
                {
                    "role": "assistant",
                    "content": [
                        block.model_dump(mode="json", exclude_none=True) for block in raw.content
                    ],
                }
            )
            conversation.append(
                {"role": "user", "content": [_tool_result(c, execute, offered) for c in calls]}
            )
        total = _sum(usages)
        _LOG.error(
            "llm.bedrock.tool_loop_exhausted",
            model=model,
            rounds=max_rounds,
            tool_calls=len(called),
            input_tokens=total.input_tokens,
            output_tokens=total.output_tokens,
        )
        raise ToolLoopExhaustedError(
            f"{model} on Bedrock still wanted a tool after {max_rounds} rounds "
            f"({len(called)} tool calls); the loop is bounded and there is no answer",
            usage=total,
            rounds=max_rounds,
        )

    # -- one streamed request ---------------------------------------------------------------------

    def _ceiling(self, route: BedrockModelRoute, asked: int) -> int:
        return min(route.max_output_tokens, max(asked, self._max_tokens))

    def _stream(
        self,
        conversation: Sequence[Any],
        *,
        model: str,
        route: BedrockModelRoute,
        system: str | None,
        ceiling: int,
        output: ToolSpec | None,
        tools: Sequence[Mapping[str, Any]] | None,
    ) -> tuple[SdkMessage, Usage, int]:
        """One streamed Messages request; the final message, its usage and its latency."""
        output_config: dict[str, Any] = {}
        if route.effort:
            output_config["effort"] = self._efforts.effort_for(model).value
        if output is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": structured_output_schema(output.input_schema),
            }
        extra: dict[str, Any] = {}
        if output_config:
            extra["output_config"] = output_config
        if route.adaptive_thinking:
            extra["thinking"] = {"type": "adaptive"}
        if tools:
            extra["tools"] = list(tools)
            extra["tool_choice"] = {"type": "auto"}
        started = time.monotonic()
        try:
            with self._sdk.messages.stream(
                model=route.bedrock_id,
                max_tokens=ceiling,
                messages=cast("Any", list(conversation)),
                system=anthropic.omit if system is None else system,
                **extra,
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
        usage = Usage(
            input_tokens=raw.usage.input_tokens,
            output_tokens=raw.usage.output_tokens,
            cache_write_tokens=raw.usage.cache_creation_input_tokens or 0,
            cache_read_tokens=raw.usage.cache_read_input_tokens or 0,
        )
        return raw, usage, latency_ms

    def _fields(
        self,
        raw: SdkMessage,
        model: str,
        route: BedrockModelRoute,
        ceiling: int,
        usage: Usage,
        latency_ms: int,
    ) -> dict[str, Any]:
        return {
            "provider": BEDROCK_PROVIDER,
            "model": model,
            "served_model": _served(raw, route),
            "effort": self._efforts.effort_for(model).value if route.effort else None,
            "max_tokens": ceiling,
            "stop_reason": raw.stop_reason,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "latency_ms": latency_ms,
        }

    def _check_stop(
        self,
        raw: SdkMessage,
        *,
        model: str,
        route: BedrockModelRoute,
        ceiling: int,
        usage: Usage,
    ) -> None:
        """Raise unless the message ended as an answer. ``usage`` is logged: it was billed."""
        billed = {
            "provider": BEDROCK_PROVIDER,
            "model": model,
            "stop_reason": raw.stop_reason,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
        }
        if raw.stop_reason == "refusal":
            _LOG.warning("llm.bedrock.refused", **billed)
            category = None if raw.stop_details is None else raw.stop_details.category
            raise LLMRefusalError(
                f"{model} on Bedrock declined the request (category {category!r}); there is no "
                "answer to act on. Do not retry the same prompt — journal the refusal and escalate."
            )
        if raw.stop_reason == "max_tokens":
            _LOG.error("llm.bedrock.truncated", max_tokens=ceiling, **billed)
            raise LLMError(
                f"{model} on Bedrock hit its {ceiling}-token output ceiling before it finished — "
                "thinking counts against that ceiling — so the answer is incomplete and is not "
                "used. Raise BEDROCK_MAX_TOKENS (up to the model's "
                f"{route.max_output_tokens}) or lower the call type's effort in effort.yaml."
            )
        if raw.stop_reason not in ("end_turn", "stop_sequence"):
            _LOG.error("llm.bedrock.unexpected_stop", **billed)
            raise LLMError(
                f"{model} on Bedrock stopped with {raw.stop_reason!r}, which this call cannot "
                "complete on; treating it as an answer could accept half of one"
            )

    def _final(
        self,
        raw: SdkMessage,
        *,
        model: str,
        route: BedrockModelRoute,
        output: ToolSpec | None,
        usage: Usage,
    ) -> LLMResponse:
        """The answer as an `LLMResponse`: text, or one structured `ToolCall` for ``output``."""
        served = _served(raw, route)
        text = "".join(block.text for block in raw.content if block.type == "text")
        if output is None:
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
                    name=output.name,
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


def _served(raw: SdkMessage, route: BedrockModelRoute) -> str:
    """The model id Bedrock reports serving, or the mapped id when it reports none."""
    return raw.model if raw.model and raw.model.strip() else route.bedrock_id


def _message_param(message: Message) -> MessageParam:
    """One turn in the SDK's wire shape."""
    role: Literal["user", "assistant"] = "user" if message.role is Role.USER else "assistant"
    return MessageParam(role=role, content=message.content)


def _arguments(value: object) -> dict[str, Any]:
    """A tool call's arguments as a mapping; strict tools guarantee an object."""
    if not isinstance(value, dict):
        raise LLMError(f"tool call arguments must be an object, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def _tool_result(call: ToolCall, execute: ToolExecutor, offered: set[str]) -> dict[str, Any]:
    """Run one tool call and shape its outcome as a `tool_result` block."""
    if call.name not in offered:
        outcome = ToolOutcome(f"no tool named {call.name!r} was offered", is_error=True)
    else:
        try:
            outcome = execute(call)
        except ToolExecutionError as exc:
            outcome = ToolOutcome(str(exc) or type(exc).__name__, is_error=True)
    _LOG.info("llm.bedrock.tool_call", tool=call.name, is_error=outcome.is_error)
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": call.id,
        "content": outcome.content,
    }
    if outcome.is_error:
        block["is_error"] = True
    return block


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
