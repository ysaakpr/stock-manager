"""X3: the `LLM` backed by the Claude CLI, so the analyst runs on a subscription, not an API key.

`AnthropicLLM` needs `ANTHROPIC_API_KEY`, which is metered spend. This implementation shells out to
`claude -p` instead, which authenticates against the machine's own Claude login — `claude
setup-token` is the documented way to create a long-lived credential for unattended use. The
practical consequence is that M6.8's live-model fire drill, blocked since M6 on a credential that
does not exist (B4), becomes runnable on a box where someone has logged in once.

**This is a completion, not an agent.** `claude -p` is ordinarily an agent loop that can read files
and run commands. Behind a protocol whose whole purpose is to be the one narrow door between
`analyst/` and a model, that would be a different risk object: the model's inputs would no longer
be the evidence bundle the journal recorded, and a replay could not reproduce a decision because
the model went and looked at the lake itself. So every invocation here pins `--tools ""`, which
empties the built-in tool set outright, plus `--permission-prompts none` and
`--no-session-persistence`. What the model sees is what the caller passed, which is what makes the
evidence pack honest (§5.7).

Two things differ from the API and callers must know both:

- **Cost is reconstructed, not billed.** `total_cost_usd` in the CLI's output is a client-side
  estimate of what the same call would have cost on the API. On a subscription nothing is billed
  per token — the binding constraint is the plan's rate limit. The token counts are real and are
  what `accounting.tokens` prices, exactly as for the stub; the rupee figure is an estimate with a
  provider attached, and `LLMResponse.provider` is what keeps it separable in the burn report.
- **The harness is not free, but `--tools ""` pays most of it back.** A default `claude -p`
  invocation carries the built-in tool definitions: measured at 18k-22k cache-write plus 10k-68k
  cache-read tokens per call, for a two-token question. Emptying the tool set removes them — the
  same question then measured 4,054 prompt tokens with no cache traffic at all. So the flag that
  makes this a completion rather than an agent is also the one that makes it affordable, and the
  residual overhead over a bare API call is small. What is consumed is rate limit, not a card.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any, Final

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
from dataplatform.logging import get_logger

__all__ = ["CLAUDE_CLI_PROVIDER", "STRUCTURED_TOOL_CALL_ID", "ClaudeCliLLM"]

log = get_logger(__name__)

#: What lands in `token_usage.provider` for a call made through the CLI. Deliberately distinct from
#: `anthropic`: the token counts are the same fact, the money is not, and a burn report that
#: conflated subscription calls with billed ones would overstate the spend it exists to track.
CLAUDE_CLI_PROVIDER: Final[str] = "claude_cli"

#: The id given to the single synthetic tool call that carries `--json-schema` output. The CLI
#: returns a bare object with no call id — there is no tool-use protocol here to take one from —
#: and a caller correlating by id needs something stable rather than something invented per call.
STRUCTURED_TOOL_CALL_ID: Final[str] = "structured_output"

#: Wall-clock ceiling for one invocation. Matches `AnthropicLLM`'s: an adaptive-thinking answer on
#: a hard question legitimately takes minutes, and the daily loop is EOD and has the time. The CLI
#: adds process startup and MCP/plugin discovery on top, which is seconds, not minutes.
_TIMEOUT_SECONDS: Final[float] = 600.0

#: The executable. Resolved through `PATH` so a node-version manager's shim works; absence is a
#: credential-class failure, not a runtime one, and is reported at construction.
_EXECUTABLE: Final[str] = "claude"

#: Turns one completion may take. Two, not one, and the reason is measured rather than guessed:
#: `--json-schema` costs a turn of its own, so a schema request under a cap of one comes back
#: `is_error` with `subtype: error_max_turns` and the answer thrown away — which is exactly how
#: this constant was found. With `--tools ""` there is nothing for a second turn to *do* except
#: emit the structured output, so this bounds the loop without constraining the answer.
_MAX_TURNS: Final[int] = 2

#: CLI stop reasons, mapped to the vocabulary `analyst/` uses. The CLI reports the underlying API
#: reason, so this is the same table `AnthropicLLM` keeps, minus the ones a single-turn call with
#: no tools cannot produce.
_STOP_REASONS: Final[dict[str, StopReason]] = {
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.REFUSAL,
    "tool_use": StopReason.TOOL_USE,
    "pause_turn": StopReason.PAUSE_TURN,
}

#: Substrings that mean "this machine is not logged in", checked against a failed run's output.
#: Matched loosely on purpose: the exact wording is the CLI's to change, and the cost of missing
#: one is an operator reading "exit 1" instead of "run claude setup-token".
_AUTH_MARKERS: Final[tuple[str, ...]] = (
    "authentication_failed",
    "not logged in",
    "please run /login",
    "invalid api key",
    "oauth token has expired",
    "credit balance is too low",
)


class ClaudeCliLLM:
    """The `LLM` that runs on a Claude subscription through the CLI.

    What it does: renders the conversation into one prompt, runs `claude -p` as a single-turn
    completion with no tools, and returns the text, any structured output, and the token counts the
    CLI reported.
    What it assumes: someone has logged this machine in (`claude setup-token` or `claude login`);
    the credential is the CLI's to hold and this module never reads, copies or logs it.
    What it never does: let the model use a tool, persist a session, fall back to another provider,
    or return an empty completion as though it were an answer.
    """

    __slots__ = ("_executable", "_timeout_seconds")

    def __init__(
        self,
        *,
        executable: str = _EXECUTABLE,
        timeout_seconds: float = _TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds}")
        if shutil.which(executable) is None:
            raise LLMCredentialError(
                f"the Claude CLI ({executable!r}) is not on PATH, so LLM_PROVIDER=claude_cli has "
                "nothing to run. Install it (`npm install -g @anthropic-ai/claude-code`) and log "
                "in once (`claude setup-token`), or use StubLLM, which needs neither. There is no "
                "fallback — an analyst that silently ran on a stub would look like it was "
                "reasoning and would not be."
            )
        self._executable = executable
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return f"{type(self).__name__}(executable={self._executable!r})"

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        timeout_seconds: float = _TIMEOUT_SECONDS,
    ) -> ClaudeCliLLM:
        """Build from configuration.

        Takes `Settings` for symmetry with `AnthropicLLM.from_settings` and for the day a setting
        belongs here; it reads none today, because the CLI owns the credential and the model is a
        per-call argument. Raises `LLMCredentialError` when the CLI is absent.
        """
        get_settings() if settings is None else settings
        return cls(timeout_seconds=timeout_seconds)

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

        What it does: one `claude -p` invocation, single turn, no tools, JSON out.
        What it assumes: at most one `ToolSpec` is offered. The CLI has no tool-use protocol to
        expose, but it does have `--json-schema`, so one offered tool is translated into a demand
        for structured output and comes back as one `ToolCall` — which is the shape a caller that
        offered a tool already handles. Two tools have no such translation and raise, rather than
        silently dropping the one the caller cared about.
        What it never does: enforce `max_tokens`. The CLI exposes no output ceiling, so the
        argument cannot be honoured; passing a non-default value logs a warning and is otherwise
        ignored, because a silently-capped answer and a silently-uncapped one are both worse than
        a loud one. `stop_reason` still comes from the CLI, so a genuine truncation is visible.
        """
        if not messages:
            raise ValueError("complete() needs at least one message")
        if max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {max_tokens}")
        if len(tools) > 1:
            raise ValueError(
                f"the Claude CLI can express one structured-output schema, not {len(tools)} "
                "tools: it has no tool-use protocol, so a second tool could only be dropped. "
                "Offer one tool, or use LLM_PROVIDER=anthropic where real tool use exists."
            )
        if max_tokens != DEFAULT_MAX_TOKENS:
            log.warning(
                "llm.max_tokens_ignored",
                provider=CLAUDE_CLI_PROVIDER,
                requested=max_tokens,
                reason="the Claude CLI exposes no output ceiling",
            )

        command = self._build_command(model=model, tools=tools, system=system)
        raw = self._run(command, prompt=_render_prompt(messages), model=model)
        return _response(raw, model=model, structured=bool(tools), tool_name=_tool_name(tools))

    def _build_command(
        self,
        *,
        model: str,
        tools: Sequence[ToolSpec],
        system: str | None,
    ) -> list[str]:
        """The argv for one completion.

        Every flag here is load-bearing. `--tools ""` is the one that matters most: it empties the
        built-in tool set, which is what makes this a completion rather than an agent. Denying
        permissions alone would not have done it — Claude Code runs reads inside the working
        directory and its read-only command set without asking anybody, so a model with tools and
        no approver can still go and look at the lake. `--permission-prompts none` then denies
        anything an MCP server might add, `--no-session-persistence` keeps a decision's transcript
        out of a second unjournalled store, and `--output-format json` is what makes the usage
        readable at all. `--system-prompt` *replaces* Claude Code's own prompt rather than
        appending to it, because an analyst asked for a verdict should not also be carrying
        instructions about editing code.
        """
        command = [
            self._executable,
            "--print",
            "--output-format",
            "json",
            "--model",
            model,
            "--tools",
            "",
            "--max-turns",
            str(_MAX_TURNS),
            "--permission-prompts",
            "none",
            "--no-session-persistence",
        ]
        if system is not None:
            command += ["--system-prompt", system]
        if tools:
            command += ["--json-schema", json.dumps(dict(tools[0].input_schema), sort_keys=True)]
        return command

    def _run(self, command: Sequence[str], *, prompt: str, model: str) -> Mapping[str, Any]:
        """Run the CLI and return its parsed JSON, turning every failure mode into an `LLMError`.

        The prompt goes in on stdin rather than as an argv element: a thesis and its evidence
        bundle routinely exceed what a shell will accept as one argument, and an evidence pack
        truncated by `ARG_MAX` would be a decision made on half its evidence.
        """
        try:
            # argv is built in this module and never shell-interpreted; the prompt is stdin.
            finished = subprocess.run(
                list(command),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise LLMError(
                f"the Claude CLI did not answer within {self._timeout_seconds:g}s for {model}"
            ) from error
        except OSError as error:
            raise LLMError(f"could not run the Claude CLI: {error}") from error

        parsed: object = None
        try:
            parsed = json.loads(finished.stdout)
        except json.JSONDecodeError:
            parsed = None

        failed = finished.returncode != 0 or (
            isinstance(parsed, dict) and parsed.get("is_error") is True
        )
        # Only a *failed* run is searched for auth markers. On a successful one the same phrases
        # can legitimately appear in the model's own answer — an analyst asked about a broker
        # integration may well write "invalid api key" — and a false credential error would send
        # an operator to re-authenticate a machine that is authenticated.
        if failed:
            combined = f"{finished.stdout}\n{finished.stderr}".lower()
            if any(marker in combined for marker in _AUTH_MARKERS):
                raise LLMCredentialError(
                    "the Claude CLI is installed but not authenticated (or its token has "
                    "expired). Run `claude setup-token` on this machine and put nothing in the "
                    "repo: the CLI holds the credential itself."
                )
            raise LLMError(
                f"the Claude CLI failed for {model} (exit {finished.returncode}): "
                f"{finished.stderr.strip() or finished.stdout.strip() or '(no output)'}"
            )
        if not isinstance(parsed, dict):
            raise LLMError(
                f"the Claude CLI returned output that is not a JSON object for {model}: "
                f"{finished.stdout[:200]!r}"
            )
        return parsed


def _render_prompt(messages: Sequence[Message]) -> str:
    """The conversation as one prompt.

    `claude -p` takes a single prompt, not a turn list, so a multi-turn exchange has to be
    flattened. A lone user message — which is what T1 and T2 send — passes through untouched, so
    the common case carries no framing the model has to see past. Anything longer is labelled by
    speaker, because an unlabelled concatenation of two voices is a different question.
    """
    if len(messages) == 1 and messages[0].role is Role.USER:
        return messages[0].content
    labels = {Role.USER: "User", Role.ASSISTANT: "Assistant"}
    return "\n\n".join(f"{labels[m.role]}: {m.content}" for m in messages)


def _tool_name(tools: Sequence[ToolSpec]) -> str | None:
    """The offered tool's name, or None when structured output was not asked for."""
    return tools[0].name if tools else None


def _usage(raw: Mapping[str, Any]) -> Usage:
    """The CLI's usage block in this system's four disjoint buckets.

    The names differ from the API's only by prefix, and the mapping is exact — which is the whole
    reason this provider can sit behind the same protocol without X3 losing anything. A missing
    bucket reads as zero: the CLI omits the cache fields on a call that neither wrote nor read the
    prompt cache, and a zero there is the truth rather than an absence.
    """
    block = raw.get("usage")
    if not isinstance(block, dict):
        raise LLMError(
            "the Claude CLI returned no usage block, so this call's cost cannot be metered; X3 "
            "requires a token count on every decision (decision #12), so this is a failure, not a "
            "call worth keeping"
        )
    return Usage(
        input_tokens=_count(block, "input_tokens"),
        output_tokens=_count(block, "output_tokens"),
        cache_write_tokens=_count(block, "cache_creation_input_tokens"),
        cache_read_tokens=_count(block, "cache_read_input_tokens"),
    )


def _count(block: Mapping[str, Any], key: str) -> int:
    """One token bucket, as a whole number. Absent reads as zero; present-but-wrong raises."""
    value = block.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LLMError(f"usage.{key} is {value!r}, which is not a token count")
    return value


def _response(
    raw: Mapping[str, Any],
    *,
    model: str,
    structured: bool,
    tool_name: str | None,
) -> LLMResponse:
    """The CLI's JSON as an `LLMResponse`.

    The model id is taken from the CLI's own report where it gives one, because `--model haiku`
    resolves to a dated id that `accounting/model_prices.yaml` has to price, and the requested
    alias would price at nothing. A `[1m]` context-window suffix is stripped for the same reason:
    it names a variant of a model the price card already knows, not a different model.
    """
    usage = _usage(raw)
    stop_reason = _STOP_REASONS.get(str(raw.get("stop_reason", "end_turn")), StopReason.END_TURN)
    if stop_reason is StopReason.REFUSAL:
        raise LLMRefusalError(
            f"{model} declined the request; there is no answer to act on. Do not retry the same "
            "prompt — journal the refusal and escalate."
        )

    text = raw.get("result")
    text = text if isinstance(text, str) else ""
    tool_calls: tuple[ToolCall, ...] = ()
    if structured:
        payload = raw.get("structured_output")
        if not isinstance(payload, dict):
            raise LLMRefusalError(
                f"{model} was asked for output matching a schema and returned none; a verdict "
                "that did not parse is not a verdict. Journal the failure and escalate."
            )
        tool_calls = (
            ToolCall(
                id=STRUCTURED_TOOL_CALL_ID,
                name=tool_name or STRUCTURED_TOOL_CALL_ID,
                arguments=dict(payload),
            ),
        )
        stop_reason = StopReason.TOOL_USE
    elif not text.strip():
        raise LLMRefusalError(
            f"{model} returned an empty completion; there is no answer to act on. Journal it and "
            "escalate rather than treating silence as agreement."
        )

    return LLMResponse(
        provider=CLAUDE_CLI_PROVIDER,
        model=_reported_model(raw, requested=model),
        text=text,
        usage=usage,
        stop_reason=stop_reason,
        tool_calls=tool_calls,
    )


def _reported_model(raw: Mapping[str, Any], *, requested: str) -> str:
    """The model that actually answered, price-card-ready, falling back to what was asked for."""
    usage_by_model = raw.get("modelUsage")
    if isinstance(usage_by_model, dict) and usage_by_model:
        reported = next(iter(usage_by_model))
        if isinstance(reported, str) and reported.strip():
            return reported.split("[", 1)[0].strip()
    return requested
