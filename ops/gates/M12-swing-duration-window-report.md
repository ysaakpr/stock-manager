# The swing composite in multiple durations and multiple windows

> **This report has not been generated yet.** It is written by the command below, and this file is
> a placeholder standing on the deliverable's path so that its absence is visible rather than
> inferred. It carries no figures, because a gate report with numbers that did not come from a run
> is worse than no gate report at all.

**This is not the M12.3 gate.** `TASK_GRAPH.yaml` gives M12.3 the deliverable
`ops/gates/M12-strategy-verdict.md`, which `backtest/verdict.py` writes over M12.2's
twenty-three-arm sweep. This file answers the owner's separate request — the M10.7 swing composite
tried across multiple durations and windows — and neither replaces that deliverable nor discharges
its acceptance criteria. It does adopt one of them deliberately, because it is what the owner
asked to see: every arm whose verdict changes between windows is named.

The generator (`backtest/duration.py`) and its arm grid (`backtest.sweep.DURATION_ARMS`) are
covered by `tests/unit/test_sweep.py` and `tests/unit/test_duration.py`. What has *not* run is the
campaign: thirteen arms on two liquidity floors over four windows is 104 arm-runs, costing **four
shared lake passes plus sixteen further traversals**. The two momentum baselines take no shared
lake — `run_naive_momentum` and `run_momentum_v2` each build their own `_L1Reader` — so each
contributes one traversal per floor per window. This box was already running an M12 sweep when the
code was written, with CLAUDE.md capping it at two decade-scale replays in parallel; the wiring was
validated end to end on short windows instead.

## Generating it

Run it **from the worktree that holds `backtest/duration.py`**. The module does not exist on `main`
until this branch merges, so a run started from the primary checkout fails with `No module named
backtest.duration`. A worktree's own `data/` is empty, so the lake must be named explicitly; it is
opened read-only — L1 and L2 are read, nothing is written back.

```bash
cd <worktree root>
nohup uv run python -m backtest.duration \
  --data-root <path to the lake> \
  --report ops/gates/M12-swing-duration-window-report.md \
  > ~/campaign/duration-window-$(date +%F).log 2>&1 &
```

Once this branch merges, a run from the primary checkout needs neither the `cd` nor `--data-root`:
the default `data/` resolves there.

Run it uninterrupted — nothing else may compete with it for the CPU, the Postgres or the request
budget (CLAUDE.md, single-machine loop). Check `uptime` and the running drivers first: this box has
4 cores, and a second decade-scale replay alongside it is the ceiling.

**It renders straight off the in-memory sweep and persists nothing.** A report with wrong prose
cannot be re-rendered without re-running all four windows, so every defect that reaches the
markdown has to be fixed before launch, not after.

## What it will contain

- The four mandated windows (owner decision, 2026-09-07), each reported on its own and **never
  averaged**: the full decade `2016-09-01 → 2026-08-31`, the `2019-07-01 → 2026-08-31` window the
  repo's existing ~23 % figures came from, and the walk-forward pair that chooses on
  `2016-09-01 → 2021-08-31` and verifies on `2021-09-01 → 2026-08-31`. They overlap heavily, so
  they are four tables rather than four independent draws, and the report counts the overlapping
  pairs rather than asserting the relationship in prose.
- Per window: the setup, a ranked table at **both** liquidity floors (₹1 crore discovery, ₹10 crore
  reachable), and the holding-period arithmetic — net excess per book turn against the modelled
  0.45 % round trip, including the arms where it is negative.
- The walk-forward's selection rank beside its verification rank, with the winner named from the
  selection window alone and printed above any verification figure. Only the verification window is
  out-of-sample; the verdict is written from it.
- **Where the verdict changes with the window**: every arm clearing the bar on the six-year window
  but not the decade, and every arm clearing on walk-forward selection but not on verification.
- What each arm changed against its reference, the honest-limits section, and every run's digest.
- A plain answer on the owner's >25 % XIRR bar: on which window, on which floor, at which drawdown
  and at which duration — or the word **no**. A bar cleared only on an in-sample window is reported
  as *in-sample only*, not as cleared.
