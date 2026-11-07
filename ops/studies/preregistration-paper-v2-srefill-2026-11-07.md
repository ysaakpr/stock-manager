# Pre-registration: a forward paper book for V2 + S+refill beside D13, 2026-11-07

Written on Sat 7 Nov 2026 (IST), **before** any code for the book exists and before its first
session. Nothing below may change after the book's first session is decided. A changed parameter,
opening state or comparison rule is a **new book with a new id**, never an edit of this one.

Task M14.11. Decision: HUMAN_DECISIONS **D40**.

## 0. Why this book exists

M14.10 (`ops/gates/M14.10-nifty500-walk-forward-report.md`) walked D13 and four variants forward on
the paper book's own NIFTY 500 screen. Its pre-registered rule let the selection window choose; it
chose S+refill, which failed the ₹1 cr verification condition, so **D13 stays as ratified**.

The arm the walk-forward did *not* choose, **V2 + S+refill** (the momentum-turn early re-entry with
the rank-ordered, sector-refilled buys), met all three conditions after the fact. The report says
plainly that promoting it on those rows is choosing on the test window, and that the honest path, if
the owner wants it, is "pre-register it alone as the candidate and run it as a forward paper book
beside D13 … the decision to adopt would rest on forward results".

The owner chose that path on Sat 7 Nov 2026: *"go with your recommendations… merge the PR and deploy
the (a) and then do the (b)"*, where (a) is this book. This file is that pre-registration.

## 1. The book

| Item | Value |
| --- | --- |
| **Book id** | `momentum_v2_turn_srefill_paper_2026_11_09` (matches `^[a-z][a-z0-9_]{2,63}$`) |
| **Parameters** | `backtest.policies.momentum_v2.PAPER_CANDIDATE_V2_SREFILL_2026_11_07` |
| **Mode** | Paper only. A real-money version is a separate ratification (AGENTIC_CONTEXT §3.2, B9). |
| **First session** | **Mon 9 Nov 2026**. No session before it is ever decided for this book. |
| **Opening capital** | ₹10,00,000 (`PAPER_OPENING_CASH`, the same as D13) |
| **Job** | the existing `paper_session` scheduler job (21:45 IST, Mon–Fri), which decides D13 first and then this book |

### 1.1 Parameters, exactly

D13's `PAPER_RATIFIED_2026_09_06` plus **exactly** the three switches of the M14.10 arm
"V2 + S+refill (momentum turn with the sector refill)" (`backtest.sweep.NIFTY500_WF_ARMS`, its
`v2` = `D13_V2_SECTOR_REFILL`), and nothing else:

| Field | D13 | This book |
| --- | --- | --- |
| `top_n` | 20 | 20 |
| `use_12_1` | True | True |
| `sell_band` | 30 | 30 |
| `regime_filter` | True | True |
| `vol_scaled` | True | True |
| `redeploy_next_session` | True | True |
| every other M9.5/M14.5/M14.6/M16 switch | default (off) | default (off) |
| `early_entry` | `None` | **`EarlyEntry.MOMENTUM_TURN`** (V2) |
| `rank_ordered_buys` | False | **True** |
| `sector_refill` | False | **True** |

V2's constants are the M14.9 pre-registered ones, unchanged (`backtest.policies.early_entry`): a
parked book re-enters, through a full rebalance at that session's close, when the NIFTY 50 TRI is at
least 0.98 × its 200-session mean, its 50-session mean is above its value 10 sessions earlier, and
its 20-session return is positive. A unit test asserts the constant equals the M14.10 arm's
parameters field for field, and equals D13 with exactly those three switches.

### 1.2 Everything else is D13's

- **Universe / screen:** the paper world's default, `UniverseParameters()`: the PIT NIFTY 500
  membership intersected with the ₹1 cr median-daily-turnover floor over a trailing 365 days. The
  backtest reference is therefore M14.10's **NIFTY 500, ₹1 cr** cells.
- **Signal and regime inputs:** the same L1/L2 readers, the same published NIFTY 50 TRI series.
- **Cost model:** the one shared `execution.costs` model (invariant #4), the same rate card.
- **Rails:** `ratified_backtest_rail_policy()`, A8's ratified numbers and sector map. The sector
  refill reads A8's own sector map and caps; it never replaces a rail.
- **Exits:** D13's, monthly. The regime is read on the first session of each month the book decides
  (D24 #2); a risk-off rebalance parks. V2 only ever *re-enters* a parked book. There is no daily
  exit.
- **Data interlock, kill switch, reconciliation, corporate actions:** the paper session's, per book.
  The book has its own kill switch file (`<data_root>/kill_switch/<book id>.json`).
- **Paper timing departures D24 #2 and #3** apply as for D13. **D24 #1 does not** (§2).

## 2. Opening state: parked

D13 parked on its 2 Nov rebalance (paper row `rebalance_risk_off`; the TRI read 35,632.80 against
its MA200 of 36,330.77, −1.92%, M14.9 report "This time") and holds ₹10,00,000 in cash with no
positions. The owner asked for the candidate to start in that state, so V2 can fire on its first
qualifying session.

**How it is set.** The book's spec declares an opening state, `opens_parked`. On the book's first
decided session (9 Nov, or the first green session after it):

1. the book holds ₹10,00,000 cash, no position, no staged order and no pending redeploy;
2. the policy resumes with its regime-parked flag set (`MomentumV2Policy.resume(…, parked=True)`),
   the same flag a risk-off rebalance sets;
3. the month the book opens in counts as already rebalanced, so the first session is **not** a
   rebalance; the next monthly rebalance is the first session of December the book decides.

That is the state an uninterrupted V2 + S+refill replay is in on 9 Nov. Its 2 Nov rebalance reads the
same TRI against the same MA200 (`regime_filter` and `regime_ma_days` are D13's), so it parks. M14.10's
own replay of the arm reports no V2 fire through 6 Nov (MA50 still falling). The one difference: a
replay that had been invested before 2 Nov would keep up to 8 names after the park (A8's
minimum-holdings floor refuses the rest). The forward book, like D13's own paper book, is fully
in cash.

**What it is not.** Nothing is written to D13's journal, record or kill switch. No 2 Nov (or any
pre-9 Nov) session is recorded or journaled for this book; the 2 Nov reading is cited, not
re-made. The book's first session journals the opening as its own `HEARTBEAT` entry
(`payload.event = OPENED_PARKED`) that cites this file and D40, followed by that session's own
decision, so the opening is explicit in the book's journal and never implied.

**Departure from D24 #1, for this book only.** D24 #1 says a paper book that starts mid-month opens
invested: its first session rebalances. That would read the regime *level* on 9 Nov and, at or above
MA200, buy the full basket without the V2 trigger. D13's monthly rule would not make that re-entry
before 1 Dec. It is therefore not the state this book is meant to start from. A spec without
`opens_parked` (D13) keeps D24 #1 unchanged.

From the first session on, while parked, every non-rebalance session reads the TRI's trend. A V2
fire is a full rebalance at that session's close, with the sector refill, filling on the next
session. If V2 has not fired by the first December session, that rebalance reads the regime exactly
as D13's does.

## 3. Isolation from D13

- D13 is decided first, in its own transaction, and committed before this book starts.
- This book runs in its own transaction. Whatever it does or fails with cannot stop, change or block
  D13's session. A failure of this book is raised after D13 has committed, so the job run is
  recorded FAILED (fail loud) with D13's session intact. A retry is a no-op for D13.
- This book's only extra red cause: while parked, a non-rebalance session needs the session's
  published TRI level (V2 reads it). Missing, the book records `SKIPPED_DATA_RED` for that session.
  D13 does not read the TRI off a rebalance, so it is unaffected.
- D13's parameters, decisions, journal bytes and replay are unchanged. Tests assert byte-identical
  D13 output with and without this book in the job.

## 4. What it is compared on

- **Metric:** XIRR ÷ max drawdown, against D13, **over the same sessions**, from 9 Nov 2026.
  - Both books start that day with ₹10,00,000 in cash.
  - NAV is each book's cash plus its holdings at the session's NSE close (the Portfolio page's
    definition).
  - Max drawdown is measured on the daily session NAV.
- **Also reported:** XIRR, max drawdown, trades, charges, early re-entries (each with the date it
  fired and whether the next rebalance re-parked it) and MAX_SECTOR refusals, for both books.
- **No pass/fail claim before Sun 9 May 2027** (6 months). Any read before then is descriptive
  only.
- **At or after 6 months,** the comparison is reported with its sample size. Adopting this book in
  place of D13 is **the owner's decision**, a new ratification (AGENTIC_CONTEXT §3.2). This book
  never changes D13.
- **Sample-size caveat, stated in advance.** Over the backtested decade the arm made 6 early
  re-entries (M14.10, ₹1 cr). Six months of forward sessions will hold one or two at most. One fire
  and whether it reversed will dominate the comparison, so a 6-month difference in either direction
  is weak evidence.

## 5. Trial-count and forking-paths caveat (from M14.10, quoted)

M14.10's pre-registration §4: *"Five arms on one selection window, on a lake that has seen many
configurations (68 before M16, then M16, M14.5, M14.6 and M14.9). Four of the five arms were already
seen on NIFTY 500 attribution rows in M14.9 … So the verification window here is **not** unseen for
those arms. V2's M14.9 NIFTY 500 verification rows were read before this study was designed."*

M14.10's report: *"The rule lets only the selection window choose. Promoting the runner-up because
its verification rows turned out better is choosing on the test window, the forking path the
walk-forward exists to block. With five arms, one of them clearing every condition after the fact is
not much evidence on its own."*

Running this book does not change that. Its backtest rows are post-hoc. The forward sessions from
9 Nov 2026 are the only evidence about it that no backtest on this lake could have been fitted to.

## 6. What stops or changes it

- The book's own kill switch (`python -m execution.kill_switch trip --account
  momentum_v2_turn_srefill_paper_2026_11_09`) halts it alone. Each halted session is journaled as a
  no-op.
- Any change to the parameters, the opening state or the comparison is a new book with a new id and
  a new pre-registration. This book's rows and journal are never edited (invariant #12).
- Not changed by this book: D13, its paper book and schedule, the A8 rails, the cost model, M17 and
  its mandates.

## 7. Open question carried over

M14.10 recommendation 3: *settle how a park meets A8's 8-name floor before any live early-entry
book.* It does not bind a paper book. Both paper books meet it the same way: a future risk-off
rebalance on an invested book keeps the names A8 refuses to sell. It stays the owner's question.
