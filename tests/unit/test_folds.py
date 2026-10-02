"""X2 — the round-2 folds are exactly the pre-registration's, and a malformed one fails loud.

``ops/studies/preregistration-signals-2026-09-29.md`` §2 is binding: this test pins every date.
If it fails, the fold file was edited after registration — that is a new trial, not a fix.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from backtest.folds import load_folds
from backtest.windows import WindowError, load_windows


def test_the_folds_are_the_preregistered_ones() -> None:
    plan = load_folds()
    assert plan.anchor == date(2012, 7, 4)
    got = [
        (f.name, f.selection.start, f.selection.end, f.test.start, f.test.end) for f in plan.folds
    ]
    assert got == [
        ("F1", date(2012, 7, 4), date(2016, 8, 31), date(2016, 9, 1), date(2019, 8, 30)),
        ("F2", date(2012, 7, 4), date(2019, 8, 30), date(2019, 9, 2), date(2022, 8, 31)),
        ("F3", date(2012, 7, 4), date(2022, 8, 31), date(2022, 9, 1), date(2026, 8, 31)),
    ]


def test_the_anchor_is_the_full_windows_derived_start() -> None:
    assert load_folds().anchor == load_windows().full.start


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "folds.yaml"
    path.write_text("version: 1\nanchor: '2012-07-04'\nfolds:\n" + body, encoding="utf-8")
    return path


def test_a_selection_not_on_the_anchor_is_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "  F1:\n    selection: {start: '2013-01-01', end: '2016-08-31'}\n"
        "    test: {start: '2016-09-01', end: '2019-08-30'}\n",
    )
    with pytest.raises(WindowError, match="not the anchor"):
        load_folds(path)


def test_a_test_window_overlapping_its_selection_is_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "  F1:\n    selection: {start: '2012-07-04', end: '2016-09-01'}\n"
        "    test: {start: '2016-09-01', end: '2019-08-30'}\n",
    )
    with pytest.raises(WindowError, match="not after selection"):
        load_folds(path)


def test_a_window_that_does_not_expand_is_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "  F1:\n    selection: {start: '2012-07-04', end: '2016-08-31'}\n"
        "    test: {start: '2016-09-01', end: '2019-08-30'}\n"
        "  F2:\n    selection: {start: '2012-07-04', end: '2018-08-30'}\n"
        "    test: {start: '2019-09-02', end: '2022-08-31'}\n",
    )
    with pytest.raises(WindowError, match="expanding"):
        load_folds(path)
