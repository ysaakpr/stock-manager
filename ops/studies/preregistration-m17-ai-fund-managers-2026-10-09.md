# Pre-registration: M17 AI fund managers, forward paper test, 2026-10-09

**Status: RATIFIED 2026-10-09 except S0** (owner, chat; §9). Written before any M17 code exists and before any manager has made a decision. The
owner confirms the items marked **[OWNER]** and records it in §9. M17.8 cannot start the clock until
§9 is filled in. After session S0 (§6), §2–§7 are frozen. A changed manager (prompt, model, mandate,
rails or tools) is a **new manager** with a new id and its own clock. It is never a revision of the old
one.

## 0. Why this test runs forward

Every result on this lake so far is a backtest. A backtest cannot judge an LLM: the model has read
about the period it is being tested on, so its "judgment" in 2023 is partly memory of 2024
(EXECUTION_PLAN §7, "agent judgment is evaluated forward, in paper mode"). Run forward, every
decision is made before its outcome exists. The append-only journal timestamps each call, and that
timestamp is the evidence the call was made blind.

The question is narrow: **does an LLM deciding daily, with the platform's data plus the web, beat
the mechanical shortlist it starts from, after costs?** "Did it make money?" is not the question. A
rising market makes money for everyone, and the control books (§3) exist to strip that out.

## 1. What a fund manager is

A fund manager is **a configuration, not code**. It has:

- a mandate: horizon, capital, universe floor, maximum positions;
- a model;
- a frozen prompt;
- rails;
- a tool budget.

It owns one paper book (SimBroker through the M15.3 staging/recon/kill-switch path) and one journal
stream.

In plan terms, each manager is a **paper-only case with the rotation dial at 100%**: the whole book
is the tactical sleeve (decision #11). Tactical positions carry a journaled rationale, not a ratified
thesis (§5.3), so daily discretion stays within the constitution. The policy layer, meaning the
mandate, the rails and this document, is what the owner ratifies (decision #5). The daily loop
needs no human.

**Facts are shared, judgment is private.** The managers read one shared Research Commons:

- the market sheet;
- the universe sheet;
- factual digests;
- the web-fetch cache.

They never read each other's books, journals or decisions. The Commons holds no recommendation, rank
or opinion from any manager, so four managers stay four independent judgments.

## 2. Roster

Four managers: two horizons × two capitals. All four use the same model and prompt skeleton, so the
mandate is the only difference between them.

| Id | Horizon (thinks in) | Opening capital | Max positions | Max position | Universe floor |
|---|---|---|---|---|---|
| `FM-SWING-10L` | 5–20 sessions (1–4 weeks) | ₹10,00,000 | 15 | 10% | ₹1 cr/day median traded value |
| `FM-SWING-1CR` | 5–20 sessions (1–4 weeks) | ₹1,00,00,000 | 20 | 10% | ₹1 cr/day, plus the participation rail |
| `FM-POS-10L` | 20–60 sessions (1–3 months) | ₹10,00,000 | 15 | 10% | ₹1 cr/day |
| `FM-POS-1CR` | 20–60 sessions (1–3 months) | ₹1,00,00,000 | 20 | 10% | ₹1 cr/day, plus the participation rail |

- **Universe:** NSE `EQ` series, priced in L1 on the decision session, 20-session median traded
  value at or above the floor, not in GSM or ESM. Names in ASM are allowed but flagged in the
  universe sheet. Within the universe the manager decides where to look, from small cap to large
  cap. No cap-tier quota is imposed.
- **Model:** `claude-opus-5-5` through the Claude CLI (`analyst.llm.claude_cli`, subscription; owner
  decision 2026-10-09). Commons digests use `claude-sonnet-5-5`. **[OWNER]** confirm the models.
- **Horizon is guidance, not a rail.** A swing manager may hold longer. Its journal must say why when
  a position outlives 20 sessions. The horizon is checked in the scoreboard, never enforced.

## 3. Comparison books (no LLM)

| Id | What | Why |
|---|---|---|
| `CTRL-<manager>` | Equal weight across the top *N* names (*N* = the manager's max positions) of the same mechanical shortlist the manager receives. Rebalanced weekly for swing and every 21 sessions for positional. Same rails, costs and capital. | Isolates what the LLM adds over its own input |
| `BENCH-N500` | Buy-and-hold NIFTY 500 TRI proxy, the same proxy the backtests use, from S0 | The market |

The mechanical shortlist is part of the Commons. Its rule is fixed in M17.2 and frozen with this
document: a composite rank of 12-1 momentum, 20-session relative strength against NIFTY 500,
earnings-surprise rank (M16.3 definition) and liquidity, top 40 per session. A manager may research
names outside the shortlist, which is one of the things being tested. The control book never does.

## 4. Daily cycle and the decision contract

1. **Interlock.** If data is red, every manager journals `SKIPPED_DATA_RED` and stages nothing
   (invariant). A kill switch that has tripped stops every book.
2. **Commons build.** This runs once per session, after EOD is green. It computes the market sheet,
   the universe sheet, the shortlist, and digests of the session's new filings and announcements.
3. **Per manager, isolated, bounded rounds.** The Claude CLI has no tool-use protocol. It accepts one
   structured-output schema per call (`analyst/llm/claude_cli.py`), so research works as a sequence
   of rounds:
   - *Round 0:* the manager receives its book, its open rationales, the market sheet and the
     shortlist. It returns a research request: at most 12 ISINs to deep-dive and at most 8 web
     queries or URLs.
   - *Rounds 1–2:* the harness fulfils the request from the Commons and the fetch cache, cache
     first, and returns the bundle. The manager may ask once more within the same limits.
   - *Final call:* the manager returns its decisions.

   A manager therefore costs at most 4 model calls per session. Every byte it saw is in the evidence
   pack, so a decision can always be re-read alongside its inputs.
4. **Decisions.** For each holding: `HOLD`, `TRIM` or `SELL`. For each researched name: `BUY`,
   `PASS` or `WATCH`. "Nothing today" is a journaled decision. Each decision carries:
   ```
   isin, action, target_weight, rationale, horizon_sessions,
   p_beat_bench   # probability its return over horizon_sessions beats BENCH-N500
   expected_excess_pct, stop_pct, invalidation, evidence_refs[]
   ```
   A `SELL` or `TRIM` must name what changed against the position's opening rationale: an
   invalidation hit, the stop hit, the target reached, or a named better use of capital. A sell
   with no stated change is refused by the schema.
5. **Rails, deterministic and unbypassable:**
   - position ≤ 10% of book;
   - sector ≤ 30%;
   - positions ≤ the roster max;
   - an order's notional ≤ 5% of the name's 20-session median traded value (the participation
     rail; it binds mostly on the ₹1 cr books);
   - no sell within 2 sessions of the buy fill;
   - no short, no F&O, no margin.

   Cash may be anything from 0 to 100%. Idle cash earns repo − 50 bp, as in the backtests.
6. **Fills.** Next session's open, the shared cost model, and the SimBroker participation-scaled
   slippage at its defaults. A refused order is journaled with the rail that refused it.
7. **Close of day:** mark the books, run reconciliation, then update the scoreboard.

**Deadline:** a manager that has not finished by 08:30 IST on the next session journals
`MISSED_SESSION` and stages nothing for that session. There is no catch-up trading on a stale view.

## 5. Web access

The manager never browses. It names queries or URLs, and the **Commons fetcher** runs them. The
fetcher is a separate `claude -p` invocation whose tool set is limited to web search and web fetch,
and its prompt allows it to retrieve and transcribe, never to judge. Each result is stored:

- immutable and content-addressed;
- keyed by (normalised query or URL, session date);
- carrying `fetched_at`, the URLs and the text.

A repeated query or URL in the same session is a cache hit for every manager. Each decision cites the
snapshot ids it relied on. Snapshots are stored text, never re-fetched for an audit, so the audit
reads exactly what the manager read. The licensing rule follows the news store's: the text is kept
for evidence and is never republished in an archive.

## 6. Clock, scoring window, pass/fail

- **Dry run:** 5 sessions on the full path (M17.8). Nothing counts.
- **S0:** the first session after the owner's go, recorded in §9. **Scoring window:** S0 to S0 + 62,
  which is 63 sessions, about 3 months.
- **Primary metric,** per manager: book return over the window minus its control book's return,
  both after costs and cash interest. It is measured from the S0 open to the close of S0 + 62.
- **Pass [OWNER: confirm thresholds].** All three must hold:
  1. excess over its control ≥ +3.0 pp over the window;
  2. max drawdown ≤ BENCH-N500 max drawdown + 5 pp;
  3. Brier score of `p_beat_bench` < 0.25 (better than always saying 50%) over ≥ 30 resolved
     decisions.
- **Clear fail:** excess over control ≤ −3.0 pp, or a Brier score ≥ 0.25 on ≥ 30 resolved
  decisions.
- **Otherwise inconclusive,** which pre-registers one extension to S0 + 125 (126 sessions) under the
  same rules. There is no second extension.
- **Four managers, four chances at luck.** A result is always reported as "*k* of 4 passed", never as
  a lone winner. Graduating any manager toward real money needs ≥ 2 of 4 passing, or one passing
  across the extension window too. That decision is the owner's (decision #8). This rule only
  states what the evidence must at least show.

**Secondary metrics,** reported but never selected on:

- excess over BENCH-N500;
- hit rate of BUY decisions on `p_beat_bench > 0.5`;
- return by confidence tercile;
- turnover and cost drag in bp;
- the share of BUYs from outside the shortlist and their excess return;
- rail refusals by rail;
- model calls, tokens and estimated USD per manager (subscription: an estimate, decision #12);
- the swing/positional horizon adherence.

## 7. Fixed for the whole window

- No human override of any book. The owner may read the digest and the status page. Acting on a
  book ends that manager's test, and the journal records it as `OWNER_INTERVENTION`.
- The prompts, schemas, shortlist rule and rails are hashed at S0 and journaled. A changed hash means
  a new manager id (preamble).
- A Commons bug fixed mid-window is journaled with the session range it affected. The decisions
  already made stand, because they were made on what the manager saw.
- One code path: a manager stages orders through the same `StagingCoordinator` and `Reconciler` the
  M15.3 paper session uses (decision #2). `execution.kite_broker` is unreachable from M17, and an
  AST test enforces it.

## 8. Amendments

Amendments are additive, dated, and only valid before S0.

### Amendment 1 (2026-10-09, before any decision): analyst strategy

Source: `ops/studies/m17-analyst-strategy-2026-10-09.md` (research on short-horizon fundamental,
price/volume and LLM-forecasting evidence). §2–§7 stand, except where this amendment adds to them:

(a) **Commons additions** (facts only, shared; new task M17.9):
- screens S1 trend leaders, S2 volume breakout, S3 pullback in a leader, S4 earnings momentum,
  S5 event watch, with the rules in study §2;
- per-name dossier features (study §2);
- a frozen base-rate table (study §3) computed from 2016-10 → 2026-09 history;
- the three-state regime (study §4).

The composite shortlist of §3 and the control books are **unchanged**.

(b) **Universe exclusions** added to §2:
- price band ≤ 5%;
- an integrity event in the last 60 sessions: auditor, CFO or independent-director resignation,
  SEBI order, a rating downgrade to sub-investment-grade or "issuer not cooperating", or a default.
  This is detected with a frozen keyword table on announcement subjects.

The GSM/ESM exclusion reads the newest list at most 5 sessions old. With no such list, the session
admits no new BUY.

(c) **Stops are mechanical** (adds to §4 step 5). A BUY must declare `stop_pct`, 1.5–3 × ATR and at
most 15%. A close below a stop sells the position at the next open, with no model call. A manager may
tighten a stop, or convert it to a trailing stop, but never loosen it. A new integrity event in a held
name puts it first in the next session as a forced review.

(d) **The decision schema adds:**
- `edge_type` (`NONE` forces PASS/WATCH/HOLD);
- `base_rate_cell`;
- `adjustments[]`;
- `scenarios[]` (bull, base and bear, with probabilities summing to 1);
- `already_priced_in`;
- `catalyst`;
- `cost_hurdle_check`;
- `premortem[]`;
- `invalidation[]`, which must be observable and dated.

Every numeric claim cites a bundle field or snapshot id, and an unknown id voids that decision.
Holdings are shown to the manager **without** cost basis or P&L.

(e) **Fills:** a BUY whose session opens and stays locked at the upper price band (open = high = low
= the upper band) is unfilled and journaled `UNFILLED_UPPER_CIRCUIT`.

(f) **Scoring additions, secondary only.** These never change the §6 pass/fail rule, which stays on
the raw `p_beat_bench`:
- Brier score of p shrunk towards its base-rate cell (`0.5·p + 0.5·base_rate`, weight fixed now);
- excess return by regime state;
- the cap-tier mix of BUYs against the control book;
- the hit rate by `edge_type`.

(g) **Prompt:** `analyst/fundmanager/prompts/manager.md` v1 is the prompt of every manager. Its bytes
are in `mandate_hash`.

## 9. Owner confirmation

| Item | Value | Confirmed |
|---|---|---|
| Models (§2) | `claude-opus-5-5` decisions, `claude-sonnet-5-5` digests | 2026-10-09 (owner, chat) |
| Pass/fail thresholds (§6) | +3.0 pp / DD + 5 pp / Brier < 0.25 on ≥ 30 | 2026-10-09 (owner, chat) |
| Roster and capital (§2) | 4 managers, ₹10 L and ₹1 cr | 2026-10-09 (owner, chat) |
| Horizons (§2) | 1–4 weeks and 1–3 months | 2026-10-09 (owner, chat) |
| Web search (§5) | allowed, through the Commons fetcher | 2026-10-09 (owner, chat) |
| LLM path (§2) | Claude CLI subscription; monthly cap deferred | 2026-10-09 (owner, chat) |
| S0 | the first session after the 5-session dry run (M17.8) passes; owner asked for as soon as it is ready (2026-10-09) | — |
