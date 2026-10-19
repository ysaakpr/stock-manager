# PE drawdown: is "price regime AND P/E below its 63-session mean" a real low-drawdown D13? Study report, 2026-10-19

Pre-registration: `ops/studies/preregistration-pe-drawdown-2026-10-19.md`, committed at `c5ac39d` before any
run and unchanged since (the render re-checks its sha256 against the frozen baseline record). Runs:
`~/campaign/pe-drawdown-c5ac39d/` (not committed). Logs: `~/campaign/pe-drawdown-c5ac39d-2026-10-19.log` (runs)
and `~/campaign/pe-drawdown-c5ac39d-render-2026-10-19.log` (render). Driver: `python -m backtest.pe_drawdown`, run
at `cfd9045`. Runs 21:43 → 23:39 IST, render 23:40 → 23:45 IST (2026-10-19). This study stacks on PR #127
(`research/pe-timing`), which is still open.

## Verdict: **REJECT**

**Plain answer: no. The drawdown cut is real, but it comes from holding less equity, not from the P/E.**
A3-63 ("D13 + price regime AND PE<63d mean") fails three of the five pre-registered criteria and all six
robustness checks:

- **C1 (XIRR/DD beats D13 and every comparator in ≥ 2 of 3 windows) — FAIL.** It beats D13 itself in only one
  window (F2). It loses F1 badly (0.09 against D13's 0.18) and F3 (1.15 against 1.30).
- **C2 (worst drawdown ≥ 5 pp below D13's) — PASS:** 18.27 % against 25.04 %.
- **C3 (after-tax XIRR above cash in every window) — FAIL.** F1 earned −0.11 % after tax, against 3.92 % for
  cash. The pre-registration said in advance that this was expected.
- **C4 (mean after-tax XIRR ≥ 10 %) — PASS:** 14.15 %.
- **C5 (deflated XIRR/DD, N = 126) — FAIL.** The probability is 0.131 against a bar of 0.95. On the F1–F3
  windows laid end to end its ratio is 0.78 against D13's 0.72. The deflated threshold is 1.28, and 44 distinct
  trial ratios have a cross-trial spread wide enough to cover that gap many times over.

**Does the P/E add anything beyond equal-exposure D13 or vol targeting? No.**

- D13 simply held at the same 48 % average exposure (the analytic constant mix, rebalanced daily, before costs)
  makes 17.30 % with a 16.08 % drawdown over the decade. A3-63 makes 17.64 % with 16.56 %. The XIRR/DD ratios
  are 1.08 and 1.07.
- Window by window, that mix beats A3-63 in F1 (0.47 vs 0.09) and F3 (1.50 vs 1.15). A3-63 wins only F2.
- Vol targeting (15 % and the calibrated 10 %) beats it in F3 by a wide margin (1.93 and 1.95 vs 1.15). Both
  lose to it in F2, and they split F1.
- The **price-only analogue** (the same rule on the NIFTY 500 *close* instead of its P/E) is worse than A3-63 in
  F2 and F3. So the P/E is not just a relabelled price signal. But nothing it adds survives the comparison with
  plain lower exposure.

**Is the benefit specific to 63 days? It is fragile.**

- Of the neighbouring lookbacks, only 84 sessions passes C1 + C2. 42 and 126 fail, so R1 fails.
- NIFTY 50 P/E fails. So does the spliced series, which is identical to raw at 63 sessions on every rebalance.
- At ₹1 crore/day it fails (decade ratio 0.99 against D13's 0.87, but it loses window by window).
- On the six-year mandate window it fails: ratio 1.29 against D13's 1.56, and the drawdown is only 1.4 pp lower.
- **Out of sample it fails.** On O1 (2013-02 → 2015-01, never run with the P/E gate before), it beats D13 on
  XIRR/DD at ₹10 crore (2.00 vs 1.40). But its drawdown is only 2.8 pp lower, and equal-exposure D13 (2.34), the
  10 % vol target (2.39) and the price analogue (3.16) all beat it. At ₹1 crore it made 3.46 % against D13's
  19.53 %.

ADOPT here would only have meant "offer it as a low-drawdown option beside D13". It does not earn even that.
Paper keeps running D13. If the owner wants a lower-drawdown D13, **the honest version is D13 at a lower,
constant exposure.** It gets the same decade XIRR/DD without a signal that worked at 63 and 84 sessions but not
at 42 or 126.

## Headline (₹10 crore/day; the rule reads F1–F3, the continuous run is presentation)

XIRR/DD is pre-tax XIRR ÷ max drawdown. "% invested" is the time-average share of the book in equities, rebuilt
from the ledger. Charges are every rupee of the shared cost model.

| Arm | F1 XIRR/DD | F2 | F3 | Decade pre-tax XIRR | Decade max DD | **Decade XIRR/DD** | Decade after-tax XIRR | % invested | Decade charges |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D13 (frozen; identical to #127's) | 0.18 | 1.55 | 1.30 | 24.18% | 29.93% | **0.81** | 21.18% | 75% | ₹178,320 |
| **A3-63 (candidate)** | **0.09** | **1.78** | **1.15** | 17.64% | 16.56% | **1.07** | 15.00% | 48% | ₹81,351 |
| Equal-exposure D13, analytic at A3-63's exposure (pre-tax, no costs) | 0.47 | 1.56 | 1.50 | 17.30% | 16.08% | **1.08** | n/a | 48% | — |
| Equal-exposure D13, engine run (exposure cap; overshot, see deviations) | 0.18 | 1.47 | 1.25 | 21.85% | 25.76% | **0.85** | 19.14% | 69% | ₹137,607 |
| D13 + 15 % vol target (M9.5) | 0.05 | 1.32 | 1.93 | 22.76% | 30.15% | **0.75** | 19.82% | 71% | ₹185,769 |
| D13 + 10.0 % vol target (calibrated) | 0.10 | 1.17 | 1.95 | 20.98% | 27.53% | **0.76** | 18.14% | 66% | ₹210,710 |
| D13, price regime only at ≥ 1.02 × 200d MA | −0.09 | 1.66 | 1.12 | 18.96% | 47.35% | **0.40** | 16.48% | 71% | ₹126,722 |
| D13 + price regime AND NIFTY 500 < its 63d mean (price analogue) | 0.39 | 0.77 | 0.66 | 12.01% | 29.49% | **0.41** | 10.11% | 44% | ₹42,857 |

Per-window XIRR, DD, after-tax return, exposure, charges and tax for every arm, at both floors and on M6 and O1,
are in the generated tables below. Cash hurdle (after tax): F1 3.92 %, F2 2.59 %, F3 3.85 %, decade 3.55 %.

## Robustness grid (each variant against D13, both vol targets, the stricter regime, the analogue and an analytic equal-exposure D13 at the variant's own exposure; C1 + C2)

| Variant | F1 / F2 / F3 XIRR/DD | Decade XIRR / DD / ratio | C1 + C2 |
| --- | --- | --- | --- |
| A3-63 (raw NIFTY 500 P/E) | 0.09 / 1.78 / 1.15 | 17.64% / 16.56% / 1.07 | FAIL (ratio) |
| 42 sessions | 0.11 / 1.75 / 0.53 | 15.04% / 23.25% / 0.65 | FAIL |
| 84 sessions | 0.44 / 1.58 / 1.54 | 17.30% / 16.44% / 1.05 | **PASS** |
| 126 sessions | 0.44 / 0.94 / 1.43 | 17.08% / 19.36% / 0.88 | FAIL |
| NIFTY 50 P/E, 63 | 0.28 / 1.22 / 1.06 | 16.90% / 17.92% / 0.94 | FAIL |
| Spliced P/E, 63 (identical runs) | 0.09 / 1.78 / 1.15 | 17.64% / 16.56% / 1.07 | FAIL |
| A3-63 at ₹1 crore/day | 0.24 / 1.65 / 1.49 | 20.23% / 20.40% / 0.99 | FAIL |
| A3-63 on M6 (2019-07 → 2026-08) | ratio 1.29 vs D13 1.56; DD 18.44% vs 19.87% | | FAIL |
| A3-63 on O1 (2013-02 → 2015-01) | ratio 2.00 vs D13 1.40; DD 10.02% vs 12.79% | | FAIL |

R1 needed two of the three neighbours to pass and got one. The 84-session variant passing is the kind of
isolated hit that a 126-trial search produces by chance; it is a new trial, not a rescue.

## Out of sample

The window before 2016-09 is **usable, with stated caveats**. It was checked before the pre-registration
(§4.2 there):

- The NIFTY 500 P/E starts 2012-10-01. The first rebalance with a full 63-session mean is 2013-02-01.
- The P/E is missing on two first-of-month sessions (2015-02-02 and 2015-12-01), and nothing is interpolated.
  That leaves O1 = 2013-02-01 → 2015-01-30 (24 rebalances) as the longest clean span.
- No point-in-time or survivorship problem. `turnover_floor` uses listing windows and traded value only, and
  keeps names that later delisted.
- Caveats:
  - Before 2016-09 the store has only BSE corporate actions, so some NSE-only splits and dividends are missing.
    This affects every arm alike.
  - D13's own 2012–19 results had been seen in earlier studies. Only the P/E-gated arm is genuinely unseen there.

**Result:** at ₹10 crore A3-63 beat D13 on XIRR/DD (2.00 vs 1.40, DD 10.0 % vs 12.8 %), but lost to equal
exposure, the 10 % vol target and the price analogue. At ₹1 crore it lost to D13 outright (3.46 % XIRR, 22.6 %
DD, vs 19.53 % and 20.1 %). Two years is short, but the out-of-sample evidence points the same way as the
in-sample: the P/E does not beat simply holding less.

## What the cash months were

Decade, ₹10 crore/day. Each month is classified by the readings on its opening rebalance.

| Month state | Months | D13 compounded | D13 mean / month | D13 up / down | A3-63 compounded | A3-63 exposure |
| --- | --- | --- | --- | --- | --- | --- |
| both invested | 36 | +148.6% | +2.67% | 24 / 12 | +157.7% | 89% |
| **P/E-only cash** (D13 invested, the P/E said rich) | **60** | **+179.1%** | **+1.88%** | 38 / 22 | +57.5% | 32% |
| price cash (D13 parked too) | 24 | +25.8% | +1.04% | 16 / 8 | +25.2% | 27% |

- **It did both: it dodged some drawdowns and missed as many rallies.** D13 earned +1.88 % a month in the 60
  months the P/E alone kept A3-63 out, close to its overall average of about +2.0 %. Ranked by D13's return,
  those months look like a random draw.
- Six of D13's ten worst months were P/E-only cash months, for example 2018-09 (−12.4 % for D13, −6.3 % for
  A3-63) and 2018-05/06.
- Six of D13's ten best months were also P/E-only cash, among them the 2023-06/07/08 run of about +15 % a month,
  in which A3-63 made +2–4 %.
- "Cash" is not empty: A8's minimum-holdings rail keeps a few names in a parked book, so A3-63 averaged 32 %
  equity in its P/E-cash months.
- Over D13's own maximum-drawdown span (2018-01-23 → 2020-03-23, −29.9 %) A3-63 averaged 41 % exposure and fell
  9.0 %. That is about what 41 % of D13's loss would be. The drawdown cut is the exposure cut.

## Deviations from the brief and the pre-registration

1. **Equal-exposure D13: the engine run did not reach its target exposure.** It ran at 69 % on the decade
   against A3-63's 48 % (63 % against 40 % in F1).
   - Cause, found in the code after the run: the constant cap reuses the vol-target code path. That path
     measures the basket over the *target* top-20 names only, so names the sell band keeps (ranks 21–30) sit
     outside the cap.
   - The same flaw affects the existing vol-target overlay: VT15 realised 71 % and the 10 % target 66 %.
   - The pre-registered **analytic** equal-exposure mix (exact exposure, pre-tax, no costs) answers the
     equal-exposure question, and it is reported beside the engine run.
   - The verdict does not depend on this: A3-63 already fails C1 against D13 itself, and fails C3 and C5.
   - A corrected engine run is listed below as a follow-up. It was not re-run here, because a changed comparator
     after the results were in would be a new trial.
2. **A fifth comparator was added:** the price-only analogue (pre-registration §3.5). It is in the decision set
   and in N.
3. **Out-of-sample window:** O1 covers 2013-02 → 2015-01 only, for the P/E-gap reason above, and runs only the
   candidate and the decision-set comparators.
4. **Reuse instead of replay:** D13 and A3-63 (and the spliced arm) on F1–F3 and the decade were copied by
   digest from #127's runs. Their digests were identical on today's lake, so D13 was not re-frozen; the
   comparison is in the generated section.
5. **The 6-year mandate window** used 2019-07-01 → 2026-08-31.
6. A render-only bug was fixed before any figure existed (commit `cfd9045`, matching the parent's runs on
   start *and* end).

## Follow-ups (new trials, not run)

- **Fix the exposure cap so it counts the whole book,** for the constant mix and the vol target alike. Then
  re-run equal-exposure D13 and both vol targets. A constant-exposure D13 is the obvious low-drawdown variant to
  offer, if the owner wants one.
- **84-session P/E gate** as its own pre-registered trial, on new data only. It is the one neighbour that
  passed. Judged here it is a 126-trial look-elsewhere hit.
- **P/E as a level:** a long-history percentile read only at extremes, as the parent proposed.

---

## Generated tables

Everything below is `python -m backtest.pe_drawdown render` output, verbatim
(`~/campaign/pe-drawdown-c5ac39d/reports/decision.md`).

---

# PE drawdown — the drawdown-first decision (generated)

*Rule: `backtest/drawdown_rule.py`, as pre-registered in `ops/studies/preregistration-pe-drawdown-2026-10-19.md` before any run. Generated by `python -m backtest.pe_drawdown render`.*

## Verdict: **REJECT**

**D13 + price regime AND PE<63d mean** against **Momentum v2, D13 paper config** and Equal-exposure D13 (constant mix), Momentum v2, all on (M9.5), D13 + tighter vol target, D13, price regime only at >= 1.02 x 200d MA, D13 + price regime AND NIFTY 500 < its 63d mean:

- **C1 FAIL** — XIRR/DD strictly beats the baseline's and every comparator's in at least 2 of 3 windows: vs Momentum v2, D13 paper config: beaten in 1 (F1 0.091 vs 0.184, F2 1.784 vs 1.550, F3 1.155 vs 1.296); vs Equal-exposure D13 (constant mix): beaten in 1 (F1 0.091 vs 0.176, F2 1.784 vs 1.470, F3 1.155 vs 1.249); vs Momentum v2, all on (M9.5): beaten in 2 (F1 0.091 vs 0.053, F2 1.784 vs 1.315, F3 1.155 vs 1.926); vs D13 + tighter vol target: beaten in 1 (F1 0.091 vs 0.103, F2 1.784 vs 1.175, F3 1.155 vs 1.955); vs D13, price regime only at >= 1.02 x 200d MA: beaten in 3 (F1 0.091 vs -0.091, F2 1.784 vs 1.662, F3 1.155 vs 1.123); vs D13 + price regime AND NIFTY 500 < its 63d mean: beaten in 2 (F1 0.091 vs 0.389, F2 1.784 vs 0.770, F3 1.155 vs 0.665) — not beaten often enough: Momentum v2, D13 paper config, Equal-exposure D13 (constant mix), D13 + tighter vol target
- **C2 PASS** — worst max drawdown at least 5.0 pp below the baseline's worst: worst 18.27% vs baseline 25.04%: +6.77 pp lower (bar 5.0 pp)
- **C3 FAIL** — after-tax XIRR above the cash sleeve's after-tax return in every window: F1 -0.11% vs cash 3.92%, F2 28.31% vs cash 2.59%, F3 14.25% vs cash 3.85%; fails in F1
- **C4 PASS** — mean after-tax XIRR at least 10.0%: arithmetic mean 14.15% (bar 10.0%)
- **C5 FAIL** — deflated XIRR/DD ratio vs the baseline's, 126 trials, gives probability >= 0.95: probability 0.1310; ratio 0.7790 vs baseline 0.7179, threshold 1.2759 (2000 stationary-bootstrap resamples, mean block 21 sessions, seed 20261019); ratio variance 4.566868e-02 from 44 distinct trial ratios (12 arms of this study + 18 PE-timing arms (pe-timing-a369952) + 20 from trial-sharpes-a4a4003/trial-sharpes.json @ a4a40035c493 (round-1 arms))

Robustness (decides NOT ROBUST only when C1-C5 all pass; reported regardless):

- R1 neighbouring lookbacks (≥ 2 of 42/84/126 pass C1+C2): **FAIL**
- R2 NIFTY 50 P/E: **FAIL**
- R3 spliced P/E: **FAIL**
- R4 ₹1 crore/day floor: **FAIL**
- R5 the 6-year mandate window: **FAIL**
- R6 out of sample (O1): **FAIL**

- Trial count N = **126**; ratio variance 4.566868e-02 from 44 distinct trial ratios (12 arms of this study + 18 PE-timing arms (pe-timing-a369952) + 20 from trial-sharpes-a4a4003/trial-sharpes.json @ a4a40035c493 (round-1 arms)).
- Left out of the variance: D13 + price regime AND PE<63d mean (spliced P/E): same as D13 + price regime AND PE<63d mean; D13 + PE<63d mean entry gate (spliced P/E) (PE timing): same as D13 + PE<63d mean entry gate (PE timing); D13 + price regime AND PE<63d mean (PE timing): same as D13 + price regime AND PE<63d mean; D13 + price regime AND PE<63d mean (spliced P/E) (PE timing): same as D13 + price regime AND PE<63d mean; D13, PE<63d mean replaces price regime (spliced P/E) (PE timing): same as D13, PE<63d mean replaces price regime (PE timing); Momentum v2, all on (M9.5): same as Momentum v2, all on (M9.5)
- Cash hurdle (after-tax, the cash sleeve over the whole window): F1 3.92%, F2 2.59%, F3 3.85%, F1-F3 3.55%, M6 3.37%, O1 4.97%
- Calibration (`calibration.json`): tighter vol target **0.100**; equal-exposure fractions F1-F3@10000000 0.667, F1-F3@100000000 0.633, F1@10000000 0.570, F1@100000000 0.518, F2@10000000 0.811, F2@100000000 0.808, F3@10000000 0.708, F3@100000000 0.644, M6@10000000 0.697, M6@100000000 0.683, O1@10000000 0.623, O1@100000000 0.612
- Baseline frozen at commit `cfd90457eab6` on the lake through 2026-10-19 before any comparator ran.
- Idle cash: settled cash earns RBI repo - 0.50% p.a. (Actual/365, daily, credited monthly; unsettled proceeds earn nothing; the final partial month is forfeited), taxed at slab as income from other sources.

### D13 against the parent study's frozen D13

- F1 test @10000000: identical
- F1 test @100000000: identical
- F1-F3 continuous @10000000: identical
- F1-F3 continuous @100000000: identical
- F2 test @10000000: identical
- F2 test @100000000: identical
- F3 test @10000000: identical
- F3 test @100000000: identical
- M6 mandate @10000000: new window (not in frozen-baseline.json)
- M6 mandate @100000000: new window (not in frozen-baseline.json)
- O1 out-of-sample @10000000: new window (not in frozen-baseline.json)
- O1 out-of-sample @100000000: new window (not in frozen-baseline.json)

## Candidate and comparators at ₹10 crore/day (decides)

| Arm | Window | Pre-tax XIRR | Max DD | **XIRR/DD** | After-tax XIRR | % invested | Charges | Tax | Buys / sells |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Momentum v2, D13 paper config | F1 | 4.60% | 25.04% | **0.18** | 3.23% | 78% | ₹20,569 | ₹42,397 | 247 / 154 |
| D13 + price regime AND PE<63d mean | F1 | 1.50% | 16.56% | **0.09** | -0.11% | 40% | ₹10,486 | ₹48,874 | 98 / 83 |
| Equal-exposure D13 (constant mix) | F1 | 3.90% | 22.21% | **0.18** | 2.51% | 63% | ₹17,759 | ₹43,174 | 262 / 205 |
| Momentum v2, all on (M9.5) | F1 | 1.34% | 25.14% | **0.05** | 0.28% | 74% | ₹22,394 | ₹32,161 | 304 / 239 |
| D13 + tighter vol target | F1 | 2.44% | 23.81% | **0.10** | 1.23% | 70% | ₹28,554 | ₹37,155 | 414 / 404 |
| D13, price regime only at >= 1.02 x 200d MA | F1 | -3.79% | 41.50% | **-0.09** | -5.44% | 71% | ₹18,363 | ₹48,190 | 225 / 142 |
| D13 + price regime AND NIFTY 500 < its 63d mean | F1 | 6.56% | 16.88% | **0.39** | 4.18% | 41% | ₹10,398 | ₹75,808 | 98 / 74 |
| Momentum v2, D13 paper config | F2 | 31.62% | 20.40% | **1.55** | 27.98% | 68% | ₹23,949 | ₹156,043 | 244 / 150 |
| D13 + price regime AND PE<63d mean | F2 | 32.60% | 18.27% | **1.78** | 28.31% | 55% | ₹21,695 | ₹182,358 | 185 / 143 |
| Equal-exposure D13 (constant mix) | F2 | 30.39% | 20.68% | **1.47** | 26.85% | 68% | ₹23,584 | ₹149,850 | 234 / 149 |
| Momentum v2, all on (M9.5) | F2 | 26.34% | 20.03% | **1.32** | 22.52% | 63% | ₹28,829 | ₹150,996 | 335 / 319 |
| D13 + tighter vol target | F2 | 21.45% | 18.26% | **1.17** | 18.14% | 59% | ₹32,327 | ₹123,764 | 446 / 394 |
| D13, price regime only at >= 1.02 x 200d MA | F2 | 30.07% | 18.09% | **1.66** | 26.69% | 64% | ₹21,887 | ₹144,336 | 212 / 135 |
| D13 + price regime AND NIFTY 500 < its 63d mean | F2 | 13.45% | 17.45% | **0.77** | 12.36% | 37% | ₹3,584 | ₹37,195 | 37 / 29 |
| Momentum v2, D13 paper config | F3 | 32.07% | 24.74% | **1.30** | 27.43% | 81% | ₹48,676 | ₹262,008 | 384 / 260 |
| D13 + price regime AND PE<63d mean | F3 | 16.79% | 14.54% | **1.15** | 14.25% | 53% | ₹19,009 | ₹126,883 | 194 / 136 |
| Equal-exposure D13 (constant mix) | F3 | 26.24% | 21.01% | **1.25** | 22.27% | 70% | ₹36,907 | ₹208,786 | 357 / 235 |
| Momentum v2, all on (M9.5) | F3 | 36.23% | 18.81% | **1.93** | 30.32% | 79% | ₹73,661 | ₹352,185 | 597 / 582 |
| D13 + tighter vol target | F3 | 37.46% | 19.16% | **1.95** | 31.06% | 76% | ₹95,990 | ₹379,956 | 721 / 726 |
| D13, price regime only at >= 1.02 x 200d MA | F3 | 26.69% | 23.76% | **1.12** | 22.72% | 78% | ₹42,401 | ₹214,429 | 333 / 239 |
| D13 + price regime AND NIFTY 500 < its 63d mean | F3 | 9.66% | 14.54% | **0.66** | 8.24% | 45% | ₹14,544 | ₹64,455 | 168 / 116 |
| Momentum v2, D13 paper config | F1-F3 | 24.18% | 29.93% | **0.81** | 21.18% | 75% | ₹178,320 | ₹1,127,393 | 1105 / 905 |
| D13 + price regime AND PE<63d mean | F1-F3 | 17.64% | 16.56% | **1.07** | 15.00% | 48% | ₹81,351 | ₹644,978 | 574 / 476 |
| Equal-exposure D13 (constant mix) | F1-F3 | 21.85% | 25.76% | **0.85** | 19.14% | 69% | ₹137,607 | ₹884,996 | 961 / 794 |
| Momentum v2, all on (M9.5) | F1-F3 | 22.76% | 30.15% | **0.75** | 19.82% | 71% | ₹185,769 | ₹1,044,089 | 1288 / 1337 |
| D13 + tighter vol target | F1-F3 | 20.98% | 27.53% | **0.76** | 18.14% | 66% | ₹210,710 | ₹901,086 | 1574 / 1667 |
| D13, price regime only at >= 1.02 x 200d MA | F1-F3 | 18.96% | 47.35% | **0.40** | 16.48% | 71% | ₹126,722 | ₹695,909 | 866 / 719 |
| D13 + price regime AND NIFTY 500 < its 63d mean | F1-F3 | 12.01% | 29.49% | **0.41** | 10.11% | 44% | ₹42,857 | ₹328,237 | 342 / 265 |
| Momentum v2, D13 paper config | M6 | 31.08% | 19.87% | **1.56** | 26.69% | 74% | ₹130,871 | ₹877,805 | 808 / 670 |
| D13 + price regime AND PE<63d mean | M6 | 23.71% | 18.44% | **1.29** | 19.95% | 51% | ₹63,426 | ₹536,523 | 429 / 363 |
| Equal-exposure D13 (constant mix) | M6 | 27.17% | 20.39% | **1.33** | 23.40% | 69% | ₹102,085 | ₹653,590 | 708 / 547 |
| Momentum v2, all on (M9.5) | M6 | 30.21% | 21.31% | **1.42** | 25.71% | 70% | ₹147,753 | ₹869,340 | 969 / 1058 |
| D13 + tighter vol target | M6 | 28.38% | 18.11% | **1.57** | 24.04% | 64% | ₹170,839 | ₹779,694 | 1180 / 1269 |
| D13, price regime only at >= 1.02 x 200d MA | M6 | 27.51% | 18.71% | **1.47** | 23.52% | 70% | ₹111,098 | ₹695,309 | 664 / 597 |
| D13 + price regime AND NIFTY 500 < its 63d mean | M6 | 11.84% | 17.40% | **0.68** | 10.09% | 40% | ₹24,439 | ₹183,446 | 203 / 160 |
| Momentum v2, D13 paper config | O1 | 17.88% | 12.79% | **1.40** | 17.54% | 82% | ₹9,068 | ₹7,259 | 142 / 72 |
| D13 + price regime AND PE<63d mean | O1 | 20.01% | 10.02% | **2.00** | 17.93% | 50% | ₹8,916 | ₹48,327 | 86 / 69 |
| Equal-exposure D13 (constant mix) | O1 | 21.26% | 9.06% | **2.34** | 20.63% | 70% | ₹8,498 | ₹13,792 | 145 / 95 |
| Momentum v2, all on (M9.5) | O1 | 20.46% | 10.32% | **1.98** | 19.48% | 79% | ₹12,103 | ₹23,055 | 192 / 154 |
| D13 + tighter vol target | O1 | 19.52% | 8.17% | **2.39** | 17.27% | 72% | ₹16,474 | ₹53,244 | 273 / 246 |
| D13, price regime only at >= 1.02 x 200d MA | O1 | 21.25% | 12.06% | **1.76** | 20.77% | 75% | ₹10,207 | ₹10,494 | 128 / 77 |
| D13 + price regime AND NIFTY 500 < its 63d mean | O1 | 23.17% | 7.34% | **3.16** | 21.64% | 50% | ₹5,624 | ₹33,775 | 52 / 40 |

## Candidate and comparators at ₹1 crore/day (information only — decides nothing)

| Arm | Window | Pre-tax XIRR | Max DD | **XIRR/DD** | After-tax XIRR | % invested | Charges | Tax | Buys / sells |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Momentum v2, D13 paper config | F1 | 12.05% | 28.58% | **0.42** | 10.01% | 75% | ₹20,682 | ₹66,010 | 195 / 140 |
| D13 + price regime AND PE<63d mean | F1 | 4.82% | 20.40% | **0.24** | 3.51% | 43% | ₹9,339 | ₹41,215 | 105 / 71 |
| Equal-exposure D13 (constant mix) | F1 | 13.79% | 22.70% | **0.61** | 11.71% | 66% | ₹18,446 | ₹68,390 | 214 / 151 |
| Momentum v2, all on (M9.5) | F1 | 10.85% | 24.04% | **0.45** | 7.98% | 66% | ₹27,618 | ₹92,713 | 311 / 399 |
| D13 + tighter vol target | F1 | 13.84% | 18.12% | **0.76** | 10.61% | 61% | ₹34,499 | ₹106,020 | 431 / 482 |
| D13, price regime only at >= 1.02 x 200d MA | F1 | 5.75% | 41.27% | **0.14** | 3.47% | 70% | ₹17,443 | ₹71,407 | 181 / 121 |
| D13 + price regime AND NIFTY 500 < its 63d mean | F1 | 11.77% | 18.47% | **0.64** | 8.80% | 41% | ₹8,549 | ₹99,560 | 77 / 52 |
| Momentum v2, D13 paper config | F2 | 38.54% | 18.04% | **2.14** | 33.73% | 60% | ₹27,679 | ₹222,147 | 241 / 167 |
| D13 + price regime AND PE<63d mean | F2 | 31.01% | 18.82% | **1.65** | 26.82% | 49% | ₹23,530 | ₹171,310 | 203 / 147 |
| Equal-exposure D13 (constant mix) | F2 | 37.84% | 19.27% | **1.96** | 33.13% | 61% | ₹27,302 | ₹217,373 | 245 / 168 |
| Momentum v2, all on (M9.5) | F2 | 31.23% | 19.29% | **1.62** | 26.37% | 55% | ₹32,405 | ₹195,647 | 372 / 322 |
| D13 + tighter vol target | F2 | 27.94% | 20.65% | **1.35** | 23.60% | 52% | ₹35,152 | ₹169,293 | 424 / 391 |
| D13, price regime only at >= 1.02 x 200d MA | F2 | 33.71% | 16.73% | **2.02** | 29.58% | 56% | ₹23,827 | ₹183,996 | 208 / 140 |
| D13 + price regime AND NIFTY 500 < its 63d mean | F2 | 15.46% | 25.31% | **0.61** | 14.60% | 49% | ₹2,961 | ₹29,902 | 37 / 24 |
| Momentum v2, D13 paper config | F3 | 31.55% | 17.74% | **1.78** | 27.58% | 77% | ₹49,165 | ₹226,610 | 395 / 289 |
| D13 + price regime AND PE<63d mean | F3 | 21.74% | 14.62% | **1.49** | 18.94% | 54% | ₹18,661 | ₹150,502 | 170 / 129 |
| Equal-exposure D13 (constant mix) | F3 | 29.11% | 25.94% | **1.12** | 25.27% | 71% | ₹45,625 | ₹208,472 | 387 / 279 |
| Momentum v2, all on (M9.5) | F3 | 33.14% | 16.00% | **2.07** | 27.35% | 72% | ₹71,425 | ₹334,457 | 551 / 563 |
| D13 + tighter vol target | F3 | 33.94% | 17.20% | **1.97** | 27.83% | 69% | ₹89,609 | ₹350,885 | 626 / 677 |
| D13, price regime only at >= 1.02 x 200d MA | F3 | 29.95% | 17.68% | **1.69** | 25.91% | 72% | ₹46,003 | ₹223,742 | 370 / 278 |
| D13 + price regime AND NIFTY 500 < its 63d mean | F3 | 15.06% | 18.42% | **0.82** | 13.13% | 47% | ₹13,926 | ₹93,292 | 139 / 103 |
| Momentum v2, D13 paper config | F1-F3 | 25.06% | 28.71% | **0.87** | 21.35% | 68% | ₹220,343 | ₹1,376,602 | 1135 / 1100 |
| D13 + price regime AND PE<63d mean | F1-F3 | 20.23% | 20.40% | **0.99** | 17.54% | 45% | ₹90,203 | ₹789,439 | 566 / 508 |
| Equal-exposure D13 (constant mix) | F1-F3 | 23.94% | 26.00% | **0.92** | 20.21% | 64% | ₹203,969 | ₹1,291,854 | 1129 / 1061 |
| Momentum v2, all on (M9.5) | F1-F3 | 22.64% | 24.04% | **0.94** | 18.87% | 61% | ₹214,731 | ₹1,164,295 | 1337 / 1493 |
| D13 + tighter vol target | F1-F3 | 23.27% | 18.48% | **1.26** | 19.32% | 56% | ₹242,979 | ₹1,237,213 | 1544 / 1786 |
| D13, price regime only at >= 1.02 x 200d MA | F1-F3 | 21.18% | 42.53% | **0.50** | 17.85% | 65% | ₹170,974 | ₹992,502 | 949 / 889 |
| D13 + price regime AND NIFTY 500 < its 63d mean | F1-F3 | 17.15% | 32.17% | **0.53** | 14.79% | 44% | ₹50,979 | ₹536,463 | 325 / 289 |
| Momentum v2, D13 paper config | M6 | 29.07% | 18.84% | **1.54** | 24.31% | 66% | ₹145,260 | ₹850,109 | 815 / 747 |
| D13 + price regime AND PE<63d mean | M6 | 26.95% | 18.80% | **1.43** | 22.83% | 46% | ₹70,872 | ₹659,536 | 434 / 380 |
| Equal-exposure D13 (constant mix) | M6 | 27.13% | 17.65% | **1.54** | 22.90% | 64% | ₹125,290 | ₹701,929 | 768 / 701 |
| Momentum v2, all on (M9.5) | M6 | 28.42% | 16.61% | **1.71** | 23.56% | 61% | ₹152,299 | ₹823,913 | 985 / 1059 |
| D13 + tighter vol target | M6 | 28.58% | 18.66% | **1.53** | 23.70% | 56% | ₹171,859 | ₹849,933 | 1089 / 1252 |
| D13, price regime only at >= 1.02 x 200d MA | M6 | 26.37% | 16.82% | **1.57** | 22.10% | 64% | ₹119,619 | ₹692,312 | 698 / 649 |
| D13 + price regime AND NIFTY 500 < its 63d mean | M6 | 15.83% | 25.40% | **0.62** | 13.76% | 45% | ₹25,219 | ₹252,666 | 203 / 159 |
| Momentum v2, D13 paper config | O1 | 19.53% | 20.12% | **0.97** | 18.08% | 70% | ₹11,791 | ₹34,000 | 177 / 103 |
| D13 + price regime AND PE<63d mean | O1 | 3.46% | 22.56% | **0.15** | 1.95% | 44% | ₹6,780 | ₹30,810 | 85 / 62 |
| Equal-exposure D13 (constant mix) | O1 | 17.51% | 21.14% | **0.83** | 16.19% | 65% | ₹10,573 | ₹30,019 | 169 / 104 |
| Momentum v2, all on (M9.5) | O1 | 21.20% | 16.07% | **1.32** | 19.04% | 67% | ₹13,430 | ₹51,635 | 214 / 155 |
| D13 + tighter vol target | O1 | 21.24% | 15.16% | **1.40** | 18.40% | 63% | ₹17,285 | ₹67,642 | 285 / 282 |
| D13, price regime only at >= 1.02 x 200d MA | O1 | 18.65% | 19.26% | **0.97** | 17.30% | 65% | ₹10,737 | ₹30,751 | 155 / 94 |
| D13 + price regime AND NIFTY 500 < its 63d mean | O1 | 16.32% | 13.80% | **1.18** | 14.91% | 40% | ₹4,937 | ₹30,735 | 56 / 42 |

## Robustness grid (generated)

Each variant against D13, VT15, the tighter vol target, the stricter price regime, the price-only analogue and an **analytic** equal-exposure D13 matched to the variant's own exposure (pre-tax, daily-rebalanced, no costs). Criteria 1 and 2 only.

| Variant | Floor | Result | Detail |
| --- | --- | --- | --- |
| D13 + price regime AND PE<42d mean | ₹10 cr | FAIL | ratio FAIL; drawdown PASS |
| D13 + price regime AND PE<84d mean | ₹10 cr | PASS | ratio PASS; drawdown PASS |
| D13 + price regime AND PE<126d mean | ₹10 cr | FAIL | ratio FAIL; drawdown PASS |
| D13 + price regime AND NIFTY 50 PE<63d mean | ₹10 cr | FAIL | ratio FAIL; drawdown PASS |
| D13 + price regime AND PE<63d mean (spliced P/E) | ₹10 cr | FAIL | ratio FAIL; drawdown PASS |
| D13 + price regime AND PE<63d mean | ₹1 cr | FAIL | ratio FAIL; drawdown PASS |
| D13 + price regime AND PE<63d mean, M6 2019-07 → 2026-08 | ₹10 cr | FAIL | ratio 1.29; beats Momentum v2, D13 paper config (no), Equal-exposure D13 (constant mix) (no), Momentum v2, all on (M9.5) (no), D13 + tighter vol target (no), D13, price regime only at >= 1.02 x 200d MA (no), D13 + price regime AND NIFTY 500 < its 63d mean (yes); DD 18.44% vs D13 19.87% (+1.43 pp lower) |
| D13 + price regime AND PE<63d mean, O1 2013-02 → 2015-01 | ₹10 cr | FAIL | ratio 2.00; beats Momentum v2, D13 paper config (yes), Equal-exposure D13 (constant mix) (no), Momentum v2, all on (M9.5) (yes), D13 + tighter vol target (no), D13, price regime only at >= 1.02 x 200d MA (yes), D13 + price regime AND NIFTY 500 < its 63d mean (no); DD 10.02% vs D13 12.79% (+2.77 pp lower) |

### Robustness variants at ₹10 crore/day

| Arm | Window | Pre-tax XIRR | Max DD | **XIRR/DD** | After-tax XIRR | % invested | Charges | Tax | Buys / sells |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D13 + price regime AND PE<63d mean | F1 | 1.50% | 16.56% | **0.09** | -0.11% | 40% | ₹10,486 | ₹48,874 | 98 / 83 |
| D13 + price regime AND PE<42d mean | F1 | 1.83% | 16.12% | **0.11** | 0.16% | 38% | ₹8,343 | ₹50,890 | 81 / 66 |
| D13 + price regime AND PE<84d mean | F1 | 7.15% | 16.30% | **0.44** | 5.89% | 43% | ₹10,905 | ₹40,855 | 97 / 81 |
| D13 + price regime AND PE<126d mean | F1 | 7.87% | 18.01% | **0.44** | 6.95% | 49% | ₹12,435 | ₹29,486 | 128 / 94 |
| D13 + price regime AND NIFTY 50 PE<63d mean | F1 | 5.05% | 17.92% | **0.28** | 3.03% | 42% | ₹10,552 | ₹63,888 | 96 / 78 |
| D13 + price regime AND PE<63d mean (spliced P/E) | F1 | 1.50% | 16.56% | **0.09** | -0.11% | 40% | ₹10,486 | ₹48,874 | 98 / 83 |
| D13 + price regime AND PE<63d mean | F2 | 32.60% | 18.27% | **1.78** | 28.31% | 55% | ₹21,695 | ₹182,358 | 185 / 143 |
| D13 + price regime AND PE<42d mean | F2 | 32.02% | 18.27% | **1.75** | 28.30% | 54% | ₹19,367 | ₹154,613 | 161 / 126 |
| D13 + price regime AND PE<84d mean | F2 | 28.83% | 18.27% | **1.58** | 24.60% | 54% | ₹22,599 | ₹167,085 | 199 / 148 |
| D13 + price regime AND PE<126d mean | F2 | 18.59% | 19.82% | **0.94** | 16.42% | 56% | ₹17,391 | ₹80,144 | 184 / 113 |
| D13 + price regime AND NIFTY 50 PE<63d mean | F2 | 22.24% | 18.27% | **1.22** | 19.05% | 49% | ₹15,633 | ₹120,715 | 158 / 110 |
| D13 + price regime AND PE<63d mean (spliced P/E) | F2 | 32.60% | 18.27% | **1.78** | 28.31% | 55% | ₹21,695 | ₹182,358 | 185 / 143 |
| D13 + price regime AND PE<63d mean | F3 | 16.79% | 14.54% | **1.15** | 14.25% | 53% | ₹19,009 | ₹126,883 | 194 / 136 |
| D13 + price regime AND PE<42d mean | F3 | 10.41% | 19.57% | **0.53** | 8.97% | 51% | ₹18,108 | ₹66,735 | 201 / 143 |
| D13 + price regime AND PE<84d mean | F3 | 19.06% | 12.35% | **1.54** | 15.87% | 46% | ₹17,548 | ₹158,112 | 149 / 110 |
| D13 + price regime AND PE<126d mean | F3 | 20.84% | 14.54% | **1.43** | 18.09% | 51% | ₹24,274 | ₹144,179 | 232 / 177 |
| D13 + price regime AND NIFTY 50 PE<63d mean | F3 | 15.43% | 14.54% | **1.06** | 12.75% | 54% | ₹21,642 | ₹131,590 | 227 / 153 |
| D13 + price regime AND PE<63d mean (spliced P/E) | F3 | 16.79% | 14.54% | **1.15** | 14.25% | 53% | ₹19,009 | ₹126,883 | 194 / 136 |
| D13 + price regime AND PE<63d mean | F1-F3 | 17.64% | 16.56% | **1.07** | 15.00% | 48% | ₹81,351 | ₹644,978 | 574 / 476 |
| D13 + price regime AND PE<42d mean | F1-F3 | 15.04% | 23.25% | **0.65** | 12.80% | 47% | ₹74,044 | ₹457,823 | 542 / 435 |
| D13 + price regime AND PE<84d mean | F1-F3 | 17.30% | 16.44% | **1.05** | 14.68% | 47% | ₹78,479 | ₹618,633 | 533 / 457 |
| D13 + price regime AND PE<126d mean | F1-F3 | 17.08% | 19.36% | **0.88** | 14.90% | 52% | ₹86,881 | ₹552,210 | 624 / 511 |
| D13 + price regime AND NIFTY 50 PE<63d mean | F1-F3 | 16.90% | 17.92% | **0.94** | 14.34% | 48% | ₹82,458 | ₹591,408 | 559 / 475 |
| D13 + price regime AND PE<63d mean (spliced P/E) | F1-F3 | 17.64% | 16.56% | **1.07** | 15.00% | 48% | ₹81,351 | ₹644,978 | 574 / 476 |
| D13 + price regime AND PE<63d mean | M6 | 23.71% | 18.44% | **1.29** | 19.95% | 51% | ₹63,426 | ₹536,523 | 429 / 363 |
| D13 + price regime AND PE<42d mean | M6 | 19.95% | 23.07% | **0.86** | 17.02% | 51% | ₹59,140 | ₹362,085 | 418 / 335 |
| D13 + price regime AND PE<84d mean | M6 | 21.61% | 18.44% | **1.17** | 18.07% | 49% | ₹55,292 | ₹467,758 | 390 / 322 |
| D13 + price regime AND PE<126d mean | M6 | 20.84% | 19.90% | **1.05** | 17.98% | 52% | ₹60,551 | ₹407,544 | 467 / 364 |
| D13 + price regime AND NIFTY 50 PE<63d mean | M6 | 19.58% | 18.44% | **1.06** | 16.50% | 50% | ₹55,371 | ₹400,547 | 416 / 325 |
| D13 + price regime AND PE<63d mean (spliced P/E) | M6 | 23.71% | 18.44% | **1.29** | 19.95% | 51% | ₹63,426 | ₹536,523 | 429 / 363 |
| D13 + price regime AND PE<63d mean | O1 | 20.01% | 10.02% | **2.00** | 17.93% | 50% | ₹8,916 | ₹48,327 | 86 / 69 |

### Robustness variants at ₹1 crore/day

| Arm | Window | Pre-tax XIRR | Max DD | **XIRR/DD** | After-tax XIRR | % invested | Charges | Tax | Buys / sells |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D13 + price regime AND PE<63d mean | F1 | 4.82% | 20.40% | **0.24** | 3.51% | 43% | ₹9,339 | ₹41,215 | 105 / 71 |
| D13 + price regime AND PE<42d mean | F1 | 4.87% | 20.40% | **0.24** | 3.55% | 41% | ₹7,359 | ₹41,855 | 84 / 55 |
| D13 + price regime AND PE<84d mean | F1 | 6.07% | 22.76% | **0.27** | 5.00% | 44% | ₹9,075 | ₹33,883 | 97 / 68 |
| D13 + price regime AND PE<126d mean | F1 | 3.45% | 31.28% | **0.11** | 2.50% | 49% | ₹10,501 | ₹29,397 | 116 / 83 |
| D13 + price regime AND NIFTY 50 PE<63d mean | F1 | 9.56% | 18.27% | **0.52** | 7.94% | 42% | ₹9,775 | ₹54,632 | 103 / 68 |
| D13 + price regime AND PE<63d mean (spliced P/E) | F1 | 4.82% | 20.40% | **0.24** | 3.51% | 43% | ₹9,339 | ₹41,215 | 105 / 71 |
| D13 + price regime AND PE<63d mean | F2 | 31.01% | 18.82% | **1.65** | 26.82% | 49% | ₹23,530 | ₹171,310 | 203 / 147 |
| D13 + price regime AND PE<42d mean | F2 | 27.92% | 18.82% | **1.48** | 24.41% | 48% | ₹21,929 | ₹133,840 | 193 / 146 |
| D13 + price regime AND PE<84d mean | F2 | 31.83% | 18.82% | **1.69** | 27.41% | 49% | ₹23,925 | ₹182,501 | 194 / 151 |
| D13 + price regime AND PE<126d mean | F2 | 23.65% | 20.83% | **1.14** | 21.00% | 48% | ₹18,503 | ₹103,176 | 180 / 125 |
| D13 + price regime AND NIFTY 50 PE<63d mean | F2 | 28.82% | 18.82% | **1.53** | 25.06% | 47% | ₹19,217 | ₹148,184 | 171 / 125 |
| D13 + price regime AND PE<63d mean (spliced P/E) | F2 | 31.01% | 18.82% | **1.65** | 26.82% | 49% | ₹23,530 | ₹171,310 | 203 / 147 |
| D13 + price regime AND PE<63d mean | F3 | 21.74% | 14.62% | **1.49** | 18.94% | 54% | ₹18,661 | ₹150,502 | 170 / 129 |
| D13 + price regime AND PE<42d mean | F3 | 20.09% | 15.22% | **1.32** | 17.11% | 49% | ₹19,921 | ₹147,666 | 184 / 135 |
| D13 + price regime AND PE<84d mean | F3 | 26.66% | 8.99% | **2.97** | 22.48% | 46% | ₹19,643 | ₹223,413 | 150 / 117 |
| D13 + price regime AND PE<126d mean | F3 | 20.89% | 14.62% | **1.43** | 17.63% | 50% | ₹24,857 | ₹168,017 | 217 / 169 |
| D13 + price regime AND NIFTY 50 PE<63d mean | F3 | 19.83% | 14.62% | **1.36** | 17.37% | 53% | ₹21,333 | ₹128,769 | 187 / 146 |
| D13 + price regime AND PE<63d mean (spliced P/E) | F3 | 21.74% | 14.62% | **1.49** | 18.94% | 54% | ₹18,661 | ₹150,502 | 170 / 129 |
| D13 + price regime AND PE<63d mean | F1-F3 | 20.23% | 20.40% | **0.99** | 17.54% | 45% | ₹90,203 | ₹789,439 | 566 / 508 |
| D13 + price regime AND PE<42d mean | F1-F3 | 18.82% | 20.40% | **0.92** | 16.42% | 45% | ₹84,342 | ₹636,204 | 540 / 482 |
| D13 + price regime AND PE<84d mean | F1-F3 | 19.10% | 22.76% | **0.84** | 16.60% | 46% | ₹79,198 | ₹691,428 | 510 / 456 |
| D13 + price regime AND PE<126d mean | F1-F3 | 18.23% | 31.28% | **0.58** | 16.07% | 48% | ₹86,395 | ₹589,356 | 576 / 501 |
| D13 + price regime AND NIFTY 50 PE<63d mean | F1-F3 | 21.34% | 18.27% | **1.17** | 18.42% | 45% | ₹103,627 | ₹891,782 | 576 / 554 |
| D13 + price regime AND PE<63d mean (spliced P/E) | F1-F3 | 20.23% | 20.40% | **0.99** | 17.54% | 45% | ₹90,203 | ₹789,439 | 566 / 508 |
| D13 + price regime AND PE<63d mean | M6 | 26.95% | 18.80% | **1.43** | 22.83% | 46% | ₹70,872 | ₹659,536 | 434 / 380 |
| D13 + price regime AND PE<42d mean | M6 | 22.40% | 18.80% | **1.19** | 19.01% | 46% | ₹62,660 | ₹458,897 | 432 / 359 |
| D13 + price regime AND PE<84d mean | M6 | 27.35% | 18.80% | **1.45** | 23.22% | 45% | ₹67,299 | ₹660,322 | 410 / 373 |
| D13 + price regime AND PE<126d mean | M6 | 23.42% | 20.81% | **1.13** | 20.16% | 46% | ₹67,979 | ₹485,717 | 452 / 390 |
| D13 + price regime AND NIFTY 50 PE<63d mean | M6 | 24.69% | 18.80% | **1.31** | 20.87% | 47% | ₹71,385 | ₹574,040 | 437 / 398 |
| D13 + price regime AND PE<63d mean (spliced P/E) | M6 | 26.95% | 18.80% | **1.43** | 22.83% | 46% | ₹70,872 | ₹659,536 | 434 / 380 |
| D13 + price regime AND PE<63d mean | O1 | 3.46% | 22.56% | **0.15** | 1.95% | 44% | ₹6,780 | ₹30,810 | 85 / 62 |

### Equal exposure: engine run against the analytic mix (candidate's exposure)

| Window | Floor | Engine: XIRR / DD / ratio / exposure | Analytic: XIRR / DD / ratio / exposure |
| --- | --- | --- | --- |
| F1 | ₹10 crore/day | 3.90% / 22.21% / 0.18 / 63% | 5.56% / 11.84% / 0.47 / 40% |
| F2 | ₹10 crore/day | 30.39% / 20.68% / 1.47 / 68% | 26.06% / 16.66% / 1.56 / 55% |
| F3 | ₹10 crore/day | 26.24% / 21.01% / 1.25 / 70% | 22.59% / 15.10% / 1.50 / 53% |
| F1-F3 | ₹10 crore/day | 21.85% / 25.76% / 0.85 / 69% | 17.30% / 16.08% / 1.08 / 48% |
| M6 | ₹10 crore/day | 27.17% / 20.39% / 1.33 / 69% | 22.64% / 13.76% / 1.64 / 51% |
| O1 | ₹10 crore/day | 21.26% / 9.06% / 2.34 / 70% | 14.04% / 6.55% / 2.14 / 50% |
| F1 | ₹1 crore/day | 13.79% / 22.70% / 0.61 / 66% | 9.82% / 15.96% / 0.62 / 43% |
| F2 | ₹1 crore/day | 37.84% / 19.27% / 1.96 / 61% | 31.45% / 14.79% / 2.13 / 49% |
| F3 | ₹1 crore/day | 29.11% / 25.94% / 1.12 / 71% | 23.90% / 12.20% / 1.96 / 54% |
| F1-F3 | ₹1 crore/day | 23.94% / 26.00% / 0.92 / 64% | 18.46% / 18.78% / 0.98 / 45% |
| M6 | ₹1 crore/day | 27.13% / 17.65% / 1.54 / 64% | 21.61% / 13.39% / 1.61 / 46% |
| O1 | ₹1 crore/day | 17.51% / 21.14% / 0.83 / 65% | 15.10% / 11.67% / 1.29 / 44% |

## Cash months (generated)

Continuous window 2016-09-01 → 2026-08-31, ₹10 crore/day. Each month runs from a D13 rebalance session to the next; its state is read on the opening session (D13's price regime: NIFTY 50 TRI ≥ its 200-session mean; P/E gate: NIFTY 500 P/E < its 63-session mean). The candidate is invested only in *both invested* months; *P/E-only cash* months are the ones the P/E alone took it out of (D13 was invested); in *price cash* months D13 itself was parked. The candidate's actual exposure is its ledger's (A8's minimum-holdings rail keeps a few names in a parked book).

| Month state | Months | D13 compounded | D13 mean / month | D13 up / down months | Candidate compounded | Candidate mean exposure | NIFTY 500 TRI compounded |
| --- | --- | --- | --- | --- | --- | --- | --- |
| both invested | 36 | +148.56% | +2.67% | 24 / 12 | +157.68% | 89% | +65.13% |
| P/E-only cash | 60 | +179.10% | +1.88% | 38 / 22 | +57.47% | 32% | +59.27% |
| price cash | 24 | +25.76% | +1.04% | 16 / 8 | +25.17% | 27% | +32.97% |

D13's ten **worst** and ten **best** months, and what state the candidate was in:

| Rank | Worst month | D13 | Candidate | State | Best month | D13 | Candidate | State |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 2018-09 | -12.41% | -6.29% | P/E-only cash | 2023-06 | +15.66% | +2.62% | P/E-only cash |
| 2 | 2020-03 | -10.07% | -6.74% | price cash | 2023-08 | +15.55% | +3.67% | P/E-only cash |
| 3 | 2018-05 | -7.99% | -3.79% | P/E-only cash | 2023-07 | +15.42% | +1.93% | P/E-only cash |
| 4 | 2019-07 | -7.79% | -1.66% | P/E-only cash | 2024-01 | +12.11% | +4.42% | P/E-only cash |
| 5 | 2026-01 | -7.34% | -5.56% | P/E-only cash | 2021-02 | +12.01% | +16.81% | both invested |
| 6 | 2023-01 | -7.32% | -1.16% | P/E-only cash | 2024-04 | +11.76% | +4.50% | P/E-only cash |
| 7 | 2022-02 | -6.63% | -6.63% | both invested | 2023-11 | +11.73% | +11.75% | both invested |
| 8 | 2018-06 | -6.19% | -1.42% | P/E-only cash | 2025-05 | +11.06% | +3.56% | P/E-only cash |
| 9 | 2025-02 | -5.07% | -3.05% | price cash | 2020-11 | +10.81% | +12.83% | both invested |
| 10 | 2016-11 | -4.62% | -6.35% | both invested | 2021-04 | +10.63% | +12.58% | both invested |

Of D13's ten worst months the candidate was in P/E-only cash in 6, price cash in 2 and invested in 2; of its ten best, P/E-only cash in 6, price cash in 0 and invested in 4.

Every P/E-only cash month (D13 invested, the P/E gate rich):

| Month | D13 | Candidate | Candidate exposure | NIFTY 500 TRI |
| --- | --- | --- | --- | --- |
| 2016-09 | +4.38% | +0.49% | 0% | +0.86% |
| 2016-10 | +1.08% | +0.49% | 0% | -0.32% |
| 2017-02 | +4.77% | +2.63% | 36% | +3.34% |
| 2017-03 | +1.84% | +0.23% | 36% | +4.02% |
| 2017-04 | +3.98% | +1.74% | 37% | +2.26% |
| 2017-05 | -2.30% | -0.46% | 36% | +1.76% |
| 2017-06 | -0.39% | +0.88% | 37% | +0.82% |
| 2017-07 | +2.94% | +3.05% | 38% | +4.97% |
| 2017-08 | +0.61% | +0.07% | 38% | -0.54% |
| 2017-09 | +0.09% | +2.14% | 39% | -0.99% |
| 2017-10 | +8.68% | +3.46% | 40% | +6.68% |
| 2017-11 | +2.42% | -0.47% | 40% | -1.85% |
| 2017-12 | +8.11% | +4.49% | 41% | +4.13% |
| 2018-01 | -2.98% | +0.67% | 43% | +2.71% |
| 2018-02 | -2.38% | -0.86% | 42% | -4.63% |
| 2018-05 | -7.99% | -3.79% | 24% | -1.86% |
| 2018-06 | -6.19% | -1.42% | 20% | -1.41% |
| 2018-08 | +3.90% | +1.32% | 32% | +2.95% |
| 2018-09 | -12.41% | -6.29% | 27% | -7.56% |
| 2019-01 | +1.45% | -3.42% | 27% | -1.50% |
| 2019-02 | -0.71% | -0.30% | 21% | -0.08% |
| 2019-03 | +4.73% | +2.62% | 23% | +7.43% |
| 2019-04 | +0.31% | -1.46% | 22% | -0.66% |
| 2019-05 | +1.36% | +0.64% | 21% | +3.07% |
| 2019-06 | -2.00% | -1.52% | 19% | -1.93% |
| 2019-07 | -7.79% | -1.66% | 19% | -7.73% |
| 2019-11 | -0.50% | +0.69% | 22% | +1.00% |
| 2019-12 | +2.12% | +1.04% | 18% | +0.93% |
| 2020-01 | -0.93% | +1.91% | 20% | -2.84% |
| 2020-08 | +1.18% | +2.89% | 21% | +5.83% |
| 2020-09 | +5.26% | -0.02% | 21% | +0.25% |
| 2020-10 | -1.97% | +0.44% | 21% | +1.55% |
| 2020-12 | +4.34% | +3.03% | 30% | +6.85% |
| 2021-01 | -0.61% | +1.85% | 28% | +1.71% |
| 2022-08 | +8.73% | +3.93% | 28% | +2.63% |
| 2022-09 | +1.55% | -1.14% | 29% | -3.74% |
| 2022-11 | +4.16% | +0.68% | 32% | +3.03% |
| 2022-12 | -4.35% | -2.07% | 27% | -3.00% |
| 2023-01 | -7.32% | -1.16% | 26% | -4.34% |
| 2023-05 | +2.40% | +1.31% | 30% | +3.19% |
| 2023-06 | +15.66% | +2.62% | 30% | +5.01% |
| 2023-07 | +15.42% | +1.93% | 32% | +3.27% |
| 2023-08 | +15.55% | +3.67% | 34% | +0.33% |
| 2024-01 | +12.11% | +4.42% | 45% | +1.61% |
| 2024-02 | -1.05% | -0.33% | 43% | +3.02% |
| 2024-03 | -2.54% | +0.47% | 41% | +0.72% |
| 2024-04 | +11.76% | +4.50% | 43% | +2.89% |
| 2024-05 | +3.74% | +1.22% | 45% | +3.62% |
| 2024-06 | +3.30% | +3.37% | 46% | +4.41% |
| 2024-07 | +6.60% | +1.43% | 48% | +3.52% |
| 2024-08 | -0.82% | +2.76% | 48% | +1.25% |
| 2024-09 | +1.73% | +4.84% | 50% | +2.16% |
| 2024-10 | -3.65% | -1.52% | 50% | -5.90% |
| 2025-05 | +11.06% | +3.56% | 21% | +3.91% |
| 2025-06 | +1.30% | +0.05% | 22% | +3.61% |
| 2025-07 | +2.60% | +0.41% | 22% | -3.82% |
| 2025-11 | -0.66% | -0.33% | 42% | +0.52% |
| 2025-12 | +1.63% | +2.60% | 38% | -0.05% |
| 2026-01 | -7.34% | -5.56% | 39% | -5.39% |
| 2026-08 | +7.10% | +0.41% | 44% | -1.43% |

D13's maximum drawdown ran from 2018-01-23 to 2020-03-23 (29.93%); over that span the candidate's mean exposure was 41% and its NAV moved -8.99% (D13 -29.93%).
The candidate's own maximum drawdown ran from 2018-08-31 to 2019-08-22 (16.56%); D13 moved -19.29% over it.

## Trial ratios counted in the variance

Momentum v2, D13 paper config 0.718, D13 + price regime AND PE<63d mean 0.779, Equal-exposure D13 (constant mix) 0.676, Momentum v2, all on (M9.5) 0.683, D13 + tighter vol target 0.691, D13, price regime only at >= 1.02 x 200d MA 0.354, D13 + price regime AND NIFTY 500 < its 63d mean 0.399, D13 + price regime AND PE<42d mean 0.605, D13 + price regime AND PE<84d mean 0.991, D13 + price regime AND PE<126d mean 0.814, D13 + price regime AND NIFTY 50 PE<63d mean 0.666, D13 + PE<200d mean entry gate (PE timing) 0.896, D13 + PE<200d mean entry gate (spliced P/E) (PE timing) 0.856, D13 + PE<21d mean entry gate (PE timing) 0.399, D13 + PE<21d mean entry gate (spliced P/E) (PE timing) 0.386, D13 + PE<63d mean entry gate (PE timing) 0.786, D13 + price regime AND PE<200d mean (PE timing) 0.876, D13 + price regime AND PE<200d mean (spliced P/E) (PE timing) 0.817, D13 + price regime AND PE<21d mean (PE timing) 0.529, D13 + price regime AND PE<21d mean (spliced P/E) (PE timing) 0.481, D13, PE<200d mean replaces price regime (PE timing) 0.527, D13, PE<200d mean replaces price regime (spliced P/E) (PE timing) 0.494, D13, PE<21d mean replaces price regime (PE timing) 0.360, D13, PE<21d mean replaces price regime (spliced P/E) (PE timing) 0.327, D13, PE<63d mean replaces price regime (PE timing) 0.470, Reversal: 5-day losers 0.086, Trend: 1-month 0.250, Delivery acceleration 0.266, Turnover expansion 0.191, Mean proximity (50d) 0.364, Short composite 0.360, Short composite + reversal 0.553, Breakout 0.435, M10.7 + delivery acceleration 0.556, M10.7 + 1-month trend 0.718, Short composite, 2-week holds 0.266, Short composite, 1-month holds 0.314, Short composite, 3-month holds 0.552, Short composite + regime gate 0.321, Short composite + low-vol leg 0.353, Short composite + regime + low-vol 0.355, Short composite, top-10 0.445, Short composite, top-10 + regime 0.407, Naive momentum (M4.10) 0.585

