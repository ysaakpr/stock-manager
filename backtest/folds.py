"""X2: the round-2 walk-forward folds — configured once, as data, and checked on load.

``ops/studies/preregistration-signals-2026-09-29.md`` §2 fixes three expanding-window folds
anchored at 2012-07-04. Their dates live in ``backtest/folds.yaml`` and nowhere else: the
baseline-folds and round2-signals campaign units (``backtest.fold_campaign``) and the decision-rule
verdict (``backtest.decision_rule``) all read them from here, so a fold cannot be one thing in the
runner and another in the report.

**What loading checks** — the shape an expanding walk-forward must have, so a typo fails loud:
every selection window opens on the anchor; each test window opens after its own selection window
closes (no overlap, so the choice never sees the answer); test windows are in order and do not
overlap each other; and each fold's selection window ends where the previous fold's test window
ended (the window *expands* by exactly one test window).

What this module never does: read a clock, derive a date, or read the lake. Whether each window's
first session carries the full lookback is ``backtest.windows``'s check (the anchor is its derived
``full.start``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from backtest.windows import Window, WindowError

__all__ = ["FOLDS_PATH", "Fold", "FoldPlan", "load_folds"]

#: The checked-in fold configuration.
FOLDS_PATH = Path(__file__).with_name("folds.yaml")


@dataclass(frozen=True, slots=True)
class Fold:
    """One fold: the in-sample window an arm would be chosen on, and the window it is tested on."""

    name: str
    selection: Window
    test: Window


@dataclass(frozen=True, slots=True)
class FoldPlan:
    """Every fold, in order, and the anchor their selection windows share."""

    anchor: date
    folds: tuple[Fold, ...]

    def named(self, name: str) -> Fold:
        for fold in self.folds:
            if fold.name == name:
                return fold
        raise WindowError(f"no fold named {name!r}")

    @property
    def test_windows(self) -> tuple[Window, ...]:
        return tuple(fold.test for fold in self.folds)

    @property
    def selection_windows(self) -> tuple[Window, ...]:
        return tuple(fold.selection for fold in self.folds)


def _date(raw: object, where: str) -> date:
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        return date.fromisoformat(raw)
    raise WindowError(f"{where}: not a date: {raw!r}")


def _window(name: str, raw: Any) -> Window:
    if not isinstance(raw, dict):
        raise WindowError(f"{name}: expected a mapping with start/end")
    return Window(
        name, _date(raw.get("start"), f"{name}.start"), _date(raw.get("end"), f"{name}.end")
    )


def load_folds(path: Path = FOLDS_PATH) -> FoldPlan:
    """Load and check the fold configuration; ``WindowError`` on any violation of its shape."""
    doc: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise WindowError(f"{path}: not a version-1 fold configuration")
    anchor = _date(doc.get("anchor"), "anchor")
    raw_folds = doc.get("folds")
    if not isinstance(raw_folds, dict) or not raw_folds:
        raise WindowError(f"{path}: no folds")
    folds = tuple(
        Fold(
            name,
            _window(f"{name}-selection", raw.get("selection")),
            _window(f"{name}-test", raw.get("test")),
        )
        for name, raw in raw_folds.items()
    )
    for fold in folds:
        if fold.selection.start != anchor:
            raise WindowError(
                f"fold {fold.name}: selection opens {fold.selection.start}, not the anchor {anchor}"
            )
        if fold.test.start <= fold.selection.end:
            raise WindowError(
                f"fold {fold.name}: test opens {fold.test.start}, not after selection closes "
                f"{fold.selection.end}"
            )
    for earlier, later in pairwise(folds):
        if later.test.start <= earlier.test.end:
            raise WindowError(f"folds {earlier.name} and {later.name}: test windows overlap")
        if later.selection.end != earlier.test.end:
            raise WindowError(
                f"fold {later.name}: selection closes {later.selection.end}, not where "
                f"{earlier.name}'s test closed ({earlier.test.end}) — not an expanding window"
            )
    return FoldPlan(anchor=anchor, folds=folds)
