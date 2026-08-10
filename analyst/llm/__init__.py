"""X3: the analyst's language-model client — one protocol, two implementations.

Every A-module that asks a model anything imports from here and nothing deeper. The protocol is
`LLM`; `StubLLM` satisfies it without a credential and deterministically (B4), `AnthropicLLM`
satisfies it against the real API and refuses to exist until a key is configured. `build_llm`
picks between them from `LLM_PROVIDER`, and never downgrades one to the other.

What comes back is always an `LLMResponse` carrying a `Usage`. Pricing that usage — turning tokens
into `cost_inr` on a dated card, writing the `token_usage` row and the journal line's token fields
— is `accounting.tokens`' job, which wraps an `LLM` from here in a `MeteredLLM`.
"""

from analyst.llm.anthropic import (
    ANTHROPIC_PROVIDER,
    DEFAULT_MODEL,
    TRIAGE_MODEL,
    AnthropicLLM,
)
from analyst.llm.client import (
    DEFAULT_MAX_TOKENS,
    LLM,
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
    build_llm,
    prompt_digest,
)
from analyst.llm.stub import (
    STUB_PROVIDER,
    StubCall,
    StubLLM,
    StubReply,
    UnknownPromptError,
)

__all__ = [
    "ANTHROPIC_PROVIDER",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "LLM",
    "STUB_PROVIDER",
    "TRIAGE_MODEL",
    "AnthropicLLM",
    "LLMCredentialError",
    "LLMError",
    "LLMRefusalError",
    "LLMResponse",
    "Message",
    "Role",
    "StopReason",
    "StubCall",
    "StubLLM",
    "StubReply",
    "ToolCall",
    "ToolSpec",
    "UnknownPromptError",
    "Usage",
    "build_llm",
    "prompt_digest",
]
