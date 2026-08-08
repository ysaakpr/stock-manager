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

## Coming up

Not yet open — each becomes an entry below the moment its dependencies complete and it becomes
the actual blocker. Listed here so nothing is a surprise.

| Task | Decision you'll be asked for | Blocks |
|---|---|---|
| M1.13 | Go for the full 10-year NSE backfill (~4–6 hrs of rate-limited fetching) | M1 gate, and everything that needs real history: M2 golden suite, M4 reference case + momentum run |
| M6.8 | An Anthropic API key, to exercise T1/T2 against a real model and measure real cost | live-model quality evidence for the M6 gate |
| M8.3 | Whether to run the tiny-capital live-order sessions yourself (Kite credentials + real money) | M8 gate |
| M8.4 | Graduation: fund a case with real money, or not (decision #8 — discretionary, always yours) | — |

---
