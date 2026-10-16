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

### Amendment 2 (2026-10-10, before S0): four distinct styles, each trading two capital books

Owner decision 2026-10-10. The first live dry session (2026-10-09) showed the four managers, who
differed only by horizon and capital, researching largely the same names: 8 of 19 were researched
by all four. The two managers on each horizon were near-duplicates, since capital matters only
through the participation rail. This amendment replaces §2's roster and adds to §3, §6 and
Amendment 1 (g). Everything else stands.

(a) **Roster.** Four managers, each with a distinct style and its own starting screens. All four
read the full universe and may research any name in it.

| Manager | Horizon | Style | Starting screens |
|---|---|---|---|
| `FM-SWING-BRK` | 5–20 sessions | Volume breakouts and pullbacks in leaders | S2, S3 |
| `FM-SWING-EVT` | 5–20 sessions | Results reactions and event drift | S4, S5 |
| `FM-POS-TREND` | 20–60 sessions | Persistent trend and sector leadership | S1 |
| `FM-POS-FUND` | 20–60 sessions | Earnings inflection with quality and reasonable valuation | S4 + dossier fundamentals |

Same model (`claude-opus-5-5`), rails, research limits (12 ISINs, 8 queries, 2 research rounds)
and decision contract as before.

(b) **One manager, two books.** Each manager's decisions trade a ₹10,00,000 book
(`<manager>-10L`, the **primary** book) and a ₹1,00,00,000 book (`<manager>-1CR`, the **mirror**):
8 books in all.
- The manager sees and decides on its primary book only.
- After each session, the mirror is driven mechanically toward the primary's target weights, through
  its own rails. The participation rail may refuse or slice its orders over several sessions. That
  divergence is the capacity measurement, and it is journaled per book.
- Stops are declared once, in percent, and apply to both books.
- Both books hold at most 15 positions, at most 10% per position and at most 30% per sector.
- No extra model calls: the ₹1 cr book costs only execution.

(c) **Presentation order.** Each manager's desk lists the shortlist and screens in a deterministic
shuffle seeded by (manager id, session), with every name's rank still shown. This stops four managers
anchoring on the same top rows. Each manager's playbook names its starting screens first. Nothing is
hidden.

(d) **Controls.**
- **Primary, unchanged in kind:** every book has a `CTRL-<book>` equal-weighting the top 15 of the
  composite shortlist, on the same rails, costs and capital, rebalanced weekly for swing and every 21
  sessions for positional. 8 control books. BENCH-N500 is unchanged.
- **Secondary:** one `STYLE-<manager>` book per manager, ₹10 L, equal-weighting up to 15 names from
  its starting screens:
  - BRK: S2 ∪ S3, ranked S2 first;
  - EVT: S4;
  - TREND: S1;
  - FUND: S4 names passing the playbook's quality filter.

  Rebalanced on the same cadence. An empty screen means cash. Reported, never used for pass/fail.

(e) **Scoring.**
- §6's rule applies **per book**, each against its own `CTRL-<book>`.
- Brier is computed per manager, once, on its decisions.
- Results are reported as "k of 8 books passed" and, per manager, as passing on both, one or
  neither book.
- §6's graduation floor becomes: at least 2 of 4 managers pass on their primary (₹10 L) book, or one
  passes on its primary book across the extension window as well.

(f) **Prompt.** `manager.md` v1 carries four `[[STYLE]]` playbooks: `SWING_BREAKOUT`, `SWING_EVENT`,
`POSITIONAL_TREND` and `POSITIONAL_FUNDAMENTAL`. Each replaces the former `SWING` / `POSITIONAL`
block, and a manager sees only its own. The prompt's bytes stay in `mandate_hash`.

(g) **Dry run.** Dry sessions decided under the previous roster count toward wiring and timing only.
M17.8's "5 consecutive dry sessions" must include at least 3 on this roster before S0.

### Amendment 3 (2026-10-16, before S0): decision-contract and data fixes from the dry run

Owner-approved 2026-10-16. Evidence: the four dry sessions on the Amendment 2 roster (2026-10-12 to
2026-10-15, the `m17-dry` stream, 785 journal rows) and the review of their buy decisions. Three
manager BUYs and one SELL went through; the contract voided 33 decisions on the new roster, 25 of
them for `P_DISAGREES`. §2–§7 and Amendments 1–2 stand, except where this amendment changes them.

(a) **The p-versus-scenarios check (changes the contract and the prompt).** Amendment 1 (d) and
`contract.py` v1 required `|p_beat_bench − P*| ≤ 0.15`, with `P*` the mass of scenarios whose
excess is above zero. Almost every memo used BULL 0.30 / BASE 0.40 at +0.5 to +1% / BEAR 0.30, so
`P*` came to 0.70–0.75 and the check:
- voided 12 BUYs (all three of FM-SWING-BRK's among them) and one SELL;
- passed every executed BUY at exactly p = 0.55 against P* = 0.70, the band's edge — whether a buy
  executed turned on writing 0.54 or 0.55;
- pushed stated probabilities **up** towards 0.70, against the Brier criterion of §6. A base case of
  +1% excess, against base-rate IQRs of 20–25 pp, is a coin flip, not a certain win.

The rule from this amendment on, stated verbatim in `manager.md` (final round, step 8):

> `P*` is the probability mass of the scenarios whose excess is above +2 pp, plus half the mass of
> the scenarios within ±2 pp of zero. `p_beat_bench` must be on the same side of 0.5 as `P*`
> (either may be exactly 0.5) and within 0.20 of it; otherwise the decision is voided.

Why this rule: the dead band removes the artifact (a near-zero base case counted as a win) while
keeping a real check; the sign condition is the part that matters — a p that says "beats" against
scenarios that say "does not" is never consistent — and the band is widened from 0.15 to 0.20 because
three points are a coarse distribution. Replaying the 25 dry-run `P_DISAGREES` voids under it: all
25 pass the check (other checks still apply); none of the 28 accepted decisions that carried
scenarios fails it. The three cost checks of `contract.py` (model's own check, expected excess and
scenario mean, each net of the round trip) are unchanged and still carry the economics of a BUY.

(b) **One contract repair (changes the runtime).** When the contract voids a decision for
`P_DISAGREES`, the final call is re-asked once, with each such decision and all its reasons
appended. Only decisions on the voided names are read from the answer; every first-pass void is
journaled before it, the repaired decisions carry `contract_repair = true`, and a failed or
malformed repair journals `FM_CONTRACT_REPAIR_FAILED` and leaves the voids standing. Like the schema
repair it sits outside the 4-call budget of §4 step 3 and is journaled as an `FM_CALL` with its
tokens, so a session costs at most 4 calls plus 2 repairs (one schema, one contract).

(c) **Exits are never voided by memo arithmetic (changes the contract).** A `SELL` or `TRIM` is
not checked against its scenarios (shape, sum to 1, or (a)). EVT's 2026-10-15 SELL of Swaraj
Engines (INE277A01016), whose stated invalidation (a close below SMA50) had been hit, was voided by
the v1 check (p 0.48 against P* 0.30) and the position stayed open. An exit must fail safe. Every
other hard check on an exit stands: the schema (`what_changed` required), the holding existing, its
citations resolving, its weights, and the rails (min hold included).

(d) **F&O L2 is derived nightly (defect fix, Commons).** The prompt advertises open-interest
build-up, but every dossier since 2026-10-06 showed `fo_* unknown` ("no partition for the sessions
asked"): the nightly `nse_daily_capture` landed L1 `fo_contracts` only and nothing scheduled rebuilt
L2 `fo_aggregates`, which stopped at the last backfilled session (2026-10-05). The capture now
derives L2 from the same rows, the job heals any recent session with L1 and no L2, and a ranged
offline rebuild exists. Facts the managers were told they had, not a change of what they are told.

(e) **Unclassified names are not pooled by the sector rail (defect fix, rails).** §4 step 5's sector
cap grouped every ISIN the NSE industry map does not name under one `UNKNOWN` sector. On
STYLE-FM-SWING-BRK it refused 6 of 15 orders (`UNKNOWN` at 32.5%) and left the book 41% cash. Each
unclassified ISIN is now its own bucket (`UNCLASSIFIED:<isin>`), bounded by the 10% position cap
and the name count; a classified name is always grouped under its own label (a property test holds
every classified sector within 30% on generated streams); and if no classification can be read at
all, every name pools under `UNKNOWN` again (fail closed).

(f) **Hashes and the dry run.** (a)–(c) change `prompts/manager.md` and `contract.py`/the runtime,
so every manager's `mandate_hash` changes. No digest or hash of the prompt is pinned in a file or a
test (the hash is computed and journaled per session), so nothing pinned needs updating. Dry
sessions decided before this amendment count toward wiring and timing only: **M17.8's "5
consecutive dry sessions" must include at least 3 decided under the Amendment 3 mandate hashes
before S0**, which resets Amendment 2 (g)'s count (4 sessions on the Amendment 2 roster). (d) and
(e) are defect fixes of the kind §7 allows mid-window; landing them before S0 means no scored
session is affected.

(g) **Recorded and deferred — not part of this amendment.** The same review recommended these; none
is adopted here, and each would need its own dated amendment before S0 (or a new manager id after):
- price invalidations no tighter than about 1.5 × ATR, or confirmed over N closes, enforced in the
  contract like the stop rule (Apar and Swaraj invalidations sat at 1.3× and 0.9× ATR);
- a results-date dossier field (`sessions_to_results`) from the NSE board-meetings feed — 36% of web
  queries went on finding results dates, and S5's `results_board_meeting` category never matches;
- delivery-share signals: 5- and 20-session delivery share and percentile, results-day delivery,
  and day-0 volume net of matched bulk deals;
- the `EARNINGS_MOMENTUM` label only within `sessions_since_day0 ≤ 30`, S4's own window, with that
  field shown in the dossier;
- a secondary metric: the invested sleeve's return against CTRL and the average exposure, beside
  the unchanged primary;
- a red-flag dossier field classifying each announcement of the last 20 sessions against the
  integrity / related-party / dilution keyword table.

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
