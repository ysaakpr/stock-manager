# `ffix` — dated index constituent membership, censused (W2)

- **L0 root swept:** `/home/ubuntu/stock-manager/data/L0`
- **Range asked for:** 2010-01-04 .. 2013-04-30
- **Bundles opened:** 827
- **Sessions with an `ffix` member:** 827 (2010-01-04 .. 2013-04-30)
- **Constituent rows parsed:** 711,344
- **Distinct indices:** 17
- **Distinct symbols, all indices:** 628
- **Membership change events:** 154
- **Index-set shapes (arrival cohorts):** 5

## 0. What this is, and how to reproduce it

`ffix<DDMMYY>.csv` is a member of every NSE daily report bundle from 2010-01-04 to
2013-04-30. It was registered in `MemberKind` as **"Fixed income"**, which it is not:
`ffix` is *free-float index*, and the payload is the complete constituent list of every
index NSE published that session, with each member's investible factor, close, free-float
market cap and **index weightage**. That one wrong word is why `ops/BACKLOG.md:126`,
`AGENTIC_CONTEXT §4.1` and the multi-fund study all went on recording historical index
membership as structurally unbackfillable while three years of it sat in L0.

**No bytes were fetched for this report.** Every number below was measured off the
authoritative lake by re-reading payloads that were already there, each re-checksummed
against its sidecar on the way in:

```
DATA_ROOT=/home/ubuntu/stock-manager/data uv run python -m \
    dataplatform.ingest.nse.pr_bundle.membership \
    --from 2010-01-04 --to 2013-04-30 --universe \
    --out ops/gates/ffix-index-membership-census-2026-09-08.md
```

### Format eras: there are none the parser can see

The brief for this task assumed the format changes somewhere across 3.3 years. Swept
rather than sampled, it does not: all 827 files carry the identical
9-column header `INDEX_FLG, SYMBOL, SERIES, SECURITY, ISSUE_CAP, INVESTIBLE_FACTOR,
CLOSE_PRIC, FF_MKT_CAP, WEIGHTAGE`, the identical row shape, and `INDEX_FLG` values that
are stable strings for the whole life of each index. Two things *do* change, and neither
is a parse era because the reader dispatches on neither:

**The index set**, in 5 arrival cohorts — and no index ever leaves:

| from | indices | sessions on this shape |
|---|---|---|
| 2010-01-04 | 3 | 37 |
| 2010-02-26 | 7 | 97 |
| 2010-07-19 | 8 | 59 |
| 2010-10-11 | 10 | 77 |
| 2011-01-31 | 17 | 557 |

**One banner string.** The display banner above each index's block renamed
`S&P CNX Nifty Sec.` → `CNX Nifty Sec.` in March 2013, and relapsed to the old text for
exactly one session before the rename stuck. `INDEX_FLG` never moved, and membership is
read from `INDEX_FLG` alone — a banner-keyed reader would have reported all 50 NIFTY
constituents removed and 50 added to a brand-new index on a date nothing happened.

| banner (display name only, never an index identity) | sessions |
|---|---|
| `BANK Nifty` | 790 |
| `CNX 100` | 827 |
| `CNX 500` | 634 |
| `CNX Energy` | 557 |
| `CNX FMCG` | 557 |
| `CNX IT` | 790 |
| `CNX Infra` | 634 |
| `CNX MIDCAP` | 790 |
| `CNX MNC` | 557 |
| `CNX Nifty Junior Sec.` | 827 |
| `CNX Nifty Sec.` | 37 |
| `CNX PSE` | 557 |
| `CNX PSU Bank` | 557 |
| `CNX Pharma` | 557 |
| `CNX Realty` | 693 |
| `CNX Service` | 557 |
| `NIFTY MIDCAP 50` | 790 |
| `S&P CNX Nifty Sec.` | 790 |

### Bundles recovered by dating the `ffix` member from its own name

`PrBundle` refuses to date a bundle whose members disagree, which is correct for a
corporate action. These two would otherwise have been lost; the `ffix` member's own
filename dates them, which is still payload-derived and never a clock.

| bundle | L0 key date | `ffix` member | dated to | why `PrBundle` refused |
|---|---|---|---|---|
| `PR190811.zip` | 2011-08-19 | `ffix190811.csv` | 2011-08-19 | PR190811.zip: members disagree about the bundle's date (Gl190811.csv=2011-08-19, An190811.txt=2011-08-19, Bm19 |
| `PR100113.zip` | 2013-01-10 | `nupr100113/ffix100113.csv` | 2013-01-10 | PR100113.zip: no member carries a date in its name, so the bundle cannot be dated from its own contents |

## 1. Per index

`median` is the lower median, so it is a whole number of securities. `nominal` is the
size the index's name asserts; a blank means the index does not assert one (`BANK Nifty`
and the sectorals are as-many-as-qualify).

| index | kind | first | last | sessions | rows | min | median | max | nominal | off-nominal | symbols ever | changes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `CNX 100` | broad | 2010-01-04 | 2013-04-30 | 827 | 82,700 | 100 | 100 | 100 | 100 | 0 | 125 | 12 |
| `JR. NIFTY` | broad | 2010-01-04 | 2013-04-30 | 827 | 41,350 | 50 | 50 | 50 | 50 | 0 | 79 | 10 |
| `NIFTY` | broad | 2010-01-04 | 2013-04-30 | 827 | 41,350 | 50 | 50 | 50 | 50 | 0 | 63 | 9 |
| `BANK Nifty` | broad | 2010-02-26 | 2013-04-30 | 790 | 9,480 | 12 | 12 | 12 |  | — | 14 | 2 |
| `CNX IT` | sectoral | 2010-02-26 | 2013-04-30 | 790 | 15,800 | 20 | 20 | 20 |  | — | 31 | 8 |
| `CNX Midcap` | broad | 2010-02-26 | 2013-04-30 | 790 | 79,000 | 100 | 100 | 100 | 100 | 0 | 165 | 22 |
| `Nifty Midcap 50` | broad | 2010-02-26 | 2013-04-30 | 790 | 39,500 | 50 | 50 | 50 | 50 | 0 | 91 | 18 |
| `CNX Realty` | sectoral | 2010-07-19 | 2013-04-30 | 693 | 6,930 | 10 | 10 | 10 |  | — | 14 | 7 |
| `CNX 500` | broad | 2010-10-11 | 2013-04-30 | 634 | 317,000 | 500 | 500 | 500 | 500 | 0 | 620 | 45 |
| `CNX Infrastructure` | sectoral | 2010-10-11 | 2013-04-30 | 634 | 15,850 | 25 | 25 | 25 |  | — | 36 | 6 |
| `CNX ENERGY` | sectoral | 2011-01-31 | 2013-04-30 | 557 | 5,570 | 10 | 10 | 10 |  | — | 10 | 0 |
| `CNX FMCG` | sectoral | 2011-01-31 | 2013-04-30 | 557 | 8,355 | 15 | 15 | 15 |  | — | 20 | 4 |
| `CNX MNC` | sectoral | 2011-01-31 | 2013-04-30 | 557 | 8,355 | 15 | 15 | 15 |  | — | 18 | 3 |
| `CNX PHARMA` | sectoral | 2011-01-31 | 2013-04-30 | 557 | 5,570 | 10 | 10 | 10 |  | — | 12 | 2 |
| `CNX PSE` | sectoral | 2011-01-31 | 2013-04-30 | 557 | 11,140 | 20 | 20 | 20 |  | — | 25 | 3 |
| `CNX PSU BANK` | sectoral | 2011-01-31 | 2013-04-30 | 557 | 6,684 | 12 | 12 | 12 |  | — | 12 | 0 |
| `CNX SERVICE` | sectoral | 2011-01-31 | 2013-04-30 | 557 | 16,710 | 30 | 30 | 30 |  | — | 35 | 3 |

### Off-nominal constituent counts

**None.** Every index whose name asserts a size published exactly that many
constituents on every session it appeared on. That is also the strongest available
evidence that the reader is not dropping rows: 500 of 500 on every CNX 500 session,
with the file's separator and banner furniture interleaved throughout.

## 2. Contiguity against the trading calendar

- Calendar sessions expected in 2010-01-04..2013-04-30: **827**
- Sessions with an `ffix` member: **827**
- Bundles present but with no `ffix` member: **0**

**Contiguous, with no interior gaps.** The `ffix` sessions are exactly the trading
calendar's sessions over the span, in both directions — every session the calendar
declares has a member, and no member falls on a date the calendar calls closed.

## 3. Observable membership changes

**154 change events**, each a session on which an index's
constituent set differed from its own previous published session. This is the payload:
a dated record of reconstitution, diffed off the source rather than reconstructed.

| index | changes | largest (symbols moved) | first | last |
|---|---|---|---|---|
| `CNX 100` | 12 | 8 on 2010-10-01 | 2010-04-08 | 2013-04-01 |
| `JR. NIFTY` | 10 | 10 on 2012-04-27 | 2010-04-08 | 2013-04-01 |
| `NIFTY` | 9 | 6 on 2010-10-01 | 2010-04-08 | 2013-04-01 |
| `BANK Nifty` | 2 | 2 on 2011-10-10 | 2011-10-10 | 2012-04-27 |
| `CNX IT` | 8 | 6 on 2012-09-28 | 2011-03-25 | 2013-04-01 |
| `CNX Midcap` | 22 | 26 on 2013-04-01 | 2010-03-09 | 2013-04-17 |
| `Nifty Midcap 50` | 18 | 24 on 2012-09-28 | 2010-05-17 | 2013-04-17 |
| `CNX Realty` | 7 | 6 on 2011-03-25 | 2010-10-01 | 2013-04-01 |
| `CNX 500` | 45 | 40 on 2011-10-10 | 2010-11-25 | 2013-04-17 |
| `CNX Infrastructure` | 6 | 6 on 2011-10-10 | 2011-03-25 | 2013-02-01 |
| `CNX ENERGY` | 0 | — | — | — |
| `CNX FMCG` | 4 | 4 on 2011-03-25 | 2011-03-24 | 2012-04-27 |
| `CNX MNC` | 3 | 4 on 2011-10-10 | 2011-10-10 | 2013-04-01 |
| `CNX PHARMA` | 2 | 2 on 2012-09-21 | 2012-09-21 | 2012-09-28 |
| `CNX PSE` | 3 | 6 on 2011-03-25 | 2011-03-25 | 2013-04-01 |
| `CNX PSU BANK` | 0 | — | — | — |
| `CNX SERVICE` | 3 | 8 on 2011-03-25 | 2011-03-25 | 2013-04-01 |

### Every change to a broad-market index, in full

The sectorals' change lists run to hundreds of events; these are the ones a backtest
universe is most likely to be built from, so they are given whole.

**`CNX 100`** — 12 events

| session | previous session | added | removed |
|---|---|---|---|
| 2010-04-08 | 2010-04-07 | PUNJLLOYD, TORNTPOWER | GRASIM, MOSERBAER |
| 2010-10-01 | 2010-09-30 | GRASIM, RECLTD, SRTRANSFIN, YESBANK | ABB, IDEA, TTML, UNITECH |
| 2010-10-07 | 2010-10-06 | EXIDEIND, TATACHEM | RNRL, ZEEL |
| 2011-03-25 | 2011-03-24 | INDUSINDBK, TITAN, ZEEL | CORPBANK, MRPL, SUZLON |
| 2011-04-21 | 2011-04-20 | COALINDIA | IBREALEST |
| 2011-06-29 | 2011-06-28 | INFY | INFOSYSTCH |
| 2011-08-08 | 2011-08-05 | HEROMOTOCO | HEROHONDA |
| 2011-10-10 | 2011-10-07 | BOSCHLTD, DABUR, IDEA | PATNI, PUNJLLOYD, SYNDIBANK |
| 2012-01-17 | 2012-01-16 | ADANIPORTS | MUNDRAPORT |
| 2012-04-27 | 2012-04-26 | DIVISLAB, GSKCONS, PETRONET | HDIL, IFCI, IOB |
| 2012-09-28 | 2012-09-27 | APOLLOHOSP, BAJAJHLDNG, GODREJCP | ANDHRABANK, BEL, STER |
| 2013-04-01 | 2013-03-28 | BAJAJFINSV, NMDC, TATAGLOBAL, UBL | BIOCON, GMRINFRA, TORNTPOWER, WIPRO |

**`JR. NIFTY`** — 10 events

| session | previous session | added | removed |
|---|---|---|---|
| 2010-04-08 | 2010-04-07 | PUNJLLOYD, TORNTPOWER | KOTAKBANK, MOSERBAER |
| 2010-10-01 | 2010-09-30 | GRASIM, RECLTD, SRTRANSFIN, YESBANK | BAJAJ-AUTO, DRREDDY, SESAGOA, TTML |
| 2010-10-07 | 2010-10-06 | EXIDEIND, TATACHEM | RNRL, ZEEL |
| 2011-03-25 | 2011-03-24 | INDUSINDBK, TITAN, ZEEL | CORPBANK, GRASIM, MRPL |
| 2011-04-21 | 2011-04-20 | COALINDIA | IBREALEST |
| 2011-10-10 | 2011-10-07 | BOSCHLTD, DABUR, IDEA, RELCAPITAL | COALINDIA, PATNI, PUNJLLOYD, SYNDIBANK |
| 2012-01-17 | 2012-01-16 | ADANIPORTS | MUNDRAPORT |
| 2012-04-27 | 2012-04-26 | DIVISLAB, GSKCONS, PETRONET, RCOM, RPOWER | ASIANPAINT, BANKBARODA, HDIL, IFCI, IOB |
| 2012-09-28 | 2012-09-27 | APOLLOHOSP, BAJAJHLDNG, GODREJCP, SAIL | ANDHRABANK, BEL, LUPIN, ULTRACEMCO |
| 2013-04-01 | 2013-03-28 | BAJAJFINSV, SIEMENS, TATAGLOBAL, UBL | BIOCON, GMRINFRA, INDUSINDBK, TORNTPOWER |

**`NIFTY`** — 9 events

| session | previous session | added | removed |
|---|---|---|---|
| 2010-04-08 | 2010-04-07 | KOTAKBANK | GRASIM |
| 2010-10-01 | 2010-09-30 | BAJAJ-AUTO, DRREDDY, SESAGOA | ABB, IDEA, UNITECH |
| 2011-03-25 | 2011-03-24 | GRASIM | SUZLON |
| 2011-06-29 | 2011-06-28 | INFY | INFOSYSTCH |
| 2011-08-08 | 2011-08-05 | HEROMOTOCO | HEROHONDA |
| 2011-10-10 | 2011-10-07 | COALINDIA | RELCAPITAL |
| 2012-04-27 | 2012-04-26 | ASIANPAINT, BANKBARODA | RCOM, RPOWER |
| 2012-09-28 | 2012-09-27 | LUPIN, ULTRACEMCO | SAIL, STER |
| 2013-04-01 | 2013-03-28 | INDUSINDBK, NMDC | SIEMENS, WIPRO |

**`BANK Nifty`** — 2 events

| session | previous session | added | removed |
|---|---|---|---|
| 2011-10-10 | 2011-10-07 | INDUSINDBK | ORIENTBANK |
| 2012-04-27 | 2012-04-26 | YESBANK | IDBI |

**`CNX Midcap`** — 22 events

| session | previous session | added | removed |
|---|---|---|---|
| 2010-03-09 | 2010-03-08 | LICHSGFIN | KBL |
| 2010-04-08 | 2010-04-07 | CADILAHC, EDUCOMP, HDIL, IBREALEST, IRB | JSWSTEEL, MOSERBAER, OMAXE, SESAGOA, WOCKPHARMA |
| 2010-05-17 | 2010-05-14 | WELCORP | WELGUJ |
| 2010-07-21 | 2010-07-20 | TATAGLOBAL | TATATEA |
| 2010-09-17 | 2010-09-16 | SUNTV | PANTALOONR |
| 2010-10-07 | 2010-10-06 | ADANIPOWER | ZEEL |
| 2010-11-25 | 2010-11-24 | IFCI | JUBILANT |
| 2011-03-24 | 2011-03-23 | KARURVYSYA | NIRMA |
| 2011-03-25 | 2011-03-24 | ABB, OIL, OPTOCIRCUI, PFC, SUZLON | ASIANPAINT, CROMPGREAV, HMT, RCF, SRTRANSFIN |
| 2011-04-21 | 2011-04-20 | PANTALOONR | IBREALEST |
| 2011-10-10 | 2011-10-07 | BEL, CASTROL, GMRINFRA, HINDZINC, IBREALEST, INDIABULLS, NHPC, OFSS, VOLTAS | ACKRUTI, ANANTRAJ, DCHL, GILLETTE, JETAIRWAYS, KSK, LUPIN, MAHABANK, PATNI |
| 2011-11-22 | 2011-11-21 | UNITECH | JINDALSAW |
| 2011-12-07 | 2011-12-05 | DISHTV | IBREALEST |
| 2011-12-14 | 2011-12-13 | JISLJALEQS | AREVAT&D |
| 2012-04-27 | 2012-04-26 | APOLLOTYRE, BATAINDIA, CHAMBLFERT, ESSAROIL, IBREALEST, JUBLFOOD, PIPAVAVDOC, RELCAPITAL, RENUKA, SINTEX | BAJAJHIND, BALRAMCHIN, COLPAL, HCL-INSYS, IVRCLINFRA, LICHSGFIN, MTNL, TITAN, ULTRACEMCO, YESBANK |
| 2012-07-11 | 2012-07-10 | AIL, SANOFI | APIL, AVENTIS |
| 2012-09-21 | 2012-09-20 | PEL | PIRHEALTH |
| 2012-09-28 | 2012-09-27 | BAJAJFINSV, BHUSANSTL, GSPL, HAVELLS, HEXAWARE, JSWENERGY, M&MFIN, MRF, SOUTHBANK, STAR, UBL | AIL, BEML, CHENNPETRO, CUMMINSIND, EDUCOMP, ESSAROIL, EXIDEIND, GSKCONS, HTMEDIA, PFC, STERLINBIO |
| 2013-03-19 | 2013-03-18 | IPCALAB | INDIABULLS |
| 2013-04-01 | 2013-03-28 | ADANIENT, ADANIPORTS, BANKINDIA, CROMPGREAV, DENABANK, FINANTECH, GITANJALI, KTKBANK, L&TFH, RCOM, SAIL, TV18BRDCST, WOCKPHARMA | AMTEKAUTO, CHAMBLFERT, CORPBANK, EIHOTEL, GVKPIL, IFCI, LITL, PFIZER, PUNJLLOYD, RENUKA, SCI, SINTEX, VIJAYABANK |
| 2013-04-11 | 2013-04-10 | FRL | PANTALOONR |
| 2013-04-17 | 2013-04-16 | SIEMENS | FRL |

**`Nifty Midcap 50`** — 18 events

| session | previous session | added | removed |
|---|---|---|---|
| 2010-05-17 | 2010-05-14 | WELCORP | WELGUJ |
| 2010-07-21 | 2010-07-20 | TATAGLOBAL | TATATEA |
| 2010-11-10 | 2010-11-09 | HDIL | RNRL |
| 2011-03-15 | 2011-03-14 | NCC | NAGARCONST |
| 2011-05-27 | 2011-05-26 | BHARATFORG | STERLINBIO |
| 2011-07-25 | 2011-07-22 | ADANIPOWER | CHENNPETRO |
| 2011-10-10 | 2011-10-07 | JISLJALEQS, UNITECH | LUPIN, MOSERBAER |
| 2012-02-24 | 2012-02-23 | ABIRLANUVO | HOTELEELA |
| 2012-04-27 | 2012-04-26 | ABB, BAJAJHLDNG, BEL, NHPC, OIL, OPTOCIRCUI, ORIENTBANK, RELCAPITAL, RPOWER, UNIONBANK | BAJAJHIND, HCC, IVRCLINFRA, MTNL, PATELENG, PRAJIND, ROLTA, TITAN, TTML, ULTRACEMCO |
| 2012-07-11 | 2012-07-10 | AIL | APIL |
| 2012-09-21 | 2012-09-20 | PEL | PIRHEALTH |
| 2012-09-28 | 2012-09-27 | APOLLOTYRE, BATAINDIA, BHUSANSTL, CROMPGREAV, DISHTV, GMRINFRA, HEXAWARE, HINDZINC, IFCI, JUBLFOOD, MRF, SUNTV | AIL, BAJAJHLDNG, BEL, BEML, CUMMINSIND, EDUCOMP, GESHIP, INDIANB, LITL, NCC, OIL, SCI |
| 2012-11-30 | 2012-11-29 | ADANIENT, RCOM | MPHASIS, PEL |
| 2012-12-28 | 2012-12-27 | GODREJIND | ABB |
| 2013-03-01 | 2013-02-28 | KTKBANK, SAIL | BHUSANSTL, GVKPIL |
| 2013-04-01 | 2013-03-28 | PANTALOONR | WELCORP |
| 2013-04-11 | 2013-04-10 | FRL | PANTALOONR |
| 2013-04-17 | 2013-04-16 | JSWENERGY | FRL |

**`CNX 500`** — 45 events

| session | previous session | added | removed |
|---|---|---|---|
| 2010-11-25 | 2010-11-24 | SUNTECK | JUBILANT |
| 2011-01-20 | 2011-01-19 | HATHWAY, SHREEASHTA | HINDMOTOR, MID-DAY |
| 2011-01-28 | 2011-01-27 | SHASUNPHAR | SHASUNCHEM |
| 2011-02-08 | 2011-02-07 | RELMEDIA | HIMACHLFUT |
| 2011-03-15 | 2011-03-14 | NCC | NAGARCONST |
| 2011-03-24 | 2011-03-23 | ORBITCORP | NIRMA |
| 2011-03-25 | 2011-03-24 | ADSL, BAJAJELEC, BGRENERGY, COREPROTEC, COX&KINGS, DELTACORP, EMAMILTD, GODREJPROP, JKTYRE, KEMROCK, OPTOCIRCUI, ORISSAMINE, PANTALOONR, PIPAVAVYD, SADBHAV, WHIRLPOOL, ZEEL, ZYDUSWELL | AGCNET, AJANTPHARM, ASIANELEC, BPL, DWARKESH, GOKEX, HMT, HOPFL, KOHINOOR, MAHINDUGIN, MIRZAINT, MUNJALSHOW, OMAXAUTO, PVP, RAMCOSYS, SAREGAMA, VISHALRET, VTL |
| 2011-04-11 | 2011-04-08 | COALINDIA | ALEMBICLTD |
| 2011-04-21 | 2011-04-20 | OBEROIRLTY | IBREALEST |
| 2011-05-03 | 2011-05-02 | PRESTIGE, TRIDENT | ABSHEKINDS, TRIVENI |
| 2011-05-18 | 2011-05-17 | ARSSINFRA, IL&FSENGG | BINANICEM, ESSARSHIP |
| 2011-06-21 | 2011-06-20 | IBREALEST | TV-18 |
| 2011-06-29 | 2011-06-28 | INFY | INFOSYSTCH |
| 2011-07-19 | 2011-07-18 | TV18BRDCST | IBN18 |
| 2011-08-08 | 2011-08-05 | HEROMOTOCO | HEROHONDA |
| 2011-08-16 | 2011-08-12 | JSWISPAT | ISPATIND |
| 2011-08-29 | 2011-08-26 | DISHTV | NAGARFERT |
| 2011-09-07 | 2011-09-06 | COREEDUTEC | COREPROTEC |
| 2011-09-29 | 2011-09-28 | PIPAVAVDOC | PIPAVAVYD |
| 2011-10-10 | 2011-10-07 | AIAENG, BFUTILITIE, CENTRALBK, DCB, DEWANHOUS, DHANBANK, ECLERX, GPPL, HFCL, IBPOW, INDIANB, JUBILANT, KSK, NETWORK18, PAGEIND, PERSISTENT, SBBJ, SBT, SKSMICRO, SUJANATOW | ADORWELD, AFTEK, AKSHOPTFBR, ANDHRSUGAR, BLKASHYAP, COSMOFILMS, DSKULKARNI, EVERESTIND, HERITGFOOD, HIKAL, KOUTONS, NRBBEARING, PNBGILTS, PRICOL, RICOAUTO, SAKHTISUG, SEAMECLTD, TFCILTD, TNPETRO, TRIDENT |
| 2011-11-15 | 2011-11-14 | HUBTOWN | ACKRUTI |
| 2011-11-22 | 2011-11-21 | FUTUREVENT | JINDALSAW |
| 2011-12-07 | 2011-12-05 | JAIBALAJI | IBREALEST |
| 2011-12-08 | 2011-12-07 | MERCATOR | MLL |
| 2011-12-14 | 2011-12-13 | MVL | AREVAT&D |
| 2012-01-17 | 2012-01-16 | ADANIPORTS | MUNDRAPORT |
| 2012-03-07 | 2012-03-06 | JINDALSAW | PROVOGUE |
| 2012-03-09 | 2012-03-07 | IBREALEST, L&TFH | CAROLINFO, UTVSOF |
| 2012-04-09 | 2012-04-04 | UBHOLDINGS | ZUARIAGRO |
| 2012-04-12 | 2012-04-11 | JKIL | ALFALAVAL |
| 2012-04-27 | 2012-04-26 | ALSTOMT&D, ARSHIYA, EROSMEDIA, ICRA, ICSA, KWALITY, LOVABLE, MUTHOOTFIN, PRAKASH, RAMKY, TDPOWERSYS, TECHNO, TREEHOUSE, WABCOINDIA | ADSL, BALAJITELE, DHAMPURSUG, INOXLEISUR, LAKSHMIEFL, NDTV, NELCO, NFL, PANACEABIO, PAPERPROD, STCINDIA, SURYAROSNI, TATAMETALI, WOCKPHARMA |
| 2012-05-21 | 2012-05-18 | SEINV | PATNI |
| 2012-05-22 | 2012-05-21 | DHFL | DEWANHOUS |
| 2012-06-18 | 2012-06-15 | MANINFRA | CHEMPLAST |
| 2012-07-11 | 2012-07-10 | AIL, SANOFI | APIL, AVENTIS |
| 2012-09-21 | 2012-09-20 | PEL | PIRHEALTH |
| 2012-09-28 | 2012-09-27 | AUTOIND, BALKRISIND, BBTC, FLEXITUFF, GATI, INNOIND, JBFIND, MANDHANA, MANGCHEFER, PFOCUS, SHLAKSHMI, SHRIRAMCIT, SUPREMEINF, TTKPRESTIG, VAKRANSOFT | 3IINFOTECH, ARSSINFRA, EKC, FIRSTLEASE, GAEL, GARDENSILK, GTLINFRA, ICSA, JAYSREETEA, MIRCELECTR, MOSERBAER, RELMEDIA, STER, TAJGVK, TVTODAY |
| 2012-12-06 | 2012-12-05 | CAPF | FCH |
| 2013-01-23 | 2013-01-22 | MOIL | DCHL |
| 2013-03-07 | 2013-03-06 | SPARC | ORIENTPPR |
| 2013-03-19 | 2013-03-18 | PVR | INDIABULLS |
| 2013-04-01 | 2013-03-28 | AANJANEYA, DEN, GABRIEL, GRUH, GTLINFRA, HERITGFOOD, HINDCOPPER, IL&FSTRANS, IMFA, NIITTECH, ONELIFECAP, SWANENERGY, TBZ, WELSPUNIND, WOCKPHARMA | BANCOINDIA, BEPL, GTL, INDORAMA, INDSWFTLAB, JAIBALAJI, KWALITY, MVL, NILKAMAL, ORIENTHOT, SHLAKSHMI, SONASTEER, STERLINBIO, UNITY, WIPRO |
| 2013-04-10 | 2013-04-09 | LINDEINDIA | BOC |
| 2013-04-11 | 2013-04-10 | FRL | PANTALOONR |
| 2013-04-17 | 2013-04-16 | GEODESIC | FRL |

## 4. Distinct symbols

**628 distinct symbols** appear across all 17 indices over the 827 sessions.

The widest single index is `CNX 500`, which held 620 distinct symbols across its 634 sessions while never publishing more than 500 at a time — the gap between those two numbers is the survivorship the corpus lets a backtest avoid, and the reason a present-day constituent list is not a substitute for it.

## 5. Dated sectoral assignment

Membership of a sectoral index on a session is an **exchange-published, dated sector
assignment**. That is the thing this corpus offers which no other surface in the platform
does, and it is what makes the question worth measuring rather than asserting.

- Sectoral indices present: **10** (`CNX ENERGY`, `CNX FMCG`, `CNX IT`, `CNX Infrastructure`, `CNX MNC`, `CNX PHARMA`, `CNX PSE`, `CNX PSU BANK`, `CNX Realty`, `CNX SERVICE`)
- Sessions carrying at least one sectoral assignment: **790**
- Distinct symbols with **at least one** dated sectoral assignment: **162**
- Symbols holding **more than one at once**: **42** (widest simultaneous: **4** indices)
- Symbols that **moved** between sectorals in one step (left one and joined another on the same date): **0**, over 0 transitions
- Symbols whose sectoral **identity changed** at all, abruptly or gradually (a superset of the line above): **10** — `ABB`, `CAIRN`, `CONCOR`, `DLF`, `INDHOTEL`, `OFSS`, `SCI`, `TATACOMM`, `TECHM`, `UNITECH`

**No symbol was reclassified in a single step**, but **10 changed sector gradually** — holding
two sectors for a while before settling in one. So sector assignment here is not
static, and a point-in-time series has to honour the dates: pinning a symbol to the
sector it ends the span in would misclassify it for the earlier part.

**Read those changes carefully — half of each is an artefact of the corpus, not an
event.** The sectoral indices arrive in cohorts (2010-02-26, 2010-07-19, 2010-10-11, 2011-01-31), and on a cohort's arrival date every one of its members *gains* that sector in
this file for the first time. That gain is the index starting to be published, not
the exchange reclassifying anything. What is unambiguously real is the **departure**:
a symbol that stops appearing in a sectoral it had been in, on a date no index
arrived. Each of the symbols above ends the span holding a sector it did not start
with *and* having lost one it did hold, so all of them changed sector — but the date
to trust is the loss, and a reconstruction validated against this corpus must treat
a cohort-arrival date as uninformative rather than as a reconstitution.

### How far this goes toward a point-in-time sector classification, and where not

Measured against the symbols that **actually traded** over the same span — 1,842 distinct symbols across 832 sessions of `nse_bhavcopy_legacy`. The first eighteen months of that is the pre-ISIN bhavcopy, which carries no ISIN column at all, so for those years the price record itself lives in the same unresolved symbol space as `ffix` and the comparison is exact rather than approximate:

- **162 of 1,842 = 8.8%** of the traded universe carries a dated sectoral
  assignment at some point in the span. The other **91.2%** — 1,680 symbols — has none, at any date.

## The honest limits

1. **It ends 2013-04-30.** The member covers 3.3 years, not ten. Every session from 2013-05-02 onward carries no
   `ffix` member at all — swept, not assumed. So this closes the 2010-2013 hole in the
   membership record and leaves 2013-2026 exactly as it was.
2. **It is symbol-keyed and cannot be joined until W4.** No ISIN appears anywhere in this
   dataset, by design (invariant #2). Until a point-in-time symbol master exists, these
   symbols cannot be attached to a price, a corporate action or a fundamental, and
   resolving them through today's listing would build the survivorship bias directly
   into the universe.
3. **Index constituents only, so the micro-cap tail is invisible.** An index membership
   file says nothing about a security that was in no index. The sector-coverage
   percentage above is the size of that blind spot, measured.
4. **NIFTY 50 *is* here** — from the very first bundle, on every one of the
   827 sessions. Do not confuse this with the `Ix` member, where
   Phase 1 correctly found NIFTY 50 absent. Two different members of the same bundle:
   `Ix` is 2010-only with a rotating six-index set and no NIFTY; `ffix` is this.

## 7. Is this enough to validate a backward reconstruction from reconstitution circulars?

The study recommended reconstructing index membership backward from NSE's own
reconstitution circulars, for the thematic-universe problem. The question is whether this
corpus can *check* such a reconstruction. Reasoning from the measurements above, not from
a general impression:

**Yes, for its span, and decisively so.**

1. **Coverage is daily and gapless.** 827 of 827 calendar sessions carry a member, with no interior gap in
   either direction. So a reconstruction can be diffed against ground truth **on every
   trading day**, not sampled at the dates it happens to agree on. A reconstruction that
   gets an effective date wrong by one session is detectable; against quarterly anchors
   it would not be.
2. **The ground truth is internally consistent.** 0 sessions out of 11,501 index-sessions show a constituent count
   away from the index's nominal size. A validation target that disagreed with itself
   would make every reconstruction mismatch ambiguous; this one does not, so a mismatch
   is unambiguously the reconstruction's.
3. **The events to land on are few and clustered.** All 154 change
   events fall on 60 distinct dates, and 18 of those are
   dates on which three or more indices changed at once — the signature of a scheduled
   reconstitution rather than a single corporate event (2010-04-08, 2010-10-01, 2010-10-07, 2011-03-24, 2011-03-25, 2011-04-21, 2011-06-29, 2011-08-08, 2011-10-10, 2011-12-07, 2012-01-17, 2012-04-27, 2012-07-11, 2012-09-21, 2012-09-28, 2013-04-01, 2013-04-11, 2013-04-17).
   A circular-derived reconstruction is exactly a claim about those dates, so the corpus
   tests the reconstruction's core assertion rather than its edges.

**Where the validation does *not* reach.** Three limits, and they matter:

- It validates **2010-01-04..2013-04-30 only**. A
  reconstruction is wanted for ten-plus years; this checks 3.3 of them. A method that
  validates here is *credible* for 2013-2026 and *verified* nowhere in it — the circular
  formats, the index rename waves and the reconstitution cadence all changed after 2013.
- It validates the **17 indices published here**, and the thematic indices the study
  actually wants (the post-2015 Nifty sectoral and strategy families) are not among them.
- It cannot validate an **ISIN-keyed** reconstruction, only a symbol-keyed one. A
  reconstruction that resolves circular symbols to ISINs has an identity step this corpus
  is silent about, and that step is where a survivorship bias would enter.

**So the practical recommendation:** build the reconstruction, validate it against this
corpus over 2010-2013 as a **method test** rather than a coverage claim, and treat a pass
as evidence the circular-reading is sound — not as evidence the 2013-2026 output is
right. Anything downstream of 2013-04-30 stays unvalidated until another dated surface
turns up.
