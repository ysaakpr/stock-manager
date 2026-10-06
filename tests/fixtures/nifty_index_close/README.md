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

## `renames/` — the switch files `index_aliases.yaml` cites (M11.3)

Byte-identical copies of L0 payloads from the M11.2 backfill (`nse_index_close_snapshot`, 2026-10-06),
taken with no new fetch. Each alias row names two of these: the last file under the old name and the
first file under the new one. `test_each_alias_resolves_old_and_new_name_to_one_series_across_its_switch`
reads them. `m11_2_unknown_names.txt` is the frozen list of the 192 names M11.2's final report did
not know.

M11.2 also found a format era the probe missed: `DD/MM/YYYY` dates from 2014-06-26 into 2015-06.
The note above that this source has no format eras is therefore superseded.
