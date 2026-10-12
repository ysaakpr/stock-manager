# Runbook: M17 AI fund managers — the daily desk

The M17 forward paper test (`ops/studies/preregistration-m17-ai-fund-managers-2026-10-09.md`, roster
as §8 Amendment 2 sets it): four LLM managers with distinct styles, each trading a primary ₹10 L book
and a mechanical ₹1 cr mirror (8 manager books), a no-LLM `CTRL-<book>` per manager book (8 controls),
a secondary `STYLE-<manager>` book per manager (4) and `BENCH-N500` — 21 books — run once a trading
session by the scheduler job `m17_fund_managers` (`backtest/fm_job.py`, wired to the lake by
`backtest/fm_world.py`). Every book is a paper book on the M15.3 staging / reconciliation /
kill-switch path; nothing here can reach a real broker. All times are **IST**.

## The roster (Amendment 2)

| Manager | Style (`[[STYLE]]` playbook) | Horizon | Starting screens | Books | Style book list |
|---|---|---|---|---|---|
| `FM-SWING-BRK` | `SWING_BREAKOUT` | 5–20 sessions | S2, S3 | `FM-SWING-BRK-10L`, `-1CR` | S2 then S3 |
| `FM-SWING-EVT` | `SWING_EVENT` | 5–20 sessions | S4, S5 | `FM-SWING-EVT-10L`, `-1CR` | S4 |
| `FM-POS-TREND` | `POSITIONAL_TREND` | 20–60 sessions | S1 | `FM-POS-TREND-10L`, `-1CR` | S1 |
| `FM-POS-FUND` | `POSITIONAL_FUNDAMENTAL` | 20–60 sessions | S4 + dossier fundamentals | `FM-POS-FUND-10L`, `-1CR` | S4 passing the quality filter |

- **The manager sees and decides on its `-10L` book only.** Its prompt carries its own playbook and
  nothing of its mirror; its screens and the shortlist are listed in a shuffle fixed by (manager,
  session), each name with its rank, its starting screens first (`render.DeskOrder`).
- **The `-1CR` mirror follows mechanically** (`analyst/fundmanager/mirror.py`): after the manager's
  orders are staged, the mirror is driven toward the primary's post-decision weights through its own
  rails. A name the primary acted on is followed exactly; any other name only when it is more than
  10 % of its target off (catching up a move it could not make). A buy is offered whole when it fits
  cash and a free slot, never resized — one the participation rail refuses is `RAIL_BLOCK` on the
  `-1CR` only and offered again next session; a sell may be sliced over sessions. Stops are the
  primary's `stop_pct`, declared at the mirror's own staging close, and its tightened levels; a name
  the mirror's own stop sold is not bought back while the primary still holds it. Every session the
  mirror journals `MIRROR_DIVERGENCE`: per name, the primary's target weight, the mirror's weight once
  its orders fill, and why they differ (`STAGED`, `SLICED`, `EXITING`, `REFUSED` + rails, `CASH`,
  `SLOTS`, `UNPRICED`, `STOPPED`), plus `tracking_gap_pp`. That divergence is the capacity measurement.
- **Both books hold at most 15 positions**, 10 % a position, 30 % a sector. No extra model calls.
- **Style books** (secondary, never pass/fail): ₹10 L, equal weight `0.98 × mark / 15` over up to 15
  names of the list above, on the family's cadence; an empty list means cash. FUND's quality filter:
  a positive TTM profit (TTM net margin or TTM P/E positive) and debt-equity below 1.5 outside
  Financial Services (an unknown never passes).

## Daily timeline (Monday to Friday)

| IST | What | Where |
|---|---|---|
| 09:15–15:30 | Market session D. Last night's staged orders fill at D's open (paper, `SimBroker`). | — |
| 18:30 | EOD pipeline publishes D (the interlock reads it). | `eod_pipeline` |
| 19:50 / 20:50 / 21:30 | Same-evening TRI, **now including NIFTY 500** (the bench). | `tri_evening` |
| 20:35 / 21:10 / 21:45 | Same-evening index close/PE/PB/yield (NSE close-all file) and India VIX into `macro_series`, catching up any missed session; a fire after D landed makes no request (M17.10). | `index_close_evening` |
| 21:45 | The D13 momentum paper session (unrelated book). | `paper_session` |
| **22:00** | **`m17_fund_managers` starts for session D.** | this job |
| 22:00 + seconds | Interlock → fills at D's open → reconciliation → `BOOK_MARK` for all 21 books → due `DECISION_OUTCOME`s → mechanical stops (every manager book, mirrors included). | |
| up to 00:00 | Waits (at most 120 min, polling every 10) for D's index/VIX levels, D's NIFTY 500 TRI and the L2 refresh; past that it runs **with the gaps recorded**. | `M17_DATA_WAIT`, `M17_DATA_GAPS` |
| + ~8 min | Commons build: sheets, shortlist, screens + regime, filing digests (screens ∪ shortlist), frozen base rates. | |
| through the night | Managers one at a time: `FM-SWING-BRK`, `FM-SWING-EVT`, `FM-POS-TREND`, `FM-POS-FUND`, each deciding on its `-10L` book, then its `-1CR` mirror follows (no model call). At most 4 model calls each (+1 repair). Rate limits back off (2, 4, 8, 16 min) inside the deadline. | `FM_*`, `MIRROR_DIVERGENCE` |
| then | The 8 control books rebalance (weekly for swing, every 21 sessions for positional) on the same shortlist; the 4 style books on their screens. Desk state recorded; scoreboard built and cross-checked against the journal; digest written. | |
| **08:30 D+1** | **Deadline.** A manager not finished journals `MISSED_SESSION` and stages nothing of its own; its stop exits still stage. | |
| 09:15 D+1 | The staged orders fill at D+1's open; the 22:00 run on D+1 books them. | |

Holidays are skipped inside the job (the NSE holiday calendar). A rerun of a completed session is a
no-op; a red session can be rerun once the data heals.

## Streams: dry run and live

- **Until M17.8's go the registered job runs the dry stream** (`M17_DRY_RUN = True` in
  `dataplatform/scheduler/registry.py`). Every dry entry is journaled under `case_id`
  `m17-dry:<book id>` with `payload.stream = m17-dry`; the desk state is the `paper_session` row
  `m17_dry2_fund_managers` (M17.14: the dry desk restarted on the Amendment 2 roster; the
  2026-10-09 dry session's state, previous roster, stays under `m17_dry_fund_managers` and counts
  toward wiring and timing only, Amendment 2 g); the digest goes to `~/campaign/m17/dry/`. Nothing of
  a dry run reaches the live streams, `/status/managers` or the live scoreboard. Nothing counts
  (pre-registration §6). M17.8's 5 dry sessions must include at least 3 on this roster.
- **Live** streams use the book ids themselves (`FM-SWING-BRK-10L`, …) and the `paper_session` row
  `m17_fund_managers`; the digest goes to `~/campaign/m17/`. The live stream refuses to run before S0.

A rehearsal from a shell that persists nothing (reads the lake and the status DB only; scratch kill
switch and fetch cache; StubLLM; no data wait):

```bash
DATA_ROOT=/home/ubuntu/stock-manager/data \
  uv run python -m backtest.fm_job --memory --stub-llm --dry-run --no-wait --session 2026-10-09
```

It prints the per-stage timings and each manager's status (and its mirror's staged and refused
orders and tracking gap) as JSON.

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

`M17_S0` is journaled in every book's stream — all 21 — each with its `mandate_hash`. A manager
book's (primary and mirror alike) covers its roster entry (manager, role, style, starting screens and
the horizon the style fixes), `prompts/manager.md`'s bytes, both decision schemas with the system
prompt, the contract's tolerances, the mirror rule, `SHORTLIST_RULE_HASH`, `SCREENS_RULE_HASH`, the
desk-order domain and the shared rails; a control's its entry, rebalance constants and the shortlist
rule; a style book's its entry, the style rule and the screens rule
(`analyst/fundmanager/job.py::mandate_fingerprints`). A changed hash after S0 is a **new manager**
with a new id (§7) — never edit a running one.

## Reading `/status/managers` and the digest

`GET /status/managers` rebuilds the scoreboard from the live journal on every call:

- `s0`, `sessions_elapsed`, `k_of_n` (always "*k* of 8 books passed", never a lone winner);
- `books[]`: each book's latest mark — NAV, return since its opening capital, cash, positions;
- `managers[]`: one row per **manager book** (8), each against its own `CTRL-<book>` (§6 per book,
  Amendment 2 e): phase, verdict (`NOT_STARTED`, `IN_PROGRESS`, `PASS`, `CLEAR_FAIL`,
  `INCONCLUSIVE` → one extension to S0+125), excess vs control, excess vs bench, max drawdown vs the
  bench's, and the manager's Brier on `p_beat_bench` (computed once, on its decisions, which only its
  primary journals) with how many decisions have resolved (Brier speaks from 30);
- `manager_results[]`: per manager, the primary and mirror verdicts and whether it passed on
  `BOTH`, `ONE` or `NEITHER` book, its decision count and Brier, and whether its primary passed across
  the extension window as well;
- `graduation`: the Amendment 2 (e) floor — met when at least 2 of 4 managers pass on their primary
  book, or one passes on its primary across the extension window as well. Mirror passes never count.
  Meeting it is what the evidence must at least show; graduating is the owner's call (decision #8);
- `style_books[]`: each style book's return and drawdown over its manager's current window, and the
  primary's excess over it — secondary, never in a verdict;
- `scoreboard_error`: the journal could not be scored (a missing mark, a malformed line) — no
  numbers are shown rather than partial ones;
- `decisions[]`: the latest session's decision lines (what, never why — no rationale or prompt).

The digest `~/campaign/m17/digest-<session>.md` holds the same numbers (a *Managers* table, a
*Books against their controls* table, the graduation floor line and the *Style books* table) plus
that session's decision table, mirror orders and refusals included. The job also checks its own ledger against a rebuild from the journal every session;
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
| `CONTROL_REBALANCE` [HEARTBEAT] | A control or style book's rebalance targets (and the list's digest and rule). |
| `MIRROR_FOLLOW` [BUY/SELL] | A `-1CR` mirror order toward its primary's post-decision weight, staged after the rails. |
| `MIRROR_DIVERGENCE` [HEARTBEAT] | Per session, on the `-1CR` only: the primary's target weight, the mirror's weight once its orders fill, and each name's status (`REFUSED` names the rails), plus `tracking_gap_pp`. |
| `STYLE_LIST_UNAVAILABLE` [ESCALATE] | A style book's list could not be built from the session's screens (e.g. the FUND dossiers could not be read); its due rebalance waits. |
| `SUSPENDED_HOLDING` [HEARTBEAT] | A held, still-listed name with no bar on a normal session: marked at its last close (`last_trade_date`, `sessions_suspended`). |
| `UNFILLED_SUSPENDED` [HOLD] | An order due to fill on a session its name had no bar; a sell is re-offered (`reoffered=true`), a buy is not. |
| `SUSPENDED_EXIT_HELD` [DEFERRED] | A sell of a suspended name not staged this session; offered again next session. |

## When something goes wrong

- **Job FAILED** (`/status/jobs`, `failure_alerts` pages it): the whole session rolled back. Fix the
  cause and rerun the same session: `uv run python -m dataplatform.scheduler run-once m17_fund_managers`
  before 08:30 IST next session, or let the next evening run (the missed session's orders are not
  caught up — they lapse).
- **A held name with no close** — see *Suspended holdings* below. Delisted names (identity master
  listing record) are valued at their last traded close.
- **`repo_rates.yaml` coverage** ends at the last MPC decision entered; a session past it raises
  `RepoRateCoverageError`. Extend it from RBI's own press release after each MPC meeting.
- **Holiday calendar** `nse_holidays.yaml` ends 2026-12-31: add the 2027 NSE circular before December.

## Suspended holdings (M17.13, owner decision 2026-10-10)

A held name that is still listed but has no bar for a session no longer fails the job.

- **Market-wide or single-name?** A session whose market data is broadly missing is red data: the
  interlock (`nse_bhavcopy`, `nse_delivery` PUBLISHED and green) stops it before any book is marked.
  Past a green gate, a held name is SUSPENDED only when the session's L1 EQ close count is at least
  90 % of the median over the previous 5 sessions (`backtest.fm_world.printed_normally`); below that
  nothing is suspended and the mark still fails loudly (a gate that let a thin day through).
- **What happens:** the holding is marked at its last traded close (as a delisted name is), journaled
  `SUSPENDED_HOLDING` every session, listed in the digest's *Suspended holdings* table ("held, not
  trading since <date>") and in `/status/managers` `suspended_holdings`, and shown to its manager as
  suspended since that date with no price.
- **No pretend trades:** a SELL/TRIM/STOP_EXIT on it stays unfilled (`UNFILLED_SUSPENDED`,
  `SUSPENDED_EXIT_HELD`) and is re-offered every session; when the name prints it clears the rails
  and is staged at that close for the next open. A buy of a name with no bar is never staged.
- **Scoring:** a decision resolving while the name is suspended is scored at its last close with
  `suspended=true`; each window reports `suspended_resolved_decisions` (digest: *Resolved while
  suspended*).
- **Owner action:** none for a short suspension. A name suspended for weeks, or one whose listing
  has in fact ended, wants the identity master's listing record updated (it then becomes delisted).

## Repo rate (idle-cash interest) — after every MPC decision

`backtest/repo_rates.yaml` carries the latest confirmed rate forward only to the eve of the next
scheduled MPC decision (owner decision 2026-10-10). A session on the decision day raises
`RepoRateCoverageError` until the file is updated, and that stops the M17 job and the M15.3 paper
session alike. On the decision day (next: **2026-12-04**), read RBI's press release on rbi.org.in, then:
add its row (verified, quoted with its prid), set `confirmed_through` to that day, set
`next_mpc_decision` to the meeting the release schedules, and set `through` to the day before it.
`uv run pytest tests/unit/test_cash_interest.py -q` checks the shape.
