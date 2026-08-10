# Human Decisions Queue

Append-only. Each entry is a decision an agent is not permitted to make
([AGENTIC_CONTEXT.md](AGENTIC_CONTEXT.md) §3), written to be decidable without reading any
transcript.

**To answer one:**

```bash
./orch answer <task-id> --decision "your call, in a sentence"
```

That records the decision, marks the entry ANSWERED, and returns the task to the build queue
so the next wave picks it up. Nothing else is needed.

---

## Already decided (2026-08-08)

Ratified at setup, recorded as [AGENTIC_CONTEXT.md](AGENTIC_CONTEXT.md) §2 B1–B4. Agents treat
these as settled and will not re-ask.

| # | Decision |
|---|---|
| B1 | Verify + sample now; the full 10-year backfill waits for a one-word go (task M1.13) |
| B2 | Golden CA suite validates against yfinance adjusted closes **and** hand-computed expectations |
| B3 | `uv` with a pinned Python 3.12; Postgres in docker-compose |
| B4 | No Anthropic key and no Kite credentials yet — both sit behind interfaces with stubs |

Second round, after M0.1 and M8.2 reported:

| # | Decision |
|---|---|
| D1 | **M1.13 pre-authorized.** The full 10-year NSE backfill runs unattended as soon as M1.9–M1.12 are DONE and the dry-run plan verifies. A 403 hard stop parks rather than retries. |
| D2 | **`platform/` → `dataplatform/` ratified** (EXECUTION_PLAN §12). All 71 path strings in TASK_GRAPH.yaml swept, so no agent resolves the old name from a footnote. |
| D3 | **Broker re-auth interlock built now, in M5** — new task M5.15, `AUTH_REQUIRED` alongside `SKIPPED_DATA_RED`, rather than retrofitting the daily loop at M8. |
| D4 | **Detached unattended runs authorized** — `./orch run --permission-mode bypassPermissions`. Standing guards are the `.claude/settings.json` deny list and the §6 invariants. |

## Answered

### D5 — The repo is public. Confirm that is intentional. → **ANSWERED: yes, intentionally public.**

**Raised:** 2026-08-10, by a read-only security audit of the working tree and full git history.
**Answered:** 2026-08-10 — *"D5 is intentional."* Option 1 below. No action on visibility; the
secret-scanning gate is now **mandatory**, not advisory.

**The finding.** `origin` is `git@github.com:ysaakpr/stock-manager.git` and GitHub reports
`"private": false`. Local `main` is fully pushed, so all 36 commits are world-readable now. The
`60874b6 "first commit"` message is misleading — it is an ordinary commit on top of the full chain,
not a squash, so the entire development history is published, not a flattened snapshot.

**Why it needed a human.** [AGENTIC_CONTEXT.md](AGENTIC_CONTEXT.md) §2 B7 stated *"Local repo, no
remote"*, and `.claude/settings.json` denies `git push` and `git remote add`. Every autonomous agent
has therefore been building under a ratified threat model of "nothing leaves this machine" that did
not match reality. The remote was added outside the agent sandbox. B7 has been corrected to state the
truth; this entry records the decision behind it.

**Good news:** the audit found **no secret** anywhere — not in the working tree, not in any of the 228
blobs in history, not in dangling objects. `.env` was never tracked and `.env.example` holds no real
values. Nothing needs rotating today. This is a decision about posture, not an incident.

**The call you're being asked to make:**

1. **Public, intentionally** — fine, and the hardening tasks proceed on that assumption: the repo stays
   readable by anyone, so the secret-scanning gate becomes mandatory before any real credential (M6.8's
   Anthropic key, M8.3's Kite credentials) ever exists on this machine.
2. **Should be private** — make it private, then decide separately whether the published history matters
   enough to do anything about. It contains no secrets, so most likely it does not.

**Decision (2026-08-10): option 1 — public, intentionally.**

What this settles for every agent, permanently:

- **The threat model is "everything committed is published."** Not "published if someone looks" —
  published, immediately, irrevocably. `git push` is the publication event.
- **Rotation, never redaction.** A credential that reaches a commit is compromised the moment it is
  pushed and can only be rotated at the provider. Deleting it in a later commit changes nothing.
  See [ops/runbooks/secret-leak.md](ops/runbooks/secret-leak.md).
- **The scanning gate is mandatory and must exist before the first real credential does.** Both
  M6.8 (Anthropic key) and M8.3 (Kite credentials) introduce live secrets onto this machine; neither
  may start until a pre-commit hook, a `make check` scan, and a CI scan are all in place and
  demonstrated to fail on a planted fake. Self-attested "I reviewed my own diff" is not a control.
- **Kite is the highest-severity credential in the system** — it can move real money. Treat any
  suspected exposure of it as an incident, not a cleanup.
- **This applies to `BUILD_STATE.json` too**, now that it is tracked (commit `64651a4`). It is
  machine-written build state that is published on every push: no agent may write a credential,
  token, DSN, or raw error payload that could embed one into it.

## Open

_None open. D5 was the last one; the next entries appear as the tasks below become blockers._

## Coming up

Not yet open — each becomes an entry below the moment its dependencies complete and it becomes
the actual blocker. Listed here so nothing is a surprise.

| Task | Decision you'll be asked for | Blocks |
|---|---|---|
| M6.8 | An Anthropic API key, to exercise T1/T2 against a real model and measure real cost | live-model quality evidence for the M6 gate |
| M8.3 | Whether to run the tiny-capital live-order sessions yourself (Kite credentials + real money). **Read `ops/compliance/sebi-algo-memo.md` Q1 first** — Kite's terms 2(e) say the APIs are not intended for fully automated trading without manual intervention, which is a question about the product's shape, not just this gate. | M8 gate |
| M8.4 | Graduation: fund a case with real money, or not (decision #8 — discretionary, always yours) | — |

---
