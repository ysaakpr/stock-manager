# NSE board-meeting intimation fixtures (`nse_board_meetings`, M17.16)

A **trimmed subset** of a real payload, fetched from `www.nseindia.com` under the
`nse_board_meetings` register policy (browser UA, `Referer: https://www.nseindia.com/`, warm
session cookie, one leased fetcher) on **2026-10-20 09:37 IST**:

`https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date=20-10-2026&to_date=20-11-2026`

The full response (777,612 B, 1,001 records, sha256 `162675a5…b1c1`, the register's
`sample_sha256`) was cut to 17 records, each copied unchanged, so the parser tests see the real
field spellings (`sm_indusrty`, `oriiginalMeetingDate`) and value formats:

| File | Bytes | sha256 | Records |
|---|---|---|---|
| `json_v1/corporate_board_meetings_20261020_20261120.json` | 14614 | `ec6814a40f6aa0a87471065117c84299c23fd4d71baed6966396d9b2c6b6ce67` | 17 |

What the subset covers:

- **Results rows with both purposes.** ABB, APARINDS, INFY and MPHASIS each have a
  "Financial Results…" row and a "Board Meeting Intimation" row for the same meeting.
- **Results named only in the description.** CENTRUM, GILLETTE, MUKANDLTD and PGHH carry the
  generic purpose "Board Meeting Intimation" and say "financial results" only in `bm_desc`.
- **Not results.** MANBA (fund raising, two near-identical intimations four seconds apart) and
  GENUSPOWER (other business).
- **After the 20:00 IST cut-off.** MPHASIS (20:25, 20:34) and CUMMINSIND (20:05) were disseminated
  after the nightly capture's fire time, so they are knowable the next day.

One era so far (`json_v1`): the feed has had one shape since it was first read (2026-10-20).
