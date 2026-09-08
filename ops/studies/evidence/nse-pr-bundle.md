# NSE PR report bundle — W2 acquisition evidence

Phase 1 measured the source; Phase 2 closed the two era boundaries Phase 1 left as brackets
(§2, 12 further requests at 09:20 IST the same day) and built the campaign driver
(`dataplatform.ingest.pr_bundle_campaign`).

**Measured 2026-09-08, 08:22–08:27 IST. 39 requests to `nsearchives.nseindia.com`**, ≥2.5 s
spacing, host lease held for the whole run, browser UA and `Referer: https://www.nseindia.com/`,
every request through the normal crawl engine (`dataplatform.ingest.fetcher`) so each payload
landed checksummed in L0 before anything read it. Budget was 40; one request was left unspent.
`uptime` and `ps aux | grep -E 'backfill|campaign'` were checked first — load 0.67, no driver
running. No 403 at any point; the hard stop never armed.

A 404 here is evidence that NSE published no bundle for that date. None was retried, and no
pattern was widened to turn one into a 200.

## The source

```
https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR<DDMMYY>.zip
```

One zip per trading session, 13–25 members, 250 KB–665 KB. Registered as `nse_pr_bundle`.

## Availability, per date probed

| session | HTTP | bytes | members | `Bc` | `Bc` dates | `Ix` | `Ix` indices | `mcap` |
|---|---|---|---|---|---|---|---|---|
| 2005-01-03 | **404** | — | — | — | — | — | — | — |
| 2007-01-02 | **404** | — | — | — | — | — | — | — |
| 2008-01-02 | **404** | — | — | — | — | — | — | — |
| 2009-01-02 | **404** | — | — | — | — | — | — | — |
| 2009-04-08 | **404** | — | — | — | — | — | — | — |
| 2009-07-01 | **404** | — | — | — | — | — | — | — |
| 2009-10-01 | **404** | — | — | — | — | — | — | — |
| 2009-12-01 | **404** | — | — | — | — | — | — | — |
| 2009-12-16 | **404** | — | — | — | — | — | — | — |
| 2009-12-31 | **404** | — | — | — | — | — | — | — |
| 2010-01-01 | **404** | — | — | — | — | — | — | — |
| 2010-01-04 | 200 | 255,693 | 24 | `Bc040110.csv` | `DD/MM/YYYY` | `Ix040110.csv` | BANK Nifty (12); CNX IT (20); CNX 500 (500); CNX Midcap (100); Nifty Midcap 50 (50) | **absent** |
| 2010-01-05 | 200 | 259,757 | 24 | `Bc050110.csv` | `DD/MM/YYYY` | `Ix050110.csv` | BANK Nifty (12); CNX IT (20); CNX 500 (500); CNX Midcap (100); Nifty Midcap 50 (50) | **absent** |
| 2010-01-06 | 200 | 256,161 | 24 | `Bc060110.csv` | `DD/MM/YYYY` | `Ix060110.csv` | BANK Nifty (12); CNX IT (20); CNX 500 (500); CNX Midcap (100); Nifty Midcap 50 (50) | **absent** |
| 2010-01-07 | 200 | 253,109 | 22 | `Bc070110.csv` | `DD/MM/YYYY` | `Ix070110.csv` | BANK Nifty (12); CNX IT (20); CNX 500 (500); CNX Midcap (100); Nifty Midcap 50 (50) | **absent** |
| 2010-01-08 | 200 | 259,358 | 24 | `Bc080110.csv` | `DD/MM/YYYY` | `Ix080110.csv` | BANK Nifty (12); CNX IT (20); CNX 500 (500); CNX Midcap (100); Nifty Midcap 50 (50) | **absent** |
| 2010-04-08 | 200 | 271,955 | 25 | `Bc080410.csv` | `DD/MM/YYYY` | `Ix080410.csv` | CNX 500 (500) | **absent** |
| 2010-04-09 | 200 | 268,587 | 25 | `Bc090410.csv` | `DD/MM/YYYY` | `Ix090410.csv` | CNX 500 (500) | **absent** |
| 2010-07-01 | 200 | 278,058 | 25 | `Bc010710.csv` | `DD/MM/YYYY` | `Ix010710.csv` | CNX 500 (500) | **absent** |
| 2010-10-01 | 200 | 297,667 | 25 | `Bc011010.csv` | `DD/MM/YYYY` | `Ix011010.csv` | CNX 500 (500); CNX Infrastructure (25) | **absent** |
| 2010-10-04 | 200 | 305,201 | 25 | `Bc041010.csv` | `DD/MM/YYYY` | `Ix041010.csv` | CNX 500 (500); CNX Infrastructure (25) | **absent** |
| 2010-10-18 | 200 | 317,004 | 24 | `Bc181010.csv` | `DD/MM/YYYY` | **absent** | — | **absent** |
| 2010-11-01 | 200 | 308,086 | 25 | `Bc011110.csv` | `DD/MM/YYYY` | **absent** | — | **absent** |
| 2010-12-01 | 200 | 305,832 | 25 | `Bc011210.csv` | `DD/MM/YYYY` | **absent** | — | **absent** |
| 2011-01-03 | 200 | 298,229 | 25 | `Bc030111.csv` | `DD/MM/YYYY` | **absent** | — | **absent** |
| 2013-01-02 | 200 | 300,955 | 22 | `Bc020113.csv` | `DD/MM/YYYY` | **absent** | — | **absent** |
| 2016-01-04 | 200 | 387,127 | 17 | `Bc040116.csv` | `DD/MM/YYYY` | **absent** | — | **absent** |
| 2019-01-02 | 200 | 283,235 | 13 | `Bc020119.csv` | `DD/MM/YYYY` | **absent** | — | **absent** |
| 2022-01-03 | 200 | 321,481 | 13 | `Bc030122.csv` | `DD/MM/YYYY` | **absent** | — | **absent** |
| 2024-01-02 | 200 | 339,127 | 13 | `Bc020124.csv` | `DD/MM/YYYY` | **absent** | — | **absent** |
| 2024-07-01 | 200 | 516,355 | 15 | `Bc010724.csv` | `DD/MM/YYYY` | **absent** | — | `MCAP01072024.csv` |
| 2024-10-01 | 200 | 528,406 | 15 | `Bc011024.csv` | `DD/MM/YYYY` | **absent** | — | `MCAP01102024.csv` |
| 2025-01-02 | 200 | 482,246 | 15 | `Bc020125.csv` | `DD/MM/YYYY` | **absent** | — | `MCAP02012025.csv` |
| 2025-07-01 | 200 | 501,307 | 14 | `Bc010725.csv` | `DD/MM/YYYY` | **absent** | — | `MCAP01072025.csv` |
| 2025-10-01 | 200 | 523,724 | 14 | `Bc011025.csv` | `DD/MM/YYYY` | **absent** | — | `MCAP01102025.csv` |
| 2025-11-03 | 200 | 605,051 | 14 | `bc03112025.csv` | `YYYY-MM-DD` | **absent** | — | `mcap03112025.csv` |
| 2025-12-01 | 200 | 587,112 | 14 | `bc01122025.csv` | `YYYY-MM-DD` | **absent** | — | `mcap01122025.csv` |
| 2026-01-02 | 200 | 570,477 | 14 | `bc02012026.csv` | `YYYY-MM-DD` | **absent** | — | `mcap02012026.csv` |
| 2026-09-04 | 200 | 665,049 | 14 | `bc04092026.csv` | `YYYY-MM-DD` | **absent** | — | `mcap04092026.csv` |

## What the table establishes

### 1. The archive floor is 2010-01-04, pinned exactly

`PR311209.zip` (2009-12-31, a trading session) and `PR010110.zip` are both 404; `PR040110.zip` is
200. Nine probes spread over 2005–2009 are all 404. The bundle does not reach further back, so the
"roughly 2010-01" in the W2 brief is exactly 2010-01-04.

### 2. Four format eras; three boundaries pinned, one left as a bracket

| era | span | member names | `Bc` dates | `Ix` | `mcap` |
|---|---|---|---|---|---|
| `ix_era` | 2010-01-04 → **(2010-10-04, 2010-10-18]** | `Bc040110` upper, `DDMMYY` | `DD/MM/YYYY` | yes | no |
| `classic` | → **2024-01-31** | same | `DD/MM/YYYY` | no | no |
| `mcap_upper` | **2024-02-01** → **2025-10-10** | `Bc010724` + `MCAP01072024` | `DD/MM/YYYY` | no | yes |
| `lowercase` | **2025-10-13** → open | `bc04092026` lower, `DDMMYYYY` | `YYYY-MM-DD` | no | yes |

**Phase 2, 2026-09-08: the two brackets closed by bisection, 12 requests total** (Phase 1 had
estimated ~8 each; the two bracket ends were already in L0 from Phase 1's own probes, so both
searches started free). Run as
`uv run python -m dataplatform.ingest.pr_bundle_campaign pin-eras`, whose probes go through the
ordinary acquisition path — so every payload below is in L0 and re-running the bisection costs
zero requests.

| boundary | question | last no | first yes | requests | confirmed |
|---|---|---|---|---|---|
| `classic` → `mcap_upper` | does the bundle carry an `mcap` member? | 2024-01-31 | **2024-02-01** | 7 | still yes at 2024-02-02 |
| `mcap_upper` → `lowercase` | is the `Bc` member's name lowercase? | 2025-10-10 | **2025-10-13** | 5 | still yes at 2025-10-14 |

Read off the payloads themselves (`PrBundle` re-verifying each checksum out of L0):

```
2024-01-31  members: ['Bc310124.csv']                             115 Bc rows
2024-02-01  members: ['Bc010224.csv', 'MCAP01022024.csv']         114 Bc rows
2025-10-10  members: ['Bc101025.csv', 'MCAP10102025.csv']         169 Bc rows
2025-10-13  members: ['bc13102025.csv', 'mcap13102025.csv']       200 Bc rows
```

The casing change, the member-name width change and the `Bc` date-format change are **one event**,
now pinned to one session rather than bracketed to a month. The first data row of each `Bc`:

```
2025-10-10  18,865SCL24B,SEC RE NCD 8.70% SR.VIII,13/10/2025, , ,13/10/2025, , ,INTEREST PAYMENT
2025-10-13  NC,1003IIFL29,Sec Re NCD 10.03% Sr V,2025-10-17,,,2025-10-17,,,INTEREST PAYMENT
```

`ix_era` → `classic` is deliberately still a bracket. The `Ix` member's last day changes no parser
behaviour — `ix.py` documents it as a 2010-only validation asset either way — and **no parser
dispatches on any era**: the readers sniff casing, name width and date shape from the bytes in
front of them. A boundary is documentation accuracy, never correctness. The bisection is monotone
by assumption, so it checks that assumption by probing one session past the answer; a bracket
whose answer flips back comes back `NOT PINNED` rather than reported.

### 3. `DDMMYY` and `DDMMYYYY` coexist inside a single zip

`PR010724.zip` carries `Bc010724.csv` (6 digits) beside `MCAP01072024.csv` (8 digits). The 2010
bundles do the same (`Bc040110.csv` beside `cd04012010.doc`). Any code that decides a name format
per *bundle* is wrong by construction; width is read per member.

### 4. `Ix` is intermittent — index membership is **not** reconstructible from it

The member was published on every 2010 session probed and vanished between 2010-10-04 and
2010-10-18, never returning in 2011, 2013, 2016, 2019, 2022, 2024 or 2026. Within its nine-month
life the *set of indices* it carries changes:

* 2010-01-04 … 2010-01-08 (five consecutive sessions): **five** indices — BANK Nifty (12), CNX IT
  (20), CNX 500 (500), CNX Midcap (100), Nifty Midcap 50 (50).
* 2010-04-08, 2010-04-09, 2010-07-01: **CNX 500 only**.
* 2010-10-01, 2010-10-04: **CNX 500 + CNX Infrastructure (25)**.

So the answer to "daily-complete, rotating, or intermittent" is: **the file is daily while it
exists, but its contents are intermittent per index** — stable across consecutive days, varying
across the year, and never covering NIFTY 50 at all. Nine months of a varying, incomplete index
set ending in 2010 cannot produce a point-in-time constituent history.

**This does not overturn `ops/BACKLOG.md:126` / AGENTIC_CONTEXT §4.1.** What it provides is a
small set of dated, verifiable anchor points — chiefly CNX 500 on ~every 2010 session, with
issue-cap, close, market cap and weightage per constituent — against which a reconstruction from
another source can be *validated*. That is a validation asset, not a membership series.

### 5. `mcap` is not a delisting-date series — the opening hypothesis is contradicted

W2 opened on the idea that `Last Trade Date` dates delistings, closing the gap of zero delisting
events after 2003-03-27. Measured across nine `mcap` files:

* `Category` takes **only** `Listed` and `Permitted`. There is no `Delisted` or `Suspended` value.
  A delisted security stops appearing in the file rather than being marked.
* `Last Trade Date` differs from `Trade Date` on 48–116 rows per file. Those rows are overwhelmingly
  thin SME-series names that did not trade *that day* — an illiquidity marker.
* A further 3–6 rows per file carry the literal string `Not Traded`, marking a security with no
  trading history at all. An empty `Last Trade Date` never occurs.

So `mcap` yields a delisting signal only by **disappearance**: the last bundle a symbol appears in
bounds its delisting from below. That needs the full daily series to observe and is far weaker than
a dated event. Reported, not acted on.

What `mcap` *is* unambiguously good for: `Issue Size` is a daily shares-outstanding series, which
the platform has no other dated source for.

`mcap` does not tie to itself. Its published `Listed` + `Permitted` subtotals miss its own `Total`
by ₹0.05 (2026-09-04) and ₹0.08 (2024-07-01), and a sum of the security rows sits ₹0.10–₹0.34 above
the published `Total`. On a base of ₹4.9×10¹⁴ that is one part in 5×10¹⁵ — the exchange rounding
each line independently. The test asserts agreement to the rupee for that reason.

### 6. Every member is symbol-keyed. None carries an ISIN

`Bc`, `Ix` and `mcap` all key on the NSE trading symbol and none publishes an ISIN. ISIN is the
only join key (invariant #2), so none of these rows can be joined today, and the typed models
carry **no `isin` field** so that nothing can fill one in with a guess. See the PR body for the
identity work this implies.

## Member registry

Members seen across the probes, all registered in `MemberKind`; only the first three are parsed.

| member | what it is | parsed by W2 |
|---|---|---|
| `Bc` / `bc` | corporate actions, broadcast-dated | **yes** |
| `Ix` | index membership, weightage, issue cap | **yes** |
| `MCAP` / `mcap` | issue size, market cap, last trade date | **yes** |
| `An`, `Bm` | announcements, board meetings | no |
| `bh`, `Pd`, `Pr`, `Gl`, `HL`, `Tt` | band hits, 52-week, price report, gainers/losers, highs/lows, top traded |no |
| `etf`, `sme`, `corpbond`, `ffix` | segment reports | no |
| `fo`, `op`, `cd`, `cf`, `co` | derivatives and currency | no |
| `RPD`, `Rtt`, `NPD`, `PE_` | retail/non-promoter detail, index P/E | no |
| `Readme`, `help`, `Rdm_help`, `rdm`, `nuver` | documentation shipped in every bundle | n/a |

`PrBundle.unknown_members` reports any name outside this list, so a member NSE adds later surfaces
instead of being silently ignored. The registry is tested against the **complete** member lists of
the four frozen bundles, recorded in each fixture's `manifest.json`.

## L0

All 30 successful payloads are in the worktree lake at `data/L0/nse_pr_bundle/<YYYY>/<MM>/`,
checksummed. `data/` is gitignored and not part of this change.
