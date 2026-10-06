# `nse_market_structure` — era `2026-09-08`

The five market-structure snapshot payloads, exactly as served, from the OPS daily-snapshotter
probe on **2026-09-08T18:36 IST**. Evidence and method: `ops/gates/daily-snapshotter-2026-09-08.md`.
Checksums are recorded per entry in `dataplatform/ingest/source_register.yaml` and match these
files byte for byte.

| File | Register id | URL |
|---|---|---|
| `ind_niftytotalmarket_list.csv` | `nse_industry_classification` | `https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv` |
| `sec_list.csv` | `nse_price_bands` | `https://nsearchives.nseindia.com/content/equities/sec_list.csv` |
| `reportASM.json` | `nse_asm_list` | `https://www.nseindia.com/api/reportASM` |
| `reportGSM.json` | `nse_gsm_list` | `https://www.nseindia.com/api/reportGSM` |
| `reportESM.json` | `nse_esm_list` | `https://www.nseindia.com/api/reportESM` |

This is the **first** era for all five sources, because 2026-09-08 is the first day any of them was
ever captured. There is no earlier era to freeze and there never can be: every one of these
endpoints serves only its current snapshot.

The three JSON payloads are the fixtures the staleness test drives — each row's `asmTime` /
`gsmTime` / `esmTime` reads `08-Sep-2026`, so replaying this era under any other capture date is
exactly the "HTTP 200 carrying the previous session" case the snapshotter must detect.
