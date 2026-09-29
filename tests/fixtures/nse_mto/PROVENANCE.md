# `nse_mto` fixtures — Security Wise Delivery Position (MTO)

Real files, fetched from `https://nsearchives.nseindia.com/archives/equities/mto/MTO_{DDMMYYYY}.DAT`
on 2026-09-05. Not modelled, not trimmed — the bytes the archive served.

| File | Session | Bytes | Why this session |
|---|---|---|---|
| `MTO_22062011.DAT` | 2011-06-22 | 53,666 | W3: the first session of the bhavcopy's ISIN era (E2), the floor of the deep backfill. One `N` block. Fetched 2026-09-29 by the W3 smoke. |
| `MTO_01072011.DAT` | 2011-07-01 | 53,888 | W3: the first **multi-block** file seen — a `D` block (one GC row) then an `N` block, each with its own `Trade Date` line and column header. Fetched 2026-09-29 by the W3 smoke. |
| `MTO_01092016.DAT` | 2016-09-01 | 60,408 | W3: the last session of the deep-backfill range, the day before the platform's original first price session. One `N` block. Fetched 2026-09-29 by the W3 smoke. |
| `MTO_02092016.DAT` | 2016-09-02 | 59,639 | The first price session in this platform's lake. Only MTO reaches it. |
| `MTO_27092019.DAT` | 2019-09-27 | 64,115 | The last session `sec_bhavdata_full` does **not** serve — the era edge. |
| `MTO_20062024.DAT` | 2024-06-20 | 95,260 | A mid-history session, for a format that has grown ~60% in rows since 2016. |
| `MTO_07082026.DAT` | 2026-08-07 | 118,158 | Both sources serve it, and `../nse_delivery/sec_bhavdata_full_07082026.csv` is the other half — which is what makes the two-source agreement testable offline. |

## Why this source exists at all

`sec_bhavdata_full` is the modern delivery file and its archive begins **2019-09-30**; every earlier
session 404s. That boundary was found by probing, not from documentation, and it is a boundary in
*our sourcing* rather than in the data: MTO carries the same facts and reaches back past the start
of this platform's price history, so the delivery series has no hole once both eras are used.

## The agreement, which the era join depends on

Measured on 2026-08-07 and pinned by `tests/unit/test_mto.py`: every `(symbol, series)` key present
in both files reports an **identical** delivery quantity, and MTO values keys the modern file leaves
blank while leaving none blank that it fills. If the two ever disagreed, splicing the eras would put
a visible seam in the delivery history at 2019-09-30, and a delivery-based factor would read that
seam as a change in the market rather than a change in our sourcing.

## Format trap

The column header names six fields; a data row carries **seven**. "Name of Security" is spent on two
fields — symbol then series — so splitting on the header count shears the series off every row, and
`(symbol, series)` is the file's key.

## Settlement blocks (W3)

A file is one or more settlement blocks. Of the 769 payloads in the lake on 2026-09-29 (10 of them
2011-2016 smoke samples), 768 carry one `N` block and one — 2011-07-01 — carries `D` + `N`, with
no key in both. So "multi-block" is a property of a session, not a contiguous era, and the parser
segments every file the same way. A key restated identically by a second block is kept once; one
given different figures by two blocks is refused (`tests/unit/test_mto.py`, the double-count test).
