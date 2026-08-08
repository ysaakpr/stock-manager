# Runbook — driving the autonomous build

The build is a DAG of 88 tasks in [TASK_GRAPH.yaml](../../TASK_GRAPH.yaml). One fresh agent per task,
`orch` enforcing the definition of done, and a decision queue for the handful of things agents are not
allowed to decide.

## The four files that matter

| File | What it is |
|---|---|
| `EXECUTION_PLAN.md` | The constitution. What to build. Amendments to §1 need your ratification. |
| `AGENTIC_CONTEXT.md` | The charter. How agents build, what they may decide, the hard invariants. |
| `TASK_GRAPH.yaml` | The DAG. Task specs, dependencies, acceptance criteria, verify commands. |
| `HUMAN_DECISIONS.md` | Your inbox. Everything the build is waiting on you for. |

State lives in `BUILD_STATE.json` (machine-owned) and per-task transcripts in `ops/build-logs/`.

## Daily loop

```bash
./orch status            # where the build is, what is blocked on you
./orch run               # keep building
```

When `run` stops, it tells you exactly why — parked decisions, tasks that failed three times, or a
complete graph. Answer what it asks and run again:

```bash
./orch answer M1.13 --decision "Go — run the full backfill overnight"
./orch run
```

## Unattended overnight runs

**Prerequisite:** `./orch run` spawns `claude -p` once per task, so the Claude Code **CLI** must be on
PATH. It is not installed on this machine as of 2026-08-08 — this repo has been driven from the Claude
desktop app, which does not expose the binary. `orch run` fails loudly rather than silently doing nothing.
To enable detached runs:

```bash
npm install -g @anthropic-ai/claude-code
```

Then authenticate the CLI once interactively (`claude` in a terminal) before the first detached run —
a headless `claude -p` cannot complete a login flow. Until then, use the in-session runner below.

Once the CLI is available, agents need Bash without a prompt for each command:

```bash
./orch run --max-waves 40 --concurrency 3 --permission-mode bypassPermissions
```

That removes the per-command gate. The standing guards are then `.claude/settings.json` (allowlist plus a
deny list covering `git push` and every Kite order tool) and the invariants in `AGENTIC_CONTEXT.md` §6.
Read the deny list before you use this flag, and prefer running it while you are asleep rather than away
for days — a wave that goes wrong is cheapest to catch early.

Per-task logs are in `ops/build-logs/wave-NN/<task-id>.log`. Every completed task is one commit, so
`git log --oneline` is the build history and `git revert` is the undo.

## When something is wrong

```bash
./orch why M2.4          # state, last error, captured verify output
./orch gate M2           # re-run every verification in a milestone
```

A task that failed three times auto-parks with its error trail rather than blocking the wave. Fix the
cause and clear it:

```bash
./orch answer M2.4 --decision "fixed the factor convention by hand; retry"
```

`orch` refuses to move a task out of DONE, because downstream tasks have already trusted it. If you
genuinely need to, edit `BUILD_STATE.json` by hand — the friction is deliberate.

## In-session runner (works today, no CLI needed)

`.claude/workflows/build-wave.js` runs the same waves through the Workflow tool, with live progress in
`/workflows` instead of a detached process. Same graph, same state, same `orch`, same commits — the only
difference is that it advances while the session is open.

```
/build-wave            # or: ask for the build-wave workflow with {"maxWaves": N}
```

Each wave claims its tasks with `orch set <id> IN_PROGRESS` before working, so the in-session and detached
runners cannot hand the same task to two agents. Do not run both at once anyway.

## What it will never do on its own

Place a broker order. Ratify a policy for real money. Run a bulk fetch campaign without your go. Spend
money. Accept terms. Touch `data/L0/`. Graduate a case to real capital. Full list:
`AGENTIC_CONTEXT.md` §3.
