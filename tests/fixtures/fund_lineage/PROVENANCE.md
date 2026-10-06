# `fund_lineage` fixtures — provenance

`prices_raw_nse.csv` is NSE `prices_raw` rows copied **verbatim** out of the server lake's L1 on
2026-10-05 (one `COPY … TO` over `data/L1/prices_raw`, no value edited), for the symbols and
windows `tests/unit/test_fund_lineage.py` judges:

| symbol | window | ISINs |
|---|---|---|
| BANKBEES, GOLDBEES | 2019-11-15 .. 2019-12-31 | INF732E01078 → INF204KB15I9, INF732E01102 → INF204KB17I5 |
| AXISNIFTY | 2020-07-15 .. 2020-07-31 | INF846K01ZL0 → INF846K01W98 |
| UTISXN50 | 2021-01-15 .. 2021-03-05 | INF789F1AHR6 → INF789F1AUU3 |
| HDFCSENSEX, HDFCNIFIT | 2024-01-25 .. 2024-02-08 | INF179KC1973 → INF179KC1HX2, INF179KC1DX1 → INF179KC1IA8 |
| HDFCLIQUID | 2025-02-20 .. 2025-03-05 | INF179KC1HE2 → INF179KC1JG3 |
| AXISBANK (INE238A01034) | the sessions of every window above | the session calendar: it traded on 2020-07-24, the session AXISNIFTY missed |

The matching `Bc` evidence is `../nse_pr_bundle/fund_unit_splits/`.
