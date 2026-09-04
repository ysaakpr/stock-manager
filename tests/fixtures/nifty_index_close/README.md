# `ind_close_all_<DDMMYYYY>.csv` — real captures, both naming eras

Fetched 2026-09-04 from `nsearchives.nseindia.com/content/indices/` (browser UA,
`Referer: https://www.nseindia.com/`, >=2.6 s spacing) during the M11.1 macro-source probe.
Evidence and the full depth measurement: `ops/gates/M11-macro-source-probe.md`.

| File | Session | Indices | Era |
|---|---|---|---|
| `cnx_era/ind_close_all_01102012.csv` | 2012-10-01 | 30 | earliest session the archive serves (2012-07-01 is a 404) |
| `cnx_era/ind_close_all_06112015.csv` | 2015-11-06 | 53 | last CNX-named session observed |
| `nifty_era/ind_close_all_10112015.csv` | 2015-11-10 | 53 | first Nifty-named session observed — 48 of 53 names changed |
| `nifty_era/ind_close_all_01092026.csv` | 2026-09-01 | 165 | current |

The 2015-11-06 / 2015-11-10 pair is the evidence behind `index_aliases.yaml`: it brackets the
NSE/IISL rename event in which 48 of 53 index names changed at once. A parser keyed on today's
names reads zero rows from either `cnx_era` file, which is what the alias test asserts.

The header is byte-identical across all four (13 columns), so this source has **no format eras** —
only naming eras. That is unusual for this platform and worth not forgetting.
