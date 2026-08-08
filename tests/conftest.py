"""Shared fixtures for the whole suite.

Assumes nothing about the environment beyond the uv 3.12 venv: unit tests must pass with no
docker, no database and no network (AGENTIC_CONTEXT.md B8). Never reaches the network itself,
and never writes outside a tmp_path.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from dataplatform.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Builds a `Settings` from an explicit env file (or from the environment alone, with `None`).
SettingsLoader = Callable[[Path | None], Settings]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repo root, for tests that read checked-in fixtures or SQL."""
    return REPO_ROOT


@pytest.fixture
def clean_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every `Settings` key from the environment for the duration of one test.

    The process environment outranks `.env`, so a developer's exported `LOG_LEVEL` or a CI
    runner's `DATABASE_URL` would otherwise decide what a configuration test observes. Tests that
    assert on defaults or on `.env.example` request this first.
    """
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field, raising=False)


@pytest.fixture
def load_settings(clean_settings_env: None) -> SettingsLoader:
    """Build a `Settings` from a chosen env file, with the environment cleaned first.

    Passing `None` reads the environment only — which is what a test wants when it sets its own
    keys with `monkeypatch.setenv`, since the repo-root `.env` exists on some machines and not
    others and would make the same test observe different values.

    `_env_file` is a pydantic-settings runtime keyword. pydantic's `dataclass_transform` makes
    type checkers synthesize `__init__` from the declared fields alone, so it is invisible to
    them; the ignore below is that gap, not a loosened type.
    """

    def load(env_file: Path | None = None) -> Settings:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]

    return load
