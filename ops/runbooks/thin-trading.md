# Thinly-traded label (M13.8)

**Owner ruling, 2026-11-01:** some dividends fall on a traded day after a gap of more than 92 days
without trades. Such names are *marked* — "thinly traded / infrequently traded" — and whether to
consider them is the strategy's (fund manager's) decision. Nothing is excluded automatically.

The label is information only. It never quarantines, excludes, or changes a price, a factor or a
total return, and it is not part of anything the M17 managers read (exposing it to them changes
their mandate-hash inputs and needs a pre-registration amendment; see `ops/BACKLOG.md`, M13.8).

## What it computes

`dataplatform/quality/thin_trading.py`, once per session, for every ISIN with an equity bar in the
trailing **250 NSE sessions** (about a year):

| field | meaning |
|---|---|
| `traded_ratio` | sessions it traded ÷ sessions we observed, from its first bar (a listing inside the window starts there) |
| `nse_traded_ratio`, `bse_traded_ratio` | the same per venue (null where it has no bar on that venue) |
| `longest_gap_sessions`, `longest_gap_days`, `longest_gap_from/_to` | the longest run of observed no-trade sessions; days run from the bar before to the bar after (or to `as_of` while open) — the same span a dividend's reinvestment base is measured over |
| `current_gap_sessions` | the run still open at `as_of` |
| `nse_series`, `bse_series`, `trade_to_trade` | each venue's latest series; trade-to-trade when it is BE/BZ/ST/SZ (NSE) or T/XT/MT/TS/Z/ZP (BSE) |
| `listing_status` | the identity master's status (ACTIVE / SUSPENDED / DELISTED) |
| `capture_holes` | sessions excluded because our capture lacked them (below) |
| `identity_seam` | the gap is a reissue our lineage table has no edge for — ours, reported, never labelled |
| `thin`, `reasons` | the label and why (`ratio`, `gap`) |

**A session is traded when the ISIN traded on any venue** in any equity series (regular or
trade-to-trade). An investor can deal on either exchange, and a name NSE stopped carrying while BSE
traded it daily is not illiquid.

**Equity series.** NSE: EQ, SM (regular), BE, BZ, ST, SZ (trade-to-trade). BSE groups: A, B, X,
M, MS, P (regular), T, XT, MT, TS, Z, ZP (trade-to-trade). Debt, InvIT/REIT, ETF-on-BSE (E),
rights-entitlement (R) and auxiliary series (BL, ...) never count.

## Thresholds (stated in code: `THIN_RATIO_BELOW`, `THIN_GAP_SESSIONS`, `MIN_OBSERVED_SESSIONS`)

**Thin = `traded_ratio < 0.80` or `longest_gap_sessions >= 20`**, given at least 20 observed
sessions (fewer is `insufficient_history`, never thin).

The distribution on the live lake, label of 2026-11-03 (6,201 ISINs labelled, 6,146 with 20+
observed sessions):

| traded ratio below | 1.00 | 0.99 | 0.95 | 0.90 | 0.85 | **0.80** | 0.70 | 0.50 | 0.25 |
|---|---|---|---|---|---|---|---|---|---|
| all ISINs | 2,428 | 2,164 | 1,738 | 1,465 | 1,273 | **1,144** | 933 | 649 | 370 |
| with an NSE bar (3,643) | 718 | 640 | 500 | 411 | 342 | **301** | 235 | 151 | 92 |
| BSE only (2,503) | 1,710 | 1,524 | 1,238 | 1,054 | 931 | **843** | 698 | 498 | 278 |

| longest no-trade gap (sessions) | 0 | 1 | 2-4 | 5-9 | 10-14 | 15-19 | **20-39** | 40-59 | 60+ |
|---|---|---|---|---|---|---|---|---|---|
| all ISINs | 3,718 | 503 | 748 | 348 | 160 | 94 | **167** | 79 | 329 |
| with an NSE bar | 2,925 | 160 | 267 | 97 | 32 | 20 | **32** | 18 | 92 |

Why these numbers:

- **The ratio has no cliff.** 80% of NSE-listed names trade on at least 99.6% of sessions; below
  that the curve falls away gradually. 0.80 is one session in five with no trade at all — a name
  that cannot be sized into on a given day — and marks 8% of NSE-listed names, 34% of BSE-only.
  0.90 would add 110 NSE names that trade on four days in five; 0.70 would drop SME and
  trade-to-trade names that miss a session a week.
- **The gap adds little but the owner's case.** Only 18 names (5 with an NSE bar) have a 20+
  session gap and a ratio of at least 0.80; it exists for exactly the shape the owner asked about:
  a name that trades normally except for one long silence. Twenty sessions (four trading weeks) is
  where the histogram's short gaps (holidays, a quiet fortnight) end and the stretches verified as
  suspensions, trade-to-trade stints and months of no trades begin (below).

Result on 2026-11-03: **1,088 thin** (299 with an NSE bar, 789 BSE-only; by reason: ratio only
587, both 492, gap only 9), **74 identity seams**, **0 labels with a capture hole**. Of the 1,617
names in that day's M17 Commons universe, **2** are thin: INE173M01012 (Onix Solar; BSE
trade-to-trade before its NSE listing on 2026-08-17, ratio 0.764) and INE336H01023 (Gayatri
Projects, ratio 0.648 on both venues).

## A capture hole is never a no-trade session

A session counts as observed for a venue only when **our** capture is complete:

1. `sync_state` has the venue's bhavcopy `PUBLISHED` (`nse_bhavcopy`, `bse_bhavcopy`, or their
   pre-ISIN/legacy eras), **and** L1 `prices_raw` holds rows for the venue that day;
2. per ISIN, L1 did not refuse its row into `prices_raw_quarantine`;
3. per ISIN on NSE, the exchange's own PR bundle (`pr_security_marks`, a second file) does not
   show a trade our bhavcopy lacks.

A session failing any of these is a hole: out of the ratio's denominator, and it *ends* a gap
rather than extending it. Holes are counted per row (`capture_holes`, `/status/thin-trading`'s
`with_capture_holes`) and the job logs `quality.thin_trading.capture_holes` per venue. A bar is a
trade whatever the capture record says.

**Identity seams are ours too.** A reissued ISIN (split, face-value change) traded under its
predecessor until the change, and BSE's scrip-code era (to 2024-07-05) files a scrip's whole
history under today's ISIN — so read alone, the successor shows years of bars, a hole, then its
reissue. Two defences:

- every lineage edge effective on or before `as_of` (`isin_lineage`) stitches the predecessor's
  bars into the successor (point in time by the edge's date);
- a reissue with no edge — another ISIN of the same issuer and security type (`INE` + first nine
  characters) that trades inside the gap and never on the same session — is flagged
  `identity_seam` and listed under `seams`, never labelled thin.

Without the stitch, 75 ISINs showed a false 20+ session gap, 18 of them in the M17 universe
(Kotak Mahindra Bank, MCX, CAMS, Angel One, Nuvama, TD Power and others after their 2025-26
splits). With it, none of the M17 universe's thin labels is a seam.

## Verification of the gaps (2026-11-04, read-only; nothing re-fetched)

**The six M13.8 dividends** whose ex-date bar follows a gap of more than 92 days in L2's
EQ-only series:

| ISIN | ex-date | last EQ bar before | days |
|---|---|---|---|
| INE952H01027 (Raj Television) | 2019-09-19 | 2019-02-25 | 206 |
| INE713C01016 (Suraj) | 2025-02-10 | 2024-09-16 | 147 |
| INE217A01012 (Zuari Industries) | 2021-04-27 | 2021-01-13 | 104 |
| INE716B01029 (Tips; NSE under predecessor INE716B01011) | 2021-09-13 | 2021-06-08 | 97 |
| INE570D01018 (Arrow Greentech) | 2026-09-15 | 2026-06-11 | 96 |
| INE918N01018 (Veto Switchgears) | 2026-09-21 | 2026-06-18 | 95 |

None of the six is a no-trade gap and none is a capture hole. Every session in every gap was
captured (`PUBLISHED`, L1 rows present), and in every session the stock **traded** — in the
trade-to-trade series: BE on NSE (BZ for Raj Television, whose 2019-09-12 NSE notice moved it out
of the Z category) and T/Z on BSE. NSE's own PR bundle independently lists the BE/BZ trade on
every one of those sessions. The "gap" exists only because L2 adjusts the EQ series alone, so the
dividend is reinvested against the last EQ close — up to 206 days stale — while a BE close from
the session before the ex-date exists. These names are trade-to-trade (`trade_to_trade`), not
thinly traded. The reinvestment base is recorded as a backlog question (M13.8, `ops/BACKLOG.md`).

**Every ISIN with a 20+ session no-trade gap in the trailing year** (window 2025-10-29 .. 2026-11-03,
any venue, any equity series): 501 after lineage stitching and seam flagging, 135 with an NSE bar.
**No gap contains a capture hole**: no session is missing from `sync_state` or L1 for either venue
(the only non-PUBLISHED BSE dates, 2024-07-01..05, are published under `bse_bhavcopy_legacy`), no
row was refused into `prices_raw_quarantine`, and NSE's PR bundle shows no trade our bhavcopy lacks.
By cause:

| cause | ISINs |
|---|---|
| trade-to-trade series around the gap (BE/BZ/ST/SZ, T/XT/Z/ZP) | 182 |
| no trades: regular series, listed, captured (mostly SME and BSE X/M groups) | 117 |
| partly-paid or rights ISIN that ceased (the instrument was called up or lapsed) | 78 |
| suspended now: master SUSPENDED and the gap is still open | 67 |
| suspension: master status or an NSE suspension notice in the bundle's `an*.txt` | 51 |
| delisted (master) | 6 |

The identity seams (74 flagged; 75 false gaps removed by the lineage stitch) are the only
gaps of ours, and they are reported, not labelled.

## Operating it

- **Schedule:** `thin_trading_label`, 20:15 IST Monday to Friday — after the EOD pipeline's and
  the share-basis guard's budgets; 15-minute budget, 10-minute misfire grace, so it ends by 20:40,
  before the 21:45 paper session and the 22:00 M17 desk. It reads the lake and Postgres (a few
  seconds), holds no host lease and writes only `thin_trading_label`.
- **Session:** the newest NSE session whose `nse_bhavcopy` is PUBLISHED. A rerun replaces that
  session's rows; earlier sessions are kept as point-in-time history.
- **Read it:** `dataplatform.query.thin_trading_asof(conn, as_of)` (newest label session on or
  before `as_of`); `GET /status/thin-trading?as_of=&limit=&nse_only=`; console Data page, panel
  "Thinly traded".
- **By hand:** `uv run python -m dataplatform.quality.thin_trading --dry-run` prints the thin names
  without writing; `--as-of YYYY-MM-DD` labels a past session (lineage is cut at that date; the
  master's listing status is today's).
- **If it fails:** `/status/jobs` shows the run FAILED; the label simply stays at its last session
  (`as_of` on `/status/thin-trading`). Nothing downstream depends on it, so no trading is gated.
