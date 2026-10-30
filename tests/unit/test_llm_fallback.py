"""`FallbackLLM`: Bedrock primary, Claude CLI backup, per call — only when Bedrock could not serve.

The owner's 2026-10-30 rule, pinned from both sides so that inverting it fails a test:

- **falls back** on a connection error, a timeout, a rate limit after the SDK's retries, a 5xx or an
  overload (also mid-stream), a rejected key (401/403), and a key or region that was never
  configured;
- **never falls back** on a 400 (or any other 4xx the request earned), a refusal, a truncation, an
  answer that is not the object asked for, or a model the map does not know.

The primary is the real `BedrockLLM` over a scripted HTTP endpoint (`test_llm_bedrock`'s), so the
classification under test is the one production runs; the backup is a recorder. Then the wiring:
`build_llm` and the M17 selector put Bedrock in front of the CLI, the Commons fetcher stays on the
CLI, and every call lands in `token_usage` with the provider that answered it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import ExitStack
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from structlog.testing import capture_logs

from accounting import LedgeredLLM, TokenLedger, TokenPricer, load_price_card
from analyst.llm import (
    DEFAULT_MAX_TOKENS,
    BedrockLLM,
    FallbackLLM,
    LLMCredentialError,
    LLMError,
    LLMRateLimitError,
    LLMResponse,
    LLMUnavailableError,
    Message,
    StopReason,
    ToolCall,
    ToolSpec,
    UnavailableReason,
    UnconfiguredLLM,
    Usage,
    build_llm,
)
from analyst.llm.claude_cli import ClaudeCliLLM
from backtest import fm_world
from backtest.fm_job import ledgered
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import LlmProvider, Settings
from dataplatform.store.db import Connection
from tests.unit.test_llm_bedrock import (
    ANSWER,
    ASK,
    FAKE_KEY,
    VERDICT,
    Endpoint,
    _BrokenStream,
    _raise,
    api_error,
    bedrock,
    message_stream,
    sse,
    streamed,
)

SYSTEM = "You are a fund manager."


class Backup:
    """The CLI's stand-in: records every question, answers each with one structured output."""

    def __init__(self, *, raises: LLMError | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raises = raises

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "tools": list(tools),
                "system": system,
                "max_tokens": max_tokens,
            }
        )
        if self.raises is not None:
            raise self.raises
        return LLMResponse(
            provider="claude_cli",
            model=model,
            text="",
            usage=Usage(input_tokens=900, output_tokens=300, cache_read_tokens=50),
            stop_reason=StopReason.TOOL_USE,
            tool_calls=(ToolCall(id="structured_output", name=tools[0].name, arguments=ANSWER),),
        )


def desk(primary: Any, backup: Backup) -> FallbackLLM:
    return FallbackLLM(primary, backup, primary_provider="bedrock", backup_provider="claude_cli")


def ask(llm: Any) -> LLMResponse:
    response: LLMResponse = llm.complete(
        ASK, model="claude-opus-5-5", tools=(VERDICT,), system=SYSTEM, max_tokens=2_000
    )
    return response


def _overloaded_mid_stream() -> httpx.Response:
    head = message_stream("{}").split(b"event: content_block_start")[0]
    return streamed(head + sse({"type": "error", "error": {"type": "overloaded_error"}}))


def _dropped_mid_stream() -> httpx.Response:
    head = message_stream("{}").split(b"event: content_block_start")[0]
    return httpx.Response(200, stream=_BrokenStream(head))


#: Every way Bedrock can fail to *serve* a call, with the reason the fallback must carry.
TRIGGERS: list[tuple[str, Callable[[], Any], UnavailableReason]] = [
    ("401", lambda: api_error(401, "authentication_error"), UnavailableReason.AUTH),
    ("403", lambda: api_error(403, "permission_error"), UnavailableReason.AUTH),
    ("429", lambda: api_error(429, "rate_limit_error"), UnavailableReason.RATE_LIMIT),
    ("500", lambda: api_error(500, "api_error"), UnavailableReason.SERVER_ERROR),
    ("503", lambda: api_error(503, "api_error"), UnavailableReason.SERVER_ERROR),
    ("529", lambda: api_error(529, "overloaded_error"), UnavailableReason.SERVER_ERROR),
    ("mid-stream overload", _overloaded_mid_stream, UnavailableReason.SERVER_ERROR),
    ("mid-stream drop", _dropped_mid_stream, UnavailableReason.CONNECTION),
    ("connect", lambda: _raise(httpx.ConnectError("refused")), UnavailableReason.CONNECTION),
    ("timeout", lambda: _raise(httpx.ReadTimeout("slow")), UnavailableReason.TIMEOUT),
]

#: Every way the *request* can fail — the backup must never be asked.
NON_TRIGGERS: list[tuple[str, Callable[[], Any]]] = [
    ("400", lambda: api_error(400, "invalid_request_error")),
    ("404", lambda: api_error(404, "not_found_error")),
    ("422", lambda: api_error(422, "invalid_request_error")),
    ("refusal", lambda: streamed(message_stream("", stop_reason="refusal"))),
    ("truncated", lambda: streamed(message_stream('{"intact":', stop_reason="max_tokens"))),
    ("not json", lambda: streamed(message_stream("the thesis is intact"))),
    ("not an object", lambda: streamed(message_stream("[true]"))),
]


# ── the rule ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("make", "reason"), [(m, r) for _, m, r in TRIGGERS], ids=[n for n, _, _ in TRIGGERS]
)
def test_a_provider_failure_falls_back_to_the_backup_with_the_identical_question(
    make: Callable[[], Any], reason: UnavailableReason
) -> None:
    backup = Backup()
    response = ask(desk(bedrock(Endpoint(make())), backup))

    assert len(backup.calls) == 1
    call = backup.calls[0]
    assert call["messages"] == ASK
    assert call["model"] == "claude-opus-5-5"  # the roster's name, not the Bedrock id
    assert call["tools"] == [VERDICT]
    assert call["system"] == SYSTEM
    assert call["max_tokens"] == 2_000
    assert response.provider == "claude_cli"
    assert response.fallback is not None
    assert response.fallback.from_provider == "bedrock"
    assert response.fallback.reason is reason
    assert dict(response.tool_calls[0].arguments) == ANSWER


@pytest.mark.parametrize("make", [m for _, m in NON_TRIGGERS], ids=[n for n, _ in NON_TRIGGERS])
def test_a_failure_of_the_request_never_falls_back(make: Callable[[], Any]) -> None:
    backup = Backup()
    with pytest.raises(LLMError) as raised:
        ask(desk(bedrock(Endpoint(make())), backup))
    assert backup.calls == []
    assert not isinstance(raised.value, LLMUnavailableError)


def test_an_unmapped_model_never_falls_back() -> None:
    backup = Backup()
    with pytest.raises(LLMError, match="not mapped"):
        desk(bedrock(Endpoint(streamed(b""))), backup).complete(ASK, model="claude-sonnet-5")
    assert backup.calls == []


def test_the_primary_answers_when_it_can_and_the_backup_is_never_asked() -> None:
    import json

    backup = Backup()
    response = ask(desk(bedrock(Endpoint(streamed(message_stream(json.dumps(ANSWER))))), backup))
    assert backup.calls == []
    assert response.provider == "bedrock" and response.fallback is None


def test_a_rate_limit_falls_back_only_after_the_sdks_retries() -> None:
    endpoint = Endpoint(api_error(429, "rate_limit_error"))
    backup = Backup()
    ask(desk(bedrock(endpoint, max_retries=2), backup))
    assert len(endpoint.requests) == 3 and len(backup.calls) == 1


# ── loud ─────────────────────────────────────────────────────────────────────────────────────────


def test_each_fallback_logs_a_warning_with_provider_reason_and_model() -> None:
    with capture_logs() as logs:
        ask(desk(bedrock(Endpoint(api_error(529, "overloaded_error"))), Backup()))
    [warning] = [e for e in logs if e["event"] == "llm.fallback"]
    assert warning["log_level"] == "warning"
    assert warning["provider"] == "bedrock"
    assert warning["backup"] == "claude_cli"
    assert warning["reason"] == "server_error"
    assert warning["model"] == "claude-opus-5-5"
    [answered] = [e for e in logs if e["event"] == "llm.fallback_answered"]
    assert answered["provider"] == "claude_cli" and answered["input_tokens"] == 900
    assert not [e for e in logs if e["log_level"] == "error"]


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_also_logs_at_error_level(status: int) -> None:
    with capture_logs() as logs:
        ask(desk(bedrock(Endpoint(api_error(status, "permission_error"))), Backup()))
    [error] = [e for e in logs if e["event"] == "llm.fallback_credential_rejected"]
    assert error["log_level"] == "error"
    assert error["reason"] == "auth"
    assert "bedrock-mantle:CreateInference" in error["action"]


def test_a_backup_that_fails_too_raises_its_own_error_chained_to_the_primarys() -> None:
    backup = Backup(raises=LLMRateLimitError("the subscription is rate limited"))
    with capture_logs() as logs, pytest.raises(LLMRateLimitError) as raised:
        ask(desk(bedrock(Endpoint(api_error(503, "api_error"))), backup))
    # A rate limit still: DeadlineLLM backs off and asks the whole desk again.
    assert isinstance(raised.value.__cause__, LLMUnavailableError)
    assert [e["event"] for e in logs if e["log_level"] == "error"] == ["llm.fallback_failed"]


def test_a_fallback_to_the_same_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="retry, not a backup"):
        FallbackLLM(Backup(), Backup(), primary_provider="claude_cli", backup_provider="claude_cli")


# ── wiring: build_llm and the M17 desk ───────────────────────────────────────────────────────────


@pytest.fixture
def cli_backup(monkeypatch: pytest.MonkeyPatch) -> Backup:
    """`ClaudeCliLLM.from_settings` returns a recorder: no binary, no subprocess."""
    backup = Backup()
    monkeypatch.setattr(
        ClaudeCliLLM, "from_settings", classmethod(lambda cls, settings=None: backup)
    )
    return backup


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_the_m17_defaults_keep_the_cli_and_name_it_the_backup() -> None:
    settings = _settings()
    assert settings.m17_llm_provider is LlmProvider.CLAUDE_CLI  # unchanged until the owner flips it
    assert settings.m17_llm_fallback is LlmProvider.CLAUDE_CLI
    assert settings.llm_provider is LlmProvider.STUB  # B4: the tests' and a fresh clone's default
    assert settings.llm_fallback is LlmProvider.CLAUDE_CLI
    assert settings.bedrock_max_tokens == 64_000


@pytest.mark.parametrize("value", ["none", "", "NONE"])
def test_no_fallback_is_spelled_none_or_blank(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("M17_LLM_FALLBACK", value)
    assert _settings().m17_llm_fallback is None


def test_the_stub_is_never_a_fallback() -> None:
    with pytest.raises(ValidationError, match="stub cannot decide"):
        _settings(m17_llm_fallback="stub")


def test_bedrock_with_a_cli_backup_builds_the_fallback_desk(cli_backup: Backup) -> None:
    settings = _settings(
        m17_llm_provider="bedrock",
        aws_bearer_token_bedrock=SecretStr(FAKE_KEY),
        bedrock_region="us-east-1",
    )
    llm = fm_world._llm(settings, fm_world.m17_provider(settings, stub=False, memory=False))
    assert isinstance(llm, FallbackLLM)
    assert isinstance(llm._primary, BedrockLLM)
    assert llm._backup is cli_backup


def test_a_missing_key_with_a_backup_warns_at_build_and_falls_back_on_every_call(
    cli_backup: Backup,
) -> None:
    settings = _settings(m17_llm_provider="bedrock", bedrock_region="us-east-1")
    with capture_logs() as logs:
        llm = build_llm(settings, provider=LlmProvider.BEDROCK, fallback=LlmProvider.CLAUDE_CLI)
        response = ask(llm)
    [built] = [e for e in logs if e["event"] == "llm.primary_unconfigured"]
    assert built["log_level"] == "warning" and "AWS_BEARER_TOKEN_BEDROCK" in built["detail"]
    [fallback] = [e for e in logs if e["event"] == "llm.fallback"]
    assert fallback["reason"] == "not_configured" and fallback["log_level"] == "warning"
    assert response.fallback is not None
    assert response.fallback.reason is UnavailableReason.NOT_CONFIGURED
    assert len(cli_backup.calls) == 1
    assert not [e for e in logs if e["log_level"] == "error"]  # missing is a warning, not an alarm


def test_a_missing_key_without_a_backup_refuses_to_build() -> None:
    settings = _settings(bedrock_region="us-east-1")
    with pytest.raises(LLMCredentialError, match="AWS_BEARER_TOKEN_BEDROCK"):
        build_llm(settings, provider=LlmProvider.BEDROCK)


def test_an_unconfigured_primary_answers_nothing_on_its_own() -> None:
    with pytest.raises(LLMCredentialError):
        UnconfiguredLLM("bedrock", "no key").complete(ASK, model="claude-opus-5-5")


def test_a_backup_equal_to_the_primary_is_no_backup(cli_backup: Backup) -> None:
    llm = build_llm(_settings(), provider=LlmProvider.CLAUDE_CLI, fallback=LlmProvider.CLAUDE_CLI)
    assert llm is cli_backup


def test_the_application_path_puts_bedrock_in_front_of_the_cli(cli_backup: Backup) -> None:
    """`build_llm(settings)` with no provider: LLM_PROVIDER backed by LLM_FALLBACK, same rules."""
    settings = _settings(
        llm_provider="bedrock",
        aws_bearer_token_bedrock=SecretStr(FAKE_KEY),
        bedrock_region="us-east-1",
    )
    llm = build_llm(settings)
    assert isinstance(llm, FallbackLLM)
    assert isinstance(llm._primary, BedrockLLM)
    assert llm._backup is cli_backup


def test_the_application_path_without_a_key_falls_back_on_every_call(cli_backup: Backup) -> None:
    llm = build_llm(_settings(llm_provider="bedrock"))
    with capture_logs() as logs:
        response = ask(llm)
    assert response.fallback is not None
    assert response.fallback.reason is UnavailableReason.NOT_CONFIGURED
    assert [e["reason"] for e in logs if e["event"] == "llm.fallback"] == ["not_configured"]


def test_the_application_path_honours_no_fallback() -> None:
    with pytest.raises(LLMCredentialError):
        build_llm(_settings(llm_provider="bedrock", llm_fallback="none"))


def test_the_stub_default_never_builds_a_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    """B4: the default needs no credential and no CLI, though LLM_FALLBACK names the CLI."""
    from analyst.llm import StubLLM

    monkeypatch.setattr("analyst.llm.claude_cli.shutil.which", lambda _: None)
    assert isinstance(build_llm(_settings()), StubLLM)


def test_build_llm_refuses_the_stub_as_a_backup() -> None:
    with pytest.raises(ValueError, match="never a fallback"):
        build_llm(_settings(), provider=LlmProvider.BEDROCK, fallback=LlmProvider.STUB)


def _desk_fetcher(provider: LlmProvider, settings: Settings, root: Path) -> object:
    with ExitStack() as stack:
        return fm_world._fetcher(
            provider,
            settings=settings,
            clock=FrozenClock(datetime(2026, 10, 30, 18, 0, tzinfo=IST)),
            fetch_root=root / "commons" / "fetch",
            session=date(2026, 10, 30),
            ledger=None,
            memory=True,
            stack=stack,
        )


_VENDOR_KEYS: dict[str, Any] = {
    "brave_search_api_key": "brave-test-key-not-real",
    "tavily_api_key": "tvly-test-key-not-real",
    "firecrawl_api_key": "fc-test-key-not-real",
}


def test_bedrock_research_runs_on_our_own_search_layer_with_the_cli_behind_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Amendment 5 (d): Bedrock has no web tools, so the desk's fetcher is our own tool loop."""
    from analyst.commons.fetch import ClaudeWebFetcher
    from analyst.websearch import BedrockWebFetcher

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    settings = _settings(
        aws_bearer_token_bedrock=FAKE_KEY, bedrock_region="us-east-1", **_VENDOR_KEYS
    )
    fetcher = _desk_fetcher(LlmProvider.BEDROCK, settings, tmp_path)
    assert isinstance(fetcher, BedrockWebFetcher)
    assert fetcher.name == "bedrock+websearch:claude-sonnet-5-5"
    assert isinstance(fetcher._backup, ClaudeWebFetcher)
    # The tool results live in their own store, never in the desk's (a nested lock would hang).
    assert fetcher.research._store is not None
    assert fetcher.research._store.root == tmp_path / "commons" / "websearch"

    # No CLI backup when the desk has none.
    settings = _settings(
        aws_bearer_token_bedrock=FAKE_KEY,
        bedrock_region="us-east-1",
        m17_llm_fallback=None,
        **_VENDOR_KEYS,
    )
    fetcher = _desk_fetcher(LlmProvider.BEDROCK, settings, tmp_path)
    assert isinstance(fetcher, BedrockWebFetcher) and fetcher._backup is None


def test_bedrock_research_stays_on_the_cli_when_asked_or_when_it_cannot_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from analyst.commons.fetch import ClaudeWebFetcher

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    bedrock = {"aws_bearer_token_bedrock": FAKE_KEY, "bedrock_region": "us-east-1"}
    # The rollback: M17_WEB_RESEARCH=claude_cli.
    asked = _settings(m17_web_research="claude_cli", **bedrock, **_VENDOR_KEYS)
    assert isinstance(_desk_fetcher(LlmProvider.BEDROCK, asked, tmp_path), ClaudeWebFetcher)
    # No Bedrock key: the research cannot run on Bedrock, so it runs on the CLI, not not at all.
    assert isinstance(
        _desk_fetcher(LlmProvider.BEDROCK, _settings(**_VENDOR_KEYS), tmp_path), ClaudeWebFetcher
    )
    # No vendor key at all: the same.
    assert isinstance(
        _desk_fetcher(LlmProvider.BEDROCK, _settings(**bedrock), tmp_path), ClaudeWebFetcher
    )
    # A desk on the Claude CLI always researches there; a stub desk never researches live.
    cli = _settings(**bedrock, **_VENDOR_KEYS)
    assert isinstance(_desk_fetcher(LlmProvider.CLAUDE_CLI, cli, tmp_path), ClaudeWebFetcher)
    assert isinstance(_desk_fetcher(LlmProvider.STUB, cli, tmp_path), fm_world.NoLiveFetcher)


# ── usage: every call in token_usage, priced for whoever answered ─────────────────────────────────


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _Conn:
    """Echoes each INSERT back as its RETURNING row; keeps the parameters for the assertions."""

    def __init__(self) -> None:
        self.inserts: list[list[Any]] = []

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> _Cursor:
        assert sql.startswith("INSERT INTO token_usage")
        self.inserts.append(list(params or ()))
        return _Cursor([(len(self.inserts),)])


#: `token_usage`'s written columns, in `accounting.tokens._WRITE_COLUMNS` order.
_COLUMNS = (
    "ts",
    "provider",
    "model",
    "purpose",
    "case_id",
    "decision_journal_id",
    "tokens_in",
    "tokens_out",
    "cached_tokens",
    "cost_inr",
    "cost_usd",
    "recorded_at",
)

SESSION = date(2026, 10, 30)
NOW = datetime(2026, 10, 30, 22, 5, tzinfo=IST)


def _ledgered(inner: Any, conn: _Conn, purpose: str = "m17_manager") -> LedgeredLLM:
    clock = FrozenClock(NOW)
    return LedgeredLLM(
        inner,
        ledger=TokenLedger(cast(Connection, conn), clock=clock),
        pricer=TokenPricer(load_price_card()),
        purpose=purpose,
        on=SESSION,
        case_id="FM-MOMENTUM-1",
        clock=clock,
    )


def test_a_bedrock_call_is_recorded_with_its_provider_tokens_and_bedrock_price() -> None:
    import json

    conn = _Conn()
    endpoint = Endpoint(
        streamed(
            message_stream(
                json.dumps(ANSWER), input_tokens=1_000_000, output_tokens=100_000, cache_read=0
            )
        )
    )
    response = ask(_ledgered(desk(bedrock(endpoint), Backup()), conn))
    [row] = [dict(zip(_COLUMNS, params, strict=True)) for params in conn.inserts]
    assert response.provider == "bedrock"
    assert row["provider"] == "bedrock"
    assert row["model"] == "anthropic.claude-opus-5-5"
    assert row["purpose"] == "m17_manager" and row["case_id"] == "FM-MOMENTUM-1"
    assert (row["tokens_in"], row["tokens_out"], row["cached_tokens"]) == (1_000_000, 100_000, 0)
    # The Bedrock rate (in-region us-east-1): $4.40/MTok in, $22.00/MTok out — not first-party.
    assert row["cost_usd"] == Decimal("4.40") + Decimal("2.20")
    assert row["ts"] == NOW


def test_a_fallback_call_is_recorded_under_the_backup_that_answered_it() -> None:
    conn = _Conn()
    response = ask(_ledgered(desk(bedrock(Endpoint(api_error(503, "api_error"))), Backup()), conn))
    [row] = [dict(zip(_COLUMNS, params, strict=True)) for params in conn.inserts]
    assert response.fallback is not None
    assert row["provider"] == "claude_cli"
    assert row["model"] == "claude-opus-5-5"
    assert row["tokens_in"] == 950 and row["cached_tokens"] == 50


def test_an_unpriced_model_is_logged_and_the_answer_still_returned() -> None:
    conn = _Conn()

    class Unpriced(Backup):
        def complete(self, messages: Sequence[Message], **kwargs: Any) -> LLMResponse:
            import dataclasses

            return dataclasses.replace(super().complete(messages, **kwargs), model="mystery-1")

    with capture_logs() as logs:
        response = ask(_ledgered(Unpriced(), conn))
    assert response.model == "mystery-1"
    assert conn.inserts == []
    assert [e["event"] for e in logs if e["log_level"] == "warning"] == ["token_usage.unpriced"]


def test_without_a_ledger_the_desks_model_is_passed_through_untouched() -> None:
    inner = Backup()
    assert (
        ledgered(
            inner,
            ledger=None,
            pricer=None,
            session=SESSION,
            purpose="m17_manager",
            case_id=None,
            clock=FrozenClock(NOW),
        )
        is inner
    )
