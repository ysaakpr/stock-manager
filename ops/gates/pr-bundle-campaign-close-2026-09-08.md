# W2 Phase 2 close-out — what the PR-bundle campaign acquired, 2026-09-08

**Worktree** `w2/campaign-close` cut from `main@24baf9c` · **lake** `/home/ubuntu/stock-manager/data/L0`,
asserted before any analysis · **campaign** finished 12:50 IST today (requested 4124, fetched 4084,
already in L0 40, no-bundle 0, failed 0, hard-stopped false, 1,545,550,489 bytes, 4,084 requests,
lease released) · **nothing was promoted and nothing was deleted.**

Everything below is measured, not asserted. The measurements come from
`dataplatform.ingest.nse.pr_bundle.survey` (new in this branch, read-only, 35 unit cases),
`dataplatform.store.l0_compare` (new, read-only, 12 unit cases) and `L0Store.verify_checksums`
(existing). The command that regenerates the whole body verbatim is in the appendix.

---

## The headline, in five lines

1. **We now hold 144,281 dated corporate actions in raw form over 2010-01-04 .. 2026-09-04**
   (1,565,554 row-broadcasts across 4,107 readable `Bc` members). Against them, the
   `corporate_actions` table's 47,887 rows all carry one `knowable_date` of 2026-09-07. The raw
   corpus is the surface that makes invariant #7 non-vacuous for history; **it is not promoted**,
   and §7 says exactly why it cannot be yet.
2. **The reconcile is clean in both directions and can be shown to fail.** 4,124 bundles for 4,124
   calendar-expected dates: 0 missing, 0 unexpected, no interior gap. Three injected corruptions,
   one per direction plus the one the W1 closer's shape could not detect, all fire (§4).
3. **The lake is intact.** 102,868 payloads re-hashed, **0 defects**. The count is 40 above the
   brief's expectation and §5 decomposes it exactly — the brief's arithmetic, not the lake.
4. **Both stranded worktree lakes are fully duplicated.** 31 payloads re-hashed, every one present
   in the authoritative lake under the same key with an identical sha256, 0 held only in a
   worktree. Nothing deleted (§6).
5. **⚠ One unplanned finding, and it is the largest thing in this report.** `MemberKind.FFIX` is
   registered as *"Fixed income"* and is actually the **free-float index constituent file**: daily
   NIFTY 50, CNX 100 and (from 2010-10-11) CNX 500 membership **with weightage**, on 825 sessions
   over 2010-01-04 .. 2013-04-30, 709,286 rows, already in L0. `ops/BACKLOG.md:126` records that
   no dated membership history exists to fetch; three years of it has been sitting in the bundles
   since the first one. §3c measures it, §7 restates the limit.

**Two things the brief asked me to confirm came back refuted in part, and one confirmed:**

| Phase 1 claim | verdict over the full corpus |
|---|---|
| `Ix` published on every 2010 session, vanishing between 2010-10-04 and 2010-10-18, never returning through 2026 | **Refuted in part.** The last *readable* `Ix` is **2010-10-08** (pinned; the bracket closes). But the member *reappears* once, on **2013-02-19**, shipped as a zip container — so "never returned" is wrong, though what it returns is the `ffix` payload under an `Ix` name (§3a, §3c). |
| `Ix` rotating index set, NIFTY 50 never appearing | **Confirmed about `Ix`** — 6 index names ever, no NIFTY 50. **But the claim does not generalise to the bundle:** `ffix` carries NIFTY 50 from the first bundle onward (§3c). |
| `mcap` arrives 2024-02-01 | **Confirmed, exactly.** First `mcap` member is 2024-02-01, and every bundle after it carries one — no interior gap across 640 bundles. |
| `mcap` Category never anything but Listed/Permitted | **Confirmed across 1,673,899 rows**, not a sample. |

**⚠ One caveat to line 2, found by the bundle reader rather than by the reconcile.**
`PR020118.zip` and `PR020119.zip` are **byte-identical**: the archive served the 2019-01-02 bundle
for the 2018-01-02 URL, and every member inside the 2018 payload is named `020119`. A file does
exist under the 2018-01-02 key, so the calendar reconcile is right to call it present and
*structurally cannot* see this — it was `PrBundle`'s filename-versus-members cross-check that
caught it. **The true number of distinct published bundles is therefore 4,123, not 4,124**, and
2018-01-02 has no bundle of its own. Every digest in the corpus was grouped to check for more of
the same shape and there is exactly this one pair — measured, in §0, not spot-checked.

**18 bundle/member parse failures**, all in existing readers on main, listed in §0. Four bundles
cannot be opened at all (members disagreeing about their own date, an undated bundle, a
name/payload session mismatch), so their `Bc` is unread; eleven `Bc` members and one `Ix` member
fail on payloads the 39-request Phase 1 sample never hit. **Not repaired here** — this task is
measurement, and each is a defect in a reader that a follow-up should fix against the exact date
and line named. See the appendix for the list of follow-ups this report generates.

---

## 0. What was measured, and from where

- **Resolved L0 root:** `/home/ubuntu/stock-manager/data/L0` (asserted before any analysis).
- **Range:** 2010-01-04 .. 2026-09-04.
- **Bundles enumerated from L0:** 4124, spanning 2010-01-04 .. 2026-09-04, 1,561.8 MB.
- **Writes performed:** none. No L1, no Postgres, no `sync_state`, no `corporate_actions`, no `quality_flag`.

### ⚠ 18 bundle/member parse failure(s)

| date | file | member | message |
|---|---|---|---|
| 2010-08-30 | `PR300810.zip` | `bc` | ParseError: Bc300810.csv:911: expected 10 columns, got 11: ['N1', 'ICIBK1107', 'Regular Income Bond', ' Opti', '27/09/2010', ' ', ' ', '24/09/2010', ' ', ' ', 'INTEREST PAYMENT         '] |
| 2010-08-31 | `PR310810.zip` | `bc` | ParseError: Bc310810.csv:915: expected 10 columns, got 11: ['N1', 'ICIBK1107', 'Regular Income Bond', ' Opti', '27/09/2010', ' ', ' ', '24/09/2010', ' ', ' ', 'INTEREST PAYMENT         '] |
| 2010-09-01 | `PR010910.zip` | `bc` | ParseError: Bc010910.csv:939: expected 10 columns, got 11: ['N1', 'ICIBK1107', 'Regular Income Bond', ' Opti', '27/09/2010', ' ', ' ', '24/09/2010', ' ', ' ', 'INTEREST PAYMENT         '] |
| 2010-09-02 | `PR020910.zip` | `bc` | ParseError: Bc020910.csv:963: expected 10 columns, got 11: ['N1', 'ICIBK1107', 'Regular Income Bond', ' Opti', '27/09/2010', ' ', ' ', '24/09/2010', ' ', ' ', 'INTEREST PAYMENT         '] |
| 2010-09-03 | `PR030910.zip` | `bc` | ParseError: Bc030910.csv:962: expected 10 columns, got 11: ['N1', 'ICIBK1107', 'Regular Income Bond', ' Opti', '27/09/2010', ' ', ' ', '24/09/2010', ' ', ' ', 'INTEREST PAYMENT         '] |
| 2010-09-06 | `PR060910.zip` | `bc` | ParseError: Bc060910.csv:1062: expected 10 columns, got 11: ['N1', 'ICIBK1107', 'Regular Income Bond', ' Opti', '27/09/2010', ' ', ' ', '24/09/2010', ' ', ' ', 'INTEREST PAYMENT         '] |
| 2010-09-07 | `PR070910.zip` | `bc` | ParseError: Bc070910.csv:1099: expected 10 columns, got 11: ['N1', 'ICIBK1107', 'Regular Income Bond', ' Opti', '27/09/2010', ' ', ' ', '24/09/2010', ' ', ' ', 'INTEREST PAYMENT         '] |
| 2011-08-19 | `PR190811.zip` | `(zip)` | ParseError: PR190811.zip: members disagree about the bundle's date (Gl190811.csv=2011-08-19, An190811.txt=2011-08-19, Bm190811.txt=2011-08-19, Tt190811.csv=2011-08-19, HL190811.csv=2011-08-19, ffix190811.csv=2011-08-19, NPD190811.txt=2011-08-19, RPD190811.csv=2011-08-19, Rtt190811.csv=2011-08-19, bh19082011.csv=2011-08-19, fo19082011.zip=2011-08-19, NPD180811.txt=2011-08-18, etf190811.csv=2011-08-19, Pd190811.csv=2011-08-19, Pr190811.csv=2011-08-19, Bc190811.csv=2011-08-19); a bundle whose own members cannot agree what session they are must not date a corporate action |
| 2013-01-10 | `PR100113.zip` | `(zip)` | ParseError: PR100113.zip: no member carries a date in its name, so the bundle cannot be dated from its own contents; members: nupr100113/, nupr100113/An100113.txt, nupr100113/Bc100113.csv, nupr100113/Bm100113.txt, nupr100113/Gl100113.csv, nupr100113/HL100113.csv, nupr100113/NPD100113.txt, nupr100113/Pd100113.csv, nupr100113/Pr100113.csv, nupr100113/RPD100113.csv, nupr100113/Rdm_help.txt, nupr100113/Readme.txt, nupr100113/Rtt100113.csv, nupr100113/Tt100113.csv, nupr100113/bh100113.csv, nupr100113/cd10012013.zip, nupr100113/corpbond100113.csv, nupr100113/etf100113.csv, nupr100113/ffix100113.csv, nupr100113/fo10012013.zip, nupr100113/nuver.txt, nupr100113/rdm.docx, nupr100113/sme100113.csv |
| 2013-02-19 | `PR190213.zip` | `ix` | the Ix member is a zip container, not a CSV (it holds ffix190213.csv); ix.py hands the compressed bytes to csv.reader, which raises a bare _csv.Error about an embedded newline |
| 2013-06-04 | `PR040613.zip` | `(zip)` | ParseError: PR040613.zip: members disagree about the bundle's date (An040613.txt=2013-06-04, Bc040613.csv=2013-06-04, bh040613.csv=2013-06-04, Bm040613.txt=2013-06-04, corpbond040613.csv=2013-06-04, etf040613.csv=2013-06-04, fo04062013.zip=2013-06-04, Gl040613.csv=2013-06-04, HL040613.csv=2013-06-04, NPD040613.txt=2013-06-04, Pd040613.csv=2013-06-04, Pr040613.csv=2013-06-04, RPD030613.csv=2013-06-03, Rtt040613.csv=2013-06-04, sme040613.csv=2013-06-04, Tt040613.csv=2013-06-04, cd04062013.zip=2013-06-04); a bundle whose own members cannot agree what session they are must not date a corporate action |
| 2016-04-29 | `PR290416.zip` | `bc` | ParseError: Bc290416.csv:2: RECORD_DT is '03-05-2016', which is neither DD/MM/YYYY nor YYYY-MM-DD |
| 2018-01-02 | `PR020118.zip` | `(zip)` | ParseError: PR020118.zip: archive filename says 2018-01-02 but its members say 2019-01-02; the payload and its name are not the same session |
| 2019-10-11 | `PR111019.zip` | `bc` | ParseError: Bc111019.csv:27: expected 10 columns, got 11: ['BE', 'TCS', 'TATA CONSULTANCY SERV LTD', '18/10/2019', ' ', ' ', '17/10/2019', ' ', ' ', 'INT DIV-RS 5', ' SPL DIV-RS '] |
| 2019-10-14 | `PR141019.zip` | `bc` | ParseError: Bc141019.csv:15: expected 10 columns, got 11: ['BE', 'TCS', 'TATA CONSULTANCY SERV LTD', '18/10/2019', ' ', ' ', '17/10/2019', ' ', ' ', 'INT DIV-RS 5', ' SPL DIV-RS '] |
| 2019-10-15 | `PR151019.zip` | `bc` | ParseError: Bc151019.csv:28: expected 10 columns, got 11: ['BE', 'TCS', 'TATA CONSULTANCY SERV LTD', '18/10/2019', ' ', ' ', '17/10/2019', ' ', ' ', 'INT DIV-RS 5', ' SPL DIV-RS '] |
| 2022-01-10 | `PR100122.zip` | `bc` | ParseError: Bc100122.csv: empty response body |
| 2024-08-21 | `PR210824.zip` | `bc` | ParseError: Bc210824.csv:162: expected 10 columns, got 11: ['BE', 'SURYAROSNI', 'Surya Roshni Ltd', '23/08/2024', ' ', ' ', '23/08/2024', ' ', ' ', 'DIV - RS 2', '50 PER SH     '] |

### ⚠ 1 payload(s) served under more than one date key

The archive substituted another session's bundle. **The calendar reconcile is structurally blind to this** — a file does exist under the key, so §4 is right to call the date present — and only the payload's own member names disagree, which is what `PrBundle`'s filename cross-check caught. Checked across every digest in range, not spot-checked.

| sha256 | date keys |
|---|---|
| `553598a532395e01…` | `nse_pr_bundle/2018-01-02/PR020118.zip`, `nse_pr_bundle/2019-01-02/PR020119.zip` |

**So 4,124 bundles are present but only 4,123 distinct sessions were published.**

## 1. Per-year availability

`sessions` and `muhurat` are the shipped `nse_holidays.yaml` calendar's expectation for the year; `bundles` is what L0 holds, enumerated from the payload filenames. Every other column counts the bundles of that year carrying that member family.

| year | sessions | muhurat | bundles | an | bc | bh | bm | cd | cf | ffix | fo | gl | hl | ix | npd | op | pd | pr | rpd | rtt | tt | etf | co | corpbond | sme | mcap | pe_ |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2010 | 250 | 1 | 251 | 251 | 251 | 251 | 251 | 244 | 244 | 251 | 251 | 251 | 251 | 193 | 251 | 251 | 251 | 251 | 251 | 251 | 251 | 209 | 44 | 0 | 0 | 0 | 0 |
| 2011 | 246 | 1 | 247 | 246 | 246 | 246 | 246 | 241 | 241 | 246 | 246 | 246 | 246 | 0 | 246 | 34 | 246 | 246 | 246 | 246 | 246 | 246 | 241 | 60 | 0 | 0 | 0 |
| 2012 | 246 | 1 | 247 | 247 | 247 | 247 | 247 | 243 | 183 | 247 | 247 | 247 | 247 | 0 | 246 | 0 | 247 | 247 | 247 | 247 | 247 | 247 | 183 | 247 | 70 | 0 | 0 |
| 2013 | 248 | 1 | 249 | 247 | 247 | 247 | 247 | 243 | 0 | 81 | 247 | 247 | 247 | 1 | 247 | 0 | 247 | 247 | 246 | 246 | 247 | 247 | 0 | 247 | 247 | 0 | 0 |
| 2014 | 242 | 1 | 243 | 243 | 243 | 243 | 243 | 237 | 0 | 0 | 243 | 243 | 243 | 0 | 243 | 0 | 243 | 243 | 243 | 243 | 243 | 243 | 0 | 243 | 243 | 0 | 0 |
| 2015 | 246 | 1 | 247 | 247 | 247 | 247 | 247 | 242 | 0 | 0 | 247 | 247 | 247 | 0 | 0 | 0 | 247 | 247 | 0 | 0 | 247 | 247 | 0 | 247 | 247 | 0 | 0 |
| 2016 | 246 | 1 | 247 | 247 | 247 | 247 | 247 | 242 | 0 | 0 | 247 | 247 | 247 | 0 | 0 | 0 | 247 | 247 | 0 | 0 | 247 | 247 | 0 | 247 | 247 | 0 | 0 |
| 2017 | 247 | 1 | 248 | 248 | 248 | 248 | 248 | 243 | 0 | 0 | 248 | 247 | 248 | 0 | 0 | 0 | 248 | 248 | 0 | 0 | 248 | 248 | 0 | 248 | 248 | 0 | 0 |
| 2018 | 245 | 1 | 246 | 245 | 245 | 245 | 245 | 235 | 0 | 0 | 240 | 245 | 245 | 0 | 0 | 0 | 245 | 245 | 0 | 0 | 245 | 245 | 0 | 245 | 245 | 0 | 0 |
| 2019 | 244 | 1 | 245 | 245 | 245 | 245 | 245 | 0 | 0 | 0 | 0 | 245 | 245 | 0 | 0 | 0 | 245 | 245 | 0 | 0 | 245 | 245 | 0 | 245 | 245 | 0 | 0 |
| 2020 | 250 | 1 | 251 | 251 | 251 | 251 | 251 | 0 | 0 | 0 | 0 | 251 | 251 | 0 | 0 | 0 | 251 | 251 | 0 | 0 | 251 | 251 | 0 | 251 | 251 | 0 | 0 |
| 2021 | 247 | 1 | 248 | 248 | 248 | 248 | 248 | 0 | 0 | 0 | 0 | 248 | 248 | 0 | 0 | 0 | 248 | 248 | 0 | 0 | 248 | 248 | 0 | 248 | 248 | 0 | 0 |
| 2022 | 247 | 1 | 248 | 248 | 248 | 248 | 248 | 0 | 0 | 0 | 0 | 248 | 247 | 0 | 0 | 0 | 248 | 248 | 0 | 0 | 248 | 248 | 0 | 248 | 248 | 0 | 0 |
| 2023 | 245 | 1 | 246 | 246 | 246 | 246 | 246 | 0 | 0 | 0 | 0 | 246 | 246 | 0 | 0 | 0 | 246 | 246 | 0 | 0 | 246 | 246 | 0 | 246 | 246 | 0 | 0 |
| 2024 | 245 | 1 | 246 | 246 | 246 | 246 | 246 | 0 | 0 | 0 | 0 | 246 | 246 | 0 | 0 | 0 | 246 | 246 | 0 | 0 | 246 | 246 | 0 | 246 | 246 | 225 | 225 |
| 2025 | 247 | 1 | 248 | 248 | 248 | 248 | 248 | 0 | 0 | 0 | 0 | 248 | 248 | 0 | 0 | 0 | 248 | 248 | 0 | 0 | 248 | 248 | 0 | 248 | 248 | 248 | 56 |
| 2026 | 167 | 0 | 167 | 167 | 167 | 167 | 167 | 0 | 0 | 0 | 0 | 167 | 167 | 0 | 0 | 0 | 167 | 167 | 0 | 0 | 167 | 167 | 0 | 167 | 167 | 167 | 0 |
| **total** | **4108** | **16** | **4124** | 4120 | 4120 | 4120 | 4120 | 2170 | 668 | 825 | 2216 | 4119 | 4119 | 194 | 1233 | 285 | 4120 | 4120 | 1233 | 1233 | 4120 | 4078 | 468 | 3683 | 3446 | 640 | 281 |

### First and last appearance per member family

| member | registered | bundles | first seen | last seen | interior gaps |
|---|:--:|---:|---|---|---|
| `an` | yes | 4120 | 2010-01-04 | 2026-09-04 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1); 2018-01-02..2018-01-02 (1) |
| `bc` | yes | 4120 | 2010-01-04 | 2026-09-04 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1); 2018-01-02..2018-01-02 (1) |
| `bh` | yes | 4120 | 2010-01-04 | 2026-09-04 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1); 2018-01-02..2018-01-02 (1) |
| `bm` | yes | 4120 | 2010-01-04 | 2026-09-04 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1); 2018-01-02..2018-01-02 (1) |
| `cd` | yes | 2170 | 2010-01-04 | 2018-12-21 | 2010-01-07..2010-01-07 (1); 2010-03-16..2010-03-16 (1); 2010-04-01..2010-04-01 (1); 2010-05-27..2010-05-27 (1); 2010-08-19..2010-08-19 (1); 2010-09-30..2010-09-30 (1); +42 more |
| `cf` | yes | 668 | 2010-01-04 | 2012-09-28 | 2010-01-07..2010-01-07 (1); 2010-03-16..2010-03-16 (1); 2010-04-01..2010-04-01 (1); 2010-05-27..2010-05-27 (1); 2010-08-19..2010-08-19 (1); 2010-09-30..2010-09-30 (1); +9 more |
| `ffix` | yes | 825 | 2010-01-04 | 2013-04-30 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1) |
| `fo` | yes | 2216 | 2010-01-04 | 2018-12-21 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1); 2018-01-02..2018-01-02 (1) |
| `gl` | yes | 4119 | 2010-01-04 | 2026-09-04 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1); 2017-06-22..2017-06-22 (1); 2018-01-02..2018-01-02 (1) |
| `hl` | yes | 4119 | 2010-01-04 | 2026-09-04 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1); 2018-01-02..2018-01-02 (1); 2022-12-09..2022-12-09 (1) |
| `ix` | yes | 194 | 2010-01-04 | 2013-02-19 | 2010-10-11..2013-02-18 (587) |
| `npd` | yes | 1233 | 2010-01-04 | 2014-12-31 | 2011-08-19..2011-08-19 (1); 2012-05-30..2012-05-30 (1); 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1) |
| `op` | yes | 285 | 2010-01-04 | 2011-02-18 | none |
| `pd` | yes | 4120 | 2010-01-04 | 2026-09-04 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1); 2018-01-02..2018-01-02 (1) |
| `pr` | yes | 4120 | 2010-01-04 | 2026-09-04 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1); 2018-01-02..2018-01-02 (1) |
| `rpd` | yes | 1233 | 2010-01-04 | 2014-12-31 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1); 2013-05-24..2013-05-24 (1); 2013-06-04..2013-06-04 (1) |
| `rtt` | yes | 1233 | 2010-01-04 | 2014-12-31 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1); 2013-05-20..2013-05-20 (1); 2013-06-04..2013-06-04 (1) |
| `tt` | yes | 4120 | 2010-01-04 | 2026-09-04 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1); 2018-01-02..2018-01-02 (1) |
| `etf` | yes | 4078 | 2010-03-08 | 2026-09-04 | 2011-08-19..2011-08-19 (1); 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1); 2018-01-02..2018-01-02 (1) |
| `co` | yes | 468 | 2010-10-29 | 2012-09-28 | 2011-02-16..2011-02-16 (1); 2011-04-01..2011-04-04 (2); 2011-05-17..2011-05-17 (1); 2011-08-19..2011-08-19 (1); 2011-09-30..2011-09-30 (1); 2012-02-16..2012-02-16 (1); +2 more |
| `corpbond` | yes | 3683 | 2011-10-03 | 2026-09-04 | 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1); 2018-01-02..2018-01-02 (1) |
| `sme` | yes | 3446 | 2012-05-30 | 2026-09-04 | 2012-05-31..2012-09-17 (76); 2013-01-10..2013-01-10 (1); 2013-06-04..2013-06-04 (1); 2018-01-02..2018-01-02 (1) |
| `mcap` | yes | 640 | 2024-02-01 | 2026-09-04 | none |
| `pe_` | yes | 281 | 2024-02-01 | 2025-03-21 | none |

Families shipping more than one file per bundle in some era: `fo` (2,501 files over 2,216 bundles). Every count in this report is per **bundle**, so `fo04012010.csv` sitting beside `fo04012010.doc` is one day of availability and not two.

**Member names matching no `MemberKind`** (a format change, surfaced not raised):

- `rdm.doc.docx` — 2 bundle(s)
- `rdm.rtf` — 1 bundle(s)
- `cap120412.csv` — 1 bundle(s)
- `cap090512.csv` — 1 bundle(s)
- `cap240513.csv` — 1 bundle(s)
- `rdm.dot` — 1 bundle(s)
- `Gl220617-.csv` — 1 bundle(s)
- `MA091222.csv` — 1 bundle(s)

Documentation members shipped inside the bundles: `readme.txt` (4114), `nuver.txt` (2084), `rdm.doc` (1474), `rdm_help.txt` (1234), `rdm.docx` (604), `help.txt` (285), `readmenew.txt` (6).

## 2. The headline — dated corporate actions (`Bc`)

- **Row-broadcasts:** 1,565,554 across 4,107 bundles.
- **Distinct actions** (symbol + series + purpose text + all four published dates): **144,281**.
- **Distinct symbols:** 18,149. **Distinct purpose strings:** 5,823.
- **Knowable-date span:** 2010-01-04 .. 2026-09-04 — the broadcast dates, derived from each bundle's own members and never from a clock.
- **Ex-date span:** 2009-12-30 .. 2026-09-18; 1,363,213 of 1,565,554 row-broadcasts carry an ex-date.
- 254,218 row-broadcasts have `knowable_date > ex_date` — re-broadcasts of an action already gone ex, which is why the distinct-action count keeps the *minimum* broadcast date.

### Against the `corporate_actions` table

| | rows | distinct knowable dates | span |
|---|---:|---:|---|
| `corporate_actions` today | 47,887 | 1 | 2026-09-07 only — every row stamped with ingest day, which is what makes invariant #7 vacuously satisfied |
| `nse_pr_bundle` raw, this corpus | 144,281 distinct actions (1,565,554 broadcasts) | 17 years of distinct broadcast dates | 2010-01-04 .. 2026-09-04 |

**We now hold 144,281 dated corporate actions in raw form over 2010-01-04..2026-09-04** — each carrying the date the market could first have known it, as opposed to the single ingest-day stamp on all 47,887 promoted rows. Nothing has been promoted; see §7.

### Row-broadcasts and distinct actions per year

| year | row-broadcasts | distinct actions first broadcast that year |
|---:|---:|---:|
| 2010 | 77,878 | 5,055 |
| 2011 | 88,370 | 7,376 |
| 2012 | 97,279 | 8,239 |
| 2013 | 100,131 | 7,724 |
| 2014 | 95,072 | 7,759 |
| 2015 | 116,754 | 8,217 |
| 2016 | 102,010 | 7,804 |
| 2017 | 125,564 | 8,949 |
| 2018 | 117,444 | 8,418 |
| 2019 | 121,889 | 8,932 |
| 2020 | 118,029 | 9,178 |
| 2021 | 117,113 | 8,951 |
| 2022 | 102,310 | 9,201 |
| 2023 | 44,933 | 8,843 |
| 2024 | 48,768 | 9,278 |
| 2025 | 34,385 | 8,994 |
| 2026 | 57,625 | 11,363 |
| **total** | **1,565,554** | **144,281** |

### Distribution by purpose

`PURPOSE` is free text and one row routinely carries two facts ("AGM AND DIVIDEND RS 4"), so a row is given every tag whose keywords appear and **the counts below overlap**. This is a report-only keyword pass over `BcRow.purpose`, which is kept byte-for-byte as published; the classification a promotion needs must parse terms, not spot words.

| tag | row-broadcasts | share of rows |
|---|---:|---:|
| AGM | 805,746 | 51.5% |
| DIVIDEND | 731,319 | 46.7% |
| INTEREST | 253,620 | 16.2% |
| REDEMPTION | 142,000 | 9.1% |
| BONUS | 12,733 | 0.8% |
| SCHEME | 5,548 | 0.4% |
| BUYBACK | 5,350 | 0.3% |
| EGM | 4,221 | 0.3% |
| SPLIT | 3,235 | 0.2% |
| RIGHTS | 2,992 | 0.2% |
| CAPITAL_REDUCTION | 1,246 | 0.1% |
| OPEN_OFFER | 234 | 0.0% |
| _(no tag matched)_ | 51,468 | 3.3% |

Top 15 raw purpose strings verbatim:

| purpose | row-broadcasts |
|---|---:|
| `ANNUAL GENERAL MEETING` | 273,845 |
| `INTEREST PAYMENT` | 248,360 |
| `Annual General Meeting` | 124,507 |
| `REDEMPTION` | 85,662 |
| `DIV/REDEMPTION` | 44,645 |
| `DIV/STP` | 34,086 |
| `STP` | 30,259 |
| `INTERIM DIVIDEND` | 23,819 |
| `DIVIDEND` | 15,419 |
| `AGM/DIV-RE 1 PER SHARE` | 14,010 |
| `AGM/DIV-RE.1/- PER SHARE` | 10,903 |
| `AGM/DIV-RE 0.50 PER SHARE` | 8,119 |
| `AGM/DIV-RS 1.50 PER SHARE` | 7,821 |
| `AGM/DIV-RS 2 PER SHARE` | 7,574 |
| `AGM/DIV-RS.2/- PER SHARE` | 7,046 |

Series distribution: `EQ` 508,035, `BE` 499,142, `MF` 178,775, `ME` 20,706, `GS` 16,465, `SM` 15,587, `SQ` 15,585, `ST` 13,505, `GC` 10,424, `BL` 8,926, `IQ` 8,450, `N1` 7,093, `N5` 6,844, `U1` 6,714, `N0` 6,611.

## 3. Phase 1 findings, re-tested over the full corpus

### 3a. `Ix` — "2010 only, rotating, never NIFTY 50"

- The member is **present** in **194** of 4,124 bundles, 2010-01-04 .. 2013-02-19.
- `ix.py` **parses** it in **193** of them, 2010-01-04 .. 2010-10-08 — 104,709 constituent rows, 529 distinct symbols.
- Bundles after 2010 with the member present: **1** — 2013-02-19.

**Refuted in part.** Phase 1 said `Ix` vanished between 2010-10-04 and 2010-10-18 and never returned through 2026. The disappearance is real and now pinned exactly — the last readable `Ix` is 2010-10-08 — but *never returned* is wrong: the member reappears on 2013-02-19, shipped as a **zip container** rather than a CSV, which is why a reader-only count would have missed it. See the parse-failure table in §0 and §3c.

Distinct index names ever seen in a **readable** `Ix`, with per-index coverage:

| index | sessions carrying it | first | last | rows |
|---|---:|---|---|---:|
| `BANK Nifty` | 37 | 2010-01-04 | 2010-02-25 | 444 |
| `CNX 500` | 193 | 2010-01-04 | 2010-10-08 | 96,500 |
| `CNX IT` | 37 | 2010-01-04 | 2010-02-25 | 740 |
| `CNX Infrastructure` | 59 | 2010-07-19 | 2010-10-08 | 1,475 |
| `CNX Midcap` | 37 | 2010-01-04 | 2010-02-25 | 3,700 |
| `Nifty Midcap 50` | 37 | 2010-01-04 | 2010-02-25 | 1,850 |

NIFTY 50 in a readable `Ix`: **no** — which confirms the Phase 1 claim *about `Ix`*, and is not the same claim as "the bundle has no NIFTY 50 membership". §3c is that claim, and it is false.

Index banners announced with no constituent rows: `CNX Infra` (59), `CNX MIDCAP` (37), `NIFTY MIDCAP 50` (37).

### 3b. `mcap` — arrival at 2024-02-01, and the issue-size series

- Carried by **640** bundles; **first seen 2024-02-01**, last seen 2026-09-04. **Confirmed** — Phase 1 pinned 2024-02-01 by bisection and the full corpus agrees exactly.
- 1,673,899 rows, **3,499 distinct symbols**.
- **Issue Size** (shares outstanding) present and non-zero on 1,673,899 rows covering 3,499 symbols; 0 rows publish 0. The series is therefore dense: every row of every bundle from arrival onward carries one.
- 3,305 rows carry NSE's literal `Not Traded` in `Last Trade Date` — a security with no trading history, not a missing value.

`Category` values across every `mcap` row in the corpus:

| category | rows |
|---|---:|
| `Listed` | 1,656,951 |
| `Permitted` | 16,948 |

Category outside Listed/Permitted: **no** — confirming the docstring's claim across 1.7 M rows rather than across a sample.

No interior gap: every bundle from its arrival onward carries `mcap`.

### 3c. ⚠ UNPLANNED FINDING — `ffix` is not fixed income, it is the free-float index file

`MemberKind.FFIX` is registered with the docstring *"Fixed income"*. The payload is nothing of the kind. Its header is:

```text
INDEX_FLG,SYMBOL,SERIES,SECURITY,ISSUE_CAP,INVESTIBLE_FACTOR,CLOSE_PRIC,FF_MKT_CAP,WEIGHTAGE
```

— a **dated, daily index-membership file with free-float weightage**, present on **825** bundles and censused on 825 of them (2010-01-04 .. 2013-04-30), 709,286 constituent rows.

This was not in the brief and is reported because it bears directly on deliverable 3 and on limit (iii): **`NIFTY` — the 50 — is in the very first bundle**, 2010-01-04, and in every one after it until the member stops. The census below is a count of the `INDEX_FLG` column and the header; **nothing was parsed into rows and nothing was promoted**. A reader for this member does not exist and writing one was out of scope.

| index | sessions carrying it | first | last | rows |
|---|---:|---|---|---:|
| `BANK Nifty` | 788 | 2010-02-26 | 2013-04-30 | 9,456 |
| `CNX 100` | 825 | 2010-01-04 | 2013-04-30 | 82,500 |
| `CNX 500` | 632 | 2010-10-11 | 2013-04-30 | 316,000 |
| `CNX ENERGY` | 555 | 2011-01-31 | 2013-04-30 | 5,550 |
| `CNX FMCG` | 555 | 2011-01-31 | 2013-04-30 | 8,325 |
| `CNX IT` | 788 | 2010-02-26 | 2013-04-30 | 15,760 |
| `CNX Infrastructure` | 632 | 2010-10-11 | 2013-04-30 | 15,800 |
| `CNX MNC` | 555 | 2011-01-31 | 2013-04-30 | 8,325 |
| `CNX Midcap` | 788 | 2010-02-26 | 2013-04-30 | 78,800 |
| `CNX PHARMA` | 555 | 2011-01-31 | 2013-04-30 | 5,550 |
| `CNX PSE` | 555 | 2011-01-31 | 2013-04-30 | 11,100 |
| `CNX PSU BANK` | 555 | 2011-01-31 | 2013-04-30 | 6,660 |
| `CNX Realty` | 691 | 2010-07-19 | 2013-04-30 | 6,910 |
| `CNX SERVICE` | 555 | 2011-01-31 | 2013-04-30 | 16,650 |
| `JR. NIFTY` | 825 | 2010-01-04 | 2013-04-30 | 41,250 |
| `NIFTY` | 825 | 2010-01-04 | 2013-04-30 | 41,250 |
| `Nifty Midcap 50` | 788 | 2010-02-26 | 2013-04-30 | 39,400 |

Distinct header shapes across all 825 censused members: **1**.

Members shipped as a zip container rather than a CSV: `ix` (1). On 2013-02-19 the `Ix` member is `Ix190213.zip`, and its single inner file is `ffix190213.csv` — **byte-identical** (sha256 `e24860243ced492e…`) to that bundle's own top-level `ffix` member. So the 2013 "return of `Ix`" is the `ffix` payload published twice under two names, not a resumption of the 2010 `Ix` format.

**What this does and does not give us.** It gives a daily NIFTY 50 / CNX 100 / CNX 500 membership series with weightage over 2010-01-04..2013-04-30 — 825 sessions — which is more than the "handful of anchor points" limit (iii) was drafted to describe. It does **not** give membership after the member stops, it is symbol-keyed like everything else here (limit ii), and it has no reader. Limit (iii) is restated accordingly in §7.

## 4. Calendar reconcile, both directions

The served set is **enumerated from L0** — `/home/ubuntu/stock-manager/data/L0/nse_pr_bundle` globbed for `PR*.zip` and each date read from the archive filename. It is not derived from a `SessionPlan`, from a sidecar, or from the calendar, because a served set built out of the calendar-shaped plan cannot lose a date the calendar expects, which is what made the W1 closer's second direction structurally incapable of failing.

`2010-01-04..2026-09-04: 4124 expected, 4124 observed, 0 missing, 0 unexpected`

**(a) calendar says trading session, no bundle in L0 — 0 date(s):**

_none_

**(b) bundle in L0, calendar says closed — 0 date(s):**

_none_

### Can this check fail? Injected proof

A check that has never failed may be a check that cannot. Each row below corrupts one side of the reconcile, re-runs it, and records whether the matching direction fired. Every injection is discarded when its run ends — the calendar object is copied, not mutated, and the served set is rebuilt from L0.

| direction | injection | fired? | detail |
|---|---|:--:|---|
| control (no injection) | nothing | — | 2010-01-04..2026-09-04: 4124 expected, 4124 observed, 0 missing, 0 unexpected |
| expected (calendar says session, no bundle) | removed the declared holiday 2010-01-26 from the calendar, so it now calls that date a session | **YES** | missing went 0→1 and names 2010-01-26 |
| expected (calendar says session, no bundle) | withheld the real bundle date 2018-05-04 from the L0 sweep | **YES** | missing = 1, naming 2018-05-04 |
| served (bundle exists, calendar says closed) | added the phantom bundle date 2010-01-09 to the served set | **YES** | unexpected = 1, naming 2010-01-09 |

## 5. Whole-lake checksum audit

- **Payloads re-hashed:** 102,868
- **Defects:** 0

### The total is 102,868, not the 102,828 the brief expected

Not a defect and not a surprise — the brief's arithmetic subtracted the 40 already-in-L0 bundles from a baseline that never contained them. Decomposing the lake by source settles it exactly:

| | payloads |
|---|---:|
| every source except `nse_pr_bundle` | 98,744 |
| `nse_pr_bundle` | 4,124 |
| **total** | **102,868** |

The pre-campaign baseline of **98,744** is exactly the non-bundle count, so the baseline was struck before *any* PR bundle was in the authoritative lake — including the 40 that Phase 1's probing and bisection had already stored under their keys and that the campaign therefore reported as `already_in_l0`. 98,744 + 4,124 = **102,868**, which is what the sweep counted. Every payload in the lake carries a sidecar (payload and sidecar counts are equal for every source).

## 6. Stranded worktree lakes

`L0Store` resolves its root from `Settings.data_root`, default `<cwd>/data`, so a worktree that fetches without exporting `DATA_ROOT` builds a second lake inside itself. Deleting the worktree deletes whatever only that lake holds. Third occurrence on this box, so it is measured: every payload below was re-hashed from disk and looked for by logical key in the authoritative lake. **Nothing was deleted.**

| worktree lake | payloads | identical in authoritative L0 | absent | digest mismatch | orphan | verdict |
|---|---:|---:|---:|---:|---:|---|
| `/home/ubuntu/wt/w0-benchmark-tri/data` | 3 | 3 | 0 | 0 | 0 | **fully duplicated — safe to delete** |
| `/home/ubuntu/wt/w2-nse-pr-bundles/data` | 28 | 28 | 0 | 0 | 0 | **fully duplicated — safe to delete** |

**No payload is held only in a worktree lake.** Every stranded payload exists in `/home/ubuntu/stock-manager/data/L0` under the same logical key with an identical sha256.

## 7. Honest limits

1. **`Bc` starts 2010-01-04, not 2006.** The archive's floor is pinned by measurement, not bracketed: `PR311209.zip` and `PR010110.zip` are both 404, `PR040110.zip` is 200, and probes across 2005-2009 are all 404. **No dated corporate action exists before 2010-01-04 from this source**, so the pre-2010 stretch of the price history still has no point-in-time corporate-action surface, and a backtest reaching back further is adjusting on undated data whatever this wave acquired.

2. **Every member is symbol-keyed and none carries an ISIN.** ISIN is the only join key (invariant #2), symbols are reused across issuers over sixteen years, and the only symbol→ISIN resolver we hold (`EQUITY_L.csv`) is a present-day listing and therefore survivorship-biased. **Nothing in this corpus can be promoted to `corporate_actions`, to L1, or to a factor until W4 identity resolution exists.** The distinct-action count in §2 is a count of published rows, not of resolved securities: two issuers sharing a reused symbol with identical purpose text and identical dates would collapse into one, and the 18,149 distinct symbols include per-instrument debt codes (`ICIBK1107`) that are not securities in the D2 sense at all.

3. **`Ix` gives dated CNX 500 anchor points, not an index membership series — but `ffix` does give a series, for three years.** This limit is **restated**, not repeated: `Ix` is readable on 193 sessions in 2010 and nowhere else, with a rotating index set and no NIFTY 50, so as a *membership series* it is worth only what the original limit claimed. What §3c found is that a **different** member of the same bundle, `ffix`, carries daily NIFTY 50 / CNX 100 / CNX 500 membership with free-float weightage on 825 sessions over 2010-01-04..2013-04-30.

   So **`ops/BACKLOG.md:126` still stands, in narrowed form.** `index_constituents` is still empty, M9.3's as-of membership screen is still inert on the real store, and nothing in this wave promoted a single row — that is unchanged. What *is* no longer true is the premise that no dated membership history exists to fetch: 2010-01-04..2013-04-30 of it is in L0 as of today. Closing the backlog item still needs (a) a reader for the member, (b) W4 identity to turn its symbols into ISINs, and (c) a source for 2013-05 onward, which this archive does not publish. None of the three is in this task's scope, and each is now a smaller question than it was this morning.

---

## Appendix A — regenerating this report

Sections 0-7 above are generated verbatim by one read-only command. It opens no socket, no
database, and writes nothing but the file named by `--out`:

```text
uv run python -m dataplatform.ingest.nse.pr_bundle.survey \
  --data-root /home/ubuntu/stock-manager/data \
  --expect-l0-root /home/ubuntu/stock-manager/data/L0 \
  --from 2010-01-04 --to 2026-09-04 \
  --corp-actions-baseline 47887 --payload-baseline 98744 \
  --verify-l0 \
  --stranded-lake /home/ubuntu/wt/w0-benchmark-tri/data \
  --stranded-lake /home/ubuntu/wt/w2-nse-pr-bundles/data \
  --out ops/gates/pr-bundle-campaign-close-2026-09-08.md
```

`--expect-l0-root` is not decoration. The lake root resolves from `Settings.data_root`, whose
default is `<cwd>/data`, and a worktree-relative resolution has now built a second empty lake on
this box twice. The flag makes the run refuse with exit 2 rather than measure the wrong tree, and
the resolved root is printed to stderr before a single file is read. §6 is the same hazard
measured from the other end.

Everything above the generated body — the headline, this appendix — is written by hand and is the
only part that can drift from the lake.

## Appendix B — what this task did not do, and what it leaves behind

**Did not do, by instruction:** no promotion of any kind. No write to L1, Postgres, `sync_state`,
`corporate_actions`, `prices_raw`, or any quarantine or quality table; no import of
`dataplatform.store`'s write path; no join of any member against `EQUITY_L.csv` or the identity
master. No parser was written for a member that lacked one, and none of the three existing readers
was modified. No worktree was deleted.

**Did not do, by judgement, and flagging it:** the 18 parse failures in §0 are reader defects and
I left every one of them in place. Fixing a reader mid-measurement would have meant reporting
numbers produced by code that is not on `main`, and the point of a close-out is to say what the
shipped readers make of the corpus they were built for. The `ffix` census (§3c) is the one place I
read a member no reader covers, and it is deliberately a census — a header count and a
first-column count — rather than the fourth parser the brief told me not to write.

**Follow-ups this report generates**, in the order I would take them:

| # | what | evidence |
|---|---|---|
| 1 | **`ffix` is misregistered.** `MemberKind.FFIX`'s docstring says "Fixed income"; the payload is the free-float index constituent file. Correct the registry entry and the `bundle.py` module docstring that repeats it. | §3c |
| 2 | **Write an `ffix` reader and reassess `ops/BACKLOG.md:126`.** 825 sessions of dated NIFTY 50 / CNX 100 / CNX 500 membership with weightage are in L0. Still needs W4 identity to become ISIN-keyed, and still stops at 2013-04-30, but the item's premise — that no dated membership history exists to fetch — is now false for 2010-2013. | §3c, §7.3 |
| 3 | **`bc.py` refuses a whole member for one unquoted comma.** Eleven `Bc` members die on an 11-column row: an unquoted comma inside `SECURITY` (`Regular Income Bond, Opti…`, `ICIBK1107`, 2010-08-30 .. 2010-09-07) or inside `PURPOSE` (`INT DIV-RS 5, SPL DIV-RS …` for `TCS`, 2019-10-11 .. 15; `DIV - RS 2,50 PER SH` for `SURYAROSNI`, 2024-08-21). One malformed row currently costs a session's worth of dated actions. Routing the row to a quarantine and keeping the rest is the M2.1 shape. | §0 |
| 3b | **`bc.py` also refuses a legitimately empty member.** `Bc100122.csv` in `PR100122.zip` (2022-01-10) is **0 bytes as published** — NSE broadcast no actions that session. `_decode` raises "empty response body", which is right for a soft-404 and wrong for a real empty broadcast; the two are distinguishable (content length 0 versus markup) and are not distinguished. | §0 |
| 4 | **`ix.py` and `bc.py` let `_csv.Error` escape as itself.** `csv.reader` raises `_csv.Error`, not `ParseError`, so a caller catching the readers' documented error type still dies. Wrap it. | §0, 2013-02-19 |
| 5 | **⚠ 2018-01-02 has no bundle of its own — the archive served 2019's.** `PR020118.zip` and `PR020119.zip` are **byte-identical** (sha256 `553598a532395e01…`, 283,235 bytes both), and the 2018 payload's members are all named `020119`. `PrBundle`'s filename/member cross-check caught it; **the calendar reconcile structurally cannot**, because a file does exist under the key. So one of the 4,124 "bundles present" is a duplicate of another session, and the true count of distinct published bundles is 4,123. Worth one live re-probe of `PR020118.zip` to establish whether the archive still substitutes, and worth a general check for byte-identical payloads under different date keys. | §0, §4 |
| 5b | **Three more bundles cannot be dated from their own members** (2011-08-19 and 2013-06-04 carry a stale member from the previous session; 2013-01-10 prefixes every member with a `nupr100113/` directory, which the member-name regex does not expect). The refusal is correct in each case; a policy for reading the other 20 members of an undatable bundle is not written. | §0 |
| 6 | **`Bc` row volume drops after 2022** (413 rows/bundle in 2022, 139 in 2025, 345 in 2026) with no parse failure to explain it. Probably a real change in what NSE broadcasts; worth one confirmation before anyone reads a trend into the per-year action counts. | §2 |
| 7 | **Both stranded worktree lakes may be deleted** — measured, not assumed (§6). Do it with `git worktree remove`, and export `DATA_ROOT` in the next worktree so there is no fourth occurrence. |

## Appendix C — gate

`uv run make check` green in this worktree: `ruff format --check`, `ruff check`, `mypy --strict`
over `dataplatform`/`analyst`/`execution`/`backtest`/`accounting`/`tests`, and the full pytest
suite. Counts are in the pull request body.
