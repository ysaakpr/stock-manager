# M5.13 — AI/Robotics reference case + 10-session paper run

**Task:** M5.13 (A1, AUTO) · **Verify:** `uv run pytest tests/integration/test_paper_run_10_sessions.py -q`

This is the evidence record for the M5.13 paper run — the §5.2 AI/Robotics reference case taken
through its whole opening act and then run unattended for ten simulated sessions. It is *not* the M5
gate audit; that is M5.14 (`ops/gates/M5.md`), which re-verifies every M5 box across the milestone.
This document records what the paper run demonstrates and where each acceptance criterion is proved,
so the gate auditor can cite it rather than re-derive it.

## What was built

- **`tests/fixtures/cases/ai_robotics.yaml`** — B9's reference case as *interview inputs*: the §5.1
  transcript (₹10k/mo on the 1st, 5-year horizon, AI/Robotics, aggressive appetite, medium
  concentration, NIFTY-TRI + IT/CPSE blend), the theme map universe, one §5.3 thesis per holding,
  the LIQUIDCASE parking instrument, and — as a drift guard — the `expected_policies` the §5.1
  recommendation must derive. Pre-marked `RATIFIED_FIXTURE`: paper and tests only, **never** real
  money (AGENTIC_CONTEXT B9, §3.6).
- **`tests/integration/test_paper_run_10_sessions.py`** — opens the case through the real interview
  flow, funds it PAPER, and drives ten consecutive `paper_session` runs through the scheduler
  (`SchedulerRunner.run_once`) with a `FrozenClock` advanced one day between runs. The daily session
  composes the M5 parts: the data-red interlock + A5 T0 sweep, A7 cash park/deploy, A8 rails, A9
  journal, and X1 staging over `SimBroker` (the paper broker).

## The M5 gate boxes this proves

| Acceptance criterion | Proved by | How |
|---|---|---|
| 1. Case created through the interview flow (not hand-constructed) and ratified | `test_case_created_through_interview_flow_and_ratified` | `conduct_interview` → `build_proposal` derives the seven §5.2 policies from the fixture transcript; the *derived* policy set is proposed, ratified `FIXTURE`, funded `PAPER`, activated. The ratified dial (30%), rails (15% / 35% / 8 / −25%, ₹120k per-order cap) and T2 cadence (MONTHLY) are asserted equal to `expected_policies`. |
| 2. Ten consecutive sessions run unattended with a journal entry for every day | `test_ten_sessions_run_with_a_journal_entry_for_every_day` | Ten `run_once` calls, clock advanced daily; each of 2026-09-01..09-10 carries ≥1 journal entry, and exactly ten T0 `HEARTBEAT`s are written (invariant #9 — a quiet day still records the checks it performed). |
| 3. An injected oversized order is blocked by rails and journaled `RAIL_BLOCK` | `test_oversized_order_is_blocked_by_rails_and_journaled` | On 2026-09-05 a ₹200k buy (well over the ₹120k fat-finger cap) is put through A8; it is refused and a single `RAIL_BLOCK` line by the `RAILS` actor names the breached rails (invariant #6). |
| 4. A SIP instalment is parked in the liquid ETF then deployed on a valid trigger | `test_sip_instalment_is_parked_then_deployed` | On the SIP day (2026-09-01) the ₹10k instalment is parked in LIQUIDCASE the same session (§5.6, decision #10), journaled `BUY`/CASH at the parking ISIN; on 2026-09-03 a tactical trigger deploys the parked cash into a position, journaled `BUY`/TACTICAL, draining the deployment queue. |

## Honest limitations (for the M5.14 auditor)

- **No LLM ran.** The reference case's theses and theme map are fixture data, not model output, and
  the T0 sweep is mechanical by definition (₹0, no `LLM`). Every box above is satisfied with
  deterministic code and stub/fixture inputs (B4). Real model behaviour is M6's to test.
- **The daily-session composition lives in the test, not in a product module.** M5.13's deliverables
  are the fixture, the paper-run test and this record; the "daily loop" it exercises is the
  composition of the already-built A5/A7/A8/A9/X1 parts. A standing production daily-loop entrypoint
  is not in M5's scope.
- **The interlock is forced green.** The paper run injects an always-green `GreenGate` to prove the
  loop runs on clean days; the red-day short-circuit (invariant #10, `SKIPPED_DATA_RED`) is proved
  separately in `tests/unit/test_t0.py`.
- **Paper broker only.** "Paper mode" is `FundingMode.PAPER` over `SimBroker` on the same decision
  path a real broker plugs into (invariant #5); no real order was placed (§3.5).

## Reproduce

```
make up                                                            # docker postgres
uv run pytest tests/integration/test_paper_run_10_sessions.py -q   # 4 passed
```
