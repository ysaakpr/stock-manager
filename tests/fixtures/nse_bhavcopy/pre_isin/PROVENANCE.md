# NSE bhavcopy fixtures — era E1, the pre-ISIN archive (1995-01-02 → 2011-06-21)

Real files, fetched from the live archive through the M1.2 crawl engine
(`dataplatform.ingest.fetcher.Fetcher`) under the `nse_bhavcopy_legacy` register policy — browser
UA, `Referer: https://www.nseindia.com/`, ≥2.5 s host spacing, lease held on
`nsearchives.nseindia.com` — during the **W1 Phase 1 smoke fetch on 2026-09-08**. Each landed in L0
first and was copied here byte-for-byte, so the checksums below are the L0 sidecar checksums.

URL pattern (`source_register.yaml` → `nse_bhavcopy_legacy`, the same template E2 uses — the eras
differ in the file's columns, not in its address):
`https://nsearchives.nseindia.com/content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip`

| Session | File | Bytes | sha256 | Data rows | Why this one |
|---|---|---|---|---|---|
| 2006-01-02 | `cm02JAN2006bhav.csv.zip` | 26,077 | `7380a1ef8f266fe3118b26f89779554db42ab19e98ea90516fcb6800b47271cd` | 876 | sub-era **E1a**: `TIMESTAMP` day is *not* zero-padded (`2-JAN-2006`) |
| 2011-06-21 | `cm21JUN2011bhav.csv.zip` | 42,395 | `493a17115313710ffe25ffdd3b24cbbbbcc4442dc0ba998c4d89d116a66cb0ea` | 1,503 | **the last session ever published without an ISIN column** |

Both carry the era's 12-field header — eleven names and the trailing comma that is part of the
format:

```
SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,
```

No `TOTALTRADES`. No `ISIN`. That second absence is the whole reason this directory exists.

## The boundary these fixtures pin

`cm21JUN2011bhav.csv.zip` here and `../legacy/cm22JUN2011bhav.csv.zip` are consecutive sessions and
they are different formats. Measured, not believed — the two were fetched in the same run, minutes
apart, and their byte counts match the study's independent bisection of 2026-09-07 exactly:

| | 2011-06-21 | 2011-06-22 |
|---|---|---|
| columns | 11 + trailing comma | 13 + trailing comma |
| `ISIN` | absent | `INE144J01019` for `20MICRONS` |
| bytes | 42,395 | 53,301 |
| rows | 1,503 | 1,502 |
| era | **E1** `pre_isin` | **E2** `legacy` |

`tests/unit/test_bhavcopy_eras.py` asserts that pair against these files, so the cutover cannot
move by a day — in either direction — without a test going red. That is the point: a boundary that
drifts one session silently sends a day of ISIN-bearing prices into quarantine, or a day of
identity-less rows into `prices_raw`.

## What may and may not be done with these rows

`bhavcopy_legacy.parse` **refuses** this header, on purpose, and that refusal is not weakened
anywhere. `bhavcopy_legacy.parse_pre_isin` is the only reader for it, and it cannot return a
`PriceRow` — it returns `UnidentifiedRow`, which the L1 writer lands in `prices_raw_quarantine`
under `reason = isin_column_absent`.

There is no symbol→ISIN mapping for this era and none may be added here. The only resolver
available today is a current-day listing (`EQUITY_L.csv`), and every company delisted before today
is simply absent from it — so resolving E1 through one would be survivorship-biased in exactly the
direction a backtest cares about. ISINs are also *reissued*: `20MICRONS` is `INE144J01019` on
2011-06-22 and `INE144J01027` by 2016, so even a correct point-in-time map would need ISIN
*lineage*, not merely an ISIN column. Resolving E1 is W4 identity work and a separate funded
decision.

What these files are, then, is **retention**: the prices exist, immutably, in L0 and in this
fixture, and the platform states honestly how many rows it cannot key rather than guessing at them.
