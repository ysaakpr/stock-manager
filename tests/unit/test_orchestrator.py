"""M13.5: the build orchestrator's state transitions and scheduling.

`orchestrator/` decides what gets built and by whom, so a wrong transition does not fail loudly:
it re-runs finished work, deadlocks a wave, or puts two builders on one task. These tests pin the
transitions in `state.py`, the runnability rules in `graph.py`, and the EXTERNAL claim that keeps
`./orch run` from launching a duplicate builder for a task someone outside it is already building.

Every test works on a throwaway graph, state file and lock in `tmp_path`; nothing reads or writes
the repo's own BUILD_STATE.json or TASK_GRAPH.yaml.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest
import yaml

from orchestrator import __main__ as cli
from orchestrator import state as state_mod
from orchestrator.graph import MAX_ATTEMPTS, Graph, runnable
from orchestrator.state import BuildState

GRAPH = {
    "milestones": {"M1": {"title": "test milestone"}},
    "tasks": [
        {
            "id": "A",
            "title": "root",
            "milestone": "M1",
            "autonomy": "AUTO",
            "deps": [],
            "acceptance": ["a"],
            "verify": "true",
        },
        {
            "id": "B",
            "title": "depends on A",
            "milestone": "M1",
            "autonomy": "AUTO",
            "deps": ["A"],
            "acceptance": ["b"],
            "verify": "true",
        },
        {
            "id": "H",
            "title": "human gated",
            "milestone": "M1",
            "autonomy": "NEEDS_GO",
            "deps": [],
            "acceptance": ["h"],
            "verify": "true",
            "escalation": {"question": "go?"},
        },
    ],
}


@pytest.fixture
def graph_path(tmp_path: Path) -> Path:
    path = tmp_path / "TASK_GRAPH.yaml"
    path.write_text(yaml.safe_dump(GRAPH, sort_keys=False))
    return path


@pytest.fixture
def st(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BuildState:
    """A BuildState on a tmp file, with the lock moved off the repo's own lock file."""
    monkeypatch.setattr(state_mod, "LOCK_PATH", tmp_path / ".build_state.lock")
    return BuildState(tmp_path / "BUILD_STATE.json")


@pytest.fixture
def graph(graph_path: Path) -> Graph:
    return Graph(graph_path)


def _ready_ids(graph: Graph, st: BuildState, cleared: set[str] | None = None) -> list[str]:
    return [t.id for t in runnable(graph, st.states(), st.attempts(), cleared)]


# ── graph structure ──────────────────────────────────────────────────────────


def test_graph_validates_and_orders_dependencies_first(graph: Graph) -> None:
    assert graph.validate() == []
    order = graph.topo_order()
    assert order.index("A") < order.index("B")


def test_validate_reports_a_cycle(tmp_path: Path) -> None:
    doc = json.loads(json.dumps(GRAPH))
    doc["tasks"][0]["deps"] = ["B"]
    path = tmp_path / "cyclic.yaml"
    path.write_text(yaml.safe_dump(doc))
    assert any(p.startswith("cycle:") for p in Graph(path).validate())


# ── state transitions ────────────────────────────────────────────────────────


def test_unknown_state_is_refused(st: BuildState) -> None:
    with pytest.raises(ValueError, match="invalid state"):
        st.set("A", "BOGUS")


def test_in_progress_counts_an_attempt_and_failed_stamps_finished(st: BuildState) -> None:
    st.set("A", "IN_PROGRESS")
    rec = st.set("A", "FAILED", reason="boom")
    assert rec["attempts"] == 1
    assert rec["previous_state"] == "IN_PROGRESS"
    assert rec["reason"] == "boom"
    assert "finished" in rec
    st.set("A", "IN_PROGRESS")
    assert st.attempts()["A"] == 2


def test_done_cannot_regress(st: BuildState) -> None:
    st.set("A", "DONE")
    for target in ("PENDING", "FAILED", "IN_PROGRESS", "EXTERNAL"):
        with pytest.raises(ValueError, match="is DONE"):
            st.set("A", target)
    st.set("A", "SPLIT")  # the one sanctioned exit from DONE


def test_verify_output_is_truncated(st: BuildState) -> None:
    rec = st.set("A", "FAILED", verify_output="x" * (state_mod.VERIFY_OUTPUT_LIMIT + 50))
    assert len(rec["verify_output"]) < state_mod.VERIFY_OUTPUT_LIMIT + 50
    assert rec["verify_output"].endswith("[truncated]")


def test_state_survives_a_reload(st: BuildState) -> None:
    st.set("A", "PARKED", reason="waiting")
    reloaded = BuildState(st.path)
    assert reloaded.states() == {"A": "PARKED"}
    assert reloaded.record("missing") == {"state": "PENDING", "attempts": 0}


# ── release_stale and runnability ────────────────────────────────────────────


def test_release_stale_fails_in_progress_and_it_becomes_runnable_again(
    graph: Graph, st: BuildState
) -> None:
    st.set("A", "IN_PROGRESS")
    assert "A" not in _ready_ids(graph, st)
    assert st.release_stale(list(graph.tasks)) == ["A"]
    assert st.states()["A"] == "FAILED"
    assert "A" in _ready_ids(graph, st)


def test_failed_past_max_attempts_is_not_runnable(graph: Graph, st: BuildState) -> None:
    for _ in range(MAX_ATTEMPTS):
        st.set("A", "IN_PROGRESS")
        st.set("A", "FAILED")
    assert "A" not in _ready_ids(graph, st)


def test_dependents_wait_for_done(graph: Graph, st: BuildState) -> None:
    assert _ready_ids(graph, st) == ["A"]
    st.set("A", "DONE")
    assert _ready_ids(graph, st) == ["B"]


def test_human_gated_task_runs_only_once_cleared(graph: Graph, st: BuildState) -> None:
    assert "H" not in _ready_ids(graph, st)
    assert [t.id for t in graph.parkable(st.states())] == ["H"]
    assert "H" in _ready_ids(graph, st, cleared={"H"})


# ── EXTERNAL: a task built outside the orchestrator ─────────────────────────


def test_external_survives_release_stale(graph: Graph, st: BuildState) -> None:
    st.set("A", "EXTERNAL", note="polly worker")
    st.set("B", "IN_PROGRESS")
    assert st.release_stale(list(graph.tasks)) == ["B"]
    assert st.states()["A"] == "EXTERNAL"
    assert st.record("A")["note"] == "polly worker"


def test_external_is_never_runnable(graph: Graph, st: BuildState) -> None:
    st.set("A", "EXTERNAL")
    st.set("H", "EXTERNAL")
    assert _ready_ids(graph, st, cleared={"A", "H"}) == []
    # And it does not satisfy a dependency: B still waits.
    assert graph.why_blocked("B", st.states(), st.attempts()) == "B: waiting on A=EXTERNAL"
    assert "outside the orchestrator" in graph.why_blocked("A", st.states(), st.attempts())


def test_external_does_not_spend_an_attempt(st: BuildState) -> None:
    rec = st.set("A", "EXTERNAL")
    assert rec["attempts"] == 0
    assert "external_since" in rec


def test_external_hands_back_with_pending(graph: Graph, st: BuildState) -> None:
    st.set("A", "EXTERNAL")
    st.set("A", "PENDING")
    assert _ready_ids(graph, st) == ["A"]


# ── the CLI path: `./orch set <id> EXTERNAL`, then `./orch set <id> DONE` ────


@pytest.fixture
def cli_env(
    graph_path: Path, st: BuildState, monkeypatch: pytest.MonkeyPatch
) -> tuple[Graph, BuildState]:
    """Point the CLI's Graph() and BuildState() at the tmp graph and state."""
    monkeypatch.setattr(cli, "Graph", functools.partial(Graph, graph_path))
    monkeypatch.setattr(cli, "BuildState", functools.partial(BuildState, st.path))
    return Graph(graph_path), st


def test_cli_set_external_then_done(
    cli_env: tuple[Graph, BuildState], capsys: pytest.CaptureFixture[str]
) -> None:
    graph, st = cli_env
    assert cli.main(["set", "A", "EXTERNAL", "--note", "polly/orch-gate"]) == 0
    assert st.states()["A"] == "EXTERNAL"

    assert cli.main(["release"]) == 0
    assert "nothing stale" in capsys.readouterr().out
    assert st.states()["A"] == "EXTERNAL"
    assert "A" not in _ready_ids(graph, st)

    # DONE from EXTERNAL still goes through verification (`true` here) and is accepted.
    assert cli.main(["set", "A", "DONE", "--skip-check"]) == 0
    rec = st.record("A")
    assert rec["state"] == "DONE"
    assert rec["previous_state"] == "EXTERNAL"
    assert "$ true" in rec["verify_output"]
    assert _ready_ids(graph, st) == ["B"]


def test_cli_status_lists_external(
    cli_env: tuple[Graph, BuildState], capsys: pytest.CaptureFixture[str]
) -> None:
    _, st = cli_env
    st.set("A", "EXTERNAL")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "built outside the orchestrator (1)" in out
