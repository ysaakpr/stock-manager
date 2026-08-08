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

## Coming up

Not yet open — each becomes an entry below the moment its dependencies complete and it becomes
the actual blocker. Listed here so nothing is a surprise.

| Task | Decision you'll be asked for | Blocks |
|---|---|---|
| M6.8 | An Anthropic API key, to exercise T1/T2 against a real model and measure real cost | live-model quality evidence for the M6 gate |
| M8.3 | Whether to run the tiny-capital live-order sessions yourself (Kite credentials + real money). **Read `ops/compliance/sebi-algo-memo.md` Q1 first** — Kite's terms 2(e) say the APIs are not intended for fully automated trading without manual intervention, which is a question about the product's shape, not just this gate. | M8 gate |
| M8.4 | Graduation: fund a case with real money, or not (decision #8 — discretionary, always yours) | — |

---
