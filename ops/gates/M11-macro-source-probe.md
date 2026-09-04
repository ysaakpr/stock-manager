# M11.1 — Macro source probe, and the index-identity finding

**Date:** 2026-09-04 · **Method:** C.1's sweep method (AGENTIC_CONTEXT §8) — robots first per host,
>=2.6 s spacing, browser UA, hard stop on a 403 spike (never tripped), no retry with a different
agent, no login anywhere. **34 requests over 7 hosts**, well under the ~200-per-host line §3.3
reserves to the owner.

## 1. What was probed, and what came back

| Surface | Result |
|---|---|
| `nsearchives…/content/indices/ind_close_all_<DDMMYYYY>.csv` | **200 back to 2012-10-01.** The find. |
| `api.worldbank.org` indicators | **200**, 14,939 B JSON, keyless, 66 annual points. Current vintage only. |
| `fred` / `alfred.stlouisfed.org` | **Unreachable from this host.** Cause undetermined — see §4. |
| `www.mospi.gov.in` | robots.txt is a **soft 404** — HTTP 200 carrying the site's HTML shell, byte-identical to `/`. |
| `cpi.mospi.gov.in` | Connect timeout. |
| `dbie.rbi.org.in` | TLS **certificate hostname mismatch** — the cert is not valid for that name. |

## 2. The daily spine: `ind_close_all`, measured

Already `VERIFIED` in the register as the computed-TRI input, and its P/E, P/B and Div Yield columns
were never read. They are the platform's daily market-valuation series.

| | |
|---|---|
| Archive epoch | between **2012-07-01** (404) and **2012-10-01** (200, 30 indices) |
| Latest verified | 2026-09-01 (165 indices) |
| Format eras | **none** — the 13-column header is byte-identical across all 13+ years |
| Index count | 30 → 48 → 69 → 78 → 94 → 165, so a PIT read must not assume an index existed |
| Cost to backfill | ~2,470 sessions, one small CSV each — the same shape as the bhavcopy backfill |

## 3. The finding that matters more than the depth

**NSE/IISL renamed 48 of 53 index names in a single event between 2015-11-06 and 2015-11-10.**
Verified by fetching both sides of the switch and bisecting to it: 2015-11-06 is CNX-named,
2015-11-10 is Nifty-named, and the index *count* is 53 on both days.

A consumer keyed on today's names reads **zero rows** from any file before that date. Not an error —
zero rows. That is three years of market history silently absent, and it is the same survivorship
trap D2's `symbol_history` closes for equities, reappearing in the index dimension where nothing was
watching for it.

`dataplatform/ingest/macro/index_aliases.yaml` is the name history. **23 rows, each with its
evidence**, admitted on two grounds only:

* **Name stem + level corroboration** (20 rows) — `CNX X` → `Nifty X` where the stem matches *and*
  the close across the switch agrees (max delta 4.98%, over two sessions in Diwali week).
* **Multi-year level continuity** (3 rows) — the flagships: `S&P CNX Nifty` → `CNX Nifty` →
  `Nifty 50` (5718.8 → 8586.25 → 8179.5 across 2012/2015/2017) and `CNX Nifty Junior` →
  `Nifty Next 50`.

A first attempt matched all 48 by nearest closing value. **It was wrong and is recorded here as a
warning:** greedy nearest-close matching produced `CNX Auto → Nifty 100` and
`CNX Smallcap → Nifty Growth Sectors 15`, because indices at similar levels steal each other's
matches and the error cascades. Close proximity is corroboration, never identification.

### Deliberately unmapped — 26 names

These have no stem match and no continuity evidence. They resolve to **themselves**, forming visibly
separate series rather than being merged into the wrong index. Mapping one is a research task whose
evidence belongs in the alias table, never a guess:

- `CNX Alpha Index`
- `CNX Consumption`
- `CNX DEFTY`
- `CNX Dividend Opportunities`
- `CNX Finance`
- `CNX High Beta`
- `CNX Low Volatility`
- `CNX Midcap`
- `CNX Nifty Dividend`
- `CNX Nifty Shariah`
- `CNX Service Sector`
- `CNX Smallcap`
- `CPSE`
- `GSEC10 NSE Index`
- `GSECBM NSE Index`
- `LIX 15`
- `LIX15 Midcap`
- `NI15`
- `NIFTY Midcap 50`
- `NIFTY PR 1X Inverse`
- `NIFTY PR 2x Leverage`
- `NIFTY TR 1X Inverse`
- `NIFTY TR 2X Leverage`
- `NSE GSECBM Clean Price Index`
- `NSE Quality 30`
- `NV 20`

## 4. ALFRED: recorded as FAILED, but *not* as a source-side refusal

ALFRED (ArchivaL FRED) is the one surface probed that would solve the macro vintage problem
outright — it serves a series *as it was published on* a past date, which is exactly the
`release_date` the store partitions by.

It is unreachable from this host, and the evidence does not say why. Five attempts: httpx/HTTP-2
read timeout; curl HTTP/2 `INTERNAL_ERROR` after 0.09 s; curl `--http1.1` timing out at 45 s with
zero bytes including on the bare host root; and the same three failing identically outside the
build sandbox. TLS establishes and then nothing arrives — the signature of a network-path block, not
an application refusal. There was no status code, no body, and nothing to evade.

A control request to `api.worldbank.org` on the same network in the same sweep returned 200, so this
is host-specific rather than general egress.

**One re-probe from the Ubuntu campaign server settles it.** Until then the register carries it as
`FAILED` with `last_http_status: 0` — a new, documented encoding meaning *requested, no HTTP
response arrived*, which the validator previously conflated with *never requested*.

## 5. Register changes

- **New §4.1 row** "Macro / economic backdrop" — logged in EXECUTION_PLAN §12 as **PROPOSED**,
  awaiting owner ratification. §4.1 v1.0 has no home for macro, so nothing could be registered.
- **New hosts:** `api.worldbank.org`, `www.mospi.gov.in`, `fred.stlouisfed.org`,
  `alfred.stlouisfed.org` — each with its robots record, including the two that never answered.
- **New sources:** `worldbank_indicator_api` (VERIFIED), `alfred_series_vintage` (FAILED).
- **`nifty_index_close_snapshot`** gained a measured `history` block and its fixture flipped to
  `frozen: true` against real captures from both naming eras.

## 6. What is deliberately *not* claimed

- **World Bank is not point-in-time.** Its envelope carries one `lastupdated` for the whole
  response and there is no vintage parameter, so a revised figure silently replaces the original.
  Facts from it are storable only with `release_date` = the envelope's `lastupdated`, which makes
  them knowable *now* — fine for a present-day backdrop, useless for a historical backtest. The
  store's release-date partitioning is what keeps that enforceable rather than a comment.
- **No CPI, IIP, GDP or policy-rate series has a working PIT source yet.** MoSPI serves no robots
  policy and its CPI portal did not answer; RBI's DBIE presents an invalid certificate. Those are
  three open leads, not three sources.
- **Nothing has been backfilled.** This task built the store and the parser and froze fixtures. The
  ~2,470-session `ind_close_all` backfill is a B1 bulk campaign and needs the owner's go.
