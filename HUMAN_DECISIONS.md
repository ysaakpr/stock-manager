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

### D6 — M6.1: narrow the acceptance criteria to drop the dead GDELT DOC API

**Raised:** 2026-08-10, by read-only investigation into M6.1's unsatisfiable acceptance bullet.

**The finding — the source is not the problem, the contract is.** M6.1 owns *two* source-register
rows, not one:

| Row | Status | Live re-probe, 2026-08-10 |
|---|---|---|
| `gdelt_v2_event_files` (`source_register.yaml:881–914`) | **VERIFIED** | 200 OK; a 15-min GKG file downloaded and parsed, 2.99 MB, 27 columns |
| `gdelt_doc_api` (`:916–952`) | **FAILED** | 429, reconfirmed at the API's own stated 5 s spacing. Body: *"All high-traffic users should switch to our ngrams dataset"* |

The 429 is a permanent global throttle, not a blip — and it is the row the register itself calls
skippable (`candidate_alternatives`, `:946–951`: the event-files path *"needs no API at all"*).
Nobody hammered the throttle to find out, per the §8 rule against evading rate limits.

What is actually unsatisfiable is the acceptance bullet **"both source register rows flip to
VERIFIED"** — it demands VERIFIED on a row whose own schema and validator never required it.
`source_register.py:54–64` defines exactly three states (`VERIFIED`, `FAILED`,
`BLOCKED_CREDENTIAL`) and the validator at `:245–246` already permits a non-VERIFIED row that
carries `candidate_alternatives`. **No schema change is needed. The fix belongs in TASK_GRAPH.yaml.**

**Two further gaps found in the same contract:**

1. **There is no RSS row in the register at all**, though `rss.py` and `rss_feeds.yaml` are M6.1
   deliverables. The "both rows" bullet was written when only the two GDELT rows existed; RSS
   verification was never specified. Pre-existing gap, independent of the GDELT failure.
2. M6.1's spec (`:1408`, the `entities` column) implies ISIN resolution happens during ingestion.
   It must not — that is M6.2's job, which already has a tighter contract for it
   (`analyst/monitor/linkage.py`, alias table, labelled-sample precision/recall). M6.1 should emit
   **unresolved, source-native** org text.

**Why this is safe to narrow rather than a weakening.** Neither EXECUTION_PLAN's §1 decisions nor
the §6 invariants name GDELT, require article-level granularity, or name any vendor. The
requirement is *"News / geopolitical"* — vendor-agnostic (§134), and §277 hedges *"news replay from
timestamped stores **where available**"*, unlike the unconditional language used for prices.
GDELT was the task author's implementation choice, not a plan requirement.

**Criticality — high graph position, low data criticality.** 11 tasks sit behind M6.1, including
the M6 **and** M8 gates. But M6.1 is **not on the T0 mechanical decision path**: the ISIN-native,
PIT-clean trigger for break condition BC3 is M3.8 (NSE/BSE official announcements + keyword index)
wired into T0 by M5.11 — both predate M6.1 and do not depend on it. M6.4 triggers T1 *"on a T0 flag
or filing event"*, never on a GDELT signal. News enters only as evidence-bundle content shown to
the LLM, downstream of ratified break conditions and deterministic rails (#6). **Invariant #7 ("no
future data in a decision") is already satisfied without M6.1.** So parking buys safety we already
have, while stalling two milestone gates.

**Also relevant: RSS carries the India signal, GDELT largely does not.** GDELT's GKG is a global
firehose — sampled rows were Australian and US local crime, zero India-finance content, and the
schema has **no ticker or ISIN field**, only fuzzy free-text org names. Moneycontrol, ET Markets and
Livemint all return 200 with India-listed company names dense in plain text. PIB works but serves
some Hindi-language items, so normalization must handle or filter non-English.

**The call — pick one:**

1. **Minimal unblock** — drop the DOC API bullet only; RSS register rows deferred to a fast-follow
   task. *Trivial. Leaves gap 1 open past M6.1's DONE.*
2. **Full honest fix (recommended).** DOC API explicitly out of scope with its FAILED status
   standing; each RSS feed gets its own VERIFIED register row **before** M6.1 is DONE; `entities`
   stored unresolved with ISIN resolution named as M6.2's. *Small — four feeds to verify, same
   shape as the 15+ existing register entries. No schema change, no invariant exposure, supersedable
   without migration.*
3. **RSS-primary reframing** — as 2, but RSS becomes the declared primary source and GDELT is
   opportunistic (a GDELT failure logs and skips, never fails the pipeline). Better matches where
   the signal actually is, but rewrites the task's title/spirit, which is more surface than the
   failure requires.
4. **Defer/park M6.1** — not recommended. Stalls 11 tasks and both gates for no compensating
   safety, since the decision-critical path never needed it.

**Recommendation: option 2.** It closes both real gaps on sources confirmed live today, with no
schema change and no invariant exposure.

### D7 — Drop Business Standard from M6.1's curated RSS set

**Raised:** 2026-08-10. `business-standard.com/rss/markets-106.rss` returns **403 (WAF block)**
against the same user agent that gets 200 from Moneycontrol, ET Markets, Livemint and PIB.

**The call:** confirm dropping it, versus spending effort on UA/header tuning to get past the WAF.

**Recommendation: drop it.** Three working India-finance feeds already cover the need, and evading
an anti-bot measure for a fourth is precisely what AGENTIC_CONTEXT §8 forbids. This is a config
change to the curated set, not a blocker.

### For the record — GDELT BigQuery and NewsAPI: probed, not pursued

**No decision needed now.** Both were probed live and both are human-gated under §3 (items 4 and 9):
GDELT via BigQuery returns 401 and needs a GCP project **plus a billing account** (query cost is
normally within free tier, but the account itself is the gate); NewsAPI.org returns 401
`apiKeyMissing` and needs a paid key for anything beyond non-commercial dev use. Option 2 requires
neither. Logged only so nobody later reaches for them silently — revisit if a task genuinely needs
article-level global news volume that RSS + GDELT masterfiles cannot provide.

## Coming up

Not yet open — each becomes an entry below the moment its dependencies complete and it becomes
the actual blocker. Listed here so nothing is a surprise.

| Task | Decision you'll be asked for | Blocks |
|---|---|---|
| M6.8 | An Anthropic API key, to exercise T1/T2 against a real model and measure real cost | live-model quality evidence for the M6 gate |
| M8.3 | Whether to run the tiny-capital live-order sessions yourself (Kite credentials + real money). **Read `ops/compliance/sebi-algo-memo.md` Q1 first** — Kite's terms 2(e) say the APIs are not intended for fully automated trading without manual intervention, which is a question about the product's shape, not just this gate. | M8 gate |
| M8.4 | Graduation: fund a case with real money, or not (decision #8 — discretionary, always yours) | — |

---
