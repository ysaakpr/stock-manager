# M12.3 — The swing composite in multiple durations and multiple windows

> **This report has not been generated yet.** It is written by the command below, and this file is
> a placeholder standing on the deliverable's path so that its absence is visible rather than
> inferred. It carries no figures, because a gate report with numbers that did not come from a run
> is worse than no gate report at all.

The generator (`backtest/duration.py`) and its arm grid (`backtest.sweep.DURATION_ARMS`) landed
under M12.3 and are covered by `tests/unit/test_sweep.py` and `tests/unit/test_duration.py`. What
has *not* run is the campaign itself: twelve arms on two liquidity floors over four windows is 96
arm-runs across four lake passes, and this box was already running the M12.2 decade sweep when the
code was written, with CLAUDE.md capping it at two decade-scale replays in parallel. The wiring was
validated end to end on a short window instead (see the PR).

## Generating it

From the repo root, where the lake's default `data/` resolves without an override:

```bash
nohup uv run python -m backtest.duration \
  --report ops/gates/M12-swing-duration-window-report.md \
  > ~/campaign/duration-window-$(date +%F).log 2>&1 &
```

From a worktree, whose own `data/` is empty, add `--data-root <path to the lake>`. The lake is
read-only for this command: it opens L1/L2 and writes nothing back.

Run it uninterrupted — nothing else may compete with it for the CPU, the Postgres or the request
budget (CLAUDE.md, single-machine loop). Check `uptime` and `ps aux | grep -E 'sweep|backfill|campaign'`
first: this box has 4 cores, and a second decade-scale replay alongside it is the ceiling.

## What it will contain

- The four mandated windows (owner decision, 2026-09-07), each reported on its own and **never
  averaged**: the full decade `2016-09-01 → 2026-08-31`, the `2019-07-01 → 2026-08-31` window the
  repo's existing ~23 % figures came from, and the walk-forward pair that chooses on
  `2016-09-01 → 2021-08-31` and verifies on `2021-09-01 → 2026-08-31`.
- Per window: the setup, a ranked table at **both** liquidity floors (₹1 crore discovery, ₹10 crore
  reachable), and the holding-period arithmetic — net excess per book turn against the modelled
  0.45 % round trip, including the arms where it is negative.
- The walk-forward's selection rank beside its verification rank, with the winner named from the
  selection window alone and printed above any verification figure.
- What each arm changes against its named reference, the honest-limits section, and every run's
  digest.
- A plain answer on the owner's >25 % XIRR bar: on which window, on which floor, at which drawdown
  and at which duration — or the word **no**.
