# M14.1 — point-in-time index membership before 2016-10-24 (2026-10-07)

**Result.** The history now reaches back to 2014-03-28 for NIFTY 50, Next 50, 100 and 200, and to
2015-03-28 for NIFTY 500. Midcap 150 and Smallcap 250 now start at the first release that names
each one. Built offline from the 417 releases already in L0; no request was made, and L0 was only
read.

| Index | Published build (`date=2026-10-06`) | M14.1 build | Bounded by |
| --- | --- | --- | --- |
| NIFTY 50 | 2016-04-01 | **2014-03-28** | `ind_prs27022014.pdf` — columns printed apart (the truly detached layout) |
| NIFTY Next 50 | 2016-10-24 | **2014-03-28** | `ind_prs27022014.pdf` — same |
| NIFTY 100 | 2016-10-24 | **2014-03-28** | `ind_prs27022014.pdf` — same |
| NIFTY 200 | 2016-10-24 | **2014-03-28** | `ind_prs27022014.pdf` — no effective date it can read |
| **NIFTY 500** | 2016-10-24 | **2015-03-28** | `ind_prs20022015.pdf` — the text layer prints `IL&FS ENGG` (symbol `IL&FSENGG`), which resolves to no ISIN; quarantined, not guessed |
| NIFTY Midcap 150 | 2016-10-24 | **2016-08-12** | `ind_prs12082016.pdf` — the first release in L0 naming the index |
| NIFTY Smallcap 250 | 2016-09-30 | **2016-04-22** | `ind_prs22042016.pdf` — the first release in L0 naming the index |

Before each start the history answers `None`. Every segment has the index's fixed size once
explained windows are counted out: **0 unexplained off-size segments in all seven indices** (the
report's "Off-size (unexplained)" column is `(0)` throughout). Without the first-naming floor
(rule 5) Midcap 150 and Smallcap 250 would have claimed 2012 starts, with 20 and 46 off-size
segments.

## 1. What was actually wrong

The DQ-5.1 gate (`index-membership-history-pre-2021-2026-10-05.md`) named `ind_prs17102016`,
`ind_prs12082016` and `ind_prs22022016_2` "detached layout". **They are not detached.** Their
tables are headed `Sr. No. Scrip Name Symbol`. The table-header pattern matched only `Company
Name`, so no table opened, every statement looked tableless, and the parser reported its
detached-layout message. One alternation fixes it (`(?:company|scrip)\s+name`).

Reading back across those three releases then exposed four more holes. Each is closed narrowly,
and each change was diffed release by release against the old parser over all 417 releases
(below):

1. **`1.` headings.** `ind_prs22042016.pdf` numbers its sections `1. Nifty 500 Index`, and
   `ind_prs18112014`, `ind_prs21012015` and `ind_prs23012015` do the same. The heading pattern only
   took `1)`, so those NIFTY 500 / 200 / 100 / Next 50 sections were dropped and nothing reported
   it. Headings now take a full stop after digits only, so `a. Market capitalisation …` is still
   prose.
2. **One intro, several effective dates.** `ind_prs23012015.pdf`: "The changes in CNX 200, CNX
   500, CNX Midcap and CNX Alpha index shall be effective from February 2, 2015 … and change in
   Nifty Midcap 50 index shall be effective from February 23, 2015". The walk dated every section
   by the nearest date stated before it, which was Feb 23. Now, when the text before the first
   heading states two or more distinct dates *and names tracked indices*, each section takes the
   date of the clause that names it. A section that no clause names, or that two clauses with
   different dates name, gets no date and ends up unparsed. Nothing is guessed. Intros that name
   no index ("on February 16 IISL had announced … effective March 31 … rescheduled … effective
   from March 16", `ind_prs07032017`) keep the nearest-date rule unchanged. Over all 417 releases
   this changes exactly one release's events: `ind_prs23012015`, from Feb 23 to Feb 2.
   `ind_prs17102016` (parts A and C Nov 15, part B Oct 24) dates its tracked part-C sections
   2016-11-15 through the same rule.
3. **Silent drops.** `ind_prs27022014.pdf` is the genuinely detached text layer. Page 1 prints the
   names under truncated statements ("The following companies"), then the statements' tails, then
   the symbols in a column of their own. Its CNX Nifty and Junior sections produced no statement,
   no rows and no problem, so they vanished, and the NIFTY 50 walk crossed the 2014-03-28 change
   (JPASSOCIAT, RANBAXY out; TECHM, MCDOWELL-N in) without applying it. The independent
   reconciliation in §3 caught this. A tracked section whose text speaks of a change but yields no
   events is now unparsed ("no change could be read from the section"). "No changes are being
   made in Nifty 50" (`ind_prs23022026`) and bodyless headings (a criteria table's
   `1. NIFTY 500`, `ind_prs22082017`) are exempt.
4. **A voided release bounds nothing.** `ind_prs19032020`'s NIFTY 200 paragraph is prose (rule 3
   now flags it). That release is declared null and void for every index but NIFTY 50 by
   `ind_prs13052020` (already in `_VOIDINGS`), so its unread sections no longer cut any depth.

And one builder rule:

5. **No walk past an index's first naming release.** With the 2016 releases readable, Midcap 150
   and Smallcap 250 walked back to 2012, years before those indices existed, and showed 20 and 46
   unexplained off-size segments. Coverage now never precedes the first release in L0 that names
   the index (`before_first_named` residual).

## 2. Releases parsed vs exceptions (all 417 candidates in L0)

| | Old parser | M14.1 parser (`84bfafc`) | After review hardening (`ec8b494`) |
| --- | --- | --- | --- |
| Fully parsed, with tracked events | 173 | 180 | **122** |
| Read, no tracked change | 183 | 144 | 144 |
| With at least one unparsed tracked section (an exception) | 58 | 90 | **148** |
| `ParseError` | 3 | 3 | 3 |
| Tracked events | 4,897 | 5,386 | **5,082** |

The extra exceptions are honest ones. First, sections the old parser dropped without a word are
now listed. Second, after review, a table row that prints no symbol (pre-2011 tables, and
statements truncated by the text layer, e.g. `ind_prs07112013`, which had read Indiabulls Housing
as an *exclusion*) makes its section unparsed instead of yielding symbol-less events. That second
change touched 70 releases, all announced on or before 2013-11-07: 0 events gained and 304 lost
(303 symbol-less, plus one 2005 row whose "symbol" was an industry word). It changes no membership
interval: the rebuilt history is byte-identical before and after. **146 of the 148 exceptions are
announced on or before 2013-11-07**, below every coverage start. The other two:

- `ind_prs27022014.pdf`: CNX Nifty and Junior "no change could be read" (columns apart), CNX 100
  "detached layout", CNX 200 / 500 "no effective date". **This is the bound** for NIFTY 50, Next
  50, 100 and 200.
- `ind_prs19032020.pdf`: NIFTY 200 "no change could be read" (prose). Voided by
  `ind_prs13052020`, so it bounds nothing (rule 4).

The `ParseError`s are `ind_prs02032012` (soft-404 HTML), `ind_prs20062005_1` (AES-encrypted) and
`ind_prs23082021` (image-only, read from its curated transcription, unchanged). Inside coverage,
**0 events are quarantined.** The one unresolved event that bounds coverage is NIFTY 500's
`IL&FS ENGG` row (2015-03-27).

Releases that M14.1 made readable, with their tracked events: `ind_prs22022016_2` (288),
`ind_prs12082016` (146), `ind_prs17102016` (10), `ind_prs22042016` (+6, its NIFTY 500 section),
`ind_prs18112014` (8), `ind_prs21012015` (10), `ind_prs23012015` (4), plus 2010-2011 rows below
coverage.

Review hardening (`ec8b494`), with no in-coverage effect: a `1.` line opens a section only when
its name is an index (numbered company rows and prose such as "1. Sundaram Finance Ltd.: On
account of …" do not). An intro that names an index *after* its last date (date-first) leaves
every index it names undated rather than pairing it with the next clause's date.

## 3. Reconciliation against independent evidence

**The exchange's own per-session flags.** L1 `pr_security_marks` carries the NSE PR bundle's
`nifty50_flag` and its `NIFTY Next 50 Sec` section, per ISIN per session. They are independent of
the press releases. Rebuilt membership (effective on the session) vs the flagged set:

| Index | Span | Sessions | Exact-match sessions | Member-session agreement |
| --- | --- | --- | --- | --- |
| NIFTY 50 | **added: 2014-03-28 → 2016-03-31** | 492 | **492 (100.00%)** | **24,600 / 24,600 (100.000%)** |
| NIFTY 50 | old: 2016-04-01 → 2026-10-01, M14.1 build | 2,602 | 2,205 (84.74%) | 130,037 / 130,795 (99.420%) |
| NIFTY 50 | old span, published build | 2,602 | 2,205 (84.74%) | 130,037 / 130,795 (99.420%) |
| Next 50 | **added: 2015-11-09 → 2016-10-21** (the section is printed from 2015-11-09) | 234 | **234 (100.00%)** | **11,700 / 11,700 (100.000%)** |
| Next 50 | old: 2016-10-24 → 2026-10-01, M14.1 build | 2,465 | 1,602 (64.99%) | 118,003 / 127,320 (92.682%) |
| Next 50 | old span, published build | 2,465 | 1,587 (64.38%) | 117,988 / 127,335 (92.660%) |

The added span agrees on every session. The old-span disagreements are the same in both builds:
they come from the PR bundle's flags (e.g. 2021-03-31 → 08-06 and 2022-03-31 → 08-01 lag a review
by months) and are out of scope here. The first version of this build did not have rule 3. It
reached NIFTY 50 to 2013-04-01 and failed this check on 2013-04-01 → 2014-03-27 (248 sessions, the
2014-03-28 change missing), which is how the silent drop in `ind_prs27022014` was found.

**The published build, day by day** (PIT sets, effective and knowable, on every session from each
index's old start; 17,427 index-sessions): **75 differ, all one correction.** From 2016-10-24 to
2016-11-11 (15 sessions × 5 indices), the published build had Cairn India already replaced (by
Havells in Next 50 / 100, Crompton Greaves Consumer in 200 / 500 / Midcap 150). It started there
because `ind_prs17102016` was unread. The release makes the change effective 2016-11-15, and the
exchange's Next 50 section agrees with the new dating on all 15 sessions (the +15 exact sessions
above). NIFTY 50 and Smallcap 250: 0 differences.

**The published `index_change_events`.** 4,575 published rows, 5,054 rebuilt. Every published row
is present except 3 NIFTY 500 rows from 2011 (`ind_prs19042011`, `ind_prs12052011`). These were
quarantined in the published build and now resolve to ISINs, because L1 identity evidence for 2011
has grown since 2026-10-06; this change has nothing to do with it. The 482 new rows come from 13
releases: the seven listed in §2, and six from 2010-2011, below every coverage start.

## 4. Build (verification only — the lake was not written)

    DATA_ROOT=/home/ubuntu/stock-manager/data uv run python -m \
        dataplatform.ingest.index_history_backfill --build-only --as-of 2026-10-06 \
        --report data/m14-scratch/v4/report.md --write-l1 data/m14-scratch/v4

Run from the worktree. `data/` is gitignored there. It reads L0 and L1 identity from the lake and
writes `index_membership_history` (7 files) and `index_change_events` under
`data/m14-scratch/v4/L1/…/date=2026-10-06/` in about 52 s. The as-of matches the published build,
so the two compare like for like.

## 5. Post-merge publish

`date=2026-10-06` in the lake already holds the published build and is write-once
(`ImmutableHistoryError`). The extended history therefore lands as the next dated build. That
needs a listing and anchor capture for a newer date. The anchors for 2026-10-07 are in L0, but the
listing is not. The weekly `index_press_refresh` job captures both on **Saturday 2026-10-10 at
09:00 IST**. After it has run and this PR is merged, run from the main checkout:

    DATA_ROOT=/home/ubuntu/stock-manager/data uv run python -m \
        dataplatform.ingest.index_history_backfill --build-only --as-of 2026-10-10 \
        --report ~/campaign/index-history-2026-10-10.md \
        --write-l1 /home/ubuntu/stock-manager/data

It makes no request. `read_membership_history` / `members_asof` take the latest dated build, so
readers switch with no further change. Check the report: coverage per §header, and `(0)`
unexplained off-size segments for every index.

## 6. Residuals carried, and one correction to the DQ-5.1 note

- **Tata Motors DVR in NIFTY 500.** `ind_prs22022016_2` includes TATAMTRDVR in NIFTY 500 effective
  2016-04-01 (its 76th inclusion against 75 exclusions). No release in L0 names its NIFTY 500 exit
  (`ind_prs10062020` removes it from NIFTY 100 / 200 only). The walk reports
  `include_not_held_later` and leaves the DVR line **out** of NIFTY 500 from 2016-04-01 onwards,
  rather than inventing an exit date. The ordinary Tata Motors share is a member throughout. The
  DQ-5.1 note's claim that the DVR "was never in … NIFTY 500, per the releases" was wrong: it was
  read from releases that did not include `ind_prs22022016_2`.
- Carried unchanged: TATAMTRDVR's 2023-09-29 NIFTY 100 / 200 inclusion (`include_not_held_later`),
  and the spin-off `entry_before_first_session` clips.

## 7. Tests

- `tests/unit/test_index_changes.py`, with new frozen fixtures (`PROVENANCE.md` each, sha256 equal
  to the L0 sidecars):
  - `2016_scrip_name/ind_prs17102016.pdf`: the exact (index, action, symbol, effective) set, all
    2016-11-15.
  - `2015_dated_clauses/ind_prs23012015.pdf`: Feb 2, not Feb 23; `1.` headings.
  - `2014_columns_apart/ind_prs27022014.pdf`: no events, and every tracked section unparsed.
  - Hand-written text-layer lines: the reviewer's exact numbered rows and sentences are not
    headings; a numbered sentence inside NIFTY 500 does not close it; a made-up date-first intro
    leaves both sections unparsed (on `84bfafc` it dates NIFTY 500 by the next clause); a row
    without a symbol makes its section unparsed.

  Mutation-checked. Reverting the Scrip Name header, the clause dating, the full-stop headings or
  the silent-drop guard each fails a named test, and so does inverting include/exclude.
- `tests/unit/test_index_history.py`: the first-naming floor, and "a voided release's unread
  section bounds nothing" (pure helpers, offline). There is also an offline end-to-end
  `build_membership_history` run over a tmp L0 (listing, seven anchors, two releases) that fails
  if the floor is unwired.
- `tests/golden/test_index_history_golden.py` (lake-backed): the new coverage per index and
  `None` the day before. New facts: NIFTY 200 ARVIND → CRISIL on 2015-02-02 (and held on
  2015-02-20); NIFTY 50 on 2016-04-01; Next 50 CAIRN → HAVELLS on 2016-11-15 and not on part B's
  2016-10-24. NIFTY 50 / Next 50 must equal the exchange's per-session flags on all 492 / 234
  sessions of the added span. Sizes on every segment; NIFTY 100 = 50 ∪ Next 50 from 2014-03-28;
  NIFTY 500 = 100 ∪ 150 ∪ 250 once all three exist.

`TASK_GRAPH.yaml` and `BUILD_STATE.json` are untouched. A later bookkeeping PR adds M14.x.
