# DQ-5 — index membership history (audit 2026-10-05, fix 5 of 5)

**Source.** NSE Indices' own press releases. `https://niftyindices.com/press-release` lists every
release back to 1998 in static HTML (date + title + PDF path); each change release is
`/Press_Release/ind_prs<DDMMYYYY>[_n].pdf`, stating index, company, symbol, announcement date and
effective date. Register id `nifty_index_press_releases`; robots permits both paths. No anti-bot
evasion: browser UA, `Referer`, ≥3.5 s spacing under a host lease.

**Requests to niftyindices.com on 2026-10-05: ~193** (14 exploratory + listing 1 + 7 anchors +
171 releases), under the ~200 owner line (AGENTIC_CONTEXT §3.3). 420 releases pass the title filter;
171 are in L0 (contiguous back to 2018-01-08); **248 older ones (2018-01 → 1998) were not fetched**
— extending is an owner-approved campaign of ~250 requests.

**Depth: 2021-10-22 for all seven indices.** The wall is `ind_prs23082021.pdf` — the Sep-2021
semi-annual review, 29 pages drawn entirely as images (no text layer; no OCR on this box). The
history refuses to reconstruct across it (`coverage_start` = its announcement + 60 days). The
2018-01 → 2021-08 releases are in L0 and become usable the day that one release is read (OCR, or a
text copy of it from the exchange). Pre-~2015 releases print statements and tables detached and no
symbols; the parser marks them unparsed rather than guessing.

**Reconciliation.** Every segment (span between consecutive change dates) since coverage start
holds exactly the index's fixed size once the members clipped to their first appearance are
counted out — those are real demerger stand-in windows (JIOFIN 2023-08-21→09-07, ITC Hotels
2025-01-29→02-10, TMCV 2025-11-12→11-17, …), where the index really carried 51/501. Zero
unexplained off-size segments. All 17 stored daily snapshots (2026-09-08 → 2026-10-01) of NIFTY 50
and NIFTY 500 equal the reconstruction exactly, including the 2026-09-30 Wipro→BSE change. 0 events
quarantined (every symbol resolved at its effective date). Residual event-level items are listed per
index below (e.g. TATAMTRDVR's 2023-09-29 inclusion in NIFTY 100/200 whose exit was by a corporate
action release, not a table).

**Known limitation.** A spin-off's stay is clipped to its first NSE session or first constituents
list, not its ex-date: the dummy-symbol period between ex-date and listing is not reconstructed
(it was untradeable).

---

# Index membership history — build 2026-10-05

Listing: `nifty_index_press_releases/2026-10-05/press_release_listing_20261005.html`. Candidate releases: 417; in L0: 169; not fetched: 248.

A segment is the span between two consecutive change dates; its count is the members effective on its first day. *Explained* off-size segments are those where the excess is exactly the members clipped to their first appearance (a demerger spin-off's stand-in window, which the index really carried).

| Index | Expected | Coverage start | Anchor | Events applied | Segments | Off-size (unexplained) | Max abs unexplained residual | Other residuals |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| nifty50 | 50 | 2021-10-22 | 2026-10-05 | 25 | 23 | 3 (0) | 0 | entry_before_first_session=3, unreadable_section=1 |
| niftynext50 | 50 | 2021-10-22 | 2026-10-05 | 122 | 25 | 2 (0) | 0 | entry_before_first_session=2, unreadable_section=1 |
| nifty100 | 100 | 2021-10-22 | 2026-10-05 | 116 | 38 | 5 (0) | 0 | entry_before_first_session=5, include_not_held_later=1, unreadable_section=1 |
| nifty200 | 200 | 2021-10-22 | 2026-10-05 | 220 | 44 | 5 (0) | 0 | entry_before_first_session=5, include_not_held_later=1, unreadable_section=1 |
| nifty500 | 500 | 2021-10-22 | 2026-10-05 | 567 | 88 | 9 (0) | 0 | entry_before_first_session=7, unreadable_section=1 |
| niftymidcap150 | 150 | 2021-10-22 | 2026-10-05 | 301 | 25 | 1 (0) | 0 | entry_before_first_session=1, unreadable_section=1 |
| niftysmallcap250 | 250 | 2021-10-22 | 2026-10-05 | 652 | 54 | 2 (0) | 0 | entry_before_first_session=2, unreadable_section=1 |

## nifty50

| Segment start | Reconstructed | Residual | Clipped stand-ins |
| --- | --- | --- | --- |
| 2021-10-22 | 50 | +0 |  |
| 2022-03-31 | 50 | +0 |  |
| 2022-07-29 | 50 | +0 |  |
| 2022-09-14 | 50 | +0 |  |
| 2022-09-30 | 50 | +0 |  |
| 2023-07-13 | 50 | +0 |  |
| 2023-08-21 | 51 | +1 | 1 |
| 2023-09-07 | 50 | +0 |  |
| 2024-01-05 | 50 | +0 |  |
| 2024-03-28 | 50 | +0 |  |
| 2024-09-30 | 50 | +0 |  |
| 2024-10-28 | 50 | +0 |  |
| 2025-01-10 | 50 | +0 |  |
| 2025-01-29 | 51 | +1 | 1 |
| 2025-02-10 | 50 | +0 |  |
| 2025-03-28 | 50 | +0 |  |
| 2025-06-16 | 50 | +0 |  |
| 2025-09-30 | 50 | +0 |  |
| 2025-11-12 | 51 | +1 | 1 |
| 2025-11-17 | 50 | +0 |  |
| 2026-01-14 | 50 | +0 |  |
| 2026-09-30 | 50 | +0 |  |
| 2026-10-05 | 50 | +0 |  |

Residuals:

- 2021-10-22 `unreadable_section`  ind_prs23082021.pdf — ind_prs23082021.pdf: no extractable text — an image-only PDF; reading it needs OCR, which this platform does not have
- 2021-10-22 `entry_before_first_session` INE1TAE01010 coverage_start — walked back to 2021-10-22 but first seen 2025-11-12; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE379A01028 coverage_start — walked back to 2021-10-22 but first seen 2025-01-29; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE758E01017 coverage_start — walked back to 2021-10-22 but first seen 2023-08-21; clipped to its first appearance

## niftynext50

| Segment start | Reconstructed | Residual | Clipped stand-ins |
| --- | --- | --- | --- |
| 2021-10-22 | 50 | +0 |  |
| 2022-03-31 | 50 | +0 |  |
| 2022-04-20 | 50 | +0 |  |
| 2022-08-08 | 50 | +0 |  |
| 2022-09-30 | 50 | +0 |  |
| 2023-03-31 | 50 | +0 |  |
| 2023-06-15 | 50 | +0 |  |
| 2023-07-13 | 50 | +0 |  |
| 2023-09-28 | 50 | +0 |  |
| 2023-09-29 | 50 | +0 |  |
| 2024-03-28 | 50 | +0 |  |
| 2024-05-15 | 50 | +0 |  |
| 2024-09-12 | 50 | +0 |  |
| 2024-09-30 | 50 | +0 |  |
| 2025-03-28 | 50 | +0 |  |
| 2025-05-07 | 50 | +0 |  |
| 2025-06-19 | 51 | +1 | 1 |
| 2025-06-27 | 50 | +0 |  |
| 2025-09-22 | 50 | +0 |  |
| 2025-09-30 | 50 | +0 |  |
| 2026-03-30 | 50 | +0 |  |
| 2026-06-15 | 51 | +1 | 1 |
| 2026-06-24 | 50 | +0 |  |
| 2026-09-30 | 50 | +0 |  |
| 2026-10-05 | 50 | +0 |  |

Residuals:

- 2021-10-22 `unreadable_section`  ind_prs23082021.pdf — ind_prs23082021.pdf: no extractable text — an image-only PDF; reading it needs OCR, which this platform does not have
- 2021-10-22 `entry_before_first_session` INE704J01044 coverage_start — walked back to 2021-10-22 but first seen 2026-06-15; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE1NPP01017 coverage_start — walked back to 2021-10-22 but first seen 2025-06-19; clipped to its first appearance

## nifty100

| Segment start | Reconstructed | Residual | Clipped stand-ins |
| --- | --- | --- | --- |
| 2021-10-22 | 100 | +0 |  |
| 2022-03-31 | 100 | +0 |  |
| 2022-04-20 | 100 | +0 |  |
| 2022-07-29 | 100 | +0 |  |
| 2022-08-08 | 100 | +0 |  |
| 2022-09-14 | 100 | +0 |  |
| 2022-09-30 | 100 | +0 |  |
| 2023-03-31 | 100 | +0 |  |
| 2023-06-15 | 100 | +0 |  |
| 2023-07-13 | 100 | +0 |  |
| 2023-08-21 | 101 | +1 | 1 |
| 2023-09-07 | 100 | +0 |  |
| 2023-09-28 | 100 | +0 |  |
| 2023-09-29 | 100 | +0 |  |
| 2024-01-05 | 100 | +0 |  |
| 2024-03-28 | 100 | +0 |  |
| 2024-05-15 | 100 | +0 |  |
| 2024-09-12 | 100 | +0 |  |
| 2024-09-30 | 100 | +0 |  |
| 2024-10-28 | 100 | +0 |  |
| 2025-01-10 | 100 | +0 |  |
| 2025-01-29 | 101 | +1 | 1 |
| 2025-02-10 | 100 | +0 |  |
| 2025-03-28 | 100 | +0 |  |
| 2025-05-07 | 100 | +0 |  |
| 2025-06-16 | 100 | +0 |  |
| 2025-06-19 | 101 | +1 | 1 |
| 2025-06-27 | 100 | +0 |  |
| 2025-09-22 | 100 | +0 |  |
| 2025-09-30 | 100 | +0 |  |
| 2025-11-12 | 101 | +1 | 1 |
| 2025-11-17 | 100 | +0 |  |
| 2026-01-14 | 100 | +0 |  |
| 2026-03-30 | 100 | +0 |  |
| 2026-06-15 | 101 | +1 | 1 |
| 2026-06-24 | 100 | +0 |  |
| 2026-09-30 | 100 | +0 |  |
| 2026-10-05 | 100 | +0 |  |

Residuals:

- 2021-10-22 `unreadable_section`  ind_prs23082021.pdf — ind_prs23082021.pdf: no extractable text — an image-only PDF; reading it needs OCR, which this platform does not have
- 2023-09-29 `include_not_held_later` IN9155A01020 ind_prs17082023.pdf — TATAMTRDVR included but not in the later set
- 2021-10-22 `entry_before_first_session` INE704J01044 coverage_start — walked back to 2021-10-22 but first seen 2026-06-15; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE1TAE01010 coverage_start — walked back to 2021-10-22 but first seen 2025-11-12; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE1NPP01017 coverage_start — walked back to 2021-10-22 but first seen 2025-06-19; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE379A01028 coverage_start — walked back to 2021-10-22 but first seen 2025-01-29; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE758E01017 coverage_start — walked back to 2021-10-22 but first seen 2023-08-21; clipped to its first appearance

## nifty200

| Segment start | Reconstructed | Residual | Clipped stand-ins |
| --- | --- | --- | --- |
| 2021-10-22 | 200 | +0 |  |
| 2021-10-29 | 200 | +0 |  |
| 2022-01-11 | 200 | +0 |  |
| 2022-03-31 | 200 | +0 |  |
| 2022-04-20 | 200 | +0 |  |
| 2022-05-04 | 200 | +0 |  |
| 2022-07-29 | 200 | +0 |  |
| 2022-08-08 | 200 | +0 |  |
| 2022-09-14 | 200 | +0 |  |
| 2022-09-30 | 200 | +0 |  |
| 2023-03-31 | 200 | +0 |  |
| 2023-06-15 | 200 | +0 |  |
| 2023-07-13 | 200 | +0 |  |
| 2023-08-21 | 201 | +1 | 1 |
| 2023-09-07 | 200 | +0 |  |
| 2023-09-28 | 200 | +0 |  |
| 2023-09-29 | 200 | +0 |  |
| 2024-01-05 | 200 | +0 |  |
| 2024-03-28 | 200 | +0 |  |
| 2024-05-15 | 200 | +0 |  |
| 2024-05-27 | 200 | +0 |  |
| 2024-09-12 | 200 | +0 |  |
| 2024-09-30 | 200 | +0 |  |
| 2024-10-28 | 200 | +0 |  |
| 2024-12-27 | 200 | +0 |  |
| 2025-01-10 | 200 | +0 |  |
| 2025-01-29 | 201 | +1 | 1 |
| 2025-02-10 | 200 | +0 |  |
| 2025-03-28 | 200 | +0 |  |
| 2025-05-07 | 200 | +0 |  |
| 2025-06-04 | 200 | +0 |  |
| 2025-06-16 | 200 | +0 |  |
| 2025-06-19 | 201 | +1 | 1 |
| 2025-06-27 | 200 | +0 |  |
| 2025-09-22 | 200 | +0 |  |
| 2025-09-30 | 200 | +0 |  |
| 2025-11-12 | 201 | +1 | 1 |
| 2025-11-17 | 200 | +0 |  |
| 2026-01-14 | 200 | +0 |  |
| 2026-03-30 | 200 | +0 |  |
| 2026-06-15 | 201 | +1 | 1 |
| 2026-06-24 | 200 | +0 |  |
| 2026-09-30 | 200 | +0 |  |
| 2026-10-05 | 200 | +0 |  |

Residuals:

- 2021-10-22 `unreadable_section`  ind_prs23082021.pdf — ind_prs23082021.pdf: no extractable text — an image-only PDF; reading it needs OCR, which this platform does not have
- 2023-09-29 `include_not_held_later` IN9155A01020 ind_prs17082023.pdf — TATAMTRDVR included but not in the later set
- 2021-10-22 `entry_before_first_session` INE704J01044 coverage_start — walked back to 2021-10-22 but first seen 2026-06-15; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE1TAE01010 coverage_start — walked back to 2021-10-22 but first seen 2025-11-12; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE1NPP01017 coverage_start — walked back to 2021-10-22 but first seen 2025-06-19; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE379A01028 coverage_start — walked back to 2021-10-22 but first seen 2025-01-29; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE758E01017 coverage_start — walked back to 2021-10-22 but first seen 2023-08-21; clipped to its first appearance

## nifty500

| Segment start | Reconstructed | Residual | Clipped stand-ins |
| --- | --- | --- | --- |
| 2021-10-22 | 500 | +0 |  |
| 2021-10-29 | 500 | +0 |  |
| 2021-12-15 | 500 | +0 |  |
| 2022-01-11 | 500 | +0 |  |
| 2022-02-09 | 500 | +0 |  |
| 2022-03-25 | 500 | +0 |  |
| 2022-03-31 | 500 | +0 |  |
| 2022-04-07 | 500 | +0 |  |
| 2022-04-12 | 500 | +0 |  |
| 2022-04-20 | 500 | +0 |  |
| 2022-04-27 | 500 | +0 |  |
| 2022-05-04 | 500 | +0 |  |
| 2022-05-31 | 500 | +0 |  |
| 2022-07-29 | 500 | +0 |  |
| 2022-08-08 | 500 | +0 |  |
| 2022-09-14 | 500 | +0 |  |
| 2022-09-30 | 500 | +0 |  |
| 2022-10-19 | 500 | +0 |  |
| 2022-11-22 | 500 | +0 |  |
| 2022-12-30 | 500 | +0 |  |
| 2023-02-17 | 500 | +0 |  |
| 2023-02-22 | 500 | +0 |  |
| 2023-03-02 | 500 | +0 |  |
| 2023-03-31 | 500 | +0 |  |
| 2023-04-28 | 500 | +0 |  |
| 2023-06-15 | 500 | +0 |  |
| 2023-07-13 | 500 | +0 |  |
| 2023-08-21 | 501 | +1 | 1 |
| 2023-09-07 | 500 | +0 |  |
| 2023-09-18 | 500 | +0 |  |
| 2023-09-28 | 500 | +0 |  |
| 2023-09-29 | 500 | +0 |  |
| 2023-10-26 | 500 | +0 |  |
| 2024-01-05 | 500 | +0 |  |
| 2024-01-10 | 500 | +0 |  |
| 2024-03-05 | 500 | +0 |  |
| 2024-03-28 | 500 | +0 |  |
| 2024-05-15 | 500 | +0 |  |
| 2024-05-27 | 500 | +0 |  |
| 2024-07-19 | 500 | +0 |  |
| 2024-07-25 | 500 | +0 |  |
| 2024-09-05 | 500 | +0 |  |
| 2024-09-12 | 500 | +0 |  |
| 2024-09-13 | 500 | +0 |  |
| 2024-09-30 | 500 | +0 |  |
| 2024-10-04 | 500 | +0 |  |
| 2024-10-09 | 500 | +0 |  |
| 2024-10-10 | 500 | +0 |  |
| 2024-10-16 | 500 | +0 |  |
| 2024-10-18 | 500 | +0 |  |
| 2024-10-28 | 500 | +0 |  |
| 2024-12-27 | 500 | +0 |  |
| 2025-01-10 | 500 | +0 |  |
| 2025-01-29 | 501 | +1 | 1 |
| 2025-01-31 | 501 | +1 | 1 |
| 2025-02-10 | 500 | +0 |  |
| 2025-03-21 | 500 | +0 |  |
| 2025-03-28 | 500 | +0 |  |
| 2025-04-11 | 500 | +0 |  |
| 2025-05-07 | 500 | +0 |  |
| 2025-06-04 | 500 | +0 |  |
| 2025-06-16 | 500 | +0 |  |
| 2025-06-19 | 501 | +1 | 1 |
| 2025-06-27 | 500 | +0 |  |
| 2025-07-01 | 501 | +1 | 1 |
| 2025-07-10 | 500 | +0 |  |
| 2025-09-22 | 500 | +0 |  |
| 2025-09-23 | 500 | +0 |  |
| 2025-09-30 | 500 | +0 |  |
| 2025-10-14 | 500 | +0 |  |
| 2025-11-03 | 500 | +0 |  |
| 2025-11-12 | 501 | +1 | 1 |
| 2025-11-13 | 502 | +2 | 2 |
| 2025-11-17 | 501 | +1 | 1 |
| 2025-11-28 | 500 | +0 |  |
| 2025-12-05 | 500 | +0 |  |
| 2025-12-26 | 500 | +0 |  |
| 2025-12-31 | 500 | +0 |  |
| 2026-01-02 | 500 | +0 |  |
| 2026-01-14 | 500 | +0 |  |
| 2026-02-26 | 500 | +0 |  |
| 2026-03-30 | 500 | +0 |  |
| 2026-05-12 | 500 | +0 |  |
| 2026-06-15 | 501 | +1 | 1 |
| 2026-06-24 | 500 | +0 |  |
| 2026-07-17 | 500 | +0 |  |
| 2026-09-30 | 500 | +0 |  |
| 2026-10-05 | 500 | +0 |  |

Residuals:

- 2021-10-22 `unreadable_section`  ind_prs23082021.pdf — ind_prs23082021.pdf: no extractable text — an image-only PDF; reading it needs OCR, which this platform does not have
- 2021-10-22 `entry_before_first_session` INE704J01044 coverage_start — walked back to 2021-10-22 but first seen 2026-06-15; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE28GN01010 coverage_start — walked back to 2021-10-22 but first seen 2025-11-13; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE1TAE01010 coverage_start — walked back to 2021-10-22 but first seen 2025-11-12; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE1SY401010 coverage_start — walked back to 2021-10-22 but first seen 2025-07-01; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE1NPP01017 coverage_start — walked back to 2021-10-22 but first seen 2025-06-19; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE379A01028 coverage_start — walked back to 2021-10-22 but first seen 2025-01-29; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE758E01017 coverage_start — walked back to 2021-10-22 but first seen 2023-08-21; clipped to its first appearance

## niftymidcap150

| Segment start | Reconstructed | Residual | Clipped stand-ins |
| --- | --- | --- | --- |
| 2021-10-22 | 150 | +0 |  |
| 2021-10-29 | 150 | +0 |  |
| 2021-12-15 | 150 | +0 |  |
| 2022-01-11 | 150 | +0 |  |
| 2022-02-09 | 150 | +0 |  |
| 2022-03-31 | 150 | +0 |  |
| 2022-05-04 | 150 | +0 |  |
| 2022-08-08 | 150 | +0 |  |
| 2022-09-30 | 150 | +0 |  |
| 2023-03-31 | 150 | +0 |  |
| 2023-07-13 | 150 | +0 |  |
| 2023-09-29 | 150 | +0 |  |
| 2024-03-28 | 150 | +0 |  |
| 2024-05-27 | 150 | +0 |  |
| 2024-09-30 | 150 | +0 |  |
| 2024-12-27 | 150 | +0 |  |
| 2025-03-28 | 150 | +0 |  |
| 2025-06-04 | 150 | +0 |  |
| 2025-06-19 | 151 | +1 | 1 |
| 2025-06-27 | 150 | +0 |  |
| 2025-09-30 | 150 | +0 |  |
| 2025-10-14 | 150 | +0 |  |
| 2026-03-30 | 150 | +0 |  |
| 2026-09-30 | 150 | +0 |  |
| 2026-10-05 | 150 | +0 |  |

Residuals:

- 2021-10-22 `unreadable_section`  ind_prs23082021.pdf — ind_prs23082021.pdf: no extractable text — an image-only PDF; reading it needs OCR, which this platform does not have
- 2021-10-22 `entry_before_first_session` INE1NPP01017 coverage_start — walked back to 2021-10-22 but first seen 2025-06-19; clipped to its first appearance

## niftysmallcap250

| Segment start | Reconstructed | Residual | Clipped stand-ins |
| --- | --- | --- | --- |
| 2021-10-22 | 250 | +0 |  |
| 2022-03-25 | 250 | +0 |  |
| 2022-03-31 | 250 | +0 |  |
| 2022-04-07 | 250 | +0 |  |
| 2022-04-12 | 250 | +0 |  |
| 2022-04-27 | 250 | +0 |  |
| 2022-05-31 | 250 | +0 |  |
| 2022-08-08 | 250 | +0 |  |
| 2022-09-30 | 250 | +0 |  |
| 2022-10-19 | 250 | +0 |  |
| 2022-11-22 | 250 | +0 |  |
| 2022-12-30 | 250 | +0 |  |
| 2023-02-17 | 250 | +0 |  |
| 2023-02-22 | 250 | +0 |  |
| 2023-03-02 | 250 | +0 |  |
| 2023-03-31 | 250 | +0 |  |
| 2023-04-28 | 250 | +0 |  |
| 2023-09-18 | 250 | +0 |  |
| 2023-09-29 | 250 | +0 |  |
| 2023-10-26 | 250 | +0 |  |
| 2024-01-10 | 250 | +0 |  |
| 2024-03-05 | 250 | +0 |  |
| 2024-03-28 | 250 | +0 |  |
| 2024-07-19 | 250 | +0 |  |
| 2024-07-25 | 250 | +0 |  |
| 2024-09-05 | 250 | +0 |  |
| 2024-09-13 | 250 | +0 |  |
| 2024-09-30 | 250 | +0 |  |
| 2024-10-04 | 250 | +0 |  |
| 2024-10-09 | 250 | +0 |  |
| 2024-10-10 | 250 | +0 |  |
| 2024-10-16 | 250 | +0 |  |
| 2024-10-18 | 250 | +0 |  |
| 2025-01-31 | 250 | +0 |  |
| 2025-03-21 | 250 | +0 |  |
| 2025-03-28 | 250 | +0 |  |
| 2025-04-11 | 250 | +0 |  |
| 2025-07-01 | 251 | +1 | 1 |
| 2025-07-10 | 250 | +0 |  |
| 2025-09-23 | 250 | +0 |  |
| 2025-09-30 | 250 | +0 |  |
| 2025-11-03 | 250 | +0 |  |
| 2025-11-13 | 251 | +1 | 1 |
| 2025-11-28 | 250 | +0 |  |
| 2025-12-05 | 250 | +0 |  |
| 2025-12-26 | 250 | +0 |  |
| 2025-12-31 | 250 | +0 |  |
| 2026-01-02 | 250 | +0 |  |
| 2026-02-26 | 250 | +0 |  |
| 2026-03-30 | 250 | +0 |  |
| 2026-05-12 | 250 | +0 |  |
| 2026-07-17 | 250 | +0 |  |
| 2026-09-30 | 250 | +0 |  |
| 2026-10-05 | 250 | +0 |  |

Residuals:

- 2021-10-22 `unreadable_section`  ind_prs23082021.pdf — ind_prs23082021.pdf: no extractable text — an image-only PDF; reading it needs OCR, which this platform does not have
- 2021-10-22 `entry_before_first_session` INE28GN01010 coverage_start — walked back to 2021-10-22 but first seen 2025-11-13; clipped to its first appearance
- 2021-10-22 `entry_before_first_session` INE1SY401010 coverage_start — walked back to 2021-10-22 but first seen 2025-07-01; clipped to its first appearance

## Unreadable releases / sections

- ind_prs21012019.pdf: NIFTY 500: no effective date stated for the section
- ind_prs21012019.pdf: NIFTY Smallcap 250: no effective date stated for the section
- ind_prs01082018.pdf: NIFTY 500 Index: no effective date stated for the section
- ind_prs01082018.pdf: NIFTY Smallcap 250 Index: no effective date stated for the section
- ind_prs23082021.pdf: ind_prs23082021.pdf: no extractable text — an image-only PDF; reading it needs OCR, which this platform does not have
