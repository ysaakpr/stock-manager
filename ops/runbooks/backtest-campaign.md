# The after-tax backtest campaign

One command runs the whole measurement: **all 23 sweep arms × {full, decade, six-year} × {₹1 cr,
₹10 cr} median-turnover floors, plus the walk-forward split of the full span at both floors** —
230 backtests — with every run's fill ledger persisted and after-tax columns (realised and
liquidate-at-end XIRR, tax paid, for a resident individual) beside the pre-tax ones.

Driver: `backtest/campaign.py`. Windows: `backtest/windows.yaml`. Ledgers: `backtest/run_ledger.py`.
Tax: `backtest/tax.py` + `backtest/tax_report.py`.

## The windows

| Window | Start | End | Sessions | Why that start |
|---|---|---|---:|---|
| full | **2012-07-04** | 2026-08-31 | 3500 | first session with every arm's full lookback in ISIN-joinable L1 |
| decade | 2016-09-02 | 2026-08-31 | 2470 | the M12.2 decade |
| six-year | 2019-07-01 | 2026-08-31 | ~1760 | the window the ~23 % figures came from |
| walk-forward selection | 2012-07-04 | 2019-07-31 | 1750 | first half of full, by session count |
| walk-forward verification | 2019-08-01 | 2026-08-31 | 1750 | second half of full |

Joinable L1 history starts **2011-06-22**; the 2006–2011 bhavcopies have no ISIN and sit in
`prices_raw_quarantine`, which nothing reads. The full-span start is bound by the swing engine's
**260-print minimum history** (`_SWING_MIN_HISTORY`, every swing arm): the 260th NSE-EQ session
from 2011-06-22 is 2012-07-04. The next-longest lookbacks are the 12-1 lag (253 prints →
2012-06-25), the 52-week high (252 → 2012-06-22), the 365-day liquidity screen and 12-month
momentum reference (→ 2012-06-21), v2's 12 monthly vol points (→ 2012-06-18) and the regime
index's 200-session mean (→ 2012-04-11). Re-check any time the lake changes:

```bash
uv run python -m backtest.windows --data-root data
```

It exits 1 if any window's first decision lacks the full lookback, if `full` does not open on
exactly the first full-lookback session, or if the walk-forward is not the equal split of `full`.

## Before you start

- On `main`, clean tree (`git status`): the output directory is pinned to the commit.
- Postgres up (`make up`): each worker loads the reconciled corporate actions from it.
- Nothing else competing for the box (CLAUDE.md): `uptime`, and
  `ps aux | grep -E 'backfill|campaign|backtest' | grep -v grep` should show nothing.
- Choose the investor. Every assumption is a **required** flag — there are no defaults — and the
  report header prints them. The command below states a resident individual at the 30 % slab with
  total income under ₹50 lakh (no surcharge), tax paid at each FY end; change them to the investor
  you mean. They only affect rendering: the same runs can be re-rendered for another investor with
  `--reports-only` (below) without replaying anything.

## The command

```bash
OUT=~/campaign/after-tax-$(git rev-parse --short HEAD)
nohup uv run python -m backtest.campaign \
    --out "$OUT" --data-root data --workers 2 \
    --slab-rate 0.30 --cg-surcharge 0 --dividend-surcharge 0 --payment fy_end \
    > ~/campaign/after-tax-campaign-$(date +%F).log 2>&1 &
```

- `--workers 2` is the ceiling (the driver refuses more): each sweep keeps about two of the four
  cores busy. `--workers 1` runs the units one after another in the foreground process.
- Units of work are windows, longest first: `full`, `walk-forward` (selection then verification,
  in that order, inside one unit so the choice is frozen first), `decade`, `six-year`.
- `--out` must be outside `data/` (refused otherwise). Never commit anything under it.

**Resumable.** Every run writes `runs/<digest>.json` and `ledgers/<digest>.json` as it finishes,
atomically. Killed at any point — re-run **the same command**: finished runs are loaded, not
replayed; a second invocation over a finished campaign replays nothing and opens no lake. A run
whose arm *failed* persists nothing, so it is retried. Progress:

```bash
ls "$OUT/runs" | wc -l          # of 230
grep -c sweep.arm_done ~/campaign/after-tax-campaign-*.log
```

`manifest.json` pins the directory to the commit, the lake root, the last L1 session, the window
config and the corporate-action switch. Resuming with any of them changed is refused — use a
fresh `--out`. (A run's digest covers what it was asked, not the engine or the lake, so mixing two
commits' runs in one directory would compare two different engines in one table.)

## What it leaves

```
$OUT/manifest.json
$OUT/runs/<digest>.json        pre-tax metrics of each run (deterministic: no timings)
$OUT/ledgers/<digest>.json     each run's fills, dividend credits, splits/bonuses/reissues
$OUT/reports/sweep-full.md     ranked tables per floor, after-tax columns beside pre-tax
$OUT/reports/sweep-decade.md
$OUT/reports/sweep-six-year.md
$OUT/reports/verdict-walk-forward.md   the frozen choice, its decay, the bar, after-tax columns
```

Ranking stays on pre-tax XIRR / max drawdown (owner decision, 2026-09-07); the after-tax columns do
not re-order it. A row whose ledger could not be taxed (e.g. a pre-2018 lot with no 31-01-2018
bar in L1 for its grandfathered cost) shows `n/a` with the reason — never a pre-tax figure dressed
as after-tax.

Re-render for another investor (no replays; the ledgers are the input):

```bash
uv run python -m backtest.campaign --out "$OUT" --data-root data --workers 1 --reports-only \
    --slab-rate 0.30 --cg-surcharge 0.15 --dividend-surcharge 0.15 --payment self_assessment
```

Render at a **later commit** — a fix to rendering or tax after the runs finished — without
replaying anything:

```bash
uv run python -m backtest.campaign --out "$OUT" --data-root data --workers 1 --reports-only \
    --runs-from-commit <the manifest's commit> \
    --slab-rate 0.30 --cg-surcharge 0 --dividend-surcharge 0 --payment fy_end
```

The manifest guard exists so one table never holds two engines' runs; a render that replays
nothing cannot mix them. So this is accepted only when every manifest field but `commit` matches,
you name the directory's own commit, it is an ancestor of a clean HEAD, and **every** run is on
disk (a missing one would be replayed at HEAD — refused instead). The manifest is left as it was;
each report opens with a line naming both commits. Render into a copy (`cp -a "$OUT" "$OUT-render-<sha>"`)
if the original directory should stay exactly as the campaign left it.

One run's full after-tax report, per FY, with rate provenance:

```bash
uv run python -m backtest.tax_report "$OUT/ledgers/<digest>.json" \
    --slab-rate 0.30 --cg-surcharge 0 --dividend-surcharge 0 --payment fy_end
```

## How long it takes

Measured, not guessed: one arm (M10.7 swing composite), decade window, ₹10 cr floor, on this box —
**136.5 s** of replay after a **12.6 s** lake build (2470 sessions, 2 min 31 s wall including
Postgres corporate-action load and the after-tax pass; ~2.7 GB RSS; ~190 % CPU). That is
**≈ 0.055 s per replayed session per run**. Scaled by each unit's sessions × 46 runs
(23 arms × 2 floors):

| Unit | Sessions per run | Runs | Estimate |
|---|---:|---:|---:|
| full | 3500 | 46 | 2.5 h |
| walk-forward (selection + verification) | 1750 + 1750 | 92 | 2.5 h |
| decade | 2470 | 46 | 1.7 h |
| six-year | 1773 | 46 | 1.25 h |
| **serial total** | | **230** | **≈ 8 h** |

With `--workers 2` the units pack as full + decade (≈ 4.2 h) against walk-forward + six-year
(≈ 3.7 h): **≈ 4.5–5 h wall**, allowing 10–20 % for two sweeps sharing four cores, plus a few
minutes at the end to render the reports (every run loaded from disk, 230 ledgers taxed). Peak
memory ≈ 2 × 3–4 GB. The per-session rate is from one swing arm; the weekly-cadence arms and the
two momentum baselines (which build their own lake per run) were not timed separately, so treat
the figure as ±25 %. A resumed campaign costs only its unfinished runs; a finished one re-renders
in seconds.

## If something goes wrong

- **`error: ... was started by a different campaign (differs on: commit)`** — you resumed after a
  pull or on a dirty tree. Finish on the original commit, or start a fresh `--out`.
- **An arm row says `failed`** — the error is in the row and the log (`sweep.arm_failed`). Fix and
  re-run the same command; only the failed runs replay.
- **`after-tax ... not computed: no L1 bar for <ISIN> ... 2018-01-31`** — the lot needs a
  grandfathering FMV the lake does not have. The pre-tax figures stand; the after-tax cell is
  honest about why it is empty.
