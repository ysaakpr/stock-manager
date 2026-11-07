# Pre-registration: M14.10, a NIFTY 500 walk-forward of D13's early-entry and sector-cap arms, 2026-11-06

Written **before** any M14.10 run, on 6 Nov 2026 (about 17:00 IST). Nothing below may change
after the first M14.10 evaluation run. A changed arm, threshold, window or rule is a new trial,
reported as such and never folded into these rows.

## 0. Why this study exists

M14.9 (`ops/gates/M14.9-momentum-turn-entry-report.md`, PR #168) tested four early re-entry
triggers (V1–V4) and a rank-ordered sector cap (S, S+refill) on D13. Its walk-forward could only
run on the floor-only (`turnover_floor`) screen. The NIFTY 500 PIT membership was believed to open
on 2016-10-24, after the selection window opens, so NIFTY 500 rows were attribution only. On that
screen, which is the paper book's own universe, V2 (the momentum turn) beat D13 in 3 of 4 cells.

That belief was stale. M14.1's membership build, published to the lake on 10 Oct 2026, covers
NIFTY 500 from **2015-03-28** and returns 500 members on 2016-09-01 (PR #170,
`ops/gates/data-lake-status-2026-11-06.md`). The fair test M14.9 recommended is therefore possible
now: a walk-forward on NIFTY 500 itself. The owner approved it on 6 Nov 16:40 IST.

## 1. Code and data state

- Branch `m14-10-nifty500-walk-forward`, stacked on PR #168 (`m14-9-momentum-turn-entry`). The
  campaign runs on one commit, recorded in every run directory's manifest. That commit carries
  this file, the arm set below and the one code change §3 needs. All five arms, D13 included, are
  replayed on it; nothing is copied from M14.9.
- Engine switches as M12.R, M14.5 and M14.9: book corporate actions on, interest on idle cash on
  (repo − 50 bp), every order through A8's ratified rails, ₹10 lakh opening book, one deposit.
- Lake: last L1 session 2026-11-05 at the time of writing. NIFTY 500 PIT membership from
  2015-03-28 (M14.1 build of 2026-10-10).

## 2. Universe, windows, floors (the M12 mandate)

- **Universe: `nifty500`** (`UniverseParameters.for_universe("nifty500", …)`), the PIT NIFTY 500
  membership intersected with the liquidity floor, on every window. This is the paper book's own
  screen.
- **Windows, never averaged:**
  - decade 2016-09-01..2026-08-31;
  - six-year 2019-07-01..2026-08-31;
  - walk-forward: choose on 2016-09-01..2021-08-31, verify on 2021-09-01..2026-08-31.
- **Floors:** ₹1 cr and ₹10 cr median daily turnover.
- **Ranking:** XIRR ÷ max drawdown. Report XIRR, max drawdown, excess over the NIFTY 50 TRI,
  trades, turnover, charges and A8 refusals too. Bar: backtested XIRR > 25%, reported per cell.
- Driver `backtest.m12_rerun --universe nifty500 --units walk-forward,decade,six-year`. At most
  2 workers. Runs only 09:00–20:25 IST, on Sat 7 and Sun 8 Nov 2026 (continuing in the same
  windows on later days if unfinished).

## 3. Arms (each exactly as specified in M14.9; nothing tuned)

| Arm | Definition |
| --- | --- |
| **D13** | `PAPER_RATIFIED_2026_09_06`, unchanged. The baseline. |
| **V1 confirmed cross** | M14.9 pre-registration §3 V1: a parked book re-enters after the NIFTY 50 TRI has closed at or above its MA200 on 5 consecutive sessions. |
| **V2 momentum turn** | M14.9 §3 V2: a parked book re-enters when the TRI is at least 0.98 × MA200, MA50 is above its value 10 sessions earlier, and the 20-session return is positive. |
| **S+refill** | M14.9 §8 as fixed in §9: buys go to the rail gate in rank order, and a name A8's sector rail would refuse is replaced by the next-ranked name outside its sector. |
| **V2 + S+refill** | The one pre-specified combination: the V2 trigger **and** rank-ordered buys with the sector refill. Every rebalance, the V2 early re-entry included, uses the refill. Nothing else is changed. |

Code change for the combination only. M14.9 refused to combine `early_entry` with the sector-order
switches (combinations were left for a follow-up). M14.10 permits the momentum-turn and
confirmed-cross triggers with `rank_ordered_buys` / `sector_refill`. The scale-in stays refused,
because its half-exposure cap and the refill's full-weight projection disagree. Each single arm's
parameters, and so its run fingerprint, are identical to M14.9's.

V3 (scale-in), V4 (breadth turn) and S without refill are **not** run. The owner named the
arms. No other variant is added.

## 4. Decision rule (the M14.9 rule, applied to NIFTY 500)

1. **The walk-forward chooses.** `backtest.verdict.run_walk_forward` ranks the five arms on the
   NIFTY 500 selection window by XIRR ÷ max DD, the ₹1 cr floor first. The choice is frozen
   before the verification rows are read.
2. **If it chooses D13**, no arm is recommended; D13 stays as ratified.
3. **If it chooses an arm**, that arm is recommended *as a candidate for a separate, parallel
   paper book* only if all of these hold on the NIFTY 500 cells:
   - its verification XIRR ÷ max DD is at least D13's at **both** floors;
   - its XIRR ÷ max DD is at least D13's in at least **6 of the 8** NIFTY 500 cells;
   - its max drawdown is no more than **2 pp** deeper than D13's in every NIFTY 500 cell.

   Otherwise it is reported as not recommended, with the failing condition named.
4. **Adoption is the owner's decision.** A recommended arm would run as a separate paper book
   beside D13; D13 and its paper book are never changed by this study.

**Trial count.** Five arms on one selection window, on a lake that has seen many configurations
(68 before M16, then M16, M14.5, M14.6 and M14.9). Four of the five arms were already seen on
NIFTY 500 attribution rows in M14.9: six-year and wf-verification for V1, V2 and S+refill. So
the verification window here is **not** unseen for those arms. V2's M14.9 NIFTY 500
verification rows were read before this study was designed. The report must say so plainly.
The only genuinely out-of-sample piece is the selection window's choice, made on 2016-09..2021-08,
which no M14.9 NIFTY 500 run covered.

## 5. What the report must show

- Per window and floor: the M14.9 table columns. That means the parks, monthly and early
  re-entries with ledger confirmation, and the re-entries reversed within a month for V1/V2.
  For the S arms it also means MAX_SECTOR refusals per risk-on rebalance and idle cash after
  rebalances (M14.9 §8 definitions).
- The generated scorecard against D13, the walk-forward choice, and the decision rule above
  evaluated in code.
- A hand-written analysis, including the trial-count caveat in §4.
- Report: `ops/gates/M14.10-nifty500-walk-forward-report.md`, in a new PR, not merged.
