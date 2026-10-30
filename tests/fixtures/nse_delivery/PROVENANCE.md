# NSE delivery fixtures (`sec_bhavdata_full`)

Real files, fetched from the live NSE archive host under the `nse_sec_bhavdata_full` register
policy (`source_register.yaml`) — browser UA, `Referer: https://www.nseindia.com/` — during M1.6
on **2026-09-02**. Copied here byte-for-byte.

URL pattern (`source_register.yaml` → `nse_sec_bhavdata_full`):
`https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{DDMMYYYY}.csv`

| Session | File | Bytes | sha256 | Data rows | Rows with `-` delivery |
|---|---|---|---|---|---|
| 2026-08-07 | `sec_bhavdata_full_07082026.csv` | 374,452 | `8b937105b230a12d50395b0c9e1f02fc1c39d611914988985c4a34bc5e7af06e` | 3,299 | 318 |
| 2026-08-06 | `sec_bhavdata_full_06082026.csv` | 373,499 | `440cc611aa315736f1732873de93dd2fdee288f7abf49849a287af9e63217501` | 3,287 | 317 |

The 2026-08-07 file's checksum matches the one the C.1 verification sweep recorded in
`source_register.yaml` (`sample_sha256`, `sample_bytes`), so the frozen fixture is the exact
payload that verified the source.

## The `-` delivery values these fixtures exist to prove

Both files carry the 15-column header (every name after the first has a leading space in the
file, stripped by the parser):

```
SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
```

The trade-to-trade series (`BE`, `BZ`) publish `-` for both `DELIV_QTY` and `DELIV_PER`, because
delivery is not a meaningful concept where intraday squaring-off is barred. The parser models that
as `None`, never `0`: a zero would read downstream as "0 % delivered" and make a delivery-spike
detector fire on every trade-to-trade name every day. In these files the two delivery columns are
always `-` together (never one without the other), and there is no `(symbol, series)` duplicated
within a session.

There is no ISIN column: a delivery row is joined to prices only after
`dataplatform.ingest.nse.delivery.resolve` maps `(symbol, trade_date)` to an ISIN through the D2
identity master (invariant #2).

## 2020-07-13 — the third delivery-identity era (added 2026-10-05)

| Session | File | Bytes | sha256 | Data rows |
|---|---|---|---|---|
| 2020-07-13 | `sec_bhavdata_full_13072020.csv` | 219,205 | `e927d5d328091a09fb76f9363b642ee2941a022bba5fc8906fa653971acda39c` | 1,993 |

Copied byte-for-byte from the lake's L0 (`nse_sec_bhavdata_full/2020/07/`, fetched 2026-09-06; the
checksum above is the L0 sidecar's). It pairs with `../nse_bhavcopy/legacy/cm13JUL2020bhav.csv.zip`,
which makes the **legacy bhavcopy + `sec_bhavdata_full`** era (2019-10-01 .. 2024-07-05) testable
offline beside the other two pairs: legacy + MTO (2011-06-22) and UDiFF + `sec_bhavdata_full`
(2026-08-07). `tests/unit/test_delivery_identity_eras.py` joins all three against a master built
from the real 2026-08-08 `EQUITY_L.csv` — which backdates today's ISIN to the listing date and
lists no ETF — to pin the 2026-10-05 audit's two delivery-join defects (reissued ISINs, ETFs).

## 2026-10-26 — an ambiguous master the session settles (added 2026-10-29)

`ccavenue_reissue_26102026/` holds **excerpts**, not whole files: the header and three data lines
of each, copied byte-for-byte (line endings included) from the lake's L0 payloads for the session.
Nothing in a kept line was edited; lines were only dropped.

| Excerpt | Copied from (L0) | L0 sha256 | Lines kept |
|---|---|---|---|
| `sec_bhavdata_full_26102026_EXCERPT.csv` | `nse_sec_bhavdata_full/2026/10/sec_bhavdata_full_26102026.csv` (398,036 bytes, 3,526 data rows) | `0a6079a40b17a3bbfaeace9c5d8dfcefe2e8dbd6d4a139433c52451de2d3c235` | header, `20MICRONS` EQ, `3IINFOLTD` BE (`-` delivery), `CCAVENUE` EQ |
| `BhavCopy_NSE_CM_0_0_0_20261026_F_0000_EXCERPT.csv` | the single CSV member of `nse_bhavcopy_udiff/2026/10/BhavCopy_NSE_CM_0_0_0_20261026_F_0000.csv.zip` | `723bee076946c104b9cd56d09ca9b3776b062f19c136acc289c7c279290377e3` (the zip) | header and the same three `(symbol, series)` |

AvenuesAI's ISIN was reissued INE483S01020 → INE483S01038 (switch 2026-10-19). The 2026-10-24
identity refresh left two open `CCAVENUE` windows (reconciliation ids 72/73), so the master
answers `AmbiguousSymbolError` for the symbol, while the session's own bhavcopy states
`INE483S01038` for `(CCAVENUE, EQ)`. `nse_delivery` failed on this file for 2026-10-26 .. 10-28.
`tests/unit/test_delivery_ambiguity.py` pins how `delivery.resolve` handles it.
