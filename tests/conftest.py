"""Shared fixtures for the whole suite.

Assumes nothing about the environment beyond the uv 3.12 venv: unit tests must pass with no
docker, no database and no network (AGENTIC_CONTEXT.md B8). Never reaches the network itself,
and never writes outside a tmp_path.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repo root, for tests that read checked-in fixtures or SQL."""
    return REPO_ROOT
