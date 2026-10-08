# Runbook — M8.3 live-order sessions (plan for owner approval)

**Task:** M15.5 (real-money readiness item f) · **Written:** 2026-10-08 · **Status: PROPOSED — not approved.**
The owner has not approved M8.3. This document is the plan the owner approves or amends. Nothing in it has been
acted on: no Kite credential exists (AGENTIC_CONTEXT §2 B4), no broker was contacted, no order was placed.

Placing, modifying or cancelling a real order — including every tiny-capital test order — is reserved to the
human (AGENTIC_CONTEXT §3.5). So are credentials and accepting terms (§3.7), spending money (§3.9), real-money
policy ratification (§3.2) and graduation (§3.6, EXECUTION_PLAN §1 decision #8). Wherever this runbook says
"agent", the agent prepares, reads, compares and writes up; it never sends a write to the broker.

What M8 must prove (EXECUTION_PLAN §9, M8 gate): **10 sessions of tiny-capital live orders reconcile clean;
kill switch fired and verified mid-session; rules memo journaled.** Graduation remains the owner's call.

This file is the `ops/runbooks/live-orders.md` deliverable of TASK_GRAPH.yaml M8.3. The task's second
deliverable, `ops/gates/M8-live-plan.md`, is not written by this task; see §8.

Every claim below cites a file, a decision ID or the compliance memo. Items marked **Proposed — needs owner
decision** are proposals, not decisions; HUMAN_DECISIONS.md is not edited by this task.

---

## 0. Phases at a glance

| Phase | What happens | Money at risk | Orders sent | Entry gate |
|---|---|---|---|---|
| 1 — Preconditions | Close the checklist in §1 and the gaps in §4 | none | none | owner approves this plan |
| 2 — Dry run | ~5 sessions, real account read, `KiteBroker(dry_run=True)`, would-send vs paper | none | **none** (dry-run is send-free, `execution/kite_broker.py:16-20`) | §1 rows P1–P4, P8 done; gaps G1–G6, G10 closed |
| 3 — Tiny capital | 10 sessions, human releases each day's staged orders | the §1 P5 amount | yes, each released by the owner | phase 2 exit (§2.4); every gap in §4 closed |
| 4 — Graduation packet (M8.4) | Agents assemble evidence; owner decides | — | — | phase 3 exit (§3.6) |

---

## 1. Preconditions checklist

Status as of 2026-10-08. "Owner" = the human; "agent" = a build agent preparing evidence. A row is DONE only
when its evidence exists at the named place.

| # | Precondition | Owner | Evidence source | Status 2026-10-08 |
|---|---|---|---|---|
| P1 | **Zerodha's written answer on automated orders.** Kite Connect terms §2(e): the APIs "are not meant for placing fully automated trades (without manual intervention)" (memo `ops/compliance/sebi-algo-memo.md` §4.3, Q1; EXECUTION_PLAN §10 risk row). The reply should also confirm memo Q3 (self-hosting behind a static IP rests on NSE FAQ Q5) and Q4 (generic algo ID `99999`). | human (asks and keeps the reply) | Reply saved, credential-free, under `ops/compliance/` with its date; quoted in the M8.4 packet | **Not started** — no reply in the repo; HUMAN_DECISIONS.md "Coming up" still lists M8.3 with "Read … Q1 first" |
| P2 | **Static IP registered** on the Kite developer account. Mandatory for API order placement from 2026-04-01; at most 2 IPs; change at most once per calendar week (memo §3.3, §4.1; NSE/INVG/73992 §8.3.2.1.1, .2, .6). Host choice is memo Q2. | human (decision + spend, AGENTIC_CONTEXT §3.9) | Owner's confirmation that the registered IP is the egress IP of the host that will run phase 3. The IP itself is never written into the repo (CLAUDE.md, repo is public). | **Not started** — Q2 open |
| P3 | **Kite Connect app/subscription.** Personal (free) covers orders and portfolio; Connect is ₹500/month per API key and adds streaming + historical data (memo §4.4). Execution alone may need only the free tier (memo §4.4) — owner's call. | human (§3.7 terms, §3.9 spend) | Owner's confirmation; API key and secret placed only in `.env` as `KITE_API_KEY` / `KITE_API_SECRET` (`.env.example:107-108`, both `SecretStr` in `dataplatform/config.py:249-253`) | **Not started** — B4: no Kite credential exists |
| P4 | **Daily login / 2FA token policy.** Every API session is force-logged-out daily; re-login is OAuth + 2FA only (memo §3.3, Q5; NSE/INVG/73992 §8.3.2.1.8). Decide who logs in, when (proposed 08:30–09:00 IST, §3.3), where the day's access token lives, and that it is never in argv, a URL, a log or the journal (AGENTIC_CONTEXT §6 #13). | human decides; agent builds the storage (gap G3) | Decision recorded by the owner; G3 closed | **Open** — memo Q5 unanswered; no access-token setting exists (G3) |
| P5 | **Capital amount** for phase 3. | human | Owner's figure, written into the phase-3 session log (§3.5) | **Open.** **Proposed — needs owner decision:** an amount sized to the probe orders of §3.1, not to the paper book (the paper book is ₹10 lakh, `ops/runbooks/daily-eod.md` "The daily paper session"). Agent computes the per-session probe cost from the last close before phase 3 starts. |
| P6 | **D13 ratified for real money.** D13 ratified `PAPER_RATIFIED_2026_09_06` for paper only; "a real-money version is a separate ratification (AGENTIC_CONTEXT §3.2, B9)" (HUMAN_DECISIONS.md D13; D15). | human | A new HUMAN_DECISIONS entry ratifying a real-money policy (rails, cash policy, exit menu) | **Open.** Needed before phase 3 only if phase 3 trades the D13 basket (§3.1 option B); needed in all cases before graduation. |
| P7 | **Paper-period exit criteria met.** **Proposed — needs owner decision:** ≥20 decided paper sessions, including ≥1 rebalance that placed orders; 0 rail breaches; 0 recon breaks; one monthly evidence pack generated (`analyst/journal/evidence_pack.py:423` `generate_pack`). | agent assembles; owner accepts | `paper_session` ledger (`ops/runbooks/daily-eod.md` "Checking it" SQL), decision journal, the generated pack | **Not met.** One session decided: 2026-10-07, `COMPLETED`, reason `rebalance`, 0 orders (risk-off; read from `paper_session` on 2026-10-08). With the NSE holiday file (`dataplatform/ingest/data/nse_holidays.yaml`) the 20th session counted from 2026-10-07 is **2026-11-04**; the next rebalance is the first green session of November, **2026-11-02** (D24, `daily-eod.md` "Rebalance timing"). Whether it places orders depends on the regime filter. "0 recon breaks" is only meaningful once P10 lands — the paper job does not run `Reconciler` today. |
| P8 | **M15.1 secret scan merged.** D5: the scan must be mandatory and exist — pre-commit hook, `make check` scan and CI scan, each shown to fail on a planted fake — **before** the Kite credential exists on the machine. Kite is "the highest-severity credential in the system". | agent | Merged PR; `make check` runs the scan | **Not met.** `polly/m15-1-secret-scan` has no commits beyond main; `Makefile:17-22` `check` runs format, lint, mypy, pytest — no scan |
| P9 | **M15.2 disclosure text merged.** | agent | Merged PR | **Not met** — branch has no commits beyond main |
| P10 | **M15.3: paper on the shared staging / recon / kill-switch path.** Today the paper job runs `ReplayEngine` → `RailGate` → `SimBroker` (`ops/runbooks/daily-eod.md`), not `StagingCoordinator` / `Reconciler` / `KillSwitch` (`backtest/paper_session.py` has no reference to them). Invariant #5 needs the real path to have been exercised in paper first. | agent | Merged PR; paper sessions after it show a recon result per session | **Not met** — branch has no commits beyond main |
| P11 | **M15.4: backups + restore drill.** Existing drills ran 2026-08-08 and nothing leaves the host (`ops/runbooks/backup-restore.md` "The gap: nothing leaves this host"). The order journal and kill-switch state must be restorable before real orders exist. | agent | Merged PR; dated drill transcript | **Not met** — branch has no commits beyond main |
| P12 | **M8.1 and M8.2 done; M6 gate closed.** | agent | `BUILD_STATE.json` | **Done**: M8.1 `DONE` (commit `7ffaff7`), M8.2 `DONE` (commit `a1ade55`), M6.9 `DONE` (commit `432368a`; owner answer of 2026-09-03 in HUMAN_DECISIONS.md) |
| P13 | **Connector gaps of §4 closed** (G1–G6 and G10 before phase 2; all before phase 3). | agent builds; owner reviews the PRs | Merged PRs referencing the gap IDs | **Not met** — none started |
| P14 | **Memo re-captured.** The terms page is undated and changes silently (memo Q6); the memo is from 2026-08-08. Re-read Z1–Z3 and §5.3 before the first real order (memo §8, last bullet). | agent (read and write only) | Dated addendum to the memo | **Not started** |

---

## 2. Phase 2 — dry run (about 5 sessions, nothing sent)

### 2.1 What it proves

1. The live read side parses real payloads: `session_valid`, `holdings`, `positions`, `margins`, `ledger`
   (`execution/kite_broker.py:361-616`) raise no `KiteMalformedResponseError`. The fixtures under
   `tests/fixtures/kite/` are synthetic, not recorded (`tests/fixtures/kite/README.md`: "Values are synthetic
   but shape-faithful"), and the ledger path is a placeholder (ops/BACKLOG.md, M8.1 row). Phase 2 is the
   first contact with real payloads.
2. The would-send request for each order equals what the paper decision path decided — same ISIN, side,
   quantity — differing only in the fields the live path must add (LIMIT price, G2; pacing, G1).
3. The daily login (P4) works in practice for 5 consecutive sessions, and an unauthenticated morning
   produces `AUTH_REQUIRED` and no orders (`ops/runbooks/broker-reauth.md`).

Reads come from any IP; only order placement needs the static IP (memo §4.1, Zerodha Z1 verbatim). Phase 2
therefore does not depend on P2.

### 2.2 Sessions and timing

Proposed — needs owner decision: 5 consecutive sessions that **include the November rebalance (2026-11-02)**
if the preconditions allow, because the paper book only trades on a rebalance (D24, §8) and ordinary sessions
produce no orders to compare. If the window has no rebalance, the probe orders of §3.1 are generated in
dry-run every session instead, so every session has something to compare.

Daily, IST (market hours 09:15–15:30, as used in `ops/runbooks/daily-snapshotter.md` and
`ops/studies/evidence/multi-fund-data-requirement.md`):

| Time (IST) | Who | Step |
|---|---|---|
| D 21:45 | scheduler | `paper_session` decides session D (existing job, `ops/runbooks/daily-eod.md`) |
| D 22:00 | agent (driver, gap G9) | Build the D+1 order set from the paper decision through `KiteBroker(dry_run=True)`; log each would-send `KiteRequest` (`kite_broker.dry_run` event, `kite_broker.py:628-636`) |
| D+1 08:30–09:00 | owner | Kite login with 2FA; install the day's access token where G3 puts it |
| D+1 09:00 | agent | Read-only: `session_valid`, `holdings`, `positions`, `margins`, `ledger`; save the payloads with every credential and personal field removed (AGENTIC_CONTEXT §6 #13: fixtures must be credential-free) |
| D+1 16:00 | agent | Write the session's comparison row (§2.3) |

### 2.3 The comparison, per session

| Field | Paper side | Dry-run side | Pass condition |
|---|---|---|---|
| Orders | `paper_session.orders` for D (`daily-eod.md` "Checking it") | would-send requests logged at D 22:00 | Same count; per order same ISIN, side, quantity |
| Order type | MARKET today (G2) | LIMIT with a price (after G2) | Every would-send order is LIMIT; price rule as decided in G2 |
| Pacing | — | timestamps of the would-send log | No more than 2 per wall-clock second; daily count ≤ cap (G1) |
| Account read | — | payload parse result | No `KiteMalformedResponseError`; `holdings` t1 quantity handled (G7) |
| Auth | — | `session_valid` result | `True` on every morning the owner logged in |

### 2.4 Exit criteria for phase 2

All five sessions pass §2.3 with zero send attempts; captured payloads have replaced the synthetic fixtures
for holdings, positions, margins and ledger (ops/BACKLOG.md M8.1 row); a deliberate skip of one morning's
login produced `AUTH_REQUIRED` and no orders. Agent writes the result to `ops/gates/` and stops; the owner
decides to start phase 3.

---

## 3. Phase 3 — tiny capital (10 sessions, owner releases every order)

### 3.1 What gets traded

The paper book rebalances once a month (D24, §8), so 10 consecutive sessions of the D13 basket would send orders
on at most one or two days. The gate wants 10 sessions of live orders reconciling clean.

**Proposed — needs owner decision:**

- **Option A (recommended): probe orders every session.** One or two whole-share LIMIT orders per session in
  a single liquid instrument — proposed: the cash manager's parking ISIN (`analyst/cash/manager.py:448-465`,
  default constant noted in ops/BACKLOG.md M5.7 row) — alternating buy and sell so the position returns to
  zero by session 10. This exercises place → fill → recon every day. It exercises the execution path only,
  not the analyst decision path; that is what phase 2 compared.
- **Option B: mirror the D13 book scaled to P5.** Needs P6 (real-money ratification). Whole-share rounding at
  a tiny capital will not hold all 20 names, so the live book will diverge from paper by construction.

### 3.2 Session roles

- **Owner:** login, reviews the release sheet, releases (or refuses) the orders, cancels at the broker if
  needed, signs the day's log line. Every order write is a human action at the call site
  (`live_confirm=True`, `kite_broker.py:381-411`; AGENTIC_CONTEXT §3.5).
- **Agent:** prepares the staged set and release sheet, runs the read-only pre-flight and status polls, runs
  recon, writes the session record. Never passes `live_confirm`.

### 3.3 Daily procedure (IST) — session D+1, executing what was staged on D

| Time (IST) | Who | Step | Stop if |
|---|---|---|---|
| D 21:45 | scheduler | `paper_session` decides D (unchanged; it never reaches `KiteBroker` — `tests/unit/test_paper_session_paper_only.py`) | — |
| D 22:00 | agent | Stage D+1's orders through `StagingCoordinator` (after G9) into the order journal; write the release sheet: per order ISIN, symbol, side, quantity, LIMIT price, rupee value, rails result | any rail block → order is not on the sheet (invariant #6) |
| D+1 08:30–09:00 | owner | Kite login with 2FA; install the day's token (G3) | login fails → AUTH_REQUIRED day, no orders (`broker-reauth.md`) |
| D+1 09:00 | agent | Pre-flight, read-only: `session_valid` true; kill switch armed (`KillSwitch.is_tripped` false, `kill_switch.py:178-181`); `/status/sync` green for D (invariant #10); last recon clean; staged count ≤ daily cap (G1) | any fails → no release today; journal why |
| D+1 09:30 | owner | Read the release sheet; release with the CLI (G4) which passes `live_confirm=True`, paced at ≤2 orders/s (G1). 09:30 rather than 09:15 to stay clear of the opening minutes — **Proposed — needs owner decision.** | a sheet line the owner does not recognise → release nothing |
| D+1 09:30–15:15 | agent | Poll order status read-only every 15 min (proposed); a REJECTED order is journaled and alerted (G6, G8) | rejection → owner reviews before any further release |
| D+1 15:30 | — | Market close. Unfilled orders expire: the adapter sends `validity=DAY` only (`kite_broker.py:78-83`). No modify or chase (memo §5.3: 0–1 modifications per order). | — |
| D+1 16:00 | agent | Reconcile (`Reconciler.reconcile`, `execution/recon.py:209-233`): positions and cash vs the internal book. Any break trips the kill switch (source `RECON`) and alerts (G8). | break → abort procedure §5 |
| D+1 16:15 | owner | Compare the day's contract note with the cost model's charges for the same fills (`execution/costs/rates.yaml`, schedule `effective_from: 2025-04-01`, `provenance: verified`); sign the session log line | unexplained charge difference → recorded as a finding; cash recon will also show it |

### 3.4 Kill-switch drill (once, mid-session)

Proposed — needs owner decision: session 5 of 10, at about 11:00 IST, with at least one order still open.

1. Owner trips the switch with the CLI (G4), source `MANUAL` (`kill_switch.py:59-67`), with a reason.
2. Verify placement is refused: a release attempt raises `TradingHaltedError` from
   `require_placement_allowed` (`kill_switch.py:183-191`) and nothing is sent.
3. Verify the latch survives a restart: a fresh `KillSwitch` on the same state file reads back tripped
   (`kill_switch.py:12-16`).
4. Verify the alert reached the owner's channel (G8).
5. **Open orders are not cancelled by the switch** — it only blocks new placement (`kill_switch.py:183-235`
   has no cancel path). Owner cancels the open order (CLI, or Kite web/app directly) and confirms CANCELLED.
6. Reset with a note (`KillSwitch.reset`, `kill_switch.py:221-235`). Record the trip and reset timestamps,
   the refused attempt and the alert receipt in the session log.

### 3.5 Session log (one row per session)

Kept by the agent in `ops/gates/` (proposed file `M8-live-sessions.md`), signed by the owner:
date · capital at open · orders staged / released / filled / rejected / expired · max orders per second
observed · recon result (`ReconResult.ok`, breaks listed) · contract-note charges vs cost model · auth (login
time) · kill-switch events · owner's initials. No account number, client ID, IP or token in the log.

### 3.6 Exit criteria for phase 3

10 sessions with live orders, every session's recon clean (`ReconResult.ok` true); the drill of §3.4 done and
recorded; observed order rate re-checked against memo §5.3 (memo §8 asks for measured numbers, not the
design envelope); zero orders sent without the owner's release. Then M8.4.

---

## 4. Known gaps in the Kite connector (must close before phase 3)

Read from the code on 2026-10-08. These are gaps, not fixes; each is agent-buildable and needs no credential
except where noted. "Phase" is the first phase that cannot run without the fix.

| ID | Gap | Evidence | Needed for | Proposed closure |
|---|---|---|---|---|
| G1 | **No order-rate cap, no daily order cap.** `place`/`modify`/`cancel` call the transport directly; there is no token bucket or counter. | `execution/kite_broker.py:381-476`; memo §5.4 item 1 asks for a 2 orders/s wall-clock-second bucket, a hard ceiling below 10, and a daily counter that refuses rather than queues | phase 2 (to log pacing), phase 3 | Paced gateway on the single order path: 2/s, ceiling < 10 (NSE TOPS 10/s, memo §3.2), daily cap **50** (memo §5.2 design envelope — Proposed); refusals journaled |
| G2 | **Market orders are not refused.** `place_request` sends any `order_type`, MARKET included, with no market protection. MARKET is the interface default and the cash manager's parking buy. The order journal hard-codes `'MARKET'`. NSE: "Algo orders with order type as Market Order are not permitted". | `kite_broker.py:330-345`; `execution/broker.py:156` (default `OrderType.MARKET`); `analyst/cash/manager.py:461`; `execution/staging.py:288`; memo §3.3 (NSE/INVG/73992 §8.1.12), §5.4 item 2 | phase 2 | `KiteBroker` refuses MARKET; staged EOD orders become LIMIT with a price rule (owner decision); SimBroker fill model and journal follow so paper and live agree (memo §5.4 item 2) |
| G3 | **No access-token handling.** Settings hold API key and secret only; there is no access-token setting, no request-token → access-token exchange, and nothing constructs `LiveKiteTransport`. The transport takes `access_token` as a plain `str`. `broker-reauth.md` step 2 says to install the token "where the broker adapter reads it" — no such place exists. | `dataplatform/config.py:249-253`; `kite_broker.py:727-742`; `ops/runbooks/broker-reauth.md` "How to re-authenticate" | phase 2 | Daily token as a `SecretStr` read from the environment or a mode-600 untracked file, never argv/URL/log (AGENTIC_CONTEXT §6 #13); a login helper the owner runs; P4 decides the flow |
| G4 | **No kill-switch CLI and no configured state file.** `KillSwitch` is used only inside `execution/` and tests; no command trips, resets or shows it, and no setting names its path. There is likewise no release CLI that passes `live_confirm`. | `grep KillSwitch` outside `execution/` and `tests/` finds nothing; `kill_switch.py:165-168` takes the path as an argument | phase 3 | One CLI: `status`, `trip --reason`, `reset --note`, and `release <session>` (prints the sheet, asks for confirmation, passes `live_confirm=True`) |
| G5 | **Nothing wires `BROKER_PROVIDER=kite` to a `KiteBroker`, and there is no production `InstrumentResolver`.** | `BrokerProvider` is read nowhere outside `dataplatform/config.py:202` and tests; `InstrumentResolver` has no implementation outside `kite_broker.py` and tests | phase 2 | Factory from settings; resolver over the identity master (D2), ISIN ↔ `tradingsymbol` |
| G6 | **Broker-rejected orders are not handled.** `place` returns `STAGED` and never re-reads; `StagingCoordinator.execute` skips non-COMPLETE orders, so a rejected order stays `STAGED` in `order_` forever; `REJECTED` is never written. A `KiteRequestError` at placement (RMS reject, input error, HTTP 429) propagates with no journal entry. | `kite_broker.py:394-411`; `execution/staging.py:464-491`; ops/BACKLOG.md M5.12 row ("A broker-*rejected* staged order is not transitioned") | phase 3 | Journal `REJECTED` with Kite's `status_message`; placement errors journaled and alerted |
| G7 | **`holdings()` ignores `t1_quantity`.** Shares bought but not yet settled sit in `t1_quantity` in Kite's holdings payload (the field is in the fixture); `holdings()` reads `quantity` only. If Kite shows a previous session's buy only as `t1_quantity`, recon flags a phantom break the day after every buy. *(The Kite semantics are assumed from the fixture shape; confirm with a phase-2 payload.)* | `kite_broker.py:532-558`; `tests/fixtures/kite/holdings.json` (`"t1_quantity": 0`); `recon.py:235-263` sums holdings + positions | phase 3 | Include `t1_quantity` in the recon quantity once confirmed against a live payload |
| G8 | **No alert reaches a human on a recon break, kill-switch trip or rejection.** `recon.Alerter` and `session.AuthAlerter` are separate protocols whose production defaults are log lines. `dataplatform/alerts.py` has email and Telegram channels but they are not wired to recon or the kill switch, and `ALERT_PROVIDER` defaults to `log`. `KillSwitch.trip` alerts nobody itself. | `execution/recon.py:83-123`; `execution/session.py:141-183`; `dataplatform/alerts.py:1-26`; `dataplatform/config.py:205-206`; `kill_switch.py:193-219` | phase 3 | Adapt recon, auth and kill-switch alerts onto `dataplatform/alerts.py`; owner sets a non-log `ALERT_PROVIDER` (a credential → owner, §3.7) |
| G9 | **No execution driver for a real broker.** `StagingCoordinator.broker` is typed `SimBroker`; `stage` calls `broker.place(request)` without `live_confirm` (with a live `KiteBroker` this raises `LiveOrderNotConfirmedError`); `execute` calls `SimBroker.execute_session`, which is not on the `Broker` protocol. The paper job refuses any non-`SimBroker`, so nothing produces would-send orders for phase 2 either. | `execution/staging.py:421, 439, 472`; ops/BACKLOG.md M5.12 row ("execution-driver seam"); `tests/unit/test_paper_session_paper_only.py` | phase 2 (dry-run driver), phase 3 (live driver) | Execution-driver seam: paper = step `SimBroker`; real = place on release, poll order status, book fills from exchange reports |
| G10 | **Ledger path is a placeholder.** `/ledger` is not a Kite Connect REST endpoint; the cash statement comes from Zerodha's Console reports API with a different endpoint and auth. | `kite_broker.py:90, 560-590`; ops/BACKLOG.md M8.1 row | phase 2 (first live read) | Confirm the real endpoint against a captured response in phase 2 (needs the credential → owner logs in), refreeze `tests/fixtures/kite/ledger.json` |
| G11 | **Transport errors and ambiguous placement.** httpx timeouts and connection errors are not mapped to `KiteApiError`; a timeout after the POST leaves it unknown whether the order exists, and there is no tag-based check before a retry. `order()` maps every `KiteRequestError` (a 429 included) to `UnknownOrderError`. | `kite_broker.py:744-757`, `485-489` | phase 3 | Map transport errors; never auto-retry a place; on ambiguity read the order book by `tag` before anything else |
| G12 | **Cash recon is exact, against figures not yet seen live.** Recon compares `book.cash` with `margins().cash_value` with zero tolerance; `margins()` maps `available.live_balance` and `utilised.debits` and leaves `unsettled_proceeds` at zero. Whether those Kite fields equal the book's post-cost cash on the day of a fill — and when the depository charge is debited — is unverified. | `recon.py:265-271`; `kite_broker.py:592-616`; `execution/broker.py:277-303` | phase 3 | Verify the mapping on phase-2 payloads and the first phase-3 contract note; any change to the tolerance (there is none) is an owner decision, not a fix |

---

## 5. Abort and rollback

### 5.1 Abort triggers (any one, any phase)

- A recon break (`ReconResult.froze`) that the owner cannot explain from the day's contract note by 18:00 IST
  (proposed time).
- Any order reached the broker that the owner did not release.
- Any observed rate above the G1 cap, or any HTTP 429 from Kite.
- A rail breach on a live order (should be impossible — rails block, EXECUTION_PLAN §5.7).
- Suspected exposure of any Kite credential or token (D5: "treat … as an incident, not a cleanup").
- Zerodha's answer to P1 says the use is not within the terms.

### 5.2 Procedure

1. **Halt:** owner trips the kill switch (`MANUAL`, reason stated). Placement stops (`kill_switch.py:183-191`).
2. **Cancel open orders at the broker** — Kite web or app, by the owner. The switch does not cancel them (§3.4).
3. **Leave positions as they are.** No agent sells anything. Whether to exit a position is an owner decision
   on the next session, made outside this system.
4. **If a credential may be exposed:** follow `ops/runbooks/secret-leak.md` — rotate first (§1 of that
   runbook): log out the session, regenerate the API secret on the developer console.
5. **Roll back to paper:** set `BROKER_PROVIDER=stub` in `.env` (the default, `.env.example:69`), remove the
   day's access token, restart the scheduler (`systemctl --user restart scheduler`, `daily-eod.md`). The paper
   job is unaffected throughout (it never reads `BROKER_PROVIDER`, `daily-eod.md`).
6. **Write it down:** agent writes an incident note under `ops/gates/` (what happened, the journal rows, the
   recon breaks); the kill switch stays tripped until the owner resets it with a note.

### 5.3 Who to call

The owner is the only human in this system and the only one who acts. Broker-side help is Zerodha support
(the support portal, or the contact the Kite app lists) — the owner keeps that contact and the client ID
off the repo. Agents contact no one.

---

## 6. M8.4 graduation packet — contents

Agents assemble; the owner decides; the packet carries no recommendation to graduate (TASK_GRAPH.yaml M8.4
acceptance). Deliverable `ops/gates/M8-graduation-packet.md`, readable in ten minutes.

1. Evidence pack from the paper record (`generate_pack`, `analyst/journal/evidence_pack.py:423`): returns vs
   both benchmarks, drawdown, rail-breach count, decision review, turnover and tax events, token/cost burn,
   data-quality skips (EXECUTION_PLAN §5.7).
2. Rail-breach record and `SKIPPED_DATA_RED` / `AUTH_REQUIRED` record over the paper period and phases 2–3.
3. The M8.2 memo plus its pre-first-order addendum (P14), and Zerodha's written answer (P1).
4. Phase-2 comparison table (§2.3) and phase-3 session log (§3.5), with every recon result.
5. Kill-switch drill record (§3.4).
6. Measured order rate vs memo §5.3 thresholds.
7. Contract-note charges vs cost-model charges, per fill.
8. Gap table §4 with the closing PR for each gap.
9. Open items the owner should know: the real-money ratification state (P6), M6.8 findings that block
   autonomous trust in T1 (ops/BACKLOG.md "M6.8 (F1)" row), and memo Q7 (product stage is a different regime).

---

## 7. Decisions this plan asks the owner for

All **Proposed — needs owner decision**; none is recorded in HUMAN_DECISIONS.md by this task.

1. Approve, amend or reject this plan.
2. P7 paper-period exit criteria as written (≥20 sessions, ≥1 rebalance with orders, 0 rail breaches,
   0 recon breaks, one monthly pack).
3. P5 capital amount, and §3.1 option A (probe orders) or B (mirror D13, needs P6).
4. G1 daily order cap of 50; G2 LIMIT price rule.
5. §3.3 release time 09:30 IST and §3.4 drill on session 5 at about 11:00 IST.
6. P4 login and token policy (memo Q5); P2 host and static IP (memo Q2); P3 Kite Connect tier (memo §4.4).

---

## 8. Notes

- **Deliverables.** TASK_GRAPH.yaml M8.3 names `ops/runbooks/live-orders.md` (this file; its `verify` is
  `test -s ops/runbooks/live-orders.md`) and `ops/gates/M8-live-plan.md`, which this task does not write —
  the plan content lives in this runbook.
- **Decisions D23–D24** (orchestrator decisions 2026-10-06/07, pending entry in HUMAN_DECISIONS.md; recorded in the
  orchestrator's untracked decision log and, for D24, PR #69). They
  bear on M8.3 through the paper record that P7 and phase 2 compare against:
  - **D23** — the paper regime input must be the same TRI series as the backtest, else the job ships
    disabled. It shipped disabled; M13.7 then added the same-evening TRI fetch (`ops/runbooks/daily-eod.md`).
    A rebalance whose TRI did not land is `SKIPPED_DATA_RED`, which delays P7's "≥1 rebalance with orders".
  - **D24** — paper timing differences vs the backtest are accepted: mid-month start, a missed rebalance
    rolls to the next green session, and orders lapse unfilled on red days. A real broker would fill an
    order the paper book lets lapse, so a phase-2 comparison across a red day is expected to differ.
- What was verified and what was assumed: `ops/gates/M15.5-m8-3-plan-2026-10-08.md`.
