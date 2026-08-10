"""X3: the credential-free `LLM` — deterministic answers keyed by prompt hash (B4).

No Anthropic key exists, so this is the implementation the whole analyst is built and tested
against. It has to be a real member of the `LLM` protocol rather than a mock, because the point of
B4 is that the day a key appears nothing above this line changes.

Determinism is the property that matters. The same question — same model, same system prompt, same
turns, same tools — always produces the same answer and the same token counts, which is what makes
a replayed agent run byte-reproducible (§8.3.3, invariant #11) and what lets a test assert on an
LLM-driven decision at all.

Two ways to get an answer out of it:

* **Registered.** Hand it `{prompt_digest: reply}` and it returns exactly that. This is what a test
  that cares about the content of the answer does.
* **Synthesized.** For a prompt it has never seen it derives a reply from the digest instead of
  raising. That is a deliberate choice: a stub that refuses every unregistered prompt makes the
  analyst untestable during exactly the work that changes prompts most — and a synthesized answer
  can never be mistaken for a model's judgement, because it is prefixed with `STUB[<digest>]` and
  carries `provider="stub"` all the way into `token_usage`. Construct with
  `synthesize_unknown=False` when a test wants an unregistered prompt to be a loud failure.

The token counts are estimates, not measurements — a stub cannot know a provider's tokenizer. They
are deterministic and roughly proportional to the text, which is all the accounting layer needs in
order to be exercised end to end; the real numbers arrive with the real client.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from analyst.journal import canonical_bytes
from analyst.llm.client import (
    DEFAULT_MAX_TOKENS,
    LLMError,
    LLMResponse,
    Message,
    StopReason,
    ToolCall,
    ToolSpec,
    Usage,
    prompt_digest,
)

__all__ = ["STUB_PROVIDER", "StubCall", "StubLLM", "StubReply", "UnknownPromptError"]

#: What lands in `token_usage.provider` for every stub call. The one field that separates paper
#: spend from real spend after the fact.
STUB_PROVIDER: Final[str] = "stub"

#: Characters per token in the stub's estimate. Roughly right for English prose, and — much more
#: importantly — fixed, so the same prompt always costs the same.
_CHARS_PER_TOKEN: Final[int] = 4


class UnknownPromptError(LLMError):
    """A strict `StubLLM` was asked a prompt it has no canned answer for.

    Carries the digest, because registering the answer is the fix and the digest is the key.
    """


@dataclass(frozen=True, slots=True)
class StubReply:
    """A canned answer, and optionally the exact usage it should report.

    `usage` is left None by most callers: the estimate derived from the prompt and the text is
    deterministic and good enough. A test that asserts on a specific rupee cost pins it here so the
    expected number does not move when the prompt is reworded.
    """

    text: str
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    stop_reason: StopReason = StopReason.END_TURN
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class StubCall:
    """One call the stub answered — what was asked, and under which model."""

    digest: str
    model: str


def _estimate_tokens(text: str) -> int:
    """A stable token estimate for a string. Never zero: every call costs something."""
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


class StubLLM:
    """An `LLM` that needs no credential and answers the same way every time.

    What it does: hashes the request (`prompt_digest`), returns the registered reply for that
    digest, or — unless constructed strict — a reply synthesized from the digest itself. Every
    response carries a `Usage`, always.
    What it assumes: nothing. It reads no clock, no file, no socket, and holds no state beyond the
    replies it was given and the log of calls it answered.
    What it never does: vary. Two identical requests produce byte-identical responses, in this
    process and in any other, which is the property replay depends on.
    """

    __slots__ = ("_calls", "_replies", "_synthesize")

    def __init__(
        self,
        replies: Mapping[str, StubReply | str] | None = None,
        *,
        synthesize_unknown: bool = True,
    ) -> None:
        self._replies: dict[str, StubReply] = {
            digest: StubReply(text=reply) if isinstance(reply, str) else reply
            for digest, reply in (replies or {}).items()
        }
        self._synthesize = synthesize_unknown
        self._calls: list[StubCall] = []

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(replies={len(self._replies)}, "
            f"synthesize_unknown={self._synthesize})"
        )

    @property
    def calls(self) -> tuple[StubCall, ...]:
        """Every call answered so far, in order. What a test asserts the agent asked."""
        return tuple(self._calls)

    def register(self, digest: str, reply: StubReply | str) -> None:
        """Add or replace the canned answer for one prompt digest."""
        self._replies[digest] = StubReply(text=reply) if isinstance(reply, str) else reply

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        """Answer a conversation deterministically. See the class docstring for the two modes."""
        if max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {max_tokens}")
        digest = prompt_digest(messages, model=model, tools=tools, system=system)
        reply = self._replies.get(digest)
        if reply is None:
            if not self._synthesize:
                raise UnknownPromptError(
                    f"StubLLM has no canned reply for prompt {digest} (model {model!r}); "
                    "register one with StubLLM.register(digest, reply), or construct the stub "
                    "with synthesize_unknown=True to have it derive an answer from the digest"
                )
            reply = self._synthesized(digest)

        prompt_bytes = canonical_bytes(
            {
                "model": model,
                "system": system,
                "messages": [message.as_dict() for message in messages],
                "tools": [tool.as_dict() for tool in tools],
            }
        )
        usage = reply.usage or Usage(
            input_tokens=_estimate_tokens(prompt_bytes.decode("utf-8")),
            output_tokens=_estimate_tokens(reply.text),
        )
        self._calls.append(StubCall(digest=digest, model=model))
        return LLMResponse(
            provider=STUB_PROVIDER,
            model=model,
            text=reply.text,
            usage=usage,
            stop_reason=reply.stop_reason,
            tool_calls=reply.tool_calls,
        )

    @staticmethod
    def _synthesized(digest: str) -> StubReply:
        """The answer for a prompt nobody registered — visibly a stub, and stable forever.

        The `STUB[...]` prefix is load-bearing: it is what stops a synthesized sentence from being
        read as a model's reasoning in a journal entry six months later.
        """
        return StubReply(text=f"STUB[{digest[:12]}] no canned reply registered for this prompt")
