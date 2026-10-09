# Runbook: M17 AI fund managers — the daily desk

The M17 forward paper test (`ops/studies/preregistration-m17-ai-fund-managers-2026-10-09.md`): four
LLM managers, their four no-LLM control books and `BENCH-N500`, run once a trading session by the
scheduler job `m17_fund_managers` (`backtest/fm_job.py`, wired to the lake by `backtest/fm_world.py`).
Every book is a paper book on the M15.3 staging / reconciliation / kill-switch path; nothing here can
reach a real broker. All times are **IST**.

## Daily timeline (Monday to Friday)

| IST | What | Where |
|---|---|---|
| 09:15–15:30 | Market session D. Last night's staged orders fill at D's open (paper, `SimBroker`). | — |
| 18:30 | EOD pipeline publishes D (the interlock reads it). | `eod_pipeline` |
| 19:50 / 20:50 / 21:30 | Same-evening TRI, **now including NIFTY 500** (the bench). | `tri_evening` |
| 20:35 / 21:10 / 21:45 | Same-evening index close/PE/PB/yield (NSE close-all file) and India VIX into `macro_series`, catching up any missed session; a fire after D landed makes no request (M17.10). | `index_close_evening` |
| 21:45 | The D13 momentum paper session (unrelated book). | `paper_session` |
| **22:00** | **`m17_fund_managers` starts for session D.** | this job |
| 22:00 + seconds | Interlock → fills at D's open → reconciliation → `BOOK_MARK` for all nine books → due `DECISION_OUTCOME`s → mechanical stops. | |
| up to 00:00 | Waits (at most 120 min, polling every 10) for D's index/VIX levels, D's NIFTY 500 TRI and the L2 refresh; past that it runs **with the gaps recorded**. | `M17_DATA_WAIT`, `M17_DATA_GAPS` |
| + ~8 min | Commons build: sheets, shortlist, screens + regime, filing digests (screens ∪ shortlist), frozen base rates. | |
| through the night | Managers one at a time: `FM-SWING-10L`, `FM-SWING-1CR`, `FM-POS-10L`, `FM-POS-1CR`. At most 4 model calls each (+1 repair). Rate limits back off (2, 4, 8, 16 min) inside the deadline. | `FM_*` events |
| then | Control books rebalance (weekly / every 21 sessions) on the same shortlist. Desk state recorded; scoreboard built and cross-checked against the journal; digest written. | |
| **08:30 D+1** | **Deadline.** A manager not finished journals `MISSED_SESSION` and stages nothing of its own; its stop exits still stage. | |
| 09:15 D+1 | The staged orders fill at D+1's open; the 22:00 run on D+1 books them. | |

Holidays are skipped inside the job (the NSE holiday calendar). A rerun of a completed session is a
no-op; a red session can be rerun once the data heals.

## Streams: dry run and live

- **Until M17.8's go the registered job runs the dry stream** (`M17_DRY_RUN = True` in
  `dataplatform/scheduler/registry.py`). Every dry entry is journaled under `case_id`
  `m17-dry:<book id>` with `payload.stream = m17-dry`; the desk state is the `paper_session` row
  `m17_dry_fund_managers`; the digest goes to `~/campaign/m17/dry/`. Nothing of a dry run reaches the
  live streams, `/status/managers` or the live scoreboard. Nothing counts (pre-registration §6).
- **Live** streams use the book ids themselves (`FM-SWING-10L`, …) and the `paper_session` row
  `m17_fund_managers`; the digest goes to `~/campaign/m17/`. The live stream refuses to run before S0.

A rehearsal from a shell that persists nothing (reads the lake and the status DB only; scratch kill
switch and fetch cache; StubLLM; no data wait):

```bash
DATA_ROOT=/home/ubuntu/stock-manager/data \
  uv run python -m backtest.fm_job --memory --stub-llm --dry-run --no-wait --session 2026-10-09
```

It prints the per-stage timings and each manager's status as JSON.

## Starting S0 (owner's go, after the 5-session dry run passes)

S0 is the first session after the go (pre-registration §9). The books open **at S0's open, all in
cash** — S0 itself fills nothing — and the first decisions are made on S0's close and fill at S0+1's
open. The bench is bought at the close of the session before S0.

1. One reviewed commit: `M17_DRY_RUN = False` in `dataplatform/scheduler/registry.py`, and §9's S0 row
   filled in. Merge; **do not restart the scheduler yet**.
2. On S0's evening, after 21:45, run the start by hand (it journals `M17_S0` and every book's
   `mandate_hash`, then runs the session):

   ```bash
   uv run python -m backtest.fm_job --start <S0> --session <S0>
   ```

   `--start` must equal the session the run decides, and a stream starts once (a second `--start`
   is refused).
3. Restart the scheduler so the next evenings run live:
   `XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart scheduler` (and `status scheduler`).

The `mandate_hash` of a manager covers its roster entry, `prompts/manager.md`'s bytes, both decision
schemas with the system prompt and the contract's tolerances, `SHORTLIST_RULE_HASH`,
`SCREENS_RULE_HASH` and the shared rails (`analyst/fundmanager/job.py::mandate_fingerprints`). A
changed hash after S0 is a **new manager** with a new id (§7) — never edit a running one.

## Reading `/status/managers` and the digest

`GET /status/managers` rebuilds the scoreboard from the live journal on every call:

- `s0`, `sessions_elapsed`, `k_of_n` (always "*k* of 4 passed", never a lone winner);
- `books[]`: each book's latest mark — NAV, return since its opening capital, cash, positions;
- `managers[]`: phase, verdict (`NOT_STARTED`, `IN_PROGRESS`, `PASS`, `CLEAR_FAIL`, `INCONCLUSIVE`
  → one extension to S0+125), excess vs control, excess vs bench, max drawdown vs the bench's,
  Brier on `p_beat_bench` and how many decisions have resolved (Brier speaks from 30);
- `scoreboard_error`: the journal could not be scored (a missing mark, a malformed line) — no
  numbers are shown rather than partial ones;
- `decisions[]`: the latest session's decision lines (what, never why — no rationale or prompt).

The digest `~/campaign/m17/digest-<session>.md` holds the same numbers plus that session's
decision table. The job also checks its own ledger against a rebuild from the journal every session;
a disagreement is logged `fm_job.scoreboard_mismatch` and reported as `scoreboard_error`.

## The kill switch

One switch stops every M17 book, dry and live: `<DATA_ROOT>/kill_switch/m17_fund_managers.json`.

```bash
uv run python -m execution.kill_switch status --account m17_fund_managers   # exit 3 when tripped
uv run python -m execution.kill_switch trip   --account m17_fund_managers --reason "why"
uv run python -m execution.kill_switch reset  --account m17_fund_managers --note "why it is safe"
```

A tripped switch makes every book lapse the orders due that session, journal `KILL_SWITCH_TRIPPED`,
stage nothing and call no model; the books are still marked. The job trips it itself on a
reconciliation break (`RECON_BREAK`) and on a corporate action on a held name it cannot book
mechanically (`CORPORATE_ACTION` with status `ESCALATED`) — investigate, then reset with a note.
Acting on a book by hand ends that manager's test (`OWNER_INTERVENTION`, §7).

## What each journal event means

Per book (`payload.event`; `decision` in brackets):

| Event | Meaning |
|---|---|
| `M17_S0` [HEARTBEAT] | S0 is this session; payload carries the book's `mandate_hash` and its parts. |
| `SKIPPED_DATA_RED` [SKIPPED_DATA_RED] | The interlock was red (or unreadable): nothing decided or staged. |
| `KILL_SWITCH_TRIPPED` [SKIPPED_DATA_RED] | The M17 switch was tripped: due orders lapsed, nothing staged. |
| `ORDERS_LAPSED` [HOLD] | Orders staged for a session the desk did not execute (red/missed) lapsed. |
| `RECONCILIATION` [HEARTBEAT] / `RECON_BREAK` [ESCALATE] | The book vs the paper broker after the fills; a break trips the switch. |
| `CORPORATE_ACTION` [HOLD / ESCALATE] | A split, bonus, dividend, … on a held name booked (or escalated). |
| `UNFILLED_UPPER_CIRCUIT` [HOLD] | A buy whose fill session was locked at the upper band, left unfilled. |
| `BOOK_MARK` [HEARTBEAT] | The book's mark at the close (every book, every session). |
| `DECISION_OUTCOME` [HEARTBEAT] | A decision resolved: the name's and the bench's return over its horizon (or a BUY's exit). |
| `STOP_EXIT` [SELL] | A close below a declared stop: sold at the next open, **no model call**. A stop inside the 2-session min-hold window is refused by the rail (`RAIL_BLOCK`) and offered again next session. |
| `M17_DATA_WAIT` / `M17_DATA_GAPS` [HEARTBEAT, desk-level] | The wait for the session's data, and what was still missing when the job ran anyway. |
| `FM_CALL` [HEARTBEAT] | One model call with its prompt digest, token counts and evidence pack. |
| `FM_RESEARCH_FULFILLED` / `FM_RESEARCH_TRUNCATED` | What research the harness handed over, and what an over-long request lost. |
| `FM_DECISION` [BUY/SELL/HOLD] | An accepted decision (scored for Brier). |
| `FM_DECISION_REFUSED` [HOLD] | A decision the contract voided (never staged). |
| `NO_ACTION` [HEARTBEAT] | The manager's "nothing today", with its reason. |
| `MANAGER_ERROR` [ESCALATE] | The runtime failed (malformed twice, a failed call): nothing accepted. |
| `MISSED_SESSION` [ESCALATE] | Not finished by 08:30 IST next session (late answer, or a rate limit that could not be waited out). |
| `MANAGER_CRASHED` [ESCALATE] | The manager's session raised; isolated, the others ran. |
| `COMMONS_UNAVAILABLE` [ESCALATE] | The Commons could not be built; the manager was not asked. |
| `STAGED` [BUY/SELL] | An order staged for the next open, after the rails. `RAIL_BLOCK` names every rail that refused one. |
| `EXIT_COMPLETE` / `EXIT_SUPERSEDED` | An over-participation exit worked across sessions finished, or was replaced. |
| `CONTROL_REBALANCE` [HEARTBEAT] | A control book's rebalance targets. |

## When something goes wrong

- **Job FAILED** (`/status/jobs`, `failure_alerts` pages it): the whole session rolled back. Fix the
  cause and rerun the same session: `uv run python -m dataplatform.scheduler run-once m17_fund_managers`
  before 08:30 IST next session, or let the next evening run (the missed session's orders are not
  caught up — they lapse).
- **A held name with no close that has not delisted** (a suspension) fails the mark loudly by design
  (a gap is a data fault). Delisted names (identity master listing record) are valued at their last
  traded close.
- **`repo_rates.yaml` coverage** ends at the last MPC decision entered; a session past it raises
  `RepoRateCoverageError`. Extend it from RBI's own press release after each MPC meeting.
- **Holiday calendar** `nse_holidays.yaml` ends 2026-12-31: add the 2027 NSE circular before December.
