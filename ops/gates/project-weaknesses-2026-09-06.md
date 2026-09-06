# Project weaknesses — re-evaluated after the day's remediation

**Date:** 2026-09-06, evening · **Tree:** `5da147c` · **Scope:** the whole project this time, not
only ingestion — derived layers, System 2, execution, deployment, CI, backups, documentation.
Every claim below is a measurement made today against the laptop's lake and Postgres, the working
tree, or the server as observed through `ops/remote.sh exec`. Nothing is inferred from a docstring.

One finding is deliberately **not written here** because this repository is public. It is in the
session summary handed to the owner, and it is the first thing to act on.

---

## 0. What the morning's audit fixed, re-measured this evening

| Finding | Morning | Evening |
|---|---|---|
| N1 `/status/gaps` | HTTP 500 | 200, ten years in 1.1 s |
| N1 `/status/sources` | 27 MB / 65 s | 3.3 KB / 0.2 s |
| N1 `sync_state` distinct sources | 71,853 | 7 |
| N2 sessions absent from `prices_raw` | 2 | **0** — 2,471 partitions, calendar reports none missing |
| N3 quarantine readers | 0 | `/status/quarantine`, daily EOD check, step-change flag |
| N6 L0 sweeps ever run | 0 | 1 — 62,047 payloads, 260 s, zero defects; weekly job registered |
| N9 `/status/quality` | one number | grouped by check, `raised_today` beside it |
| N4 identity inputs | `tests/fixtures/` only | register rows, `--from-l0`, weekly job — **fetch still queued** |

Seven closed. Three fetches queued behind the server campaign (identity files, 536 XBRL documents,
the ETF-source probe). Two of today's own fixes have gaps — §1 below, first, because they are mine.

---

## 1. Defects in today's remediation (mine, small, measured)

**1.1 The host lease is taken by one driver out of six.** `leased_fetcher` is called only from
`ingest.identity_refresh`. `backfill.main`, `fundamentals_backfill`, `corp_actions_backfill`,
`eod.run_eod_pipeline` and both constituents runners still call `build_fetcher` directly — so the
two drivers that actually run campaigns take no lease, and `remote.sh run`'s new local-lease check
finds nothing to refuse on. P5.1 as shipped protects the weekly identity refresh and nothing else.
Fix: four call sites, one `with leased_fetcher([...hosts...])` each; the hosts come from each
source set's register rows.

**1.2 Migration 0008 breaks a pre-0008 driver mid-run.** Measured, in a rolled-back transaction:
an INSERT of `source = 'nse_xbrl_filing/IF99999999'` — exactly what the campaign code at `1d33c65`
writes — is **rejected** by `sync_state_source_is_a_lake_identifier`. The server is still on
`1d33c65` with that campaign running. If the owner syncs and runs `make migrate` before the driver
exits, its next checkpoint write fails, and so does the write that would have recorded the failure.
Nothing in `migrate.py` or `remote.sh` prevents that order of operations. Fix: `migrate.py` refuses
when a `dataplatform.ingest` process is running on the machine (the same `pgrep` the wrapper already
uses), plus the sequencing written into `ops/runbooks/move-campaign-to-a-server.md`. Until then:
**campaign exits → sync → migrate → next campaign**, never migrate under a running driver.

---

## 2. Structural — the platform has no operating process

**2.1 Nothing runs the scheduler.** The app container's command is `uvicorn dataplatform.status.api:app`
— the read-only status API. `dataplatform.scheduler run` exists as a verb and no compose service,
systemd unit or cron invokes it, on either machine. `job_run` holds **0 rows**; `/health` says
`scheduler: NEVER_RAN`. All four registered jobs — `eod_pipeline`, `constituents_snapshot`, and
today's `l0_verify` and `identity_refresh` — are schedules nothing will fire. The lake ends
2026-09-01 and 2026-09-02/03/04 were never fetched for exactly this reason. **Everything the plan
calls "daily" or "weekly" is currently a manual campaign.** Fix: a `scheduler` compose service
running `python -m dataplatform.scheduler run`, restart-always, with the heartbeat `/health`
already reads.

**2.2 The daily EOD job fetches one source.** `DAILY_NSE_SOURCES = ("nse_bhavcopy",)`. Delivery is
not in it, so the moment the scheduler does run, `deliv_qty` stops accumulating at the backfill's
last session; corporate actions, F&O, deals, announcements, shareholding, FII/DII and the
fundamentals feed are all campaign-only. The EOD gap check is likewise scoped to that one source,
which is why it never saw the 2,392 stuck filings.

**2.3 Every alert is a log line.** `alert_provider` defaults to `LOG`; neither machine's `.env`
overrides it (the server's has one key, `HTTP_MIN_INTERVAL_SECONDS=2.5`, inside the 2–3 s policy).
`EmailAlerter` and `TelegramAlerter` exist and are tested; neither is configured. So the CRITICAL
alerts wired today — L0 damage, a 403 spike, an EOD source left FAILED — reach a structured log
nobody tails. CLAUDE.md names this exact shape as a defect. Fix is a credential and one env var,
and is the owner's (B4).

**2.4 The price sentinel has never run.** `run_sentinel` has no caller outside tests. `quality_flag`
holds flags from exactly one check (`ca_reconciliation`); `unexplained_move` and `cross_exchange`
have never examined a production session. Ten years of `prices_raw` have not been sentinel-checked.

**2.5 System 2 has never made a decision outside a test.** `decision_journal`, `thesis` and
`policy_set` hold 0 rows. `analyst/` (16,724 lines) has one `__main__`-style entry (`cases/cli.py`)
and no scheduler job; the paper loop exists only inside `tests/integration/test_paper_run_10_sessions.py`
and the M5 gate report. The interlock *is* wired (`analyst/monitor/interlock.py` → `is_green`), the
kill switch is file-backed and latching and `recon` trips it, rails have property tests — the
machinery is real. What does not exist is an operated forward paper run, so **no paper track record
is accumulating**, and M8's real-money gate will have nothing behind it but the M5 report from
September 2.

---

## 3. Data — where the derived layers stand

**3.1 Two lakes, neither complete, no off-host copy.** Laptop L0 3.1 GB; server L0 4.5 GB and L1
593 MB. The server holds the fundamentals campaign's documents the laptop does not; the laptop holds
sessions the server's L1 was never derived for. Last backup on both: 2026-09-04 08:31 — before
migration 0008, before today's re-derivations. `ops/backup.sh` fingerprints L0 and copies nothing;
object storage is parked (owner, 2026-09-03). A disk failure on the server today loses the only
copy of a multi-hour campaign's raw documents.

**3.2 The laptop's `prices_raw` cannot hold BSE.** BSE L0 came home (536 payloads, verified) but
`dataplatform.ingest.backfill` always fetches — there is no price `--rebuild-from-l0` (recorded by
the other session today, M3.1 row). So "bring L0 home and rebuild" is true for filings and false for
prices; the server's L1 is the only one with BSE rows, and every BSE-dependent path (cross-exchange
reconciliation, the L2 primary-exchange choice) is untestable here.

**3.3 L2 is 252 ISINs with untraceable factors.** `adjustment_factors` holds 326 rows and **0 of
them carry a `corporate_action_id`**, against 9,573 corporate actions of which 506 are
price-affecting. The 1,227 `l2_invalidation` rows are all resolved now (the midday memory note
saying otherwise is stale). The adjusted layer exists, covers 3 % of traded ISINs, and cannot say
which action produced which factor. The ceiling is ISIN lineage ([memory: 95 % of ten-year splits sit
on retired ISINs]), not the factor arithmetic.

**3.4 The CA reconciliation queue is one fact repeated 2,487 times.** Every open
`ca_reconciliation` flag has `reason = SINGLE_SOURCE`: the action exists at NSE and there is no BSE
record to compare — which is true of **every** action, because `bse_corp_actions` has never been
fetched (its L0 tree is absent). A rule whose precondition is unmet has produced a permanently
saturated WARN queue since 2024-09-02, and any real `RATIO_MISMATCH` that appears will be one row
among 2,487. Either fetch the BSE side or make SINGLE_SOURCE a count, not a flag per action.

**3.5 Identity, restated.** D2 knows 2,397 of 7,536 traded ISINs. Of the 5,139 unknown, 593 are
`INF`-scheme and unreachable by any listing-history reconstruction; 2,998 are companies (delisted,
renamed) — Action 3's territory. 156 filings, 1,463 integrated-feed refusals and 536 never-fetched
documents sit behind this and the queued fetches.

**3.6 Register 30, lake 10.** 20 registered sources have never landed a byte; several have parsers,
fixtures and consumers that passed a gate against nothing (`nifty_tri_history` is FAILED and the
benchmark is a proxy computed from L1). `status: VERIFIED` means "the URL answered once in August".

**3.7 The calendar ends 2026-12-31.** Known; now four months away, and the first thing a working
scheduler will do in January is hard-fail every job that touches `expected_sessions`.

---

## 4. Verification and process

**4.1 CI is not the gate.** `.github/workflows/ci.yml` runs offline only: the 28 integration modules
— `sync_state`, the gap scanner against Postgres, the L1 writer, the paper run, the recon drill —
`pytest.skip` when the database is unreachable, so a green CI proves the unit half. The laptop is
the gate, by documented decision (`ops/runbooks/ci.md`). A Postgres service in the workflow would
close it.

**4.2 The gate excludes `orchestrator/`** (`extend-exclude` in `pyproject.toml`; known, M0.1).

**4.3 The repository's self-description lags the repository.** Found today alone: the M1.6 backlog
row saying delivery was never fetched (it was), "21 filings" (156), the M5.1 row asserting L0 is
swept proactively (it was not), the M4.10 row saying the backtest reads raw L1 (it reads L2 through
`QueryService` since M9.2), and a midday memory note saying 1,227 invalidations are unresolved (0
are). `BUILD_STATE.json` says 98 tasks / 97 DONE. None of these is individually dangerous; the
pattern is — each false belief survived because nothing re-measures a backlog row once written.

**4.4 Concurrent sessions commit to one branch.** `fef43b3` and `2f8bebf` landed at 15:14 today
while this session worked the same files. Nothing was clobbered — this time. There is no lock, no
convention, and no test that would notice a lost hunk in `ops/BACKLOG.md`.

---

## 5. Strengths that held up under the second look

Worth restating because they are what makes the rest fixable: L0 is intact (62,047 payloads,
re-hashed, zero defects) and genuinely immutable; the cost model has four dated regimes with
sources read on 2026-08-08; the interlock, kill switch, recon and rails exist and are tested at the
property level; replay determinism and the PIT-leak audit run in CI before anything else; the
backtest signal reads L2 through the query layer; and every failure in `sync_state` carries a
message specific enough that this evening's measurements took minutes.

---

## 6. Order of work

| | What | Why first |
|---|---|---|
| **0** | The finding not written here | See the session summary |
| **1** | §1.1 wire the lease into the four drivers; §1.2 `migrate.py` refuses under a running driver | Both are today's own gaps; both are ~30 lines; §1.2 guards the owner's very next action |
| **2** | §2.1 a `scheduler` compose service | Turns four schedules into a running platform; everything "daily" depends on it |
| **3** | §2.3 configure an alert channel | Otherwise 2 is a platform that fails silently |
| **4** | §2.2 add delivery (then CA) to `DAILY_NSE_SOURCES`; §2.4 call the sentinel from the EOD job | The daily job should land what the campaigns landed |
| **5** | §3.2 price `--rebuild-from-l0` | Makes "bring L0 home" true for prices; unblocks BSE locally |
| **6** | §3.4 SINGLE_SOURCE as a count, or fetch `bse_corp_actions` | A saturated queue watches nothing |
| **7** | §4.1 Postgres in CI | The gate stops being one laptop |
| **8** | The three queued fetches, in the order §4b of the ingestion audit gives | When the campaign exits |
