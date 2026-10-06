# Macro / backdrop sources — real captures, 2026-10-06

Fetched from the campaign server during the `macro-probes` task (browser UA, ≥3 s spacing per host,
no cookie, no login). Every request, with status, size and sha256, is listed in
`ops/gates/macro-probes-2026-10-06.md`; the register rows are in
`dataplatform/ingest/source_register.yaml` under §4.1 row 17.

| File | Source id | Request |
|---|---|---|
| `fbil/2026-10-06/refrates_20180709_20180713.json` | `fbil_reference_rates` | `GET www.fbil.org.in/wasdm/refrates/fetchfiltered?fromDate=2018-07-09&toDate=2018-07-13&authenticated=false` — the archive's first four sessions |
| `fbil/2026-10-06/refrates_latest.json` | `fbil_reference_rates` | `GET …/refrates/fetch?authenticated=false` — newest two sessions (2026-09-28/29) |
| `rbi_home/2026-10-06/Home.html` | `rbi_current_rates` | `GET www.rbi.org.in/Home.aspx` |
| `wpi/2026-10-06/download_data_2223.html` | `oea_wpi_monthly_index` | `GET eaindustry.nic.in/download_data_2223.asp` |
| `wpi/2026-10-06/wpi_monthly_index_202609.xlsx` | `oea_wpi_monthly_index` | the file that page links |
| `gst/2026-10-06/Gross_Net_Tax_collection.xlsx` | `gstn_tax_collection` | `GET tutorial.gst.gov.in/offlineutilities/gst_statistics/Gross_Net_Tax_collection.xlsx` |
| `india_vix/2026-10-06/india_vix_20260901_20260910.json` | `nifty_india_vix_history` | `POST niftyindices.com/BackPage/getHistoricaldatatabletoString`, `INDIA VIX`, 01-Sep-2026..10-Sep-2026 |
| `india_vix/2026-10-06/india_vix_20200302_20200306.json` | `nifty_india_vix_history` | same, 02-Mar-2020..06-Mar-2020 (`INDEX_NAME` upper-cased in this era) |
| `india_vix/2026-10-06/india_vix_20141001_20141010.json` | `nifty_india_vix_history` | same, 01-Oct-2014..10-Oct-2014 — `[]`, below the endpoint's depth |
| `worldbank/2026-10-06/IND_FP.CPI.TOTL.ZG.json` | `worldbank_indicator_api` | `GET api.worldbank.org/v2/country/IND/indicator/FP.CPI.TOTL.ZG?format=json&per_page=100` (byte-identical to the 2026-09-04 sample) |

Each is the body exactly as received. None carries a credential.
