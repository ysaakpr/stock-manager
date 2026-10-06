"""Round 2: the three H-arms resolve by label, and registering them moved no baseline digest (X2).

`round2-signals --arms` resolves labels from `backtest.sweep.ARMS`, so H2 and H3 are registered
there beside H1. Adding an arm to that tuple must not change any other arm's specification — above
all the two frozen baseline arms, whose digests `frozen-baseline.json` records and
`round2-signals` re-derives before it will start. The literals below were computed at `main`
59e8cf5 (the fold machinery, PR #26) with default contexts, before H2 or H3 existed on `main`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import backtest.fold_campaign as fc
from backtest.folds import load_folds
from backtest.sweep import (
    ARMS,
    H1_RESIDUAL_MOMENTUM,
    H2_BAND_HIT_AVOIDANCE,
    H3_RESIDUAL_AND_BAND_HIT,
    run_digests,
)
from tests.index_history_support import digests_without_index_membership

_H_LABELS = (H1_RESIDUAL_MOMENTUM, H2_BAND_HIT_AVOIDANCE, H3_RESIDUAL_AND_BAND_HIT)

#: `(start, end, label) -> digest` at main 59e8cf5, floor ₹10 crore, default contexts.
_BASELINE_AT_59E8CF5: dict[tuple[date, date, str], str] = {
    (date(2012, 7, 4), date(2016, 8, 31), "M10.7 + regime gate"): (
        "59231507d47786f7126d8625b833dde1a90b966e20be9b9a6f4dd58234b2156e"
    ),
    (date(2012, 7, 4), date(2016, 8, 31), "Swing composite (M10.7)"): (
        "475c657795d96d0ac04fb4179ec1ccd4be2f22ee636e4f04b5b590c37fa6151b"
    ),
    (date(2016, 9, 1), date(2019, 8, 30), "M10.7 + regime gate"): (
        "3056c55417be64e4c1170e80e4775f3f8be9250146adac61b047ee1a7216f013"
    ),
    (date(2016, 9, 1), date(2019, 8, 30), "Swing composite (M10.7)"): (
        "1bf1e0c2e6c789688daa28240694e7b180122c340867e63874007ac529e61de8"
    ),
    (date(2022, 9, 1), date(2026, 8, 31), "M10.7 + regime gate"): (
        "153c71f95d64dd599ab7fcaf351219733472e78920d4c6a98a50fdbe37506c3d"
    ),
    (date(2022, 9, 1), date(2026, 8, 31), "Swing composite (M10.7)"): (
        "39e8225a3626d9f99124d695eed23fc3dffcd9adac4d1d7cc4b90c8ff7e9f6ba"
    ),
}


def test_round2_plan_resolves_all_three_h_labels(tmp_path: Path) -> None:
    plan = fc.round2_plan(
        tmp_path, load_folds(), list(_H_LABELS), data_root=None, book_actions=False
    )
    assert [arm.label for arm in plan.arms] == list(_H_LABELS)
    by_label = {arm.label: arm for arm in plan.arms}
    assert not by_label[H1_RESIDUAL_MOMENTUM].band_hit_avoidance
    assert by_label[H2_BAND_HIT_AVOIDANCE].band_hit_avoidance
    assert by_label[H3_RESIDUAL_AND_BAND_HIT].band_hit_avoidance


def test_the_baseline_arms_digests_are_byte_identical_to_main_59e8cf5() -> None:
    """Nothing but the point-in-time index membership key moved the frozen baseline's digests.

    That key moved them on purpose: the frozen ledgers were replayed under the snapshot-era screen
    (a no-op before 2026-09), so they must never be resumed as if they were today's reading.
    """
    baseline = tuple(arm for arm in ARMS if arm.label in fc.BASELINE_LABELS)
    assert {arm.label for arm in baseline} == set(fc.BASELINE_LABELS)
    for start, end in sorted({(s, e) for s, e, _ in _BASELINE_AT_59E8CF5}):
        before = digests_without_index_membership(
            start=start, end=end, arms=baseline, floors=(fc.FLOOR,)
        )
        now = run_digests(start=start, end=end, arms=baseline, floors=(fc.FLOOR,))
        for label in fc.BASELINE_LABELS:
            pinned = _BASELINE_AT_59E8CF5[(start, end, label)]
            assert before[(label, fc.FLOOR)] == pinned, label
            assert now[(label, fc.FLOOR)] != pinned, label


def test_the_three_h_arms_have_three_distinct_digests_none_a_baselines() -> None:
    start, end = date(2016, 9, 1), date(2019, 8, 30)
    arms = tuple(arm for arm in ARMS if arm.label in _H_LABELS or arm.label in fc.BASELINE_LABELS)
    digests = set(run_digests(start=start, end=end, arms=arms, floors=(fc.FLOOR,)).values())
    assert len(digests) == len(_H_LABELS) + len(fc.BASELINE_LABELS)
