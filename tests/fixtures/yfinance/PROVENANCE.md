# yfinance reference-A fixtures (golden CA suite, M2.7)

Cached Yahoo Finance windows — **reference A** of the two independent references the golden
corporate-action suite demands (EXECUTION_PLAN §4.3; ratified B2 in AGENTIC_CONTEXT §2). Reference
B (`tests/golden/`) is our own factor engine checked against hand-computed literals; reference A is
Yahoo's independently-compiled adjusted series for the `.NS` ticker. The offline cross-check
(`tests/golden/test_yfinance_reference.py`) reads only these files, so it is reproducible with no
network (AGENTIC_CONTEXT B8).

Fetched on **2026-09-02** via `yfinance` 1.5.2 (`Ticker.history(auto_adjust=False, actions=True)`,
an ~8-calendar-day window either side of each ex-date). Each JSON holds the raw/adjusted close
series plus the corporate actions (`splits`, `dividends`) Yahoo attributes to the window — exactly
what Yahoo serves — with prices stored as strings and read back as `Decimal` (money is never
`float`).

Regenerate (opt-in, live network) with:

```bash
uv run pytest tests/golden/test_yfinance_reference.py -m network
```

The pull is marked `network` and skipped unless positively selected, so a bare `uv run pytest` and
`make check` never reach the network (`tests/golden/conftest.py`).

## The cases and their `.NS` tickers

| Case | Ticker | Ex-date | What Yahoo shows |
|---|---|---|---|
| `irctc_split_2021` | `IRCTC.NS` | 2021-10-28 | split `5.0` ⇔ our `price_factor` `0.2`; series continuous |
| `ril_bonus_2024` | `RELIANCE.NS` | 2024-10-28 | 1:1 bonus as split `2.0` ⇔ our `0.5`; series continuous |
| `hdfc_merger_2023` | `HDFCBANK.NS` | 2023-07-13 | surviving entity: no action, continuous; both agree factor 1 |
| `jiofin_demerger_2023` | `RELIANCE.NS` | 2023-07-20 | **no action recorded**; break not bridged — documented disagreement |
| `tatamotors_dvr_2024` | `TMPV.NS` | 2024-09-02 | **no action recorded** for the DVR conversion — documented disagreement |
| `tatamotors_demerger_2025` | `TMPV.NS` | 2025-10-14 | **no action**; a spurious ~-40% ex-day crash Yahoo never adjusts — documented disagreement |
| `ltim_merger_2022` | `LTIM.NS` | 2022-11-24 | **reference A unavailable** — see below |

### Tata Motors ticker note

Tata Motors' Oct-2025 demerger renamed the listed entity. The passenger-vehicle company retained
ISIN `INE155A01022` and the full legacy price history, and Yahoo carries that history under
`TMPV.NS` (the old `TATAMOTORS.NS` symbol was retired; `TMCV.NS` is the *new* commercial-vehicle
listing, with data only from Nov-2025). Both Tata golden cases key on `INE155A01022`, so both use
`TMPV.NS`.

### LTIMindtree gap (`ltim_merger_2022`)

Yahoo serves **no data** for `LTIM.NS` in this environment: `history()` returns zero rows for every
range (recent and full) and the symbol search returns nothing, while peer NSE large caps
(`RELIANCE.NS`, `TCS.NS`, `INFY.NS`, `HDFCBANK.NS`) return data in the same call. LTIMindtree was
formed by the 2022-11-24 merger under test, and Yahoo's coverage of the renamed line is absent.
Rather than fabricate a series — which would defeat the independence the cross-check exists for —
the fixture is an **unavailability marker** (`"available": false`) carrying the reasoned verdict:
reference B is authoritative for this case. A surviving-entity merger carries a unit price factor,
so the reference-B literals remain the golden truth and are not weakened by the missing cross-check.
If Yahoo restores coverage, re-run the live pull and delete the entry from
`REFERENCE_A_UNAVAILABLE`.

These are third-party market-data snapshots kept only as test fixtures; they are never imported by
product code and never used as a price source (reference A exists solely to cross-check reference B).
