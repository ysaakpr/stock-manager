# Pre-registration: PE drawdown, 2026-10-19

Written **before** any run of this study. No figure of any comparator below (equal-exposure D13,
the tighter vol target, the stricter price regime, the price-only analogue) and no figure of any
robustness variant (42, 84 or 126 sessions, NIFTY 50 P/E), on any window, has been computed. Nothing
below may be changed after the first study figure exists; a changed arm, window or threshold is a
new trial and is reported as one.

## 0. The question, and what was already seen

The owner (2026-10-19): test the PE-timing study's arm **A3 at 63 sessions** properly, as a
**drawdown-first** candidate. A3-63 is D13 (`PAPER_RATIFIED_2026_09_06`) invested only while D13's
price regime is on **and** the NIFTY 500 P/E is below its trailing 63-session mean; it parks when
either turns.

**Main question:** does "price regime AND P/E below its 63-session mean" give a better return per
unit of drawdown than simpler ways of reaching the same lower risk, and does it hold up outside the
exact configuration that was picked?

**This is a post-hoc follow-up, and that is a selection effect.** A3-63 was picked *because* it had
the best XIRR/DD (1.07 against D13's 0.81) of the parent study's 24 arms (PR #127,
`ops/studies/pe-timing-2026-10-19.md`). Everything in the parent report has been read, including,
at ₹10 crore/day:

| A3-63 (parent, lake through 2026-10-16) | F1 | F2 | F3 | Continuous |
| --- | --- | --- | --- | --- |
| Pre-tax XIRR | 1.50% | 32.60% | 16.79% | 17.64% |
| After-tax XIRR (realised) | −0.11% | 28.31% | 14.25% | 15.00% |
| Max drawdown | 16.56% | 18.27% | 14.54% | 16.56% |
| D13's max drawdown | 25.04% | 20.40% | 24.74% | 29.93% |

Two consequences are stated now, before any run, so that nobody mistakes them for findings later:

1. **Criterion 3 below (after-tax XIRR above the cash hurdle in every window) is expected to fail
   on F1**: the parent's A3-63 earned −0.11 % after tax there, below any positive cash return. It
   passes only if today's re-run differs materially from the parent's run. The owner asked for the
   criterion; it is kept as asked and not softened to let the arm through.
2. The thresholds that *could* be chosen to suit the known figures (the 5 pp drawdown cut, the
   2-of-3 window count) are the owner's, not this study's. The one threshold this study sets — the
   10 % return floor (§5, C4) — sits below A3-63's known 14.15 % mean, so it is no obstacle to the
   arm; it is there to stop a future "defensive" arm that returns little more than cash.

What was also looked at before writing this, for calibration and feasibility only (no return or
drawdown of a comparator): the **average invested fraction** of the parent's runs (§3.2) — A3-63
47.6 % and D13 75.1 % on the continuous window at ₹10 crore/day — and of the round-1 M9.5 run
(`~/campaign/m12-rerun-2bb2b08`, D13 + 15 % vol target, older lake): 70.9 %. And the pre-2016 data
needed for an out-of-sample window (§4.2).

## 1. Code and data state

- Branch `research/pe-drawdown`, from `research/pe-timing` at `8f94eda` (PR #127, still open: this
  study stacks on it). The study runs on the commit that adds this file; the code it runs
  (`backtest/pe_drawdown.py`, `backtest/drawdown_rule.py`, three new D13 switches) is committed
  below it at `28abad3`.
- Engine switches exactly as the parent: book corporate actions on, interest on idle cash on
  (`backtest/repo_rates.yaml`, repo − 50 bp), every order through A8's ratified rails (`RailGate`),
  the one `SimBroker` and shared cost model, ₹10 lakh opening book, one deposit.
- Universe **`turnover_floor`** (NIFTY 500 PIT membership opens 2016-10-24, after F1 opens).
- Momentum-v2 candidate sets served from the signal panel `~/campaign/m14-6-panel-5283bc5` (cut
  2016-09-01 … 2026-10-13); before 2016-09 (the out-of-sample window) the engine computes them
  live — the same code path the panel was cut from.
- The three new switches on `MomentumV2Parameters` are off by default and absent from `repr` while
  off, so D13's fingerprint and every earlier run's digest are unchanged (pinned by a unit test).

## 2. Frozen baseline

**D13**, sweep label "Momentum v2, D13 paper config", on every window and floor of §4.

- **Reuse, by digest.** A run's digest is its full spec: code path, parameters, window, floor, the
  book's corporate-action calendar, cash-interest schedule. Every D13 (and candidate) run whose
  digest the parent study already holds on disk (`~/campaign/pe-timing-a369952/`) is copied from
  there rather than replayed — the same digest is the same run on the same data. A run whose
  digest differs is re-run on today's lake.
- D13 is **frozen** (`frozen-baseline.json`: every run's digest and the sha256 of its summary,
  ledger and NAV) **before any comparator runs**, and re-verified before phase 2 and at render.
- Each D13 run is compared with the parent's frozen record
  (`~/campaign/pe-timing-a369952/frozen-baseline.json`, struck at `a369952` on the lake through
  2026-10-16): identical, differs, or a new window. **If the lake has drifted again, this study's
  own D13, frozen on today's lake, is the baseline** (as the parent did), and the report records
  the drift.

## 3. The candidate and the comparators (fixed now)

Every comparator runs on the same windows, floors, universe, cost model, rails, cash interest and
tax treatment as the candidate.

### 3.1 Candidate

**A3-63**: the parent's arm "D13 + price regime AND PE<63d mean" — the same parameters, hence the
same digest on the same data: `valuation_gate = BOTH`, `valuation_mean_days = 63`, raw NIFTY 500
P/E, read on D13's monthly rebalance session.

### 3.2 Comparator 2 — equal-exposure D13 (a constant mix)

**Chosen form: a constant mix**, not "park the same number of months on a P/E-free rule". A
constant mix carries *no timing information at all*: it is the pure "less money in the market"
null, so beating it is the cleanest test that the P/E's timing — not the cash — cut the drawdown.
A parking rule that ignores the P/E has to pick *which* months, and every way of picking them is a
timing signal of its own (the price-only rules are tested separately, §3.4–3.5; a random draw would
add noise and a seed to choose).

- **Invested fraction** of a run, per NAV session: `1 − cash / NAV`, cash rebuilt from the ledger
  alone (the deposit, every fill's net amount on its trade date, every dividend and interest
  credit). The **average invested fraction** `e` is its mean over the run's NAV sessions.
- **The arm:** D13 with its basket capped at a constant `s` of capital on every rebalance (the
  existing pro-rata trim path when over, buys only up to the cap when under; the next-session
  redeploy honours the cap) — `exposure_fraction = s`. Between rebalances the mix drifts with the
  market, as D13's own weights do.
- **Calibration:** `s = e(A3-63) / e(D13)` on the **same window and floor**, from this study's own
  runs of both, to 0.001 (half-even). D13 is itself only ~75 % invested (it parks, and keeps cash),
  so `s` scales D13's book so that the mix's average exposure matches the candidate's. If `s ≥ 1`
  the comparator is D13 itself. `s` is struck from exposures only, never from a return, written
  once to `calibration.json`, and the arm runs in **phase 2**, after phase 1.
- Label: "Equal-exposure D13 (constant mix)".

### 3.3 Comparator 3 — volatility targeting

- **3a. VT15:** D13 + the existing M9.5 overlay, `vol_target_annual = 0.15`, rho 0.3 — the same
  parameters (and label) as the round-1 arm "Momentum v2, all on (M9.5)".
- **3b. The tighter target:** `T = 0.15 × e(A3-63) / e(VT15)` on the continuous window at
  ₹10 crore/day, from phase 1's runs, rounded to the nearest 0.5 pp (half-even), one value for every
  window and floor. Exposure under a vol target is `min(1, T / estimate)`, so the scaling is
  approximate; the realised exposure is reported. On the parent-era figures (A3-63 47.6 %, an older
  M9.5 run 70.9 %) this rule gives about 10 %; the binding figure is struck from this study's runs.
  If it does not come out below 15 % the study stops and reports why.
- **Known property of the existing overlay, not changed here:** on the session after a rebalance
  that sold names, D13's redeploy fills the freed cash toward full weights without re-applying the
  vol cap, so a vol-targeted book runs above its target exposure until the next rebalance trims it.
  That is the overlay as it exists in the code (the brief asks for it); its realised exposure is
  reported beside its figures.

### 3.4 Comparator 4 — a stricter price-only regime

**"D13, price regime only at ≥ 1.02 × 200d MA"**: D13's monthly regime, but the basket is held only
while the NIFTY 50 TRI is at or above **1.02 ×** its 200-session mean (`regime_monthly_band =
0.02`), on both the exit and the re-entry reading. 2 % is the band the repo already used for M14.5's
daily re-entry (`D13 + daily regime re-entry, 2% band`), taken as is and not fitted. It asks whether
a stricter *price-only* rule gets the same drawdown cut.

### 3.5 Comparator 5 (added by this study) — the price-only analogue of the P/E rule

**"D13 + price regime AND NIFTY 500 < its 63d mean"**: the candidate's rule exactly, with the
NIFTY 500 **close** (`IN.NSE.NIFTY_500.CLOSE`, the same NSE snapshot the P/E comes from) in place
of its P/E. P/E = price ÷ earnings and trailing earnings move slowly, so "P/E below its 63-session
mean" is close to "price below its 63-session mean" — the parent found the rule fires after falls.
If this analogue matches the candidate, the P/E adds nothing that the price did not already carry.
This is a fifth comparator beyond the four the brief lists; it is in the decision set of §5 and in
the trial count.

## 4. Windows and floors (fixed)

### 4.1 The windows

| Window | Dates | Role |
| --- | --- | --- |
| F1 | 2016-09-01 → 2019-08-30 | decides (C1–C5) |
| F2 | 2019-09-02 → 2022-08-31 | decides |
| F3 | 2022-09-01 → 2026-08-31 | decides |
| F1-F3 continuous | 2016-09-01 → 2026-08-31 | headline; decides nothing |
| M6 | 2019-07-01 → 2026-08-31 | the mandate's six-year window (skipped by the parent); robustness R5 |
| O1 | 2013-02-01 → 2015-01-30 | out of sample; robustness R6 |

Each window is a separate run from cash. **₹10 crore/day** decides; **₹1 crore/day** is run on every
window (robustness R4 reads it). O1 runs the candidate and the decision-set comparators only (not the
§6 robustness variants).

### 4.2 Out of sample: O1, and its caveats (checked before writing)

- The NIFTY 500 P/E starts 2012-10-01, each value knowable on its own session. The first
  first-of-month session with 63 points behind it is **2013-02-01**.
- Two first-of-month sessions have **no published P/E** (2015-02-02 and 2015-12-01; NSE's archive
  404s, `ops/gates/M11-index-valuation-backfill.md`). The gate fails loud there by design, and
  nothing is interpolated, so the longest clean span is **2013-02-01 → 2015-01-30** (24 rebalances).
  The two shorter spans after it (9 and 8 months) are not run: a sub-year window from cash says
  little about drawdown.
- **No PIT or survivorship problem found.** `turnover_floor` builds the universe from L1 listing
  windows and median traded value only (no index membership), and keeps names that later delisted
  (of 575 NSE EQ names above ₹1 crore/day on 2014-06-02, 194 no longer trade under that ISIN). The
  regime's NIFTY 50 TRI runs from 1999; 12-1 momentum has full look-back from 2012-06.
- **Data-quality caveat (stated, not disqualifying):** NSE corporate actions in the store start
  2016-09-01; before that only BSE's exist, so some splits, bonuses and dividends of NSE-only names
  are missing in O1. It affects every arm alike (same universe, same calendar), probably understating
  returns.
- **Not pristine:** D13's own 2012–2019 results were looked at in earlier studies, 2012-07 →
  2016-08 is F1's selection window in the round-2 pre-registration, and the parent's part B timed
  the NIFTY 500 TRI on this P/E from 2013-07 in aggregate. **D13 with the P/E gate has never been
  run before 2016-09**, so O1 is out of sample for the candidate's selection, not for D13.

## 5. Decision rule (fixed): `backtest/drawdown_rule.py`, `evaluate_drawdown_first`

A separate function from round 2's `backtest/decision_rule.py`, which is not edited. On F1–F3 at
₹10 crore/day; **XIRR/DD** is a run's pre-tax XIRR (one deposit) over the max drawdown of its pre-tax
NAV. The candidate must pass **all five**:

- **C1 — ratio:** its XIRR/DD beats, strictly, the XIRR/DD of **each** of D13, equal-exposure D13,
  VT15, the tighter vol target, the stricter price regime and the price-only analogue in **at least
  2 of the 3** windows. Compared as exact `Decimal` cross-products.
- **C2 — drawdown:** its worst max drawdown over F1–F3 is **at least 5.0 pp below** D13's worst.
- **C3 — cash hurdle:** its realised after-tax XIRR (30 % slab, no surcharge, FY-end tax) is
  strictly above the cash hurdle **in every window**. The hurdle is the after-tax return of holding
  the repo − 50 bp cash sleeve over the whole window (credited monthly, slab-taxed), struck by
  `backtest.ma_timing.simulate` with every session decided *out*.
- **C4 — return floor:** the arithmetic mean of its after-tax XIRRs over F1–F3 is **≥ 10.0 %**.
- **C5 — deflated ratio:** computed on the metric being ranked. The ratio is CAGR ÷ max drawdown of
  the candidate's daily pre-tax NAV returns, the three windows laid end to end (no return across a
  seam), CAGR over their summed calendar span. Threshold = D13's ratio on the same basis + E[max] of
  N zero-mean draws with the cross-trial variance V of the ratio (the deflated Sharpe ratio's
  formula, `backtest.sharpe.expected_max_sharpe`, applied to this ratio). Probability = the share of
  **2,000 stationary-bootstrap resamples** (Politis–Romano, mean block 21 sessions, seed 20261019)
  of the candidate's returns whose ratio exceeds the threshold. **Bar ≥ 0.95.** Fewer than 20
  distinct trial ratios in V is FAIL.

Uncomputable is FAIL; no window is dropped. **Verdicts (fixed now):**

- **REJECT** — any of C1–C5 fails.
- **NOT ROBUST** — C1–C5 all pass, but any robustness check R1–R6 (§6) fails.
- **ADOPT-AS-DEFENSIVE-VARIANT** — C1–C5 and R1–R6 all pass. It means "worth offering as a
  low-drawdown option next to D13", **not** replacing D13. Nothing changes by itself: paper keeps
  running D13 until the owner decides otherwise.

## 6. Robustness (reported regardless; never used to select)

Each check except R5–R6 asks whether the variant passes **C1 and C2** against the same rivals, with
one change: the equal-exposure rival is an **analytic** mix of D13's NAV and cash at the variant's
*own* average exposure (`s = e(variant) / e(D13)`, rebalanced every session, each step earning
`s × D13's return + (1 − s) × repo − 50 bp`; pre-tax, no costs), so a variant that is simply more in
cash gets no free win. The analytic mix at the candidate's own exposure is also shown beside the
engine run of §3.2, as a cross-check.

- **R1 — neighbouring lookbacks:** 42, 84 and 126 sessions (raw NIFTY 500 P/E). Passes if **at
  least 2 of the 3** pass C1 + C2. If only 63 works, the report says plainly that the benefit
  exists only at the configuration that was picked.
- **R2 — NIFTY 50 P/E** (raw) in place of NIFTY 500, at 63 sessions.
- **R3 — the P/E spliced** at the 2021-03-31 standalone → consolidated break (the parent's
  `valuation_splice`), at 63 sessions.
- **R4 — ₹1 crore/day:** the candidate at the low floor, against the low-floor rivals.
- **R5 — the M6 window** (single window): the candidate's XIRR/DD beats D13's and every
  comparator's (engine runs, equal-exposure included) **and** its max drawdown is ≥ 5 pp below
  D13's.
- **R6 — out of sample, O1** (single window): the same test as R5.

## 7. Trial count N and the ratio variance (fixed now)

**N = 115 + 11 = 126.** 115 is the parent's N. 11 is every arm this study runs except D13:
the candidate (re-run), equal-exposure D13, VT15, the tighter vol target, the stricter price regime,
the price-only analogue, 42, 84, 126 sessions, NIFTY 50 P/E and the spliced P/E. The candidate,
VT15 and the spliced arm were already counted in 115; they are counted again, as "every arm here"
asks — that only makes C5 harder to pass.

**V** is the sample variance of the C5 ratio over distinct trials on the same basis (F1–F3, ₹10
crore/day, pre-tax NAV): this study's 12 runs (D13 included), the parent's 18 part-A arms (their NAVs
in `~/campaign/pe-timing-a369952`, matched by parameters), and the 20 round-1 arms named in
`~/campaign/trial-sharpes-a4a4003/trial-sharpes.json` (their pre-tax NAVs beside it). A duplicate
value counts once. Each source's lake date differs slightly; the report prints the provenance.

## 8. Descriptive statistics (decide nothing)

- **Cash months** (continuous window, ₹10 crore/day): every month from one D13 rebalance to the
  next, classed by its opening reading as *both invested* (price on, P/E cheap), *P/E-only cash*
  (price on, P/E rich — D13 invested, the candidate parked by the P/E alone) or *price cash* (D13
  parked too). For each class: months, D13's compounded and mean return, up/down months, the
  candidate's return and actual exposure, the NIFTY 500 TRI. D13's ten worst and ten best months and
  the candidate's state in each; every P/E-only cash month; the candidate's exposure and return over
  D13's maximum-drawdown span, and D13's return over the candidate's. This answers whether the P/E
  dodged real drawdowns or mostly missed rallies.
- Per run: pre-tax and after-tax XIRR, max drawdown, XIRR/DD, average invested fraction, charges,
  tax, fills.

## 9. Report

`ops/studies/pe-drawdown-2026-10-19.md`: the verdict and a plain-English answer; a headline table
(XIRR, DD, XIRR/DD, after-tax return, % invested, charges) for the candidate and every comparator,
per window and continuous; the robustness grid; the out-of-sample finding; the cash-months analysis;
"follow-ups (new trials)". Generated tables come from `python -m backtest.pe_drawdown render`. A run
that fails is fixed and repeated, the fix noted; an arm's definition never changes after a figure
exists.

## 10. Amendments

An amendment may change an arm, a window or the rule only before any study figure exists. After
that, any change is a new trial.

(none)
