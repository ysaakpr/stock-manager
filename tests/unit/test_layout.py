"""The package layout is itself under test (M0.1 acceptance criterion 3).

Every module in EXECUTION_PLAN.md §8.2 / CLAUDE.md's layout must exist, import cleanly, and say
which plan module owns it. A later task that adds a package without a plan ID, or deletes one,
fails here rather than quietly drifting from the plan.
"""

from __future__ import annotations

import importlib
import sys

import pytest

# import path -> the plan module ID its docstring must name.
LAYOUT: dict[str, str] = {
    "dataplatform": "D1-D7",
    "dataplatform.ingest": "D1",
    "dataplatform.identity": "D2",
    "dataplatform.corpactions": "D3",
    "dataplatform.store": "D4",
    "dataplatform.query": "D4",
    "dataplatform.quality": "D7",
    "dataplatform.archives": "D6",
    "dataplatform.status": "D5",
    "analyst": "A1-A9",
    "analyst.cases": "A1",
    "analyst.interview": "A2",
    "analyst.mapper": "A3",
    "analyst.thesis": "A4",
    "analyst.monitor": "A5",
    "analyst.rotation": "A6",
    "analyst.cash": "A7",
    "analyst.rails": "A8",
    "analyst.journal": "A9",
    "execution": "X1",
    "execution.costs": "X1",
    "backtest": "X2",
    "accounting": "X3",
}

TOP_LEVEL_PACKAGES = ("dataplatform", "analyst", "execution", "backtest", "accounting")


@pytest.mark.parametrize(("module_path", "plan_id"), sorted(LAYOUT.items()))
def test_package_exists_and_names_its_plan_module(module_path: str, plan_id: str) -> None:
    module = importlib.import_module(module_path)
    doc = module.__doc__
    assert doc, f"{module_path} has no module docstring naming its plan module"
    assert plan_id in doc, f"{module_path} docstring must name plan module {plan_id}: {doc!r}"


@pytest.mark.parametrize("name", TOP_LEVEL_PACKAGES)
def test_no_package_shadows_a_stdlib_module(name: str) -> None:
    """A top-level package that shadows the stdlib is unimportable and breaks dependencies.

    This is why System 1 is `dataplatform/` and not the plan's `platform/`: once anything has
    imported the stdlib `platform`, `platform.config` resolves to a non-package and fails; and
    when the local package wins the path race instead, pandas dies on
    `platform.python_implementation()`. Reintroducing such a name fails here.
    """
    assert name not in sys.stdlib_module_names, f"{name}/ shadows the stdlib module {name!r}"


def test_runs_on_the_pinned_interpreter() -> None:
    """uv owns a pinned 3.12 (B3); the host's 3.9 must never be what runs the suite."""
    assert sys.version_info[:2] == (3, 12)
