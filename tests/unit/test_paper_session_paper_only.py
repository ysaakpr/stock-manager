"""M13.1 — the paper session job cannot reach ``KiteBroker``.

D13 ratified the momentum book for **paper** only; real money is a separate ratification
(AGENTIC_CONTEXT §3.2) and a human fires the first real order (§3.5). These tests fail if the
scheduler's ``paper_session`` job could route an order to the real broker, by any of the three
ways it could come to:

1. **By import.** ``execution.kite_broker`` is not imported by the paper session module, statically
   or transitively: a fresh interpreter that loads the registry, resolves the job and imports its
   implementation has never loaded the real broker's module.
2. **By injection.** No function on the job's path takes a broker: the job builds its broker
   inside, and the only broker it can build is ``SimBroker``.
3. **By substitution.** The paper order path accepts exactly ``SimBroker`` — a ``KiteBroker``,
   dry-run or not, and even a ``SimBroker`` subclass, are refused before any order reaches them.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from backtest import paper_session
from backtest.paper_session import (
    PaperModeViolationError,
    _PaperBroker,
    _restore_book,
    require_paper_broker,
    run_paper_session,
    run_paper_session_job,
)
from dataplatform.scheduler import default_registry
from execution.broker import Broker
from execution.kite_broker import KiteBroker
from execution.sim_broker import SimBroker

_MODULE = Path(paper_session.__file__)


def test_the_paper_session_module_never_imports_the_real_broker() -> None:
    tree = ast.parse(_MODULE.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not [name for name in imported if "kite" in name.lower()], imported


def test_running_the_scheduler_job_end_to_end_never_loads_the_real_broker_module() -> None:
    """A fresh interpreter runs the *registered* job — enabled, with the broker provider set to
    kite — through its production path, only its I/O stubbed (no lake, no database), decides a
    session, and has never loaded a kite module."""
    probe = (
        "import sys\n"
        "from datetime import datetime\n"
        "from uuid import uuid4\n"
        "from backtest.paper_session import InMemoryPaperSessionStore, RecordingJournal\n"
        "from dataplatform.clock import IST, FrozenClock\n"
        "from dataplatform.config import Settings\n"
        "from dataplatform.scheduler import JobContext, default_registry\n"
        "from tests.paper_session_support import FixtureWorld, install_job_seams\n"
        "store, journal = InMemoryPaperSessionStore(), RecordingJournal()\n"
        "install_job_seams(setattr, world=FixtureWorld(), store=store, journal=journal)\n"
        "settings = Settings(paper_session_enabled=True, broker_provider='kite')\n"
        "clock = FrozenClock(datetime(2026, 10, 1, 20, 30, tzinfo=IST))\n"
        "context = JobContext(job_name='paper_session', run_id=uuid4(), clock=clock,"
        " settings=settings)\n"
        "default_registry().get('paper_session').fn(context)\n"
        "decisions = sorted({e.decision.value for e in journal.entries})\n"
        "assert 'BUY' in decisions, decisions\n"
        "loaded = sorted(m for m in sys.modules if 'kite' in m.lower())\n"
        "print('decisions', decisions, 'kite', loaded)\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert done.returncode == 0, f"{done.stdout[-1000:]} {done.stderr[-3000:]}"
    assert "'BUY'" in done.stdout


def test_the_broker_provider_setting_is_never_read_on_the_job_path() -> None:
    """``BROKER_PROVIDER=kite`` is how real money would be switched on; this module never asks."""
    reads = [
        node
        for node in ast.walk(ast.parse(_MODULE.read_text()))
        if isinstance(node, ast.Attribute) and node.attr == "broker_provider"
    ]
    assert reads == []


@pytest.mark.parametrize(
    "fn", [run_paper_session, run_paper_session_job, _restore_book], ids=lambda f: f.__name__
)
def test_no_function_on_the_job_path_accepts_a_broker(fn: object) -> None:
    signature = inspect.signature(fn)  # type: ignore[arg-type]
    for name, parameter in signature.parameters.items():
        assert "broker" not in name.lower(), f"{fn!r} takes {name}"
        annotation = str(parameter.annotation)
        assert "Broker" not in annotation, f"{fn!r}.{name} is annotated {annotation}"


def test_the_registered_job_is_the_paper_session_and_takes_only_the_context() -> None:
    job = default_registry().get("paper_session")
    assert list(inspect.signature(job.fn).parameters) == ["context"]
    assert "paper" in job.description.lower()


def test_a_kite_broker_is_refused_by_the_paper_order_path() -> None:
    kite = KiteBroker.__new__(KiteBroker)  # no session, no transport: it must never be used
    assert isinstance(kite, Broker)  # it *would* satisfy the interface — that is the danger
    with pytest.raises(PaperModeViolationError, match="KiteBroker"):
        require_paper_broker(kite)


def test_a_sim_broker_subclass_is_refused_too() -> None:
    class Wrapped(SimBroker):
        pass

    with pytest.raises(PaperModeViolationError):
        require_paper_broker(Wrapped.__new__(Wrapped))


def test_the_paper_broker_cannot_be_built_over_a_real_broker() -> None:
    kite = KiteBroker.__new__(KiteBroker)
    with pytest.raises(PaperModeViolationError):
        _PaperBroker(
            kite,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            clock=None,  # type: ignore[arg-type]
            corporate_actions=None,
            kill_switch=None,  # type: ignore[arg-type]
            alerter=None,  # type: ignore[arg-type]
            book_id="paper_fixture_book",
            sleeve="TACTICAL",
        )
