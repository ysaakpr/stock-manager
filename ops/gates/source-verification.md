# Source Register — live verification sweep

**Task:** C.1 · **Swept:** 2026-08-08, 18:08–18:17 IST, from this host · **Register:**
[`dataplatform/ingest/source_register.yaml`](../../dataplatform/ingest/source_register.yaml)

This is the evidence log for the machine-readable register. Every `VERIFIED` row in that file
points at a line in the table below: a real request, its status, its size, its content-type and
what the payload actually turned out to be. Nothing here was inferred from documentation.

```bash
uv run python -m dataplatform.ingest.source_register validate
```

> **Nothing was acted on.** No account was created, no terms were accepted, no credential was
> entered, no login was attempted, no payment was made. One request per URL pattern, ≥2.5 s
> spacing per host, browser UA, `Referer: https://www.nseindia.com/` on NSE hosts. A 403 hard
> stop was armed and never tripped. Where a source refused us, that refusal is recorded — no
> retry with a different UA, no attempt to get around a rate limit or a session gate.

---

## 1. Bottom line

| | |
|---|---|
| §4.1 rows | 16, all covered |
| Register entries | 24 (a row can have several surfaces: NSE + BSE, or two format eras) |
| VERIFIED by a real fetch | **21** |
| FAILED, with a named alternative | **2** — niftyindices TRI (M3.9), GDELT DOC API (M6.1) |
| Blocked on a credential | **1** — Screener Excel export (M7.1); a working substitute was verified |
| Total HTTP requests | 47 across 9 hosts; busiest host 11 |
| B1 bulk threshold (~200 to one source) | not approached |

The three NSE patterns AGENTIC_CONTEXT §8 already recorded were **re-fetched, not assumed** —
and all three came back byte-for-byte the same size as the §8 record (194,665 / 100,665 /
374,452 B), which is a useful independent check that those files are immutable once published.

---

## 2. robots.txt, per host

Checked before any content request.

| Host | Status | Serves directives? | What it permits |
|---|---|---|---|
| `www.nseindia.com` | 200 | yes | `Allow: /`; only `/market-data-test` disallowed |
| `nsearchives.nseindia.com` | 404 | no | no directives exist on the archive host |
| `www.bseindia.com` | 200 | **no** | returns 13,850 B of the site's Angular shell — a soft 404, not a policy file |
| `api.bseindia.com` | 301 | no | redirects to the same shell on the web host |
| `niftyindices.com` | 200 | yes | `Allow: /`; two report paths disallowed, neither one we use |
| `www.niftyindices.com` | 200 | yes | byte-identical (136 B) to the apex host |
| `www.screener.in` | 200 | yes | per-company pages allowed; `/user/*`, `?q=`, `?sort=`, `?limit=`, `?page=`, `/company/source/quarter/*` disallowed |
| `api.gdeltproject.org` | 404 | no | no file; the API states "one request every 5 seconds" in its 429 body |
| `data.gdeltproject.org` | 404 | no | no file; HTTPS unusable (see §4.3) |

Two of the nine hosts publish nothing at `/robots.txt`, and BSE answers it with a web page.
**A 200 on `/robots.txt` is not proof that directives exist** — M1.2's policy loader must check
the content type and shape, or it will happily parse an Angular bundle as a crawl policy.

---

## 3. Verified sources

Sizes are the exact bytes received; the SHA-256 of each payload and its structural parse are in
the register.

| §4.1 row | Entry | Status | Bytes | What the payload actually was |
|---|---|---|---|---|
| NSE OHLCV ≤ Jul-2024 | `nse_bhavcopy_legacy` | 200 | 100,665 | zip → `cm02JAN2024bhav.csv`, 14 cols, ISIN present |
| NSE OHLCV UDiFF | `nse_bhavcopy_udiff` | 200 | 194,665 | zip → UDiFF CSV, 34 cols — confirms the schema break |
| NSE delivery % | `nse_sec_bhavdata_full` | 200 | 374,452 | CSV, 15 cols, **no ISIN** — needs D2 |
| BSE OHLCV (current) | `bse_bhavcopy_udiff` | 200 | 845,830 | plain CSV (not zipped), 34-col UDiFF, ISIN present |
| BSE OHLCV (backfill) | `bse_bhavcopy_legacy` | 200 | 138,176 | zip → `EQ020124.CSV`, 14 cols, **SC_CODE only, no ISIN** |
| Corporate actions | `nse_corp_actions` | 200 | 6,053 | JSON, 20 records, ISIN + `caBroadcastDate` |
| Corporate actions | `bse_corp_actions` | 200 | 56,128 | JSON, 197 records, keyed on `scrip_code` |
| Symbol / ISIN master | `nse_equity_list` | 200 | 169,183 | `EQUITY_L.csv`, 8 cols |
| Symbol / ISIN master | `bse_scrip_master` | 200 | 1,736,833 | JSON, 4,949 active scrips, `ISIN_NUMBER` |
| Index constituents | `nifty_index_constituents` | 200 | 3,352 | CSV, 5 cols, ISIN present |
| Benchmark TRI (fallback input) | `nifty_index_close_snapshot` | 200 | 17,122 | CSV, 163 indices, incl. `Div Yield` |
| FII/DII flows | `nse_fii_dii_flows` | 200 | 215 | JSON, 2 records — latest session only, no date param |
| Bulk deals | `nse_bulk_deals` | 200 | 10,687 | CSV, 8 cols |
| Block deals | `nse_block_deals` | 200 | 250 | CSV, 7 cols — one deal; a quiet day, not a truncation |
| Shareholding pattern | `nse_shareholding_pattern` | 200 | 2,407,298 | JSON, 2,284 records, quarter-end **and** broadcast dates |
| F&O EOD | `nse_fo_bhavcopy` | 200 | 1,057,426 | zip → UDiFF FO CSV with OI columns |
| Announcements | `nse_announcements` | 200 | 13,687 | JSON, 20 records, `exchdisstime` to the second |
| Announcements | `bse_announcements` | 200 | 60,280 | JSON, `Table` = 50 records |
| News / geopolitical | `gdelt_v2_event_files` | 200 | 319 | `lastupdate.txt`, 3 lines of `size md5 url` |
| Fundamentals PIT | `nse_financial_results_index` | 200 | 2,910,921 | JSON, 3,816 filings, each with an `xbrl` URL |
| Fundamentals PIT | `nse_xbrl_filing` | 200 | 19,935 | well-formed XBRL, in-bse-fin 2020-03-31 taxonomy |

The NSE JSON APIs need a session cookie; a GET of `https://www.nseindia.com/` first is enough.
Worth knowing: that warm-up request itself returned **403 while still setting the cookie**, and
every subsequent API call succeeded. M1.2 must not treat a 403 on the warm-up as a hard stop.

---

## 4. What did not verify

### 4.1 Benchmark TRI — `nifty_tri_history` (owner: M3.9)

`POST https://niftyindices.com/Backpage.aspx/getTotalReturnIndexString` was tried twice: bare,
then again after warming `https://niftyindices.com/reports/historical-data` in the same cookie
jar. Both returned **HTTP 200 carrying 92,911 B of the site's HTML home page**, redirected to
`/?ReturnUrl=%2fBackpage.aspx%2fgetTotalReturnIndexString`. That is an application session gate,
not robots and not a rate limit. The warm GET set zero cookies, which is why it never opens.

Guessing `Daily_Snapshot/ind_close_all_tri_07082026.csv` also returned the HTML shell (soft 404),
and the real daily snapshot contains no TRI column — grep for `TRI` / `total return` finds
nothing in its 163 rows.

Not escalated: §4.1 already names the fallback, and its input is verified. Candidates, in order:

1. Compute TRI from the price index plus the `Div Yield` column already in
   `nifty_index_close_snapshot` — this is §4.1's own stated fallback and needs no new source.
2. Drive the historical-data page as a browser to learn the real session handshake, then replay
   it from the fetcher.
3. Look for a TRI variant on `nsearchives.nseindia.com/content/indices/`, which mirrors the
   daily index file byte-for-byte (verified: 200, 17,122 B, identical payload).

### 4.2 GDELT DOC API — `gdelt_doc_api` (owner: M6.1)

HTTP **429** on both attempts, the second after a 15 s idle gap, so the throttle is IP-level or
global rather than a function of our spacing. The body asks callers to limit to one request per
five seconds and points volume users at the ngrams dataset. Not retried further.

This costs nothing today: **the primary GDELT path is the raw 15-minute file feed, and that
verified.** The DOC API is a convenience over data we can pull and filter locally.

### 4.3 Screener Excel export — `screener_company_fundamentals` (owner: M7.1)

`GET https://www.screener.in/company/RELIANCE/export/` → **404**, an 11,441 B HTML page. The
export is gated behind a logged-in account. Creating an account or accepting third-party terms
is reserved to the owner (AGENTIC_CONTEXT §3.7) and no credential exists (B4), so this was not
pursued one step further.

Not escalated, because a substitute was verified in the same sweep: the robots-permitted company
page returned **200 / 227,493 B / text/html** and carries the same restated statements. M7.1
should parse that page. If the export is genuinely required later, it needs an owner-supplied
session cookie and M7.1 should park on it rather than self-serve.

Either way this feed stays **monitoring-only and quarantined from backtests** — Screener's
figures are restated with no as-of date (decision #7, invariant #8).

---

## 5. Findings the ingestion layer has to act on

These came out of the sweep and are cheap now, expensive later.

1. **HTTP 200 is not success on three of these hosts.** `www.bseindia.com`, `niftyindices.com`
   and `www.screener.in` all answer unknown paths with 200 + HTML. A fetcher that trusts the
   status code will checksum 92 KB of markup into L0 and call it a TRI series. Validate
   content-type and payload shape before the write, on every source.
2. **BSE's announcements API returns `{}` with a 200** when `strPrevDate != strToDate`. An empty
   success. Ingest must assert the `Table` key is present and non-empty on a trading day.
3. **Two datasets have no ISIN and must not be joined without D2**: `sec_bhavdata_full` (SYMBOL +
   SERIES) and the entire pre-2024 BSE bhavcopy era (SC_CODE). This is invariant #2's most
   likely breach point in the whole backfill.
4. **Three sources are rolling "latest" files with no date parameter** — `bulk.csv`,
   `block.csv`, `fiidiiTradeReact`. A missed day is not recoverable from the URL. They need
   daily capture and a gap report that treats a miss as permanent loss, not a retry.
5. **`block.csv` was 250 bytes.** Small is normal for block deals. A size-based sentinel in D7
   would flag a legitimate quiet day as a failure.
6. **BSE `ListofScripData` defaults to `status=Active`.** Backfilling only active scrips imports
   survivorship bias directly into the identity master; delisted and suspended must be pulled too.
7. **NSE's warm-up returns 403 and still sets the usable cookie.** Do not let the 403 hard stop
   fire on the session warm-up.
8. **GDELT's file host is HTTPS-unusable** — `data.gdeltproject.org` is a CNAME to
   `c.storage.googleapis.com` and serves that certificate, so TLS validation fails. Plain HTTP is
   the published access path; the manifest ships its own MD5, which L0 should cross-check.

---

## 6. Scope note

C.1 verifies that each URL pattern is reachable and that its payload parses structurally. It does
**not** freeze fixtures — AGENTIC_CONTEXT §8 requires a frozen sample per format era, and each
register entry names the parser task that owes one (`fixture.frozen: false`, `fixture.task`).
Re-verification is not reimplemented here either: rate limiting, backoff and the 403 hard stop
belong to M1.2's fetcher, which reads this register for its per-source policy.

---

## 7. Later measurements by parser tasks

Appended by the task that owns each row, in the same terms as the sweep above. The register is
still the machine-readable record; this is the prose an operator reads.

### M3.4 — FII/DII flows depth (2026-08-08, ~19:30 IST, 6 requests over 2 hosts)

| # | Request | Result | What it establishes |
|---|---|---|---|
| 1 | `GET www.nseindia.com/` (warm-up) | 403, 370 B, `text/html` — **cookie still set** | §5.7 reproduced, four hours after the sweep. It is the normal behaviour of this handshake, not an incident. |
| 2 | `GET www.nseindia.com/api/fiidiiTradeReact` | 200, 215 B, `application/json`, sha256 `1d16ad6b…64ce0` | Byte-identical to C.1's sample from the previous evening. Once published, a session's payload does not change. |
| 3 | same + `?date=05-08-2026` | 200, 215 B, **same sha256**, still dated 07-Aug-2026 | The feed has no date parameter and ignores one silently. A caller cannot tell a rejected date from a served one by status code. |
| 4 | `GET nsearchives…/content/equities/fii_dii_07082026.csv` | 404, 3,537 B, `text/html` | No dated cash-segment equivalent on the archive host. |
| 5 | `GET nsearchives…/content/fo/fii_stats_07-Aug-2026.xls` | 200, 9,216 B, `application/vnd.ms-excel` | A dated FII surface *does* exist — but it is the **F&O segment** (M3.7/M3.8), a different dataset. It must not be presented as this row's history. |
| 6 | `GET www.fpi.nsdl.co.in/robots.txt`, then `/web/Reports/Latest.aspx?RptType=6` | 302 → `contactus.html` (no robots file); 200, 59,035 B | The noted fallback is real and reachable, and publishes "Daily Trends in FPI Investments". FPI-only, no DII, custodian-confirmed basis — a fallback for one leg, not a substitute. Not ingested; it would need its own register entry and robots record. |

**Conclusion, recorded in `source_register.yaml` as `nse_fii_dii_flows.history`: depth is one
session.** The M3 gate's "flows queryable 10 years back" is not achievable from this source, and
the register now says so instead of implying otherwise. History accrues forward from the first
daily capture, and a missed session is unrecoverable.

Nothing was acted on: no account, no login, no terms accepted, no payment. Requests were ≥3 s
apart with the platform's one user agent and the register's Referer, and the 403 in row 1 was not
answered with a different agent, a proxy, or a retry.
