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

### [M1.13] Execute 10-year NSE backfill

- **Status:** ANSWERED 2026-09-03 01:00 UTC
- **Opened:** 2026-09-02 14:31 UTC
- **Unblocks:** M1.14

**Decision needed:** The 10-year NSE backfill published 2469/2471 sessions; 2 sessions (2020-07-13, 2021-02-16) fail permanently because those NSE bhavcopy files are corrupt at source (a 2-digit-year timestamp and a 'DUMMY' ISIN row) and the parsers correctly reject them — does criterion 2's '100% of missing days explained' accept these two FAILED days as explained-by-enumerated-cause (gap report at 99.95% fully_explained), or must they be recovered before M1.13 is DONE?

**Why an agent can't decide this:** Backfill is otherwise complete (all 10 years in L1, 5.68M rows; delisted spot-check passes; 403 tripwire never fired). The 2 failures are deterministic across 4 fetch/parse attempts each, so retry cannot heal them. Every agent-side fix is unsafe or forbidden: relaxing the legacy date parser to accept 2-digit years or the ISIN regex to admit 'DUMMY' weakens validators that guard the date and ISIN-identity invariants (AGENTIC_CONTEXT §6, CLAUDE.md 'fail loud'), which §7 forbids doing to pass a gate; and I cannot re-source clean upstream files. Redefining the acceptance criterion from fully_explained to explained-by-cause is a change to what the M1.14 gate means, which is the owner's call, not mine.

**Options:**

- A: Relax the date/ISIN validators to admit the two bad files (rejected — weakens §6 invariants)
- B: Accept the 2 enumerated-cause FAILED days as explained (99.95%); treat FAILED-with-cause as satisfying criterion 2; backfill is otherwise complete
- C: Require manual re-sourcing of clean cm13JUL2020 and cm16FEB2021 files before DONE

**Agent recommendation:** B — the two files are corrupt upstream, not a platform defect; the gap report enumerates each with its exact cause and sync_state history, which is precisely the auditability criterion 2 exists to provide, and trading a core data invariant for a green box (A) is the opposite of what the criterion protects. See ops/gates/M1-backfill-report.md.

**Your decision:** Accept the 2 enumerated-cause FAILED days as explained (option B): cm13JUL2020 (malformed 2-digit-year) and cm16FEB2021 (DUMMY ISIN) are corrupt at NSE's source and correctly rejected by the fail-loud parsers; 99.95% coverage satisfies criterion 2. Do not weaken the date/ISIN validators.

---

### [M6.9] GATE M6 — Monitoring depth audit

- **Status:** ANSWERED 2026-09-03 01:01 UTC
- **Opened:** 2026-09-02 18:45 UTC
- **Unblocks:** M8.3

**Decision needed:** The M6 StubLLM gate audit is complete and PASSES (three boxes evidenced in ops/gates/M6.md); closing the milestone gate mechanically needs the M6.8 Anthropic credential. Provide the key to complete M6.8 (which lets 'orch gate M6' exit 0), or accept the StubLLM gate with the live-model quality as a standing open item?

**Why an agent can't decide this:** M6.9's verify is 'orch gate M6', which fails any in-scope task not DONE. M6.8 is NEEDS_SECRET and cannot be DONE without an Anthropic key (B4), so the verify can never exit 0 and 'orch set M6.9 DONE' refuses. M6.8 is not a defect (FAILED would retry a credential-blocked task); per AGENTIC_CONTEXT §1 a missing-credential block is a PARKED exit. Same limitation stucks M1.14/M1.13. Not an agent's call to patch the build system's gate semantics or self-approve DONE past a failing verify.

**Options:**

- A: Provide an Anthropic API key -> M6.8 runs the live drill + quality/cost review, gate M6 then closes green.
- B: Accept the StubLLM gate now; treat M6.8 live-model quality as a standing open item and adjust cmd_gate so human-reserved tasks (NEEDS_SECRET/NEEDS_GO/HUMAN_GATE, or PARKED) report as open items rather than gate failures (also unblocks M1.14).
- C: Leave M6.9 parked until the key exists.

**Agent recommendation:** B — the three StubLLM boxes are fully evidenced (ops/gates/M6.md) and the plan's own M6.9 acceptance is 'three boxes under StubLLM; live-model gap named as an open item', which is satisfied now; the credential is a real but separate live-quality step, and cmd_gate counting a legitimately human-reserved task as a hard failure is the actual blocker, shared with M1.14.

**Your decision:** Fix gate semantics (option B): human-reserved tasks (NEEDS_SECRET/HUMAN_GATE/NEEDS_GO) report as open items, not gate failures — implemented in cmd_gate. M6 StubLLM audit passes all 3 boxes (ops/gates/M6.md); M6.8 live-model drill remains a standing open item pending an Anthropic key.

---

### [M10.4] Fundamentals backfill runner + PIT ingest (XBRL)

- **Status:** ANSWERED 2026-09-03 17:00 UTC
- **Opened:** 2026-09-03 14:41 UTC
- **Unblocks:** M10.5

**Decision needed:** Correct M7.3's XBRL path (built on fabricated fixtures) so real NSE filings ingest, before the M10.4 fundamentals backfill can land real facts?

**Why an agent can't decide this:** The M10.4 runner is built and unit-verified (8 tests green, ruff+mypy clean) and both Source Register URLs verified live (index 200/json, xbrl 200/xml). But running it against the live feed exposes two M7.3 defects: (1) discovery._nature rejects the real feed's consolidated='Non-Consolidated' (fixtures used 'Standalone'); (2) real NSE XBRL identifies the entity by NSE symbol via scheme http://www.nseindia.com/NSESymbol and carries NO ISIN element, but M7.3's parser assumes the entity identifier IS the ISIN (uses it as Filing.isin/FundamentalFact.isin, ISIN_PATTERN-validated, and cross-checks the index ISIN) — so every real filing raises ParseError. Fixing this means resolving symbol->ISIN via the D2 master (invariant #2) or trusting the index ISIN, re-freezing M7.3's fixtures from real filings, and updating M7.3's tests; that changes M7.3's parser contract (also consumed by M7.4), so it is beyond M10.4's blast radius (AGENTIC_CONTEXT §7). No safe default: silently trusting a symbol-as-ISIN would violate invariant #2.

**Options:**

- A: Reopen/fix M7.3 (discovery accepts Non-Consolidated + Un-Audited synonyms; parser reads the entity identifier as an NSE symbol and resolves to ISIN via the index/D2 master; re-freeze fixtures from a real filing + update tests), then resume M10.4 unchanged
- B: Absorb the M7.3 parser+fixture fix into M10.4, expanding its scope and risking M7.4 which also consumes the parser
- C: Ship M10.4 trusting the index ISIN without any symbol cross-check (weakest identity guarantee, borderline invariant #2)

**Agent recommendation:** A, because the defect is M7.3's (its fabricated fixtures never matched the real NSE feed) and its fix is consumed by M7.4 too; keeping it in M7.3 with real re-frozen fixtures fixes it once, correctly, for every consumer, and the M10.4 runner then needs no change.

**Your decision:** Fix M7.3 properly (option A) — "Fix the M7 XBRL and rebuild this for the real filing data." Done: 15 real NSE filings and 2 real index responses captured and frozen under `tests/fixtures/xbrl/` (replacing the fabricated ones), the parser rewritten around the format's actual column model, and M7.4/M10.4 re-pointed at it. The M10.4 runner needed no logic change beyond skipping announcements that carry no XBRL document. Two further defects the rebuild surfaced and fixed: the feed's hyphenated `Un-Audited` was read as "did not say", and `pit_fundamentals.read_latest` collapsed a fourth quarter and a full year ending the same day into one fact. Detail in `ops/gates/M7-xbrl-rebuild.md`. Follow-up 2026-09-03: the live dry-run and a decade-wide era sample then exposed five more format generations the rebuilt fixtures did not cover (a third taxonomy, undeclared column contexts, no contexts at all, BSE-scrip-code identity, company renames) plus two defects in the M10.4 runner itself (the two periods of one chunk shared a checkpoint, losing every Annual filing; a missing L0 payload crashed a resume). All fixed and verified against the fetched bytes — `ops/gates/M10-fundamentals-backfill-live.md`. The campaign execution stays NEEDS_GO.

---
