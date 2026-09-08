# Data Requirement Specification — Multi-Fund Automatic Fund Manager (Indian Equities)

**Status:** design research, read-only. Nothing in the repo was changed.
**Date:** 2026-09-07. **Backtest window assumed:** 2006-09-07 → 2026-09-07 (20y).
**Scope:** what a system that *automatically constructs and continuously manages many theme-defined
funds* cannot be built or honestly backtested without. This is a requirement, not an inventory of
what the repo currently holds.

---

## 0. Method, conventions and the four guarantees

### 0.1 Notation used in every entity entry

- **Grain / PK** — what exactly one row means. If you cannot state the grain in one line, the entity
  is two entities.
- **PIT** column on each attribute: `PIT` means the value is *revisable* and a backtest must read the
  version known at the decision instant, not the latest one. `STATIC` means it is a fact that, once
  true, never changes (an ex-date that has passed; a fill that happened).
- Types are the repo's types: `Decimal` for all money/quantity/ratio, `date` for trading dates,
  tz-aware `datetime` (Asia/Kolkata) for instants, `enum` for closed vocabularies, `str` for free text.
- **ISIN is the only join key.** Symbols, scrip codes, tickers and names are *attributes with
  effective dates*, never keys. Every entity below that is quoted per-instrument keys on `isin`.

### 0.2 The bitemporal envelope — every fetched entity carries it

This is not optional decoration; it is the mechanism that makes a 20-year backtest honest. Every
externally-sourced row carries:

| field | type | meaning |
|---|---|---|
| `knowledge_ts` | tz-aware datetime | the earliest instant the platform *could* have known this fact — the source's own publication timestamp, never our ingest time |
| `effective_from` | date | first business date the fact is true of |
| `effective_to` | date, nullable | last business date it is true of (`NULL` = open) |
| `revision_no` | int | 0 = as first published; 1..n = restatements/corrections |
| `superseded_by` | uuid, nullable | the revision that replaced this row |
| `source_id` | enum | which source produced it |
| `l0_ref` | str | pointer into the immutable L0 lake: the exact file + byte range it was parsed from |
| `ingested_at` | tz-aware datetime | our clock, for ops only — **never** readable by a strategy |

**The single query rule that makes or breaks the whole system:** every read a strategy or backtest
performs is `WHERE knowledge_ts <= :decision_cutoff` and picks `max(revision_no)` within that filter.
If any table cannot answer that query, that table cannot be used in a backtest. Full stop.

`knowledge_ts` distinctions that matter and are routinely botched:

- A quarterly result for period ending 2015-06-30 has `effective_from = 2015-06-30` and
  `knowledge_ts = 2015-08-13 18:42 IST` (when it hit the exchange). A backtest on 2015-07-15 must
  see nothing.
- A bhavcopy for trade date D is published ~18:30 IST on D. A decision made at 15:20 on D may not
  read it. Either your decision cutoff is post-close (and you trade at D+1 open) or you read only
  D-1. Pick one and encode it; do not leave it implicit.
- A shareholding pattern for quarter ending 2019-09-30 is filed up to 21 days later. `knowledge_ts`
  is the filing timestamp, not the quarter end.
- Delisting/suspension: the *announcement* date is knowable; the effective date is in the future at
  announcement. Two different timestamps, both needed.

### 0.3 The four guarantees the data must underwrite

Everything in this catalog exists to defend one of these:

1. **G1 — No survivorship.** The universe on any past date contains every security that was
   *actually* tradable then, including those now dead. Requires: delisted/merged/suspended masters,
   their full price history, and their terminal event (final price, cash consideration, exchange ratio).
2. **G2 — No look-ahead.** No fact enters a decision before its `knowledge_ts`. The hardest cases are
   not prices; they are **classifications, index memberships, restated fundamentals and business
   descriptions**, all of which the vendor silently backfills with today's truth.
3. **G3 — No phantom liquidity.** Every simulated fill must be defensible against what actually
   traded that day, at what price, in what band, in what series, with what settlement lag — and
   against what *the other funds in the same house* were doing in the same name.
4. **G4 — Every decision is reproducible and journaled, including the no-ops.** Given the same
   `decision_cutoff` and the same L0 lake, the journal must be byte-identical. This requires the
   inputs to a decision to be *addressable* (hashable), not merely present.

### 0.4 The one structural warning about this specific product

A **thematic** fund manager is uniquely exposed to G2 through a channel that a factor or momentum
system never touches: **the theme label itself is a forward-looking artefact.** A company that
everyone calls "defence manufacturing" in 2026 was filed under "Industrial Products — Castings &
Forgings" in 2012, its annual report said nothing about defence, and no index carried it. If your
theme rule resolves against *today's* classification, description or index membership, your 2012
backtest is not merely optimistic — it is a list of companies selected because they later succeeded
at the theme. That is the single largest lie this system can tell, and it is why family D
(Classification & Thematics) carries more MUST-HAVE weight here than in any generic backtester.

---

# FAMILY A — UNIVERSE & IDENTITY

## A1. `security_master` — the ISIN-level instrument record

**Definition.** One row per ISIN per *validity interval of its descriptive attributes*. A row means:
"between `effective_from` and `effective_to`, the security identified by this ISIN was an instrument of
this type, issued by this issuer, with this face value, in this currency, and was known by this name."
It is the spine every other entity hangs off. It is **not** one row per ISIN — an ISIN whose face
value splits or whose name changes gets a new row, because a backtest on the old date must see the
old name and the old face value.

**Grain / PK.** `(isin, effective_from)`. Unique index on `(isin, effective_from)`, exclusion
constraint so intervals for one ISIN never overlap.

**Attributes.**

| field | type | units | null | PIT | notes |
|---|---|---|---|---|---|
| `isin` | str(12) | — | no | STATIC | ISO 6166, `INE`/`INF`/`IN9` prefixes. `IN9` = partly-paid/rights entitlement — different instrument, do not merge |
| `effective_from` / `effective_to` | date | — | no / yes | — | validity interval of this row |
| `issuer_id` | uuid | — | no | PIT | FK to `issuer` (A2). Survives ISIN changes |
| `instrument_type` | enum | — | no | PIT | `EQUITY_ORDINARY`, `EQUITY_DVR`, `PREFERENCE`, `PARTLY_PAID`, `RIGHTS_ENTITLEMENT`, `WARRANT`, `ETF`, `INVIT`, `REIT`, `SGB`, `NCD`, `MF_CLOSED_END` |
| `security_name` | str | — | no | PIT | as printed on the exchange master that day |
| `face_value` | Decimal | INR | no | PIT | changes on face-value split; needed to interpret dividend "% of face value" |
| `paid_up_value` | Decimal | INR | no | PIT | differs from face value on partly-paid |
| `currency` | enum | — | no | STATIC | `INR` |
| `country_of_incorporation` | str(2) | — | no | PIT | non-IN issuers exist on NSE/BSE |
| `date_of_listing` | date | — | yes | STATIC | first trading date on the primary exchange |
| `date_of_delisting` | date | — | yes | STATIC | NULL while live |
| `isin_status` | enum | — | no | PIT | `ACTIVE`, `SUSPENDED`, `DELISTED`, `MERGED_AWAY`, `REDEEMED` |
| `predecessor_isin` / `successor_isin` | str(12) | — | yes | STATIC | the ISIN-change chain (A4) |
| `is_shariah_compliant`, `is_dvr`, `has_differential_rights` | bool | — | no | PIT | mandate filters |

**Frequency / volume.** Rebuilt from a daily exchange master; new *rows* only on change.
Over 20y: NSE has carried roughly 2,000–2,300 tradable EQ-series names at any instant but the union
over the window — counting delistings, migrations to/from BE, SME and merged-away entities — is on
the order of **4,500–6,000 ISINs on NSE and 9,000–13,000 across NSE+BSE**. With ~2–4 attribute
revisions each, **~25k–50k rows**. Trivial storage; the difficulty is entirely in reconstruction.

**Acquisition — FETCHED.**
- Primary, current: NSE `EQUITY_L.csv` (`https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv`)
  — symbol, name, series, listing date, paid-up, face value, ISIN. **Snapshot only, no history.**
  If you have not been snapshotting it daily since 2006, you cannot reconstruct the past from it.
- BSE: `https://www.bseindia.com/corporates/List_Scrips.aspx` (scrip code, ISIN, group, face value,
  status incl. Active/Suspended/Delisted). BSE's list *does* retain delisted/suspended rows, which
  makes it the better survivorship anchor of the two. Also `https://www.bseindia.com/static/markets/equity/EQReports/downloads.aspx`.
- Reconstruction path when no history was snapshotted: **derive the master from the bhavcopy union.**
  Every ISIN that ever appears in a daily bhavcopy between 2006 and today was tradable on that day.
  This is the single most survivorship-honest construction available for free, and it is the one to
  build. BSE bhavcopy carries `ISIN_CODE` per row; NSE's `cm<DDMMMYYYY>bhav.csv` historically did
  **not** carry ISIN (symbol + series only) — NSE ISIN arrives only via `sec_bhavdata_full` and the
  master. That asymmetry is the core identity problem of this project (see A3).
- Depository of record: NSDL ISIN master (`https://www.nsdl.co.in/master_search.php`) and CDSL —
  authoritative for ISIN↔issuer↔security-description, weak on dates.
- Paid alternatives with real history: CMIE Prowess/ProwessIQ (`cmie.com`, time series from 1989,
  ~50k companies incl. unlisted — the strongest Indian survivorship-safe universe available),
  Capitaline (`capitaline.com`), ACE Equity (Accord Fintech), LSEG/Refinitiv Datastream, Bloomberg.
  Academic licences for Prowess are common; commercial licences are negotiated and expensive.

**Format eras you will hit.** (i) NSE moved from `www1.nseindia.com/content/historical/...` to
`archives.nseindia.com` to `nsearchives.nseindia.com`; (ii) NSE discontinued the legacy CM bhavcopy
CSV on **2024-07-08** in favour of **UDiFF** (`BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip`), a
completely different column set with ISIN present — so post-2024-07 and pre-2024-07 are two parsers;
(iii) NSE and BSE both require a browser-like `User-Agent` and cookie priming or return 403;
(iv) BSE `EQ_ISINCODE_<DDMMYY>.zip` naming and column set changed around 2007 and again around 2015.

**Why the algorithm needs it.** Every fund's universe rule filters on instrument type, series
eligibility and listing age. Without effective-dated identity you cannot even ask "what was
investable on 2011-03-14".

**Failure mode if wrong.** Survivorship bias in its purest form — the backtest universe is "companies
that exist today", which excludes every fraud, every bankruptcy, every reverse-merger. In Indian
small caps over 2006–2026 this is not a rounding error: the compulsory-delisting and
suspension count over the window runs into the **many hundreds**, concentrated exactly in the
small/micro-cap band that theme funds fish in.

---

## A2. `issuer` — the legal entity, above ISIN

**Definition.** One row per issuing company per validity interval. Exists because an ISIN can change
(A4) while the business does not, and because two ISINs (ordinary + DVR, or pre/post-merger) can be
the same economic exposure. The theme engine reasons about *businesses*; the trading engine reasons
about ISINs; this table is the bridge.

**Grain / PK.** `(issuer_id, effective_from)`; natural key candidates `cin` (MCA Corporate Identity
Number), `pan`, `lei`.

**Attributes.** `issuer_id` (uuid), `cin` (str(21), MCA, PIT — changes on state/listing-status change,
and its own 5-char prefix is an NIC industry code), `pan` (str(10), STATIC), `lei` (str(20), nullable),
`legal_name` (PIT), `former_names[]` with dates, `date_of_incorporation` (date), `registered_state`
(enum — needed for pre-2020 stamp duty, §I), `registrar_and_transfer_agent` (str), `group_name`
(str, PIT — promoter group: "Tata", "Adani"; needed for group-exposure rails), `is_psu` (bool, PIT —
changes on divestment; a real theme dimension), `auditor_name` (PIT), `industry_nic_code` (PIT).

**Volume.** ~10k–15k issuers over 20y, ~40k rows with revisions.

**Acquisition — FETCHED / DERIVED.** CIN and incorporation from **MCA21**
(`https://www.mca.gov.in/mcafoportal/viewCompanyMasterData.do`, free lookup, no bulk API; bulk data
sold via MCA's Company Master Data downloads). Group affiliation is *not published by anyone
authoritatively* — derive from promoter names in the shareholding pattern (F1) plus manual curation;
Prowess ships a curated `group` field and is the pragmatic source. PSU status derivable from
promoter category = `Central Government / State Government` in SHP.

**Why needed.** Group-exposure rails ("no more than 15% in one promoter group") and theme rules that
are about a *business* ("Adani infrastructure complex") rather than a listed line. Also the only way
to carry a thesis across an ISIN change.

**Failure mode.** Rails silently under-count: two ISINs of the same group counted as independent
positions; concentration risk understated; a backtest that looks diversified and was not.

---

## A3. `identifier_map` — ISIN ↔ symbol ↔ scrip code ↔ vendor id, effective-dated

**Definition.** One row per (identifier namespace, identifier value, ISIN, validity interval). A row
means "in namespace *NSE_SYMBOL*, the string `MINDTREE` referred to ISIN INE018I01017 from
2007-02-12 to 2022-11-14". Symbols are **recycled** and **reassigned**; this is not a lookup table,
it is a temporal relation.

**Grain / PK.** `(namespace, identifier, effective_from)`, with a second unique index on
`(isin, namespace, effective_from)`.

**Attributes.** `namespace` (enum: `NSE_SYMBOL`, `BSE_SCRIP_CODE`, `BSE_TICKER`, `NSE_TOKEN`,
`KITE_INSTRUMENT_TOKEN`, `BLOOMBERG_TICKER`, `RIC`, `PROWESS_CO_CODE`, `CAPITALINE_CODE`,
`SCREENER_SLUG`), `identifier` (str), `isin` (str(12)), `exchange` (enum `NSE`/`BSE`/`MSEI`),
`series` (enum, nullable — see A5), `effective_from`/`effective_to` (date), `change_reason` (enum:
`LISTING`, `NAME_CHANGE`, `MERGER`, `SERIES_CHANGE`, `ISIN_CHANGE`, `RECYCLED_TO_NEW_ISSUER`),
`source_id`, `knowledge_ts`.

**Volume.** ~10 namespaces × 10k securities × ~2 intervals ≈ **150k–250k rows**.

**Acquisition — DERIVED + FETCHED.**
Derivation (the reliable one): join **BSE bhavcopy** (has both `SC_CODE` and `ISIN_CODE` on every
row, every day) to **NSE bhavcopy** (has `SYMBOL`+`SERIES`, and ISIN only post-UDiFF/`sec_bhavdata_full`)
on the days where both are available, keyed by ISIN via BSE, matching NSE symbols by name/close-price
correlation for the pre-ISIN era. Then close the intervals at the days where the mapping flips.
Direct sources: NSE `EQUITY_L.csv`, NSE symbol-change file
`https://nsearchives.nseindia.com/content/equities/symbolchange.csv` (**the key artefact** — old
symbol, new symbol, date of change; historically deep), BSE `List_Scrips.aspx` export, Zerodha Kite
`https://api.kite.trade/instruments` (daily instrument dump, `instrument_token`↔`tradingsymbol`↔ISIN,
snapshot only — you must archive it daily or lose it).

**Format quirks.** NSE symbol changes are not always accompanied by an ISIN change and vice versa.
BSE scrip codes are stable across name changes (a virtue) but are reused after very long gaps.
Kite `instrument_token` is stable per exchange-symbol, **not** per ISIN — it follows the symbol
through a name change, which is exactly wrong for our purposes.

**Why needed.** Every external source keys on something different. Without effective-dated mapping,
joining a 2013 corporate action (announced under the old symbol) to a 2013 price row (published under
the new symbol) silently drops the action.

**Failure mode.** Two distinct lies. (a) *Dropped joins* → a split goes unadjusted → a 1:10 split
looks like a -90% day → momentum/drawdown metrics are garbage. (b) *Wrong joins* via a recycled
symbol → you splice two unrelated companies' price histories into one series and backtest a
chimera. Both are silent.

---

## A4. `isin_change_event`

**Definition.** One row per ISIN succession: old ISIN, new ISIN, reason, effective date. Distinct
from a corporate action, though usually caused by one (face-value change, scheme of arrangement,
consolidation of DVR into ordinary, re-domiciliation).

**Grain / PK.** `(old_isin, new_isin, effective_date)`.

**Attributes.** `old_isin`, `new_isin`, `effective_date` (date), `announcement_date` (date),
`reason` (enum: `FACE_VALUE_CHANGE`, `SPLIT`, `CONSOLIDATION`, `MERGER`, `DEMERGER`, `NAME_CHANGE`,
`RESTRUCTURING`, `PARTLY_PAID_TO_FULLY_PAID`), `ratio_old_to_new` (Decimal, nullable),
`source_id`, `knowledge_ts`, `l0_ref`.

**Volume.** ~150–400/yr → **3k–8k rows** over 20y.

**Acquisition — FETCHED.** NSE corporate-action feed carries "Change in ISIN" as a purpose string;
BSE notices; NSDL ISIN master diffs (if you snapshot it). The honest fallback is a *derived* one:
diff consecutive daily identifier snapshots and raise every unexplained ISIN appearance/disappearance
as a **quarantined event needing resolution**, rather than letting it pass.

**Why needed / failure mode.** Chains the position, tax lot and price history across the change. If
missed, the backtest sees a delisting (position force-liquidated at the last price, a fake realised
gain/loss and a fake tax event) followed by a new listing (fresh, "unrelated" company). This is
one of the top-3 sources of spurious P&L in naive Indian backtests.

---

## A5. `listing_status_event` — listing, suspension, revocation, delisting

**Definition.** One row per state transition of a security's tradability on one exchange. A row means
"on exchange X, ISIN Y entered state S with effect from date D, announced on date A, for reason R".

**Grain / PK.** `(isin, exchange, effective_date, new_status)`.

**Attributes.** `isin`, `exchange` (enum), `announcement_date` (date, PIT-critical), `effective_date`
(date), `new_status` (enum: `LISTED`, `SUSPENDED_FOR_PENALAL_REASONS`, `SUSPENDED_CORPORATE_ACTION`,
`REVOKED_FROM_SUSPENSION`, `DELISTED_VOLUNTARY`, `DELISTED_COMPULSORY`, `MERGED_AWAY`,
`MOVED_TO_DEALER_MARKET`), `reason_text` (str), `exit_price` (Decimal, nullable — the delisting exit
offer price, **essential**), `exit_offer_open`/`exit_offer_close` (date, nullable), `acquirer_name`
(str, nullable), `is_terminal` (bool), `circular_ref` (str), `knowledge_ts`, `l0_ref`.

**Volume.** NSE+BSE compulsory delistings alone ran to hundreds in single years (BSE delisted ~194
companies in one 2016 action). Over 20y, expect **3,000–6,000 transitions** across both exchanges,
including the very large BSE suspended-company backlog.

**Acquisition — FETCHED, hard.**
- NSE: `https://www.nseindia.com/companies-listing/corporate-filings-delisting`, circulars under
  `https://nsearchives.nseindia.com/content/circulars/`, and the historical-reports hub
  `https://www.nseindia.com/static/resources/historical-reports-capital-market-daily-monthly-archives`.
- BSE: notices + `List_Scrips.aspx` status column + the "Delisted Securities" and "Suspended
  Securities" listings under `https://www.bseindia.com/corporates/Delisted_Company.aspx`.
- SEBI delisting orders and the "SOP for Delisting of Equity Shares"
  (`https://nsearchives.nseindia.com/web/sites/default/files/inline-files/SOP_for_Delisting_of_Equity_Shares_0.pdf`).
- **Derived backstop, and you should build it regardless:** an ISIN that stops appearing in the
  bhavcopy for N consecutive trading days while the exchange is open has, de facto, stopped trading.
  Emit a `DERIVED_TRADING_HALT` event with the last traded date. This never misses; it just cannot
  tell you *why* or what you got paid.

**Real 20-year-depth doubt.** Exchange delisting circulars older than ~2012 are patchy on the public
sites and the exit-offer price is very often *not* in any machine-readable form. **Flagged: exit
consideration for pre-2015 delistings is the single least-available MUST-HAVE field in this catalog.**
Plan for a manual/LLM-assisted extraction pass over circular PDFs, and for an explicit
`exit_price_source = ASSUMED_ZERO | ASSUMED_LAST_PRICE | EXTRACTED | VENDOR` flag so the backtest can
report how much of its return came from assumptions.

**Why needed.** Determines both universe membership and the terminal cash flow of a position you were
holding when the music stopped.

**Failure mode.** *Delisting-return bias.* The two naive defaults are both lies in opposite
directions: force-exit at last traded price (which is usually the last of many lower-circuit days, so
you are assuming a fill nobody got) overstates recovery; drop the security from the panel entirely
(the "quiet delete") deletes the loss and is pure survivorship bias. The correct model needs
`exit_price` and `effective_date` and, absent them, a **conservative documented assumption** with the
flag above so the backtest's dependence on it is measurable.

---

## A6. `series_membership` — EQ / BE / BZ / SM / ST and the trade-to-trade regime

**Definition.** One row per (ISIN, exchange, series, validity interval). The series determines whether
intraday trading is allowed, whether 100% delivery is compulsory, what the price band is, and often
whether institutional participation is permitted at all. Series changes are **monthly** and are a
first-class tradability constraint.

**Grain / PK.** `(isin, exchange, effective_from)`.

**Attributes.** `series` (enum: NSE `EQ`, `BE` (trade-to-trade), `BZ` (T2T + surveillance), `BL`
(block), `IL`, `SM`/`ST` (SME/SME-ITP), `RE` (rights entitlement), `W` (warrants); BSE groups `A`,
`B`, `T`, `M`, `MT`, `X`, `XT`, `Z`, `ZP`, `ZY`), `is_trade_to_trade` (bool), `is_intraday_allowed`
(bool), `board_lot` (int), `effective_from`/`effective_to`, `change_circular_ref`, `knowledge_ts`.

**Volume.** ~2,000 securities × several changes over 20y → **20k–60k rows**.

**Acquisition — DERIVED (best) + FETCHED.** *Derived and authoritative:* the `SERIES` column is in
every NSE bhavcopy row for every day since 1994 — so series membership over 20 years is
**free, complete and already in L0** if you hold the bhavcopies. Just run-length-encode it. BSE's
group is likewise in its bhavcopy. Fetched supplement: NSE monthly circulars on shifting securities
to/from trade-to-trade, which give the *announcement* date (the PIT-honest one).

**Why needed.** A T2T security cannot be intraday-traded, its band is usually 5% or 2%, and a fund
rebalancing into it needs many more days. A backtest that assumes EQ-series liquidity for a name
sitting in BE for eighteen months has invented tradability.

**Failure mode.** Phantom liquidity and impossible rebalances, concentrated in exactly the small-cap
theme names. Also under-modelled cost: T2T names have far wider effective spreads.

---

## A7. `board_lot_and_tick` — lot size and tick size regimes

**Definition.** One row per (segment, instrument-or-price-band, validity interval) giving the minimum
tradable quantity and the minimum price increment.

**Grain / PK.** `(exchange, segment, applicability_key, effective_from)`.

**Attributes.** `exchange`, `segment` (enum `CASH`, `SME`, `FNO`), `applicability_key` (str — `ALL`,
or `PRICE_LT_250`, or an ISIN for SME), `lot_size` (int), `tick_size` (Decimal, INR),
`effective_from`, `circular_ref`.

**Known regime facts to encode.** Cash-market equity lot = 1 share throughout the window. Tick size
was ₹0.05 across the cash market for most of the period; **SEBI/exchanges moved to a ₹0.01 tick for
securities priced below ₹250 with effect from 2024-06-10**, and SME lot sizes are instrument-specific
and large (₹1L+ notional per lot by design). F&O lot sizes change on every contract-size revision.

**Volume.** Tens of rows for cash; thousands if SME is in scope.

**Acquisition — FETCHED** from NSE/BSE circulars; SME lot sizes from the SME issue documents and the
instrument master.

**Why needed / failure mode.** Rounding. A ₹0.05 tick on a ₹12 stock is 42bps — larger than your
whole brokerage budget. Simulating fills at three decimal places manufactures free money, and doing
it 5,000 times over 20 years manufactures a lot of it. Lot size on SME makes small allocations
literally impossible; ignoring it produces target weights no fund could hold.

---

## A8. `exchange_venue` and primary-venue selection

**Definition.** One row per (ISIN, date-range) naming which exchange is the *primary* venue for price
and liquidity purposes, plus which venues it is listed on at all.

**Grain / PK.** `(isin, effective_from)`.

**Attributes.** `listed_exchanges` (enum[]), `primary_exchange` (enum), `selection_rule` (enum:
`HIGHER_ADTV_TRAILING_60D`, `NSE_PREFERRED`, `ONLY_VENUE`), `nse_share_of_volume` (Decimal 0–1, PIT).

**Acquisition — DERIVED** from A1 + B1: primary = the venue with higher trailing-60-day traded value,
recomputed monthly and **lagged** so it is knowable at decision time.

**Why needed.** ~40% of BSE-listed names never trade meaningfully on NSE and vice versa; small-cap
theme universes are exactly where this bites. A single-venue price panel silently drops or misprices
them.

**Failure mode.** Universe truncation (a whole class of BSE-only microcaps invisible) and, worse,
ADTV computed on one venue while the fill is assumed on the other — phantom liquidity again.

---

# FAMILY B — PRICES & LIQUIDITY

## B1. `eod_quote_raw` — the unadjusted daily bar (L1 spine)

**Definition.** One row per security per exchange per trading date: the exchange's own end-of-day
record of that security's trading, **exactly as published, never adjusted**. This is the immutable
foundation; every adjusted series in the system is a *view* computed from this plus C-family factors,
and no adjusted number is ever stored as if it were an observation.

**Grain / PK.** `(isin, exchange, trade_date)`, with `series` as an attribute (a security in EQ and
BE on different days is still one row per day).

**Attributes.**

| field | type | units | null | PIT | notes |
|---|---|---|---|---|---|
| `isin`, `exchange`, `trade_date` | — | — | no | STATIC | PK |
| `symbol_at_date`, `series_at_date` | str/enum | — | no | STATIC | as printed; denormalised deliberately for L0 fidelity |
| `open`, `high`, `low`, `close` | Decimal(18,4) | INR | yes | STATIC | `open` is NULL/0 on no-trade days in some eras — do **not** coerce to close |
| `last_price` | Decimal | INR | yes | STATIC | NSE `LAST` — distinct from `close` (close is a weighted avg of last 30 min) |
| `prev_close` | Decimal | INR | yes | STATIC | as printed — **the adjusted-vs-unadjusted trap: NSE's `PREVCLOSE` is corporate-action adjusted on ex-dates**. Never derive returns from it |
| `volume_shares` | Decimal | shares | no | STATIC | `TOTTRDQTY` |
| `traded_value` | Decimal | INR | no | STATIC | `TOTTRDVAL` — in **lakh** in some BSE eras, rupees in others. Unit era flag required |
| `num_trades` | int | count | yes | STATIC | `TOTALTRADES`; absent pre-~2011 |
| `vwap` | Decimal | INR | yes | STATIC | present in `sec_bhavdata_full` era only; else derive `traded_value / volume_shares` |
| `deliverable_qty` | Decimal | shares | yes | STATIC | see B3 |
| `delivery_pct` | Decimal | 0–100 | yes | STATIC | see B3 |
| `series_code_raw`, `file_era` | str/enum | — | no | STATIC | which parser produced this |
| `l0_ref` | str | — | no | — | provenance |

`knowledge_ts` for this entity = **publication instant of the bhavcopy**, ~18:00–19:00 IST on
`trade_date` (later in the 2006–2010 era). Encode it as a per-era constant, not as `trade_date`.

**Frequency / volume.** ~250 trading days/yr × ~2,000 NSE EQ names = 500k rows/yr; over 20y with all
series and delisted names, **~10–12M rows for NSE**, **~25–40M rows for NSE+BSE all-series**. At ~120
bytes/row this is 3–5 GB — comfortably Postgres-sized with monthly partitioning on `trade_date` and
a BRIN index.

**Acquisition — FETCHED (bulk archive, free).**
- NSE legacy CM bhavcopy:
  `https://nsearchives.nseindia.com/content/historical/EQUITIES/<YYYY>/<MON>/cm<DDMMMYYYY>bhav.csv.zip`
  — deep history (commonly cited back to 1994), **discontinued 2024-07-08**.
- NSE UDiFF (current):
  `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip`
  — different columns, ISIN present, mandatory from 2024-07-08.
- NSE `sec_bhavdata_full_<DDMMYYYY>.csv` — the richer one: adds `DELIV_QTY`, `DELIV_PER`, `AVG_PRICE`,
  `TURNOVER_LACS`. **Depth is materially shallower than the legacy bhavcopy** (see B3).
- BSE: `https://www.bseindia.com/download/BhavCopy/Equity/EQ<DDMMYY>_CSV.ZIP` and the ISIN variant
  `EQ_ISINCODE_<DDMMYY>.ZIP`; hub at
  `https://www.bseindia.com/static/markets/equity/EQReports/downloads.aspx`. Publicly reported depth
  is solid from **~2007**; earlier is doubtful. BSE also publishes a modern UDiFF-style file.
- Index-of-record cross-check: `https://www.niftyindices.com/reports/historical-data`.
- Vendor alternates: Zerodha **Kite Connect Historical API** (`https://kite.trade/docs/connect/v3/historical/`)
  — **day candles from ~2000, minute candles from ~2015**, ₹2,000/month per app, hard rate limit
  (3 req/s, no bulk endpoint, max 2,000 candles or 60 days of 1-min per request), and
  **instrument-token-keyed, so it is symbol-continuous, not ISIN-continuous** — you must map it
  through A3 yourself. LSEG/Refinitiv claims NSE history back to 1994 under a paid licence.

**Format eras (this is the real work).** At minimum: (1) 2006–2011 legacy NSE CSV, no `TOTALTRADES`;
(2) 2011–2020 legacy + separate MTO delivery file; (3) 2020–2024-07 legacy + `sec_bhavdata_full`;
(4) 2024-07→ UDiFF only; (5) BSE pre-2015 and post-2015 column sets; (6) sporadic days where the file
is missing, truncated, or double-published (revised bhavcopies exist). Frozen fixture per era in
`tests/fixtures/<source>/<era>/`, per repo convention.

**Licensing.** NSE and BSE publish these files free for download; both assert copyright over the data
and prohibit **redistribution**. Internal research and backtesting is the accepted use; republishing
the lake is not. Since this repo is public, L0 must stay gitignored — which it already is.

**Why needed.** Everything. Signals, risk, fills, NAV, capacity.

**Failure mode if wrong.** Using `PREVCLOSE` or a vendor's pre-adjusted close as the observation is
the classic silent catastrophe: on ex-dates the return is computed against an already-adjusted base
and you double-adjust, producing a systematic drift that looks like alpha. Missing days quietly
change trailing-window statistics. Unit confusion (lakh vs rupee `TOTTRDVAL`) makes ADTV wrong by 10⁵
and destroys every capacity calculation.

---

## B2. `intraday_bar` (optional depth) — minute bars for execution modelling

**Definition.** One row per (isin, exchange, minute-open timestamp). Only needed if the fund manager
models intra-day execution (VWAP slicing, open/close auction participation) rather than assuming a
single daily fill price.

**Grain / PK.** `(isin, exchange, bar_start_ts)` with `bar_start_ts` tz-aware IST.

**Attributes.** `open`/`high`/`low`/`close` (Decimal INR), `volume` (Decimal shares), `vwap`
(Decimal, derived), `is_auction_window` (bool: pre-open 09:00–09:15, closing 15:30–15:40).

**Volume.** 375 bars/day × 250 days × 2,000 names = **~190M rows/yr**. Ten years is ~2 billion rows.
This is a different storage problem entirely (columnar/parquet, not Postgres) and it only exists from
**2015** anyway via Kite.

**Acquisition — FETCHED.** Kite Connect historical (minute from ~2015, per-instrument, rate-limited —
a full-universe 10-year minute backfill is a multi-week campaign against a 3 req/s limit, which is
exactly the kind of thing the repo's single-request-budget rule governs). No free deep source exists.
Paid: TrueData, GDFL (Global Datafeeds), AlgoTest/Dhan historical archives.

**Verdict: IMPROVES-FIDELITY, not MUST-HAVE, and impossible for 2006–2015 anyway.** Model daily fills
with an impact model (B7) instead, and be explicit that pre-2015 execution is a model, not a replay.

---

## B3. `delivery_position` — deliverable quantity and percentage

**Definition.** One row per (isin, exchange, trade_date) giving the quantity of the day's traded volume
that actually settled in the depository, i.e. non-intraday volume. In the Indian market this is the
single best free proxy for *real* investable liquidity, because headline volume in small caps is
dominated by intraday churn and operator activity.

**Grain / PK.** `(isin, exchange, trade_date)`.

**Attributes.** `deliverable_qty` (Decimal shares), `delivery_pct` (Decimal 0–100),
`deliverable_value` (Decimal INR, derived = `deliverable_qty × vwap`), `is_estimated` (bool).

**Volume.** Same as B1, ~10M+ rows; usually merged into B1 as columns rather than a separate table
(recommended: merge, keep `delivery_source_era` so nullability is explainable).

**Acquisition — FETCHED, with a genuine depth problem.**
- NSE MTO file: `https://nsearchives.nseindia.com/archives/equities/mto/MTO_<DDMMYYYY>.DAT` —
  a fixed-width/CSV hybrid with a multi-line header, security-wise deliverable quantity.
- NSE `sec_bhavdata_full_<DDMMYYYY>.csv` under `.../archives/equities/bhavcopy/pr/` — has
  `DELIV_QTY`/`DELIV_PER` as first-class columns.
- BSE: the daily "Delivery Position" file under the EQ Reports downloads page.

**⚠ 20-year depth: DOUBTFUL and must be probed, not assumed.** Public reporting on when NSE's MTO
archive begins is inconsistent, and `sec_bhavdata_full` is clearly a later-era artefact (broadly
2020→). Plan on: reliable delivery data from roughly **2011 onward on NSE**, likely nothing usable for
2006–2010. **Recommended action: run a dated probe sweep over the archive (one HEAD per month back to
2006) and record the true first-available date as a fact in the source registry, rather than trusting
any blog.** Design the liquidity filter so it degrades explicitly: pre-availability, fall back to
traded value with a documented `liquidity_proxy_mode` flag, and report what fraction of any backtest
ran in degraded mode.

**Why needed.** Capacity. A ₹50cr theme fund taking a 2% position needs ₹1cr of a name; if that name
turns over ₹4cr/day of which ₹0.6cr is delivery, you are 17% of real daily liquidity and the fill is
fiction.

**Failure mode.** Phantom liquidity in its most expensive form — the small-cap thematic sleeve looks
tradable on headline volume and is not. Every thematic backtest that ignores delivery over-states
smallcap capacity by roughly 2–4×.

---

## B4. `price_band` — circuit limits and the hit-limit flag

**Definition.** One row per (isin, exchange, trade_date) giving the operative daily price band and
whether trading was locked at it. Without this the backtest can buy a stock that was frozen
limit-up with zero sellers.

**Grain / PK.** `(isin, exchange, trade_date)`.

**Attributes.** `band_pct` (Decimal: 2/5/10/20, or NULL for no-band F&O names), `band_type` (enum:
`FIXED`, `DYNAMIC_FLEX`, `NO_BAND`, `T2T_BAND`), `lower_limit`, `upper_limit` (Decimal INR),
`hit_upper` (bool, derived `high >= upper_limit - tick/2`), `hit_lower` (bool),
`locked_all_day` (bool, derived `open == high == low == close == limit` and volume tiny),
`is_in_asm`, `is_in_gsm` (bool, from G5).

**Volume.** ~10M rows (same grain as B1); or store only the band-defining rows plus derive the flags.

**Acquisition — FETCHED + DERIVED.**
- NSE daily price-band file: `https://nsearchives.nseindia.com/content/equities/sec_list.csv` (current)
  and the daily "Price Band" report in the all-reports hub (`https://www.nseindia.com/all-reports`).
  Historical archive of the band file is **thin**.
- **DERIVED backstop (build this — it is robust and free):** for each day compute
  `r = close/prev_close_unadjusted - 1`; the band is inferable from the empirical clustering of `|r|`
  at 0.02/0.05/0.10/0.20 across the security's history, and `locked` is inferable from
  `high == low == close` with a large order imbalance and small volume. This recovers ~95% of the
  signal from data you already have in L0.
- Index-level circuit breakers: SEBI/NSE circulars — 10/15/20% index-wide breakers since 2001-07-02,
  with the trigger-reference and halt-duration methodology **revised with effect from 2013-10-01**.
  One famous instance in the window: the 2020-03-13 lower-circuit halt.

**Why needed.** Entry and exit feasibility. Also, thematic momentum strategies systematically select
into limit-up names — the exact place where an unmodelled band manufactures returns.

**Failure mode.** The most flattering lie in Indian backtesting: buying at the upper circuit on the
announcement day and selling into strength. Real fills did not exist. Equally, the strategy never
takes the loss it should because it "exits" a name that was locked lower-circuit for eleven straight
sessions.

---

## B5. `liquidity_metrics` — trailing ADTV, turnover ratio, tradability score

**Definition.** One row per (isin, as_of_date) holding the trailing liquidity statistics used by
universe filters, position sizing and capacity limits. Purely derived, but materialised because it is
read on every rebalance for every fund and must be **lag-correct**.

**Grain / PK.** `(isin, as_of_date)`.

**Attributes (all Decimal unless noted, all PIT-by-construction).**
`adtv_shares_20d`, `adtv_value_20d` (INR), `adtv_value_60d`, `adtv_value_252d`,
`median_daily_value_60d` (median, not mean — the mean is destroyed by one block deal),
`deliv_adtv_value_60d`, `pct_days_traded_60d` (0–1), `zero_volume_days_60d` (int),
`turnover_ratio_60d` (= adtv_value / free_float_market_cap), `amihud_illiquidity_60d`
(= mean(|daily return| / traded_value) ×1e6 — the standard impact proxy),
`roll_spread_estimate_60d` (= 2·sqrt(−cov(Δp_t, Δp_{t−1})) when the covariance is negative, else NULL),
`corwin_schultz_spread_20d` (high–low based spread estimator — the best free bid-ask proxy for a
market with no historical quote data), `days_to_liquidate_1cr` (Decimal, = 1e7 / (participation_cap ×
deliv_adtv_value_60d)), `liquidity_tier` (enum `MEGA`/`LARGE`/`MID`/`SMALL`/`MICRO`/`UNTRADABLE`).

**Volume.** ~10M rows (one per security-day), or compute at rebalance dates only (~240 dates over 20y
for monthly rebalancing × 2,000 names = 480k rows — recommended).

**Acquisition — DERIVED.** Inputs: B1 (`traded_value`, `volume_shares`, `close`), B3 (`deliverable_qty`),
E-family `free_float_market_cap`. **Derivation must use only `trade_date <= as_of_date - lag`**, where
`lag ≥ 1` if decisions are made intraday. This is where look-ahead sneaks in most often and most
invisibly: a 60-day mean computed on a window that includes the decision day.

**Why needed.** It is the input to *both* the universe filter and the capacity model, and it is the
only defence against a theme fund's natural drift into untradable microcaps.

**Failure mode.** Phantom liquidity; and, subtly, **look-ahead liquidity selection** — filtering the
2012 universe on liquidity measured over 2012–2013 selects the names that were *about to* become
liquid, which correlates strongly with subsequent returns. A beautiful, entirely fake, backtest.

---

## B6. `bid_ask_and_microstructure` (proxy entity)

**Definition.** One row per (isin, as_of_date) carrying the best available *estimate* of transaction
cost microstructure, explicitly labelled as an estimate. India has **no free historical quote/L2
archive**; NSE sells tick and order-book data through its data products division under a paid,
restrictive licence, and the historical depth of any free source is zero.

**Attributes.** `spread_bps_estimate` (Decimal), `spread_estimator` (enum: `CORWIN_SCHULTZ`, `ROLL`,
`ABDI_RANALDO`, `VENDOR_QUOTED`), `estimator_confidence` (Decimal 0–1), `impact_coefficient`
(Decimal — the k in the square-root law), `impact_model_version` (str).

**Acquisition — DERIVED** (high–low and serial-covariance estimators from B1) or **PAID**
(NSE Data & Analytics / TrueData tick archives; not economically sensible for a 20-year backfill).

**Verdict: derived estimate is the honest answer, and the model version must be journaled** so a
backtest's cost assumption is auditable rather than a magic number.

---

## B7. `capacity_model_input` and `capacity_estimate`

**Definition.** `capacity_estimate` holds one row per (fund_id, as_of_date, isin) stating the maximum
INR the fund could put into (or take out of) that name over a stated number of days at a stated
participation cap and expected impact. It is the entity that converts "the strategy says 4% weight"
into "the strategy may hold ₹X of this name".

**Grain / PK.** `(fund_id, as_of_date, isin)`.

**Attributes.** `participation_cap_pct` (Decimal, e.g. 0.10 of delivery ADTV — a *policy* input, must
be versioned), `horizon_days` (int), `max_position_value` (Decimal INR), `days_to_build`,
`days_to_exit` (Decimal), `expected_impact_bps` (Decimal), `impact_model_version`,
`binding_constraint` (enum: `ADTV`, `FREE_FLOAT_PCT`, `SEBI_5PCT_DISCLOSURE`, `BAND`, `SERIES_T2T`,
`CROSS_FUND_CONTENTION`), `cross_fund_share` (Decimal 0–1 — this fund's share of the house's total
demand for this name that day; see J13).

**Acquisition — DERIVED** from B5 + E7 (free float) + J-family fund AUM + the cross-fund aggregate.

**Why needed.** This is the difference between a research backtest and a fund manager. A multi-fund
system that runs six thematic funds all overweight the same twelve defence smallcaps has an
*aggregate* capacity problem that no single fund's backtest can see.

**Failure mode.** The signature failure of thematic multi-fund products: each fund is individually
plausible, the house in aggregate is 40% of a smallcap's float, and the exit is not merely costly but
impossible. A backtest without cross-fund capacity aggregation cannot detect this and will report six
independent Sharpe ratios that were never simultaneously achievable.

---

# FAMILY C — CORPORATE ACTIONS & ADJUSTMENT

The governing principle for this whole family, and the reason it is the highest-defect-density area of
any Indian equity platform: **L1 stores unadjusted observations and a separate factor series; adjusted
prices are computed on read, never stored.** Storing adjusted prices means every new corporate action
silently rewrites history, which destroys replay determinism (a repo invariant) and makes a
backtest run in March irreproducible in April.

## C1. `corporate_action` — the canonical event record

**Definition.** One row per (security, action type, ex-date) — one distinct entitlement event. A row
means: "holders of ISIN X on the record date R, established by trading ex on date E, became entitled
to the benefit described by (`action_type`, `ratio_numerator`, `ratio_denominator`, `cash_amount`)."

**Grain / PK.** `(isin, ex_date, action_type, sub_type)`. Note the composite: a company can go ex for
a dividend and a bonus on the same day, and both must survive.

**Attributes.**

| field | type | units | null | PIT | notes |
|---|---|---|---|---|---|
| `action_id` | uuid | — | no | — | |
| `isin` | str(12) | — | no | STATIC | resolved through A3 at parse time, with the *as-of-then* symbol retained in `symbol_raw` |
| `action_type` | enum | — | no | STATIC | `DIVIDEND`, `SPLIT`, `BONUS`, `RIGHTS`, `MERGER`, `DEMERGER`, `AMALGAMATION`, `BUYBACK`, `CAPITAL_REDUCTION`, `SPIN_OFF`, `SCHEME_OF_ARRANGEMENT`, `NAME_CHANGE`, `ISIN_CHANGE`, `FACE_VALUE_CHANGE`, `CONSOLIDATION`, `LIQUIDATION`, `OPEN_OFFER`, `DELISTING_OFFER` |
| `sub_type` | enum | — | yes | STATIC | for dividends: `INTERIM`, `FINAL`, `SPECIAL`, `SECOND_INTERIM`… — **materially different for tax and for yield signals** |
| `announcement_date` | date | — | yes | **PIT-critical** | board meeting / outcome disclosure date |
| `announcement_ts` | tz-aware dt | — | yes | **PIT-critical** | to the minute where available; the difference between "before close" and "after close" is a whole day of alpha |
| `ex_date` | date | — | no | STATIC | first day trading without the benefit |
| `record_date` | date | — | yes | STATIC | |
| `book_closure_start`/`_end` | date | — | yes | STATIC | the pre-2019 mechanism, still appears |
| `payment_date` | date | — | yes | STATIC | **when the cash actually arrives** — typically 30–45 days after record date |
| `cash_amount_per_share` | Decimal(18,6) | INR | yes | STATIC | dividends, capital reduction, buyback price |
| `dividend_pct_of_face` | Decimal | % | yes | STATIC | as announced (Indian convention); **must be reconciled against face value at that date, not today's** |
| `ratio_new` / `ratio_old` | Decimal | shares | yes | STATIC | see C2 for semantics — this is where the bugs live |
| `resulting_isin` | str(12) | — | yes | STATIC | for demerger/spin-off: the ISIN of the entity received |
| `resulting_ratio_new`/`_old` | Decimal | — | yes | STATIC | shares of the resulting entity per share held |
| `rights_issue_price` | Decimal | INR | yes | STATIC | |
| `rights_entitlement_isin` | str(12) | — | yes | STATIC | the `IN9…` RE instrument, tradable since 2020 |
| `buyback_mode` | enum | — | yes | STATIC | `TENDER` (pro-rata, with acceptance ratio) vs `OPEN_MARKET` |
| `buyback_acceptance_ratio` | Decimal | 0–1 | yes | STATIC | **only known after the fact** — a separate `knowledge_ts` |
| `purpose_text_raw` | str | — | no | STATIC | the exchange's free-text purpose string, kept verbatim for re-parsing |
| `is_cancelled` | bool | — | no | PIT | actions do get withdrawn after announcement |
| `revision_no`, `knowledge_ts`, `source_id`, `l0_ref` | | | | | the bitemporal envelope — **essential here**, because ex-dates get revised |

**Frequency / volume.** Dividends dominate. Across NSE+BSE: **~6,000–12,000 actions per year** in the
recent era (dividends ~70%, of which many are from BSE-only names), lower in 2006–2010.
Over 20y: **~120,000–200,000 rows**, plus revisions. Small table, enormous blast radius.

**Acquisition — FETCHED, from at least two independent sources, always.**
- NSE: `https://www.nseindia.com/companies-listing/corporate-filings-actions` and the JSON behind it
  (`/api/corporates-corporateActions?index=equities&from_date=&to_date=`), plus the static hub
  `https://www.nseindia.com/static/investor-relations/corporate-actions`. Date-range queries; the
  API caps the window, so a 20-year pull is a month-by-month campaign with UA/cookie priming.
- BSE: `https://www.bseindia.com/corporates/corporate_act.aspx` — **BSE's corporate action archive is
  broader than NSE's** because it covers the BSE-only universe, and it keys on scrip code.
- Registrar/depository: NSDL and CDSL corporate-action files (authoritative on record/payment dates).
- Company filings (G1) — the `announcement_ts` of record; the exchange CA feed usually carries only a
  date, so if you want minute-accurate announcement timing you must join to the announcement stream.
- Vendors with clean CA history: Prowess, Capitaline, ACE, Refinitiv, and (cheap, partial)
  EODHD's splits/dividends API (`https://eodhd.com/financial-apis/api-splits-dividends`) — useful as a
  **third opinion for reconciliation**, not as a primary.

**Format quirks and known traps.**
1. The exchange publishes the ratio inside a free-text `purpose` string: `"BONUS 1:2"`, `"FV SPLIT
   FROM RS.10 TO RS.2"`, `"DIV RS.4.50 PER SHARE"`, `"RIGHTS 3:8 @ PREM RS 90"`, `"AMALGAMATION"`.
   **There is no structured ratio field.** You are writing a parser with a golden test suite — which
   is precisely why the repo has `tests/golden/` for the CA suite.
2. `1:2` is ambiguous across sources and eras: bonus `1:2` in India conventionally means *one new
   share for every two held* (holding ×1.5), but some vendors mean the opposite. **Store
   `ratio_new`/`ratio_old` explicitly and never store the string as truth.**
3. Split ratios are expressed as face-value changes (`FV 10 → 2` = 5-for-1), bonus as share counts.
   Two different arithmetics under one `ratio` column is how factor bugs happen.
4. Ex-dates get revised after announcement. Without `revision_no`, a re-fetch silently changes history.
5. Dividends "per share" vs "% of face value" — a 100% dividend on a ₹10 face is ₹10; on a ₹1 face
   (post-split) it is ₹1. Using today's face value on a 2009 dividend is a 10× error.

**Why needed.** Adjustment factors, dividend cash flows, share-count evolution, merger position
mapping, tax lots.

**Failure mode.** A missed split = a fake -80% return and a momentum system that shorts it. A missed
demerger = a permanent phantom loss on the parent. A missed dividend = systematic understatement of
total return by ~1.3%/yr, compounding to ~30% over 20 years. Double-application (adjusting an
already-adjusted vendor price) = the same magnitude in the other direction.

---

## C2. `adjustment_factor` — the derived factor series

**Definition.** One row per (isin, ex_date) giving the multiplicative factors that convert an
unadjusted price/quantity observed before that ex-date into a comparable post-action basis. This is
the *derived* artefact; C1 is the source of truth.

**Grain / PK.** `(isin, ex_date)` — one row per ex-date even if several actions share it, with the
factors multiplied together and the contributing `action_id`s recorded.

**Attributes.** `price_factor` (Decimal(24,12)), `qty_factor` (Decimal(24,12)),
`total_return_factor` (Decimal(24,12)), `contributing_action_ids` (uuid[]),
`prev_close_unadjusted` (Decimal — the denominator used, retained for audit),
`factor_method` (enum: `RATIO_ONLY`, `DIVIDEND_PRICE_RATIO`, `SPINOFF_VALUE_ALLOCATION`),
`computed_from_revision` (int), `knowledge_ts`.

**Exact derivation (this is the spec, and it should be a golden test):**

```
Let P = last unadjusted close strictly before ex_date.

SPLIT (FV f_old -> f_new):          r = f_old / f_new
BONUS  a new for every b held:      r = (a + b) / b
CONSOLIDATION (reverse split):      r = f_old / f_new   (r < 1)
  qty_factor   = r
  price_factor = 1 / r

DIVIDEND of D per share:
  price_factor = (P - D) / P          # and qty_factor = 1
  total_return_factor = P / (P - D)   # for the TR series only

RIGHTS: a new shares at price S for every b held:
  theoretical_ex_rights_price TERP = (b*P + a*S) / (a + b)
  price_factor = TERP / P
  qty_factor   = (a + b) / b
  # NOTE: only valid if the right is taken up. For a fund that does NOT subscribe,
  # the correct treatment is a cash inflow from selling the Rights Entitlement (post-2020,
  # REs are tradable) or a real dilution loss (pre-2020). Do not silently assume take-up.

DEMERGER / SPIN-OFF: parent keeps fraction w of pre-event value
  price_factor = w
  and a NEW position of (resulting_ratio_new/resulting_ratio_old) shares of resulting_isin
  is created at cost basis (1-w)*P.
  # w must come from the scheme's court-approved value allocation, or be inferred from the
  # first-day prices of parent and child (record the method in factor_method).

CUMULATIVE factor for adjusting a price observed on date d:
  cum(d) = Π price_factor(e) for all ex_dates e with d < e <= as_of_date
  adjusted_price(d) = raw_close(d) * cum(d)
```

**The PIT rule that is almost always broken:** `cum()` must be truncated at the *decision date*, not
at today. An adjusted price series for a backtest running as of 2015-06-01 must not include factors
for actions that happened in 2016. Otherwise the *scale* of the series encodes future actions — a
subtle but real leak, and one that breaks replay determinism outright.

**Volume.** ~150k rows. Recomputed whenever C1 revises; store the recomputation as a new revision,
never in place.

**Acquisition — DERIVED, wholly, from C1 + B1.** Never fetch adjusted prices. If you must ingest a
vendor's adjusted series, ingest it into a *separate reconciliation table* and diff it against yours;
disagreements are the best corporate-action bug detector that exists.

**Failure mode.** Every adjustment-factor defect shows up as an isolated extreme return, and extreme
returns are exactly what momentum and thematic-breakout strategies select. So the defect does not
average out — it is *actively sought* by the strategy. This is why the repo convention demands a test
that fails if the adjustment logic is inverted.

---

## C3. `merger_mapping` and `scheme_of_arrangement`

**Definition.** One row per (source ISIN, destination ISIN, effective date) describing how a holding
in the disappearing entity became a holding in something else — shares, cash, or both.

**Grain / PK.** `(source_isin, destination_isin, effective_date)`.

**Attributes.** `scheme_type` (enum: `MERGER`, `AMALGAMATION`, `DEMERGER`, `SLUMP_SALE`,
`REVERSE_MERGER`, `CROSS_BORDER`), `share_exchange_ratio_new`/`_old` (Decimal),
`cash_per_share` (Decimal INR, nullable), `appointed_date` (date — accounting), `effective_date`
(date — NCLT order), `record_date` (date — entitlement), `last_trading_date` (date),
`first_trading_date_of_destination` (date), `nclt_order_ref` (str), `value_allocation_parent`
(Decimal 0–1 for demergers), `knowledge_ts`.

**Volume.** ~60–150/yr → **1,500–3,000 rows** over 20y. Small; high per-row value.

**Acquisition — FETCHED.** Exchange notices and outcome-of-board-meeting filings (G1), NCLT orders,
SEBI scheme-of-arrangement observation letters, and the CA feed's `AMALGAMATION`/`SCHEME` purposes
(which give the date but rarely the ratio — the ratio is in the scheme document PDF). Expect a
**manual/LLM-assisted extraction pass over PDFs** for the ratio; there is no free structured feed.
Vendors (Prowess, Capitaline) carry the mapping and are worth paying for on this entity specifically.

**Why needed.** Continuity of a position through the most common cause of "disappearance" in the
Indian market that is *not* a failure. Roughly a third of the names that vanish from the universe
merge rather than die.

**Failure mode.** Treated as a delisting → the fund "sells" at the last price and books a phantom
gain/loss and a phantom tax event; the acquirer position that should have appeared never does.
Systematically biases against exactly the successful-consolidation outcomes that thematic sectors
(defence, cement, banking) produce.

---

## C4. `dividend_cash_flow` (fund-facing, distinct from C1)

**Definition.** One row per (fund_id, isin, action_id) recording the actual cash a *fund* received:
gross, withholding, net, and the date it landed. Separated from C1 because the market fact and the
fund's cash fact are different things with different dates and different tax treatment.

**Grain / PK.** `(fund_id, action_id)`.

**Attributes.** `shares_held_on_record_date` (Decimal), `gross_amount` (Decimal INR),
`tds_rate` (Decimal), `tds_amount` (Decimal), `net_amount` (Decimal), `credited_on` (date),
`tax_regime` (enum: `DDT_ERA_EXEMPT`, `SEC_115BBDA`, `TAXABLE_IN_HANDS_POST_2020`),
`accrual_date` (= ex_date, for NAV accrual), `is_accrued_not_received` (bool).

**Why needed.** Two effects an adjusted-price backtest hides completely: (1) the **30–45 day lag**
between ex-date and cash — the fund's cash is lower than the TR index assumes for over a month;
(2) **tax**. From 2020-04-01 dividends are taxable in the recipient's hands with 10% TDS above
₹5,000 (s.194); before that DDT was paid by the company and the receipt was exempt (with s.115BBDA
biting above ₹10L for resident individuals from FY2016-17). A TR-index-style backtest reinvests the
gross dividend at the ex-date price, instantly and tax-free. None of those three things is true.

**Failure mode.** Over 20 years at a ~1.3% average yield, the combined lag + tax + reinvestment-price
error is worth **several hundred basis points of cumulative, entirely fake, outperformance** — and
it lands disproportionately in "quality/dividend" style themes, which is where it will be least
noticed and most damaging.

---

## C5. `buyback_participation` and `open_offer_participation`

**Definition.** One row per (fund_id, action_id) capturing tender participation: shares tendered,
acceptance ratio, shares accepted, price, unaccepted shares returned, and the date they returned.

**Attributes.** `shares_tendered`, `acceptance_ratio` (Decimal 0–1, **knowledge_ts = post-close of
the tender window**), `shares_accepted`, `price_per_share` (Decimal INR), `settlement_date` (date),
`tax_treatment` (enum: `SEC_115QA_COMPANY_PAID` for buybacks up to 2024-09-30,
`DEEMED_DIVIDEND_SEC_2_22_F` from 2024-10-01), `residual_shares_returned_on` (date).

**Why needed.** Buyback arbitrage is a real, mechanical source of return in Indian small/mid caps and
a theme fund holding a name into a tender must model *partial* acceptance. And the tax treatment
**changed regime on 2024-10-01**: before, the company paid 20% buyback tax and the receipt was exempt
to the shareholder; after, the entire consideration is a deemed dividend taxed at the shareholder's
slab with the cost allowed as a capital loss. A single hard-coded rate is wrong on one side of that
date by a large margin.

**Failure mode.** Assumes 100% acceptance (overstates return, understates residual position) and the
wrong tax regime for half the window.

---

# FAMILY D — CLASSIFICATION & THEMATICS (the crux)

This family is what distinguishes a *thematic multi-fund manager* from a generic quant backtester,
and it is where the honest-backtest problem is hardest, because **the label is the alpha and the label
is contaminated by the future.**

## D0. The design position: how a theme becomes a machine-checkable PIT universe rule

A theme ("India defence manufacturing", "quality compounders under ₹5,000cr", "monsoon-linked rural
consumption") is a sentence. A fund needs a *predicate* evaluable on any past date using only facts
knowable then. The compiler has four layers, and each layer is a data requirement:

**Layer 1 — Structural (cheap, coarse, PIT-safe if you have history).**
Membership in a taxonomy node as of the date: `basic_industry_code ∈ {…}` from D1, evaluated with
`knowledge_ts <= cutoff`. Handles "cement", "private banks", "IT services" well. Handles "defence
manufacturing" **badly** — no Indian taxonomy had a defence node for most of the window, and the
companies sat under Industrial Products, Aerospace & Defence (added late), Electronic Equipment and
Shipbuilding.

**Layer 2 — Economic exposure (expensive, precise).**
`segment_revenue_share(theme) >= threshold` from D5, using the segment disclosures *published before
the cutoff*. This is the only layer that can honestly say "this company has real exposure to the
theme", as opposed to "someone tagged it". It is the layer that separates a defence *fund* from a
defence *narrative*. Its cost is that segment data must be extracted from annual reports.

**Layer 3 — Textual/semantic evidence (broad, noisy, must be date-stamped).**
Keyword and embedding match over **dated documents**: the MD&A and business description in the FY
annual report filed on date F, the exchange announcements stream, the DRHP. Never over a
current-as-of-today company description. Produces `theme_evidence` rows (D6) each carrying the
document's publication date. The rule can then say `count(evidence where knowledge_ts <= cutoff) >= k`.

**Layer 4 — Relational (supply chain, peers, index co-membership).**
"Suppliers to HAL", "peers of Bharat Electronics". Requires a `company_relation` graph (D8) whose
edges are themselves dated. The weakest and most contaminable layer; use as a *ranking* input, never
as a sole inclusion criterion, and audit it hardest.

**The rule object itself is data** (see J2 `theme_rule`): versioned, `effective_from`-dated,
content-hashed. If a human edits the "defence" rule in 2026, the 2012 backtest must either (a) rerun
under the new rule and be labelled as such, or (b) use the rule version in force then. Both are
legitimate; silently mixing them is not. **The rule version hash goes in the decision journal.**

**The falsifiability requirement.** For each theme you must hold a small, hand-labelled
`theme_ground_truth` set (say 50 in / 50 out, labelled *as of several historical dates*) so the
compiler's precision/recall is measurable. Without it, "the LLM said it's a defence company" is not a
verifiable claim and the whole fund family is unfalsifiable.

---

## D1. `industry_classification` — taxonomy assignment, effective-dated

**Definition.** One row per (isin, taxonomy, effective interval) giving the company's position in a
four-level industry taxonomy. This must be a **history**, not a snapshot; that is the entire point.

**Grain / PK.** `(isin, taxonomy_id, effective_from)`.

**Attributes.** `taxonomy_id` (enum: `NSE_INDICES_4TIER`, `BSE_INDUSTRY`, `AMFI_SECTOR`, `NIC_2008`,
`GICS`, `INTERNAL`), `macro_economic_sector` (str), `sector` (str), `industry` (str),
`basic_industry` (str), plus the code fields for each level, `taxonomy_version` (str, e.g. `2023-07`),
`effective_from`/`effective_to` (date), `change_reason` (enum: `INITIAL`, `COMPANY_BUSINESS_CHANGE`,
`TAXONOMY_RESTRUCTURE`, `RECLASSIFICATION`), `knowledge_ts`, `source_id`.

**The taxonomy to standardise on.** **NSE Indices' 4-tier classification** — 12 macro-economic
sectors, 22 sectors, 59 industries, 197 basic industries — is the right primary, because it is the
one the index provider actually uses to build the sector and thematic indices you will benchmark
against, and it is published with methodology:
`https://www.nseindia.com/static/products-services/industry-classification` and the guideline PDF
`https://www.niftyindices.com/docs/default-source/default-document-library/nse-indices_industry-classification-guideline-2023-07.pdf`.
Carry **BSE's** structure as a cross-check
(`https://www.bseindices.com/Downloads/India_Industry_Classification_Structure.pdf`), **AMFI's**
sector classification because SEBI mutual-fund disclosures use it
(`https://www.sebi.gov.in/mf/equitydec15.html`), and **NIC-2008** because it is embedded in every
company's CIN (chars 3–7) and is therefore free, PIT-stable and available for the whole window.

**⚠ The 20-year depth problem — the single most important flag in this document.**
NSE Indices publishes the *current* classification and the *current* structure document. It does
**not** publish a historical time series of company→node assignments, and the structure itself has
been restructured repeatedly (documents exist for 2022-04, 2023-07, and the AMFI-aligned
recategorisation work of 2017–2018). Therefore:

- **You cannot buy or download an Indian classification history for 2006–2026 from the exchanges.**
- Options, in descending order of honesty:
  1. **Snapshot forward from today** and accept that the history is missing (and say so loudly in
     every backtest report). Start archiving the classification file daily/monthly *now*; in five
     years you have a real history.
  2. **Reconstruct from dated primary documents**: CIN's NIC code (available from incorporation),
     the industry field on old exchange master files if you have archived copies, the sector
     assignment implied by historical index membership (D3 — if a company was in the CNX IT index in
     2009, it was an IT company in 2009), and the annual-report business description (D4).
  3. **Buy it**: Prowess and Capitaline maintain industry assignment with history; Prowess's NIC-based
     classification goes back to 1989. This is the only realistic route to a genuinely PIT industry
     history for the early window. **Recommend budgeting for it.**
  4. **Use the current classification and mark every backtest that touches it as
     `classification_pit = FALSE`** — legitimate only if the number is reported alongside the result.

**Volume.** ~10k securities × ~4 taxonomies × ~2–4 intervals = **80k–160k rows**.

**Why the algorithm needs it.** Layer 1 of the theme compiler; sector-neutrality rails; Brinson
attribution; peer-group construction for relative valuation; sector-concentration limits.

**Failure mode.** *Classification look-ahead.* Selecting the 2010 "defence" universe using 2026 tags
picks companies that later became defence companies — which is precisely a selection on the outcome.
The measured effect in equivalent US studies (using current GICS to define past sector portfolios) is
large; in an Indian thematic context, where sector narratives re-form every few years, it is larger.
This one error alone can turn a flat strategy into a 25%+ CAGR backtest.

---

## D2. `index_membership` — index constituent history with entry/exit dates

**Definition.** One row per (index, isin, inclusion interval), with weight and index-weight factor.
A row means "ISIN X was a constituent of index I from date A to date B, with an investable weight
factor of w during that period".

**Grain / PK.** `(index_id, isin, effective_from)`.

**Attributes.** `index_id` (str: `NIFTY50`, `NIFTY500`, `NIFTYMIDCAP150`, `NIFTYSMALLCAP250`,
`NIFTY_INDIA_DEFENCE`, `NIFTY_INDIA_MANUFACTURING`, `NIFTY_INDIA_CONSUMPTION`, `BSE500`, …),
`isin`, `effective_from`/`effective_to` (date), `entry_reason`/`exit_reason` (enum: `PERIODIC_REVIEW`,
`MERGER`, `DELISTING`, `IPO_FAST_ENTRY`, `SUSPENSION`, `REPLACEMENT`), `announcement_date` (date —
index changes are pre-announced by ~4 weeks, which is itself tradable information and must be
separately timestamped), `weight_pct` (Decimal, PIT), `index_weight_factor_iwf` (Decimal 0–1, PIT),
`capping_factor` (Decimal, nullable), `circular_ref`, `knowledge_ts`.

**Volume.** Say 40 indices tracked × average 150 constituents × ~1.5 turnover events/name/decade →
**~50k–150k membership intervals** over 20y; daily weight snapshots would be ~40 × 150 × 5,000 =
30M rows (only materialise weights at rebalance dates unless you need daily index replication).

**Acquisition — FETCHED, with a real 20-year depth problem.**
- `https://www.niftyindices.com/reports/historical-data` — index **levels** (PR, TRI, NTR) from
  inception. Levels are easy; **constituents are not**.
- Current constituent CSVs: `https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv`
  (and `ind_nifty500list.csv` etc.) — **snapshot only**.
- The reconstructible path: NSE Indices publishes a **press release / circular for every index
  change**, with the announcement date and the effective date. Twenty years of those PDFs, parsed,
  *is* the membership history. This is a genuine multi-week extraction project and there is no free
  shortcut. Third-party reconstructions exist but are unverified.
- Paid: NSE Indices data licence, Refinitiv, Bloomberg (`INDX MWEB`), Prowess.

**⚠ The thematic-index trap, stated explicitly.** Nifty India Defence, Nifty India Manufacturing and
most thematic indices were **launched recently** and their published "history" is **back-computed**
using the *current* classification and the *current* eligibility rules. Using a thematic index's
pre-launch back-history as ground truth for what was in the theme is therefore a direct import of the
look-ahead you are trying to avoid. **Rule: an index's membership history is usable as PIT evidence
only from its live-launch date forward.** Store `index_live_from` on the index definition and enforce it.

**Why needed.** Benchmarks; float factors (IWF); the "index inclusion" event as a signal; sector
neutrality; and as one honest Layer-1 signal for older themes.

**Failure mode.** Backtesting a "Nifty 500 universe" using today's Nifty 500 is textbook survivorship
+ look-ahead: today's 500 is the set that grew into the index. Benchmarking against a back-computed
thematic index makes your fund look worse or better than reality depending on which contamination
dominates — and you cannot tell which.

---

## D3. `index_definition`

**Definition.** One row per index per methodology version. `index_id`, `name`, `provider` (enum
`NSE_INDICES`/`BSE`/`MSCI`), `index_type` (enum `BROAD`, `SECTOR`, `THEMATIC`, `STRATEGY`, `TRI`),
`base_date`, `base_value` (Decimal), `live_from` (date — **the anti-backfill guard**),
`is_back_computed_before_live` (bool), `methodology_version`, `rebalance_frequency` (enum),
`capping_rule` (str), `eligible_universe_rule` (str), `currency`.
**Volume:** low hundreds. **Source:** niftyindices.com methodology documents, BSE indices site.

---

## D4. `business_description` — dated narrative text about what the company does

**Definition.** One row per (issuer_id, document_id, section) holding a block of company-authored or
regulator-filed prose about the business, **with the document's publication date**. This is the
substrate for Layer 3 of the theme compiler.

**Grain / PK.** `(document_id, section_ordinal)`.

**Attributes.** `issuer_id`, `isin_at_publication`, `document_type` (enum: `ANNUAL_REPORT_MDA`,
`ANNUAL_REPORT_BUSINESS_OVERVIEW`, `DIRECTORS_REPORT`, `DRHP`, `RHP`, `EARNINGS_CALL_TRANSCRIPT`,
`INVESTOR_PRESENTATION`, `PRESS_RELEASE`, `EXCHANGE_FILING`, `WEBSITE_ABOUT` — the last one
**flagged as non-PIT**), `fiscal_year` (int, nullable), `publication_date` (date),
`publication_ts` (tz-aware, where the exchange timestamps it), `language` (enum), `text` (str),
`text_sha256` (str), `embedding` (vector, nullable, with `embedding_model_version` — because an
embedding computed by a 2026 model over 2011 text is fine for *matching*, but the **text** must be
2011 text), `source_url`, `l0_ref`, `knowledge_ts` (= publication_ts).

**Volume.** ~5,000 companies × 20 annual reports = 100k documents, ~10–50 sections each →
**1M–5M text rows**, tens of GB of raw PDFs in L0. Earnings-call transcripts add ~4/yr for the ~500
covered names (and effectively do not exist before ~2010 for most of the market).

**Acquisition — FETCHED, effortful.**
- **BSE is the best free archive of annual reports**: `https://www.bseindia.com/corporates/Comp_Resultsnew.aspx`
  and the annual-report section under each scrip; PDFs going back well into the 2000s.
- NSE: `https://www.nseindia.com/companies-listing/corporate-filings-annual-reports`.
- MCA21 holds filed annual returns (paid per document, no bulk).
- Earnings-call transcripts: exchange filings under LODR Reg 30 (mandatory disclosure of transcripts
  **only since 2018** — a hard depth boundary), plus company IR pages.
- Screener.in, Tijori, Trendlyne carry *current* descriptions — **useful for building the theme
  vocabulary, forbidden as PIT evidence.**

**Licensing/ToS.** Exchange-hosted PDFs are public filings; scraping them at polite rates for internal
research is the normal practice. Screener/Trendlyne ToS prohibit scraping; do not build a pipeline on
them.

**Why needed.** It is the only PIT-honest way to know that a company was talking about defence orders
in 2013. A classification will not tell you; an index will not tell you; the annual report will.

**Failure mode.** Using today's one-line company description (the trivially available thing) makes
every theme fund clairvoyant. This is the most likely accidental sin in the whole system precisely
because current descriptions are so easy to get.

---

## D5. `segment_revenue` — revenue/EBIT/assets by reported business segment

**Definition.** One row per (issuer_id, fiscal_period, segment_name, measure). The company's own
Ind AS 108 / AS-17 operating-segment disclosure. This is Layer 2 of the theme compiler and the only
quantitative measure of "true exposure".

**Grain / PK.** `(issuer_id, period_end, consolidation, segment_name, measure, revision_no)`.

**Attributes.** `segment_name` (str, as reported — companies rename segments, so keep raw and add a
mapped `segment_canonical` with its own mapping table), `segment_type` (enum `BUSINESS`, `GEOGRAPHIC`),
`measure` (enum: `REVENUE_EXTERNAL`, `REVENUE_INTERSEGMENT`, `RESULT_EBIT`, `ASSETS`, `LIABILITIES`,
`CAPEX`, `DEPRECIATION`), `value` (Decimal INR), `unit_scale` (enum `UNITS`/`THOUSAND`/`LAKH`/
`MILLION`/`CRORE` — reports vary and this is a common 10²/10³ error source), `period_end` (date),
`period_type` (enum `QUARTER`/`HALF`/`ANNUAL`), `consolidation` (enum `STANDALONE`/`CONSOLIDATED`),
`filing_date` (date, **PIT**), `accounting_framework` (enum `IGAAP`/`IND_AS` — see E4),
`revision_no`, `source_doc_id` (FK to D4's document), `extraction_method` (enum `XBRL`, `PDF_TABLE`,
`LLM_EXTRACTION`, `VENDOR`), `extraction_confidence` (Decimal 0–1).

**Volume.** ~4,000 reporting companies × 20 years × ~4 segments × ~5 measures = **1.6M rows**
annual-only; ×4 if quarterly segment data is captured (quarterly segment disclosure is required but
much thinner).

**Acquisition — FETCHED / EXTRACTED, and this is the most expensive entity in the catalog.**
- **XBRL**: BSE was the first Indian exchange to implement XBRL, initially for shareholding pattern
  and financial results (`https://www.bseindia.com/static/about/xbrl_info.aspx`); NSE hosts XBRL
  filings too. **Segment data tagging is partial and inconsistent** — the taxonomy supports it,
  compliance with it varies, and coverage before ~2012 is effectively nil.
- **PDF extraction** from the annual report's segment note — the realistic route for depth. Table
  extraction plus LLM normalisation, with `extraction_confidence` and a human-audited sample.
- **Vendors**: Prowess, Capitaline and ACE all carry segment data with real history and are the
  pragmatic answer for 2006–2015. Prowess's segment coverage is the deepest.

**Why needed.** It is the difference between "Company X is tagged defence" and "Company X earned 62%
of FY2014 revenue from defence contracts". A thematic fund whose holdings have 8% theme revenue is
not a thematic fund; without D5 you cannot know, and neither can your investor.

**Failure mode.** Theme drift and mislabelling: the fund holds a diversified engineering conglomerate
because a keyword matched, the backtest attributes its return to the theme, and the live fund fails to
track the theme it advertised. Also enables the *worst* version of look-ahead if the segment split is
taken from the latest annual report and applied backwards — a company that entered defence in 2019
looks like a defence company in 2011.

---

## D6. `theme_evidence` — dated evidence linking a company to a theme

**Definition.** One row per (theme_id, issuer_id, evidence_document, extraction). The audit trail
behind a thematic inclusion: *what* said this company belongs to this theme, *when* it said it, and
how strongly.

**Grain / PK.** `(theme_id, issuer_id, source_doc_id, extractor_version)`.

**Attributes.** `theme_id` (FK), `issuer_id`, `isin_at_evidence`, `evidence_type` (enum:
`CLASSIFICATION_NODE`, `SEGMENT_REVENUE`, `KEYWORD_MATCH`, `EMBEDDING_SIMILARITY`, `ORDER_WIN_FILING`,
`INDEX_MEMBERSHIP`, `SUPPLY_CHAIN_EDGE`, `ANALYST_ASSERTION`, `HUMAN_LABEL`), `score` (Decimal 0–1),
`evidence_date` (date — the document's publication date), `knowledge_ts` (tz-aware),
`snippet` (str — the matched text, kept for audit), `source_doc_id`, `extractor_version` (str —
model + prompt hash, so the extraction is reproducible), `is_human_verified` (bool).

**Volume.** ~20 themes × ~500 candidate companies × ~20 documents = **200k rows** and growing with
each theme.

**Acquisition — DERIVED** (from D1/D2/D4/D5/G1) by the mapper module, but it is a first-class stored
entity because **the extractor version is part of the decision journal**. Re-running the mapper with a
better model in 2027 must not silently change what the 2013 backtest saw.

**Why needed.** G4 — proving a decision. "Why did this fund buy this company in 2013?" must be
answerable with dated evidence, not with a model's current opinion.

**Failure mode.** Unfalsifiable thematics; and non-reproducible backtests, because the LLM that
assigns the labels is nondeterministic unless its version and output are pinned as data.

---

## D7. `theme_definition` and `theme_vocabulary`

**Definition.** `theme_definition`: one row per (theme_id, version) — the human-authored statement of
the theme, its inclusion/exclusion predicate tree, its thresholds, and its benchmark.
`theme_vocabulary`: one row per (theme_id, version, term) — the controlled keyword/phrase list with
weights and negative terms, and the classification nodes considered in/near the theme.

**Attributes (theme_definition).** `theme_id`, `version` (int), `title`, `prose_statement` (str),
`predicate_tree` (JSON, but validated against a pydantic model — no bare dicts across the boundary),
`min_theme_revenue_pct` (Decimal), `min_evidence_score` (Decimal), `max_names`, `benchmark_index_id`,
`effective_from` (date), `author` (str), `rule_hash` (str, sha256 of the canonicalised predicate),
`ground_truth_set_id`, `precision_at_last_audit`, `recall_at_last_audit` (Decimal).

**Attributes (theme_vocabulary).** `term` (str), `term_type` (enum `INCLUDE`/`EXCLUDE`/`CONTEXT`),
`weight` (Decimal), `language`, `added_on` (date), `added_by`.

**⚠ PIT hazard unique to this entity.** The vocabulary is written today. Writing the word
"drone" into the 2010 defence vocabulary is not look-ahead (the word existed); writing the *name of a
specific company that later won a drone contract* into the vocabulary **is**. Rule: vocabularies may
contain concepts, products, technologies, regulations and government programmes; they may **not**
contain company names, tickers or ISINs. Enforce it with a validator — this is a cheap, high-value
guard that a reviewer can actually check.

---

## D8. `company_relation` — supply chain, peers, group, competitor edges

**Definition.** One row per directed, dated edge between two issuers (or an issuer and a non-listed
counterparty). `(from_issuer, to_issuer, relation_type, effective_from)`.
`relation_type` enum: `SUPPLIES_TO`, `CUSTOMER_OF`, `JV_PARTNER`, `SUBSIDIARY_OF`, `ASSOCIATE_OF`,
`SAME_PROMOTER_GROUP`, `PEER_OF`, `COMPETES_WITH`, `LICENSEE_OF`.
Attributes: `evidence_doc_id`, `revenue_dependency_pct` (Decimal, nullable — from the
related-party/customer-concentration disclosure), `confidence`, `knowledge_ts`.

**Acquisition — DERIVED/EXTRACTED**: related-party transaction notes and subsidiary lists in annual
reports (structured-ish, mandatory, deep history), customer-concentration disclosures (sparse in
India), order-win announcements naming the counterparty (G1 — genuinely rich for defence and
infrastructure themes), and NIC/industry co-membership for peers. No free structured Indian supply
chain dataset exists; FactSet Revere / Bloomberg SPLC are the paid options and their Indian smallcap
coverage is weak.

**Verdict: IMPROVES-FIDELITY.** Use for ranking and for theme *breadth*, never as sole inclusion.
**Failure mode:** a supply-chain graph built from today's disclosures asserts 2011 relationships that
did not exist — the same contamination as D1, with less visibility.

---

## D9. `peer_group`

**Definition.** One row per (isin, as_of_date, peer_isin, method). Needed for relative valuation
("cheap *versus its peers*"), which is how most quality/value theme rules are actually written.
Derived from D1 (same basic industry) ∩ B5 (comparable liquidity tier) ∩ E (comparable size),
recomputed at each rebalance with a lag. Volume: 2,000 names × 240 rebalances × 15 peers = **7M rows**;
materialise only for held/candidate names.
**Failure mode if wrong:** peer sets built on today's industry tags reintroduce D1's look-ahead into
every relative-value screen.

---

# FAMILY E — FUNDAMENTALS (POINT-IN-TIME)

## E1. `financial_statement_fact` — the atomic PIT fundamental

**Definition.** One row per (issuer, fiscal period, consolidation basis, line item, revision). A row
means: "in the statement for period P on basis B, published on date F (revision r), line item L had
value V." It is deliberately **long/EAV-shaped rather than a wide table**, because Indian line items
differ by industry format (banking, NBFC, insurance, general) and changed wholesale at the Ind AS
transition; a wide table forces destructive normalisation at ingest.

**Grain / PK.** `(issuer_id, period_end, period_type, consolidation, statement, line_item_code, revision_no)`.

**Attributes.**

| field | type | units | null | PIT | notes |
|---|---|---|---|---|---|
| `issuer_id` | uuid | — | no | | keyed on issuer, not ISIN — statements survive ISIN changes |
| `period_end` | date | — | no | STATIC | |
| `period_start` | date | — | no | STATIC | needed because Indian fiscal years are not always 12 months (transition years, first years) |
| `period_type` | enum | — | no | STATIC | `Q1..Q4`, `H1`, `H2`, `FY`, `TTM_DERIVED`, `STUB` |
| `fiscal_year_end_month` | int | 1–12 | no | PIT | March for most, but not all — December and June exist |
| `consolidation` | enum | — | no | STATIC | `STANDALONE`, `CONSOLIDATED` — **never mix them in a time series** |
| `statement` | enum | — | no | STATIC | `PL`, `BS`, `CF`, `SEGMENT`, `NOTES`, `RATIOS_REPORTED` |
| `line_item_code` | str | — | no | STATIC | canonical internal code |
| `line_item_label_raw` | str | — | no | STATIC | as filed |
| `value` | Decimal(24,4) | INR | yes | STATIC per revision | |
| `unit_scale` | enum | — | no | STATIC | `UNITS`/`THOUSAND`/`LAKH`/`MILLION`/`CRORE` |
| `accounting_framework` | enum | — | no | STATIC | `IGAAP`, `IND_AS`, `IFRS` |
| `industry_format` | enum | — | no | STATIC | `GENERAL`, `BANKING`, `NBFC`, `INSURANCE`, `UTILITY` |
| `is_audited` | bool | — | no | STATIC | Q4/annual audited; quarterlies reviewed |
| `is_restated` | bool | — | no | STATIC | true when this revision restates a previously published figure |
| `filing_date` | date | — | no | **PIT** | |
| `filing_ts` | tz-aware dt | — | yes | **PIT** | exchange receipt timestamp — the honest `knowledge_ts` |
| `revision_no`, `superseded_by`, `source_id`, `l0_ref` | | | | | envelope |

**Frequency / volume.** ~5,000 listed reporters × 4 quarters × 20 years = 400k statements; at ~40
line items for a quarterly and ~250 for an annual, **~25M–40M facts**, plus ~10–20% revision rows.

**Acquisition — FETCHED.**
- **Free, structured, shallow:** BSE/NSE XBRL financial results. BSE was first to mandate XBRL for
  quarterly results and shareholding pattern; NSE hosts corporate-filing XBRL at
  `https://www.nseindia.com/companies-listing/corporate-filings-financial-results`. **Usable depth is
  roughly 2011–2012 onward, and genuinely clean only from ~2015.**
- **Free, unstructured, deep:** the results PDFs on BSE going back to the early 2000s, plus annual
  reports. Extraction project.
- **Paid, structured, deep:** CMIE Prowess / ProwessIQ (time series from **1989**, ~50,000 companies
  including unlisted, 3,500+ fields, built from annual reports and exchange feeds — the deepest
  Indian source there is), Capitaline (74,800+ companies, ~2,500 fields, 15–20 years),
  ACE Equity (10 standardised industry formats, 1,750+ fields), LSEG/Refinitiv, S&P Capital IQ.
- **Free, current-only, not PIT:** Screener.in, Trendlyne, Tijori — fine for exploration, ToS-hostile
  to scraping, and they present *latest restated* figures, so they cannot be used for backtesting.

**⚠ The PIT problem with every vendor.** Prowess, Capitaline and ACE are **restated-latest** databases
by default: they present the currently-correct figure for a past period, not the figure as first
published. Prowess exposes some vintage information; none of them give you a clean bitemporal feed
without special arrangement. **Therefore: if fundamentals matter to a theme rule (they do for "quality
compounders"), the only fully honest source is the original filing with its filing date, and the
vendor is a convenience whose PIT limitation must be recorded per field.**

**⚠ The Ind AS discontinuity — a hard, unavoidable break in the middle of the window.**
Indian Accounting Standards (Ind AS) became mandatory in phases from FY2016-17 (large companies) and
FY2017-18 (the rest). Line-item definitions changed materially: revenue recognition, leases (Ind AS
116 from FY2019-20 moved operating leases onto the balance sheet, inflating both debt and EBITDA),
fair-value measurement, "other comprehensive income", consolidation scope. **Any ratio time series
that crosses FY2016-17 is comparing two different measurement systems.** Requirements that follow:
(a) `accounting_framework` on every fact; (b) a documented mapping layer with explicit "not
comparable" markers; (c) a backtest report field stating what fraction of the sample straddles the
transition. A "quality compounders" screen on ROCE or Debt/EBITDA that ignores this will show a
spurious regime shift in 2016–2020 and mistake it for a factor.

**Why needed.** Every non-price theme rule ("under ₹5,000cr", "ROCE > 18%", "net debt/EBITDA < 2",
"revenue CAGR > 15%") and every valuation overlay.

**Failure mode.** *Restatement look-ahead* — using a figure that was corrected in 2020 to make a 2016
decision. The bias direction is systematic and vicious: restatements concentrate in companies with
accounting problems, so a restated database quietly cleans up exactly the companies your quality
screen should have rejected, and the screen looks brilliant. Plus *filing-date look-ahead* — using
FY-end date instead of filing date grants 45–60 days of foresight, four times a year, for 20 years.

---

## E2. `financial_statement_filing` — the header/announcement record

**Definition.** One row per (issuer, period, consolidation, revision) — the *event* of a result being
published, independent of its contents. Carries the timestamps and the audit-opinion metadata.

**Grain / PK.** `(issuer_id, period_end, period_type, consolidation, revision_no)`.

**Attributes.** `board_meeting_date` (date), `board_meeting_announced_on` (date — the *intimation*,
itself a tradable event), `filing_ts` (tz-aware — **the PIT anchor for E1**),
`is_after_market_hours` (bool, derived), `auditor_opinion` (enum: `UNQUALIFIED`, `QUALIFIED`,
`ADVERSE`, `DISCLAIMER`, `EMPHASIS_OF_MATTER`), `going_concern_flag` (bool),
`auditor_name`, `auditor_change_flag` (bool), `delay_days` (int, derived — days past the
regulatory deadline; a genuine red flag), `restatement_of_period` (date, nullable),
`filing_document_id`.

**Why needed.** `filing_ts` is what makes E1 point-in-time at all. `auditor_opinion`,
`going_concern_flag`, `auditor_change_flag` and `delay_days` are the four cheapest, highest-signal
governance red flags available in Indian markets and belong in every fund's exclusion rail.

**Acquisition — FETCHED.** Exchange filings (G1) carry board-meeting intimations and results with
timestamps; auditor opinion is in the results filing and the annual report. Auditor resignations are
a mandatory Reg 30 disclosure with high signal value.

**Failure mode.** Without `filing_ts`: universal, invisible look-ahead. Without the red flags: the
"quality" fund holds the next accounting fraud, which is the one outcome that discredits the whole
product.

---

## E3. `shares_outstanding` — the share-count time series

**Definition.** One row per (isin, effective_from) giving the number of shares in issue. Not published
as a series by anyone; must be constructed. Everything about size, market cap, per-share metrics and
free float depends on it.

**Grain / PK.** `(isin, effective_from)`.

**Attributes.** `shares_outstanding` (Decimal, shares), `shares_listed` (Decimal — can differ; not all
issued shares are listed), `source_basis` (enum: `SHP_QUARTERLY`, `CORPORATE_ACTION_DERIVED`,
`ALLOTMENT_FILING`, `EXCHANGE_MASTER`, `VENDOR`), `face_value` (Decimal),
`change_reason` (enum: `BONUS`, `SPLIT`, `RIGHTS`, `QIP`, `PREFERENTIAL_ALLOTMENT`, `ESOP`,
`CONVERSION_OF_WARRANTS`, `BUYBACK_EXTINGUISHMENT`, `MERGER_ISSUANCE`, `CAPITAL_REDUCTION`),
`knowledge_ts`, `confidence` (enum `EXACT`/`INTERPOLATED`).

**Volume.** ~10k securities × ~15 changes over 20y = **150k rows**.

**Acquisition — DERIVED, from three anchors.**
1. **Quarterly shareholding pattern (F1)** gives an exact total share count every quarter — the
   strongest free anchor, available since the SHP disclosure regime (good depth from ~2001 on BSE,
   XBRL-structured from ~2011).
2. **Corporate actions (C1)** give exact multiplicative changes at ex-dates between anchors.
3. **Allotment filings** (Reg 30 / "Allotment of securities" announcements, G1) give the intra-quarter
   steps that (1) and (2) miss — QIPs, preferential issues, ESOP allotments, warrant conversions.
   Without these, an ESOP-heavy company's share count is a quarterly staircase and its per-share
   metrics are wrong every intra-quarter day.

Reconciliation rule: `SHP_total(q)` must equal `SHP_total(q-1) × Π(CA qty factors) + Σ(allotments)`.
Mismatches are a defect to quarantine, not to average away.

**Why needed.** Market cap = close × shares_outstanding. "Under ₹5,000cr" is a share-count question
before it is a price question.

**Failure mode.** Using today's share count with historical prices: for a company that has issued 3×
its shares since 2010, its 2010 market cap is overstated 3× — so a "smallcap" universe filter
systematically **excludes** the companies that later grew by issuing equity, and **includes** the ones
that shrank. That is a size-factor look-ahead nobody notices because market cap "feels" like price.

---

## E4. `market_cap_and_free_float`

**Definition.** One row per (isin, as_of_date): total and free-float market capitalisation and the
investable weight factor.

**Grain / PK.** `(isin, as_of_date)`.

**Attributes.** `market_cap` (Decimal INR = `close × shares_outstanding`, both as-of),
`free_float_shares` (Decimal), `free_float_market_cap` (Decimal INR),
`investable_weight_factor` (Decimal 0–1), `iwf_source` (enum: `NSE_INDEX_FILE`, `DERIVED_FROM_SHP`,
`VENDOR`), `size_bucket` (enum `LARGE`/`MID`/`SMALL`/`MICRO` — **SEBI's definition: top 100 by full
market cap = large, 101–250 = mid, 251+ = small, per AMFI's semi-annual list, and the AMFI list is
itself a dated, fetchable artefact**), `amfi_rank` (int, PIT).

**Derivation of free float.**
`free_float_shares = total_shares − promoter_and_promoter_group − government_strategic_holdings
                     − locked_in_shares − shares_held_by_associates/ESOP_trusts`
all taken from the SHP (F1) with `knowledge_ts <= cutoff`. NSE's own IWF is published only in current
index files and methodology notes, not as a history — so the derived version is what you will have for
2006–2020 and it must be validated against NSE's current IWF where they overlap.

**Volume.** Materialise at rebalance dates: ~2,000 × 240 = **480k rows**; daily is ~10M.

**Why needed.** Size-constrained mandates ("under ₹5,000cr"), index-relative weighting, capacity
(you cannot own 12% of a free float without triggering disclosure and destroying your exit), and the
SEBI large/mid/small buckets that Indian mandates are actually written in.

**Failure mode.** Free float computed from today's promoter stake mis-sizes every company whose
promoter sold down or pledged; capacity limits computed on total rather than free-float cap overstate
tradable size by 40–70% in promoter-heavy Indian smallcaps — which is most of the thematic universe.

---

## E5. `derived_ratio` — the computed fundamental metrics

**Definition.** One row per (issuer_id, as_of_date, metric). Materialised, versioned, and — critically
— carrying the **inputs' knowledge timestamps**, not just the output.

**Grain / PK.** `(issuer_id, as_of_date, metric_code, formula_version)`.

**Attributes.** `metric_code` (enum: `PE_TTM`, `PB`, `EV_EBITDA`, `ROCE`, `ROE`, `NET_DEBT_EBITDA`,
`INTEREST_COVERAGE`, `FCF_YIELD`, `SALES_CAGR_3Y`, `SALES_CAGR_5Y`, `EPS_CAGR_5Y`,
`GROSS_MARGIN`, `OPM`, `CASH_CONVERSION`, `ACCRUAL_RATIO`, `PIOTROSKI_F`, `ALTMAN_Z`,
`BENEISH_M`, `DIVIDEND_YIELD`, `PAYOUT_RATIO`, `WORKING_CAPITAL_DAYS`), `value` (Decimal),
`numerator_fact_ids` / `denominator_fact_ids` (uuid[] — traceability), `inputs_max_knowledge_ts`
(tz-aware — **must be ≤ as_of_date**; assert it), `formula_version` (str), `is_ttm` (bool),
`consolidation_used` (enum), `accounting_framework_span` (enum: `PURE_IGAAP`, `PURE_IND_AS`,
`STRADDLES_TRANSITION` — the comparability flag), `is_estimated` (bool).

**Volume.** 2,000 names × 240 rebalance dates × 25 metrics = **12M rows**; prune to held+candidate.

**Acquisition — DERIVED** from E1 + E3 + E4 + B1. **Never ingest a vendor's computed ratio as
primary** — you cannot audit its inputs, its consolidation choice, or its PIT-ness, and vendors
silently recompute history.

**Why needed.** "Quality compounders" is a ratio rule. **Failure mode.** Silent PIT violation via a
TTM window that includes an unfiled quarter; consolidation-mixing (standalone P&L over consolidated
equity) producing nonsense ROEs; and cross-framework ratio series that show a fake 2017 regime break.

---

## E6. `restatement_quarantine`

**Definition.** One row per detected restatement: the fact that a previously published number changed.
Per the repo's invariant that restated fundamentals are quarantined from backtests, this is a
first-class entity, not a log line.

**Grain / PK.** `(issuer_id, period_end, consolidation, line_item_code, from_revision, to_revision)`.

**Attributes.** `original_value`, `restated_value` (Decimal), `delta_pct` (Decimal),
`original_knowledge_ts`, `restatement_knowledge_ts` (tz-aware), `restatement_reason` (enum:
`ERROR_CORRECTION`, `ACCOUNTING_POLICY_CHANGE`, `IND_AS_TRANSITION`, `MERGER_RECAST`,
`REGULATORY_DIRECTION`, `UNKNOWN`), `materiality_flag` (bool), `quarantine_action` (enum:
`BACKTEST_USES_ORIGINAL`, `EXCLUDE_ISSUER_FROM_WINDOW`, `FLAG_ONLY`).

**Why needed.** Two separate uses: (a) enforce that backtests read the original; (b) **restatement
frequency is itself a governance signal** — companies that restate materially are companies to avoid,
and that signal is only available if you keep both versions.

**Failure mode.** Without it, the platform's "PIT" claim is unverifiable. The quarantine invariant
becomes a comment rather than a mechanism.

---

## E7. `governance_red_flag`

**Definition.** One row per (issuer_id, flag_type, observed_on) — a dated, machine-checkable
governance warning. An exclusion rail for every fund, and a mandatory one for a system that will
otherwise happily buy a fraud because its momentum is excellent.

**Grain / PK.** `(issuer_id, flag_type, observed_on)`.

**Attributes.** `flag_type` (enum: `AUDITOR_RESIGNATION`, `AUDITOR_QUALIFIED_OPINION`,
`GOING_CONCERN_DOUBT`, `DELAYED_FILING`, `CFO_EXIT`, `INDEPENDENT_DIRECTOR_RESIGNATION_WITH_CAUSE`,
`PROMOTER_PLEDGE_ABOVE_THRESHOLD`, `PLEDGE_INVOCATION`, `SEBI_ENFORCEMENT_ORDER`,
`NCLT_INSOLVENCY_ADMISSION`, `CREDIT_RATING_DEFAULT_OR_D`, `ASM_STAGE_ESCALATION`,
`GSM_STAGE`, `RELATED_PARTY_SPIKE`, `SUBSIDIARY_WRITE_OFF`, `SHORT_SELLER_REPORT`),
`severity` (enum `INFO`/`WARN`/`CRITICAL`), `observed_on` (date), `knowledge_ts`,
`source_doc_id`, `auto_exclude_days` (int — how long the exclusion persists).

**Acquisition — DERIVED from E2/F3/G4/G5 + FETCHED from SEBI orders and IBBI/NCLT filings.**
`https://www.sebi.gov.in/enforcement/orders`, `https://ibbi.gov.in/` (CIRP admissions, deep and
structured since 2017 — which is also the IBC's start, a hard depth boundary).

**Failure mode.** The single most reputationally destructive failure available: a thematic smallcap
fund that automatically buys momentum into a company that was already qualified by its auditor.

---

# FAMILY F — OWNERSHIP & FLOWS

## F1. `shareholding_pattern` — the quarterly ownership disclosure

**Definition.** One row per (isin, quarter_end, category, sub_category) from the LODR Reg 31 filing.
A row means "as at the end of quarter Q, category C held N shares (P% of total) in ISIN X, of which
L were locked in and D were in dematerialised form."

**Grain / PK.** `(isin, quarter_end, category_code, revision_no)`.

**Attributes.** `category_code` (enum, the SEBI Table I/II/III structure: `PROMOTER_INDIAN`,
`PROMOTER_FOREIGN`, `PUBLIC_INSTITUTIONS_MF`, `PUBLIC_INSTITUTIONS_FPI_FII`, `PUBLIC_INSTITUTIONS_BANKS`,
`PUBLIC_INSTITUTIONS_INSURANCE`, `PUBLIC_INSTITUTIONS_AIF`, `PUBLIC_INSTITUTIONS_NBFC`,
`CENTRAL_GOVT`, `STATE_GOVT`, `PUBLIC_NON_INSTITUTIONS_INDIVIDUAL_UPTO_2L`,
`PUBLIC_NON_INSTITUTIONS_INDIVIDUAL_ABOVE_2L`, `BODIES_CORPORATE`, `NRI`, `TRUSTS`, `CLEARING_MEMBERS`,
`EMPLOYEE_TRUSTS`, `CUSTODIAN_DR`, `NON_PROMOTER_NON_PUBLIC`),
`num_shareholders` (int), `shares_held` (Decimal), `pct_of_total` (Decimal),
`shares_pledged_or_encumbered` (Decimal), `pct_pledged_of_promoter_holding` (Decimal),
`shares_locked_in` (Decimal), `shares_in_demat` (Decimal),
`total_shares_outstanding` (Decimal — the anchor for E3),
`quarter_end` (date), `filing_date` (date, **PIT — up to 21 days after quarter end**),
`filing_ts` (tz-aware), `revision_no`, `source_id`, `l0_ref`.

Plus a child entity `shareholder_detail` for the named >1% holders:
`(isin, quarter_end, holder_name_raw, holder_category)` → `shares_held`, `pct`, `holder_pan_masked`,
`holder_canonical_id` (an entity-resolution output — "HDFC MUTUAL FUND A/C HDFC TOP 100" and
"HDFC MF - TOP 100 FUND" are the same holder and matching them is a real, non-trivial job).

**Frequency / volume.** Quarterly. ~5,000 companies × 80 quarters × ~20 categories = **8M rows**,
plus ~5,000 × 80 × ~15 named holders = **6M detail rows**.

**Acquisition — FETCHED.**
- **BSE** is the better source: XBRL-structured shareholding pattern since the first phase of BSE's
  XBRL programme, browsable per scrip at `https://www.bseindia.com/corporates/shpPromoterNGroup.aspx`
  and the XBRL info hub `https://www.bseindia.com/static/about/xbrl_info.aspx`. HTML/PDF filings run
  back to roughly **2001**; machine-readable XBRL is good from about **2011–2013**.
- **NSE**: `https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern`.
- **SEBI** hosts the prescribed formats; the format itself changed materially in **2015–2016**
  (the current Table I–VI structure, with pledge and locked-in as first-class fields). Pre-2015
  filings have a different, coarser schema — **two parser eras minimum, more likely four.**

**Why needed.** Free float (E4); promoter pledge (F3); "institutional validation" and
"promoter-skin-in-the-game" screens that many quality/thematic mandates use; and the FII/DII
*stock-level* ownership trend, which is a genuine signal and is only available here.

**Failure mode.** Two. (a) Using quarter-end date as knowledge date gives up to 21 days of foresight,
80 times over — and quarter-end SHP filings cluster with results, so the leak lands exactly on the
most price-sensitive days. (b) A promoter-stake series built from the latest filing (with the current
share count) mis-computes free float in every earlier period.

---

## F2. `insider_and_substantial_acquisition_disclosure`

**Definition.** One row per disclosed transaction by a designated person, promoter or substantial
acquirer. `(isin, disclosure_id)`, where a disclosure covers a transaction or series.

**Attributes.** `regulation` (enum: `PIT_REG_7_2` (continual disclosure by insiders),
`PIT_REG_7_1` (initial), `SAST_REG_29_1`/`29_2` (5% and 2%-change acquirer disclosures),
`SAST_REG_31` (encumbrance)), `person_name`, `person_category` (enum: `PROMOTER`, `KMP`, `DIRECTOR`,
`DESIGNATED_PERSON`, `IMMEDIATE_RELATIVE`, `ACQUIRER`), `transaction_type` (enum `BUY`, `SELL`,
`PLEDGE`, `REVOKE`, `INVOKE`, `ESOP_ALLOTMENT`, `GIFT`, `INTER_SE_TRANSFER`),
`shares` (Decimal), `value` (Decimal INR), `price_per_share` (Decimal),
`transaction_from_date`/`_to_date` (date), `mode` (enum `MARKET`, `OFF_MARKET`, `BLOCK`),
`holding_before_pct`, `holding_after_pct` (Decimal),
`disclosure_ts` (tz-aware, **PIT anchor — must be within 2 trading days of the trade under Reg 7**),
`l0_ref`.

**Volume.** ~40k–80k disclosures/yr in the recent era → **500k–1M rows** over 20y, heavily
back-loaded.

**Acquisition — FETCHED.** NSE `https://www.nseindia.com/companies-listing/corporate-filings-insider-trading`
and `.../corporate-filings-sast`, BSE equivalents. **Depth boundary: the current insider-disclosure
regime is SEBI (Prohibition of Insider Trading) Regulations, 2015 — structured filings are reliable
from 2015. The 1992 PIT Regulations and SAST 1997/2011 produced sparser, less structured
disclosure.** So 2006–2014 insider data is thin and 2006–2010 is close to unusable.

**Why needed.** Promoter buying is one of the few Indian signals with persistent published evidence;
promoter selling is a first-order risk flag for a concentrated thematic smallcap fund.

**Failure mode.** Backdating — using the *transaction* date rather than the *disclosure* date grants
up to 2 trading days of foresight on the highest-information trades in the market. That is a
look-ahead that will produce a beautiful and completely unrealisable backtest.

---

## F3. `promoter_pledge`

**Definition.** One row per (isin, as_of_date, pledgee) or, at minimum, one per (isin, quarter_end):
promoter shares encumbered. Kept separate from F1 because pledge is disclosed both quarterly (SHP)
and event-wise (SAST Reg 31), and the event stream is the one that matters.

**Attributes.** `pledged_shares` (Decimal), `pledged_pct_of_promoter_holding` (Decimal),
`pledged_pct_of_total_capital` (Decimal), `pledgee_name` (str, nullable),
`event_type` (enum `CREATION`, `RELEASE`, `INVOCATION`), `event_date`, `disclosure_ts`,
`is_invocation` (bool — the catastrophic one).

**Why needed.** A hard exclusion rail. Pledge invocation is the mechanism by which Indian smallcaps go
to zero in a fortnight, and it is *disclosed*, so a fund that owns one after the disclosure has no
excuse. **Failure mode:** the fund holds a name through a margin-call cascade the data warned about.

---

## F4. `bulk_and_block_deals`

**Definition.** One row per reported bulk or block deal: (exchange, trade_date, isin, client_name,
buy_sell, quantity, price).

**Grain / PK.** `(exchange, trade_date, isin, client_name, buy_sell, price, deal_type)`.

**Attributes.** `deal_type` (enum `BULK` (>0.5% of listed shares, disclosed post-close),
`BLOCK` (special window, min ₹10cr/₹5cr by era)), `client_name_raw` (str),
`client_canonical_id` (uuid — entity resolution again), `quantity` (Decimal),
`weighted_avg_price` (Decimal INR), `remarks`, `knowledge_ts` (= post-close publication on the
trade date; block deals are published same day).

**Volume.** ~15k–30k deals/yr → **300k–600k rows** over 20y.

**Acquisition — FETCHED, free, good depth.** NSE `https://www.nseindia.com/report-detail/display-bulk-and-block-deals`
with daily archive files; BSE equivalent. **The bulk-deal disclosure regime dates from SEBI's
January 2004 circular, so this entity genuinely covers the full 20-year window** — one of the few
that does.

**Why needed.** Smart-money tracking; and, more practically, **bulk deals contaminate ADTV**. A single
₹200cr block in a ₹3cr/day smallcap makes the 20-day mean ADTV say the name is liquid when it is not.
Capacity models must be able to *exclude* block volume — which requires knowing which volume was block.

**Failure mode.** Phantom liquidity via block-deal-inflated ADTV, and a fund that thinks it can build a
position because someone else once did a single negotiated trade.

---

## F5. `institutional_flow_aggregate`

**Definition.** One row per (date, participant_category, segment): market-wide FII/FPI and DII gross
buy, gross sell, net.

**Grain / PK.** `(trade_date, participant_category, segment, source)`.

**Attributes.** `participant_category` (enum `FII_FPI`, `DII`, `MUTUAL_FUND`, `INSURANCE`,
`PROPRIETARY`, `CLIENT`), `segment` (enum `CASH`, `INDEX_FUTURES`, `INDEX_OPTIONS`, `STOCK_FUTURES`,
`STOCK_OPTIONS`, `DEBT`), `gross_buy`, `gross_sell`, `net` (Decimal, INR crore — **watch the unit,
these files are in crore**), `open_interest_value` (Decimal, for the derivative rows).

**Volume.** ~250 days × ~15 category-segment combos × 20y = **75k rows**. Tiny.

**Acquisition — FETCHED, free.** NSE `https://www.nseindia.com/reports/fii-dii` (combined NSE+BSE+MSEI),
NSE participant-wise open interest and participant-wise volume daily archives, SEBI's FPI statistics,
and **NSDL FPI data at `https://www.fpi.nsdl.co.in/web/Reports/ReportSelect.aspx` — fortnightly and
monthly FPI *sector-wise* assets under custody, which is the closest thing to a thematic flow series
India publishes, with depth back to the mid-2000s.** Note the FII→FPI regime renaming in 2014.

**Why needed.** Regime/risk-on conditioning; a genuine cross-sectional signal at the sector level via
NSDL AUC; and a sanity check that a theme's inflow story is real.

**Failure mode.** Mild by comparison — mostly a wrong macro overlay. Unit errors (crore vs rupee) are
the practical hazard.

---

## F6. `mutual_fund_scheme_holdings`

**Definition.** One row per (scheme_code, portfolio_date, isin): what each Indian MF scheme held.

**Grain / PK.** `(amc_id, scheme_code, portfolio_date, isin)`.

**Attributes.** `scheme_name`, `scheme_category` (enum, per SEBI's 2017 scheme categorisation —
another regime boundary), `isin`, `quantity` (Decimal), `market_value` (Decimal INR),
`pct_of_nav` (Decimal), `portfolio_date` (date), `disclosure_date` (date, **PIT**),
`is_thematic_scheme` (bool).

**Volume.** ~1,500 equity schemes × ~60 holdings × 12 months × 8 years = **8.6M rows** for the
monthly era.

**Acquisition — FETCHED.** AMFI (`https://www.amfiindia.com/`) publishes NAVs daily with full history
and monthly portfolio disclosures; individual AMC sites host the portfolio files.
**⚠ Depth boundary: SEBI mandated *monthly* portfolio disclosure from October 2018; before that it was
half-yearly (and, for a period, monthly only for some categories).** So for 2006–2018 this entity is
at best semi-annual and format-chaotic (per-AMC Excel/PDF layouts, no standard schema).

**Why needed.** Two uses: (a) *competitive positioning* — knowing which names the domestic thematic
funds already crowd into is directly relevant to a multi-fund manager's capacity and exit risk;
(b) *benchmark of record* for a peer-relative performance claim.

**Verdict: IMPROVES-FIDELITY.** Do not make a core rule depend on it — the pre-2018 gap is fatal to a
20-year backtest of any rule that uses it.

**Failure mode.** Crowding blindness: six of your thematic funds and forty external schemes all own
the same eight defence smallcaps, and the correlated-exit scenario is invisible in a backtest that
models only your own funds.

---

# FAMILY G — EVENTS & NARRATIVE

The hard requirement across this family: **timestamps must be publication-accurate to the minute, or
the entity is not backtestable.** A news-driven or announcement-driven thematic engine whose events
are dated to the day is trading on an average of half a day of foresight, and in the Indian market a
half-day of foresight on an order-win announcement is worth more than the entire strategy.

## G1. `exchange_announcement` — the corporate filings stream

**Definition.** One row per filing submitted by a listed company to an exchange under LODR Reg 30 and
its neighbours. This is the **spine of the narrative system** and the only PIT-honest, deep, free
event source India has.

**Grain / PK.** `(exchange, announcement_id)` — plus a `dedupe_key` because the same event is filed to
both NSE and BSE minutes apart.

**Attributes.**

| field | type | null | PIT | notes |
|---|---|---|---|---|
| `announcement_id` | str | no | | exchange-assigned |
| `exchange` | enum | no | | `NSE`/`BSE` |
| `isin`, `symbol_at_filing`, `scrip_code` | | no | | resolve via A3 |
| `submitted_ts` | tz-aware dt | no | **PIT ANCHOR** | company's submission time |
| `disseminated_ts` | tz-aware dt | yes | **PIT ANCHOR** | exchange's publication time — **use this one** |
| `is_after_hours` | bool | no | derived | disseminated outside 09:15–15:30 IST |
| `category` | enum | no | | `BOARD_MEETING_INTIMATION`, `BOARD_MEETING_OUTCOME`, `FINANCIAL_RESULTS`, `ORDER_WIN_CONTRACT`, `ACQUISITION`, `DIVESTMENT`, `CAPACITY_EXPANSION`, `CREDIT_RATING`, `CHANGE_IN_MANAGEMENT`, `AUDITOR_CHANGE`, `LITIGATION_REGULATORY`, `ALLOTMENT_OF_SECURITIES`, `INVESTOR_PRESENTATION`, `EARNINGS_CALL_TRANSCRIPT`, `CLARIFICATION_ON_PRICE_MOVEMENT`, `SCHEME_OF_ARRANGEMENT`, `DISRUPTION_OF_OPERATIONS`, `INSOLVENCY`, `OTHER` |
| `subject`, `body_text` | str | no | | |
| `attachment_url`, `attachment_sha256`, `attachment_text` | str | yes | | the PDF, extracted |
| `is_price_sensitive` | bool | yes | | exchange flag where present |
| `nlp_extractions` | jsonb (validated model) | yes | | order value, counterparty, capacity, theme tags, sentiment — each with `extractor_version` |
| `l0_ref` | str | no | | |

**Frequency / volume.** Recent years run to **~200,000–400,000 announcements per year across NSE+BSE**
(the Reg 30 regime broadened materially in 2015 and again in 2023). 2006–2010 is an order of magnitude
lower. Realistic 20-year total: **2.5M–4M announcements**, plus several TB of attachment PDFs (L0).
This is the largest storage line item in the whole catalog after intraday bars.

**Acquisition — FETCHED, free, but operationally the hardest campaign.**
- NSE: `https://www.nseindia.com/companies-listing/corporate-filings-announcements` and its backing
  JSON API (date-range windowed, UA/cookie priming, aggressive rate limiting).
- BSE: `https://www.bseindia.com/corporates/ann.html` and its API — **BSE's archive is deeper and its
  timestamps are good**; BSE covers the BSE-only universe that NSE never sees.
- Depth: BSE announcements are retrievable to roughly the mid-2000s; NSE's public API window is
  shallower and the further back you go the more you rely on BSE.
- Rate limits: unpublished but real; both sites 403 or tarpit on aggressive fetching. A 20-year
  day-by-day sweep is a **multi-week single-threaded campaign** — precisely the sort of thing the
  repo's one-request-budget-per-host rule exists to serialise.
- Licensing: public regulatory filings, free to read; redistribution restricted. Internal use fine.

**Why needed.** Everything narrative: theme evidence with honest dates (D6), order-win momentum for
defence/infra themes, red flags (E7), the announcement timestamp for corporate actions (C1), the
allotment steps for share count (E3).

**Failure mode.** Using the *date* rather than the *dissemination timestamp* is a half-day look-ahead
on the market's most price-sensitive information. And because thematic strategies are precisely
announcement-driven, this error does not average out — it *is* the strategy's apparent alpha.

---

## G2. `results_calendar`

**Definition.** One row per (issuer, expected/actual board meeting for results): the intimation and
the event. `(issuer_id, board_meeting_date, purpose)`.
Attributes: `intimation_ts` (tz-aware — companies must intimate ≥2/5 working days ahead),
`board_meeting_date`, `purpose` (enum `RESULTS`, `DIVIDEND`, `FUND_RAISING`, `BUYBACK`, `SPLIT`,
`BONUS`, `OTHER`), `is_confirmed`, `rescheduled_from` (date, nullable), `actual_result_ts`.
**Source:** derived from G1 `BOARD_MEETING_INTIMATION`; NSE/BSE also publish a board-meetings report.
**Why needed:** event-risk rails ("do not initiate a position within N days of results"), and the
intimation itself is a dated fact. **Failure mode:** rebalancing blind into an earnings event and
attributing the resulting variance to the theme.

---

## G3. `credit_rating_action`

**Definition.** One row per rating action on an issuer or instrument.
`(issuer_id, agency, instrument_class, action_date)`.
Attributes: `agency` (enum `CRISIL`, `ICRA`, `CARE`, `INDIA_RATINGS`, `BRICKWORK`, `ACUITE`,
`INFOMERICS`), `instrument_class` (enum `LONG_TERM`, `SHORT_TERM`, `NCD`, `BANK_FACILITIES`,
`FIXED_DEPOSIT`), `rating_before`, `rating_after` (str, e.g. `CRISIL A+/Stable`),
`rating_scale_numeric` (int — a canonical mapping so agencies are comparable),
`outlook` (enum `POSITIVE`/`STABLE`/`NEGATIVE`/`WATCH_DEVELOPING`), `action_type` (enum `ASSIGNED`,
`UPGRADE`, `DOWNGRADE`, `REAFFIRMED`, `WITHDRAWN`, `SUSPENDED`, `DEFAULT`, `ISSUER_NOT_COOPERATING`),
`rationale_text`, `press_release_url`, `knowledge_ts` (publication of the press release).

**Acquisition — FETCHED.** Agency websites host every rating rationale as a dated press release
(crisilratings.com, icra.in, careratings.com, indiaratings.co.in); SEBI requires them to be public and
maintained. **Depth: good from ~2010, excellent from ~2016; sparse before.** Also mirrored into G1
because Reg 30 requires the company to disclose rating changes to the exchange — **use the exchange
copy as the PIT anchor** since that is when the market saw it.

**Why needed.** Downgrades and especially `ISSUER_NOT_COOPERATING` are among the highest-precision
distress signals in the Indian market, and they lead price. An exclusion rail for every fund.

**Failure mode.** Holding into a credit event that was publicly flagged; and, if the rating is joined
on the agency's publication rather than the exchange dissemination, a small but real look-ahead.

---

## G4. `regulatory_action`

**Definition.** One row per SEBI/exchange/other-regulator action naming an issuer or its officers.
`(regulator, order_ref, issuer_id)`.
Attributes: `regulator` (enum `SEBI`, `NSE`, `BSE`, `RBI`, `NCLT`, `SFIO`, `IT_DEPT`, `ED`, `CCI`),
`action_type` (enum `SHOW_CAUSE`, `INTERIM_ORDER`, `FINAL_ORDER`, `PENALTY`, `DEBARMENT`,
`SETTLEMENT_CONSENT`, `INVESTIGATION_DISCLOSED`, `INSOLVENCY_ADMISSION`, `TRADING_BAN`),
`order_date`, `publication_ts`, `penalty_amount` (Decimal INR), `persons_named` (str[]),
`summary_text`, `order_url`.
**Source:** `https://www.sebi.gov.in/enforcement/orders`, `https://www.sebi.gov.in/enforcement/orders/adjudication`,
NCLT/IBBI (`https://ibbi.gov.in/`, structured CIRP data from 2017), exchange penalty circulars.
**Depth:** SEBI orders are online with good depth (mid-2000s onward), unstructured PDFs throughout.
**Failure mode:** owning a company under a trading ban; and the reputational failure of an
"automatic" manager that had the order in its lake and ignored it.

---

## G5. `surveillance_list_membership` — ASM / GSM / other market-structure flags

**Definition.** One row per (isin, framework, stage, effective interval). Surveillance placement
changes margin (up to 100%), caps price bands (down to 2%), can move a name to trade-to-trade or to
periodic call auction, and in GSM's higher stages effectively **eliminates tradability**.

**Grain / PK.** `(isin, framework, effective_from)`.

**Attributes.** `framework` (enum `ASM_SHORT_TERM`, `ASM_LONG_TERM`, `GSM`, `TRADE_TO_TRADE`,
`PERIODIC_CALL_AUCTION`, `IBC_SURVEILLANCE`, `ESM` (enhanced surveillance for microcaps, 2023+)),
`stage` (int 0–4), `margin_pct` (Decimal), `price_band_pct` (Decimal), `is_intraday_banned` (bool),
`effective_from`/`effective_to` (date), `announcement_date` (date), `circular_ref`.

**⚠ Depth boundary — a real one.** **GSM was introduced by SEBI/exchanges in March 2017; ASM came into
force on 2018-03-26; ESM in 2023.** These frameworks *did not exist* for the first 11 years of a
2006–2026 backtest. Consequences you must state in every backtest report: (a) pre-2017 there is no
surveillance flag to apply, so pre-2017 smallcap tradability is *systematically overstated* relative
to today's reality; (b) any strategy whose live behaviour depends on avoiding ASM names cannot be
validated on pre-2017 data. This asymmetry is not fixable — it must be **disclosed as a
regime-coverage gap**, and the walk-forward windows should be reported separately either side of it.

**Acquisition — FETCHED.** NSE `https://www.nseindia.com/reports/asm` and
`https://www.nseindia.com/static/regulations/graded-surveillance-measure`; BSE equivalents; daily
list files. **These are published as daily snapshots with no archive** — if you are not snapshotting
them daily, the history is gone. Start now.

**Why needed.** Tradability. An ASM Stage-4 name with 100% margin and a 2% band is not investable at
scale, and a thematic smallcap fund's holdings enter ASM *often*.

**Failure mode.** Phantom liquidity and phantom exits, concentrated in the exact names a momentum-led
theme fund accumulates.

---

## G6. `news_item` — general news with publication timestamps

**Definition.** One row per news article: `(source, article_id)` with entity links to issuers/themes.
Attributes: `published_ts` (tz-aware, **must be the publisher's timestamp, not the crawl time**),
`first_seen_ts` (our crawl — kept separately and never used as PIT),
`headline`, `body`, `url`, `publisher`, `language`, `linked_issuer_ids` (uuid[], with
`linkage_confidence`), `linked_theme_ids`, `sentiment` (Decimal, with `model_version`),
`is_syndicated_duplicate` (bool), `dedupe_cluster_id`.

**Volume.** Unbounded. A realistic Indian-market feed is 5k–20k articles/day → **tens of millions**
over 20y.

**Acquisition — FETCHED, and this is the entity with the weakest 20-year story in the catalog.**
- **GDELT** (`https://www.gdeltproject.org/`) — free, global, event and GKG streams; the full
  2.0 stream with 15-minute granularity dates from **February 2015**; the 1.0 event database goes back
  to 1979 but is event-coded, English-biased and links to URLs that are largely dead. Coverage of
  Indian business press pre-2015 is poor.
- **Common Crawl** (`https://commoncrawl.org/`) — archives from 2008, but it is a *web crawl*, not a
  news archive: publication timestamps must be extracted from the page and are unreliable.
- **Paid**: LSEG/Refinitiv News Archive, Dow Jones Factiva, Bloomberg — these have real depth and real
  timestamps and real money attached (Factiva's Indian regional coverage is the best available).
- **Free and honest**: **the exchange announcement stream (G1) is itself the news for the purposes
  that matter.** For an Indian listed company, price-sensitive information is legally required to
  reach the exchange first.

**Verdict.** **G1 is MUST-HAVE; G6 is NICE-TO-HAVE, and any rule that depends on G6 must be marked
"not backtestable before 2015".** Do not let a news-sentiment feature into a 20-year qualification
result; it will silently be backtested on a period where the data does not exist and the missingness
will be non-random.

**Failure mode.** Crawl-time-as-publication-time is the classic. A crawler that discovers an article
at 09:00 on day D+1 and stamps it D+1 09:00 is *late* (harmless); one that parses "yesterday" into
D 00:00 is *early by a full trading session* (fatal). Also survivorship in news: dead companies' news
disappears from the web, so a news-based feature is measured only on survivors.

---

# FAMILY H — MACRO & BENCHMARKS

## H1. `index_level` — price, total-return and net-total-return index series

**Definition.** One row per (index_id, trade_date, variant). The benchmark of record.

**Grain / PK.** `(index_id, trade_date, variant)`.

**Attributes.** `variant` (enum `PR` (price return), `TRI` (total return, gross dividends reinvested),
`NTR` (net of withholding tax)), `open`, `high`, `low`, `close` (Decimal),
`pe`, `pb`, `div_yield` (Decimal — NSE publishes these daily per index, and they are a genuinely
useful macro-valuation series), `is_back_computed` (bool — **true for any date before the index's
`live_from`**).

**Volume.** ~60 indices × 3 variants × 5,000 days = **~900k rows**. Trivial.

**Acquisition — FETCHED, free, and this one genuinely has 20-year depth.**
`https://www.niftyindices.com/reports/historical-data` publishes daily values including TRI and NTR
for every Nifty index from inception; BSE publishes its own at `https://www.bseindia.com/indices/`.
NSE also publishes daily index P/E, P/B and dividend yield archives.

**Why needed.** **Benchmarking must be against TRI, not PR.** The Nifty 50's dividend yield has
averaged roughly 1.2–1.5% over the window; benchmarking a fund's total return against the price index
awards the manager ~1.3%/yr of free, fictitious alpha — over 20 years, roughly **30% of cumulative
outperformance that does not exist**. This is the single most common benchmarking error in Indian
performance reporting and the system must make it structurally impossible (make `variant` non-nullable
and default nothing).

**Failure mode.** Fake alpha, uniformly, in every fund, forever.

---

## H2. `risk_free_rate_curve`

**Definition.** One row per (curve_date, tenor): the risk-free yield used for Sharpe/Sortino, for
discounting, and for the cash-drag model.

**Grain / PK.** `(curve_date, tenor_days, instrument)`.

**Attributes.** `instrument` (enum `TBILL_91D`, `TBILL_182D`, `TBILL_364D`, `GSEC_ZCYC`,
`MIBOR_OVERNIGHT`, `REPO_RATE`, `LIQUID_FUND_PROXY`), `yield_pct` (Decimal, annualised),
`compounding` (enum), `source`.

**Acquisition — FETCHED, free, full depth.** RBI's **Database on Indian Economy**
(`https://data.rbi.org.in/DBIE/` and `https://dbie.rbihub.in/`) publishes 91/182/364-day T-bill
auction cut-off yields weekly with decades of history, plus policy rates, and CCIL/FBIL publish the
G-sec zero-coupon yield curve and MIBOR.
**Quirk:** 91-day T-bills are auctioned weekly, so the series is weekly, not daily — interpolate
explicitly and record the interpolation method rather than forward-filling silently.

**Why needed.** Every risk-adjusted metric. Also, for a fund holding 3–8% cash, the cash return over
20 years at 5–7% is a *material* part of total return — modelling cash at zero understates the fund
and, worse, understates it inconsistently across rate regimes (2008's 8% vs 2021's 3.4%).

**Failure mode.** Sharpe ratios computed against a constant assumed rate are wrong in both directions
across the window; a strategy that holds more cash in high-rate periods gets penalised for it.

---

## H3. `macro_series` — the general macro fact table

**Definition.** One row per (series_id, observation_date, vintage). **Vintage is mandatory**: macro
data is revised, and CPI/IIP/GDP first prints differ materially from the final figures.

**Grain / PK.** `(series_id, observation_period_end, release_ts)`.

**Attributes.** `series_id` (str), `value` (Decimal), `unit`, `frequency` (enum `D`/`W`/`M`/`Q`/`A`),
`observation_period_end` (date), `release_ts` (tz-aware — **PIT anchor**), `is_revision` (bool),
`revision_of_release_ts`, `seasonal_adjustment` (enum), `base_year` (str), `source`.

**Series to carry (minimum).** CPI (combined, and rural/urban), CPI food, WPI, IIP (headline and
manufacturing/mining/electricity), GDP/GVA, repo rate, 10Y G-sec yield, USD/INR, credit growth,
GST collections, PMI (manufacturing & services), IIP-capital-goods, and the trade balance.

**⚠ Base-year and definitional breaks that a 20-year backtest will hit.** The current CPI (Combined)
series with **base 2012 starts in January 2011** — before that there was no unified CPI, only CPI-IW,
CPI-AL and CPI-RL on different baskets. WPI's base changed (2004-05 → 2011-12). IIP's base changed
(2004-05 → 2011-12). GDP was rebased to 2011-12 in 2015 with a methodology change that shifted the
level. **A macro conditioning rule calibrated on post-2012 data cannot be honestly backtested to 2006
without an explicit splice, and the splice must be a documented, versioned artefact.**

**Acquisition — FETCHED, free.** RBI DBIE (`https://data.rbi.org.in/DBIE/`), MOSPI
(`https://www.mospi.gov.in/`, CPI and IIP releases), Office of the Economic Adviser for WPI
(`https://eaindustry.nic.in/`), GST Council/PIB for collections, IMF/World Bank as cross-checks.
FX: RBI reference rates daily, deep history.

**Why needed.** Regime conditioning; and for macro-linked themes, directly (see H5).

**Failure mode.** *Macro look-ahead by revision* — using the final revised IIP for a 2011 decision when
the first print (published 6 weeks later, and later revised by 2 percentage points) is what was
knowable. And *base-year splicing* that manufactures a spurious level shift the strategy learns to
trade.

---

## H4. `fx_and_commodity`

**Definition.** One row per (instrument, quote_date): FX rates and commodity prices relevant to
sector/theme exposure.
Attributes: `instrument` (enum/str: `USDINR`, `EURINR`, `BRENT`, `WTI`, `GOLD_INR_10G`,
`STEEL_HRC_INDIA`, `COPPER_LME`, `ALUMINIUM_LME`, `THERMAL_COAL`, `NATURAL_GAS`, `UREA`,
`PALM_OIL`, `SUGAR`, `COTTON`, `CEMENT_PRICE_INDEX`), `price` (Decimal), `currency`, `unit`,
`source`, `is_spot_or_future` (enum).
**Sources:** RBI reference rates (FX, deep, free); **World Bank Pink Sheet**
(`https://www.worldbank.org/en/research/commodity-markets`) — monthly commodity prices back to 1960,
free, CSV, excellent for a 20-year backtest; MCX for Indian commodity futures; DGCIS for trade.
**Why needed.** Input-cost exposure is a core theme mechanic (a cement theme is a coal-and-power
theme; a chemicals theme is a crude theme). **Failure mode:** monthly commodity data applied at daily
frequency with forward-fill creates a stale-signal artefact; and Pink Sheet observations are published
with a lag, so `release_ts` matters here too.

## H5. `theme_exogenous_driver` — the macro series a *specific theme* depends on

**Definition.** One row per (theme_id, series_id, relationship). The explicit, declared link between a
theme and the exogenous data that drives it — so that a "monsoon-linked rural consumption" fund has a
*data* dependency, not a vibe.

**Attributes.** `theme_id`, `series_id`, `expected_sign` (enum `POSITIVE`/`NEGATIVE`),
`lag_days` (int), `rationale_text`, `added_on`.

**Concrete driver sources this product will actually need:**
- **Monsoon/rural**: IMD rainfall — subdivision- and district-wise daily/weekly rainfall and departure
  from LPA (`https://mausam.imd.gov.in/`, `https://www.imdpune.gov.in/` for gridded and historical
  datasets; free, registration required for bulk, and the file formats are archaic fixed-width);
  **weekly crop sowing area** from the Ministry of Agriculture (`https://agriwelfare.gov.in/`);
  reservoir storage from CWC (`http://cwc.gov.in/`, weekly, ~20-year depth); MSP announcements;
  MGNREGA demand (`https://nrega.nic.in/`, monthly, deep); tractor and two-wheeler sales
  (SIAM/FADA/**Vahan** `https://vahan.parivahan.gov.in/vahan4dashboard/` — Vahan gives registration
  data at district granularity but only from ~2018 nationally).
- **Defence**: Ministry of Defence capital-acquisition budget (Union Budget documents, deep, annual),
  DAC approval press releases (PIB `https://www.pib.gov.in/`, dated, free, and the highest-signal
  dated evidence source for the defence theme), defence export figures, and the order-win
  announcements in G1.
- **Power/energy**: **Grid India / POSOCO daily generation reports** (`https://grid-india.in/`, daily
  since ~2012), CEA monthly generation (`https://cea.nic.in/`, deep).
- **Infrastructure**: NHAI awarding/construction km, cement dispatches, e-way bill volumes (2018+).

**Why needed.** Without them, a "monsoon-linked" fund is a keyword filter wearing a macro costume, and
its backtest cannot show that the thesis mechanism ever operated.
**Failure mode.** Theme-thesis unfalsifiability; and, for any driver series that starts mid-window
(Vahan 2018, e-way bills 2018, Grid India 2012), a rule that silently cannot be backtested at all in
the early period.

## H6. `factor_return_series`

**Definition.** One row per (factor, date, region): daily/monthly factor returns for attribution.
Attributes: `factor` (enum `MKT_RF`, `SMB`, `HML`, `WML_MOMENTUM`, `RMW`, `CMA`, `LOW_VOL`,
`QUALITY`), `return_pct` (Decimal), `frequency`, `construction_source`.
**Source — free and genuinely good:** **IIM Ahmedabad's Indian Fama-French and Momentum data library**
(`https://faculty.iima.ac.in/~iffm/Indian-Fama-French-Momentum/`) — daily and monthly factor returns
for the Indian market from **1993**, maintained, free for research. This is the correct benchmark for
"is this theme fund just a smallcap-momentum bet?"
**Why needed.** A thematic fund's return decomposes into market + size + momentum + theme. If the
theme term is not significantly positive, the fund is an expensive smallcap-momentum tracker and
should be reported as such. **Verdict: IMPROVES-FIDELITY, strongly recommended** — it is the cheapest
available honesty check on the entire product.

---

# FAMILY I — COSTS, FRICTIONS & TAX

The design rule the repo already asserts — **one shared cost model, all `Decimal`** — has a data
consequence that is usually missed: **every rate in the cost model is a time series, not a constant.**
Twenty years of Indian equity investing spans a service-tax→GST transition, a state-wise→uniform stamp
duty transition, at least three STT regimes, an LTCG exemption that ended and then changed rate, a
dividend-tax regime inversion and a buyback-tax regime inversion. A cost model with hard-coded 2026
rates will misprice the entire 2006–2020 half of the backtest.

## I1. `charge_schedule` — the effective-dated rate table

**Definition.** One row per (charge_type, applicability, effective_from) giving the rate or fixed
amount in force. This is a *fetched, curated, versioned regulatory dataset* and it deserves the same
provenance rigour as price data: each row cites the circular or Finance Act section that created it.

**Grain / PK.** `(charge_type, exchange, segment, side, applicability_key, effective_from)`.

**Attributes.** `charge_type` (enum: `BROKERAGE`, `STT`, `EXCHANGE_TXN_CHARGE`, `SEBI_TURNOVER_FEE`,
`STAMP_DUTY`, `GST_OR_SERVICE_TAX`, `DP_CHARGE`, `CLEARING_MEMBER_CHARGE`, `AUCTION_PENALTY`,
`IPFT`), `basis` (enum `PCT_OF_TURNOVER`, `PCT_OF_PREMIUM`, `FLAT_PER_ORDER`, `FLAT_PER_SCRIP_PER_DAY`,
`PCT_OF_OTHER_CHARGES`), `rate` (Decimal), `cap_amount` (Decimal, nullable — several Indian charges
are capped), `floor_amount` (Decimal, nullable), `side` (enum `BUY`/`SELL`/`BOTH`),
`applicability_key` (str: `DELIVERY`, `INTRADAY`, `EQ_SERIES`, `T2T`, or a state code for pre-2020
stamp duty), `effective_from`/`effective_to` (date), `legal_ref` (str — Finance Act section, SEBI
circular number, exchange circular number), `source_url`, `knowledge_ts`.

**The regime timeline the table must encode (verified anchors; each row still needs its citation
pulled from the primary source before use).**

| Charge | Regime facts to encode |
|---|---|
| **STT** | Introduced **2004-10-01** (Finance (No.2) Act 2004). Delivery equity was **0.125% each side** through the late-2000s, cut to **0.1% each side with effect from 2012-07-01**, and has stayed 0.1% since. Derivative rates moved separately: futures-sell and options-sell rates changed in 2013, 2016, **2023-04-01** (options sale 0.05%→0.0625%, futures 0.01%→0.0125%) and **2024-10-01** (options 0.0625%→0.1% of premium, futures 0.0125%→0.02%). **⚠ Secondary sources disagree on the exact 2004→2006 step sequence (0.075% vs 0.1% vs 0.125%). Source the 2004–2012 rows from the Finance Act texts / CBDT notifications, not from broker blogs.** |
| **Stamp duty** | Two regimes. **Before 2020-07-01**: levied by the *state of the client*, rates and caps varying by state (Maharashtra's ~0.01% on delivery with a per-contract-note cap was the common case for a Mumbai-domiciled entity). **From 2020-07-01** (Finance Act 2019 amendments to the Indian Stamp Act, 1899, notified via PIB release PRID 1635399): a **uniform, nationwide** rate collected at one point by the exchange/clearing corporation/depository — **0.015% on delivery transfer, 0.003% on intraday, buy side only**. A fund domiciled in one state makes the pre-2020 half tractable: you need **one** state's historical schedule, not thirty-six. |
| **GST / Service tax** | 18% GST on (brokerage + exchange charges + SEBI fee) from **2017-07-01**. Before that, service tax with cesses, stepping roughly: 10.3% (2009–2012) → 12.36% (2012–2015) → 14% (2015-06) → 14.5% (2015-11, Swachh Bharat cess) → 15% (2016-06, Krishi Kalyan cess) → 18% GST (2017-07). |
| **Exchange transaction charge** | NSE cash-market charge has stepped down over the window (0.00335% → 0.00325% → **0.00297% from 2024-10-01**); BSE differs by group and has had large historical differences for the X/XT groups. Source: NSE/BSE circular archives. |
| **SEBI turnover fee** | Currently ₹10 per crore (0.0001%) of turnover; it has been revised more than once over the window. Source: SEBI circulars. |
| **DP charge** | A flat per-scrip-per-sell-day debit (order of ₹13–₹25 + GST depending on DP and era), levied by the depository participant, **not** by the exchange. It is *per scrip per day*, which means it is a **fixed cost per name sold** — brutal for a small fund with many small positions, and completely invisible in a percentage-based cost model. |
| **IPFT / auction penalty** | Investor protection fund charges; short-delivery auction penalties (up to 20% + close-out). Matters only if the simulation ever short-delivers. |

**Acquisition — FETCHED, curated by hand, small.** Primary sources: `https://www.incometaxindia.gov.in/`
(Finance Acts), `https://www.sebi.gov.in/legal/circulars`, NSE/BSE circular archives, PIB releases,
`https://www.pib.gov.in/PressReleasePage.aspx?PRID=1635399` (stamp duty implementation),
and — as a **cross-check only, never a citation** — broker rate pages such as Zerodha's charge list
and its STT article. **Volume: a few hundred rows. This is the highest value-per-row table in the
entire catalog.**

**Why needed.** A thematic smallcap fund rebalancing monthly across 40 names turns over aggressively;
at 20–40 bps round-trip all-in, the cost drag is 2–5%/yr. Getting the *era* wrong systematically
mis-states half the backtest.

**Failure mode.** Cost understatement, which is the second-most-common source of fake backtest alpha
after look-ahead — and unlike look-ahead it is *monotonic*: it always flatters, and it flatters the
high-turnover strategies most, which are exactly the ones an automatic manager will select.

---

## I2. `tax_regime_schedule`

**Definition.** One row per (tax_type, taxpayer_class, effective_from) giving the rate, threshold and
computational rule. Separate from I1 because these apply at *fund* level on realised gains and income,
not per trade.

**Grain / PK.** `(tax_type, taxpayer_class, effective_from)`.

**Attributes.** `tax_type` (enum `STCG_111A`, `LTCG_112A`, `LTCG_EXEMPT_10_38`, `DIVIDEND`,
`BUYBACK_115QA`, `BUYBACK_DEEMED_DIVIDEND`, `SURCHARGE`, `CESS`), `taxpayer_class` (enum
`RESIDENT_INDIVIDUAL`, `HUF`, `DOMESTIC_COMPANY`, `AIF_CAT3`, `FPI`, `MUTUAL_FUND_PASS_THROUGH`),
`rate` (Decimal), `exemption_threshold` (Decimal INR), `holding_period_days` (int),
`grandfather_reference_date` (date, nullable), `grandfather_method` (enum),
`effective_from`, `legal_ref`.

**The regime timeline (verified):**

| Period | Equity STCG | Equity LTCG | Dividends |
|---|---|---|---|
| pre-2004-10-01 | slab | 10% w/o indexation or 20% with | DDT era, exempt to investor |
| 2004-10-01 → 2008-03-31 | **10%** (s.111A) | **exempt** (s.10(38), STT paid) | exempt (DDT paid by company) |
| 2008-04-01 → 2018-03-31 | **15%** | **exempt** | exempt; **s.115BBDA 10% above ₹10L for resident individuals from FY2016-17** |
| 2018-04-01 → 2024-07-22 | 15% | **10% above ₹1L** (s.112A), **grandfathered to FMV as on 2018-01-31** | exempt until 2020-03-31 |
| 2020-04-01 → | — | — | **taxable in recipient's hands at slab, 10% TDS above ₹5,000 (s.194)** |
| **2024-07-23 →** | **20%** | **12.5% above ₹1.25L** | as above |
| buybacks to 2024-09-30 | company pays 20% u/s 115QA; receipt exempt to shareholder | | |
| **buybacks from 2024-10-01** | **entire consideration is a deemed dividend u/s 2(22)(f), taxed at slab; cost allowed as a capital loss** | | |

Plus surcharge and cess layered on top (4% health & education cess from FY2018-19, 3% education cess
before; surcharge slabs changing repeatedly), and the **capping of surcharge on capital gains at 15%**.

**Why needed.** A backtest that reports pre-tax returns for a 20-year Indian equity strategy is
reporting a number no investor can receive, and the *ranking* between strategies changes once tax is
applied: a 12-month-plus holding strategy taxed at 12.5% beats a 3-month strategy taxed at 20% by far
more than their pre-tax spread suggests. **For a fund manager product this is not optional — turnover
is a policy choice and its tax cost is the main argument against it.**

**Failure mode.** Systematic over-ranking of high-turnover strategies; and a hard-coded rate that is
correct for 2026 and wrong for 18 of the 20 years — in particular, applying today's 12.5% LTCG to
2010, when equity LTCG was **exempt**, penalises the early period and distorts every walk-forward
comparison across the 2018 and 2024 boundaries.

---

## I3. `slippage_and_impact_model_config`

**Definition.** One row per (model_version, parameter). The versioned parameters of the execution-cost
model, stored as data so a backtest's assumptions are auditable and comparable across runs.

**Attributes.** `model_version` (str), `parameter_name` (str), `parameter_value` (Decimal),
`applicable_liquidity_tier` (enum), `calibration_sample_start`/`_end` (date),
`calibration_r_squared` (Decimal), `notes`.

**The model this feeds (state it explicitly, and journal the version):**
```
cost_bps = half_spread_bps
         + k * sigma_daily_bps * sqrt(participation_rate)     # square-root impact law
         + fixed_bps(tier)
where participation_rate = order_qty / expected_delivery_volume_that_day
      sigma_daily_bps    = trailing 20d realised vol
      k                  = calibrated per liquidity tier (and >1.5x for T2T/ASM names)
```
**Calibration input:** the only honest calibration is against the fund's own live fills (J8) once they
exist. Until then, `k` is an assumption and every backtest result must carry a sensitivity band across
plausible `k`. **Failure mode:** a single unversioned magic number that quietly makes microcap themes
look tradable.

---

## I4. `borrow_and_short_availability` (only if shorting is in scope)

**Definition.** One row per (isin, date): whether the name was shortable and at what cost.
Attributes: `sblm_available_qty` (Decimal), `sblm_lending_fee_pct` (Decimal),
`is_in_fno` (bool — in India, cash-market shorting is intraday-only, so real shorting means **stock
futures**, which exist only for the ~180–230 names in the F&O list at any time),
`fno_ban_period_flag` (bool — names cross 95% of market-wide position limit and are banned from fresh
positions; a daily published list), `single_stock_futures_basis` (Decimal).
**Sources:** NSE SLB (securities lending & borrowing) reports under `https://www.nseindia.com/all-reports`;
the **F&O list membership history** (which is itself an effective-dated entity — names enter and exit
the derivative segment on eligibility reviews); the daily F&O ban list.
**Verdict:** MUST-HAVE **if and only if** any fund mandate permits shorting or hedging. For long-only
thematic funds, NICE-TO-HAVE — but the `is_in_fno` flag is worth carrying regardless, because F&O
membership is the cleanest available proxy for "institutionally tradable" and it removes the price band.
**Failure mode:** backtesting a hedge or a short leg on names that could not be shorted, or during a
ban period — pure fiction.

---

# FAMILY J — FUND-LEVEL & MULTI-FUND ENTITIES

Everything above is *market* data — shared, fetched, expensive. This family is *our own* data:
generated, cheap, and the part that actually makes this a multi-fund manager rather than a screener.
The governing requirement is the repo's own invariant — **one decision path for paper and real** —
which means these entities must be identical in a backtest, in paper trading and in live operation.
The only thing that changes is the clock and the broker.

## J1. `fund_definition`

**Definition.** One row per (fund_id, version). The mandate. Versioned because mandates change, and a
performance record spanning a mandate change must be able to say so.

**Grain / PK.** `(fund_id, version)`.

**Attributes.** `fund_id` (uuid), `version` (int), `name` (str), `theme_id` + `theme_version` (FK to
D7 — **the fund's universe rule is a reference to a versioned theme, not an inline copy**),
`inception_date` (date), `base_currency` (enum `INR`), `benchmark_index_id` + `benchmark_variant`
(FK to D3/H1 — **must be a TRI variant**), `secondary_benchmark_id`,
`target_holdings_count_min`/`_max` (int), `weighting_scheme` (enum: `EQUAL`, `MCAP`, `FREE_FLOAT_MCAP`,
`SCORE_TILTED`, `RISK_PARITY`, `INVERSE_VOL`, `MIN_VARIANCE`, `CONVICTION_TIERED`),
`rebalance_frequency` (enum `WEEKLY`/`MONTHLY`/`QUARTERLY`/`SEMIANNUAL`/`THRESHOLD_TRIGGERED`),
`rebalance_calendar_rule` (str), `max_position_weight`, `min_position_weight` (Decimal),
`max_sector_weight`, `max_group_weight` (Decimal), `cash_target_pct`, `cash_max_pct` (Decimal),
`min_market_cap`, `max_market_cap` (Decimal INR — "under ₹5,000cr" lives here),
`min_adtv_value` (Decimal INR), `min_listing_age_days` (int),
`allow_shorting`, `allow_derivatives`, `allow_sme_series` (bool),
`aum_capacity_cap` (Decimal INR), `management_fee_bps`, `performance_fee_terms` (str),
`effective_from` (date), `approved_by`, `mandate_hash` (str — sha256 of the canonicalised mandate;
goes in every decision journal entry).

**Volume.** Tens of funds × a handful of versions = **hundreds of rows.**

**Why needed.** It is the fund. **Failure mode:** an unversioned mandate makes historical performance
uninterpretable — you cannot tell whether 2019's return came from the strategy or from a rule someone
loosened in 2021 and backfilled.

## J2. `theme_rule_version` — see D7; referenced here because the fund binds to a *version*, not a name.

## J3. `universe_snapshot`

**Definition.** One row per (fund_id, as_of_date, isin): the fund's eligible universe on that date,
with every eligibility test's outcome recorded — **including the failures**. Storing only the
survivors makes the rule unauditable.

**Grain / PK.** `(fund_id, as_of_date, isin)`.

**Attributes.** `is_eligible` (bool), `eligibility_flags` (jsonb/typed model: one boolean per gate —
`passes_theme_rule`, `passes_mcap_band`, `passes_liquidity`, `passes_listing_age`,
`passes_series`, `passes_surveillance`, `passes_governance`, `passes_capacity`),
`first_failed_gate` (enum — cheap and enormously useful for debugging a shrinking universe),
`theme_score` (Decimal 0–1), `theme_evidence_ids` (uuid[]), `theme_revenue_pct` (Decimal),
`rank_within_universe` (int), `data_asof_ts` (tz-aware — **the cutoff used**),
`rule_hash`, `mapper_version`.

**Volume.** 20 funds × 240 rebalance dates × ~800 candidates = **~4M rows** over 20y. Cheap, and worth
every byte.

**Why needed.** G4 and, practically, the ability to answer "why is this name not in the fund?" —
which is the question a fund manager is asked most often and an automatic manager must answer without
a human re-deriving it. **Failure mode:** an opaque universe; and the inability to detect that a
theme's universe silently collapsed to nine names in 2013 because one data source was empty.

## J4. `target_portfolio`

**Definition.** One row per (fund_id, as_of_date, isin): what the fund *wants* to hold, before
implementation. Separate from actual positions because the gap between them is the implementation
shortfall and must be measurable.
**Attributes:** `target_weight` (Decimal), `target_value` (Decimal INR), `target_shares` (Decimal),
`prior_weight` (Decimal), `weight_delta`, `reason_code` (enum `NEW_ENTRY`, `ADD`, `TRIM`,
`FULL_EXIT`, `DRIFT_REBALANCE`, `RAIL_FORCED_TRIM`, `CAPACITY_CAPPED`, `NO_CHANGE`),
`score_components` (typed model — the sub-scores that produced the weight),
`constraint_binding` (enum[]), `pre_constraint_weight` (Decimal — the weight before rails clipped it,
so rail impact is measurable).
**Volume:** 20 funds × 240 dates × 40 names = **~200k rows**.
**Failure mode if absent:** you cannot separate "the signal was wrong" from "we could not implement
the signal" — which is the single most important diagnostic a fund manager has.

## J5. `rebalance_event`

**Definition.** One row per (fund_id, rebalance_id): the decision cycle itself.
**Attributes:** `trigger` (enum `SCHEDULED`, `DRIFT_THRESHOLD`, `CASH_FLOW`, `RAIL_BREACH`,
`CORPORATE_ACTION`, `MANUAL`), `decision_ts` (tz-aware), `data_cutoff_ts` (tz-aware — **the PIT
boundary, and the single most audited field in the system**), `intended_trade_date` (date),
`inputs_hash` (str — hash of every input row-set read, making replay verifiable),
`code_version` (git sha), `mandate_hash`, `rule_hash`, `model_versions` (typed model),
`status` (enum `PLANNED`, `EXECUTING`, `COMPLETE`, `ABORTED`), `abort_reason`.
**Why needed:** replay determinism. Same `inputs_hash` + same `code_version` ⇒ byte-identical journal,
which is exactly the repo's stated invariant.

## J6. `order` and J7. `fill`

**`order`** — one row per (fund_id, order_id): `isin`, `side` (enum `BUY`/`SELL`), `quantity`
(Decimal), `order_type` (enum `MARKET`, `LIMIT`, `VWAP_SLICE`, `TWAP_SLICE`, `CLOSE_AUCTION`),
`limit_price` (Decimal), `time_in_force`, `placed_ts` (tz-aware), `parent_rebalance_id`,
`intended_price` (Decimal — the **decision price**, the arrival benchmark for implementation
shortfall), `expected_cost_bps` (Decimal, from I3), `status` (enum `PLACED`, `PARTIAL`, `FILLED`,
`CANCELLED`, `REJECTED`, `EXPIRED`), `rejection_reason` (enum: `RAIL_BLOCKED`, `BAND_LIMIT`,
`INSUFFICIENT_CASH`, `SURVEILLANCE_BLOCK`, `KILL_SWITCH`, `BROKER_REJECT`, `NO_LIQUIDITY`),
`broker_order_ref`, `is_simulated` (bool).

**`fill`** — one row per (order_id, fill_seq): `quantity` (Decimal), `price` (Decimal INR),
`executed_ts` (tz-aware), `exchange`, `trade_ref`, `settlement_date` (date — **computed from K2, not
assumed**), `charges` (a typed breakdown, one Decimal per `charge_type` from I1, itemised — never a
single "costs" number), `realised_slippage_bps` (Decimal vs `intended_price`),
`is_simulated`, `sim_model_version`.

**Volume.** 20 funds × 240 rebalances × 30 trades = **~150k orders**, more fills. Small.

**Why needed.** Itemised charges per fill are what let you *verify* the cost model against reality
once live, and what let a tax computation be exact rather than approximate.
**Failure mode.** Aggregated costs are unauditable; a settlement date assumed as T+2 for the whole
window mis-models cash by a day for four of the twenty years (see K2).

## J8. `position` and J9. `tax_lot`

**`position`** — `(fund_id, isin, as_of_date)`: `quantity` (Decimal), `avg_cost` (Decimal),
`market_value` (Decimal), `weight` (Decimal), `unrealised_pnl` (Decimal),
`quantity_pending_settlement` (Decimal — **the T+1/T+2 tail; a share sold today is not deliverable
today**), `is_pledged`, `days_held`, `entry_rebalance_id`, `theme_score_at_entry`.

**`tax_lot`** — `(fund_id, isin, lot_id)`: `open_date` (date), `open_fill_id`, `quantity_opened`,
`quantity_remaining` (Decimal), `cost_per_share` (Decimal, inclusive of buy-side charges),
`grandfathered_fmv_2018_01_31` (Decimal, nullable — **required by s.112A for any lot opened before
2018-02-01; if you do not store it you cannot compute LTCG correctly for those lots, ever**),
`close_date`, `close_fill_id`, `realised_gain` (Decimal), `holding_period_days` (int),
`gain_class` (enum `STCG`/`LTCG`), `tax_regime_applied` (FK to I2), `matching_method` (enum `FIFO` —
**Indian tax law requires FIFO for demat shares; the system must not offer LIFO or HIFO as an option
and then quietly use it**).

**Volume.** 20 funds × 2,000 lots/yr × 20y = **~800k lots**.

**Why needed.** Post-tax return is the only return an investor gets, and it is lot-dependent. Also:
holding one more month to cross the 12-month LTCG boundary is a *decision* the manager can make, and
it cannot be made without lot-level data.
**Failure mode.** Average-cost accounting instead of FIFO lots produces a wrong tax number and hides
the LTCG-boundary optimisation entirely. Missing the 2018-01-31 FMV makes every pre-2018 lot's tax
uncomputable.

## J10. `cash_ledger`

**Definition.** One row per (fund_id, entry_id): every cash movement, on both a trade-date and a
settlement-date basis.
**Attributes:** `entry_type` (enum `TRADE_BUY`, `TRADE_SELL`, `CHARGE`, `DIVIDEND_RECEIPT`,
`SUBSCRIPTION`, `REDEMPTION`, `MANAGEMENT_FEE`, `TAX_PAYMENT`, `INTEREST_ON_CASH`,
`BUYBACK_PROCEEDS`, `RIGHTS_SUBSCRIPTION_PAYMENT`, `RE_SALE_PROCEEDS`, `MARGIN_BLOCK`),
`amount` (Decimal INR, signed), `trade_date` (date), `value_date`/`settlement_date` (date),
`available_balance_after` (Decimal), `encumbered_balance_after` (Decimal),
`linked_fill_id`/`linked_action_id`, `is_projected` (bool — a receivable not yet received).
**Why needed:** **you cannot buy with money you have not been paid.** Under T+2 (most of the window)
a sell on Monday funds a buy on Wednesday, not Monday. A backtest that nets buys against same-day
sells is running an implicit margin facility it does not have.
**Failure mode:** silent leverage — the classic, and it is worth several percent a year in a
high-turnover strategy because it removes the cash drag entirely.

## J11. `unit_ledger` and `nav`

**`nav`** — `(fund_id, nav_date)`: `nav_per_unit` (Decimal, 4dp), `units_outstanding` (Decimal),
`aum` (Decimal INR), `gross_asset_value`, `cash_balance`, `accrued_income`, `accrued_expenses`,
`nav_method` (enum `CLOSE_PRICE`, `LAST_TRADED`, `FAIR_VALUE_MODEL` — the last for suspended or
untraded holdings, which **must** have an explicit fair-value policy or a suspended holding silently
freezes at its last price and flatters the NAV indefinitely),
`stale_priced_holdings_count` (int), `stale_priced_value_pct` (Decimal — a published honesty metric).
**`unit_ledger`** — `(fund_id, transaction_id)`: `investor_id`, `transaction_type` (enum
`SUBSCRIPTION`/`REDEMPTION`/`SWITCH_IN`/`SWITCH_OUT`), `units` (Decimal), `nav_applied` (Decimal),
`amount` (Decimal), `transaction_ts`, `nav_date_applied` (date — the cut-off rule matters),
`exit_load_pct` (Decimal), `exit_load_amount`.
**Why needed:** unitised NAV is what makes a multi-fund manager a *fund* manager. It is also the only
way to compute time-weighted return (manager skill) separately from money-weighted return (investor
experience) — and for a system that will be judged on both, conflating them is a real error.
**Failure mode:** a fund that reports a TWR while investors experienced a much worse MWR because
subscriptions arrived after the run-up. Also: stale-priced suspended holdings inflating NAV, the
mechanism behind several real-world fund blow-ups.

## J12. `flow_event` (subscriptions and redemptions as a *backtest input*)

**Definition.** One row per (fund_id, flow_date): the net external cash flow the fund must absorb.
**Attributes:** `gross_subscription`, `gross_redemption`, `net_flow` (Decimal INR),
`flow_source` (enum `ACTUAL`, `SIMULATED_CONSTANT`, `SIMULATED_MOMENTUM_CHASING`,
`SIMULATED_STRESS`), `days_to_deploy_policy` (int).
**Why needed — and this is a genuinely under-appreciated requirement.** A backtest run on a static
notional AUM is the easy case and the flattering one. Real thematic funds receive money **after** the
theme has run and face redemptions **during** the drawdown, which forces buying high and selling low
in a mechanical way that has nothing to do with the signal. A capacity-constrained thematic fund
manager that has not simulated flow-driven trading has not measured its own worst risk.
**Failure mode:** the backtest reports the *strategy's* return; investors receive the *fund's* return,
which is materially lower. Simulating at least one adverse flow scenario per fund is the fix.

## J13. `risk_rail` and `rail_breach`

**`risk_rail`** — `(fund_id, rail_id, version)`: `rail_type` (enum `MAX_POSITION_WEIGHT`,
`MAX_SECTOR_WEIGHT`, `MAX_GROUP_WEIGHT`, `MAX_ILLIQUID_WEIGHT`, `MAX_PARTICIPATION_OF_ADTV`,
`MAX_PCT_OF_FREE_FLOAT`, `MIN_HOLDINGS_COUNT`, `MAX_DAILY_TURNOVER`, `MAX_DRAWDOWN_HALT`,
`MIN_CASH`, `NO_SURVEILLANCE_NAMES`, `NO_GOVERNANCE_FLAGGED`, `MAX_CROSS_FUND_HOUSE_EXPOSURE`,
`DATA_QUALITY_RED_BLOCK`), `threshold` (Decimal), `scope` (enum `FUND`/`HOUSE`),
`action_on_breach` (enum `BLOCK_ORDER`, `FORCE_TRIM`, `WARN_ONLY`, `HALT_FUND`),
`is_hard` (bool), `effective_from`, `version`.
**`rail_breach`** — `(fund_id, rail_id, detected_ts)`: `observed_value`, `threshold`, `severity`,
`action_taken`, `orders_blocked` (uuid[]), `resolved_ts`, `resolution_note`.
**Why needed.** The repo invariant is that rails are unbypassable and that **red data means no
trading** — which requires the data-quality state (from the status/quality modules) to be a rail
input, and requires breaches to be *recorded*, because "the rail never fired" and "the rail was not
evaluated" are indistinguishable otherwise. Property tests over generated order streams (per the repo's
testing convention) need `risk_rail` to be data so they can generate against it.
**Failure mode.** A backtest that quietly exceeds its own limits and reports the resulting return.

## J14. `cross_fund_exposure` — the entity that makes this *multi*-fund

**Definition.** One row per (as_of_date, isin): the house's aggregate position and demand across all
funds. **This entity has no single-fund analogue and it is the one most likely to be omitted.**

**Grain / PK.** `(as_of_date, isin)`.

**Attributes.** `total_shares_held` (Decimal), `total_value` (Decimal INR),
`holding_fund_ids` (uuid[]), `pct_of_free_float` (Decimal), `pct_of_shares_outstanding` (Decimal),
`disclosure_threshold_breached` (bool — **SEBI SAST Reg 29 requires disclosure at 5% and on every
2% change; crossing it is a legal event, not just a risk number**),
`aggregate_demand_shares_today` (Decimal — sum of all funds' buy intent),
`aggregate_supply_shares_today` (Decimal), `net_house_demand` (Decimal),
`house_participation_pct_of_adtv` (Decimal), `contention_flag` (bool),
`allocation_method` (enum `PRO_RATA_BY_TARGET`, `PRIORITY_BY_FUND`, `RANDOMISED`,
`TIME_PRIORITY`).

**And a child, `cross_fund_allocation`** — `(as_of_date, isin, fund_id)`: `requested_shares`,
`allocated_shares`, `allocation_price` (Decimal — **must be identical across funds for the same
day's aggregated order, or you have created a cross-subsidy that is both unfair and, for a regulated
entity, illegal**), `shortfall_shares`, `deferred_to_date`.

**Why needed.** Four distinct things break without it:
1. **Capacity is a house-level property.** Six thematic funds each taking 8% of a name's ADTV is 48%
   of ADTV, which is not executable.
2. **Cross-trades.** Fund A selling a name Fund B is buying on the same day should be netted or
   explicitly crossed at an independent price — and if the backtest lets both trade in the market it
   double-counts market impact and double-pays costs; if it silently nets them it grants a free
   internal crossing that a real operation may not be permitted to do. **Either policy is defensible;
   having no policy is not**, and the policy must be data (`cross_trade_policy` on the house config).
3. **Wash-sale-like and fairness constraints.** Allocating the good fills to one fund is a real
   compliance failure with a real audit trail requirement.
4. **Disclosure thresholds** are aggregated at the *house/beneficial owner* level under SAST, not per
   fund.
**Failure mode.** Six individually-plausible backtests that were never simultaneously achievable —
the defining failure of an automatic multi-fund product, and one that is completely invisible to
per-fund testing.

## J15. `performance_record` and J16. `attribution_record`

**`performance_record`** — `(fund_id, period_type, period_end)`: `twr_pct`, `mwr_xirr_pct`,
`benchmark_twr_pct`, `excess_return_pct`, `volatility`, `downside_deviation`, `max_drawdown`,
`max_drawdown_start`/`_end` (date), `calmar`, `sharpe` (with `risk_free_series_id` — never a constant),
`sortino`, `beta_to_benchmark`, `tracking_error`, `information_ratio`, `hit_rate`,
`turnover_annualised`, `avg_holding_period_days`, `cost_drag_bps`, `tax_drag_bps`,
`cash_drag_bps`, `capacity_utilisation_pct`, `is_post_tax` (bool — **explicit, never implied**),
`is_post_cost` (bool), `stale_price_pct`.
**`attribution_record`** — `(fund_id, period_end, dimension, bucket)`: `dimension` (enum `SECTOR`,
`THEME_SUBSEGMENT`, `SIZE_BUCKET`, `POSITION`, `FACTOR`), `allocation_effect`, `selection_effect`,
`interaction_effect`, `total_contribution` (Decimal) — Brinson-Fachler against the benchmark, plus a
factor regression against H6 with `alpha`, `t_stat`, and per-factor betas.

**Why needed.** The mandated deliverable of the whole system: "does this theme fund earn anything the
theme's beta does not already give you?" Without factor attribution the answer is unavailable and the
product is unfalsifiable. **The `alpha` t-stat against the IIM-A Indian factor set is the single
number that should gate whether a fund is allowed to launch.**

## J17. `decision_journal_entry` — including no-ops

**Definition.** One row per decision the system made or declined to make. **Grain: one row per
(fund_id, decision_ts, decision_subject)** — where `decision_subject` is an ISIN, a fund-level action,
or the sentinel `NO_ACTION`. The repo invariant is that *every decision is journaled including
no-ops*, and the grain is what makes that enforceable: a rebalance cycle that changes nothing still
emits one row per evaluated candidate with `action = NO_ACTION` and the reason.

**Attributes.** `decision_id` (uuid), `fund_id`, `decision_ts` (tz-aware), `data_cutoff_ts`,
`decision_type` (enum `UNIVERSE_EVAL`, `WEIGHT_SET`, `ORDER_INTENT`, `ORDER_SUPPRESSED`,
`RAIL_BLOCK`, `NO_ACTION`, `HALT`, `DATA_QUALITY_BLOCK`), `subject_isin` (nullable),
`action` (enum), `reason_code` (enum), `reason_detail` (str),
`inputs_hash` (str), `input_row_refs` (typed model — the exact `(table, pk, revision_no)` tuples
read, which is what makes a decision *reconstructable* rather than merely *described*),
`rule_hash`, `mandate_hash`, `mapper_version`, `code_version`, `clock_source` (enum
`INJECTED_BACKTEST_CLOCK`/`SYSTEM_CLOCK`), `is_simulated` (bool),
`outcome_order_ids` (uuid[]).

**Volume.** 20 funds × 240 cycles × 800 candidates = **~4M entries**, append-only.

**⚠ Secrets note, since the journal is append-only:** per the repo's rules, nothing that can
authenticate may ever enter this table. A DSN, a token or a whole `Settings` object logged into an
append-only journal is permanent and can only be rotated, never removed.

**Why needed.** G4, wholly. And in practice: it is the only artefact that can answer a regulator, an
investor, or a future maintainer asking why a fund did what it did in 2019.
**Failure mode.** Journaling only the trades makes the *absence* of a trade unexplainable — and for an
automatic manager, most of what it does on any given day is decline to act. A journal of trades alone
documents perhaps 2% of the system's behaviour.

## J18. `data_quality_state` (the rail input)

**Definition.** One row per (source_id, as_of_date): the freshness/completeness/consistency state of
each upstream source, as an enum the rails can read.
**Attributes:** `state` (enum `GREEN`/`AMBER`/`RED`), `expected_rows`, `actual_rows`,
`staleness_hours`, `failed_checks` (str[]), `blocks_trading` (bool), `first_detected_ts`.
**Why needed.** The repo invariant "red data means no trading" is only mechanisable if the state is a
queryable fact rather than a log line. **Failure mode:** the fund trades on a day the bhavcopy was
half-empty, and nobody knows until the NAV is wrong.

---

# FAMILY K — CALENDAR & MARKET STRUCTURE

## K1. `trading_calendar`

**Definition.** One row per (exchange, segment, calendar_date): whether the market was open, and how.
Not a holiday list — a **session** table, because Indian markets have non-standard sessions that a
holiday list cannot express.

**Grain / PK.** `(exchange, segment, calendar_date, session_type)`.

**Attributes.** `is_trading_day` (bool), `session_type` (enum `NORMAL`, `MUHURAT`,
`SPECIAL_LIVE_TRADING`, `BCP_DR_SESSION`, `HALF_DAY`, `CLOSED`),
`session_open_ts`, `session_close_ts` (tz-aware — **Muhurat is a ~1-hour evening session on Diwali
with its own settlement**), `pre_open_start`/`_end`, `closing_session_start`/`_end`,
`holiday_name` (str, nullable), `is_settlement_holiday` (bool — **distinct from a trading holiday;
a day can be a trading day and a banking/settlement holiday, which shifts pay-in/pay-out**),
`circular_ref`.

**Volume.** 2 exchanges × 3 segments × 7,300 calendar days = **~44k rows**. Trivial and essential.

**Acquisition — DERIVED + FETCHED.**
- **Derived and authoritative for the past:** a day on which a bhavcopy exists is a trading day. Build
  the historical calendar from the L0 bhavcopy index. This is exact, free, and already implied by data
  you must hold anyway.
- **Fetched for the future and for session detail:** NSE publishes an annual holiday circular and a
  holiday master (`https://www.nseindia.com/resources/exchange-communication-holidays`); Muhurat and
  special sessions are announced by separate circular. **Settlement holidays are a separate NSE
  Clearing list** (`https://www.nseclearing.in/`) and are the ones people forget.
- Special sessions in the window worth knowing about: Muhurat trading every Diwali (a real trading
  session with a real bhavcopy and a *separate settlement*), and occasional Saturday special/BCP live
  sessions. Both will appear as anomalous "trading days" in a derived calendar and must be typed, not
  deleted.

**Why needed.** Every trailing window, every rebalance schedule, every settlement date computation.
**Failure mode.** Two flavours. (a) Treating a holiday as a trading day → forward-filled prices →
artificially suppressed volatility and a Sharpe ratio inflated by roughly `sqrt(actual/assumed days)`.
(b) Treating a Muhurat session as a normal day → a one-hour session's tiny volume enters ADTV and its
odd prices enter returns; and its separate settlement breaks the cash ledger.

## K2. `settlement_cycle_regime` — and why it is per-security, not global

**Definition.** One row per (exchange, segment, applicability_key, effective_from) giving the
settlement cycle in force. **The applicability key matters: India's T+1 migration was phased
security-by-security over eleven months, so for that period the cycle is a per-ISIN fact.**

**Grain / PK.** `(exchange, segment, applicability_key, effective_from)`.

**Attributes.** `cycle` (enum `T_PLUS_5`, `T_PLUS_3`, `T_PLUS_2`, `T_PLUS_1`, `T_PLUS_0_OPTIONAL`),
`applicability_key` (str — `ALL`, or an ISIN, or a market-cap-rank band),
`funds_payout_time` (time), `securities_payout_time` (time),
`effective_from`/`effective_to` (date), `circular_ref`.

**The verified timeline to encode:**

| From | Cycle | Note |
|---|---|---|
| 2000-01 | T+5 rolling, phased | 10 scrips first; pre-window |
| **2002-04-01** | **T+3 rolling** | all scrips |
| **2003-04-01** | **T+2 rolling** | SEBI shortened T+3 → T+2; **this is the regime for the first ~16 years of a 2006-start backtest** |
| **2022-01-01** | T+1 available on an optional basis | SEBI circular dated **2021-09-07** |
| **2022-02-25 → 2023-01-27** | **phased T+1**, per security | began with the **bottom 100 by market cap**, then the next 500 lowest each **last Friday of the month**, until all securities were T+1 by end-January 2023. **⚠ This is the per-ISIN period — a global flag is wrong for 11 months and wrong in a size-correlated way, i.e. exactly on the smallcaps a theme fund holds.** |
| **2023-01-27** | **T+1 for all** | |
| **2024-03-28** | **T+0 optional beta**, 25 scrips, expanded later | optional; model only if the strategy uses it |

**Acquisition — FETCHED.** SEBI circulars (`https://www.sebi.gov.in/legal/circulars`), NSE Clearing
(`https://www.nseclearing.in/clearing-settlement/capital-market/settlement-cycle`), NSE's settlement
cycle page. The per-security phase-in lists were published as monthly exchange circulars —
**reconstructing them requires parsing ~12 circulars; there is no consolidated file.** Budget for it,
or accept a documented approximation (e.g. assign the transition date by market-cap rank band) and
record the approximation as a data-quality flag.

**Why needed.** Cash availability (J10), the deliverable-quantity constraint on selling, the timing of
subscription deployment, and the interest earned on settlement float. Over 20 years the difference
between T+2 and T+1 on a high-turnover fund is worth real basis points of cash drag.
**Failure mode.** Modelling the whole window at T+1 (today's regime) grants an extra day of buying
power for 16 of 20 years — silent leverage again, and it compounds.

## K3. `circuit_breaker_regime`

**Definition.** One row per (regime, effective_from): the market-wide index circuit breaker rules and
the security-level price-band rules in force.
**Attributes:** `scope` (enum `MARKET_WIDE`, `SECURITY_LEVEL`), `trigger_pcts` (Decimal[] — 10/15/20),
`reference_price_basis` (enum `PREVIOUS_QUARTER_CLOSE`, `PREVIOUS_DAY_CLOSE`),
`halt_durations_by_time_of_day` (typed model), `dynamic_band_rules` (typed model),
`effective_from`, `circular_ref`.
**Facts to encode:** index-wide breakers at 10/15/20% since **2001-07-02**; the trigger-reference and
halt-duration methodology was **revised with effect from 2013-10-01** (moving to the previous day's
close as the reference); dynamic price bands ("flexing") for F&O and index-constituent securities were
introduced in 2013; the 2%/5%/10%/20% security bands and their assignment rules changed several times;
`ESM`/`ASM` overlay bands from 2018/2023.
**Why needed.** B4's semantics change with the regime; a halt on 2020-03-13 is a real day on which
nothing executed. **Failure mode:** simulating fills during a market-wide halt.

## K4. `market_segment_regime`

**Definition.** One row per structural change to the market's shape that affects universe or
tradability: SME platform launches (NSE Emerge and BSE SME both **2012**), the introduction of the
Rights Entitlement tradable instrument (**2020**), the FII→FPI regime change (**2014**), the MSEI
segment, the periodic call auction mechanism for illiquid securities (**2013**), the block-deal window
size and timing revisions, the introduction of the ESM framework (**2023**), and the tick-size change
for sub-₹250 securities (**2024-06-10**).
**Attributes:** `change_type` (enum), `effective_from`, `description`, `affects_universe` (bool),
`circular_ref`.
**Why needed.** These are the "the market was a different place then" facts that determine whether a
rule is even *expressible* on a historical date. A fund mandate that permits SME-series names cannot
be backtested before 2012 — not because the data is missing, but because the segment did not exist.
**Failure mode.** Silently backtesting a rule in a period where its subject did not exist, and
reporting the result as though the period were comparable.

---

# FAMILY L — ENTITIES THE BRIEF DID NOT LIST BUT THE SYSTEM NEEDS

## L1. `source_registry` and `fetch_campaign` — provenance as data

**Definition.** `source_registry`: one row per (source_id, version) describing a data source and, most
importantly, **its verified availability window**. `fetch_campaign`: one row per fetch run, recording
what was attempted, what arrived, and what did not.

**`source_registry` attributes.** `source_id` (enum), `display_name`, `base_url`, `access_mechanism`
(enum `BULK_ARCHIVE`, `JSON_API`, `HTML_SCRAPE`, `PDF_EXTRACT`, `PAID_VENDOR_API`, `MANUAL_UPLOAD`),
`auth_required` (bool), `credential_ref` (str — **a reference to an env var name, never a value**),
`verified_earliest_date` (date — **the probed truth, not the documented claim**),
`verified_latest_date`, `probe_method` (str), `probe_run_at` (date),
`rate_limit_desc`, `polite_delay_ms` (int), `tos_url`, `redistribution_allowed` (bool),
`format_eras` (typed model: list of `(from, to, parser_version)`), `known_quirks` (str[]).

**Why this is a first-class entity and not a README.** Half of this document's uncertainties
("does the MTO archive really go back to 2011?") are resolvable only by probing, and the answer must
then be *durable and machine-readable* so that a backtest can automatically report
`"liquidity_proxy_mode = DEGRADED for 2006-01-01..2011-03-31 because source NSE_MTO
verified_earliest_date = 2011-04-01"`. A backtest that knows the provenance limits of its own inputs
can tell the truth about itself; one that does not, cannot.

**`fetch_campaign` attributes.** `campaign_id`, `source_id`, `date_range_requested`,
`files_expected`, `files_received`, `files_missing` (str[]), `bytes`, `started_ts`, `finished_ts`,
`http_error_counts` (typed model), `operator_note`, `l0_manifest_ref`.

**Failure mode if absent.** Missing days that nobody notices, and a permanent inability to distinguish
"the market was closed", "the file does not exist", "we were rate-limited" and "the parser failed" —
four completely different facts that all present as a gap in the price panel.

## L2. `entity_resolution_map`

**Definition.** One row per (raw_name_string, namespace, resolved_entity_id, confidence). The
canonicaliser for the many free-text names that arrive in bulk-deal records, shareholding-pattern
detail rows, insider disclosures, rating actions and news.
**Attributes:** `raw_string`, `normalised_string`, `namespace` (enum `ISSUER`, `INVESTOR`,
`PROMOTER`, `AUDITOR`, `RATING_AGENCY`), `resolved_id` (uuid), `method` (enum `EXACT`, `FUZZY`,
`LLM`, `MANUAL`), `confidence` (Decimal), `verified_by` (str, nullable), `first_seen`, `last_seen`.
**Why needed.** "LIFE INSURANCE CORPORATION OF INDIA", "L I C OF INDIA" and "LIC OF INDIA P&GS FUND"
are the same investor, and any flow or ownership signal that does not resolve them is measuring noise.
**Failure mode.** Fragmented ownership series that under-count institutional accumulation, producing
a signal that appears weak when it is merely mis-joined.

## L3. `primary_issuance` — IPOs, FPOs, QIPs, OFS

**Definition.** One row per primary/secondary market issuance event.
**Grain / PK.** `(issuer_id, issue_type, issue_open_date)`.
**Attributes:** `issue_type` (enum `IPO`, `FPO`, `QIP`, `PREFERENTIAL`, `RIGHTS`, `OFS`, `SME_IPO`),
`price_band_low`/`_high`, `issue_price` (Decimal), `issue_size` (Decimal INR),
`fresh_issue_size`, `ofs_size`, `subscription_times_qib`/`_hni`/`_retail` (Decimal),
`anchor_investors` (str[]), `listing_date` (date), `listing_open`/`listing_close` (Decimal),
`lock_in_expiry_dates` (date[] — **anchor lock-in expiry is a real, dated supply event**),
`drhp_document_id`, `knowledge_ts`.
**Sources:** SEBI's DRHP/RHP filings (`https://www.sebi.gov.in/filings/public-issues`), NSE/BSE IPO
pages, exchange listing circulars, Chittorgarh (unofficial but comprehensive; scraping ToS applies).
**Why needed.** (a) `min_listing_age_days` in a mandate needs the listing date; (b) new listings are
frequently the purest expression of a theme (India's defence, renewables and manufacturing themes are
substantially IPO-driven), so a fund that structurally cannot buy new listings misses the theme;
(c) DRHPs are the single richest dated business description available for a new company; (d) lock-in
expiries are supply events that a concentrated smallcap fund must model.
**Failure mode.** Excluding new listings understates the theme; including them without listing-date
and lock-in data overstates tradability at listing (where bands are wide and volume is one-sided).

## L4. `custom_benchmark_construction`

**Definition.** For the (common) case where a theme has **no published index**, the fund must
construct its own benchmark and that construction is itself an entity, with a version and a rule.
**Grain / PK.** `(benchmark_id, version, rebalance_date, isin)`.
**Attributes:** `construction_rule_hash`, `weighting` (enum), `weight` (Decimal),
`is_investable_replica` (bool — does the benchmark respect the same liquidity floors the fund does?),
`level` (Decimal, the computed index level), `variant` (enum `PR`/`TRI`).
**Why needed.** Benchmarking a defence fund against Nifty 50 measures the sector bet, not the manager;
benchmarking against a back-computed thematic index imports look-ahead (D2). A **rules-based,
PIT-constructed, liquidity-respecting custom benchmark built by the same universe compiler with
equal weights and no stock selection** is the honest comparator: it isolates *selection and weighting
skill within the theme* from *the theme's own beta*.
**Failure mode.** Without it, every thematic fund's "alpha" is really the theme's return and the
product cannot be evaluated at all.

## L5. `analyst_estimate` (optional)

**Definition.** One row per (issuer_id, estimate_date, fiscal_period, metric, broker): consensus and
individual forward estimates. **Attributes:** `metric` (enum `EPS`, `REVENUE`, `EBITDA`,
`TARGET_PRICE`), `value` (Decimal), `broker`, `estimate_ts` (**PIT — this data is notoriously
backfilled and restated by vendors**), `num_estimates`, `std_dev`.
**Sources:** Refinitiv I/B/E/S, Bloomberg, Capitaline consensus, Trendlyne (current only). No free
Indian source with real history exists.
**Verdict: NICE-TO-HAVE.** Coverage of the Indian smallcap thematic universe is poor to nonexistent —
which is precisely the universe these funds fish in — so an estimate-dependent rule would apply to a
biased subset of large, well-covered names. **Failure mode if used carelessly:** vendor consensus
files are the most PIT-contaminated data in finance; using one without vintage support is close to
guaranteed look-ahead.

## L6. `derivatives_reference` (optional)

`(isin, effective_from)`: `is_in_fno` (bool), `fno_entry_date`, `fno_exit_date`, `lot_size` (int),
`market_wide_position_limit` (Decimal), `mwpl_utilisation_pct` (Decimal, daily), `is_in_ban` (bool).
**Sources:** NSE F&O security list and the daily ban list; NSE circulars for eligibility reviews.
**Why worth carrying even for a long-only fund:** F&O membership removes the price band, is the
cleanest available "institutionally tradable" flag, and its *exit* (a name being dropped from F&O) is
a genuine liquidity-deterioration signal. **Verdict: IMPROVES-FIDELITY.**

---

# REQUIREMENTS BY PRIORITY

Three tiers, defined strictly:

- **MUST** — *without it the backtest is dishonest, not merely imprecise.* Its absence produces a
  known, named, directional bias (survivorship, look-ahead, phantom liquidity, silent leverage,
  fake alpha). No result may be published without it.
- **FIDELITY** — its absence produces a result that is directionally honest but quantitatively wrong,
  usually flattering. Required before capital is committed; not required to reject a bad strategy.
- **NICE** — improves decisions or diagnostics; its absence biases nothing.

| # | Entity | Tier | Reason (the specific lie avoided) |
|---|---|---|---|
| A1 | `security_master` | **MUST** | Survivorship. No dated universe without it. |
| A2 | `issuer` | FIDELITY | Group rails and cross-ISIN continuity; not needed to price a strategy. |
| A3 | `identifier_map` (effective-dated) | **MUST** | Dropped/wrong joins → unadjusted splits and spliced chimeric price series. |
| A4 | `isin_change_event` | **MUST** | Phantom delist+relist, fake realised P&L and fake tax events. |
| A5 | `listing_status_event` (+ exit price) | **MUST** | Survivorship and delisting-return bias. The **exit price** is the hardest MUST to source. |
| A6 | `series_membership` | **MUST** | T2T/BE names are not intraday-tradable; free to derive from bhavcopy. |
| A7 | `board_lot_and_tick` | FIDELITY | Sub-tick fills manufacture free money on low-priced stocks. |
| A8 | `exchange_venue` | FIDELITY | BSE-only microcaps; one-venue ADTV vs other-venue fills. |
| B1 | `eod_quote_raw` (unadjusted) | **MUST** | Everything. Storing adjusted prices breaks replay determinism outright. |
| B2 | `intraday_bar` | NICE | Impossible pre-2015 anyway; use a daily impact model instead. |
| B3 | `delivery_position` | **MUST** (post-availability) | Headline volume overstates smallcap liquidity 2–4×. Degrade explicitly where unavailable. |
| B4 | `price_band` / hit-limit | **MUST** | Buying at a locked upper circuit is the most flattering fiction available. |
| B5 | `liquidity_metrics` (lagged) | **MUST** | Universe filter + capacity. Unlagged windows are look-ahead liquidity selection. |
| B6 | `bid_ask` proxy | FIDELITY | No historical quote data exists free; a versioned estimator is the honest answer. |
| B7 | `capacity_estimate` (incl. cross-fund) | **MUST** | Six funds each taking 8% of ADTV is 48% of ADTV. |
| C1 | `corporate_action` | **MUST** | A missed split is a fake −80% day; a missed dividend costs ~1.3%/yr of true return. |
| C2 | `adjustment_factor` (derived, PIT-truncated) | **MUST** | Double-adjustment; and factors computed to *today* leak future actions into past scale. |
| C3 | `merger_mapping` | **MUST** | A third of "disappearances" are mergers, not deaths. |
| C4 | `dividend_cash_flow` (lag + tax) | FIDELITY | TR-index-style instant tax-free reinvestment is worth several hundred bps cumulative. |
| C5 | `buyback_participation` | FIDELITY | 100%-acceptance assumption; tax regime inverts on 2024-10-01. |
| D1 | `industry_classification` **with history** | **MUST** | *The* thematic look-ahead. Today's tags select companies that later succeeded at the theme. |
| D2 | `index_membership` history | **MUST** for benchmarks; **MUST** if used as a universe | Today's Nifty 500 is the set that grew into it. Back-computed thematic indices are contaminated by construction. |
| D3 | `index_definition` (incl. `live_from`) | **MUST** | The guard that stops back-computed history being used as PIT evidence. |
| D4 | `business_description` **dated** | **MUST** for any text-driven theme | Today's company description makes every theme fund clairvoyant. |
| D5 | `segment_revenue` | **MUST** for "true exposure" claims; FIDELITY otherwise | Without it "thematic" is a keyword match, and the fund cannot prove it holds the theme. |
| D6 | `theme_evidence` (dated, versioned extractor) | **MUST** | Reproducibility: a nondeterministic LLM label makes a backtest non-replayable. |
| D7 | `theme_definition` / `vocabulary` | **MUST** | The rule must be versioned data; the no-company-names validator is the cheap anti-look-ahead guard. |
| D8 | `company_relation` | NICE | Ranking input only; today's graph asserts yesterday's relationships. |
| D9 | `peer_group` | FIDELITY | Relative-value screens inherit D1's contamination. |
| E1 | `financial_statement_fact` (PIT, filing-dated) | **MUST** for any fundamental rule | Filing-date look-ahead = 45–60 days of foresight, 4×/yr, 20 years. |
| E2 | `financial_statement_filing` (`filing_ts`, audit flags) | **MUST** | Without `filing_ts`, E1 is not PIT at all. |
| E3 | `shares_outstanding` series | **MUST** | Today's share count mis-sizes every past market cap; a size-factor look-ahead disguised as price. |
| E4 | `market_cap_and_free_float` | **MUST** | "Under ₹5,000cr" mandates; free-float capacity limits. |
| E5 | `derived_ratio` (versioned, input-traced) | **MUST** for quality/value themes | TTM windows silently including unfiled quarters. |
| E6 | `restatement_quarantine` | **MUST** | Restatements cluster in accounting-problem companies; a restated DB cleans up exactly what a quality screen should reject. |
| E7 | `governance_red_flag` | **MUST** | The one failure that discredits the product: auto-buying a flagged fraud. |
| F1 | `shareholding_pattern` | **MUST** | Free float, pledge, share-count anchor. 21-day filing lag is the look-ahead. |
| F2 | `insider_disclosure` | FIDELITY | Thin before 2015; using trade date not disclosure date is a 2-day look-ahead on the best-informed trades. |
| F3 | `promoter_pledge` | **MUST** (as a rail) | Pledge invocation is how Indian smallcaps go to zero, and it is disclosed. |
| F4 | `bulk_and_block_deals` | FIDELITY | Mainly needed to *strip* block volume out of ADTV. Full 20-year depth available (2004 regime). |
| F5 | `institutional_flow_aggregate` | NICE | Macro overlay; NSDL sector AUC is the useful part. |
| F6 | `mutual_fund_scheme_holdings` | NICE | Monthly only from Oct 2018 — cannot underpin a 20-year rule. Crowding diagnostics. |
| G1 | `exchange_announcement` (**dissemination ts**) | **MUST** | Day-granularity = half a session of foresight on the market's most price-sensitive information. |
| G2 | `results_calendar` | FIDELITY | Event-risk rails; derivable from G1. |
| G3 | `credit_rating_action` | FIDELITY (→ MUST as a rail if any leverage/credit exposure) | High-precision distress signal; use the exchange copy for PIT timing. |
| G4 | `regulatory_action` | **MUST** (as a rail) | Owning a company under a trading ban. |
| G5 | `surveillance_list` (ASM/GSM/ESM) | **MUST** post-2017; **disclose the gap** pre-2017 | Tradability. Pre-2017 smallcap tradability is structurally overstated and that is unfixable — say so. |
| G6 | `news_item` | NICE | No honest 20-year Indian archive. Any news rule is "not backtestable before 2015". |
| H1 | `index_level` **TRI** | **MUST** | Benchmarking against price return awards ~1.3%/yr of fictitious alpha. |
| H2 | `risk_free_rate_curve` | **MUST** | Every risk-adjusted metric; and cash return is material over 20 years of 3–8% rates. |
| H3 | `macro_series` (with vintages) | FIDELITY | Revision look-ahead; base-year splices at CPI 2011/2012, WPI/IIP 2011-12, GDP 2011-12. |
| H4 | `fx_and_commodity` | FIDELITY | Input-cost exposure is a core theme mechanic. |
| H5 | `theme_exogenous_driver` | **MUST** for macro-linked themes | A "monsoon-linked" fund with no rainfall data is a keyword filter in costume. |
| H6 | `factor_return_series` | FIDELITY (**strongly recommended**) | The cheapest available test of whether a theme fund is just smallcap momentum. Free from IIM-A, 1993+. |
| I1 | `charge_schedule` (effective-dated) | **MUST** | 2026 rates applied to 2006–2020 mis-price half the window; cost error always flatters. |
| I2 | `tax_regime_schedule` | **MUST** for a fund product; FIDELITY for pure signal research | Ranking between strategies inverts once tax is applied. LTCG was *exempt* for the first ~12 years. |
| I3 | `slippage_model_config` (versioned) | **MUST** | An unversioned magic number is what makes microcap themes look tradable. |
| I4 | `borrow_availability` | **MUST if shorting permitted**, else NICE | Shorting names that could not be shorted, or during an F&O ban. |
| J1 | `fund_definition` (versioned) | **MUST** | Unversioned mandates make history uninterpretable. |
| J3 | `universe_snapshot` (incl. failures) | **MUST** | Auditability, and detecting a silently collapsed universe. |
| J4 | `target_portfolio` (pre- and post-constraint) | **MUST** | Separates "signal wrong" from "not implementable". |
| J5 | `rebalance_event` (`data_cutoff_ts`, `inputs_hash`) | **MUST** | Replay determinism, which is a stated repo invariant. |
| J6/J7 | `order` / `fill` (itemised charges) | **MUST** | Verifiable cost model; exact tax. |
| J8/J9 | `position` / `tax_lot` (FIFO, 2018 FMV) | **MUST** for post-tax reporting | Average-cost accounting hides the LTCG-boundary decision and gets the tax wrong. |
| J10 | `cash_ledger` (trade **and** settlement date) | **MUST** | Netting same-day buys against unsettled sells is silent leverage. |
| J11 | `nav` / `unit_ledger` | **MUST** for a fund product | TWR vs MWR; stale-priced suspended holdings inflating NAV. |
| J12 | `flow_event` (incl. adverse scenarios) | FIDELITY (**strongly recommended**) | Thematic funds get money after the run and redemptions in the drawdown. |
| J13 | `risk_rail` / `rail_breach` | **MUST** | "Rail never fired" vs "rail never evaluated" must be distinguishable. |
| J14 | `cross_fund_exposure` / allocation | **MUST** for a multi-fund system | Six individually-plausible backtests that were never simultaneously achievable. |
| J15/J16 | `performance` / `attribution` | **MUST** | The deliverable. Factor attribution is the gate on whether a fund should exist. |
| J17 | `decision_journal_entry` **incl. no-ops** | **MUST** | A trade-only journal documents ~2% of what the system does. |
| J18 | `data_quality_state` | **MUST** | "Red data means no trading" is only mechanisable if the state is queryable. |
| K1 | `trading_calendar` (session-typed) | **MUST** | Forward-filled holidays inflate Sharpe; Muhurat sessions corrupt ADTV and cash. |
| K2 | `settlement_cycle_regime` (**per-security 2022–23**) | **MUST** | T+1 for the whole window grants an extra day of buying power for 16 of 20 years. |
| K3 | `circuit_breaker_regime` | FIDELITY | Simulating fills during a market-wide halt (2020-03-13). |
| K4 | `market_segment_regime` | FIDELITY | Backtesting a rule in a period where its subject did not exist (SME pre-2012, REs pre-2020). |
| L1 | `source_registry` / `fetch_campaign` | **MUST** | Distinguishes closed-market / missing-file / rate-limited / parser-failed — four different facts that look identical. |
| L2 | `entity_resolution_map` | FIDELITY | Fragmented ownership series measure noise. |
| L3 | `primary_issuance` | FIDELITY (**MUST** if new listings are in mandate) | Indian defence/renewables/manufacturing themes are substantially IPO-driven. |
| L4 | `custom_benchmark_construction` | **MUST** for any theme without a live index | Otherwise the fund's "alpha" is the theme's beta and the product is unevaluable. |
| L5 | `analyst_estimate` | NICE | Poor smallcap coverage; vendor consensus is the most PIT-contaminated data in finance. |
| L6 | `derivatives_reference` | FIDELITY | `is_in_fno` is the cleanest "institutionally tradable" flag; F&O exit is a liquidity-deterioration signal. |

**Count: 34 MUST, 20 FIDELITY, 8 NICE.**

---

# THE 20-YEAR DEPTH REGISTER — where the window genuinely breaks

Every row here is a place where the requested 2006→2026 backtest **cannot** be uniformly honest. Each
needs an explicit, published coverage statement in every backtest report, not a silent workaround.

| Thing | Genuinely available from | Consequence for a 2006-start backtest |
|---|---|---|
| NSE/BSE EOD bhavcopy | ~1994 / ~2007 (BSE) | **Fine.** The one comfortable answer. |
| NSE delivery (MTO / `sec_bhavdata_full`) | **doubtful before ~2011**; `sec_bhavdata_full` is a ~2020 artefact | Liquidity filter must degrade to traded-value for the first ~5 years. **Probe and record the true date.** |
| Index TRI levels | inception (deep) | Fine. |
| Index **constituent history** | reconstructable from 20y of change circulars; **not published as a series** | Multi-week PDF-parsing project, or pay a vendor. |
| Thematic index membership | **only from each index's live launch date** (mostly 2019–2024) | Pre-launch "history" is back-computed with today's classification — unusable as PIT evidence. |
| Industry classification **history** | **not published by NSE/BSE at all**; Prowess/Capitaline have it, paid | The largest single MUST-HAVE gap. Either buy it, reconstruct from CIN/NIC + index membership + annual reports, or declare every classification-dependent backtest `classification_pit = FALSE`. |
| XBRL financial results | ~2011–2013 structured; clean ~2015+ | 2006–2012 fundamentals require PDF extraction or a paid vendor. |
| Ind AS transition | FY2016-17 / FY2017-18 | **Fundamental ratio series are not comparable across the middle of the window.** Not a data gap — a definitional break. Must be flagged, not patched. |
| Segment revenue | vendor-deep; XBRL partial and late | The "true theme exposure" test is the most expensive thing in the catalog. |
| Shareholding pattern | HTML/PDF ~2001; XBRL ~2011+; **format changed materially 2015–16** | 2–4 parser eras; pre-2015 has no first-class pledge/locked-in fields. |
| Insider disclosures (PIT Regs 2015) | **2015** | 2006–2014 insider signal is thin; 2006–2010 unusable. |
| Bulk/block deals | **2004** | Fine — full window. |
| ASM / GSM / ESM | **GSM Mar 2017, ASM 2018-03-26, ESM 2023** | Pre-2017 smallcap tradability is **structurally overstated** and cannot be corrected. Report walk-forward windows separately either side. |
| Exchange announcements | BSE ~mid-2000s, NSE shallower; volume exploded after the 2015 Reg 30 expansion | Early-window narrative coverage is a fraction of late-window. Any announcement-count feature has a massive non-stationary trend that is an artefact of regulation, not of the market. |
| Earnings-call transcripts | mandatory exchange disclosure only from **2018** | Not usable as a 20-year feature. |
| MF scheme portfolios | monthly only from **Oct 2018**; half-yearly before | Cannot underpin a 20-year rule. |
| News with publication timestamps | GDELT 2.0 from **Feb 2015**; nothing free and reliable before | Any news feature is "not backtestable before 2015". |
| CPI (Combined, base 2012) | **Jan 2011** | Pre-2011 requires CPI-IW/AL splice — a documented, versioned artefact. |
| IIP / WPI / GDP base revisions | 2011-12 rebasing (GDP rebased 2015) | Level shifts that a macro-conditioned rule can learn as signal. |
| Settlement T+1 | optional 2022-01-01; **phased per-security 2022-02-25 → 2023-01-27**; all 2023-01-27 | Per-ISIN for 11 months, phased **by market-cap rank** — i.e. correlated with exactly the smallcaps a theme fund holds. |
| Stamp duty uniform regime | **2020-07-01** | Pre-2020 needs one state's historical schedule (tractable for a single-domicile fund). |
| GST on charges | **2017-07-01** (service tax, stepping, before) | Two regimes. |
| Equity LTCG | **exempt 2004-10-01 → 2018-03-31**; 10%>₹1L 2018–2024-07-22; **12.5%>₹1.25L from 2024-07-23** | Applying today's rate to the whole window penalises 12 years that were tax-free. |
| Buyback taxation | company-level to 2024-09-30; **deemed dividend from 2024-10-01** | Regime inversion mid-2024. |
| SME segment | **2012** | A mandate permitting SME cannot be backtested before 2012 — the segment did not exist. |
| Tradable Rights Entitlements | **2020** | Pre-2020, an unsubscribed rights issue is a real dilution loss, not a sellable RE. |
| Minute bars | **~2015** (Kite) | Pre-2015 execution is a model, never a replay. State it. |
| IBC / NCLT insolvency data | **2017** | The cleanest distress-outcome dataset covers only the back half. |
| Vahan district registrations, e-way bills | **2018** | Rural/consumption theme drivers with no early-window history. |

---

# RECOMMENDED BUILD ORDER (dependency-respecting)

1. **L1 source registry + probe sweep.** Before fetching anything at scale, probe every archive's true
   earliest date and record it. Everything downstream reports honestly only if this exists.
2. **B1 + A6 from bhavcopies (NSE+BSE, all series, full window).** This single campaign yields the raw
   price panel, the series history, the trading calendar (K1) and the survivorship-safe ISIN universe
   (A1) as by-products. Highest value per unit of effort in the entire plan.
3. **A3/A4 identifier map** from the BSE↔NSE bhavcopy join plus `symbolchange.csv`.
4. **C1 + C2** corporate actions from both exchanges, with a golden test suite and a two-source
   reconciliation. Nothing downstream is trustworthy until adjustment factors are.
5. **A5 listing/delisting events**, including the derived trading-halt backstop and an explicit
   `exit_price_source` flag.
6. **K2 settlement regime + I1 charge schedule + I2 tax schedule** — small, hand-curated, and they
   make every subsequent number defensible.
7. **H1 TRI + H2 risk-free.** Now a backtest can be benchmarked honestly.
8. **F1 shareholding pattern → E3 shares outstanding → E4 market cap/free float.** Unlocks every
   size-constrained mandate and the free-float capacity limits.
9. **B3 delivery + B5 liquidity + B7 capacity (with J14 cross-fund aggregation from day one).**
10. **G1 announcement stream** — the long campaign; start it early because it is the slowest.
11. **D1/D2/D4/D5 classification, index membership, dated descriptions, segments** — the thematic core
    and the most expensive. **Start archiving the daily/monthly snapshots (classification files, index
    constituent files, ASM/GSM lists, `EQUITY_L.csv`, Kite instruments) TODAY**, because these are
    snapshot-only sources whose history is being lost every day it is not captured.
12. **E1/E2 fundamentals PIT**, then E5/E6/E7.
13. **J-family fund entities**, built once against the backtest clock and reused unchanged for paper
    and live.

**The single highest-urgency item on this list is not a fetch — it is item 11's parenthesis.** Every
snapshot-only source (industry classification, index constituents, ASM/GSM/ESM lists, price bands,
the instrument master, the F&O list) is losing history irrecoverably at a rate of one day per day. A
daily snapshotter costing an afternoon of work is worth more to the 2031 version of this system than
any amount of retrospective effort will then be able to buy.

---

# APPENDIX — CONSOLIDATED SOURCE DOSSIER

Access mechanism legend: **BULK** = downloadable archive files; **API** = JSON/HTTP API;
**SCRAPE** = HTML parsing; **PDF** = document extraction; **PAID** = commercial licence.

## Exchanges — NSE

| What | URL | Mech | Depth | Notes |
|---|---|---|---|---|
| All reports hub | https://www.nseindia.com/all-reports | SCRAPE | — | Entry point for every daily report |
| Historical CM archives | https://www.nseindia.com/static/resources/historical-reports-capital-market-daily-monthly-archives | BULK | ~1994 | |
| Legacy bhavcopy | `https://nsearchives.nseindia.com/content/historical/EQUITIES/<YYYY>/<MON>/cm<DDMMMYYYY>bhav.csv.zip` | BULK | ~1994 → **2024-07-08** | Discontinued |
| UDiFF bhavcopy | `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip` | BULK | 2024-07 → | New column set, ISIN present |
| `sec_bhavdata_full` | `.../archives/equities/bhavcopy/pr/` | BULK | ~2020 → | Adds delivery, VWAP |
| MTO delivery | `https://nsearchives.nseindia.com/archives/equities/mto/MTO_<DDMMYYYY>.DAT` | BULK | **probe: ~2011?** | Depth uncertain — verify |
| Equity master | https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv | BULK | snapshot | **Snapshot only — archive daily** |
| Symbol changes | https://nsearchives.nseindia.com/content/equities/symbolchange.csv | BULK | deep | Key identity artefact |
| Corporate actions | https://www.nseindia.com/companies-listing/corporate-filings-actions | API | deep | Windowed date-range queries |
| Announcements | https://www.nseindia.com/companies-listing/corporate-filings-announcements | API | shallower than BSE | |
| Shareholding pattern | https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern | SCRAPE/API | | |
| Insider trading / SAST | https://www.nseindia.com/companies-listing/corporate-filings-insider-trading | API | 2015+ | |
| Financial results | https://www.nseindia.com/companies-listing/corporate-filings-financial-results | API/PDF | XBRL ~2011+ | |
| Annual reports | https://www.nseindia.com/companies-listing/corporate-filings-annual-reports | PDF | | |
| Bulk & block deals | https://www.nseindia.com/report-detail/display-bulk-and-block-deals | BULK | **2004+** | Full window |
| FII/DII activity | https://www.nseindia.com/reports/fii-dii | BULK | deep | Combined NSE+BSE+MSEI |
| ASM list | https://www.nseindia.com/reports/asm | SCRAPE | **2018-03-26+**, snapshot | **Archive daily** |
| GSM | https://www.nseindia.com/static/regulations/graded-surveillance-measure | SCRAPE | **2017-03+**, snapshot | **Archive daily** |
| Industry classification | https://www.nseindia.com/static/products-services/industry-classification | SCRAPE | **snapshot only** | The big gap |
| Holidays | https://www.nseindia.com/resources/exchange-communication-holidays | SCRAPE | annual circulars | |
| Settlement cycle | https://www.nseclearing.in/clearing-settlement/capital-market/settlement-cycle | SCRAPE | | Settlement holidays live here too |
| Delisting SOP | https://nsearchives.nseindia.com/web/sites/default/files/inline-files/SOP_for_Delisting_of_Equity_Shares_0.pdf | PDF | | |

**Access quirks (all NSE endpoints):** require a browser-like `User-Agent` and a primed cookie from
`https://www.nseindia.com` before `nsearchives`/API calls; unpublished rate limits, 403/tarpit on
aggressive fetching. Copyright asserted; **redistribution prohibited**, internal research use is the
accepted practice.

## Exchanges — BSE

| What | URL | Mech | Depth |
|---|---|---|---|
| EQ reports/downloads hub | https://www.bseindia.com/static/markets/equity/EQReports/downloads.aspx | BULK | ~2007 |
| Bhavcopy (ISIN variant) | `https://www.bseindia.com/download/BhavCopy/Equity/EQ_ISINCODE_<DDMMYY>.ZIP` | BULK | ~2007 |
| Scrip list (incl. delisted/suspended status) | https://www.bseindia.com/corporates/List_Scrips.aspx | BULK | current w/ status |
| Delisted companies | https://www.bseindia.com/corporates/Delisted_Company.aspx | SCRAPE | |
| Corporate actions | https://www.bseindia.com/corporates/corporate_act.aspx | SCRAPE | **broader than NSE** |
| Announcements | https://www.bseindia.com/corporates/ann.html | API | **~mid-2000s — the deepest free announcement archive** |
| Results / annual reports | https://www.bseindia.com/corporates/Comp_Resultsnew.aspx | PDF | early 2000s |
| Shareholding pattern | https://www.bseindia.com/corporates/shpPromoterNGroup.aspx | SCRAPE/XBRL | HTML ~2001, XBRL ~2011 |
| XBRL programme info | https://www.bseindia.com/static/about/xbrl_info.aspx | — | First Indian exchange to implement XBRL |
| Industry classification structure | https://www.bseindices.com/Downloads/India_Industry_Classification_Structure.pdf | PDF | current |

## Indices

| What | URL | Depth |
|---|---|---|
| Nifty historical data (levels, **TRI**, NTR) | https://www.niftyindices.com/reports/historical-data | inception → |
| NSE Indices industry classification guideline (2023-07) | https://www.niftyindices.com/docs/default-source/default-document-library/nse-indices_industry-classification-guideline-2023-07.pdf | current structure: 12 macro sectors / 22 sectors / 59 industries / 197 basic industries |
| Current constituent lists | `https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv` (and `ind_nifty500list.csv`, …) | **snapshot only — archive daily** |
| Index change circulars/press releases | niftyindices.com press releases | the only route to membership history |
| BSE indices | https://www.bseindia.com/indices/ | |

## Regulators, government, macro

| What | URL | Notes |
|---|---|---|
| SEBI circulars | https://www.sebi.gov.in/legal/circulars | Settlement, surveillance, disclosure regimes |
| SEBI enforcement orders | https://www.sebi.gov.in/enforcement/orders | Deep, PDF |
| SEBI public issue filings (DRHP/RHP) | https://www.sebi.gov.in/filings/public-issues | |
| SEBI AMFI sector classification (MF disclosures) | https://www.sebi.gov.in/mf/equitydec15.html | |
| RBI Database on Indian Economy | https://data.rbi.org.in/DBIE/ · https://dbie.rbihub.in/ | T-bill yields, policy rates, FX, monetary/credit — decades |
| RBI T-bill auctions | https://dbie.rbihub.in/government/treasury-bill-auctions | Weekly 91/182/364-day cut-off yields |
| MOSPI (CPI, IIP) | https://www.mospi.gov.in/ | CPI Combined base 2012 from **Jan 2011** |
| Office of the Economic Adviser (WPI) | https://eaindustry.nic.in/ | Base 2011-12 |
| MCA company master | https://www.mca.gov.in/mcafoportal/viewCompanyMasterData.do | CIN, incorporation; no bulk API |
| IBBI / insolvency | https://ibbi.gov.in/ | CIRP data **2017+** |
| PIB press releases (incl. defence DAC approvals, stamp duty notification PRID 1635399) | https://www.pib.gov.in/ | Dated, free, high-signal for policy-driven themes |
| NSDL FPI sector-wise AUC | https://www.fpi.nsdl.co.in/web/Reports/ReportSelect.aspx | Fortnightly/monthly, mid-2000s+ |
| AMFI (NAVs, MF portfolios, large/mid/small lists) | https://www.amfiindia.com/ | Monthly portfolios **Oct 2018+** |
| NSDL ISIN master | https://www.nsdl.co.in/master_search.php | ISIN↔security, weak on dates |
| IMD (rainfall) | https://mausam.imd.gov.in/ · https://www.imdpune.gov.in/ | Subdivision/district rainfall; archaic formats |
| CWC reservoir storage | http://cwc.gov.in/ | Weekly, ~20y |
| Ministry of Agriculture (sowing) | https://agriwelfare.gov.in/ | Weekly during kharif/rabi |
| Grid India / CEA (power) | https://grid-india.in/ · https://cea.nic.in/ | Daily ~2012+ / monthly deep |
| Vahan (vehicle registrations) | https://vahan.parivahan.gov.in/vahan4dashboard/ | National coverage **~2018+** |
| World Bank Pink Sheet (commodities) | https://www.worldbank.org/en/research/commodity-markets | Monthly, **1960+**, free CSV |
| IIM-A Indian Fama-French & Momentum factors | https://faculty.iima.ac.in/~iffm/Indian-Fama-French-Momentum/ | Daily & monthly, **1993+**, free |

## Rating agencies

crisilratings.com · icra.in · careratings.com · indiaratings.co.in · acuite.in — dated rationale
press releases, SEBI-mandated public hosting. Depth good ~2010+, excellent ~2016+. **Use the exchange
(Reg 30) copy as the PIT timestamp.**

## Brokers / market-data vendors

| Source | URL | What | Depth | Constraints |
|---|---|---|---|---|
| Zerodha Kite Connect | https://kite.trade/docs/connect/v3/historical/ | OHLCV candles, instrument dump | **day ~2000, minute ~2015** | ₹2,000/mo per app; ~3 req/s; no bulk endpoint; max 60 days of 1-min per call; **symbol-token-keyed, not ISIN-keyed** |
| CMIE Prowess / ProwessIQ | https://www.cmie.com/ | ~50,000 companies, 3,500+ fields, **from 1989**; industry classification with history; segments | deepest Indian source | PAID; academic licences common; **restated-latest by default — PIT support must be negotiated** |
| Capitaline | capitaline.com | 74,800+ companies, ~2,500 fields | 15–20y | PAID |
| ACE Equity (Accord Fintech) | — | 1,750+ fields, 10 industry formats | | PAID |
| LSEG / Refinitiv | https://www.lseg.com/en/data-analytics/financial-data/pricing-and-market-data/equities-market-data/national-stock-exchange-india | NSE history claimed from 1994; news archive | deep | PAID |
| Bloomberg / S&P Capital IQ / Factiva | — | prices, estimates, supply chain, news | deep | PAID |
| EODHD splits/dividends | https://eodhd.com/financial-apis/api-splits-dividends | corporate actions | partial | Cheap; use as a **reconciliation third opinion**, not primary |
| Screener.in / Trendlyne / Tijori | — | current fundamentals & descriptions | **current only** | ToS prohibit scraping; **not PIT — exploration only** |
| GDELT | https://www.gdeltproject.org/ | global news/GKG | **2.0 from Feb 2015** | Free; weak Indian pre-2015 coverage |

## Legal / licensing posture (repo-relevant)

- Exchange and regulator files are free to download; **copyright is asserted and redistribution is
  prohibited**. Internal research and backtesting is the normal, accepted use.
- **This repo is public.** `data/` must stay gitignored — no L0, no vendor extract, no fixture
  containing a meaningful slice of licensed data may be committed. Test fixtures should be small,
  synthetic-where-possible, and never a redistributable subset of a paid vendor's file.
- Paid vendor licences (Prowess, Capitaline, LSEG) typically forbid redistribution **and** derived-data
  publication. If a vendor field ever enters a published backtest report, check the licence first.
- Credentials for any paid API live in the environment / untracked `.env` only, as `SecretStr`, and
  never in `source_registry` (which stores the **env var name**, not the value), never in the
  append-only decision journal, and never in a URL or argv.
