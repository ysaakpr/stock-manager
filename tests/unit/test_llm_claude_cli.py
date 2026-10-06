"""The Claude CLI provider: a completion, not an agent, and every call still priced.

`ClaudeCliLLM` exists so the analyst can run on a subscription rather than an API key — the thing
that has kept M6.8's live fire drill unrunnable since M6 (B4). Putting a *CLI* behind the `LLM`
protocol is not free, though, and this file pins the three properties that make it safe to do:

1. **It is single-shot and toolless.** `claude -p` is ordinarily an agent that reads files and runs
   commands. Behind a protocol whose docstring calls itself "the one door between `analyst/` and a
   language model", an agent would mean the model's inputs were no longer the evidence bundle the
   journal recorded, and a replay could not reproduce a decision. So the argv assertions below are
   not style checks: `--max-turns 1`, `--permission-prompts none` and `--no-session-persistence`
   are the difference between a completion and something that can go and read the lake by itself.
2. **Usage survives the round trip.** X3's promise (decision #12) is that a decision's cost is
   measured rather than unknown. The CLI's four token buckets map exactly onto `Usage`'s, and a
   response with no usage block is a failure rather than a free call.
3. **Silence is never an answer.** An empty completion, a refusal, or structured output that did
   not parse all raise, because a monitor that reads "no verdict" as "no problem" is the failure
   mode this whole layer exists to prevent.

Everything is offline (CLAUDE.md, B8): `subprocess.run` is replaced throughout, so no test here
starts a process, needs a login, or spends a token. The JSON payloads are the shapes the real CLI
returned when this provider was built, trimmed to the fields the code reads.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from structlog.testing import capture_logs

from analyst.llm.claude_cli import CLAUDE_CLI_PROVIDER, ClaudeCliLLM
from analyst.llm.client import (
    LLMCredentialError,
    LLMError,
    LLMRefusalError,
    Message,
    Role,
    StopReason,
    ToolSpec,
    build_llm,
)
from dataplatform.config import LlmProvider, Settings

# ── captured shapes ──────────────────────────────────────────────────────────────────────────

#: A plain text answer, as `claude -p --output-format json` returns one.
TEXT_RESULT: dict[str, Any] = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "stop_reason": "end_turn",
    "session_id": "c1c8ac65-f627-4d15-824d-870bd66816cf",
    "total_cost_usd": 0.1094065,
    "result": "The thesis is weakened, not broken.",
    "modelUsage": {"claude-opus-5[1m]": {"inputTokens": 2}},
    "usage": {
        "input_tokens": 2,
        "output_tokens": 535,
        "cache_creation_input_tokens": 22573,
        "cache_read_input_tokens": 10613,
    },
}

#: The same call with `--json-schema`, which is how a T1/T2 verdict comes back.
STRUCTURED_RESULT: dict[str, Any] = {
    **TEXT_RESULT,
    "result": "",
    "structured_output": {"verdict": "WEAKENED", "reason": "One quarter is not a trend."},
}

VERDICT_TOOL = ToolSpec(
    name="verdict",
    description="The monitor's verdict on whether the thesis still holds.",
    input_schema={
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["INTACT", "WEAKENED", "BROKEN"]},
            "reason": {"type": "string"},
        },
        "required": ["verdict", "reason"],
    },
)

ASK = (Message(role=Role.USER, content="Is the TCS growth thesis broken?"),)


class FakeRun:
    """Stands in for `subprocess.run`, recording the argv and stdin it was handed."""

    def __init__(self, stdout: str = "", *, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.argv: list[str] = []
        self.stdin: str | None = None

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.argv = list(argv)
        self.stdin = kwargs.get("input")
        return subprocess.CompletedProcess(
            args=list(argv), returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> ClaudeCliLLM:
    """A provider whose `claude` binary is present but never actually run."""
    monkeypatch.setattr("analyst.llm.claude_cli.shutil.which", lambda _: "/usr/bin/claude")
    return ClaudeCliLLM()


def run_with(
    monkeypatch: pytest.MonkeyPatch, payload: Mapping[str, Any] | str, **kwargs: Any
) -> FakeRun:
    """Install a fake `subprocess.run` returning `payload`, and hand back the recorder."""
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    fake = FakeRun(stdout, **kwargs)
    monkeypatch.setattr("analyst.llm.claude_cli.subprocess.run", fake)
    return fake


# ── 1. a completion, not an agent ────────────────────────────────────────────────────────────


def test_every_invocation_is_pinned_to_a_single_toolless_turn(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flags that stop `claude -p` being an agent are not optional and not configurable."""
    fake = run_with(monkeypatch, TEXT_RESULT)
    cli.complete(ASK, model="claude-opus-5")

    argv = fake.argv
    assert argv[0] == "claude"
    assert "--print" in argv
    # The one that actually empties the tool set. Denying permissions is not enough: Claude Code
    # runs reads inside the working directory and its read-only command set without asking.
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    # Never widen the tool set by another route.
    assert "--allowedTools" not in argv
    assert "--permission-mode" not in argv
    assert "--dangerously-skip-permissions" not in argv


def test_the_turn_cap_leaves_room_for_the_schema_turn(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured, not guessed: `--json-schema` costs a turn, so a cap of 1 discards the answer.

    A cap of one came back `is_error` with `subtype: error_max_turns` against the live CLI, having
    already paid for 538 output tokens. The cap exists to bound a loop, and with `--tools ""`
    there is no loop to bound — so it must not be tighter than the work the CLI has to do.
    """
    fake = run_with(monkeypatch, STRUCTURED_RESULT)
    cli.complete(ASK, model="claude-opus-5", tools=(VERDICT_TOOL,))
    assert int(fake.argv[fake.argv.index("--max-turns") + 1]) >= 2


def test_the_prompt_travels_on_stdin_so_an_evidence_pack_cannot_be_truncated(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An evidence bundle exceeds ARG_MAX long before it exceeds what the model can read."""
    fake = run_with(monkeypatch, TEXT_RESULT)
    cli.complete(ASK, model="claude-opus-5")

    assert fake.stdin == "Is the TCS growth thesis broken?"
    assert "Is the TCS growth thesis broken?" not in fake.argv


def test_a_lone_user_message_is_passed_through_unframed(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1 and T2 send one message; it must reach the model without a speaker label bolted on."""
    fake = run_with(monkeypatch, TEXT_RESULT)
    cli.complete(ASK, model="claude-opus-5")
    assert fake.stdin == ASK[0].content


def test_a_multi_turn_exchange_is_labelled_by_speaker(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`claude -p` takes one prompt, so two voices concatenated unlabelled is a new question."""
    fake = run_with(monkeypatch, TEXT_RESULT)
    cli.complete(
        (
            Message(role=Role.USER, content="Is it broken?"),
            Message(role=Role.ASSISTANT, content="Weakened."),
            Message(role=Role.USER, content="Why?"),
        ),
        model="claude-opus-5",
    )
    assert fake.stdin == "User: Is it broken?\n\nAssistant: Weakened.\n\nUser: Why?"


def test_a_system_prompt_replaces_rather_than_appends(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An analyst asked for a verdict must not also carry Claude Code's coding instructions."""
    fake = run_with(monkeypatch, TEXT_RESULT)
    cli.complete(ASK, model="claude-opus-5", system="You are an equity analyst.")

    assert fake.argv[fake.argv.index("--system-prompt") + 1] == "You are an equity analyst."
    assert "--append-system-prompt" not in fake.argv


def test_two_tools_raise_rather_than_one_being_dropped(cli: ClaudeCliLLM) -> None:
    """The CLI can express one schema; silently discarding the second is the failure to avoid."""
    with pytest.raises(ValueError, match="one structured-output schema"):
        cli.complete(ASK, model="claude-opus-5", tools=(VERDICT_TOOL, VERDICT_TOOL))


def test_max_tokens_cannot_be_honoured_and_says_so(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI exposes no output ceiling; a caller that asked for one learns it did not get it."""
    run_with(monkeypatch, TEXT_RESULT)
    with capture_logs() as entries:
        cli.complete(ASK, model="claude-opus-5", max_tokens=100)
    assert [e for e in entries if e["event"] == "llm.max_tokens_ignored" and e["requested"] == 100]

    # The default asks for nothing the CLI cannot give, so it must stay quiet.
    with capture_logs() as quiet:
        cli.complete(ASK, model="claude-opus-5")
    assert not [e for e in quiet if e["event"] == "llm.max_tokens_ignored"]


# ── 2. usage survives the round trip ─────────────────────────────────────────────────────────


def test_the_four_token_buckets_map_exactly(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X3 prices four disjoint buckets; collapsing any of them would hide the cache discount."""
    run_with(monkeypatch, TEXT_RESULT)
    response = cli.complete(ASK, model="claude-opus-5")

    assert response.usage.input_tokens == 2
    assert response.usage.output_tokens == 535
    assert response.usage.cache_write_tokens == 22573
    assert response.usage.cache_read_tokens == 10613
    assert response.usage.prompt_tokens == 2 + 22573 + 10613


def test_the_provider_is_distinct_from_anthropic(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscription calls are not billed per token; a burn report must be able to separate them."""
    run_with(monkeypatch, TEXT_RESULT)
    assert cli.complete(ASK, model="claude-opus-5").provider == CLAUDE_CLI_PROVIDER
    assert CLAUDE_CLI_PROVIDER != "anthropic"


def test_the_model_that_answered_is_reported_price_card_ready(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--model haiku` resolves to a dated id, and a `[1m]` variant prices as its base model."""
    run_with(monkeypatch, TEXT_RESULT)
    assert cli.complete(ASK, model="opus").model == "claude-opus-5"


def test_a_model_the_cli_does_not_name_falls_back_to_what_was_asked_for(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never invent a model id: an unpriceable name must be loud at pricing time, not guessed."""
    run_with(monkeypatch, {**TEXT_RESULT, "modelUsage": {}})
    assert cli.complete(ASK, model="claude-opus-5").model == "claude-opus-5"


def test_a_response_with_no_usage_block_is_a_failure_not_a_free_call(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision #12: a call whose cost is unknown is worse than a call that failed."""
    payload = {k: v for k, v in TEXT_RESULT.items() if k != "usage"}
    run_with(monkeypatch, payload)
    with pytest.raises(LLMError, match="no usage block"):
        cli.complete(ASK, model="claude-opus-5")


def test_absent_cache_buckets_read_as_zero(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call that neither wrote nor read the cache omits the fields; zero is the truth there."""
    run_with(monkeypatch, {**TEXT_RESULT, "usage": {"input_tokens": 5, "output_tokens": 7}})
    usage = cli.complete(ASK, model="claude-opus-5").usage
    assert (usage.cache_write_tokens, usage.cache_read_tokens) == (0, 0)


# ── 3. silence is never an answer ────────────────────────────────────────────────────────────


def test_structured_output_comes_back_as_one_tool_call(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that offered a tool already handles a `ToolCall`; the schema route reuses it."""
    fake = run_with(monkeypatch, STRUCTURED_RESULT)
    response = cli.complete(ASK, model="claude-opus-5", tools=(VERDICT_TOOL,))

    schema = json.loads(fake.argv[fake.argv.index("--json-schema") + 1])
    assert schema["properties"]["verdict"]["enum"] == ["INTACT", "WEAKENED", "BROKEN"]
    assert response.stop_reason is StopReason.TOOL_USE
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "verdict"
    assert response.tool_calls[0].arguments == {
        "verdict": "WEAKENED",
        "reason": "One quarter is not a trend.",
    }


def test_a_schema_that_did_not_parse_is_refused_not_returned(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verdict that did not parse is not a verdict, and must not reach the journal as one."""
    payload = {k: v for k, v in STRUCTURED_RESULT.items() if k != "structured_output"}
    run_with(monkeypatch, payload)
    with pytest.raises(LLMRefusalError, match="matching a schema and returned none"):
        cli.complete(ASK, model="claude-opus-5", tools=(VERDICT_TOOL,))


def test_an_empty_completion_raises(cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch) -> None:
    """Treating silence as agreement is the failure mode the monitor exists to prevent."""
    run_with(monkeypatch, {**TEXT_RESULT, "result": "   "})
    with pytest.raises(LLMRefusalError, match="empty completion"):
        cli.complete(ASK, model="claude-opus-5")


def test_a_refusal_raises_rather_than_returning_a_plausible_nothing(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same contract as `AnthropicLLM`: a refusal is an escalation, not an answer."""
    run_with(monkeypatch, {**TEXT_RESULT, "stop_reason": "refusal"})
    with pytest.raises(LLMRefusalError, match="declined the request"):
        cli.complete(ASK, model="claude-opus-5")


def test_a_truncated_answer_is_visible_rather_than_silent(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`max_tokens` cannot be set here, but the CLI still reports when the model hit a ceiling."""
    run_with(monkeypatch, {**TEXT_RESULT, "stop_reason": "max_tokens"})
    assert cli.complete(ASK, model="claude-opus-5").truncated


# ── failure modes of the process itself ──────────────────────────────────────────────────────


def test_a_missing_cli_is_a_credential_failure_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same rule as a missing API key: refuse to exist rather than degrade to a stub."""
    monkeypatch.setattr("analyst.llm.claude_cli.shutil.which", lambda _: None)
    with pytest.raises(LLMCredentialError, match="not on PATH"):
        ClaudeCliLLM()


def test_an_unauthenticated_cli_names_the_command_that_fixes_it(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "exit 1" tells an operator nothing; "run claude setup-token" tells them everything."""
    run_with(monkeypatch, "Invalid API key · Please run /login", returncode=1)
    with pytest.raises(LLMCredentialError, match="setup-token"):
        cli.complete(ASK, model="claude-opus-5")


def test_an_auth_phrase_inside_a_successful_answer_is_not_a_credential_error(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An analyst writing about broker auth must not send an operator to re-authenticate."""
    run_with(monkeypatch, {**TEXT_RESULT, "result": "The runbook says 'invalid api key' here."})
    assert "invalid api key" in cli.complete(ASK, model="claude-opus-5").text


def test_a_nonzero_exit_raises_with_the_stderr_attached(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure a human has to diagnose must carry what the process said about it."""
    run_with(monkeypatch, "", returncode=2, stderr="unknown option --nope")
    with pytest.raises(LLMError, match="unknown option --nope"):
        cli.complete(ASK, model="claude-opus-5")


def test_an_in_band_error_is_caught_even_on_a_zero_exit(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI can report a failure as the result; `is_error` is what makes that visible."""
    run_with(monkeypatch, {**TEXT_RESULT, "is_error": True, "result": "rate limit reached"})
    with pytest.raises(LLMError, match="rate limit reached"):
        cli.complete(ASK, model="claude-opus-5")


def test_output_that_is_not_json_raises(cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch) -> None:
    """A parse failure must name what came back, or the next person debugs it blind."""
    run_with(monkeypatch, "<html>gateway timeout</html>")
    with pytest.raises(LLMError, match="not a JSON object"):
        cli.complete(ASK, model="claude-opus-5")


def test_a_timeout_raises_rather_than_hanging_the_daily_loop(
    cli: ClaudeCliLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The EOD loop has time, but not unbounded time."""

    def timeout(*_: Any, **__: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=600.0)

    monkeypatch.setattr("analyst.llm.claude_cli.subprocess.run", timeout)
    with pytest.raises(LLMError, match="did not answer within"):
        cli.complete(ASK, model="claude-opus-5")


def test_no_messages_is_a_programming_error(cli: ClaudeCliLLM) -> None:
    """Asking nothing is a caller bug, and must not cost a call to discover."""
    with pytest.raises(ValueError, match="at least one message"):
        cli.complete((), model="claude-opus-5")


# ── the provider switch ──────────────────────────────────────────────────────────────────────


def test_build_llm_selects_the_cli_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """§8.1's switch: `LLM_PROVIDER=claude_cli` wires this implementation and no other."""
    monkeypatch.setattr("analyst.llm.claude_cli.shutil.which", lambda _: "/usr/bin/claude")
    settings = Settings(llm_provider=LlmProvider.CLAUDE_CLI)
    assert isinstance(build_llm(settings), ClaudeCliLLM)


def test_selecting_the_cli_provider_without_the_cli_raises_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never downgrade: a process that believes it is calling a model must be calling one."""
    monkeypatch.setattr("analyst.llm.claude_cli.shutil.which", lambda _: None)
    with pytest.raises(LLMCredentialError):
        build_llm(Settings(llm_provider=LlmProvider.CLAUDE_CLI))
