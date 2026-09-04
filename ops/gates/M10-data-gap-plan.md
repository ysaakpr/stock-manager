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

## Action 1 — Extract the balance-sheet concepts. No fetching at all.

This is a parser change. Every document needed is already in L0, and `_ref_for` reuses stored
payloads, so re-deriving the whole store costs **zero requests**.

1. Extend `CONCEPTS` in `dataplatform/ingest/xbrl/models.py` with the elements above, per taxonomy
   family (the banking form names its own asset-quality items).
2. Add the two derived facts, named for exactly what they are: `shares_outstanding` and
   `shareholders_equity_excl_revaluation`. Computed in the parser from stated elements, never
   inferred when an input is missing — a filing lacking either input yields no derived fact rather
   than a guess, matching how the existing whitelist behaves.
3. **Guard `shares_outstanding` with `profit_after_tax / eps_basic`** and refuse the fact when the
   two disagree beyond a tolerance. 5% of filings state a paid-up capital that is wrong by a clean
   power of ten (see the validation above), and without the guard those become market caps wrong by
   the same factor. Surface the refusals as a count rather than dropping them silently.
4. Add `taxonomy` to `FundamentalFact` and the L1 schema in the same change (already filed
   separately in `ops/BACKLOG.md`). It is the same schema migration and the same re-derivation, so
   doing them together halves the work and avoids a second rewrite of every partition.
5. Re-derive `pit_fundamentals` from L0.

**Sequencing is not optional.** `_rows_of` enforces `_L1_SCHEMA` on read and `write_pit` merges
existing partitions, so adding a column while the campaign is writing makes already-written
partitions unreadable — and the running processes hold the old schema anyway. **Do this after the
campaign and its sweeps finish.**

Deliberately *not* included: `DebtServiceCoverageRatio` and `InterestServiceCoverageRatio` are
stated by ~26% of filings but are ratios a filer computed under its own conventions, not primitives.
Storing a number we cannot reconstruct from its inputs makes it un-auditable. `DebtEquityRatio` is
included only because leverage has no other route here; it should carry the same caveat.

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

1. **Finish the fundamentals campaign and its sweeps.** In flight.
2. **Action 1 (balance-sheet concepts + taxonomy).** Zero fetching, one schema migration, one
   re-derivation. Unlocks ROE, P/B, market cap and leverage — the largest capability gain per unit
   of work available anywhere in this plan, and it needs no owner GO.
3. **Action 2 (delivery).** 1.7 h of fetching behind an owner GO. Cheap, fully built, and the
   signal is genuinely differentiated.
4. **Action 3 (identity history).** Largest scope, gates the ceiling of everything else, and is the
   one that also removes a survivorship bias rather than just adding a column.

Actions 1 and 2 are independent of each other and could run in either order; 1 is placed first only
because it needs no permission and no network.
