# Pre-registration: M14.9 momentum-turn entry for a parked D13 book, 2026-11-05

Written **before** any M14.9 variant has been implemented or run. No M14.9 return, XIRR, drawdown
or switch count exists anywhere yet. Nothing below may change after the first M14.9 evaluation run.
A changed trigger (a different threshold, look-back, lag, fraction or universe rule) is a **new
trial**: it gets a new id here and is reported as such, never folded into these rows.

## 0. Why this study exists

D13 (`PAPER_RATIFIED_2026_09_06`) reads the regime (NIFTY 50 TRI against its 200-session moving
average) only on the first session of each month. On the 2 Nov 2026 rebalance the TRI stood 1.92%
below its average, so the book parked. Two sessions later, on 4 Nov, it closed 0.32% above it. Under
D13 the book waits for the 1 Dec rebalance.

The owner's question (5 Nov 2026) is whether ignoring the **rate of change** of momentum costs the
book its best entries. The alternative is to enter at the earliest point the trend turns up.

M14.5 (`ops/gates/M14.5-regime-reentry-report.md`) tested only level crossings: daily re-entry,
daily re-entry and exit, and a 2% band. Daily re-entry lost to D13 on XIRR ÷ max drawdown in all 12
cells, because most crossings reverse; the scoping investigation counted 77 crossings of the
200-session average since 2016, 61% of them reversing within 5 sessions. This study tests four
*different* early-entry triggers. Three of them try to filter out the crossings that reverse, and
one scales in.

## 1. Code and data state

- `main` at `e68755f` or later. The campaign runs on one commit that carries this task's policy
  switches. That commit is recorded in every run directory's manifest.
- Engine switches are the same as M12.R, M14.5 and M14.6: book corporate actions on, interest on
  idle cash on (repo − 50 bp, `backtest/repo_rates.yaml`), every order through A8's ratified rails,
  ₹10 lakh opening book, one deposit at the start.
- Lake: last L1 session 2026-11-04 at the time of writing. The PIT NIFTY 500 membership history
  opens 2016-10-24.

## 2. What stays D13

Every variant is D13 (`PAPER_RATIFIED_2026_09_06`: top 20, 12-1 ranking, sell band 30, monthly
regime filter on the published NIFTY 50 TRI against its 200-session mean, inverse-vol weights,
next-session redeploy, no vol target) with **one** switch on, and nothing else changed:

- **Exits stay D13's.** The regime is read for an exit only on the monthly rebalance session. A
  risk-off rebalance parks the book exactly as D13 does. No variant has a daily exit; daily exit
  lost in M14.5.
- **The variants act only while the book is parked.** "Parked" is the policy's own flag: set by
  every park and cleared by a risk-on rebalance or a variant re-entry. It is not an empty book (see
  §7). Once the book is invested, D13's rules apply unchanged.
- **A rebalance session is always D13's.** On the first session of a month the D13 rule decides,
  invested or parked: risk-on → full rebalance, risk-off → park. The variants' triggers are read only
  on the other sessions.
- **A re-entry is a D13 rebalance** on the trigger session: the same 12-1 ranking, band, inverse-vol
  weights, A8 sizing, and next-session redeploy of sale proceeds. As for every decision in the
  engine, the orders are placed at the trigger session's close and fill on the next session.
- **At most one re-entry per parked spell.** After it the book is invested until the next risk-off
  rebalance parks it again.

## 3. The variants (parameters fixed now)

All index figures are the published NIFTY 50 TRI (the series D13's regime reads), on its own
published sessions, using only points knowable on or before the decision session. "MA*N*" is the
simple mean of the last *N* published closes, the decision session's close included.

**V1 — confirmed cross.** While parked, re-enter on the session after the TRI has closed at or
above its MA200 on **5** consecutive sessions. Concretely, the decision is taken at the close of
the fifth such session and fills on the next one. A streak is broken by any close below MA200.

**V2 — momentum turn (the owner's idea).** While parked, re-enter at the close of the first session
on which all three hold:
1. TRI ≥ **0.98** × MA200 (it may still be below the average);
2. MA50 is rising: MA50 today > MA50 **10** sessions earlier;
3. the TRI's **20**-session return is positive: today's close > the close 20 sessions earlier.

**V3 — scale-in.** While parked, at the close of the first session with TRI ≥ MA200, deploy
**half** the basket. That is a D13 rebalance at **50%** exposure: the same basket and weights, each
name bought to half its weight. The buys are capped so the basket's value is at most 50% of the
book's capital (cash, settled or not, plus holdings at that session's prices), and the next-session
redeploy honours the same cap. Names outside the sell band are sold as in any rebalance. A kept
name already above its half-weight is not trimmed. The rest is deployed at the next monthly
rebalance if it reads risk-on, which is the ordinary D13 rebalance at full exposure. If that
rebalance reads risk-off, D13 parks the book again.

**V4 — breadth turn.** While parked, re-enter at the close of the first session on which:
1. the share of the PIT universe closing above its own 200-day moving average is ≥ **50%**, and
2. that share is higher than it was **20** sessions earlier.

The breadth series is defined now:
- **Universe:** the run's own investable universe (the same screen and liquidity floor the run
  trades) as of the first session of the decision session's month, i.e. the latest rebalance on or
  before it. It is screened from the names L1 priced that session.
- **A name's MA200:** the mean of its last **200** printed closes, the session's close included,
  on the same L2-adjusted-over-raw close source the momentum signal reads. A name counts only if it
  printed on the session and has 200 such closes.
- **Above:** close strictly greater than its MA200.
- **Share** = names above ÷ names counted. The lagged share is the same computation 20 NSE sessions
  earlier, with that session's own universe.

V4 is dropped only if this series cannot be built point-in-time from the lake within the time
budget. If it is dropped, the report says why.

**Baseline: D13 itself**, with every switch off. M14.5's saved D13 runs (`m14-5-regime-floor-cb1fcf9`,
`m14-5-regime-nifty500-cb1fcf9`) are reused only if a fresh D13 replay on the campaign commit
reproduces the saved replay digest (journal, book and rails) in at least the floor-only
wf-verification cells at both floors. Otherwise every D13 cell is re-run on the campaign commit.

**No grid, no tuning.** The constants above (5 sessions; 0.98, 50, 10, 20; 50%; 50%, 20, 200) are
the owner's or the standard ones, fixed now and stated as constants in code. Nothing is searched.

## 4. Mandate: windows, floors, universe

- **Rank on XIRR ÷ max drawdown.** Report XIRR, max drawdown and excess over the NIFTY 50 TRI too.
- **Both liquidity floors:** ₹1 cr and ₹10 cr median daily turnover.
- **Windows, never averaged:** the decade 2016-09-01..2026-08-31; the six-year window
  2019-07-01..2026-08-31; the walk-forward, choosing on 2016-09-01..2021-08-31 and verifying on
  2021-09-01..2026-08-31.
- **Bar:** backtested XIRR > 25%, reported per cell.
- **Universe:** the `turnover_floor` screen on every window, as in M14.5. NIFTY 500 rows are added
  for the six-year and wf-verification windows, where its PIT membership covers (it opens
  2016-10-24). They are attribution only and are never used for the choice.
- Driver: `backtest.m12_rerun` with a new arm set; report rendered by the M14.5 report machinery.
  At most 2 workers; runs stop by 20:30 IST each day and resume the next morning (resumable by run).

## 5. Decision rule (fixed now)

1. **The walk-forward chooses.** `backtest.verdict.run_walk_forward` ranks D13, V1, V2, V3 and V4
   on the floor-only selection window by XIRR ÷ max drawdown, the ₹1 cr floor first, exactly as in
   M14.5. The choice is frozen before the verification rows are read.
2. **If it chooses D13**, no variant is recommended. D13 stays as ratified.
3. **If it chooses a variant**, that variant is recommended *as a candidate for a separate,
   parallel paper book* only if all of these hold:
   - its floor-only verification XIRR ÷ max DD is ≥ D13's at **both** floors;
   - its XIRR ÷ max DD is ≥ D13's in at least **6 of the 8** floor-only cells;
   - its max drawdown is no more than **2 pp** deeper than D13's in every floor-only cell.

   Otherwise it is reported as not recommended, with the failing condition named.
4. NIFTY 500 rows, the >25% bar and the switch counts are reported and discussed. They do not
   override 1–3.
5. **Adoption is the owner's decision.** A recommended variant would run as a separate paper book
   beside D13. D13 and its paper book are never changed by this study.

**Trial count.** These are 4 new configurations (V1–V4) on a lake that has already seen many (M16
pre-registration, Appendix A, counted 68 before M16). The report states that the winner of five
arms on one selection window is partly luck. No deflation is applied to the choice; the
verification window is the check.

## 6. What the report must show

- Per window, floor and universe: XIRR, max DD, XIRR ÷ max DD, Δ vs D13, excess, trades, one-way
  turnover, charges, A8 min-holdings refusals, and the >25% flag.
- **Parks and re-entries**: re-derived from the published TRI (and, for V4, the breadth series)
  with the policy's own trigger functions, and checked against the fill ledger.
- **Re-entries that reversed within a month**, counted two ways: (a) the book was re-parked at the
  first monthly rebalance after the re-entry; (b) the TRI closed below its MA200 on any of the 21
  sessions after the re-entry decision.
- The generated scorecard against D13, and the walk-forward choice.
- **"This time" readout:** on the live lake, for each variant, the session in Oct–Nov 2026 on which
  it would have re-entered after D13's parks (1 Oct and 2 Nov), or "not yet" through the lake's
  last session. Also the D13 book's position, i.e. what the paper book holds today.
- The A8 caveat (§7) and how it bears on these results.

## 7. Known limitation carried in: A8's 8-name floor

A8's minimum-holdings rail refuses any sell that would take a book holding 8 or more names below 8.
A D13 park therefore leaves at least 8 names invested through the risk-off spell (M14.5). Every
variant inherits this. An early re-entry therefore moves the book from "≥ 8 names plus cash" to
"20 names", not from cash to fully invested. The study measures the variants on the book as the
rails actually allow. It does not change the rails. How a park should meet the floor is an owner
decision about the ratified rails.

## 8. Amendment, 2026-11-05 (≈13:00 IST): arm S, rank-ordered sector cap

Added at the owner's request (5 Nov 12:30 IST) **before any S run**, while the V1–V4 campaign
was already running. §§0–7 are unchanged; V1–V4 and their decision rule stand as written above.
The S arms are a separate family with their own walk-forward. They are never combined with
V1–V4 here; any combination (for example the V-winner plus S) would be a follow-up with its own
pre-registration.

**Why.** A read-only simulation of 4 Nov's live paper path found that D13's A8 sector rail
(MAX_SECTOR, 35%) refused CPPLUS (momentum rank 2) and INOXINDIA (rank 18), leaving about 12.7%
of the book in cash until the next rebalance. A8 clears a session's buys one by one, in the order
the policy submits them. So *which* names of a crowded sector are refused depends on that order,
not on rank.

**S — rank-ordered buys.** D13 with one change, policy-side only: every session's buys
(rebalance and next-session redeploy) are submitted to the rail gate in momentum-rank order, best
first, using the ranking of the last rebalance. When the sector cap binds, the lowest-ranked
names of that sector are the ones refused. A8's rails, limits and sector map are untouched.

**S+refill.** S, and at each rebalance a basket name whose buy would take its sector past the cap
is replaced by the next-ranked name outside that sector, up to `top_n` (20), so the cash is not
left idle. The check is a policy-side projection of what A8 will see:
- held names the rebalance keeps (inside the sell band) count at their current price;
- each basket name is topped up to its inverse-vol weight of the book's capital (cash, settled or
  not, plus holdings), walking the basket in rank order;
- the first name whose top-up would take its sector's value above 35% of capital is dropped, the
  basket is re-formed from the next-ranked names, and the walk repeats until nothing is dropped;
- a name already held at or above its weight is never dropped;
- the sector of a name is the rails' own map (`ratified_sector_map`), so unclassified names share
  the rails' pooled UNKNOWN bucket.

The rails still clear every order; a buy they refuse anyway stays refused.

**Runs.** Same mandate and cells as V1–V4 (§4): floor-only walk-forward, decade and six-year at
both floors; NIFTY 500 six-year and wf-verification at both floors. They are queued after the
V1–V4 campaign, at most 2 workers, inside the same 09:00–20:30 IST window. They run on a new
commit carrying the S code, in their own directories, with D13 beside them. D13's runs are copied
in from the V1–V4 directories (commit `d9ec7cf`) only if a fresh D13 replay on the S commit
reproduces the V1–V4 directory's D13 replay digest in the floor-only wf-verification ₹1 cr cell.
Otherwise D13 is re-run there.

**Decision rule for S (fixed now).** As §5, applied to the family {D13, S, S+refill}: the
walk-forward on the floor-only selection window (XIRR ÷ max DD, ₹1 cr first) chooses; if it
chooses an S arm, that arm is recommended as a candidate for a separate paper book only if its
floor-only verification XIRR ÷ max DD ≥ D13's at both floors, it is ≥ D13 in at least 6 of the 8
floor-only cells, and its max DD is no more than 2 pp deeper than D13's in every floor-only cell.

**Extra report items for S.**
- How often the cap bound: A8 MAX_SECTOR refusals over the run (from the run summary), in total
  and per risk-on rebalance.
- Idle cash after rebalances: the book's cash share at the close of the second session after each
  risk-on rebalance (after the rebalance's fills and the next-session redeploy). Cash is rebuilt
  from the fill ledger (trades, dividends, interest, the opening deposit) and divided by that
  session's NAV. Reported as the mean and the maximum over the window.
- The scorecard against D13, as for V1–V4.

## 9. Note, 2026-11-06 (≈12:15 IST): a bug fix in S+refill, not a rule change

The first S campaign (commit `cbed66b`, 5 Nov) crashed twice on an S+refill run with
`DuplicateStagedOrderError` (INE805D01034, session 2018-03-05). Cause: a held name ranked outside
the sell band is sold by the rebalance; when the sector cap pushed the refill below the band, the
refill picked that same name back into the basket, so the policy staged a sell and a buy of one
ISIN in one session, which the engine refuses. §8 says the refill takes "the next-ranked name"
and that names outside the sell band are sold "as in any rebalance". A name being sold in that
session is therefore not eligible, and the fix (commit `a1d70f7`) makes the refill pass over it.
No threshold, weight, cap, ranking or universe changed. S itself (rank-ordered buys without
refill) and D13 are untouched by the fix.

No S result had been analysed when this was fixed: the crashed campaign produced no
`results.json`, nothing was rendered, and no S figure was compared with D13. (Locating the failing
arm meant reading the driver's per-run `sweep.arm_done` log lines, which print each finished
run's drawdown; those were not compared or used.) Every S and S+refill run is re-run on the fixed commit, with the
same D13 check and copy rule as §8. The runs of the crashed campaign are not used.

**Addendum, 2026-11-06 (≈13:00 IST): a second implementation fix, also not a rule change.** The
repo's gate forbids any rail-cap comparison under `backtest/` (A8 owns every rail; test
`test_the_backtest_holds_no_rail_logic_of_its_own`). The refill's projection compared sector value
to the cap itself, so it now asks A8 instead (commit `8786b7d`). It builds an A8 `Portfolio` of
the kept names plus cash, proposes each basket top-up as a whole-share `ProposedOrder`, and drops
a name when `analyst.rails.check_order` reports a `MAX_SECTOR` breach. The rule is §8's,
unchanged: a name whose top-up would take its sector above 35% of the book is replaced. Only the
code that evaluates it moved into A8, with top-ups now in whole shares as A8 sizes every order.
The re-run on `d1539a9` had started and was stopped before any S result was rendered; it is
discarded. S and S+refill are re-run on the fixed commit, with the same D13 check and copy rule.

**Addendum, 2026-11-06 (≈13:00 IST): the replacement now follows §8's wording; not a rule
change.** The gate's property test found a falsifying case: every name in one sector, `top_n` 3.
The refill raised "cannot weight an empty basket". The implementation had dropped each refused
name and taken the next-ranked name of *any* sector. §8 says the replacement is the next-ranked
name **outside the capped sector**, and that a capped name is *replaced*. The code now does
exactly that (commit `98b72d5`):
- a refused name and every lower-ranked name of its sector leave the candidates;
- better-ranked names of that sector that the basket already holds stay;
- if too few eligible names remain to fill the basket, the refill stops and leaves the basket to
  the gate, as S does.

Thresholds, weights, caps, ranking and universe are unchanged. The re-run on `449680f` was
stopped before any S result was rendered and is discarded. S and S+refill are re-run on the fixed
commit, with the same D13 check and copy rule.
