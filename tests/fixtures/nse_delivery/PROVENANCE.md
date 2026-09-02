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
