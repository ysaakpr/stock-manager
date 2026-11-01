# NSE F&O UDiFF bhavcopy — 2026-10-26 excerpt for an ambiguous identity master (added 2026-11-01)

**Excerpts**, not whole files: the header and a few data lines of each, copied byte-for-byte (line
endings included) from the lake's L0 payloads for the session. Nothing in a kept line was edited;
lines were only dropped.

| Excerpt | Copied from (L0, the single CSV member of the zip) | L0 zip sha256 | Lines kept |
|---|---|---|---|
| `BhavCopy_NSE_FO_0_0_0_20261026_F_0000_EXCERPT.csv` | `nse_fo_bhavcopy/2026/10/BhavCopy_NSE_FO_0_0_0_20261026_F_0000.csv.zip` (7,578,167-byte member) | `16a528b1e7f7ba1cfaf4d1e60846ca30f143f8d4f9d47a857d1573a2c5829ec1` | header, `KOTAKBANK` OCT and NOV futures, `RELIANCE` OCT and NOV futures, `NIFTY` OCT future |
| `BhavCopy_NSE_CM_0_0_0_20261026_F_0000_EXCERPT.csv` | `nse_bhavcopy_udiff/2026/10/BhavCopy_NSE_CM_0_0_0_20261026_F_0000.csv.zip` | `723bee076946c104b9cd56d09ca9b3776b062f19c136acc289c7c279290377e3` | header, `KOTAKBANK` EQ, `RELIANCE` EQ |

The F&O file's `ISIN` column is empty on every derivative row, so a stock underlier's ISIN comes
from the identity master, and the session's cash bhavcopy is the only same-session evidence
(`INE237A01036` for `KOTAKBANK`). KOTAKBANK's real reissue (`INE237A01028` → `INE237A01036`,
`isin_lineage`, 2026-01-14) gives the two ISINs a broken master refresh would leave with two open
windows. The master is constructed in the test; the files are real. Hand check for the KOTAKBANK
aggregate: total OI 33,104,000 + 122,186,000 = 155,290,000; basis = OCT settle 439.85 − spot
440.75 = −0.90. `tests/unit/test_deals_fo_ambiguity.py` and `tests/unit/test_fo_backfill.py` pin it.
