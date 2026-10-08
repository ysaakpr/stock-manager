# `month_first_2023/` — the three month-first close-all files and their neighbours (M14.2)

Byte-identical copies of L0 payloads under `data/L0/nse_index_close_snapshot/2023/04/`, fetched by
the M11.2 backfill from `nsearchives.nseindia.com/content/indices/` on 2026-10-06 and copied
2026-10-07 with no new request. Each SHA-256 below equals the `sha256` in its L0 `.meta.json`.

| File | `Index Date` as printed | Order | Nifty 50 close | SHA-256 |
|---|---|---|---|---|
| `ind_close_all_05042023.csv` | `05-04-2023` | day-first | 17557.05 | `1ca3afe023a81d57c683da96cbb36498e439395cb1116f64cee1362d117869ec` |
| `ind_close_all_06042023.csv` | `04-06-2023` | **month-first** | 17599.15 | `5ec0d8a41e5494f414df3774648d954ca0160c799759bd538625277dc9011cda` |
| `ind_close_all_10042023.csv` | `04-10-2023` | **month-first** | 17624.05 | `575d1b73761842e3150c3e788fef14664ad725055aa5e2fe49e1abfe78bef9f4` |
| `ind_close_all_11042023.csv` | `04-11-2023` | **month-first** | 17722.30 | `37ede13a06f935aa915f51fb3533c75c9e75dc317114e0eb14e3eacc733b1483` |
| `ind_close_all_12042023.csv` | `12-04-2023` | day-first | 17812.40 | `101aa4698a24da5c7985ec0bbf69e545a0cfe1d9466b260c1c4c86502e2cad24` |

Every row of each file carries the same `Index Date`. 2023-04-07 (Good Friday) is a market holiday,
so there is no file between the 6th and the 10th.

The content is each named session's, not the session the printed date names read day-first:
the Nifty 50 `Points Change` column chains exactly — 17557.05 + 42.10 = 17599.15, + 24.90 =
17624.05, + 98.25 = 17722.30, + 90.10 = 17812.40. Read day-first, the three dates would be
2023-06-04 (a Sunday), 2023-10-04 and 2023-11-04 (a Saturday) — the last two months after the files
were published.

Read by `tests/unit/test_macro_backfill.py` (`test_month_first_files_publish_the_session_they_are_named_for`
and the two tests after it).
