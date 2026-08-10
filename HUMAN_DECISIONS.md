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

### D8 — M3.9 was never blocked. Confirm the acceptance rewrite, and whether to split the task.

**Raised:** 2026-08-10. **Blocks ~34 dependents** — the largest single unblock in the graph.

`nifty_tri_history` is marked `FAILED` because the register recorded a **stale URL path**, not
because the source is gated. Corrected path, probed live 2026-08-10 (4 POSTs, ≥2.5s apart, no 403
and no 429):

| index | HTTP | bytes | rows | earliest | latest |
|---|---|---|---|---|---|
| NIFTY 50 | 200 | 177,036 | 1,239 | 01 Apr 2021 | 30 Mar 2026 |
| NIFTY IT | 200 | 168,505 | 1,239 | 01 Apr 2021 | 30 Mar 2026 |
| NIFTY CPSE | 200 | 170,170 | 1,239 | 01 Apr 2021 | 30 Mar 2026 |
| NIFTY 50, max depth | 200 | 880,867 | 6,213 | **02 Apr 2001** | 30 Mar 2026 |

`POST https://niftyindices.com/BackPage/getTotalReturnIndexString` — no `.aspx` — body
`{"cinfo": "{'name':'NIFTY 50','startDate':'...','endDate':'...','indexName':'NIFTY 50'}"}`,
**no session cookie, no Referer**. 25 years of all three indices is available in one request each.

**Three defects in the current acceptance text (lines 858–862), not one:**

1. *"spot-checked against a published value"* requires a **live fetch at verify time**, which
   collides with ratified rule B8 (fixtures are checked in, `AGENTIC_CONTEXT.md:66`) and with the
   no-network guard at `dataplatform/ingest/fetcher.py:27-29`. As written it cannot pass offline.
2. It says **"NIFTY-TRI" singular**, but the ratified reference-case fixture
   (`AGENTIC_CONTEXT.md:70`) needs **NIFTY 50 + NIFTY IT + NIFTY CPSE**.
3. It is silent on depth. The binding constraint is **10 years**, set by M4.10's verify line
   (`TASK_GRAPH.yaml:1063`) — not the case fixture's 5-year horizon.

**Data-requirement verdict: nothing needs daily TRI *levels*.** M4.6's acceptance (`:988-990`)
never mentions TRI; M4.10 wants a period return (`:1060`); M6.6's drawdown profile is
portfolio-side and must trace to journal entries (`:1508`); the only TRI-aware code in the tree
stores benchmark *names* as strings (`analyst/cases/policies.py:176-181`). Periodic return / XIRR
plus a drawdown profile is sufficient. **Ingest daily anyway** — 25 years costs one request.

**The call, part 1 — which acceptance rewrite.** Recommended (difficulty b, no data migration, no
new invariant risk), replacing lines 858–862's third bullet with an assertion that is fully
verifiable **offline against a frozen fixture**: TRI parses from
`tests/fixtures/nifty_indices/2026/` for all three indices over 2021-04-01..2026-03-31, asserting
(i) 1,239 rows per index with identical date sets; (ii) strictly increasing dates after
normalisation, no duplicate, no gap against the M1.7 trading calendar; (iii) the literal `Decimal`
levels **33655.43 / 41606.83 / 11793.29** on 2026-03-30; (iv) every level is `Decimal` and
positive, and `NTR_Value` is `None` — never `Decimal 0` — where the source publishes `'-'`.
Full option set (1: mechanical single-index; 2: recommended; 3: adds a TRI ≥ price-index
cross-check but depends on an unverified historical-snapshot fetch; 4: split-aware) is in the
options memo.

**The call, part 2 — split M3.9 into a constituents task and a TRI task?** **Recommended: yes.**
Not for parallelism — 16 of the dependents reconverge at M4.8 — but because **one task currently
owns one VERIFIED row and one FAILED row, so it can be neither done nor blocked honestly.** The 34
dependents divide 5 constituents-only, 9 TRI-only, 16 both, and M7.1/M7.3 need neither (they reach
M3.9 only through the M3.10 gate edge). Cost of the split: 2 dependency lines and 1 task block.

**Three parser traps worth knowing before anyone builds this** (all observed, not inferred): rows
arrive **newest-first**; index names must be sent in **CAPS** and echo back title-cased; and a 5th
key `RequestNumber` **regenerates on every request** — hash the payload as-is and determinism dies,
which invariant "same inputs → byte-identical journal" would catch only after it hurt.

---

### D9 — A 200 with the wrong body is written straight into L0. Fix in the shared layer?

**Raised:** 2026-08-10. This is the **generalizable** half of D8 and it outlives M3.9.

`Fetcher.fetch` writes to L0 on **any 2xx with zero payload inspection**:
`dataplatform/ingest/fetcher.py:402` calls `_request(...)`, `:403` calls `_l0.put(...)`, and there
is **nothing between them**. The register's `parse_check` field is **prose, not executable**;
`fetch_succeeded` only checks that the body string is non-empty
(`dataplatform/ingest/source_register.py:175-182`), and the register validator runs against the
YAML, never against a live payload. And because L0 is immutable by invariant #1, **a bad write
cannot be cleaned up** — only quarantined.

**Do not reach for a content-type check.** The working niftyindices response is
`text/html; charset=utf-8`, identical to the block page — so a content-type guard rejects every
good response. `ops/gates/source-verification.md` §5 item 1 taught exactly that wrong fix and has
been corrected in place today.

**Same hole, other rows:** `bse_announcements` (returns `{}` at 200),
`screener_company_fundamentals`, both BSE bhavcopy rows, and both niftyindices CSV rows.

**The call:** add a `validator`/`expect` hook to `CrawlPolicy`
(`dataplatform/ingest/policy.py:168-191`), invoked between `fetcher.py:402` and `:403`, raising a
fetch-level `PayloadShapeError` before anything reaches L0 — **as its own M1-series task**
(recommended) rather than smuggled into M3.9. It reopens M1.2's module, which is why it wants its
own task entry and its own gate rather than riding along on an M3 task.

Two sub-questions bundled here: **(a)** when a row flips `FAILED → VERIFIED`, must a real fetcher
run re-derive `sample_bytes` / `sample_sha256` / `parse_check` (recommended — otherwise VERIFIED
just means "an agent edited a YAML file"), and **(b)** given two of two investigated `FAILED` rows
turned out to be **bookkeeping errors rather than dead sources** (M6.1's contract, M3.9's stale
URL), should the remaining `FAILED` / `BLOCKED_CREDENTIAL` rows be **re-probed before anyone treats
them as real constraints**? Recommended: yes, as one scoped sweep task.

---

### D10 — Wave A merge order is not optional, and one fix only lands when two branches meet

**Raised:** 2026-08-10. Three repair branches exist locally, none pushed. Read this before merging any
of them.

**Merge order:**

1. **`polly/m5.4-finish` first** (7 commits). It is the branch that makes `make check` green — it
   fixes the 6 static-analysis failures that commit `60874b6` introduced. This matters beyond its own
   task: `make check` currently dies at `ruff format` *before* reaching the secret scan, so until
   M5.4 lands, the scan does not run inside `make check` at all.
2. **`polly/secrets-hardening` and `polly/m0.3-rework` together.** Neither is independently complete:

| Merged alone | What you get |
|---|---|
| M0.3 only | A container stack that works, and a test suite that **still silently skips 136 tests** and exits 0 — the M0 gate's exact signature. The skip-guard fix lives in `dataplatform/config.py` + `tests/integration/**`, which the secrets branch owns. |
| secrets only | A suite that fails loudly on a misconfigured DSN, against a stack whose migration-at-start and loopback-bind fixes are on the other branch. |

**Known merge conflict:** both branches modify `Makefile` (secrets adds the scan step; M0.3's earlier
work touched targets) and `ops/BACKLOG.md`. Small and textual, but expect to resolve them by hand.

**Post-merge integration task — do NOT skip it.** The secrets branch replaced the single interpolated
DSN with discrete `postgres_host/port/user/password/db` settings passed as psycopg keyword arguments,
so no character is ever URI grammar. That closes a **silent misparse** on the host path: a password
containing `/` made the old DSN parse as `host='trading', user=None, password=None`, and `%41`
silently became `A` — connecting as the wrong user rather than failing. But
`ops/docker-compose.yml` still hands the container **one interpolated `DATABASE_URL`**, so the
in-container path keeps exactly the weakness the host path just shed. The fix is for compose to pass
the discrete `POSTGRES_*` variables instead.

It could not be done inside either branch: `ops/**` belongs to M0.3, whose `config.py` has no
discrete fields, so making the change there would have broken that branch's own 138-test
verification. It is a genuine two-branch dependency, deferred deliberately rather than forgotten.
Until it lands, **a container password containing `/`, `%`, `@` or a space is still unsafe** even
though the host-side path is fixed.

**Also unenforced until the secrets branch's B1 fix lands:** nothing scans for secrets on the path
agents actually commit through. `orch set-state DONE` runs format/lint/types only and never calls
`make check`; `.pre-commit-config.yaml` is not installed (no `.git/hooks/pre-commit` exists anywhere);
and `make check` reaches the scan only on a tree that already passes formatting. The one control that
works today fires **after** push — which on a deliberately-public repo (D5) is after the harm.

---

## Coming up

Not yet open — each becomes an entry below the moment its dependencies complete and it becomes
the actual blocker. Listed here so nothing is a surprise.

| Task | Decision you'll be asked for | Blocks |
|---|---|---|
| M6.8 | An Anthropic API key, to exercise T1/T2 against a real model and measure real cost | live-model quality evidence for the M6 gate |
| M8.3 | Whether to run the tiny-capital live-order sessions yourself (Kite credentials + real money). **Read `ops/compliance/sebi-algo-memo.md` Q1 first** — Kite's terms 2(e) say the APIs are not intended for fully automated trading without manual intervention, which is a question about the product's shape, not just this gate. | M8 gate |
| M8.4 | Graduation: fund a case with real money, or not (decision #8 — discretionary, always yours) | — |

---
