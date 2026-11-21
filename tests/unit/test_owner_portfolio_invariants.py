"""M19 phase-A invariants (spec §4), each a test that fails if the invariant is broken.

- I1 read-only by construction: no ``post``/``put``/``delete``/``patch``/``request`` call and no
  `KiteBroker` import anywhere under ``analyst/owner_portfolio/`` or in ``execution/kite_read.py``;
  the MCP tool list is pinned.
- I2 private: logs carry no symbol, ISIN, quantity or value; `analyst.fundmanager` (and the
  Commons and the backtest) never import `analyst.owner_portfolio`; nothing here writes to Postgres,
  the journal or the status API.
- I3 secrets: no credential on argv, in a URL, a log or the ``.mcp.json``; the token file is 0600
  (`test_kite_session`), the new fixtures pass the secret scan (`make check`).
- I4/I5: Decimal money, injected clock (the repo-wide clock guard), IST timestamps, offline tests.

Every AST scanner here is itself tested on a synthetic violation, so a scanner that silently finds
nothing cannot keep these green.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Final

import pytest
import structlog

from analyst.owner_portfolio.mcp_server import build_server
from analyst.owner_portfolio.tools import TOOL_NAMES
from tests.unit.owner_portfolio_fakes import ISINS, SYMBOLS, service

REPO: Final = Path(__file__).resolve().parents[2]
PHASE_A_FILES: Final = (
    *sorted((REPO / "analyst" / "owner_portfolio").rglob("*.py")),
    REPO / "execution" / "kite_read.py",
)
WRITE_VERBS: Final = frozenset({"post", "put", "delete", "patch", "request"})
SPEC_TOOLS: Final = (
    "session_status",
    "refresh",
    "portfolio_overview",
    "holding_detail",
    "exposures",
    "changes",
    "orders_today",
    "trades_today",
    "mf_holdings",
    "gtt_triggers",
)


# ── I1 read-only by construction ───────────────────────────────────────────────────────────


def _write_calls(source: str) -> list[str]:
    """Every call of a write verb, as ``x.post(``, ``post(``, or an ``import`` of KiteBroker."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else (func.id if isinstance(func, ast.Name) else "")
            )
            if name.lower() in WRITE_VERBS:
                found.append(f"call {name}( line {node.lineno}")
        if isinstance(node, ast.ImportFrom) and any(a.name == "KiteBroker" for a in node.names):
            found.append(f"import KiteBroker line {node.lineno}")
        if isinstance(node, ast.Attribute) and node.attr == "KiteBroker":
            found.append(f"attribute KiteBroker line {node.lineno}")
        if isinstance(node, ast.Name) and node.id == "KiteBroker":
            found.append(f"name KiteBroker line {node.lineno}")
    return found


def test_the_write_scanner_catches_violations() -> None:
    assert _write_calls("client.get(url)\n") == []
    assert _write_calls("client.post(url, data=x)\n")
    assert _write_calls("self._client.request('DELETE', url)\n")
    assert _write_calls("httpx.put(url)\n")
    assert _write_calls("from execution.kite_broker import KiteBroker\n")
    assert _write_calls("import execution.kite_broker as kb\nkb.KiteBroker(...)\n")


def test_phase_a_has_no_write_call_and_no_kitebroker() -> None:
    assert len(PHASE_A_FILES) >= 6, "the scan would pass vacuously"
    offenders = {
        p.relative_to(REPO).as_posix(): hits
        for p in PHASE_A_FILES
        if (hits := _write_calls(p.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_the_mcp_tool_list_is_pinned(tmp_path: Path) -> None:
    assert TOOL_NAMES == SPEC_TOOLS
    server = build_server(service(tmp_path))
    tools = asyncio.run(server.list_tools())
    assert tuple(t.name for t in tools) == SPEC_TOOLS
    for tool in tools:
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


# ── I2 privacy ─────────────────────────────────────────────────────────────────────────────

FIRST_PARTY: Final = frozenset({"analyst", "dataplatform", "execution", "backtest", "accounting"})


def _module_file(name: str) -> Path | None:
    base = REPO.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


@functools.cache
def _imports_of(path: Path) -> frozenset[str]:
    rel = path.relative_to(REPO).with_suffix("").parts
    module = ".".join(rel[:-1] if rel[-1] == "__init__" else rel)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*anchor, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            if base:
                found.add(base)
                found.update(f"{base}.{a.name}" for a in node.names)
    return frozenset(n for n in found if n.split(".")[0] in FIRST_PARTY)


def _reaches(start_package: str, target: str) -> list[str]:
    """Modules under ``start_package`` whose transitive first-party imports reach ``target``."""
    root = REPO.joinpath(*start_package.split("."))
    starts = sorted(root.rglob("*.py")) if root.is_dir() else []
    bad: list[str] = []
    for start in starts:
        seen: set[Path] = set()
        queue = deque([start])
        while queue:
            path = queue.popleft()
            if path in seen:
                continue
            seen.add(path)
            for name in _imports_of(path):
                if name == target or name.startswith(target + "."):
                    bad.append(start.relative_to(REPO).as_posix())
                    queue.clear()
                    break
                parts = name.split(".")
                for i in range(1, len(parts) + 1):  # parent packages execute too
                    file = _module_file(".".join(parts[:i]))
                    if file is not None and file not in seen:
                        queue.append(file)
    return sorted(set(bad))


def test_the_import_walker_finds_a_transitive_edge() -> None:
    # analyst.owner_portfolio.tools -> execution.kite_session: a known edge, found transitively.
    assert _reaches("analyst.owner_portfolio", "execution.kite_session")


@pytest.mark.parametrize("package", ["analyst.fundmanager", "analyst.commons", "backtest"])
def test_m17_commons_and_backtest_never_import_the_owner_portfolio(package: str) -> None:
    assert _reaches(package, "analyst.owner_portfolio") == []


_WRITE_SQL: Final = ("INSERT ", "UPDATE ", "DELETE FROM", "CREATE ", "ALTER ", "DROP ", "TRUNCATE")
_FORBIDDEN_MODULES: Final = ("analyst.journal", "dataplatform.status", "psycopg")


def test_nothing_writes_to_postgres_the_journal_or_the_status_api() -> None:
    for path in PHASE_A_FILES:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = _imports_of(path) | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        }
        assert not [m for m in imported if m.startswith(_FORBIDDEN_MODULES)], path
        strings = [
            n.value.upper()
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        assert not [s for s in strings if s.lstrip().startswith(_WRITE_SQL)], path
        calls = {
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "commit" not in calls and "execute" not in calls, path


def test_logs_carry_no_symbol_isin_quantity_or_value(tmp_path: Path) -> None:
    svc = service(tmp_path)
    with structlog.testing.capture_logs() as logs:
        svc.session_status()
        svc.refresh()
        svc.portfolio_overview()
        svc.holding_detail("INFY")
        svc.exposures()
        svc.changes("2026-11-06")
        svc.orders_today()
        svc.trades_today()
        svc.mf_holdings()
        svc.gtt_triggers()
    assert logs, "nothing was logged: the check would pass vacuously"
    text = json.dumps(logs, default=str)
    leaks = [s for s in (*SYMBOLS, *ISINS) if s in text]
    # Distinctive quantities and values from the fixtures and the review.
    leaks += [
        v
        for v in ("1500.50", "125000.75", "69750", "315910", "440910", "3200.25", "500.123")
        if v in text
    ]
    assert leaks == []


# ── I3 secrets ─────────────────────────────────────────────────────────────────────────────


def test_mcp_json_registers_the_server_with_uv_run_and_no_secret() -> None:
    config = json.loads((REPO / ".mcp.json").read_text())
    server = config["mcpServers"]["kite-portfolio"]
    assert server["command"] == "uv"
    assert server["args"][0] == "run"
    assert server["args"][-2:] == ["-m", "analyst.owner_portfolio.mcp_server"]
    assert "env" not in server  # credentials come from .env via Settings, never this file
    text = (REPO / ".mcp.json").read_text().lower()
    assert not any(k in text for k in ("token", "secret", "api_key", "password"))


def test_no_cli_takes_a_secret_on_argv() -> None:
    from analyst.owner_portfolio.__main__ import _parser

    parser = _parser()
    flags = {a for action in parser._actions for a in action.option_strings}
    for sub in parser._subparsers._group_actions:  # type: ignore[union-attr]
        for p in sub.choices.values():  # type: ignore[union-attr]
            flags |= {a for action in p._actions for a in action.option_strings}
    assert not [f for f in flags if any(k in f for k in ("token", "secret", "key", "password"))]


# ── I4 house rules ─────────────────────────────────────────────────────────────────────────


def test_timestamps_are_ist(tmp_path: Path) -> None:
    svc = service(tmp_path)
    out = svc.refresh()
    taken = datetime.fromisoformat(out["taken_at_ist"])
    assert taken.utcoffset() is not None and taken.utcoffset().total_seconds() == 19800  # type: ignore[union-attr]
    status = svc.session_status()
    assert status["expires_at_ist"].endswith("+05:30")


def test_phase_a_never_reads_the_wall_clock() -> None:
    for path in (*PHASE_A_FILES, REPO / "execution" / "kite_session.py"):
        source = path.read_text(encoding="utf-8")
        for banned in ("datetime.now(", "date.today(", "time.time("):
            assert banned not in source, (path, banned)


def test_entrypoints_log_warnings_and_up_only() -> None:
    """The lake's INFO lines name the ISINs they read; the review's entrypoints silence them."""
    from analyst.owner_portfolio.tools import quiet_settings
    from dataplatform.config import LogLevel, Settings

    loud = Settings(_env_file=None, log_level=LogLevel.DEBUG)  # type: ignore[call-arg]
    assert quiet_settings(loud).log_level is LogLevel.WARNING
    strict = Settings(_env_file=None, log_level=LogLevel.ERROR)  # type: ignore[call-arg]
    assert quiet_settings(strict).log_level is LogLevel.ERROR
    for name in ("mcp_server.py", "__main__.py"):
        source = (REPO / "analyst" / "owner_portfolio" / name).read_text(encoding="utf-8")
        assert "configure_logging(quiet_settings(settings)" in source, name
