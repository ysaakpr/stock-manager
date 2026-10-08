# M14.2 — Month-first close-all dates (the three refused M11.2 sessions)

**Date:** 2026-10-07 (IST) · **Branch:** `polly/macro-monthfirst-dates` · **Follow-up to:** M11.2
([gate](M11-index-valuation-backfill.md) §3, BACKLOG row M11.2) ·
**Status: code and evidence complete. The real L1 and `sync_state` are unchanged until the
post-merge run in §4.**

## 1. The defect

The M11.2 backfill refused 2023-04-06, 2023-04-10 and 2023-04-11. Those three `ind_close_all` files
print `Index Date` as `04-06-2023`, `04-10-2023` and `04-11-2023`. That is month-first, and their
neighbours `05-04-2023` and `12-04-2023` are day-first. The parser read only day-first, dated the
files to 2023-06-04, 2023-10-04 and 2023-11-04, and the runner correctly refused them as dated to
another session. Their bytes are in L0, and `sync_state` holds them FAILED and retryable.

The contents belong to the sessions named by the filenames. The Nifty 50 `Points Change` column
chains exactly from the 5th to the 12th: 17557.05 + 42.10 = 17599.15, + 24.90 = 17624.05,
+ 98.25 = 17722.30, + 90.10 = 17812.40. The day-first readings are a Sunday, a Saturday, and
months after the files were published.

## 2. The rule (`dataplatform/ingest/macro/index_valuation.py`, `_session_date`)

A row's date is read **month-first only when all three of these hold**:

1. The filename names a session (`ind_close_all_<DDMMYYYY>.csv`). That date comes from the
   trading calendar the URL was built from, never from the file.
2. The day-first reading is **not** that session.
3. The month-first reading **is exactly** that session.

When all three hold, the two readings differ and only one of them agrees with the date the file was
requested under, so nothing is guessed. In every other case the day-first reading stands, including
when the filename has no date in it. A file that day-first dates to another session is still
refused by the runner, exactly as before. A swap is logged once per file as the warning
`macro.index_valuation_month_first`.

## 3. Evidence

### Tests (`tests/unit/test_macro_backfill.py`, fixtures `tests/fixtures/nifty_index_close/month_first_2023/`)

The fixtures are the five L0 payloads for 2023-04-05, 06, 10, 11 and 12: byte-identical copies,
with SHA-256s equal to their L0 `.meta.json`, and a PROVENANCE file. No request was made.

| Test | What it pins |
|---|---|
| `test_month_first_files_publish_the_session_they_are_named_for` | Runner end to end over the five files: 5 published, 0 refused, Nifty 50 close per session, every `period_end` = the named session |
| `test_an_ambiguous_day_first_date_is_never_swapped` | `05-04-2023` named for 5 April stays 5 April. The same bytes under the 12th's name keep their day-first date. A filename without a date never swaps |
| `test_a_month_first_file_under_another_sessions_name_is_still_refused` | `04-06-2023` served for the 12th: refused, nothing written |

I mutation-checked the tests against two broken versions of the parser, then restored it:

- **Rule removed** (`if False:` in place of the month-first branch). `…publish_the_session…`
  FAILS: the runner refuses the three sessions with `file reports session 2023-06-04 / 2023-10-04 /
  2023-11-04`.
- **Swap whenever both readings are real dates.** Both `…publish_the_session…` and
  `…ambiguous_day_first…` FAIL: the 5th and the 12th would be dated 2023-05-04 and 2023-12-04.

### Whole-lake re-derive into scratch roots (real `data/L1` never written)

Each scratch data root held only a symlink to the real
`data/L0/nse_index_close_snapshot`, which was only read. `backfill.rederive` ran over the M11.2
window, 2012-10-01 .. 2026-10-05, with 3,468 planned sessions. The "old" run used the parser from
`main` @ `1822c5f` (`git archive` on `PYTHONPATH`); the "new" run used this branch.

| Run | Sessions written | Facts | Not in L0 | Refused |
|---|---|---|---|---|
| old (`1822c5f`) | 3,453 | 994,224 | 12 | 3 (the three month-first files) |
| new (this branch) | **3,456** | **995,331** | 12 | **0** |

- The old run reproduces the M11.2 gate's numbers exactly: 3,453 sessions, 994,224 facts, 12 sessions
  answered 404, 3 refused.
- Over the whole lake the month-first rule fired on **exactly 3 files**: 3 warnings, for the 6th,
  10th and 11th.
- `diff -rq root_old/L1 root_new/L1` reports **only** the three new partitions, `date=2023-04-06`,
  `date=2023-04-10` and `date=2023-04-11`. It reports no differing file, so **every one of the
  other 3,453 `part.parquet` files is byte-identical** between the old and the new code.
- Compared fact by fact (`series_id`, value, `period_end`, `l0_key`) with the real
  `data/L1/macro_series` for this source, read-only: 3,453 sessions are equal. The only differences
  are the three new sessions, with 369 facts each (1,107 in all = 995,331 − 994,224).
- New sessions: Nifty 50 close and P/E were 17599.15 and 20.72 on 2023-04-06, 17624.05 and 20.75 on
  2023-04-10, and 17722.30 and 20.86 on 2023-04-11. `period_end` = `release_date` = the session.

## 4. Post-merge: publish the three sessions (0 requests)

Run this on the server after the merge, from the main checkout. Check first that no other driver
holds `nsearchives.nseindia.com` (`ps aux | grep -E 'backfill|campaign'`), and do not start it
21:40–21:50 IST:

```bash
uv run python -m dataplatform.ingest.macro.backfill --from 2023-04-06 --to 2023-04-11 \
  --report ~/campaign/macro-monthfirst-$(TZ=Asia/Kolkata date +%F).md
```

The window plans exactly the three sessions; 2023-04-07 is Good Friday. All three are in L0, so the
runner reuses them, makes no request, writes the three partitions and drives each `sync_state` row
from FAILED (retryable) to PUBLISHED. Expected summary line: `3 sessions published, 0 resumed,
0 requests, 0 not published (404), 0 failed, 0 refused, 1107 facts`. The runner still takes the
archive host's lease; if another driver holds it, it exits 4 without doing anything.

`--report` is pointed away from `ops/reports/macro-backfill-latest.md` because that file is the
whole-window report and should not be replaced by a three-session one.

Optional whole-window confirmation, idempotent and also 0 requests. It rewrites this source's facts
in every partition, which §3 shows are byte-identical to the current ones:

```bash
uv run python -m dataplatform.ingest.macro.backfill --from 2012-10-01 --to 2026-10-05 --rederive
# expect: 3456 sessions rewritten, 995331 facts, 12 not in L0, 0 refused (0 requests)
```

## 5. Not in scope

- `dataplatform.ingest.indices.parse_close_snapshot` (the §4.1 computed-TRI reader of the same
  file) has its own `_index_date`, which reads `DD-Mon-YYYY` only. It would refuse every numeric
  archive date, these three included. It has no caller in `dataplatform/` today, so nothing is lost
  now. If it is ever wired to the archive files, it needs a numeric reader with this same rule.
- `TASK_GRAPH.yaml` and `BUILD_STATE.json` were not touched.
