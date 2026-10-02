# Pre-registration: strategy round 2 (new signals), 2026-09-29

Written **before** any new signal has been evaluated, and before the fixed baseline has been run.
Nothing below may be changed after the first evaluation run. A changed hypothesis is a new trial:
it gets a new id here, and it counts toward the trial total.

## 0. Why this document exists

In round 1, the pre-chosen arm ranked 1st on 2012–19 and 13th of 23 on 2019–26, and it trailed
the NIFTY 50 TRI out of sample. Trying many tweaks and keeping the best one reliably finds gains
that do not hold up. This file fixes the hypotheses, their parameters, the evaluation windows and
the decision rule in advance, so the result can be trusted whichever way it comes out.

## 1. Data and code state it applies to

- `main` at `701e4cf` or later, with all round-2 fixes merged. Those are: the split-invariant stop
  (#19), cash interest (#20), MTO delivery backfill (#21), the delivery coverage gate, published
  NIFTY regime, seam-consistent prices, min order value (#22), and the lineage/L2 fixes (#23, #24).
- Lake: L0 104,528 payloads, 0 defects. L2 adjusted prices reach 2011-06-22 for 1,563 partitions.
  Delivery 2011–2016 is 52–64% joined, so the 80% coverage gate keeps the delivery legs off before
  about 2019. That is expected and not a defect.

## 2. Evaluation protocol (fixed)

- **Universe floor:** ₹10 crore/day only. ₹1 crore results may be reported but decide nothing.
- **Metric:** after-tax XIRR on realised gains, for a resident individual at the 30% slab, no
  surcharge, tax paid at FY end, with cash interest ON. Max drawdown is reported alongside.
- **Folds (expanding window, anchored at 2012-07-04):**

  | Fold | Selection (in-sample) | Test (out-of-sample) |
  |---|---|---|
  | F1 | 2012-07-04 → 2016-08-31 | 2016-09-01 → 2019-08-30 |
  | F2 | 2012-07-04 → 2019-08-30 | 2019-09-02 → 2022-08-31 |
  | F3 | 2012-07-04 → 2022-08-31 | 2022-09-01 → 2026-08-31 |

- **Baseline:** the fixed-code Swing composite (M10.7) and Swing composite + regime gate, run on
  the same folds first and frozen (digests recorded) before any H-arm is run.
- **Trial count for multiple-testing correction:** every arm in the round-2 sweep, plus the H-arms
  below, plus the round-1 sweep's 23 arms that were already looked at on this data. Initial count
  ≥ 28, with the exact number recorded in the verdict.

## 3. Hypotheses (parameters fixed now)

**H1 — residual momentum.** Replace plain 12-1 momentum in the swing composite with residual
momentum. For each name, regress its daily log returns on NIFTY 50 TRI daily log returns over the
trailing 252 sessions. Take the sum of residuals over sessions t−252 … t−21 and divide it by the
residual standard deviation over the same span. Names with fewer than 200 valid sessions in the
span are excluded from this leg. Everything else in the composite is unchanged. The first possible
signal is about 2013-07, which the anchor date allows for.

**H2 — price-band-hit avoidance.** Exclude from new buys any name that closed at its upper or lower
daily price band in any of the last 5 sessions, using the `bh` member of the NSE PR bundles
(knowable on its own publication date). The PR bundle is symbol-keyed, so a symbol maps to an ISIN
only through that same session's NSE bhavcopy `(symbol, series) → ISIN`. Unresolved rows are
counted and reported, never guessed. Existing holdings are not force-sold.

**H3 — H1 + H2 combined.** The only combination tested.

No other variants, lookbacks, thresholds or weights will be tried in this round.

## 4. Decision rule (fixed)

An H-arm is **kept** only if all of these hold against the frozen Swing composite baseline:

1. Mean after-tax XIRR across F1–F3 test windows improves by **≥ 1.0 pp**.
2. It improves in **at least 2 of the 3** test windows.
3. Max drawdown in no test window worsens by more than **3.0 pp**.
4. The deflated Sharpe ratio (Bailey & López de Prado), computed on daily after-tax NAV of the
   concatenated test windows with the recorded trial count, gives **probability ≥ 0.95** that the
   true Sharpe ratio exceeds the baseline's.

If no arm passes, the answer is "no improvement found", and it is reported as such.

## 5. Known open items that do not block this round

- 60 lineage-retired L2 partitions duplicate history for direct L2 readers. Backtests are not
  affected, because they gate names on L1.
- The Kesar Terminals bonus ratio (INE096L01025) looks mis-recorded (1:25 vs 1:1).
- The ₹1.2 lakh per-order cap still needs an owner decision on scaling.
- Sector data remains current-snapshot only, so the sector cap mostly binds on the unknown bucket.

## 6. Clarifications recorded before any evaluation

**2026-09-29, H1 regression intercept.** §3 did not say whether the regression has an intercept.
With one, the residuals over t−252…t−1 sum to zero by construction, so their t−252…t−21 sum is
just minus the last 20 sessions' residuals. That turns H1 into a one-month reversal signal, and a
steady stock-specific trend would score about zero, contrary to the hypothesis. H1 therefore fits
**β = Σ(r·m)/Σ(m²) with no intercept**, which keeps the stock's own drift in the residual. The
200-valid-session count is taken over t−252…t−21. A session is valid only when the stock and the
market both have a print on it and on the session before it.
This was decided from the definition alone: no H1 return, XIRR or drawdown had been computed or
seen (the implementation smoke reported trade counts only). It is a clarification, not a new trial.

**2026-09-29, H2 "new buys".** A blocked name gets no buy order of any kind for the 5-session window.
That covers both opening a new position and topping up an existing holding towards equal weight.
Existing holdings are never force-sold. The H2 smoke over 2013-07 → 2014-06 at ₹10cr blocked 0
buys: 44 blocked names fell in the candidate set, and none of them was in a top-20 the composite
would have bought. This was reported as a count only; no return figure was computed or seen. It is
a clarification, not a new trial.
