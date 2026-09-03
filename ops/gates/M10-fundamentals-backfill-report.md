# M10.4 — Fundamentals backfill: build + live-verification report

**Status: PARKED on an upstream (M7.3) dependency defect.** The runner is built and unit-verified;
the live campaign cannot land real facts until the M7.3 XBRL path is corrected to match the real NSE
feed. Details below.

## What was built

- `dataplatform/ingest/fundamentals_backfill.py` — the resumable, checkpointed two-phase runner
  (discovery index → per-filing XBRL → `write_pit`), mirroring `backfill.py` / `corp_actions_backfill.py`:
  resume from `sync_state`, commit-per-unit checkpoint, `--dry-run`, `--limit` (index sample),
  `--max-filings` (bulk-half sample), and a 403 spike that **parks** with an enumerated `ParkReason`.
- `tests/unit/test_fundamentals_backfill.py` — 8 offline tests covering every acceptance criterion
  (real facts land keyed by ISIN+filing_date; PIT correctness + restatement-as-new-record;
  resume; 403-park; coverage report; out-of-universe skip; pure planning; sample cap). All green.
- `ops/runbooks/fundamentals_backfill.md` — operator runbook.

`uv run pytest tests/unit/test_fundamentals_backfill.py -q` → 8 passed. `ruff` + `mypy --strict`
clean on both deliverable files.

## Live source verification (B1 "verify the URL patterns for real")

Both Source Register rows were exercised against the live host from this machine on 2026-09-03:

| Source | URL | Result |
|---|---|---|
| `nse_financial_results_index` | `.../api/corporates-financial-results?index=equities&period=Quarterly&from_date=..&to_date=..` | `200`, `application/json`, real filing records returned |
| `nse_xbrl_filing` | `https://nsearchives.nseindia.com/corporate/xbrl/<file>.xml` | `200`, `application/xml`, real XBRL returned |

The runner drove the discovery phase end to end against the live index (session warmed, payload
landed in L0, parse attempted).

## The block — two M7.3 defects: the parsers were built against fabricated fixtures

The live feed does not match the vocabulary or the identity scheme the M7.3 fixtures encoded, so
M7.3's DONE parsers reject real filings:

1. **Index nature vocabulary.** The real `corporates-financial-results` feed states
   `consolidated: "Non-Consolidated"` (= standalone) and `"Consolidated"`; it also states
   `audited: "Un-Audited"`. `dataplatform/ingest/xbrl/discovery.py::_nature` only accepts
   `"Standalone"`/`"Consolidated"` and raises `ParseError` on `"Non-Consolidated"`. (`_optional_audited`
   silently returns `None` on `"Un-Audited"` — cosmetic, non-blocking.)

2. **XBRL entity identity (the hard one).** The real NSE XBRL identifies the reporting entity by
   **NSE symbol**, not ISIN — `<xbrli:identifier scheme="http://www.nseindia.com/NSESymbol">KANANIIND`
   — and the document carries **no ISIN element at all**. M7.3's `parser.py` assumes the entity
   identifier *is* the ISIN: it reads that text into `_Context.isin`, uses it as `Filing.isin` and
   every `FundamentalFact.isin` (both `ISIN_PATTERN`-validated), and cross-checks it against the
   index ISIN. Against a real filing this raises
   `"index says this filing is INE… but the document's entity identifier is KANANIIND"`, and even
   past the cross-check a symbol would fail `ISIN_PATTERN`. The M7.3 fixtures fabricated the
   identifier as `<...identifier scheme="http://www.nseindia.com/">INE467B01029`, so the suite never
   caught this.

Correcting M7.3 means resolving the document's NSE symbol to an ISIN through the D2 master as-of the
filing date (invariant #2) — or making the index-supplied ISIN authoritative — plus re-freezing the
fixtures from real filings and updating M7.3's tests. That changes M7.3's parser contract and is
consumed by M7.4 as well, so it is beyond M10.4's blast radius (AGENTIC_CONTEXT §7): parked naming
M7.3 rather than silently worked around.

## What happens after M7.3 is fixed

Nothing in the M10.4 runner needs to change: it already passes the index ISIN to `parse(isin=…)` and
calls `write_pit`. Once M7.3 yields real ISINs, rerun the bounded sample, confirm real facts land,
then run the full window under the owner's go.
