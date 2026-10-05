# `nse_pr_bundle` fixtures — provenance

Four frozen bundles, one per format era measured by the W2 Phase 1 smoke fetch on **2026-09-08**
(39 requests, ≥2.5 s spacing, host lease held). Full evidence:
[`ops/studies/evidence/nse-pr-bundle.md`](../../../ops/studies/evidence/nse-pr-bundle.md).

| fixture dir | session | archive file | era characteristics |
|---|---|---|---|
| `ix_era/`     | 2010-01-04 | `PR040110.zip` | first bundle the archive serves; `Ix` present; `Bc` uppercase `DDMMYY`, dates `DD/MM/YYYY` |
| `classic/`    | 2013-01-02 | `PR020113.zip` | `Ix` gone, no `mcap` |
| `mcap_upper/` | 2024-07-01 | `PR010724.zip` | `mcap` present; `Bc010724` (`DDMMYY`) and `MCAP01072024` (`DDMMYYYY`) **in one zip** |
| `lowercase/`  | 2026-09-04 | `PR040926.zip` | all-lowercase names, `DDMMYYYY` throughout, `Bc` dates `YYYY-MM-DD` |

## Four more, for the `ffix` member (added 2026-09-08)

`ffix` — dated index constituent membership with free-float weightage, registered in `MemberKind`
as "Fixed income" until 2026-09-08 and never opened because of it. These were **reduced from the
authoritative lake**, not fetched: the whole corpus was already in `data/L0`, so this cost zero
requests. Each zip holds that bundle's `ffix` member plus the readme, byte-for-byte.

| fixture dir | session | archive file | why this session is an era edge |
|---|---|---|---|
| `ffix_three_index/`     | 2010-01-04 | `PR040110.zip` | the first bundle: NIFTY / JR. NIFTY / CNX 100 only, S&P-era banners, and the six all-blank separator rows plus three banners that make the furniture rule load-bearing |
| `ffix_seventeen_index/` | 2011-01-31 | `PR310111.zip` | the seven sectoral indices arrive; all 17 `INDEX_FLG` values present in one file |
| `ffix_renamed_banner/`  | 2013-03-04 | `PR040313.zip` | the NIFTY banner renames `S&P CNX Nifty Sec.` → `CNX Nifty Sec.`; `INDEX_FLG` is unchanged |
| `ffix_banner_relapse/`  | 2013-04-09 | `PR090413.zip` | the old banner returns for exactly one session before the rename sticks |

**The `ffix` format itself never changes across the 827 files** — one header shape, one row shape,
stable `INDEX_FLG` strings. These four are frozen because that is a claim which has to keep being
true, and only the last two are format events at all (both cosmetic, both in the banner the reader
deliberately does not key on). `manifest.json` in each records the `ffix` member's own sha256, and
`tests/unit/test_pr_bundle_ffix.py` re-hashes it, so a fixture that drifts fails rather than
quietly re-baselining.

## These zips are trimmed, and the trim is the only edit

Each fixture zip contains the members the parsers read (`Bc`/`bc`, `Ix`, `MCAP`/`mcap`) plus the
bundle's readme, **byte-for-byte as NSE published them**. No row was truncated, reordered or
rewritten; a format regression in any parsed member still fires. The other 11-22 members
(`fo`, `cd`, `op`, `tt`, `pr`, …) were dropped because W2 parses none of them and keeping them
would put ~1.5 MB of unread payload in the repo.

Because the member *list* is therefore not the real one, each directory carries a
`manifest.json` recording the original bundle's byte count, sha256 and **complete** member list as
served. `tests/unit/test_pr_bundle_bundle.py` runs the member-registry assertions against those
manifests, so `MemberKind` is checked against every name the real archive produced, not against
the reduced set in the zip.

The unmodified originals are in the worktree lake at `data/L0/nse_pr_bundle/<YYYY>/<MM>/`, which is
gitignored — `data/` is never committed.

## Tests never hit the network

Every test in `tests/unit/test_pr_bundle_*.py` reads these files from disk. Nothing in
`dataplatform/ingest/nse/pr_bundle/` opens a socket; fetching is the crawl engine's job alone.

## Eight more, for the `bh` member (added 2026-09-29, X2 H2)

`bh` — the securities that hit a daily price band, the source H2 of
`ops/studies/preregistration-signals-2026-09-29.md` names. **Reduced from the authoritative lake**,
not fetched: zero requests. Each zip holds that bundle's `bh` member plus its readme, byte-for-byte;
`manifest.json` records the original bundle and the `bh` member's own sha256, which
`tests/unit/test_pr_bundle_bh.py` re-hashes.

The eras were found by sniffing the member in all 4,124 bundles in L0, not by sampling:

| fixture dir | session | archive file | why |
|---|---|---|---|
| `bh_sr/`         | 2010-01-04 | `PR040110.zip` | `SR` header, CRLF, `DDMMYYYY` member name, four- and five-cell rows under a five-cell header |
| `bh_index_flag/` | 2010-08-11 | `PR110810.zip` | `INDEX FLAG` actually filled on some rows |
| `bh_sr_padded/`  | 2010-09-17 | `PR170910.zip` | no `INDEX FLAG` in the header; header and rows padded to ten cells |
| `bh_sr_swapped/` | 2010-11-09 | `PR091110.zip` | the one file with `HIGH/LOW` and `SECURITY` swapped |
| `bh_series/`     | 2013-07-01 | `PR010713.zip` | `SERIES` header, LF, `DDMMYY` name, four-cell rows; the long 2012-2025 era |
| `bh_empty/`      | 2021-05-17 | `PR170521.zip` | header and no rows |
| `bh_no_flag/`    | 2026-09-04 | `PR040926.zip` | the 2025-10 lowercase era: four-cell header |
| `bh_misserved/`  | 2018-01-02 | `PR020118.zip` | NSE serves the **2019-01-02** bundle under this name; `PrBundle` must refuse to date it. The original carries no readme, so the zip is the `bh` member alone |

## Five more, for fund lineage (added 2026-10-05, `dq-etf-isin-lineage`)

`fund_unit_splits/` — the `Bc` evidence `dataplatform.identity.fund_lineage` reads to accept (or
refuse) an ETF's ISIN switch as a unit split. **Reduced from the authoritative lake**, not fetched:
zero requests. Each zip holds that bundle's `Bc` member plus its readme, byte-for-byte;
`manifest.json` records the original bundle's size and sha256 and the `Bc` member's own sha256,
which `tests/unit/test_fund_lineage.py` re-hashes.

| archive file | session | why |
|---|---|---|
| `PR111219.zip` | 2019-12-11 | first broadcast of the 2019-12-19 BeES unit splits (GOLDBEES, BANKBEES, …): ex-date on the old ISIN's last session |
| `PR150221.zip` | 2021-02-15 | the 2021-02-17 HDFC/UTI splits (UTISXN50 among them): ex-date on the old ISIN's second-to-last session |
| `PR160720.zip` | 2020-07-16 | AXISNIFTY's 2020-07-23 split is broadcast, but the units skipped 2020-07-24 — the gap rejects it |
| `PR290124.zip` | 2024-01-29 | HDFCNIFIT's 2024-02-02 split is here; HDFCSENSEX, which switched ISIN the same day, is in no `Bc` file |
| `PR210225.zip` | 2025-02-21 | HDFCLIQUID's `CHANGE IN ATTRIBUTE` on its switch — an event, but not a unit re-basing |
