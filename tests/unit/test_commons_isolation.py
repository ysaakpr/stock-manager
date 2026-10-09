"""`analyst.commons` never reaches `analyst.fundmanager` (M17.0, pre-registration §1).

Facts are shared, judgment is private: every manager reads the Commons, so if the Commons could
import the fund-manager package, one manager's decision content could leak into what the others
read. The check walks the first-party import graph from every module under `analyst/commons/`,
transitively and through parent packages (importing `a.b.c` executes `a/__init__` and
`a/b/__init__`), and fails on any path that lands in `analyst.fundmanager`. Imports under
`if TYPE_CHECKING:` count too — a type-only edge is still coupling.

The scanner itself is tested against synthetic trees, so a scanner that silently finds nothing
cannot keep this test green.
"""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FIRST_PARTY = frozenset({"analyst", "dataplatform", "execution", "backtest", "accounting"})
SHARED = "analyst.commons"
PRIVATE = "analyst.fundmanager"


def _module_file(root: Path, name: str) -> Path | None:
    base = root.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(root: Path, path: Path) -> set[str]:
    """Every module name `path` may import: static imports plus literal `import_module` calls."""
    name = _module_name(root, path)
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join([*anchor, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            if base:
                found.add(base)
            # `from pkg import sub` may name a submodule; over-approximating is the safe side.
            found.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "__import__")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
            )
        ):
            found.add(node.args[0].value)
    return found


def _forbidden(name: str) -> bool:
    return name == PRIVATE or name.startswith(PRIVATE + ".")


def private_reach(root: Path, shared: str = SHARED) -> list[str]:
    """Import chains from a module in `shared` to `analyst.fundmanager`, e.g. `a -> b -> fm`."""
    package_dir = root.joinpath(*shared.split("."))
    starts = sorted(_module_name(root, p) for p in package_dir.rglob("*.py"))
    if not starts:
        raise AssertionError(f"no modules found under {package_dir}; the scan would be vacuous")
    parent: dict[str, str | None] = dict.fromkeys(starts)
    queue = deque(starts)
    chains: list[str] = []
    while queue:
        current = queue.popleft()
        path = _module_file(root, current)
        if path is None:
            continue
        for target in sorted(_imports(root, path)):
            parts = target.split(".")
            if parts[0] not in FIRST_PARTY:
                continue
            # Importing `a.b.c` executes `a`, `a.b` and `a.b.c`.
            for depth in range(1, len(parts) + 1):
                edge = ".".join(parts[:depth])
                if edge in parent:
                    if _forbidden(edge):
                        break  # already reported through this edge
                    continue
                parent[edge] = current
                if _forbidden(edge):
                    chain = [edge]
                    step: str | None = current
                    while step is not None:
                        chain.append(step)
                        step = parent[step]
                    chains.append(" -> ".join(reversed(chain)))
                    break
                queue.append(edge)
    return chains


def test_commons_never_reaches_fundmanager() -> None:
    assert private_reach(REPO) == []


def test_both_packages_exist() -> None:
    assert _module_file(REPO, SHARED) is not None
    assert _module_file(REPO, PRIVATE) is not None


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    base = {
        "analyst/__init__.py": "",
        "analyst/commons/__init__.py": "",
        "analyst/fundmanager/__init__.py": "",
        "analyst/fundmanager/mandate.py": "X = 1\n",
        "analyst/journal/__init__.py": "",
    }
    for rel, text in {**base, **files}.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "source",
    [
        "import analyst.fundmanager\n",
        "import analyst.fundmanager.mandate as m\n",
        "from analyst.fundmanager import mandate\n",
        "from analyst.fundmanager.mandate import X\n",
        "from analyst import fundmanager\n",
        "from .. import fundmanager\n",
        "from ..fundmanager.mandate import X\n",
        "import importlib\nm = importlib.import_module('analyst.fundmanager')\n",
        "m = __import__('analyst.fundmanager.mandate')\n",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from analyst.fundmanager import X\n",
    ],
)
def test_scanner_catches_a_direct_import(tmp_path: Path, source: str) -> None:
    root = _tree(tmp_path, {"analyst/commons/sheets.py": source})
    chains = private_reach(root)
    assert chains, f"the scanner missed: {source!r}"
    assert chains[0].startswith("analyst.commons.sheets -> analyst.fundmanager")


def test_scanner_catches_a_transitive_import(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "analyst/commons/sheets.py": "from analyst.journal.writer import W\n",
            "analyst/journal/writer.py": "from analyst.fundmanager.mandate import X\nW = X\n",
        },
    )
    assert private_reach(root) == [
        "analyst.commons.sheets -> analyst.journal.writer -> analyst.fundmanager"
    ]


def test_scanner_catches_an_import_through_a_parent_package(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "analyst/commons/sheets.py": "from analyst.journal.writer import W\n",
            "analyst/journal/__init__.py": "from analyst import fundmanager\n",
            "analyst/journal/writer.py": "W = 1\n",
        },
    )
    assert private_reach(root) == [
        "analyst.commons.sheets -> analyst.journal -> analyst.fundmanager"
    ]


def test_scanner_allows_the_permitted_direction(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "analyst/commons/sheets.py": "import json\nfrom analyst.journal import x\n",
            "analyst/fundmanager/runtime.py": "from analyst.commons import sheets\n",
        },
    )
    assert private_reach(root) == []
