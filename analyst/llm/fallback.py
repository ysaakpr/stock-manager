"""X3: a primary provider with a per-call backup — Bedrock first, the Claude CLI behind it.

The owner's 2026-10-30 decision: Bedrock answers, and the Claude CLI subscription answers instead
whenever Bedrock *could not*. The rule for "could not" is one exception class,
`LLMUnavailableError` — unreachable, timed out, throttled after the SDK's retries, a 5xx, a
rejected key, or no key at all — and nothing else. A 400, a refusal, a truncation or an answer that
did not parse propagates from the primary untouched: those say something about the question, and
the same question on the backup would either fail the same way a minute later or, worse, succeed
and hide a defect in the request that the primary was right to refuse.

Each fallback is loud. A structlog warning names the primary, the reason class and the model before
the backup is asked; a rejected key also logs at error level, because it means someone has to
replace the key or grant a permission; and the backup's response carries a `Fallback` record, so
the journal line and the `token_usage` row of that call say which provider answered and why it was
not the primary.

A model the backup cannot run — DeepSeek on Bedrock, behind the Claude CLI — is re-asked as the
model the primary's configuration names for it (`backup_model`, from `bedrock_models.yaml`'s
`cli_fallback_model`: DeepSeek V3.2's digests fall back to Sonnet 5.5). The response then names the
backup's provider and model, so a digest records who actually wrote it. A Claude model maps to
itself.

There is no fallback from the fallback. If the backup fails too, its error is raised — chained to
the primary's, so both are in the traceback — and the caller handles it as it would without a
backup (`DeadlineLLM` still backs off on a rate limit).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence

from analyst.llm.client import (
    DEFAULT_MAX_TOKENS,
    LLM,
    Fallback,
    LLMError,
    LLMResponse,
    LLMUnavailableCredentialError,
    LLMUnavailableError,
    Message,
    ToolSpec,
    UnavailableReason,
)
from dataplatform.logging import get_logger

__all__ = ["FallbackLLM", "UnconfiguredLLM"]

_LOG = get_logger(__name__)

#: The longest provider message a fallback log line quotes; the exception keeps the whole text.
_LOG_DETAIL_CHARS = 300


class UnconfiguredLLM:
    """The primary that could not be built: every call is a `NOT_CONFIGURED` fallback trigger.

    What it does: stands in for a primary whose key or region is missing, so that `FallbackLLM`
    logs and records the fallback on every call rather than once at startup where nobody looks.
    What it never does: answer. Without a `FallbackLLM` around it, it fails every call.
    """

    __slots__ = ("_detail", "_provider")

    def __init__(self, provider: str, detail: str) -> None:
        if not provider.strip():
            raise ValueError("an unconfigured provider still needs a name")
        self._provider = provider
        self._detail = detail

    def __repr__(self) -> str:
        return f"{type(self).__name__}(provider={self._provider!r})"

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        raise LLMUnavailableCredentialError(
            f"{self._provider} is not configured, so {model} was not asked there: {self._detail}",
            provider=self._provider,
            reason=UnavailableReason.NOT_CONFIGURED,
        )


class FallbackLLM:
    """An `LLM` that asks ``primary`` and, only when it could not serve the call, ``backup``.

    What it does: per call, returns the primary's response; on `LLMUnavailableError` logs the
    fallback, asks the backup the identical question (same messages, tool, system prompt, ceiling,
    and the model or its `backup_model`) and returns its response with a `Fallback` record.
    What it assumes: ``backup_model`` maps a model name the backup cannot run to one it can
    (identity when omitted — both accept the same names, as the Claude models do).
    What it never does: fall back on any other error, retry the primary, or fall back twice.
    """

    __slots__ = (
        "_backup",
        "_backup_model",
        "_backup_provider",
        "_primary",
        "_primary_provider",
    )

    def __init__(
        self,
        primary: LLM,
        backup: LLM,
        *,
        primary_provider: str,
        backup_provider: str,
        backup_model: Callable[[str], str] | None = None,
    ) -> None:
        if primary_provider == backup_provider:
            raise ValueError(
                f"a fallback to the same provider ({primary_provider!r}) is a retry, not a backup"
            )
        self._primary = primary
        self._backup = backup
        self._primary_provider = primary_provider
        self._backup_provider = backup_provider
        self._backup_model = backup_model

    def __repr__(self) -> str:
        return f"{type(self).__name__}(primary={self._primary!r}, backup={self._backup!r})"

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        """The primary's answer, or — when it could not serve the call — the backup's."""
        try:
            return self._primary.complete(
                messages, model=model, tools=tools, system=system, max_tokens=max_tokens
            )
        except LLMUnavailableError as unavailable:
            failed = unavailable
        asked = model if self._backup_model is None else self._backup_model(model)
        fields = {
            "provider": failed.provider,
            "backup": self._backup_provider,
            "reason": failed.reason.value,
            "model": model,
            "backup_model": asked,
            "detail": str(failed)[:_LOG_DETAIL_CHARS],
        }
        _LOG.warning("llm.fallback", **fields)
        if failed.reason is UnavailableReason.AUTH:
            _LOG.error(
                "llm.fallback_credential_rejected",
                **fields,
                action=(
                    "replace the provider's key, or grant its permission (Bedrock: "
                    "bedrock-mantle:CreateInference); every call falls back until then"
                ),
            )
        try:
            response = self._backup.complete(
                messages, model=asked, tools=tools, system=system, max_tokens=max_tokens
            )
        except LLMError as backup_failed:
            _LOG.error(
                "llm.fallback_failed",
                **fields,
                backup_error=f"{type(backup_failed).__name__}: {backup_failed}"[:_LOG_DETAIL_CHARS],
            )
            raise backup_failed from failed
        _LOG.info(
            "llm.fallback_answered",
            provider=response.provider,
            primary=failed.provider,
            reason=failed.reason.value,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_write_tokens=response.usage.cache_write_tokens,
            cache_read_tokens=response.usage.cache_read_tokens,
        )
        return dataclasses.replace(
            response, fallback=Fallback(from_provider=failed.provider, reason=failed.reason)
        )
