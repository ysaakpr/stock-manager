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
Adaptive thinking is a per-model capability, not a universal one, so the parameter is chosen from
the model being called rather than sent unconditionally — see `_thinking_param`, which is where
the two ways of getting that wrong are written down.

Not implemented here, deliberately: server-side refusal fallbacks (`fallbacks`), prompt-cache
breakpoints, and streaming. All three are beta or shape-changing surfaces that cannot be exercised
without a key, and untestable code on the money path is worse than absent code. This paragraph is
the record of that decision; the day a credential lands they are the first thing to add.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal, cast

import anthropic
import httpx
from anthropic.types import (
    MessageParam,
    ThinkingConfigAdaptiveParam,
    ThinkingConfigDisabledParam,
    ThinkingConfigParam,
    ToolParam,
)
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

#: Models documented as accepting `thinking={"type": "adaptive"}` — a 4.6-generation capability.
#: Older models, `TRIAGE_MODEL` (Haiku 4.5) among them, are documented to take a `budget_tokens`
#: thinking config instead and to reject `adaptive`; that rejection is read off the provider's
#: documentation and has *not* been observed here, because no credential exists (B4) and the
#: suite never calls out (B8). The SDK cannot settle it either: `anthropic` 0.121.0 types
#: `thinking` identically for every model, so a wrong value here type-checks and fails, if it
#: fails, only against the live API.
#:
#: The list is therefore conservative in the safe direction. A model missing from it has the
#: parameter omitted entirely rather than guessed at — which costs thinking on a model that
#: would have accepted it, and is never itself rejected.
_ADAPTIVE_THINKING_MODELS: Final[frozenset[str]] = frozenset(
    {"claude-opus-5", "claude-opus-4-8", "claude-sonnet-5"}
)

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

    __slots__ = (
        "_api_key",
        "_client",
        "_http_client",
        "_max_retries",
        "_thinking",
        "_timeout_seconds",
    )

    def __init__(
        self,
        api_key: SecretStr | str | None = None,
        *,
        timeout_seconds: float = _TIMEOUT_SECONDS,
        max_retries: int = _MAX_RETRIES,
        adaptive_thinking: bool = True,
        http_client: httpx.Client | None = None,
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
        self._http_client = http_client
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
        http_client: httpx.Client | None = None,
    ) -> AnthropicLLM:
        """Build from configuration, raising `LLMCredentialError` when the key is absent (B4)."""
        resolved = get_settings() if settings is None else settings
        return cls(
            resolved.anthropic_api_key,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            adaptive_thinking=adaptive_thinking,
            http_client=http_client,
        )

    @property
    def _sdk(self) -> anthropic.Anthropic:
        """The SDK client, built on first use.

        Lazy so that constructing this object — which is what startup wiring does, and what the
        credential check exists for — opens no connection pool and touches no environment beyond
        the key it was handed. Passing `api_key` explicitly is what stops the SDK consulting the
        environment for a credential of its own; `http_client` is the seam a test drives, and it
        carries no credential.
        """
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=self._api_key.get_secret_value(),
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

        thinking = _thinking_param(model, adaptive=self._thinking)
        raw = self._sdk.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[_message_param(message) for message in messages],
            system=anthropic.omit if system is None else system,
            tools=[_tool_param(tool) for tool in tools] if tools else anthropic.omit,
            thinking=anthropic.omit if thinking is None else thinking,
        )

        # Usage is read first and travels on every failure below. The provider billed this call
        # whatever it decided about the answer, so an error that dropped the token counts would
        # book a real charge at ₹0 — X3's own blindness, on the path nobody inspects.
        usage = Usage(
            input_tokens=raw.usage.input_tokens,
            output_tokens=raw.usage.output_tokens,
            cache_write_tokens=raw.usage.cache_creation_input_tokens or 0,
            cache_read_tokens=raw.usage.cache_read_input_tokens or 0,
        )
        stop_reason = _stop_reason(raw.stop_reason, usage=usage)
        if stop_reason is StopReason.REFUSAL:
            category = None if raw.stop_details is None else raw.stop_details.category
            raise LLMRefusalError(
                f"{model} declined the request (category {category!r}); there is no answer to act "
                "on. Do not retry the same prompt — journal the refusal and escalate.",
                usage=usage,
            )

        text: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in raw.content:
            if block.type == "text":
                text.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=_arguments(block.input, usage=usage),
                    )
                )
        return LLMResponse(
            provider=ANTHROPIC_PROVIDER,
            model=raw.model,
            text="".join(text),
            usage=usage,
            stop_reason=stop_reason,
            tool_calls=tuple(tool_calls),
        )


def _thinking_param(model: str, *, adaptive: bool) -> ThinkingConfigParam | None:
    """The `thinking` value for one call, or None to omit the parameter.

    What it does: asks for adaptive thinking on the models that have it, and explicitly disables
    it when the caller said not to think.
    What it assumes: `_ADAPTIVE_THINKING_MODELS` is the accurate list — a model missing from it
    loses thinking rather than erroring, which is the safe direction.
    What it never does: send `adaptive` to a model documented not to take it, or reach for
    omission as the off switch. The second is the subtle one. On Claude Opus 5 and Sonnet 5,
    omitting `thinking` runs adaptive anyway, so `adaptive_thinking=False` implemented by leaving
    the parameter out would be a no-op that reads like a setting; `disabled` has to be said out
    loud. It is not uniform across this list — on Opus 4.8 omitting the parameter does mean no
    thinking — which is exactly why the off switch is explicit rather than inferred per model.

    Both behaviours are documented rather than observed: no credential exists here (B4), and
    nothing in `anthropic` 0.121.0's types distinguishes the models, so neither can be checked
    offline. What *is* checked offline is the request this builds, in
    `tests/unit/test_llm_accounting.py`.
    """
    if model not in _ADAPTIVE_THINKING_MODELS:
        return None
    if adaptive:
        return ThinkingConfigAdaptiveParam(type="adaptive")
    return ThinkingConfigDisabledParam(type="disabled")


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


def _stop_reason(value: str | None, *, usage: Usage) -> StopReason:
    """Map a provider stop reason, refusing to guess at one this module has never seen.

    `usage` is carried onto the error rather than merely reported in it. A stop reason this module
    does not map still describes a call the provider ran and billed — `model_context_window_
    exceeded` is the live example, and it arrives precisely on the calls with the largest prompts —
    so the counts have to survive the raise or that spend is booked at ₹0.
    """
    if value is None:
        raise LLMError(
            "the response carried no stop_reason; a completion whose termination is unknown "
            "cannot be trusted to be complete",
            usage=usage,
        )
    try:
        return _STOP_REASONS[value]
    except KeyError:
        raise LLMError(
            f"unknown stop_reason {value!r} from the provider; treating it as a normal end of "
            "turn could silently accept a partial answer. Add it to _STOP_REASONS once its "
            "meaning is established.",
            usage=usage,
        ) from None


def _arguments(value: object, *, usage: Usage) -> dict[str, object]:
    """A tool call's arguments as a mapping, or a loud failure.

    The SDK types `input` as `object` because a tool schema can in principle be any JSON. Every
    tool this system offers takes an object, so anything else means the model answered a tool
    contract that is not the one we published.

    `usage` is carried for the same reason it is on every other raise reachable from a completed
    response: the model generated this tool call and the provider billed the whole exchange, so
    dropping the counts here would book real spend at ₹0. It is the last raise site in this
    module's happy path, and the easiest one to miss — the failure is in a content block, long
    after the call itself has plainly succeeded.
    """
    if not isinstance(value, dict):
        raise LLMError(
            f"tool call arguments must be an object, got {type(value).__name__}", usage=usage
        )
    return {str(key): item for key, item in value.items()}
