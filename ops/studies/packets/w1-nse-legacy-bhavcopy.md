# W1 task packet — NSE legacy bhavcopy backfill (the price spine)

**Status: PREPARED, NOT DISPATCHED.** Blocked on two gates, both listed in §0.
Prepared 2026-09-08 from `ops/studies/multi-fund-data-study-2026-09-07.md` §7 (wave W1) and
`ops/studies/evidence/india-acquisition-atlas.md`.

Dispatch target: `claude_code`, `purpose: implement`, `title: w1-nse-legacy-bhavcopy`.
Worktree `/home/ubuntu/wt/w1-nse-legacy-bhavcopy`, branch `w1/nse-legacy-bhavcopy`, based on `main`
**after** the W0 PRs are merged.

---

## 0. Gates that must clear BEFORE dispatch

1. **Owner sign-off on the request budget.** ~2,670 requests to one host.
   `AGENTIC_CONTEXT.md:88` reserves any bulk fetch over ~200 requests to the human. Phase 1 below is
   under that line and does *not* need sign-off; Phase 2 does.
2. **`w0/era-coverage` merged to `main`.** `dataplatform/ingest/data/nse_holidays.yaml` refuses date
   ranges it does not cover, and its coverage starts 2016-01-01. Until it reaches 2006, W1 cannot be
   planned, let alone run. This is a hard dependency, not a preference.
3. **The box is free.** One request budget, and it is local. Confirm `uptime` is quiet and
   `ps aux | grep -E 'backfill|campaign'` is empty. A W1 campaign wants ~2 uninterrupted hours; the
   4-core box tolerates nothing else heavy alongside it.

---

## 1. What W1 is

Fetch the daily NSE legacy bhavcopy for every trading session from **2006-01** up to where the lake
currently begins (**2016-09-01**), land it in L0 under the existing immutability contract, and
promote what is joinable into L1 `prices_raw`.

Measured facts this rests on (from ~113 live probes during the study, do not re-litigate):

- NSE bhavcopy reaches back to **1995-01-02**; the source register's `era.start: null` understates it.
- The **ISIN column appears on 2011-06-22** and is absent on 2011-06-21. A clean single-day cutover.
- The **production parser already handles the 2011-06-22 era**: run against the live payload during
  the study it returned `LEGACY_COLUMNS match: True`, `refused=0 rows=1502 state=NORMALIZED`,
  `isin='INE144J01019' symbol='20MICRONS' close=Decimal('46.65')`.
- The pre-2011 era (**E1**) has **11 columns and no ISIN**, and the parser refuses it on purpose
  (`bhavcopy_legacy.py:288-292`).
- ISINs are **reissued** — `20MICRONS` is `INE144J01019` in 2011 and `INE144J01027` in 2016. A
  long-span join needs ISIN *lineage*, not merely an ISIN column.

**Value: 2011-06-22 → 2026-09 is 15.2 years against today's 10.0 — a 52% span increase for zero new
parser code.** That is Horizon B, and it is the recommendation.

---

## 2. Phases

### Phase 1 — plumbing and smoke (no sign-off needed, <= ~20 requests)

- Build/extend the backfill driver so the campaign is **resumable and idempotent**: it must skip any
  session already present in L0 by key, never re-fetch, and never overwrite (L0 enforces this in
  three layers — mode `0o444`, writes refuse to overwrite, differing bytes at the same key raise).
- Register the era boundaries explicitly so the driver knows, per session, which era it is in.
- Freeze one real payload per era as a fixture under `tests/fixtures/<source>/<era>/`, and unit-test
  the parser against the fixtures. **Tests never hit the network.**
- Smoke-fetch ~10 sessions spread across the window (include 2011-06-21 and 2011-06-22 — the era
  boundary — plus a known holiday to confirm the 404 path), spaced >= 2.5 s.
- Open the PR at the end of Phase 1. Phase 2 is a driver run, not a code change.

### Phase 2 — the campaign (needs owner sign-off)

- Launch detached with a dated log: `nohup uv run python -m <driver> ... > ~/campaign/w1-$(date +%F).log 2>&1 &`
- Spacing >= 2.5 s/request per host. Browser-style UA. Respect robots.
- **Hard stop** on N consecutive unexpected errors (not on 404s, which are expected — see §3).
- Report progress into the log per session with `structlog` key-value: source, date, era, state.
- Commit each coherent green piece as you go. The runner has died mid-task seven times in this repo
  and taken the uncommitted tree with it every time.

---

## 3. Two behaviours that are easy to get wrong

**404 means the market was closed, and that is data.** The campaign's by-product is the exact
historical trading calendar, and it is the highest-authority source for it that exists. Requirements:

- A 404 is an expected outcome, recorded as `HOLIDAY_OR_NO_SESSION` evidence. It must **not**
  increment the error counter and must **not** trip the hard stop.
- Publish the full 404 date list as a report artefact.
- Where the 404 evidence disagrees with `nse_holidays.yaml`, **report the diff; do not silently
  patch the calendar.** A calendar correction is a separate, explicit commit citing the 404 evidence
  as provenance, and only after `w0/era-coverage` is on `main`. Never widen the calendar to make a
  fetch succeed.

**Era E1 (2006-01 → 2011-06-21) is quarantined, not refused and not resolved.**

- Fetch it and store the raw payload in L0 anyway. L0 is the immutable record, storage is cheap, and
  this is the one chance to avoid ever re-fetching.
- Its rows have no ISIN. Invariant #2 makes ISIN the only join key, so they **must not** enter L1
  `prices_raw`. Park them in the existing quarantine store — the repo already does exactly this for
  delivery rows (`prices_raw_quarantine` holds 1.79M rows and drops nothing).
- **Do NOT invent a symbol->ISIN mapping to rescue E1.** The only available resolver is a
  current-day listing (`EQUITY_L.csv`), and companies delisted before today are simply absent from
  it — so mapping through it is silently survivorship-biased in exactly the direction that matters.
  Resolving E1 is Horizon C, a separate funded decision (W4 identity work), not a side effect of W1.
- Publish the **unresolved-row count per year**. That number is the honest published bound on how
  far back the platform can claim to reach.

---

## 4. Acceptance contract

1. `make check` green in the worktree. `mypy --strict` on any new package.
2. Offline unit tests: parser against a frozen fixture per era; the era-boundary selection
   (2011-06-21 vs 2011-06-22 must route to different eras); the 404-is-a-holiday path; and
   idempotent resume (a second run over an already-fetched range performs zero fetches). At least one
   test must FAIL if the era boundary moves by a day.
3. L0: 1:1 payload-to-sidecar receipt, zero orphans, checksum verification clean over the new range
   (the existing lake is 96,097/96,097 with 0 defects — do not be the wave that breaks that).
4. L1 `prices_raw` extended for the ISIN era only. **No adjusted prices in L1** (invariant). No
   symbol-keyed join anywhere. `Decimal` for every price, never `float`.
5. No bare `except:`, no silent `pass` on an ingestion failure — a broken source must reach the
   status API, not a log line nobody reads.
6. Never commit `data/`. Never commit a credential. Repo is public: secret-scan the diff before push.
7. Report artefacts in the PR body: per-year coverage table (sessions expected / fetched / 404 /
   refused / quarantined), the 404 date list, the calendar diff vs `nse_holidays.yaml`, and the
   per-year unresolved-identity count.

---

## 5. File-scope notes

Owns: the NSE bhavcopy ingest/backfill driver, its fixtures and tests, era registration, and (only
after `w0/era-coverage` is merged, and only as an explicit evidence-cited commit)
`dataplatform/ingest/data/nse_holidays.yaml`.

Must not touch: `execution/costs/rates.yaml`, `backtest/run.py`, `source_register.yaml`,
`TASK_GRAPH.yaml`, `BUILD_STATE.json` — unless the W0 PRs are merged first, in which case the
register's `era.start` for NSE bhavcopy should be corrected to the measured 1995-01-02 with the ISIN
era noted at 2011-06-22.

---

## 6. Commits and PR

Message format `[<task-id>] <title>` with `Task:` and `Acceptance:` trailers. Find the owning task in
`TASK_GRAPH.yaml`; if none owns it, use `[W1]` and say in the PR body that a graph entry is needed —
off-graph commits under a bare module tag are a known problem in this repo (1,827 lines of forecast
code went in under a bare `[X2]`). Every commit ends with a blank line then exactly:

    Co-authored-by: omnigent <noreply@omnigent.ai>

Never force-push, never rewrite history. Push `w1/nse-legacy-bhavcopy` and open a PR against `main`
with `gh pr create`. **`gh` is installed but not authenticated on this box** — if PR creation fails
for that reason, push anyway and report
`https://github.com/ysaakpr/stock-manager/compare/w1/nse-legacy-bhavcopy` plus a ready-to-paste title
and body.

---

## 7. What W1 does not do

Nothing about the thematic problem. Sector classification, index membership and PIT fundamentals are
untouched by W1 — those are the gaps no amount of price fetching closes. W1 also does not make E1
*usable*, only *retained*. And it does not fix the five defects (D1, D2, D3, D9, D11) that are wrong
today at 10 years; deeper history makes several of them worse, not better.

**Do W1 for span. Do not let it be mistaken for progress on the fund manager's hard problems.**
