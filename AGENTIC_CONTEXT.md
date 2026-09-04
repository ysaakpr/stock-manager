# Agentic Execution Context

**Version:** 1.0 · **Date:** 2026-08-08 · **Governs:** autonomous build of [EXECUTION_PLAN.md](EXECUTION_PLAN.md)

This is the operating charter for every autonomous agent that builds this system. `EXECUTION_PLAN.md` says
*what* to build and is the constitution. This document says *how agents are allowed to build it*, *when they
must stop*, and *what has already been decided so they never have to ask*.

Read order for any agent starting a task: `CLAUDE.md` (conventions, auto-loaded) → this file → the task's
entry in `TASK_GRAPH.yaml` → the referenced `EXECUTION_PLAN.md` sections.

---

## 1. The autonomy contract

> Build continuously. Stop only when the work itself is genuinely blocked on a human, or when this task no
> longer fits in one agent's context. Never stop because something was merely hard, ambiguous at the margins,
> or unpleasant.

Concretely, an agent has exactly three legal exits from a task:

| Exit | State written | When |
|---|---|---|
| **DONE** | `DONE` | Every acceptance criterion in the task entry verifiably passes |
| **PARKED** | `PARKED` + an entry in `HUMAN_DECISIONS.md` | A decision is reserved to the human by §3, or a credential/authorization that does not exist is required |
| **SPLIT** | `SPLIT` + child tasks appended to the graph | The task is too large for one context; the agent decomposes rather than degrades |

`FAILED` is not an exit — it is a retry state. Three failed attempts on the same task auto-converts to
`PARKED` with the accumulated errors as the decision brief, so the wave never deadlocks on a stuck task.

**Anti-perfectionism rule (from decision #14/#15):** a gate is passed on *acceptance*, not polish. If a task's
acceptance criteria pass, ship it and move on. Do not refactor adjacent code, do not add unrequested features,
do not gold-plate. Improvements that are genuinely warranted go in `ops/BACKLOG.md`, not into the current task.

---

## 2. Ratified build decisions

These four were gaps in the plan and were ratified by the owner on 2026-08-08. Agents treat them as settled.

| # | Gap | Ratified answer |
|---|---|---|
| **B1** | How much live fetching may agents do unattended? | **Verify + sample, then full go.** Agents verify every Source Register URL pattern for real, fetch a ~60-session fixture spread across all format eras, and build the complete backfill runner with resume/checkpoint. The 10-year backfill execution itself is `NEEDS_GO` — parked for a one-word human go. |
| **B2** | What are §4.3's "two independent references" for the golden CA suite? | **Reference A:** `yfinance` adjusted closes for the `.NS` ticker. **Reference B:** closes recomputed by hand from published CA terms, checked into the repo as literal expected values. Independent *in method*, so a shared-direction error in third-party adjusted series cannot pass the suite. |
| **B3** | Python 3.12+ is absent from the host (3.9.6 only) | **uv**, installed (0.12.3). `uv` owns a pinned 3.12 venv and `uv.lock`. Postgres stays in docker-compose. Host Python 3.9 is never used. |
| **B4** | Runtime credentials | **None exist yet.** No Anthropic key, no Kite key. Both the LLM provider and the broker sit behind interfaces with recorded/stub implementations. Every task needing a real key is `NEEDS_SECRET` and parks. This is a build constraint, not a design change — `SimBroker`/`StubLLM` must satisfy the *same* interface the real ones will. |

Derived engineering decisions made under §1's authority (recorded here so they are not re-litigated):

- **B5 — Repo root.** This directory *is* `trading-platform/`. Packages (`platform/`, `analyst/`, `execution/`,
  `backtest/`, `accounting/`, `ops/`) live at the root per §8.2; no redundant nesting level.
- **B6 — Migrations.** Plain numbered SQL files in `platform/store/migrations/NNNN_name.sql` plus a ~50-line
  runner, not Alembic. Boring tech (#13); the schema is small and append-only-ish.
- **B7 — Git discipline. THE REPO IS PUBLIC.** `origin` is `git@github.com:ysaakpr/stock-manager.git`
  and its visibility is **public**. Every commit you make is world-readable the moment it is pushed, and
  the full history — all of it, not a squashed snapshot — is already published. An earlier revision of
  this line said "local repo, no remote"; that was **false**, and every agent that trusted it was
  building under the wrong threat model. Write every file, commit message, fixture, log line and
  docstring as if a stranger is reading it, because one can. Deleting a secret in a later commit does
  **not** unpublish it: anything committed and pushed is compromised and must be rotated at the
  provider (invariant #13, `ops/runbooks/secret-leak.md`).
  One commit per completed task, message `[<task-id>] <title>` with a `Task:`/`Acceptance:` trailer.
  Commits are the rollback unit for a bad autonomous wave. Never `--force`, never rewrite history,
  never commit `data/` or `.env`. History rewriting to expunge a leak is a §3 human-only decision —
  park it, never attempt it.
- **B8 — Test fixtures are checked in.** Every source parser has frozen sample files per format era under
  `tests/fixtures/<source>/<era>/`. Live network is *never* touched by the test suite; ingestion tests run
  offline and deterministically in CI.
- **B9 — The reference case is a fixture.** `tests/fixtures/cases/ai_robotics.yaml` encodes the §5.2 example
  case (₹10k/mo on the 1st, 5 yr, NIFTY-TRI + IT/CPSE blend, dial 30%, rails 15%/35%/8/−25%, staged exit,
  LIQUIDCASE parking, T2 monthly) pre-marked `RATIFIED_FIXTURE`. It exists so M5 can be built and self-tested
  without blocking on a live ratification. A fixture ratification is **never** valid for real money — the
  real-money path requires a genuine human ratification record.
- **B10 — Time is injected.** No module calls `datetime.now()` directly; a `Clock` is injected. Non-negotiable
  for replay determinism (§8.3.3) and PIT correctness (§8.3.6).

---

## 3. Reserved to the human — park, never decide

An agent that reaches any of these writes the escalation and moves to the next ready task.

1. **Amendments to `EXECUTION_PLAN.md` §1 (the 15 decisions).** Agents may append to §12's Amendment Log only
   as a *proposal* clearly marked `PROPOSED`, never as a ratified change.
2. **Policy ratification for any real-money case** — break conditions, exit menu, rails, rotation dial, cash
   policy. Governance model: agent proposes, human ratifies (#4, #5, #9). Fixture ratification (B9) is
   paper/test-only.
3. **The 10-year backfill go** (B1) and any other bulk-fetch campaign over ~200 requests to one source.
4. **Anything requiring a credential that does not exist** (B4).
5. **Placing, modifying, or cancelling a real broker order** — including tiny-capital M8 test orders. Agents
   build, unit-test and dry-run `KiteBroker`; a human fires the first real order.
6. **Graduation from paper to real money** (#8). Agents generate the evidence pack; the human flips the switch.
7. **Accepting third-party terms, creating accounts, entering credentials anywhere.**
8. **Anything with legal exposure** — the exchange data-redistribution license question (§10), SEBI RA/RIA
   path, publishing archives externally. Agents may *research and write a memo*; they may not act on it.
9. **Spending money** — paid data sources, paid API tiers, cloud resources.
10. **Deleting or rewriting L0.** L0 is immutable (§4.2). No exceptions, no "cleanup", no "it was corrupt".

Escalation is a single command; it is designed to be cheap so agents prefer it over guessing:

```bash
./orch escalate <task-id> \
  --question "One sentence, decidable as written." \
  --why-blocked "What the agent tried and why no default is safe." \
  --options "A: ... | B: ... | C: ..." \
  --recommendation "A, because ..."
```

Escalations that a careful colleague would just decide are a defect. Before escalating, ask: *would a
competent engineer with this plan in hand stop and email the owner about this?* If no — decide it, record the
assumption in the task's `notes` and in the commit body, and continue.

---

## 4. Autonomy classes

Every task in `TASK_GRAPH.yaml` carries one.

| Class | Meaning |
|---|---|
| `AUTO` | Build it. No human contact. The default and the overwhelming majority. |
| `NEEDS_GO` | Fully built and dry-run-verified by agents; *execution* needs a human go (B1). |
| `NEEDS_SECRET` | Blocked only by a missing credential (B4). Build everything behind the interface; park the live-exercise step. |
| `HUMAN_GATE` | Irreducibly human — ratification, graduation, real orders, legal. Agents prepare the decision packet and park. |
| `GATE_AUDIT` | Machine-verifiable milestone audit. Runs every acceptance criterion of the milestone and reports pass/fail per box; may not mark a gate passed on partial evidence. |

---

## 5. Definition of Done

A task is `DONE` only when **all** hold:

1. Every acceptance criterion in the task entry demonstrably passes.
2. The task's `verify` command exits 0, and its output is captured into the state record.
3. **The full gate is green, repo-wide** — secret scan, format, lint, types, and the tests. `orch
   set <id> DONE` runs it and refuses the transition when it fails (D11).

   *Amended 2026-08-10; corrected the same day.* This rule previously mandated **scoped** checks,
   on the premise that "several builder agents share one working tree, so a repo-wide check fails a
   finished task because a *different* agent has a half-written file on disk."

   **The correction:** the first version of this amendment asserted that builders "now run in
   per-agent git worktrees". That was **false for `orch run`** and I should not have written it.
   There is no worktree support anywhere in `orchestrator/`; `_spawn` passes `cwd=REPO` and
   concurrency defaults to 3. It was true only of the separately-driven sub-agents that repaired
   Wave A. A review demonstrated the consequence: a peer's untracked half-written file failed an
   innocent task's gate three times at the `format` step, auto-parking it and emptying the ready
   queue — a livelock.

   **So the repo-wide gate REQUIRES worktree isolation per builder, and that isolation is a
   prerequisite, not an assumption.** Until `orch` spawns each builder in its own worktree, a
   repo-wide gate under concurrency > 1 can park a task for someone else's mess. The old scoped
   rule was not wrong about the hazard — it was wrong about the remedy, because it bought
   isolation by giving up the tests.
   The old rule also let "DONE" mean "compiles and is well formatted" — `orch` never ran pytest at
   all — which is exactly how four tasks (C.3, M0.4, M1.4, M1.11) came to be recorded DONE with
   `reason: "make check failed with exit 2"`.

   A scoped format/lint/types pass still runs **first**, as a fast fail, so you learn immediately
   whether the broken file is one of yours. It is a convenience, not the gate.

   The gate is defined exactly once, in `orchestrator/checks.py`. `make check` and `orch` both
   call it; they cannot drift, because there is nothing left to drift from.
4. New behaviour has tests. Ingestion parsers have era fixtures (B8); anything touching money, adjustment
   factors, rails, or PIT boundaries has tests that fail if the logic is reversed.
5. The secret scan passes and the diff introduces no credential-shaped literal — no API key, token,
   password, DSN with an embedded password, or private key, in code, fixture, config or commit message
   (invariant #13). No `data/`, no large binaries in the commit.
6. Docstring or `ops/runbooks/` entry for anything an operator must run or recover.
7. Commits on the task's own branch, every message prefixed `[<task-id>]` per B7.

   *Amended 2026-08-10.* This previously read "one commit". It now reads *commits*, plural, and
   deliberately: §7 requires committing each coherent green piece as you reach it, because the
   runner has died mid-task seven times and taken uncommitted trees with it. A task that lands as
   five well-named commits is not untidy — it is the recoverable outcome, and the one-commit rule
   was quietly buying crash-fragility in exchange for a tidy log. Squashing is the merger's option
   at merge time, never a reason to delay committing.

Reporting a task DONE whose verify command did not pass is the single worst failure mode in this system —
it poisons every downstream task that trusts it. Report the failure instead; `FAILED` costs one retry,
a false `DONE` costs a milestone.

---

## 6. Hard invariants

Deterministic properties the codebase must always have. Any agent may reject a change that breaks one, and
the verifier must fail the task.

1. **L0 is immutable.** Write-once, checksummed, never edited, never deleted. Every L1/L2 value re-derivable
   from L0 alone.
2. **Nothing joins on a raw symbol, ever.** ISIN via the identity master (D2) is the only join key.
3. **No adjusted prices are stored in L1.** Adjusted series are derived on read or materialized into L2, and
   are always recomputable from raw + factors.
4. **One cost model.** `SimBroker` and the backtest import the *same* module. Two implementations of Indian
   transaction costs is a defect by construction.
5. **One decision code path.** Paper and real money differ only by which `Broker` implementation is injected.
   No `if paper:` branch anywhere in `analyst/`.
6. **Rails cannot be bypassed.** Every order from A5/A6/A7 passes A8. Rails are deterministic code, never an
   LLM judgment, and the agent has no override.
7. **No data with `knowable_date > decision_date` reaches a decision.** Enforced in the query layer and
   asserted by the PIT leak test.
8. **Restated fundamentals are unreachable from backtests.** Screener-derived restated data is monitoring-only,
   physically quarantined (#7, §7).
9. **Every decision is journaled, including no-ops.** A day with nothing to do still writes a heartbeat with
   the evidence considered. The journal is the product (§0).
10. **Bad data never becomes decisions.** The daily loop reads `/status/sync` first; not-green means
    `SKIPPED_DATA_RED` in the journal and no trading (§4.4).
11. **The clock is injected** (B10). Replay is byte-for-byte reproducible.
12. **Append-only means append-only.** Journal and amendment log are never updated or deleted in place.
13. **A secret never enters the repo, a log, or an artifact.** The repo is public (B7), so this is the one
    invariant whose breach cannot be undone by a later commit.
    - **Where a secret may live:** process environment and the untracked `.env` / `ops/.env`. Nowhere else
      — not source, YAML, JSON, SQL migration, test fixture, runbook, commit message, or `data/`.
    - **Every credential-bearing setting is a `SecretStr`**, including connection strings that embed a
      password (a Postgres DSN *is* a credential). If it can authenticate, it is a secret.
    - **No secret reaches a log, the status API, or the journal.** The journal is append-only (#12), so a
      secret written there can never be redacted — it is permanent. Never log a whole `Settings` object,
      never print the environment, never enable frame-locals in a traceback renderer, and scrub
      credential-bearing URLs before any error string is logged.
    - **Never interpolate a secret into a URL, a subprocess argv, or an exception message.** A token in a
      URL path leaks through every library that quotes the URL back at you when a request fails.
    - **Checked-in fixtures (B8) must be credential-free.** A recorder that captures request headers must
      strip authentication before anything is written under `tests/fixtures/`.
    - **`BUILD_STATE.json` is tracked and therefore published** (D5, commit `64651a4`). It is written by
      machines, not reviewed by eyes, and every push publishes it. A task's recorded `reason` or `note` is
      often a raw error string — scrub it. Never let a DSN, token, argv, or captured response body reach
      build state, and never widen it to hold configuration that could carry a credential.
    - **A leaked key is rotated first and argued about second.** See `ops/runbooks/secret-leak.md`.

---

## 7. Failure and context handling

**Task fails verify.** Read the actual error. Fix the cause, not the test. Re-run. Attempt 2 and 3 are
allowed; on attempt 3's failure the task auto-parks with the full error trail — do not silently weaken the
acceptance criteria to get green.

**Never weaken a test to pass a task.** Deleting an assertion, adding `pytest.mark.skip`, loosening a
tolerance, or narrowing a fixture to dodge a failure is a defect that must be reported, not a fix. If a test
is genuinely wrong, say so explicitly in the state record and escalate — a wrong golden-suite expectation is
exactly the kind of thing the human wants to see.

**Context exhaustion.** If a task cannot fit — too many files, too much history to read, scope discovered to
be 3× the estimate — do not thrash. Stop, and split:

```bash
./orch split <task-id> \
  --child "id=<parent>.a title=... acceptance=A;B verify=<cmd>" \
  --child "id=<parent>.b title=... acceptance=C;D verify=<cmd>"
```

Children inherit the parent's deps and milestone; the parent becomes a no-op container that completes when
its children do. Splitting early is cheap and correct. Degrading quality to squeeze a too-large task into one
context is the failure this rule exists to prevent.

**Commit early, commit often — the runner will die under you.** This is not style advice. The runner has
exited mid-task at least seven times (M5.3, M5.4 and M6.1 on 2026-08-08; four more sessions on 2026-08-10),
and every time it took the *uncommitted* working tree with it. That is the whole story behind
`60874b6 "first commit"`: an abandoned tree, hand-committed later under a message that breaks the
`[<task-id>]` convention, carrying 1,316 lines whose declared tests had never been written.

So: **commit each coherent green piece as you reach it, on your own branch, before moving to the next.**
Do not save a single commit for the end of the task. A crash must cost you the last few minutes, not the
whole task. Intermediate commits use the same `[<task-id>] <title>` convention; a task that ends up with
five commits instead of one is not untidy, it is recoverable.

Corollary for whoever inherits a crashed task: **the working tree left behind is untrusted WIP.** It was
never run to green and no one reviewed it. Read it, judge it against the acceptance criteria, and keep or
discard it on the merits. Never assume it was nearly done.

**A dependency turns out to be wrong or missing.** Do not work around it silently. If the fix is small and
inside your task's blast radius, fix it and note it. If it is not, park with `--why-blocked` naming the
upstream task; the orchestrator will surface it as a graph defect rather than a human decision.

---

## 8. Source Register discipline

`EXECUTION_PLAN.md` §4.1 marks rows `VERIFIED` or `VERIFY-AT-BUILD`. Verification results confirmed on
2026-08-08 from this host (browser UA + `Referer: https://www.nseindia.com/`, no session cookie needed):

| Pattern | Result |
|---|---|
| `nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260807_F_0000.csv.zip` | `200`, 194,665 B |
| `nsearchives.nseindia.com/content/historical/EQUITIES/2024/JAN/cm02JAN2024bhav.csv.zip` | `200`, 100,665 B |
| `nsearchives.nseindia.com/products/content/sec_bhavdata_full_07082026.csv` | `200`, 374,452 B |

Rules for agents touching sources:

- A `VERIFY-AT-BUILD` row is flipped to `VERIFIED` only after a real fetch succeeded, the response was parsed,
  and a fixture was frozen. Record the exact URL, date tested, status, size, and content-type in
  `platform/ingest/source_register.yaml` — the machine-readable register that mirrors §4.1.
- Respect `robots.txt` per source. Screener: no `?page=`, no `?q=`, no `/user/*`; per-company pages and Excel
  exports only, at per-company cadence.
- 2–3 s minimum spacing per source, exponential backoff, **hard stop and alert on a 403 spike** — do not
  attempt to evade anti-bot measures. A blocked source is a status-API-visible failure and possibly an
  escalation, never something to route around.
- Fetch once, keep forever: everything lands in L0 before parsing, so a source going dark never costs history.

---

## 9. What the runners do

Two runners consume the same graph, state and agent prompts.

```bash
./orch status                 # per-milestone progress, what is ready, what is blocked on you
./orch ready                  # exactly what the next wave would pick up
./orch why <task-id>          # why a task is not running, with its last error
./orch run --dry-run          # show the next wave without spawning anything
./orch run                    # the unattended loop
./orch gate M2                # re-run every verification in a milestone
./orch answer <id> --decision "..."   # unblock a parked task
```

`./orch run` spawns one fresh `claude -p` process per ready task, `--concurrency` at a time (default 3).
Fresh process means fresh context — that is what lets the build run for hours without the orchestrator's own
context growing, since the loop holds task ids and exit codes, never the work. Per-task transcripts land in
`ops/build-logs/wave-NN/<task-id>.log`.

For a genuinely unattended overnight run the agents need to use Bash without prompting; pass
`--permission-mode bypassPermissions` deliberately, knowing that removes the per-command gate (the
`.claude/settings.json` allowlist plus the deny list are the standing guard). Default is `acceptEdits`.

In-session, `.claude/workflows/build-wave.js` does the same fan-out through the Workflow tool.

The loop is: park newly-blocking human tasks → compute the ready set → fan out one fresh builder agent per
task → each records its own outcome, with `orch` re-running verification before it will accept a DONE →
recompute. It terminates only when the ready set is empty, then prints why each remaining task is not ready.
That report — not a summary of what got built — is the handoff to the human.
