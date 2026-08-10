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


def _scoped_paths(task: object) -> list[str]:
    """The task's own files, for a DoD check that does not depend on the rest of the tree.

    Several builder agents share one working tree, so a repo-wide `make check` fails a
    finished task because a *different* agent has a half-written file on disk. A task is
    accountable for its own deliverables; repo-wide green is the gate auditor's job, run
    when the tree is quiet.
    """
    out: set[str] = set()
    for raw in getattr(task, "deliverables", ()):  # type: ignore[arg-type]
        spec = str(raw).rstrip("/")
        matches = (
            list(REPO.glob(spec)) if any(c in spec for c in "*?[") else [REPO / spec]
        )
        for match in matches:
            if not match.exists():
                continue
            # A fixture directory holds CSVs, not modules, and mypy errors out on a
            # directory with no .py in it -- which would fail the task for nothing.
            if match.is_dir() and not any(match.rglob("*.py")):
                continue
            if match.is_dir() or match.suffix == ".py":
                out.add(str(match.relative_to(REPO)))
    return sorted(out)


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
    #
    # Both scans are unconditional and first: every other check below is scoped to the task's own
    # deliverables (or skippable with --skip-check) because a *different* agent's half-written
    # file must not fail *this* task's format/lint/types. A leaked credential has no such excuse —
    # it is a defect regardless of which task's diff it rode in on, `orch set <id> DONE` is the
    # path every agent actually commits through (the agent runs `git commit` itself — allow-listed
    # in .claude/settings.json, and prompts.py:for_task instructs it to — `orch` never commits on
    # anyone's behalf), and invariant #13 (AGENTIC_CONTEXT.md §6) does not carve out an exception
    # for --skip-check.
    #
    # The commit-message scan runs against the commit that ALREADY EXISTS by the time this
    # function is reached: the agent commits, then runs `orch set <id> DONE`, in that order (that
    # ordering is what lets `git rev-parse HEAD` below record the task's own commit at all). A
    # secret this catches is therefore caught one step later than the pre-commit hook it
    # complements, in a commit object that already exists locally — not a leak by itself (nothing
    # here pushes, and `git push` is denied), but AGENTIC_CONTEXT.md §7's "History rewriting to
    # expunge a leak is a §3 human-only decision — park it, never attempt it" means the recovery
    # from a hit here is an escalation, not a clean rewrite. Scanning before the commit exists
    # would need `orch` to own commit creation, which it does not.
    #
    # --disable-filter .../is_line_allowlisted: a `# pragma: allowlist secret` comment is not a
    # review — it has no baseline entry and no diff anyone looks at. Every accepted false positive
    # goes through .secrets.baseline instead (Makefile's `check` target carries the full reason).
    scan_flags = (
        "-n --disable-filter detect_secrets.filters.allowlist.is_line_allowlisted "
        "--baseline .secrets.baseline"
    )
    checks: list[tuple[str, str]] = [
        ("secret scan", f"uv run detect-secrets-hook {scan_flags} $(git ls-files)"),
        (
            "commit message scan",
            # detect-secrets-hook skips /dev/stdin outright (its own is_invalid_file filter), so
            # the message has to land in a real temp file first.
            f'f="$(mktemp)"; git log -1 --format=%B > "$f"; '
            f'uv run detect-secrets-hook {scan_flags} "$f"; rc=$?; rm -f "$f"; exit "$rc"',
        ),
    ]
    if task.verify:
        checks.append(("verify", task.verify))
    scoped = _scoped_paths(task)
    if task.autonomy == "AUTO" and scoped and not args.skip_check:
        joined = " ".join(scoped)
        checks.append(("format", f"uv run ruff format --check {joined}"))
        checks.append(("lint", f"uv run ruff check {joined}"))
        checks.append(("types", f"uv run mypy {joined}"))

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


def cmd_release(_args: argparse.Namespace) -> int:
    """Clear IN_PROGRESS rows abandoned by a killed runner (session limit, crash, Ctrl-C).

    An abandoned claim is invisible to `ready` forever, which looks exactly like a deadlock.
    """
    graph, st = Graph(), BuildState()
    released = st.release_stale(list(graph.tasks))
    if released:
        print(f"released {len(released)}: {', '.join(released)}")
    else:
        print("nothing stale")
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

    sub.add_parser("release").set_defaults(fn=cmd_release)

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
