# Closing the two data gaps: delivery, and balance-sheet fundamentals

**Date:** 2026-09-04. Written while the M10.4 fundamentals campaign is still running.

Two gaps were called out as blocking after the per-instrument inventory: `deliv_qty`/`deliv_pct`
NULL in 100% of `prices_raw`, and "no balance sheet" in `pit_fundamentals`. Both were investigated
against the real data rather than the documentation. **One of them was overstated by me, and the
correction is the most valuable finding here.**

## Correction first: there *is* balance-sheet data, and we are not extracting it

I previously wrote that ROE, ROCE, book value, P/B and leverage "cannot be computed at all" because
a quarterly results filing is profit-and-loss only. That is wrong. SEBI LODR requires a balance
sheet alongside annual results, and the XBRL carries it. Searching 500 randomly-sampled documents
from the 9,514 already in L0, by the period each filing reports:

| Element | All filings | Annual filings | Unlocks |
|---|---|---|---|
| `PaidUpValueOfEquityShareCapital` | **100%** | 100% | with face value → **share count** |
| `FaceValueOfEquityShareCapital` | **100%** | 100% | ″ |
| `ReserveExcludingRevaluationReserves` | 0% | **91%** | with paid-up → **shareholders' equity** |
| `DebtEquityRatio` | ~28% | 29% | leverage, stated directly |
| `SegmentAssets` / `SegmentLiabilities` | ~37% | 42% | segment ROA, capital intensity |
| `UnAllocableAssets` / `UnAllocableLiabilities` | ~35% | 39% | with segments → **total assets** |
| `GrossNonPerformingAssets`, `NonPerformingAssets`, `ReturnOnAssets`, `PercentageOfGrossNpa` | banking only | | **bank asset quality** |

Two derivations follow immediately, and both are exact rather than estimated:

* **Shares outstanding** = `PaidUpValueOfEquityShareCapital / FaceValueOfEquityShareCapital`, at
  **100% coverage**. That gives market capitalisation, and therefore aggregate P/E and any
  per-share metric, without needing a share-count source at all.
* **Shareholders' equity** = paid-up capital + reserves excluding revaluation, at **91% of annual
  filings**. That gives book value, **P/B and ROE on an annual basis** — the factor family I said
  was impossible.

Revaluation reserves are excluded by the element's own definition, which is the conservative
choice: a revaluation surplus inflates book value without cash ever moving, so leaving it out makes
book value more comparable across companies, not less.

**What is still genuinely absent**, and should stay described that way: no cash-flow statement, so
free cash flow and accruals remain impossible; no gross-block or working-capital detail, so ROCE
can only be approximated (equity + debt, where the debt/equity ratio is stated) rather than
computed; and quarterly equity is unavailable, so ROE is an annual measure here, not a rolling one.

## Validation: the derivations checked, before building anything

Both derivations were tested before committing to a schema migration, which turned up one design
change that would otherwise have shipped as a silent defect.

### Shares outstanding — 95.0% correct, and the failures detect themselves

`profit_after_tax / eps_basic` is an implied share count using only whitelisted concepts, so it is
a free, offline check on `paid_up / face_value`. Over the **10,547 filings** where both are
computable:

| Agreement | Filings | |
|---|---|---|
| within ±50% | **10,016 (95.0%)** | the derivation is right |
| ×1e5 or worse | 97 (0.9%) | filer scale error |
| ×10–1e3 | 233 (2.2%) | filer scale error |
| < 0.5× | 201 (1.9%) | filer scale error |

The failures are not noise — they are **clean powers of ten**. KIOCL states
`PaidUpValueOfEquityShareCapital = 62,192,556,500,000` with `unitRef="INR"`, which would be ₹62
lakh crore for a company whose paid-up capital is around ₹600 crore; BASF, SPAL and VIKASMCORP are
off by almost exactly 1e6. The absolute-rupee scale verified earlier was verified on *P&L*
concepts (a captured Reliance filing's revenue matches its published crore figure exactly); it does
**not** hold for the capital elements, and assuming it did would have put a 1e6 error into a market
cap.

**So Action 1 must ship with a guard, not just a mapping:** derive the share count, compare it to
`profit_after_tax / eps_basic`, and refuse the fact when they disagree beyond a tolerance rather
than storing a number that is wrong by six orders of magnitude. The filing's own EPS is the
detector, so this costs nothing and needs no third party.

### Equity — validated against Screener, with one definitional difference

Equity has no equivalent internal identity, so this one needed an outside source. Screener's Excel
export is `BLOCKED_CREDENTIAL`, but the register records a verified working substitute — the
robots-permitted company page — and the M7.1 parser already reads its tables. **Invariant #8
permits this**: it quarantines restated fundamentals from *backtests and decisions*, and comparing
our arithmetic against an independent statement of the same accounting fact is neither. The check
keeps that structural rather than promised — the pages land in a scratch L0 the real lake never
sees, and nothing writes to `pit_fundamentals`. Verified after the run: the PIT store's only source
is `nse_xbrl_filing`.

Thirteen companies, largest first, standalone or consolidated matched to Screener's own basis:

| Component | Result |
|---|---|
| Paid-up capital | **12 of 13 exact** (only IOC differs, 2.5% — a bonus/buyback timing difference) |
| Reserves | **10 of 13 exact to 0.0%**; TCS −0.2%, LT +1.4% |
| Reserves, two outliers | VEDL −20.0%, GAIL −11.8% |

Reliance, NTPC, POWERGRID, ITC, HINDALCO, WIPRO and SAIL agree to the rupee on both components.

The two outliers are **not errors, and their direction gives them away**: ours is lower in both
cases, and our element is `ReserveExcludingRevaluationReserves` while Screener reports total
reserves. The gap is the revaluation surplus, which asset-heavy companies (a miner and a pipeline
utility) carry and which the element excludes *by name*. Two valid definitions, not a discrepancy —
and for a P/B or ROE factor the exclusion is the better choice, since a revaluation surplus inflates
book value with no cash movement and at management's discretion.

**So the concept must be named for what it is** — `shareholders_equity_excl_revaluation`, not
`shareholders_equity`. Calling it the latter would invite a reader to reconcile it against a vendor
figure and conclude the data is wrong.

## Action 1 — Extract the balance-sheet concepts. **DONE** (`9f26a83`, `b2e3dbb`).

A parser change, as planned: every document was already in L0 and `_ref_for` reuses stored payloads,
so the whole store was re-derived with **zero network requests**.

Four things were built differently from the plan above, each because measuring the real corpus
contradicted what the plan assumed. All four are recorded here because the plan was wrong, not
because the implementation drifted.

### 1. The concept names were measured per family, not per document sample

The plan said "per taxonomy family (the banking form names its own asset-quality items)". Measured
over 3,000 captured documents, the split is sharper than that and the other direction too:

| | Ind-AS | Banking | Non-Ind-AS |
|---|---|---|---|
| `PaidUpValueOfEquityShareCapital`, `FaceValueOfEquityShareCapital` | 100% | 100% | 100% |
| `ReserveExcludingRevaluationReserves` | 24% | 16% | 41% |
| `DebtEquityRatio` | 26% | **0%** | 47% |
| `GrossNonPerformingAssets`, `NonPerformingAssets`, `PercentageOfGrossNpa`, `PercentageOfNpa`, `ReturnOnAssets`, `CET1Ratio` | 0% | **100%** | 0% |

The capital elements share one name across all three families, so they are one shared block rather
than three copies. No bank states a debt-to-equity ratio, which is correct rather than missing for a
deposit-taker. Every mapped element name was then verified to appear in real documents — none was
read off the schema, which is how the four names this map originally guessed came to match nothing.

### 2. `profit_attributable_to_owners` had to be mapped for the guard to work at all

Not in the plan, and the guard is unsound without it. GRASIM consolidates UltraTech and Aditya Birla
Capital, so its EPS is struck on ₹899 crore of a ₹1,844 crore consolidated profit. Corroborating a
share count against the group bottom line puts the two counts **more than 2x apart on a filing where
nothing is wrong** — which would force the tolerance so wide it could no longer catch a real error.
Using the parent's share instead lifts exact agreement from 83.3% to 86.6% across 10,468 filings.

Ind-AS and banking state it under different names; the pre-Ind-AS form states only
`ProfitLossForPeriodBeforeMinorityInterest`, the wrong side of the deduction, so it is deliberately
left unmapped and those filings fall back to the bottom line (3 consolidated filings in 4,000).

It is also simply the right earnings figure for a P/E whose market cap is the parent's shares.

### 3. A reserves figure of exactly zero is an unfilled tag — a second guard the plan did not foresee

**29.5% of the filings that state `ReserveExcludingRevaluationReserves` state it as 0.00.** The tag
is mandatory and a filer who has not computed it enters nothing. Taken literally, Schaeffler India's
book value becomes its ₹31.26 crore of paid-up capital alone — roughly 100x too low — and a P/B
screen ranks it spectacularly wrong in the direction that most attracts a value strategy. Zero is
now read as absent. A *negative* reserve is kept: accumulated losses are real, and so is the
negative equity they produce.

### 4. The tolerance is 3x, taken from the distribution

The plan said "beyond a tolerance" without naming one. Measured over 10,468 (document, column)
pairs: 86.6% agree within 5%, 95.6% fall inside a 3x band, and **every** case outside it is out by a
clean power of ten. The band therefore has a 3.3x margin below the smallest error it must catch,
while admitting the honest reasons the two counts differ — EPS is struck over weighted-average
shares against a period-end capital figure, and an EPS rounded to two decimals is worth ±10% to a
company earning ₹0.05 a share.

A filing that fails yields **neither** derived fact. The suspect input is the paid-up capital, which
is also a term of the equity sum, and nothing inside the document says whether the fault lies there
or in the face value. Refusing a possibly-good book value is recoverable; publishing one wrong by
six orders of magnitude is not.

### What the guards actually cost, measured through the production parse path

35,548 filings re-parsed exactly as the runner does:

| | |
|---|---|
| state paid-up + face value | **100.0%** |
| `shares_outstanding` published | **95.0%** — the guard refuses **4.95%** |
| state the reserves element | 27.0% |
| `shareholders_equity_excl_revaluation` published | **19.0%** — zero-reserves removes 29.5% |
| state `profit_attributable_to_owners` | 41.0% |
| state `debt_equity_ratio` | 27.5% |

The 4.95% refusal rate matches the 5% this plan predicted from the paid-up scale errors, which is
the check that the guard is catching that population and not something else.

### One defect the rebuild caught that the fixtures could not

`ArrowInvalid: Rescaling Decimal value would cause data loss`, 641 filings into the first rebuild.
Paid-up over face value rarely divides exactly — SPICEMOBI's ₹60.52 crore of ₹3 paid-up equity gives
201,749,666.666… to 19 decimal places — and a repeating decimal has no precision the L1 scale can
hold. A share count is a count of shares and the residual is the filing's own rounding to the
nearest thousand rupees, so it is rounded to a whole share. Audited the other direction as well: no
stated value of any newly-mapped concept exceeds four decimal places across 8,000 documents, so
nothing else will fail that write later in a run.

### The re-derived store, verified

Re-derived from L0 in one pass, **zero network requests**, on the batched write path (a separate
task, merged first: `write_pit` was 96.9% of rebuild time because it rewrote each partition once per
filing — 90 minutes became about 12).

| | before | after |
|---|---|---|
| Filings published | 68,839 | **68,839** — identical set |
| Facts | 636,661 | **911,954** (+43%) |
| Distinct concepts | 9 | **22** |
| Partitions / size | 1,592 / 16 MB | 1,592 / 20 MB |
| ISINs | 1,598 | 1,598 |

Coverage of the new concepts, over all 68,839 filings:

| concept | filings | |
|---|---|---|
| `paid_up_equity_capital`, `face_value_per_share` | 68,837 | **100.0%** |
| `shares_outstanding` | 65,319 | **94.9%** — 3,518 (5.11%) refused by the guard |
| `profit_attributable_to_owners` | 27,829 | 40.4% |
| `debt_equity_ratio` | 18,250 | 26.5% |
| `reserves_excl_revaluation` | 13,129 | 19.1% |
| `shareholders_equity_excl_revaluation` | 9,171 | 13.3% |
| bank asset quality (NPA ×4, ROA, CET1) | 654 | 100% *of bank filings* |

The 5.11% refusal rate lands on the 5% this plan predicted from the paid-up scale errors, and
13.3/19.1 = 69.6% of the filings stating reserves yield a book value — the complement is the 30.4%
that state 0.00, matching the 29.5% measured independently. Both guards are doing what they were
sized to do, at the rate they were sized for.

Verification, all passing:

* every L1 filing is `PUBLISHED` and every published filing is in L1 — no orphans, none missing;
* `filing_date > period_end` on all 911,954 facts (invariant #7); every value a `Decimal`; every row
  carrying its `l0_key`; one source only (invariant #8); `taxonomy` populated on every row;
* all 65,319 share counts recompute exactly from their own paid-up and face value, and all 9,171
  equity facts from paid-up plus reserves, with none resting on a zero reserves figure;
* all 65,319 published share counts sit inside the 3x band against their own filing's EPS — the
  guard held on the real corpus, not just on fixtures;
* **1,500 filings re-parsed from L0 and compared fact by fact: 0 differ.**

All 2,297 failures classify into the three known classes, none new: 1,434 correct refusals (the
entry's period is not a column in that document), 774 documents NSE lists but never served into L0,
89 symbols missing from D2's history.

One difference worth naming rather than rounding away: the rebuild attempted 71,136 units against
the campaign's 70,734. The 402 extra are all entries whose document is absent from L0 — the campaign
ran as eleven yearly segments plus sweeps, this ran as one uniform plan in a single pass, so it
reached entries the segments did not. They contribute no facts either way, which is why the
published set is the same 68,839. Whether those 402 documents are fetchable at all is an
archive-gap question, not a parser one.

### Also shipped, per the plan

`taxonomy` and `derived` on `FundamentalFact` and the L1 schema, in the same migration and the same
re-derivation. A bank's `revenue_from_operations` is `InterestEarned` and its `total_expenses`
excludes provisions, so a screen ranking banks beside manufacturers on one concept key needs that
visible on the row rather than reachable by a join. `derived` separates what the parser computed
from what the filer stated, which is what makes the refusal population auditable.

### Deliberately still not included

`SegmentAssets` and `SegmentLiabilities` (36% of Ind-AS filings, 100% of banking). `SegmentAssets`
carries name and value in the same dimensioned context, so it would fit the existing
`_segment_facts` path; `SegmentLiabilities` splits them across a `…01D`/`…01I` context pair, which
that path cannot express. Adding one without the other gives segment assets with no liabilities to
set against them — half a balance sheet per segment, which supports no ratio. Left as one piece of
work rather than shipped half-done.

`DebtServiceCoverageRatio` and `InterestServiceCoverageRatio` remain excluded as before: ratios a
filer computed under its own conventions, which we cannot reconstruct from inputs and therefore
cannot audit. `DebtEquityRatio` carries that same caveat and is included only because leverage has
no other route out of this dataset.

## Action 2 — Fetch the delivery file. 1.7 hours, and everything else is built.

Verified: `nse_sec_bhavdata_full` is **VERIFIED** in the register, needs no session cookie, has a
per-session dated URL (`sec_bhavdata_full_{DDMMYYYY}.csv`) and an open-ended era, so it is genuinely
backfillable session by session. The parser (`dataplatform/ingest/nse/delivery.py`, M1.6) exists with
frozen fixtures and unit tests; `deliv_qty`/`deliv_pct` are already columns of `prices_raw`; the
ISIN-resolving join and its reconciliation contract already exist in `dataplatform/store/l1.py`.
**The only thing that never happened is the fetch.**

| | |
|---|---|
| Files to fetch | **2,469**, one per session |
| Wall-clock at 2.5 s spacing | **1.7 h** |
| Price side | **no re-fetch** — all 2,473 bhavcopy payloads are already in L0 |
| Autonomy | >200 requests to one host ⇒ **owner GO** under B1 |

The join happens at L1 *write* time, not as a later patch, so backfilling delivery means
re-deriving each session's `prices_raw` partition from its L0 bhavcopy plus the newly-fetched
delivery file. That is the immutable-L0 design working as intended, and it is why the price side
costs nothing.

1. Build a resumable runner mirroring `fundamentals_backfill.py` — `sync_state` checkpoint per
   session, commit-per-unit, `--dry-run`, `--max-sessions`, and a 403 spike that parks with an
   enumerated reason. Reuse `l1.py`'s existing join and its
   `delivery_rows == joined + unresolved + orphaned` reconciliation rather than writing a second one.
2. Fetch and re-derive, oldest session first.
3. Report coverage per session, and the unresolved count, which is where Action 3 shows up.

**Sequence it after the fundamentals campaign**, not alongside: both target
`nsearchives.nseindia.com`, and two concurrent runners would halve the effective per-host spacing
through a side channel — which is the politeness policy being circumvented rather than changed.

What it unlocks: the `DeliverySignal` consumer with its `delivery_spike_multiple` is already built
and has never had data. Delivery percentage separates accumulated from churned volume and is
published in no other market's EOD feed, which makes it the most differentiated signal input the
register offers.

## Action 3 — Extend the identity master backwards. This one is a multiplier.

Neither action above reaches its ceiling without it, and it is the common cause behind three
separate symptoms already observed:

* **883 of 2,946** equities that traded in the window are unknown to `security_master` — and they
  skew toward *delisted* names, the exact population whose absence manufactures survivorship bias.
* The delivery file has **no ISIN** and joins on `(symbol, series, date)`, so every delivery row for
  an unresolvable symbol lands in quarantine rather than in `prices_raw`.
* 21 fundamentals filings are being refused right now because two renamed companies
  (`WABCOINDIA`→`ZFCVINDIA`, `SHRIRAMEPC`→`SEPC`) filed under symbols D2 has never seen.

The master was built from a recent equity-list snapshot, so it knows companies listed *today*. The
register is explicit that the equity list is "a snapshot, not a history", so historical windows
cannot be recovered from that source alone — this needs either an alternative historical listing
source or reconstruction from the bhavcopy series already in L0 (every session names every symbol
that traded, which is a genuine symbol-history signal we already hold). Scoping that is its own
task; recording here that it gates the other two.

## Order, and why

1. **Finish the fundamentals campaign and its sweeps.** ~~In flight.~~ **Done** — 68,839 filings,
   636,661 facts.
2. **Action 1 (balance-sheet concepts + taxonomy).** **Done** (`9f26a83`, `b2e3dbb`). Zero fetching,
   one schema migration, one re-derivation. Market cap now computable for ~95% of filings and book
   value for ~19% (the annual ones that fill the reserves tag), so P/E on a share count we derive
   rather than buy, P/B and annual ROE are all reachable. Bank asset quality — gross and net NPA,
   both as amounts and percentages, plus CET1 and return on assets — arrives at 100% of bank filings
   as a bonus the plan did not count on.
3. **Action 2 (delivery).** 1.7 h of fetching behind an owner GO. Cheap, fully built, and the
   signal is genuinely differentiated.
4. **Action 3 (identity history).** Largest scope, gates the ceiling of everything else, and is the
   one that also removes a survivorship bias rather than just adding a column.

Actions 1 and 2 are independent of each other and could run in either order; 1 is placed first only
because it needs no permission and no network.
