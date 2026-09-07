# Provider-by-provider evaluation — macro, flows, news

**Date:** 2026-09-07. Companion to `ops/gates/macro-news-ingestion-plan-2026-09-07.md`, which it
also **corrects in two places** (§9).

Graded by **backtestability**, because that is the only property that decides what a series may be
used for, and it is invisible when providers are compared by topic or by depth. A provider with
13 years of history and no vintages is *less* useful for a backtest than one with two years of
immutable dated files.

---

## The grading scale

| Grade | Meaning | What you may do with it |
|---|---|---|
| **A** | True PIT, **and** the archive is addressable by date, so history is backfillable | Backtest freely over the archive depth |
| **B** | True PIT going forward, but **no dated archive** — the endpoint serves "latest" only | Backtest **only from capture day.** Every uncaptured day is gone permanently |
| **C** | History available, but only the **current vintage** — revisions silently replace originals | **Never backtestable from this source.** Present-day backdrop only |
| **D** | Not a series. Evidence for a human or an LLM | Not backtestable, and not permitted to trade (D6 / §7) |
| **X** | Credential-, licence- or payment-gated | Decide the gate before the data |

The B/C distinction is the one that gets missed. **B is a wasting asset** — the cost of not
starting is one day of history per day. **C is a trap** — it looks backfillable, the numbers look
plausible, and using them is fabrication.

---

## Grade A — backtest freely

### A1. `niftyindices.com` / `nsearchives.nseindia.com` — `ind_close_all_<DDMMYYYY>.csv`

**The single most valuable unfetched source in the project.**

| | |
|---|---|
| What you get | Per session, per index: Open/High/Low/Close, Points Change, Change%, Volume, Turnover, **P/E, P/B, Div Yield** — 13 columns, 163–165 index rows today |
| Depth | **2012-10-01** measured (2012-07-01 → 404). ~2,470 sessions |
| Format eras | **None.** The 13-column header is byte-identical 2012-10-01 → 2026-09-01 |
| PIT | Release date = session date. Published after the close. **Immutable, never revised** — `revision_seq` 0 forever |
| Cost | ~2,470 requests, one small CSV each (17 KB) |
| Auth | None. No session cookie. Browser UA + Referer only |
| Register | `nifty_index_close_snapshot`, **VERIFIED**, sha256 recorded, fixture frozen |

**How it helps — this one file is four datasets:**

1. **Market valuation regime.** NIFTY 50 / 500 P/E and P/B, daily, 13 years. The classic mean-reversion
   and regime-filter input, and you can compute the z-score honestly because every observation
   was knowable that evening.
2. **Sector valuation dispersion and rotation.** 165 indices means sectoral P/E across the whole
   family — the input to "which sector is cheap relative to its own history", which is a genuine
   rotation signal rather than a momentum re-labelling.
3. **The 10Y G-sec yield.** `GSEC10 NSE Index` is a row in this file. Discount rate, rate-cycle
   regime, and equity-vs-bond earnings yield gap — free, in the same fetch.
4. **The TRI fallback.** `nifty_tri_history` is **FAILED** (application session gate: POST returns
   200 carrying the site's HTML home page, redirected to `?ReturnUrl=`). §4.1's stated fallback is
   *"computed price-index proxy + dividend estimate"* — and the Div Yield column needed for it is
   right here. M11.2 is also the path to closing M3.9.

**Verdict: CONSIDER — top priority. This is the whole Tier A panel and it needs one `go`.**

**One probe worth doing first, because it may dissolve the campaign conflict.** The register
carries **two verified hosts** for this file with a *byte-identical payload*:
`niftyindices.com/Daily_Snapshot/ind_close_all_{DDMMYYYY}.csv` and
`nsearchives.nseindia.com/content/indices/ind_close_all_{DDMMYYYY}.csv`. The measured 2012 archive
depth was established on the **nsearchives** host. If `niftyindices.com/Daily_Snapshot/` also
serves the 2012–2015 archive, **M11.2 can run on niftyindices.com and stop contending with
M10.4's `nsearchives` request budget** — the two campaigns could then run concurrently instead of
being serialised. That is a ~6-request bisect and it is the highest-leverage probe on this page.
Unprobed today; do not assume it.

### A2. `nsearchives.nseindia.com` — F&O bhavcopy (`nse_fo_bhavcopy`)

| | |
|---|---|
| What you get | 34-column UDiFF derivatives bhavcopy: `XpryDt`, `StrkPric`, `OptnTp`, `OpnIntrst`, `ChngInOpnIntrst` — the columns PCR and basis are computed from. Includes `FUTIVX` (India VIX futures) |
| Depth | **era starts 2024-07-08** — this is the UDiFF row only. ~2 years, not 13 |
| PIT | Immutable, dated URL |
| Cost | ~530 sessions × ~1 MB |

**How it helps:** put/call ratio, aggregate open interest and futures basis are the cleanest
available proxies for positioning and leverage. `store/fo_aggregates.py` already computes them.
Note the plan's constitution: **F&O is sentiment context only, never traded.**

**Verdict: CONSIDER, second tier.** Cheap and PIT-clean, but the ~2-year depth means it can inform
a *forward* policy far better than it can validate one. The pre-Jul-2024 legacy F&O format is
**not registered** — treat 13-year PCR history as unavailable unless someone probes that era.

---

## Grade B — PIT, but a wasting asset. Not backfillable.

**This is the correction to the earlier plan.** I wrote "daily forward + backfill" for flows. The
register says otherwise, in its own `pit_notes`, and it is unambiguous.

### B1. `www.nseindia.com/api/fiidiiTradeReact` — FII/DII flows

> **`pit_notes`:** *"Immutable, but the endpoint serves only the latest session — there is no date
> parameter. It must be captured daily; a missed day is not recoverable from this URL."*

| | |
|---|---|
| What you get | JSON, 2 records/session (FII, DII): `category, date, buyValue, sellValue, netValue`. Values arrive as **strings** — Decimal on parse, never float |
| Depth | **one session.** No archive. No date parameter |
| Auth | Needs a warm session cookie + browser UA |
| Fallback in register | **NSDL/CDSL monthly** |

**How it helps:** FII net flow is the single best available observable for global risk appetite
reaching Indian equities — it is the transmission channel for Fed policy, EM sentiment and
geopolitical stress, without any need to model the event itself.

**Verdict: CONSIDER — and start capturing today, because the cost of waiting is permanent.** But
be clear-eyed: **a policy using FII flows cannot be backtested at all until enough forward history
accumulates.** If you want FII history for a backtest, the only route is the NSDL/CDSL monthly FPI
series (unprobed, monthly granularity, a different and coarser dataset) — see C5.

### B2. `nsearchives.nseindia.com` — bulk deals / block deals

Same shape: *"the URL is not date-parameterised — it is a rolling current file. Capture daily and
key on the Date column."* No ISIN in the payload — resolve via D2. One useful register note: a
250-byte `block.csv` is a genuinely quiet day with one deal row, **not** a truncated response, and
the quality layer must not treat it as a failure.

**Verdict: CONSIDER, low priority.** Genuinely useful for the T0 "bulk deals in holdings" flow
anomaly the plan's §5.4 names, and near-worthless for a macro regime. Cheap to add to the same
daily job.

### B3. `www.rbi.org.in` — press releases RSS (`curated_rss`)

| | |
|---|---|
| What you get | Headline + link + per-item timestamp. Monetary policy statements, rate decisions, regulatory circulars |
| PIT | Timestamped at source — natural PIT. `NewsRow.ts` required and tz-aware |
| Depth | Whatever the feed window holds — typically weeks. **No archive** |
| Licence | Headlines + links only. `NewsRow` has **no body field** by construction |

**How it helps:** it is the highest-signal-per-byte India macro feed available free, and it is
already active and VERIFIED. A repo-rate change reaches you the day it happens.

**Verdict: CONSIDER — keep it, it is your only live feed.** But understand what it is: Grade B/D.
An RSS window is not a series. If you want a *policy rate series* you must derive it from captured
releases going forward, or find a rate source (C3).

### B4. `data.gdeltproject.org` — GDELT 2.0 export files

| | |
|---|---|
| What you get | One row per detected event: actors (`entities`), average `tone`, source URL, `DATEADDED` (UTC). **No headline** — a GDELT event is actors plus an action |
| PIT | Natural. `DATEADDED` is the source's own instant; no clock is consulted |
| Integrity | The `lastupdate.txt` manifest states the export's MD5, and `gdelt.py` cross-checks stored bytes against it. A mismatch fails loud |
| Archive | Exists — but 96 slots/day. ~405,000 files ≈ 30 GB since its 2015 start |
| HTTPS caveat | Host is a CNAME to GCS and presents a cert for that name, so `https://` fails validation. `http://` is the published path; the payload's own MD5 is the integrity control |

**How it helps — and the honest measurement against it.** This is the only free global geopolitics
feed with real coverage. But D6 measured its GKG sample as *"Australian and US local crime, zero
India-finance content"*, and the schema has **no ticker or ISIN field** — only fuzzy free-text org
names.

**Verdict: CONSIDER for daily forward capture. IGNORE the backfill.** 405,000 slots would be the
largest campaign in this project's history — against 112,870 files for `nse_xbrl_filing` — for a
feed that is not permitted to drive a decision (D6) and cannot be replayed anyway (§7). If global
attention history is ever genuinely wanted, GDELT's own aggregate masterfiles are the route and
their shape needs a probe first.

---

## Grade C — the traps. History that looks usable and is not.

### C1. `api.worldbank.org` — indicator API (`worldbank_indicator_api`, **VERIFIED**)

| | |
|---|---|
| What you get | 66 annual observations in one keyless request (`per_page=100`, no paging). CPI inflation, GDP growth, current account, and hundreds more indicators |
| The killer | **One `lastupdated` for the whole envelope, and no vintage parameter.** A revised figure silently replaces the original |

**Why it is Grade C, precisely:** a fact from here is storable only with `release_date` = the
envelope's `lastupdated`, which makes every observation knowable *now*. The store's release-date
partitioning enforces that rather than leaving it to a comment — which means World Bank facts will
correctly be **invisible to every historical `read_pit`**, by design.

**Verdict: CONSIDER for present-day backdrop only. IGNORE for backtesting.** It is free, keyless,
reliable, and annual. It can tell an LLM "India CPI inflation has run 4–6% recently". It can never
tell a 2019 backtest what was knowable in 2019.

### C2. `mospi.gov.in` / `cpi.mospi.gov.in` — CPI, IIP, GDP

| Surface | Probed result |
|---|---|
| `cpi.mospi.gov.in` | **connect timeout** |
| `www.mospi.gov.in` | robots.txt is a **soft 404** — HTTP 200 carrying the site's HTML shell, byte-identical to `/` |

**How it would help:** CPI (~12th of the following month), IIP (~28th, **revised routinely**), GDP
(quarterly, **revised**) are the actual economic cycle. Nothing else substitutes.

**Verdict: IGNORE as a historical source. CONSIDER as forward capture, if it can be reached at
all.** Two separate problems: it did not answer, and even if it does, MoSPI publishes current
tables, not vintages. **Neither the platform nor anyone else can reconstruct what CPI looked like
before its revision** — so any pre-capture CPI backtest is fabrication regardless of source health.
A soft-404 robots.txt also means there is **no robots policy to comply with**, which is a reason
for extra restraint on request rate, not less.

### C3. `dbie.rbi.org.in` — RBI Database on the Indian Economy

**Probed: TLS certificate hostname mismatch — the cert is not valid for that name.**

This is the one that would carry policy rate, money supply, FX reserves, USD/INR reference rate and
the full macro set for India, with more history than anything else free.

**Verdict: worth ONE careful re-probe, then IGNORE if it fails again.** A cert mismatch is not a
refusal and not something to evade — but it is also not something to work around by disabling
verification, and a platform whose whole discipline is "checksummed bytes from a named source"
should not ingest over an unverifiable TLS identity. Try `rbi.org.in`'s own publication paths
instead (the host already has a robots record and a VERIFIED row for RSS).

### C4. `www.screener.in` — `screener_company_fundamentals` (**BLOCKED_CREDENTIAL**)

Included because it is the existing precedent for how this platform handles Grade C: restated
fundamentals are **structurally quarantined from backtests** (invariant #8, `screen.PitFundamentals`),
monitoring use only. Not a macro source. **Verdict: unchanged — leave as is.**

### C5. NSDL / CDSL — monthly FPI flows *(unregistered, unprobed)*

The register's own stated fallback for `nse_fii_dii_flows`. Monthly, and monthly FPI data is
published as a dated report — which if true would make it **Grade A**, and the only route to
*historical* foreign flow.

**Verdict: PROBE — this is the highest-value unprobed lead on this page.** It is the only candidate
that could give backtestable foreign-flow history, and its absence is what makes B1 a wasting
asset. ~10 requests to settle.

---

## Grade A-if-reachable — the one that changes the answer

### D1. `alfred.stlouisfed.org` — ALFRED (ArchivaL FRED)

**ALFRED serves a series as it was published on a past date.** That is literally the `release_date`
this store partitions by. It is the only surface probed that would make Grade B and C series
backtestable, because it carries the vintages nobody else keeps.

What it would give: US CPI/PCE, Fed funds, US 10Y, DXY, crude — **as first printed** — plus a
number of India series via IFS/OECD mirrors. Every one of the international-politics transmission
channels, with honest release dates.

Probe result: **`last_http_status: 0`** — a documented encoding meaning *requested, no HTTP response
arrived*, which the validator previously conflated with *never requested*. Five attempts:
httpx/HTTP-2 read timeout; curl HTTP/2 `INTERNAL_ERROR` after 0.09 s; curl `--http1.1` timing out
at 45 s with **zero bytes, including on the bare host root**; the same three failing identically
outside the build sandbox. TLS establishes and then nothing arrives. A control request to
`api.worldbank.org` on the same network in the same sweep returned **200**.

That signature is a network-path block, not an application refusal. **There was no status code, no
body, and nothing to evade.**

**Verdict: PROBE FROM THE SERVER, TODAY. Highest information per request on this page.** The
server is quiet (`ops/remote.sh status`: 0 drivers). One command settles whether Tier B has any
history at all, and the answer changes the shape of the whole plan.

---

## Grade X — gated. Decide the gate, not the data.

### X1. Kite Connect historical data — **you already own this, and it is not registered**

I probed the instrument master (it answers unauthenticated) and confirmed:

| Instrument | Token | Segment | Note |
|---|---|---|---|
| `NSE:INDIA VIX` | 264969 | INDICES | **spot India VIX** — better than the `FUTIVX` proxy |
| `NSE:NIFTY 50` | 256265 | INDICES | and 49 more index instruments |
| `CDS:USDINR<exp>FUT` | — | CDS-FUT | currency **futures** (1,547 USDINR instruments). No spot |
| `MCX:CRUDEOIL<exp>FUT` | — | MCX-FUT | crude in **INR/barrel**, `delivery_units: BBL` (2,907 instruments) |

`get_historical_data` returned **"Please log in first using the login tool"** — so depth is
**unverified**, and I did not log in: that is a credential action and it is yours to take.

**How it would help:** this is the cleanest available answer to three of the six transmission
channels — India VIX spot, USD/INR, crude — from **one already-paid-for provider**, as daily OHLC.
A daily close is immutable once printed, so these are genuinely **Grade A** *if* the depth is
there.

**Two real caveats before you count on it:**

1. **Depth is unverified and Kite's historical API is known to be shallower than the exchange
   archives** for some instrument classes. Three calls after one login settle it: `INDIA VIX` at
   2012, 2015, 2019.
2. **The currency and commodity figures are futures, not spot.** A near-month future carries basis
   and rolls; a naive continuous series stitched across expiries introduces jumps that look exactly
   like the split-misread problem already documented in this repo. If you use them, roll
   deliberately and store the roll rule in L2 with a `rule_version`.

**Verdict: PROBE — best cost/benefit on the whole page after ALFRED, because the gate is already
paid.** Then register it properly: it needs a host row, a robots record, a register row, and — being
credential-gated — the same `SecretStr`/`.env` discipline as Kite trading. **Note it is
authenticated, so it is B4/human-gated under §3, and the M8 Kite gate is about *orders*, not
historical reads — those are different questions and should not be conflated in either direction.**

### X2. FBIL — USD/INR reference rate *(unregistered, unprobed)*

Financial Benchmarks India publishes the official daily USD/INR reference rate. If it serves a
dated archive it is **Grade A** and strictly better than a futures proxy — an administered
benchmark, published once, never revised.

**Verdict: PROBE.** ~6 requests. Preferable to Kite's CDS futures for USD/INR if it answers.

### X3. EIA (US Energy Information Administration) — crude *(unregistered, unprobed)*

Free API (key by email), daily Brent and WTI spot with long history, published once and not revised.
Would be **Grade A** for the crude channel, in USD rather than INR/barrel — which is arguably what
you want, since the INR conversion is a separate observable you would rather keep separate.

**Verdict: PROBE.** Resolves the crude licence question cleanly: an explicit public-domain US
government source beats scraping a price you are not licensed to store.

### X4. IMF (SDMX) / OECD *(unregistered, unprobed)*

Both free, both keyless, both carry India macro. Both are **current-vintage** in their default
endpoints, so expect **Grade C** — same trap as World Bank, at higher frequency (IMF IFS is monthly
where World Bank is annual).

**Verdict: LOW PRIORITY.** Only worth it if you want a *monthly* present-day backdrop and ALFRED
has failed. Do not expect vintages.

### X5. Yahoo Finance / `yfinance`

Already in the repo as a **sanity-check tool** (`ops/sanity/l2_yahoo_check.py`), and it earned its
place there — it independently confirmed the UNOMINDA/HINDPETRO/BEARDSELL adjustment gaps.

**Verdict: KEEP AS A CHECKER. IGNORE AS A STORED SOURCE.** Two distinct reasons, and the second is
the one that matters: (a) its terms do not clearly permit storing a redistributed archive, which is
a §10 question, not an engineering one; (b) **an independent cross-check stops being independent
the moment it becomes an ingested source.** Its whole value is being outside the lake.

### X6. Paid and account-gated — GDELT BigQuery, NewsAPI, Trading Economics, CMIE

Already on the record: GDELT via BigQuery returns 401 and needs a GCP project **plus a billing
account** (query cost normally within free tier — the account itself is the gate); NewsAPI.org
returns 401 `apiKeyMissing` and needs a paid key for anything beyond non-commercial dev use. Both
are human-gated under §3 (items 4 and 9). Trading Economics and CMIE Economic Outlook are the
commercial India-macro vendors; CMIE is the one that genuinely has vintages, and it is priced for
institutions.

**Verdict: IGNORE all four for now.** Logged so nobody reaches for them silently. Revisit only if a
task genuinely needs article-level global news volume, or if the ALFRED probe fails *and* you
decide a vintage-correct macro backtest is worth paying for. That would be a real decision with a
real number attached, not a source swap.

---

## The recommendation, compressed

| Do now — free, no decision needed | Why |
|---|---|
| **ALFRED re-probe from the server** | Settles whether *any* Grade B/C series is ever backtestable |
| **`niftyindices.com` archive-depth bisect** | May let M11.2 and M10.4 run concurrently instead of serialised |
| **NSDL/CDSL FPI probe** | The only candidate for backtestable foreign-flow history |
| **FBIL + EIA probes** | Would make USD/INR and crude Grade A on public sources |
| **Kite: you log in, then 3 depth calls** | Confirms India VIX spot depth on a gate you already paid |

| Decide | Recommendation |
|---|---|
| M11.2 `go` | **Yes, first** — 2,470 immutable CSVs, no format eras, unlocks four datasets and M3.9's TRI fallback |
| Start daily capture of B-grade sources | **Yes, today.** FII/DII and deals are unrecoverable per uncaptured day |
| GDELT backfill | **No.** Forward only |
| World Bank / MoSPI / IMF for backtests | **No.** Backdrop only, and the store will correctly hide them from `read_pit` |
| Kite for USD/INR and crude | **Only after FBIL and EIA fail** — prefer spot benchmarks over rolled futures |

**The one-line summary: your backtestable macro history is almost entirely in a single 17 KB CSV
per session that nobody has fetched yet, and almost everything else free is either forward-only or
vintage-less.** That is not a pessimistic reading — it means the highest-value action is also the
cheapest one, and it is waiting on a `go`.

---

## §9. Corrections to `macro-news-ingestion-plan-2026-09-07.md`

1. **Tier A said FII/DII was "daily forward + backfill". It is forward-only.** The register's
   `pit_notes` state the endpoint serves the latest session with no date parameter and *"a missed
   day is not recoverable from this URL"*. Same for bulk and block deals. Corrected in the plan.
2. **Tier A implied 13-year depth for the VIX/F&O row.** `nse_fo_bhavcopy`'s era starts
   **2024-07-08** (UDiFF); the pre-Jul-2024 legacy F&O format is not registered. Corrected in the
   plan.

Neither correction changes the plan's conclusion — it strengthens it. Both point the same way: the
`ind_close_all` backfill is carrying more of the backtestable panel than the first draft credited,
and the forward-capture job is more urgent than it credited, because three of its sources lose
history every day they are not run.
