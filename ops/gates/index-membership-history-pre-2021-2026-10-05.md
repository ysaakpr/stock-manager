# DQ-5.1 — index membership history before 2021-10-22 (2026-10-05)

**Result.** The point-in-time membership history now starts in 2016 for all seven tracked
indices, up from 2021-10-22 (PR #37):

| Index | PR #37 start | New start | Bounded by |
| --- | --- | --- | --- |
| NIFTY 50 | 2021-10-22 | **2016-04-01** | `ind_prs22022016_2.pdf` — detached layout (tables apart from their statements) |
| NIFTY Next 50 | 2021-10-22 | **2016-10-24** | `ind_prs17102016.pdf` — detached layout |
| NIFTY 100 | 2021-10-22 | **2016-10-24** | `ind_prs17102016.pdf` — detached layout |
| NIFTY 200 | 2021-10-22 | **2016-10-24** | `ind_prs17102016.pdf` — detached layout |
| NIFTY 500 | 2021-10-22 | **2016-10-24** | `ind_prs17102016.pdf` — detached layout |
| NIFTY Midcap 150 | 2021-10-22 | **2016-10-24** | `ind_prs17102016.pdf` — detached layout |
| NIFTY Smallcap 250 | 2021-10-22 | **2016-09-30** | `ind_prs12082016.pdf` — detached layout |

Before each start the history answers `None` (an empty universe), never the oldest set or
today's list. Every segment since each start has the index's fixed size once explained windows
are counted out; **zero unexplained off-size segments**. The extended build is **identical,
day by day, to PR #37's stored build** over its whole span (12,670 index-days compared,
0 differences), and every stored daily constituents snapshot still equals the reconstruction
(0 `snapshot_mismatch`).

## 1. The image-only September 2021 release, transcribed

`ind_prs23082021.pdf` (29 pages; L0 `nifty_index_press_releases/2021/08/ind_prs23082021.pdf`,
sha256 `c9b9feef…1231e`) draws each word as a 1-bit image mask — there is no text layer, and no
OCR on the box. The pages were rasterised from the PDF's own mask images by a throwaway
pure-Python renderer (no dependency added, nothing committed) and **read visually by the agent,
page by page**. The result is `dataplatform/ingest/data/index_release_transcriptions.yaml`:
every tracked-index row as printed (Sr. No., company, symbol — the release's own spellings kept),
pinned to the L0 object by key and sha256, with page numbers, the effective-date quote (p.1),
the "No changes are being made in NIFTY 50 …" quote (p.27), the date transcribed (2026-10-05)
and `transcribed_by: agent (Claude Opus 5.5 …)`. L0 was not modified.
`index_transcription` validates it (Sr. No. sequence, symbols, balance, pages) and the builder
applies it only when the PDF has no text layer **and** the L0 object's sha256 matches.

**Cross-verification** (`tests/unit/test_index_transcription.py`):

- Each complete tracked section balances: Next 50 5/5, NIFTY 100 5/5, NIFTY 200 6/6,
  Midcap 150 20/20, Smallcap 250 32/32; NIFTY 50 stated unchanged.
- NIFTY 100's table equals Next 50's (NIFTY 50 unchanged); NIFTY 200's equals the net of
  NIFTY 100's and the transcribed NIFTY Midcap 100 table (NIFTY 200 = 100 + Midcap 100).
- Against the exchange's **text** restatement three weeks later (`ind_prs15092021.pdf`, a
  frozen fixture): all 104 Midcap 150 / Smallcap 250 rows agree except exactly what it
  announces — the REIT inclusions revoked (EMBASSY, MINDSPACE, BIRET), so GILLETTE and
  MOTILALOFS stay in Midcap 150 and HIKAL and HGS enter Smallcap 250 instead.
- Every transcribed symbol resolved to an ISIN at 2021-09-30 (32/32 applied events; 144/144
  for the restatement). The walk's own before/after check passes: no transcribed inclusion is
  absent from, and no exclusion present in, the later (text-release-derived) set.

**Flagged, not guessed — NIFTY 500.** The scan prints NIFTY 500 exclusion rows 1-19 at the foot
of page 4 and page 5 opens on "3) NIFTY 100": rows 20+ and the whole inclusion table are not in
the PDF (every image the pages draw was rendered; nothing lies outside the page boxes). The
section is recorded as printed, flagged `truncated_in_source`, and yields no event. The exchange
replaced that list wholesale on 2021-09-15 ("The earlier list of replacement of these indices
published through a press release on August 23, 2021 stands replaced with the list given
hereunder" — NIFTY 500, Midcap 150, Smallcap 250); that sentence is a quoted erratum in
`_VOIDINGS`, so NIFTY 500's 2021-09-30 change is read from the text release. The 19 printed rows
are the restatement's first 18 plus GILLETTE (which the restatement kept). No other entry was
ambiguous.

## 2. Backwards through 2016-2021

Same walk and invariants as PR #37 (ISIN-only identity resolved at the event date, reissue
renames, PIT rule effective-on-D *and* announced-by-D, gap before `coverage_start`). Changes
needed to cross the older releases, each a narrow fix with a frozen-fixture test:

- `ind_prs21012019.pdf` prints "January 2 8, 2019": a day split by the text layer before its
  comma is read as one number (only before a comma).
- `ind_prs01082018.pdf` states its one effective date *after* its tables: undated rows take the
  release's single stated effective date; with zero or several, still unparsed.
- `ind_prs16022017.pdf` / `ind_prs27042017.pdf`: a page number on its own line mid-table is no
  longer a nameless row (a bare number that is not the next serial, or that nothing follows).
- The 2020 "null and void" erratum (`ind_prs13052020.pdf`) now voids whole releases. Matched on
  (release, effective date), `ind_prs19032020.pdf`'s Midcap 150 rows — dated 2020-03-19 by a
  narrative sentence — slipped through and put Yes Bank in Midcap 150 beside its NIFTY 50 seat
  for 2017-2020.
- An AES-encrypted 2005 release fails as a `ParseError` instead of crashing the build.

**Explained windows.** Besides PR #37's demerger stand-ins (clipped to first appearance), one
older one: Tata Motors' 'A' Ordinary (DVR) shares sat beside the ordinary shares in NIFTY 50
until 2017-09-29 (`ind_prs28082017`) and in NIFTY 100/200 until 2020-06-26 (`ind_prs10062020`)
— 51/101/201 securities. The report counts a second share class of an issuer already in the
index (an `IN9` ISIN whose issuer code matches a member's) as explained. Composition identities
hold on every segment since 2016-10-24: NIFTY 100 = NIFTY 50 ∪ Next 50 and NIFTY 500 = NIFTY 100
∪ Midcap 150 ∪ Smallcap 250, the DVR line being the only exception (it was never in Next 50 or
NIFTY 500, per the releases).

**Residuals carried over, unchanged:** TATAMTRDVR's 2023-09-29 inclusion in NIFTY 100/200 whose
exit came by a corporate-action release (`include_not_held_later`), and the spin-off
`entry_before_first_session` clips. 0 events quarantined inside any index's coverage; the 225
quarantined events listed in the report are all older than coverage (pre-2015 releases print no
symbols) and bound nothing.

## 3. Fetch — the pre-2018 releases (owner-approved)

One ranged run of the existing fetcher, `--fetch-only`, under the `niftyindices.com` host lease,
2026-10-05 16:19:04Z → 16:32:12Z (21:49 → 22:02 IST, outside the 18:00-19:30 snapshot window;
the daily-snapshot timer last ran 13:45Z and next runs 2026-10-06 13:45Z; no other driver was
on the host). Log: `~/campaign/index-press-pre2018-2026-10-05.log`.

    DATA_ROOT=/home/ubuntu/stock-manager/data uv run python -m \
        dataplatform.ingest.index_history_backfill --as-of 2026-10-05 --fetch-only --max-releases 260

**248 requests, 248 fetched (15.3 MB), 0 failed, 0 refusals/403s**, 1.1-2.8 s spacing waits;
listing and anchors reused from L0 (0 requests). L0 now holds all 417 candidate releases,
contiguous 1998-09-02 → 2026-10-01. One object, `ind_prs02032012.pdf`, came back HTTP 200
`text/html` (a soft-404 for that one release, not a block — the 187 after it fetched normally); it is
kept in L0 as served and refused by the parser ("not a PDF"). It and the encrypted
`ind_prs20062005_1.pdf` are both below every index's coverage.

## 4. Build (verification only — the lake was not written)

    DATA_ROOT=/home/ubuntu/stock-manager/data uv run python -m \
        dataplatform.ingest.index_history_backfill --build-only --as-of 2026-10-05 \
        --report /tmp/index-history-pre2021/report.md --write-l1 /tmp/index-history-pre2021

Reads L0 and L1 identity from the lake, writes `index_membership_history` (7 files) and
`index_change_events` under `/tmp/index-history-pre2021/L1/…/date=2026-10-05/` (~56 s).
Publishing into the lake is a separate step: `date=2026-10-05` there already holds PR #37's
build and is write-once (`ImmutableHistoryError`), so the extended history lands as the next
dated build (the next listing + anchor capture, e.g. the weekly `index_press_refresh`).

## 5. Tests

- `tests/unit/test_index_transcription.py` — provenance, sha pinning, balance, the
  NIFTY 100/200 identities, the 104-row cross-check against the text restatement, the truncated
  section yields nothing, malformed files refused; **PIT boundary**: the Next 50 switch happens on
  2021-09-30 (a one-day shift fails) and the new set is invisible between the 2021-08-23
  announcement and the eve (announcement leakage fails), with `knowable_from` = announcement.
- `tests/unit/test_index_changes.py` — the split-day date, the trailing effective date.
- `tests/unit/test_index_history.py` — whole-release voiding; the 2021-09-15 replacement.
- `tests/golden/test_index_history_golden.py` (lake-backed, skipped without it) — coverage per
  index and `None` the day before; known switches on the eve / day / after announcement:
  NIFTY 50 2017-03-31 (BHEL, IDEA → IBULHSGFIN, IOC), Yes Bank → Shree Cement 2020-03-19 and
  never in Midcap 150, the DVR leaving NIFTY 100 2020-06-26, Next 50 2021-09-30, NIFTY 500 per
  the restatement (GILLETTE kept, HIKAL/HGS/LODHA in, no REIT before 2026-09-30); sizes and the
  composition identities on every segment; every release-opened interval knowable from its
  release's announcement.

---

# Index membership history — build 2026-10-05

Listing: `nifty_index_press_releases/2026-10-05/press_release_listing_20261005.html`. Candidate releases: 417; in L0: 417; not fetched: 0.

A segment is the span between two consecutive change dates; its count is the members effective on its first day. *Explained* off-size segments are those where the excess is exactly the members clipped to their first appearance (a demerger spin-off's stand-in window, which the index really carried) plus any second share class of an issuer already in the index (Tata Motors DVR beside the ordinary shares, to 2020-06-26).

| Index | Expected | Coverage start | Anchor | Events applied | Segments | Off-size (unexplained) | Max abs unexplained residual | Other residuals |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| nifty50 | 50 | 2016-04-01 | 2026-10-05 | 60 | 39 | 8 (0) | 0 | entry_before_first_session=3, unreadable_section=1 |
| niftynext50 | 50 | 2016-10-24 | 2026-10-05 | 240 | 44 | 2 (0) | 0 | entry_before_first_session=2, unreadable_section=1 |
| nifty100 | 100 | 2016-10-24 | 2026-10-05 | 213 | 61 | 22 (0) | 0 | entry_before_first_session=5, include_not_held_later=1, unreadable_section=1 |
| nifty200 | 200 | 2016-10-24 | 2026-10-05 | 409 | 79 | 33 (0) | 0 | entry_before_first_session=5, include_not_held_later=1, unreadable_section=1 |
| nifty500 | 500 | 2016-10-24 | 2026-10-05 | 1153 | 161 | 9 (0) | 0 | entry_before_first_session=7, unreadable_section=1 |
| niftymidcap150 | 150 | 2016-10-24 | 2026-10-05 | 617 | 56 | 1 (0) | 0 | entry_before_first_session=1, unreadable_section=1 |
| niftysmallcap250 | 250 | 2016-09-30 | 2026-10-05 | 1314 | 108 | 2 (0) | 0 | entry_before_first_session=2, unreadable_section=1 |

## nifty50

| Segment start | Reconstructed | Residual | Clipped stand-ins | Second share class |
| --- | --- | --- | --- | --- |
| 2016-04-01 | 51 | +1 |  | 1 |
| 2016-10-07 | 51 | +1 |  | 1 |
| 2017-03-31 | 51 | +1 |  | 1 |
| 2017-05-26 | 51 | +1 |  | 1 |
| 2017-09-22 | 51 | +1 |  | 1 |
| 2017-09-29 | 50 | +0 |  |  |
| 2018-04-02 | 50 | +0 |  |  |
| 2018-09-28 | 50 | +0 |  |  |
| 2019-03-29 | 50 | +0 |  |  |
| 2019-09-20 | 50 | +0 |  |  |
| 2019-09-27 | 50 | +0 |  |  |
| 2020-03-16 | 50 | +0 |  |  |
| 2020-03-19 | 50 | +0 |  |  |
| 2020-07-31 | 50 | +0 |  |  |
| 2020-08-25 | 50 | +0 |  |  |
| 2020-09-25 | 50 | +0 |  |  |
| 2021-03-31 | 50 | +0 |  |  |
| 2022-03-31 | 50 | +0 |  |  |
| 2022-07-29 | 50 | +0 |  |  |
| 2022-09-14 | 50 | +0 |  |  |
| 2022-09-30 | 50 | +0 |  |  |
| 2023-07-13 | 50 | +0 |  |  |
| 2023-08-21 | 51 | +1 | 1 |  |
| 2023-09-07 | 50 | +0 |  |  |
| 2024-01-05 | 50 | +0 |  |  |
| 2024-03-28 | 50 | +0 |  |  |
| 2024-09-30 | 50 | +0 |  |  |
| 2024-10-28 | 50 | +0 |  |  |
| 2025-01-10 | 50 | +0 |  |  |
| 2025-01-29 | 51 | +1 | 1 |  |
| 2025-02-10 | 50 | +0 |  |  |
| 2025-03-28 | 50 | +0 |  |  |
| 2025-06-16 | 50 | +0 |  |  |
| 2025-09-30 | 50 | +0 |  |  |
| 2025-11-12 | 51 | +1 | 1 |  |
| 2025-11-17 | 50 | +0 |  |  |
| 2026-01-14 | 50 | +0 |  |  |
| 2026-09-30 | 50 | +0 |  |  |
| 2026-10-05 | 50 | +0 |  |  |

Residuals:

- 2016-04-01 `unreadable_section`  ind_prs22022016_2.pdf — Nifty 50 Index: the 'exclude' statement has no table of its own (detached layout)
- 2016-04-01 `entry_before_first_session` INE1TAE01010 coverage_start — walked back to 2016-04-01 but first seen 2025-11-12; clipped to its first appearance
- 2016-04-01 `entry_before_first_session` INE379A01028 coverage_start — walked back to 2016-04-01 but first seen 2025-01-29; clipped to its first appearance
- 2016-04-01 `entry_before_first_session` INE758E01017 coverage_start — walked back to 2016-04-01 but first seen 2023-08-21; clipped to its first appearance

## niftynext50

| Segment start | Reconstructed | Residual | Clipped stand-ins | Second share class |
| --- | --- | --- | --- | --- |
| 2016-10-24 | 50 | +0 |  |  |
| 2017-01-05 | 50 | +0 |  |  |
| 2017-03-17 | 50 | +0 |  |  |
| 2017-03-31 | 50 | +0 |  |  |
| 2017-05-26 | 50 | +0 |  |  |
| 2017-09-29 | 50 | +0 |  |  |
| 2018-04-02 | 50 | +0 |  |  |
| 2018-06-18 | 50 | +0 |  |  |
| 2018-06-27 | 50 | +0 |  |  |
| 2018-09-28 | 50 | +0 |  |  |
| 2018-11-30 | 50 | +0 |  |  |
| 2019-03-29 | 50 | +0 |  |  |
| 2019-09-27 | 50 | +0 |  |  |
| 2020-03-19 | 50 | +0 |  |  |
| 2020-06-26 | 50 | +0 |  |  |
| 2020-07-31 | 50 | +0 |  |  |
| 2020-09-25 | 50 | +0 |  |  |
| 2021-03-31 | 50 | +0 |  |  |
| 2021-06-30 | 50 | +0 |  |  |
| 2021-09-30 | 50 | +0 |  |  |
| 2022-03-31 | 50 | +0 |  |  |
| 2022-04-20 | 50 | +0 |  |  |
| 2022-08-08 | 50 | +0 |  |  |
| 2022-09-30 | 50 | +0 |  |  |
| 2023-03-31 | 50 | +0 |  |  |
| 2023-06-15 | 50 | +0 |  |  |
| 2023-07-13 | 50 | +0 |  |  |
| 2023-09-28 | 50 | +0 |  |  |
| 2023-09-29 | 50 | +0 |  |  |
| 2024-03-28 | 50 | +0 |  |  |
| 2024-05-15 | 50 | +0 |  |  |
| 2024-09-12 | 50 | +0 |  |  |
| 2024-09-30 | 50 | +0 |  |  |
| 2025-03-28 | 50 | +0 |  |  |
| 2025-05-07 | 50 | +0 |  |  |
| 2025-06-19 | 51 | +1 | 1 |  |
| 2025-06-27 | 50 | +0 |  |  |
| 2025-09-22 | 50 | +0 |  |  |
| 2025-09-30 | 50 | +0 |  |  |
| 2026-03-30 | 50 | +0 |  |  |
| 2026-06-15 | 51 | +1 | 1 |  |
| 2026-06-24 | 50 | +0 |  |  |
| 2026-09-30 | 50 | +0 |  |  |
| 2026-10-05 | 50 | +0 |  |  |

Residuals:

- 2016-10-24 `unreadable_section`  ind_prs17102016.pdf — Nifty Next 50 Index: the 'exclude' statement has no table of its own (detached layout)
- 2016-10-24 `entry_before_first_session` INE704J01044 coverage_start — walked back to 2016-10-24 but first seen 2026-06-15; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE1NPP01017 coverage_start — walked back to 2016-10-24 but first seen 2025-06-19; clipped to its first appearance

## nifty100

| Segment start | Reconstructed | Residual | Clipped stand-ins | Second share class |
| --- | --- | --- | --- | --- |
| 2016-10-24 | 101 | +1 |  | 1 |
| 2017-01-05 | 101 | +1 |  | 1 |
| 2017-03-17 | 101 | +1 |  | 1 |
| 2017-03-31 | 101 | +1 |  | 1 |
| 2017-05-26 | 101 | +1 |  | 1 |
| 2017-09-22 | 101 | +1 |  | 1 |
| 2017-09-29 | 101 | +1 |  | 1 |
| 2018-04-02 | 101 | +1 |  | 1 |
| 2018-06-18 | 101 | +1 |  | 1 |
| 2018-06-27 | 101 | +1 |  | 1 |
| 2018-09-28 | 101 | +1 |  | 1 |
| 2018-11-30 | 101 | +1 |  | 1 |
| 2019-03-29 | 101 | +1 |  | 1 |
| 2019-09-20 | 101 | +1 |  | 1 |
| 2019-09-27 | 101 | +1 |  | 1 |
| 2020-03-16 | 101 | +1 |  | 1 |
| 2020-03-19 | 101 | +1 |  | 1 |
| 2020-06-26 | 100 | +0 |  |  |
| 2020-07-31 | 100 | +0 |  |  |
| 2020-08-25 | 100 | +0 |  |  |
| 2020-09-25 | 100 | +0 |  |  |
| 2021-03-31 | 100 | +0 |  |  |
| 2021-06-30 | 100 | +0 |  |  |
| 2021-09-30 | 100 | +0 |  |  |
| 2022-03-31 | 100 | +0 |  |  |
| 2022-04-20 | 100 | +0 |  |  |
| 2022-07-29 | 100 | +0 |  |  |
| 2022-08-08 | 100 | +0 |  |  |
| 2022-09-14 | 100 | +0 |  |  |
| 2022-09-30 | 100 | +0 |  |  |
| 2023-03-31 | 100 | +0 |  |  |
| 2023-06-15 | 100 | +0 |  |  |
| 2023-07-13 | 100 | +0 |  |  |
| 2023-08-21 | 101 | +1 | 1 |  |
| 2023-09-07 | 100 | +0 |  |  |
| 2023-09-28 | 100 | +0 |  |  |
| 2023-09-29 | 100 | +0 |  |  |
| 2024-01-05 | 100 | +0 |  |  |
| 2024-03-28 | 100 | +0 |  |  |
| 2024-05-15 | 100 | +0 |  |  |
| 2024-09-12 | 100 | +0 |  |  |
| 2024-09-30 | 100 | +0 |  |  |
| 2024-10-28 | 100 | +0 |  |  |
| 2025-01-10 | 100 | +0 |  |  |
| 2025-01-29 | 101 | +1 | 1 |  |
| 2025-02-10 | 100 | +0 |  |  |
| 2025-03-28 | 100 | +0 |  |  |
| 2025-05-07 | 100 | +0 |  |  |
| 2025-06-16 | 100 | +0 |  |  |
| 2025-06-19 | 101 | +1 | 1 |  |
| 2025-06-27 | 100 | +0 |  |  |
| 2025-09-22 | 100 | +0 |  |  |
| 2025-09-30 | 100 | +0 |  |  |
| 2025-11-12 | 101 | +1 | 1 |  |
| 2025-11-17 | 100 | +0 |  |  |
| 2026-01-14 | 100 | +0 |  |  |
| 2026-03-30 | 100 | +0 |  |  |
| 2026-06-15 | 101 | +1 | 1 |  |
| 2026-06-24 | 100 | +0 |  |  |
| 2026-09-30 | 100 | +0 |  |  |
| 2026-10-05 | 100 | +0 |  |  |

Residuals:

- 2016-10-24 `unreadable_section`  ind_prs17102016.pdf — Nifty 100 Index: the 'exclude' statement has no table of its own (detached layout)
- 2023-09-29 `include_not_held_later` IN9155A01020 ind_prs17082023.pdf — TATAMTRDVR included but not in the later set
- 2016-10-24 `entry_before_first_session` INE704J01044 coverage_start — walked back to 2016-10-24 but first seen 2026-06-15; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE1TAE01010 coverage_start — walked back to 2016-10-24 but first seen 2025-11-12; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE1NPP01017 coverage_start — walked back to 2016-10-24 but first seen 2025-06-19; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE379A01028 coverage_start — walked back to 2016-10-24 but first seen 2025-01-29; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE758E01017 coverage_start — walked back to 2016-10-24 but first seen 2023-08-21; clipped to its first appearance

## nifty200

| Segment start | Reconstructed | Residual | Clipped stand-ins | Second share class |
| --- | --- | --- | --- | --- |
| 2016-10-24 | 201 | +1 |  | 1 |
| 2016-11-18 | 201 | +1 |  | 1 |
| 2017-01-05 | 201 | +1 |  | 1 |
| 2017-01-23 | 201 | +1 |  | 1 |
| 2017-03-17 | 201 | +1 |  | 1 |
| 2017-03-31 | 201 | +1 |  | 1 |
| 2017-05-26 | 201 | +1 |  | 1 |
| 2017-09-05 | 201 | +1 |  | 1 |
| 2017-09-22 | 201 | +1 |  | 1 |
| 2017-09-29 | 201 | +1 |  | 1 |
| 2017-10-30 | 201 | +1 |  | 1 |
| 2017-11-10 | 201 | +1 |  | 1 |
| 2018-02-05 | 201 | +1 |  | 1 |
| 2018-04-02 | 201 | +1 |  | 1 |
| 2018-04-26 | 201 | +1 |  | 1 |
| 2018-06-18 | 201 | +1 |  | 1 |
| 2018-06-27 | 201 | +1 |  | 1 |
| 2018-06-29 | 201 | +1 |  | 1 |
| 2018-09-28 | 201 | +1 |  | 1 |
| 2018-11-30 | 201 | +1 |  | 1 |
| 2018-12-28 | 201 | +1 |  | 1 |
| 2019-03-29 | 201 | +1 |  | 1 |
| 2019-06-17 | 201 | +1 |  | 1 |
| 2019-09-20 | 201 | +1 |  | 1 |
| 2019-09-27 | 201 | +1 |  | 1 |
| 2019-12-27 | 201 | +1 |  | 1 |
| 2020-03-16 | 201 | +1 |  | 1 |
| 2020-03-19 | 201 | +1 |  | 1 |
| 2020-06-26 | 200 | +0 |  |  |
| 2020-07-31 | 200 | +0 |  |  |
| 2020-08-25 | 200 | +0 |  |  |
| 2020-09-25 | 200 | +0 |  |  |
| 2021-03-31 | 200 | +0 |  |  |
| 2021-06-30 | 200 | +0 |  |  |
| 2021-09-20 | 200 | +0 |  |  |
| 2021-09-30 | 200 | +0 |  |  |
| 2021-10-29 | 200 | +0 |  |  |
| 2022-01-11 | 200 | +0 |  |  |
| 2022-03-31 | 200 | +0 |  |  |
| 2022-04-20 | 200 | +0 |  |  |
| 2022-05-04 | 200 | +0 |  |  |
| 2022-07-29 | 200 | +0 |  |  |
| 2022-08-08 | 200 | +0 |  |  |
| 2022-09-14 | 200 | +0 |  |  |
| 2022-09-30 | 200 | +0 |  |  |
| 2023-03-31 | 200 | +0 |  |  |
| 2023-06-15 | 200 | +0 |  |  |
| 2023-07-13 | 200 | +0 |  |  |
| 2023-08-21 | 201 | +1 | 1 |  |
| 2023-09-07 | 200 | +0 |  |  |
| 2023-09-28 | 200 | +0 |  |  |
| 2023-09-29 | 200 | +0 |  |  |
| 2024-01-05 | 200 | +0 |  |  |
| 2024-03-28 | 200 | +0 |  |  |
| 2024-05-15 | 200 | +0 |  |  |
| 2024-05-27 | 200 | +0 |  |  |
| 2024-09-12 | 200 | +0 |  |  |
| 2024-09-30 | 200 | +0 |  |  |
| 2024-10-28 | 200 | +0 |  |  |
| 2024-12-27 | 200 | +0 |  |  |
| 2025-01-10 | 200 | +0 |  |  |
| 2025-01-29 | 201 | +1 | 1 |  |
| 2025-02-10 | 200 | +0 |  |  |
| 2025-03-28 | 200 | +0 |  |  |
| 2025-05-07 | 200 | +0 |  |  |
| 2025-06-04 | 200 | +0 |  |  |
| 2025-06-16 | 200 | +0 |  |  |
| 2025-06-19 | 201 | +1 | 1 |  |
| 2025-06-27 | 200 | +0 |  |  |
| 2025-09-22 | 200 | +0 |  |  |
| 2025-09-30 | 200 | +0 |  |  |
| 2025-11-12 | 201 | +1 | 1 |  |
| 2025-11-17 | 200 | +0 |  |  |
| 2026-01-14 | 200 | +0 |  |  |
| 2026-03-30 | 200 | +0 |  |  |
| 2026-06-15 | 201 | +1 | 1 |  |
| 2026-06-24 | 200 | +0 |  |  |
| 2026-09-30 | 200 | +0 |  |  |
| 2026-10-05 | 200 | +0 |  |  |

Residuals:

- 2016-10-24 `unreadable_section`  ind_prs17102016.pdf — Nifty 200 Index: the 'exclude' statement has no table of its own (detached layout)
- 2023-09-29 `include_not_held_later` IN9155A01020 ind_prs17082023.pdf — TATAMTRDVR included but not in the later set
- 2016-10-24 `entry_before_first_session` INE704J01044 coverage_start — walked back to 2016-10-24 but first seen 2026-06-15; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE1TAE01010 coverage_start — walked back to 2016-10-24 but first seen 2025-11-12; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE1NPP01017 coverage_start — walked back to 2016-10-24 but first seen 2025-06-19; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE379A01028 coverage_start — walked back to 2016-10-24 but first seen 2025-01-29; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE758E01017 coverage_start — walked back to 2016-10-24 but first seen 2023-08-21; clipped to its first appearance

## nifty500

| Segment start | Reconstructed | Residual | Clipped stand-ins | Second share class |
| --- | --- | --- | --- | --- |
| 2016-10-24 | 500 | +0 |  |  |
| 2016-11-15 | 500 | +0 |  |  |
| 2016-11-18 | 500 | +0 |  |  |
| 2016-11-30 | 500 | +0 |  |  |
| 2017-01-05 | 500 | +0 |  |  |
| 2017-01-23 | 500 | +0 |  |  |
| 2017-02-13 | 500 | +0 |  |  |
| 2017-03-16 | 500 | +0 |  |  |
| 2017-03-17 | 500 | +0 |  |  |
| 2017-03-31 | 500 | +0 |  |  |
| 2017-05-26 | 500 | +0 |  |  |
| 2017-06-23 | 500 | +0 |  |  |
| 2017-07-20 | 500 | +0 |  |  |
| 2017-07-26 | 500 | +0 |  |  |
| 2017-09-01 | 500 | +0 |  |  |
| 2017-09-05 | 500 | +0 |  |  |
| 2017-09-22 | 500 | +0 |  |  |
| 2017-09-29 | 500 | +0 |  |  |
| 2017-10-11 | 500 | +0 |  |  |
| 2017-10-13 | 500 | +0 |  |  |
| 2017-10-30 | 500 | +0 |  |  |
| 2017-11-10 | 500 | +0 |  |  |
| 2017-11-13 | 500 | +0 |  |  |
| 2017-11-20 | 500 | +0 |  |  |
| 2018-02-05 | 500 | +0 |  |  |
| 2018-03-13 | 500 | +0 |  |  |
| 2018-04-02 | 500 | +0 |  |  |
| 2018-04-26 | 500 | +0 |  |  |
| 2018-06-18 | 500 | +0 |  |  |
| 2018-06-27 | 500 | +0 |  |  |
| 2018-06-29 | 500 | +0 |  |  |
| 2018-08-08 | 500 | +0 |  |  |
| 2018-09-28 | 500 | +0 |  |  |
| 2018-10-22 | 500 | +0 |  |  |
| 2018-11-30 | 500 | +0 |  |  |
| 2018-12-28 | 500 | +0 |  |  |
| 2019-01-16 | 500 | +0 |  |  |
| 2019-01-28 | 500 | +0 |  |  |
| 2019-03-29 | 500 | +0 |  |  |
| 2019-04-15 | 500 | +0 |  |  |
| 2019-06-17 | 500 | +0 |  |  |
| 2019-06-28 | 500 | +0 |  |  |
| 2019-09-20 | 500 | +0 |  |  |
| 2019-09-24 | 500 | +0 |  |  |
| 2019-09-27 | 500 | +0 |  |  |
| 2019-12-16 | 500 | +0 |  |  |
| 2019-12-26 | 500 | +0 |  |  |
| 2019-12-27 | 500 | +0 |  |  |
| 2020-01-16 | 500 | +0 |  |  |
| 2020-02-06 | 500 | +0 |  |  |
| 2020-03-16 | 500 | +0 |  |  |
| 2020-03-19 | 500 | +0 |  |  |
| 2020-04-08 | 500 | +0 |  |  |
| 2020-06-26 | 500 | +0 |  |  |
| 2020-07-31 | 500 | +0 |  |  |
| 2020-08-25 | 500 | +0 |  |  |
| 2020-09-14 | 500 | +0 |  |  |
| 2020-09-25 | 500 | +0 |  |  |
| 2020-09-30 | 500 | +0 |  |  |
| 2020-10-05 | 500 | +0 |  |  |
| 2020-11-02 | 500 | +0 |  |  |
| 2020-11-25 | 500 | +0 |  |  |
| 2020-12-16 | 500 | +0 |  |  |
| 2021-03-19 | 500 | +0 |  |  |
| 2021-03-31 | 500 | +0 |  |  |
| 2021-04-19 | 500 | +0 |  |  |
| 2021-04-29 | 500 | +0 |  |  |
| 2021-05-10 | 500 | +0 |  |  |
| 2021-05-12 | 500 | +0 |  |  |
| 2021-06-30 | 500 | +0 |  |  |
| 2021-09-20 | 500 | +0 |  |  |
| 2021-09-27 | 500 | +0 |  |  |
| 2021-09-30 | 500 | +0 |  |  |
| 2021-10-08 | 500 | +0 |  |  |
| 2021-10-29 | 500 | +0 |  |  |
| 2021-12-15 | 500 | +0 |  |  |
| 2022-01-11 | 500 | +0 |  |  |
| 2022-02-09 | 500 | +0 |  |  |
| 2022-03-25 | 500 | +0 |  |  |
| 2022-03-31 | 500 | +0 |  |  |
| 2022-04-07 | 500 | +0 |  |  |
| 2022-04-12 | 500 | +0 |  |  |
| 2022-04-20 | 500 | +0 |  |  |
| 2022-04-27 | 500 | +0 |  |  |
| 2022-05-04 | 500 | +0 |  |  |
| 2022-05-31 | 500 | +0 |  |  |
| 2022-07-29 | 500 | +0 |  |  |
| 2022-08-08 | 500 | +0 |  |  |
| 2022-09-14 | 500 | +0 |  |  |
| 2022-09-30 | 500 | +0 |  |  |
| 2022-10-19 | 500 | +0 |  |  |
| 2022-11-22 | 500 | +0 |  |  |
| 2022-12-30 | 500 | +0 |  |  |
| 2023-02-17 | 500 | +0 |  |  |
| 2023-02-22 | 500 | +0 |  |  |
| 2023-03-02 | 500 | +0 |  |  |
| 2023-03-31 | 500 | +0 |  |  |
| 2023-04-28 | 500 | +0 |  |  |
| 2023-06-15 | 500 | +0 |  |  |
| 2023-07-13 | 500 | +0 |  |  |
| 2023-08-21 | 501 | +1 | 1 |  |
| 2023-09-07 | 500 | +0 |  |  |
| 2023-09-18 | 500 | +0 |  |  |
| 2023-09-28 | 500 | +0 |  |  |
| 2023-09-29 | 500 | +0 |  |  |
| 2023-10-26 | 500 | +0 |  |  |
| 2024-01-05 | 500 | +0 |  |  |
| 2024-01-10 | 500 | +0 |  |  |
| 2024-03-05 | 500 | +0 |  |  |
| 2024-03-28 | 500 | +0 |  |  |
| 2024-05-15 | 500 | +0 |  |  |
| 2024-05-27 | 500 | +0 |  |  |
| 2024-07-19 | 500 | +0 |  |  |
| 2024-07-25 | 500 | +0 |  |  |
| 2024-09-05 | 500 | +0 |  |  |
| 2024-09-12 | 500 | +0 |  |  |
| 2024-09-13 | 500 | +0 |  |  |
| 2024-09-30 | 500 | +0 |  |  |
| 2024-10-04 | 500 | +0 |  |  |
| 2024-10-09 | 500 | +0 |  |  |
| 2024-10-10 | 500 | +0 |  |  |
| 2024-10-16 | 500 | +0 |  |  |
| 2024-10-18 | 500 | +0 |  |  |
| 2024-10-28 | 500 | +0 |  |  |
| 2024-12-27 | 500 | +0 |  |  |
| 2025-01-10 | 500 | +0 |  |  |
| 2025-01-29 | 501 | +1 | 1 |  |
| 2025-01-31 | 501 | +1 | 1 |  |
| 2025-02-10 | 500 | +0 |  |  |
| 2025-03-21 | 500 | +0 |  |  |
| 2025-03-28 | 500 | +0 |  |  |
| 2025-04-11 | 500 | +0 |  |  |
| 2025-05-07 | 500 | +0 |  |  |
| 2025-06-04 | 500 | +0 |  |  |
| 2025-06-16 | 500 | +0 |  |  |
| 2025-06-19 | 501 | +1 | 1 |  |
| 2025-06-27 | 500 | +0 |  |  |
| 2025-07-01 | 501 | +1 | 1 |  |
| 2025-07-10 | 500 | +0 |  |  |
| 2025-09-22 | 500 | +0 |  |  |
| 2025-09-23 | 500 | +0 |  |  |
| 2025-09-30 | 500 | +0 |  |  |
| 2025-10-14 | 500 | +0 |  |  |
| 2025-11-03 | 500 | +0 |  |  |
| 2025-11-12 | 501 | +1 | 1 |  |
| 2025-11-13 | 502 | +2 | 2 |  |
| 2025-11-17 | 501 | +1 | 1 |  |
| 2025-11-28 | 500 | +0 |  |  |
| 2025-12-05 | 500 | +0 |  |  |
| 2025-12-26 | 500 | +0 |  |  |
| 2025-12-31 | 500 | +0 |  |  |
| 2026-01-02 | 500 | +0 |  |  |
| 2026-01-14 | 500 | +0 |  |  |
| 2026-02-26 | 500 | +0 |  |  |
| 2026-03-30 | 500 | +0 |  |  |
| 2026-05-12 | 500 | +0 |  |  |
| 2026-06-15 | 501 | +1 | 1 |  |
| 2026-06-24 | 500 | +0 |  |  |
| 2026-07-17 | 500 | +0 |  |  |
| 2026-09-30 | 500 | +0 |  |  |
| 2026-10-05 | 500 | +0 |  |  |

Residuals:

- 2016-10-24 `unreadable_section`  ind_prs17102016.pdf — Nifty 500 Index: the 'exclude' statement has no table of its own (detached layout)
- 2016-10-24 `entry_before_first_session` INE704J01044 coverage_start — walked back to 2016-10-24 but first seen 2026-06-15; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE28GN01010 coverage_start — walked back to 2016-10-24 but first seen 2025-11-13; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE1TAE01010 coverage_start — walked back to 2016-10-24 but first seen 2025-11-12; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE1SY401010 coverage_start — walked back to 2016-10-24 but first seen 2025-07-01; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE1NPP01017 coverage_start — walked back to 2016-10-24 but first seen 2025-06-19; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE379A01028 coverage_start — walked back to 2016-10-24 but first seen 2025-01-29; clipped to its first appearance
- 2016-10-24 `entry_before_first_session` INE758E01017 coverage_start — walked back to 2016-10-24 but first seen 2023-08-21; clipped to its first appearance

## niftymidcap150

| Segment start | Reconstructed | Residual | Clipped stand-ins | Second share class |
| --- | --- | --- | --- | --- |
| 2016-10-24 | 150 | +0 |  |  |
| 2016-11-18 | 150 | +0 |  |  |
| 2017-01-23 | 150 | +0 |  |  |
| 2017-03-31 | 150 | +0 |  |  |
| 2017-05-26 | 150 | +0 |  |  |
| 2017-06-23 | 150 | +0 |  |  |
| 2017-09-05 | 150 | +0 |  |  |
| 2017-09-29 | 150 | +0 |  |  |
| 2017-10-30 | 150 | +0 |  |  |
| 2017-11-10 | 150 | +0 |  |  |
| 2017-11-13 | 150 | +0 |  |  |
| 2018-02-05 | 150 | +0 |  |  |
| 2018-04-02 | 150 | +0 |  |  |
| 2018-04-26 | 150 | +0 |  |  |
| 2018-06-27 | 150 | +0 |  |  |
| 2018-06-29 | 150 | +0 |  |  |
| 2018-09-28 | 150 | +0 |  |  |
| 2018-12-28 | 150 | +0 |  |  |
| 2019-01-16 | 150 | +0 |  |  |
| 2019-03-29 | 150 | +0 |  |  |
| 2019-06-17 | 150 | +0 |  |  |
| 2019-06-28 | 150 | +0 |  |  |
| 2019-09-27 | 150 | +0 |  |  |
| 2019-12-27 | 150 | +0 |  |  |
| 2020-02-06 | 150 | +0 |  |  |
| 2020-03-19 | 150 | +0 |  |  |
| 2020-06-26 | 150 | +0 |  |  |
| 2020-09-25 | 150 | +0 |  |  |
| 2021-03-31 | 150 | +0 |  |  |
| 2021-09-20 | 150 | +0 |  |  |
| 2021-09-30 | 150 | +0 |  |  |
| 2021-10-08 | 150 | +0 |  |  |
| 2021-10-29 | 150 | +0 |  |  |
| 2021-12-15 | 150 | +0 |  |  |
| 2022-01-11 | 150 | +0 |  |  |
| 2022-02-09 | 150 | +0 |  |  |
| 2022-03-31 | 150 | +0 |  |  |
| 2022-05-04 | 150 | +0 |  |  |
| 2022-08-08 | 150 | +0 |  |  |
| 2022-09-30 | 150 | +0 |  |  |
| 2023-03-31 | 150 | +0 |  |  |
| 2023-07-13 | 150 | +0 |  |  |
| 2023-09-29 | 150 | +0 |  |  |
| 2024-03-28 | 150 | +0 |  |  |
| 2024-05-27 | 150 | +0 |  |  |
| 2024-09-30 | 150 | +0 |  |  |
| 2024-12-27 | 150 | +0 |  |  |
| 2025-03-28 | 150 | +0 |  |  |
| 2025-06-04 | 150 | +0 |  |  |
| 2025-06-19 | 151 | +1 | 1 |  |
| 2025-06-27 | 150 | +0 |  |  |
| 2025-09-30 | 150 | +0 |  |  |
| 2025-10-14 | 150 | +0 |  |  |
| 2026-03-30 | 150 | +0 |  |  |
| 2026-09-30 | 150 | +0 |  |  |
| 2026-10-05 | 150 | +0 |  |  |

Residuals:

- 2016-10-24 `unreadable_section`  ind_prs17102016.pdf — Nifty Midcap 150 Index: the 'exclude' statement has no table of its own (detached layout)
- 2016-10-24 `entry_before_first_session` INE1NPP01017 coverage_start — walked back to 2016-10-24 but first seen 2025-06-19; clipped to its first appearance

## niftysmallcap250

| Segment start | Reconstructed | Residual | Clipped stand-ins | Second share class |
| --- | --- | --- | --- | --- |
| 2016-09-30 | 250 | +0 |  |  |
| 2016-10-13 | 250 | +0 |  |  |
| 2016-10-20 | 250 | +0 |  |  |
| 2016-11-15 | 250 | +0 |  |  |
| 2016-11-30 | 250 | +0 |  |  |
| 2017-01-23 | 250 | +0 |  |  |
| 2017-02-13 | 250 | +0 |  |  |
| 2017-03-16 | 250 | +0 |  |  |
| 2017-03-31 | 250 | +0 |  |  |
| 2017-05-26 | 250 | +0 |  |  |
| 2017-06-23 | 250 | +0 |  |  |
| 2017-07-20 | 250 | +0 |  |  |
| 2017-07-26 | 250 | +0 |  |  |
| 2017-09-01 | 250 | +0 |  |  |
| 2017-09-29 | 250 | +0 |  |  |
| 2017-10-11 | 250 | +0 |  |  |
| 2017-10-13 | 250 | +0 |  |  |
| 2017-11-13 | 250 | +0 |  |  |
| 2017-11-20 | 250 | +0 |  |  |
| 2018-02-05 | 250 | +0 |  |  |
| 2018-03-13 | 250 | +0 |  |  |
| 2018-04-02 | 250 | +0 |  |  |
| 2018-06-29 | 250 | +0 |  |  |
| 2018-08-08 | 250 | +0 |  |  |
| 2018-09-28 | 250 | +0 |  |  |
| 2018-10-22 | 250 | +0 |  |  |
| 2018-12-28 | 250 | +0 |  |  |
| 2019-01-28 | 250 | +0 |  |  |
| 2019-03-29 | 250 | +0 |  |  |
| 2019-04-15 | 250 | +0 |  |  |
| 2019-06-28 | 250 | +0 |  |  |
| 2019-09-24 | 250 | +0 |  |  |
| 2019-09-27 | 250 | +0 |  |  |
| 2019-12-16 | 250 | +0 |  |  |
| 2019-12-26 | 250 | +0 |  |  |
| 2019-12-27 | 250 | +0 |  |  |
| 2020-01-16 | 250 | +0 |  |  |
| 2020-03-19 | 250 | +0 |  |  |
| 2020-04-08 | 250 | +0 |  |  |
| 2020-06-26 | 250 | +0 |  |  |
| 2020-09-14 | 250 | +0 |  |  |
| 2020-09-25 | 250 | +0 |  |  |
| 2020-09-30 | 250 | +0 |  |  |
| 2020-10-05 | 250 | +0 |  |  |
| 2020-11-02 | 250 | +0 |  |  |
| 2020-11-25 | 250 | +0 |  |  |
| 2020-12-16 | 250 | +0 |  |  |
| 2021-03-19 | 250 | +0 |  |  |
| 2021-03-31 | 250 | +0 |  |  |
| 2021-04-19 | 250 | +0 |  |  |
| 2021-04-29 | 250 | +0 |  |  |
| 2021-05-10 | 250 | +0 |  |  |
| 2021-05-12 | 250 | +0 |  |  |
| 2021-09-27 | 250 | +0 |  |  |
| 2021-09-30 | 250 | +0 |  |  |
| 2022-03-25 | 250 | +0 |  |  |
| 2022-03-31 | 250 | +0 |  |  |
| 2022-04-07 | 250 | +0 |  |  |
| 2022-04-12 | 250 | +0 |  |  |
| 2022-04-27 | 250 | +0 |  |  |
| 2022-05-31 | 250 | +0 |  |  |
| 2022-08-08 | 250 | +0 |  |  |
| 2022-09-30 | 250 | +0 |  |  |
| 2022-10-19 | 250 | +0 |  |  |
| 2022-11-22 | 250 | +0 |  |  |
| 2022-12-30 | 250 | +0 |  |  |
| 2023-02-17 | 250 | +0 |  |  |
| 2023-02-22 | 250 | +0 |  |  |
| 2023-03-02 | 250 | +0 |  |  |
| 2023-03-31 | 250 | +0 |  |  |
| 2023-04-28 | 250 | +0 |  |  |
| 2023-09-18 | 250 | +0 |  |  |
| 2023-09-29 | 250 | +0 |  |  |
| 2023-10-26 | 250 | +0 |  |  |
| 2024-01-10 | 250 | +0 |  |  |
| 2024-03-05 | 250 | +0 |  |  |
| 2024-03-28 | 250 | +0 |  |  |
| 2024-07-19 | 250 | +0 |  |  |
| 2024-07-25 | 250 | +0 |  |  |
| 2024-09-05 | 250 | +0 |  |  |
| 2024-09-13 | 250 | +0 |  |  |
| 2024-09-30 | 250 | +0 |  |  |
| 2024-10-04 | 250 | +0 |  |  |
| 2024-10-09 | 250 | +0 |  |  |
| 2024-10-10 | 250 | +0 |  |  |
| 2024-10-16 | 250 | +0 |  |  |
| 2024-10-18 | 250 | +0 |  |  |
| 2025-01-31 | 250 | +0 |  |  |
| 2025-03-21 | 250 | +0 |  |  |
| 2025-03-28 | 250 | +0 |  |  |
| 2025-04-11 | 250 | +0 |  |  |
| 2025-07-01 | 251 | +1 | 1 |  |
| 2025-07-10 | 250 | +0 |  |  |
| 2025-09-23 | 250 | +0 |  |  |
| 2025-09-30 | 250 | +0 |  |  |
| 2025-11-03 | 250 | +0 |  |  |
| 2025-11-13 | 251 | +1 | 1 |  |
| 2025-11-28 | 250 | +0 |  |  |
| 2025-12-05 | 250 | +0 |  |  |
| 2025-12-26 | 250 | +0 |  |  |
| 2025-12-31 | 250 | +0 |  |  |
| 2026-01-02 | 250 | +0 |  |  |
| 2026-02-26 | 250 | +0 |  |  |
| 2026-03-30 | 250 | +0 |  |  |
| 2026-05-12 | 250 | +0 |  |  |
| 2026-07-17 | 250 | +0 |  |  |
| 2026-09-30 | 250 | +0 |  |  |
| 2026-10-05 | 250 | +0 |  |  |

Residuals:

- 2016-09-30 `unreadable_section`  ind_prs12082016.pdf — Nifty Smallcap 250 Index: the 'exclude' statement has no table of its own (detached layout)
- 2016-09-30 `entry_before_first_session` INE28GN01010 coverage_start — walked back to 2016-09-30 but first seen 2025-11-13; clipped to its first appearance
- 2016-09-30 `entry_before_first_session` INE1SY401010 coverage_start — walked back to 2016-09-30 but first seen 2025-07-01; clipped to its first appearance

## Releases read from a curated transcription

- ind_prs23082021.pdf: no text layer; read from `index_release_transcriptions.yaml` (sha256-pinned to the L0 object)

## Quarantined events (unresolved symbol)

- 2011-04-21 nifty500 exclude Indiabulls Real Estate Limited (IBREALEST) [ind_prs19042011.pdf] — no ISIN traded as IBREALEST within 15 days of 2011-04-21
- 2005-10-06 nifty500 exclude Bank of Punjab Ltd. (BANKS) [ind_prs05102005.pdf] — no ISIN traded as BANKS within 15 days of 2005-10-06
- 2005-05-09 nifty500 exclude IDBI Bank Ltd. (BANKS) [ind_prs06052005.pdf] — no ISIN traded as BANKS within 15 days of 2005-05-09
- 2005-09-26 nifty500 include Rajesh Exports ltd. (TRADING) [ind_prs23082005.pdf] — no ISIN traded as TRADING within 15 days of 2005-09-26
- 2005-05-09 nifty500 include Vishal Exports Overseas Ltd. (TRADING) [ind_prs06052005.pdf] — no ISIN traded as TRADING within 15 days of 2005-05-09
- 2015-03-27 nifty500 exclude IL&FS Engineering and Construction Co. Ltd. IL&FS (ENGG) [ind_prs20022015.pdf] — no ISIN traded as ENGG within 15 days of 2015-03-27
- 2013-11-15 nifty200 exclude Welspun Corp Limited The following company is being (None) [ind_prs07112013.pdf] — the release prints no symbol
- 2013-06-21 nifty500 exclude K S Oils Limited The following company is being (None) [ind_prs14062013.pdf] — the release prints no symbol
- 2013-06-11 nifty500 exclude Bharti Infratel Limited (None) [ind_prs06062013.pdf] — the release prints no symbol
- 2012-05-21 nifty200 exclude Patni Computer System The following company is being (None) [ind_prs16052012.pdf] — the release prints no symbol
- 2011-06-21 nifty500 exclude Television Eighteen India Limited (TV-18) [ind_prs16062011.pdf] — no ISIN traded as TV-18 within 15 days of 2011-06-21
- 2011-05-18 nifty500 exclude Essar Shipping Ports & Logistics Limited (ESSARSHIP) [ind_prs12052011.pdf] — no ISIN traded as ESSARSHIP within 15 days of 2011-05-18
- 2011-05-18 nifty500 include ARSS Infrastructure Projects Limited (ARSSINFRA) [ind_prs12052011.pdf] — no ISIN traded as ARSSINFRA within 15 days of 2011-05-18
- 2011-04-21 nifty500 include Oberoi Realty Limited (OBEROIRLTY) [ind_prs19042011.pdf] — no ISIN traded as OBEROIRLTY within 15 days of 2011-04-21
- 2011-03-25 nifty50 exclude Suzlon Energy Ltd. (None) [ind_prs10022011.pdf] — the release prints no symbol
- 2011-03-25 nifty50 include Grasim Industries Ltd. (None) [ind_prs10022011.pdf] — the release prints no symbol
- 2011-03-25 niftynext50 exclude Corporation Bank (None) [ind_prs10022011.pdf] — the release prints no symbol
- 2004-12-10 niftynext50 exclude Mangalore Refinery & Petrochemicals Ltd. (None) [ind_prs29102004.pdf] — the release prints no symbol
- 2011-03-25 niftynext50 include IndusInd Bank Ltd. (None) [ind_prs10022011.pdf] — the release prints no symbol
- 2011-03-25 nifty100 exclude Corporation Bank (None) [ind_prs10022011.pdf] — the release prints no symbol
- 2011-03-25 nifty100 include IndusInd Bank Ltd. (None) [ind_prs10022011.pdf] — the release prints no symbol
- 2011-03-25 nifty500 exclude AGC Networks Ltd. (None) [ind_prs10022011.pdf] — the release prints no symbol
- 2011-03-25 nifty500 include Allied Digital Services Ltd. (None) [ind_prs10022011.pdf] — the release prints no symbol
- 2009-03-27 nifty500 exclude Biocon Ltd. (None) [ind_prs10022009.pdf] — the release prints no symbol
- 2006-12-15 nifty500 exclude FCL Technologies & Products Ltd (None) [ind_prs12122006.pdf] — the release prints no symbol
- 2006-12-15 nifty500 include GMR Infrastructure Ltd. (None) [ind_prs12122006.pdf] — the release prints no symbol
- 2006-11-17 nifty500 exclude Television Eighteen India Ltd. (None) [ind_prs16112006.pdf] — the release prints no symbol
- 2006-11-17 nifty500 include D.S. Kulkarni Developers Ltd (None) [ind_prs16112006.pdf] — the release prints no symbol
- 2006-11-08 niftynext50 exclude Great Eastern Shipping Co. Ltd. (None) [ind_prs03112006.pdf] — the release prints no symbol
- 2006-11-08 niftynext50 include Indian Hotels Ltd. (None) [ind_prs03112006.pdf] — the release prints no symbol
- 2006-11-08 nifty500 exclude Great Eastern Shipping Co. Ltd. (None) [ind_prs03112006.pdf] — the release prints no symbol
- 2006-11-08 nifty500 include Era Construction (I) Ltd. (None) [ind_prs03112006.pdf] — the release prints no symbol
- 2006-11-08 nifty100 exclude Great Eastern Shipping Co. Ltd. National Stock Exchange of India Ltd. http://www.nseindia.com/[31-03-2011 11:08:21] (None) [ind_prs03112006.pdf] — the release prints no symbol
- 2006-11-08 nifty100 include Indian Hotels Ltd. (None) [ind_prs03112006.pdf] — the release prints no symbol
- 2006-10-05 nifty500 exclude United Western Bank Ltd. (None) [ind_prs04102006.pdf] — the release prints no symbol
- 2006-10-05 nifty500 include Mahindra & Mahindra Financial Services Ltd. (None) [ind_prs04102006.pdf] — the release prints no symbol
- 2006-09-28 nifty500 exclude Manali Petrochemicals Ltd. (None) [ind_prs22092006.pdf] — the release prints no symbol
- 2006-09-28 nifty500 include Gitanjali Gems Ltd. (None) [ind_prs22092006.pdf] — the release prints no symbol
- 2006-09-21 nifty500 exclude Torrent Power AEC Ltd. (None) [ind_prs20092006.pdf] — the release prints no symbol
- 2006-09-21 nifty500 include Inox Leisure Ltd. (None) [ind_prs20092006.pdf] — the release prints no symbol
- 2006-09-22 niftynext50 exclude Kochi Refineries Ltd. (None) [ind_prs08092006_1.pdf] — the release prints no symbol
- 2006-09-22 niftynext50 include Reliance Petroleum Ltd. (None) [ind_prs08092006_1.pdf] — the release prints no symbol
- 2006-09-22 nifty100 exclude Kochi Refineries Ltd. (None) [ind_prs08092006_1.pdf] — the release prints no symbol
- 2006-09-22 nifty100 include Reliance Petroleum Ltd. (None) [ind_prs08092006_1.pdf] — the release prints no symbol
- 2006-09-22 nifty500 exclude Kochi Refineries Ltd. (None) [ind_prs08092006_1.pdf] — the release prints no symbol
- 2006-09-22 nifty500 include Sun TV Ltd. (None) [ind_prs08092006_1.pdf] — the release prints no symbol
- 2006-09-13 nifty500 exclude Kitply Industries Ltd. (None) [ind_prs07092006.pdf] — the release prints no symbol
- 2006-09-13 nifty500 include Ansal Properties & Infrastructure Ltd. (None) [ind_prs07092006.pdf] — the release prints no symbol
- 2006-07-25 nifty500 exclude Shyam Telecom Ltd. (None) [ind_prs24072006.pdf] — the release prints no symbol
- 2006-07-25 nifty500 include Reliance Natural Resources Ltd. (None) [ind_prs24072006.pdf] — the release prints no symbol
- 2006-07-21 nifty500 exclude Rama Newsprint & Papers Ltd. (None) [ind_prs20072006_2.pdf] — the release prints no symbol
- 2006-07-21 nifty500 include B L Kashyap & Sons Ltd. (None) [ind_prs20072006_2.pdf] — the release prints no symbol
- 2006-09-01 nifty50 exclude Tata Tea Ltd. (None) [ind_prs20072006_1.pdf] — the release prints no symbol
- 2006-09-01 nifty50 include Reliance Communications Ltd. (None) [ind_prs20072006_1.pdf] — the release prints no symbol
- 2006-09-01 nifty100 exclude Tata Tea Ltd. (None) [ind_prs20072006_1.pdf] — the release prints no symbol
- 2006-09-01 nifty100 include Reliance Communications Ltd. (None) [ind_prs20072006_1.pdf] — the release prints no symbol
- 2006-07-14 nifty500 exclude Williamson Tea Assam Ltd. (None) [ind_prs13072006.pdf] — the release prints no symbol
- 2006-07-14 nifty500 include Videocon Industries Ltd. (None) [ind_prs13072006.pdf] — the release prints no symbol
- 2006-07-10 nifty500 exclude Birla Global Finance Ltd. (None) [ind_prs07072006.pdf] — the release prints no symbol
- 2006-07-10 nifty500 include Reliance Communications Ltd. (None) [ind_prs07072006.pdf] — the release prints no symbol
- 2006-06-27 nifty50 exclude Shipping Corporation of India Ltd. (None) [ind_prs21062006.pdf] — the release prints no symbol
- 2006-06-27 nifty50 include Suzlon Energy Ltd. (None) [ind_prs21062006.pdf] — the release prints no symbol
- 2006-06-27 niftynext50 exclude Siemens Ltd. (None) [ind_prs21062006.pdf] — the release prints no symbol
- 2006-06-27 niftynext50 include Infrastructure Development Finance Co. Ltd. (None) [ind_prs21062006.pdf] — the release prints no symbol
- 2006-06-27 nifty100 exclude Shipping Corporation of India Ltd. (None) [ind_prs21062006.pdf] — the release prints no symbol
- 2006-06-27 nifty100 include Suzlon Energy Ltd. (None) [ind_prs21062006.pdf] — the release prints no symbol
- 2006-05-17 nifty500 exclude Munjal Auto Industries Ltd. (None) [ind_prs16052006.pdf] — the release prints no symbol
- 2006-05-17 nifty500 include Deccan Chronicle Holdings Ltd. (None) [ind_prs16052006.pdf] — the release prints no symbol
- 2006-06-26 nifty100 exclude Tata Chemicals Ltd. National Stock Exchange of India Ltd. http://www.nseindia.com/[31-03-2011 10:48:40] (None) [ind_prs12052006.pdf] — the release prints no symbol
- 2006-04-13 nifty500 exclude Clariant (India) Ltd. (None) [ind_prs10042006.pdf] — the release prints no symbol
- 2006-04-13 nifty500 include Shree Renuka Sugars Ltd. (None) [ind_prs10042006.pdf] — the release prints no symbol
- 2004-12-10 nifty500 exclude Madras Fertilizers Ltd. (FERTILISERS) [ind_prs29102004.pdf] — no ISIN traded as FERTILISERS within 15 days of 2004-12-10
- 2006-03-02 nifty500 exclude Fertilisers & Chemicals Travancore Ltd. (FERTILISERS) [ind_prs01022006_1.pdf] — no ISIN traded as FERTILISERS within 15 days of 2006-03-02
- 2006-04-10 nifty500 exclude Indo Gulf Fertilisers Ltd. (FERTILISERS) [ind_prs07042006.pdf] — no ISIN traded as FERTILISERS within 15 days of 2006-04-10
- 2005-09-26 nifty500 include Dwarikesh Sugar Industrial ltd. (SUGAR) [ind_prs23082005.pdf] — no ISIN traded as SUGAR within 15 days of 2005-09-26
- 2006-04-10 nifty500 include Triveni Engineering & Industries Ltd. (SUGAR) [ind_prs07042006.pdf] — no ISIN traded as SUGAR within 15 days of 2006-04-10
- 2006-03-23 nifty500 exclude Vashisti Detergents Ltd. (DETERGENTS) [ind_prs21032006.pdf] — no ISIN traded as DETERGENTS within 15 days of 2006-03-23
- 2006-03-23 nifty500 include Suzlon Energy Ltd. ELECTRICAL (EQUIPMENT) [ind_prs21032006.pdf] — no ISIN traded as EQUIPMENT within 15 days of 2006-03-23
- 2006-03-02 niftynext50 exclude Flextronics Software Systems Ltd. * (None) [ind_prs01022006_1.pdf] — the release prints no symbol
- 2006-03-02 niftynext50 include Container Corporation of India Ltd. * (None) [ind_prs01022006_1.pdf] — the release prints no symbol
- 2004-12-10 nifty500 exclude Compudyne Winfosystems Ltd. COMPUTERS - (SOFTWARE) [ind_prs29102004.pdf] — no ISIN traded as SOFTWARE within 15 days of 2004-12-10
- 2006-03-02 nifty500 exclude Flextronics Software Systems Ltd. * COMPUTERS – (SOFTWARE) [ind_prs01022006_1.pdf] — no ISIN traded as SOFTWARE within 15 days of 2006-03-02
- 2006-03-02 nifty500 exclude Shree Rama Multi Tech Ltd. (PACKAGING) [ind_prs01022006_1.pdf] — no ISIN traded as PACKAGING within 15 days of 2006-03-02
- 2006-03-02 nifty500 exclude Tata Teleservices (Maharashtra) Ltd. TELECOMMUNICATION (SERVICES) [ind_prs01022006_1.pdf] — no ISIN traded as SERVICES within 15 days of 2006-03-02
- 2006-03-02 nifty500 exclude KDL Biotech Ltd. (PHARMACEUTICALS) [ind_prs01022006_1.pdf] — no ISIN traded as PHARMACEUTICALS within 15 days of 2006-03-02
- 2004-12-10 nifty500 exclude Morarjee Realties Ltd. (CONSTRUCTION) [ind_prs29102004.pdf] — no ISIN traded as CONSTRUCTION within 15 days of 2004-12-10
- 2006-03-02 nifty500 exclude Regency Ceramics Ltd. (CONSTRUCTION) [ind_prs01022006_1.pdf] — no ISIN traded as CONSTRUCTION within 15 days of 2006-03-02
- 2004-12-10 nifty500 exclude National Organic Chemical Industries Ltd. (PETROCHEMICALS) [ind_prs29102004.pdf] — no ISIN traded as PETROCHEMICALS within 15 days of 2004-12-10
- 2006-03-02 nifty500 exclude Vinyl Chemicals (India) Ltd. (PETROCHEMICALS) [ind_prs01022006_1.pdf] — no ISIN traded as PETROCHEMICALS within 15 days of 2006-03-02
- 2004-12-10 nifty500 exclude Mysore Cements Ltd. CEMENT AND CEMENT (PRODUCTS) [ind_prs29102004.pdf] — no ISIN traded as PRODUCTS within 15 days of 2004-12-10
- 2005-04-20 nifty500 exclude Welspun Gujarat Stahl Rohren Ltd. STEEL AND STEEL (PRODUCTS) [ind_prs12042005.pdf] — no ISIN traded as PRODUCTS within 15 days of 2005-04-20
- 2005-04-25 nifty500 exclude Essar Steel Ltd STEEL AND STEEL (PRODUCTS) [ind_prs22042005.pdf] — no ISIN traded as PRODUCTS within 15 days of 2005-04-25
- 2005-09-26 nifty500 exclude Birla VXL ltd. TEXTILE (PRODUCTS) [ind_prs23082005.pdf] — no ISIN traded as PRODUCTS within 15 days of 2005-09-26
- 2005-12-08 nifty500 exclude Ispat Industries Ltd. STEEL AND STEEL (PRODUCTS) [ind_prs29112005.pdf] — no ISIN traded as PRODUCTS within 15 days of 2005-12-08
- 2006-03-02 nifty500 exclude BSL Ltd. TEXTILES (PRODUCTS) [ind_prs01022006_1.pdf] — no ISIN traded as PRODUCTS within 15 days of 2006-03-02
- 2006-03-02 nifty500 include IDFC Ltd. * FINANCIAL (INSTITUTION) [ind_prs01022006_1.pdf] — no ISIN traded as INSTITUTION within 15 days of 2006-03-02
- 2006-03-02 nifty500 include HT Media Ltd. PRINTING & (PUBLISHING) [ind_prs01022006_1.pdf] — no ISIN traded as PUBLISHING within 15 days of 2006-03-02
- 2006-01-06 nifty500 exclude Videocon International Ltd. Consumer Durables (None) [ind_prs04012006.pdf] — the release prints no symbol
- 2006-01-06 nifty500 include Shoppers Stop Ltd. Miscellaneous (None) [ind_prs04012006.pdf] — the release prints no symbol
- 2005-12-14 nifty500 exclude Shriram Investments Ltd. (FINANCE) [ind_prs13122005.pdf] — no ISIN traded as FINANCE within 15 days of 2005-12-14
- 2005-04-06 nifty500 include Tata Metaliks Ltd. STEEL AND STEEL (PRODUCTS) [ind_prs04042005.pdf] — no ISIN traded as PRODUCTS within 15 days of 2005-04-06
- 2005-10-06 nifty500 include JSW Steel Ltd. STEEL AND STEEL (PRODUCTS) [ind_prs05102005.pdf] — no ISIN traded as PRODUCTS within 15 days of 2005-10-06
- 2005-12-14 nifty500 include Essar Steel Ltd. STEEL AND STEEL (PRODUCTS) [ind_prs13122005.pdf] — no ISIN traded as PRODUCTS within 15 days of 2005-12-14
- 2005-12-08 nifty500 include Financial Technologies (India) Ltd. COMPUTERS - (SOFTWARE) [ind_prs29112005.pdf] — no ISIN traded as SOFTWARE within 15 days of 2005-12-08
- 2005-09-26 nifty50 exclude Colgate Palmolive (India) ltd. (None) [ind_prs23082005.pdf] — the release prints no symbol
- 2005-09-26 nifty50 include Jet Airways (India) Ltd. (None) [ind_prs23082005.pdf] — the release prints no symbol
- 2005-09-26 nifty500 exclude Tamilnadu Telecommunication ltd. (CABLES-TELECOM) [ind_prs23082005.pdf] — no ISIN traded as CABLES-TELECOM within 15 days of 2005-09-26
- 2005-09-26 nifty500 exclude Hindustan Organic Chemical ltd. (CHEMICALS-ORGANIC) [ind_prs23082005.pdf] — no ISIN traded as CHEMICALS-ORGANIC within 15 days of 2005-09-26
- 2005-09-26 nifty500 exclude DSQ Software ltd. (COMPUTERS-SOFTWARE) [ind_prs23082005.pdf] — no ISIN traded as COMPUTERS-SOFTWARE within 15 days of 2005-09-26
- 2005-09-26 nifty500 exclude Creative Eye ltd. MEDIA & (ENTERTAINMENT) [ind_prs23082005.pdf] — no ISIN traded as ENTERTAINMENT within 15 days of 2005-09-26
- 2005-09-26 nifty500 include Gateway Distriparks ltd. TRAVEL & (TRANSPORT) [ind_prs23082005.pdf] — no ISIN traded as TRANSPORT within 15 days of 2005-09-26
- 2005-09-26 nifty500 include Bhansali Engg. Polymers ltd. (PETROCHEMICALS) [ind_prs23082005.pdf] — no ISIN traded as PETROCHEMICALS within 15 days of 2005-09-26
- 2004-12-10 nifty500 include Jaiprakash Associates Ltd. (CONSTRUCTION) [ind_prs29102004.pdf] — no ISIN traded as CONSTRUCTION within 15 days of 2004-12-10
- 2005-09-26 nifty500 include Hindustan Sanitaryware & industrial ltd. (CONSTRUCTION) [ind_prs23082005.pdf] — no ISIN traded as CONSTRUCTION within 15 days of 2005-09-26
- 2005-04-20 nifty500 include Eicher Ltd. (FINANCE) [ind_prs12042005.pdf] — no ISIN traded as FINANCE within 15 days of 2005-04-20
- 2005-09-26 nifty500 include Consolidated Finvest & Holdings ltd. (FINANCE) [ind_prs23082005.pdf] — no ISIN traded as FINANCE within 15 days of 2005-09-26
- 2005-08-05 nifty500 exclude Tata Finance Ltd. Finance (None) [ind_prs03082005.pdf] — the release prints no symbol
- 2005-08-05 nifty500 include Jet Airways (India) Ltd. Travel & Transport (None) [ind_prs03082005.pdf] — the release prints no symbol
- 2004-12-10 nifty500 include Biocon Ltd. (PHARMACEUTICALS) [ind_prs29102004.pdf] — no ISIN traded as PHARMACEUTICALS within 15 days of 2004-12-10
- 2005-04-13 nifty500 include Dabur Pharma Ltd. (PHARMACEUTICALS) [ind_prs12042005.pdf] — no ISIN traded as PHARMACEUTICALS within 15 days of 2005-04-13
- 2005-04-25 nifty500 include Ind-Swift Laboratories Ltd. (PHARMACEUTICALS) [ind_prs22042005.pdf] — no ISIN traded as PHARMACEUTICALS within 15 days of 2005-04-25
- 2005-04-06 nifty500 exclude Suryalakshmi Cotton Mills Ltd. TEXTILES - (COTTON) [ind_prs04042005.pdf] — no ISIN traded as COTTON within 15 days of 2005-04-06
- 2005-04-13 nifty500 exclude Vardhman Spinning & General Mills Ltd. TEXTILES - (COTTON) [ind_prs12042005.pdf] — no ISIN traded as COTTON within 15 days of 2005-04-13
- 2005-04-20 nifty500 include Radico Khaitan Ltd BREW/DISTILLERIES (None) [ind_prs12042005.pdf] — the release prints no symbol
- 2005-04-20 nifty500 exclude Agee Gold Refiners Ltd. (METALS) [ind_prs12042005.pdf] — no ISIN traded as METALS within 15 days of 2005-04-20
- 2004-12-10 nifty500 exclude BPL Engineering Ltd. ELECTRONICS - (INDUSTRIAL) [ind_prs29102004.pdf] — no ISIN traded as INDUSTRIAL within 15 days of 2004-12-10
- 2005-04-20 nifty500 exclude Wellwin Industry Ltd. ELECTRONICS - (INDUSTRIAL) [ind_prs12042005.pdf] — no ISIN traded as INDUSTRIAL within 15 days of 2005-04-20
- 2005-04-20 nifty500 include Aarti Industries Ltd. CHEMICALS - (ORGANIC) [ind_prs12042005.pdf] — no ISIN traded as ORGANIC within 15 days of 2005-04-20
- 2005-04-08 nifty500 exclude ITC Hotels Ltd. (HOTELS) [ind_prs04042005.pdf] — no ISIN traded as HOTELS within 15 days of 2005-04-08
- 2005-04-08 nifty500 include Gujarat NRE Coke Ltd. (MINING) [ind_prs04042005.pdf] — no ISIN traded as MINING within 15 days of 2005-04-08
- 2005-03-24 nifty500 exclude Wimco Ltd. Miscellaneous (None) [ind_prs21032005.pdf] — the release prints no symbol
- 2005-03-24 nifty500 include National Thermal Power Corporation Ltd. Power (None) [ind_prs21032005.pdf] — the release prints no symbol
- 2005-03-04 nifty500 exclude Pharmacia Healthcare Ltd. Pharmaceuticals (None) [ind_prs03032005.pdf] — the release prints no symbol
- 2005-03-04 nifty500 include Kalyani Brakes Ltd. Auto Ancillaries (None) [ind_prs03032005.pdf] — the release prints no symbol
- 2005-03-15 nifty500 exclude Eveready Industries Ltd. Miscellaneous (None) [ind_prs03032005.pdf] — the release prints no symbol
- 2005-03-15 nifty500 include Shriram Investments Ltd Finance (None) [ind_prs03032005.pdf] — the release prints no symbol
- 2005-02-21 niftynext50 exclude Jindal Vijayanagar Steel Ltd. (None) [ind_prs16022005.pdf] — the release prints no symbol
- 2005-02-21 niftynext50 include Sterlite Industries (India) Ltd. (None) [ind_prs16022005.pdf] — the release prints no symbol
- 2005-02-21 nifty500 exclude Jindal Vijayanagar Steel Ltd. Steel & Steel Products (None) [ind_prs16022005.pdf] — the release prints no symbol
- 2005-02-21 nifty500 include Jindal Stainless Ltd. Steel & Steel Products (None) [ind_prs16022005.pdf] — the release prints no symbol
- 2005-02-18 nifty500 exclude Jindal Iron & Steel Co. Ltd. Steel & Steel Products (None) [ind_prs14022005.pdf] — the release prints no symbol
- 2005-02-18 nifty500 include Indiabulls Financial Services Ltd. Finance (None) [ind_prs14022005.pdf] — the release prints no symbol
- 2005-02-25 nifty50 exclude Indian Hotels Co. Ltd. (None) [ind_prs12012005.pdf] — the release prints no symbol
- 2005-02-25 nifty50 include Tata Consultancy Services Ltd. (None) [ind_prs12012005.pdf] — the release prints no symbol
- 2005-01-05 nifty500 exclude India Gypsum Ltd. Construction (None) [ind_prs27122004.pdf] — the release prints no symbol
- 2005-01-05 nifty500 include UltraTech Cement Ltd. Cement and Cement Products (None) [ind_prs27122004.pdf] — the release prints no symbol
- 2004-12-22 nifty500 exclude Jindal Photo Ltd. (None) [ind_prs14122004_2.pdf] — the release prints no symbol
- 2004-12-22 nifty500 include Tata Consultancy Services Ltd. (None) [ind_prs14122004_2.pdf] — the release prints no symbol
- 2004-11-18 nifty500 exclude Compudyne Winosystems Ltd. (None) [ind_prs05112004.pdf] — the release prints no symbol
- 2004-11-18 nifty500 include Larsen & Toubro Ltd. (None) [ind_prs05112004.pdf] — the release prints no symbol
- 2004-12-10 nifty50 exclude Britannia Industries Ltd. (None) [ind_prs29102004.pdf] — the release prints no symbol
- 2004-12-10 nifty50 include Larsen & Toubro Ltd. (None) [ind_prs29102004.pdf] — the release prints no symbol
- 2004-12-10 niftynext50 include Biocon Ltd. (None) [ind_prs29102004.pdf] — the release prints no symbol
- 2004-12-10 nifty500 exclude Atcom Technologies Ltd. (MISCELLANEOUS) [ind_prs29102004.pdf] — no ISIN traded as MISCELLANEOUS within 15 days of 2004-12-10
- 2004-12-10 nifty500 exclude IFCI Ltd. FINANCIAL (INSTITUTION) [ind_prs29102004.pdf] — no ISIN traded as INSTITUTION within 15 days of 2004-12-10
- 2004-12-10 nifty500 exclude IT&T Ltd. COMPUTERS - (HARDWARE) [ind_prs29102004.pdf] — no ISIN traded as HARDWARE within 15 days of 2004-12-10
- 2004-12-10 nifty500 exclude ITI Ltd. TELECOMMUNICATION - (EQUIPMENT) [ind_prs29102004.pdf] — no ISIN traded as EQUIPMENT within 15 days of 2004-12-10
- 2004-12-10 nifty500 exclude LML Ltd. AUTOMOBILES - 2 AND 3 (WHEELERS) [ind_prs29102004.pdf] — no ISIN traded as WHEELERS within 15 days of 2004-12-10
- 2004-12-10 nifty500 exclude Parekh Platinum Ltd. GEMS, JEWELLERY (AND) [ind_prs29102004.pdf] — no ISIN traded as AND within 15 days of 2004-12-10
- 2004-12-10 nifty500 include Indraprastha Gas Ltd. (GAS) [ind_prs29102004.pdf] — no ISIN traded as GAS within 15 days of 2004-12-10
- 2004-12-10 nifty500 include L.G. Balakrishnan & Bros Ltd. (METALS) [ind_prs29102004.pdf] — no ISIN traded as METALS within 15 days of 2004-12-10
- 2004-12-10 nifty500 include Larsen & Toubro Ltd. (ENGINEERING) [ind_prs29102004.pdf] — no ISIN traded as ENGINEERING within 15 days of 2004-12-10
- 2004-12-10 nifty500 include Monnet Ispat Ltd. STEEL AND (STEEL) [ind_prs29102004.pdf] — no ISIN traded as STEEL within 15 days of 2004-12-10
- 2004-08-30 nifty500 exclude Eicher Ltd Automobiles 4 wheelers (None) [ind_prs26082004.pdf] — the release prints no symbol
- 2004-08-30 nifty500 include United Phosphorus Ltd Pesticides & Agrochemicals (None) [ind_prs26082004.pdf] — the release prints no symbol
- 2004-08-27 nifty500 exclude Global Trust Bank Bank (None) [ind_prs25082004.pdf] — the release prints no symbol
- 2004-08-27 nifty500 include Dredging Corp of India Miscellaneous (None) [ind_prs25082004.pdf] — the release prints no symbol
- 2004-07-09 nifty500 exclude Ashok Leyland Finance Ltd Finance (None) [ind_prs30062004.pdf] — the release prints no symbol
- 2004-07-09 nifty500 include Welspun India Ltd Textile Products Ltd (None) [ind_prs30062004.pdf] — the release prints no symbol
- 2004-06-14 nifty500 exclude Hind Lever Chemicals Ltd. Fertilisers (None) [ind_prs09062004.pdf] — the release prints no symbol
- 2004-06-14 nifty500 include Welspun Guj Stahl Rohren Ltd. Steel & Steel Products (None) [ind_prs09062004.pdf] — the release prints no symbol
- 2004-05-24 nifty50 exclude Larsen & Toubro Ltd. Diversified (None) [ind_prs19052004.pdf] — the release prints no symbol
- 2004-05-24 nifty50 include Punjab & National Bank Bank (None) [ind_prs19052004.pdf] — the release prints no symbol
- 2004-05-24 niftynext50 exclude Punjab & National Bank Bank (None) [ind_prs19052004.pdf] — the release prints no symbol
- 2004-05-24 niftynext50 include Vijaya Bank Bank (None) [ind_prs19052004.pdf] — the release prints no symbol
- 2004-05-24 nifty500 exclude Larsen & Toubro Ltd. Diversified (None) [ind_prs19052004.pdf] — the release prints no symbol
- 2004-05-24 nifty500 include UCO Bank Bank (None) [ind_prs19052004.pdf] — the release prints no symbol
- 2004-05-20 nifty500 exclude Larsen & Toubro Ltd. Diversified National Stock Exchange of India Ltd. http://www.nseindia.com/[29-03-2011 16:26:19] (None) [ind_prs18052004.pdf] — the release prints no symbol
- 2004-04-16 nifty500 exclude Lakshmi Auto Components Ltd Auto Ancillaries (None) [ind_prs15042004.pdf] — the release prints no symbol
- 2004-04-16 nifty500 include Rane Brake Lining Ltd Auto Ancillaries (None) [ind_prs15042004.pdf] — the release prints no symbol
- 2004-04-12 nifty50 exclude Digital Globalsoft Ltd. Computer Software (None) [ind_prs26032004.pdf] — the release prints no symbol
- 2004-04-12 nifty50 include Oil & Natural Gas Corporation Ltd. Oil Exploration/Production (None) [ind_prs26032004.pdf] — the release prints no symbol
- 2004-04-12 nifty500 exclude Digital Globalsoft Ltd. Computer Software (None) [ind_prs26032004.pdf] — the release prints no symbol
- 2004-04-12 nifty500 include Munjal Auto Industries Ltd Auto Ancillaries (None) [ind_prs26032004.pdf] — the release prints no symbol
- 2004-03-22 nifty500 exclude Jai Prakash Industries Ltd. Construction - Civil (None) [ind_prs19032004.pdf] — the release prints no symbol
- 2004-03-22 nifty500 include Rane Engine Valves Ltd. Auto Ancillaries (None) [ind_prs19032004.pdf] — the release prints no symbol
- 2004-03-15 nifty500 exclude Balaji Distilleries Ltd. Brew / Distilleries (None) [ind_prs12032004.pdf] — the release prints no symbol
- 2004-03-15 nifty500 include Indo Rama Synthetics Ltd. Textiles Synthetic (None) [ind_prs12032004.pdf] — the release prints no symbol
- 2004-01-28 nifty500 exclude Centurion Bank Ltd. Bank (None) [ind_prs27012004.pdf] — the release prints no symbol
- 2004-01-28 nifty500 include Jindal Vijayanagar Steel Ltd. Steel & Steel Products (None) [ind_prs27012004.pdf] — the release prints no symbol
- 2004-03-01 nifty50 exclude Glaxosmithkline Consumer Healthcare Ltd. (None) [ind_prs16012004.pdf] — the release prints no symbol
- 2004-03-01 nifty50 include Bharti Tele-Ventures Ltd. (None) [ind_prs16012004.pdf] — the release prints no symbol
- 2004-03-01 niftynext50 exclude Bharti Tele-Ventures Ltd. (None) [ind_prs16012004.pdf] — the release prints no symbol
- 2004-03-01 niftynext50 include Canara Bank (None) [ind_prs16012004.pdf] — the release prints no symbol
- 2004-03-01 nifty500 exclude Bank of Rajasthan Bank (None) [ind_prs16012004.pdf] — the release prints no symbol
- 2004-03-01 nifty500 include Maruti Udyog Ltd. Automobiles 4 Wheelers (None) [ind_prs16012004.pdf] — the release prints no symbol
- 2004-01-02 nifty500 exclude Mukand Ltd. Steel and Steel Products (None) [ind_prs01012004.pdf] — the release prints no symbol
- 2004-01-02 nifty500 include Swaraj Mazda Ltd. Automobiles - 4 Wheelers (None) [ind_prs01012004.pdf] — the release prints no symbol
- 2003-12-11 nifty500 exclude Indian Aluminium Ltd. Aluminium (None) [ind_prs10122003.pdf] — the release prints no symbol
- 2003-12-11 nifty500 include Indo Gulf Fertilisers Ltd. Fertilisers (None) [ind_prs10122003.pdf] — the release prints no symbol
- 2003-12-05 nifty500 exclude United Phosphorous Ltd Pesticides & Agrochemicals (None) [ind_prs03122003.pdf] — the release prints no symbol
- 2003-12-05 nifty500 include State Trading Corpn. of India Ltd Trading (None) [ind_prs03122003.pdf] — the release prints no symbol
- 2003-11-21 nifty500 exclude Bayer Cropscience India Ltd Pesticides & Agrochemicals (None) [ind_prs19112003.pdf] — the release prints no symbol
- 2003-11-21 nifty500 include Divi’s Laboratories Ltd. Pharmaceuticals (None) [ind_prs19112003.pdf] — the release prints no symbol
- 2003-11-19 nifty500 exclude Standard Industries Ltd Diversified (None) [ind_prs18112003.pdf] — the release prints no symbol
- 2003-11-19 nifty500 include Amtex Auto Ltd Auto Ancillaries (None) [ind_prs18112003.pdf] — the release prints no symbol
- 2003-09-05 nifty500 exclude German Remedies Ltd Pharmaceuticals (None) [ind_prs04092003.pdf] — the release prints no symbol
- 2003-09-05 nifty500 include Shriram Transport Finance Co. Ltd Finance (None) [ind_prs04092003.pdf] — the release prints no symbol
- 2003-08-11 nifty500 exclude Whirlpool of India Ltd Consumer Durable (None) [ind_prs08082003.pdf] — the release prints no symbol
- 2003-08-11 nifty500 include Allahabad Bank Ltd Banks (None) [ind_prs08082003.pdf] — the release prints no symbol
- 2003-06-17 nifty500 exclude Bharat Hotels Ltd Hotels (None) [ind_prs16062003.pdf] — the release prints no symbol
- 2003-06-17 nifty500 include Canara Bank Ltd. Banks (None) [ind_prs16062003.pdf] — the release prints no symbol
- 2003-05-23 nifty500 exclude Indo Rama Synthetics (India) Ltd. Textile – Synthetics (None) [ind_prs21052003.pdf] — the release prints no symbol
- 2003-05-23 nifty500 include Punjab Mohta Polytex Ltd. Textile - Cotton (None) [ind_prs21052003.pdf] — the release prints no symbol
- 2003-04-25 nifty500 exclude Madura Coats Ltd. Textile – Cotton (None) [ind_prs22042003.pdf] — the release prints no symbol
- 2003-04-25 nifty500 include Jindal Iron & Steel Co. Ltd. Steel & Steel Products (None) [ind_prs22042003.pdf] — the release prints no symbol
- 2003-05-02 nifty50 include National Aluminium Co. Ltd. (None) [ind_prs13032003.pdf] — the release prints no symbol
- 2003-03-19 niftynext50 include Steel Authority of India Ltd. (None) [ind_prs13032003.pdf] — the release prints no symbol
- 2003-04-01 niftynext50 include I-Flex Sloution Ltd. (None) [ind_prs13032003.pdf] — the release prints no symbol
- 2003-04-07 niftynext50 include TVS Motor Co Ltd. (None) [ind_prs13032003.pdf] — the release prints no symbol
- 2003-05-02 niftynext50 include Mphasis BFL Ltd. (None) [ind_prs13032003.pdf] — the release prints no symbol
- 2003-02-25 nifty500 exclude Kinetic Motor Co. Ltd Automobiles 2 & 3 Wheelers (None) [ind_prs12022003.pdf] — the release prints no symbol
- 2003-02-25 nifty500 include Macmillian India Ltd. Printing & Publishing (None) [ind_prs12022003.pdf] — the release prints no symbol
- 2003-01-21 nifty500 exclude J. K. Corp Ltd. Diversified (None) [ind_prs16012003.pdf] — the release prints no symbol
- 2003-01-21 nifty500 include Mahavir Spinning Mills Ltd. Textiles Cotton (None) [ind_prs16012003.pdf] — the release prints no symbol

## Unreadable releases / sections

- ind_prs17102016.pdf: Nifty Next 50 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs17102016.pdf: Nifty 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs17102016.pdf: Nifty 200 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs17102016.pdf: Nifty 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs17102016.pdf: Nifty Midcap 150 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs12082016.pdf: Nifty Next 50 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs12082016.pdf: Nifty 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs12082016.pdf: Nifty 200 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs12082016.pdf: Nifty 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs12082016.pdf: Nifty Midcap 150 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs12082016.pdf: Nifty Smallcap 250 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs22022016_2.pdf: Nifty 50 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs22022016_2.pdf: Nifty Next 50 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs22022016_2.pdf: Nifty 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs22022016_2.pdf: Nifty 200 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs22022016_2.pdf: Nifty 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs27022014.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs27022014.pdf: CNX 200 Index: no effective date stated for the section
- ind_prs27022014.pdf: CNX 500 Index: no effective date stated for the section
- ind_prs27082013.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs27082013.pdf: CNX 200 Index: no effective date stated for the section
- ind_prs27082013.pdf: CNX 500 Index: no effective date stated for the section
- ind_prs13022013.pdf: CNX Nifty Index: no effective date stated for the section
- ind_prs13022013.pdf: CNX Nifty Junior Index: no effective date stated for the section
- ind_prs13022013.pdf: CNX 100 Index: no effective date stated for the section
- ind_prs13022013.pdf: CNX 200 Index: no effective date stated for the section
- ind_prs13022013.pdf: CNX 500 Index: no effective date stated for the section
- ind_prs16082012.pdf: CNX Nifty Junior: the 'exclude' statement has no table of its own (detached layout)
- ind_prs16052012.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs04042012.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs14032012.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs14032012.pdf: CNX 100 Index: no effective date stated for the section
- ind_prs14032012.pdf: CNX 200 Index: no effective date stated for the section
- ind_prs14032012.pdf: S&P CNX 500 Index: no effective date stated for the section
- ind_prs01122011.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs01122011.pdf: CNX 200 Index: no effective date stated for the section
- ind_prs25082011.pdf: S&P CNX Nifty Index: no effective date stated for the section
- ind_prs25082011.pdf: CNX Nifty Junior Index: no effective date stated for the section
- ind_prs25082011.pdf: CNX 100 Index: no effective date stated for the section
- ind_prs25082011.pdf: S&P CNX 500 Index: no effective date stated for the section
- ind_prs19042011.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs01102010.pdf: CNX Nifty Junior: the 'exclude' statement has no table of its own (detached layout)
- ind_prs01102010.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs01102010.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs18082010.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs18082010.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs18082010.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs04032010.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs24022010.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs24022010.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs24022010.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs24022010.pdf: S&P CNX 500 Index: the 'include' statement has no table of its own (detached layout)
- ind_prs19022010.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs16122009.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs09092009.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs09092009.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs09092009.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs09092009.pdf: S&P CNX 500 Index: the 'include' statement has no table of its own (detached layout)
- ind_prs04092009.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs04092009.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs04092009.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs04092009.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs27072009.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs18062009.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs19052009.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs19052009.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs19052009.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs19052009.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs06052009.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs16042009.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs01042009.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs20022009.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs10022009.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs10022009.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs10022009.pdf: CNX 100 Index: the 'include' statement has no table of its own (detached layout)
- ind_prs07012009.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs07012009.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs07012009.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs07012009.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs26112008.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs31102008.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs18092008.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs29072008.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs29072008.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs29072008.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs29072008.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs16052008.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs09052008.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs09052008.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs21042008.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs11042008.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs26022008.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs26022008.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs26022008.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs11022008.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs31012008_2.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs31012008_2.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs31012008_2.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs31012008_1.pdf: CNX NIFTY Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs31012008_1.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs31012008_1.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs23012008.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs20122007.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs27112007.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs08112007.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs30102007.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs30102007.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs30102007.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs22102007.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs12092007.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs12092007.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs12092007.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs06092007_1.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs10082007.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs10082007.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs10082007.pdf: S&P CNX 500 Index: the 'include' statement has no table of its own (detached layout)
- ind_prs24052007.pdf: S&P CNX 500 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs17052007.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs17052007.pdf: CNX 100 Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs20022007.pdf: S&P CNX Nifty Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs20022007.pdf: CNX Nifty Junior Index: the 'exclude' statement has no table of its own (detached layout)
- ind_prs20022007.pdf: CNX 100 Index: the 'include' statement has no table of its own (detached layout)
- ind_prs20022007.pdf: S&P CNX 500 Index: the 'include' statement has no table of its own (detached layout)
- ind_prs07082006.pdf: S&P CNX 500: the 'exclude' statement has no table of its own (detached layout)
- ind_prs26092003.pdf: S&P CNX 500: no effective date stated for the section
- ind_prs08092003.pdf: S&P CNX 500: no effective date stated for the section
- ind_prs13032003.pdf: S&P CNX 500: the 'exclude' statement has no table of its own (detached layout)
- ind_prs13032003.pdf: S&P CNX Nifty: no effective date stated for the section
- ind_prs13032003.pdf: CNX Nifty Junior: no effective date stated for the section
- ind_prs02032012.pdf: ind_prs02032012.pdf: not a PDF (no %PDF header) — a soft-404 or a gate
- ind_prs20062005_1.pdf: ind_prs20062005_1.pdf: encrypted PDF this platform cannot open: cryptography>=3.1 is required for AES algorithm
