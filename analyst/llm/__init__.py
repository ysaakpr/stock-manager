"""X3: the analyst's language-model client — one protocol, several implementations.

Every A-module that asks a model anything imports from here and nothing deeper. The protocol is
`LLM`; `StubLLM` satisfies it without a credential and deterministically (B4), `AnthropicLLM`
satisfies it against the real API and refuses to exist until a key is configured, `ClaudeCliLLM`
runs on the Claude CLI's subscription and `BedrockLLM` on Claude in Amazon Bedrock. `build_llm`
picks between them from `LLM_PROVIDER` (or a caller's own selector), and never downgrades one to
another silently: the one sanctioned substitution is `FallbackLLM`, a configured backup asked per
call when the primary could not serve it, logged and recorded every time.

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
from analyst.llm.bedrock import (
    BEDROCK_PROVIDER,
    BedrockLLM,
    ToolExecutionError,
    ToolExecutor,
    ToolLoopExhaustedError,
    ToolLoopResult,
    ToolOutcome,
)
from analyst.llm.cli_env import CLAUDE_CLI_ENV_ALLOWLIST, claude_cli_env
from analyst.llm.cli_launch import (
    CLI_LAUNCH_RETRY_DELAYS_SECONDS,
    is_cli_launch_gap,
    run_claude_cli,
)
from analyst.llm.client import (
    DEFAULT_MAX_TOKENS,
    LLM,
    Fallback,
    LLMCredentialError,
    LLMError,
    LLMRateLimitError,
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
    build_llm,
    prompt_digest,
)
from analyst.llm.effort import EffortPolicy, effort_for, load_effort_policy
from analyst.llm.fallback import FallbackLLM, UnconfiguredLLM
from analyst.llm.stub import (
    STUB_PROVIDER,
    StubCall,
    StubLLM,
    StubReply,
    UnknownPromptError,
)

__all__ = [
    "ANTHROPIC_PROVIDER",
    "BEDROCK_PROVIDER",
    "CLAUDE_CLI_ENV_ALLOWLIST",
    "CLI_LAUNCH_RETRY_DELAYS_SECONDS",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "LLM",
    "STUB_PROVIDER",
    "TRIAGE_MODEL",
    "AnthropicLLM",
    "BedrockLLM",
    "EffortPolicy",
    "Fallback",
    "FallbackLLM",
    "LLMCredentialError",
    "LLMError",
    "LLMRateLimitError",
    "LLMRefusalError",
    "LLMResponse",
    "LLMUnavailableCredentialError",
    "LLMUnavailableError",
    "LLMUnavailableRateLimitError",
    "Message",
    "Role",
    "StopReason",
    "StubCall",
    "StubLLM",
    "StubReply",
    "ToolCall",
    "ToolExecutionError",
    "ToolExecutor",
    "ToolLoopExhaustedError",
    "ToolLoopResult",
    "ToolOutcome",
    "ToolSpec",
    "UnavailableReason",
    "UnconfiguredLLM",
    "UnknownPromptError",
    "Usage",
    "build_llm",
    "claude_cli_env",
    "effort_for",
    "is_cli_launch_gap",
    "load_effort_policy",
    "prompt_digest",
    "run_claude_cli",
]
