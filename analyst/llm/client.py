"""X3: the one door between `analyst/` and a language model.

Every A-module that asks a model a question goes through the `LLM` protocol here. Two
implementations satisfy it — `StubLLM`, which needs no credential and answers deterministically
from a prompt hash, and `AnthropicLLM`, which calls the real API — and B4 is the reason both
exist: no Anthropic key exists yet, so the agent has to be buildable, testable and replayable
against the stub while the live implementation sits behind the same interface, unchanged on the
day a key appears.

What crosses this boundary is deliberately narrow. A caller passes messages, names a model, and
optionally offers tools; it gets back text, any tool calls, and — always — a `Usage`. Usage is not
optional and has no default: X3's whole job is that a decision's cost is measured rather than
unknown (decision #12), and a response that could omit its token counts would make that a
best-effort property.

What this module does *not* do is price anything. `Usage` is raw counts as the provider reported
them; turning counts into rupees is `accounting.tokens`' job, because the rate card is dated and
the FX rate is configuration, while a token count is a fact about a call that already happened.

Nothing here reads a clock or a database. `complete` is a function of its arguments, which is what
lets `StubLLM` be a deterministic replay of the same conversation (§8.3.3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol, assert_never, runtime_checkable

from analyst.journal import canonical_bytes, digest_of
from dataplatform.config import LlmProvider, Settings, get_settings

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "LLM",
    "LLMCredentialError",
    "LLMError",
    "LLMRefusalError",
    "LLMResponse",
    "Message",
    "Role",
    "StopReason",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "build_llm",
    "prompt_digest",
]

#: Output ceiling used when a caller does not name one. Generous on purpose: a truncated analysis
#: is a decision made on half an argument, and the failure is silent in the text and only visible
#: in `stop_reason`.
DEFAULT_MAX_TOKENS: Final[int] = 16_000


class LLMError(Exception):
    """Base for every failure of this package, so callers can catch the module."""


class LLMCredentialError(LLMError):
    """A provider was selected but the credential it needs does not exist (B4).

    Names the environment key to set, because the caller is a daily-loop module and "auth failed"
    tells an operator nothing they can act on. Raised at construction, not at the first call.
    """


class LLMRefusalError(LLMError):
    """The provider declined the request; there is no answer to read.

    A refusal arrives as a successful HTTP response whose content is empty or partial, so a caller
    that reads `content[0]` gets a plausible-looking nothing. This package raises instead: a
    decision made on a refusal is a decision made on no evidence.
    """


class Role(StrEnum):
    """Who spoke. Deliberately two values — a system prompt is a separate argument, not a turn."""

    USER = "user"
    ASSISTANT = "assistant"


class StopReason(StrEnum):
    """Why generation stopped. Kept because `END_TURN` and `MAX_TOKENS` read identically in text."""

    END_TURN = "end_turn"
    """The model finished. The only reason a caller may treat the text as complete."""

    MAX_TOKENS = "max_tokens"
    """The output ceiling was hit; the text is truncated mid-thought."""

    TOOL_USE = "tool_use"
    """The model wants a tool result before it continues."""

    STOP_SEQUENCE = "stop_sequence"
    """A caller-supplied stop sequence matched."""

    REFUSAL = "refusal"
    """The provider declined. Surfaced as `LLMRefusalError` rather than returned."""

    PAUSE_TURN = "pause_turn"
    """A long server-side tool turn paused and can be resumed by re-sending the exchange."""


@dataclass(frozen=True, slots=True)
class Message:
    """One conversational turn. Text only — this system asks questions, it does not send images."""

    role: Role
    content: str

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError(f"a {self.role.value} message must not be blank")

    def as_dict(self) -> dict[str, str]:
        """The wire shape, and the shape the prompt digest is computed over."""
        return {"role": self.role.value, "content": self.content}


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool offered to the model: what it is called, what it does, what it takes.

    `input_schema` is a JSON Schema object. It is carried as an opaque mapping because the
    provider validates it and this module has no opinion about it beyond it being serializable —
    which the prompt digest enforces, since an unserializable schema cannot be hashed.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a tool needs a name")
        if not self.description.strip():
            raise ValueError(
                f"tool {self.name!r} needs a description: it is what the model reads to decide "
                "whether to call it, and an undescribed tool is one that never gets used"
            )

    def as_dict(self) -> dict[str, Any]:
        """The wire shape, and the shape the prompt digest is computed over."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool the model asked to have run, with the arguments it chose."""

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Usage:
    """What one call consumed, in tokens, exactly as the provider counted it.

    The four buckets are disjoint and priced differently: `input_tokens` are prompt tokens
    processed at full rate, `cache_write_tokens` were written to the provider's prompt cache at a
    premium, `cache_read_tokens` were served from it at a discount, and `output_tokens` are what
    the model generated. Collapsing them here would make the discount invisible in the burn
    report, which is the one number that tells an operator whether caching is working at all.
    """

    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be a whole number of tokens, got {value!r}")
            if value < 0:
                raise ValueError(f"{name} must not be negative, got {value}")

    @property
    def prompt_tokens(self) -> int:
        """Every prompt token the provider billed, cached and uncached alike."""
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens

    @property
    def total_tokens(self) -> int:
        """Prompt plus completion — the volume of the call."""
        return self.prompt_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """One completed call: what came back, and what it consumed.

    `provider` travels with the response rather than being assumed by the caller because a stub
    call and a real call are priced by the same card (`accounting.tokens`) and are only
    distinguishable afterwards by this field. A burn report that cannot separate paper from real
    spend is not a burn report.
    """

    provider: str
    model: str
    text: str
    usage: Usage
    stop_reason: StopReason = StopReason.END_TURN
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("a response must name the provider that produced it")
        if not self.model.strip():
            raise ValueError("a response must name the model that produced it")

    @property
    def truncated(self) -> bool:
        """True when the output ceiling cut the answer short, so the text is half an argument."""
        return self.stop_reason is StopReason.MAX_TOKENS


@runtime_checkable
class LLM(Protocol):
    """Everything `analyst/` is allowed to know about a language model.

    One method. A caller cannot reach a provider's specifics through it, which is what makes the
    stub and the real client substitutable (B4) and what keeps the daily loop's code path the same
    in paper and in production (invariant #5).
    """

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        """Answer a conversation, optionally offering tools, and report what it cost in tokens."""


def build_llm(settings: Settings | None = None) -> LLM:
    """The `LLM` this configuration selects (§8.1's provider switch).

    What it does: reads `LLM_PROVIDER` and returns the matching implementation.
    What it assumes: `STUB` is the default, because no credential exists (B4).
    What it never does: downgrade. Selecting `anthropic` without a key raises here, at startup,
    rather than producing a process that believes it is calling a model and is not.

    The implementations are imported inside the function on purpose: both import this module for
    the protocol and the value types, so importing them at the top would be a cycle.
    """
    from analyst.llm.anthropic import AnthropicLLM
    from analyst.llm.stub import StubLLM

    resolved = get_settings() if settings is None else settings
    match resolved.llm_provider:
        case LlmProvider.STUB:
            return StubLLM()
        case LlmProvider.ANTHROPIC:
            return AnthropicLLM.from_settings(resolved)
        case _:  # pragma: no cover — exhaustive over LlmProvider
            assert_never(resolved.llm_provider)


def prompt_digest(
    messages: Sequence[Message],
    *,
    model: str,
    tools: Sequence[ToolSpec] = (),
    system: str | None = None,
) -> str:
    """The sha256 that identifies exactly this request, lowercase hex.

    What it does: canonicalizes everything that decides what the model would answer — the model
    id, the system prompt, the turns, and the tools offered — and hashes it. Two calls with the
    same digest are the same question, so `StubLLM` can key a canned answer on it and a replay can
    prove it asked what it asked (§8.3.3).
    What it assumes: `max_tokens` does not change the answer, only how much of it arrives, so it is
    deliberately outside the digest. Tools are hashed in the order they were offered, because that
    order is part of the prompt the provider renders.
    What it never does: hash anything a caller cannot see — no timestamps, no request ids, nothing
    that would make the same question hash differently twice.
    """
    document = {
        "model": model,
        "system": system,
        "messages": [message.as_dict() for message in messages],
        "tools": [tool.as_dict() for tool in tools],
    }
    return digest_of(canonical_bytes(document))
