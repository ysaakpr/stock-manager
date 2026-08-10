"""X3: the real `LLM` — Anthropic's Messages API, behind the same interface as the stub.

B4: no Anthropic key exists. This module is therefore written, typed and wired, and refuses to be
constructed until a credential is configured. That refusal is the whole point of the file — an
implementation that quietly fell back to `StubLLM` when the key was missing would give an operator
a system that looks like it is reasoning and is not, and the failure would only surface in the
quality of decisions months later.

It never resolves an ambient credential. The Anthropic SDK will happily authenticate from
`ANTHROPIC_API_KEY`, an `ANTHROPIC_AUTH_TOKEN`, or an `ant auth login` profile left on a developer
machine; this client passes exactly the key `Settings` holds and nothing else, so "configured"
means one thing and a stray profile can never make a paper deployment start spending money.

Adaptive thinking is on by default (`thinking={"type": "adaptive"}`): the questions this system
asks — is this thesis broken, what does this filing imply for the driver — are exactly the
multi-step kind it exists for, and the model decides per call how much of it to do. The reasoning
itself is not requested back (`display` is left at its default): the journal records the decision
and the evidence it was made on, and a summary of the model's private reasoning is neither.

Not implemented here, deliberately: server-side refusal fallbacks (`fallbacks`), prompt-cache
breakpoints, and streaming. All three are beta or shape-changing surfaces that cannot be exercised
without a key, and untestable code on the money path is worse than absent code. `ops/BACKLOG.md`
carries them; the day a credential lands they are the first thing to add.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal, cast

import anthropic
from anthropic.types import MessageParam, ThinkingConfigAdaptiveParam, ToolParam
from pydantic import SecretStr

from analyst.llm.client import (
    DEFAULT_MAX_TOKENS,
    LLMCredentialError,
    LLMError,
    LLMRefusalError,
    LLMResponse,
    Message,
    Role,
    StopReason,
    ToolCall,
    ToolSpec,
    Usage,
)
from dataplatform.config import Settings, get_settings

__all__ = ["ANTHROPIC_PROVIDER", "DEFAULT_MODEL", "TRIAGE_MODEL", "AnthropicLLM"]

#: What lands in `token_usage.provider` for a real call.
ANTHROPIC_PROVIDER: Final[str] = "anthropic"

#: The model the analyst asks for unless a caller names another. Quality-first per decision #12 —
#: cost is metered, not minimized. Must appear in `accounting/model_prices.yaml` or the first call
#: fails loudly at pricing time rather than quietly at ₹0.
DEFAULT_MODEL: Final[str] = "claude-opus-5"

#: The cheap model for mechanical triage and keyword work (§8.1's second half). Same rule: priced.
TRIAGE_MODEL: Final[str] = "claude-haiku-4-5"

#: Wall-clock ceiling for one call. Generous because an adaptive-thinking answer on a hard
#: question legitimately takes minutes; the daily loop is EOD and has the time.
_TIMEOUT_SECONDS: Final[float] = 600.0

#: Retries the SDK performs on 408/409/429/5xx. Two is the SDK default and is right here: the
#: daily loop can afford to wait and cannot afford to skip a review because of one 529.
_MAX_RETRIES: Final[int] = 2

#: Provider stop reasons this module understands, mapped to the vocabulary `analyst/` uses.
_STOP_REASONS: Final[dict[str, StopReason]] = {
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "tool_use": StopReason.TOOL_USE,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.REFUSAL,
    "pause_turn": StopReason.PAUSE_TURN,
}


class AnthropicLLM:
    """The live `LLM`. Constructing one without a credential raises `LLMCredentialError`.

    What it does: sends a conversation to the Messages API and returns the text, any tool calls,
    and the token counts the provider billed — the same `LLMResponse` `StubLLM` returns.
    What it assumes: the key came from `Settings` (i.e. from the environment or `.env`), and the
    caller owns retry policy above the SDK's own.
    What it never does: fall back to a stub, resolve an ambient credential, or return a refusal or
    an empty completion as if it were an answer.
    """

    __slots__ = ("_api_key", "_client", "_max_retries", "_thinking", "_timeout_seconds")

    def __init__(
        self,
        api_key: SecretStr | str | None = None,
        *,
        timeout_seconds: float = _TIMEOUT_SECONDS,
        max_retries: int = _MAX_RETRIES,
        adaptive_thinking: bool = True,
    ) -> None:
        secret = SecretStr(api_key) if isinstance(api_key, str) else api_key
        if secret is None or not secret.get_secret_value().strip():
            raise LLMCredentialError(
                "AnthropicLLM has no credential configured: set ANTHROPIC_API_KEY in .env "
                "(with LLM_PROVIDER=anthropic), or use StubLLM, which needs no key. There is no "
                "fallback — an analyst that silently ran on a stub would look like it was "
                "reasoning and would not be."
            )
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds}")
        if max_retries < 0:
            raise ValueError(f"max_retries must not be negative, got {max_retries}")
        self._api_key = secret
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._thinking = adaptive_thinking
        self._client: anthropic.Anthropic | None = None

    def __repr__(self) -> str:
        # The key is a SecretStr and is never interpolated here; a repr in a log must stay safe.
        return f"{type(self).__name__}(timeout_seconds={self._timeout_seconds})"

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        timeout_seconds: float = _TIMEOUT_SECONDS,
        max_retries: int = _MAX_RETRIES,
        adaptive_thinking: bool = True,
    ) -> AnthropicLLM:
        """Build from configuration, raising `LLMCredentialError` when the key is absent (B4)."""
        resolved = get_settings() if settings is None else settings
        return cls(
            resolved.anthropic_api_key,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            adaptive_thinking=adaptive_thinking,
        )

    @property
    def _sdk(self) -> anthropic.Anthropic:
        """The SDK client, built on first use.

        Lazy so that constructing this object — which is what startup wiring does, and what the
        credential check exists for — opens no connection pool and touches no environment beyond
        the key it was handed.
        """
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=self._api_key.get_secret_value(),
                timeout=self._timeout_seconds,
                max_retries=self._max_retries,
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
        """Ask the model, and report what the answer cost in tokens.

        What it does: one non-streaming Messages call, returning text, tool calls and usage.
        What it assumes: `max_tokens` leaves room for both the thinking and the answer — the
        ceiling covers them together, so a value tuned against a non-thinking model truncates.
        What it never does: hide a refusal or an unrecognized termination. A refusal raises; a
        truncated answer comes back with `stop_reason == MAX_TOKENS` so the caller can see the
        argument was cut in half, and its usage is still reported because it was still billed.
        """
        if not messages:
            raise ValueError("complete() needs at least one message")
        if max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {max_tokens}")

        raw = self._sdk.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[_message_param(message) for message in messages],
            system=anthropic.NOT_GIVEN if system is None else system,
            tools=[_tool_param(tool) for tool in tools] if tools else anthropic.NOT_GIVEN,
            thinking=(
                ThinkingConfigAdaptiveParam(type="adaptive")
                if self._thinking
                else anthropic.NOT_GIVEN
            ),
        )

        usage = Usage(
            input_tokens=raw.usage.input_tokens,
            output_tokens=raw.usage.output_tokens,
            cache_write_tokens=raw.usage.cache_creation_input_tokens or 0,
            cache_read_tokens=raw.usage.cache_read_input_tokens or 0,
        )
        stop_reason = _stop_reason(raw.stop_reason)
        if stop_reason is StopReason.REFUSAL:
            category = None if raw.stop_details is None else getattr(raw.stop_details, "category")
            raise LLMRefusalError(
                f"{model} declined the request (category {category!r}); there is no answer to act "
                "on. Do not retry the same prompt — journal the refusal and escalate."
            )

        text: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in raw.content:
            if block.type == "text":
                text.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=_arguments(block.input))
                )
        return LLMResponse(
            provider=ANTHROPIC_PROVIDER,
            model=raw.model,
            text="".join(text),
            usage=usage,
            stop_reason=stop_reason,
            tool_calls=tuple(tool_calls),
        )


def _message_param(message: Message) -> MessageParam:
    """One turn in the SDK's wire shape."""
    role: Literal["user", "assistant"] = "user" if message.role is Role.USER else "assistant"
    return MessageParam(role=role, content=message.content)


def _tool_param(tool: ToolSpec) -> ToolParam:
    """One tool in the SDK's wire shape.

    The cast is the honest shape of what is known: `input_schema` is caller-supplied JSON Schema,
    and the SDK's TypedDict is narrower than anything provable about a mapping built at runtime.
    A malformed schema is rejected by the provider, loudly, on the first call.
    """
    return cast(
        "ToolParam",
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": dict(tool.input_schema),
        },
    )


def _stop_reason(value: str | None) -> StopReason:
    """Map a provider stop reason, refusing to guess at one this module has never seen."""
    if value is None:
        raise LLMError(
            "the response carried no stop_reason; a completion whose termination is unknown "
            "cannot be trusted to be complete"
        )
    try:
        return _STOP_REASONS[value]
    except KeyError:
        raise LLMError(
            f"unknown stop_reason {value!r} from the provider; treating it as a normal end of "
            "turn could silently accept a partial answer. Add it to _STOP_REASONS once its "
            "meaning is established."
        ) from None


def _arguments(value: object) -> dict[str, object]:
    """A tool call's arguments as a mapping, or a loud failure.

    The SDK types `input` as `object` because a tool schema can in principle be any JSON. Every
    tool this system offers takes an object, so anything else means the model answered a tool
    contract that is not the one we published.
    """
    if not isinstance(value, dict):
        raise LLMError(f"tool call arguments must be an object, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}
