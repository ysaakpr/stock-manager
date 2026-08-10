"""Static enforcement of invariant #13's real rule: `get_secret_value()` never reaches a log call,
an exception constructor, or a name that outlives the expression it was unwrapped in.

This is the allowlist direction a review of this branch asked for, applied where it is tractable
without a rewrite of the logging pipeline: `dataplatform.logging`'s `_redaction_processor` is a
regex denylist — it has zero coverage for SMTP, Anthropic or Kite credentials (nothing calls
`get_secret_value()` on them yet, so there is nothing to redact today, but nothing would catch it
the day M6.8 or M8.3 adds that call either) and cannot reach into Rich's rendered console output
at all. A regex can only clean up after a secret has already reached the boundary; this scans
every call site that would let one arrive there in the first place, mechanically, on every run —
not just the shapes the audit happened to think to name a pattern for.

Deliberately NOT a runtime guard (a custom logger wrapper, a monkeypatched `get_secret_value`):
this repo's actual entrypoints (`dataplatform.logging.get_logger`, plain `raise`) are the ones
every call site already uses, and a static check catches the mistake at review time rather than
waiting for the one test run that happens to exercise the failing branch. `SecretStr` intact all
the way to the point of use, never `.get_secret_value()` bound to a name that outlives one
expression: this is that rule, enforced rather than merely documented.

COVERAGE, stated exactly rather than implied (a review of an earlier version of this file found
it missed the most common shape — an intermediate variable — while its docstring claimed the
opposite; a boundary test that reads as comprehensive but is not is worse than one honest about
its limits, because the next reader stops looking):

CAUGHT — `get_secret_value()` nested anywhere inside:
  - a log-shaped method call's arguments (`.debug/.info/.warning/.warn/.error/.exception/
    .critical/.log(...)`, `.write(...)`, `.add_note(...)`)
  - a bare sink call's arguments (`print(...)`, `bind_context(...)`, `bind_contextvars(...)`)
  - a directly-raised exception's constructor arguments (`raise Err(x.get_secret_value())`)
  - `raise <name>` where `<name>` was assigned a value containing `get_secret_value()` anywhere in
    it, including via a constructor argument (`err = Err(x.get_secret_value())` ; `raise err`) —
    checked separately from the plain-assignment rule below, and deliberately blanket rather than
    narrowed, because a name that is *going to be raised* is dangerous the moment a secret reaches
    it by any route, unlike a name that is merely stored for later legitimate use
  - the value of an assignment (`=`, annotated `:=` walrus, or `AnnAssign`) whose value *is*
    `get_secret_value()`'s own return (directly, or through `.strip()`-style chaining on the same
    receiver) — the intermediate-variable shape this file's predecessor missed while claiming to
    enforce it. Narrowed to exclude a value built by *passing* the secret as an argument to a
    different call (`client = Anthropic(api_key=x.get_secret_value())`): that call's result, not
    the secret, is what `client` holds, exactly like the already-legitimate `psycopg.connect(
    password=x.get_secret_value())` — a blanket version of this rule flagged that pattern as if it
    were `pw = x.get_secret_value()`, a real false positive against this repo's own code.

NOT CAUGHT, by design or by tractability limit — do not assume these are safe because this file is
green, and do not add a shape here without adding the matching test in `_KNOWN_SHAPES` below:
  - helper-function indirection (`def _pw(s): return s.x.get_secret_value()`; `log.info(p=_pw(s))`)
    — would need interprocedural return-value tracking across function boundaries; out of scope
    for a single-file AST walk
  - a bare, unqualified logging call bound via `from logging import info` or similar re-import —
    the generic names (`info`, `error`, `debug`...) collide too easily with unrelated functions to
    add as bare-call sinks without real false-positive risk, and nothing in this codebase does this
  - anything reached only through `getattr`/`setattr`/`eval`/dynamic dispatch
  - legitimate sinks are correctly NOT flagged: `psycopg.connect(password=...)`,
    `smtp.login(..., password.get_secret_value())` — call *arguments* to a function that is not
    itself a logging/exception/assignment sink stay clean, which is the whole point.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from dataplatform.config import REPO_ROOT

#: The product-code packages `pyproject.toml`'s `[tool.mypy] files` also scopes to, plus
#: `orchestrator`: it is excluded from the mypy/ruff gate (predates it — CLAUDE.md), but it is the
#: code that writes `BUILD_STATE.json`, which invariant #13 singles out as published and
#: machine-written (AGENTIC_CONTEXT.md §6) — the mypy-scope rationale for excluding it does not
#: apply to a pure AST scan, which has no dependency on the package being importable or typed.
#: Deliberately not `tests/`, which legitimately calls `get_secret_value()` to construct and
#: assert on fixture secrets, never to log or raise with one.
PRODUCT_PACKAGES = (
    "dataplatform",
    "analyst",
    "execution",
    "backtest",
    "accounting",
    "orchestrator",
)

#: Method names that emit a record or otherwise put a value somewhere it can be read back:
#: bound-logger/stdlib-logger methods, plus `write` (a file, buffer or `sys.stderr`) and
#: `add_note` (attaches text to an exception that every renderer prints alongside it).
_ATTRIBUTE_SINK_NAMES = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "log",
        "write",
        "add_note",
    }
)

#: Bare-name (not method) sink calls. `bind_context`/`bind_contextvars` are the worst case in this
#: whole file if missed: they bind a value onto every subsequent log event on the calling task
#: until cleared, not just the one call site (`dataplatform/logging.py:bind_context`).
_BARE_FUNCTION_SINK_NAMES = frozenset({"print", "bind_context", "bind_contextvars"})


def _calls_get_secret_value(node: ast.AST) -> bool:
    """Whether `.get_secret_value()` is called anywhere inside this subtree."""
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == "get_secret_value"
        for sub in ast.walk(node)
    )


def _is_sink_call(node: ast.Call, raised_exception_calls: set[int]) -> bool:
    is_attribute_sink = (
        isinstance(node.func, ast.Attribute) and node.func.attr in _ATTRIBUTE_SINK_NAMES
    )
    is_bare_sink = isinstance(node.func, ast.Name) and node.func.id in _BARE_FUNCTION_SINK_NAMES
    is_raised = id(node) in raised_exception_calls
    return is_attribute_sink or is_bare_sink or is_raised


def _is_bare_secret(node: ast.AST, *, consumed_by_another_call: bool = False) -> bool:
    """Whether `node` *is* (a trivial transform of) `get_secret_value()`'s own return value, as
    opposed to a value built by handing the secret to a *different* call as one of its arguments.

    The distinction is the difference between `pw = x.get_secret_value()` (`pw` now holds the
    plaintext — dangerous to bind) and `client = Anthropic(api_key=x.get_secret_value())` (`client`
    holds an SDK object the constructor built; the secret was consumed, not returned — the same
    shape as the already-legitimate `psycopg.connect(password=x.get_secret_value())`). Both are
    "a `Call` node with `get_secret_value()` nested inside," so a shape-blind walk cannot tell
    them apart; this follows the *receiver* chain (attribute access, and a call's `.func`, which a
    method call chains through) but stops crediting `get_secret_value()` for danger the moment it
    is found inside a call's `.args`/`.keywords` instead — that argument position is exactly where
    "handed to something else" shows up in the AST.
    """
    if isinstance(node, ast.Call):
        is_gsv_call = isinstance(node.func, ast.Attribute) and node.func.attr == "get_secret_value"
        if is_gsv_call and not consumed_by_another_call:
            return True
        if isinstance(node.func, ast.Attribute) and _is_bare_secret(
            node.func.value, consumed_by_another_call=consumed_by_another_call
        ):
            return True
        arguments = list(node.args) + [kw.value for kw in node.keywords]
        return any(_is_bare_secret(arg, consumed_by_another_call=True) for arg in arguments)
    if isinstance(node, ast.Attribute):
        return _is_bare_secret(node.value, consumed_by_another_call=consumed_by_another_call)
    if isinstance(node, ast.JoinedStr):  # an f-string: each {expr} starts a fresh chain
        return any(_is_bare_secret(value) for value in node.values)
    if isinstance(node, ast.FormattedValue):
        return _is_bare_secret(node.value)
    return False


def _assigned_names_holding_a_bare_secret(tree: ast.AST) -> set[str]:
    """Every name assigned (anywhere in `tree`) a value `_is_bare_secret` calls dangerous.

    File-flat rather than scope-aware: a name reused in a different function with the same
    spelling is a false positive this trades for never missing a same-function case with simple
    AST work. Erring toward flagging more, not less, is the correct direction for a security check.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and _is_bare_secret(node.value):
                names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name) and _is_bare_secret(node.value):
                names.add(node.target.id)
    return names


def _offending_nodes(tree: ast.AST) -> list[ast.expr | ast.stmt]:
    """Every node that lets `get_secret_value()` reach a sink, an exception, or a name.

    Four shapes, one pass:
      1. a sink call (a log method, `print`, `write`, `add_note`, `bind_context[vars]`) with the
         secret nested anywhere in its own arguments — a blanket walk is correct here, because
         there is no safe way to bury a secret inside a logging call's arguments regardless of
         how deeply it is nested;
      2. a directly-raised exception's constructor arguments — same reasoning;
      3. an assignment (`=`, `AnnAssign`, or walrus `:=`) whose value *is* the secret itself
         (`_is_bare_secret`) — the shape a predecessor of this file missed while its docstring
         claimed to catch it, and the one that makes `config.py`'s own DSN validator honest;
      4. `raise <Name>` where `<Name>` was assigned a value containing `get_secret_value()`
         *anywhere* in that value's tree (the blanket walk, not `_is_bare_secret`) — unlike rule 3,
         a name that is *going to be raised* is dangerous even if the secret arrived via a
         constructor argument (`err = SomeError(x.get_secret_value())` is exactly as dangerous
         once raised as `raise SomeError(x.get_secret_value())` directly is), so rule 3's narrower
         check would under-flag it; rule 4 exists because rule 3 deliberately does not blanket-flag
         every assignment.
    """
    raised_exception_calls = {
        id(node.exc)
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
    }
    dangerous_assigned_names = _assigned_names_holding_a_bare_secret(tree)
    danger_by_any_assignment = {
        node.targets[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and _calls_get_secret_value(node.value)
    }

    offenders: list[ast.expr | ast.stmt] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_sink_call(node, raised_exception_calls):
            arguments = list(node.args) + [kw.value for kw in node.keywords]
            if any(_calls_get_secret_value(arg) for arg in arguments):
                offenders.append(node)
            continue
        if isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in dangerous_assigned_names:
                offenders.append(node)
            continue
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in dangerous_assigned_names:
                offenders.append(node)
            continue
        if isinstance(node, ast.NamedExpr) and _is_bare_secret(node.value):
            offenders.append(node)
        if (
            isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Name)
            and node.exc.id in danger_by_any_assignment
        ):
            offenders.append(node)
    return offenders


def _product_source_files() -> list[Path]:
    return sorted(
        path
        for package in PRODUCT_PACKAGES
        for path in (REPO_ROOT / package).rglob("*.py")
        if "__pycache__" not in path.parts
    )


@pytest.mark.parametrize(
    "path", _product_source_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_no_product_file_lets_get_secret_value_reach_a_sink(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    offenders = _offending_nodes(tree)

    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)}: get_secret_value() reaches a log call, a raised "
        f"exception, or an assignment at line(s) {sorted(o.lineno for o in offenders)} — keep "
        "the SecretStr wrapped until the single call (psycopg.connect, smtp.login, httpx's URL "
        "builder, ...) that actually needs the plaintext, and never bind get_secret_value()'s "
        "result to a name, however short-lived."
    )


#: Every shape a review actually probed against this file's `_offending_nodes`, kept as one
#: table so the docstring's CAUGHT/NOT CAUGHT split is asserted, not just asserted-about. A
#: `False` here documents a known miss; changing one to `True` without a matching implementation
#: change means the test is lying about what it covers, which is the exact defect this table
#: exists to prevent.
_KNOWN_SHAPES = [
    pytest.param(
        'log.info("db", dsn=s.database_url.get_secret_value())', True, id="log-call-kwarg"
    ),
    pytest.param('log.error(f"failed {s.x.get_secret_value()}")', True, id="log-call-fstring"),
    pytest.param("raise RuntimeError(s.x.get_secret_value())", True, id="raise-direct"),
    pytest.param(
        'pw = s.postgres_password.get_secret_value()\nlog.info("db", p=pw)',
        True,
        id="intermediate-variable-then-log",
    ),
    pytest.param(
        'pw = s.x.get_secret_value()\nlog.error(f"failed {pw}")',
        True,
        id="intermediate-variable-then-fstring",
    ),
    pytest.param(
        "err = RuntimeError(s.x.get_secret_value())\nraise err",
        True,
        id="raise-via-variable",
    ),
    pytest.param("print(s.x.get_secret_value())", True, id="print"),
    pytest.param("sys.stderr.write(s.x.get_secret_value())", True, id="stderr-write"),
    pytest.param("e.add_note(s.x.get_secret_value())", True, id="add-note"),
    pytest.param("bind_context(dsn=s.x.get_secret_value())", True, id="bind-context"),
    pytest.param("bind_contextvars(dsn=s.x.get_secret_value())", True, id="bind-contextvars"),
    pytest.param(
        'def _pw(s):\n    return s.x.get_secret_value()\nlog.info("db", p=_pw(s))',
        False,
        id="helper-indirection-KNOWN-MISS",
    ),
    pytest.param(
        "from logging import info\ninfo(s.x.get_secret_value())",
        False,
        id="bare-reimported-logger-KNOWN-MISS",
    ),
    pytest.param("psycopg.connect(password=s.x.get_secret_value())", False, id="legit-connect"),
    pytest.param("smtp.login(user, pw.get_secret_value())", False, id="legit-smtp-login"),
    pytest.param(
        "self._client = Anthropic(api_key=self._api_key.get_secret_value())",
        False,
        id="legit-assign-result-of-a-call-that-consumes-the-secret",
    ),
]


@pytest.mark.parametrize(("snippet", "should_be_flagged"), _KNOWN_SHAPES)
def test_boundary_coverage_matches_the_documented_shape_table(
    snippet: str, should_be_flagged: bool
) -> None:
    tree = ast.parse(snippet)

    offenders = _offending_nodes(tree)

    assert bool(offenders) is should_be_flagged, (
        f"{snippet!r} expected flagged={should_be_flagged}, got {bool(offenders)} — either the "
        "detector regressed or this file's module docstring needs updating to match reality."
    )
