"""M17.12 — the M17 desk runs on its own provider setting, and a persisted stub run refuses.

The first live dry session (2026-10-09) ran from the scheduler with no ``LLM_PROVIDER`` set: the
desk followed the global default (the stub), so every manager hit ``MANAGER_ERROR`` and the
fetcher was the no-op one. Setting the global provider would also have switched the older
analyst paper paths. The desk now reads ``M17_LLM_PROVIDER`` (default the Claude CLI) for its
managers, digests and fetcher, ``--stub-llm`` still overrides, and a non-``--memory`` run that
resolves to the stub refuses before it opens anything.

Offline: no CLI is run; a CLI-backed object is only constructed where a fake ``claude`` is on
``PATH``.
"""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from analyst.commons.fetch import ClaudeWebFetcher
from analyst.llm import StubLLM
from analyst.llm.claude_cli import ClaudeCliLLM
from backtest import fm_world
from backtest.fm_job import M17JobError
from backtest.fm_world import NoLiveFetcher, m17_provider
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import LlmProvider, Settings
from dataplatform.scheduler.registry import JobContext


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key in ("LLM_PROVIDER", "M17_LLM_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``claude`` on PATH that is never executed: construction only checks it exists."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text("#!/bin/sh\nexit 97\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def test_the_desk_defaults_to_the_claude_cli_and_the_global_default_stays_the_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    assert settings.llm_provider is LlmProvider.STUB  # B4: the global default is untouched
    assert settings.m17_llm_provider is LlmProvider.CLAUDE_CLI
    assert m17_provider(settings, stub=False, memory=False) is LlmProvider.CLAUDE_CLI


def test_the_desk_follows_its_own_setting_never_the_global_one(
    monkeypatch: pytest.MonkeyPatch, fake_claude: None
) -> None:
    # The global provider says stub (the older paper paths); the desk still runs on the CLI.
    settings = _settings(monkeypatch, LLM_PROVIDER="stub", M17_LLM_PROVIDER="claude_cli")
    provider = m17_provider(settings, stub=False, memory=False)
    assert provider is LlmProvider.CLAUDE_CLI
    assert isinstance(fm_world._llm(settings, provider), ClaudeCliLLM)
    assert isinstance(fm_world._fetcher(provider), ClaudeWebFetcher)

    # And the other way round: a global CLI does not drag the desk off its own setting.
    settings = _settings(monkeypatch, LLM_PROVIDER="claude_cli", M17_LLM_PROVIDER="stub")
    provider = m17_provider(settings, stub=False, memory=True)
    assert provider is LlmProvider.STUB
    assert isinstance(fm_world._llm(settings, provider), StubLLM)
    assert isinstance(fm_world._fetcher(provider), NoLiveFetcher)


def test_stub_llm_overrides_the_setting_for_a_memory_rehearsal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, M17_LLM_PROVIDER="claude_cli")
    provider = m17_provider(settings, stub=True, memory=True)
    assert provider is LlmProvider.STUB
    assert isinstance(fm_world._llm(settings, provider), StubLLM)
    assert isinstance(fm_world._fetcher(provider), NoLiveFetcher)


@pytest.mark.parametrize(
    ("env", "stub"),
    [({"M17_LLM_PROVIDER": "stub"}, False), ({"M17_LLM_PROVIDER": "claude_cli"}, True)],
)
def test_a_persisted_run_that_resolves_to_the_stub_refuses_before_it_opens_anything(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], stub: bool
) -> None:
    settings = _settings(monkeypatch, **env)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a refused run must not touch the lake, the roster or Postgres")

    monkeypatch.setattr(fm_world, "LakeM17World", forbidden)
    monkeypatch.setattr(fm_world, "load_roster", forbidden)
    monkeypatch.setattr(fm_world, "store_listings", forbidden)
    with pytest.raises(M17JobError, match="stub LLM"), ExitStack() as stack:
        fm_world._wire(
            settings=settings,
            clock=FrozenClock(datetime(2026, 10, 9, 22, 0, tzinfo=IST)),
            session=None,
            dry_run=True,
            start=None,
            memory=False,
            stub_llm=stub,
            no_wait=True,
            scratch=None,
            stack=stack,
        )


def test_the_scheduler_job_refuses_a_stub_desk(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, M17_LLM_PROVIDER="stub")
    monkeypatch.setattr(fm_world, "LakeM17World", lambda *a, **k: pytest.fail("opened the lake"))
    context = JobContext(
        job_name="m17_fund_managers",
        run_id=uuid4(),
        clock=FrozenClock(datetime(2026, 10, 9, 22, 0, tzinfo=IST)),
        settings=settings,
    )
    with pytest.raises(M17JobError, match="M17_LLM_PROVIDER=stub"):
        fm_world.production_run(context, dry_run=True, start=None, session=None)


def test_a_memory_rehearsal_may_run_on_the_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, M17_LLM_PROVIDER="stub")
    assert m17_provider(settings, stub=False, memory=True) is LlmProvider.STUB
