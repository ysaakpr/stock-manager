# macro-probes — macro / backdrop sources probed, registered, and capturing

**Date:** 2026-10-06 (all times IST). **Machine:** the campaign server (AWS, single-machine model).
**Method:** C.1's sweep method (AGENTIC_CONTEXT §8) — robots first per host, ≥3 s spacing (≥4 s on
the probe helper), one browser UA, no cookie, no login, no retry with another agent, hard stop on a
refusal. **No TLS check was disabled or weakened anywhere.** ≤10 probe requests per host. NSE hosts
were touched only while their lease was free and outside 18:00–20:30 (13:30–13:31, both lease files
absent at the time).

Read with: `ops/gates/macro-news-ingestion-plan-2026-09-07.md` (§5 Tiers A′ and B),
`ops/gates/macro-news-provider-evaluation-2026-09-07.md`, `ops/gates/M11-macro-source-probe.md`.

## 0. Outcome per source

| Source | Register id | Verdict | Grade | Captured into the lake |
|---|---|---|---|---|
| ALFRED / FRED vintages | `alfred_series_vintage` | **FAILED** again, now from a second network | — | nothing |
| USD/INR (and 6 other INR pairs) — FBIL | `fbil_reference_rates` **(new)** | **VERIFIED** | A (dated archive, never revised) | L0: 9 windows, 2018-07-10 → 2026-09-29 (see §4 — L1 rows withheld) |
| USD/INR before 2018 — RBI archive | `rbi_reference_rate_archive` **(new)** | **VERIFIED** (probe only) | A | nothing — needs a form-postback capture (§3.2) |
| Policy repo / SDF / MSF / bank rate / CRR / SLR | `rbi_current_rates` **(new)** | **VERIFIED** | B (forward only) | 7 facts, release 2026-10-06 |
| WPI (headline + 3 major groups + food) | `oea_wpi_monthly_index` **(new)** | **VERIFIED** | B | 205 facts (Apr-23 … Aug-26), release 2026-10-06 |
| GST gross / net collections | `gstn_tax_collection` **(new)** | **VERIFIED** | B | 58 facts (Apr-24 … Aug-26), release 2026-10-06 |
| India VIX **spot** | `nifty_india_vix_history` **(new)** | **VERIFIED** | A | nothing yet — NSE host, backfill left for the coordinator (§5) |
| World Bank (8 India indicators) | `worldbank_indicator_api` | VERIFIED (parser now built) | C (current vintage) | L0: 8 payloads (see §4 — L1 rows withheld) |
| CPI, IIP, GDP — MoSPI | `mospi_api` **(new)** | **FAILED** — TLS requires unsafe legacy renegotiation | — | nothing |
| RBI DBIE | `rbi_dbie` **(new)** | **FAILED** — certificate hostname mismatch (again) | — | nothing |
| Brent, gold via yfinance | — | **not stored** — open §10 licence question | — | nothing |

Scheduler jobs added (`dataplatform/scheduler/registry.py`): **`fbil_reference_rates`** (daily,
16:00 Mon–Fri) and **`macro_release_capture`** (weekly, Sunday 10:00: World Bank, RBI rates, WPI,
GST, India VIX trailing month). Both take a per-host lease per step, so one busy or failing host
never blocks the others; the job raises at the end naming every failed step.

## 1. Every request made

### 1.1 Probes (48)

| Time | Host | Request | Status | Bytes | Parse / note |
|---|---|---|---|---|---|
| 13:26:29 | alfred.stlouisfed.org | GET /robots.txt | 0 | — | ReadTimeout at 40 s, zero bytes |
| 13:27:13 | fred.stlouisfed.org | GET /robots.txt | 0 | — | ReadTimeout at 40 s |
| 13:28:00 | alfred.stlouisfed.org | GET /graph/alfredgraph.csv?id=INDCPIALLMINMEI&vintage_date=2020-01-15 | 0 | — | ReadTimeout at 40 s |
| 13:28:44 | fred.stlouisfed.org | GET / | 0 | — | ReadTimeout at 40 s |
| 13:29:28 | api.worldbank.org | GET /v2/country/IND/indicator/FP.CPI.TOTL.ZG?format=json&per_page=100 | 200 | 14,939 | control; byte-identical to the M11.1 sample (sha256 32b62553…) |
| 13:29:55 | alfred.stlouisfed.org | GET / (curl -v --http1.1, 20 s cap) | 0 | — | DNS → 23.38.63.56 (Akamai), TLS 1.3 OK with a valid DigiCert cert for the name, then no HTTP response |
| 13:30:34 | niftyindices.com | POST /BackPage/getHistoricaldatatabletoString — INDIA VIX 01-Mar-2008..31-Mar-2008 | 200 | 2 | `[]` |
| 13:30:35 | nsearchives.nseindia.com | GET /content/indices/ind_close_all_01102012.csv | 200 | 2,900 | 30 indices, **no India VIX row** |
| 13:30:49 | niftyindices.com | POST … INDIA VIX 01-Sep-2026..10-Sep-2026 | 200 | 1,436 | 8 sessions, OHLC; frozen |
| 13:30:49 | nsearchives.nseindia.com | GET …/ind_close_all_01102013.csv | 200 | 3,403 | no India VIX row |
| 13:30:53 | nsearchives.nseindia.com | GET …/ind_close_all_01102014.csv | 200 | 4,260 | **India VIX row present** (01-10-2014 close 13) |
| 13:31:06 | niftyindices.com | POST … INDIA VIX 01-Jan-2010..15-Jan-2010 | 200 | 2 | `[]` |
| 13:31:15 | niftyindices.com | POST … INDIA VIX 01-Oct-2014..10-Oct-2014 | 200 | 2 | `[]`; frozen |
| 13:31:26 | niftyindices.com | POST … INDIA VIX 02-Mar-2020..06-Mar-2020 | 200 | 900 | 5 sessions; frozen |
| 13:31:44 | www.fbil.org.in | GET /robots.txt | 404 | 431 | Tomcat 404 page — no policy |
| 13:31:48 | www.fbil.org.in | GET / | 200 | 1,519 | Angular shell |
| 13:31:58 | www.fbil.org.in | GET /main.12b820d6bf685718ad3d.js | 200 | 5,213,386 | the site's own bundle: API base `/wasdm`, `refrates/fetch` and `refrates/fetchfiltered?fromDate&toDate&authenticated=false` |
| 13:32:55 | www.fbil.org.in | GET /wasdm/refrates/fetch?authenticated=false | 200 | 1,919 | 14 rows, newest 2026-09-29 (USD 96.0321); frozen |
| 13:32:59 | www.fbil.org.in | GET /wasdm/refrates/fetchfiltered?fromDate=2018-07-09&toDate=2018-07-13&authenticated=false | 200 | 2,185 | 16 rows from 2018-07-10 (USD 68.7942); frozen |
| 13:33:19 | www.rbi.org.in | GET /robots.txt | 418 | 626 | "Unauthorised Access" — no policy (unchanged since M6.1) |
| 13:33:23 | www.rbi.org.in | GET /scripts/ReferenceRateArchive.aspx | 200 | 79,834 | ASP.NET form, hidden state fields, DD/MM/YYYY range |
| 13:33:50 | www.rbi.org.in | POST ReferenceRateArchive.aspx — USD 02/01/2012..31/01/2012 | 200 | 84,370 | 21 rows, 53.2975 → 49.6825 |
| 13:34:15 | www.rbi.org.in | POST … USD 01/01/2004..31/01/2004 | 200 | 84,362 | 21 rows, 45.6100 → 45.3100 |
| 13:34:25 | www.rbi.org.in | POST … USD 01/07/2018..31/07/2018 | 200 | 81,652 | 02–09 Jul 2018 + a lone 24/07/2018 69.0530 |
| 13:34:55 | cpi.mospi.gov.in | GET / | 0 | — | ConnectTimeout at 40 s |
| 13:35:38 | esankhyiki.mospi.gov.in | GET /robots.txt | 200 | 319 | `Allow: /` |
| 13:35:41 | esankhyiki.mospi.gov.in | GET / | 200 | 1,198 | React shell |
| 13:35:44 | dbie.rbi.org.in | GET / | 0 | — | CERTIFICATE_VERIFY_FAILED: hostname mismatch |
| 13:35:47 | eaindustry.nic.in | GET /robots.txt | 404 | 4,849 | site 404 page — no policy |
| 13:35:50 | gstcouncil.gov.in | GET /robots.txt | 200 | 1,706 | Drupal default (admin/user paths disallowed) |
| 13:35:59 | esankhyiki.mospi.gov.in | GET /llms.txt | 200 | 7,028 | dataset list; points at an MCP server |
| 13:36:07 | esankhyiki.mospi.gov.in | GET /.well-known/ard.json | 200 | 24,418 | dataset manifest, HTML entry points only |
| 13:36:13 | esankhyiki.mospi.gov.in | GET /static/js/main.47408a9d.js | 200 | 2,893,299 | API base `https://api.mospi.gov.in/api/esankhyiki/` |
| 13:37:39 | api.mospi.gov.in | GET /robots.txt | 0 | — | UNSAFE_LEGACY_RENEGOTIATION_DISABLED |
| 13:37:43 | api.mospi.gov.in | GET /api/esankhyiki/cpi/getCpiBaseYear | 0 | — | same |
| 13:37:47 | api.mospi.gov.in | GET /api/cpi/getCPIIndex?… | 0 | — | same |
| 13:38:05 | www.rbi.org.in | GET /Home.aspx | 200 | 150,447 | "Current Rates" panel, all 7 rates parse; frozen |
| 13:38:08 | www.mospi.gov.in | GET /robots.txt | 200 | 2,644 | soft 404 (JS shell), as on 2026-09-04 |
| 13:38:11 | eaindustry.nic.in | GET / | 200 | 38,354 | WPI / ICI navigation |
| 13:38:15 | gstcouncil.gov.in | GET / | 200 | 94,904 | links www.gst.gov.in/download/gststatistics |
| 13:38:47 | www.gst.gov.in | GET /robots.txt | 404 | 6,687 | portal 404 page — no policy |
| 13:38:51 | www.gst.gov.in | GET /download/gststatistics | 200 | 23,079 | links the tutorial.gst.gov.in xlsx files |
| 13:38:54 | eaindustry.nic.in | GET /download_data_2223.asp | 200 | 20,270 | links `wpi_monthly_index_202609.xlsx`; frozen |
| 13:38:57 | eaindustry.nic.in | GET /wpi_press_release_archive.asp | 200 | 44,705 | dated press-release PDFs (not used) |
| 13:39:00 | www.mospi.gov.in | GET /press-release | 200 | 2,644 | the same JS shell as robots.txt — soft 404 |
| 13:39:30 | eaindustry.nic.in | GET /indx_download_2223/wpi_monthly_index_202609.xlsx | 200 | 323,839 | 1,138 rows × Apr-23..Aug-26; frozen |
| 13:39:33 | tutorial.gst.gov.in | GET /robots.txt | 200 | 75 | `Allow: /` |
| 13:39:36 | tutorial.gst.gov.in | GET /offlineutilities/gst_statistics/Gross_Net_Tax_collection.xlsx | 200 | 192,489 | 29 month sheets; frozen |

Per host: alfred 3, fred 2, worldbank 1, niftyindices 5, nsearchives 3, fbil 5, rbi 6, cpi.mospi
1, esankhyiki 5, api.mospi 3, dbie 1, eaindustry 5, gstcouncil 2, www.gst.gov.in 2, www.mospi 2,
tutorial.gst 2. No 403 anywhere; no 429.

### 1.2 Captures (21), through the platform's `Fetcher` under host leases

| Time | Host | What | Requests | Result |
|---|---|---|---|---|
| 13:58:10–13:58:35 | www.fbil.org.in | `macro.capture fbil` — 2018-07-10..2026-10-06 in 365-day windows | 9 | 200 × 9, 49–165 kB each; 1,988 sessions, 8,316 facts parsed |
| 13:58:42–13:59:03 | api.worldbank.org | 8 indicators | 8 | 200 × 8, 13–16 kB; 490 facts, vintage `lastupdated` 2026-07-13 |
| 13:59:04 | www.rbi.org.in | /Home.aspx | 1 | 200, 150,343 B; 7 facts |
| 13:59:04–13:59:07 | eaindustry.nic.in | download page + the xlsx it links | 2 | 200; 205 facts |
| 13:59:14 | tutorial.gst.gov.in | the collection workbook | 1 | 200, 192,489 B; 58 facts |

## 2. ALFRED — re-probed from the server: FAILED, and it is the edge, not the path

Four requests on the platform's HTTP client and one `curl -v` from the campaign server reproduce
the laptop's signature exactly: DNS resolves `alfred.stlouisfed.org` to an Akamai edge
(`e13502.b.akamaiedge.net`, CNAME chain through `kona-prod.stlouisfed.org`), TLS 1.3 completes with
a **valid** certificate for the name, and then no HTTP response arrives — 40 s read timeouts on
`/robots.txt`, the bare root and the verified CSV URL, on both `alfred.` and `fred.`. The World Bank
control in the same minute answered 200 in 0.11 s.

Two different networks (a home ISP and AWS), one behaviour: a CDN that accepts the connection and
then answers nothing is making a bot-management decision about where the request comes from. That
is a refusal, and it is not worked around — no other UA, no proxy, no other route. The register row
stays **FAILED** with the new evidence, the two host records are re-dated, and nothing is scheduled.

**What it would have carried** — from FRED's published catalogue, *not verified from here*, since
no response ever arrived: India CPI (`INDCPIALLMINMEI`, the register's verified_url), OECD MEI
mirrors of industrial production and interest rates, INR/USD, and Brent, with vintages. Two routes remain and **both are the owner's**: FRED's keyed API
(`api.stlouisfed.org`, a free key is still an account — §3.7) or a re-probe from a residential
network.

**Consequence for Tier B:** there is still no free historical-vintage source for Indian macro.
B2 does not happen; Tier B is forward capture with a hard "backtestable from first capture" boundary.

## 3. USD/INR reference rate

### 3.1 FBIL — VERIFIED, Grade A, backfilled to L0

FBIL's site is an Angular app over a keyless JSON API; its own bundle names the anonymous calls.
`/wasdm/refrates/fetchfiltered?fromDate=YYYY-MM-DD&toDate=YYYY-MM-DD&authenticated=false` is a dated
archive addressable by any window, starting **2018-07-10** (the day FBIL took the benchmark over;
a 2018-07-09..13 window starts on the 10th). Seven pairs: USD, GBP, EUR, JPY per 100, AED,
IDR per 10,000, and RUB (later, published 15:00).

PIT: each row has `processRunDate` and `displayTime` (13:30 IST in 2018, 13:00 in 2026). Facts land
with `period_end = release_date = processRunDate`, `revision_seq` 0. **The free site lags** — on
2026-10-06 its newest row was 2026-09-29 — but that delays our capture, not the knowable date: the
RBI home page fetched the same afternoon quotes "INR / 1 USD : 96.4348 (As at 1.00pm of October 06,
2026) (Source : FBIL)". The daily job therefore asks for a trailing 21 days.

Backfill: **9 requests**, 1,988 sessions × up to 7 pairs = 8,316 facts, all in L0 under
`data/L0/fbil_reference_rates/`.

### 3.2 RBI website — the pre-2018 archive VERIFIED, not yet captured

`www.rbi.org.in/scripts/ReferenceRateArchive.aspx` is an ASP.NET form; a postback with the page's
own hidden state answers a dated table. Verified back to **January 2004** (deeper is unmeasured)
and up to 2018-07-09, where FBIL's series begins — so the two together give one unbroken USD/INR
reference series from at least 2004. Registered as `rbi_reference_rate_archive`
(`cadence: backfill_only`, era end 2018-07-10). **Not captured:** the platform `Fetcher` has no
form-postback capture (GET the form into L0, then POST its state), and building one plus a depth
bisect is a follow-up of ~15–25 requests (one postback per year). One anomaly to explain before use:
the July 2018 response carries a lone 24/07/2018 row after the hand-over.

### 3.3 RBI DBIE — FAILED again

`dbie.rbi.org.in`: "certificate verify failed: Hostname mismatch". Not worked around. Registered as
`rbi_dbie` FAILED with a host record.

## 4. Hazard found while capturing — L1 rows withheld, coordinator action needed

While these captures ran, another worktree's **M11.2 index-valuation backfill** was writing the same
dataset (`dataplatform.ingest.macro.backfill`, pid 3865034, `--from 2012-10-01 --to 2026-10-05
--stop-before 17:50`, then at ~2015-02 and advancing ~18 sessions/min). `macro_series.write_release`
merges into a date partition by *reading it back*, and that process runs the pre-PR code, whose
`Unit` enum has no `INR` or `USD`. The FBIL facts (unit `INR`) sit in every session partition from
2018-07-10, and the World Bank facts (`USD`/`INR`) in 2026-07-13. **When M11.2 reaches 2018-07-10
(estimated ~14:45 IST) its read of that partition will raise** `ValueError: 'INR' is not a valid
Unit`.

I tried to withdraw my 1,988 FBIL/World Bank partitions from L1 (every one holds only this task's
rows, verified by a dry run; L0 untouched, so they are exactly re-derivable) and the action was
**denied by the auto-mode permission classifier** as irreversible local deletion. I have not tried
another route to that outcome. So the coordinator needs to choose one of:

- **A.** Let M11.2 stop at its error (it is resumable per session), merge this PR (which adds
  `Unit.INR`/`USD`), and restart M11.2 from the new code — it then merges cleanly into these partitions.
- **B.** Remove the FBIL and World Bank partitions now (my dry run lists exactly 1,988, all with
  only `fbil_reference_rates`/`worldbank_indicator_api` rows) and re-derive after the merge with
  `macro.capture fbil --end 2026-10-06` and `macro.capture weekly --only worldbank_indicator_api`
  — both find their payloads already in L0 and make **zero requests** (same day only for the
  weekly one; on a later day it would fetch a new vintage instead).

The RBI-rates, WPI and GST facts (units PCT / INDEX / INR_CRORE, partition 2026-10-06, beyond M11.2's
`--to`) do not trigger this.

A second, smaller point for the same reason: `write_release` is not safe against two *processes*
writing the same partition at the same moment (read-merge-rename; last rename wins). Running two
`macro_series` writers over the same date range concurrently can lose one writer's facts. The VIX
backfill below must not run concurrently with M11.2.

## 5. India VIX spot — VERIFIED; backfill left for the coordinator

`niftyindices.com/BackPage/getHistoricaldatatabletoString` with the same `cinfo` body
`tri_request_body` builds answers `INDIA VIX` daily OHLC. Depth: empty for windows in 2008-03,
2010-01 and 2014-10; rows in 2020-03 and 2026-09 — so the endpoint starts somewhere in 2014-10 ..
2020-03. Independently, `ind_close_all` (the M11.2 file) carries an "India VIX" row from between
2013-10-01 and 2014-10-01 onward, so M11.2 already lands India VIX *close* from ~2014; this endpoint
adds OPEN/HIGH/LOW and is a second NSE file for the same close (`IN.NSE.INDIA_VIX.CLOSE` — one
series; if the two files ever disagree on a session the write is refused, not tie-broken).

**Not run** (NSE host, another agent holds the NSE queue). The command, for after M11.2 has finished
(see §4: never concurrently, and after this PR is merged):

```bash
cd /home/ubuntu/stock-manager && DATA_ROOT=/home/ubuntu/stock-manager/data \
  uv run python -m dataplatform.ingest.macro.capture india-vix --start 2008-03-01 --end 2026-10-06
```

**1 POST** to `niftyindices.com` (the whole range in one request, as the TRI backfill does; the
dry run says so: `… india-vix --dry-run`). Takes the `niftyindices.com` lease; exits rather than
waits if it is held. Outside 18:00–20:30. The weekly job then keeps the trailing 31 days current.

## 6. World Bank — parser built, eight India series

`dataplatform/ingest/macro/worldbank.py`. `release_date` is the envelope's `lastupdated`
(2026-07-13), so no World Bank fact is visible to any `read_pit` before that date — asserted by a
test that reads 2010-06-30 and 2026-07-12 and gets nothing. A value for a year not ended by the
vintage is refused. Series: CPI inflation, real GDP growth, GDP-deflator inflation, GDP (current
US$), current account (% GDP), INR per USD (annual average), total reserves (US$), lending rate —
490 facts, 1960–2025. In L0; L1 rows subject to §4.

## 7. Tier B forward capture

| Release | Probe | Outcome |
|---|---|---|
| **CPI, IIP, GDP** (MoSPI) | www.mospi.gov.in answers every path with one 2,644 B JS shell (soft 404); cpi.mospi.gov.in connect-times-out; eSankhyiki is a React shell whose data API is api.mospi.gov.in, which **requires unsafe legacy TLS renegotiation** | **FAILED** (`mospi_api`) — enabling legacy renegotiation is weakening TLS and is not done. Candidates: a MoSPI server fix; its MCP server (unprobed, terms unknown); World Bank annual as backdrop |
| **WPI** (OEA, DPIIT) | download page + monthly xlsx | **VERIFIED**, weekly capture |
| **GST collections** (GSTN) | gststatistics page → tutorial.gst.gov.in xlsx | **VERIFIED**, weekly capture |
| **Repo rate** (+ SDF, MSF, bank rate, reverse repo, CRR, SLR) | RBI home page "Current Rates" | **VERIFIED**, weekly capture; DBIE FAILED |

Release dates: none of the three Tier B files states a release *day* (the WPI filename states the
release month). Facts carry `release_date` = **the capture date** — never earlier than publication,
so never a leak — and a capture writes only observations that are **new or changed** against what
the store already knew (`capture.new_or_revised`). A revised provisional WPI or GST month therefore
lands as a second record on the capture that first saw it, and an unchanged week writes nothing
(both asserted). The RBI panel states rates *in force* with no effective date; each capture is a
dated observation of state (frequency `EVENT`), never back-dated to an MPC meeting.

Measured first capture (2026-10-06): repo 5.25%, SDF 5.00%, MSF 5.50%, bank rate 5.50%, fixed
reverse repo 3.35%, CRR 3.00%, SLR 18.00%; WPI all-commodities Aug-26 110.8 (2022-23 = 100); GST
gross Aug-26 ₹1,99,852.74 cr, net ₹1,68,057.35 cr.

## 8. Commodities — not stored

Brent and gold via yfinance remain an open licence question (§10; the provider evaluation's X5:
storing it is a redistribution question, and an independent checker stops being independent once it
is ingested). Nothing stored, nothing registered. The licence-clean routes are EIA (keyed — an
account, owner's call) and MCX settlement prices (unprobed). ALFRED would have carried Brent too.

## 9. Register and plan changes

- New sources under §4.1 row 17 "Macro / economic backdrop" (still **PROPOSED** in EXECUTION_PLAN
  §12; no new §4.1 row is needed, so no new §12 entry): `fbil_reference_rates`,
  `rbi_reference_rate_archive`, `rbi_current_rates`, `oea_wpi_monthly_index`,
  `gstn_tax_collection`, `nifty_india_vix_history` (VERIFIED); `mospi_api`, `rbi_dbie` (FAILED).
- `alfred_series_vintage` stays FAILED with the server evidence; fred/alfred host records re-dated.
- New host records with their robots outcome: www.fbil.org.in, eaindustry.nic.in, www.gst.gov.in,
  tutorial.gst.gov.in, esankhyiki.mospi.gov.in, api.mospi.gov.in, cpi.mospi.gov.in,
  dbie.rbi.org.in; www.rbi.org.in's record names the two extra pages read.
- `worldbank_indicator_api` moves from `UNSCHEDULED` to `macro_release_capture`; `mospi_api` and
  `rbi_dbie` are `UNSCHEDULED` with their failure as the reason.
- `macro.models.Unit` gains `INR` (an FX quote; the quantity is in the `series_id`, e.g.
  `IN.FBIL.INR_PER_100_JPY.REFERENCE`) and `USD`.

## 10. What is deliberately not claimed

- That any Tier B series is backtestable before 2026-10-06. It is not, and the store holds no
  earlier partition for it.
- That the World Bank series can inform a historical decision. Grade C: invisible before its vintage.
- That FBIL's public archive is real-time. It lags publication by a few sessions; the rate's
  knowable date is its publication, the capture follows.
- That the RBI archive's pre-2004 depth is known, or that the 24/07/2018 row is understood.
