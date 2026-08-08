"""The unattended wave loop.

One fresh `claude -p` process per task, `concurrency` of them at a time. Fresh process means
fresh context, which is what lets the build run for hours without the orchestrator's own
context growing -- the loop holds task ids and exit codes, never the work.

Stops for exactly two reasons (AGENTIC_CONTEXT.md §1):
  1. Nothing is ready — everything left is parked on a human or waiting on something parked.
  2. `--max-waves` reached (a backstop, not a design goal).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from . import escalate as esc
from .graph import MAX_ATTEMPTS, Graph, Task, runnable
from .prompts import for_task, splitter
from .state import BuildState

REPO = Path(__file__).resolve().parent.parent
LOG_ROOT = REPO / "ops" / "build-logs"
AGENT_TIMEOUT = 5400  # 90 min per task; a task needing longer should have been split

CONTEXT_MARKERS = (
    "prompt is too long",
    "context window",
    "context low",
    "exceeds the maximum",
    "too many tokens",
)

GREEN, RED, YELLOW, GREY, BOLD, OFF = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[90m",
    "\033[1m",
    "\033[0m",
)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def _log(msg: str) -> None:
    print(f"{GREY}[{_stamp()}]{OFF} {msg}", flush=True)


def _spawn(prompt: str, log_path: Path, permission_mode: str, model: str | None) -> tuple[int, str]:
    cmd = ["claude", "-p", prompt, "--permission-mode", permission_mode]
    if model:
        cmd += ["--model", model]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            cmd, cwd=REPO, capture_output=True, text=True, timeout=AGENT_TIMEOUT
        )
        output = proc.stdout + proc.stderr
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = f"agent timed out after {AGENT_TIMEOUT}s\n{exc.stdout or ''}{exc.stderr or ''}"
        code = 124
    log_path.write_text(output)
    return code, output


def _park_human_gated(graph: Graph, st: BuildState) -> list[str]:
    """File decisions for human-gated tasks that have just become the real blocker."""
    parked = []
    states = st.states()
    doc = st.load()
    for task in graph.parkable(states):
        if doc["tasks"].get(task.id, {}).get("human_cleared"):
            continue
        e = task.escalation or {}
        esc.add(
            task.id,
            title=task.title,
            question=e.get("question")
            or f"{task.title}: this task is {task.autonomy} and needs your decision.",
            why_blocked=e.get("why_blocked")
            or f"autonomy={task.autonomy} — reserved to the human by AGENTIC_CONTEXT §3.",
            options=e.get("options", ""),
            recommendation=e.get("recommendation", ""),
            unblocks=", ".join(e.get("unblocks") or graph.dependents(task.id)),
        )
        st.set(task.id, "PARKED", reason=e.get("question", f"autonomy={task.autonomy}"))
        parked.append(task.id)
    return parked


def _execute(
    task: Task,
    graph: Graph,
    st: BuildState,
    wave: int,
    permission_mode: str,
    model: str | None,
) -> str:
    """Run one task to a recorded outcome. Returns the final state."""
    st.set(task.id, "IN_PROGRESS", agent=f"wave{wave}")
    log_path = LOG_ROOT / f"wave-{wave:02d}" / f"{task.id}.log"
    started = time.monotonic()
    _log(f"{BOLD}▶ {task.id}{OFF} {task.title}")

    code, output = _spawn(for_task(task, graph), log_path, permission_mode, model)
    elapsed = int(time.monotonic() - started)
    state = st.record(task.id).get("state", "IN_PROGRESS")

    if state == "IN_PROGRESS":
        # The agent finished without recording an outcome. Treat as a failure rather than
        # leaving the task invisible to the next wave.
        low = output.lower()
        if any(marker in low for marker in CONTEXT_MARKERS):
            _log(f"{YELLOW}⤢ {task.id} ran out of context — splitting{OFF}")
            st.set(task.id, "FAILED", reason="context exceeded; splitting")
            split_log = LOG_ROOT / f"wave-{wave:02d}" / f"{task.id}.split.log"
            _spawn(splitter(task), split_log, permission_mode, model)
            return st.record(task.id).get("state", "FAILED")
        st.set(
            task.id,
            "FAILED",
            reason=f"agent exited (code {code}) without recording an outcome",
            verify_output=output[-4000:],
        )
        state = "FAILED"

    marks = {"DONE": f"{GREEN}✓{OFF}", "FAILED": f"{RED}✗{OFF}", "PARKED": f"{YELLOW}⏸{OFF}"}
    _log(f"{marks.get(state, '·')} {task.id} → {state} ({elapsed}s, log: {log_path.relative_to(REPO)})")
    return state


def _stop_report(graph: Graph, st: BuildState, wave: int) -> None:
    states = st.states()
    attempts = st.attempts()
    done = sum(1 for s in states.values() if s == "DONE")
    print(f"\n{BOLD}{'─' * 70}{OFF}")
    print(f"{BOLD}Build stopped after {wave} wave(s){OFF} — {done}/{len(graph.tasks)} tasks DONE\n")

    parked = [t for t in graph.tasks.values() if states.get(t.id) == "PARKED"]
    stuck = [
        t
        for t in graph.tasks.values()
        if states.get(t.id) == "FAILED" and attempts.get(t.id, 0) >= MAX_ATTEMPTS
    ]
    waiting = [
        t
        for t in graph.tasks.values()
        if states.get(t.id, "PENDING") == "PENDING" and not graph.deps_done(t, states)
    ]

    if parked:
        print(f"{YELLOW}Waiting on your decision ({len(parked)}){OFF} — see HUMAN_DECISIONS.md")
        for t in parked:
            print(f"  {t.id:8} {t.title}")
            print(f"           {GREY}{st.record(t.id).get('reason', '')}{OFF}")
    if stuck:
        print(f"\n{RED}Failed {MAX_ATTEMPTS}x, needs your eyes ({len(stuck)}){OFF}")
        for t in stuck:
            print(f"  {t.id:8} {t.title}")
            print(f"           {GREY}{st.record(t.id).get('reason', '')}{OFF}")
            print(f"           ./orch why {t.id}")
    if waiting:
        print(f"\n{GREY}Downstream of the above ({len(waiting)} tasks){OFF}")

    if parked or stuck:
        print('\n  Unblock with:  ./orch answer <task-id> --decision "..."')
        print("  Then resume:   ./orch run")
    else:
        print(f"{GREEN}Nothing is blocked and nothing is ready — the graph is complete.{OFF}")


def run_loop(
    *,
    max_waves: int = 40,
    concurrency: int = 3,
    permission_mode: str = "acceptEdits",
    dry_run: bool = False,
    model: str | None = None,
) -> int:
    graph, st = Graph(), BuildState()

    problems = graph.validate()
    if problems:
        print(f"{RED}TASK_GRAPH.yaml has {len(problems)} problem(s); fix before running:{OFF}")
        for p in problems:
            print(f"  - {p}")
        return 2

    if not shutil.which("claude") and not dry_run:
        print(f"{RED}`claude` CLI not on PATH — the runner spawns it per task.{OFF}", file=sys.stderr)
        return 2

    released = st.release_stale(list(graph.tasks))
    if released:
        _log(f"released {len(released)} stale IN_PROGRESS task(s): {', '.join(released)}")

    wave = 0
    while wave < max_waves:
        newly_parked = _park_human_gated(graph, st)
        if newly_parked:
            _log(f"{YELLOW}parked (human decision): {', '.join(newly_parked)}{OFF}")

        ready = runnable(
            graph,
            st.states(),
            st.attempts(),
            {tid for tid, r in st.load()["tasks"].items() if r.get("human_cleared")},
        )
        if not ready:
            _stop_report(graph, st, wave)
            return 0

        wave = st.bump_wave()
        _log(f"{BOLD}── wave {wave}: {len(ready)} task(s), {concurrency} at a time ──{OFF}")
        for t in ready:
            _log(f"   queued {t.id:8} {t.title}")

        if dry_run:
            _log(f"dry run — nothing spawned. `./orch run` would start these {len(ready)} now.")
            return 0

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(
                pool.map(
                    lambda t: _execute(t, graph, st, wave, permission_mode, model),
                    ready,
                )
            )

    _log(f"{YELLOW}hit --max-waves {max_waves}{OFF}")
    _stop_report(graph, st, wave)
    return 0
