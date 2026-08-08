"""Task DAG: load TASK_GRAPH.yaml, validate it, compute what is runnable now.

This module knows nothing about agents or state transitions -- it answers structural
questions only. `state.py` owns what has happened; this owns what is possible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
GRAPH_PATH = REPO / "TASK_GRAPH.yaml"

# Classes an agent may execute without human contact. See AGENTIC_CONTEXT.md §4.
AUTONOMOUS = {"AUTO", "GATE_AUDIT"}
# Classes that park (with an escalation) once their dependencies are satisfied.
HUMAN = {"NEEDS_GO", "NEEDS_SECRET", "HUMAN_GATE"}
ALL_AUTONOMY = AUTONOMOUS | HUMAN

MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    milestone: str
    autonomy: str
    deps: tuple[str, ...]
    spec: str
    acceptance: tuple[str, ...]
    verify: str
    module: str = "—"
    deliverables: tuple[str, ...] = ()
    escalation: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    @property
    def is_autonomous(self) -> bool:
        return self.autonomy in AUTONOMOUS


class GraphError(Exception):
    pass


class Graph:
    def __init__(self, path: Path = GRAPH_PATH) -> None:
        self.path = path
        raw = yaml.safe_load(path.read_text())
        self.milestones: dict[str, dict[str, Any]] = raw.get("milestones") or {}
        self.tasks: dict[str, Task] = {}
        for entry in raw.get("tasks") or []:
            task = self._parse(entry)
            if task.id in self.tasks:
                raise GraphError(f"duplicate task id: {task.id}")
            self.tasks[task.id] = task

    @staticmethod
    def _parse(entry: dict[str, Any]) -> Task:
        try:
            return Task(
                id=str(entry["id"]),
                title=str(entry["title"]),
                milestone=str(entry["milestone"]),
                autonomy=str(entry["autonomy"]),
                deps=tuple(entry.get("deps") or ()),
                spec=str(entry.get("spec", "")).strip(),
                acceptance=tuple(entry.get("acceptance") or ()),
                verify=str(entry.get("verify", "")).strip(),
                module=str(entry.get("module", "—")),
                deliverables=tuple(entry.get("deliverables") or ()),
                escalation=dict(entry.get("escalation") or {}),
                notes=str(entry.get("notes", "")).strip(),
            )
        except KeyError as exc:
            raise GraphError(f"task missing required field {exc}: {entry!r}") from exc

    # ── structure ────────────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """Return a list of structural problems. Empty means the graph is sound."""
        problems: list[str] = []
        for task in self.tasks.values():
            if task.autonomy not in ALL_AUTONOMY:
                problems.append(f"{task.id}: unknown autonomy {task.autonomy!r}")
            if task.milestone not in self.milestones:
                problems.append(f"{task.id}: unknown milestone {task.milestone!r}")
            for dep in task.deps:
                if dep not in self.tasks:
                    problems.append(f"{task.id}: dep {dep!r} does not exist")
            if not task.acceptance:
                problems.append(f"{task.id}: no acceptance criteria (unverifiable task)")
            if task.autonomy in AUTONOMOUS and not task.verify:
                problems.append(f"{task.id}: autonomous task with no verify command")
            if task.autonomy in HUMAN and not task.escalation and task.autonomy != "NEEDS_SECRET":
                problems.append(f"{task.id}: human-gated task with no escalation block")
        problems.extend(f"cycle: {' -> '.join(c)}" for c in self._cycles())
        return problems

    def _cycles(self) -> list[list[str]]:
        """Depth-first cycle detection. Returns each cycle found as a node path."""
        WHITE, GREY, BLACK = 0, 1, 2
        color = dict.fromkeys(self.tasks, WHITE)
        found: list[list[str]] = []

        def walk(node: str, path: list[str]) -> None:
            color[node] = GREY
            for dep in self.tasks[node].deps:
                if dep not in self.tasks:
                    continue
                if color[dep] == GREY:
                    found.append([*path, node, dep])
                elif color[dep] == WHITE:
                    walk(dep, [*path, node])
            color[node] = BLACK

        for node in self.tasks:
            if color[node] == WHITE:
                walk(node, [])
        return found

    def dependents(self, task_id: str) -> list[str]:
        return sorted(t.id for t in self.tasks.values() if task_id in t.deps)

    def milestone_tasks(self, milestone: str, include_gates: bool = True) -> list[Task]:
        return [
            t
            for t in self.tasks.values()
            if t.milestone == milestone and (include_gates or t.autonomy != "GATE_AUDIT")
        ]

    def topo_order(self) -> list[str]:
        """Stable topological order; ties broken by task id so waves are reproducible."""
        remaining = {tid: set(t.deps) for tid, t in self.tasks.items()}
        order: list[str] = []
        while remaining:
            free = sorted(tid for tid, deps in remaining.items() if not deps - set(order))
            if not free:  # cycle -- validate() reports it; don't hang here
                order.extend(sorted(remaining))
                break
            order.extend(free)
            for tid in free:
                remaining.pop(tid)
        return order

    # ── runnability ──────────────────────────────────────────────────────────

    def deps_done(self, task: Task, states: dict[str, str]) -> bool:
        return all(states.get(dep) in ("DONE", "SPLIT") for dep in task.deps)

    def ready(self, states: dict[str, str], attempts: dict[str, int]) -> list[Task]:
        """Autonomous tasks whose dependencies are all DONE and which still need work.

        Order follows topo order so a wave picks up foundational work first, which
        matters when concurrency is lower than the ready-set size.
        """
        out: list[Task] = []
        for tid in self.topo_order():
            task = self.tasks[tid]
            if not task.is_autonomous:
                continue
            state = states.get(tid, "PENDING")
            if state in ("DONE", "SPLIT", "PARKED", "IN_PROGRESS"):
                continue
            if state == "FAILED" and attempts.get(tid, 0) >= MAX_ATTEMPTS:
                continue
            if self.deps_done(task, states):
                out.append(task)
        return out

    def parkable(self, states: dict[str, str]) -> list[Task]:
        """Human-gated tasks that have become the actual blocker (deps satisfied)."""
        return [
            t
            for t in self.tasks.values()
            if t.autonomy in HUMAN
            and states.get(t.id, "PENDING") == "PENDING"
            and self.deps_done(t, states)
        ]

    def why_blocked(self, task_id: str, states: dict[str, str], attempts: dict[str, int]) -> str:
        task = self.tasks.get(task_id)
        if task is None:
            return f"{task_id}: no such task"
        state = states.get(task_id, "PENDING")
        if state == "DONE":
            return f"{task_id}: DONE"
        if state == "PARKED":
            return f"{task_id}: PARKED — see HUMAN_DECISIONS.md"
        if state == "IN_PROGRESS":
            return f"{task_id}: an agent is working on it"
        if state == "FAILED" and attempts.get(task_id, 0) >= MAX_ATTEMPTS:
            return f"{task_id}: FAILED {attempts[task_id]}x — auto-parked, needs a human look"
        pending = [d for d in task.deps if states.get(d) not in ("DONE", "SPLIT")]
        if pending:
            detail = ", ".join(f"{d}={states.get(d, 'PENDING')}" for d in pending)
            return f"{task_id}: waiting on {detail}"
        if not task.is_autonomous:
            return f"{task_id}: autonomy={task.autonomy} — reserved to the human"
        return f"{task_id}: ready to run"

    # ── mutation (only via `orch split`) ─────────────────────────────────────

    def append_tasks(self, entries: list[dict[str, Any]]) -> None:
        """Append child tasks to the YAML file as text, preserving the existing document.

        Round-tripping the whole file through yaml would strip its comments, which are
        load-bearing here (they explain the plan mapping to the next agent that reads it).
        """
        for entry in entries:
            if entry["id"] in self.tasks:
                raise GraphError(f"child id already exists: {entry['id']}")
            self.tasks[entry["id"]] = self._parse(entry)
        lines = ["", "# ── appended by `orch split` ──"]
        for entry in entries:
            body = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=100)
            lines.append(body.rstrip("\n"))
        with self.path.open("a") as fh:
            fh.write("\n".join(lines) + "\n")


def runnable(
    graph: Graph,
    states: dict[str, str],
    attempts: dict[str, int],
    cleared: set[str] | None = None,
) -> list[Task]:
    """What the next wave may pick up: autonomous tasks, plus human-gated tasks the owner
    has explicitly cleared with `orch answer`.

    A cleared NEEDS_GO / NEEDS_SECRET / HUMAN_GATE task becomes ordinary work; that is the
    whole mechanism by which a human decision unblocks the build.
    """
    cleared = cleared or set()
    out = graph.ready(states, attempts)
    have = {t.id for t in out}
    for tid in graph.topo_order():
        task = graph.tasks[tid]
        if (
            tid in cleared
            and tid not in have
            and not task.is_autonomous
            and states.get(tid, "PENDING") in ("PENDING", "FAILED")
            and attempts.get(tid, 0) < MAX_ATTEMPTS
            and graph.deps_done(task, states)
        ):
            out.append(task)
    return out


CHILD_FIELD = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|$)", re.S)


def parse_child_spec(spec: str, parent: Task) -> dict[str, Any]:
    """Parse a `--child "id=X title=Y acceptance=A;B verify=cmd"` string.

    Children inherit milestone, module and deps from the parent unless overridden.
    """
    fields = {m.group(1): m.group(2).strip() for m in CHILD_FIELD.finditer(spec.strip())}
    if "id" not in fields or "title" not in fields:
        raise GraphError(f"child spec needs at least id= and title=: {spec!r}")
    entry: dict[str, Any] = {
        "id": fields["id"],
        "title": fields["title"],
        "milestone": fields.get("milestone", parent.milestone),
        "module": fields.get("module", parent.module),
        "autonomy": fields.get("autonomy", parent.autonomy),
        "deps": [d for d in fields.get("deps", ",".join(parent.deps)).split(",") if d],
        "spec": fields.get("spec", f"Split from {parent.id}: {parent.title}.\n{parent.spec}"),
        "acceptance": [a.strip() for a in fields.get("acceptance", "").split(";") if a.strip()]
        or list(parent.acceptance),
        "verify": fields.get("verify", parent.verify),
    }
    if "deliverables" in fields:
        entry["deliverables"] = [d.strip() for d in fields["deliverables"].split(",") if d.strip()]
    return entry
