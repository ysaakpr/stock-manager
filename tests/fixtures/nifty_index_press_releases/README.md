# nifty_index_press_releases fixtures

Each PDF is the exchange's own bytes, fetched 2026-10-05 from `https://niftyindices.com/Press_Release/<name>` (the 2007 one by a one-off exploratory GET the same day; the rest via the campaign into L0 `nifty_index_press_releases/<announced>/<name>`). One per layout era the parser handles:

| Path | Layout | sha256 |
| --- | --- | --- |
| `2007_detached/ind_prs12092007.pdf` | 2007_detached | `4b94e4c2406088e3…` |
| `2014_columns_apart/ind_prs27022014.pdf` | 2014_columns_apart — the text layer prints table columns apart; unparsed, never dropped (M14.1, `PROVENANCE.md`) | `ad374e90736c719b…` |
| `2015_dated_clauses/ind_prs23012015.pdf` | 2015_dated_clauses — one intro sentence gives two effective dates; `1.` headings (M14.1, `PROVENANCE.md`) | `e6472350cb1369dc…` |
| `2016_scrip_name/ind_prs17102016.pdf` | 2016_scrip_name — `Scrip Name` tables; lettered parts dated differently (M14.1, `PROVENANCE.md`) | `e3ad170876e6278a…` |
| `2018_trailing_date/ind_prs01082018.pdf` | 2018_trailing_date — the one effective date is stated after the tables | `5b31cf53425bf0e2…` |
| `2019_split_day/ind_prs21012019.pdf` | 2019_split_day — the text layer prints "January 2 8, 2019" | `ff617ab210bc5a15…` |
| `2021_replaced_lists/ind_prs15092021.pdf` | 2021_replaced_lists — restates the Aug-2021 NIFTY 500 / Midcap 150 / Smallcap 250 lists after the REIT revocation | `56ab5fbf0a9e6e93…` |
| `2023_image_only/ind_prs19062023.pdf` | 2023_image_only | `03a9a561f6aecc24…` |
| `2023_spinoff_exclusion/ind_prs05092023.pdf` | 2023_spinoff_exclusion | `b8b7f5638efcc5e4…` |
| `2024_modern/ind_prs10102024.pdf` | 2024_modern | `415d454c98245950…` |
| `2024_revocation/ind_prs19032024.pdf` | 2024_revocation | `1bef44dabdf594b9…` |
| `listing/press_release_listing_excerpt.html` | listing | `521b2a2b53f8ff31…` |

`listing/press_release_listing_excerpt.html` is an *excerpt* of `https://niftyindices.com/press-release` (captured 2026-10-05): the `pressItem` blocks for the releases above plus a few the title filter must reject, copied verbatim.
