# BN Agrochem ISIN reissue — a cut of the live L0 captures (2026-11-12 .. 2026-11-21)

The evidence `dataplatform.identity.repair_reissue` re-reads, frozen for
`tests/integration/test_repair_reissue.py`. BN Agrochem Ltd (NSE BNAGROCHEM, listed 2026-04-20)
split ₹10 → ₹1 ex 2026-11-16, and its ISIN was reissued INE00HZ01011 → INE00HZ01029 that day,
with no suspension.

**Record-verbatim cut.** Every CSV line is byte-for-byte a line of the L0 payload named below;
only the selection is ours: the header, then the BNAGROCHEM row, and the RELIANCE and TCS rows
as bystanders (so "the repair touches only these two ISINs" is observable). Order is the
source's. The bhavcopies are stored here unzipped; the test zips each back under its original
inner filename before landing it in a `tmp_path` L0. The corporate-actions file keeps the one
BNAGROCHEM record with every key and value as published, re-serialised as indented JSON. No
request was made to build this: it was cut from `data/L0` on 2026-11-21.

## `nse_bhavcopy_udiff/` — the sessions either side of the switch

| Session | BNAGROCHEM trades as | L0 sha256 of `BhavCopy_NSE_CM_0_0_0_<date>_F_0000.csv.zip` |
|---|---|---|
| 2026-11-12 | INE00HZ01011 | `b28a71c49f757ceff661093fd308ef6e49740ea583c9fb214f836a3b011e0652` |
| 2026-11-13 | INE00HZ01011 (last old session, close ₹162.66) | `758ec3b60fd492a29ff3ebc6c069269bc326dc5ca662cfba8892f16939841b7b` |
| 2026-11-16 | INE00HZ01029 (first new session, close ₹17.08) | `4b092142ecfdd80eb359a62a9146a598a5fad274ef84930d0d7caf0e9eeaf645` |
| 2026-11-17 | INE00HZ01029 | `2bb2797549576fb056f9b739823c19697671e6e3c59ecf730844d0111381fb9f` |

2026-11-14/15 are a weekend; there is no session and no file.

## `nse_equity_list/` — the `EQUITY_L` captures

| Capture | BNAGROCHEM listed as | L0 sha256 of `EQUITY_L_<date>.csv` |
|---|---|---|
| 2026-11-13 | INE00HZ01011, face value 10 | `c78517837fe47a29a2b4cbb970899c202eb45fab827134aacf8287835ec5acee` |
| 2026-11-14 | INE00HZ01011, face value 10 (Saturday `identity_refresh`; same bytes) | `c78517837fe47a29a2b4cbb970899c202eb45fab827134aacf8287835ec5acee` |
| 2026-11-16 | INE00HZ01029, face value 1 | `d99b125033700564a93a82b54351436d8d11598ef97899673b79aab0ea10382b` |
| 2026-11-21 | INE00HZ01029, face value 1 (the snapshot the broken ingest read) | `9a583c3c9e5be66d21a29af998714504d9b684a5a64b6da63daded5372a13016` |

The last old capture is the Saturday one, so `L0EquityListSeries` looked for a session in
[2026-11-14, 2026-11-16) to confirm the old ISIN, found none, and refused — the live defect. Since
2026-11-24 it reaches back past a non-session capture to Friday 2026-11-13's bhavcopy, and the
ingest over this cut resolves the boundary to 2026-11-16 itself
(`test_the_fixed_ingest_places_the_boundary_itself`); the broken state the repair tests start
from is reproduced with no evidence.

## `nse_symbol_changes/symbolchange_20261121.csv`

The 2026-11-21 capture (`cc184a556ee7537ebd63f038ea26da698ae9e1c7e879a7ddf4a3183f4a712141`): the
header and its first data row as a bystander. BNAGROCHEM has never been renamed.

## `nse_corp_actions/corporateActions_20261017_20261121.json`

The 2026-11-21 `ca_refresh` capture (`70e352674e427e19c659339bcbc6122e0a3cc08e355a8e02694cbfcd04a3d62f`):
`Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share`, ex and record
date 16-Nov-2026, filed against INE00HZ01011.
