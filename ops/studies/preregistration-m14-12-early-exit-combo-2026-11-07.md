# Pre-registration: M14.12, early entry + early exit + sector refill on NIFTY 500, 2026-11-07

Written **before** any M14.12 code or run, on Sat 7 Nov 2026 (about 11:10 IST). Nothing below may
change after the first M14.12 evaluation run. A changed arm, threshold, window or rule is a new
trial, reported as such and never folded into these rows.

## 0. Why this study exists

The owner asked on 7 Nov 2026 for a backtest of early entry, early exit and the sector refill
together. This is study (b). Study (a), the forward paper book that runs V2 + S+refill beside D13
(M14.11, PR #174, owner decision D40), was deployed at 11:01 IST today. This study does not touch
it, D13's paper book or M17.

What is already known, and was read before this study was designed:

- **M14.5** (`ops/gates/M14.5-regime-reentry-report.md`) read the regime daily on D13. Daily
  re-entry *and* exit (`regime_daily_reentry` + `regime_daily_exit`, unbanded) lost XIRR ÷ max DD
  in 11 of 12 cells. It deepened the drawdown (₹10 cr floor-only decade 42.0% against 30.1%),
  parked about 4× as often (39 parks against 9 over the decade) and paid 16% to 61% more charges
  across cells (+42% at the ₹1 cr floor-only decade). The 2%-banded arm M14.5 ran was a
  banded daily **re-entry** with monthly exits; it won 9 of 12 cells and lost the three ₹1 cr
  floor-only cells. **No banded daily exit has been run on this lake.**
- **M14.9** (PR #168) added the early re-entry triggers (V2 = the momentum turn) and the
  rank-ordered sector refill. Exits stayed monthly.
- **M14.10** (PR #173, `ops/gates/M14.10-nifty500-walk-forward-report.md`) ran the NIFTY 500
  walk-forward. V2 + S+refill met all three conditions against D13 but was not the walk-forward's
  choice (S+refill was, and failed the ₹1 cr verification condition). Its rows on every window
  and floor have been read.

## 1. Code and data state

- Branch `m14-12-early-exit-combo`, from `main` at `ab69648` (PR #174 merged). The campaign runs
  on one commit, recorded in every run directory's manifest. That commit carries this file, the
  arm set below and the one composition change §3 needs. All four arms, D13 and C0 included, are
  replayed on it; nothing is copied from M14.10.
- Engine switches as M14.10: book corporate actions on, interest on idle cash on (repo − 50 bp),
  every order through A8's ratified rails, ₹10 lakh opening book, one deposit.
- Lake: last L1 session 2026-11-06 at the time of writing. NIFTY 500 PIT membership from
  2015-03-28.

## 2. Universe, windows, floors (M14.10's, unchanged)

- **Universe: `nifty500`**, the PIT NIFTY 500 membership intersected with the liquidity floor, on
  every window.
- **Windows, never averaged:** decade 2016-09-01..2026-08-31; six-year 2019-07-01..2026-08-31;
  walk-forward: choose on 2016-09-01..2021-08-31, verify on 2021-09-01..2026-08-31.
  4 windows × 2 floors = **8 cells**.
- **Floors:** ₹1 cr and ₹10 cr median daily turnover.
- **Ranking:** XIRR ÷ max drawdown. Bar: backtested XIRR > 25%, reported per cell.
- Driver `backtest.m12_rerun --arm-set early-exit-combo --universe nifty500 --units
  walk-forward,decade,six-year`, 2 workers, from a worktree pinned to the campaign commit. Runs
  only 09:00–20:25 IST (killed at 20:25, resumed the next morning by run) on Sat 7 and Sun 8 Nov
  2026. It does not run on Monday 9 Nov without the owner being told first.

## 3. Arms

| Arm | Definition |
| --- | --- |
| **D13** | `PAPER_RATIFIED_2026_09_06`, unchanged. |
| **C0** | V2 + S+refill: exactly `PAPER_CANDIDATE_V2_SREFILL_2026_11_07` (equal, field for field and by run fingerprint, to M14.10's `D13_V2_SECTOR_REFILL` arm). |
| **C1** | C0 + a daily exit with a 2% band: `regime_daily_exit=True`, `regime_daily_band=0.02`. |
| **C2** | C0 + an unbanded daily exit: `regime_daily_exit=True`, `regime_daily_band=0`. |

`regime_daily_reentry` stays **off** in C1 and C2: re-entry is C0's (the monthly rebalance and
the V2 trigger), and nothing else.

### 3.1 The daily exit (C1, C2)

M14.5's `regime_daily_exit`, unchanged. On a **non-rebalance** session, a book that is **not
parked** and holds **settled shares** is parked when the NIFTY 50 TRI closes below
MA200 × (1 − b): b = 0.02 for C1 ("the first session the TRI closes below MA200 × 0.98"),
b = 0 for C2. The park is D13's own park: every settled holding is offered for sale (A8's 8-name
floor refuses what would take the book below 8 names), any pending next-session redeploy is
dropped, and the book is marked parked. The reading goes through the PIT guard. Because M14.5's
band is symmetric but only the exit is on, b affects the exit only.

The monthly rebalance is unchanged in every arm: on the first session of the month the book parks
if TRI < MA200 (unbanded, D13's rule) and otherwise rebalances and is unparked.

### 3.2 The spell rule (C0, C1, C2)

- The book is either **invested** (not parked) or **parked**. Parked is the policy's remembered
  flag (`MomentumV2Policy.parked`), not an empty book: A8's floor usually leaves 8 names held.
- A **park** is any transition from invested to parked: a risk-off monthly rebalance (all arms),
  or a daily exit (§3.1, C1 and C2 only). A risk-off monthly rebalance of a book that is already
  parked is not a new park; it continues the spell (as D13's park count already treats it).
- A **spell** runs from a park to the next re-entry. A **re-entry** is either a risk-on monthly
  rebalance of a parked book or a V2 early re-entry.
- **While parked**, on every non-rebalance session V2 is read through the PIT guard (TRI ≥ 0.98 ×
  MA200, MA50 above its value 10 sessions earlier, 20-session return > 0). When it fires, the
  book re-enters through a full rebalance on that session, with rank-ordered buys and the sector
  refill, without re-reading the regime (M14.9's rule). The re-entry ends the spell. So **V2
  re-enters at most once per spell, and each park opens a new spell** in which V2 may fire again.
- A parked book is **never re-parked daily** (M14.5's rule). The daily exit is never read on a
  rebalance session or while parked.
- **Order on a non-rebalance session:** the daily exit (if invested) → V2 (if parked) → the
  next-session redeploy → heartbeat. A session is at most one park or one re-entry, never both.

**Predicted interaction, stated before any run.** V2 can fire below MA200 (from 0.98 × MA200);
C2's exit fires at anything below MA200. While the TRI sits in [0.98, 1.00) × MA200 with MA50
rising and a positive 20-session return, C2 can therefore re-enter on one session and park on the
next session its new shares have settled, and repeat. C1's thresholds meet at 0.98 × MA200, so it
has no such gap but can still flip at the boundary. This is the arm as specified; **no cool-down,
confirmation or extra band is added** (any would be an untested new parameter). The whipsaw
counts in §5 measure it.

### 3.3 The composition change (the only code change)

Today `MomentumV2Parameters` refuses `regime_daily_exit` with `early_entry`, and with
`rank_ordered_buys`. M14.12 permits `regime_daily_exit` (with or without `regime_daily_band`)
together with a trend trigger (`early_entry` = momentum turn or confirmed cross), with or
without the sector order. Everything else stays refused: `regime_daily_reentry` with any
`early_entry` (two re-entry rules), the daily exit with the scale-in or breadth trigger, with the
sector order but no trend trigger, and with the vol target, A1 or the M14.6 switches.
`policy.decide` already evaluates the daily exit before the early trigger, which is the order in
§3.2, so the change is in validation only. The forward paper runner keeps refusing any daily
regime check.

The change is off by default. Tests must show that **D13's and C0's replays are byte-identical**
with and without the change (D13's digest struck before M14.5; C0's repr unchanged and its
replay digest on the scripted month unchanged), and that C1 and C2 park on the session their
rule says and re-enter through V2 once per spell, each with an inverted twin.

## 4. Decision rule (M14.10 §4's form)

1. **The walk-forward chooses.** `backtest.verdict.run_walk_forward` ranks the four arms on the
   NIFTY 500 selection window by XIRR ÷ max DD, the ₹1 cr floor first. The choice is frozen
   before the verification rows are read.
2. **If it chooses D13 or C0, no change is recommended.**
3. **If it chooses an exit arm (C1 or C2)**, that arm is recommended as a candidate only if all of
   the following hold on the NIFTY 500 cells **against D13 and also against C0** (six checks):
   - its verification XIRR ÷ max DD is at least the comparator's at **both** floors;
   - its XIRR ÷ max DD is at least the comparator's in at least **6 of the 8** cells;
   - its max drawdown is no more than **2 pp** deeper than the comparator's in **every** cell.

   Otherwise it is reported as not recommended, with each failing condition and comparator named.
4. **Adoption is the owner's decision.** A recommended exit arm would be a candidate for a
   separate paper book or a change to the M14.11 candidate book; this study changes neither D13,
   nor the candidate book, nor M17.

**Trial count and forking paths, stated plainly.** This lake has now seen more than 70
configurations (68 before M16, then M16, M14.5, M14.6, M14.9, M14.10 and this). M14.5 already
showed an unbanded daily exit (with daily re-entry) losing in 11 of 12 cells, so the prior for C2
is poor. C0's rows on every M14.10 window and floor have been read, and C0 was itself chosen for
the paper book after its verification rows were seen, so **no window here is unseen for C0 or
D13**. C1 and C2 are new configurations, but their parent (C0) and the daily-exit mechanism are
not: the only genuinely out-of-sample element is the selection window's choice among the four
arms. Any recommendation from this study is a candidate for a forward paper test, never evidence
for adoption on its own.

## 5. What the report must show

`ops/gates/M14.12-early-exit-combo-report.md`, in a new PR, not merged:

- **Per window and floor:** M14.10's table columns exactly (XIRR, max DD, XIRR/DD, Δ vs D13,
  excess over the NIFTY 50 TRI, parks / monthly re-entries, early re-entries with ledger
  confirmation, reversed ≤ 1 month, trades, one-way turnover, charges, A8 min-holdings refusals,
  >25%).
- **Parks and whipsaw, every arm and cell:** parks split monthly / daily; re-entries split
  monthly / V2 early; how many parks and re-entries the fill ledger confirms (a sell, or a buy, on
  the following session); **re-entries reversed** (a park within the 21 sessions after a
  re-entry); **parks reversed** (a re-entry within the 21 sessions after a park); trades,
  one-way turnover and charges, with charges against C0. Parks and re-entries are replayed with
  the policy's own predicates over the published TRI, as in M14.9 and M14.10, and checked
  against the ledger.
- The generated scorecard against D13 and against C0, the walk-forward choice, the sector-cap
  and idle-cash table, the decision rule above evaluated in code (both comparators), and the
  >25% bar per cell.
- A check that D13's and C0's replay digests equal M14.10's on the same spec digests (or a
  statement of which differ and why, e.g. a lake restatement).
- A hand-written analysis, including the §4 caveat.
