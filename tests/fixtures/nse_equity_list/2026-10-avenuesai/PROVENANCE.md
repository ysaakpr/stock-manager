# AvenuesAI ISIN reissue — a cut of the live L0 captures (2026-10-12 .. 2026-10-24)

The evidence `dataplatform.identity.repair_avenuesai` re-reads, frozen for
`tests/integration/test_repair_avenuesai.py`. AvenuesAI Ltd (NSE INFIBEAM, CCAVENUE from
2026-02-03) had its ISIN reissued INE483S01020 → INE483S01038 (face value ₹1 → ₹10) while the
stock was suspended.

**Record-verbatim cut.** Every line is byte-for-byte a line of the L0 payload named below; only
the selection is ours: the header, then the CCAVENUE row when the payload carries one, and the
RELIANCE and TCS rows as bystanders (so "the repair touches only these two ISINs" is observable).
Order is the source's. The bhavcopies are stored here unzipped; the test zips each back under its
original inner filename before landing it in a `tmp_path` L0. No request was made to build this:
it was cut from `data/L0` on 2026-10-29.

## `nse_bhavcopy_udiff/` — the sessions either side of the switch

| Session | CCAVENUE trades as | L0 sha256 of `BhavCopy_NSE_CM_0_0_0_<date>_F_0000.csv.zip` |
|---|---|---|
| 2026-10-12 | INE483S01020 (last old session) | `818f673b3cdb96ae8ec4e466c7c4fd3a28d035482173a675dd24f9437a3ecd99` |
| 2026-10-13 | absent | `b3aba7f1e20587e4c77a5de2b720cbcd5d8f4c8fe42e1b67f06f0d7deaabbf46` |
| 2026-10-14 | absent | `db0b3819da45da615d3aa5ac7888a2b0670189d73f8d33f1e377146c89d9c94f` |
| 2026-10-15 | absent | `3d1c7039e2c70868cc44b51d1f9e4b0f942afc0575fe8c4a524aed9e01eac3e8` |
| 2026-10-16 | absent | `d2c4406c1ecbfbcb9acdafa424f921caad7467d97da47c65973b34aa0aac70bc` |
| 2026-10-19 | INE483S01038 (first new session) | `f0a2e9bf7cb185bb112704355772d2954d662abee3d7295414aabc26d6ecfbde` |

2026-10-17/18 are a weekend; there is no session and no file.

## `nse_equity_list/` — the daily `EQUITY_L` captures (~19:15 IST)

| Capture | CCAVENUE listed as | L0 sha256 of `EQUITY_L_<date>.csv` |
|---|---|---|
| 2026-10-12 | INE483S01020 | `617149fce76d0b73a02e1da44eb6cfaf88e936817d4a77fa9ed39411576de228` |
| 2026-10-13 | absent | `bf199940551b6b977afc88f5096b072cdd2663d600bd7abe75433723dc4535a8` |
| 2026-10-14 | absent | `83154043b34313468d9c02a616dd731308526af503dc7137f37eaa308a217424` |
| 2026-10-15 | absent | `951c5d376d83ef0ddf8b040f9a52cd8b19388e7a51de8880af9a0b06ea0865a1` |
| 2026-10-16 | absent | `474ad748f65c9ffa4cf9aa2e483117dfd0f68ccafd6c4c8a0cca02583556f765` |
| 2026-10-17 | absent | `474ad748f65c9ffa4cf9aa2e483117dfd0f68ccafd6c4c8a0cca02583556f765` |
| 2026-10-19 | INE483S01038 | `64bc3997cc8e6d67f77fec2d04e4f74c45d7f7cb712ffd55c0885fb5a56722d8` |
| 2026-10-24 | INE483S01038 (the snapshot the broken ingest read) | `6d8b5e219f2877136346831610975a1286f6aac3591d7566d78a22556adbab6a` |

## `nse_symbol_changes/symbolchange_20261024.csv`

The 2026-10-24 capture (`b080e538a5c3c8b5cc1fd048768b2b660c880cc38b3c2bafc7c0f1b2f983566e`): the
header, its first data row as a bystander, and `AvenuesAI Limited,INFIBEAM,CCAVENUE,03-FEB-2026`.
