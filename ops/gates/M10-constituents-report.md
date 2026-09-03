# M10.1 — index-constituents ingest coverage

**As-of snapshot:** 2026-09-03
**Coverage:** 2 broad + 14 sectoral/thematic queryable (bar: 1 broad + 8 sectoral) — **PASS**
**Sweep:** 16 published, 0 skipped (already in L1), 1 parked

| Slug | Index | Category | Status | Rows | Park cause |
| --- | --- | --- | --- | --- | --- |
| nifty500 | NIFTY 500 | broad | published | 500 |  |
| nifty50 | NIFTY 50 | broad | published | 50 |  |
| niftybank | NIFTY BANK | sectoral | published | 14 |  |
| niftyit | NIFTY IT | sectoral | published | 10 |  |
| niftyauto | NIFTY AUTO | sectoral | published | 15 |  |
| niftypharma | NIFTY PHARMA | sectoral | published | 20 |  |
| niftyfmcg | NIFTY FMCG | sectoral | published | 15 |  |
| niftymetal | NIFTY METAL | sectoral | published | 15 |  |
| niftyrealty | NIFTY REALTY | sectoral | published | 10 |  |
| niftymedia | NIFTY MEDIA | sectoral | published | 10 |  |
| niftypsubank | NIFTY PSU BANK | sectoral | published | 12 |  |
| niftyprivatebank | NIFTY PRIVATE BANK | sectoral | parked |  | gated |
| niftyfinance | NIFTY FINANCIAL SERVICES | sectoral | published | 20 |  |
| niftyhealthcare | NIFTY HEALTHCARE INDEX | sectoral | published | 20 |  |
| niftyconsumerdurables | NIFTY CONSUMER DURABLES | sectoral | published | 13 |  |
| niftyenergy | NIFTY ENERGY | thematic | published | 40 |  |
| niftyinfra | NIFTY INFRASTRUCTURE | thematic | published | 30 |  |

## Parked slugs
- **niftyprivatebank** (gated): ParseError: ind_niftyprivatebanklist_20260903.csv: body is markup, not CSV — the site's Angular shell answered a bad path with HTML and a 200; it must not become an index membership
