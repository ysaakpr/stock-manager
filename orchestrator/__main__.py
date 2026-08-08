"""orch — the build orchestrator CLI.

    ./orch validate            structural check on TASK_GRAPH.yaml
    ./orch status              per-milestone progress + what is parked and why
    ./orch ready               exactly what the next wave would pick up
    ./orch why <id>            why a task is not running
    ./orch prompt <id>         print the agent brief for a task
    ./orch set <id> <STATE>    record an outcome (DONE re-verifies before it is accepted)
    ./orch escalate <id> ...   park a task and file a human decision
    ./orch answer <id> ...     record your decision and return the task to the queue
    ./orch split <id> ...      replace a too-large task with children
    ./orch gate <M>            re-run every verification in a milestone
    ./orch run                 the unattended wave loop
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from . import escalate as esc
from .graph import MAX_ATTEMPTS, Graph, GraphError, parse_child_spec, runnable
from .prompts import for_task
from .state import BuildState

REPO = Path(__file__).resolve().parent.parent
VERIFY_TIMEOUT = 3600

GREEN, RED, YELLOW, GREY, BOLD, OFF = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[90m",
    "\033[1m",
    "\033[0m",
)
COLOR = {
    "DONE": GREEN,
    "FAILED": RED,
    "PARKED": YELLOW,
    "IN_PROGRESS": BOLD,
    "PENDING": GREY,
    "SPLIT": GREY,
}


def _run(cmd: str, timeout: int = VERIFY_TIMEOUT) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"verification timed out after {timeout}s: {cmd}"
    return proc.returncode, (proc.stdout + proc.stderr)[-20000:]


def _cleared(st: BuildState) -> set[str]:
    doc = st.load()
    return {tid for tid, rec in doc["tasks"].items() if rec.get("human_cleared")}


# ── commands ─────────────────────────────────────────────────────────────────


def cmd_validate(_args: argparse.Namespace) -> int:
    graph = Graph()
    problems = graph.validate()
    if problems:
        print(f"{RED}{len(problems)} problem(s) in TASK_GRAPH.yaml{OFF}")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"{GREEN}graph OK{OFF}: {len(graph.tasks)} tasks, {len(graph.milestones)} milestones")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    graph, st = Graph(), BuildState()
    doc = st.load()
    states = {tid: rec.get("state", "PENDING") for tid, rec in doc["tasks"].items()}
    attempts = {tid: int(rec.get("attempts", 0)) for tid, rec in doc["tasks"].items()}

    print(f"{BOLD}wave {doc.get('wave', 0)}  ·  {len(graph.tasks)} tasks{OFF}\n")
    for mid, meta in graph.milestones.items():
        tasks = graph.milestone_tasks(mid)
        if not tasks:
            continue
        counts: dict[str, int] = {}
        for t in tasks:
            s = states.get(t.id, "PENDING")
            counts[s] = counts.get(s, 0) + 1
        done = counts.get("DONE", 0)
        bar = "█" * done + "·" * (len(tasks) - done)
        detail = "  ".join(
            f"{COLOR.get(s, '')}{s.lower()} {n}{OFF}" for s, n in sorted(counts.items())
        )
        print(f"  {mid:3} {bar:<16} {done}/{len(tasks)}  {meta['title']}")
        if detail:
            print(f"      {detail}")

    ready = runnable(graph, states, attempts, _cleared(st))
    print(f"\n{BOLD}ready now ({len(ready)}){OFF}")
    for t in ready[:20]:
        print(f"  {t.id:8} {t.title}")
    if len(ready) > 20:
        print(f"  … {len(ready) - 20} more")

    parked = [tid for tid, s in states.items() if s == "PARKED"]
    stuck = [tid for tid, s in states.items() if s == "FAILED" and attempts.get(tid, 0) >= MAX_ATTEMPTS]
    if parked or stuck:
        print(f"\n{YELLOW}blocked on you ({len(parked) + len(stuck)}){OFF}")
        for tid in sorted(parked + stuck):
            task = graph.tasks.get(tid)
            print(f"  {tid:8} {task.title if task else ''}")
        print("  → see HUMAN_DECISIONS.md, then: ./orch answer <id> --decision \"...\"")
    return 0


def cmd_ready(args: argparse.Namespace) -> int:
    graph, st = Graph(), BuildState()
    ready = runnable(graph, st.states(), st.attempts(), _cleared(st))
    if args.json:
        import json

        print(json.dumps([{"id": t.id, "title": t.title, "autonomy": t.autonomy} for t in ready]))
    else:
        for t in ready:
            print(f"{t.id:8} {t.title}")
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    graph, st = Graph(), BuildState()
    print(graph.why_blocked(args.task_id, st.states(), st.attempts()))
    rec = st.record(args.task_id)
    for key in ("reason", "note", "verify_output"):
        if rec.get(key):
            print(f"\n{key}:\n{rec[key]}")
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    graph = Graph()
    task = graph.tasks.get(args.task_id)
    if task is None:
        print(f"no such task: {args.task_id}", file=sys.stderr)
        return 2
    print(for_task(task, graph))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    graph, st = Graph(), BuildState()
    task = graph.tasks.get(args.task_id)
    if task is None:
        print(f"no such task: {args.task_id}", file=sys.stderr)
        return 2

    if args.state != "DONE":
        st.set(args.task_id, args.state, reason=args.reason, note=args.note)
        print(f"{args.task_id} → {args.state}")
        return 0

    # DONE is the one state the agent does not get to assert. We verify it ourselves.
    checks: list[tuple[str, str]] = []
    if task.verify:
        checks.append(("verify", task.verify))
    if (
        task.autonomy == "AUTO"
        and (REPO / "Makefile").exists()
        and "make check" not in task.verify
        and not args.skip_check
    ):
        checks.append(("make check", "make check"))

    transcript: list[str] = []
    for label, cmd in checks:
        print(f"{GREY}running {label}: {cmd}{OFF}")
        code, out = _run(cmd)
        transcript.append(f"$ {cmd}\n(exit {code})\n{out.strip()[-4000:]}")
        if code != 0:
            st.set(
                args.task_id,
                "FAILED",
                reason=f"{label} failed with exit {code}",
                verify_output="\n\n".join(transcript),
                note=args.note,
            )
            print(f"\n{RED}{args.task_id} NOT done — {label} exited {code}{OFF}")
            print(out.strip()[-4000:])
            print(
                f"\n{YELLOW}Recorded FAILED. Fix the cause (not the test) and set DONE again.{OFF}"
            )
            return 1

    commit = None
    if shutil.which("git"):
        code, out = _run("git rev-parse --short HEAD 2>/dev/null || true", timeout=30)
        commit = out.strip() or None
    st.set(
        args.task_id,
        "DONE",
        note=args.note,
        verify_output="\n\n".join(transcript) or "no verify command",
        commit=commit,
    )
    print(f"{GREEN}{args.task_id} → DONE{OFF} ({len(checks)} check(s) passed)")
    unblocked = [
        d
        for d in graph.dependents(args.task_id)
        if graph.deps_done(graph.tasks[d], st.states())
    ]
    if unblocked:
        print(f"unblocked: {', '.join(unblocked)}")
    return 0


def cmd_escalate(args: argparse.Namespace) -> int:
    graph, st = Graph(), BuildState()
    task = graph.tasks.get(args.task_id)
    title = task.title if task else args.task_id
    filed = esc.add(
        args.task_id,
        title=title,
        question=args.question,
        why_blocked=args.why_blocked,
        options=args.options or "",
        recommendation=args.recommendation or "",
        unblocks=args.unblocks or (", ".join(graph.dependents(args.task_id)) if task else ""),
    )
    st.set(args.task_id, "PARKED", reason=args.question)
    print(f"{YELLOW}{args.task_id} → PARKED{OFF}"
          f"{' (decision filed)' if filed else ' (an open decision already exists)'}")
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    st = BuildState()
    found = esc.answer(args.task_id, args.decision)
    doc = st.load()
    rec = doc["tasks"].get(args.task_id, {})
    if rec.get("state") in ("PARKED", "FAILED", "PENDING"):
        # Clear the human gate and put it back in the queue for the next wave.
        st.set(args.task_id, "PENDING", human_cleared=True, note=f"cleared: {args.decision}")
        print(f"{GREEN}{args.task_id} cleared and re-queued{OFF}")
    else:
        st.set(args.task_id, rec.get("state", "PENDING"), human_cleared=True)
        print(f"{args.task_id} marked cleared (state {rec.get('state')})")
    if not found:
        print(f"{GREY}note: no open queue entry found for {args.task_id}{OFF}")
    return 0


def cmd_split(args: argparse.Namespace) -> int:
    graph, st = Graph(), BuildState()
    task = graph.tasks.get(args.task_id)
    if task is None:
        print(f"no such task: {args.task_id}", file=sys.stderr)
        return 2
    try:
        children = [parse_child_spec(spec, task) for spec in args.child]
    except GraphError as exc:
        print(f"{RED}{exc}{OFF}", file=sys.stderr)
        return 2
    graph.append_tasks(children)
    ids = [c["id"] for c in children]
    st.set(args.task_id, "SPLIT", note=f"split into {', '.join(ids)}")
    print(f"{args.task_id} → SPLIT into {', '.join(ids)}")
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    graph, st = Graph(), BuildState()
    states = st.states()
    tasks = [
        t
        for t in graph.milestone_tasks(args.milestone, include_gates=False)
        if "orch gate" not in t.verify and "orchestrator gate" not in t.verify
    ]
    if args.only:
        tasks = [t for t in tasks if args.only.lower() in (t.id + " " + t.title).lower()]
    if not tasks:
        print(f"{RED}no tasks matched for gate {args.milestone}{OFF}")
        return 1

    print(f"{BOLD}gate {args.milestone}{OFF}: re-verifying {len(tasks)} task(s)\n")
    failures: list[str] = []
    for t in tasks:
        state = states.get(t.id, "PENDING")
        if state != "DONE":
            print(f"  {RED}✗{OFF} {t.id:8} state={state} (never completed)")
            failures.append(f"{t.id} state={state}")
            continue
        if not t.verify:
            print(f"  {YELLOW}·{OFF} {t.id:8} no verify command")
            continue
        code, out = _run(t.verify)
        if code == 0:
            print(f"  {GREEN}✓{OFF} {t.id:8} {t.title}")
        else:
            print(f"  {RED}✗{OFF} {t.id:8} {t.title} — exit {code}")
            print("      " + out.strip().splitlines()[-1][:160] if out.strip() else "")
            failures.append(f"{t.id} verify exit {code}")

    if failures:
        print(f"\n{RED}gate {args.milestone}: FAIL{OFF} ({len(failures)})")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\n{GREEN}gate {args.milestone}: all verifications pass{OFF}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .run import run_loop

    return run_loop(
        max_waves=args.max_waves,
        concurrency=args.concurrency,
        permission_mode=args.permission_mode,
        dry_run=args.dry_run,
        model=args.model,
    )


# ── argument plumbing ────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orch", description=__doc__ or "")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    sub.add_parser("status").set_defaults(fn=cmd_status)

    p = sub.add_parser("ready")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ready)

    p = sub.add_parser("why")
    p.add_argument("task_id")
    p.set_defaults(fn=cmd_why)

    p = sub.add_parser("prompt")
    p.add_argument("task_id")
    p.set_defaults(fn=cmd_prompt)

    p = sub.add_parser("set")
    p.add_argument("task_id")
    p.add_argument("state", choices=["DONE", "FAILED", "PARKED", "IN_PROGRESS", "SPLIT", "PENDING"])
    p.add_argument("--note")
    p.add_argument("--reason")
    p.add_argument("--skip-check", action="store_true", help="skip `make check` (needs a reason)")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("escalate")
    p.add_argument("task_id")
    p.add_argument("--question", required=True)
    p.add_argument("--why-blocked", required=True, dest="why_blocked")
    p.add_argument("--options")
    p.add_argument("--recommendation")
    p.add_argument("--unblocks")
    p.set_defaults(fn=cmd_escalate)

    p = sub.add_parser("answer")
    p.add_argument("task_id")
    p.add_argument("--decision", required=True)
    p.set_defaults(fn=cmd_answer)

    p = sub.add_parser("split")
    p.add_argument("task_id")
    p.add_argument("--child", action="append", required=True)
    p.set_defaults(fn=cmd_split)

    p = sub.add_parser("gate")
    p.add_argument("milestone")
    p.add_argument("--only")
    p.set_defaults(fn=cmd_gate)

    p = sub.add_parser("run")
    p.add_argument("--max-waves", type=int, default=40)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument(
        "--permission-mode",
        default="acceptEdits",
        help="claude -p permission mode; use bypassPermissions only for a truly unattended run",
    )
    p.add_argument("--model", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_run)

    args = parser.parse_args(argv)
    try:
        return int(args.fn(args))
    except GraphError as exc:
        print(f"{RED}graph error: {exc}{OFF}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"{RED}{exc}{OFF}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
